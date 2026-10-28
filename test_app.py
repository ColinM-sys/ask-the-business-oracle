"""Unit checks and live integration against the existing local Oracle tables."""
import copy
import json
import runpy
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tools
from db import connect
from agent import answer_question, parse_question
from app import app
from critic import critique, supported
from policies import ensure_policies, hash_embedding


@pytest.mark.parametrize("sql", [
    "DELETE FROM sales", "DROP TABLE sales", "UPDATE sales SET quantity=0",
    "INSERT INTO sales (sale_id) VALUES (0)", "ALTER TABLE sales ADD x NUMBER",
    "GRANT SELECT ON sales TO public", "BEGIN NULL; END;", "EXEC something",
    "SELECT * FROM sales; DELETE FROM sales", "SELECT * FROM sales FOR UPDATE",
    "SELECT * INTO shadow FROM sales", "SELECT my_function() FROM sales",
    "SELECT dbms_random.value() FROM sales", "SELECT seq.nextval FROM sales",
    "SELECT * FROM user_users", "SELECT * FROM other.sales",
    "WITH x AS (SELECT * FROM sales) DELETE FROM sales", "", "-- just a comment",
    "SELECT UTL_HTTP.REQUEST('https://example.com') FROM sales",
    "SELECT * FROM sales UNION ALL SELECT * FROM sales",
    "SELECT * FROM sales FOR UPDATE SKIP LOCKED", "SELECT * FROM sales; COMMIT",
    "SELECT 'unterminated FROM sales",
])
def test_guard_refuses_before_connect(sql, monkeypatch):
    def forbidden_connection():
        pytest.fail("Rejected SQL must never connect")
    monkeypatch.setattr(tools, "connect", forbidden_connection)
    with pytest.raises(ValueError):
        tools.run_sql(sql)


def test_guard_accepts_comments_and_harmless_string():
    tools.validate_select("/* report */ SELECT COUNT(*) FROM sales WHERE sale_month=:month;")
    tools.validate_select("SELECT 'DROP TABLE sales' AS harmless FROM regions")


@pytest.fixture
def sample_evidence():
    return {"E1": {"kind": "sql", "sql": "SELECT ...", "rows": [{"margin": 25.53}]},
            "P1": {"kind": "policy", "rows": [{"margin": 99}]}}


def test_critic_drops_unsupported_numbers_and_preserves_valid(sample_evidence):
    valid = {"kind": "finding", "parts": [{"text": "Margin: "},
        {"value": "25.53", "suffix": "%", "evidence": "E1", "row": 0, "column": "margin"}]}
    invented = copy.deepcopy(valid)
    invented["parts"][1]["value"] = "99"
    uncited = {"parts": [{"text": "Margin fell 13 points."}]}
    bad_policy = {"parts": [{"text": "A reason."}], "policy_ids": ["missing"]}
    kept, dropped = critique([valid, invented, uncited, bad_policy], sample_evidence)
    assert kept == [valid]
    assert len(dropped) == 3


@pytest.mark.parametrize("change", [
    {"row": -1}, {"row": 10}, {"column": "missing"}, {"evidence": "missing"},
    {"value": "NaN"}, {"value": "Infinity"}, {"evidence": "P1", "value": 99},
])
def test_critic_refuses_invalid_cell_references(sample_evidence, change):
    part = {"value": 25.53, "evidence": "E1", "row": 0, "column": "margin", **change}
    assert not supported(part, sample_evidence)


@pytest.mark.parametrize("prefix", ["Margin: -", "Margin: +", "Margin: \u2212", "Margin: - "])
@pytest.mark.parametrize("split", [False, True])
def test_critic_refuses_signs_outside_cited_value(sample_evidence, prefix, split):
    texts = [{"text": prefix}]
    if split:
        texts = [{"text": prefix[:-1]}, {"text": prefix[-1:]}, {"text": ""}]
    claim = {"parts": texts + [
        {"value": 25.53, "evidence": "E1", "row": 0, "column": "margin"}]}
    assert critique([claim], sample_evidence) == ([], [claim])


def test_critic_preserves_cited_negative_value(sample_evidence):
    sample_evidence["E1"]["rows"][0]["margin"] = -25.53
    claim = {"parts": [{"text": "Margin: "},
        {"value": -25.53, "suffix": "%", "evidence": "E1", "row": 0, "column": "margin"}]}
@pytest.mark.parametrize("suffix", [" million", " billion", "k", "M", "e6", "0", " percent", None])
def test_critic_rejects_unsupported_suffixes(sample_evidence, suffix):
    claim = {"parts": [{"value": 25.53, "suffix": suffix,
                        "evidence": "E1", "row": 0, "column": "margin"}]}
    assert critique([claim], sample_evidence) == ([], [claim])


@pytest.mark.parametrize("suffix", [None, "", "%", " percentage points"])
def test_critic_preserves_supported_suffixes(sample_evidence, suffix):
    part = {"value": 25.53, "evidence": "E1", "row": 0, "column": "margin"}
    if suffix is not None:
        part["suffix"] = suffix
    claim = {"parts": [part]}
    assert critique([claim], sample_evidence) == ([claim], [])


