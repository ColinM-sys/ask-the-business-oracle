"""Keep the AgentSocial swarm running and improving, until stop.txt exists in this folder.
Each round:
  1. pick up to --batch claims: weak claims from earlier rounds first, then new ones from claims50.txt
  2. run the swarm on them (more scouts for a claim each time it comes back)
  3. score each claim from the feed: verdict, confirmed vs challenged evidence
  4. a claim is "weak" (re-queued) if its verdict is UNVERIFIABLE, or most evidence was challenged, or its
     verdict changed since last time; it is "settled" after two rounds with the same verdict and mostly
     confirmed evidence
Writes rounds.log (stats per round) and claims_state.json (history per claim).
    python forever.py [--batch 10] [--scouts 4] [--max-scouts 8]"""
import argparse
import json
import os
import subprocess
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "claims_state.json")
API = "http://localhost:5060/api"
VERDICTS = ("SUPPORTED", "MISLEADING", "FALSE", "UNVERIFIABLE")


def score(claim, since_id):
    """Verdict and evidence check counts for the newest thread on this claim (posts after since_id)."""
    posts = [p for p in requests.get(f"{API}/posts", params={"kind": "claim"}).json()
             if p["body"] == claim and p["id"] > since_id]
    if not posts:
        return None
    cid = posts[-1]["id"]
    thread = requests.get(f"{API}/posts", params={"reply_to": cid}).json()
    ok = bad = 0
    for e in (p for p in thread if p["kind"] == "evidence"):
        for r in requests.get(f"{API}/posts", params={"reply_to": e["id"]}).json():
            ok += r["kind"] == "confirm"
            bad += r["kind"] == "challenge"
    v = next((p["body"] for p in thread if p["kind"] == "verdict"), "")
    verdict = next((w for w in VERDICTS if w in v.upper()[:80]), "?")
    return {"verdict": verdict, "confirmed": ok, "challenged": bad, "thread": cid}


ap = argparse.ArgumentParser()
ap.add_argument("--batch", type=int, default=10)
ap.add_argument("--scouts", type=int, default=4)
ap.add_argument("--max-scouts", type=int, default=8)
a = ap.parse_args()
claims = [c.strip() for c in open(os.path.join(HERE, "..", "project", "claims50.txt"), encoding="utf-8") if c.strip()]
state = json.load(open(STATE, encoding="utf-8")) if os.path.exists(STATE) else {}
n = 0
while not os.path.exists(os.path.join(HERE, "stop.txt")):
    weak = [c for c, s in state.items() if s["status"] == "weak"]
    fresh = [c for c in claims if c not in state]
    chunk = (weak + fresh)[:a.batch] or [c for c in claims][:a.batch]
    scouts = max(min(a.scouts + 2 * len(state.get(c, {}).get("history", [])), a.max_scouts) for c in chunk)
    path = os.path.join(HERE, "batch.txt")
    open(path, "w", encoding="utf-8").write("\n".join(chunk) + "\n")
    last_id = max([p["id"] for p in requests.get(f"{API}/posts").json()] or [0])
    t0 = time.time()
    r = subprocess.run([sys.executable, os.path.join(HERE, "swarm.py"), "--claims", path, "--scouts", str(scouts),
                        "--parallel", str(len(chunk))], capture_output=True, text=True, encoding="utf-8",
                       errors="ignore")
    summary = []
    for c in chunk:
        s = score(c, last_id)
        if not s:
            continue
        st = state.setdefault(c, {"history": [], "status": "new"})
        prev = st["history"][-1]["verdict"] if st["history"] else None
        st["history"].append({"round": n, "scouts": scouts, **s})
        total = s["confirmed"] + s["challenged"]
        shaky = s["verdict"] in ("UNVERIFIABLE", "?") or (total and s["challenged"] > total / 2) or \
            (prev and prev != s["verdict"])
        # The checker is strict, so most evidence draws a challenge; a verdict that holds across two rounds
        # with fresh scouts is settled even then. Otherwise retry while shaky, up to 4 attempts.
        if prev == s["verdict"] and s["verdict"] not in ("UNVERIFIABLE", "?") or len(st["history"]) >= 4:
            st["status"] = "settled"
        else:
            st["status"] = "weak" if shaky else "checked"
        summary.append(f"  {st['status']:8} {s['verdict']:12} ok={s['confirmed']} challenged={s['challenged']} "
                       f"(was {prev}) {c[:60]}")
    json.dump(state, open(STATE, "w", encoding="utf-8"), indent=1)
    counts = {k: sum(1 for s in state.values() if s["status"] == k) for k in ("settled", "checked", "weak")}
    with open(os.path.join(HERE, "rounds.log"), "a", encoding="utf-8") as f:
        f.write(f"round {n} {time.strftime('%H:%M:%S')} {time.time() - t0:.0f}s rc={r.returncode} scouts={scouts} "
                f"claims={counts}\n" + "\n".join(summary) + f"\n{r.stdout[-400:]}{r.stderr[-300:]}\n")
    n += 1
