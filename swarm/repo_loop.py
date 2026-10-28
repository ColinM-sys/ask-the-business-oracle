"""Improvement loop on the GitHub repo ColinM-sys/ask-the-business-oracle, run by Codex agents.
Each round:
  1. FIND   - one read-only Codex reviewer per area, in parallel; each returns JSON findings
  2. FIX    - one Codex fixer per finding, each in its own git worktree on its own branch
  3. VERIFY - pytest must pass in that worktree; otherwise the branch is dropped
  4. SHIP   - push the branch and open a PR (never to master). Claude reviews and merges.
Stops after --rounds or when a round finds nothing. Log: loop_rounds.log
    python repo_loop.py [--rounds 3] [--max-fixes 6]"""
import argparse
import json
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, "project")
WT = os.path.join(HERE, "worktrees")
CODEX = [shutil.which("node"), os.path.join(os.environ["APPDATA"], "npm", "node_modules", "@openai", "codex", "bin",
                                            "codex.js")]
AREAS = {
    "correctness": "bugs in agent.py, critic.py, tools.py, policies.py: wrong numbers, wrong SQL, edge cases",
    "security": "the read-only SQL guard in tools.py, secrets handling, injection paths in app.py",
    "tests": "important behaviour in agent.py, critic.py, tools.py that test_app.py does not cover",
    "ux": "templates/index.html and app.py: errors shown to the user, empty states, accessibility",
    "oracle": "Oracle AI Database usage: vector index (HNSW/IVF), VECTOR types, bind variables, connection "
              "pooling, SQL that could use Oracle 26ai features better",
    "performance": "slow paths: repeated queries, embeddings recomputed, missing caching, N+1 database calls",
    "docs": "README.md and docstrings that are wrong, missing setup steps, or out of date with the code",
    "features": "small, clearly useful features the app is missing for a business user (e.g. other regions, "
                "date ranges, export of the brief), each small enough to build and test in one change",
}
USAGE = {"in": 0, "out": 0, "calls": 0}


def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="ignore")


def codex(prompt, cwd, write=False, tries=2, model=None):
    for _ in range(tries):
        text = _codex(prompt, cwd, write, model)
        if text is not None:
            return text
    return ""


