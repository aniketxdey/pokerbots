"""The practice event: schedule, teams, submissions, rounds and downloads.

Mirrors the No Limit regional format:
  * a 60-minute build phase with an unscored practice round every 10 minutes,
  * then six scored rounds, 20 minutes apart, weighted 1..6,
  * every round freezes each team's current submission at its start,
  * one submission per 5 minutes, 100 MB max, one Python file, standard library only,
  * after each round every team can download its own hand history and the results.

All times are "event time": real seconds multiplied by the speed factor (1 = real time).
"""
import ast
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid

from . import config as C

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
RUNS_DIR = os.path.join(ROOT, "sim_runs")
SPEND_FILE = os.path.join(RUNS_DIR, "spend.json")

HUMAN_ID = "you"

# every child process the simulator starts (agent scripts, local tests), so Reset can kill them
_PROCS = set()
_PROCS_LOCK = threading.Lock()


def run_proc(args, cwd, timeout):
    """subprocess.run replacement that registers the process so it can be killed on reset."""
    p = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    with _PROCS_LOCK:
        _PROCS.add(p)
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise
    finally:
        with _PROCS_LOCK:
            _PROCS.discard(p)


def kill_all_procs():
    with _PROCS_LOCK:
        procs = list(_PROCS)
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass
    return len(procs)
ALLOWED_IMPORT_ROOTS = None  # standard library only (checked against sys.stdlib_module_names)


def _now():
    return time.time()


def _stdlib():
    names = set(getattr(sys, "stdlib_module_names", ()))
    if not names:  # Python < 3.10: be permissive
        return None
    return names


def check_imports(src):
    """Return a problem string if the bot imports anything outside the standard library."""
    std = _stdlib()
    if std is None:
        return ""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return f"syntax error line {e.lineno}: {e.msg}"
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods = [node.module]
        for m in mods:
            if m.split(".")[0] not in std:
                return f"imports '{m}': only the Python standard library is available"
    return ""


class Team:
    def __init__(self, tid, name, kind, persona="", house_bot=None):
        self.id = tid
        self.name = name
        self.kind = kind            # human | ai | house
        self.persona = persona
        self.house_bot = house_bot
        self.submissions = []       # [{n, t, file, ok, problem, bytes}]
        self.last_submit_t = None   # event seconds
        self.current = None         # path of the active (latest accepted) submission

    def to_dict(self, ev_t):
        cd = 0
        if self.last_submit_t is not None:
            cd = max(0, C.SUBMIT_COOLDOWN_S - (ev_t - self.last_submit_t))
        return {"id": self.id, "name": self.name, "kind": self.kind, "persona": self.persona,
                "submissions": self.submissions[-20:], "n_submissions": len(self.submissions),
                "has_bot": self.current is not None, "cooldown_s": cd}


