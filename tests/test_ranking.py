"""Ranking tests. No network, no clock, no randomness.

Each test here exists to pin one promise the module docstring makes. If a future
change breaks the promise, one of these fails and names it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from railmind.core.types import inr
from railmind.engine.ranking import (
    DEFAULT_WEIGHTS,
    NEUTRAL,
    pareto_front,
    rank,
)
from railmind.providers.fake import make_itinerary


def _strip(itinerary, **fields):
    """A copy with fields removed. `model_copy` is how a missing fact is simulated."""
    return itinerary.model_copy(update=fields, deep=True)


def test_rank_puts_the_shortest_and_cheapest_first():
    slow = make_itinerary(service_id="slow", duration_h=18.0, price=inr(1500))
    fast = make_itinerary(service_id="fast", depart_hour=6, duration_h=9.0, price=inr(1500))

    result = rank([slow, fast])

    assert [item.legs[0].service.service_id for item in result.itineraries] == ["fast", "slow"]
    assert [item.rank for item in result.itineraries] == [1, 2]
    assert result.itineraries[0].score > result.itineraries[1].score


def test_missing_price_is_not_scored_as_average_and_is_announced():
    """The rule that matters most: a gap must never look like data."""
    priced = make_itinerary(service_id="priced", price=inr(2000))
    unpriced = make_itinerary(service_id="unpriced", depart_hour=6, price=inr(0))
    unpriced = _strip(unpriced, total_price=None)

    result = rank([priced, unpriced])

    unpriced_card = next(
        item for item in result.itineraries if item.legs[0].service.service_id == "unpriced"
    )
    assert unpriced_card.score_breakdown.price == NEUTRAL
    assert "price" not in result.dimensions_used
    assert any("No fare available" in warning for warning in unpriced_card.warnings)


def test_unknown_risk_stays_unknown_rather_than_optimistic():
    known = make_itinerary(service_id="known", delay_probability=0.9)
    unknown = make_itinerary(service_id="unknown")
    unknown.legs[0].risk = None
    unknown = _strip(unknown, legs=unknown.legs)

    result = rank([known, unknown])

    card = next(item for item in result.itineraries if item.legs[0].service.service_id == "unknown")
    assert card.risk_band.value == "unknown"
    assert any("punctuality" in warning for warning in card.warnings)
    assert next(item for item in result.itineraries if item.rank == 1).risk_band.value == "high"


def test_tags_name_the_single_dimension_winners():
    cheap = make_itinerary(service_id="cheap", duration_h=20.0, price=inr(900))
    quick = make_itinerary(service_id="quick", depart_hour=6, duration_h=8.0, price=inr(4000))

    result = rank([cheap, quick])
    tags = {item.legs[0].service.service_id: set(item.tags) for item in result.itineraries}

    assert "cheapest" in tags["cheap"]
    assert "fastest" in tags["quick"]
    assert "fewest_changes" in tags["cheap"]  # single-leg trips all tie on legs
    # Both are non-dominated, so "efficient" would be true of everything and is
    # withheld. A tag that is always on says nothing.
    assert "efficient" not in tags["cheap"]


def test_efficient_appears_only_when_something_is_dominated():
    winner = make_itinerary(service_id="winner", duration_h=8.0, price=inr(900))
    loser = make_itinerary(service_id="loser", duration_h=12.0, price=inr(1800))

    result = rank([winner, loser])
    tags = {item.legs[0].service.service_id: set(item.tags) for item in result.itineraries}

    assert "efficient" in tags["winner"]
    assert "efficient" not in tags["loser"]


def test_score_is_capped_by_the_evidence_behind_it():
    """A fast train with no fare and no punctuality data cannot score 1.0."""
    bare = make_itinerary(service_id="bare")
    bare.legs[0].risk = None
    bare.legs[0].availability = None
    bare = bare.model_copy(update={"legs": bare.legs, "total_price": None}, deep=True)

    result = rank([bare])

    # Only duration and transfers were comparable, so most of the weight is
    # unusable and the score says so.
    assert result.itineraries[0].score < 0.5


def test_dominated_option_is_off_the_pareto_front():
    winner = make_itinerary(service_id="winner", duration_h=8.0, price=inr(900))
    loser = make_itinerary(service_id="loser", duration_h=12.0, price=inr(1800))

    front = pareto_front([winner, loser])

    assert [item.legs[0].service.service_id for item in front] == ["winner"]


def test_pareto_skips_dimensions_where_either_side_is_unknown():
    """An unknown must not be able to make a train look dominant."""
    first = make_itinerary(service_id="a", duration_h=8.0, price=inr(1000))
    second = make_itinerary(service_id="b", duration_h=12.0, price=inr(900))
    second = _strip(second, total_price=None)

    # b is slower but its price is unknown, so nobody dominates anybody.
    front = pareto_front([first, second])

    assert len(front) == 2


def test_tie_break_is_departure_then_id_so_a_demo_never_reshuffles():
    later = make_itinerary(service_id="zzz", depart_hour=20)
    earlier = make_itinerary(service_id="aaa", depart_hour=6)
    # Identical duration and price: nothing in the score separates them.
    earlier = earlier.model_copy(update={"total_duration_s": later.total_duration_s}, deep=True)

    result = rank([later, earlier])

    assert [item.legs[0].service.service_id for item in result.itineraries] == ["aaa", "zzz"]


def test_weights_are_renormalised_so_zeroing_one_does_not_shrink_the_score():
    slow = make_itinerary(service_id="slow", duration_h=20.0)
    fast = make_itinerary(service_id="fast", depart_hour=6, duration_h=6.0)

    first = rank([slow.model_copy(deep=True), fast.model_copy(deep=True)])
    second = rank(
        [slow.model_copy(deep=True), fast.model_copy(deep=True)],
        weights={**DEFAULT_WEIGHTS, "duration": 0.0},
    )

    # Duration is the only separating dimension, so removing it flattens the gap.
    gap_with = first.itineraries[0].score - first.itineraries[1].score
    gap_without = second.itineraries[0].score - second.itineraries[1].score
    assert gap_with > 0
    assert gap_without == pytest.approx(0.0, abs=1e-9)


def test_negative_weight_is_refused():
    with pytest.raises(ValueError, match="must not be negative"):
        rank([make_itinerary()], weights={"price": -1.0})


def test_ranking_nothing_is_not_an_error():
    result = rank([])

    assert result.itineraries == []
    assert result.dimensions_used == ()


def test_confidence_rises_with_each_verified_fact():
    full = make_itinerary(service_id="full")
    bare = make_itinerary(service_id="bare")
    bare.legs[0].risk = None
    bare.legs[0].availability = None
    bare = bare.model_copy(update={"legs": bare.legs, "total_price": None}, deep=True)

    result = rank([full, bare])
    scores = {item.legs[0].service.service_id: item.confidence for item in result.itineraries}

    assert scores["full"] > scores["bare"]
    assert 0.0 <= scores["bare"] <= 1.0
    assert 0.0 <= scores["full"] <= 1.0


def test_comfort_only_counts_classes_we_can_judge():
    sleeper = make_itinerary(service_id="sleeper")
    sleeper.legs[0].availability.class_code = "SL"
    ac = make_itinerary(service_id="ac", depart_hour=6)
    ac.legs[0].availability.class_code = "1A"
    sleeper = sleeper.model_copy(update={"legs": sleeper.legs}, deep=True)
    ac = ac.model_copy(update={"legs": ac.legs}, deep=True)

    result = rank([sleeper, ac])
    breakdowns = {
        item.legs[0].service.service_id: item.score_breakdown.comfort for item in result.itineraries
    }

    assert breakdowns["ac"] == 1.0
    assert breakdowns["sleeper"] == 0.0  # lowest of the two, not absolutely low
    assert "comfort" in result.dimensions_used


def test_score_and_breakdown_stay_inside_the_contract_bounds():
    candidates = [
        make_itinerary(service_id=f"t{index}", depart_hour=index + 1, duration_h=6.0 + index)
        for index in range(5)
    ]

    result = rank(candidates)

    for item in result.itineraries:
        assert 0.0 <= item.score <= 1.0
        for value in item.score_breakdown.model_dump().values():
            assert 0.0 <= value <= 1.0
        assert item.rank >= 1
        assert item.depart_at is not None
        assert isinstance(item.depart_at, dt.datetime)


def test_an_unrated_option_is_labelled_not_silently_outranking_a_bad_one():
    """Unknown can beat known-bad by design, so the card has to say so."""
    unrated = make_itinerary(service_id="unrated", depart_hour=6, duration_h=8.0)
    unrated.legs[0].risk = None
    unrated = unrated.model_copy(update={"legs": unrated.legs}, deep=True)
    bad = make_itinerary(service_id="bad", duration_h=9.0, delay_probability=0.9)

    result = rank([unrated, bad])
    tags = {item.legs[0].service.service_id: set(item.tags) for item in result.itineraries}

    assert "punctuality_unknown" in tags["unrated"]
    assert "punctuality_unknown" not in tags["bad"]
    # The unrated option does win, which is the behaviour the tag explains.
    assert result.itineraries[0].legs[0].service.service_id == "unrated"
    assert any("No punctuality estimate" in w for w in result.itineraries[0].warnings)
