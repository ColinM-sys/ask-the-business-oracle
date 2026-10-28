"""Unit checks and live integration against the existing local Oracle tables."""
import copy
import hashlib
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
import critic
import tools
from db import connect
from agent import (DataQualityError, DiscountDataQualityError, RevenueDataQualityError,
                   answer_question, parse_question, summary_sql)
from app import app
from critic import critique, supported
from policies import ensure_policies, hash_embedding, source_fingerprint


def mock_run_sql(monkeypatch, execute):
    monkeypatch.setattr("agent.run_sql_many", lambda queries: [
        execute(sql, params) for sql, params in queries
    ])


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


def test_readme_documents_reference_dataset_for_integration_tests():
    readme = " ".join(Path(__file__).with_name("README.md").read_text(encoding="utf-8").split())
    setup = readme.split("python policies.py", 1)[0]
    assert "`data/oracle_load.sql`" in setup
    assert "`data/*.csv`" in setup
    assert "fresh Oracle schema" in setup
    assert "python load.py" in setup
    test_instructions = readme.split("python -m pytest -q test_app.py", 1)[1]
    assert "West metric ranges" in test_instructions
    assert "`13.36`" in test_instructions
    assert "depend on the unmodified reference dataset" in test_instructions
def test_readme_documents_supported_year_range():
    readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
    assert ("a full month name plus year or `YYYY-MM`. "
            "Both formats require a year from 2000 through 2099.") in " ".join(readme.split())


def test_readme_documents_policy_fingerprint_maintenance():
    readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
    text = " ".join(readme.split())
    assert "review the fixed claims in `agent.py`" in text
    assert "update both `POLICY_SHA256` (SHA-256 of the stripped full document)" in text
    assert ("`REVIEWED_POLICY_HASHES` (SHA-256 of its complete chunk with whitespace collapsed). "
            "Then restart the Flask app to load the updated fingerprints.") in text
    assert "The revised document must still fit in one chunk" in text
    assert "affected explanations and recommendations remain unavailable even after refresh and retrieval" in text


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


def test_setup_files_are_present():
    root = Path(__file__).parent
    for name in (
        "requirements.txt", ".env.example", "data/oracle_load.sql",
        "docs/freight_contract_memo.md", "docs/pricing_policy.md",
        "docs/regional_playbook.md", "docs/west_q3_volume_push.md",
    ):
        path = root / name
        assert path.is_file(), f"Missing setup file: {name}"
        assert path.read_text(encoding="utf-8").strip(), f"Empty setup file: {name}"


def test_readme_includes_initial_business_data_load():
    readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
    setup = readme.split("```powershell", 1)[1].split("```", 1)[0]
    assert setup.index("docker start oracle-free") < setup.index("python load.py")
    assert setup.index("python load.py") < setup.index("python policies.py")
    assert "only once" in readme.lower()
    assert "skip" in readme.lower()


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


@pytest.mark.parametrize("sql, columns, rows", [
    ("SELECT region_id, region_id + 1 AS region_id FROM regions",
     ["REGION_ID", "REGION_ID"], [(1, 2)]),
    ('SELECT region_id AS "ID", region_id + 1 AS "id" FROM regions',
     ["ID", "id"], [(1, 2)]),
    ("SELECT region_id, region_id + 1 AS region_id FROM regions WHERE 1 = 0",
     ["REGION_ID", "REGION_ID"], []),
])
def test_run_sql_rejects_duplicate_evidence_columns(sql, columns, rows, monkeypatch):
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.description = [(name,) for name in columns]
    cursor.fetchmany.return_value = rows
    monkeypatch.setattr(tools, "connect", lambda: connection)

    with pytest.raises(ValueError, match="duplicate column names"):
        tools.run_sql(sql)


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


def test_docs_document_unchecked_spelled_out_numbers():
    claim = {"parts": [{"text": "Sales were five million."}]}
    assert critique([claim], {}) == ([claim], [])

    readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
    documentation = " ".join(readme.lower().split())
    assert "numbers written as words" in documentation
    assert "five million" in documentation
    assert "can pass without a citation" in documentation

    documentation = " ".join(critic.__doc__.lower().split())
    assert "structured numeric parts" in documentation
    assert "numbers written as words are unchecked" in documentation


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


def policy_source_hash(directory):
    sources = [(path.name, path.read_text(encoding="utf-8"))
               for path in sorted(directory.glob("*.md"))]
    return "v" * 32 + source_fingerprint(sources)


@pytest.mark.parametrize("failure_mode", ["incompatible", "null"])
def test_policy_probe_cache_tracks_model_versions(failure_mode, tmp_path, monkeypatch):
    (tmp_path / "policy.md").write_text("Distinct policy content", encoding="utf-8")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = ("POLICY_CHUNKS",)
    models = [("INCOMPATIBLE_MODEL", "created", 10, "v1"), ("GOOD_MODEL", "created", 20, "v1")]
    rows, probes, document_embeddings = [], [], []
    bad_recovers = [False]

    def execute(sql, **params):
        if "FROM user_mining_models" in sql:
            cursor.fetchall.return_value = (list(models) if "object_id" in sql.lower()
                                             else [(model[0],) for model in models])
        elif sql.startswith("SELECT DISTINCT backend, source_hash"):
            cursor.fetchall.return_value = [(row["backend"], row["fingerprint"]) for row in rows]
        elif sql.startswith("DELETE FROM policy_chunks"):
            rows.clear()
        return cursor

    def embedding(cur, text, backend):
        if text == "Business policy":
            probes.append(next(model for model in models if model[0] == backend))
            if backend == "INCOMPATIBLE_MODEL" and not bad_recovers[0]:
                if failure_mode == "null":
                    return None
                raise oracledb.DatabaseError(SimpleNamespace(code=40290))
        else:
            document_embeddings.append((backend, text))
        return hash_embedding(text)

    cursor.execute.side_effect = execute
    cursor.executemany.side_effect = lambda sql, bindings: rows.extend(bindings)
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)
    monkeypatch.setattr("policies.embed", embedding)
    monkeypatch.setattr("policies._probe_cache", {}, raising=False)

    assert ensure_policies(include_fingerprint=True) == ("GOOD_MODEL", rows[0]["fingerprint"])
    assert ensure_policies(include_fingerprint=True) == ("GOOD_MODEL", rows[0]["fingerprint"])
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(lambda _: ensure_policies(), range(4))) == ["GOOD_MODEL"] * 4
    assert probes == models
    assert document_embeddings == [("GOOD_MODEL", "Distinct policy content")]

    models[0] = ("INCOMPATIBLE_MODEL", "created", 11, "v1")
    assert ensure_policies() == "GOOD_MODEL"
    assert probes == [("INCOMPATIBLE_MODEL", "created", 10, "v1"), ("GOOD_MODEL", "created", 20, "v1"), models[0]]
    assert len(document_embeddings) == 1

    old_fingerprint = rows[0]["fingerprint"]
    models[1] = ("GOOD_MODEL", "created", 21, "v1")
    assert ensure_policies() == "GOOD_MODEL"
    assert probes[-1] == models[1]
    assert len(probes) == 4
    assert len(document_embeddings) == 2
    assert rows[0]["fingerprint"] != old_fingerprint

    models[1] = ("GOOD_MODEL", "created", 21, "v2")
    assert ensure_policies() == "GOOD_MODEL"
    assert probes[-1] == models[1]
    assert len(probes) == 5
    assert len(document_embeddings) == 3

    if failure_mode == "null":
        # A revised model can recover after the prior null probe was cached.
        bad_recovers[0] = True
        models[0] = ("INCOMPATIBLE_MODEL", "created", 11, "v2")
        assert ensure_policies() == "INCOMPATIBLE_MODEL"
        assert probes[-1] == models[0]
        assert document_embeddings[-1] == ("INCOMPATIBLE_MODEL", "Distinct policy content")


@pytest.mark.parametrize("text_type", ["VARCHAR2", "CLOB", "NVARCHAR2", "NCLOB"])
@pytest.mark.parametrize("image, null_model, good_model", [
    (True, False, True),
    (False, True, True),
    (False, True, False),
])
def test_policy_probe_selects_usable_text_model(
        text_type, image, null_model, good_model, tmp_path, monkeypatch):
    (tmp_path / "policy.md").write_text("Business policy", encoding="utf-8")
    metadata = sqlite3.connect(":memory:")
    metadata.execute("""CREATE TABLE user_mining_models
        (model_name TEXT, mining_function TEXT, creation_date TEXT)""")
    metadata.execute("""CREATE TABLE user_mining_model_attributes
        (model_name TEXT, attribute_name TEXT, attribute_type TEXT, data_type TEXT)""")
    metadata.execute("""CREATE TABLE user_objects
        (object_name TEXT, object_type TEXT, subobject_name TEXT, object_id INTEGER, last_ddl_time TEXT)""")
    models = []
    if image:
        models.append(("A_IMAGE", "UNSTRUCTURED", "BLOB"))
    if null_model:
        models.append(("B_NULL", "TEXT", text_type))
    if good_model:
        models.append(("C_GOOD", "TEXT", text_type))
    model_versions = {}
    for object_id, (name, attribute_type, data_type) in enumerate(models, 1):
        created = f"created-{name}"
        model_versions[name] = (name, created, object_id, "modified")
        metadata.execute("INSERT INTO user_mining_models VALUES (?, 'EMBEDDING', ?)", (name, created))
        metadata.execute("INSERT INTO user_objects VALUES (?, 'MINING MODEL', NULL, ?, 'modified')", (name, object_id))
        metadata.execute("INSERT INTO user_mining_model_attributes VALUES (?, 'DATA', ?, ?)",
                         (name, attribute_type, data_type))
        metadata.execute("INSERT INTO user_mining_model_attributes VALUES (?, 'ORA$ONNXTARGET', 'VECTOR', 'VECTOR')",
                         (name,))

    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = ("POLICY_CHUNKS",)
    cursor.fetchall.return_value = []
    cursor.execute.side_effect = lambda sql, **params: (
        metadata.execute(sql) if "user_mining_models" in sql.lower() else cursor)
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)
    monkeypatch.setattr("policies._probe_cache", {})
    probed = []

    def embedding(cur, text, backend):
        probed.append(backend)
        if backend == "A_IMAGE":
            raise oracledb.DatabaseError(SimpleNamespace(code=40284))
        if backend == "B_NULL":
            return None
        return hash_embedding(text)

    monkeypatch.setattr("policies.embed", embedding)
    expected = "C_GOOD" if good_model else "hash-v1-512"
    assert ensure_policies() == expected
    assert probed == (["B_NULL"] if null_model else []) + (["C_GOOD"] if good_model else []) + [expected]
    cursor.executemany.assert_called_once()
    inserted = cursor.executemany.call_args.args[1]
    assert inserted and all(row["backend"] == expected and row["vector"] is not None for row in inserted)
    model_version = model_versions[expected] if good_model else (expected,)
    version = "v" + hashlib.sha256(repr(model_version).encode()).hexdigest()[:31]
    assert all(row["fingerprint"].startswith(version) for row in inserted)
    connection.commit.assert_called_once()
    metadata.close()


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
    cursor.execute.return_value.fetchall.side_effect = [[("BROKEN_MODEL", "created", 1, "modified")]] * 2
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)
    monkeypatch.setattr("policies._probe_cache", {})

    attempts = []

    def embedding(cur, text, backend):
        attempts.append(backend)
        if backend == "BROKEN_MODEL":
            raise failure
        return hash_embedding(text)

    monkeypatch.setattr("policies.embed", embedding)

    for _ in range(2):
        with pytest.raises(type(failure)) as caught:
            ensure_policies()

        assert caught.value is failure
    assert attempts == ["BROKEN_MODEL", "BROKEN_MODEL"]
    connection.commit.assert_not_called()
    assert not any(call.args[0].startswith("DELETE") for call in cursor.execute.call_args_list)


