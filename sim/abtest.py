"""Paired A/B test of two bot versions on identical tables.

Every table is played twice: once with version A in the test seat, once with version B, with
the same seat, the same opponents and the same cards (same seeds). The difference in points per
table is reported with its standard error, plus each version's failed decisions and timing.

    python -m sim.abtest agent.py agent_b.py --tables 40 --variant sf2026_reveal_preflop \
        --opponents p1/agent.py p2/agent.py ...
Prints one JSON object.
"""
import argparse
import json
import os
import random
import statistics
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)


def _load(path, bid):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    return {"id": bid, "name": bid, "path": os.path.abspath(path), "code": compile(src, path, "exec")}


def run(a_path, b_path, opp_paths, tables, variant, seed=7):
    from engine.sandbox import BotStats, install_alarm, mute_output
    from engine.table import Table
    from engine.tournament import Config, BotSlot, _make_agent
    install_alarm()
    cfg = Config(variant=variant, max_hands=250, blind_interval=20, time_bank_ms=100.0).validate()
    A, B = _load(a_path, "A"), _load(b_path, "B")
    opps = [_load(p, f"opp{i}") for i, p in enumerate(opp_paths)]
    if len(opps) < 1:
        raise SystemExit("need at least one opponent")
    out = {"A": [], "B": []}
    stats = {"A": BotStats(), "B": BotStats()}
    real = sys.stdout
    mute_output()
    try:
        for t in range(tables):
            rng = random.Random(f"{seed}-{t}")
            field = [rng.choice(opps) for _ in range(7)]
            hero_seat = rng.randrange(8)
            for tag, hero in (("A", A), ("B", B)):
                seats = list(field)
                seats.insert(hero_seat, hero)
                random.seed(f"{seed}-{t}-bots")
                slots = []
                for i, b in enumerate(seats):
                    st = stats[tag] if i == hero_seat else BotStats()
                    slots.append(BotSlot(i, b["name"], _make_agent(b, st, cfg), st, True))
                for k, sl in enumerate(slots):
                    if sl.agent is not None and hasattr(sl.agent, "new_table"):
                        sl.agent.new_table({"seat": k, "num_players": 8, "points": list(cfg.points),
                                            "table_id": f"ab-{t}", "variant": variant,
                                            "time_bank_ms": cfg.time_bank_ms, "memory_bytes": cfg.memory_bytes})
                res = Table(slots, cfg, random.Random(f"{seed}-{t}-cards")).play()
                out[tag].append(res.points[hero_seat])
    finally:
        sys.stdout = real
    diff = [b - a for a, b in zip(out["A"], out["B"])]
    se = statistics.pstdev(diff) / max(1, len(diff)) ** 0.5 if len(diff) > 1 else 0.0

    def s(tag):
        st = stats[tag]
        return {"avg_points": round(statistics.mean(out[tag]), 3),
                "first_place_rate": round(sum(1 for x in out[tag] if x >= max(cfg.points)) / len(out[tag]), 3),
                "decisions": st.decisions, "failed": st.timeouts + st.errors + st.invalid,
                "timeouts": st.timeouts, "errors": st.errors, "illegal": st.invalid,
                "max_ms": round(1000 * st.time_max, 3), "error_samples": list(st.error_samples)[:2]}
    d = statistics.mean(diff)
    verdict = ("B better" if d > 2 * se and d > 0 else "A better" if d < -2 * se and d < 0 else "no clear difference")
    return {"ok": True, "tables": tables, "A": s("A"), "B": s("B"), "B_minus_A": round(d, 3),
            "std_error": round(se, 3), "verdict": verdict}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--opponents", nargs="+", required=True)
    ap.add_argument("--tables", type=int, default=40)
    ap.add_argument("--variant", default="sf2026_reveal_preflop")
    ap.add_argument("--seed", type=int, default=7)
    x = ap.parse_args()
    try:
        r = run(x.a, x.b, x.opponents, max(2, min(200, x.tables)), x.variant, x.seed)
    except SystemExit:
        raise
    except Exception as e:
        r = {"ok": False, "problem": f"{type(e).__name__}: {e}"}
    print(json.dumps(r))


if __name__ == "__main__":
    main()
