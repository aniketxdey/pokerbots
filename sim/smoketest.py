"""Smoke test for the AI-rival model connection.

Deals one hand of the SF variant to a table and asks 7 agents (one per rival persona) to decide
their action through a tool call, exactly the way rival agents talk to the model. Checks that the
endpoint answers, that the model makes valid tool calls, and reports speed, tokens and cost.

    python -m sim.smoketest                                  # local Ollama, default model
    python -m sim.smoketest --model gpt-oss:120b-cloud --concurrency 3
    python -m sim.smoketest --base-url https://ollama.com/v1 --api-key KEY
"""
import argparse
import json
import os
import random
import sys
import threading
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from sim import config as C  # noqa: E402
from sim.llm import make_client  # noqa: E402

ACT_TOOL = [{"type": "function", "function": {
    "name": "act", "description": "Choose your poker action for this decision.",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["fold", "check", "call", "raise"]},
        "amount": {"type": "integer", "description": "raise-to amount (only for raise)"},
        "reason": {"type": "string"}}, "required": ["action"]}}}]


def deal(seed):
    rng = random.Random(seed)
    deck = [r + s for r in "23456789TJQKA" for s in "cdhs"]
    rng.shuffle(deck)
    hands = [deck[3 * i:3 * i + 3] for i in range(7)]
    flop = deck[21:24]
    return hands, flop


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=C.DEFAULT_BASE_URL)
    ap.add_argument("--model", default=C.DEFAULT_MODEL)
    ap.add_argument("--api-key", default="")
    ap.add_argument("--concurrency", type=int, default=3)
    a = ap.parse_args()

    print(f"Endpoint {a.base_url}  model {a.model}  concurrency {a.concurrency}")
    hands, flop = deal(7)
    price = C.price_for(a.model)
    sem = threading.Semaphore(a.concurrency)
    results = [None] * 7

    def run(k):
        name, persona = C.PERSONAS[k]
        situation = {
            "variant": "Discard & Reveal: 3 hole cards; after the flop betting you discard 1, keep 1 hidden, show 1",
            "your_seat": k, "your_hole_cards": hands[k], "board": flop, "street": "flop",
            "pot": 120, "to_call": 40, "your_stack": 1940, "big_blind": 20, "min_raise_to": 80,
            "max_raise_to": 1980, "players_in_hand": 4, "legal_actions": ["fold", "call", "raise"],
        }
        msgs = [{"role": "system", "content": f"You are the poker bot of {name}. {persona} Decide by calling the act tool."},
                {"role": "user", "content": "Your decision:\n" + json.dumps(situation)}]
        client = make_client(a.base_url, a.model, a.api_key)
        t0 = time.time()
        try:
            with sem:
                t1 = time.time()
                msg, usage = client.chat(msgs, ACT_TOOL)
            dt = time.time() - t1
            calls = msg.get("tool_calls") or []
            ok, act = False, None
            if calls:
                fn = calls[0].get("function", {})
                args = json.loads(fn.get("arguments") or "{}")
                act = args
                ok = fn.get("name") == "act" and args.get("action") in situation["legal_actions"]
            pin = int(usage.get("prompt_tokens") or 0)
            pout = int(usage.get("completion_tokens") or 0)
            cost = (pin * price[0] + pout * price[2]) / 1e6
            results[k] = dict(team=name, ok=ok, act=act, text=(msg.get("content") or "")[:120], secs=dt,
                              wait=t1 - t0, tin=pin, tout=pout, cost=cost)
        except Exception as e:
            results[k] = dict(team=name, ok=False, error=f"{type(e).__name__}: {e}", secs=time.time() - t0)

    t0 = time.time()
    ths = [threading.Thread(target=run, args=(k,)) for k in range(7)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    total = time.time() - t0

    print(f"Flop: {' '.join(flop)}   (pot 120, 40 to call)\n")
    for k, r in enumerate(results):
        cards = " ".join(hands[k])
        if "error" in r:
            print(f"  FAIL  {r['team']:12s} {cards:10s} {r['error'][:200]}")
            continue
        act = r["act"]
        shown = (f"{act.get('action')}" + (f" to {act.get('amount')}" if act and act.get("action") == "raise" else "")
                 if act else f"no tool call (said: {r['text']!r})")
        reason = (act or {}).get("reason", "")
        print(f"  {'OK  ' if r['ok'] else 'BAD '}  {r['team']:12s} {cards:10s} {shown:14s} "
              f"{r['secs']:5.1f}s  {r['tin']}/{r['tout']} tok  ${r['cost']:.5f}  {reason[:70]}")
    good = sum(1 for r in results if r.get("ok"))
    cost = sum(r.get("cost", 0) for r in results)
    print(f"\n{good}/7 agents answered with a valid tool call in {total:.1f}s total; cost about ${cost:.4f}")
    if good == 7:
        print("Connection works: the AI rivals are ready.")
    elif good == 0:
        print("Nothing worked. Is the Ollama app running and signed in (ollama signin), and has the model been "
              "run once (ollama run %s)?" % a.model)
    sys.exit(0 if good == 7 else 1)


if __name__ == "__main__":
    main()
