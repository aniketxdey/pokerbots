"""Command-line runner.

    python run_tournament.py                       # official settings, all bots in bots/
    python run_tournament.py --iterations 200      # quicker
    python run_tournament.py --practice            # one unscored round
    python run_tournament.py --variant plo         # any variant (see --list-variants)
    python run_tournament.py --bots mit2023_precompute fullhouse_shark ppe_fish_player
    python run_tournament.py --live 20             # real schedule: rounds 20 minutes apart,
                                                   # bots re-frozen at the start of each round
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from engine.loader import list_bot_files  # noqa: E402
from engine.scoring import POINT_PRESETS  # noqa: E402
from engine.tournament import Config, run_tournament, save_results  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--variant", default="nlhe", help="variant id (see --list-variants)")
    ap.add_argument("--betting", default="", choices=["", "no_limit", "pot_limit", "fixed_limit"],
                    help="override the variant's default betting structure")
    ap.add_argument("--list-variants", action="store_true")
    ap.add_argument("--iterations", type=int, default=10000)
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--workers", type=int, default=0, help="0 = all cores")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--time-limit-ms", type=float, default=1.0)
    ap.add_argument("--time-mode", choices=["hard", "soft", "off"], default="hard")
    ap.add_argument("--points", default="top3_50_30_20",
                    help=f"preset ({', '.join(POINT_PRESETS)}) or comma list, e.g. 10,6,4,2,1,0,0,0")
    ap.add_argument("--live", type=float, default=0, help="minutes between rounds (live mode)")
    ap.add_argument("--practice", action="store_true")
    ap.add_argument("--bots", nargs="*", help="bot ids (file names without .py); default all")
    ap.add_argument("--bots-dir", default=os.path.join(ROOT, "bots"))
    a = ap.parse_args()
    if a.list_variants:
        from engine.variants import V
        for v in V:
            print(f"{v.id:22s} {v.name:40s} [{v.category}] {v.betting}")
        return

    points = POINT_PRESETS.get(a.points) or [float(x) for x in a.points.split(",")]
    cfg = Config(variant=a.variant, betting=a.betting, rounds=a.rounds, iterations=a.iterations, workers=a.workers, seed=a.seed,
                 time_limit_ms=a.time_limit_ms, time_mode=a.time_mode, points=points,
                 round_interval_minutes=a.live, practice=a.practice).validate()
    paths = list_bot_files(a.bots_dir)
    if a.bots:
        want = set(a.bots)
        paths = [p for p in paths if os.path.splitext(os.path.basename(p))[0] in want]
    print(f"{len(paths)} bots, {cfg.variant}, {cfg.rounds} round(s) x {cfg.iterations} iterations")

    last = [0.0]

    def progress(r, done, total, summary, elapsed):
        if time.time() - last[0] > 2 or done == total:
            last[0] = time.time()
            eta = elapsed / done * (total - done) if done else 0
            print(f"  T{r + 1}: {done}/{total} iterations  {elapsed:6.0f}s elapsed  ~{eta:5.0f}s left", flush=True)

    def wait_cb(r, remaining):
        if int(remaining) % 30 == 0:
            print(f"  live mode: T{r + 1} starts in {remaining:.0f}s (edit bots now)", flush=True)

    res = run_tournament(cfg, paths, progress=progress, wait_cb=wait_cb)
    path = save_results(res, os.path.join(ROOT, "results"))
    print()
    hdr = f"{'#':>3}  {'bot':40s} {'weighted':>9}  " + " ".join(f"{'T' + str(r['round']):>6}" for r in res["rounds"])
    print(hdr + f"  {'timeouts':>8} {'errors':>6}")
    for e in res["leaderboard"]:
        per = " ".join(f"{e['per_round'].get(r['round'], 0):6.2f}" for r in res["rounds"])
        print(f"{e['rank']:>3}  {e['name'][:40]:40s} {e['weighted']:9.2f}  {per}  {e['timeout_rate']:8.1%} {e['errors']:6d}")
    print(f"\nsaved {path}")


if __name__ == "__main__":
    main()
