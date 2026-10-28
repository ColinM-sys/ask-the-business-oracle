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
        fingerprint = hashlib.sha256(repr(sources).encode()).hexdigest()
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
                # Keep headings with their paragraphs; these short documents fit in one chunk.
                paragraphs = re.split(r"\n\s*\n", content.strip())
                chunk = ""
                for paragraph in paragraphs:
                    # At most four UTF-8 bytes per character; each part fits VARCHAR2(4000 BYTE).
                    for offset in range(0, len(paragraph), 1000):
                        part = paragraph[offset:offset + 1000]
                        if chunk and (len(chunk) + len(part) + 2 > 1800
                                      or len((chunk + "\n\n" + part).encode("utf-8")) > 4000):
                            chunks.append((name, chunk))
                            chunk = ""
                        chunk = (chunk + "\n\n" + part).strip()
                if chunk:
                    chunks.append((name, chunk))
            if not chunks:
                raise RuntimeError("No policy content found")
            rows = [dict(id=index, doc=doc, chunk=chunk, backend=backend, fingerprint=fingerprint)
                    for index, (doc, chunk) in enumerate(chunks, 1)]
            if backend == "hash-v1-512":
                for row in rows:
                    row["vector"] = hash_embedding(row["chunk"])
            vector_value = ":vector" if backend == "hash-v1-512" else "NULL"
            cur.execute("DELETE FROM policy_chunks")
            cur.executemany(f"""INSERT INTO policy_chunks (id,doc,chunk,embedding,backend,source_hash)
                VALUES (:id,:doc,:chunk,{vector_value},:backend,:fingerprint)""", rows)
            if backend != "hash-v1-512":
                cur.execute(f"""UPDATE policy_chunks
                    SET embedding = VECTOR_EMBEDDING({backend} USING chunk AS DATA)""")
            con.commit()
            return backend


if __name__ == "__main__":
    print("Policy embeddings ready:", ensure_policies())
