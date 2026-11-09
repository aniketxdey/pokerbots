"""Summarize your bot's play from a hand-history file (gzipped JSON lines, one hand per line).

    python hand-history/analyze.py downloads/practice_1/hand_history_subset.jsonl.gz
    python hand-history/analyze.py FILE --bot NAME     # someone else's seat, from a full history

Prints a compact report: results, preflop by position and stack depth, postflop and showdown,
discard & reveal, all-ins, the field's tendencies by opponent, and the biggest pots. Chip
results are in big blinds (bb) at the blind level of each hand. Use it to find leaks, then make a
change that targets the leak and A/B test it.
"""
import argparse
import collections
import gzip
import json
import sys

RANKS = "23456789TJQKA"
STREET = {0: "pre", 3: "flop", 4: "turn", 5: "river"}


def _hands(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except ValueError:
                    pass


def _positions(h):
    """Seat -> position name for the players dealt in, counting from the button."""
    live = [s["seat"] for s in h["seats"] if s.get("in_hand", True) and s.get("stack", 1) > 0]
    n = len(live)
    if not live:
        return {}
    b = h.get("button", live[0])
    order = sorted(live, key=lambda s: (s - b - 1) % 64)    # SB first, button last
    names = {}
    for i, s in enumerate(order):
        if i == 0 and n > 2:
            names[s] = "SB"
        elif i == 1 or (n == 2 and i == 0):
            names[s] = "BB" if i == 1 else "BTN/SB"
        elif s == b:
            names[s] = "BTN"
        elif i == n - 2:
            names[s] = "CO"
        elif i == n - 3 and n >= 6:
            names[s] = "HJ"
        else:
            names[s] = "EP"
    return names


def _depth(bb_stack):
    for lim, lab in ((10, "<10bb"), (20, "10-20bb"), (40, "20-40bb")):
        if bb_stack < lim:
            return lab
    return "40bb+"


class Agg:
    def __init__(self):
        self.n = 0
        self.vpip = 0
        self.pfr = 0
        self.net = 0.0

    def row(self):
        n = max(1, self.n)
        return f"{self.n:6d} hands  VPIP {100 * self.vpip / n:5.1f}%  PFR {100 * self.pfr / n:5.1f}%  net {self.net:+8.1f} bb  ({100 * self.net / n:+6.1f} bb/100)"


def analyze(path, bot=None, top=6):
    by_pos = collections.defaultdict(Agg)
    by_depth = collections.defaultdict(Agg)
    total = Agg()
    tables = {}
    flop_seen = showdown = sd_won = 0
    sd_net = nsd_net = 0.0
    bets = calls = 0
    rev_n = rev_net = 0.0
    rev_sd = rev_sd_won = 0
    up_hi = collections.defaultdict(lambda: [0, 0.0])
    allin = [0, 0, 0.0]
    facing_open = [0, 0]        # times facing a preflop raise before acting, times folded
    opp = collections.defaultdict(lambda: [0, 0, 0, 0])  # hands, vpip, pfr, aggressive actions
    pots = []
    for h in _hands(path):
        seats = {s["seat"]: s for s in h["seats"]}
        me = h.get("my_seat") if bot is None else next((s["seat"] for s in h["seats"] if s.get("name") == bot), None)
        if me is None or me not in seats:
            continue
        bb = max(1, (h.get("blinds") or [10, 20])[1])
        if bot is None:
            tables[(h.get("round"), h.get("table"))] = (h.get("my_place"), h.get("my_points"))
        pos = _positions(h).get(me, "?")
        stack = seats[me].get("stack", 0)
        d = (h.get("deltas") or [0] * 8)[me] / bb
        acts = h.get("actions", [])
        pre = [a for a in acts if a[3] == 0 and a[1] in ("fold", "check", "call", "raise")]
        mine_pre = [a for a in pre if a[0] == me]
        vp = any(a[1] in ("call", "raise") for a in mine_pre)
        pr = any(a[1] == "raise" for a in mine_pre)
        first = next((i for i, a in enumerate(pre) if a[0] == me), None)
        if first is not None and any(a[1] == "raise" for a in pre[:first]):
            facing_open[0] += 1
            facing_open[1] += pre[first][1] == "fold"
        for g in (by_pos[pos], by_depth[_depth(stack / bb)], total):
            g.n += 1
            g.vpip += vp
            g.pfr += pr
            g.net += d
        folded = any(a[0] == me and a[1] == "fold" for a in acts)
        post = [a for a in acts if a[3] > 0]
        if post and not any(a[0] == me and a[1] == "fold" and a[3] == 0 for a in acts):
            flop_seen += 1
        for a in acts:
            if a[0] == me and a[3] > 0:
                bets += a[1] == "raise"
                calls += a[1] == "call"
        sd = str(me) in (h.get("showdown") or {})
        if sd:
            showdown += 1
            sd_won += str(me) in (h.get("winners") or {})
            sd_net += d
        elif vp:
            nsd_net += d
        if any(a[0] == me and a[1] == "reveal" for a in acts):
            rev_n += 1
            rev_net += d
            if sd:
                rev_sd += 1
                rev_sd_won += str(me) in (h.get("winners") or {})
            up = (h.get("up_cards") or {}).get(str(me))
            fin = h.get("my_final_cards") or []
            if up and bot is None and fin:
                hi = max(RANKS.index(c[0]) for c in fin)
                key = "revealed my highest card" if RANKS.index(up[0][0]) == hi else "kept my highest card hidden"
                up_hi[key][0] += 1
                up_hi[key][1] += d
        if any(a[0] == me and a[1] in ("raise", "call") and a[2] >= stack for a in acts):
            allin[0] += 1
            allin[1] += str(me) in (h.get("winners") or {})
            allin[2] += d
        for s in seats:
            if s == me:
                continue
            nm = seats[s].get("name", str(s))
            o = opp[nm]
            o[0] += 1
            sp = [a for a in pre if a[0] == s]
            o[1] += any(a[1] in ("call", "raise") for a in sp)
            o[2] += any(a[1] == "raise" for a in sp)
            o[3] += sum(1 for a in acts if a[0] == s and a[1] == "raise")
        pots.append((d, me, h))
    if not total.n:
        return "no hands found for that seat"
    out = []
    pts = [p for _, p in tables.values() if p is not None]
    places = [p for p, _ in tables.values() if p is not None]
    out.append(f"== {path}" + (f"  (bot {bot})" if bot else ""))
    if tables:
        out.append(f"tables {len(tables)}  avg points {sum(pts) / max(1, len(pts)):.2f}  avg place {sum(places) / max(1, len(places)):.2f}")
    out.append("all    " + total.row())
    out.append(f"facing a preflop raise: folded {facing_open[1]}/{facing_open[0]} ({100 * facing_open[1] / max(1, facing_open[0]):.0f}%)")
    out.append("-- by position")
    for k in ("EP", "HJ", "CO", "BTN", "SB", "BB", "BTN/SB"):
        if k in by_pos:
            out.append(f"{k:7s}" + by_pos[k].row())
    out.append("-- by my stack depth at the start of the hand")
    for k in ("<10bb", "10-20bb", "20-40bb", "40bb+"):
        if k in by_depth:
            out.append(f"{k:8s}" + by_depth[k].row())
    out.append("-- postflop")
    out.append(f"saw flop {flop_seen} ({100 * flop_seen / total.n:.1f}% of hands)  bets/raises {bets}  calls {calls}  "
               f"aggression {bets / max(1, calls):.2f}")
    out.append(f"showdowns {showdown}: won {sd_won} ({100 * sd_won / max(1, showdown):.0f}%), net {sd_net:+.1f} bb; "
               f"voluntary hands without showdown net {nsd_net:+.1f} bb")
    out.append("-- discard & reveal")
    out.append(f"reached the reveal {int(rev_n)} times, net {rev_net:+.1f} bb ({rev_net / max(1, rev_n):+.2f} bb/hand); "
               f"showdowns {rev_sd}, won {rev_sd_won}")
    for k, (n, v) in up_hi.items():
        out.append(f"  {k}: {n} hands, {v / max(1, n):+.2f} bb/hand")
    out.append(f"-- all-ins: {allin[0]}, won {allin[1]} ({100 * allin[1] / max(1, allin[0]):.0f}%), net {allin[2]:+.1f} bb")
    out.append("-- opponents (their preflop VPIP / PFR, raises per hand)")
    for nm, (n, v, p, r) in sorted(opp.items(), key=lambda x: -x[1][0])[:10]:
        out.append(f"  {nm:22s} {n:6d} hands  VPIP {100 * v / max(1, n):5.1f}%  PFR {100 * p / max(1, n):5.1f}%  raises/hand {r / max(1, n):.2f}")
    pots.sort(key=lambda x: x[0])
    for title, sel in (("biggest losses", pots[:top]), ("biggest wins", pots[::-1][:top])):
        out.append(f"-- {title}")
        for d, me, h in sel:
            line = [f"{d:+7.1f} bb", "cards " + " ".join(h.get("my_cards") or []),
                    "up " + " ".join((h.get("up_cards") or {}).get(str(me), []) or ["-"]),
                    "board " + " ".join(h.get("board") or []) if h.get("board") else "",
                    "sd " + json.dumps(h.get("showdown")) if h.get("showdown") else ""]
            acts = [f"{STREET.get(a[3], a[3])}:{'ME' if a[0] == me else a[0]} {a[1]} {a[2]}" for a in h.get("actions", [])
                    if a[1] in ("raise", "call") or (a[0] == me and a[1] == "fold")][-8:]
            out.append("  " + "  ".join(x for x in line if x) + "\n      " + ", ".join(acts))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--bot", default=None, help="analyze this bot's seat instead of yours (full histories)")
    ap.add_argument("--top", type=int, default=6)
    a = ap.parse_args()
    print(analyze(a.path, a.bot, a.top))


if __name__ == "__main__":
    sys.exit(main())
