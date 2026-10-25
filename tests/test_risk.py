"""Punctuality model tests: hermetic, tiny, and strict about what a number means.

A synthetic artifact is used rather than the real 248 KB one, so these tests do not
depend on whether `scripts/build_delay_model.py` has ever been run on this machine.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from railmind.risk.predict import PunctualityModel


def artifact(tmp_path, *, priors=None, train_taken=2.0, hour_taken=0.0, bias=-1.0):
    payload = {
        "model_version": "test-0.0.1",
        "features": ["departure_hour", "train_late_pct"],
        "weights": [hour_taken, train_taken],
        "bias": bias,
        "metrics": {"roc_auc": 0.5},
        "train_priors": priors
        if priors is not None
        else {
            "12127": {"n": 21, "late_pct": 70.0, "mean_delay": 26.4, "p90_delay": 50.0},
            "22105": {"n": 2, "late_pct": 0.0, "mean_delay": 1.0, "p90_delay": 3.0},
        },
    }
    path = tmp_path / "model.json"
    path.write_text(json.dumps(payload))
    return path


def test_a_missing_artifact_is_a_supported_state_not_a_crash(tmp_path):
    assert PunctualityModel.load(tmp_path / "nope.json") is None


def test_a_train_with_no_history_gets_no_prediction(tmp_path):
    model = PunctualityModel.load(artifact(tmp_path))

    assert model is not None
    assert (
        model.predict(
            train_number="99999", departure_hour=6, weekday=0, distance_km=100, stop_count=5
        )
        is None
    )


def test_a_known_train_gets_a_probability_inside_the_open_interval(tmp_path):
    model = PunctualityModel.load(artifact(tmp_path))

    prediction = model.predict(
        train_number="12127", departure_hour=6, weekday=0, distance_km=190, stop_count=8
    )

    assert prediction is not None
    assert 0.0 < prediction.probability < 1.0
    assert prediction.samples == 21
    assert prediction.expected_delay_min == 26.4
    assert prediction.p90_delay_min == 50.0


def test_a_better_record_means_a_lower_probability(tmp_path):
    model = PunctualityModel.load(artifact(tmp_path))
    late_train = model.predict(
        train_number="12127", departure_hour=6, weekday=0, distance_km=190, stop_count=8
    )
    punctual_train = model.predict(
        train_number="22105", departure_hour=6, weekday=0, distance_km=190, stop_count=8
    )

    assert late_train is not None and punctual_train is not None
    assert late_train.probability > punctual_train.probability


def test_confidence_follows_the_sample_size(tmp_path):
    model = PunctualityModel.load(artifact(tmp_path))
    many = model.predict(
        train_number="12127", departure_hour=6, weekday=0, distance_km=1, stop_count=1
    )
    few = model.predict(
        train_number="22105", departure_hour=6, weekday=0, distance_km=1, stop_count=1
    )

    assert many is not None and few is not None
    assert many.confidence > few.confidence
    # A model trained on one month never claims certainty.
    assert many.confidence < 1.0


def test_prediction_is_deterministic(tmp_path):
    model = PunctualityModel.load(artifact(tmp_path))
    arguments = dict(
        train_number="12127", departure_hour=6, weekday=0, distance_km=190, stop_count=8
    )

    assert model.predict(**arguments) == model.predict(**arguments)


def test_the_wire_risk_carries_its_provenance(tmp_path):
    model = PunctualityModel.load(artifact(tmp_path))
    prediction = model.predict(
        train_number="12127", departure_hour=6, weekday=0, distance_km=190, stop_count=8
    )

    risk = model.to_risk(prediction)

    assert risk.model_version == "test-0.0.1"
    assert risk.confidence is not None
    assert risk.top_features
    # Feature names are read by a person, not by a machine.
    assert all("_" not in feature.name for feature in risk.top_features)


def test_an_inconsistent_artifact_is_refused_loudly(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text(
        json.dumps(
            {
                "model_version": "x",
                "features": ["a", "b"],
                "weights": [1.0],
                "bias": 0.0,
            }
        )
    )

    with pytest.raises(ValueError, match="inconsistent"):
        PunctualityModel.load(path)


def test_the_committed_artifact_is_usable_if_present():
    """When the real model has been built, it must load and answer for a real train."""
    path = pathlib.Path("data/delay-model.json")
    if not path.exists():
        pytest.skip("delay model not built on this machine")

    model = PunctualityModel.load(path)
    assert model is not None
    assert model.priors
    assert model.metrics.get("roc_auc", 0) > 0.5

    prediction = model.predict(
        train_number="12127", departure_hour=6, weekday=0, distance_km=190.7, stop_count=8
    )
    assert prediction is not None
    assert 0.0 < prediction.probability < 1.0
