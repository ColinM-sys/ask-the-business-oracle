# Ask the Business

A local Flask app that explains regional margin changes with Oracle SQL evidence
and policy retrieval. No API key or language model is required.

## First-time Oracle setup

Install Python and Docker, start Docker, and run these commands from the project
root in PowerShell. The [Oracle Container Registry](https://container-registry.oracle.com/ords/ocr/ba/database/free)
may require accepting the image terms and `docker login container-registry.oracle.com`.
Port 1521 must be free.
Create a fresh Oracle AI Database Free container and persistent volume:

```powershell
python -m pip install -r requirements.txt
$env:ORACLE_PWD = [System.Net.NetworkCredential]::new('', (Read-Host 'Choose a SYS/SYSTEM password' -AsSecureString)).Password
docker run -d --name oracle-free -p 127.0.0.1:1521:1521 -e ORACLE_PWD -v oracle-free-data:/opt/oracle/oradata container-registry.oracle.com/database/free:latest-lite
Remove-Item Env:ORACLE_PWD
docker logs -f oracle-free
```

Wait for `DATABASE IS READY TO USE!`, then press Ctrl+C to stop following the
logs. On later starts use `docker start oracle-free` and wait for readiness; do
not recreate the container or reload an existing schema.

Create the application schema in `FREEPDB1` using SQL*Plus inside the container:

```powershell
docker exec -it --user oracle -e ORACLE_SID=FREE oracle-free sqlplus / as sysdba
```

At the SQL*Plus prompt, enter these commands in order. When prompted, choose
and enter an ATB password of at least eight letters and digits, including
uppercase, lowercase and numbers, before continuing. The hidden prompt avoids
putting it in command history; remember it for the next step.

```sql
ALTER SESSION SET CONTAINER=FREEPDB1;
SET VERIFY OFF
ACCEPT atb_password CHAR PROMPT 'Choose ATB password: ' HIDE
CREATE USER atb IDENTIFIED BY "&atb_password" DEFAULT TABLESPACE users QUOTA UNLIMITED ON users;
GRANT CREATE SESSION, CREATE TABLE TO atb;
UNDEFINE atb_password
EXIT
```

Create a `.env` file in the project root with these three unquoted settings;
replace the placeholder with the ATB password you chose above:

```text
ORACLE_USER=atb
ORACLE_PASSWORD=<ATB password>
ORACLE_DSN=localhost:1521/FREEPDB1
```

Existing process environment variables override `.env`. Never commit credentials;
`.env` is ignored by Git. Load `data/oracle_load.sql` into the fresh schema once:
`load.py` creates the business tables and inserts their data, so it is not safe
to rerun on an already populated schema. Then set up policies and start the app:

```powershell
python load.py
python policies.py
python app.py
```

Open http://127.0.0.1:5055 and ask “Why did West margin fall since July 2025?”.
`policies.py` creates only `policy_chunks`, probes
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

Tests include live Oracle integration; start the local database first. They also
require Node.js installed and available as `node` on PATH. Tests do not reload or
alter the business tables.

Oracle syntax reference: [VECTOR_EMBEDDING](https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/vector_embedding.html)
and [Oracle vector search quick start](https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/sql-quick-start-using-vector-embedding-model-uploaded-database.html).