def test_hashing_is_stable_and_normalized():
    first = hash_embedding("West freight contract")
    assert first == hash_embedding("West freight contract")
    assert abs(sum(v*v for v in first) - 1) < 0.000001


@pytest.mark.parametrize("character", ["界", "😀"])
def test_policy_chunks_fit_byte_limit(character, tmp_path, monkeypatch):
    content = character * 1500 + "\n\n" + character * 200
    (tmp_path / "unicode.md").write_text(content, encoding="utf-8")
    connection = MagicMock()
    cursor = connection.cursor.return_value
    cursor.execute.return_value.fetchone.return_value = ("POLICY_CHUNKS",)
    cursor.execute.return_value.fetchall.return_value = []
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)
    connection.__enter__.return_value = connection

    ensure_policies()

    chunks = [call.kwargs["chunk"] for call in cursor.execute.call_args_list
              if call.args[0].lstrip().startswith("INSERT INTO policy_chunks")]
    assert chunks
    assert all(chunk and len(chunk.encode("utf-8")) <= 4000 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == content.replace("\n", "")
    connection.commit.assert_called_once()


def test_question_scope():
    assert parse_question("East since March 2025") == ("East", "2025-03")
    assert parse_question("West since 2025-07") == ("West", "2025-07")
    with pytest.raises(ValueError):
        parse_question("West and East")


def test_zero_baseline_freight_preserves_discount_and_freight(monkeypatch):
    row = {
        "region_name": "West", "first_month": "2025-01", "last_month": "2025-09",
        "before_last_month": "2025-06", "after_first_month": "2025-07",
        "before_margin_pct": 40, "after_margin_pct": 25, "margin_drop_pts": 15,
        "before_discount_pct": 3, "after_discount_pct": 18,
        "before_freight_per_unit": 0, "after_freight_per_unit": 5,
        "freight_increase_pct": None,
    }
    monkeypatch.setattr("agent.run_sql", lambda sql, params=None: {
        "sql": sql, "params": params or {}, "rows": [row]})
    monkeypatch.setattr("agent.search_policies", lambda _: {"rows": []})

    result = answer_question("Why did West margin fall since July 2025?")

    finding = next(c for c in result["claims"]
                   if c["parts"][0].get("text") == "Average discount moved from ")
    assert "".join(p.get("text", str(p.get("value", "")) + p.get("suffix", ""))
                   for p in finding["parts"]) == (
        "Average discount moved from 3% to 18%. Freight per unit moved from $0 to $5.")
    assert result["dropped_count"] == 0
    for part in finding["parts"]:
        if "value" in part:
            assert supported(part, result["evidence"])
@pytest.mark.parametrize("month", ["Jul", "jul.", "Sept", "SEP"])
def test_question_month_abbreviations(month):
    expected = "07" if month.lower().startswith("jul") else "09"
    assert parse_question(f"West since {month} 2026") == ("West", f"2026-{expected}")


@pytest.mark.parametrize("date", ["2026-13", "2026-00", "2026/07", "2026-07-15",
                                  "Smarch 2026", "2026", "July 26", "July",
                                  "July 2025 and 2026-13"])
def test_question_rejects_unsupported_explicit_dates(date):
    with pytest.raises(ValueError, match="date"):
        parse_question(f"West since {date}")


def test_question_defaults_only_without_date():
    assert parse_question("Why did West margin fall?") == ("West", "2025-07")
@pytest.mark.parametrize("region", ["North", "north", "Northeast", "Northwest", "Southeast", "Southwest", "West and North"])
def test_unsupported_region_is_rejected_before_querying(monkeypatch, region):
    def forbidden_query(*args, **kwargs):
        pytest.fail("Unsupported regions must be rejected before querying")
    monkeypatch.setattr("agent.run_sql", forbidden_query)
    with pytest.raises(ValueError, match="Unsupported region"):
        answer_question(f"Why did {region} margin fall since July 2025?")
    assert parse_question("Why did margin fall since July 2025?") == ("West", "2025-07")


@pytest.fixture(scope="module")
def west_answer():
    return answer_question("Why did West margin fall since July 2025?")


def test_west_live_end_to_end(west_answer):
    result = west_answer
    row = next(r for r in result["evidence"]["E2"]["rows"] if r["region_name"] == "West")
    assert 12.5 < row["margin_drop_pts"] < 14
    assert 38 < row["before_margin_pct"] < 40
    assert 24 < row["after_margin_pct"] < 27
    assert 2 < row["before_discount_pct"] < 5
    assert 15 < row["after_discount_pct"] < 22
    assert 50 < row["freight_increase_pct"] < 80
    assert result["dropped_count"] == 0
    assert len([c for c in result["claims"] if c["kind"] == "action"]) == 3
    assert any(c["kind"] == "reason" and len(c["policy_ids"]) == 2 for c in result["claims"])
    kept, dropped = critique(result["claims"], result["evidence"])
    assert kept == result["claims"] and not dropped
    for claim in result["claims"]:
        for part in claim["parts"]:
            if "value" in part:
                assert supported(part, result["evidence"])


def test_live_evidence_replays(west_answer):
    for source in west_answer["evidence"].values():
        if source["kind"] == "sql":
            assert tools.run_sql(source["sql"], source["params"])["rows"] == source["rows"]


def test_policy_vector_search_and_idempotent_setup():
    assert ensure_policies() == ensure_policies()
    policies = tools.search_policies("West freight carrier contract", k=4)
    assert "VECTOR_DISTANCE" in policies["sql"]
    assert len(policies["rows"]) == 4
    assert policies["rows"][0]["doc"] == "freight_contract_memo.md"
    assert policies["rows"] == sorted(policies["rows"], key=lambda r: (r["distance"], r["id"]))
    # Replay the recorded bindings after the same JSON round trip used by the UI.
    params = json.loads(json.dumps(policies["params"]))
    assert set(params) == {"query_vector", "k"}
    with connect() as con, con.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        rows = cur.execute(policies["sql"], params).fetchall()
    assert [dict(zip(("id", "doc", "chunk", "distance"), row)) for row in rows] == policies["rows"]


@pytest.mark.parametrize("ending", ["", ";", "; -- report", "; /* report */"])
def test_real_select_and_bind(ending):
    sql = "SELECT region_name FROM regions WHERE region_name=:name" + ending
    result = tools.run_sql(sql, {"name": "West"})
    assert result["rows"] == [{"region_name": "West"}]
    assert result["sql"] == sql


def test_select_preserves_semicolon_in_string():
    result = tools.run_sql("SELECT 'West; -- report' AS label FROM regions WHERE region_name=:name; -- report",
                           {"name": "West"})
    assert result["rows"] == [{"label": "West; -- report"}]


def test_flask_live_brief_and_evidence():
    client = app.test_client()
    assert client.get("/").status_code == 200
    response = client.post("/", data={"question": "Why did West margin fall since July 2025?"})
    assert response.status_code == 200
    assert b"13.36" in response.data
    assert b'data-evidence="E2"' in response.data
    assert b"VECTOR_DISTANCE" in response.data
    assert b"west_q3_volume_push.md" in response.data
    assert b"evidence-dialog" in response.data


def test_ui_restores_submit_button_on_pageshow():
    html = app.test_client().get("/").get_data(as_text=True)
    script = html.rsplit("<script>", 1)[1].split("</script>", 1)[0]
    subprocess.run(["node", "-e", """
        const assert = require('node:assert/strict');
        const handlers = {};
        const button = {disabled: false, textContent: 'Analyze question'};
        const form = {addEventListener: (event, handler) => handlers[event] = handler};
        global.document = {
            getElementById: () => form,
            querySelector: () => button,
        };
        global.window = {addEventListener: (event, handler) => handlers[event] = handler};
    """ + script + """
        for (let visit = 0; visit < 2; visit++) {
            handlers.submit();
            assert.equal(button.disabled, true);
            assert.equal(button.textContent, 'Analyzing\u2026');
            handlers.pageshow({persisted: true});
            assert.equal(button.disabled, false);
            assert.equal(button.textContent, 'Analyze question');
        }
    """], check=True, capture_output=True, text=True)


def test_ui_validation_and_safe_error(monkeypatch):
    client = app.test_client()
    assert client.post("/", data={"question": ""}).status_code == 400
    assert client.post("/", data={"question": "West since July 2030"}).status_code == 400
    def failure(_):
        raise RuntimeError("secret-driver-settings")
    monkeypatch.setattr("app.answer_question", failure)
    response = client.post("/", data={"question": "West"})
    assert response.status_code == 503
    assert b"secret-driver-settings" not in response.data


def test_script_starts_and_shows_unavailable_when_setup_fails(monkeypatch):
    def failure(*args, **kwargs):
        raise RuntimeError("secret-driver-settings")

    monkeypatch.setattr("policies.ensure_policies", failure)
    monkeypatch.setattr("tools.connect", failure)
    started = []

    def run(server, **kwargs):
        started.append(server)
        client = server.test_client()
        assert client.get("/").status_code == 200
        response = client.post("/", data={"question": "West"})
        assert response.status_code == 503
        assert b"Analysis unavailable. Check the local Oracle connection and policy setup, then retry." in response.data
        assert b"secret-driver-settings" not in response.data

    monkeypatch.setattr("flask.Flask.run", run)
    runpy.run_path(str(Path(__file__).with_name("app.py")), run_name="__main__")
    assert len(started) == 1
def test_ui_oversized_submission(monkeypatch):
    def unexpected_analysis(_):
        pytest.fail("Oversized submissions must not run analysis")

    monkeypatch.setattr("app.answer_question", unexpected_analysis)
    response = app.test_client().post(
        "/", data={"question": "x" * (app.config["MAX_CONTENT_LENGTH"] + 1)}
    )
    assert response.status_code == 413
    assert b'id="question-form"' in response.data
    assert b'class="error" role="alert"' in response.data
    assert b"Submission too large. Shorten your question and try again." in response.data


def test_ui_escapes_question():
    response = app.test_client().post("/", data={"question": '<script>alert("x")</script> West'})
    assert response.status_code == 200
    assert b'<script>alert("x")</script>' not in response.data
