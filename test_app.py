"""Unit checks and live integration against the existing local Oracle tables."""
import copy
import json
import re
import runpy
import subprocess
import sqlite3
from contextlib import nullcontext
from decimal import Decimal
import threading
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import oracledb
import pytest
from packaging.requirements import Requirement

import db
import tools
from db import connect
from agent import (DataQualityError, DiscountDataQualityError, RevenueDataQualityError,
                   answer_question, parse_question, summary_sql)
from app import app
from critic import critique, supported
from policies import ensure_policies, hash_embedding


@pytest.mark.parametrize("wallet", [None, "test-wallet"])
def test_connect_reuses_pool_and_preserves_settings(monkeypatch, wallet):
    monkeypatch.setattr(db, "_pool", None, raising=False)
    monkeypatch.setattr(db, "_load_env", lambda: None)
    settings = {"user": "test-user", "password": "test-password", "dsn": "test-dsn"}
    for key, value in settings.items():
        monkeypatch.setenv("ORACLE_" + key.upper(), value)
    monkeypatch.delenv("ORACLE_WALLET_DIR", raising=False)
    if wallet:
        monkeypatch.setenv("ORACLE_WALLET_DIR", wallet)
        monkeypatch.setenv("ORACLE_WALLET_PASSWORD", "test-wallet-password")
        settings.update(config_dir=wallet, wallet_location=wallet,
                        wallet_password="test-wallet-password")
    create_pool = MagicMock()
    standalone_connect = MagicMock()
    monkeypatch.setattr(db.oracledb, "create_pool", create_pool)
    monkeypatch.setattr(db.oracledb, "connect", standalone_connect)

    for _ in range(4):
        assert connect() is create_pool.return_value.acquire.return_value

    create_pool.assert_called_once_with(**settings)
    assert create_pool.return_value.acquire.call_count == 4
    standalone_connect.assert_not_called()


@pytest.mark.parametrize("fail", [False, True])
def test_pooled_connection_cleans_up_transaction(monkeypatch, fail):
    monkeypatch.setattr(db, "_pool", None)
    create_pool = oracledb.create_pool
    monkeypatch.setattr(db.oracledb, "create_pool",
                        lambda **kw: create_pool(min=1, max=1, **kw))
    try:
        with pytest.raises(RuntimeError, match="test failure") if fail else nullcontext():
            with connect() as con, con.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
                session = cur.execute("SELECT SYS_CONTEXT('USERENV', 'SID') FROM dual").fetchone()
                if fail:
                    raise RuntimeError("test failure")
        assert db._pool.busy == 0
        with connect() as con, con.cursor() as cur:
            # This must start a new transaction on the same physical session.
            cur.execute("SET TRANSACTION READ WRITE")
            assert cur.execute("SELECT SYS_CONTEXT('USERENV', 'SID') FROM dual").fetchone() == session
        assert db._pool.busy == 0
    finally:
        if db._pool is not None:
            db._pool.close(force=True)


def test_readme_install_requirements_are_available():
    readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
    assert "python -m pip install -r requirements.txt" in readme
    requirements = Path(__file__).with_name("requirements.txt").read_text(encoding="utf-8")
    packages = {re.split(r"[<>=!~\[;\s]", line.strip(), maxsplit=1)[0].lower()
                for line in requirements.splitlines()}
    assert {"flask", "oracledb", "sqlglot", "pytest"} <= packages
def test_readme_requirements_include_compatible_dependencies():
    root = Path(__file__).parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "python -m pip install -r requirements.txt" in readme
    requirements = [Requirement(line) for line in
                    (root / "requirements.txt").read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    by_name = {requirement.name.lower(): requirement for requirement in requirements}
    assert {"flask", "oracledb", "sqlglot", "pytest"} <= by_name.keys()
    # VECTOR column support for generally available Oracle 23ai starts at 2.2.
    assert "2.1" not in by_name["oracledb"].specifier
    assert "2.2" in by_name["oracledb"].specifier


def test_readme_documents_node_test_prerequisite():
    readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
    test_instructions = readme.split("python -m pytest -q test_app.py", 1)[1]
    assert "Node.js installed and available as `node` on PATH" in " ".join(test_instructions.split())


def test_readme_documents_runtime_policy_refresh():
    readme = " ".join(Path(__file__).with_name("README.md").read_text(encoding="utf-8").split())
    assert "`search_policies()` calls `ensure_policies()` on every policy search" in readme
    assert "including normal POST requests" in readme
    assert "`CREATE TABLE`" in readme and "tablespace quota" in readme
    assert "`SELECT`, `DELETE`, and `INSERT` access to `policy_chunks`" in readme
    assert "source documents or selected embedding backend change" in readme
    assert "deletes and replaces all rows and commits" in readme
    assert "shared by requests and other app instances using the same schema" in readme
    assert "Policy setup is the separate write path" not in readme
def test_readme_explains_independent_rounding():
    readme = " ".join(Path(__file__).with_name("README.md").read_text(encoding="utf-8").split())
    assert "Changes and margin direction use unrounded aggregates." in readme
    assert "SQL rounds period metrics and changes independently to two decimal places" in readme
    assert "subtracting displayed endpoints may give a different result" in readme
def test_readme_documents_request_time_policy_writes():
    readme = " ".join(Path(__file__).with_name("README.md").read_text(encoding="utf-8").split())
    assert "`search_policies()` calls `ensure_policies()` on every policy search" in readme
    assert "creates `policy_chunks` if absent" in readme
    assert "source documents or selected embedding backend change" in readme
    assert "`CREATE TABLE`" in readme and "tablespace quota" in readme
    assert "`SELECT`, `DELETE`, and `INSERT` access to `policy_chunks`" in readme
    assert "Running `python policies.py` once does not make SELECT-only runtime permissions sufficient." in readme
def test_readme_documents_required_oracle_configuration():
    root = Path(__file__).parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    setup = readme.split("python policies.py", 1)[0]
    assert "Copy-Item .env.example .env" in setup
    assert "before running `policies.py`" in " ".join(setup.split())
    example = dict(line.split("=", 1) for line in
                   (root / ".env.example").read_text(encoding="utf-8").splitlines()
                   if "=" in line and not line.lstrip().startswith("#"))
    for name in ("ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_DSN"):
        assert name in setup
        assert name in example
        is_empty = not example[name].strip()
        assert is_empty, f"{name} must be blank in .env.example"


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
    "SELECT * FROM \u017fales", "SELECT * FROM d\u0131scounts",
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


@pytest.mark.parametrize("name", ["sales", "costs", "customers", "discounts", "products", "regions"])
def test_guard_resolves_oracle_table_names(name, monkeypatch):
    for identifier in (name, name.upper(), name.capitalize(), f'"{name.upper()}"'):
        tools.validate_select(f"SELECT * FROM {identifier}")

    def forbidden_connection():
        pytest.fail("Rejected SQL must never connect")

    monkeypatch.setattr(tools, "connect", forbidden_connection)
    for identifier in (f'"{name}"', f'"{name.capitalize()}"'):
        with pytest.raises(ValueError, match="Only governed business tables"):
            tools.run_sql(f"SELECT * FROM sales JOIN {identifier} ON 1=1")


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
    assert critique([claim], sample_evidence) == ([claim], [])


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


@pytest.mark.parametrize("modifier", [" million", " billion", " thousand", "k", "M",
                                      " percent", "%", " percentage points", " basis points"])
@pytest.mark.parametrize("split", [False, True])
def test_critic_rejects_units_and_magnitudes_in_adjacent_text(sample_evidence, modifier, split):
    texts = [{"text": modifier}]
    if split:
        texts = [{"text": character} for character in modifier]
    claim = {"parts": [
        {"value": 25.53, "evidence": "E1", "row": 0, "column": "margin"},
        *texts]}
    assert critique([claim], sample_evidence) == ([], [claim])


def test_critic_documents_limit_of_adjacent_modifier_check(sample_evidence):
    claim = {"parts": [
        {"value": 25.53, "evidence": "E1", "row": 0, "column": "margin"},
        {"text": " (in millions)"}]}
    assert critique([claim], sample_evidence) == ([claim], [])
    doc = " ".join(critique.__doc__.split())
    assert "does not catch other wording such as ' (in millions)'" in doc
def test_critic_documents_unchecked_text_modifiers(sample_evidence):
    claim = {"parts": [
        {"value": 25.53, "evidence": "E1", "row": 0, "column": "margin"},
        {"text": " quadrillion"}]}
    assert critique([claim], sample_evidence) == ([claim], [])

    documentation = " ".join(critique.__doc__.lower().split())
    assert "other unit or magnitude modifiers" in documentation
    assert "quadrillion" in documentation
    assert "may pass unchecked" in documentation


def test_hashing_is_stable_and_normalized():
    first = hash_embedding("West freight contract")
    assert first == hash_embedding("West freight contract")
    assert abs(sum(v*v for v in first) - 1) < 0.000001


@pytest.mark.parametrize("failure", [
    RuntimeError("private-driver-details"),
    oracledb.DatabaseError(SimpleNamespace(code=3113, message="private-driver-details")),
    oracledb.DatabaseError(SimpleNamespace(code=40284, message="private-driver-details")),
])
def test_policy_probe_propagates_unexpected_failures(failure, tmp_path, monkeypatch):
    (tmp_path / "policy.md").write_text("Business policy", encoding="utf-8")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    cursor.execute.return_value.fetchone.return_value = ("POLICY_CHUNKS",)
    cursor.execute.return_value.fetchall.side_effect = [[("BROKEN_MODEL",)], []]
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)

    def embedding(cur, text, backend):
        if backend == "BROKEN_MODEL":
            raise failure
        return hash_embedding(text)

    monkeypatch.setattr("policies.embed", embedding)

    with pytest.raises(type(failure)) as caught:
        ensure_policies()

    assert caught.value is failure
    connection.commit.assert_not_called()
    assert not any(call.args[0].startswith("DELETE") for call in cursor.execute.call_args_list)


