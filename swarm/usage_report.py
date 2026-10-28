"""Account for every Codex run of the hackathon from Codex's own session logs (~/.codex/sessions).
Each rollout file is one `codex exec` call. Reports calls, completed vs cut short, and tokens by component and
model. Writes usage_report.json and prints Markdown tables.
    python usage_report.py [--date 2026/10/28]"""
import argparse
import glob
import json
import os
from collections import defaultdict

COMPONENTS = [  # first matching prompt prefix wins
    ("Create a file named hello.txt", "Setup tests"),
    ("Using the sqlcl MCP server", "Setup tests"),
    ("Reply OK", "Setup tests"),
    ("Say OK", "Setup tests"),
    ("Without using any tools, write about 600 words", "Setup tests"),
    ("List the files in this folder", "Setup tests"),
    ("Think carefully", "Setup tests"),
    ("Read AGENTS.md and build", "App build (Codex)"),
    ("Fact-check this claim", "Swarm scaling test"),
    ("Find the 2 strongest pieces of evidence", "AgentSocial"),
    ("Another agent (", "AgentSocial"),
    ("Here is a public discussion between agents", "AgentSocial"),
    ("Review this repository", "Repo loop: reviewers"),
    ("You are reviewing the code below", "Repo loop: reviewers"),
    ("Review this code for bugs", "Repo loop: reviewers"),
    ("Fix this problem with the smallest correct change", "Repo loop: fixers"),
    ("This branch has merge conflicts with master", "Merge bots"),
    ("You are a strict code reviewer for this repository", "Merge bots"),
]


def first_prompt(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"user_message"' in line or '"role":"user"' in line:
                try:
                    p = json.loads(line)["payload"]
                except (ValueError, KeyError):
                    continue
                msg = p.get("message") or "".join(c.get("text", "") for c in p.get("content", []) or [])
                if msg and not msg.lstrip().startswith(("<", "# AGENTS.md")):  # skip injected context
                    return msg
    return ""


def scan(path):
    model, usage, completed, cwd = None, {}, False, ""
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"turn_context"' in line and model is None:
                try:
                    model = json.loads(line)["payload"].get("model")
                except (ValueError, KeyError):
                    pass
            elif '"session_meta"' in line and not cwd:
                try:
                    cwd = json.loads(line)["payload"].get("cwd", "")
                except (ValueError, KeyError):
                    pass
            elif '"token_count"' in line:
                try:
                    info = json.loads(line)["payload"].get("info") or {}
                    usage = info.get("total_token_usage") or usage
                except (ValueError, KeyError):
                    pass
            elif '"task_complete"' in line or '"turn_complete"' in line:
                completed = True
    return model or "?", usage, completed, cwd


def component(prompt, cwd):
    for prefix, name in COMPONENTS:
        if prompt.lstrip().startswith(prefix) or prefix in prompt[:400]:
            return name
    if "\\worktrees\\review_" in cwd:
        return "Merge bots"
    if "\\worktrees\\loop" in cwd:
        return "Repo loop: fixers"
    return "Other"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026/10/28")
    a = ap.parse_args()
    files = sorted(glob.glob(os.path.join(os.path.expanduser("~"), ".codex", "sessions", *a.date.split("/"),
                                          "rollout-*.jsonl")))
    by = defaultdict(lambda: defaultdict(int))
    models = defaultdict(lambda: defaultdict(int))
    first = last = None
    for path in files:
        stamp = os.path.basename(path)[8:27]
        first, last = first or stamp, stamp
        model, u, done, cwd = scan(path)
        comp = component(first_prompt(path), cwd)
        for d in (by[comp], by["TOTAL"], models[model]):
            d["calls"] += 1
            d["completed"] += done
            d["input"] += u.get("input_tokens", 0)
            d["cached"] += u.get("cached_input_tokens", 0)
            d["output"] += u.get("output_tokens", 0)
            d["reasoning"] += u.get("reasoning_output_tokens", 0)
            d["total"] += u.get("total_tokens", 0)
    out = {"files": len(files), "first": first, "last": last,
           "by_component": {k: dict(v) for k, v in by.items()}, "by_model": {k: dict(v) for k, v in models.items()}}
    json.dump(out, open("usage_report.json", "w"), indent=1)

    def table(rows, label):
        print(f"| {label} | Codex calls | completed | input tokens | (cached) | output tokens | (reasoning) | total |")
        print("|---|---:|---:|---:|---:|---:|---:|---:|")
        for k, v in rows:
            print(f"| {k} | {v['calls']:,} | {v['completed']:,} | {v['input']:,} | {v['cached']:,} | "
                  f"{v['output']:,} | {v['reasoning']:,} | {v['total']:,} |")
    print(f"{len(files)} Codex runs from {first} to {last}\n")
    table(sorted(((k, v) for k, v in by.items() if k != "TOTAL"), key=lambda kv: -kv[1]["total"])
          + [("**TOTAL**", by["TOTAL"])], "Component")
    print()
    table(sorted(models.items(), key=lambda kv: -kv[1]["total"]), "Model")
