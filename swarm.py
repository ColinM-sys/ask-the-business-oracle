"""Research swarm that checks whether claims hold up: are they lying?

Do not run before the hackathon starts. Needs OPENAI_API_KEY in env or .env.
    python swarm.py --model <model> --claims claims.txt [--agents 20] [--rounds 2]

For each claim (one per line in claims.txt):
  1. scouts search the web (OpenAI's built-in web_search tool, read-only) for evidence for AND against it,
  2. a checker re-reads each scout's cited passages and keeps only quotes the source actually supports,
  3. a judge gives a verdict: SUPPORTED / MISLEADING / FALSE / UNVERIFIABLE, with the kept sources.
Round 2+ re-checks claims where scouts disagreed. Writes results/swarm_<ts>.json and results/swarm_<ts>.md.
Read-only: no logins, forms, posting or downloads. Concurrency is capped by --agents.
"""
import argparse
import asyncio
import json
import os
import time

from openai import AsyncOpenAI

from db import _load_env

SCOUT = """Claim: "{claim}"
Search the web for the strongest evidence {side} this claim. Prefer primary sources (papers, official benchmarks,
company docs, independent tests). Return JSON only:
{{"findings": [{{"url": "...", "quote": "exact sentence from the page", "why": "how it bears on the claim"}}]}}
Give 2-5 findings. Quotes must be copied exactly from the page."""

CHECK = """Claim: "{claim}"
Below are findings from researchers. Open each URL with web search and confirm the quote is really on that page
and really means what "why" says. Return JSON only:
{{"kept": [{{"url": "...", "quote": "...", "stance": "for|against"}}], "dropped": [{{"url": "...", "reason": "..."}}]}}
Findings:
{findings}"""

JUDGE = """Claim: "{claim}"
Verified evidence (already checked against the sources):
{kept}
Give a verdict. Return JSON only:
{{"verdict": "SUPPORTED|MISLEADING|FALSE|UNVERIFIABLE", "confidence": 0-1, "summary": "2-3 sentences, cite urls"}}"""


def _json(text):
    s, e = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[s:e + 1])
    except Exception:
        return {}


class Swarm:
    def __init__(self, model, agents):
        self.client = AsyncOpenAI()
        self.model = model
        self.sem = asyncio.Semaphore(agents)
        self.calls = 0
        self.tokens = 0

    async def ask(self, prompt, web=True):
        async with self.sem:
            r = await self.client.responses.create(
                model=self.model, input=prompt, tools=[{"type": "web_search"}] if web else [])
            self.calls += 1
            self.tokens += r.usage.total_tokens if r.usage else 0
            return _json(r.output_text)

    async def claim(self, claim):
        scouts = await asyncio.gather(*(self.ask(SCOUT.format(claim=claim, side=side))
                                         for side in ("supporting", "against", "supporting", "against")))
        findings = [f for s in scouts for f in s.get("findings", [])]
        check = await self.ask(CHECK.format(claim=claim, findings=json.dumps(findings, indent=1)))
        kept = check.get("kept", [])
        verdict = await self.ask(JUDGE.format(claim=claim, kept=json.dumps(kept, indent=1)), web=False)
        return {"claim": claim, "found": len(findings), "kept": kept, "dropped": check.get("dropped", []), **verdict}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--claims", default="claims.txt")
    ap.add_argument("--agents", type=int, default=20)
    ap.add_argument("--rounds", type=int, default=2)
    a = ap.parse_args()
    _load_env()
    claims = [c.strip() for c in open(a.claims, encoding="utf-8") if c.strip() and not c.startswith("#")]
    sw = Swarm(a.model, a.agents)
    t0 = time.time()
    results = await asyncio.gather(*(sw.claim(c) for c in claims))
    for _ in range(a.rounds - 1):  # re-check the shaky ones
        redo = [i for i, r in enumerate(results) if r.get("verdict") == "UNVERIFIABLE" or r.get("confidence", 1) < 0.6]
        if not redo:
            break
        again = await asyncio.gather(*(sw.claim(results[i]["claim"]) for i in redo))
        for i, r in zip(redo, again):
            if r.get("confidence", 0) > results[i].get("confidence", 0):
                results[i] = r

    ts = time.strftime("%Y%m%d_%H%M%S")
    os.makedirs("results", exist_ok=True)
    meta = {"model": a.model, "claims": len(claims), "calls": sw.calls, "tokens": sw.tokens,
            "seconds": round(time.time() - t0), "dropped_quotes": sum(len(r.get("dropped", [])) for r in results)}
    json.dump({"meta": meta, "results": results}, open(f"results/swarm_{ts}.json", "w"), indent=1)
    with open(f"results/swarm_{ts}.md", "w", encoding="utf-8") as f:
        f.write(f"# Are they lying? {meta}\n\n")
        for r in results:
            f.write(f"## {r.get('verdict', '?')} ({r.get('confidence', '?')}): {r['claim']}\n{r.get('summary', '')}\n"
                    f"Sources kept {len(r['kept'])} / found {r['found']}\n\n")
    print(json.dumps(meta, indent=1))
    print(f"results/swarm_{ts}.md")


if __name__ == "__main__":
    asyncio.run(main())
