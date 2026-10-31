# The punctuality model: what it is, what it is not, and how to reproduce it

Written 2026-10-25, after the model was trained and wired into the card.

## What it predicts

**One number**: the probability that a train arrives more than 15 minutes after
schedule. Fifteen minutes is not our choice. It is the threshold Indian Railways
reports its own punctuality against, so our number and the official statistic
count the same thing.

The card also shows the train's recorded average delay and its 90th-percentile
delay, because "60% chance of being late" is useless without "typically 24 minutes
late, and one run in ten is 47 minutes late".

## Where the data comes from

| | |
|---|---|
| Dataset | Indian Railway Network and Delays (RSTGCN), September 2024 |
| Source | `https://github.com/KoyenaChowdhury/RSTGCN`, file `Indian-Railway-Network-and-Delays.zip` |
| Contents | **1,282,325** per-stop observations, 3,892 trains, 4,735 stations, 17 zones |
| Fields | train, date, station, scheduled and actual arrival/departure, arrival delay, departure delay, all in minutes |
| Licence | **None declared on the repository.** Treated as research data. The raw 23 MB archive stays in `data/raw/` (gitignored) and is not redistributed. The committed artifact contains derived aggregates and a trained linear model, not the source observations. |
| Our trains | All 5 seeded trains appear: 12127 (180 rows), 12951 (240), 12137 (1620), 22105 (240), 11007 (330) |

## Method

`scripts/build_delay_model.py`, 3.5 seconds on a laptop CPU, standard library for
loading plus numpy and scikit-learn for fitting. Serving uses the standard library
only.

1. Collapse 1.28 million per-stop rows into **one row per train-day**: the delay
   observed at the last stop of that day's run is the destination delay.
   Result: 57,485 train-days.
2. **Split by date**, not by row: train on 1-21 September, validate on 22-30
   September. A random split would let the same train-day appear on both sides and
   report a fantasy AUC, because delays persist across a route.
3. **Per-train priors computed on the training window only**, then used as
   features. Computing them on the whole month leaks the answer into the input.
4. Fit standardised logistic regression with L2. Eight features: scheduled
   departure hour, day of week, is-weekend, distance, stop count, the train's own
   recorded late percentage, and its recorded mean delay.

### Measured performance

| Metric | Value |
|---|---|
| ROC-AUC (validation, last 9 days) | **0.7778** |
| Brier score | 0.1242 |
| Late rate, train / validation | 18.34% / 18.25% |
| Rows trained / validated | 44,190 / 11,245 |

That AUC is deliberately unspectacular. It is what is left after removing the one
feature that would have inflated it: the delay already observed at the train's
first stop of the day. At search time we do not know it, so including it would
have produced an impressive number for a question nobody asks.

### What is not modelled, and is therefore discounted in the confidence

- **Season.** The data is one month, so there is no month feature. The model
  predicts the same probability in January as in September. Fog and monsoon
  effects are real and unmodelled, so confidence carries a fixed 0.75 penalty.
- **Weather, congestion, rake availability, crew.** No free source, and inventing
  them is what turns a project into a demo about fake data.
- **Route position.** Delay is predicted for the journey as booked, not per stop
  along the way.

## How to read the output

```
risk.delay_probability   0.7397   probability of arriving >15 min late
risk.expected_delay_min  26.4     recorded average delay for this train
risk.p90_delay_min       50.0     one run in ten is worse than this
risk.model_version       logit-2024.09.1
risk.confidence          0.675    sample size of this train's history, season-discounted
risk.top_features        the three features that moved the number most, in words
```

Bands, applied by the ranking layer from the probability: **low** under 25%,
**medium** under 50%, **high** at or above 50%, **unknown** when there is no
prediction. With a base rate of 18%, a 60% probability is three times the base
rate, so "high" is not alarmism.

## The honest failure mode

A train that never appears in the training window gets **no prediction at all**.
`risk_band` stays `unknown` and the card says so in words. Nothing is substituted,
because a neutral probability would look exactly like knowledge. `PunctualityModel.load`
returning `None` (no artifact built) is a supported state for the same reason: the
site works, the cards tell the truth, and nobody pretends.

## Reproducing it

```bash
cd railmind
mkdir -p data/raw
curl -L -o data/raw/rstgcn.zip \
  https://raw.githubusercontent.com/KoyenaChowdhury/RSTGCN/main/Indian-Railway-Network-and-Delays.zip
.venv/bin/pip install numpy scikit-learn        # training only
.venv/bin/python scripts/build_delay_model.py   # writes data/delay-model.json
```

The artifact is committed (248 KB) so a demo machine needs neither the archive nor
scikit-learn. Point `RAILMIND_DELAY_MODEL` elsewhere to swap models.