@pytest.mark.parametrize("models, expected", [
    ([], "hash-v1-512"),
    ([("INCOMPATIBLE_MODEL", "created", 1, "v1")], "hash-v1-512"),
    ([("INCOMPATIBLE_MODEL", "created", 1, "v1"), ("GOOD_MODEL", "created", 2, "v1")], "GOOD_MODEL"),
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
    monkeypatch.setattr("policies._probe_cache", {})

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
    models = [(unsupported, "created", 1, "modified")] + ([(later_model, "created", 2, "modified")] if later_model else [])
    cursor.execute.return_value.fetchall.side_effect = [models, []]
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)
    monkeypatch.setattr("policies._probe_cache", {})

    expected = later_model or "hash-v1-512"
    assert ensure_policies() == expected
    embedding_sql = [call.args[0] for call in cursor.execute.call_args_list
                     if "VECTOR_EMBEDDING" in call.args[0]]
    assert embedding_sql == (["SELECT VECTOR_EMBEDDING(GOOD_MODEL USING :text AS DATA) FROM dual"] * 2
                             if later_model else [])
    cursor.executemany.assert_called_once()
    inserted = cursor.executemany.call_args.args[1]
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

    cursor.executemany.assert_called_once()
    chunks = [row["chunk"] for row in cursor.executemany.call_args.args[1]]
    assert chunks
    assert all(chunk and len(chunk.encode("utf-8")) <= 4000 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == content.replace("\n", "")
    connection.commit.assert_called_once()


@pytest.mark.parametrize("paragraph", [
    "a" * 999 + "bc" + "d" * 299,
    "a" * 999 + " " + "b" * 300,
    "a" * 999 + " " + "b" * 1300,
], ids=["split-word", "boundary-space", "space-across-chunks"])
def test_long_policy_paragraph_preserves_text_and_embedding(paragraph, tmp_path, monkeypatch):
    content = paragraph + "\n\nNext paragraph."
    (tmp_path / "policy.md").write_text(content, encoding="utf-8")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    cursor.execute.return_value.fetchone.return_value = ("POLICY_CHUNKS",)
    cursor.execute.return_value.fetchall.return_value = []
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)

    ensure_policies()

    cursor.executemany.assert_called_once()
    inserted = cursor.executemany.call_args.args[1]
    assert "".join(row["chunk"] for row in inserted) == content
    assert all(row["vector"] == hash_embedding(row["chunk"]) for row in inserted)


def test_policy_refresh_replaces_legacy_chunks_once(tmp_path, monkeypatch):
    content = "a" * 1300
    (tmp_path / "policy.md").write_text(content, encoding="utf-8")
    legacy_hash = hashlib.sha256(repr([("policy.md", content)]).encode()).hexdigest()
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    cursor.execute.return_value.fetchone.return_value = ("POLICY_CHUNKS",)
    cursor.execute.return_value.fetchall.side_effect = [[], [("hash-v1-512", legacy_hash)]]
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)

    ensure_policies()

    cursor.executemany.assert_called_once()
    inserted = cursor.executemany.call_args.args[1]
    assert inserted and inserted[0]["chunk"] == content
    new_hash = inserted[0]["fingerprint"]
    assert new_hash != legacy_hash
    connection.commit.assert_called_once()

    cursor.execute.reset_mock()
    cursor.executemany.reset_mock()
    connection.commit.reset_mock()
    cursor.execute.return_value.fetchall.side_effect = [[], [("hash-v1-512", new_hash)]]
    ensure_policies()
    assert not any(call.args[0].lstrip().startswith(("DELETE", "INSERT"))
                   for call in cursor.execute.call_args_list)
    cursor.executemany.assert_not_called()
    connection.commit.assert_not_called()


def test_policy_refresh_rechunks_model_versioned_cache(tmp_path, monkeypatch):
    paragraph = "a" * 999 + "bc" + "d" * 299
    sources = [("a.md", paragraph), ("b.md", "Shared policy")]
    for name, content in sources:
        (tmp_path / name).write_text(content, encoding="utf-8")

    backend = "hash-v1-512"
    version = "v" + hashlib.sha256(repr((backend,)).encode()).hexdigest()[:31]
    old_hash = version + hashlib.sha256(repr(sources).encode()).hexdigest()[:32]
    rows = [
        {"backend": backend, "fingerprint": old_hash,
         "chunk": paragraph[:1000] + "\n\n" + paragraph[1000:], "vector": "old long vector"},
        {"backend": backend, "fingerprint": old_hash,
         "chunk": "Shared policy", "vector": "shared vector"},
    ]
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = ("POLICY_CHUNKS",)

    def execute(sql, **params):
        if "FROM user_mining_models" in sql:
            cursor.fetchall.return_value = []
        elif sql.startswith("SELECT DISTINCT backend, source_hash"):
            cursor.fetchall.return_value = list({(row["backend"], row["fingerprint"]) for row in rows})
        elif sql.startswith("SELECT chunk, embedding FROM policy_chunks"):
            cursor.fetchall.return_value = [(row["chunk"], row["vector"]) for row in rows
                                           if row["backend"] == params["backend"]
                                           and row["fingerprint"].startswith(params["version"])]
        elif sql.startswith("DELETE FROM policy_chunks"):
            rows.clear()
        return cursor

    cursor.execute.side_effect = execute
    cursor.executemany.side_effect = lambda sql, bindings: rows.extend(bindings)
    embed = MagicMock(side_effect=lambda cur, text, model: "new vector for " + text)
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)
    monkeypatch.setattr("policies.embed", embed)

    assert ensure_policies() == backend
    embed.assert_called_once_with(cursor, paragraph, backend)
    assert [(row["doc"], row["chunk"], row["vector"]) for row in rows] == [
        ("a.md", paragraph, "new vector for " + paragraph),
        ("b.md", "Shared policy", "shared vector"),
    ]
    assert all(row["fingerprint"].startswith(version) and row["fingerprint"] != old_hash
               for row in rows)
    connection.commit.assert_called_once()

    embed.reset_mock()
    assert ensure_policies() == backend
    embed.assert_not_called()
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
    cursor.executemany.assert_not_called()
    connection.commit.assert_not_called()


def test_policy_refresh_reuses_chunk_embeddings_by_model(tmp_path, monkeypatch):
    documents = [tmp_path / name for name in ("a.md", "b.md", "c.md")]
    for document, content in zip(documents, ("Old policy", "Shared policy", "Shared policy")):
        document.write_text(content, encoding="utf-8")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = ("POLICY_CHUNKS",)
    # Legacy rows have no recorded model revision and must be refreshed once.
    rows = [{"backend": "MODEL_V1", "fingerprint": "0" * 64,
             "chunk": "Shared policy", "vector": "legacy vector"}]
    model = ["MODEL_V1", "created", 100, "modified"]
    embedding_calls = []

    def execute(sql, **params):
        if "FROM user_mining_models" in sql:
            cursor.fetchall.return_value = [tuple(model)]
        elif sql.startswith("SELECT DISTINCT backend, source_hash"):
            cursor.fetchall.return_value = list({(row["backend"], row["fingerprint"]) for row in rows})
        elif sql.startswith("SELECT chunk, embedding FROM policy_chunks"):
            cursor.fetchall.return_value = [(row["chunk"], row["vector"]) for row in rows
                                           if row["backend"] == params["backend"]
                                           and row["fingerprint"].startswith(params["version"])]
        elif sql.startswith("DELETE FROM policy_chunks"):
            rows.clear()
        return cursor

    def embedding(cur, text, backend):
        if text == "Business policy":  # The model compatibility probe is not a chunk.
            return "probe"
        embedding_calls.append((backend, text))
        if text == "Rejected policy":
            raise RuntimeError("embedding failed")
        return f"{backend}:{model[2]}:{text}"

    cursor.execute.side_effect = execute
    cursor.executemany.side_effect = lambda sql, bindings: rows.extend(bindings)
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)
    monkeypatch.setattr("policies.embed", embedding)

    assert ensure_policies() == "MODEL_V1"
    initial_calls = embedding_calls[:]
    original_shared_vector = rows[1]["vector"]
    original_fingerprint = rows[0]["fingerprint"]
    embedding_calls.clear()
    commits = connection.commit.call_count
    cursor.executemany.reset_mock()
    assert ensure_policies() == "MODEL_V1"
    assert not embedding_calls
    cursor.executemany.assert_not_called()
    assert connection.commit.call_count == commits
    documents[0].write_text("Updated policy", encoding="utf-8")
    cursor.execute.reset_mock()
    cursor.executemany.reset_mock()

    assert ensure_policies() == "MODEL_V1"
    assert embedding_calls == [("MODEL_V1", "Updated policy")]
    assert initial_calls == [("MODEL_V1", "Old policy"), ("MODEL_V1", "Shared policy")]
    cursor.executemany.assert_called_once()
    insert_sql, bindings = cursor.executemany.call_args.args
    assert insert_sql.lstrip().startswith("INSERT INTO policy_chunks")
    assert len(bindings) == 3
    assert not any(call.args[0].lstrip().startswith("INSERT INTO policy_chunks")
                   for call in cursor.execute.call_args_list)
    assert connection.commit.call_count == commits + 1
    assert [(row["id"], row["doc"], row["chunk"], row["vector"]) for row in rows] == [
        (1, "a.md", "Updated policy", "MODEL_V1:100:Updated policy"),
        (2, "b.md", "Shared policy", original_shared_vector),
        (3, "c.md", "Shared policy", original_shared_vector),
    ]
    assert len({row["fingerprint"] for row in rows}) == 1
    assert rows[0]["fingerprint"] != original_fingerprint

    model[2] = 101  # Replacing a model can keep its name but changes its object ID.
    embedding_calls.clear()
    assert ensure_policies() == "MODEL_V1"
    assert embedding_calls == [("MODEL_V1", "Updated policy"), ("MODEL_V1", "Shared policy")]
    assert all(row["vector"] == f'MODEL_V1:101:{row["chunk"]}' for row in rows)

    model[0] = "MODEL_V2"
    embedding_calls.clear()
    assert ensure_policies() == "MODEL_V2"
    assert embedding_calls == [("MODEL_V2", "Updated policy"), ("MODEL_V2", "Shared policy")]
    assert all(row["vector"] == f'MODEL_V2:101:{row["chunk"]}' for row in rows)

    previous_rows = copy.deepcopy(rows)
    commits = connection.commit.call_count
    documents[0].write_text("Rejected policy", encoding="utf-8")
    with pytest.raises(RuntimeError, match="embedding failed"):
        ensure_policies()
    assert rows == previous_rows
    assert connection.commit.call_count == commits


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
        if "FROM user_mining_models" in sql:
            cursor.fetchall.return_value = []
        elif sql.startswith("SELECT DISTINCT backend, source_hash"):
            cursor.fetchall.return_value = [(row["backend"], row["fingerprint"]) for row in rows]
        elif sql.startswith("DELETE FROM policy_chunks"):
            rows.clear()
        return cursor

    cursor.execute.side_effect = execute
    cursor.executemany.side_effect = lambda sql, bindings: rows.extend(bindings)
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


