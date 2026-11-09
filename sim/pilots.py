"""Pilot strategies: freeze a set of starting bots so events can start from them without ever
changing the originals.

Put the bots in pilots/incoming/ (each an agent.py-style .py file, a folder with agent.py, or a
zip with agent.py at its root), then freeze them as a named set:

    python -m sim.pilots freeze night1        # or the "Freeze" button on the page
    python -m sim.pilots list

A frozen set lives in pilots/frozen/<name>/<pilot>/agent.py with a manifest (sha256 and the
upload test-match result per pilot). Files are made read-only. Every event copies the pilots
into its own team workspaces, so agents only ever edit copies.
"""
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PILOTS = os.path.join(ROOT, "pilots")
INCOMING = os.path.join(PILOTS, "incoming")
FROZEN = os.path.join(PILOTS, "frozen")


def _slug(name):
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-.")
    return s[:60] or "pilot"


def list_incoming():
    os.makedirs(INCOMING, exist_ok=True)
    out = []
    for fn in sorted(os.listdir(INCOMING)):
        if fn.startswith("."):
            continue
        p = os.path.join(INCOMING, fn)
        if os.path.isdir(p) and os.path.isfile(os.path.join(p, "agent.py")):
            out.append({"id": _slug(fn), "source": fn, "kind": "folder"})
        elif fn.endswith(".py") or fn.endswith(".zip"):
            out.append({"id": _slug(os.path.splitext(fn)[0]), "source": fn, "kind": fn.rsplit(".", 1)[-1]})
    return out


def _sha(path):
    h = hashlib.sha256()
    for root, dirs, files in sorted(os.walk(path)):
        dirs.sort()
        for f in sorted(files):
            with open(os.path.join(root, f), "rb") as fh:
                h.update(f.encode() + b"\0" + fh.read())
    return h.hexdigest()[:16]


def _readonly(path):
    for root, dirs, files in os.walk(path):
        for f in files:
            os.chmod(os.path.join(root, f), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    for root, dirs, files in os.walk(path, topdown=False):
        for d in dirs:
            os.chmod(os.path.join(root, d), stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


def freeze(name, variant="sf2026_reveal_preflop", check=True):
    """Copy everything in pilots/incoming/ into pilots/frozen/<name>/, check each, lock it."""
    from poker_agent_sdk.cli import unpack, check as run_check
    name = _slug(name)
    dest = os.path.join(FROZEN, name)
    if os.path.exists(dest):
        return {"ok": False, "problem": f"a frozen set named '{name}' already exists"}
    items = list_incoming()
    if not items:
        return {"ok": False, "problem": "pilots/incoming/ is empty"}
    os.makedirs(dest)
    seen = set()
    pilots = []
    for it in items:
        pid = it["id"]
        while pid in seen:
            pid += "_"
        seen.add(pid)
        src = os.path.join(INCOMING, it["source"])
        pdir = os.path.join(dest, pid)
        if it["kind"] == "folder":
            shutil.copytree(src, pdir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".*"))
            ok, problem = True, ""
        else:
            ok, problem, _ = unpack(src, pdir)
        rec = {"id": pid, "source": it["source"], "ok": ok, "problem": problem}
        if ok:
            rec["sha256"] = _sha(pdir)
            if check:
                with tempfile.TemporaryDirectory() as td:
                    z = shutil.make_archive(os.path.join(td, "bot"), "zip", pdir)
                    r = run_check(z, variant)
                rec["ok"], rec["problem"], rec["log"] = r.get("ok", False), r.get("problem", ""), r.get("log", [])
        pilots.append(rec)
    manifest = {"name": name, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "variant_checked": variant,
                "pilots": pilots}
    with open(os.path.join(dest, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    _readonly(dest)
    return {"ok": True, "set": manifest}


def list_sets():
    os.makedirs(FROZEN, exist_ok=True)
    out = []
    for name in sorted(os.listdir(FROZEN)):
        m = os.path.join(FROZEN, name, "manifest.json")
        if os.path.isfile(m):
            with open(m) as f:
                out.append(json.load(f))
    return out


def get_set(name):
    for s in list_sets():
        if s["name"] == name:
            return s
    return None


def pilot_path(set_name, pid):
    return os.path.join(FROZEN, set_name, pid, "agent.py")


def main():
    a = sys.argv[1:]
    if a[:1] == ["freeze"] and len(a) >= 2:
        sys.path.insert(0, ROOT)
        r = freeze(a[1], a[2] if len(a) > 2 else "sf2026_reveal_preflop")
        print(json.dumps(r, indent=1))
    elif a[:1] == ["list"]:
        for s in list_sets():
            print(s["name"], s["created"], [(p["id"], "ok" if p["ok"] else p["problem"]) for p in s["pilots"]])
        print("incoming:", [i["source"] for i in list_incoming()])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
