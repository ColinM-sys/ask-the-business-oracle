"""Fixed, auditable business analysis; no language model or API key needed."""
from hashlib import sha256
import re

from critic import critique
from policies import DOCS
from tools import run_sql, search_policies


class QuestionValidationError(ValueError):
    """A user-facing problem with the requested analysis."""


# SHA-256 of the complete, whitespace-normalized chunks reviewed for the fixed
# claims below. Changed or partial chunks require reviewing both the claims and
# these fingerprints; deriving them from current files would approve edits blindly.
REVIEWED_POLICY_HASHES = {
    "freight_contract_memo.md": "24ae9302b18048c3ff2d8fc47ccae10ff4a3ba4903c25597d80486a83f528ceb",
    "pricing_policy.md": "5f208bfcde2f07d8bc60a113a8462ae37b92ac1e16375f31ed13e5da1c4adaba",
    "regional_playbook.md": "daf0bc450ad707035f7697abb7715583418c0b6605dedd878dec7416501a5595",
    "west_q3_volume_push.md": "5dc8fd6cd5ce406fcd582abb8220e16d24dbf5fe7a133eb798c5bf871a3f8c1f",
}

# The fixed claims below were reviewed against these complete document versions.
# Review the claims again before accepting any changed document digest.
POLICY_SHA256 = {
    "west_q3_volume_push.md": "f5c98d964fc1b7b6a872152b15fef8044ff7a30797a3a65e1b5891e152bf3b71",
    "freight_contract_memo.md": "8b73b6dbad7be9dcdc24759e02da963a465420d4a00f1c0c8b0940c4dd6da747",
    "pricing_policy.md": "79264bb14aae2da366604872c36b2b4e4ae26a27459b7cb801873e0df8f8fac0",
    "regional_playbook.md": "6a3fd585c58b2528206a9545db63db828c836ce0a265e1fb3fc90525d6ab03d8",
}

JOIN = """FROM sales s LEFT JOIN
    (SELECT region_id, product_id, cost_month, MIN(unit_cogs) AS unit_cogs,
        MIN(freight_per_unit) AS freight_per_unit FROM costs
        GROUP BY region_id, product_id, cost_month HAVING COUNT(*)=1) c ON c.region_id=s.region_id
    AND c.product_id=s.product_id AND c.cost_month=s.sale_month
    JOIN regions r ON r.region_id=s.region_id"""
MISSING_DISCOUNT_COUNT = """SUM(CASE WHEN s.discount_pct IS NULL OR s.quantity IS NULL
    OR s.unit_price IS NULL THEN 1 ELSE 0 END)"""
MONTHLY_SQL = f"""SELECT r.region_name, s.sale_month,
    ROUND(SUM(s.net_revenue),2) AS revenue,
    SUM(s.quantity) AS units,
    ROUND(100*SUM(s.net_revenue-s.quantity*(c.unit_cogs+c.freight_per_unit))
        /NULLIF(SUM(s.net_revenue),0),2) AS margin_pct,
    CASE WHEN {MISSING_DISCOUNT_COUNT}=0 THEN
        ROUND(SUM(s.quantity*s.unit_price*s.discount_pct)
            /NULLIF(SUM(s.quantity*s.unit_price),0),2) END AS discount_pct,
    ROUND(SUM(s.quantity*c.freight_per_unit)/NULLIF(SUM(s.quantity),0),2) AS freight_per_unit,
    SUM(CASE WHEN c.unit_cogs IS NULL OR c.freight_per_unit IS NULL THEN 1 ELSE 0 END) AS missing_cost_count,
    {MISSING_DISCOUNT_COUNT} AS missing_discount_count
    {JOIN} GROUP BY r.region_name,s.sale_month ORDER BY r.region_name,s.sale_month"""


