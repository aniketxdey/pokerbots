"""Flask routes for the practice-event simulator (registered by server.py)."""
import io
import json
import os
import threading
import zipfile

from flask import Blueprint, jsonify, request, send_file, send_from_directory, abort

from . import config as C
from .agent import RivalAgent
from .game import Game, HUMAN_ID, RUNS_DIR, total_spend, run_local_test

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
        "practice_iterations": 1000, "scored_iterations": 10000, "speed": 1,
        "spend_all_time": total_spend(),
    })


@bp.post("/api/sim/new")
def new_game():
    s = request.get_json(force=True) or {}
    with _lock:
        old = game()
        if old is not None and not old.finished:
            old.stop()
            for a in old.agents:
                a.stop = True
        settings = {
            "team_name": s.get("team_name") or "Aniket & Mitch",
            "rivals": max(0, min(7, int(s.get("rivals", 7)))),
            "variant": s.get("variant") or "sf2026_reveal",
            "points": [float(x) for x in (s.get("points") or [50, 30, 20, 0, 0, 0, 0, 0])][:8],
            "practice_iterations": max(10, int(s.get("practice_iterations", 1000))),
            "scored_iterations": max(10, int(s.get("scored_iterations", 10000))),
            "speed": max(0.1, min(120.0, float(s.get("speed", 1)))),
            "model": s.get("model") or C.DEFAULT_MODEL,
            "base_url": s.get("base_url") or C.DEFAULT_BASE_URL,
            "concurrency": max(1, min(10, int(s.get("concurrency", 3)))),
            "workers": int(s.get("workers", 0)),
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


@bp.post("/api/sim/stop")
def stop():
    g = game()
    if g is not None:
        g.stop()
        for a in g.agents:
            a.stop = True
    return jsonify({"ok": True})


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
    return jsonify(g.submit(HUMAN_ID, data))


@bp.post("/api/sim/localtest")
def localtest():
    g = game()
    if g is None:
        return jsonify({"ok": False, "problem": "no game running"}), 400
    data = _source_from_request()
    if data is None:
        return jsonify({"ok": False, "problem": "no file"}), 400
    p = os.path.join(g.workspace(g.team(HUMAN_ID)), "_localtest_bot.py")
    with open(p, "wb") as f:
        f.write(data)
    return jsonify(run_local_test(p, g.variant, 3))


def _source_from_request():
    if "file" in request.files:
        return request.files["file"].read()
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
    g = game()
    if g is None:
        abort(404)
    ws = g.workspace(g.team(HUMAN_ID))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for fn in ("RULES.md", "api.py", "kit_example_bot.py"):
            z.write(os.path.join(ws, fn), fn)
        z.write(os.path.join(ROOT, "sim", "localtest.py"), "localtest.py")
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="starter_kit.zip", mimetype="application/zip")


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