def test_concurrent_policy_refreshes_do_not_overwrite_newer_content(tmp_path, monkeypatch):
    document = tmp_path / "policy.md"
    document.write_text("Old policy", encoding="utf-8")
    old_embedding = threading.Event()
    resume_old = threading.Event()
    second_progress = threading.Event()
    second_waiting = threading.Event()
    second_read = threading.Event()
    worker = threading.local()
    database_lock = threading.Lock()
    rows_lock = threading.Lock()
    rows = []

    class Sources:
        def glob(self, pattern):
            if worker.name == "new":
                second_read.set()
                second_progress.set()
            return [document]

    class Connection:
        def __init__(self):
            self.locked = False
            self.pending = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.release()

        def release(self):
            if self.locked:
                self.locked = False
                database_lock.release()

        def cursor(self):
            return Cursor(self)

        def commit(self):
            if self.pending is not None:
                with rows_lock:
                    rows[:] = self.pending
            self.release()

    class Cursor:
        def __init__(self, connection):
            self.connection = connection
            self.result = []

        def execute(self, sql, **params):
            sql = " ".join(sql.upper().split())
            if sql.startswith("SELECT TABLE_NAME FROM USER_TABLES"):
                self.result = [("POLICY_CHUNKS",)]
            elif re.fullmatch(r"LOCK TABLE POLICY_CHUNKS IN EXCLUSIVE MODE WAIT [1-9][0-9]*", sql):
                if worker.name == "new" and database_lock.locked():
                    second_waiting.set()
                    second_progress.set()
                assert database_lock.acquire(timeout=10), "Database lock was not released"
                self.connection.locked = True
            elif sql.startswith("SELECT M.MODEL_NAME, M.CREATION_DATE,"):
                self.result = []
            elif sql.startswith("SELECT DISTINCT BACKEND, SOURCE_HASH FROM POLICY_CHUNKS"):
                with rows_lock:
                    self.result = [(row["backend"], row["fingerprint"]) for row in rows]
            elif sql.startswith("SELECT CHUNK, EMBEDDING FROM POLICY_CHUNKS"):
                with rows_lock:
                    self.result = [
                        (row["chunk"], row["vector"]) for row in rows
                        if row["backend"] == params["backend"]
                        and row["fingerprint"].startswith(params["version"])
                    ]
            elif sql.startswith("DELETE FROM POLICY_CHUNKS"):
                self.connection.pending = []
            else:
                pytest.fail(f"Unexpected SQL: {sql}")
            return self

        def executemany(self, sql, bindings):
            assert sql.lstrip().upper().startswith("INSERT INTO POLICY_CHUNKS")
            self.connection.pending.extend(bindings)

        def fetchone(self):
            return self.result[0] if self.result else None

        def fetchall(self):
            return self.result

    def embedding(cursor, text, backend):
        if text == "Old policy":
            old_embedding.set()
            assert resume_old.wait(10), "Old refresh was not released"
        return hash_embedding(text)

    def refresh(name):
        worker.name = name
        return ensure_policies()

    monkeypatch.setattr("policies.DOCS", Sources())
    monkeypatch.setattr("policies.connect", Connection)
    monkeypatch.setattr("policies.embed", embedding)
    # Each process has its own threading lock; database sessions must coordinate.
    monkeypatch.setattr("policies._lock", nullcontext())

    with ThreadPoolExecutor(max_workers=2) as executor:
        old = executor.submit(refresh, "old")
        try:
            assert old_embedding.wait(10), "Old refresh did not start embedding"
            document.write_text("Updated policy", encoding="utf-8")
            new = executor.submit(refresh, "new")
            assert second_progress.wait(10), "New refresh did not make progress"
            if second_waiting.is_set():
                assert not second_read.is_set()
                assert not new.done()
            else:
                # Without the database lock, the new version commits first.
                assert new.result(timeout=10) == "hash-v1-512"
        finally:
            resume_old.set()
        assert old.result(timeout=10) == "hash-v1-512"
        assert new.result(timeout=10) == "hash-v1-512"

    assert [row["chunk"] for row in rows] == ["Updated policy"]


@pytest.mark.parametrize("code", [955, 1031])
def test_policy_table_creation_race(code, tmp_path, monkeypatch):
    (tmp_path / "policy.md").write_text("Policy", encoding="utf-8")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    failure = oracledb.DatabaseError(SimpleNamespace(code=code))

    def execute(sql, **params):
        if sql.lstrip().startswith("CREATE TABLE"):
            raise failure
        return cursor

    cursor.execute.side_effect = execute
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)

    if code == 955:
        assert ensure_policies() == "hash-v1-512"
        assert any(re.fullmatch(r"LOCK TABLE POLICY_CHUNKS IN EXCLUSIVE MODE WAIT [1-9][0-9]*",
                                " ".join(call.args[0].upper().split()))
                   for call in cursor.execute.call_args_list)
        connection.commit.assert_called_once()
    else:
        with pytest.raises(oracledb.DatabaseError) as caught:
            ensure_policies()
        assert caught.value is failure
        assert not any(call.args[0].upper().startswith("LOCK TABLE")
                       for call in cursor.execute.call_args_list)
        connection.commit.assert_not_called()


@pytest.mark.parametrize("code", [54, 30006])
def test_policy_lock_timeout_returns_temporary_failure_and_allows_retry(code, tmp_path, monkeypatch):
    (tmp_path / "policy.md").write_text("Policy", encoding="utf-8")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value
    cursor.fetchone.return_value = ("POLICY_CHUNKS",)
    cursor.fetchall.return_value = []
    lock_attempts = []

    def execute(sql, **params):
        normalized = " ".join(sql.upper().split())
        if normalized.startswith("LOCK TABLE"):
            lock_attempts.append(normalized)
            if len(lock_attempts) == 1:
                raise oracledb.DatabaseError(SimpleNamespace(
                    code=code, message="private-driver-details"))
        return cursor

    cursor.execute.side_effect = execute
    monkeypatch.setattr("policies.DOCS", tmp_path)
    monkeypatch.setattr("policies.connect", lambda: connection)
    monkeypatch.setattr("app.answer_question", lambda _: tools.search_policies("Policy"))

    response = app.test_client().post("/", data={"question": "West"})

    assert response.status_code == 503
    assert b"retry" in response.data
    assert b"private-driver-details" not in response.data
    wait = re.fullmatch(r"LOCK TABLE POLICY_CHUNKS IN EXCLUSIVE MODE WAIT ([1-9][0-9]*)",
                        lock_attempts[0])
    assert wait and int(wait.group(1)) <= 30
    connection.__exit__.assert_called_once()
    connection.commit.assert_not_called()
    assert not any(call.args[0].lstrip().upper().startswith(("DELETE", "INSERT"))
                   for call in cursor.execute.call_args_list)

    assert ensure_policies() == "hash-v1-512"
    assert len(lock_attempts) == 2
    connection.commit.assert_called_once()


def test_policy_search_handles_empty_table(monkeypatch):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.execute.return_value.fetchall.return_value = []
    monkeypatch.setattr(tools, "ensure_policies", lambda **kwargs: ("hash-v1-512", "fingerprint"))
    monkeypatch.setattr(tools, "connect", lambda: connection)
    embedding = MagicMock()
    monkeypatch.setattr(tools, "embed", embedding)

    with pytest.raises(RuntimeError, match="No policy chunks"):
        tools.search_policies("West freight")

    embedding.assert_not_called()


