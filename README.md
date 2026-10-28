# Ask the Business

> **Hackathon report: [REPORT.md](REPORT.md).** Built at the Codex + Oracle AI Database Hackathon (Oracle AI
> World, Oct 28, 2026) by Colin McDonough, orchestrating **8,809 OpenAI Codex agent runs (1.19 billion tokens)**.
> Up to 200 agents ran in parallel across GPT-6.1-Sol, GPT-6-Astra and GPT-6.1-Terra. They reviewed, fixed,
> tested and merged this repo: **138 agent PRs** (66 merged, 68 closed with a reason) and **42 → 345 tests**.
> Usage was updated at 14:47 ET, after the swarm paused. The report covers calls and tokens by component and
> model, the 6-to-200-agent scaling curve, the merge-bot pipeline, the failures hit and how each was fixed, and
> **AgentSocial**, where agents fact-check each other in the open on a local social network. AgentSocial has its
> own repo: [ColinM-sys/agentsocial](https://github.com/ColinM-sys/agentsocial). The orchestration code is in
> [`swarm/`](swarm).

A local Flask app that explains regional margin changes with Oracle SQL evidence
and policy retrieval. No API key or language model is required.

If `.env` does not exist, copy `.env.example` to `.env`. Set `ORACLE_USER`
(database user), `ORACLE_PASSWORD` (database password), and `ORACLE_DSN`
(connection string, such as `host:port/service_name`) in `.env` before running
`policies.py` or `load.py`. The repository includes `requirements.txt`, `.env.example`, and
the four policy documents in `docs/`. Run `python load.py` only once on an empty
database; skip it if the business tables are already loaded.

```powershell
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Fill in the required Oracle settings in .env before continuing.
docker start oracle-free
python load.py  # Only for an empty database; skip if tables are already loaded.
python policies.py
python app.py
```

Open http://127.0.0.1:5055 and ask “Why did West margin fall since July 2025?”.
`.env` is read by `db.py`; do not commit credentials. `policies.py` creates only
`policy_chunks`, probes registered Oracle embedding models, and otherwise uses
deterministic normalized hashing vectors. Source changes trigger a transactional
policy refresh. To accept a revised policy, review the fixed claims in `agent.py`,
then update both `POLICY_SHA256` (SHA-256 of the stripped full document) and
`REVIEWED_POLICY_HASHES` (SHA-256 of its complete chunk with whitespace collapsed).
The revised document must still fit in one chunk. Until both fingerprints match,
affected explanations and recommendations remain unavailable even after refresh
and retrieval. Queries use Oracle `VECTOR_DISTANCE` with cosine distance; the local
hashing fallback is lexical retrieval rather than a semantic language model.

The app runs fixed analysis for margin, discount and freight, regardless of
question wording. It recognizes West, Central, East or South and a full month
name plus year or `YYYY-MM`. Both formats require a year from 2000 through 2099.
Defaults are West and July 2025. It compares all available sales before the
selected month with all available sales from that month onward, including later
years. It displays those boundaries explicitly.
Margin includes COGS and freight and is weighted by net revenue; discounts use
gross-sales weights; freight per unit uses quantity weights. Freight movement
therefore includes product mix and can differ from the contract's rate change.
Changes and margin direction use unrounded aggregates. SQL rounds period metrics
and changes independently to two decimal places, so subtracting displayed
endpoints may give a different result. For example, margins of 25.004% and
24.996% both round to 25%, but the drop is 0.01 percentage points.
The other regions provide context, not a causal decomposition.

The fixed brief uses cited SQL result cells for dates and SQL-derived values. The
critic checks numeric parts against the cited cells and drops claims with
unsupported numeric parts or digits in plain text. Numbers written as words,
such as "five million", are not checked and can pass without a citation. Click a
number to view SQL, bound parameters and returned rows, with the cited cell
highlighted. Policy citations open retrieved source text and vector SQL.
Fixed qualitative explanations are included only when their policy documents
were retrieved; West's explanation applies to the default change point.

`run_sql` parses one SELECT, limits tables and functions to an allowlist, rejects
writes/locks/sequences/procedural calls, and executes in a read-only transaction.
It refuses evidence over the row limit instead of silently truncating it.
`search_policies()` calls `ensure_policies()` on every policy search, including
normal POST requests. It creates `policy_chunks` if absent and transactionally
deletes and replaces all rows and commits when the table is empty or the source
documents or selected embedding backend change. The runtime database user needs
`CREATE TABLE` when the table is missing, tablespace quota (or
`UNLIMITED TABLESPACE`) for storage, and `SELECT`, `DELETE`, and `INSERT` access
to `policy_chunks` (the table owner has these privileges). Running
`python policies.py` once does not make SELECT-only runtime permissions sufficient.
The table is shared by requests and other app instances using the same schema,
so refreshes change their policy state too. Instances with different documents
or embedding backends can overwrite each other's policy state.

The Flask server binds only to localhost and runs without debug mode; this demo
has no production authentication layer.

```powershell
python -m pytest -q test_app.py
```

Tests include live Oracle integration; start the local database first. They also
require Node.js installed and available as `node` on PATH. Tests do not reload or
alter the business tables.

Oracle syntax reference: [VECTOR_EMBEDDING](https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/vector_embedding.html)
and [Oracle vector search quick start](https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/sql-quick-start-using-vector-embedding-model-uploaded-database.html).
