# A 200-Agent Codex Swarm on Oracle AI Database: Review–Fix–Merge Pipelines, Scaling Measurements, and Open Agent Coordination

**Colin McDonough**
*Agents: OpenAI Codex CLI 0.165.0 (GPT-6.1-Sol, GPT-6-Astra, GPT-6.1-Terra)*
Codex + Oracle AI Database Hackathon, Oracle AI World, Las Vegas, October 28, 2026

---

## Abstract

We report on one hackathon day in which a single desktop ran **8,809 OpenAI Codex agent runs** and used
**1.19 billion tokens** to build and continuously improve *Ask the Business*. Ask the Business is an
Oracle AI Database 26ai application in which every number in a generated business brief is tied to the SQL
result cell it came from.

An orchestration layer drove four kinds of agent:
- **Reviewers:** 200 per round, in 8 areas.
- **Fixers:** up to 80 per round, each in its own git worktree.
- **Merge bots:** 20 reviewer-mergers.
- **AgentSocial agents:** scouts, checkers and judges that coordinate in the open on a local social network.

**Results:**
- **Scaling:** throughput rose from 181 to about 1,630 output tokens/s as parallel agents grew from 6 to 200,
  with **zero rate-limit errors**. The binding limit was the host, not the API.
- **Repository:** the swarm opened **138 pull requests**, merged 66 and closed 68 with a written reason. The
  test suite grew from **42 to 345**, and `master` reached **227 commits**.
- **AgentSocial:** a prompt change turned a rubber-stamping checker (46 confirmed, 0 challenged) into a
  discriminating one (9 confirmed, 43 challenged).

We also document twelve operational failures. The largest were process mass-termination tied to how jobs were
launched, and a model/effort combination that exits under `codex exec`. We give the fix for each.

## 1 Introduction

Multi-agent coding systems are often described but rarely measured end to end. The hackathon supplied an
OpenAI key on a shared event project and asked teams to build on Oracle AI Database with Codex. We used the day
to answer three practical questions:

1. **Throughput.** How far does one desktop scale with parallel Codex agents, and what limits it?
2. **Quality.** Can a review–fix–merge pipeline improve a real repository continuously without degrading it?
3. **Coordination.** Can agents check each other's claims in the open, through a shared feed, and does that
   checking actually discriminate?

**Contributions:**
- **(i)** An application, *Ask the Business*, that combines Oracle SQL (numbers) with AI Vector Search
  (reasons), plus a deterministic critic.
- **(ii)** A pipelined review–fix–merge swarm, with deduplication and merge gates learned from rejected PRs.
- **(iii)** Scaling and token-accounting measurements taken from Codex's own session logs.
- **(iv)** AgentSocial, an open protocol for agent-to-agent fact-checking.
- **(v)** A failure analysis with concrete fixes.

## 2 System

### 2.1 Application: Ask the Business

| Layer | Implementation |
|---|---|
| Data | Oracle AI Database 26ai Free (local container), with sales, costs, customers, discounts, products and regions (6,810 sales rows). A FreeSQL.com schema is reachable from Codex through the SQLcl 26.3 MCP server. |
| Numbers | `run_sql` parses each query with sqlglot. It allows one `SELECT` over allowlisted tables and functions, rejects writes, locks and procedural calls, and runs in a read-only transaction. |
| Reasons | Policy documents are chunked into a `VECTOR` column and retrieved with `VECTOR_DISTANCE(..., COSINE)`. Registered in-database embedding models are probed; otherwise a deterministic hashing embedding is used. |
| Critic | A deterministic pass drops any claim whose number does not match the cited result cell, including sign, suffix and magnitude checks that were added by agents. |
| UI | A Flask page on port 5055. Clicking any number shows its SQL, bound parameters and rows, with the cited cell marked. |

**Reference result:** "Why did West margin fall since July 2025?" gives gross margin **38.89% → 25.53%
(−13.36 pts)**, driven by discounting and freight per unit. Every figure is linked to its query.

### 2.2 Agent runtime

Every agent is one `codex exec` process.

**Invocation:**
- Python calls `node …/@openai/codex/bin/codex.js exec --json -` directly. Calling `codex.cmd` lets `cmd.exe`
  mangle `|` and quotes.
- The prompt is sent on stdin, because Windows limits the command line to 32k characters.
- `--json` emits events. The last `agent_message` is the answer, and `turn.completed` carries usage.

**Models and effort:**

| Model | Effort used | Notes |
|---|---|---|
| GPT-6.1-Sol | default | Codex default until 13:40 ET |
| GPT-6.1-Terra | `ultra` | default from 13:56; 500k context |
| GPT-6-Astra | `max` | `ultra` (with automatic task delegation) exits with code 15 under `codex exec` |

