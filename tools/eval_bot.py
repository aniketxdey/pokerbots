"""Focused, paired evaluation of one bot against the field.

Every table contains the hero plus 7 random opponents from the field. With --baseline, the very
same tables (same seats, same cards, same opponents' random seeds) are replayed with the
baseline bot in the hero's seat, so the difference in points is measured with much less noise.

  python tools/eval_bot.py sf_contender --variant sf2026_reveal --tables 400
  python tools/eval_bot.py sf_contender --baseline fullhouse_template --tables 400
"""
import argparse
import multiprocessing as mp
import os
import random
import statistics
import sys
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)

from engine.loader import list_bot_files, snapshot  # noqa: E402
from engine.sandbox import BotStats, install_alarm, mute_output  # noqa: E402
from engine.table import Table  # noqa: E402
from engine.tournament import Config, BotSlot, _make_agent  # noqa: E402

_G = {}


def _init(snap, cfgd):
    install_alarm()
    mute_output()
    _G["cfg"] = Config.from_dict(cfgd)
    bots = []
    for b in snap:
        try:
            code = compile(b["code"], b["path"], "exec")
        except Exception:
            code = None
        bots.append({"id": b["id"], "name": b["name"], "path": b["path"], "code": code})
    _G["bots"] = bots


def _play(task):
    t, hero_idx, field, seed = task
    cfg = _G["cfg"]
    bots = _G["bots"]
    rng = random.Random(f"{seed}-{t}")
    opp = rng.sample(field, 7)
    seats = [hero_idx] + opp
    rng.shuffle(seats)
    hero_seat = seats.index(hero_idx)
    random.seed(f"{seed}-{t}-bots")
    stats = BotStats()
    slots = []
    for k, b in enumerate(seats):
        st = stats if k == hero_seat else BotStats()
        slots.append(BotSlot(b, bots[b]["name"], _make_agent(bots[b], st, cfg), st, True))
    res = Table(slots, cfg, random.Random(f"{seed}-{t}-table"), log=None).play()
    return (res.points[hero_seat], res.places[hero_seat] or 8, stats.decisions, stats.timeouts,
            stats.errors, stats.time_max * 1000, stats.callback_timeouts, stats.invalid, list(stats.error_samples))


def run(hero, field, snap, cfg, tables, seed, workers):
    ids = [b["id"] for b in snap]
    hi = ids.index(hero)
    fi = [ids.index(f) for f in field]
    ctx = mp.get_context("forkserver")
    with ctx.Pool(workers, initializer=_init, initargs=(snap, cfg.to_dict())) as pool:
        return pool.map(_play, [(t, hi, fi, seed) for t in range(tables)], chunksize=4)


def summarize(name, rows):
    pts = [r[0] for r in rows]
    places = [r[1] for r in rows]
    dec = sum(r[2] for r in rows)
    to = sum(r[3] for r in rows)
    err = sum(r[4] for r in rows)
    mx = max(r[5] for r in rows)
    cbto = sum(r[6] for r in rows)
    inv = sum(r[7] for r in rows)
    samples = [e for r in rows for e in r[8]][:3]
    se = statistics.pstdev(pts) / max(1, len(pts)) ** 0.5
    dist = [sum(1 for p in places if p == k) / len(places) for k in (1, 2, 3)]
    print(f"{name:28s} avg pts {statistics.mean(pts):6.2f} ± {se:4.2f}   1st {dist[0]:.1%} 2nd {dist[1]:.1%} "
          f"3rd {dist[2]:.1%}  avg place {statistics.mean(places):.2f}   timeouts {to}/{dec} "
          f"({to / max(1, dec):.2%})  errors {err}  invalid {inv}  cb-timeouts {cbto}  max {mx:.2f} ms")
    for e in samples:
        print("   error:", e)
    return pts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hero")
    ap.add_argument("--baseline", default="")
    ap.add_argument("--variant", default="sf2026_reveal")
    ap.add_argument("--tables", type=int, default=300)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--time-mode", default="hard")
    ap.add_argument("--exclude", nargs="*", default=[], help="bot ids to leave out of the field")
    ap.add_argument("--field", nargs="*", default=[], help="only these bot ids as opponents (7+ needed)")
    a = ap.parse_args()
    paths = list_bot_files(os.path.join(ROOT, "bots"))
    snap = snapshot(paths)
    ids = [b["id"] for b in snap]
    excl = set(a.exclude) | {a.hero, a.baseline}
    field = [i for i in ids if i not in excl]
    if a.field:
        field = [i for i in a.field if i in ids]
    cfg = Config(variant=a.variant, time_mode=a.time_mode).validate()
    t0 = time.time()
    r1 = run(a.hero, field, snap, cfg, a.tables, a.seed, a.workers)
    p1 = summarize(a.hero, r1)
    if a.baseline:
        r2 = run(a.baseline, field, snap, cfg, a.tables, a.seed, a.workers)
        p2 = summarize(a.baseline, r2)
        d = [x - y for x, y in zip(p1, p2)]
        se = statistics.pstdev(d) / len(d) ** 0.5
        print(f"paired difference {statistics.mean(d):+.2f} ± {se:.2f} points per table")
    print(f"({time.time() - t0:.0f}s, field of {len(field)}, variant {a.variant})")


if __name__ == "__main__":
    main()