@pytest.mark.parametrize("models, expected", [
    ([], "hash-v1-512"),
    ([("INCOMPATIBLE_MODEL",)], "hash-v1-512"),
    ([("INCOMPATIBLE_MODEL",), ("GOOD_MODEL",)], "GOOD_MODEL"),
])
def test_policy_probe_fallback_is_explicit_and_sanitized(
        models, expected, tmp_path, monkeypatch, caplog):
    (tmp_path / "policy.md").write_text("Business policy", encoding="utf-8")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    cursor.execute.return_value.fetchone.return_value = ("POLICY_CHUNKS",)
    cursor.execute.return_value.fetchall.side_effect = [models, []]
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)

    def embedding(cur, text, backend):
        if backend == "INCOMPATIBLE_MODEL":
            raise oracledb.DatabaseError(SimpleNamespace(
                code=40290, message="private-driver-details\nforged log entry"))
        return hash_embedding(text)

    monkeypatch.setattr("policies.embed", embedding)
    assert ensure_policies() == expected
    connection.commit.assert_called_once()
    if models:
        assert "INCOMPATIBLE_MODEL" in caplog.text
        assert "40290" in caplog.text
    assert ("Using hash-v1-512 fallback" in caplog.text) == (expected == "hash-v1-512")
    assert "private-driver-details" not in caplog.text
    assert "forged log entry" not in caplog.text


@pytest.mark.parametrize("unsupported", ["my_model", 'EVIL") FROM dual; DELETE FROM sales--\nFORGED'])
@pytest.mark.parametrize("later_model", [None, "GOOD_MODEL"])
def test_policy_probe_skips_unsupported_names(unsupported, later_model, tmp_path, monkeypatch):
    (tmp_path / "policy.md").write_text("Business policy", encoding="utf-8")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    cursor.execute.return_value.fetchone.return_value = ("POLICY_CHUNKS",)
    models = [(unsupported,)] + ([(later_model,)] if later_model else [])
    cursor.execute.return_value.fetchall.side_effect = [models, []]
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)

    expected = later_model or "hash-v1-512"
    assert ensure_policies() == expected
    embedding_sql = [call.args[0] for call in cursor.execute.call_args_list
                     if "VECTOR_EMBEDDING" in call.args[0]]
    assert embedding_sql == (["SELECT VECTOR_EMBEDDING(GOOD_MODEL USING :text AS DATA) FROM dual"] * 2
                             if later_model else [])
    inserted = [call.kwargs for call in cursor.execute.call_args_list
                if call.args[0].lstrip().startswith("INSERT INTO policy_chunks")]
    assert inserted and all(row["backend"] == expected for row in inserted)
    connection.commit.assert_called_once()


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


def test_empty_policy_documents_preserve_existing_rows(tmp_path, monkeypatch):
    (tmp_path / "empty.md").write_text(" \n\t\n ", encoding="utf-8")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    cursor.execute.return_value.fetchone.return_value = ("POLICY_CHUNKS",)
    cursor.execute.return_value.fetchall.side_effect = [[], [("hash-v1-512", "old-hash")]]
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)

    with pytest.raises(RuntimeError, match="No policy content"):
        ensure_policies()

    assert not any(call.args[0].lstrip().startswith(("DELETE", "INSERT"))
                   for call in cursor.execute.call_args_list)
    connection.commit.assert_not_called()


def test_delayed_policy_refresh_preserves_newer_content(tmp_path, monkeypatch):
    document = tmp_path / "policy.md"
    document.write_text("Old policy", encoding="utf-8")
    waiting = threading.Event()
    resume = threading.Event()
    lock = threading.Lock()
    main_thread = threading.current_thread()

    class DelayedLock:
        def __enter__(self):
            if threading.current_thread() is not main_thread:
                waiting.set()
                assert resume.wait(10), "Delayed refresh was not released"
            lock.acquire()

        def __exit__(self, *args):
            lock.release()

    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = ("POLICY_CHUNKS",)
    rows = []

    def execute(sql, **params):
        if sql.startswith("SELECT model_name"):
            cursor.fetchall.return_value = []
        elif sql.startswith("SELECT DISTINCT backend, source_hash"):
            cursor.fetchall.return_value = [(row["backend"], row["fingerprint"]) for row in rows]
        elif sql.startswith("DELETE FROM policy_chunks"):
            rows.clear()
        elif sql.startswith("INSERT INTO policy_chunks"):
            rows.append(params)
        return cursor

    cursor.execute.side_effect = execute
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)
    monkeypatch.setattr("policies._lock", DelayedLock())

    with ThreadPoolExecutor(max_workers=1) as executor:
        delayed = executor.submit(ensure_policies)
        try:
            assert waiting.wait(10), "Delayed refresh did not reach the lock"
            # Commit updated content while the first refresh is waiting for the lock.
            document.write_text("Updated policy", encoding="utf-8")
            assert ensure_policies() == "hash-v1-512"
        finally:
            resume.set()
        assert delayed.result(timeout=10) == "hash-v1-512"

    assert [row["chunk"] for row in rows] == ["Updated policy"]
    connection.commit.assert_called_once()


def test_policy_search_handles_empty_table(monkeypatch):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.execute.return_value.fetchone.return_value = None
    monkeypatch.setattr(tools, "ensure_policies", lambda: None)
    monkeypatch.setattr(tools, "connect", lambda: connection)
    embedding = MagicMock()
    monkeypatch.setattr(tools, "embed", embedding)

    with pytest.raises(RuntimeError, match="No policy chunks"):
        tools.search_policies("West freight")

    embedding.assert_not_called()


def test_question_scope():
    assert parse_question("East since March 2025") == ("East", "2025-03")
    assert parse_question("West since 2025-07") == ("West", "2025-07")
    with pytest.raises(ValueError):
        parse_question("West and East")


