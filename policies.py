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
    if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", backend):
        raise ValueError("Invalid embedding model name")
    return cursor.execute(
        f"SELECT VECTOR_EMBEDDING({backend} USING :text AS DATA) FROM dual",
        text=text,
    ).fetchone()[0]


def ensure_policies():
    """Refresh only on source/model changes; never modify the business tables."""
    with _lock:
        sources = [(p.name, p.read_text(encoding="utf-8")) for p in sorted(DOCS.glob("*.md"))]
        if not sources:
            raise RuntimeError("No policy documents found")
        # Include the chunking version so existing rows are refreshed after changes.
        fingerprint = hashlib.sha256(repr(("chunk-v2", sources)).encode()).hexdigest()
        with connect() as con:
            cur = con.cursor()
            if not cur.execute("SELECT table_name FROM user_tables WHERE table_name='POLICY_CHUNKS'").fetchone():
                cur.execute("""CREATE TABLE policy_chunks (
                    id NUMBER PRIMARY KEY, doc VARCHAR2(255), chunk VARCHAR2(4000),
                    embedding VECTOR(*, FLOAT32), backend VARCHAR2(128), source_hash VARCHAR2(64))""")
            backend = "hash-v1-512"
            for (model,) in cur.execute(
                "SELECT model_name FROM user_mining_models WHERE mining_function='EMBEDDING' ORDER BY model_name"
            ).fetchall():
                try:
                    embed(cur, "Business policy", model)
                    backend = model
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
            existing = cur.execute("SELECT DISTINCT backend, source_hash FROM policy_chunks").fetchall()
            if existing == [(backend, fingerprint)]:
                return backend
            chunks = []
            for name, content in sources:
                if not content.strip():
                    continue
                offset = 0
                while offset < len(content):
                    # Respect both the character target and VARCHAR2(4000 BYTE).
                    part = content[offset:offset + 1800].encode("utf-8")[:4000].decode("utf-8", "ignore")
                    if (offset + len(part) < len(content) and not part[-1].isspace()
                            and not content[offset + len(part)].isspace()):
                        # Prefer a whitespace boundary; an oversized word must be split.
                        boundary = re.search(r"(\s+)\S*$", part)
                        if boundary:
                            part = part[:boundary.end(1)]
                    chunks.append((name, part))
                    offset += len(part)
            if not chunks:
                raise RuntimeError("No policy content found")
            vectors = [embed(cur, chunk, backend) for _, chunk in chunks]
            cur.execute("DELETE FROM policy_chunks")
            for index, ((doc, chunk), vector) in enumerate(zip(chunks, vectors), 1):
                cur.execute("""INSERT INTO policy_chunks (id,doc,chunk,embedding,backend,source_hash)
                    VALUES (:id,:doc,:chunk,:vector,:backend,:fingerprint)""",
                    id=index, doc=doc, chunk=chunk, vector=vector, backend=backend, fingerprint=fingerprint)
            con.commit()
            return backend


if __name__ == "__main__":
    print("Policy embeddings ready:", ensure_policies())
