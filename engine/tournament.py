"""Tournament runner: 6 scored rounds x N iterations of randomly seated 8-bot sit-and-gos.

Mirrors the official "No Limit" rules (pokerbot.sequoiacap.com/rules.html):
  §4.1  six scored tournaments T1..T6 (optionally spaced 20 minutes apart in live mode)
  §4.2  each bot's code is frozen at the start of each tournament
  §4.3  10,000 iterations; each iteration randomly seats every bot at 8-bot tables
  §4.4  2,000 chips, blinds 10/20 increasing every 25 hands, 200-hand cap
  §4.5  points by finishing position; ICM at the hand cap
  §4.6  filler seats = random copies of submitted bots that earn no points
  §4.7  score = average points per table; tournament weights 1,2,3,4,5,6
  §3.6-3.9  no network, 1 ms per decision (auto check/fold), state reset per table,
            anonymized opponents
"""
import json
import multiprocessing as mp
import os
import random
import sys
import time
import types
from dataclasses import dataclass, field, asdict

from .sandbox import (BotStats, install_alarm, disable_network, mute_output, timed_call,
                      TIMEOUT, ERROR)
from .scoring import DEFAULT_POINTS
from .table import Table

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The rules only say "blinds start at 10/20 and increase every 25 hands" - the increments are
# not published, so this default (8 levels = 200 hands) is an editable assumption.
DEFAULT_BLINDS = [[10, 20], [15, 30], [25, 50], [40, 80], [60, 120], [100, 200],
                  [150, 300], [250, 500]]


@dataclass
class Config:
    variant: str = "nlhe"            # see engine/variants.py
    betting: str = ""                # "" = variant default | no_limit | pot_limit | fixed_limit
    rounds: int = 6
    round_weights: list = field(default_factory=lambda: [1, 2, 3, 4, 5, 6])
    iterations: int = 10000
    table_size: int = 8
    starting_stack: int = 2000
    blind_schedule: list = field(default_factory=lambda: [list(b) for b in DEFAULT_BLINDS])
    blind_interval: int = 25
    max_hands: int = 200
    points: list = field(default_factory=lambda: list(DEFAULT_POINTS))
    time_limit_ms: float = 1.0
    time_mode: str = "hard"          # hard | soft | off
    callback_time_limit_ms: float = 1.0  # limit for new_table/new_hand/hand_over callbacks
    init_time_limit_ms: float = 2000.0  # budget for loading the module + Agent() per table
    workers: int = 0                 # 0 = all CPU cores
    seed: int = 0                    # 0 = random
    round_interval_minutes: float = 0.0  # >0 = live mode: wait between rounds, re-freeze bots
    practice: bool = False           # practice round: 1 round, unscored
    sample_log_hands: int = 200      # hands of the first table to keep as a readable log

    @classmethod
    def from_dict(cls, d):
        c = cls()
        for k, v in (d or {}).items():
            if hasattr(c, k):
                setattr(c, k, v)
        c.validate()
        return c

    def validate(self):
        from .variants import VARIANTS
        if self.variant not in VARIANTS:
            self.variant = "nlhe"
        if self.betting not in ("", "no_limit", "pot_limit", "fixed_limit"):
            self.betting = ""
        self.rounds = max(1, int(self.rounds))
        self.iterations = max(1, int(self.iterations))
        self.table_size = max(2, min(10, int(self.table_size)))
        self.starting_stack = max(1, int(self.starting_stack))
        self.blind_interval = max(1, int(self.blind_interval))
        self.max_hands = max(1, int(self.max_hands))
        self.blind_schedule = [[int(x) for x in lvl] for lvl in self.blind_schedule if lvl]
        if not self.blind_schedule:
            self.blind_schedule = [list(b) for b in DEFAULT_BLINDS]
        w = [float(x) for x in (self.round_weights or [])]
        while len(w) < self.rounds:
            w.append(float(len(w) + 1))
        self.round_weights = w[: self.rounds]
        p = [float(x) for x in (self.points or [])]
        p = (p + [0.0] * self.table_size)[: self.table_size]
        self.points = p
        self.time_limit_ms = float(self.time_limit_ms)
        self.callback_time_limit_ms = float(self.callback_time_limit_ms)
        if self.time_mode not in ("hard", "soft", "off"):
            self.time_mode = "hard"
        self.workers = int(self.workers)
        self.seed = int(self.seed)
        if self.practice:
            self.rounds = 1
            self.round_weights = [0.0]
        return self

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------- worker side
class BotSlot:
    __slots__ = ("idx", "name", "agent", "stats", "scoring")

    def __init__(self, idx, name, agent, stats, scoring):
        self.idx = idx
        self.name = name
        self.agent = agent
        self.stats = stats
        self.scoring = scoring


