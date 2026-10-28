# Ask the Business on Oracle AI Database (Codex + Oracle AI Database Hackathon, Oct 28)

Codex reads this file first. It describes the project and the rules.

## Goal
A user asks a plain-English business question, such as "Why did West margin fall since July 2025?". The app returns
a short brief: the cause, the evidence and 2-3 actions. Every number links to the SQL and the rows it came from.
- **Numbers** come from SQL over the governed tables in Oracle AI Database.
- **Reasons** come from AI Vector Search over the policy docs (`VECTOR` column + `VECTOR_DISTANCE`).
- The brief cites evidence ids. A deterministic check drops any number that doesn't match its cited rows.

## Data (already here)
- `data/*.csv`: sales, costs, customers, discounts, products, regions.
- `data/oracle_load.sql` holds the same tables as CREATE TABLE + INSERT statements. Run it once against the database.
- `docs/*.md`: 4 policy docs. Chunk them, embed them, and store them in table `policy_chunks(id, doc, chunk, embedding VECTOR)`.
- There is a planted story to find: from July 2025, West ran a Q3 volume push (15-22% discounts) and took on a new
  freight contract. Expected result: West gross margin fell about 13.3 pts (38.9% to 25.6%), and freight per unit
  rose about 60%.

## Build order
1. `db.py`: connect with python-oracledb. Read the settings from env (see `.env.example`). Never hardcode secrets.
2. Load the tables, then the policy chunks with embeddings. Use in-database ONNX embeddings if available, otherwise
   OpenAI embeddings.
3. Tools: `run_sql(select)` is read-only and refuses anything that isn't a SELECT. `search_policies(text, k=5)` does
   the vector search.
4. Agent loop: plan sub-questions, run the SQL, run the policy search, then write the brief with citations.
5. A small web UI (one page) shows the brief. Clicking a number shows its SQL and rows.
6. Tests: the read-only guard, the critic, and an end-to-end run of the West question.

## Rules
- Read-only SQL from the agent. Refuse DELETE, DROP, UPDATE, INSERT, ALTER, GRANT, and PL/SQL blocks.
- Never print or commit API keys or DB passwords. Keep `.env` in `.gitignore`.

Local DB: docker container oracle-free (Oracle AI Database 26ai Free), user atb, settings in .env. `docker start oracle-free` after a reboot. Tables loaded by `python load.py`.
