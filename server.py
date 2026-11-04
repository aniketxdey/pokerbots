"""Web UI for running mock tournaments.

    pip install -r requirements.txt
    python server.py            # then open http://127.0.0.1:5000
"""
import json
import os
import shutil
import sys
import threading
import time
import traceback

from flask import Flask, jsonify, request, send_from_directory, abort

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from engine.loader import list_bot_files, inspect_bot, MAX_BOTS, MAX_FILE_BYTES  # noqa: E402
from engine.scoring import POINT_PRESETS  # noqa: E402
from engine.tournament import Config, run_tournament, save_results, combine, DEFAULT_BLINDS  # noqa: E402

BOTS_DIR = os.path.join(ROOT, "bots")
RESULTS_DIR = os.path.join(ROOT, "results")

app = Flask(__name__, static_folder=os.path.join(ROOT, "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_BYTES + 1024 * 1024

_lock = threading.Lock()
_state = {"phase": "idle"}
_stop = threading.Event()
_thread = None


def _set(**kw):
    with _lock:
        _state.update(kw)


@app.get("/")
def index():
    return send_from_directory(os.path.join(ROOT, "static"), "index.html")


@app.get("/api/defaults")
def defaults():
    return jsonify({"config": Config().to_dict(), "point_presets": POINT_PRESETS,
                    "blinds": DEFAULT_BLINDS, "max_bots": MAX_BOTS, "cpu_count": os.cpu_count()})


@app.get("/api/bots")
def bots():
    out = []
    for p in list_bot_files(BOTS_DIR):
        info = inspect_bot(p)
        info.pop("path", None)
        out.append(info)
    return jsonify(out)


@app.get("/api/bots/<bot_id>/source")
def bot_source(bot_id):
    path = os.path.join(BOTS_DIR, os.path.basename(bot_id) + ".py")
    if not os.path.isfile(path):
        abort(404)
    with open(path, encoding="utf-8") as f:
        return jsonify({"id": bot_id, "source": f.read()})


@app.post("/api/bots")
def upload_bots():
    files = request.files.getlist("files")
    saved, errors = [], []
    existing = {os.path.basename(p) for p in list_bot_files(BOTS_DIR)}
    for f in files:
        name = os.path.basename(f.filename or "")
        if not name.endswith(".py") or name.startswith("_"):
            errors.append(f"{name}: must be a .py file not starting with '_'")
            continue
        safe = "".join(c if (c.isalnum() or c in "_-.") else "_" for c in name)
        if safe not in existing and len(existing) >= MAX_BOTS:
            errors.append(f"{name}: the bots folder already has {MAX_BOTS} bots")
            continue
        path = os.path.join(BOTS_DIR, safe)
        tmp = path + ".upload"
        f.save(tmp)
        info = inspect_bot(tmp)
        if not info["valid"] and info["problem"] != "":
            os.remove(tmp)
            errors.append(f"{name}: {info['problem']}")
            continue
        os.replace(tmp, path)
        existing.add(safe)
        saved.append(safe)
    return jsonify({"saved": saved, "errors": errors})


@app.delete("/api/bots/<bot_id>")
def delete_bot(bot_id):
    """Moves the file to bots_removed/ (never deletes)."""
    path = os.path.join(BOTS_DIR, os.path.basename(bot_id) + ".py")
    if not os.path.isfile(path):
        abort(404)
    dest_dir = os.path.join(ROOT, "bots_removed")
    os.makedirs(dest_dir, exist_ok=True)
    shutil.move(path, os.path.join(dest_dir, os.path.basename(path)))
    return jsonify({"ok": True})


def _runner(cfg, paths):
    rounds_done = []

    def progress(r, done, total, summary, elapsed):
        rate = done / elapsed if elapsed > 0 else 0
        partial_round = {"round": r + 1, "bots": summary}
        board = combine(rounds_done + [partial_round], cfg)
        _set(phase="running", round=r + 1, done=done, total=total, elapsed=elapsed,
             eta=(total - done) / rate if rate > 0 else None, leaderboard=board,
             current_round=summary)

    def on_round(res, board):
        rounds_done.append(res)
        _set(rounds=[{k: v for k, v in r.items()} for r in rounds_done], leaderboard=board)

    def wait_cb(r, remaining):
        _set(phase="waiting", next_round=r + 1, wait_remaining=remaining)

    try:
        _set(phase="running", round=1, done=0, total=cfg.iterations, started=time.time(),
             config=cfg.to_dict(), rounds=[], leaderboard=[], error=None, result_file=None)
        res = run_tournament(cfg, paths, progress=progress, stop=_stop, on_round=on_round,
                             wait_cb=wait_cb)
        path = save_results(res, RESULTS_DIR)
        _set(phase="stopped" if _stop.is_set() else "done", leaderboard=res["leaderboard"],
             rounds=res["rounds"], result_file=os.path.basename(path), finished=time.time())
    except Exception as e:
        _set(phase="error", error=f"{e}\n{traceback.format_exc(limit=4)}")


@app.post("/api/run")
def run():
    global _thread
    if _thread is not None and _thread.is_alive():
        return jsonify({"error": "a tournament is already running"}), 409
    body = request.get_json(force=True) or {}
    cfg = Config.from_dict(body.get("config", {}))
    ids = body.get("bots")
    all_paths = list_bot_files(BOTS_DIR)
    if ids:
        ids = set(ids)
        paths = [p for p in all_paths if os.path.splitext(os.path.basename(p))[0] in ids]
    else:
        paths = all_paths
    paths = [p for p in paths if inspect_bot(p)["valid"]]
    if len(paths) < 2:
        return jsonify({"error": "select at least 2 valid bots"}), 400
    if len(paths) > MAX_BOTS:
        return jsonify({"error": f"at most {MAX_BOTS} bots"}), 400
    _stop.clear()
    with _lock:
        _state.clear()
        _state["phase"] = "starting"
    _thread = threading.Thread(target=_runner, args=(cfg, paths), daemon=True)
    _thread.start()
    return jsonify({"ok": True, "bots": len(paths)})


@app.post("/api/stop")
def stop():
    _stop.set()
    return jsonify({"ok": True})


@app.get("/api/status")
def status():
    with _lock:
        s = dict(_state)
    # sample logs are big; the UI fetches them separately
    if "rounds" in s:
        s["rounds"] = [{k: v for k, v in r.items() if k != "sample_log"} for r in s["rounds"]]
    return jsonify(s)


@app.get("/api/log/<int:round_no>")
def log(round_no):
    with _lock:
        rounds = list(_state.get("rounds", []))
    for r in rounds:
        if r["round"] == round_no:
            return jsonify({"round": round_no, "lines": r.get("sample_log", [])})
    abort(404)


@app.get("/api/runs")
def runs():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    out = []
    for fn in sorted(os.listdir(RESULTS_DIR), reverse=True):
        if fn.endswith(".json"):
            out.append(fn)
    return jsonify(out)


@app.get("/api/runs/<name>")
def get_run(name):
    path = os.path.join(RESULTS_DIR, os.path.basename(name))
    if not os.path.isfile(path):
        abort(404)
    with open(path) as f:
        return jsonify(json.load(f))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    a = ap.parse_args()
    print(f"Mock tournament UI: http://{a.host}:{a.port}")
    app.run(host=a.host, port=a.port, threaded=True, debug=False)
