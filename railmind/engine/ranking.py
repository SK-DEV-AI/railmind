"""Ranking: one function that turns candidates into a single recommendation.

Why this is its own module. An adapter can produce itineraries, but it must not
decide which one a rushing human sees. If every provider ranked its own results,
adding a second provider would silently change what a score means and the number
on the card would stop being comparable between modes. So ranking lives here,
above the provider boundary, and knows nothing about trains, buses, or Beckn.

The five rules this module enforces:

1. Score from facts we hold, never from gaps. A dimension with no data is left
   out of the weighted mean and its weight is redistributed over the dimensions
   that do have data. It is not scored as if it were average, because a neutral
   value quietly steals weight from real evidence.
2. The wire shape needs all five components as floats, so a missing component is
   written as `NEUTRAL` and the itinerary carries a plain-language warning. A
   reader of `score_breakdown` can therefore tell a real 0.5 from an unknown 0.5
   only by reading `warnings`, which is exactly where a human will look.
3. Ties are broken deterministically, by departure then leg id, so a demo does
   not reshuffle itself between two identical requests.
4. `risk_band` stays `unknown` until a delay model actually exists. `unknown` is
   a real value, not a placeholder, and a client must render it as "no estimate".
5. Tags mark the options that win on a single dimension, because "cheapest" and
   "fastest" are questions a user asks directly and a weighted score cannot
   answer them. A tag is only added when it discriminates: "efficient" on every
   card means the comparison found nothing, and a tag that is always true is
   noise dressed as information.

The score is evidence-weighted, not just quality-weighted: the weighted mean of
the dimensions we could compare is multiplied by the share of the total weight
those dimensions carry. A card built on duration alone cannot score 1.0 however
fast the train is, because we do not know its fare or its punctuality. That keeps
`score` from saying "perfect" when it means "the least unknown".`

Everything here is pure and synchronous: no clock, no I/O, no provider. That is
what makes it testable without a network and cheap enough to run inside the
800 ms budget with room to spare.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from railmind.wire.models import Itinerary, RiskBand, ScoreBreakdown

#: Weights for the weighted mean. They sum to 1.0 for readability, but the
#: arithmetic renormalises over whatever dimensions are available, so they may
#: be changed independently. Rationale for the order: a Tatkal buyer is
#: price-sensitive, a rushed buyer hates long trips, and a late train ruins a
#: plan, so those three carry most of the weight. Comfort and changes matter,
#: but nobody picks a worse arrival time to avoid one change.
DEFAULT_WEIGHTS: Mapping[str, float] = {
    "price": 0.30,
    "duration": 0.28,
    "delay_risk": 0.22,
    "comfort": 0.12,
    "transfers": 0.08,
}

#: Written into `score_breakdown` for a dimension with no data. The wire schema
#: requires a float, so absence has to be represented somehow; a warning on the
#: itinerary is the part that makes it honest.
NEUTRAL = 0.5

#: Comfort by travel class, used only when a leg names one. Slower and cheaper
#: is not the same as less comfortable: an AC chair car beats a sleeper on a
#: day trip and loses on an overnight one, which is why this is a lookup and not
#: a formula. Unlisted classes fall back to NEUTRAL.
_CLASS_COMFORT: Mapping[str, float] = {
    "1A": 1.0,
    "EC": 1.0,
    "2A": 0.85,
    "EA": 0.8,
    "3A": 0.7,
    "CC": 0.7,
    "3E": 0.6,
    "SL": 0.45,
    "2S": 0.3,
}

#: Delay probability thresholds for the band. 25 percent late is a normal
#: corridor day, 50 percent is a coin flip, above that the train is a gamble.
_LOW_RISK = 0.25
_HIGH_RISK = 0.50


@dataclass(frozen=True, slots=True)
class Ranked:
    """The outcome of ranking, kept together so callers cannot use half of it."""

    itineraries: list[Itinerary]
    weights: Mapping[str, float]
    dimensions_used: tuple[str, ...]
    notes: tuple[str, ...]


def rank(
    itineraries: Iterable[Itinerary],
    *,
    weights: Mapping[str, float] | None = None,
) -> Ranked:
    """Order candidates best first and label them. Mutates the itineraries given.

    Mutation is deliberate: `score`, `rank`, `tags` and `risk_band` are fields of
    the itinerary itself, and copying the whole model to set four fields would
    create a second shape to keep in sync.
    """
    candidates = list(itineraries)
    chosen = dict(weights or DEFAULT_WEIGHTS)
    for name, value in chosen.items():
        if value < 0:
            raise ValueError(f"weight {name!r} must not be negative")

    if not candidates:
        return Ranked([], chosen, (), ())

    notes: list[str] = []
    scored: dict[str, list[float | None]] = {
        # Comfort is the only dimension scored by min-max where more is better;
        # price, duration and delay risk are all "less is better".
        name: _normalise(_raw_dimension(candidates, name), lower_is_better=name != "comfort")
        for name in chosen
        if name != "transfers"
    }
    # Changes are scored on an absolute scale rather than min-max, because the
    # number of transfers means something on its own: one leg is the best a trip
    # can do, and a set of three bad two-leg trips must not produce a "1.0".
    scored["transfers"] = [_transfer_quality(len(item.legs)) for item in candidates]

    used: list[str] = []
    for name, values in scored.items():
        if any(value is not None for value in values) and chosen[name] > 0:
            used.append(name)

    if not used:
        notes.append("no comparable dimensions; every candidate scored equally")
        used = ["transfers"]

    # Computed once: the front is a property of the candidate set, not of one
    # itinerary, and recomputing it per candidate would be cubic for no reason.
    front = {id(item) for item in pareto_front(candidates)}

    for index, itinerary in enumerate(candidates):
        totals: dict[str, float] = {}
        weight_sum = 0.0
        breakdown: dict[str, float] = {}
        for name in DEFAULT_WEIGHTS:
            weight = chosen.get(name, 0.0)
            value = scored.get(name, [None] * len(candidates))[index]
            if value is None:
                breakdown[name] = NEUTRAL
                continue
            breakdown[name] = round(value, 4)
            if name in used and weight > 0:
                totals[name] = value
                weight_sum += weight

        # Evidence-weighted: the mean over comparable dimensions, scaled by how
        # much of the intended weight we were actually able to use.
        coverage = 0.0 if not chosen else weight_sum / sum(chosen.values() or [1.0])
        score = (
            0.0
            if weight_sum == 0
            else sum(totals[n] * chosen[n] for n in totals) / weight_sum * coverage
        )
        itinerary.score = round(score, 4)
        itinerary.score_breakdown = ScoreBreakdown(**breakdown)
        itinerary.risk_band = _band(itinerary)
        itinerary.tags = _tags(itinerary, candidates, front)
        itinerary.confidence = _confidence(itinerary)
        _warn_missing(itinerary)

    candidates.sort(key=lambda item: (-item.score, item.depart_at, _sort_key(item)))
    for position, itinerary in enumerate(candidates, start=1):
        itinerary.rank = position

    return Ranked(candidates, chosen, tuple(used), tuple(notes))


def pareto_front(itineraries: Iterable[Itinerary]) -> list[Itinerary]:
    """Candidates that nothing else beats on every dimension we can compare.

    A dominated option is one where another option is at least as good on all
    three of price, duration and delay risk, and strictly better on one. That is
    the definition a reviewer will ask for, so it is worth being able to show it
    rather than describing a weighted sum and hoping.
    """
    items = list(itineraries)
    front: list[Itinerary] = []
    for candidate in items:
        dominated = False
        for other in items:
            if other is candidate:
                continue
            if _dominates(other, candidate):
                dominated = True
                break
        if not dominated:
            front.append(candidate)
    return front


# -- dimensions ---------------------------------------------------------------


def _raw_dimension(candidates: list[Itinerary], name: str) -> list[float | None]:
    """One comparable number per candidate, or None when that candidate lacks it."""
    if name == "price":
        # Minor units (paise) throughout: integer arithmetic is exact, and the
        # wire type says so. Converting to rupees here would add a rounding bug
        # to every comparison for no benefit.
        return [
            float(item.total_price.amount_minor) if item.total_price is not None else None
            for item in candidates
        ]
    if name == "duration":
        return [float(item.total_duration_s) for item in candidates]
    if name == "delay_risk":
        return [_worst_delay_probability(item) for item in candidates]
    if name == "comfort":
        return [_comfort(item) for item in candidates]
    if name == "transfers":
        return [float(len(item.legs)) for item in candidates]
    raise ValueError(f"unknown ranking dimension {name!r}")


def _normalise(values: list[float | None], *, lower_is_better: bool) -> list[float | None]:
    """Scale to 0..1 across the candidate set, with 1.0 always the better end.

    A dimension where every candidate is identical scores 1.0 for all of them:
    it is not evidence for anyone, but it is not evidence against anyone either,
    and a constant offset cancels out of the weighted mean anyway.
    """
    present = [value for value in values if value is not None]
    if len(present) < 2:
        # Fewer than two values means there is nothing to compare, and a
        # comparison of one is not evidence. Reporting it as 1.0 would hand a
        # bonus to whichever candidate happened to have the fact, so the whole
        # dimension drops out of the score instead.
        return [None] * len(values)

    low, high = min(present), max(present)
    if high == low:
        return [1.0 if value is not None else None for value in values]

    span = high - low
    result: list[float | None] = []
    for value in values:
        if value is None:
            result.append(None)
            continue
        position = (value - low) / span
        result.append(1.0 - position if lower_is_better else position)
    return result


def _transfer_quality(legs: int) -> float:
    """Absolute, not relative: how good is this number of changes, full stop.

    A three-way choice between two-leg trips and three-leg trips is not evidence
    that two legs is comfortable, and a min-max scale would say exactly that.
    """
    if legs <= 1:
        return 1.0
    if legs == 2:
        return 0.6
    if legs == 3:
        return 0.35
    return 0.2


def _worst_delay_probability(itinerary: Itinerary) -> float | None:
    """The leg the plan is most exposed to. A chain is as reliable as its worst link."""
    probabilities = [
        leg.risk.delay_probability
        for leg in itinerary.legs
        if leg.risk is not None and leg.risk.delay_probability is not None
    ]
    return max(probabilities) if probabilities else None


def _comfort(itinerary: Itinerary) -> float | None:
    """Mean class comfort, or None when no leg names a class we recognise."""
    known = [
        _CLASS_COMFORT[class_code]
        for leg in itinerary.legs
        if (class_code := _class_code(leg)) in _CLASS_COMFORT
    ]
    return sum(known) / len(known) if known else None


def _class_code(leg: object) -> str:
    availability = getattr(leg, "availability", None)
    code = getattr(availability, "class_code", None) if availability is not None else None
    return code.upper() if isinstance(code, str) else ""


# -- labels -------------------------------------------------------------------


def _band(itinerary: Itinerary) -> RiskBand:
    probability = _worst_delay_probability(itinerary)
    if probability is None:
        return RiskBand.unknown
    if probability < _LOW_RISK:
        return RiskBand.low
    if probability < _HIGH_RISK:
        return RiskBand.medium
    return RiskBand.high


def _tags(itinerary: Itinerary, candidates: list[Itinerary], front: set[int]) -> list[str]:
    """Single-dimension winners, so a user can ask a direct question directly."""
    tags: list[str] = []

    priced = [item.total_price.amount_minor for item in candidates if item.total_price is not None]
    if (
        priced
        and itinerary.total_price is not None
        and itinerary.total_price.amount_minor == min(priced)
    ):
        tags.append("cheapest")

    shortest = min(item.total_duration_s for item in candidates)
    if itinerary.total_duration_s == shortest:
        tags.append("fastest")

    fewest = min(len(item.legs) for item in candidates)
    if len(itinerary.legs) == fewest:
        tags.append("fewest_changes")

    known = [
        probability
        for item in candidates
        if (probability := _worst_delay_probability(item)) is not None
    ]
    probability = _worst_delay_probability(itinerary)
    if known and probability is not None and probability == min(known):
        tags.append("most_reliable")

    # Identity, not equality: two identical trips are the same trip, and both
    # belong on the front or neither does. The tag is withheld when the front is
    # everything, because then it distinguishes nothing.
    if id(itinerary) in front and len(front) < len(candidates):
        tags.append("efficient")

    # Surfaced as a label, not just a warning, because of a result that surprises
    # people: an option with NO punctuality history can outrank one with a bad
    # history. That is deliberate (unknown is not the same as proven late, and we
    # refuse to score a gap as average), but a reader who cannot see why will
    # assume the ranking is broken. So the card says which options are unrated.
    if _worst_delay_probability(itinerary) is None:
        tags.append("punctuality_unknown")

    return tags


def _confidence(itinerary: Itinerary) -> float:
    """How much the card can be trusted. A statement about data, not about quality.

    The base is deliberately below a half. A card built only from a schedule is
    better than a guess but is not evidence about today: no price, no seats, no
    delay model. Each verified fact adds a fixed amount, so the number moves for
    reasons a reviewer can follow rather than by a fitted weight nobody can check.
    """
    value = 0.35
    if itinerary.total_price is not None:
        value += 0.20
    availability = itinerary.legs[0].availability
    if availability is not None:
        value += 0.05 if availability.stale else 0.20
    if _worst_delay_probability(itinerary) is not None:
        value += 0.15
    if all(leg.geometry is not None for leg in itinerary.legs):
        # Geometry only matters when the whole trip can be drawn; half a line on
        # a map is worse than no map.
        value += 0.05
    return round(min(1.0, value), 4)


def _warn_missing(itinerary: Itinerary) -> None:
    """Say out loud which facts are missing, because they changed the score.

    This is the promise that a missing input cannot become a silent advantage. A
    train with no known fare is not "as cheap as" a priced one; it simply did not
    take part in the price comparison, and the user is told so.
    """
    warnings = list(itinerary.warnings or [])
    if itinerary.total_price is None:
        warnings.append("No fare available for this train, so price did not affect its ranking.")
    if _worst_delay_probability(itinerary) is None:
        # Deliberately does not say why. The reason differs (no model built, or a
        # model that has never seen this train) and the engine cannot tell which,
        # so it states only what is true in both cases.
        warnings.append("No punctuality estimate for this train, so its risk band is unknown.")
    if itinerary.legs[0].availability is None:
        warnings.append("Seat availability unknown: check the class and quota on the booking page.")
    itinerary.warnings = _dedupe(warnings)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


# -- dominance ----------------------------------------------------------------


def _dominates(better: Itinerary, worse: Itinerary) -> bool:
    """True when `better` is no worse on every comparable dimension and better on one.

    Every dimension here is "less is better". If either side is missing a value
    on any of them, this returns False and the pair is judged incomparable.
    Declaring dominance anyway would be using an unknown as a loss for one side,
    which is the same error as scoring a gap as average, one level up.
    """
    strictly_better = False
    compared = 0
    for name in ("price", "duration", "delay_risk"):
        left = _raw_dimension([better], name)[0]
        right = _raw_dimension([worse], name)[0]
        if left is None or right is None:
            return False
        compared += 1
        if left < right:
            strictly_better = True
        elif left > right:
            return False
    return compared > 0 and strictly_better


def _sort_key(itinerary: Itinerary) -> str:
    first = itinerary.legs[0]
    return first.leg_id if first.leg_id else itinerary.itinerary_id