_W = {}  # worker globals


def _worker_init(snapshot, cfg_dict):
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    install_alarm()
    mute_output()
    _W["cfg"] = Config.from_dict(cfg_dict)
    _W["bots"] = []
    for b in snapshot:
        try:
            code = compile(b["code"], b["path"], "exec")
        except Exception:
            code = None
        _W["bots"].append({"id": b["id"], "name": b["name"], "path": b["path"], "code": code})
    # import compat helpers before network is cut (they don't need it, but be safe)
    try:
        import compat  # noqa: F401
    except Exception:
        pass
    disable_network()


def _make_agent(bot, stats, cfg):
    """Fresh module + fresh Agent for every table: bot state resets per table (§3.8)."""
    if bot["code"] is None:
        stats.errors += 1
        if len(stats.error_samples) < 5:
            stats.error_samples.append("SyntaxError: bot failed to compile")
        return None

    def build():
        mod = types.ModuleType("bot_" + bot["id"])
        mod.__file__ = bot["path"]
        exec(bot["code"], mod.__dict__)
        return mod.Agent()

    agent = timed_call(build, (), cfg.init_time_limit_ms / 1000.0, cfg.time_mode, stats, False)
    if agent is TIMEOUT or agent is ERROR:
        return None
    return agent


def _run_chunk(task):
    """Run a block of iterations. Returns aggregated per-bot results."""
    round_idx, it_start, it_end, seed, want_log = task
    cfg = _W["cfg"]
    bots = _W["bots"]
    nb = len(bots)
    ts = cfg.table_size
    agg = [{"tables": 0, "points": 0.0, "place_sum": 0.0, "wins": 0, "top3": 0, "busts": 0,
            "hands": 0, "chips_end": 0, "stats": BotStats()} for _ in range(nb)]
    log = None
    tables_played = 0
    for it in range(it_start, it_end):
        rng = random.Random(f"{seed}-{round_idx}-{it}")
        order = list(range(nb))
        rng.shuffle(order)
        tables = [order[k:k + ts] for k in range(0, nb, ts)]
        for t_i, members in enumerate(tables):
            fillers = []
            while len(members) + len(fillers) < ts:
                fillers.append(rng.randrange(nb))
            seat_list = [(b, True) for b in members] + [(b, False) for b in fillers]
            rng.shuffle(seat_list)  # random seats
            # bots' own randomness: reproducible per table
            random.seed(f"{seed}-{round_idx}-{it}-{t_i}-bots")
            slots = []
            for bidx, scoring in seat_list:
                stats = agg[bidx]["stats"] if scoring else BotStats()
                agent = _make_agent(bots[bidx], stats, cfg)
                name = bots[bidx]["name"] + ("" if scoring else " (filler)")
                slots.append(BotSlot(bidx, name, agent, stats, scoring))
            # new_table callback
            for seat, sl in enumerate(slots):
                if sl.agent is not None and hasattr(sl.agent, "new_table"):
                    info = {"seat": seat, "num_players": ts, "starting_stack": cfg.starting_stack,
                            "blind_schedule": [list(b) for b in cfg.blind_schedule],
                            "blind_interval": cfg.blind_interval, "max_hands": cfg.max_hands,
                            "points": list(cfg.points), "time_limit_ms": cfg.time_limit_ms}
                    timed_call(sl.agent.new_table, (info,), cfg.callback_time_limit_ms / 1000.0,
                               cfg.time_mode, sl.stats, False)
            tl = [] if (want_log and log is None) else None
            table = Table(slots, cfg, rng, log=tl)
            if tl is not None:
                tl.append("Seats: " + ", ".join(f"S{i}={s.name}" for i, s in enumerate(slots)))
            res = table.play()
            tables_played += 1
            if tl is not None:
                log = _trim_log(tl, cfg.sample_log_hands)
            for seat, sl in enumerate(slots):
                if not sl.scoring:
                    continue
                a = agg[sl.idx]
                a["tables"] += 1
                a["points"] += res.points[seat]
                place = res.places[seat] or ts
                a["place_sum"] += place
                if place == 1:
                    a["wins"] += 1
                if place <= 3:
                    a["top3"] += 1
                if res.bust_hand[seat] is not None:
                    a["busts"] += 1
                    a["hands"] += res.bust_hand[seat]
                else:
                    a["hands"] += res.hands_played
                a["chips_end"] += res.final_stacks[seat]
    for a in agg:
        a["stats"] = a["stats"].to_dict()
    return {"agg": agg, "log": log, "iterations": it_end - it_start, "tables": tables_played}


