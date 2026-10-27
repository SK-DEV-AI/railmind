"""The brand table, checked as a table.

A brand is a promise that a spoken name turns into a filter that finds real
trains. The failures that matter are silent: a duplicate key that shadows one
brand, a marker so short it matches half the timetable, a phrase that never
matches its own marker. None of those raise at import time, so they are asserted
here.
"""

from __future__ import annotations

import pytest

from railmind.core import services


def test_keys_are_unique_and_non_empty():
    keys = [brand.key for brand in services.BRANDS]

    assert keys
    assert len(keys) == len(set(keys))
    assert all(key and " " not in key for key in keys)


def test_every_brand_has_phrases_and_markers():
    for brand in services.BRANDS:
        assert brand.phrases, brand.key
        assert brand.markers, brand.key
        assert brand.label.strip(), brand.key


def test_a_marker_always_matches_a_name_that_carries_it():
    # "Garib Rath" is printed both with and without the space, so a brand that
    # names one spelling must recognise the other or the filter silently misses
    # half of that fleet.
    for brand in services.BRANDS:
        assert any(brand.matches(f"{marker} EXPRESS") for marker in brand.markers), brand.key


def test_markers_are_long_enough_to_mean_something():
    for brand in services.BRANDS:
        for marker in brand.markers:
            assert len(marker) >= 4, (brand.key, marker)


def test_an_unknown_key_raises_rather_than_passing_everything_through():
    with pytest.raises(KeyError):
        services.brand("no-such-brand")


def test_labels_read_as_a_sentence():
    assert services.labels(["vande-bharat"]) == "Vande Bharat"
    assert services.labels(["vande-bharat", "rajdhani"]) == "Vande Bharat and Rajdhani"
    assert services.labels(()) == ""


def test_from_text_finds_the_brand_and_ignores_it_inside_a_longer_word():
    assert services.from_text("book a vande bharat to pune") == ["vande-bharat"]
    # A station or word that merely contains the letters is not a brand request.
    assert services.from_text("mumbai tejaswini to pune") == []


def test_without_brands_removes_the_brand_and_leaves_the_route():
    text = services.without_brands("mumbai to pune vande bharat tomorrow").lower()

    assert "vande" not in text
    assert "pune" in text
    assert "tomorrow" in text
