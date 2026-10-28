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
    """Drop an entire claim if a number lacks an exact cited SQL cell.

    Plain text cannot carry digits (including dates); numerical segments must
    name the evidence, row, column and exact value. Policy text is evidence for
    qualitative reasons only, never an authority for business numbers.
    """
    kept, dropped = [], []
    for claim in claims:
        valid = bool(claim.get("parts"))
        for part in claim.get("parts", []):
            if "text" in part:
                valid &= not bool(re.search(r"\d", part["text"]))
            else:
                valid &= supported(part, evidence)
                # Cells are matched without conversion; scaling suffixes are unsupported.
                valid &= part.get("suffix", "") in ("", "%", " percentage points")
        valid &= all(p in evidence and evidence[p].get("kind") == "policy"
                     for p in claim.get("policy_ids", []))
        (kept if valid else dropped).append(claim)
    return kept, dropped
