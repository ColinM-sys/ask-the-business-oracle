"""Run the fact-check swarm on AgentSocial, all in the open.
    python swarm.py --claims ../project/claims10.txt --scouts 2 --parallel 30
Each agent posts its profile card, then:
  scouts   read the claim posts, research with web search (Codex), post evidence as replies
  checkers find scouts' evidence by reading profiles + feed, post challenges or confirmations
  judges   read the whole thread and post the verdict
Agents are Codex runs (event key via Codex's own login)."""
import argparse
import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import requests

API = "http://localhost:5060/api"
CODEX = [shutil.which("node"), os.path.join(os.environ["APPDATA"], "npm", "node_modules", "@openai", "codex", "bin",
                                            "codex.js")]
TOKENS = {"in": 0, "out": 0, "calls": 0}


def codex(prompt, web=False):
    args = CODEX + ["exec", "--skip-git-repo-check", "-s", "read-only", "--json"]
    if web:
        args += ["-c", "tools.web_search=true"]
    r = subprocess.run(args + [prompt], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                       errors="ignore", timeout=900)
    text = ""
    for line in r.stdout.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("type") == "item.completed" and m["item"].get("type") == "agent_message":
            text = m["item"]["text"]
        if m.get("type") == "turn.completed":
            u = m.get("usage", {})
            TOKENS["in"] += u.get("input_tokens", 0)
            TOKENS["out"] += u.get("output_tokens", 0)
            TOKENS["calls"] += 1
    return text.strip()


def profile(name, role, skills, task):
    requests.post(f"{API}/profiles", json=dict(name=name, role=role, skills=skills, task=task))


def post(author, body, topic, kind="post", reply_to=None):
    return requests.post(f"{API}/posts", json=dict(author=author, body=body, topic=topic, kind=kind,
                                                   reply_to=reply_to)).json()["id"]


def thread(pid):
    return requests.get(f"{API}/posts", params={"reply_to": pid}).json()


def scout(name, claim_id, claim, topic, side):
    profile(name, "scout", "web research, primary sources", f"finding evidence {side} claim #{claim_id}")
    body = codex(f'Find the 2 strongest pieces of evidence {side} this claim using web search. For each: exact quote, '
                 f'URL, one line on why it matters. Under 150 words. Claim: "{claim}"', web=True)
    return post(name, body, topic, "evidence", claim_id)


def checker(name, claim_id, claim, topic):
    scouts = [p["name"] for p in requests.get(f"{API}/profiles", params={"role": "scout"}).json()
              if f"#{claim_id}" in p["task"]]
    profile(name, "checker", "source verification", f"checking evidence from {', '.join(scouts)} on #{claim_id}")
    ev = [p for p in thread(claim_id) if p["kind"] == "evidence"]
    for e in ev:
        body = codex(f'Another agent ({e["author"]}) posted this evidence about the claim "{claim}". You are a skeptical '
                     f'reviewer. Open each URL with web search. For EACH quote: is the exact sentence on the page? Does '
                     f'the source actually show what the scout says it shows (the scout may be arguing for OR against the claim; '
                     f'judge the evidence against the scout\'s own point, not against the claim) - check scope, date, '
                     f'sample size, who measured it? '
                     f'Is it a primary source or a blog repeating someone else? Default to CHALLENGE: reply CONFIRMED '
                     f'only if every quote is verbatim on its page AND directly supports the point, and then quote the '
                     f'sentence you found. Otherwise reply CHALLENGE and name the specific problem. Under 100 words, '
                     f'starting with CONFIRMED or CHALLENGE.\n\n{e["body"]}', web=True)
        post(name, body, topic, "challenge" if body.upper().startswith("CHALLENGE") else "confirm", e["id"])


def judge(name, claim_id, claim, topic):
    profile(name, "judge", "weighing evidence", f"verdict on #{claim_id}")
    posts = thread(claim_id)
    replies = [r for p in posts for r in thread(p["id"])]
    convo = "\n\n".join(f'[{p["kind"]} by {p["author"]}] {p["body"]}' for p in posts + replies)
    body = codex(f'Here is a public discussion between agents about the claim "{claim}". Give a verdict: SUPPORTED, '
                 f'MISLEADING, FALSE or UNVERIFIABLE, then 2 sentences citing which agents\' evidence you relied on.'
                 f'\n\n{convo}')
    post(name, body, topic, "verdict", claim_id)


def run_claim(i, claim, scouts):
    topic = f"claim-{i}"
    cid = post("lead", claim, topic, "claim")
    with ThreadPoolExecutor(scouts) as ex:
        list(ex.map(lambda k: scout(f"scout-{i}-{k}", cid, claim, topic, "supporting" if k % 2 == 0 else "against"),
                    range(scouts)))
    checker(f"checker-{i}", cid, claim, topic)
    judge(f"judge-{i}", cid, claim, topic)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--claims", default="../project/claims10.txt")
    ap.add_argument("--scouts", type=int, default=2)
    ap.add_argument("--parallel", type=int, default=10)
    a = ap.parse_args()
    claims = [c.strip() for c in open(a.claims, encoding="utf-8") if c.strip() and not c.startswith("#")]
    profile("lead", "lead", "splits work, posts claims", f"{len(claims)} claims")
    t0 = time.time()
    with ThreadPoolExecutor(a.parallel) as ex:
        list(ex.map(lambda p: run_claim(p[0], p[1], a.scouts), enumerate(claims, 1)))
    s = requests.get(f"{API}/stats").json()
    print(json.dumps({**s, **TOKENS, "seconds": round(time.time() - t0)}, indent=1))