@pytest.mark.parametrize("region_id", [1, 2])
@pytest.mark.parametrize("month", ["2025-06", "2025-07"])
@pytest.mark.parametrize("incomplete_when", ["both", "monthly", "summary"])
def test_missing_net_revenue_rejects_analysis(region_id, month, incomplete_when, monkeypatch):
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE regions (region_id, region_name);
            INSERT INTO regions VALUES (1, 'West'), (2, 'East');
            CREATE TABLE sales (region_id, product_id, sale_month, net_revenue,
                                quantity, unit_price, discount_pct);
            CREATE TABLE costs (region_id, product_id, cost_month, unit_cogs, freight_per_unit);
        """)
        for current_region in (1, 2):
            for current_month in ("2025-06", "2025-07"):
                con.execute("INSERT INTO sales VALUES (?, 1, ?, 90.0, 1, 100.0, 10)",
                            (current_region, current_month))
                con.execute("INSERT INTO costs VALUES (?, 1, ?, 60.0, 5.0)",
                            (current_region, current_month))
        sale_id = con.execute("INSERT INTO sales VALUES (?, 1, ?, 0, 1, 100.0, 0)",
                              (region_id, month)).lastrowid

        def set_revenue(value):
            con.execute("UPDATE sales SET net_revenue=? WHERE rowid=?", (value, sale_id))

        def execute(sql, params=None):
            return {"sql": sql, "params": params or {}, "rows": [dict(row) for row in
                con.execute(tools.validate_select(sql), params or {})]}

        policies = MagicMock(return_value={"rows": []})
        monkeypatch.setattr("agent.run_sql", execute)
        monkeypatch.setattr("agent.search_policies", policies)
        answer_question("West since July 2025")  # Zero revenue is present, not missing.
        policies.reset_mock()

        if incomplete_when != "summary":
            set_revenue(None)
        snapshots = []

        def run_sql(sql, params=None):
            result = execute(sql, params)
            snapshots.append(result)
            if len(snapshots) == 1:
                if incomplete_when == "monthly":
                    set_revenue(0)
                elif incomplete_when == "summary":
                    set_revenue(None)
            return result

        monkeypatch.setattr("agent.run_sql", run_sql)
        with pytest.raises(RevenueDataQualityError, match="Missing net revenue"):
            answer_question("West since July 2025")
        policies.assert_not_called()
        region = "West" if region_id == 1 else "East"
        for index, snapshot in enumerate(snapshots):
            row = next(row for row in snapshot["rows"] if row["region_name"] == region
                       and (index == 1 or row["sale_month"] == month))
            missing = incomplete_when == "both" or (index == 0) == (incomplete_when == "monthly")
            assert row["missing_revenue_count"] == int(missing)
            if missing and index == 0:
                assert row["margin_pct"] is None
            elif missing:
                assert row["before_margin_pct"] is row["after_margin_pct"] is None
        monthly = next(row for row in snapshots[0]["rows"]
                       if row["region_name"] == region and row["sale_month"] == month)
        summary = next(row for row in snapshots[1]["rows"] if row["region_name"] == region)
        assert monthly["missing_revenue_count"] == (incomplete_when != "summary")
        assert summary["missing_revenue_count"] == (incomplete_when != "monthly")

        # Zero revenue is a real value; its costs still contribute to the margin.
        set_revenue(0)
        monkeypatch.setattr("agent.run_sql", execute)
        result = answer_question("West since July 2025")
        monthly = next(row for row in result["evidence"]["E1"]["rows"]
                       if row["region_name"] == region and row["sale_month"] == month)
        assert (monthly["revenue"], monthly["units"], monthly["margin_pct"]) == (90, 2, -44.44)
        assert result["dropped_count"] == 0


@pytest.mark.parametrize("column", ["discount_pct", "quantity", "unit_price"])
@pytest.mark.parametrize("region_id", [1, 2])
@pytest.mark.parametrize("month", ["2025-06", "2025-07"])
@pytest.mark.parametrize("incomplete_when", ["both", "monthly", "summary"])
def test_missing_discount_inputs_reject_analysis(column, region_id, month, incomplete_when, monkeypatch):
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE regions (region_id, region_name);
            INSERT INTO regions VALUES (1, 'West'), (2, 'East');
            CREATE TABLE sales (region_id, product_id, sale_month, net_revenue,
                                quantity, unit_price, discount_pct);
            CREATE TABLE costs (region_id, product_id, cost_month, unit_cogs, freight_per_unit);
        """)
        for current_region in (1, 2):
            for current_month in ("2025-06", "2025-07"):
                con.execute("INSERT INTO sales VALUES (?, 1, ?, 90.0, 1, 100.0, 10)",
                            (current_region, current_month))
                con.execute("INSERT INTO costs VALUES (?, 1, ?, 60.0, 5.0)",
                            (current_region, current_month))
        sale_id = con.execute("INSERT INTO sales VALUES (?, 1, ?, 100.0, 1, 100.0, 0)",
                              (region_id, month)).lastrowid

        def execute(sql, params=None):
            return {"sql": sql, "params": params or {}, "rows": [dict(row) for row in
                con.execute(tools.validate_select(sql), params or {})]}

        policies = MagicMock(return_value={"rows": []})
        monkeypatch.setattr("agent.run_sql", execute)
        monkeypatch.setattr("agent.search_policies", policies)
        result = answer_question("West since July 2025")
        region = "West" if region_id == 1 else "East"
        monthly = next(row for row in result["evidence"]["E1"]["rows"]
                       if row["region_name"] == region and row["sale_month"] == month)
        summary = next(row for row in result["evidence"]["E2"]["rows"]
                       if row["region_name"] == region)
        period = "before" if month == "2025-06" else "after"
        assert monthly["discount_pct"] == summary[f"{period}_discount_pct"] == 5
        policies.reset_mock()

        def set_input(value):
            con.execute(f"UPDATE sales SET {column}=? WHERE rowid=?", (value, sale_id))

        valid_value = {"discount_pct": 0, "quantity": 1, "unit_price": 100.0}[column]
        if incomplete_when != "summary":
            set_input(None)
        snapshots = []

        def run_sql(sql, params=None):
            result = execute(sql, params)
            snapshots.append(result)
            if len(snapshots) == 1:
                if incomplete_when == "monthly":
                    set_input(valid_value)
                elif incomplete_when == "summary":
                    set_input(None)
            return result

        monkeypatch.setattr("agent.run_sql", run_sql)
        with pytest.raises(DiscountDataQualityError, match="Missing discount inputs"):
            answer_question("West since July 2025")
        policies.assert_not_called()
        for snapshot in snapshots:
            for row in snapshot["rows"]:
                if row["missing_discount_count"]:
                    if "sale_month" in row:
                        assert row["discount_pct"] is None
                    else:
                        assert row[f"{period}_discount_pct"] is None

        set_input(None)
        monkeypatch.setattr("agent.run_sql", execute)
        response = app.test_client().post("/", data={"question": "West since July 2025"})
        assert response.status_code == 503
        assert b"Correct missing discount, quantity, or unit price inputs, then retry." in response.data
        policies.assert_not_called()


@pytest.mark.parametrize("region_id", [1, 2])
@pytest.mark.parametrize("cost", [None, (None, 5.0), (60.0, None), (None, None)])
@pytest.mark.parametrize("month,product_id", [
    ("2025-05", 1), ("2025-06", 2), ("2025-08", 1),
])
def test_missing_monthly_costs_reject_incomplete_analysis(region_id, month, product_id, cost, monkeypatch):
    # Missing costs must not hide boundary months or sales within a covered month,
    # including sales in a comparison region.
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE regions (region_id, region_name);
            INSERT INTO regions VALUES (1, 'West'), (2, 'East');
            CREATE TABLE sales (region_id, product_id, sale_month, net_revenue,
                                quantity, unit_price, discount_pct);
            CREATE TABLE costs (region_id, product_id, cost_month, unit_cogs, freight_per_unit);
        """)
        for matched_region in (1, 2):
            for matched_month in ("2025-06", "2025-07"):
                con.execute("INSERT INTO sales VALUES (?, 1, ?, 100.0, 1, 100.0, 0)",
                            (matched_region, matched_month))
                con.execute("INSERT INTO costs VALUES (?, 1, ?, 60.0, 5.0)",
                            (matched_region, matched_month))
        con.execute("INSERT INTO sales VALUES (?, ?, ?, 100.0, 1, 100.0, 0)",
                    (region_id, product_id, month))
        if cost is not None:
            con.execute("INSERT INTO costs VALUES (?, ?, ?, ?, ?)",
                        (region_id, product_id, month, *cost))

        def run_sql(sql, params=None):
            return {"sql": sql, "params": params or {}, "rows": [dict(row) for row in
                con.execute(tools.validate_select(sql), params or {})]}

        policies = MagicMock(return_value={"rows": []})
        monkeypatch.setattr("agent.run_sql", run_sql)
        monkeypatch.setattr("agent.search_policies", policies)
        with pytest.raises(DataQualityError, match="Missing monthly costs"):
            answer_question("West since July 2025")
        policies.assert_not_called()
        response = app.test_client().post("/", data={"question": "West since July 2025"})
        assert response.status_code == 503
        assert b"Correct missing, incomplete, or duplicate monthly cost records, then retry." in response.data

        if cost is None:
            con.execute("INSERT INTO costs VALUES (?, ?, ?, 60.0, 5.0)",
                        (region_id, product_id, month))
        else:
            con.execute("UPDATE costs SET unit_cogs=0, freight_per_unit=0 "
                        "WHERE region_id=? AND product_id=? AND cost_month=?",
                        (region_id, product_id, month))
        assert answer_question("West since July 2025")["dropped_count"] == 0


@pytest.mark.parametrize("unit_cogs,freight_per_unit", [
    (None, 5.0), (60.0, None), (None, None),
])
def test_monthly_evidence_rejects_null_costs_repaired_before_summary(
        unit_cogs, freight_per_unit, monkeypatch):
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE regions (region_id, region_name);
            INSERT INTO regions VALUES (1, 'West');
            CREATE TABLE sales (region_id, product_id, sale_month, net_revenue,
                                quantity, unit_price, discount_pct);
            CREATE TABLE costs (region_id, product_id, cost_month, unit_cogs, freight_per_unit);
            INSERT INTO sales VALUES (1, 1, '2025-06', 100.0, 1, 100.0, 0);
            INSERT INTO sales VALUES (1, 1, '2025-06', 100.0, 1, 100.0, 0);
            INSERT INTO sales VALUES (1, 1, '2025-07', 100.0, 1, 100.0, 0);
            INSERT INTO costs VALUES (1, 1, '2025-07', 60.0, 5.0);
        """)
        con.execute("INSERT INTO costs VALUES (1, 1, '2025-06', ?, ?)",
                    (unit_cogs, freight_per_unit))

        calls = 0

        def run_sql(sql, params=None):
            nonlocal calls
            rows = [dict(row) for row in con.execute(tools.validate_select(sql), params or {})]
            calls += 1
            if calls == 1:
                con.execute("UPDATE costs SET unit_cogs=60.0, freight_per_unit=5.0 "
                            "WHERE cost_month='2025-06'")
            return {"sql": sql, "params": params or {}, "rows": rows}

        policies = MagicMock(return_value={"rows": []})
        monkeypatch.setattr("agent.run_sql", run_sql)
        monkeypatch.setattr("agent.search_policies", policies)
        with pytest.raises(DataQualityError, match="Missing monthly costs"):
            answer_question("West since July 2025")
        assert calls == 2
        policies.assert_not_called()