class Game:
    def __init__(self, settings):
        self.settings = s = dict(settings)
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.dir = os.path.join(RUNS_DIR, self.id)
        os.makedirs(self.dir, exist_ok=True)
        self.speed = float(s.get("speed", 1.0))
        self.variant = s.get("variant", "sf2026_reveal")
        self.points = s.get("points", [50, 30, 20, 0, 0, 0, 0, 0])
        self.practice_iterations = int(s.get("practice_iterations", 1000))
        self.scored_iterations = int(s.get("scored_iterations", 10000))
        self.history_tables = int(s.get("history_tables", 25))
        self.n_rivals = max(0, min(7, int(s.get("rivals", 7))))
        self.lock = threading.RLock()
        self.log = []
        self.rounds = []            # finished rounds
        self.current_round = None   # {"kind", "n", "progress"}
        self.schedule = self._make_schedule()
        self.started_real = None
        self.finished = False
        self.stopped = False
        self.agents = []            # filled by the server (sim.agent.RivalAgent)
        self.listeners = []         # callables(event_dict) for agents
        rng = random.Random(self.id)
        # teams: you + AI rivals + house bots to fill the 8 seats
        self.teams = [Team(HUMAN_ID, s.get("team_name") or "Aniket & Mitch", "human")]
        personas = list(C.PERSONAS)
        for k in range(self.n_rivals):
            name, persona = personas[k % len(personas)]
            self.teams.append(Team(f"ai{k + 1}", name, "ai", persona))
        house = [b for b in self._house_pool()]
        rng.shuffle(house)
        for k in range(7 - self.n_rivals):
            b = house[k % len(house)]
            self.teams.append(Team(f"house{k + 1}", f"House: {b}", "house", house_bot=b))
        for t in self.teams:
            self._init_workspace(t)
        self._save()

    # ------------------------------------------------------------------ setup
    def _house_pool(self):
        d = os.path.join(ROOT, "bots")
        return sorted(f[:-3] for f in os.listdir(d) if f.endswith(".py") and not f.startswith("sf_contender")
                      and not f.startswith("zz_"))

    def _make_schedule(self):
        ev = []
        n = 0
        for m in range(C.PRACTICE_EVERY, C.BUILD_MINUTES, C.PRACTICE_EVERY):
            n += 1
            ev.append({"kind": "practice", "n": n, "at": m * 60})
        for k in range(C.SCORED_ROUNDS):
            ev.append({"kind": "scored", "n": k + 1, "at": (C.BUILD_MINUTES + k * C.SCORED_GAP) * 60})
        return ev

    def workspace(self, team):
        return os.path.join(self.dir, "teams", team.id)

    def _init_workspace(self, team):
        ws = self.workspace(team)
        os.makedirs(os.path.join(ws, "downloads"), exist_ok=True)
        os.makedirs(os.path.join(ws, "submissions"), exist_ok=True)
        if team.kind == "house":
            src = os.path.join(ROOT, "bots", team.house_bot + ".py")
            dst = os.path.join(ws, "submissions", "house.py")
            shutil.copy(src, dst)
            team.current = dst
            return
        write_starter_kit(ws, self)

    # ------------------------------------------------------------------ time
    def event_time(self):
        if self.started_real is None:
            return 0.0
        return (_now() - self.started_real) * self.speed

    def phase(self):
        t = self.event_time()
        if self.finished:
            return "finished"
        if t < C.BUILD_MINUTES * 60:
            return "build"
        return "scored"

    def next_event(self):
        t = self.event_time()
        for e in self.schedule:
            if e["at"] > t and not e.get("done") and not e.get("started"):
                return e
        return None

    # ------------------------------------------------------------------ submissions
    def submit(self, team_id, src, filename="bot.py"):
        """Validate and accept a submission (same rules for every team)."""
        with self.lock:
            team = self.team(team_id)
            if team is None or team.kind == "house":
                return {"ok": False, "problem": "unknown team"}
            if self.finished:
                return {"ok": False, "problem": "the event is over"}
            t = self.event_time()
            if team.last_submit_t is not None and t - team.last_submit_t < C.SUBMIT_COOLDOWN_S:
                wait = C.SUBMIT_COOLDOWN_S - (t - team.last_submit_t)
                return {"ok": False, "problem": f"one submission per 5 minutes: wait {wait / 60:.1f} more "
                                                f"minutes (event time)"}
            data = src.encode("utf-8") if isinstance(src, str) else src
            n = len(team.submissions) + 1
            rec = {"n": n, "t": round(t), "bytes": len(data), "ok": False, "problem": ""}
            if len(data) > C.MAX_BOT_BYTES:
                rec["problem"] = "file larger than 100 MB"
            else:
                text = data.decode("utf-8", errors="replace")
                prob = check_imports(text)
                if not prob:
                    try:
                        tree = ast.parse(text)
                        if not any((isinstance(nd, ast.ClassDef) and nd.name == "Agent") or
                                   (isinstance(nd, ast.Assign) and any(getattr(x, "id", "") == "Agent"
                                                                       for x in nd.targets))
                                   for nd in tree.body):
                            prob = "no top-level `Agent` class"
                    except SyntaxError as e:
                        prob = f"syntax error line {e.lineno}: {e.msg}"
                rec["problem"] = prob
            path = os.path.join(self.workspace(team), "submissions", f"v{n:02d}.py")
            with open(path, "wb") as f:
                f.write(data)
            rec["file"] = os.path.basename(path)
            # a rejected file does not use up the 5-minute slot
            if not rec["problem"]:
                rec["ok"] = True
                team.current = path
                team.last_submit_t = t
            team.submissions.append(rec)
            self._log(f"{team.name} submitted v{n}" + ("" if rec["ok"] else f" (rejected: {rec['problem']})"))
            self._save()
            return {"ok": rec["ok"], "problem": rec["problem"], "version": n}

    def team(self, tid):
        return next((t for t in self.teams if t.id == tid), None)

    # ------------------------------------------------------------------ main loop
    def start(self):
        self.started_real = _now()
        self._log(f"Event started: {self.variant}, {len(self.teams)} teams "
                  f"({self.n_rivals} AI rivals), speed x{self.speed:g}")
        self._notify({"type": "start"})
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self.stopped = True
        self.finished = True
        self._log("Event stopped")
        self._save()

    def _loop(self):
        try:
            for e in self.schedule:
                while self.event_time() < e["at"]:
                    if self.stopped:
                        return
                    time.sleep(0.2)
                if self.stopped:
                    return
                self._run_round(e)
            self.finished = True
            self._log("Event finished")
            self._notify({"type": "finished"})
            self._save()
        except Exception:
            self._log("ERROR in event loop: " + traceback.format_exc(limit=3))
            self.finished = True
            self._save()

    def _freeze(self, e):
        snap = []
        tag = f"{e['kind']}{e['n']}"
        fdir = os.path.join(self.dir, "frozen", tag)
        os.makedirs(fdir, exist_ok=True)
        for t in self.teams:
            src_path = t.current
            note = ""
            if src_path is None:  # nothing submitted: the starter kit's example bot plays
                src_path = os.path.join(self.workspace(t), "kit_example_bot.py")
                note = "no submission (starter example)"
            dst = os.path.join(fdir, t.id + ".py")
            shutil.copy(src_path, dst)
            with open(dst) as f:
                code = f.read()
            ver = next((s["n"] for s in reversed(t.submissions) if s["ok"]), 0)
            snap.append({"id": t.id, "name": t.name, "path": dst, "code": code, "version": ver, "note": note})
        return snap

    def _run_round(self, e):
        from engine.tournament import Config, run_round
        e["started"] = True
        with self.lock:
            snap = self._freeze(e)
        label = f"Practice round {e['n']}" if e["kind"] == "practice" else f"Scored round {e['n']}"
        self._log(f"{label}: submissions frozen " +
                  ", ".join(f"{s['name']} v{s['version']}" for s in snap))
        iters = self.practice_iterations if e["kind"] == "practice" else self.scored_iterations
        cfg = Config(variant=self.variant, iterations=iters, points=list(self.points),
                     history_bot="*", history_tables=self.history_tables,
                     workers=int(self.settings.get("workers", 0)),
                     seed=random.randrange(1, 2 ** 31)).validate()
        self.current_round = {"kind": e["kind"], "n": e["n"], "label": label, "done": 0, "total": iters,
                              "started_t": round(self.event_time())}

        def progress(r, done, total, summary, elapsed):
            if self.current_round is not None:
                self.current_round["done"] = done
                self.current_round["tables_per_s"] = round(done / elapsed, 2) if elapsed > 0 else None
                # estimated real seconds to finish
                self.current_round["eta_s"] = round((total - done) * elapsed / done) if done else None

        # The round must finish before the next round's freeze, as on the real schedule. If this
        # machine is too slow for the full table count, the round stops 30 s (event time) early and
        # is scored on the tables completed so far.
        nxt = next((x for x in self.schedule if x["at"] > e["at"]), None)
        end_at = (nxt["at"] if nxt else e["at"] + C.SCORED_GAP * 60) - 30
        deadline_real = self.started_real + end_at / self.speed
        res = run_round(cfg, snap, 0, cfg.seed, progress=progress,
                        stop=_StopFlag(lambda: self.stopped or time.time() > deadline_real))
        hist = res.pop("hand_history", [])
        cut = res["iterations_done"] < iters and not self.stopped
        if cut:
            self._log(f"{label}: this machine finished {res['iterations_done']:,} of {iters:,} tables before "
                      f"the next round; scored on those (lower the table count to run full rounds)")
        rd = {"kind": e["kind"], "n": e["n"], "label": label, "iterations": res["iterations_done"],
              "planned": iters, "cut_short": cut,
              "seconds": round(res["seconds"], 1), "finished_t": round(self.event_time()),
              "bots": res["bots"], "versions": {s["id"]: s["version"] for s in snap},
              "weight": (e["n"] if e["kind"] == "scored" else 0)}
        with self.lock:
            self.rounds.append(rd)
            e["done"] = True
            self.current_round = None
            self._write_downloads(rd, hist)
            self._save()
        self._log(f"{label} finished in {rd['seconds']:.0f}s real time; downloads are ready")
        self._notify({"type": "round", "round": rd})

    # ------------------------------------------------------------------ downloads
    def _results_table(self, rd):
        rows = sorted(rd["bots"], key=lambda b: -b["avg_points"])
        out = []
        for k, b in enumerate(rows):
            out.append({"rank": k + 1, "team": b["name"], "avg_points": round(b["avg_points"], 3),
                        "win_rate": round(b["win_rate"], 4), "top3_rate": round(b["top3_rate"], 4),
                        "avg_place": round(b["avg_place"] or 0, 3), "tables": b["tables"],
                        "version": rd["versions"].get(b["id"])})
        return out

    def leaderboard(self):
        scored = [r for r in self.rounds if r["kind"] == "scored"]
        board = {}
        for r in scored:
            for b in r["bots"]:
                e = board.setdefault(b["id"], {"id": b["id"], "team": b["name"], "weighted": 0.0, "per_round": {}})
                e["weighted"] += r["weight"] * b["avg_points"]
                e["per_round"][r["n"]] = round(b["avg_points"], 2)
        rows = sorted(board.values(), key=lambda e: -e["weighted"])
        for k, e in enumerate(rows):
            e["rank"] = k + 1
            e["weighted"] = round(e["weighted"], 2)
        return rows

    def _write_downloads(self, rd, hist):
        tag = f"{rd['kind']}_{rd['n']}"
        results = {"round": rd["label"], "kind": rd["kind"], "variant": self.variant,
                   "points_by_place": self.points, "iterations": rd["iterations"],
                   "results": self._results_table(rd)}
        if rd["kind"] == "scored":
            results["leaderboard_so_far"] = self.leaderboard()
        by_team = {}
        for t in hist:
            by_team.setdefault(t["bot_id"], []).append(t)
        for team in self.teams:
            if team.kind == "house":
                continue
            d = os.path.join(self.workspace(team), "downloads", tag)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "results.json"), "w") as f:
                json.dump(results, f, indent=1)
            mine = next((b for b in rd["bots"] if b["id"] == team.id), None)
            if mine:
                stats = {k: mine.get(k) for k in ("tables", "avg_points", "avg_place", "win_rate", "top3_rate",
                                                  "bust_rate", "avg_hands_survived", "decisions", "timeouts",
                                                  "timeout_rate", "errors", "invalid", "avg_decision_ms",
                                                  "max_decision_ms", "error_samples")}
                stats["version"] = rd["versions"].get(team.id)
                with open(os.path.join(d, "my_bot_stats.json"), "w") as f:
                    json.dump(stats, f, indent=1)
            with open(os.path.join(d, "hand_history.jsonl"), "w") as f:
                for tb in by_team.get(team.id, []):
                    meta = {"round": rd["label"], "table": tb["table"], "my_place": tb["place"],
                            "my_points": tb["points"]}
                    for h in tb["hands"]:
                        h = dict(h)
                        me = h["my_seat"]
                        # opponents are anonymized: seat numbers only
                        h["seats"] = [dict(x, name=("you" if x["seat"] == me else f"seat {x['seat']}"))
                                      for x in h["seats"]]
                        f.write(json.dumps(dict(meta, **h)) + "\n")

    # ------------------------------------------------------------------ misc
    def _notify(self, ev):
        for fn in list(self.listeners):
            try:
                fn(ev)
            except Exception:
                pass

    def _log(self, msg):
        with self.lock:
            self.log.append({"t": round(self.event_time()), "msg": msg})
            self.log = self.log[-500:]

    def spend(self):
        return sum(a.cost for a in self.agents)

    def state(self):
        t = self.event_time()
        nxt = self.next_event()
        return {
            "id": self.id, "settings": self.settings, "variant": self.variant, "speed": self.speed,
            "event_time": t, "phase": self.phase(), "finished": self.finished,
            "build_left": max(0, C.BUILD_MINUTES * 60 - t),
            "next_event": ({"label": ("Practice round %d" % nxt["n"]) if nxt["kind"] == "practice"
                            else ("Scored round %d" % nxt["n"]), "in": nxt["at"] - t} if nxt else None),
            "current_round": self.current_round,
            "schedule": [{"kind": e["kind"], "n": e["n"], "at": e["at"], "done": bool(e.get("done")),
                          "running": bool(e.get("started") and not e.get("done"))} for e in self.schedule],
            "teams": [tm.to_dict(t) for tm in self.teams],
            "rounds": [{"kind": r["kind"], "n": r["n"], "label": r["label"], "results": self._results_table(r),
                        "iterations": r["iterations"], "planned": r.get("planned"), "cut_short": r.get("cut_short")}
                       for r in self.rounds],
            "leaderboard": self.leaderboard(),
            "log": self.log[-80:],
            "agents": [a.to_dict() for a in self.agents],
            "spend": {"game": self.spend(), "all_time": total_spend() + self.spend()},
        }

    def _save(self):
        try:
            with open(os.path.join(self.dir, "state.json"), "w") as f:
                json.dump({"id": self.id, "settings": self.settings,
                           "teams": [t.to_dict(self.event_time()) for t in self.teams],
                           "rounds": [{k: v for k, v in r.items() if k != "bots"} | {"results": self._results_table(r)}
                                      for r in self.rounds],
                           "leaderboard": self.leaderboard(), "finished": self.finished}, f, indent=1)
        except Exception:
            pass

    def downloads(self, team_id):
        team = self.team(team_id)
        d = os.path.join(self.workspace(team), "downloads")
        out = []
        for tag in sorted(os.listdir(d)):
            for fn in sorted(os.listdir(os.path.join(d, tag))):
                p = os.path.join(d, tag, fn)
                out.append({"round": tag, "file": fn, "path": f"{tag}/{fn}", "bytes": os.path.getsize(p)})
        return out

    def local_test(self, path, tables=3):
        return run_local_test(path, self.variant, tables)


