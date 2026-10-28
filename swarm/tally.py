"""Running tally of event-key usage: every Codex run logged under project/results (swarm runs, app build) and
AgentSocial rounds. Prints totals and the largest number of agents run at once.
    python tally.py      (also writes TALLY.txt next to this file)"""
import glob
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
tin = tout = calls = 0
runs = []
for path in glob.glob(os.path.join(HERE, "project", "results", "**", "*.jsonl"), recursive=True):
    for line in open(path, encoding="utf-8", errors="ignore"):
        if '"turn.completed"' in line:
            try:
                u = json.loads(line).get("usage", {})
            except ValueError:
                continue
            tin += u.get("input_tokens", 0)
            tout += u.get("output_tokens", 0) + u.get("reasoning_output_tokens", 0)
            calls += 1
for d in glob.glob(os.path.join(HERE, "project", "results", "run_*_agents*")):
    m = re.search(r"run_(\d+)_agents", d)
    runs.append(int(m.group(1)))

log = os.path.join(HERE, "agent_social", "rounds.log")
s_in = s_out = s_calls = rounds = 0
if os.path.exists(log):
    text = open(log, encoding="utf-8").read()
    rounds = text.count("round ")
    s_in = sum(int(x) for x in re.findall(r'"in": (\d+)', text))
    s_out = sum(int(x) for x in re.findall(r'"out": (\d+)', text))
    s_calls = sum(int(x) for x in re.findall(r'"calls": (\d+)', text))

out = (f"EVENT KEY TALLY\n"
       f"swarm/app runs : {calls:,} Codex calls, {tin:,} in + {tout:,} out tokens\n"
       f"AgentSocial    : {rounds} rounds (continuous), {s_calls:,} calls, {s_in:,} in + {s_out:,} out tokens\n"
       f"TOTAL          : {calls + s_calls:,} calls, {tin + s_in + tout + s_out:,} tokens\n"
       f"max agents at once: {max(runs) if runs else 0} (runs: {sorted(set(runs))})\n")
print(out)
open(os.path.join(HERE, "TALLY.txt"), "w", encoding="utf-8").write(out)
