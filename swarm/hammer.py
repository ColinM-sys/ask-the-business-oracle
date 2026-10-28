"""Keep 200 agents working the repo nonstop, until hammer.stop exists:
  round after round: 200 Codex reviewers (8 areas x 25) -> up to 80 fixers -> PRs,
  while merge_bot.py (10 reviewer-mergers, run separately) merges what passes.
Before each new round it waits for the PR queue to drain below --max-open, so reviewers always see the
newest master and find new work instead of re-finding the last round's bugs.
    python hammer.py [--max-open 10]"""
import argparse
import json
import re
import os
import subprocess
import sys
import time

from repo_loop import REPO, sh

HERE = os.path.dirname(os.path.abspath(__file__))
STOP = os.path.join(HERE, "hammer.stop")
R = "ColinM-sys/ask-the-business-oracle"

ap = argparse.ArgumentParser()
ap.add_argument("--max-open", type=int, default=10)
a = ap.parse_args()
def loop_running():
    out = subprocess.run(["powershell", "-c", "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                          "? { $_.CommandLine -like '*repo_loop.py*' } | measure | select -expand Count"],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0) > 0


def next_start():
    # Branch numbers must never repeat (worktree add -b fails on an existing branch): start past the highest.
    names = sh(["git", "branch", "-a", "--format=%(refname:short)"], REPO).stdout.split()
    nums = [int(m.group(1)) for b in names if (m := re.search(r"loop/[a-z]+-(\d+)-", b))]
    return (max(nums, default=0) // 100 + 1) * 100


def workers():
    try:  # tunable live: echo 25 > hammer_workers.txt
        return int(open(os.path.join(HERE, "hammer_workers.txt")).read().strip())
    except (OSError, ValueError):
        return 25


while not os.path.exists(STOP):
    while loop_running() and not os.path.exists(STOP):  # never overlap rounds
        time.sleep(30)
    while True:  # let the merge bots catch up first
        n_open = len(json.loads(sh(["gh", "pr", "list", "-R", R, "--json", "number"], REPO).stdout or "[]"))
        if n_open < a.max_open or os.path.exists(STOP):
            break
        time.sleep(60)
    sh(["git", "checkout", "-q", "master"], REPO)
    sh(["git", "pull", "-q"], REPO)
    sh(["git", "fetch", "-q", "origin"], REPO)
    start = next_start()
    t0 = time.time()
    subprocess.run([sys.executable, os.path.join(HERE, "repo_loop.py"), "--rounds", "1", "--per-area", "25",
                    "--workers", str(workers()), "--max-fixes", "80", "--start", str(start)], cwd=HERE)
    with open(os.path.join(HERE, "hammer.log"), "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} round from branch #{start} done in {time.time() - t0:.0f}s\n")

