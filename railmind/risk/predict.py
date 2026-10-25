"""Punctuality inference. Standard library only, so serving needs no ML dependency.

The model is a logistic regression trained by `scripts/build_delay_model.py` on
1.28 million real per-stop delay observations (RSTGCN, September 2024). It is
small enough to ship as JSON and cheap enough to evaluate in microseconds, which
is the point: the card has to be ready in under two seconds, and a prediction that
costs 40 ms of Python is worth more than one that costs 400 ms and is 0.02 better.

Four rules this file enforces, each because the alternative is a confident lie:

1. **No prior, no prediction.** A train that never appears in the training window
   returns `None`, and the itinerary keeps `risk_band: unknown`. The model has
   nothing to say about it, and a neutral probability would look like knowledge.
2. **The number comes with its provenance.** `model_version` names the artifact,
   `confidence` reflects how many observations that train had, and the top three
   features come with signed contributions so the card can say why.
3. **Seasonality is admitted, not invented.** The training data covers one month,
   so the model has no month feature. It predicts the same probability in January
   as in September, and the confidence is reduced for that reason rather than
   hidden. Fog and monsoon effects are real and unmodelled.
4. **One definition of late.** More than 15 minutes after schedule, which is the
   threshold Indian Railways itself reports against, so our number and the
   official punctuality statistic cannot disagree about what they are counting.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from railmind.wire.models import Risk, TopFeature

DEFAULT_MODEL = Path("data/delay-model.json")

#: Observed on-time statistics saturate: 30 observations of one train is already
#: enough to trust its own history, and beyond that the limit is the model, not the
#: sample size.
_FULL_CONFIDENCE_SAMPLES = 30.0

#: How far to trust a model trained on one month when the journey is in another.
#: Not a fitted number and not arbitrary either: the model has no month feature, so
#: a quarter of the confidence is withheld for the unmodelled season.
_SEASON_PENALTY = 0.75


@dataclass(frozen=True, slots=True)
class Prediction:
    """One probability, with everything needed to explain or audit it."""

    probability: float
    expected_delay_min: float
    p90_delay_min: float
    confidence: float
    samples: int
    top_features: tuple[TopFeature, ...]


class PunctualityModel:
    """Loaded artifact. Construct once per process; `predict` is pure and cheap."""

    def __init__(self, payload: dict) -> None:
        self.model_version: str = str(payload["model_version"])
        self.features: tuple[str, ...] = tuple(payload["features"])
        self.weights: tuple[float, ...] = tuple(float(w) for w in payload["weights"])
        self.bias: float = float(payload["bias"])
        self.priors: dict[str, dict] = dict(payload.get("train_priors") or {})
        self.metrics: dict = dict(payload.get("metrics") or {})
        if len(self.features) != len(self.weights):
            raise ValueError(
                f"model artifact is inconsistent: {len(self.features)} features, "
                f"{len(self.weights)} weights"
            )

    @classmethod
    def load(cls, path: str | Path = DEFAULT_MODEL) -> PunctualityModel | None:
        """Load the artifact, or return None when it has not been built.

        None is a valid, well-understood state: the site works, cards say risk is
        unknown, and nobody has to pretend a model exists. Raising here would turn
        a missing optional artifact into a startup failure.
        """
        file = Path(path)
        if not file.exists():
            return None
        return cls(json.loads(file.read_text()))

    def predict(
        self,
        *,
        train_number: str,
        departure_hour: int,
        weekday: int,
        distance_km: float | None,
        stop_count: int | None,
    ) -> Prediction | None:
        """Probability this train arrives more than 15 minutes late, or None."""
        prior = self.priors.get(str(train_number))
        if prior is None:
            return None

        values = {
            "departure_hour": float(departure_hour),
            "day_of_week": float(weekday),
            "is_weekend": 1.0 if weekday >= 5 else 0.0,
            "distance_km": float(distance_km or 0.0),
            "stop_count": float(stop_count or 0),
            "train_late_pct": float(prior["late_pct"]),
            "train_mean_delay_min": float(prior["mean_delay"]),
        }
        contributions = [
            (name, self.weights[index] * values[name])
            for index, name in enumerate(self.features)
            if name in values
        ]
        score = self.bias + sum(value for _name, value in contributions)
        probability = _sigmoid(score)

        ordered = sorted(contributions, key=lambda item: abs(item[1]), reverse=True)[:3]
        samples = int(prior["n"])
        confidence = _confidence(samples)

        return Prediction(
            probability=round(min(0.999, max(0.001, probability)), 4),
            expected_delay_min=float(prior["mean_delay"]),
            p90_delay_min=float(prior["p90_delay"]),
            confidence=confidence,
            samples=samples,
            top_features=tuple(
                TopFeature(name=_human(name), contribution=round(value, 4))
                for name, value in ordered
            ),
        )

    def to_risk(self, prediction: Prediction) -> Risk:
        """Wire `Risk`. `model_version` travels so a card can be audited later."""
        return Risk(
            delay_probability=prediction.probability,
            expected_delay_min=prediction.expected_delay_min,
            p90_delay_min=prediction.p90_delay_min,
            model_version=self.model_version,
            confidence=prediction.confidence,
            top_features=list(prediction.top_features),
        )


def _confidence(samples: int) -> float:
    """Sample size, then the season penalty. Deliberately formulaic and readable."""
    from_samples = min(1.0, math.log1p(samples) / math.log1p(_FULL_CONFIDENCE_SAMPLES))
    return round(from_samples * _SEASON_PENALTY, 3)


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


_LABELS = {
    "departure_hour": "scheduled departure hour",
    "day_of_week": "day of the week",
    "is_weekend": "weekend",
    "distance_km": "journey distance",
    "stop_count": "number of stops",
    "train_late_pct": "this train's own recorded punctuality",
    "train_mean_delay_min": "this train's recorded average delay",
}


def _human(name: str) -> str:
    """Feature names are read by a person on a card, so they are written for one."""
    return _LABELS.get(name, name.replace("_", " "))