@pytest.mark.parametrize("region_id", [1, 2])
@pytest.mark.parametrize("unit_cogs", [60.0, 80.0])
@pytest.mark.parametrize("duplicate_when", ["both", "monthly", "summary"])
def test_duplicate_monthly_costs_reject_analysis(region_id, unit_cogs, duplicate_when, monkeypatch):
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE regions (region_id, region_name);
            INSERT INTO regions VALUES (1, 'West'), (2, 'East');
            CREATE TABLE sales (region_id, product_id, sale_month, net_revenue,
                                quantity, unit_price, discount_pct);
            CREATE TABLE costs (region_id, product_id, cost_month, unit_cogs, freight_per_unit);
        """)
        for matched_region in (1, 2):
            for month in ("2025-06", "2025-07"):
                con.execute("INSERT INTO sales VALUES (?, 1, ?, 100.0, 1, 100.0, 0)",
                            (matched_region, month))
                con.execute("INSERT INTO costs VALUES (?, 1, ?, 60.0, 5.0)",
                            (matched_region, month))
        # Multiple sales for one cost key are valid; unused duplicate costs are irrelevant.
        con.execute("INSERT INTO sales VALUES (1, 1, '2025-06', 100.0, 1, 100.0, 0)")
        con.execute("INSERT INTO costs VALUES (1, 2, '2025-06', 60.0, 5.0)")
        con.execute("INSERT INTO costs VALUES (1, 2, '2025-06', 80.0, 5.0)")

        def execute(sql, params=None):
            return {"sql": sql, "params": params or {}, "rows": [dict(row) for row in
                con.execute(tools.validate_select(sql), params or {})]}

        policies = MagicMock(return_value={"rows": []})
        monkeypatch.setattr("agent.run_sql", execute)
        monkeypatch.setattr("agent.search_policies", policies)
        result = answer_question("West since July 2025")
        june = next(row for row in result["evidence"]["E1"]["rows"]
                    if row["region_name"] == "West" and row["sale_month"] == "2025-06")
        assert (june["revenue"], june["units"], june["margin_pct"]) == (200, 2, 35)
        policies.reset_mock()

        def insert_duplicate():
            con.execute("INSERT INTO costs VALUES (?, 1, '2025-06', ?, 5.0)",
                        (region_id, unit_cogs))

        if duplicate_when != "summary":
            insert_duplicate()
        calls = 0

        def run_sql(sql, params=None):
            nonlocal calls
            rows = execute(sql, params)
            calls += 1
            if calls == 1 and duplicate_when == "monthly":
                con.execute("DELETE FROM costs WHERE rowid = (SELECT MAX(rowid) FROM costs)")
            elif calls == 1 and duplicate_when == "summary":
                insert_duplicate()
            return rows

        monkeypatch.setattr("agent.run_sql", run_sql)
        with pytest.raises(DataQualityError, match="duplicate"):
            answer_question("West since July 2025")
        policies.assert_not_called()
        if duplicate_when == "monthly":
            insert_duplicate()
        monkeypatch.setattr("agent.run_sql", execute)
        response = app.test_client().post("/", data={"question": "West since July 2025"})
        assert response.status_code == 503
        assert b"Correct missing, incomplete, or duplicate monthly cost records, then retry." in response.data


@pytest.mark.parametrize("rows", [[], [{"region_name": "East"}]])
def test_missing_region_has_descriptive_validation_error(monkeypatch, rows):
    monkeypatch.setattr("agent.run_sql", lambda sql, params=None: {
        "sql": sql, "params": params or {}, "rows": rows})
    with pytest.raises(ValueError, match="No sales/cost data found for West"):
        answer_question("Why did West margin fall since July 2025?")
    response = app.test_client().post("/", data={"question": "West"})
    assert response.status_code == 400
    assert b"No sales/cost data found for West" in response.data


@pytest.mark.parametrize("cutoff,valid", [
    ("2025-02", False), ("2025-03", False), ("2025-08", True), ("2025-09", False),
])
def test_cutoff_error_shows_selected_region_coverage(monkeypatch, cutoff, valid):
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE regions (region_id, region_name);
            INSERT INTO regions VALUES (1, 'East'), (2, 'West');
            CREATE TABLE sales (region_id, product_id, sale_month, net_revenue,
                                quantity, unit_price, discount_pct);
            CREATE TABLE costs (region_id, product_id, cost_month, unit_cogs, freight_per_unit);
        """)
        for region_id, months in ((1, ("2025-01", "2025-12")), (2, ("2025-03", "2025-08"))):
            for month in months:
                con.execute("INSERT INTO sales VALUES (?, 1, ?, 100.0, 1, 100.0, 0)", (region_id, month))
                con.execute("INSERT INTO costs VALUES (?, 1, ?, 60.0, 5.0)", (region_id, month))

        def run_sql(sql, params=None):
            return {"sql": sql, "params": params or {}, "rows": [dict(row) for row in
                con.execute(tools.validate_select(sql), params or {})]}

        policies = MagicMock(return_value={"rows": []})
        monkeypatch.setattr("agent.run_sql", run_sql)
        monkeypatch.setattr("agent.search_policies", policies)
        response = app.test_client().post("/", data={"question": f"West since {cutoff}"})

        assert response.status_code == (200 if valid else 400)
        if not valid:
            assert b"West sales are available from 2025-03 through 2025-08" in response.data
            assert b"cutoff must be after the first month and no later than the last month" in response.data
            policies.assert_not_called()


@pytest.mark.parametrize("before,after,direction,below_precision", [
    (25.001, 25.004, "rose", True),
    (25.004, 25.001, "fell", True),
    (25.001, 25.001, "was unchanged", False),
    (25, 26, "rose", False),
    (26, 25, "fell", False),
])
def test_margin_direction_uses_unrounded_sql(before, after, direction, below_precision, monkeypatch, policy_rows):
    # Execute the production aggregation over controlled rows without changing Oracle data.
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE regions (region_id, region_name);
            INSERT INTO regions VALUES (1, 'West');
            CREATE TABLE sales (region_id, product_id, sale_month, net_revenue,
                                quantity, unit_price, discount_pct);
            CREATE TABLE costs (region_id, product_id, cost_month, unit_cogs, freight_per_unit);
        """)
        for month, margin in (("2025-06", before), ("2025-07", after)):
            con.execute("INSERT INTO sales VALUES (1, 1, ?, 100.0, 1, 100.0, 0)", (month,))
            con.execute("INSERT INTO costs VALUES (1, 1, ?, ?, 1.0)", (month, 99 - margin))
        sql = summary_sql()
        tools.validate_select(sql)
        rows = [dict(row) for row in con.execute(sql, {"cutoff": "2025-07"})]

    monkeypatch.setattr("agent.run_sql", lambda sql, params=None: {
        "sql": sql, "params": params or {}, "rows": rows})
    result = answer_question("West since July 2025")
    finding = next(c for c in result["claims"] if c["kind"] == "finding")
    rendered = "".join(p.get("text", str(p.get("value", "")) + p.get("suffix", ""))
                       for p in finding["parts"])
    assert rendered.startswith(f"West gross margin {direction} from ")
    assert ("below display precision" in rendered) == below_precision
    reason = next(c for c in result["claims"] if c["kind"] == "reason")
    explanation = "".join(p["text"] for p in reason["parts"])
    if direction == "fell":
        assert "consistent with the observed margin decline" in explanation
    else:
        assert f"The observed margin {direction}" in explanation
        assert "margin decline" not in explanation
    assert set(reason["policy_ids"]) == {
        f"P{p['id']}" for p in policy_rows
        if p["doc"] in {"west_q3_volume_push.md", "freight_contract_memo.md"}}
    assert result["dropped_count"] == 0


@pytest.mark.parametrize("available_period,margin", [
    ("before", 35), ("after", 35), ("before", 0), ("after", 0),
])
def test_comparison_keeps_available_margin_when_other_period_is_empty(available_period, margin, monkeypatch):
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE regions (region_id, region_name);
            INSERT INTO regions VALUES (1, 'West'), (2, 'East');
            CREATE TABLE sales (region_id, product_id, sale_month, net_revenue,
                                quantity, unit_price, discount_pct);
            CREATE TABLE costs (region_id, product_id, cost_month, unit_cogs, freight_per_unit);
        """)
        for region_id, month, value in (
            (1, "2025-06", 40), (1, "2025-07", 25),
            (2, "2025-06" if available_period == "before" else "2025-07", margin),
        ):
            con.execute("INSERT INTO sales VALUES (?, 1, ?, 100.0, 1, 100.0, 0)", (region_id, month))
            con.execute("INSERT INTO costs VALUES (?, 1, ?, ?, 5)", (region_id, month, 95 - value))

        def run_sql(sql, params=None):
            return {"sql": sql, "params": params or {}, "rows": [dict(row) for row in
                con.execute(tools.validate_select(sql), params or {})]}

        monkeypatch.setattr("agent.run_sql", run_sql)
        monkeypatch.setattr("agent.search_policies", lambda _: {"rows": []})
        result = answer_question("West since July 2025")

    comparison = next(claim for claim in result["claims"] if claim["kind"] == "comparison")
    rendered = "".join(part.get("text", str(part.get("value", "")) + part.get("suffix", ""))
                       for part in comparison["parts"])
    missing_period = "after" if available_period == "before" else "before"
    assert f"{margin}.0% {available_period}" in rendered
    assert f"unavailable {missing_period}" in rendered
    parts = [part for part in comparison["parts"] if "value" in part]
    assert len(parts) == 1
    assert parts[0]["column"] == f"{available_period}_margin_pct"
    assert parts[0]["evidence"] == "E2"
    assert result["evidence"]["E2"]["rows"][parts[0]["row"]]["region_name"] == "East"
    assert supported(parts[0], result["evidence"])
    assert result["dropped_count"] == 0


def test_zero_baseline_freight_preserves_discount_and_freight(monkeypatch):
    row = {
        "region_name": "West", "first_month": "2025-01", "last_month": "2025-09",
        "before_last_month": "2025-06", "after_first_month": "2025-07",
        "before_margin_pct": 40, "after_margin_pct": 25, "margin_drop_pts": 15,
        "margin_direction": "fell",
        "before_discount_pct": 3, "after_discount_pct": 18,
        "before_freight_per_unit": 0, "after_freight_per_unit": 5,
        "freight_increase_pct": None,
        "missing_cost_count": 0, "missing_discount_count": 0, "missing_revenue_count": 0,
    }
    monkeypatch.setattr("agent.run_sql", lambda sql, params=None: {
        "sql": sql, "params": params or {}, "rows": [row]})
    monkeypatch.setattr("agent.search_policies", lambda _: {"rows": []})

    result = answer_question("Why did West margin fall since July 2025?")

    findings = [c for c in result["claims"] if c["kind"] == "finding"]
    discount = next(c for c in findings if "discount" in c["parts"][0].get("text", "").lower())
    freight = next(c for c in findings if "freight" in c["parts"][0].get("text", "").lower())
    render = lambda claim: "".join(p.get("text", str(p.get("value", "")) + p.get("suffix", ""))
                                   for p in claim["parts"])
    assert "3%" in render(discount) and "18%" in render(discount)
    assert "$0" in render(freight) and "$5" in render(freight)
    assert "change" in render(freight).lower() and "unavailable" in render(freight).lower()
    assert result["dropped_count"] == 0
    for finding in (discount, freight):
        for part in finding["parts"]:
            if "value" in part:
                assert supported(part, result["evidence"])


