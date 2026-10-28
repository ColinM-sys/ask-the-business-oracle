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
_cache = None


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


def _source_state():
    directory = DOCS.resolve()
    paths = sorted(directory.glob("*.md"))
    if not paths:
        raise RuntimeError("No policy documents found")
    files = []
    for path in paths:
        stat = path.stat()
        files.append((path.name, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                      stat.st_dev, stat.st_ino))
    return directory, tuple(files)


def _model_state(cur):
    return tuple(cur.execute(
        """SELECT model_name, object_id, last_ddl_time FROM user_mining_models
           LEFT JOIN user_objects ON object_name = model_name AND object_type = 'MINING MODEL'
           WHERE mining_function = 'EMBEDDING' ORDER BY model_name"""
    ).fetchall())


def ensure_policies():
    """Refresh only on source/model changes; never modify the business tables."""
    global _cache
    source_state = _source_state()
    with connect() as con:
        cur = con.cursor()
        database = (connect, getattr(con, "username", None), getattr(con, "dsn", None))
        cached = _cache
        if cached and cached[:2] == (database, source_state):
            models = _model_state(cur)
            if cached[2] == models:
                return cached[3]

        with _lock:
            # A waiting refresh must use current sources and models, not its old snapshot.
            source_state = _source_state()
            models = _model_state(cur)
            cached = _cache
            if cached and cached[:3] == (database, source_state, models):
                return cached[3]
            _cache = None
            while True:
                directory, files = source_state
                sources = [(name, (directory / name).read_text(encoding="utf-8"))
                           for name, *_ in files]
                current_state = _source_state()
                if current_state == source_state:
                    break
                source_state = current_state
            # Include model identity so replacement under the same name refreshes vectors,
            # including after a process restart.
            fingerprint = hashlib.sha256(repr((sources, models)).encode()).hexdigest()
            if not cur.execute("SELECT table_name FROM user_tables WHERE table_name='POLICY_CHUNKS'").fetchone():
                cur.execute("""CREATE TABLE policy_chunks (
                    id NUMBER PRIMARY KEY, doc VARCHAR2(255), chunk VARCHAR2(4000),
                    embedding VECTOR(*, FLOAT32), backend VARCHAR2(128), source_hash VARCHAR2(64))""")
            existing = cur.execute("SELECT DISTINCT backend, source_hash FROM policy_chunks").fetchall()
            if (len(existing) == 1 and existing[0][1] == fingerprint
                    and (existing[0][0] == "hash-v1-512"
                         or any(model[0] == existing[0][0] for model in models))):
                backend = existing[0][0]
                _cache = (database, source_state, models, backend)
                return backend
            if cached and cached[0] == database and cached[2] == models:
                backend = cached[3]
            else:
                backend = "hash-v1-512"
                for model, _, _ in models:
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
            vectors = [embed(cur, chunk, backend) for _, chunk in chunks]
            cur.execute("DELETE FROM policy_chunks")
            for index, ((doc, chunk), vector) in enumerate(zip(chunks, vectors), 1):
                cur.execute("""INSERT INTO policy_chunks (id,doc,chunk,embedding,backend,source_hash)
                    VALUES (:id,:doc,:chunk,:vector,:backend,:fingerprint)""",
                    id=index, doc=doc, chunk=chunk, vector=vector, backend=backend, fingerprint=fingerprint)
            con.commit()
            _cache = (database, source_state, models, backend)
            return backend


if __name__ == "__main__":
    print("Policy embeddings ready:", ensure_policies())
