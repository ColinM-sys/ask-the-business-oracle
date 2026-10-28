"""Deterministic cell-level grounding for all numbers in the brief."""
from decimal import Decimal, InvalidOperation
import re


# Plain text may not introduce numbers or signs, even in words.  Keep the
# boundaries separate from the alternatives so ordinary words such as
# "someone" do not match "one".
NUMBER_WORD = re.compile(
    r"(?<![^\W_])(?:"
    r"zero|nought|naught|nil|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|"
    r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"twenties|thirties|forties|fifties|sixties|seventies|eighties|nineties|"
    r"hundred|thousand|million|billion|trillion|quadrillion|quintillion|"
    r"sextillion|septillion|octillion|nonillion|decillion|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
    r"eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|"
    r"eighteenth|nineteenth|twentieth|thirtieth|fortieth|fiftieth|sixtieth|"
    r"seventieth|eightieth|ninetieth|hundredth|thousandth|millionth|billionth|"
    r"half|halves|quarter|dozen|couple|pair|single|double|triple|once|twice|thrice|"
    r"minus|negative|plus|positive"
    r")(?:s|es|th|ths|fold)?(?![^\W_])", re.IGNORECASE)


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

    Plain text cannot carry digits, number words or signs; numerical segments must
    name the evidence, row, column and exact value. Policy text is evidence for
    qualitative reasons only, never an authority for business numbers. Units
    and magnitudes cannot be appended as plain text; use validated suffixes.
    """
    kept, dropped = [], []
    for claim in claims:
        valid = bool(claim.get("parts"))
        rendered = ""
        number_tails = []
        text_runs = [""]
        for part in claim.get("parts", []):
            if "text" in part:
                valid &= not bool(re.search(r"\d", part["text"]))
                text_runs[-1] += part["text"]
                if number_tails:
                    number_tails[-1] += part["text"]
                rendered += part["text"]
            else:
                # A sign must belong to the cited value, even across text parts.
                valid &= not bool(re.search(r"[+\-\u2212]\s*(?:[$£€]\s*)?$", rendered))
                valid &= supported(part, evidence)
                # Only literal units are supported; magnitude conversions are not.
                valid &= part.get("suffix", "") in ("", "%", " percentage points")
                rendered += str(part.get("value", "")) + str(part.get("suffix") or "")
                number_tails.append("")
                text_runs.append("")
        # Join adjacent text parts so splitting a modifier cannot hide it.
        valid &= not any(NUMBER_WORD.search(text) for text in text_runs)
        valid &= not any(re.match(
            r"\s*(?:%|(?:hundred|thousand|million|billion|trillion|percent|percentage|"
            r"points?|basis|bps|[kmbt])\b)", tail, re.IGNORECASE)
            for tail in number_tails)
        valid &= all(p in evidence and evidence[p].get("kind") == "policy"
                     for p in claim.get("policy_ids", []))
        (kept if valid else dropped).append(claim)
    return kept, dropped