@pytest.mark.parametrize("unavailable_metric", ["discount", "freight"])
@pytest.mark.parametrize("unavailable_period", ["before", "after", "both"])
def test_undefined_metric_preserves_other_metric(unavailable_metric, unavailable_period, monkeypatch):
    # Returns can zero either denominator without making the other metric undefined.
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE regions (region_id, region_name);
            INSERT INTO regions VALUES (1, 'West');
            CREATE TABLE sales (region_id, product_id, sale_month, net_revenue,
                                quantity, unit_price, discount_pct);
            CREATE TABLE costs (region_id, product_id, cost_month, unit_cogs, freight_per_unit);
        """)
        for period, month in (("before", "2025-06"), ("after", "2025-07")):
            if period != unavailable_period and unavailable_period != "both":
                sales = [(90, 1, 100, 10)]
            elif unavailable_metric == "freight":
                sales = [(160, 1, 200, 20), (-90, -1, 100, 10)]
            else:
                sales = [(160, 2, 100, 20), (-100, -1, 200, 50)]
            con.executemany("INSERT INTO sales VALUES (1, 1, ?, ?, ?, ?, ?)",
                            [(month, *sale) for sale in sales])
            con.execute("INSERT INTO costs VALUES (1, 1, ?, 60, 5)", (month,))

        def run_sql(sql, params=None):
            return {"sql": sql, "params": params or {}, "rows": [dict(row) for row in
                con.execute(tools.validate_select(sql), params or {})]}

        monkeypatch.setattr("agent.run_sql", run_sql)
        monkeypatch.setattr("agent.search_policies", lambda _: {"rows": []})
        result = answer_question("West since July 2025")

    row = result["evidence"]["E2"]["rows"][0]
    columns = {"discount": "discount_pct", "freight": "freight_per_unit"}
    findings = [c for c in result["claims"] if c["kind"] == "finding"]
    for metric, column in columns.items():
        finding = next(c for c in findings if metric in c["parts"][0].get("text", "").lower())
        rendered = "".join(p.get("text", str(p.get("value", "")) + p.get("suffix", ""))
                           for p in finding["parts"]).lower()
        cited_columns = {p["column"] for p in finding["parts"] if "value" in p
                         and supported(p, result["evidence"])}
        for period in ("before", "after"):
            if metric == unavailable_metric and unavailable_period in (period, "both"):
                assert row[f"{period}_{column}"] is None
                assert f"{period} unavailable" in rendered
            else:
                assert row[f"{period}_{column}"] is not None
                expected = (30 if unavailable_metric == "freight"
                            and unavailable_period in (period, "both") else 10) if metric == "discount" else 5
                assert row[f"{period}_{column}"] == expected
                assert f"{period}_{column}" in cited_columns
        if metric == "freight" and unavailable_metric == "freight":
            assert "change: unavailable" in rendered
    assert result["dropped_count"] == 0


@pytest.mark.parametrize("missing_metric", ["discount", "freight"])
@pytest.mark.parametrize("missing_period", ["before", "after"])
def test_undefined_metric_does_not_hide_other_movement(missing_metric, missing_period, monkeypatch):
    # Returns can make gross sales or units sum to zero while revenue remains nonzero.
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE regions (region_id, region_name);
            INSERT INTO regions VALUES (1, 'West');
            CREATE TABLE sales (region_id, product_id, sale_month, net_revenue,
                                quantity, unit_price, discount_pct);
            CREATE TABLE costs (region_id, product_id, cost_month, unit_cogs, freight_per_unit);
        """)
        for period, month, freight in (("before", "2025-06", 5.0), ("after", "2025-07", 8.0)):
            sales = [(2, 10, 18, 10)]
            if period == missing_period:
                sales.append((-1, 20, -10, 50) if missing_metric == "discount"
                             else (-2, 5, -9, 10))
            for quantity, price, revenue, discount in sales:
                con.execute("INSERT INTO sales VALUES (1, 1, ?, ?, ?, ?, ?)",
                            (month, revenue, quantity, price, discount))
            con.execute("INSERT INTO costs VALUES (1, 1, ?, 1, ?)", (month, freight))

        def run_sql(sql, params=None):
            return {"sql": sql, "params": params or {}, "rows": [dict(row) for row in
                con.execute(tools.validate_select(sql), params or {})]}

        monkeypatch.setattr("agent.run_sql", run_sql)
        monkeypatch.setattr("agent.search_policies", lambda _: {"rows": []})
        result = answer_question("West since July 2025")

    row = result["evidence"]["E2"]["rows"][0]
    missing_column = "discount_pct" if missing_metric == "discount" else "freight_per_unit"
    assert row[f"{missing_period}_{missing_column}"] is None
    findings = [claim for claim in result["claims"] if claim["kind"] == "finding"]
    missing = next(claim for claim in findings if missing_metric in claim["parts"][0]["text"].lower())
    available_metric = "freight" if missing_metric == "discount" else "discount"
    available = next(claim for claim in findings if available_metric in claim["parts"][0]["text"].lower())

    def render(claim):
        return "".join(part.get("text", str(part.get("value", "")) + part.get("suffix", ""))
                       for part in claim["parts"])

    assert f"{missing_period} unavailable" in render(missing).lower()
    available_period = "after" if missing_period == "before" else "before"
    available_column = f"{available_period}_{missing_column}"
    assert any(part.get("column") == available_column and supported(part, result["evidence"])
               for part in missing["parts"] if "value" in part)
    if missing_metric == "freight":
        assert "change: unavailable" in render(missing).lower()
        assert render(available) == "Average discount: before 10.0%; after 10.0%."
    else:
        assert render(available) == "Freight per unit: before $5.0; after $8.0; change: 60.0%."
    assert all(supported(part, result["evidence"]) for claim in (missing, available)
               for part in claim["parts"] if "value" in part)
    assert result["dropped_count"] == 0


@pytest.fixture
def policy_rows(monkeypatch):
    row = {
        "region_name": "West", "first_month": "2025-01", "last_month": "2025-09",
        "before_last_month": "2025-06", "after_first_month": "2025-07",
        "before_margin_pct": 40, "after_margin_pct": 25, "margin_drop_pts": 15,
        "margin_direction": "fell", "before_discount_pct": 3, "after_discount_pct": 18,
        "before_freight_per_unit": 5, "after_freight_per_unit": 8,
        "freight_increase_pct": 60,
        "missing_cost_count": 0, "missing_discount_count": 0, "missing_revenue_count": 0,
    }
    policies = [{"id": i, "doc": path.name, "chunk": path.read_text(encoding="utf-8").strip()}
                for i, path in enumerate(sorted(Path(__file__).with_name("docs").glob("*.md")), 1)]
    monkeypatch.setattr("agent.run_sql", lambda sql, params=None: {
        "sql": sql, "params": params or {}, "rows": [row]})
    monkeypatch.setattr("agent.search_policies", lambda _: {
        "sql": "SELECT policy evidence", "params": {}, "rows": policies})
    return policies


def test_reviewed_policy_text_supports_claims_and_only_matching_citations(policy_rows):
    policy_rows.extend({**policy, "id": policy["id"] + 10, "chunk": "Unrelated policy text."}
                       for policy in policy_rows[:])
    result = answer_question("West since July 2025")
    claims = [claim for claim in result["claims"] if claim["kind"] in {"reason", "action"}]
    assert [claim["kind"] for claim in claims] == ["reason", "action", "action", "action"]
    assert all(claim["policy_ids"] for claim in claims)
    assert {citation for claim in claims for citation in claim["policy_ids"]} == {"P1", "P2", "P3", "P4"}
    assert result["dropped_count"] == 0


@pytest.mark.parametrize("filename,contradiction", [
    ("west_q3_volume_push.md", "West cancelled the discount push; Sales and Finance approved every deal."),
    ("freight_contract_memo.md", "West retained its freight carrier and the existing rates."),
    ("pricing_policy.md", "Freight surcharges are prohibited on all orders."),
    ("regional_playbook.md", "Keep broad discount programs; do not re-tender or consolidate freight."),
])
@pytest.mark.parametrize("revision", ["missing", "header", "replacement", "addendum"])
def test_policy_filenames_do_not_prove_claims(policy_rows, filename, contradiction, revision):
    policy = next(policy for policy in policy_rows if policy["doc"] == filename)
    original = policy["chunk"]
    if revision == "missing":
        del policy["chunk"]
    else:
        policy["chunk"] = {"header": original.splitlines()[0], "replacement": contradiction,
                           "addendum": original + "\n\nCorrection: " + contradiction}[revision]

    result = answer_question("West since July 2025")

    assert all(f"P{policy['id']}" not in claim["policy_ids"] for claim in result["claims"])
    if filename in {"west_q3_volume_push.md", "freight_contract_memo.md"}:
        assert not any(claim["kind"] == "reason" for claim in result["claims"])
    else:
        assert not any(claim["kind"] == "action" for claim in result["claims"])
    assert len([claim for claim in result["claims"] if claim["kind"] == "finding"]) == 3
    assert result["dropped_count"] == 0


