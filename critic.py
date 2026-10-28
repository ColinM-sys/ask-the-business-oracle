"""Deterministic cell-level grounding for all numbers in the brief."""
from decimal import Decimal, InvalidOperation
import re


NUMBER_WORD = re.compile(
    r"(?<![^\W\d_])(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
    r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|"
    r"forty|fifty|sixty|seventy|eighty|ninety|hundred|thousand|million|billion|"
    r"trillion|quadrillion|quintillion|sextillion|septillion|octillion|nonillion|decillion|"
    r"first|second|third|fifth|eighth|ninth|twelfth|twentieth|thirtieth|fortieth|"
    r"fiftieth|sixtieth|seventieth|eightieth|ninetieth|half|halves|quarter|dozen|"
    r"single|pair|couple|once|twice|thrice|double|doubled|doubling|triple|tripled|tripling)"
    r"(?:s|th|ths)?(?![^\W\d_])", re.IGNORECASE)


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
    """Drop an entire claim if a number lacks an exact cited SQL cell.

    Plain text cannot carry digits or number words; numerical segments must
    name the evidence, row, column and exact value. Policy text is evidence for
    qualitative reasons only, never an authority for business numbers. Units
    and magnitudes cannot be appended as plain text; use validated suffixes.
    """
    kept, dropped = [], []
    for claim in claims:
        valid = bool(claim.get("parts"))
        rendered = ""
        number_tails = []
        cited_values = []
        for part in claim.get("parts", []):
            if "text" in part:
                valid &= not bool(re.search(r"\d", part["text"]))
                if number_tails:
                    number_tails[-1] += part["text"]
                rendered += part["text"]
            else:
                # A sign must belong to the cited value, even across text parts.
                valid &= not bool(re.search(r"[+\-\u2212]\s*$", rendered))
                valid &= supported(part, evidence)
                # Only literal units are supported; magnitude conversions are not.
                valid &= part.get("suffix", "") in ("", "%", " percentage points")
                value = str(part.get("value", ""))
                cited_values.append((len(rendered), len(rendered) + len(value)))
                rendered += value + str(part.get("suffix") or "")
                number_tails.append("")
        # A word assembled across parts is not supported by any one cited cell.
        valid &= all(any(start <= match.start() and match.end() <= end
                         for start, end in cited_values)
                     for match in NUMBER_WORD.finditer(rendered))
        # Join adjacent text parts so splitting a modifier cannot hide it.
        valid &= not any(re.match(
            r"\s*(?:%|(?:hundred|thousand|million|billion|trillion|percent|percentage|"
            r"points?|basis|bps|[kmbt])\b)", tail, re.IGNORECASE)
            for tail in number_tails)
        valid &= all(p in evidence and evidence[p].get("kind") == "policy"
                     for p in claim.get("policy_ids", []))
        (kept if valid else dropped).append(claim)
    return kept, dropped