def test_monthly_trend_and_brief_share_a_database_snapshot(tmp_path, monkeypatch):
    database = tmp_path / "snapshot.sqlite"
    with sqlite3.connect(database) as writer:
        writer.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE regions (region_id, region_name);
            CREATE TABLE sales (region_id, product_id, sale_month, net_revenue,
                                quantity, unit_price, discount_pct);
            CREATE TABLE costs (region_id, product_id, cost_month, unit_cogs, freight_per_unit);
            INSERT INTO regions VALUES (1, 'West');
            INSERT INTO sales VALUES (1, 1, '2025-06', 100.0, 1, 100.0, 0);
            INSERT INTO sales VALUES (1, 1, '2025-07', 100.0, 1, 100.0, 0);
            INSERT INTO costs VALUES (1, 1, '2025-06', 60.0, 5.0);
            INSERT INTO costs VALUES (1, 1, '2025-07', 60.0, 5.0);
        """)
        connections, statements, updates = [], [], []

        class Cursor:
            def __init__(self, connection):
                self.cursor = connection.cursor()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.cursor.close()

            @property
            def description(self):
                return self.cursor.description

            def execute(self, sql, params=None):
                statements.append(sql)
                if sql == "SET TRANSACTION READ ONLY":
                    self.cursor.execute("BEGIN")
                else:
                    self.cursor.execute(sql, params or {})
                return self

            def fetchmany(self, limit):
                rows = self.cursor.fetchmany(limit)
                if not updates:
                    writer.execute("UPDATE costs SET unit_cogs=85.0 WHERE cost_month='2025-07'")
                    writer.commit()
                    updates.append(True)
                return rows

        class Connection:
            def __init__(self):
                self.connection = sqlite3.connect(database)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.connection.close()

            def cursor(self):
                return Cursor(self.connection)

        def connect():
            connection = Connection()
            connections.append(connection)
            return connection

        monkeypatch.setattr(tools, "connect", connect)
        monkeypatch.setattr("agent.search_policies", lambda text: {"rows": []})

        result = answer_question("West since July 2025")

        monthly = next(row for row in result["evidence"]["E1"]["rows"]
                       if row["sale_month"] == "2025-07")
        summary = result["evidence"]["E2"]["rows"][0]
        assert writer.execute("SELECT unit_cogs FROM costs WHERE cost_month='2025-07'").fetchone() == (85.0,)
        assert monthly["margin_pct"] == summary["after_margin_pct"] == 35
        assert len(connections) == 1
        assert statements.count("SET TRANSACTION READ ONLY") == 1
@pytest.mark.parametrize("metadata", [
    [("hash-v1-512", "v" * 32 + "b" * 32)],
    [("hash-v1-512", "v" * 32 + "a" * 32),
     ("hash-v1-512", "v" * 32 + "b" * 32)],
    [("OTHER_MODEL", "v" * 32 + "a" * 32)],
])
def test_policy_search_rejects_corpus_replaced_after_setup(metadata, monkeypatch):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value.__enter__.return_value
    ensured = ("hash-v1-512", "v" * 32 + "a" * 32)
    setup = MagicMock(return_value=ensured)
    monkeypatch.setattr(tools, "ensure_policies", setup)
    monkeypatch.setattr(tools, "connect", lambda: connection)
    embedding = MagicMock(return_value=hash_embedding("West freight"))
    monkeypatch.setattr(tools, "embed", embedding)

    def execute(sql, *args, **kwargs):
        if sql.startswith("SELECT backend FROM"):
            cursor.fetchone.return_value = ("hash-v1-512",)
        elif sql.startswith("SELECT DISTINCT backend, source_hash"):
            cursor.fetchall.return_value = metadata
        elif "VECTOR_DISTANCE" in sql:
            cursor.fetchall.return_value = [
                (1, "west_q3_volume_push.md", "Unchanged reviewed policy", 0.1),
                (2, "superseding.md", "The earlier policy is revoked", 0.2),
            ]
        return cursor

    cursor.execute.side_effect = execute
    with pytest.raises(RuntimeError, match="Policy corpus"):
        tools.search_policies("West freight")

    setup.assert_called_once_with(include_fingerprint=True)
    assert cursor.execute.call_args_list[0].args[0] == "SET TRANSACTION READ ONLY"
    assert cursor.execute.call_args_list[1].args[0].startswith("SELECT DISTINCT backend, source_hash")
    embedding.assert_not_called()


def test_policy_search_keeps_fingerprint_and_results_in_one_snapshot(monkeypatch):
    ensured = ("hash-v1-512", "v" * 32 + "a" * 32)
    current = [(1, "original.md", "Original policy", 0.1)]
    snapshot = []
    events = []
    connection = MagicMock()
    connection.__enter__.return_value = connection
    cursor = connection.cursor.return_value.__enter__.return_value

    def execute(sql, *args, **kwargs):
        if sql == "SET TRANSACTION READ ONLY":
            events.append("snapshot")
            snapshot.extend(current)
        elif sql.startswith("SELECT DISTINCT backend, source_hash"):
            events.append("metadata")
            cursor.fetchall.return_value = [ensured]
        elif "VECTOR_DISTANCE" in sql:
            events.append("search")
            cursor.fetchall.return_value = list(snapshot)
        return cursor

    def embed_in_next_snapshot(*args):
        events.append("embed")
        current[:] = [(2, "superseding.md", "Revised policy", 0.1)]
        return hash_embedding("West freight")

    cursor.execute.side_effect = execute
    monkeypatch.setattr(tools, "ensure_policies", lambda **kwargs: ensured)
    monkeypatch.setattr(tools, "connect", lambda: connection)
    monkeypatch.setattr(tools, "embed", embed_in_next_snapshot)

    result = tools.search_policies("West freight")

    assert events == ["snapshot", "metadata", "embed", "search"]
    assert result["source_hash"] == ensured[1]
    assert [row["doc"] for row in result["rows"]] == ["original.md"]


def test_question_scope():
    assert parse_question("East since March 2025") == ("East", "2025-03")
    assert parse_question("West since 2025-07") == ("West", "2025-07")
    with pytest.raises(ValueError):
        parse_question("West and East")


@pytest.mark.parametrize("name, expected", [
    ("Eastern", "East"), ("eAsTeRn", "East"),
    ("Western", "West"), ("Southern", "South"), ("Central", "Central"),
])
def test_question_recognizes_region_adjectives(name, expected):
    assert parse_question(f"Why did the {name} region's margin fall since July 2025?") == (expected, "2025-07")


def test_question_counts_region_adjectives_as_the_same_region():
    assert parse_question("East and Eastern since July 2025") == ("East", "2025-07")


@pytest.mark.parametrize("regions", ["West and Eastern", "Western and East", "Southern and East"])
def test_question_rejects_multiple_regions_with_adjectives(regions):
    with pytest.raises(ValueError, match="one region"):
        parse_question(f"{regions} since July 2025")
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
        mock_run_sql(monkeypatch, execute)
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

        mock_run_sql(monkeypatch, run_sql)
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
        mock_run_sql(monkeypatch, execute)
        result = answer_question("West since July 2025")
        monthly = next(row for row in result["evidence"]["E1"]["rows"]
                       if row["region_name"] == region and row["sale_month"] == month)
        assert (monthly["revenue"], monthly["units"], monthly["margin_pct"]) == (90, 2, -44.44)
        assert result["dropped_count"] == 0


@pytest.mark.parametrize("column", ["discount_pct", "quantity", "unit_price", "net_revenue"])
@pytest.mark.parametrize("region_id", [1, 2])
@pytest.mark.parametrize("month", ["2025-06", "2025-07"])
@pytest.mark.parametrize("incomplete_when", ["both", "monthly", "summary"])
def test_missing_sales_inputs_reject_analysis(column, region_id, month, incomplete_when, monkeypatch):
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
        if column == "net_revenue":
            con.execute("UPDATE sales SET net_revenue=0 WHERE rowid=?", (sale_id,))

        def execute(sql, params=None):
            return {"sql": sql, "params": params or {}, "rows": [dict(row) for row in
                con.execute(tools.validate_select(sql), params or {})]}

        policies = MagicMock(return_value={"rows": []})
        mock_run_sql(monkeypatch, execute)
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

        valid_value = {"discount_pct": 0, "quantity": 1, "unit_price": 100.0, "net_revenue": 0}[column]
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

        mock_run_sql(monkeypatch, run_sql)
        error = RevenueDataQualityError if column == "net_revenue" else DiscountDataQualityError
        message = "Missing net revenue" if column == "net_revenue" else "Missing discount inputs"
        with pytest.raises(error, match=message):
            answer_question("West since July 2025")
        policies.assert_not_called()
        assert len(snapshots) == 2
        for index, snapshot in enumerate(snapshots):
            for row in snapshot["rows"]:
                if column == "net_revenue":
                    incomplete = incomplete_when == "both" or incomplete_when == (
                        "monthly" if index == 0 else "summary")
                    affected = row["region_name"] == region and (
                        "sale_month" not in row or row["sale_month"] == month)
                    assert row["missing_revenue_count"] == int(incomplete and affected)
                elif row["missing_discount_count"]:
                    if "sale_month" in row:
                        assert row["discount_pct"] is None
                    else:
                        assert row[f"{period}_discount_pct"] is None

        set_input(None)
        mock_run_sql(monkeypatch, execute)
        response = app.test_client().post("/", data={"question": "West since July 2025"})
        assert response.status_code == 503
        message = (b"Correct missing net revenue for sales, then retry." if column == "net_revenue"
                   else b"Correct missing discount, quantity, or unit price inputs, then retry.")
        assert message in response.data
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
        mock_run_sql(monkeypatch, run_sql)
        monkeypatch.setattr("agent.search_policies", policies)
        with pytest.raises(DataQualityError, match="Missing monthly costs"):
            answer_question("West since July 2025")
        policies.assert_not_called()
        response = app.test_client().post("/", data={"question": "West since July 2025"})
        assert response.status_code == 503
        assert b"Missing monthly costs or duplicate cost rows" in response.data
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
        mock_run_sql(monkeypatch, run_sql)
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
        mock_run_sql(monkeypatch, execute)
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

        mock_run_sql(monkeypatch, run_sql)
        with pytest.raises(DataQualityError, match="duplicate"):
            answer_question("West since July 2025")
        policies.assert_not_called()
        if duplicate_when == "monthly":
            insert_duplicate()
        mock_run_sql(monkeypatch, execute)
        response = app.test_client().post("/", data={"question": "West since July 2025"})
        assert response.status_code == 503
        assert b"Correct missing, incomplete, or duplicate monthly cost records, then retry." in response.data


@pytest.mark.parametrize("rows", [[], [{"region_name": "East"}]])
def test_missing_region_has_descriptive_validation_error(monkeypatch, rows):
    mock_run_sql(monkeypatch, lambda sql, params=None: {
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
        mock_run_sql(monkeypatch, run_sql)
        monkeypatch.setattr("agent.search_policies", policies)
        response = app.test_client().post("/", data={"question": f"West since {cutoff}"})

        assert response.status_code == (200 if valid else 400)
        if not valid:
            assert b"West sales are available from 2025-03 through 2025-08" in response.data
            assert b"cutoff must be after the first month and no later than the last month" in response.data
            policies.assert_not_called()


@pytest.mark.parametrize("before,after,direction,below_precision,displayed,rounding_note", [
    (25.001, 25.004, "rose", True, (25, 25, 0), False),
    (25.004, 25.001, "fell", True, (25, 25, 0), False),
    (25.001, 25.001, "was unchanged", False, (25, 25, 0), False),
    (25, 26, "rose", False, (25, 26, -1), False),
    (26, 25, "fell", False, (26, 25, 1), False),
    (25.004, 24.996, "fell", False, (25, 25, 0.01), True),
    (24.996, 25.004, "rose", False, (25, 25, -0.01), True),
    (25.006, 25.004, "fell", True, (25.01, 25, 0), True),
    (25.014, 24.996, "fell", False, (25.01, 25, 0.02), True),
    (25.28, 25.29, "rose", False, (25.28, 25.29, -0.01), False),
])
def test_margin_direction_uses_unrounded_sql(before, after, direction, below_precision, displayed,
                                              rounding_note, monkeypatch, policy_rows):
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

    assert tuple(rows[0][key] for key in ("before_margin_pct", "after_margin_pct", "margin_drop_pts")) == displayed
    mock_run_sql(monkeypatch, lambda sql, params=None: {
        "sql": sql, "params": params or {}, "rows": rows})
    result = answer_question("West since July 2025")
    finding = next(c for c in result["claims"] if c["kind"] == "finding")
    rendered = "".join(p.get("text", str(p.get("value", "")) + p.get("suffix", ""))
                       for p in finding["parts"])
    assert rendered.startswith(f"West gross margin {direction} from ")
    assert ("below display precision" in rendered) == below_precision
    assert ("The displayed margins and change are independently rounded from unrounded aggregates." in rendered) == rounding_note
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

        mock_run_sql(monkeypatch, run_sql)
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
    assert result["evidence"]["E2"]["rows"][parts[0]["row"]]["margin_direction"] is None
    assert supported(parts[0], result["evidence"])
    assert result["dropped_count"] == 0


@pytest.mark.parametrize("return_freight,after_freight,before,change", [
    (18.0, 5.0, -8.0, None),
    (10.0, 5.0, 0.0, None),
    (10.004, 5.0, 0.0, None),
    (9.996, 5.0, 0.0, 124900.0),
    (5.0, 8.0, 5.0, 60.0),
])
def test_freight_change_requires_positive_unrounded_baseline(
        return_freight, after_freight, before, change, monkeypatch):
    # Returns can make the weighted freight baseline nonpositive.
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE regions (region_id, region_name);
            INSERT INTO regions VALUES (1, 'West');
            CREATE TABLE sales (region_id, product_id, sale_month, net_revenue,
                                quantity, unit_price, discount_pct);
            CREATE TABLE costs (region_id, product_id, cost_month, unit_cogs, freight_per_unit);
            INSERT INTO sales VALUES (1, 1, '2025-06', 180.0, 2, 100.0, 10);
            INSERT INTO sales VALUES (1, 2, '2025-06', -90.0, -1, 100.0, 10);
            INSERT INTO sales VALUES (1, 1, '2025-07', 90.0, 1, 100.0, 10);
            INSERT INTO costs VALUES (1, 1, '2025-06', 60.0, 5.0);
        """)
        con.execute("INSERT INTO costs VALUES (1, 2, '2025-06', 60.0, ?)", (return_freight,))
        con.execute("INSERT INTO costs VALUES (1, 1, '2025-07', 60.0, ?)", (after_freight,))

        def run_sql(sql, params=None):
            return {"sql": sql, "params": params or {}, "rows": [dict(row) for row in
                con.execute(tools.validate_select(sql), params or {})]}

        mock_run_sql(monkeypatch, run_sql)
        monkeypatch.setattr("agent.search_policies", lambda _: {"rows": []})
        result = answer_question("West since July 2025")

    row = result["evidence"]["E2"]["rows"][0]
    assert row["before_freight_per_unit"] == before
    assert row["after_freight_per_unit"] == after_freight
    if change is None:
        assert row["freight_increase_pct"] is None
    else:
        assert row["freight_increase_pct"] == pytest.approx(change)
    finding = next(c for c in result["claims"] if c["kind"] == "finding"
                   and "Freight" in c["parts"][0].get("text", ""))
    rendered = "".join(p.get("text", str(p.get("value", "")) + p.get("suffix", ""))
                       for p in finding["parts"])
    assert ("change: unavailable" in rendered) == (change is None)
    cited = {p["column"] for p in finding["parts"] if "value" in p and supported(p, result["evidence"])}
    assert {"before_freight_per_unit", "after_freight_per_unit"} <= cited
    assert ("freight_increase_pct" in cited) == (change is not None)
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
    mock_run_sql(monkeypatch, lambda sql, params=None: {
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


@pytest.mark.parametrize("zero_period", ["before", "after", "both"])
def test_zero_revenue_preserves_available_metrics(zero_period, monkeypatch):
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE regions (region_id, region_name);
            INSERT INTO regions VALUES (1, 'West'), (2, 'East');
            CREATE TABLE sales (region_id, product_id, sale_month, net_revenue,
                                quantity, unit_price, discount_pct);
            CREATE TABLE costs (region_id, product_id, cost_month, unit_cogs, freight_per_unit);
        """)
        for region_id in (1, 2):
            for period, month, freight in (("before", "2025-06", 5), ("after", "2025-07", 8)):
                zero = (region_id == 1 and zero_period in (period, "both")) or (region_id == 2 and period == "after")
                con.execute("INSERT INTO sales VALUES (?, 1, ?, ?, 1, 100, ?)",
                            (region_id, month, 0 if zero else 90, 100 if zero else 10))
                con.execute("INSERT INTO costs VALUES (?, 1, ?, 60, ?)", (region_id, month, freight))

        def run_sql(sql, params=None):
            return {"sql": sql, "params": params or {}, "rows": [dict(row) for row in
                con.execute(tools.validate_select(sql), params or {})]}

        mock_run_sql(monkeypatch, run_sql)
        policies = [{"id": i, "doc": path.name, "chunk": path.read_text(encoding="utf-8").strip()}
                    for i, path in enumerate(sorted(Path(__file__).with_name("docs").glob("*.md")), 1)]
        monkeypatch.setattr("agent.search_policies", lambda _: {
            "sql": "SELECT policy evidence", "params": {}, "rows": policies,
            "source_hash": policy_source_hash(Path(__file__).with_name("docs"))})
        question = "West since July 2025"
        result = answer_question(question)
        response = app.test_client().post("/", data={"question": question})
        assert response.status_code == 200

        rows = {row["region_name"]: row for row in result["evidence"]["E2"]["rows"]}
        assert rows["West"]["margin_direction"] is None
        assert rows["East"]["margin_direction"] is None
        findings = [claim for claim in result["claims"] if claim["kind"] == "finding"]
        render = lambda claim: "".join(p.get("text", str(p.get("value", "")) + p.get("suffix", ""))
                                       for p in claim["parts"]).lower()
        margin = next(claim for claim in findings if "margin" in render(claim))
        assert "unavailable" in render(margin) and "revenue" in render(margin)
        assert "missing" not in render(margin)
        assert "unchanged" not in render(margin)
        cited = {part["column"] for part in margin["parts"] if "value" in part}
        for period in ("before", "after"):
            if zero_period in (period, "both"):
                assert rows["West"][f"{period}_margin_pct"] is None
                assert any(part.get("column") == f"{period}_revenue" and part.get("value") == 0
                           and supported(part, result["evidence"]) for part in margin["parts"])
            else:
                assert f"{period}_margin_pct" in cited
        for metric, values in (("discount", (100 if zero_period in ("before", "both") else 10,
                                              100 if zero_period in ("after", "both") else 10)),
                               ("freight", (5, 8))):
            finding = next(claim for claim in findings if metric in render(claim))
            columns = ("discount_pct" if metric == "discount" else "freight_per_unit")
            for period, value in zip(("before", "after"), values):
                assert rows["West"][f"{period}_{columns}"] == value
                assert any(part.get("column") == f"{period}_{columns}" and supported(part, result["evidence"])
                           for part in finding["parts"])
        comparison = next(claim for claim in result["claims"] if claim["kind"] == "comparison")
        assert "unavailable" in render(comparison)
        assert any(part.get("column") == "before_margin_pct" and supported(part, result["evidence"])
                   for part in comparison["parts"])
        reason = next(claim for claim in result["claims"] if claim["kind"] == "reason")
        assert reason["policy_ids"]
        assert not any(direction in render(reason) for direction in ("unchanged", "decline", "fell", "rose"))
        assert result["dropped_count"] == 0

        # A genuinely absent period must still be rejected, even when the other has zero revenue.
        for missing_month in ("2025-06", "2025-07"):
            con.execute("SAVEPOINT missing_period")
            con.execute("DELETE FROM sales WHERE region_id=1 AND sale_month=?", (missing_month,))
            response = app.test_client().post("/", data={"question": question})
            assert response.status_code == 400
            assert b"sales both before and after" in response.data
            con.execute("ROLLBACK TO missing_period")
            con.execute("RELEASE missing_period")


@pytest.mark.parametrize("missing_period", ["before", "after"])
def test_missing_revenue_is_not_reported_as_zero(missing_period, monkeypatch):
    with sqlite3.connect(":memory:") as con:
        con.row_factory = sqlite3.Row
        con.executescript("""
            CREATE TABLE regions (region_id, region_name);
            INSERT INTO regions VALUES (1, 'West');
            CREATE TABLE sales (region_id, product_id, sale_month, net_revenue,
                                quantity, unit_price, discount_pct);
            CREATE TABLE costs (region_id, product_id, cost_month, unit_cogs, freight_per_unit);
        """)
        for period, month, freight in (("before", "2025-06", 5), ("after", "2025-07", 8)):
            if period == missing_period:
                # The partial sum is zero, but a missing value means total revenue is unknown.
                con.executemany("INSERT INTO sales VALUES (1, 1, ?, ?, 1, 100, ?)",
                                [(month, None, 10), (month, 0, 100)])
            else:
                con.execute("INSERT INTO sales VALUES (1, 1, ?, 90, 1, 100, 10)", (month,))
            con.execute("INSERT INTO costs VALUES (1, 1, ?, 60, ?)", (month, freight))

        snapshots = []

        def run_sql(sql, params=None):
            result = {"sql": sql, "params": params or {}, "rows": [dict(row) for row in
                con.execute(tools.validate_select(sql), params or {})]}
            snapshots.append(result)
            return result

        policies = MagicMock(return_value={"rows": []})
        mock_run_sql(monkeypatch, run_sql)
        monkeypatch.setattr("agent.search_policies", policies)
        with pytest.raises(RevenueDataQualityError, match="Missing net revenue"):
            answer_question("West since July 2025")
        policies.assert_not_called()

        month = "2025-06" if missing_period == "before" else "2025-07"
        monthly = next(row for row in snapshots[0]["rows"] if row["sale_month"] == month)
        row = snapshots[1]["rows"][0]
        assert monthly["revenue"] == 0 and monthly["missing_revenue_count"] == 1
        assert monthly["margin_pct"] is None
        assert row["missing_revenue_count"] == 1
        assert row[f"{missing_period}_revenue"] is None
        assert row[f"{missing_period}_margin_pct"] is None
        assert row[f"{missing_period}_discount_pct"] == 55
        assert row[f"{missing_period}_freight_per_unit"] == (5 if missing_period == "before" else 8)

        # Replacing the missing value with an actual zero permits the analysis.
        con.execute("UPDATE sales SET net_revenue=0 WHERE net_revenue IS NULL")
        result = answer_question("West since July 2025")
        row = result["evidence"]["E2"]["rows"][0]
        assert row[f"{missing_period}_revenue"] == 0
        margin = next(c for c in result["claims"] if c["kind"] == "finding"
                      and "margin" in c["parts"][0].get("text", "").lower())
        assert any(part.get("column") == f"{missing_period}_revenue" and part.get("value") == 0
                   and supported(part, result["evidence"]) for part in margin["parts"])
        assert result["dropped_count"] == 0


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

        mock_run_sql(monkeypatch, run_sql)
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

        mock_run_sql(monkeypatch, run_sql)
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
    mock_run_sql(monkeypatch, lambda sql, params=None: {
        "sql": sql, "params": params or {}, "rows": [row]})
    monkeypatch.setattr("agent.search_policies", lambda _: {
        "sql": "SELECT policy evidence", "params": {}, "rows": policies,
        "source_hash": policy_source_hash(Path(__file__).with_name("docs"))})
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


def test_fixed_claims_require_searched_corpus_to_match_reviewed_files(policy_rows, monkeypatch):
    # The files were restored after setup, while the searched snapshot still contains
    # a superseding policy alongside unchanged, individually reviewed chunks.
    superseding = {"id": 99, "doc": "superseding.md", "chunk": "The earlier policy is revoked."}
    monkeypatch.setattr("agent.search_policies", lambda _: {
        "sql": "SELECT policy evidence", "params": {}, "rows": [*policy_rows, superseding],
        "source_hash": "v" * 32 + "b" * 32})

    result = answer_question("West since July 2025")

    assert not any(claim["kind"] in {"reason", "action"} for claim in result["claims"])


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


@pytest.mark.parametrize("region,cutoff", [("West", "August 2025"), ("East", "July 2025")])
@pytest.mark.parametrize("margin_available", [True, False])
def test_nondefault_comparison_does_not_make_uncited_policy_claims(
        policy_rows, tmp_path, monkeypatch, region, cutoff, margin_available):
    import agent

    summary = dict(agent.run_sql_many([("SELECT ...", None)])[0]["rows"][0], region_name=region)
    if not margin_available:
        summary.update(after_margin_pct=None, after_revenue=0, margin_direction=None, margin_drop_pts=None)
    mock_run_sql(monkeypatch, lambda sql, params=None: {
        "sql": sql, "params": params or {}, "rows": [summary]})
    for policy in policy_rows:
        if policy["doc"] == "west_q3_volume_push.md":
            policy["chunk"] += f"\n\nThe freight contract caused {region} margin to fall since {cutoff}."
        (tmp_path / policy["doc"]).write_text(policy["chunk"], encoding="utf-8")
    monkeypatch.setattr("agent.DOCS", tmp_path)

    result = answer_question(f"Why did {region} margin fall since {cutoff}?")

    reason = next(claim for claim in result["claims"] if claim["kind"] == "reason")
    assert not reason["policy_ids"]
    assert "no verified policy explanation is available" in reason["parts"][0]["text"].lower()
    assert "do not establish a cause" not in reason["parts"][0]["text"].lower()


@pytest.mark.parametrize("revision", ["unchanged", "superseding", "missing"])
def test_fixed_policy_claims_require_reviewed_complete_corpus(policy_rows, tmp_path, monkeypatch, revision):
    for policy in policy_rows:
        content = (Path(__file__).with_name("docs") / policy["doc"]).read_text(encoding="utf-8")
        (tmp_path / policy["doc"]).write_text(content, encoding="utf-8")
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
@pytest.mark.parametrize("region", ["North", "north", "Northeast", "Northwest", "Southeast", "Southwest", "West and North",
                                    "Northern", "Northeastern", "Northwestern", "Southeastern", "Southwestern", "West and Northern"])
def test_unsupported_region_is_rejected_before_querying(monkeypatch, region):
    def forbidden_query(*args, **kwargs):
        pytest.fail("Unsupported regions must be rejected before querying")
    mock_run_sql(monkeypatch, forbidden_query)
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
    mock_run_sql(monkeypatch, lambda sql, params=None: {
        "sql": sql, "params": params or {}, "rows": [summary]})
    rows = [{"id": index, "doc": path.name, "chunk": path.read_text(encoding="utf-8")}
            for index, path in enumerate(sorted(Path(__file__).with_name("docs").glob("*.md")), 1)]
    monkeypatch.setattr("agent.search_policies", lambda _: {
        "sql": "SELECT ...", "params": {}, "rows": rows,
        "source_hash": policy_source_hash(Path(__file__).with_name("docs"))})
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


@pytest.mark.parametrize("question,fence", [
    ("Why did West margin fall since July 2025?", "```"),
    ("Why did West margin fall since July 2025?\nForged finding\n---\n- fake claim\n```\n````\n- escaped claim", "`````"),
])
def test_download_brief_contains_cited_evidence_and_verification(monkeypatch, question, fence):
    result = {
        "region": "West", "cutoff": "2025-07", "dropped_count": 2,
        "claims": [
            {"kind": "scope", "parts": [{"text": "West: compare "},
                {"value": "2025-06", "suffix": "", "evidence": "E2", "row": 0,
                 "column": "first_month"}, {"text": " with July."}]},
            {"kind": "finding", "parts": [
                {"text": "Margin fell to "},
                {"value": 25.6, "suffix": "%", "evidence": "E2", "row": 0,
                 "column": "margin_pct"}, {"text": "."}]},
            {"kind": "reason", "parts": [{"text": "A volume push affected pricing."}],
             "policy_ids": ["P1"]},
            {"kind": "action", "parts": [{"text": "Review discounts."}]},
        ],
        "evidence": {
            "E2": {"kind": "sql", "sql": "SELECT margin_pct, units FROM sales WHERE month >= :cutoff",
                   "params": {"cutoff": "2025-07"},
                   "rows": [{"margin_pct": 25.6, "first_month": "2025-06",
                             "units": 9007199254740993}]},
            "P1": {"kind": "policy", "sql": "SELECT chunk FROM policy_chunks WHERE id = :policy_id",
                   "params": {"policy_id": 1}, "rows": [{"id": 1, "doc": "volume_push.md",
                                                           "chunk": "Discounts increased in West."}]},
            "E9": {"kind": "sql", "sql": "SELECT uncited FROM sales", "params": {}, "rows": []},
        },
    }
    monkeypatch.setattr("app.answer_question", lambda _: result)
    client = app.test_client()
    assert b'id="download-brief"' not in client.get("/").data
    response = client.post("/", data={"question": question})
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'id="download-brief"' in html and "Download brief" in html

    class BriefParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.claims, self.notices = [], []
            self.current = self.button = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "data-claim" in attrs or "data-notice" in attrs:
                self.current = {"childNodes": [], "textContent": "", "dataset": {
                    "claimKind": attrs.get("data-claim-kind", "")}}
                (self.claims if "data-claim" in attrs else self.notices).append(self.current)
            elif self.current is not None and tag == "button":
                self.button = {"nodeType": 1, "textContent": "", "dataset": {
                    key[5:]: value for key, value in attrs.items() if key.startswith("data-")}}
                self.current["childNodes"].append(self.button)

        def handle_data(self, data):
            if self.current is not None:
                self.current["textContent"] += data
                if self.button is not None:
                    self.button["textContent"] += data
                else:
                    self.current["childNodes"].append({"nodeType": 3, "textContent": data})

        def handle_endtag(self, tag):
            if tag == "button":
                self.button = None
            elif tag == "p":
                self.current = None

    page = BriefParser()
    page.feed(html)
    assert len(page.claims) == len(result["claims"])
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    dom = json.dumps({"claims": page.claims, "notices": page.notices})
    check = subprocess.run(["node", "-e", """
        const assert = require('node:assert/strict');
        const page = """ + dom + """;
        const handlers = {};
        let blob, clicked = false;
        const anchor = {click() { clicked = true; }, remove() {}};
        const element = {addEventListener() {}};
        const button = {addEventListener(event, handler) { handlers[event] = handler; }};
        global.Node = {TEXT_NODE: 3, ELEMENT_NODE: 1};
        global.document = {
            getElementById: id => id === 'download-brief' ? button :
                id === 'question' ? {value: 'Edited after the analysis'} : element,
            querySelectorAll: selector => selector === '[data-claim]' ? page.claims :
                selector === '[data-notice]' ? page.notices : [],
            createElement: tag => { assert.equal(tag, 'a'); return anchor; },
            body: {appendChild() {}, append() {}},
        };
        URL.createObjectURL = value => { blob = value; return 'blob:brief'; };
        URL.revokeObjectURL = () => {};
    """ + script + """
        assert.equal(typeof handlers.click, 'function');
        handlers.click();
        assert.ok(clicked && blob);
        assert.match(anchor.download, /\\.md$/);
        assert.match(blob.type, /^text\\/(markdown|plain)/);
        blob.text().then(text => process.stdout.write(text));
    """], capture_output=True, text=True, encoding="utf-8")
    assert check.returncode == 0, check.stderr
    markdown = check.stdout
    assert f"## Question\n\n{fence}\n{question}\n{fence}\n\n## Scope" in markdown
    assert "Edited after the analysis" not in markdown
    scope = next(line for line in markdown.splitlines() if "West: compare" in line)
    assert "2025-06" in scope and "E2" in scope and "row 1" in scope
    assert "first_month" in scope.replace("\\_", "_")
    finding = next(line for line in markdown.splitlines() if "Margin fell to" in line)
    assert "25.6%" in finding and "E2" in finding and "row 1" in finding
    assert "margin_pct" in finding.replace("\\_", "_")
    reason = next(line for line in markdown.splitlines() if "A volume push affected pricing." in line)
    assert "P1" in reason
    assert "Review discounts." in markdown
    assert "2 claims were omitted because they failed verification." in markdown
    assert "Numbers in retained claims verified against cited SQL cells." in markdown
    assert "Policy-backed recommendations are unavailable for this brief." in markdown
    assert "SELECT margin_pct, units FROM sales WHERE month >= :cutoff" in markdown
    assert '"cutoff": "2025-07"' in markdown and '"margin_pct": 25.6' in markdown
    assert "9007199254740993" in markdown
    assert "SELECT chunk FROM policy_chunks WHERE id = :policy_id" in markdown
    assert '"policy_id": 1' in markdown
    assert "volume_push.md" in markdown.replace("\\_", "_") and "Discounts increased in West." in markdown
    assert "SELECT uncited FROM sales" not in markdown
@pytest.mark.parametrize("region", ["West", "East"])
def test_monthly_trend_links_selected_region_to_original_evidence_rows(monkeypatch, region):
    rows = [
        {"region_name": "East", "sale_month": "2025-06", "revenue": 1450.25, "units": 100,
         "margin_pct": 45, "discount_pct": 2.5, "freight_per_unit": 4.25},
        {"region_name": "West", "sale_month": "2025-06", "revenue": 0, "units": 0,
         "margin_pct": 38.9, "discount_pct": 0, "freight_per_unit": 5.5},
        {"region_name": "East", "sale_month": "2025-07", "margin_pct": 47,
         "revenue": None, "discount_pct": None, "freight_per_unit": 4.75},
        {"region_name": "West", "sale_month": "2025-07", "margin_pct": 25.6,
         "units": None, "discount_pct": 18, "freight_per_unit": None},
    ]
    monkeypatch.setattr("app.answer_question", lambda _: {
        "region": region, "claims": [],
        "evidence": {"E1": {"kind": "sql", "sql": "SELECT ...", "params": {}, "rows": rows}},
    })

    class TrendParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.in_table = self.in_body = self.in_caption = False
            self.caption = ""
            self.rows = []
            self.cell = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "table" and attrs.get("id") == "monthly-trend":
                self.in_table = True
            elif self.in_table and tag == "caption":
                self.in_caption = True
            elif self.in_table and tag == "tbody":
                self.in_body = True
            elif self.in_body and tag == "tr":
                self.rows.append([])
            elif self.in_body and tag in ("td", "th"):
                self.cell = {"text": "", "button": None}
                self.rows[-1].append(self.cell)
            elif self.cell is not None and tag == "button":
                self.cell["button"] = attrs

        def handle_data(self, data):
            if self.in_caption:
                self.caption += data
            if self.cell is not None:
                self.cell["text"] += data

        def handle_endtag(self, tag):
            if tag == "caption":
                self.in_caption = False
            elif self.in_body and tag in ("td", "th"):
                self.cell = None
            elif tag == "tbody":
                self.in_body = False
            elif tag == "table":
                self.in_table = False

    response = app.test_client().post("/", data={"question": region})
    assert response.status_code == 200
    parser = TrendParser()
    parser.feed(response.get_data(as_text=True))
    assert region in parser.caption
    selected = [(index, row) for index, row in enumerate(rows) if row["region_name"] == region]
    assert len(parser.rows) == len(selected)
    columns = ("sale_month", "revenue", "units", "margin_pct", "discount_pct", "freight_per_unit")
    for cells, (index, row) in zip(parser.rows, selected):
        assert len(cells) == len(columns)
        for cell, column in zip(cells, columns):
            value = row.get(column)
            if value is None:
                assert cell["text"].strip().lower() == "unavailable"
                assert cell["button"] is None
            else:
                expected = str(value)
                if column in ("margin_pct", "discount_pct"):
                    expected += "%"
                elif column in ("revenue", "freight_per_unit"):
                    expected = "$" + expected
                assert cell["text"].strip() == expected
                button = cell["button"]
                assert button["data-evidence"] == "E1"
                assert button["data-row"] == str(index)
                assert button["data-column"] == column
                assert region in button["aria-label"] and row["sale_month"] in button["aria-label"]
                if column in ("revenue", "units"):
                    assert column in button["aria-label"] and expected in button["aria-label"]


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
        {"region_name": "West", "sale_month": "2025-06", "revenue": 1200.5, "units": 75, "margin_pct": 38.9,
         "discount_pct": 0, "freight_per_unit": 5.4},
        {"region_name": "East", "sale_month": "2025-07", "margin_pct": 71,
         "discount_pct": 2, "freight_per_unit": 3},
        {"region_name": "West", "sale_month": "2025-07", "revenue": 0, "units": None, "margin_pct": None,
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
    assert labels == ["month", "revenue ($)", "units", "gross margin (%)", "discount (%)", "freight per unit ($)"]
    assert len(body) == 2
    columns = ("sale_month", "revenue", "units", "margin_pct", "discount_pct", "freight_per_unit")
    for table_row, evidence_index in zip(body, (1, 3)):
        assert len(table_row) == len(columns)
        for cell, column, label in zip(table_row, columns, labels):
            value = rows[evidence_index][column]
            displayed = cell["text"].strip()
            if value is None:
                assert displayed == "Unavailable"
                assert not cell["buttons"]
                continue
            assert len(cell["buttons"]) == 1
            button = cell["buttons"][0]
            assert button["data-evidence"] == "E1"
            assert button["data-row"] == str(evidence_index)
            assert button["data-column"] == column
            if column == "sale_month":
                assert displayed == value
            elif column == "units":
                assert displayed == str(value)
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

    mock_run_sql(monkeypatch, forbidden_query)
    response = app.test_client().post("/", data=data)
    assert response.status_code == 400


def test_ui_boundaries_contrast_with_adjacent_backgrounds():
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

    page = declaration(":root", "background")
    for boundary, surfaces in (
        (declaration(":focus-visible", "outline").split()[-1],
         (page, declaration(".card", "background"))),
        (declaration("input", "border").split()[-1],
         (page, declaration("input", "background"))),
    ):
        for surface in surfaces:
            dark, light = sorted((luminance(boundary), luminance(surface)))
            contrast = (light + 0.05) / (dark + 0.05)
            assert contrast >= 3, f"Boundary {boundary} has {contrast:.2f}:1 contrast against {surface}"


@pytest.mark.parametrize("has_result", [False, True])
def test_ui_allows_retry_after_stopped_submission(monkeypatch, has_result):
    monkeypatch.setattr("app.answer_question", lambda _: {
        "region": "West", "claims": [], "dropped_count": 0, "evidence": {},
    })
    client = app.test_client()
    response = client.post("/", data={"question": "West"}) if has_result else client.get("/")
    html = response.get_data(as_text=True)

    class SubmitButtonParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.button = None
            self.label = ""
            self.in_button = False

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "button" and attrs.get("class") == "submit":
                self.button = attrs
                self.in_button = True

        def handle_data(self, data):
            if self.in_button:
                self.label += data

        def handle_endtag(self, tag):
            if tag == "button":
                self.in_button = False

    parser = SubmitButtonParser()
    parser.feed(html)
    assert parser.button is not None
    assert parser.button.get("type") == "submit"
    assert "disabled" not in parser.button
    assert parser.label == "Analyze question"

    scripts = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.DOTALL))
    check = subprocess.run(["node", "-e", """
        const assert = require('node:assert/strict');
        const handlers = {};
        const button = {disabled: false, textContent: 'Analyze question'};
        const form = {addEventListener: (event, handler) => (handlers[event] ??= []).push(handler)};
        const other = {addEventListener() {}};
        global.document = {
            getElementById: id => id === 'question-form' ? form : other,
            querySelector: () => button,
            querySelectorAll: () => [],
        };
        global.window = {addEventListener() {}};
    """ + scripts + """
        // A stopped navigation produces no pageshow; the same form must be retryable.
        for (let attempt = 0; attempt < 2; attempt++) {
            for (const handler of handlers.submit || []) handler({preventDefault() {}});
            assert.equal(button.disabled, false);
            assert.equal(button.textContent, 'Analyze question');
        }
    """], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


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


def test_evidence_dialog_explains_empty_rows(monkeypatch):
    result = {
        "region": "West", "claims": [], "evidence": {
            "E1": {"kind": "sql", "sql": "SELECT ...", "params": {}, "rows": []},
            "E2": {"kind": "sql", "sql": "SELECT ...", "params": {},
                   "rows": [{"region": "West"}]},
        },
    }
    monkeypatch.setattr("app.answer_question", lambda _: result)
    response = app.test_client().post("/", data={"question": "West"})
    assert response.status_code == 200
    script = response.get_data(as_text=True).split("<script>", 1)[1].split("</script>", 1)[0]
    check = subprocess.run(["node", "-e", """
        const assert = require('node:assert/strict');
        class Element {
            constructor() { this.children = []; this.value = ''; }
            set textContent(value) { this.value = String(value); this.children = []; }
            get textContent() { return this.value + this.children.map(child => typeof child === 'string' ? child : child.textContent).join(''); }
            append(...children) { this.children.push(...children); }
            replaceChildren(...children) { this.value = ''; this.children = children; }
            addEventListener() {}
            showModal() {}
            querySelector() { return null; }
            createTHead() { const child = new Element(); this.append(child); return child; }
            createTBody() { const child = new Element(); this.append(child); return child; }
            insertRow() { const child = new Element(); this.append(child); return child; }
            insertCell() { const child = new Element(); this.append(child); return child; }
        }
        const elements = {};
        const buttons = ['E1', 'E2'].map(id => ({
            dataset: {evidence: id},
            addEventListener: (_, handler) => { buttonsById[id] = handler; },
        }));
        const buttonsById = {};
        global.document = {
            getElementById: id => elements[id] ||= new Element(),
            querySelectorAll: () => buttons,
            createElement: () => new Element(),
        };
    """ + script + """
        buttonsById.E2();
        const rows = elements['evidence-rows'];
        assert.match(rows.textContent, /West/);
        buttonsById.E1();
        assert.equal(rows.textContent.trim(), 'No rows returned');
        buttonsById.E2();
        assert.match(rows.textContent, /West/);
        assert.doesNotMatch(rows.textContent, /No rows returned/);
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


def test_numeric_buttons_describe_cited_metric_period_and_units(monkeypatch):
    rows = [
        {"region_name": "East", "first_month": "2025-02", "before_last_month": "2025-05",
         "after_first_month": "2025-08", "last_month": "2025-10", "before_margin_pct": 5},
        {"region_name": 'West & "Co"', "first_month": "2025-01",
         "before_last_month": "2025-06", "after_first_month": "2025-07",
         "last_month": "2025-09", "before_margin_pct": 5,
         "after_margin_pct": 5, "before_discount_pct": 5,
         "after_discount_pct": 5, "before_freight_per_unit": 5,
         "after_freight_per_unit": 5, "margin_drop_pts": 0,
         "freight_increase_pct": 0},
    ]
    citations = [
        (0, "before_margin_pct", "%", ("before", "margin", "%")),
        (1, "before_margin_pct", "%", ("before", "margin", "%")),
        (1, "after_margin_pct", "%", ("after", "margin", "%")),
        (1, "before_discount_pct", "%", ("before", "discount", "%")),
        (1, "after_discount_pct", "%", ("after", "discount", "%")),
        (1, "before_freight_per_unit", "", ("before", "freight", "per unit")),
        (1, "after_freight_per_unit", "", ("after", "freight", "per unit")),
        (1, "margin_drop_pts", " percentage points", ("margin", "drop", "percentage points")),
        (1, "freight_increase_pct", "%", ("freight", "%")),
        (1, "first_month", "", ("before", "month")),
        (1, "before_last_month", "", ("before", "month")),
        (1, "after_first_month", "", ("after", "month")),
        (1, "last_month", "", ("after", "month")),
    ]
    result = {
        "region": rows[1]["region_name"],
        "claims": [{"kind": "finding", "parts": [
            {"value": rows[row][column], "suffix": suffix,
             "evidence": "E2", "row": row, "column": column}
            for row, column, suffix, _ in citations]}],
        "evidence": {"E2": {"kind": "sql", "sql": "SELECT ...", "params": {}, "rows": rows}},
    }
    monkeypatch.setattr("app.answer_question", lambda _: result)

    class ButtonParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.buttons = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "button" and attrs.get("class") == "number":
                self.buttons.append(attrs)

    response = app.test_client().post("/", data={"question": "West"})
    assert response.status_code == 200
    parser = ButtonParser()
    parser.feed(response.get_data(as_text=True))
    assert len(parser.buttons) == len(citations)
    for button, (row, column, suffix, descriptors) in zip(parser.buttons, citations):
        label = button.get("aria-label", "").lower()
        assert rows[row]["region_name"].lower() in label
        assert str(rows[row][column]).lower() in label
        for descriptor in descriptors:
            assert descriptor in label
        if "freight_per_unit" in column:
            assert "$" in label or "dollar" in label
        if column in ("before_margin_pct", "before_discount_pct", "before_freight_per_unit"):
            assert rows[row]["first_month"] in label and rows[row]["before_last_month"] in label
        elif column in ("after_margin_pct", "after_discount_pct", "after_freight_per_unit"):
            assert rows[row]["after_first_month"] in label and rows[row]["last_month"] in label
        elif column in ("margin_drop_pts", "freight_increase_pct"):
            for endpoint in ("first_month", "before_last_month", "after_first_month", "last_month"):
                assert rows[row][endpoint] in label
        elif column in ("first_month", "after_first_month"):
            assert "first" in label or "start" in label
        else:
            assert "last" in label or "end" in label
    assert parser.buttons[1]["aria-label"] != parser.buttons[2]["aria-label"]
    assert parser.buttons[5]["aria-label"] != parser.buttons[6]["aria-label"]
def test_evidence_dialog_focuses_cited_cell(monkeypatch):
    result = {
        "region": "West", "claims": [],
        "evidence": {"E1": {"kind": "sql", "sql": "SELECT ...", "params": {},
                            "rows": [{"margin": 38}, {"margin": 25}]}},
    }
    monkeypatch.setattr("app.answer_question", lambda _: result)
    html = app.test_client().post("/", data={"question": "West"}).get_data(as_text=True)
    script = html.split("<script>", 1)[1].split("</script>", 1)[0]
    check = subprocess.run(["node", "-e", """
        const assert = require('node:assert/strict');
        const close = {addEventListener() {}};
        const buttons = [
            {dataset: {evidence: 'E1', row: '1', column: 'margin'}},
            {dataset: {evidence: 'E1'}},
        ];
        buttons.forEach(button => button.addEventListener = (_, handler) => button.click = handler);
        let cells = [];
        let visibleCell = null;
        const modalStub = {
            showModal() { document.activeElement = close; visibleCell = null; },
            querySelector: () => cells.find(cell => cell.className === 'selected'),
        };
        const elements = {
            'evidence-dialog': modalStub, 'close-dialog': close,
            'download-brief': {addEventListener() {}},
            'evidence-description': {}, 'evidence-title': {},
            'evidence-sql': {}, 'evidence-params': {},
            'evidence-rows': {replaceChildren() {}},
        };
        global.document = {
            getElementById: id => elements[id],
            querySelectorAll: () => buttons,
            createElement(tag) {
                if (tag !== 'table') return {};
                cells = [];
                return {
                    createTHead: () => ({insertRow: () => ({append() {}})}),
                    createTBody: () => ({insertRow: () => ({insertCell() {
                        const cell = {
                            attributes: {}, append() {},
                            setAttribute(name, value) { this.attributes[name] = value; },
                            scrollIntoView() { visibleCell = this; },
                            focus(options = {}) {
                                if (this.attributes.tabindex === '-1') {
                                    document.activeElement = this;
                                    if (!options.preventScroll) visibleCell = this;
                                }
                            },
                        };
                        cells.push(cell);
                        return cell;
                    }})}),
                };
            },
        };
    """ + script + """
        buttons[0].click();
        assert.equal(document.activeElement, cells[1]);
        assert.equal(visibleCell, cells[1]);
        assert.equal(cells[1].attributes.tabindex, '-1');
        assert.equal(cells[0].attributes.tabindex, undefined);

        buttons[1].click();
        assert.equal(document.activeElement, close);
        assert.equal(visibleCell, null);
    """], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


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


def test_successful_analysis_focuses_brief_heading(monkeypatch):
    monkeypatch.setattr("app.answer_question", lambda _: {
        "region": "West", "claims": [], "dropped_count": 0, "evidence": {},
    })

    class PageParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.focused = []
            self.headings = []
            self.current_heading = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "autofocus" in attrs:
                self.focused.append((tag, attrs))
            if tag == "h2":
                self.current_heading = [attrs, ""]
                self.headings.append(self.current_heading)

        def handle_data(self, data):
            if self.current_heading is not None:
                self.current_heading[1] += data

        def handle_endtag(self, tag):
            if tag == "h2":
                self.current_heading = None

    def parse(response):
        page = PageParser()
        page.feed(response.get_data(as_text=True))
        return page

    client = app.test_client()
    assert not parse(client.get("/")).focused
    response = client.post("/", data={"question": "West"})
    assert response.status_code == 200
    page = parse(response)
    heading, = [attrs for attrs, text in page.headings
                if text == "What changed, and what to do"]
    assert heading.get("tabindex") == "-1"
    assert page.focused == [("h2", heading)]


def test_question_validation_errors_are_associated_and_focused(monkeypatch):
    class FormParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.question = None
            self.hint = None
            self.hint_text = ""
            self.in_hint = False
            self.alert = None
            self.alert_text = ""
            self.in_alert = False

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "input" and attrs.get("id") == "question":
                self.question = attrs
            if tag == "p" and attrs.get("class") == "hint" and self.hint is None:
                self.hint = attrs
                self.in_hint = True
            if tag == "p" and attrs.get("role") == "alert":
                self.alert = attrs
                self.in_alert = True

        def handle_data(self, data):
            if self.in_hint:
                self.hint_text += data
            if self.in_alert:
                self.alert_text += data

        def handle_endtag(self, tag):
            if tag == "p":
                self.in_hint = False
                self.in_alert = False

    def parse(response):
        parser = FormParser()
        parser.feed(response.get_data(as_text=True))
        assert parser.hint.get("id")
        assert parser.hint["id"] in parser.question.get("aria-describedby", "").split()
        assert "Fixed analysis of margin, discounts and freight" in parser.hint_text
        assert "defaults are West and July 2025" in parser.hint_text
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
        assert page.alert.get("id")
        assert set(page.question["aria-describedby"].split()) == {page.hint["id"], page.alert["id"]}
        assert "autofocus" in page.question
        assert "autofocus" not in page.alert
        assert message in page.alert_text

    def failure(_):
        raise RuntimeError("unavailable")

    monkeypatch.setattr("app.answer_question", failure)
    for response, status in [(client.get("/"), 200),
                             (client.post("/", data={"question": "West"}), 503)]:
        assert response.status_code == status
        page = parse(response)
        assert "aria-invalid" not in page.question
        assert page.question["aria-describedby"].split() == [page.hint["id"]]
        assert "autofocus" not in page.question
        if status == 503:
            assert page.alert.get("tabindex") == "-1"
            assert "autofocus" in page.alert
            assert "Analysis unavailable" in page.alert_text
        else:
            assert page.alert is None


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


def test_ui_reports_monthly_evidence_row_limit(monkeypatch):
    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.description = [("REGION_NAME",)]
    cursor.fetchmany.return_value = [("West",)] * 1001
    monkeypatch.setattr("tools.connect", lambda: connection)
    policies = MagicMock()
    monkeypatch.setattr("agent.search_policies", policies)

    response = app.test_client().post("/", data={"question": "West"})

    assert response.status_code == 503
    message = response.get_data(as_text=True).lower()
    assert "evidence query returned more than 1,000 rows" in message
    assert "filter the sales data or aggregate the evidence query into fewer rows" in message
    assert "oracle connection and policy setup" not in message
    cursor.fetchmany.assert_called_once_with(1001)
    policies.assert_not_called()


@pytest.mark.parametrize("column, message", [
    ("missing_cost_count", b"Missing monthly costs or duplicate cost rows"),
    ("missing_discount_count", b"Missing discount inputs"),
])
@pytest.mark.parametrize("incomplete_when", ["monthly", "summary"])
def test_ui_reports_data_quality_errors(column, message, incomplete_when, monkeypatch):
    def run_sql(sql, params=None):
        row = {"region_name": "West", "missing_cost_count": 0, "missing_discount_count": 0,
               "missing_revenue_count": 0}
        if (params is None) == (incomplete_when == "monthly"):
            row[column] = 1
        return {"sql": sql, "params": params or {}, "rows": [row]}

    policies = MagicMock()
    mock_run_sql(monkeypatch, run_sql)
    monkeypatch.setattr("agent.search_policies", policies)

    response = app.test_client().post("/", data={"question": "West"})

    assert response.status_code == 503
    assert message in response.data
    assert b"Oracle connection and policy setup" not in response.data
    policies.assert_not_called()


@pytest.mark.parametrize("failure", [
    RuntimeError("secret-driver-settings\nforged log entry"),
    ValueError("Missing discount inputs: secret-driver-settings\nforged log entry"),
    oracledb.DatabaseError(SimpleNamespace(code=3113, message="secret-driver-settings\nforged log entry")),
])
def test_ui_logs_sanitized_unexpected_failures(failure, monkeypatch, caplog):
    def fail(_):
        raise failure

    monkeypatch.setattr("app.answer_question", fail)
    with caplog.at_level("ERROR", logger=app.logger.name):
        response = app.test_client().post("/", data={"question": "West private-question"})

    assert response.status_code == 503
    assert b"Analysis unavailable." in response.data
    assert b"secret-driver-settings" not in response.data
    records = [record for record in caplog.records if record.name == app.logger.name]
    assert records
    assert any(type(failure).__name__ in record.getMessage() for record in records)
    assert all(record.exc_info is None for record in records)
    assert "secret-driver-settings" not in caplog.text
    assert "forged log entry" not in caplog.text
    assert "private-question" not in caplog.text


@pytest.mark.parametrize("error_type", ["MissingCostsError", "MissingDiscountInputsError"])
def test_ui_data_quality_message_does_not_expose_exception_details(error_type, monkeypatch):
    import agent

    def fail(_):
        raise getattr(agent, error_type)("secret-driver-settings")

    monkeypatch.setattr("app.answer_question", fail)
    response = app.test_client().post("/", data={"question": "West"})

    assert response.status_code == 503
    assert b"Check" in response.data
    assert b"secret-driver-settings" not in response.data


def test_ui_logs_oracle_code_and_owned_failure_location(monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise oracledb.DatabaseError(SimpleNamespace(code=3113, message="secret-driver-settings"))

    mock_run_sql(monkeypatch, fail)
    with caplog.at_level("ERROR", logger=app.logger.name):
        response = app.test_client().post("/", data={"question": "West"})

    assert response.status_code == 503
    assert "DatabaseError" in caplog.text
    assert "ORA-03113" in caplog.text
    assert "agent.py:" in caplog.text
    assert "secret-driver-settings" not in caplog.text


def test_ui_does_not_log_untrusted_exception_class_name(monkeypatch, caplog):
    failure = type("secret_driver_settings", (Exception,), {})()

    def fail(_):
        raise failure

    monkeypatch.setattr("app.answer_question", fail)
    with caplog.at_level("ERROR", logger=app.logger.name):
        response = app.test_client().post("/", data={"question": "West"})

    assert response.status_code == 503
    assert "Analysis failed" in caplog.text
    assert "secret_driver_settings" not in caplog.text


def test_ui_sanitizes_invalid_policy_encoding(tmp_path, monkeypatch):
    (tmp_path / "policy.md").write_bytes(b"\xff")
    monkeypatch.setattr("policies.DOCS", tmp_path)
    row = {"region_name": "West", "missing_cost_count": 0, "missing_discount_count": 0,
           "missing_revenue_count": 0,
           "before_last_month": "2025-06", "after_first_month": "2025-07",
           "before_margin_pct": 40, "after_margin_pct": 25}
    mock_run_sql(monkeypatch, lambda sql, params=None: {
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

    mock_run_sql(monkeypatch, failure)
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