def test_updated_policy_source_invalidates_unchanged_retrieved_chunk(policy_rows, tmp_path, monkeypatch):
    for policy in policy_rows:
        content = policy["chunk"]
        if policy["doc"] == "west_q3_volume_push.md":
            content += "\n\nCorrection: the program was cancelled before launch."
        (tmp_path / policy["doc"]).write_text(content, encoding="utf-8")
    monkeypatch.setattr("agent.DOCS", tmp_path)

    result = answer_question("West since July 2025")

    assert not any(claim["kind"] == "reason" for claim in result["claims"])
    assert not any(claim["kind"] == "action" for claim in result["claims"])
    assert result["dropped_count"] == 0


@pytest.mark.parametrize("revision", ["unchanged", "superseding", "missing"])
def test_fixed_policy_claims_require_reviewed_complete_corpus(policy_rows, tmp_path, monkeypatch, revision):
    for policy in policy_rows:
        (tmp_path / policy["doc"]).write_text(policy["chunk"], encoding="utf-8")
    if revision == "superseding":
        (tmp_path / "superseding.md").write_text(
            "The West discount push is revoked; do not follow the earlier freight recommendations.",
            encoding="utf-8")
    elif revision == "missing":
        (tmp_path / "regional_playbook.md").unlink()
    monkeypatch.setattr("agent.DOCS", tmp_path)

    # The search results still contain only the original, unchanged chunks.
    result = answer_question("West since July 2025")

    policy_claims = [claim for claim in result["claims"] if claim["kind"] in {"reason", "action"}]
    if revision == "unchanged":
        assert [claim["kind"] for claim in policy_claims] == ["reason", "action", "action", "action"]
    else:
        assert policy_claims == []
    assert len([claim for claim in result["claims"] if claim["kind"] == "finding"]) == 3
    assert result["dropped_count"] == 0


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


@pytest.fixture
def retrieved_policy_rows(monkeypatch):
    summary = {
        "region_name": "West", "first_month": "2025-01", "last_month": "2025-09",
        "before_last_month": "2025-06", "after_first_month": "2025-07",
        "before_margin_pct": 40, "after_margin_pct": 25, "margin_drop_pts": 15,
        "margin_direction": "fell", "before_discount_pct": 3, "after_discount_pct": 18,
        "before_freight_per_unit": 5, "after_freight_per_unit": 8,
        "freight_increase_pct": 60,
        "missing_cost_count": 0, "missing_discount_count": 0, "missing_revenue_count": 0,
    }
    monkeypatch.setattr("agent.run_sql", lambda sql, params=None: {
        "sql": sql, "params": params or {}, "rows": [summary]})
    rows = [{"id": index, "doc": path.name, "chunk": path.read_text(encoding="utf-8")}
            for index, path in enumerate(sorted(Path(__file__).with_name("docs").glob("*.md")), 1)]
    monkeypatch.setattr("agent.search_policies", lambda _: {
        "sql": "SELECT ...", "params": {}, "rows": rows})
    return rows


@pytest.mark.parametrize("content", [
    "original", "whitespace", "unrelated", "missing", "empty", "null", "changed", "revoked",
])
def test_policy_claims_require_reviewed_content(retrieved_policy_rows, content):
    changes = {
        "west_q3_volume_push.md": ("It was not sent", "It was sent"),
        "freight_contract_memo.md": ("the West region moves", "the West region does not move"),
        "pricing_policy.md": ("may be added", "must not be added"),
        "regional_playbook.md": ("Re-tender freight lanes", "Do not re-tender freight lanes"),
    }
    for row in retrieved_policy_rows:
        if content == "whitespace":
            row["chunk"] = "\r\n  ".join(row["chunk"].split())
        elif content == "unrelated":
            row["chunk"] = "Office opening hours and holiday arrangements."
        elif content == "missing":
            del row["chunk"]
        elif content == "empty":
            row["chunk"] = ""
        elif content == "null":
            row["chunk"] = None
        elif content == "changed":
            row["chunk"] = row["chunk"].replace(*changes[row["doc"]])
        elif content == "revoked":
            row["chunk"] += "\nThis document is revoked; its statements and recommendations no longer apply."

    result = answer_question("Why did West margin fall since July 2025?")
    policy_claims = [c for c in result["claims"] if c["kind"] in ("reason", "action")]
    if content in ("original", "whitespace"):
        assert [c["kind"] for c in policy_claims] == ["reason", "action", "action", "action"]
        assert {p for c in policy_claims for p in c["policy_ids"]} == {
            f"P{row['id']}" for row in retrieved_policy_rows}
    else:
        assert policy_claims == []
    assert any(c["kind"] == "finding" for c in result["claims"])
    assert result["dropped_count"] == 0


def test_policy_claims_cite_only_supporting_chunks(retrieved_policy_rows):
    supporting_ids = {f"P{row['id']}" for row in retrieved_policy_rows}
    retrieved_policy_rows.extend([
        {**row, "id": row["id"] + 100, "chunk": "Owner: Regional managers."}
        for row in retrieved_policy_rows
    ])

    result = answer_question("Why did West margin fall since July 2025?")

    assert [c["kind"] for c in result["claims"] if c["policy_ids"]] == [
        "reason", "action", "action", "action"]
    assert {p for c in result["claims"] for p in c["policy_ids"]} == supporting_ids


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
    policy_sources = [(key, source) for key, source in west_answer["evidence"].items()
                      if source["kind"] == "policy"]
    assert len(policy_sources) > 1
    with connect() as con, con.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        for key, source in policy_sources:
            params = json.loads(json.dumps(source["params"]))
            rows = cur.execute(source["sql"], params).fetchall()
            columns = [column[0].lower() for column in cur.description]
            assert [dict(zip(columns, row)) for row in rows] == source["rows"]
            assert len(source["rows"]) == 1
            assert key == f"P{source['rows'][0]['id']}"
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


def test_fractional_oracle_numbers_remain_exact_in_evidence(monkeypatch):
    sql = ("SELECT 9007199254740993.25 AS amount, -9007199254740993.25 AS negative_amount, "
           "0.0000000000000000000000000001 AS tiny, 42 AS whole, NULL AS missing "
           "FROM regions WHERE region_name=:name")
    result = tools.run_sql(sql, {"name": "West"})
    row = result["rows"][0]
    assert row == {"amount": Decimal("9007199254740993.25"),
                   "negative_amount": Decimal("-9007199254740993.25"),
                   "tiny": Decimal("0.0000000000000000000000000001"),
                   "whole": Decimal("42"), "missing": None}
    assert all(isinstance(row[column], Decimal)
               for column in ("amount", "negative_amount", "tiny", "whole"))

    evidence = {"E1": {"kind": "sql", **result}}
    part = {"value": row["amount"], "evidence": "E1", "row": 0, "column": "amount"}
    assert supported(part, evidence)
    assert not supported({**part, "value": "9007199254740994"}, evidence)
    monkeypatch.setattr("app.answer_question", lambda _: {
        "region": "West", "claims": [{"kind": "finding", "parts": [part], "policy_ids": []}],
        "evidence": evidence,
    })
    response = app.test_client().post("/", data={"question": "West"})
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert ">9007199254740993.25</button>" in html
    serialized = json.loads(re.search(r"const evidence = (.*?);", html).group(1))
    assert serialized["E1"]["rows"][0] == {
        "amount": "9007199254740993.25", "negative_amount": "-9007199254740993.25",
        "tiny": "1E-28", "whole": "42", "missing": None,
    }
def test_sql_decimal_amount_is_exact_in_critic_and_browser(monkeypatch):
    source = {"kind": "sql", **tools.run_sql(
        "SELECT SUM(100000000000000.01) AS total FROM regions WHERE region_name=:name",
        {"name": "West"})}
    evidence = {"E1": source}
    amount = source["rows"][0]["total"]
    assert isinstance(amount, Decimal)
    assert amount == Decimal("100000000000000.01")

    part = {"value": "100000000000000.01", "evidence": "E1", "row": 0, "column": "total"}
    assert supported(part, evidence)
    assert not supported({**part, "value": "100000000000000.02"}, evidence)

    monkeypatch.setattr("app.answer_question", lambda _: {
        "region": "West", "claims": [{"kind": "finding", "parts": [part]}], "evidence": evidence})
    response = app.test_client().post("/", data={"question": "West"})
    assert response.status_code == 200
    serialized = response.get_data(as_text=True).split("const evidence = ", 1)[1].split(";", 1)[0]
    assert json.loads(serialized)["E1"]["rows"][0]["total"] == "100000000000000.01"
    assert source["rows"][0]["total"] == amount


def test_select_preserves_semicolon_in_string():
    result = tools.run_sql("SELECT 'West; -- report' AS label FROM regions WHERE region_name=:name; -- report",
                           {"name": "West"})
    assert result["rows"] == [{"label": "West; -- report"}]


@pytest.mark.parametrize("host", [
    "rebind.example:5055", "localhost.rebind.example:5055",
    "evil.localhost:5055", "127.0.0.1.rebind.example:5055",
    "localhost@rebind.example:5055",
])
@pytest.mark.parametrize("method", ["get", "post"])
def test_ui_rejects_untrusted_hosts_before_analysis(monkeypatch, host, method):
    analysis = MagicMock()
    monkeypatch.setattr("app.answer_question", analysis)

    response = getattr(app.test_client(), method)(
        "/", headers={"Host": host}, data={"question": "West"},
    )

    assert response.status_code == 400
    analysis.assert_not_called()


