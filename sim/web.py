"""Flask routes for the practice-event simulator (registered by server.py)."""
import io
import json
import os
import threading
import zipfile

from flask import Blueprint, jsonify, request, send_file, send_from_directory, abort

from . import config as C
from .agent import RivalAgent
from .game import Game, HUMAN_ID, RUNS_DIR, total_spend, kill_all_procs

bp = Blueprint("sim", __name__)
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_G = {"game": None}
_lock = threading.Lock()


def game():
    return _G["game"]


@bp.get("/sim")
def page():
    return send_from_directory(os.path.join(ROOT, "static"), "sim.html")


@bp.get("/api/sim/defaults")
def defaults():
    from engine.variants import V
    from engine.scoring import POINT_PRESETS
    return jsonify({
        "base_url": C.DEFAULT_BASE_URL, "model": C.DEFAULT_MODEL, "rivals": 7, "concurrency": 3,
        "variants": [{"id": v.id, "name": v.name, "description": v.description} for v in V
                     if v.id.startswith("sf2026")],
        "points_presets": POINT_PRESETS, "prices": C.PRICES,
        "practice_iterations": 1000, "scored_iterations": 5000, "speed": 1,
        "spend_all_time": total_spend(),
    })


@bp.post("/api/sim/new")
def new_game():
    s = request.get_json(force=True) or {}
    with _lock:
        old = game()
        if old is not None:
            _halt(old)
        settings = {
            "team_name": s.get("team_name") or "Aniket & Mitch",
            "rivals": max(0, min(7, int(s.get("rivals", 7)))),
            "variant": s.get("variant") or "sf2026_reveal",
            "points": [float(x) for x in (s.get("points") or [50, 30, 20, 0, 0, 0, 0, 0])][:8],
            "practice_iterations": max(10, int(s.get("practice_iterations", 1000))),
            "scored_iterations": max(10, int(s.get("scored_iterations", 5000))),
            "speed": max(0.1, min(120.0, float(s.get("speed", 1)))),
            "model": s.get("model") or C.DEFAULT_MODEL,
            "base_url": s.get("base_url") or C.DEFAULT_BASE_URL,
            "concurrency": max(1, min(10, int(s.get("concurrency", 3)))),
            "workers": int(s.get("workers", 0)),
            "build_minutes": max(1.0, float(s.get("build_minutes", 60))),
            "practice_every": max(0.5, float(s.get("practice_every", 10))),
            "scored_gap": max(0.5, float(s.get("scored_gap", 20))),
        }
        while len(settings["points"]) < 8:
            settings["points"].append(0.0)
        g = Game(settings)
        sem = threading.Semaphore(settings["concurrency"])
        agent_settings = dict(settings, api_key=s.get("api_key") or "", _semaphore=sem)
        for t in g.teams:
            if t.kind == "ai":
                g.agents.append(RivalAgent(g, t, agent_settings))
        _G["game"] = g
        g.start()
    return jsonify({"ok": True, "id": g.id})


def _halt(g):
    """Stop the event loop, the running round's worker processes, every AI rival and every
    script or local test they started. No further model requests are sent."""
    if g is None:
        return 0
    g.stop()
    for a in g.agents:
        a.stop = True
        with a.cv:
            a.pending = []
            a.cv.notify_all()
    return kill_all_procs()


@bp.post("/api/sim/stop")
def stop():
    _halt(game())
    return jsonify({"ok": True})


@bp.post("/api/sim/reset")
def reset():
    """Kill everything and return to the setup screen."""
    with _lock:
        g = game()
        killed = _halt(g)
        _G["game"] = None
    return jsonify({"ok": True, "killed_processes": killed})


@bp.post("/api/sim/retry_agents")
def retry_agents():
    g = game()
    n = sum(1 for a in (g.agents if g else []) if a.retry())
    return jsonify({"ok": True, "retried": n})


@bp.get("/api/sim/state")
def state():
    g = game()
    if g is None:
        return jsonify({"id": None, "spend": {"game": 0.0, "all_time": total_spend()}})
    return jsonify(g.state())


@bp.post("/api/sim/submit")
def submit():
    g = game()
    if g is None:
        return jsonify({"ok": False, "problem": "no game running"}), 400
    data = _source_from_request()
    if data is None:
        return jsonify({"ok": False, "problem": "no file"}), 400
    name = request.files["file"].filename if "file" in request.files else "agent.py"
    return jsonify(g.submit(HUMAN_ID, data, name or "agent.py"))


@bp.post("/api/sim/activate")
def activate():
    g = game()
    if g is None:
        return jsonify({"ok": False, "problem": "no game running"}), 400
    return jsonify(g.set_active(HUMAN_ID, (request.get_json(force=True) or {}).get("version")))


