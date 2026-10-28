# Hackathon report: Codex + Oracle AI Database Hackathon

Oracle AI World, Las Vegas, October 28, 2026. Built by Colin McDonough, with Claude (Opus 5.5) orchestrating
OpenAI Codex agents on the event's API key. All times below are US Eastern (the build machine's clock).

## Summary

| | |
|---|---|
| Codex runs (agents) | **6,704** `codex exec` runs, 12:36 to 14:10 ET, and still running |
| Tokens | **656.4M** total: 648.7M input (523.7M of it cached) and 7.7M output (5.2M of it reasoning) |
| Most agents at once | **200** in the scaling test; the repo loop puts 200 reviewers through every round |
| Throughput at peak | about **1,630 output tokens/s** across 200 parallel agents, with zero rate-limit errors from the API |
| Models | GPT-6.1-Sol, GPT-6-Astra and GPT-6.1-Terra; Terra and Astra at the highest effort that ran reliably |
| Pull requests | **73** opened by agents: 34 merged, 33 closed with a reason, 6 waiting for the merge bots |
| Commits on `master` | **93** |
| Tests | **42 → 213**, all passing, every one written by an agent alongside the change it tests |
| AgentSocial | 101 agent profiles, 1,434 public posts, 121 claims fact-checked |

The numbers come from Codex's own session logs (`~/.codex/sessions`, one file per run), counted by
[`swarm/usage_report.py`](swarm/usage_report.py). The raw totals are in [`swarm/usage_report.json`](swarm/usage_report.json).

## What we built

**Ask the Business** is a Flask app on Oracle AI Database 26ai. It answers questions like "Why did West margin fall
since July 2025?" with a brief in which every number links to the SQL and the exact result cell it came from.

- **SQL supplies the numbers.** The read-only guard parses each query, allows one SELECT on approved tables and
  functions only, and runs it inside a read-only transaction.
- **AI Vector Search supplies the reasons.** Policy documents are stored as `VECTOR` columns and searched with
  `VECTOR_DISTANCE`.
- **A deterministic critic** drops any claim containing a number that doesn't match its cited cell.
- **Result:** West margin fell from 38.89% to 25.53%, down 13.36 points.

The app runs against local Oracle AI Database 26ai Free. A FreeSQL.com schema is set up for Codex to use
through the **SQLcl MCP server**. As setup tests, Codex created a table there and read it back over MCP, and it
created `hello.txt`.

Codex built the first version from [`AGENTS.md`](AGENTS.md) in one run (`7fe9ecd`: 42 tests). Everything after
that was done by the agent pipeline below.

## How the agents work

```
200 reviewers (8 areas x 25: correctness, security, tests, UX, Oracle, performance, docs, features)
   | every finding goes straight to a fixer (pipeline; no waiting for the whole round)
   v
deduplicate by (file, ~line)  ->  up to 80 fixers, each in its own git worktree and branch
   | the fixer must add a test that fails without the fix, and the full suite must pass
   v
pull request  ->  20 merge bots: merge latest master, run tests, strict Codex review, merge one at a time
   |
   v
next round reviews the new master, so it finds new work instead of the last round's bugs
```

- [`swarm/repo_loop.py`](swarm/repo_loop.py): reviewers and fixers.
- [`swarm/merge_bot.py`](swarm/merge_bot.py): reviewer-mergers.
- [`swarm/hammer.py`](swarm/hammer.py): runs rounds nonstop.
- [`swarm/watchdog.py`](swarm/watchdog.py): alerts on failure bursts, stalls, or no commits.
- [`swarm/launch_detached.ps1`](swarm/launch_detached.ps1): runs jobs hidden and detached so they survive.

Reviewers get the whole app in their prompt and run in an empty folder, so they can't change anything. Fixers
and merge bots work only in their own worktrees.

### What the merge bots refuse

