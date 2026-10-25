"""Train the punctuality model from observed Indian Railways delays.

Input is real: 1,282,325 per-stop delay observations covering 3,892 trains for
September 2024, from the RSTGCN dataset (KoyenaChowdhury/RSTGCN, derived from
Indian Railways operational data; no licence declared, used here as research data
with no redistribution of the raw file - `data/raw/` is gitignored).

What this produces, and why in this shape:

- `data/delay-model.json` holds a small logistic model plus per-train observed
  statistics. Inference reads that JSON with the standard library only, so the
  server has no ML dependency and the demo runs offline. Training may use numpy
  and scikit-learn; serving may not.
- The split is by DATE, not by row. Delays propagate along a route, so a random
  split lets the model see the same train-day in both halves and reports a fantasy
  AUC. Training uses 1-21 September, validation 22-30 September.
- Per-train priors are computed on the training weeks only and then used as a
  feature. Computing them on the whole month would leak the answer into the input.
- The target is the operational definition Indian Railways publishes: a train is
  late if it arrives more than 15 minutes after schedule.

Run:  .venv/bin/python scripts/build_delay_model.py
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import sys
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
ZIP = ROOT / "data" / "raw" / "rstgcn.zip"
OUT = ROOT / "data" / "delay-model.json"
DELAYS_CSV = "Indian-Railway-Network-and-Delays/train_routes_delays_Sep2024.csv"
ROUTES_CSV = "Indian-Railway-Network-and-Delays/train_routes_Sep2024.csv"

#: Indian Railways counts a train as on time if it arrives within 15 minutes.
LATE_THRESHOLD_MIN = 15.0

#: Split dates. The last nine days of the month are never trained on.
TRAIN_UNTIL = dt.date(2024, 9, 21)
VALIDATE_FROM = dt.date(2024, 9, 22)

MODEL_VERSION = "logit-2024.09.1"


@dataclass
class TrainDay:
    """One train on one date: what we know before the journey, and what happened."""

    train: str
    date: dt.date
    destination_delay: float | None = None
    departure_hour: int | None = None
    distance_km: float = 0.0
    stops: int = 0
    delays: list[float] = field(default_factory=list)


def load_routes(zf: zipfile.ZipFile) -> dict[str, dict[str, float]]:
    """Train number to its schedule shape: distance, stop count, departure hour."""
    routes: dict[str, dict[str, float]] = {}
    with zf.open(ROUTES_CSV) as handle:
        for row in csv.DictReader(io.TextIOWrapper(handle, "utf-8")):
            number = str(row.get("trainNumber", "")).strip()
            if not number:
                continue
            entry = routes.setdefault(number, {"distance": 0.0, "stops": 0, "hour": -1.0})
            entry["stops"] += 1
            distance = _float(row.get("distance"))
            if distance is not None:
                entry["distance"] = max(entry["distance"], distance)
            if entry["hour"] < 0:
                hour = _hour(row.get("departureTime") or row.get("arrivalTime"))
                if hour is not None:
                    entry["hour"] = float(hour)
    return routes


def load_train_days(zf: zipfile.ZipFile) -> dict[tuple[str, dt.date], TrainDay]:
    """Every train-day in the file, with the delays observed along its route."""
    days: dict[tuple[str, dt.date], TrainDay] = {}
    with zf.open(DELAYS_CSV) as handle:
        for row in csv.DictReader(io.TextIOWrapper(handle, "utf-8")):
            train = (row.get("train") or "").strip()
            date = _date(row.get("date"))
            if not train or date is None:
                continue
            key = (train, date)
            day = days.get(key)
            if day is None:
                day = TrainDay(train=train, date=date)
                days[key] = day
            delay = _float(row.get("arr_delay"))
            if delay is None:
                continue
            day.delays.append(delay)
            # The last observed stop is the destination for our purposes: that is
            # the arrival a passenger cares about, and it is what the on-time
            # statistic counts.
            day.destination_delay = delay
    return days


def _float(value: object) -> float | None:
    try:
        text = str(value).strip()
        return float(text) if text else None
    except (TypeError, ValueError):
        return None


def _date(value: object) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def _hour(value: object) -> int | None:
    """'08:00 AM' to 8, in 24-hour form."""
    text = str(value or "").strip().upper()
    if not text:
        return None
    try:
        parsed = dt.datetime.strptime(text, "%I:%M %p")
    except ValueError:
        return None
    return parsed.hour


def build_rows(
    days: dict[tuple[str, dt.date], TrainDay],
    routes: dict[str, dict[str, float]],
) -> list[tuple[dt.date, str, list[float], bool, float]]:
    """One row per train-day: (date, train, features, label, destination delay)."""
    rows: list[tuple[dt.date, str, list[float], bool, float]] = []
    for (train, date), day in days.items():
        if day.destination_delay is None:
            continue
        route = routes.get(train, {"distance": 0.0, "stops": 0, "hour": -1.0})
        rows.append(
            (
                date,
                train,
                [
                    float(route["hour"] if route["hour"] >= 0 else 12),
                    float(date.weekday()),
                    1.0 if date.weekday() >= 5 else 0.0,
                    float(route["distance"]),
                    float(route["stops"]),
                ],
                day.destination_delay > LATE_THRESHOLD_MIN,
                day.destination_delay,
            )
        )
    return rows


def train_priors(rows: list[tuple[dt.date, str, list[float], bool, float]]) -> dict[str, dict]:
    """Per-train observed punctuality, from the training window only."""
    buckets: dict[str, list[float]] = defaultdict(list)
    for date, train, _features, _label, delay in rows:
        if date <= TRAIN_UNTIL:
            buckets[train].append(delay)

    priors: dict[str, dict] = {}
    for train, delays in buckets.items():
        ordered = sorted(delays)
        late = sum(1 for delay in ordered if delay > LATE_THRESHOLD_MIN)
        priors[train] = {
            "n": len(ordered),
            "late_pct": round(100.0 * late / len(ordered), 1),
            "mean_delay": round(sum(ordered) / len(ordered), 1),
            "p90_delay": round(ordered[int(0.9 * (len(ordered) - 1))], 1),
        }
    return priors


def fit(rows, priors) -> tuple[list[float], float, dict]:
    """Standardised logistic regression. Returns (weights, bias, metrics)."""
    import numpy as np
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import brier_score_loss, roc_auc_score

    def matrix(subset) -> tuple[np.ndarray, np.ndarray]:
        x = []
        y = []
        for _date, train, features, label, _delay in subset:
            x.append(_with_prior(features, priors.get(train)))
            y.append(1.0 if label else 0.0)
        return np.asarray(x, dtype=float), np.asarray(y, dtype=float)

    train_rows = [row for row in rows if row[0] <= TRAIN_UNTIL]
    valid_rows = [row for row in rows if row[0] >= VALIDATE_FROM]
    x_train, y_train = matrix(train_rows)
    x_valid, y_valid = matrix(valid_rows)

    mean = x_train.mean(axis=0)
    spread = x_train.std(axis=0)
    spread[spread == 0] = 1.0

    model = LogisticRegression(max_iter=2000, C=1.0, solver="lbfgs")
    model.fit((x_train - mean) / spread, y_train)

    probabilities = model.predict_proba((x_valid - mean) / spread)[:, 1]
    auc = float(roc_auc_score(y_valid, probabilities)) if len(set(y_valid)) > 1 else float("nan")
    brier = float(brier_score_loss(y_valid, probabilities))

    metrics = {
        "n_train": len(train_rows),
        "n_validate": len(valid_rows),
        "train_late_pct": round(100.0 * float(y_train.mean()), 2),
        "validate_late_pct": round(100.0 * float(y_valid.mean()), 2),
        "roc_auc": round(auc, 4),
        "brier": round(brier, 4),
        "trained_until": TRAIN_UNTIL.isoformat(),
        "validated_from": VALIDATE_FROM.isoformat(),
        "threshold_min": LATE_THRESHOLD_MIN,
    }
    weights = [float(value) for value in model.coef_[0] / spread]
    scaled = model.coef_[0] / spread
    bias = float(model.intercept_[0] - sum(w * m for w, m in zip(scaled, mean, strict=True)))
    return weights, bias, metrics


def _with_prior(features: list[float], prior: dict | None) -> list[float]:
    """Append the train's own training-window punctuality, or a neutral zero.

    An unknown train gets zeros, which after standardisation means "average". That
    is a modelling convenience, and the loader refuses to serve a prediction for a
    train that has no prior at all, so the neutral value never reaches a user.
    """
    if prior is None:
        return [*features, 0.0, 0.0]
    return [*features, prior["late_pct"], prior["mean_delay"]]


#: Deliberately absent: the delay observed at the train's first stop of the day.
#: It is the strongest single predictor in this data, and at search time we do not
#: have it, so including it would produce an impressive AUC for a question nobody
#: asks. The card predicts before departure or not at all.
FEATURES = [
    "departure_hour",
    "day_of_week",
    "is_weekend",
    "distance_km",
    "stop_count",
    "train_late_pct",
    "train_mean_delay_min",
]


def main() -> int:
    if not ZIP.exists():
        print(f"FAIL missing {ZIP}. Download Indian-Railway-Network-and-Delays.zip from")
        print("     https://github.com/KoyenaChowdhury/RSTGCN into data/raw/")
        return 2

    with zipfile.ZipFile(ZIP) as zf:
        routes = load_routes(zf)
        days = load_train_days(zf)

    rows = build_rows(days, routes)
    priors = train_priors(rows)
    weights, bias, metrics = fit(rows, priors)

    payload = {
        "model_version": MODEL_VERSION,
        "source": "RSTGCN Indian-Railway-Network-and-Delays, September 2024",
        "source_url": "https://github.com/KoyenaChowdhury/RSTGCN",
        "built_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "features": FEATURES,
        "weights": [round(value, 6) for value in weights],
        "bias": round(bias, 6),
        "metrics": metrics,
        "train_priors": priors,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1024:.0f} KB)")
    print(f"trains: {len(priors)}  rows: {metrics['n_train'] + metrics['n_validate']}")
    print(
        f"late rate: train {metrics['train_late_pct']}% / validate "
        f"{metrics['validate_late_pct']}%  AUC {metrics['roc_auc']}  Brier {metrics['brier']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