@pytest.mark.parametrize("host", ["localhost", "localhost:5055", "127.0.0.1", "127.0.0.1:5055"])
def test_ui_accepts_local_hosts(monkeypatch, host):
    monkeypatch.setattr("app.answer_question", lambda _: {
        "region": "West", "claims": [], "dropped_count": 0,
        "evidence": {"E1": {"kind": "sql", "sql": "SELECT revenue FROM sales",
                            "params": {}, "rows": [{"revenue": 12345}]}},
    })
    client = app.test_client()

    assert client.get("/", headers={"Host": host}).status_code == 200
    response = client.post("/", headers={"Host": host}, data={"question": "West"})
    assert response.status_code == 200
    assert b"SELECT revenue FROM sales" in response.data


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


@pytest.mark.parametrize("missing_reason,missing_actions", [
    (False, False), (True, False), (False, True), (True, True),
])
def test_ui_discloses_missing_explanation_and_actions(policy_rows, missing_reason, missing_actions):
    for policy in policy_rows:
        if ((missing_reason and policy["doc"] == "west_q3_volume_push.md") or
                (missing_actions and policy["doc"] == "pricing_policy.md")):
            policy["chunk"] = ""

    response = app.test_client().post("/", data={"question": "West since July 2025"})

    assert response.status_code == 200
    assert b"West gross margin" in response.data
    assert (b"No verified explanation is available for this brief." in response.data) == missing_reason
    assert (b"No verified actions are available for this brief." in response.data) == missing_actions
    assert (b"The policy documents identify a regional volume discount push" in response.data) == (not missing_reason)
    assert (b"Review discount approvals with Sales and Finance" in response.data) == (not missing_actions)
def test_brief_shows_selected_region_monthly_trend_with_cell_citations(monkeypatch):
    rows = [
        {"region_name": "East", "sale_month": "2025-06", "margin_pct": 70,
         "discount_pct": 1, "freight_per_unit": 2},
        {"region_name": "West", "sale_month": "2025-06", "margin_pct": 38.9,
         "discount_pct": 0, "freight_per_unit": 5.4},
        {"region_name": "East", "sale_month": "2025-07", "margin_pct": 71,
         "discount_pct": 2, "freight_per_unit": 3},
        {"region_name": "West", "sale_month": "2025-07", "margin_pct": None,
         "discount_pct": 18.25, "freight_per_unit": 8.6},
    ]
    result = {"region": "West", "claims": [], "dropped_count": 0,
              "evidence": {"E1": {"kind": "sql", "sql": "SELECT ...", "params": {}, "rows": rows}}}
    monkeypatch.setattr("app.answer_question", lambda _: result)

    class BriefTableParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_article = False
            self.in_table = False
            self.tables = []
            self.current_row = None
            self.cell = None

        def handle_starttag(self, tag, attrs):
            if tag == "article":
                self.in_article = True
            elif self.in_article and tag == "table":
                self.in_table = True
                self.tables.append([])
            elif self.in_table and tag == "tr":
                self.current_row = []
                self.tables[-1].append(self.current_row)
            elif self.in_table and tag in ("th", "td"):
                self.cell = {"text": "", "buttons": []}
                self.current_row.append(self.cell)
            elif self.cell is not None and tag == "button":
                self.cell["buttons"].append(dict(attrs))

        def handle_data(self, data):
            if self.cell is not None:
                self.cell["text"] += data

        def handle_endtag(self, tag):
            if tag in ("th", "td"):
                self.cell = None
            elif tag == "table":
                self.in_table = False
            elif tag == "article":
                self.in_article = False

    response = app.test_client().post("/", data={"question": "West"})
    assert response.status_code == 200
    parser = BriefTableParser()
    parser.feed(response.get_data(as_text=True))
    assert len(parser.tables) == 1
    header, *body = parser.tables[0]
    labels = [cell["text"].strip().lower() for cell in header]
    assert len(labels) == 4
    assert all(term in label for term, label in zip(("month", "margin", "discount", "freight"), labels))
    assert len(body) == 2
    for table_row, evidence_index in zip(body, (1, 3)):
        assert len(table_row) == 4
        for cell, column, label in zip(table_row, ("sale_month", "margin_pct", "discount_pct", "freight_per_unit"), labels):
            value = rows[evidence_index][column]
            displayed = cell["text"].strip()
            if value is None:
                assert displayed in ("N/A", "—", "-")
                assert not cell["buttons"]
                continue
            assert len(cell["buttons"]) == 1
            button = cell["buttons"][0]
            assert button["data-evidence"] == "E1"
            assert button["data-row"] == str(evidence_index)
            assert button["data-column"] == column
            if column == "sale_month":
                assert displayed == value
            else:
                unit = "%" if column.endswith("_pct") else "$"
                assert unit in displayed or unit in label
                assert float(displayed.replace("%", "").replace("$", "").strip()) == value


@pytest.mark.parametrize("has_claims,dropped_count", [(True, 0), (True, 2), (False, 2), (False, 0)])
def test_ui_discloses_dropped_claims_and_empty_brief(monkeypatch, has_claims, dropped_count):
    result = {
        "region": "West",
        "claims": [{"kind": "finding", "parts": [{"text": "Retained finding."}]}] if has_claims else [],
        "dropped_count": dropped_count,
        "evidence": {},
    }
    monkeypatch.setattr("app.answer_question", lambda _: result)

    response = app.test_client().post("/", data={"question": "West"})

    assert response.status_code == 200
    assert (b"2 claims were omitted because they failed verification." in response.data) == bool(dropped_count)
    assert (b"No verified claims are available for this brief." in response.data) == (not has_claims)
    assert (b"Numbers in retained claims verified against cited SQL cells." in response.data) == has_claims
    assert (b"Retained finding." in response.data) == has_claims
    assert b"No verified explanation is available for this brief." in response.data
    assert b"No verified actions are available for this brief." in response.data
    assert b"Policy-backed explanation is unavailable for this brief." in response.data
    assert b"Policy-backed recommendations are unavailable for this brief." in response.data
    assert b"Explanation unavailable" in response.data
    assert b"Recommendations unavailable" in response.data


@pytest.mark.parametrize("unavailable,question,missing_reason,missing_actions,missing_explanation", [
    ((), "West since July 2025", False, False, False),
    (("west_q3_volume_push.md",), "West since July 2025", True, False, True),
    (("pricing_policy.md",), "West since July 2025", False, True, False),
    (("west_q3_volume_push.md", "pricing_policy.md"), "West since July 2025", True, True, True),
    (None, "West since July 2025", True, True, True),
    ((), "West since August 2025", True, False, False),
])
def test_ui_reports_unavailable_policy_claims(
        policy_rows, unavailable, question, missing_reason, missing_actions, missing_explanation):
    if unavailable is None:
        policy_rows.clear()
    else:
        for policy in policy_rows:
            if policy["doc"] in unavailable:
                policy["chunk"] = "This policy has changed."

    response = app.test_client().post("/", data={"question": question})

    assert response.status_code == 200
    assert b'data-evidence="E2"' in response.data
    assert b"West gross margin fell from" in response.data
    assert (b"Policy-backed explanation is unavailable for this brief." in response.data) == missing_reason
    assert (b"Policy-backed recommendations are unavailable for this brief." in response.data) == missing_actions
    assert (b"Policy citations support the qualitative explanation." in response.data) == (not missing_reason)
    assert (b"Explanation unavailable" in response.data) == missing_explanation
    assert (b"Recommendations unavailable" in response.data) == missing_actions


@pytest.mark.parametrize("reason_available,actions_available", [
    (True, True), (True, False), (False, True), (False, False),
])
@pytest.mark.parametrize("missing_policy", ["not retrieved", "changed"])
def test_ui_discloses_missing_policy_backed_sections(
    policy_rows, reason_available, actions_available, missing_policy
):
    missing_docs = set()
    if not reason_available:
        missing_docs.add("west_q3_volume_push.md")
    if not actions_available:
        missing_docs.add("pricing_policy.md")
    if missing_policy == "not retrieved":
        policy_rows[:] = [row for row in policy_rows if row["doc"] not in missing_docs]
    else:
        for row in policy_rows:
            if row["doc"] in missing_docs:
                row["chunk"] = "Changed policy content."

    response = app.test_client().post("/", data={"question": "West since July 2025"})

    assert response.status_code == 200
    assert (b"Explanation unavailable" in response.data) == (not reason_available)
    assert (b"Recommendations unavailable" in response.data) == (not actions_available)
    assert (b"The policy documents identify" in response.data) == reason_available
    assert (b"Review discount approvals" in response.data) == actions_available
    assert b"omitted because they failed verification" not in response.data


@pytest.mark.parametrize("data", [None, {}, {"unrelated": "value"}, {"question": ""}])
def test_ui_missing_question_rejected_before_querying(monkeypatch, data):
    def forbidden_query(*args, **kwargs):
        pytest.fail("Missing questions must be rejected before querying")

    monkeypatch.setattr("agent.run_sql", forbidden_query)
    response = app.test_client().post("/", data=data)
    assert response.status_code == 400


