"""Unit checks and live integration against the existing local Oracle tables."""
import copy
import runpy
from pathlib import Path

import pytest

import tools
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


def test_hashing_is_stable_and_normalized():
    first = hash_embedding("West freight contract")
    assert first == hash_embedding("West freight contract")
    assert abs(sum(v*v for v in first) - 1) < 0.000001


def test_question_scope():
    assert parse_question("East since March 2025") == ("East", "2025-03")
    assert parse_question("West since 2025-07") == ("West", "2025-07")
    with pytest.raises(ValueError):
        parse_question("West and East")


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


def test_real_select_and_bind():
    result = tools.run_sql("SELECT region_name FROM regions WHERE region_name=:name", {"name": "West"})
    assert result["rows"] == [{"region_name": "West"}]


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


def test_ui_escapes_question():
    response = app.test_client().post("/", data={"question": '<script>alert("x")</script> West'})
    assert response.status_code == 200
    assert b'<script>alert("x")</script>' not in response.data
