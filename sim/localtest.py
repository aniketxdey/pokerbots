"""Local test harness from the starter kit: plays a bot for a few tables against sample bots
under the real time limit and reports crashes, timeouts and speed.

    python -m sim.localtest path/to/bot.py --variant sf2026_reveal --tables 3
Prints one JSON object.
"""
import argparse
import json
import os
import random
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

SAMPLE_BOTS = ["mit2023_precompute", "ngnnah_v6_precomputed", "fullhouse_shark", "mit2025_skeleton_random",
               "ppe_fish_player", "mit2022_pair_all_in", "fullhouse_template", "neuron_random"]


def run(path, variant, tables, seed=0):
    from engine.loader import inspect_bot
    from engine.sandbox import BotStats, install_alarm, mute_output
    from engine.table import Table
    from engine.tournament import Config, BotSlot, _make_agent

    info = inspect_bot(path)
    if not info["valid"]:
        return {"ok": False, "problem": info["problem"]}
    install_alarm()
    cfg = Config(variant=variant).validate()
    with open(path) as f:
        src = f.read()
    try:
        code = compile(src, path, "exec")
    except SyntaxError as e:
        return {"ok": False, "problem": f"syntax error line {e.lineno}: {e.msg}"}
    me = {"id": "you", "name": "your bot", "path": path, "code": code}
    samples = []
    for b in SAMPLE_BOTS:
        p = os.path.join(ROOT, "bots", b + ".py")
        if os.path.exists(p):
            samples.append({"id": b, "name": b, "path": p, "code": compile(open(p).read(), p, "exec")})
    rng = random.Random(seed)
    stats = BotStats()
    out = []
    real_stdout = sys.stdout
    mute_output()
    try:
        for t in range(tables):
            opp = [rng.choice(samples) for _ in range(7)]
            seats = [me] + opp
            rng.shuffle(seats)
            k = seats.index(me)
            slots = []
            for i, b in enumerate(seats):
                st = stats if i == k else BotStats()
                slots.append(BotSlot(i, b["name"], _make_agent(b, st, cfg), st, True))
            res = Table(slots, cfg, random.Random(seed * 1000 + t)).play()
            out.append({"place": res.places[k] or 8, "points": res.points[k], "hands": res.hands_played})
    finally:
        sys.stdout = real_stdout
    return {"ok": True, "tables": out, "decisions": stats.decisions, "timeouts": stats.timeouts,
            "errors": stats.errors, "invalid_actions": stats.invalid,
            "callback_timeouts": stats.callback_timeouts,
            "avg_ms": round(1000 * stats.time_total / max(1, stats.decisions), 3),
            "max_ms": round(1000 * stats.time_max, 3), "error_samples": list(stats.error_samples)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--variant", default="sf2026_reveal")
    ap.add_argument("--tables", type=int, default=3)
    a = ap.parse_args()
    try:
        r = run(a.path, a.variant, max(1, min(10, a.tables)))
    except Exception as e:
        r = {"ok": False, "problem": f"{type(e).__name__}: {e}"}
    print(json.dumps(r))


if __name__ == "__main__":
    main()