def summary_sql():
    # Compute changes in SQL too: a difference calculated in Python is not evidence.
    columns = []
    raw = {}
    for period, condition in (("before", "s.sale_month < :cutoff"), ("after", "s.sale_month >= :cutoff")):
        def total(expression):
            return f"SUM(CASE WHEN {condition} THEN {expression} ELSE 0 END)"
        revenue = total("s.net_revenue")
        units = total("s.quantity")
        raw[period] = {
            "margin_pct": f"100*{total('s.net_revenue-s.quantity*(c.unit_cogs+c.freight_per_unit)')}/NULLIF({revenue},0)",
            "discount_pct": f"CASE WHEN {MISSING_DISCOUNT_COUNT}=0 THEN {total('s.quantity*s.unit_price*s.discount_pct')}/NULLIF({total('s.quantity*s.unit_price')},0) END",
            "freight_per_unit": f"{total('s.quantity*c.freight_per_unit')}/NULLIF({units},0)",
        }
        for name, expression in raw[period].items():
            columns.append(f"ROUND({expression},2) AS {period}_{name}")
    columns.extend([
        "SUM(CASE WHEN c.unit_cogs IS NULL OR c.freight_per_unit IS NULL THEN 1 ELSE 0 END) AS missing_cost_count",
        f"{MISSING_DISCOUNT_COUNT} AS missing_discount_count",
        f"CASE WHEN ({raw['before']['margin_pct']}) > ({raw['after']['margin_pct']}) THEN 'fell' WHEN ({raw['before']['margin_pct']}) < ({raw['after']['margin_pct']}) THEN 'rose' ELSE 'was unchanged' END AS margin_direction",
        f"ROUND(({raw['before']['margin_pct']})-({raw['after']['margin_pct']}),2) AS margin_drop_pts",
        f"ROUND(100*(({raw['after']['freight_per_unit']})/NULLIF(({raw['before']['freight_per_unit']}),0)-1),2) AS freight_increase_pct",
        "MIN(s.sale_month) AS first_month", "MAX(s.sale_month) AS last_month",
        "MIN(CASE WHEN s.sale_month >= :cutoff THEN s.sale_month END) AS after_first_month",
        "MAX(CASE WHEN s.sale_month < :cutoff THEN s.sale_month END) AS before_last_month",
    ])
    return f"SELECT r.region_name, {', '.join(columns)} {JOIN} GROUP BY r.region_name ORDER BY r.region_name"


MONTHS = {name: f"{index:02}" for index, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"), 1)}
MONTHS.update({name[:3]: value for name, value in list(MONTHS.items())})
MONTHS["sept"] = "09"


def parse_question(question):
    unsupported = re.search(
        r"\b(north|northwest|northeast|southwest|southeast)\b", question, re.I)
    if unsupported:
        raise QuestionValidationError(f"Unsupported region: {unsupported[0]}. Choose West, Central, East, or South.")
    region_matches = [r for r in ("West", "Central", "East", "South")
                      if re.search(rf"\b{r}\b", question, re.I)]
    if len(region_matches) > 1:
        raise QuestionValidationError("Ask about one region at a time; the evidence includes all regions for comparison.")
    cutoff = "2025-07"
    date = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])\b(?![-/\d])", question)
    month_pattern = r"\b(" + "|".join(MONTHS) + r")\b"
    named = re.search(month_pattern + r"\.?\s+(20\d{2})\b(?![-/\d])", question, re.I)
    if date:
        cutoff = date.group(0)
    elif named:
        cutoff = named[2] + "-" + MONTHS[named[1].lower()]
    # A recognized date must not hide another unsupported date expression.
    matched = date or named
    remaining = question[:matched.start()] + question[matched.end():] if matched else question
    if (re.search(r"\b\d{4}\b|\b\d+[-/]\d+\b", remaining)
            or re.search(month_pattern, remaining, re.I)):
        raise QuestionValidationError("Unsupported date; use YYYY-MM or a month name and four-digit year.")
    return region_matches[0] if region_matches else "West", cutoff


