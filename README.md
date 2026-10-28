# Ask the Business

A local Flask app that explains regional margin changes with Oracle SQL evidence
and policy retrieval. No API key or language model is required.

```powershell
python -m pip install -r requirements.txt
docker start oracle-free
python policies.py
python app.py
```

Open http://127.0.0.1:5055 and ask “Why did West margin fall since July 2025?”.
The existing `.env` is read by `db.py`; business tables must already be loaded.
Do not commit credentials. `policies.py` creates only `policy_chunks`, probes
registered Oracle embedding models, and otherwise uses deterministic normalized
hashing vectors. Source changes trigger a transactional policy refresh. Queries
use Oracle `VECTOR_DISTANCE` with cosine distance; the local hashing fallback is
lexical retrieval rather than a semantic language model.

The app runs fixed analysis for margin, discount and freight, regardless of
question wording. It recognizes West, Central, East or South and a full month
name plus year or `YYYY-MM`. Defaults are West and July 2025. It compares all
available sales before the selected month with all available sales from that
month onward, including later years. It displays those boundaries explicitly.
Margin includes COGS and freight and is weighted by net revenue; discounts use
gross-sales weights; freight per unit uses quantity weights. Freight movement
therefore includes product mix and can differ from the contract's rate change.
The other regions provide context, not a causal decomposition.

Every number in the brief, including dates and SQL-derived differences, names an
exact SQL result cell. The critic drops entire claims with unsupported numbers.
Click a number to view SQL, bound parameters and returned rows, with the cited
cell highlighted. Policy citations open retrieved source text and vector SQL.
Fixed qualitative explanations are included only when their policy documents
were retrieved; West's explanation applies to the default change point.

`run_sql` parses one SELECT, limits tables and functions to an allowlist, rejects
writes/locks/sequences/procedural calls, and executes in a read-only transaction.
It refuses evidence over the row limit instead of silently truncating it.
Policy setup is the separate write path. The Flask server binds only to localhost
and runs without debug mode; this demo has no production authentication layer.

```powershell
python -m pytest -q test_app.py
```

Tests include live Oracle integration; start the local database first. They do
not reload or alter the business tables.

Oracle syntax reference: [VECTOR_EMBEDDING](https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/vector_embedding.html)
and [Oracle vector search quick start](https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/sql-quick-start-using-vector-embedding-model-uploaded-database.html).
