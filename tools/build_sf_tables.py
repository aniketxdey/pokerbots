"""Offline table builder for bots/sf_contender.py.

Computes, by Monte Carlo, preflop equities that the bot embeds as Python literals so that a
decision is a dictionary lookup instead of a simulation (1 ms per decision rule):

  PF2[(hi, lo, suited)] = [equity vs 1..7 random opponents]            (2-card Hold'em hands)
  PF3[canonical 3-card key] = [equity vs 1, 2, 3, 4, 6 opponents]       (3 hole cards; every
        player plays the best 2 of their 3 cards at showdown - the "keep 2 of 3" games)

Run:  python tools/build_sf_tables.py --samples2 3000 --samples3 500 > bots/_sf_tables.txt
then paste the two literals into bots/sf_contender.py (the bot must stay a single file).
"""
import argparse
import itertools
import multiprocessing as mp
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from engine.evaluator import evaluate  # noqa: E402

N2 = (1, 2, 3, 4, 5, 6, 7)
N3 = (1, 2, 3, 4, 6)
PERMS = list(itertools.permutations(range(4)))


def key2(a, b):
    ra, rb = a >> 2, b >> 2
    hi, lo = max(ra, rb), min(ra, rb)
    return (hi, lo, int((a & 3) == (b & 3) and ra != rb))


def key3(cards):
    best = None
    for p in PERMS:
        k = tuple(sorted(((c >> 2) * 4 + p[c & 3] for c in cards), reverse=True))
        if best is None or k < best:
            best = k
    return best


def best2of3(h, board):
    a, b, c = h
    e1 = evaluate([a, b] + board)
    e2 = evaluate([a, c] + board)
    e3 = evaluate([b, c] + board)
    return e1 if e1 >= e2 and e1 >= e3 else (e2 if e2 >= e3 else e3)


def eq2(task):
    k, rep, samples, seed = task
    rng = random.Random(seed)
    out = []
    for n in N2:
        deck = [c for c in range(52) if c not in rep]
        tot = 0.0
        for _ in range(samples):
            d = rng.sample(deck, 5 + 2 * n)
            board = d[:5]
            me = evaluate(list(rep) + board)
            best = 0
            cnt = 0
            for j in range(n):
                v = evaluate(d[5 + 2 * j:7 + 2 * j] + board)
                if v > best:
                    best, cnt = v, 1
                elif v == best:
                    cnt += 1
            if me > best:
                tot += 1
            elif me == best:
                tot += 1.0 / (cnt + 1)
        out.append(round(tot / samples, 4))
    return k, out


def eq3(task):
    k, rep, samples, seed = task
    rng = random.Random(seed)
    out = []
    for n in N3:
        deck = [c for c in range(52) if c not in rep]
        tot = 0.0
        for _ in range(samples):
            d = rng.sample(deck, 5 + 3 * n)
            board = d[:5]
            me = best2of3(list(rep), board)
            best = 0
            cnt = 0
            for j in range(n):
                v = best2of3(d[5 + 3 * j:8 + 3 * j], board)
                if v > best:
                    best, cnt = v, 1
                elif v == best:
                    cnt += 1
            if me > best:
                tot += 1
            elif me == best:
                tot += 1.0 / (cnt + 1)
        out.append(round(tot / samples, 4))
    return k, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples2", type=int, default=3000)
    ap.add_argument("--samples3", type=int, default=500)
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    a = ap.parse_args()
    reps2, reps3 = {}, {}
    for a1, b1 in itertools.combinations(range(52), 2):
        reps2.setdefault(key2(a1, b1), (a1, b1))
    for c3 in itertools.combinations(range(52), 3):
        reps3.setdefault(key3(c3), c3)
    print(f"# {len(reps2)} two-card classes, {len(reps3)} three-card classes", file=sys.stderr)
    t2 = [(k, r, a.samples2, i) for i, (k, r) in enumerate(sorted(reps2.items()))]
    t3 = [(k, r, a.samples3, 10_000 + i) for i, (k, r) in enumerate(sorted(reps3.items()))]
    with mp.Pool(a.workers) as pool:
        pf2 = dict(pool.imap_unordered(eq2, t2, chunksize=4))
        print("# PF2 done", file=sys.stderr)
        pf3 = dict(pool.imap_unordered(eq3, t3, chunksize=16))
    print("PF2 = {")
    for k in sorted(pf2):
        print(f"    {k}: {pf2[k]},")
    print("}")
    print("PF3 = {")
    for k in sorted(pf3):
        print(f"    {k}: {pf3[k]},")
    print("}")


if __name__ == "__main__":
    main()