**Sandboxing on Windows:**
- **Reviewers** receive the code in their prompt and run in an empty folder with `-s danger-full-access`.
  Under `read-only`, the auto-approval reviewer rejects file reads, and the agent reports "blocked".
- **Fixers and merge bots** each run in their own git worktree, with no secrets in their environment.

### 2.3 Review–fix–merge pipeline

```
          ┌────────── 200 reviewers / round (8 areas × 25 lenses), 25–100 concurrent ──────────┐
 master ─►│ correctness · security · tests · UX · Oracle · performance · docs · features        │
          └──────────────┬─────────────────────────────────────────────────────────────────────┘
                         │ each finding streams immediately (no round barrier)
                         ▼
               dedupe on (file, ⌊line/10⌋), cap 80/round
                         ▼
          fixer agent in its own worktree + branch (≤30 concurrent)
          must add a failing-first test; full suite must pass ─► PR
                         ▼
          20 merge bots: merge latest master → full suite → strict diff review → merge (serialized)
                         ▼
                 next round reviews the new master
```

Each reviewer gets a lens, such as "think like an attacker", "numbers and rounding" or "Oracle SQL specifics".
That way 25 reviewers in the same area look in different places.

The merge bots enforce rules learned from PRs that were closed in review before the bots existed. They refuse
changes that:
- need DBA/SYS setup
- change the schema at runtime for locking
- hardcode data-file content
- weaken the SQL guard or the critic

A round driver (`hammer.py`) never overlaps rounds, and numbers new branches above the highest existing branch.
A watchdog (`watchdog.py`) logs failure bursts, stalls and commit droughts.

### 2.4 AgentSocial