def _trim_log(lines, max_hands):
    out = []
    hands = 0
    for ln in lines:
        if ln.startswith("--- Hand ") and not ln.startswith("--- Hand cap"):
            hands += 1
            if hands > max_hands:
                out.append(f"... (log truncated after {max_hands} hands)")
                break
        out.append(ln)
    if lines and lines[-1].startswith("--- Hand cap") and out[-1] != lines[-1]:
        out.append(lines[-1])
    return out


# ---------------------------------------------------------------- driver side
def _mp_context():
    if sys.platform.startswith("linux"):
        try:
            return mp.get_context("forkserver")
        except ValueError:
            pass
    return mp.get_context("spawn")


def run_round(cfg, snap, round_idx, seed, progress=None, stop=None):
    nb = len(snap)
    workers = cfg.workers if cfg.workers > 0 else (os.cpu_count() or 1)
    workers = max(1, min(workers, cfg.iterations))
    n_chunks = max(1, min(cfg.iterations, workers * 16))
    bounds = [round(cfg.iterations * k / n_chunks) for k in range(n_chunks + 1)]
    tasks = [(round_idx, bounds[k], bounds[k + 1], seed, k == 0)
             for k in range(n_chunks) if bounds[k + 1] > bounds[k]]
    total = {"tables": [0] * nb, "points": [0.0] * nb, "place_sum": [0.0] * nb,
             "wins": [0] * nb, "top3": [0] * nb, "busts": [0] * nb, "hands": [0] * nb,
             "chips_end": [0] * nb}
    stats = [BotStats() for _ in range(nb)]
    log = None
    done = 0
    n_tables = 0
    t0 = time.time()
    ctx = _mp_context()
    cfg_dict = cfg.to_dict()
    slim = [{k: b[k] for k in ("id", "name", "path", "code")} for b in snap]
    pool = ctx.Pool(workers, initializer=_worker_init, initargs=(slim, cfg_dict))
    stopped = False
    try:
        for res in pool.imap_unordered(_run_chunk, tasks):
            for i, a in enumerate(res["agg"]):
                for k in total:
                    total[k][i] += a[k]
                stats[i].merge(BotStats.from_dict(a["stats"]))
            if res["log"] is not None:
                log = res["log"]
            done += res["iterations"]
            n_tables += res["tables"]
            if progress:
                progress(round_idx, done, cfg.iterations,
                         _round_summary(snap, total, stats, cfg), time.time() - t0)
            if stop is not None and stop.is_set():
                stopped = True
                break
    finally:
        if stopped:
            pool.terminate()
        else:
            pool.close()
        pool.join()
    summary = _round_summary(snap, total, stats, cfg)
    return {"round": round_idx + 1, "iterations_done": done, "tables": n_tables,
            "seconds": time.time() - t0, "bots": summary, "sample_log": log or [],
            "stopped": stopped}


def _round_summary(snap, total, stats, cfg):
    out = []
    for i, b in enumerate(snap):
        t = total["tables"][i] or 0
        st = stats[i]
        out.append({
            "id": b["id"], "name": b["name"], "tables": t,
            "avg_points": total["points"][i] / t if t else 0.0,
            "avg_place": total["place_sum"][i] / t if t else None,
            "win_rate": total["wins"][i] / t if t else 0.0,
            "top3_rate": total["top3"][i] / t if t else 0.0,
            "bust_rate": total["busts"][i] / t if t else 0.0,
            "avg_hands_survived": total["hands"][i] / t if t else 0.0,
            "avg_end_chips": total["chips_end"][i] / t if t else 0.0,
            "decisions": st.decisions, "timeouts": st.timeouts,
            "timeout_rate": st.timeouts / st.decisions if st.decisions else 0.0,
            "callback_timeouts": st.callback_timeouts,
            "errors": st.errors, "invalid": st.invalid,
            "avg_decision_ms": 1000 * st.time_total / st.decisions if st.decisions else 0.0,
            "max_decision_ms": 1000 * st.time_max,
            "error_samples": list(st.error_samples),
            "frozen_at": b.get("frozen_at"),
            "hash": b.get("hash"),
        })
    return out