class _StopFlag:
    def __init__(self, fn):
        self.fn = fn

    def is_set(self):
        return self.fn()


def run_local_test(path, variant, tables=3, timeout=180):
    try:
        code, out, err = run_proc([sys.executable, "-m", "sim.localtest", path, "--variant", variant,
                                   "--tables", str(tables)], ROOT, timeout)
        line = (out.strip().splitlines() or ['{"ok": false, "problem": "local test was stopped"}'])[-1]
        return json.loads(line)
    except subprocess.TimeoutExpired:
        return {"ok": False, "problem": f"local test took longer than {timeout}s"}
    except Exception as e:
        return {"ok": False, "problem": str(e)}


# ---------------------------------------------------------------------- spend tracking
def total_spend():
    try:
        with open(SPEND_FILE) as f:
            return float(json.load(f).get("total_usd", 0.0))
    except Exception:
        return 0.0


def add_spend(usd):
    if usd <= 0:
        return
    os.makedirs(RUNS_DIR, exist_ok=True)
    try:
        with open(SPEND_FILE) as f:
            d = json.load(f)
    except Exception:
        d = {"total_usd": 0.0}
    d["total_usd"] = float(d.get("total_usd", 0.0)) + usd
    with open(SPEND_FILE, "w") as f:
        json.dump(d, f)