def test_focus_indicator_contrasts_with_page_and_cards():
    html = app.test_client().get("/").get_data(as_text=True)

    def declaration(selector, name):
        rule = re.search(rf"{re.escape(selector)}\s*\{{([^}}]*)\}}", html)
        assert rule, f"Missing {selector} rule"
        value = re.search(rf"(?:^|;)\s*{re.escape(name)}\s*:\s*([^;]+)", rule.group(1))
        assert value, f"Missing {name} in {selector}"
        return value.group(1).strip()

    def luminance(color):
        color = {"white": "#ffffff"}.get(color.lower(), color)
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", color)
        channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                  for c in channels]
        return sum(weight * channel for weight, channel in zip((0.2126, 0.7152, 0.0722), linear))

    outline = declaration(":focus-visible", "outline").split()[-1]
    for surface in (declaration(":root", "background"), declaration(".card", "background")):
        dark, light = sorted((luminance(outline), luminance(surface)))
        contrast = (light + 0.05) / (dark + 0.05)
        assert contrast >= 3, f"Focus outline {outline} has {contrast:.2f}:1 contrast against {surface}"


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
def test_evidence_dialog_preserves_exact_integers(monkeypatch):
    values = [9007199254740993, -9007199254740993, 10**37 + 1,
              9007199254740991, 0, 42, 1.25, None, True, False, "text"]
    result = {
        "region": "West", "claims": [],
        "evidence": {"E1": {"kind": "sql", "sql": "SELECT ...", "params": {},
                            "rows": [{"units": value} for value in values]}},
    }
    original = copy.deepcopy(result)
    monkeypatch.setattr("app.answer_question", lambda _: result)
    response = app.test_client().post("/", data={"question": "West"})
    assert response.status_code == 200
    assert result == original  # Rendering must not change the critic's evidence.
    script = response.get_data(as_text=True).split("<script>", 1)[1].split("</script>", 1)[0]
    check = subprocess.run(["node", "-e", """
        const assert = require('node:assert/strict');
        const cells = [];
        let openEvidence;
        const button = {
            dataset: {evidence: 'E1'},
            addEventListener: (_, handler) => { openEvidence = handler; },
        };
        const table = {
            createTHead: () => ({insertRow: () => ({append() {}})}),
            createTBody: () => ({insertRow: () => ({insertCell: () => {
                const cell = {};
                cells.push(cell);
                return cell;
            }})}),
        };
        const element = {
            addEventListener() {}, replaceChildren() {}, showModal() {},
            querySelector: () => null,
        };
        global.document = {
            getElementById: () => element,
            querySelectorAll: () => [button],
            createElement: tag => tag === 'table' ? table : {},
        };
    """ + script + """
        openEvidence();
        assert.deepEqual(cells.map(cell => String(cell.textContent)), [
            '9007199254740993', '-9007199254740993',
            '10000000000000000000000000000000000001', '9007199254740991',
            '0', '42', '1.25', 'NULL', 'true', 'false', 'text',
        ]);
        assert.deepEqual(evidence.E1.rows.map(row => row.units), [
            '9007199254740993', '-9007199254740993',
            '10000000000000000000000000000000000001', '9007199254740991',
            '0', '42', 1.25, null, true, false, 'text',
        ]);
    """], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_evidence_rows_region_is_keyboard_focusable_and_named(monkeypatch):
    monkeypatch.setattr("app.answer_question", lambda _: {
        "region": "West", "claims": [], "evidence": {},
    })

    class EvidenceParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.region = None
            self.heading_id = None
            self.rows_heading_id = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if attrs.get("id") == "evidence-rows":
                self.region = (tag, attrs)
            if tag == "h3":
                self.heading_id = attrs.get("id")

        def handle_data(self, data):
            if data.strip() == "Returned rows":
                self.rows_heading_id = self.heading_id

        def handle_endtag(self, tag):
            if tag == "h3":
                self.heading_id = None

    response = app.test_client().post("/", data={"question": "West"})
    assert response.status_code == 200
    parser = EvidenceParser()
    parser.feed(response.get_data(as_text=True))
    tag, attrs = parser.region
    assert tag == "div"
    assert "table-wrap" in attrs["class"].split()
    assert attrs.get("tabindex") == "0"
    assert attrs.get("role") == "region"
    assert parser.rows_heading_id and attrs.get("aria-labelledby") == parser.rows_heading_id


def test_evidence_dialog_identifies_cited_cell(monkeypatch):
    monkeypatch.setattr("app.answer_question", lambda _: {
        "region": "West", "claims": [], "evidence": {},
    })
    html = app.test_client().post("/", data={"question": "West"}).get_data(as_text=True)

    assert 'aria-describedby="evidence-description"' in html
    assert '<p id="evidence-description"></p>' in html
    assert "description.textContent = '';" in html  # Clear the previous citation on every open.
    assert "description.textContent = `Cited cell: row ${index + 1}, column ${column}.`;" in html
    assert "td.append(' (Cited cell)');" in html
    assert "td.setAttribute('aria-describedby', 'evidence-description');" in html
    assert html.index("description.textContent = `Cited cell:") < html.index("dialog.showModal()")
def test_policy_citations_have_descriptive_accessible_names(monkeypatch):
    documents = {"P1": "west_q3_volume_push.md", "P2": 'Freight & "carrier" memo.md'}
    result = {
        "region": "West",
        "claims": [{"kind": "reason", "parts": [{"text": "Policy context."}],
                    "policy_ids": list(documents)}],
        "evidence": {id: {"kind": "policy", "rows": [{"doc": doc}]}
                     for id, doc in documents.items()},
    }
    monkeypatch.setattr("app.answer_question", lambda _: result)

    class CitationParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.buttons = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "button" and attrs.get("class") == "citation":
                self.buttons.append(attrs)

    response = app.test_client().post("/", data={"question": "West"})
    assert response.status_code == 200
    parser = CitationParser()
    parser.feed(response.get_data(as_text=True))
    # Both inline citations and the evidence explorer must identify the document.
    assert len(parser.buttons) == 4
    for button in parser.buttons:
        evidence_id = button["data-evidence"]
        assert evidence_id in button.get("aria-label", "")
        assert documents[evidence_id] in button["aria-label"]


def test_question_validation_errors_are_associated_and_focused(monkeypatch):
    class FormParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.question = None
            self.alert = None
            self.alert_text = ""
            self.in_alert = False

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "input" and attrs.get("id") == "question":
                self.question = attrs
            if tag == "p" and attrs.get("role") == "alert":
                self.alert = attrs
                self.in_alert = True

        def handle_data(self, data):
            if self.in_alert:
                self.alert_text += data

        def handle_endtag(self, tag):
            if tag == "p":
                self.in_alert = False

    def parse(response):
        parser = FormParser()
        parser.feed(response.get_data(as_text=True))
        return parser

    client = app.test_client()
    for data, status, message in [
        ({"question": ""}, 400, "Enter a business question"),
        ({"question": "North"}, 400, "Unsupported region"),
        ({"question": "x" * (app.config["MAX_CONTENT_LENGTH"] + 1)}, 413,
         "Submission too large"),
    ]:
        response = client.post("/", data=data)
        assert response.status_code == status
        page = parse(response)
        assert page.question.get("aria-invalid") == "true"
        assert page.question.get("aria-describedby") == page.alert.get("id")
        assert page.question.get("aria-describedby")
        assert "autofocus" in page.question
        assert message in page.alert_text

    def failure(_):
        raise RuntimeError("unavailable")

    monkeypatch.setattr("app.answer_question", failure)
    for response in [client.get("/"), client.post("/", data={"question": "West"})]:
        page = parse(response)
        assert "aria-invalid" not in page.question
        assert "autofocus" not in page.question


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


def test_ui_sanitizes_invalid_policy_encoding(tmp_path, monkeypatch):
    (tmp_path / "policy.md").write_bytes(b"\xff")
    monkeypatch.setattr("policies.DOCS", tmp_path)
    row = {"region_name": "West", "missing_cost_count": 0, "missing_discount_count": 0, "missing_revenue_count": 0,
           "before_margin_pct": 40, "after_margin_pct": 25}
    monkeypatch.setattr("agent.run_sql", lambda sql, params=None: {
        "sql": sql, "params": params or {}, "rows": [row]})

    response = app.test_client().post("/", data={"question": "West since July 2025"})

    assert response.status_code == 503
    assert b"Analysis unavailable. Check the local Oracle connection and policy setup, then retry." in response.data
    assert b"codec" not in response.data


def test_ui_sanitizes_data_quality_error(monkeypatch):
    def failure(_):
        raise DataQualityError("private-data-quality-details")

    monkeypatch.setattr("app.answer_question", failure)
    response = app.test_client().post("/", data={"question": "West"})

    assert response.status_code == 503
    assert b"Correct missing, incomplete, or duplicate monthly cost records, then retry." in response.data
    assert b"private-data-quality-details" not in response.data
    assert b"Oracle connection" not in response.data


def test_ui_sanitizes_discount_data_quality_error(monkeypatch):
    def failure(_):
        raise DiscountDataQualityError("private-data-quality-details")

    monkeypatch.setattr("app.answer_question", failure)
    response = app.test_client().post("/", data={"question": "West"})

    assert response.status_code == 503
    assert b"Correct missing discount, quantity, or unit price inputs, then retry." in response.data
    assert b"private-data-quality-details" not in response.data
    assert b"Oracle connection" not in response.data


def test_ui_sanitizes_revenue_data_quality_error(monkeypatch):
    def failure(_):
        raise RevenueDataQualityError("private-data-quality-details")

    monkeypatch.setattr("app.answer_question", failure)
    response = app.test_client().post("/", data={"question": "West"})

    assert response.status_code == 503
    assert b"Correct missing net revenue for sales, then retry." in response.data
    assert b"private-data-quality-details" not in response.data
    assert b"monthly cost records" not in response.data


def test_ui_sanitizes_downstream_value_error(monkeypatch):
    def failure(*args, **kwargs):
        raise ValueError("private-internal-details")

    monkeypatch.setattr("agent.run_sql", failure)
    response = app.test_client().post("/", data={"question": "West"})

    assert response.status_code == 503
    assert b"Analysis unavailable. Check the local Oracle connection and policy setup, then retry." in response.data
    assert b"private-internal-details" not in response.data


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
