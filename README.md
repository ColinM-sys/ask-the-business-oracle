# Ask the Business

A local Flask app that explains regional margin changes with Oracle SQL evidence
and policy retrieval. No API key or language model is required.

Run these commands from the repository root in PowerShell with Python 3.10+ and
Docker Desktop running Linux containers. For a fresh installation, create the
database using the [official Oracle AI Database Free image](https://www.oracle.com/database/free/get-started/):

```powershell
python -m pip install -r requirements.txt
$env:ORACLE_PWD = [System.Net.NetworkCredential]::new('', (Read-Host 'Oracle admin password' -AsSecureString)).Password
docker run -d --name oracle-free -p 127.0.0.1:1521:1521 -e ORACLE_PWD -v oracle-free-data:/opt/oracle/oradata container-registry.oracle.com/database/free:latest
Remove-Item Env:\ORACLE_PWD
docker inspect --format '{{.State.Health.Status}}' oracle-free
```

Choose a strong admin password at the hidden prompt. Repeat the health check
until it reports `healthy`; first startup can take several minutes. The named
volume preserves the database. If `oracle-free` already exists, use
`docker start oracle-free` and the health check instead of creating it again.

For a new database, open SQL*Plus inside the container:

```powershell
docker exec -it oracle-free sqlplus / as sysdba
```

Enter this SQL one command at a time to create the application user in `FREEPDB1`.
At the hidden prompt, enter a separate application password before continuing;
keep it for the configuration below:

```sql
ALTER SESSION SET CONTAINER = FREEPDB1;
SET VERIFY OFF
ACCEPT atb_password CHAR PROMPT 'New atb password: ' HIDE
CREATE USER atb IDENTIFIED BY "&atb_password" DEFAULT TABLESPACE users QUOTA 100M ON users;
UNDEFINE atb_password
GRANT CREATE SESSION, CREATE TABLE TO atb;
EXIT
```

If `atb` already exists, reuse its credentials and skip user creation. These
privileges allow the setup scripts to create tables in the user's own schema.

Create a local configuration only if you do not already have `.env`:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

The [.env.example](.env.example) supplies `ORACLE_USER=atb` and
`ORACLE_DSN=localhost:1521/FREEPDB1`. Edit only your local `.env` to set
`ORACLE_PASSWORD` to the application password entered above. Use raw values,
without surrounding quotes; `db.py` does not remove quotes. Leave the wallet and
API-key fields blank for this local database. Existing environment variables
override `.env`. Never print or commit credentials; `.env` is gitignored.

Verify the connection, then load the schema and sample business data once into
the empty `atb` schema before loading policies and starting the app:

```powershell
python db.py
python load.py
python policies.py
python app.py
```

`load.py` executes [data/oracle_load.sql](data/oracle_load.sql), which contains
the `CREATE TABLE` and `INSERT` statements for `sales`, `costs`, `regions`,
`customers`, `discounts`, and `products`. It prints row counts: 6,810 sales,
768 costs, 4 regions, 120 customers, 6 discounts, and 8 products. Sales and costs
join on region, product, and month, using the schema expected by `agent.py`.
Do not rerun `python load.py` on populated tables: it is a one-time loader and
will fail on existing table names. After a reboot, start the existing container,
wait for `healthy`, and run `python policies.py` and `python app.py`.

Open http://127.0.0.1:5055 and ask “Why did West margin fall since July 2025?”.
The bundled sample compares 2024-10 through 2025-06 with 2025-07 through 2026-09:
West margin falls from 38.89% to 25.53%, a 13.36 percentage-point drop.
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
python -m pytest -q
```

Tests require Node.js on PATH for the UI checks and include live Oracle
integration; complete the bootstrap above and start the local database first.
They do not reload or alter the business tables.

Oracle syntax reference: [VECTOR_EMBEDDING](https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/vector_embedding.html)
and [Oracle vector search quick start](https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/sql-quick-start-using-vector-embedding-model-uploaded-database.html).
