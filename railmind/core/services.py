"""Named train brands a traveller can ask for by name.

"Book me a Vande Bharat tomorrow" is a different request from "book me a train
tomorrow": the brand is the thing the person has already decided, five of them
are priced from tables the circular does not print in the Mail/Express section,
and a shortlist that answers a Vande Bharat question with a 21:15 Mail/Express is
answering a question nobody asked.

One table, used from both ends of the pipe:

- the intent parser turns spoken words ("vande bharat", "garibrath") into keys;
- the provider turns a key into "does this train's name carry the marker".

Neither side invents a brand. A key that is not in `BRANDS` is a contract
mistake, not a silent no-op, and the markers are matched against the train name
we already hold, so a brand filter costs no request and cannot hallucinate a
train that does not exist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Brand:
    """One named service: what a human says, what the timetable prints."""

    key: str
    label: str
    #: Spoken forms, lower case. Matched as whole words against the sentence.
    phrases: tuple[str, ...]
    #: Substrings that identify the brand inside a train name, upper case.
    markers: tuple[str, ...]

    def matches(self, train_name: str) -> bool:
        upper = train_name.upper()
        return any(marker in upper for marker in self.markers)


BRANDS: tuple[Brand, ...] = (
    Brand(
        key="vande-bharat",
        label="Vande Bharat",
        phrases=("vande bharat", "vandebharat", "vande bharath"),
        markers=("VANDE BHARAT", "VANDEBHARAT"),
    ),
    Brand(
        key="rajdhani",
        label="Rajdhani",
        phrases=("rajdhani", "rajdhani express"),
        markers=("RAJDHANI",),
    ),
    Brand(
        key="shatabdi",
        label="Shatabdi",
        phrases=("shatabdi", "shatabdi express"),
        markers=("SHATABDI",),
    ),
    Brand(
        key="duronto",
        label="Duronto",
        phrases=("duronto", "duronto express"),
        markers=("DURONTO",),
    ),
    Brand(
        key="garib-rath",
        label="Garib Rath",
        phrases=("garib rath", "garibrath", "garib-rath"),
        markers=("GARIB RATH", "GARIBRATH"),
    ),
    Brand(
        key="tejas",
        label="Tejas",
        phrases=("tejas", "tejas express"),
        markers=("TEJAS",),
    ),
    Brand(
        key="gatimaan",
        label="Gatimaan",
        phrases=("gatimaan", "gatiman"),
        markers=("GATIMAAN",),
    ),
    Brand(
        key="humsafar",
        label="Humsafar",
        phrases=("humsafar", "humsafar express"),
        markers=("HUMSAFAR",),
    ),
    Brand(
        key="jan-shatabdi",
        label="Jan Shatabdi",
        phrases=("jan shatabdi", "jan-shatabdi"),
        markers=("JAN SHATABDI", "JANSHATABDI"),
    ),
    Brand(
        key="amrit-bharat",
        label="Amrit Bharat",
        phrases=("amrit bharat", "amritbharat"),
        markers=("AMRIT BHARAT", "AMRITBHARAT"),
    ),
    Brand(
        key="antyodaya",
        label="Antyodaya",
        phrases=("antyodaya", "antyodaya express"),
        markers=("ANTYODAYA",),
    ),
    Brand(
        key="yuva",
        label="Yuva",
        phrases=("yuva", "yuva express"),
        markers=("YUVA",),
    ),
)

_BY_KEY: dict[str, Brand] = {brand.key: brand for brand in BRANDS}


def keys() -> tuple[str, ...]:
    """Every brand key, in table order. The contract's enum lives off this."""
    return tuple(brand.key for brand in BRANDS)


def brand(key: str) -> Brand:
    """The brand for a key, or `KeyError`. A typo must not become a silent pass."""
    return _BY_KEY[key]


def label(key: str) -> str:
    """The human name for a key, falling back to the key itself."""
    found = _BY_KEY.get(key)
    return found.label if found is not None else key


def labels(keys: tuple[str, ...] | list[str]) -> str:
    """ "Vande Bharat and Shatabdi" for a warning sentence."""
    names = [label(key) for key in keys]
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def matches(key: str, train_name: str) -> bool:
    found = _BY_KEY.get(key)
    return found is not None and found.matches(train_name)


def without_brands(text: str) -> str:
    """The sentence with brand names removed.

    Brand words sit where a station name sits ("mumbai to pune vande bharat"),
    and leaving them in makes the place resolver read "pune vande bharat" as one
    place name, which matches nothing and asks the user a question they already
    answered. Removing the phrase we just recognised costs nothing and keeps the
    route parse honest.
    """
    result = text
    for item in BRANDS:
        for phrase in item.phrases:
            result = re.sub(
                rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])",
                " ",
                result,
                flags=re.IGNORECASE,
            )
    return result


def from_text(text: str) -> list[str]:
    """Brand keys mentioned in a sentence, in the order they appear.

    Whole-word matching, because "tejas" inside a station name would otherwise
    filter a shortlist down to a coincidence.
    """
    found: list[tuple[int, str]] = []
    for item in BRANDS:
        for phrase in item.phrases:
            match = re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text)
            if match is not None:
                if item.key not in [key for _, key in found]:
                    found.append((match.start(), item.key))
                break
    return [key for _, key in sorted(found)]
