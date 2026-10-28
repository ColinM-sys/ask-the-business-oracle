"""Inference ramp test: concurrency 1 -> N against the OpenAI API, measuring throughput, latency and errors.

Do not run before the hackathon starts. Needs OPENAI_API_KEY in env or .env.
    python ramp.py --model <model> [--steps 1,5,10,25,50,100,250] [--per-step 60] [--max-tokens 400]
First prints the key's rate-limit headers. Stops ramping once errors pass 20% or throughput stops rising.
Writes results/ramp_<model>.json and prints a table.
"""
import argparse
import asyncio
import json
import os
import statistics
import time

from openai import AsyncOpenAI

from db import _load_env

PROMPT = ("You are a business analyst. In about 250 words, explain how a 15-22% discount program plus a 60% rise in "
          "freight per unit would move gross margin, and list three actions.")


async def one(client, model, max_tokens):
    t0 = time.perf_counter()
    first = None
    out = 0
    stream = await client.chat.completions.create(
        model=model, messages=[{"role": "user", "content": PROMPT}], max_completion_tokens=max_tokens,
        stream=True, stream_options={"include_usage": True})
    async for ev in stream:
        if first is None and ev.choices and ev.choices[0].delta.content:
            first = time.perf_counter() - t0
        if ev.usage:
            out = ev.usage.completion_tokens
    return first or 0.0, time.perf_counter() - t0, out


async def step(client, model, conc, seconds, max_tokens):
    deadline = time.perf_counter() + seconds
    ok, errs, ttft, tok = [], {}, [], 0

    async def worker():
        nonlocal tok
        while time.perf_counter() < deadline:
            try:
                f, total, n = await one(client, model, max_tokens)
                ok.append(total); ttft.append(f); tok += n
            except Exception as e:  # count by status code / class
                k = str(getattr(e, "status_code", type(e).__name__))
                errs[k] = errs.get(k, 0) + 1
                await asyncio.sleep(1)

    t0 = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(conc)))
    wall = time.perf_counter() - t0
    n_err = sum(errs.values())
    return {"concurrency": conc, "requests": len(ok), "errors": errs,
            "error_rate": round(n_err / max(1, n_err + len(ok)), 3),
            "out_tok_per_s": round(tok / wall, 1),
            "ttft_p50": round(statistics.median(ttft), 2) if ttft else None,
            "latency_p50": round(statistics.median(ok), 2) if ok else None}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--steps", default="1,5,10,25,50,100,250")
    ap.add_argument("--per-step", type=int, default=60)
    ap.add_argument("--max-tokens", type=int, default=400)
    a = ap.parse_args()
    _load_env()
    client = AsyncOpenAI(max_retries=0)

    raw = await client.chat.completions.with_raw_response.create(
        model=a.model, messages=[{"role": "user", "content": "ok"}], max_completion_tokens=16)
    limits = {k: v for k, v in raw.headers.items() if k.startswith("x-ratelimit")}
    print("rate limits:", json.dumps(limits, indent=1))

    rows, best = [], 0.0
    for c in (int(x) for x in a.steps.split(",")):
        r = await step(client, a.model, c, a.per_step, a.max_tokens)
        rows.append(r)
        print(f"{c:>5} conc  {r['out_tok_per_s']:>8} tok/s  ttft {r['ttft_p50']}s  lat {r['latency_p50']}s  "
              f"err {r['error_rate']:.0%} {r['errors']}")
        if r["error_rate"] > 0.2 or r["out_tok_per_s"] < best * 1.05:
            print("ceiling reached")
            break
        best = max(best, r["out_tok_per_s"])

    os.makedirs("results", exist_ok=True)
    path = os.path.join("results", f"ramp_{a.model}.json")
    json.dump({"model": a.model, "limits": limits, "steps": rows}, open(path, "w"), indent=1)
    print("saved", path)


if __name__ == "__main__":
    asyncio.run(main())
