"""Deterministic cell-level grounding for all numbers in the brief."""
from decimal import Decimal, InvalidOperation
import re


NUMBER_WORDS = re.compile(
    r"\b(?:(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:fold)?|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|"
    r"eighteenth|nineteenth|twentieth|thirtieth|fortieth|fiftieth|sixtieth|"
    r"seventieth|eightieth|ninetieth|"
    r"(?:hundred|thousand|[a-z]+illion|googol(?:plex)?)(?:th)?|"
    r"half|halves|halve(?:d)?|halving|quarter|dozen|couple|single|once|twice|thrice|"
    r"double(?:d)?|doubling|triple(?:d)?|tripling|quadruple(?:d)?|quadrupling)s?\b", re.IGNORECASE)


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
        return (isinstance(cell, str) and str(value) == cell
                and bool(re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", cell)))
    except (KeyError, IndexError, TypeError, InvalidOperation, ValueError):
        return False


def critique(claims, evidence):
    """Drop an entire claim if a numeric part lacks an exact cited SQL cell.

    Plain text cannot carry numeric characters or number words; numerical
    segments must name the evidence, row, column and exact value. String cells
    are restricted to YYYY-MM dates. Policy text is evidence for qualitative
    reasons only, never an authority for business numbers. Numeric suffixes are
    limited to "", "%", and " percentage points", without checking cell units.
    """
    kept, dropped = [], []
    for claim in claims:
        valid = bool(claim.get("parts"))
        rendered = ""
        text_runs = [""]
        number_tails = []
        for part in claim.get("parts", []):
            if "text" in part:
                text_runs[-1] += part["text"]
                if number_tails:
                    number_tails[-1] += part["text"]
                rendered += part["text"]
            else:
                # A sign must belong to the cited value, even across text parts.
                valid &= not bool(re.search(r"[+\-\u2212]\s*$", rendered))
                valid &= supported(part, evidence)
                # Only literal units are supported; magnitude conversions are not.
                valid &= part.get("suffix", "") in ("", "%", " percentage points")
                rendered += str(part.get("value", "")) + str(part.get("suffix") or "")
                number_tails.append("")
                text_runs.append("")
        # Check complete text runs so splitting a number word across parts cannot hide it.
        valid &= not any(NUMBER_WORDS.search(run) or any(c.isnumeric() for c in run)
                         for run in text_runs)
        # Join adjacent text parts so splitting a modifier cannot hide it.
        valid &= not any(re.match(
            r"\s*(?:%|(?:hundred|thousand|million|billion|trillion|percent|percentage|"
            r"points?|basis|bps|[kmbt])\b)", tail, re.IGNORECASE)
            for tail in number_tails)
        valid &= all(p in evidence and evidence[p].get("kind") == "policy"
                     for p in claim.get("policy_ids", []))
        (kept if valid else dropped).append(claim)
    return kept, dropped