def _codex(prompt, cwd, write, model=None):
    r = subprocess.run(CODEX + ["exec", "--skip-git-repo-check", "-s", "danger-full-access" if write else "read-only",
                                *(["-m", model] if model else []),
                                # Astra at "ultra" (task delegation) exits 15 under codex exec; run it at max.
                                *(["-c", "model_reasoning_effort=max"] if model == "gpt-6-astra" else []),
                                "--json", "-"], cwd=cwd, input=prompt, capture_output=True, text=True,
                       encoding="utf-8", errors="ignore", timeout=600)  # prompt on stdin: too long for argv
    text, done = "", False
    for line in r.stdout.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("type") == "item.completed" and m["item"].get("type") == "agent_message":
            text = m["item"]["text"]
        if m.get("type") == "turn.completed":
            done = True
            u = m.get("usage", {})
            USAGE["in"] += u.get("input_tokens", 0)
            USAGE["out"] += u.get("output_tokens", 0)
            USAGE["calls"] += 1
    if not done:  # crashed or rate-limited: log why, caller retries
        USAGE["failed"] = USAGE.get("failed", 0) + 1
        with open(os.path.join(HERE, "codex_failures.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')} rc={r.returncode} {(r.stdout + r.stderr)[-400:]!r}\n")
        return None
    return text


SOURCES = ("README.md", "agent.py", "app.py", "critic.py", "policies.py", "tools.py", "db.py", "test_app.py",
           "templates/index.html")


EMPTY = os.path.join(HERE, "empty_cwd")
os.makedirs(EMPTY, exist_ok=True)


def find(area, focus, done, model=None):
    code = "\n\n".join(f"===== {name} =====\n" + "\n".join(f"{i:4} {l}" for i, l in enumerate(
        open(os.path.join(REPO, name), encoding="utf-8").read().splitlines(), 1)) for name in SOURCES)
    text = codex(f"You are reviewing the code below (it is the whole app; line numbers on the left). Do not run "
                 f"any commands. Look for: {focus}. Report only real, specific problems you can point to "
                 f"(file and line), at most 3, most important first. Skip anything already fixed: {done}. "
                 'Return JSON only: {"findings":[{"title":"...","file":"...","line":0,"problem":"...","fix":"..."}]}'
                 f"\n\n{code}", EMPTY, write=True, model=model)  # no sandbox, empty dir: nothing to touch
    s, e = text.find("{"), text.rfind("}")
    try:
        fs = json.loads(text[s:e + 1]).get("findings", [])
        return [dict(f, area=area) for f in fs if not re.search(r"blocked|denied|could not (read|inspect)",
                                                                   f.get("title", "") + f.get("problem", ""), re.I)]
    except ValueError:
        return []


def fix(n, f):
    branch = f"loop/{f['area']}-{n}-{re.sub(r'[^a-z0-9]+', '-', f['title'].lower())[:30].strip('-')}"
    path = os.path.join(WT, branch.replace("/", "_"))
    sh(["git", "worktree", "add", "-q", "-b", branch, path, "master"], REPO)
    shutil.copy(os.path.join(REPO, ".env"), path)  # local DB settings for tests; .env is gitignored
    codex(f"Fix this problem with the smallest correct change, add or update a test in test_app.py that fails "
          f"without the fix, run `python -m pytest -q` until it passes, then `git add -A` and commit with a clear "
          f"message. Do not touch .env or print secrets.\n\n{json.dumps(f, indent=1)}", path, write=True)
    ok = sh(["python", "-m", "pytest", "-q"], path)
    ahead = sh(["git", "rev-list", "--count", "master..HEAD"], path).stdout.strip()
    result = {"branch": branch, "title": f["title"], "tests": ok.returncode == 0, "commits": ahead}
    if ok.returncode == 0 and ahead not in ("", "0"):
        sh(["git", "push", "-q", "-u", "origin", branch], path)
        pr = sh(["gh", "pr", "create", "--base", "master", "--head", branch, "--title", f["title"][:70], "--body",
                 f"Found by the Codex improvement loop ({f['area']}).\n\n**Problem:** {f['problem']}\n\n"
                 f"**Location:** {f['file']}:{f.get('line')}\n\nTests pass in the branch worktree.\n\n"
                 "🤖 Generated with [Claude Code](https://claude.com/claude-code)"], path)
        result["pr"] = pr.stdout.strip()
    sh(["git", "worktree", "remove", "--force", path], REPO)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--max-fixes", type=int, default=6)
    ap.add_argument("--per-area", type=int, default=1, help="reviewers per area, each with a different lens")
    ap.add_argument("--workers", type=int, default=100, help="max Codex reviewers running at once")
    ap.add_argument("--models", default="gpt-6-astra,gpt-6.1-terra", help="reviewers alternate between these")
    ap.add_argument("--start", type=int, default=0, help="first branch number (avoid reusing old branch names)")
    a = ap.parse_args()
    os.makedirs(WT, exist_ok=True)
    lenses = ["", "Think like an attacker.", "Think like a new user.", "Focus on edge cases and empty inputs.",
              "Focus on numbers and rounding.", "Focus on error handling.", "Focus on what the tests miss.",
              "Focus on Oracle SQL specifics.", "Focus on concurrency and state.", "Focus on data the docs promise."]
    models = a.models.split(",")
    jobs = [(area, f"{focus}. {lenses[k % len(lenses)]}", models[k % len(models)])
            for area, focus in AREAS.items() for k in range(a.per_area)]
    done = []
    n = a.start
    for rnd in range(a.rounds):
        t0 = time.time()
        # Pipeline: each reviewer's findings go straight to fixers, no waiting for all reviewers.
        seen, futures, lock = set(), [], threading.Lock()
        logpath = os.path.join(HERE, "loop_rounds.log")
        with ThreadPoolExecutor(30) as fixers:
            def review_then_fix(j):
                global n
                for f in find(j[0], j[1], done, j[2]):
                    key = (str(f.get("file", "")).split("/")[-1], int(f.get("line") or 0) // 10)
                    with lock:  # one fix per (file, ~line): many reviewers find the same bug
                        if key in seen or len(seen) >= a.max_fixes:
                            continue
                        seen.add(key)
                        n += 1
                        num = n
                    futures.append(fixers.submit(lambda f=f, num=num: log_fix(fix(num, f))))

            def log_fix(r):
                with lock, open(logpath, "a", encoding="utf-8") as fh:
                    fh.write(f"  {time.strftime('%H:%M:%S')} tests={'pass' if r['tests'] else 'FAIL'} "
                             f"{r.get('pr', 'no PR')} {r['title']}\n")
                return r

            with ThreadPoolExecutor(min(len(jobs), a.workers)) as reviewers:
                list(reviewers.map(review_then_fix, jobs))
            results = [fu.result() for fu in futures]
        done += [r["title"] for r in results]
        with open(logpath, "a", encoding="utf-8") as fh:
            fh.write(f"round {rnd} {time.strftime('%H:%M:%S')} {time.time() - t0:.0f}s {len(jobs)} reviewers, "
                     f"{len(results)} fixes, {sum(r['tests'] for r in results)} passed, usage={USAGE}\n")
    print(json.dumps(USAGE))
