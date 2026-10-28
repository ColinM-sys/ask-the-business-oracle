"""Deterministic cell-level grounding for all numbers in the brief."""
from decimal import Decimal, InvalidOperation
import re


def supported(part, evidence):
    source = evidence.get(part.get("evidence"), {})
    if source.get("kind") != "sql":
        return False
    try:
        row_index = part["row"]
        if not isinstance(row_index, int) or row_index < 0:
            return False
        cell = source["rows"][row_index][part["column"]]
        value = part["value"]
        if isinstance(cell, (int, float, Decimal)) and not isinstance(cell, bool):
            return Decimal(str(value)).is_finite() and Decimal(str(value)) == Decimal(str(cell))
        return isinstance(cell, str) and str(value) == cell
    except (KeyError, IndexError, TypeError, InvalidOperation, ValueError):
        return False


def critique(claims, evidence):
    """Drop an entire claim if a numeric part lacks an exact cited SQL cell.

    Plain text cannot carry Unicode numeric characters (including dates);
    numerical segments must name the evidence, row, column and exact value.
    Written-out numbers and ASCII Roman numerals are not checked. Policy text is
    evidence for qualitative reasons only, never an authority for business
    numbers. Numeric suffixes are limited to "", "%", and " percentage points",
    without checking the cell's units. The adjacent-text check rejects certain
    unit and magnitude tokens only at the start of the text after a number,
    after whitespace and joining adjacent text parts; it does not catch other
    wording such as ' (in millions)'. Other unit or magnitude modifiers, such
    as "quadrillion", may pass unchecked.
    """
    kept, dropped = [], []
    for claim in claims:
        valid = bool(claim.get("parts"))
        rendered = ""
        number_tails = []
        for part in claim.get("parts", []):
            if "text" in part:
                valid &= not any(character.isnumeric() for character in part["text"])
                if number_tails:
                    number_tails[-1] += part["text"]
                rendered += part["text"]
            else:
                # A sign must belong to the cited value, even across text parts.
                valid &= not bool(re.search(r"[+\-\u2212]\s*$", rendered))
                valid &= supported(part, evidence)
                # Limit suffixes; the cited cell's units are not checked.
                valid &= part.get("suffix", "") in ("", "%", " percentage points")
                rendered += str(part.get("value", "")) + str(part.get("suffix") or "")
                number_tails.append("")
        # Join adjacent text parts so splitting a modifier cannot hide it.
        valid &= not any(re.match(
            r"\s*(?:%|(?:hundred|thousand|million|billion|trillion|percent|percentage|"
            r"points?|basis|bps|[kmbt])\b)", tail, re.IGNORECASE)
            for tail in number_tails)
        valid &= all(p in evidence and evidence[p].get("kind") == "policy"
                     for p in claim.get("policy_ids", []))
        (kept if valid else dropped).append(claim)
    return kept, dropped