# ---------------------------------------------------------------------- starter kit
def write_starter_kit(ws, game):
    from engine.variants import get_variant
    v = get_variant(game.variant)
    api_doc = open(os.path.join(ROOT, "engine", "api.py")).read()
    template = open(os.path.join(ROOT, "templates", "agent_template.py")).read()
    pts = ", ".join(f"{p:g}" for p in game.points)
    rules = f"""# No Limit regional - rules (practice event)

## Format
- 60-minute build phase. An unscored practice round runs every 10 minutes (minutes 10, 20, 30,
  40, 50). After each one you can download your hand history and everyone's results.
- Then six scored rounds, 20 minutes apart (minutes 60, 80, 100, 120, 140, 160).
  Later rounds weigh more: round k counts k times (1..6). Score = sum of weight x average points.
- At the start of every round (practice or scored) your current submission is frozen and used
  for that whole round. You can keep improving between rounds.

## Submissions
- One Python file defining `class Agent` (see `api.py` and `bot.py`). Standard library only.
- At most 100 MB. One accepted submission per 5 minutes. A rejected file does not use your slot.
- If you have not submitted anything when a round starts, the starter example bot plays for you.

## Each round
- Many 8-bot sit-and-go tables ({game.practice_iterations} per practice round,
  {game.scored_iterations} per scored round). Bots are randomly seated.
- 2,000 chips each. Blinds start at 10/20 and go up every 25 hands
  (10/20, 15/30, 25/50, 40/80, 60/120, 100/200, 150/300, 250/500).
- A table ends when one bot has all the chips, or after 200 hands. At the cap the remaining
  places are paid by ICM on the surviving stacks.
- Points by finishing place (1st..8th): {pts}.
- Your bot's state resets at the start of every table. Opponents are anonymous (seat numbers).

## Bot limits
- 1 ms per decision (act, discard, reveal and the other callbacks). If your bot errors or runs
  over, it automatically checks if it can, otherwise folds.
- No network access.

## Variant: {v.name}
{v.description}
Your bot answers `discard(obs)` (which card to throw away face down) and then `reveal(obs)`
(which of the two remaining cards to show face up). Opponents' face-up cards are visible in
`obs.players[seat].up_cards`. A bot without `reveal()` shows its lowest card.

## Downloads after each round (downloads/<round>/)
- `results.json`: every team's average points, win rate, top-3 rate and place for the round.
- `my_bot_stats.json`: your bot's decisions, timeouts, errors and decision times.
- `hand_history.jsonl`: one JSON object per hand from your seat (your cards, discards,
  everyone's face-up cards, every action, board, showdowns, chip changes).

## Local testing
`local_test` (or `python -m sim.localtest bot.py` from the repository) plays your bot for a few
tables against sample bots under the real time limit and reports crashes and timeouts.
"""
    with open(os.path.join(ws, "RULES.md"), "w") as f:
        f.write(rules)
    with open(os.path.join(ws, "api.py"), "w") as f:
        f.write(api_doc)
    with open(os.path.join(ws, "kit_example_bot.py"), "w") as f:
        f.write(template)
    if not os.path.exists(os.path.join(ws, "bot.py")):
        with open(os.path.join(ws, "bot.py"), "w") as f:
            f.write(template)
    if not os.path.exists(os.path.join(ws, "NOTES.md")):
        with open(os.path.join(ws, "NOTES.md"), "w") as f:
            f.write("# Team notes\n\n(keep track of what you tried and what happened)\n")
