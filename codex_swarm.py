"""Fact-check swarm on Codex: one `codex exec` agent per claim, N at a time, using Codex's own login.
    python codex_swarm.py [--claims claims.txt] [--parallel 6]
Writes results/swarm_live/agentN.jsonl, results/swarm_live/summary.md and prints tokens and throughput."""
import argparse
import json
import os
import subprocess
import shutil
import time
from concurrent.futures import ThreadPoolExecutor

PROMPT = ('Fact-check this claim using web search. Find 2+ sources supporting and 2+ against. Quote exact sentences '
          'with URLs, then end with a line "VERDICT: <one of SUPPORTED, MISLEADING, FALSE, UNVERIFIABLE>" and a 2-sentence reason. '
          'Claim: "{claim}"')
CODEX = [shutil.which("node"), os.path.join(os.environ["APPDATA"], "npm", "node_modules", "@openai", "codex", "bin", "codex.js")]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "swarm_live")


EXTRA = []


def run(i, claim):
    path = os.path.join(OUT, f"agent{i}.jsonl")
    t0 = time.time()
    with open(path, "w", encoding="utf-8") as f:
        subprocess.run(CODEX + ["exec", "--skip-git-repo-check", "-s", "read-only", "-c", "tools.web_search=true",
                        *EXTRA, "--json", PROMPT.format(claim=claim)], stdin=subprocess.DEVNULL, stdout=f,
                       stderr=subprocess.STDOUT, timeout=900)
    usage, text, searches = {}, "", 0
    for line in open(path, encoding="utf-8", errors="ignore"):
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("type") == "turn.completed":
            usage = m.get("usage", {})
        item = m.get("item", {})
        if m.get("type") == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text", "")
        if m.get("type") == "item.completed" and item.get("type") == "web_search":
            searches += 1
    verdict = next((l.split(":", 1)[1].strip() for l in text.splitlines() if l.upper().startswith("VERDICT")), "?")
    return {"i": i, "claim": claim, "seconds": round(time.time() - t0), "searches": searches,
            "verdict": verdict, "usage": usage, "answer": text}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--claims", default="claims.txt")
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--out", default="swarm_live")
    ap.add_argument("--effort", help="model_reasoning_effort: minimal/low/medium/high")
    a = ap.parse_args()
    OUT = os.path.join(os.path.dirname(OUT), a.out)
    if a.effort:
        EXTRA += ["-c", f"model_reasoning_effort={a.effort}"]
    os.makedirs(OUT, exist_ok=True)
    claims = [c.strip() for c in open(a.claims, encoding="utf-8") if c.strip() and not c.startswith("#")]
    t0 = time.time()
    with ThreadPoolExecutor(a.parallel) as ex:
        res = list(ex.map(lambda p: run(*p), enumerate(claims, 1)))
    wall = time.time() - t0
    tin = sum(r["usage"].get("input_tokens", 0) for r in res)
    tout = sum(r["usage"].get("output_tokens", 0) + r["usage"].get("reasoning_output_tokens", 0) for r in res)
    with open(os.path.join(OUT, "summary.md"), "w", encoding="utf-8") as f:
        f.write(f"# Codex fact-check swarm\n{len(res)} agents, {a.parallel} parallel, {wall:.0f}s wall, "
                f"{tin:,} input + {tout:,} output tokens\n\n")
        for r in res:
            f.write(f"## {r['verdict']}: {r['claim']}\n({r['seconds']}s, {r['searches']} searches)\n\n{r['answer']}\n\n")
    for r in res:
        print(f"{r['verdict'][:40]:40} {r['seconds']:>4}s {r['searches']:>2} searches  {r['claim'][:70]}")
    print(f"wall {wall:.0f}s | input {tin:,} | output {tout:,} | aggregate output {tout / wall:.1f} tok/s")
