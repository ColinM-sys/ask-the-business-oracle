"""Idempotent policy ingestion; local hashing fallback needs no API key."""
from array import array
import hashlib
import logging
import math
from pathlib import Path
import re
import threading

import oracledb

from db import connect

DOCS = Path(__file__).parent / "docs"
_lock = threading.Lock()
_logger = logging.getLogger(__name__)
_model_identifier = re.compile(r"[A-Z][A-Z0-9_$#]*")


def hash_embedding(text, dimensions=512):
    values = [0.0] * dimensions
    for word in re.findall(r"[a-z]+", text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        values[int.from_bytes(digest[:4], "big") % dimensions] += 1
    norm = math.sqrt(sum(v * v for v in values)) or 1
    return array("f", (v / norm for v in values))


def embed(cursor, text, backend):
    if backend == "hash-v1-512":
        return hash_embedding(text)
    if not _model_identifier.fullmatch(backend):
        raise ValueError("Invalid embedding model name")
    return cursor.execute(
        f"SELECT VECTOR_EMBEDDING({backend} USING :text AS DATA) FROM dual",
        text=text,
    ).fetchone()[0]


def ensure_policies():
    """Refresh only on source/model changes; never modify the business tables."""
    with _lock:
        with connect() as con:
            cur = con.cursor()
            if not cur.execute("SELECT table_name FROM user_tables WHERE table_name='POLICY_CHUNKS'").fetchone():
                try:
                    cur.execute("""CREATE TABLE policy_chunks (
                        id NUMBER PRIMARY KEY, doc VARCHAR2(255), chunk VARCHAR2(4000),
                        embedding VECTOR(*, FLOAT32), backend VARCHAR2(128), source_hash VARCHAR2(64))""")
                except oracledb.DatabaseError as exc:
                    # Another process may have created the table after our check.
                    if not exc.args or getattr(exc.args[0], "code", None) != 955:
                        raise
            # DDL commits implicitly, so acquire the transaction lock only after setup.
            cur.execute("LOCK TABLE policy_chunks IN EXCLUSIVE MODE")
            sources = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(DOCS.glob("*.md"))]
            if not sources:
                raise RuntimeError("No policy documents found")
            # Refresh cached chunks when the chunking algorithm changes.
            source_hash = hashlib.sha256(repr(("chunk-v2", sources)).encode()).hexdigest()
            backend = "hash-v1-512"
            model_version = (backend,)
            for model, created, object_id in cur.execute(
                """SELECT model_name, creation_date,
                    (SELECT object_id FROM user_objects WHERE object_name=m.model_name
                        AND object_type='MINING MODEL' AND subobject_name IS NULL)
                    FROM user_mining_models m WHERE mining_function='EMBEDDING' ORDER BY model_name"""
            ).fetchall():
                if not _model_identifier.fullmatch(model):
                    _logger.warning("Skipping unsupported embedding model %r", model)
                    continue
                try:
                    embed(cur, "Business policy", model)
                    backend = model
                    model_version = (model, created, object_id)
                    break
                except oracledb.DatabaseError as exc:
                    code = getattr(exc.args[0], "code", None) if exc.args else None
                    # ORA-40290: the model is incompatible with this SQL operator.
                    # Other failures indicate broken operations and must stop ingestion.
                    if code != 40290:
                        raise
                    _logger.warning("Embedding model %r incompatible (ORA-%05d)", model, code)
            else:
                _logger.warning("Using hash-v1-512 fallback: no compatible embedding models available")
            # Tag the collection fingerprint with the model revision. The non-hex prefix
            # also invalidates legacy rows, whose model revision was not recorded.
            version = "v" + hashlib.sha256(repr(model_version).encode()).hexdigest()[:31]
            fingerprint = version + source_hash[:32]
            existing = cur.execute("SELECT DISTINCT backend, source_hash FROM policy_chunks").fetchall()
            if existing == [(backend, fingerprint)]:
                return backend
            chunks = []
            for name, content in sources:
                # Keep headings with their paragraphs; these short documents fit in one chunk.
                paragraphs = re.split(r"\n\s*\n", content.strip())
                chunk = ""
                for paragraph in paragraphs:
                    # At most four UTF-8 bytes per character; each part fits VARCHAR2(4000 BYTE).
                    for offset in range(0, len(paragraph), 1000):
                        part = paragraph[offset:offset + 1000]
                        separator = "\n\n" if chunk and offset == 0 else ""
                        if chunk and (len(chunk) + len(separator) + len(part) > 1800
                                      or len((chunk + separator + part).encode("utf-8")) > 4000):
                            chunks.append((name, chunk))
                            chunk = ""
                            separator = ""
                        chunk += separator + part
                if chunk:
                    chunks.append((name, chunk))
            if not chunks:
                raise RuntimeError("No policy content found")
            vectors_by_chunk = {}
            if any(old_backend == backend and old_hash and old_hash.startswith(version)
                   for old_backend, old_hash in existing):
                for old_chunk, vector in cur.execute(
                    "SELECT chunk, embedding FROM policy_chunks "
                    "WHERE backend=:backend AND SUBSTR(source_hash, 1, 32)=:version",
                    backend=backend, version=version,
                ).fetchall():
                    if old_chunk is not None and vector is not None:
                        key = (backend, hashlib.sha256(old_chunk.encode("utf-8")).digest())
                        vectors_by_chunk[key] = vector
            vectors = []
            for _, chunk in chunks:
                key = (backend, hashlib.sha256(chunk.encode("utf-8")).digest())
                if key not in vectors_by_chunk:
                    vectors_by_chunk[key] = embed(cur, chunk, backend)
                vectors.append(vectors_by_chunk[key])
            cur.execute("DELETE FROM policy_chunks")
            for index, ((doc, chunk), vector) in enumerate(zip(chunks, vectors), 1):
                cur.execute("""INSERT INTO policy_chunks (id,doc,chunk,embedding,backend,source_hash)
                    VALUES (:id,:doc,:chunk,:vector,:backend,:fingerprint)""",
                    id=index, doc=doc, chunk=chunk, vector=vector, backend=backend, fingerprint=fingerprint)
            con.commit()
            return backend


if __name__ == "__main__":
    print("Policy embeddings ready:", ensure_policies())