def answer_question(question):
    if not isinstance(question, str) or not question.strip() or len(question) > 1000:
        raise QuestionValidationError("Enter a business question of up to one thousand characters.")
    region, cutoff = parse_question(question)
    evidence = {
        "E1": {"kind": "sql", **run_sql(MONTHLY_SQL)},
        "E2": {"kind": "sql", **run_sql(summary_sql(), {"cutoff": cutoff})},
    }
    rows = evidence["E2"]["rows"]
    index = next((i for i, row in enumerate(rows) if row["region_name"] == region), None)
    if index is None:
        raise QuestionValidationError(f"No sales/cost data found for {region}.")
    if any(row["missing_cost_count"] for key in ("E1", "E2") for row in evidence[key]["rows"]):
        raise ValueError("Missing monthly costs or duplicate cost rows for some sales; cannot analyze incomplete or ambiguous data.")
    if any(row["missing_discount_count"] for key in ("E1", "E2") for row in evidence[key]["rows"]):
        raise ValueError("Missing discount inputs for some sales; cannot analyze incomplete data.")
    row = rows[index]
    if row["before_margin_pct"] is None or row["after_margin_pct"] is None:
        raise QuestionValidationError("The comparison date must have sales both before and after it.")
    policies = search_policies(f"{region} margin discount freight policy " + question)
    for policy in policies["rows"]:
        evidence[f"P{policy['id']}"] = {"kind": "policy",
            "sql": f"SELECT * FROM (\n{policies['sql']}\n) WHERE id = :policy_id",
            "params": {**policies["params"], "policy_id": policy["id"]}, "rows": [policy]}

    def text(value):
        return {"text": value}

    def number(column, suffix="", row_index=index):
        return {"value": rows[row_index][column], "evidence": "E2", "row": row_index,
                "column": column, "suffix": suffix}

    def policy_ids(filename):
        # Verify the whole source too: an old chunk may survive a contradictory addendum.
        try:
            source = (DOCS / filename).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return []
        if sha256(source.encode("utf-8")).hexdigest() != POLICY_SHA256[filename]:
            return []
        # These reviewed documents each fit in one chunk; partial text is not proof.
        citations = []
        for key, value in evidence.items():
            if value["kind"] != "policy":
                continue
            policy = value["rows"][0]
            chunk = policy.get("chunk")
            if policy["doc"] == filename and isinstance(chunk, str):
                fingerprint = sha256(" ".join(chunk.split()).encode("utf-8")).hexdigest()
                if fingerprint == REVIEWED_POLICY_HASHES[filename]:
                    citations.append(key)
        return citations

    claims = []
    def claim(kind, parts, citations=None):
        claims.append({"kind": kind, "parts": parts, "policy_ids": citations or []})

    claim("scope", [text(f"{region}: compare "), number("first_month"), text(" through "),
        number("before_last_month"), text(" with "), number("after_first_month"), text(" through "),
        number("last_month"), text(". Margin includes COGS and freight; percentages are revenue-weighted. Discounts are weighted by gross sales; freight is weighted by units.")])
    direction = row["margin_direction"]
    precision_note = (" The change is below display precision."
                      if row["margin_drop_pts"] == 0 and direction != "was unchanged" else "")
    # The signed drop is kept intact, including for regions whose margin rose.
    claim("finding", [text(f"{region} gross margin {direction} from "), number("before_margin_pct", "%"),
        text(" to "), number("after_margin_pct", "%"), text("; the signed margin drop is "),
        number("margin_drop_pts", " percentage points"), text("." + precision_note)])
    freight_parts = [text("Average discount moved from "), number("before_discount_pct", "%"),
        text(" to "), number("after_discount_pct", "%"), text(". Freight per unit moved from $"),
        number("before_freight_per_unit"), text(" to $"), number("after_freight_per_unit")]
    if row["freight_increase_pct"] is not None:
        freight_parts.extend([text(" (change: "), number("freight_increase_pct", "%"), text(")")])
    claim("finding", freight_parts + [text(".")])
    for other_index, other in enumerate(rows):
        if other_index != index:
            claim("comparison", [text(f"{other['region_name']} margin: "),
                number("before_margin_pct", "%", other_index), text(" before; "),
                number("after_margin_pct", "%", other_index), text(" after.")])
    if region == "West" and cutoff == "2025-07":
        push, freight = policy_ids("west_q3_volume_push.md"), policy_ids("freight_contract_memo.md")
        if push and freight:
            impact = ("These changes are consistent with the observed margin decline"
                      if direction == "fell" else f"The observed margin {direction}")
            claim("reason", [text("The policy documents identify a regional volume discount push and a replacement freight contract. The discount program bypassed senior sales approval and Finance review. "
                f"{impact}; this comparison does not isolate their individual causal effects.")], push + freight)
    else:
        claim("reason", [text("Use the discount and freight movements to investigate the margin change. The retrieved West-specific documents do not establish a cause for this region and comparison period.")])
    pricing = policy_ids("pricing_policy.md")
    playbook = policy_ids("regional_playbook.md")
    if pricing and playbook:
        claim("action", [text("Review discount approvals with Sales and Finance; end broad programs that fail the margin review and use targeted deal discounts.")], pricing + playbook)
        claim("action", [text("Check customer eligibility for a freight surcharge and apply it where pricing policy allows.")], pricing + playbook)
        claim("action", [text("Re-tender freight lanes and consolidate shipments of heavy products.")], playbook)
    kept, dropped = critique(claims, evidence)
    return {"question": question, "region": region, "cutoff": cutoff, "claims": kept,
            "evidence": evidence, "dropped_count": len(dropped),
            "plan": ["Compare region and month margin", "Compare discount and freight", "Retrieve policy reasons", "Verify every numerical claim"]}


if __name__ == "__main__":
    result = answer_question("Why did West margin fall since July 2025?")
    for claim in result["claims"]:
        print("".join(p.get("text", str(p.get("value", "")) + p.get("suffix", "")) for p in claim["parts"]))