AgentSocial has its own repository: [ColinM-sys/agentsocial](https://github.com/ColinM-sys/agentsocial).
It is a local social network. Every agent posts a public profile card (role, skills, current task) and
public posts. For each claim:
- **2–8 scouts** post evidence, for or against, with verbatim quotes and URLs.
- A **checker** finds the scouts by reading their profiles and replies CONFIRMED or CHALLENGE.
- A **judge** reads the thread and posts a verdict: SUPPORTED, MISLEADING, FALSE or UNVERIFIABLE.

An improvement loop requeues a claim with more scouts when its verdict is unverifiable or contested. A claim
settles once its verdict repeats across two rounds.

### 2.5 Accounting

Codex writes one session log per run (`~/.codex/sessions/**/rollout-*.jsonl`). `usage_report.py` reads each
log's last `token_count` record, model and working folder, and labels the run by its prompt prefix. It filters
by folder (`--cwd-contains`) and start time (`--since`). All usage figures here come from these logs, not from
our own counters.

## 3 Experimental setup

- **Host:** one Windows 11 desktop (RTX 4090, 31.7 GB RAM). The GPU is not used by Codex; inference is in
  OpenAI's cloud.
- **Key:** one OpenAI service-account key on the event's shared project, used only through Codex's saved login.
- **Window:** 12:36 to 14:40 ET. The swarm was then paused so the machine could be used by another project.
- **Agents per stage:**

| Stage | Agents |
|---|---|
| Scaling test | 6, 10, 20, 50, 100, 200 in parallel, plus a 50-agent low-effort control |
| Repo rounds 1–2 | 4 reviewers per round, 1 fixer per finding |
| Repo round 3 | 200 reviewers, then 24 fixers |
| Pipelined rounds | 200 reviewers each, ≤80 fixers, 20 merge bots running alongside |
| AgentSocial | 3–10 claims per round, 2–8 scouts, 1 checker and 1 judge per claim |

- **Peak concurrency:** 200 Codex processes in the scaling test. Up to 120 were observed during the pipelined
  rounds.

## 4 Results

### 4.1 Scaling

**Table 1.** Parallel fact-checking agents (`codex_swarm.py`, web search on, GPT-6.1-Sol).

| Agents | Wall time | Output tok/s (all agents) | Finished |
|---:|---:|---:|---:|
| 6 | 46 s | 181 | 6/6 |
| 10 | 41 s | 280 | 10/10 |
| 20 | 41 s | 625 | 20/20 |
| 50 | 59 s | 1,091 | 50/50 |
| 100 | 110 s | 1,228 | 100/100 |
| 200 | ~165 s | ~1,630 | 199/200 |

**Observations:**
- Wall time stayed flat up to 50 agents, then grew under host contention. The API never returned a rate-limit
  error.
- The single failure at 200 agents was Windows error 1450 ("insufficient system resources").
- One agent alone produced about 42 output tok/s, including startup.
- **Low-effort control:** reasoning effort `low` used the same tokens (7.41M vs 7.78M) and the same wall time
  (60 s vs 59 s) as default, and changed 3 of 50 verdicts. We kept the default.

### 4.2 Calls and tokens

**Table 2.** Usage by component. These are this project's runs only, as of 14:47 ET.

| Component | Codex calls | completed | input tokens | (cached) | output tokens | (reasoning) | total |
|---|---:|---:|---:|---:|---:|---:|---:|
| AgentSocial | 2,177 | 1,988 | 418,565,313 | 345,936,802 | 2,023,281 | 573,117 | 420,588,594 |
| Repo loop: reviewers | 5,007 | 1,985 | 401,103,698 | 333,652,318 | 8,797,542 | 7,701,066 | 409,901,240 |
| Repo loop: fixers | 439 | 417 | 175,835,188 | 163,690,001 | 1,503,791 | 713,935 | 177,338,979 |
| Merge bots | 549 | 504 | 103,890,054 | 94,934,773 | 992,523 | 573,971 | 104,882,577 |
| Swarm scaling test | 442 | 441 | 74,285,317 | 54,041,597 | 503,692 | 80,333 | 74,789,009 |
| Killed before the prompt was logged | 175 | 40 | 560,320 | 0 | 200 | 0 | 560,520 |
| App build (Codex) | 1 | 1 | 524,021 | 481,808 | 14,655 | 1,739 | 538,676 |
| Setup tests | 19 | 19 | 502,779 | 234,119 | 2,323 | 33 | 505,102 |
| **Total** | **8,809** | **5,395** | **1,175,266,690** | **992,971,418** | **13,838,007** | **9,644,194** | **1,189,104,697** |

**Table 3.** Usage by model.

| Model | Codex calls | completed | total tokens | output tokens |
|---|---:|---:|---:|---:|
| GPT-6.1-Terra | 4,392 | 2,706 | 864,573,968 | 10,537,387 |
| GPT-6.1-Sol | 2,408 | 2,011 | 173,156,509 | 1,189,271 |
| GPT-6-Astra | 1,868 | 678 | 151,374,220 | 2,111,349 |
| (killed before the model was logged) | 141 | 0 | 0 | 0 |

Across the whole event key that day, as of 14:47 ET, the totals were **9,092 runs and 1.195B tokens**. That
figure includes a few runs for other private projects.

**Cost structure:**
- **Input dominates.** 98.8% of tokens are input, and 84.5% of input was served from cache.
- **Output is small.** Output is 1.2% of tokens, and 70% of that is reasoning.
- **Fixed overhead.** Every call carries about 13.4k tokens of Codex system instructions.
- **Reviewers are prompt-heavy.** Each reviewer receives the whole app, about 150k tokens.
- **Most of the bill is reading, not writing.**

### 4.3 Repository improvement

**Table 4.** Rounds and outcomes.

| Stage | Reviewers | Findings → unique | Fixes passing tests | Merged | Tests after |
|---|---:|---:|---:|---:|---:|
| Codex initial build | — | — | — | — | 42 |
| Rounds 1–2 | 4 + 4 | 12 PRs | 12 | 10 (2 duplicates closed) | 93 |
| Round 3 | 200 | 277 → 24 | 15 | 10 (5 closed with reasons) | 137 |
| Pipelined rounds (four) | 200 each | streamed | 20/20, 26/26, 28/28, + a partial round | by merge bots | 213 → 345 |

**Totals:**
- **138** agent PRs: **66 merged**, 68 closed with a written reason, 4 open at pause.
- `master` reached **227 commits**.
- Of 149 logged fixes, **139 passed** the full suite and **10 failed** and were dropped.

**Examples of merged fixes:**
- **Critic:** it accepted a number with the opposite sign, and magnitude words or suffixes ("million", `k`)
  slipped past it.
- **Analysis:** a zero freight baseline silently removed whole findings, rounding could reverse "fell" and
  "rose", and empty results crashed the app.
- **SQL guard:** queries with trailing comments failed to execute.
- **Policy retrieval:** chunks could exceed Oracle's byte limit, and an embedding failure silently changed the
  retrieval backend.
- **Database:** connection pooling.
- **UI:** dropped claims were hidden, and several accessibility fixes, including the cited cell being shown only
  by colour.

**Closed with reasons, for example:**
- a resource-limit change that needed a SYS profile FreeSQL cannot grant
- a runtime lock table
- a fix that hardcoded exact policy sentences

### 4.4 Merge bots

The bots logged:
- **46 merges**
- **61 rejections**
- **11 closures** for failing on the newest `master`
- **9 requeues** after `master` moved

**A tooling error behind 11 rejections.** The bot's own reviewer had run in the read-only sandbox, could not
read files, and rejected sound PRs "because reads were blocked". After the reviewer was moved to diff-only
review in an empty folder, those 11 PRs were reopened and re-reviewed, and several were then merged.

### 4.5 AgentSocial

**Checker calibration:** the original checker confirmed **46 of 46** pieces of evidence. After the prompt
required it to quote the verified sentence verbatim and to judge scope, date, sample size and source, it
confirmed **9** and challenged **43**, with specific reasons. For example: "verbatim quote, but a 96-person lab
study does not support the claim as stated".

**Totals:** 101 agent profiles and 1,586 public posts (711 evidence, 323 confirmations, 296 challenges,
120 verdicts), covering 136 claims. Of 49 tracked claims, 18 settled after their verdicts repeated, 29 were
checked once, and 2 remained contested at pause.

## 5 Failure analysis

**Table 5.** Operational failures and fixes.

| # | Symptom | Cause | Fix |
|---:|---|---|---|
| 1 | Writer agents saw their folder as read-only | Windows `workspace-write` sandbox | `danger-full-access` in an isolated worktree |
| 2 | Reviewers reviewed nothing ("blocked") | auto-approval rejects reads under `read-only` | code in the prompt; empty folder |
| 3 | Prompts broken by `|` and quotes | `codex.cmd` goes through `cmd.exe` | call `node codex.js` directly |
| 4 | `WinError 206` | 32k command-line limit | prompt on stdin (`exec -`) |
| 5 | Runs hung | `exec` waits on stdin | always redirect stdin |
| 6 | Many fixers on the same bug | redundant reviewers | dedupe on (file, ⌊line/10⌋) |
| 7 | Later rounds re-found old bugs | no merge between rounds | merge bots plus rounds on the new `master` |
| 8 | No commits for 20+ minutes | round barrier behind 200 slow reviewers | stream findings straight to fixers |
| 9 | Hundreds of agents exited 15 at once | jobs started inside the orchestrating session ended with it | launch detached and hidden through WMI |
| 10 | Every Astra reviewer exited 15 | Astra at `ultra` effort under `codex exec` | Astra at `max`, Terra at `ultra` |
| 11 | Merge bots rejected sound PRs | bot reviewer was sandboxed | diff-only review; wrongly closed PRs reopened |
| 12 | Fixers failed after a restart | branch numbers reused | number new branches above the highest existing branch |

**Most of the "not completed" runs** in Table 2 come from failures 9 and 10. Their tokens up to the point of
termination are still counted.

## 6 Limitations

- **"Completed"** means Codex logged the end of a turn. Runs cut short still consumed the tokens counted for them.
- **The 200-agent wall time is approximate.** It was taken from file timestamps, because the runner's summary
  step crashed.
- **The model changed mid-run.** The default moved from Sol to Terra, so per-model comparisons aren't controlled
  experiments.
- **Agents wrote the tests.** A passing suite shows internal consistency, not independent correctness. The first
  20 merges were reviewed outside the swarm before merging; later merges relied on the merge bots.
- **One host and one day.** Concurrency limits reflect this machine's RAM and process budget, about 175 MB per
  active agent.

## 7 Conclusion

A single desktop sustained a 200-agent Codex swarm, about 1.6k output tok/s at peak, and spent 1.19B tokens
improving a working Oracle AI Database application. The test suite grew eightfold and every number in the
app stayed traceable to SQL.

The difficult parts were operational, not model quality: process lifetimes, sandboxes and orchestration
barriers. Once those were fixed, the pipeline produced merged, tested changes at a steady rate. Open
agent-to-agent checking was useful only after the checker was made skeptical by default.

## Reproducibility

```powershell
# application
python load.py; python policies.py; python app.py            # http://127.0.0.1:5055
python -m pytest -q
# scaling
python codex_swarm.py --claims claims50.txt --parallel 50 --out run_50
# pipeline (folder layout: <root>\project = this repo, swarm scripts in <root>)
python swarm\repo_loop.py --rounds 1 --per-area 25 --workers 25 --max-fixes 80 --start 800
python swarm\merge_bot.py --reviewers 20
# accounting
python swarm\usage_report.py --cwd-contains "Codex Hackathon Oct28" --out usage.json
```

The key is read from Codex's own login (`codex login --with-api-key`), and no key is stored in this repository.
The orchestration code is in [`swarm/`](swarm), and the raw totals are in
[`swarm/usage_report.json`](swarm/usage_report.json).

## Acknowledgments

The agents ran on OpenAI Codex with the event's API key. The orchestration scripts were developed with AI coding
assistance (Claude Code), consistent with the co-author trailers in the commit history.

## References

1. Oracle. *VECTOR_EMBEDDING* and *VECTOR_DISTANCE*, Oracle AI Database 26ai SQL reference.
   https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/vector_embedding.html
2. Oracle. *Oracle AI Vector Search quick start.*
   https://docs.oracle.com/en/database/oracle/oracle-database/26/vecse/sql-quick-start-using-vector-embedding-model-uploaded-database.html
3. OpenAI. *Codex CLI and app documentation* (exec, sandbox, models). https://learn.chatgpt.com/docs/app
4. Oracle. *SQLcl MCP server.* Oracle SQLcl 26.3 documentation.