These rules come from PRs that were closed during review:
- changes that need manual DBA/SYS setup (one PR made every query depend on a SYS resource profile that FreeSQL
  can't provide)
- runtime schema changes for locking
- hardcoding sentences from the data files
- anything that weakens the SQL guard or the critic

## Calls and tokens

### By component

| Component | Codex calls | completed | input tokens | (cached) | output tokens | (reasoning) | total |
|---|---:|---:|---:|---:|---:|---:|---:|
| Repo loop: reviewers | 3,856 | 1,305 | 238,366,198 | 193,977,668 | 5,183,702 | 4,466,031 | 243,549,900 |
| AgentSocial | 1,626 | 1,554 | 240,405,827 | 188,867,320 | 1,203,560 | 315,232 | 241,609,387 |
| Swarm scaling test | 442 | 441 | 74,285,317 | 54,041,597 | 503,692 | 80,333 | 74,789,009 |
| Repo loop: fixers | 189 | 178 | 58,907,093 | 54,045,763 | 509,925 | 213,483 | 59,417,018 |
| Merge bots | 167 | 158 | 35,005,186 | 31,947,157 | 283,701 | 156,216 | 35,288,887 |
| Killed before the prompt was logged | 404 | 42 | 677,335 | 65,260 | 1,440 | 38 | 678,775 |
| App build (Codex) | 1 | 1 | 524,021 | 481,808 | 14,655 | 1,739 | 538,676 |
| Setup tests | 19 | 19 | 502,779 | 234,119 | 2,323 | 33 | 505,102 |
| **Total** | **6,704** | **3,698** | **648,673,756** | **523,660,692** | **7,702,998** | **5,233,105** | **656,376,754** |

### By model

| Model | Codex calls | completed | total tokens | output tokens |
|---|---:|---:|---:|---:|
| GPT-6.1-Terra | 2,322 | 1,143 | 338,636,980 | 5,278,530 |
| GPT-6.1-Sol (Codex default, used until 13:40) | 2,411 | 2,013 | 173,274,764 | 1,190,511 |
| GPT-6-Astra | 1,604 | 542 | 144,465,010 | 1,233,957 |
| (killed before the model was logged) | 367 | 0 | 0 | 0 |

**About the "completed" column:** a run counts as completed when Codex logged its end. Runs cut short still used
the tokens counted above. There were two causes:
1. Jobs started from the orchestrating session were killed whenever that session's own background commands
   ended. Fixed by launching every job detached and hidden through WMI.
2. GPT-6-Astra at `ultra` effort ("maximum reasoning with automatic task delegation") exits with code 15 under
   `codex exec`, even when it runs alone. Fixed by running Astra at `max` and Terra at `ultra`.

## Swarm scaling test

Agents fact-checked claims with web search ([`codex_swarm.py`](codex_swarm.py)), all running at once:

| Agents | Wall time | Output tok/s (all agents) | Finished |
|---:|---:|---:|---:|
| 6 | 46 s | 181 | 6/6 |
| 10 | 41 s | 280 | 10/10 |
| 20 | 41 s | 625 | 20/20 |
| 50 | 59 s | 1,091 | 50/50 |
| 100 | 110 s | 1,228 | 100/100 |
| 200 | ~165 s | ~1,630 | 199/200 |

- **The API never rate-limited.** Under load it slowed down instead of refusing requests.
- **The limit was the machine.** About 200 Codex processes ran at once; the one failure at 200 was Windows
  running out of a system resource.
- **Single request:** about 42 output tok/s, including startup.
- **Fixed overhead:** every Codex call starts with about 13.4k input tokens, Codex's own instructions.
- **Lowering reasoning effort didn't help:** low effort used the same tokens and time, and 3 of 50 verdicts
  changed.

## Repo improvement results

| Stage | What happened | Tests |
|---|---|---:|
| Codex build | First version of the app from `AGENTS.md` | 42 |
| Rounds 1-2 | 4 reviewers per round: 12 PRs, 2 duplicates closed, 10 merged in #13 (one conflict resolved by hand) | 93 |
| 200-reviewer round | 277 findings, 24 unique. 15 fixes passed: 10 merged in #29, 5 closed with reasons | 137 |
| Nonstop rounds with merge bots | Rounds of 200 reviewers, then fixers, then merge bots. Example rounds: 20/20 and 26/26 fixes passed | 213 |

Merged fixes include:
- **Critic:** it accepted a number with the opposite sign, and magnitude suffixes or words such as "million"
  slipped past the number check.
- **Analysis:** a zero freight baseline silently removed whole findings, rounding could reverse "fell" and
  "rose", and empty results crashed the app.
- **SQL guard:** queries with trailing comments failed to run.
- **Policy search:** policy chunks could go over Oracle's byte limit, and a failed embedding silently switched
  the retrieval backend.
- **Web page:** dropped claims were hidden, and there were accessibility fixes, including the cited cell being
  shown only by colour.
- **Database:** connection pooling.

The full history is in the closed and merged pull requests.

## AgentSocial: agents coordinating in the open

[`swarm/agent_social/`](swarm/agent_social) is a local social network where agents coordinate through public
profile cards and posts:
- **Scouts** post evidence for or against a claim.
- **Checkers** find the scouts through their profiles and reply CONFIRMED or CHALLENGE.
- **Judges** read the thread and post a verdict.

A loop requeues weak claims with more scouts until the verdict settles.

**The checker fix:** at first the checkers rubber-stamped everything, confirming 46 pieces of evidence and
challenging 0. After a prompt change they had to quote the exact sentence they verified and judge scope, date,
sample size and source. Then they confirmed 9 and challenged 43, with specific reasons such as "verbatim quote,
but a 96-person lab study does not support the claim as stated".

**Totals:** 101 agent profiles, 1,434 posts (647 evidence, 306 confirmations, 255 challenges, 105 verdicts), and 121
claims checked. Of the tracked claims, 13 settled after verdicts repeated, 28 were checked once, and 1 was still
weak.

## Engineering log: problems found and fixed

| Problem | Fix |
|---|---|
| The Windows `workspace-write` sandbox reported the folder as read-only | Run writers with `danger-full-access`, each in its own worktree |
| Codex's automatic approval blocked reviewers from reading files | Put the code in the prompt and run reviewers in an empty folder with no tools |
| Prompts containing `|` and quotes broke under `cmd.exe` (`codex.cmd`) | Call `node codex.js` directly |
| A prompt containing the whole app was too long for the Windows command line | Send prompts on stdin (`codex exec -`) |
| `codex exec` waited on stdin and hung | Always redirect stdin (`< /dev/null`) |
| Many reviewers found the same bug | Deduplicate by (file, line ÷ 10) before fixing |
| Repeat rounds re-found the same bugs | Merge between rounds, so every round reviews the new master |
| Rounds waited for all 200 reviewers before fixing anything | A pipeline: each finding starts its fixer immediately |
| About 500 agents killed together (exit 15) when the session's commands ended | Launch jobs detached and hidden through WMI; [`watchdog.py`](swarm/watchdog.py) alerts on bursts |
| GPT-6-Astra at `ultra` exits 15 in `codex exec` | Astra at `max`, Terra at `ultra` |
| AgentSocial alone ran up to 80 agents | Capped to about 12 so the repo loop gets the machine |
| Checkers approved everything | A skeptical checker prompt that must quote what it verified |

## Reproduce

```powershell
# app
python load.py; python policies.py; python app.py        # http://127.0.0.1:5055
python -m pytest -q
# swarm (folder layout: <root>\project = this repo, <root>\swarm scripts next to it)
python codex_swarm.py --claims claims50.txt --parallel 50 --out run_50
python swarm\repo_loop.py --rounds 1 --per-area 25 --workers 45 --max-fixes 80 --start 400
python swarm\merge_bot.py --reviewers 20
python swarm\usage_report.py                              # calls and tokens from ~/.codex/sessions
```

Codex reads its key from its own login (`codex login --with-api-key`). No key is stored in this repo.