@bp.post("/api/sim/localtest")
def localtest():
    """`poker-agent play`: a few local tables vs the kit's examples (100 ms bank), plus `check`."""
    g = game()
    variant = g.variant if g else (request.args.get("variant") or "sf2026_reveal")
    data = _source_from_request()
    if data is None:
        return jsonify({"ok": False, "problem": "no file"}), 400
    import tempfile
    from .game import run_proc
    import sys as _sys
    d = tempfile.mkdtemp(prefix="localtest_")
    is_zip = data[:4] == b"PK\x03\x04"
    p = os.path.join(d, "upload.zip" if is_zip else "agent.py")
    with open(p, "wb") as f:
        f.write(data)
    out = {}
    for cmd in ("check", "play"):
        args = [_sys.executable, "-m", "poker_agent_sdk", cmd, p, "--variant", variant] + (["--tables", "5"] if cmd == "play" else [])
        try:
            code, so, se = run_proc(args, ROOT, 300)
            out[cmd] = json.loads(so) if so.strip() else {"ok": False, "problem": se[-400:]}
        except Exception as e:
            out[cmd] = {"ok": False, "problem": str(e)}
    return jsonify(out)


def _source_from_request():
    if "file" in request.files:
        f = request.files["file"]
        return f.read()
    body = request.get_json(silent=True) or {}
    src = body.get("source")
    return src.encode("utf-8") if src else None


@bp.get("/api/sim/downloads")
def downloads():
    g = game()
    if g is None:
        return jsonify([])
    return jsonify(g.downloads(HUMAN_ID))


@bp.get("/api/sim/download/<path:rel>")
def download(rel):
    g = game()
    if g is None:
        abort(404)
    base = os.path.abspath(os.path.join(g.workspace(g.team(HUMAN_ID)), "downloads"))
    p = os.path.abspath(os.path.join(base, rel))
    if not p.startswith(base + os.sep) or not os.path.isfile(p):
        abort(404)
    return send_file(p, as_attachment=True, download_name=rel.replace("/", "_"))


@bp.get("/api/sim/kit")
def kit():
    """Starter kit zip: from the running event, or from the setup form's settings
    (?variant=&points=50,30,20&practice_iterations=&scored_iterations=)."""
    import tempfile
    from .game import KitSpec, write_starter_kit
    g = game()
    if g is not None and not request.args.get("variant"):
        spec = g
    else:
        a = request.args
        pts = [float(x) for x in (a.get("points") or "50,30,20,0,0,0,0,0").split(",") if x.strip()]
        spec = KitSpec(a.get("variant") or "sf2026_reveal", (pts + [0.0] * 8)[:8],
                       int(a.get("practice_iterations") or 1000), int(a.get("scored_iterations") or 5000))
    with tempfile.TemporaryDirectory() as d:
        write_starter_kit(d, spec)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(d):
                dirs[:] = sorted(x for x in dirs if x != "__pycache__")
                for fn in sorted(files):
                    if fn == "NOTES.md" or fn.endswith(".pyc"):
                        continue
                    full = os.path.join(root, fn)
                    info = zipfile.ZipInfo("starter_kit/" + os.path.relpath(full, d).replace(os.sep, "/"))
                    info.external_attr = (0o755 if fn == "poker-agent" else 0o644) << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with open(full, "rb") as f:
                        z.writestr(info, f.read())
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="starter_kit.zip", mimetype="application/zip")


@bp.post("/api/sim/icm")
def icm_calc():
    import importlib.util
    spec = importlib.util.spec_from_file_location("kit_icm", os.path.join(ROOT, "sim", "kit", "icm.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    b = request.get_json(force=True) or {}
    try:
        stacks = [float(x) for x in b.get("stacks", [])][:12]
        pays = [float(x) for x in b.get("payouts", [])]
        if not stacks or any(x < 0 for x in stacks) or sum(stacks) <= 0:
            return jsonify({"ok": False, "problem": "enter at least one positive stack"})
        return jsonify({"ok": True, "probs": m.place_probabilities(stacks), "values": m.icm(stacks, pays),
                        "pay": (pays + [0.0] * len(stacks))[:len(stacks)]})
    except (TypeError, ValueError) as e:
        return jsonify({"ok": False, "problem": str(e)})


@bp.get("/api/sim/agent/<tid>")
def agent_detail(tid):
    g = game()
    if g is None:
        abort(404)
    a = next((x for x in g.agents if x.team.id == tid), None)
    if a is None:
        abort(404)
    notes = ""
    try:
        with open(os.path.join(a.ws, "NOTES.md")) as f:
            notes = f.read()[-6000:]
    except Exception:
        pass
    return jsonify(dict(a.to_dict(), transcript=a.transcript[-150:], notes=notes))