def combine(rounds, cfg):
    """Weighted leaderboard across rounds (§4.7: T1x1 ... T6x6)."""
    board = {}
    for r in rounds:
        w = cfg.round_weights[r["round"] - 1] if r["round"] - 1 < len(cfg.round_weights) else 1
        for b in r["bots"]:
            e = board.setdefault(b["id"], {"id": b["id"], "name": b["name"], "weighted": 0.0,
                                           "per_round": {}, "timeouts": 0, "errors": 0,
                                           "decisions": 0, "tables": 0, "wins": 0.0})
            e["name"] = b["name"]
            e["weighted"] += w * b["avg_points"]
            e["per_round"][r["round"]] = b["avg_points"]
            e["timeouts"] += b["timeouts"]
            e["errors"] += b["errors"]
            e["decisions"] += b["decisions"]
            e["tables"] += b["tables"]
            e["wins"] += b["win_rate"] * b["tables"]
    total_w = sum(cfg.round_weights[: len(rounds)]) or 1
    rows = list(board.values())
    for e in rows:
        e["weighted_avg"] = e["weighted"] / total_w
        e["win_rate"] = e["wins"] / e["tables"] if e["tables"] else 0.0
        e["timeout_rate"] = e["timeouts"] / e["decisions"] if e["decisions"] else 0.0
    rows.sort(key=lambda e: -e["weighted"])
    for k, e in enumerate(rows):
        e["rank"] = k + 1
    return rows


def run_tournament(cfg, bot_paths, progress=None, stop=None, on_round=None, wait_cb=None):
    """Run the whole tournament. bot_paths are re-read (frozen) at the start of each round."""
    from .loader import snapshot
    seed = cfg.seed or random.randrange(1, 2 ** 31)
    started = time.time()
    rounds = []
    for r in range(cfg.rounds):
        if r > 0 and cfg.round_interval_minutes > 0:
            target = started + r * cfg.round_interval_minutes * 60
            while time.time() < target:
                if stop is not None and stop.is_set():
                    break
                if wait_cb:
                    wait_cb(r, target - time.time())
                time.sleep(min(1.0, max(0.0, target - time.time())))
        if stop is not None and stop.is_set():
            break
        snap = snapshot(bot_paths)
        if len(snap) < 2:
            raise ValueError("need at least 2 valid bots")
        res = run_round(cfg, snap, r, seed, progress=progress, stop=stop)
        res["weight"] = cfg.round_weights[r]
        rounds.append(res)
        if on_round:
            on_round(res, combine(rounds, cfg))
        if res["stopped"]:
            break
    from .variants import get_variant
    v = get_variant(cfg.variant)
    versions = {}
    for r in rounds:
        for b in r["bots"]:
            versions.setdefault(b["id"], [])
            if b.get("hash") and b["hash"] not in versions[b["id"]]:
                versions[b["id"]].append(b["hash"])
    return {"config": cfg.to_dict(), "seed": seed, "started": started, "finished": time.time(),
            "variant": v.id, "variant_name": v.name, "variant_category": v.category,
            "betting": cfg.betting or v.betting,
            "rounds": rounds, "leaderboard": combine(rounds, cfg),
            "bot_versions": versions, "schema": 2,
            "bots": [os.path.basename(p) for p in bot_paths]}


def save_results(results, results_dir):
    os.makedirs(results_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(results.get("started", time.time())))
    tag = results.get("variant", "nlhe")
    path = os.path.join(results_dir, f"run-{stamp}-{tag}.json")
    k = 2
    while os.path.exists(path):
        path = os.path.join(results_dir, f"run-{stamp}-{tag}-{k}.json")
        k += 1
    with open(path, "w") as f:
        json.dump(results, f, indent=1)
    return path
