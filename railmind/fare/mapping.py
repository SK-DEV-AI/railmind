"""Which printed table a class is read from.

The circular publishes several tables rather than one: Mail/Express non-AC,
Mail/Express AC, Rajdhani, and then the dedicated trains (Shatabdi, Vande Bharat,
Duronto, ...) further into the document. Only the first three are transcribed so
far, so this module maps a class onto those three and refuses anything else.

The rule it encodes is deliberately narrow:

- A Rajdhani train is priced from the Rajdhani table whatever the class, because
  Rajdhani carries its own base fare.
- Otherwise a class the non-AC table carries (`2S`, `SL`, `FC`) comes from the
  non-AC table, and any other class comes from the AC table.

A caller that knows its train carries a dedicated table this module does not have
must pass the section explicitly and surface the gap, rather than let a Shatabdi
Chair Car be priced off the Mail/Express AC table.
"""

from __future__ import annotations

from railmind.fare.table import FareTable, FareTableError

NON_AC_CLASSES = ("2S", "SL", "FC")


class UnknownServiceError(LookupError):
    """The train's table is not transcribed, so no fare can be quoted from it."""


def section_for(class_code: str, *, rajdhani: bool = False) -> str:
    base = class_code.split("_")[0]
    if rajdhani:
        return "rajdhani"
    return "mail_express_non_ac" if base in NON_AC_CLASSES else "mail_express_ac"


def supported(table: FareTable, section: str, class_code: str) -> bool:
    """Whether this table prints a fare for the class in that section at all."""
    base = class_code.split("_")[0]
    try:
        classes = table.classes(section)
    except FareTableError:
        return False
    return any(name == base or name.startswith(f"{base}_") for name in classes)


def resolve(table: FareTable, class_code: str, *, rajdhani: bool = False) -> str:
    """The section to read, or a refusal that names what is missing."""
    section = section_for(class_code, rajdhani=rajdhani)
    if not supported(table, section, class_code):
        raise UnknownServiceError(
            f"the {section} table prints no {class_code} column; the circular prices "
            f"that class in a table this engine has not transcribed yet"
        )
    return section
