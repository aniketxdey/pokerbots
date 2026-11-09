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


def run_proc(args, cwd, timeout, env=None):
    """subprocess.run replacement that registers the process so it can be killed on reset."""
    if env is None:
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    p = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
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


class Team:
    def __init__(self, tid, name, kind, persona="", house_bot=None):
        self.id = tid
        self.name = name
        self.kind = kind            # human | ai | house
        self.persona = persona
        self.house_bot = house_bot
        self.submissions = []       # uploads: [{n, t, file, status, problem, log, bytes}]
        self.last_submit_t = None   # event seconds of the last upload
        self.active = None          # upload number of the active bot
        self.current = None         # path of the active bot's agent.py (house bots: their file)

    def to_dict(self, ev_t):
        cd = 0
        if self.last_submit_t is not None:
            cd = max(0, C.UPLOAD_COOLDOWN_S - (ev_t - self.last_submit_t))
        return {"id": self.id, "name": self.name, "kind": self.kind, "persona": self.persona,
                "submissions": self.submissions[-30:], "n_submissions": len(self.submissions),
                "active": self.active, "has_bot": self.current is not None, "cooldown_s": cd}


class Game:
    def __init__(self, settings):
        self.settings = s = dict(settings)
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.dir = os.path.join(RUNS_DIR, self.id)
        os.makedirs(self.dir, exist_ok=True)
        self.speed = float(s.get("speed", 1.0))
        self.variant = s.get("variant", "sf2026_reveal")
        self.points = s.get("points", [50, 30, 20, 0, 0, 0, 0, 0])
        self.practice_iterations = int(s.get("practice_iterations", C.PRACTICE_ITERATIONS))
        self.scored_iterations = int(s.get("scored_iterations", C.SCORED_ITERATIONS))
        self.history_tables = int(s.get("history_tables", 25))
        self.n_rivals = max(0, min(7, int(s.get("rivals", 7))))
        # schedule (minutes of event time); defaults are the official ones
        self.build_minutes = max(1.0, float(s.get("build_minutes", C.BUILD_MINUTES)))
        self.practice_every = max(0.5, float(s.get("practice_every", C.PRACTICE_EVERY)))
        self.scored_gap = max(0.5, float(s.get("scored_gap", C.SCORED_GAP)))
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
        m = self.practice_every
        while m < self.build_minutes - 1e-9:
            n += 1
            ev.append({"kind": "practice", "n": n, "at": round(m * 60)})
            m += self.practice_every
        for k in range(C.SCORED_ROUNDS):
            ev.append({"kind": "scored", "n": k + 1, "at": round((self.build_minutes + k * self.scored_gap) * 60)})
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
        if t < self.build_minutes * 60:
            return "build"
        return "scored"

    def next_event(self):
        t = self.event_time()
        for e in self.schedule:
            if e["at"] > t and not e.get("done") and not e.get("started"):
                return e
        return None

    # ------------------------------------------------------------------ uploads
    def submit(self, team_id, src, filename="agent.py"):
        """Upload agent.py or a zip with agent.py at its root. Same rules for every team: one
        upload per 30 s, package limits, then a heads-up test match (up to 20 hands vs
        check-or-fold, 1 ms, no time bank). A passing upload becomes active automatically."""
        with self.lock:
            team = self.team(team_id)
            if team is None or team.kind == "house":
                return {"ok": False, "problem": "unknown team"}
            if self.finished:
                return {"ok": False, "problem": "the event is over: uploads are closed"}
            t = self.event_time()
            if team.last_submit_t is not None and t - team.last_submit_t < C.UPLOAD_COOLDOWN_S:
                wait = C.UPLOAD_COOLDOWN_S - (t - team.last_submit_t)
                return {"ok": False, "problem": f"one upload every 30 seconds: wait {wait:.0f} s (event time)"}
            team.last_submit_t = t
            n = len(team.submissions) + 1
            data = src.encode("utf-8") if isinstance(src, str) else src
            is_zip = data[:4] == b"PK\x03\x04" or filename.lower().endswith(".zip")
            vdir = os.path.join(self.workspace(team), "submissions", f"v{n:02d}")
            os.makedirs(vdir, exist_ok=True)
            raw = os.path.join(vdir, "upload.zip" if is_zip else "agent.py")
            with open(raw, "wb") as f:
                f.write(data)
            rec = {"n": n, "t": round(t), "bytes": len(data), "status": "testing", "problem": "", "log": [],
                   "file": os.path.basename(raw), "kind": "zip" if is_zip else "agent.py"}
            team.submissions.append(rec)
        # the test match runs outside the lock (a few seconds)
        from poker_agent_sdk.cli import unpack
        bdir = os.path.join(vdir, "bot")
        ok, problem, agent = unpack(raw, bdir)
        if ok:
            r = run_check(raw, self.variant)
            ok, problem, rec["log"] = r.get("ok", False), r.get("problem", ""), r.get("log", [])
        with self.lock:
            if not ok:
                rec["status"], rec["problem"] = "rejected", problem
                self._log(f"{team.name} upload v{n} rejected: {problem}")
            else:
                rec["status"] = "ready"
                rec["path"] = agent
                if not self.finished:
                    team.active, team.current = n, agent
                    rec["status"] = "active"
                    for o in team.submissions:
                        if o is not rec and o["status"] == "active":
                            o["status"] = "ready"
                self._log(f"{team.name} upload v{n} passed its test match and is now active")
            self._save()
        return {"ok": ok, "problem": problem, "version": n, "status": rec["status"], "log": rec["log"]}

    def set_active(self, team_id, n):
        with self.lock:
            team = self.team(team_id)
            if team is None or team.kind == "house":
                return {"ok": False, "problem": "unknown team"}
            if self.finished:
                return {"ok": False, "problem": "the event is over"}
            rec = next((x for x in team.submissions if x["n"] == int(n)), None)
            if rec is None or rec["status"] not in ("ready", "active"):
                return {"ok": False, "problem": "only uploads that passed their test match can be made active"}
            for o in team.submissions:
                if o["status"] == "active":
                    o["status"] = "ready"
            rec["status"] = "active"
            team.active, team.current = rec["n"], rec["path"]
            self._log(f"{team.name} made upload v{rec['n']} active")
            self._save()
            return {"ok": True, "version": rec["n"]}

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
        """Freeze every round at its scheduled start; compute rounds one after another. A round
        finishes when all its iterations complete (20 minutes is the gap between starts, not a
        time limit), so results can arrive after the next round has already been frozen."""
        import queue
        q = queue.Queue()
        worker = threading.Thread(target=self._compute_loop, args=(q,), daemon=True)
        worker.start()
        try:
            for e in self.schedule:
                while self.event_time() < e["at"]:
                    if self.stopped:
                        q.put(None)
                        return
                    time.sleep(0.2)
                if self.stopped:
                    q.put(None)
                    return
                with self.lock:
                    snap, forfeits = self._freeze(e)
                e["started"] = True
                label = self._label(e)
                self._log(f"{label}: active bots frozen - " + ", ".join(f"{s['name']} v{s['version']}" for s in snap)
                          + (f"; forfeits (no active bot): {', '.join(forfeits)}" if forfeits else ""))
                q.put((e, snap, forfeits))
            q.put(None)
        except Exception:
            self._log("ERROR in event loop: " + traceback.format_exc(limit=3))
            q.put(None)

    def _compute_loop(self, q):
        while True:
            item = q.get()
            if item is None:
                break
            if self.stopped:
                break
            try:
                self._run_round(*item)
            except Exception:
                self._log("ERROR computing round: " + traceback.format_exc(limit=3))
        if not self.stopped:
            self.finished = True
            self._log("Event finished: all rounds computed")
            self._notify({"type": "finished"})
            self._save()

    @staticmethod
    def _label(e):
        return f"Practice round {e['n']}" if e["kind"] == "practice" else f"Scored round {e['n']}"

    def _freeze(self, e):
        """Snapshot every team's active bot. A team without one forfeits the round (0 points)."""
        snap, forfeits = [], []
        tag = f"{e['kind']}{e['n']}"
        fdir = os.path.join(self.dir, "frozen", tag)
        os.makedirs(fdir, exist_ok=True)
        for t in self.teams:
            if t.current is None:
                forfeits.append(t.name)
                continue
            src_dir = os.path.dirname(t.current)
            dst = os.path.join(fdir, t.id)
            if t.kind == "house":
                os.makedirs(dst, exist_ok=True)
                path = os.path.join(dst, os.path.basename(t.current))
                shutil.copy(t.current, path)
            else:
                shutil.copytree(src_dir, dst, dirs_exist_ok=True)
                path = os.path.join(dst, "agent.py")
            with open(path, encoding="utf-8") as f:
                code = f.read()
            snap.append({"id": t.id, "name": t.name, "path": path, "code": code,
                         "version": t.active or 0, "note": ""})
        return snap, forfeits

    def _run_round(self, e, snap, forfeits):
        from engine.tournament import Config, run_round
        label = self._label(e)
        iters = self.practice_iterations if e["kind"] == "practice" else self.scored_iterations
        cfg = Config(variant=self.variant, iterations=iters, points=list(self.points),
                     history_bot="*", history_tables=self.history_tables, max_hands=C.HAND_CAP,
                     history_full=self._full_history_ids(), round_label=label,
                     history_dir=os.path.join(self.dir, "full_history", f"{e['kind']}_{e['n']}"),
                     blind_interval=C.HANDS_PER_LEVEL, time_bank_ms=C.TIME_BANK_MS,
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

        res = run_round(cfg, snap, 0, cfg.seed, progress=progress, stop=_StopFlag(lambda: self.stopped))
        if self.stopped:
            return
        hist = res.pop("hand_history", [])
        rd = {"kind": e["kind"], "n": e["n"], "label": label, "iterations": res["iterations_done"],
              "planned": iters, "forfeits": forfeits,
              "seconds": round(res["seconds"], 1), "finished_t": round(self.event_time()),
              "bots": res["bots"], "versions": {s["id"]: s["version"] for s in snap},
              "weight": (e["n"] if e["kind"] == "scored" else 0)}
        with self.lock:
            self.rounds.append(rd)
            e["done"] = True
            self.current_round = None
            self._write_downloads(rd, hist)
            for team in self.teams:
                if team.id in self._full_history_ids():
                    d = os.path.join(self.workspace(team), "downloads", f"{rd['kind']}_{rd['n']}")
                    os.makedirs(d, exist_ok=True)
                    self._merge_full_history(e, team, os.path.join(d, "hand_history_full.jsonl.gz"))
            self._save()
        self._log(f"{label} finished in {rd['seconds']:.0f}s real time; downloads are ready")
        self._notify({"type": "round", "round": rd})

    def _full_history_ids(self):
        """Teams that get the full hand history (every table they played), not just the subset."""
        mode = self.settings.get("full_history", "you")
        if mode == "off":
            return []
        return [t.id for t in self.teams if t.kind != "house" and (mode == "all" or t.id == HUMAN_ID)]

    def _merge_full_history(self, e, team, dest):
        """Concatenate the per-worker gzip files (gzip members concatenate into one valid file)."""
        src = os.path.join(self.dir, "full_history", f"{e['kind']}_{e['n']}", team.id)
        if not os.path.isdir(src):
            return
        with open(dest, "wb") as out:
            for fn in sorted(os.listdir(src)):
                with open(os.path.join(src, fn), "rb") as f:
                    shutil.copyfileobj(f, out)
        shutil.rmtree(src, ignore_errors=True)

    # ------------------------------------------------------------------ downloads
    def _results_table(self, rd):
        rows = sorted(rd["bots"], key=lambda b: -b["avg_points"])
        out = []
        for k, b in enumerate(rows):
            t = b["tables"] or 1
            out.append({"rank": k + 1, "id": b["id"], "team": b["name"], "avg_points": round(b["avg_points"], 3),
                        "win_rate": round(b["win_rate"], 4), "top3_rate": round(b["top3_rate"], 4),
                        "avg_place": round(b["avg_place"] or 0, 3), "tables": b["tables"],
                        "finishes": [round(c / t, 4) for c in (b.get("place_counts") or [])],
                        "version": rd["versions"].get(b["id"])})
        return out

    def leaderboard(self):
        """Final score = sum of round score x weight. Equal totals are ordered by countback: the
        weighted share of tables won, then of second places, and so on."""
        scored = [r for r in self.rounds if r["kind"] == "scored"]
        board = {}
        for r in scored:
            w = r["weight"]
            for b in r["bots"]:
                e = board.setdefault(b["id"], {"id": b["id"], "team": b["name"], "weighted": 0.0, "per_round": {},
                                               "countback": [0.0] * 8})
                e["weighted"] += w * b["avg_points"]
                e["per_round"][r["n"]] = round(b["avg_points"], 2)
                t = b["tables"] or 1
                for k, c in enumerate((b.get("place_counts") or [])[:8]):
                    e["countback"][k] += w * c / t
        for t in self.teams:  # forfeited rounds score zero
            if t.kind != "house" and scored:
                board.setdefault(t.id, {"id": t.id, "team": t.name, "weighted": 0.0, "per_round": {},
                                        "countback": [0.0] * 8})
        rows = sorted(board.values(), key=lambda e: (-round(e["weighted"], 9), [-x for x in e["countback"]]))
        for k, e in enumerate(rows):
            e["rank"] = k + 1
            e["weighted"] = round(e["weighted"], 3)
            e["countback"] = [round(x, 4) for x in e["countback"]]
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
            names = {}
            import gzip
            with gzip.open(os.path.join(d, "hand_history_subset.jsonl.gz"), "wt") as f:
                for tb in by_team.get(team.id, []):
                    meta = {"round": rd["label"], "table": tb["table"], "my_place": tb["place"],
                            "my_points": tb["points"]}
                    for h in tb["hands"]:
                        h = dict(h)
                        # opponents are named in the downloads (seats are anonymous in play)
                        h["seats"] = [dict(x) for x in h["seats"]]
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
            "build_left": max(0, self.build_minutes * 60 - t),
            "next_event": ({"label": ("Practice round %d" % nxt["n"]) if nxt["kind"] == "practice"
                            else ("Scored round %d" % nxt["n"]), "in": nxt["at"] - t} if nxt else None),
            "current_round": self.current_round,
            "schedule": [{"kind": e["kind"], "n": e["n"], "at": e["at"], "done": bool(e.get("done")),
                          "running": bool(e.get("started") and not e.get("done"))} for e in self.schedule],
            "teams": [tm.to_dict(t) for tm in self.teams],
            "rounds": [{"kind": r["kind"], "n": r["n"], "label": r["label"], "results": self._results_table(r),
                        "iterations": r["iterations"], "planned": r.get("planned"), "forfeits": r.get("forfeits", [])}
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


def run_check(path, variant, timeout=180):
    """The upload test match, in a separate process (a hanging bot can't stall the event)."""
    try:
        code, out, err = run_proc([sys.executable, "-m", "poker_agent_sdk", "check", path, "--variant", variant],
                                  ROOT, timeout)
        line = out.strip()
        return json.loads(line) if line else {"ok": False, "problem": "test match was stopped", "log": [err[-500:]]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "problem": "did not finish its test match (stopped responding)", "log": []}
    except Exception as e:
        return {"ok": False, "problem": f"test match failed: {e}", "log": []}


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
class KitSpec:
    """The settings a starter kit is written from (an event, or the setup form before one)."""
    def __init__(self, variant="sf2026_reveal", points=(50, 30, 20, 0, 0, 0, 0, 0),
                 practice_iterations=1000, scored_iterations=10000):
        self.variant = variant
        self.points = list(points)
        self.practice_iterations = practice_iterations
        self.scored_iterations = scored_iterations


def write_starter_kit(ws, game):
    """Write the starter kit in the layout the organizers announced:
    agent.py, examples/, sdk/, requirements.txt, RULES.md, hand-history/, README.md,
    AGENTS.md, CLAUDE.md (plus a poker-agent launcher)."""
    from engine.variants import get_variant
    from engine.tournament import DEFAULT_BLINDS
    v = get_variant(game.variant)
    kit = os.path.join(ROOT, "sim", "kit")
    os.makedirs(ws, exist_ok=True)
    if not os.path.exists(os.path.join(ws, "agent.py")):
        shutil.copy(os.path.join(kit, "agent.py"), os.path.join(ws, "agent.py"))
    shutil.copytree(os.path.join(kit, "examples"), os.path.join(ws, "examples"), dirs_exist_ok=True)
    shutil.copytree(os.path.join(kit, "hand-history"), os.path.join(ws, "hand-history"), dirs_exist_ok=True)
    sdk = os.path.join(ws, "sdk")
    ign = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(os.path.join(ROOT, "poker_agent_sdk"), os.path.join(sdk, "poker_agent_sdk"), dirs_exist_ok=True, ignore=ign)
    shutil.copytree(os.path.join(ROOT, "engine"), os.path.join(sdk, "engine"), dirs_exist_ok=True, ignore=ign)
    shutil.copy(os.path.join(kit, "icm.py"), os.path.join(sdk, "icm.py"))
    pts = [float(p) for p in game.points]
    with open(os.path.join(sdk, "payouts.json"), "w") as f:
        json.dump({"points_by_place": pts, "note": "Default table payouts. A round may draw payouts per table: "
                   "read state.config.payouts. At the hand cap, survivors split the remaining points by ICM."}, f, indent=1)
    with open(os.path.join(sdk, "blinds.json"), "w") as f:
        json.dump({"starting_stack": 2000, "hands_per_level": C.HANDS_PER_LEVEL, "hand_cap": C.HAND_CAP,
                   "levels": [{"level": k + 1, "hands": f"{C.HANDS_PER_LEVEL * k + 1}-{C.HANDS_PER_LEVEL * (k + 1)}",
                               "small_blind": sb, "big_blind": bb} for k, (sb, bb) in enumerate(DEFAULT_BLINDS)],
                   "note": "10/20 rising every 20 hands (format page). The exact levels after 10/20 are a "
                           "stand-in until the real kit; read state.config.blinds."}, f, indent=1)
    launcher = os.path.join(ws, "poker-agent")
    with open(launcher, "w") as f:
        f.write('#!/bin/sh\nDIR="$(cd "$(dirname "$0")" && pwd)"\n'
                'PYTHONPATH="$DIR/sdk:$PYTHONPATH" exec python3 -m poker_agent_sdk "$@"\n')
    os.chmod(launcher, 0o755)
    with open(os.path.join(ws, "requirements.txt"), "w") as f:
        f.write("# What the competition machines install (Python 3.12). poker_engine and pokerkit exist only locally.\n"
                "numpy\nscipy\npandas\nscikit-learn\ntorch  # CPU\nlightgbm\nxgboost\nonnxruntime\nnumba\neval7\n")
    p = ", ".join(f"{x:g}" for x in pts[:3])
    rules = f"""# Variant: {v.name}

{v.description}

## How it plays through the SDK
- Phases: `state.phase` is "betting", or "discard" / "reveal" at the decision point.
- Discard: return `Action("discard", cards=[card])`; reveal: `Action("reveal", cards=[card])`.
  `state.legal_actions[0].min_amount` is how many cards to choose. Your choices must be from
  `state.variant["down_cards"]`. A failed choice plays the automatic choice (your lowest card).
- Everyone's face-up cards: `state.variant["up_cards"]` ({{seat: [cards]}}) and reveal events.
- The face-up card still plays in your hand: best 5 cards from your 2 cards plus the 5 board cards.

## Table defaults (state.config holds each table's actual settings)
- 8 players, 2,000 chips, blinds 10/20 rising every {C.HANDS_PER_LEVEL} hands, hand cap {C.HAND_CAP}.
- Points by place: {p} for 1st-3rd, 0 otherwise. At the hand cap, survivors split the
  remaining points by ICM on their stacks (sdk/icm.py). Several busts in one hand: the bot with
  more chips at the start of the hand places higher.
- 1 ms per decision; overruns draw on a 100 ms bank per table (a timeout empties it). A timeout,
  exception or illegal action checks when free, otherwise folds.
- Memory starts as b"" at every table (64 KB max); opponents have anonymized seat IDs.

## Schedule
- Build for 60 minutes. Practice runs (~{game.practice_iterations:,} iterations) every 10 minutes;
  practice never counts.
- Six scored rounds, starting 20 minutes apart from the end of the build, weights 1x..6x;
  at least {game.scored_iterations:,} iterations each. Each round snapshots every team's active bot at
  its start; a team without an active bot forfeits that round (0 points).
"""
    with open(os.path.join(ws, "RULES.md"), "w") as f:
        f.write(rules)
    readme = """# Starter kit (practice-event mock)

## Setup
Python 3.12 recommended. The local simulator needs only the standard library:

    ./poker-agent play agent.py                # 10 local tables vs examples/ (100 ms bank)
    ./poker-agent check agent.py               # the upload test match (1 ms, no bank)
    ./poker-agent pack mybot/ mybot.zip        # zip a bot folder reproducibly, then check it

(or `PYTHONPATH=sdk python3 -m poker_agent_sdk play agent.py`)

## Writing a bot
Edit `agent.py`: `act(self, state, memory)` returns `(Action, memory_bytes)`. See the docstring
in `sdk/poker_agent_sdk/__init__.py` for every field of `state`. Don't keep state on `self` or in
globals between calls; per-table state goes in `memory`. Load anything heavy at import.

## Uploading
Upload `agent.py`, or a zip with `agent.py` at its root plus the files it reads. Limits: 50 MB
uploaded, 200 MB unpacked, 1,000 archive entries, 1 MB of code in agent.py, paths up to 512
characters and 16 levels. One upload every 30 seconds. Each upload plays a heads-up test match
(up to 20 hands vs check-or-fold, 1 ms, no bank); it is rejected if it doesn't start, crashes,
or fails more than 10% of its decisions. A passing upload becomes your active bot automatically;
you can pick an earlier one on your team page.

## Hand-history format
Downloads are gzipped JSON lines, one hand per line, from your seat: `hand`, `blinds`
([sb, bb, ante]), `button`, `my_seat`, `seats` (seat, name, stack at the start of the hand),
`my_cards` (dealt), `my_discards`, `my_final_cards`, `up_cards` ({seat: cards}), `actions`
([seat, action, amount_to, street]), `board`, `showdown` ({seat: cards}, only cards made
public), `winners` ({seat: chips}), `deltas` (chip change per seat), `eliminated`, plus the
table's `round`, `table`, `my_place`, `my_points`. See `hand-history/sample.jsonl.gz` and
`hand-history/reader.py`.
"""
    with open(os.path.join(ws, "README.md"), "w") as f:
        f.write(readme)
    agents_md = """# Instructions for AI coding assistants

- The bot is `agent.py`: class Agent(poker_agent_sdk.Agent) with act(self, state, memory) ->
  (Action, bytes). Read RULES.md for the variant and README.md for limits.
- Every decision must return within 1 ms (state.config.decision_ms). Precompute at import.
- Never rely on self or globals between calls; keep per-table state in memory (bytes, 64 KB).
- Only return actions offered in state.legal_actions; raise_to totals must be within
  min_amount..max_amount (not clamped).
- Allowed libraries: the standard library, poker_agent_sdk and requirements.txt. No network.
- Before uploading run `./poker-agent check agent.py`; for strength run `./poker-agent play agent.py`.
"""
    for fn in ("AGENTS.md", "CLAUDE.md"):
        with open(os.path.join(ws, fn), "w") as f:
            f.write(agents_md)
    if not os.path.exists(os.path.join(ws, "NOTES.md")):
        with open(os.path.join(ws, "NOTES.md"), "w") as f:
            f.write("# Team notes\n\n(keep track of what you tried and what happened)\n")
