"""10 Codex reviewer-mergers that keep the repo's PR queue moving, until merge_bot.stop exists.
For each open PR (up to --reviewers at once):
  1. fresh worktree of the PR branch, merge current master into it (Codex resolves conflicts if any)
  2. full test suite must pass
  3. a Codex reviewer reads the diff against master and returns APPROVE or REJECT with a reason
  4. APPROVE + tests pass -> push the updated branch, post the review with the real test output, merge
     REJECT -> comment with the reason and close; tests fail -> comment with the failure and close
Merges are one at a time (a lock), and each re-checks against the newest master first.
Log: merge_bot.log
    python merge_bot.py [--reviewers 10]"""
import argparse
import json
import os
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from repo_loop import REPO, WT, codex, sh

HERE = os.path.dirname(os.path.abspath(__file__))
STOP = os.path.join(HERE, "merge_bot.stop")
MERGE_LOCK = threading.Lock()
SEEN = set()
APPROVED = {}  # PR number -> approval reason, so a requeue skips the second Codex review
R = "ColinM-sys/ask-the-business-oracle"


def log(msg):
    with open(os.path.join(HERE, "merge_bot.log"), "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def tests(path):
    r = sh(["python", "-m", "pytest", "-q"], path)
    return r.returncode == 0, (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]


def review(pr):
    n, branch, title = pr["number"], pr["headRefName"], pr["title"]
    path = os.path.join(WT, f"review_{n}")
    sh(["git", "fetch", "-q", "origin"], REPO)
    sh(["git", "worktree", "add", "-q", "-f", path, f"origin/{branch}"], REPO)
    try:
        sh(["git", "checkout", "-q", "-B", branch], path)
        shutil.copy(os.path.join(REPO, ".env"), path)
        if sh(["git", "merge", "-q", "--no-edit", "origin/master"], path).returncode:
            codex("This branch has merge conflicts with master. Resolve every conflict keeping the intent of both "
                  "sides, run `python -m pytest -q` until it passes, then `git add -A` and `git commit --no-edit`.",
                  path, write=True)
            if sh(["git", "diff", "--name-only", "--diff-filter=U"], path).stdout.strip():
                sh(["gh", "pr", "comment", str(n), "-R", R, "--body", "merge-bot: could not resolve conflicts with "
                    "master; leaving open for a person."], path)
                log(f"#{n} conflicts unresolved")
                return
        ok, out = tests(path)
        if not ok:
            sh(["gh", "pr", "close", str(n), "-R", R, "-c", f"merge-bot: tests fail after merging master: `{out[0]}`"],
               path)
            log(f"#{n} closed: tests fail {out[0]}")
            return
        diff = sh(["git", "diff", "origin/master...HEAD"], path).stdout[:60000]
        text = '{"verdict":"APPROVE","reason":"%s"}' % APPROVED[n] if n in APPROVED else codex("You are a strict code reviewer for this repository. Below is a pull request diff against "
                     "master. Approve only if the change is correct, small and focused, has a test for what it "
                     "changes, does not weaken the read-only SQL guard or the critic, needs no manual DBA/admin "
                     "setup, and does not hardcode content from data files. Return JSON only: "
                     '{"verdict":"APPROVE or REJECT","reason":"one or two sentences"}'
                     f"\n\nPR #{n}: {title}\n\n{diff}", path)
        s, e = text.find("{"), text.rfind("}")
        try:
            v = json.loads(text[s:e + 1])
        except ValueError:
            v = {"verdict": "REJECT", "reason": "reviewer returned no verdict"}
        if not str(v.get("verdict", "")).upper().startswith("APPROVE"):
            sh(["gh", "pr", "close", str(n), "-R", R, "-c", f"merge-bot review: REJECT. {v.get('reason', '')}"], path)
            log(f"#{n} rejected: {v.get('reason', '')}")
            return
        APPROVED[n] = str(v.get("reason", "")).replace('"', "'")
        with MERGE_LOCK:  # one merge at a time, re-checked against the newest master
            sh(["git", "fetch", "-q", "origin"], path)
            if sh(["git", "merge", "-q", "--no-edit", "origin/master"], path).returncode:
                sh(["git", "merge", "--abort"], path)
                log(f"#{n} master moved and now conflicts; requeued")
                SEEN.discard(n)  # picked up again next poll, resolved outside the lock
                return
            ok, out = tests(path)
            if not ok:
                sh(["gh", "pr", "close", str(n), "-R", R, "-c", f"merge-bot: tests fail on newest master: `{out[0]}`"],
                   path)
                log(f"#{n} closed: tests fail on newest master")
                return
            sh(["git", "push", "-q", "origin", f"HEAD:{branch}"], path)
            sh(["gh", "pr", "comment", str(n), "-R", R, "--body",
                f"merge-bot review: APPROVE. {v.get('reason', '')}\n\nFull suite on this branch merged with "
                f"master: `{out[0]}`"], path)
            m = sh(["gh", "pr", "merge", str(n), "-R", R, "--merge"], path)
            log(f"#{n} merged rc={m.returncode} ({out[0]}) {title}")
    finally:
        sh(["git", "worktree", "remove", "--force", path], REPO)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reviewers", type=int, default=20)
    a = ap.parse_args()
    os.makedirs(WT, exist_ok=True)
    with ThreadPoolExecutor(a.reviewers) as ex:
        while not os.path.exists(STOP):
            prs = json.loads(sh(["gh", "pr", "list", "-R", R, "--state", "open", "--limit", "200", "--json",
                                 "number,headRefName,title"], REPO).stdout or "[]")
            new = [p for p in prs if p["number"] not in SEEN and re.match(r"loop/", p["headRefName"])]
            for p in new:
                SEEN.add(p["number"])
                ex.submit(review, p)
            time.sleep(10)
