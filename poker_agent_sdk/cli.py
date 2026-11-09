"""poker-agent: local play, the upload check, and packaging (mock of the kit's command).

    python -m poker_agent_sdk play agent.py [--opponents a.py b.py ...] [--tables 20] [--variant ID]
    python -m poker_agent_sdk check agent.py|bot.zip      # the upload test match
    python -m poker_agent_sdk pack DIR OUT.zip            # zip a bot folder, then check it

play always gives a 100 ms time bank; check uses the default 1 ms with no bank, like the upload
test match, so a bot that leans on the bank can pass play and still fail check.
"""
import argparse
import json
import os
import random
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.dirname(HERE),):
    if p not in sys.path:
        sys.path.insert(0, p)

LIMITS = {"upload_bytes": 50 * 1024 * 1024, "unpacked_bytes": 200 * 1024 * 1024, "entries": 1000,
          "agent_py_bytes": 1024 * 1024, "path_chars": 512, "path_levels": 16}
DEFAULT_VARIANT = os.environ.get("POKER_VARIANT", "sf2026_reveal")
EXAMPLES = os.environ.get("POKER_EXAMPLES", "")


# ---------------------------------------------------------------------------- packages
def unpack(path, dest):
    """Validate an upload (agent.py or zip with agent.py at its root) and unpack it into dest.
    Returns (ok, problem, agent_path)."""
    if not os.path.isfile(path):
        return False, "file not found", None
    size = os.path.getsize(path)
    if size > LIMITS["upload_bytes"]:
        return False, f"upload is {size / 1e6:.1f} MB; the limit is 50 MB", None
    os.makedirs(dest, exist_ok=True)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > LIMITS["entries"]:
                return False, f"{len(infos)} archive entries; the limit is 1,000", None
            total = sum(i.file_size for i in infos)
            if total > LIMITS["unpacked_bytes"]:
                return False, f"unpacks to {total / 1e6:.0f} MB; the limit is 200 MB", None
            names = [i.filename for i in infos]
            for n in names:
                if len(n) > LIMITS["path_chars"] or n.strip("/").count("/") + 1 > LIMITS["path_levels"]:
                    return False, f"path too long or too deep: {n[:80]}", None
                if n.startswith("/") or ".." in n.split("/"):
                    return False, f"unsafe path in zip: {n[:80]}", None
            if "agent.py" not in names:
                return False, "agent.py must be at the zip's root", None
            z.extractall(dest)
        agent = os.path.join(dest, "agent.py")
    else:
        if not path.endswith(".py"):
            return False, "upload agent.py or a .zip with agent.py at its root", None
        agent = os.path.join(dest, "agent.py")
        shutil.copy(path, agent)
    if os.path.getsize(agent) > LIMITS["agent_py_bytes"]:
        return False, "agent.py is over 1 MB of code", None
    try:
        compile(open(agent, encoding="utf-8").read(), agent, "exec")
    except SyntaxError as e:
        return False, f"syntax error in agent.py line {e.lineno}: {e.msg}", None
    except UnicodeDecodeError:
        return False, "agent.py is not UTF-8 text", None
    return True, "", agent


def _load(path, bid):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    return {"id": bid, "name": os.path.basename(os.path.dirname(path)) + "/" + os.path.basename(path),
            "path": path, "code": compile(src, path, "exec")}


def _table(bots, cfg, seed, record=False):
    from engine.sandbox import BotStats
    from engine.table import Table
    from engine.tournament import BotSlot, _make_agent
    stats = [BotStats() for _ in bots]
    slots = []
    for i, b in enumerate(bots):
        slots.append(BotSlot(i, b["name"], _make_agent(b, stats[i], cfg), stats[i], True))
    for k, sl in enumerate(slots):
        if sl.agent is not None and hasattr(sl.agent, "new_table"):
            sl.agent.new_table({"seat": k, "num_players": len(slots), "starting_stack": cfg.starting_stack,
                                "blind_schedule": cfg.blind_schedule, "blind_interval": cfg.blind_interval,
                                "max_hands": cfg.max_hands, "points": list(cfg.points), "table_id": f"local-{seed}",
                                "time_limit_ms": cfg.time_limit_ms, "time_bank_ms": cfg.time_bank_ms,
                                "memory_bytes": cfg.memory_bytes, "variant": cfg.variant})
    res = Table(slots, cfg, random.Random(seed), record_seat=0 if record else None).play()
    return res, stats, slots


def check(path, variant=DEFAULT_VARIANT, examples_dir=None):
    """The upload test match: heads-up, up to 20 hands, vs a bot that checks when free and
    otherwise folds; 1 ms per decision, no time bank. Fails on startup error, crash, or more
    than 10% failed decisions."""
    from engine.sandbox import install_alarm
    from engine.tournament import Config
    install_alarm()
    with tempfile.TemporaryDirectory() as d:
        ok, problem, agent = unpack(path, d)
        if not ok:
            return {"ok": False, "problem": problem, "log": []}
        cfg = Config(variant=variant, table_size=2, max_hands=20, time_bank_ms=0.0).validate()
        cof = os.path.join(examples_dir or _examples_dir(), "check_or_fold.py")
        try:
            me = _load(agent, "upload")
        except Exception as e:
            return {"ok": False, "problem": f"could not load agent.py: {e}", "log": []}
        res, stats, slots = _table([me, _load(cof, "check_or_fold")], cfg, 1)
        st = stats[0]
        log = [f"heads-up test match: {res.hands_played} hands, variant {variant}, 1 ms per decision, no bank"]
        from poker_agent_sdk._host import SDKHost
        if slots[0].agent is not None and not isinstance(slots[0].agent, SDKHost):
            return {"ok": False, "problem": "agent.py must define Agent(poker_agent_sdk.Agent) with "
                                            "act(self, state, memory) -> (Action, bytes)", "log": log}
        if slots[0].agent is None:
            sample = st.error_samples[0] if st.error_samples else "Agent() could not be created"
            return {"ok": False, "problem": f"did not start: {sample}", "log": log}
        failed = st.timeouts + st.errors + st.invalid
        rate = failed / max(1, st.decisions)
        log.append(f"decisions {st.decisions}, failed {failed} (timeouts {st.timeouts}, errors {st.errors}, "
                   f"illegal {st.invalid}), max {1000 * st.time_max:.2f} ms")
        log += [f"error: {e}" for e in st.error_samples]
        if st.decisions == 0:
            return {"ok": False, "problem": "made no decisions", "log": log}
        if rate > 0.10:
            return {"ok": False, "problem": f"failed {100 * rate:.0f}% of its decisions (limit 10%)", "log": log}
        return {"ok": True, "problem": "", "log": log, "failed_rate": rate}


def _examples_dir():
    if EXAMPLES:
        return EXAMPLES
    for c in (os.path.join(os.path.dirname(HERE), "..", "examples"),          # kit layout: sdk/../examples
              os.path.join(os.path.dirname(HERE), "sim", "kit", "examples")):  # repository layout
        if os.path.isdir(c):
            return os.path.abspath(c)
    raise SystemExit("examples/ folder not found")


def play(path, opponents=None, tables=10, variant=DEFAULT_VARIANT, seed=1):
    from engine.sandbox import install_alarm
    from engine.tournament import Config
    install_alarm()
    with tempfile.TemporaryDirectory() as d:
        ok, problem, agent = unpack(path, d)
        if not ok:
            return {"ok": False, "problem": problem}
        ex = _examples_dir()
        opp = opponents or [os.path.join(ex, "check_or_fold.py"), os.path.join(ex, "min_raiser.py")]
        cfg = Config(variant=variant, time_bank_ms=100.0).validate()
        me = _load(agent, "you")
        pool = [_load(p, f"opp{i}") for i, p in enumerate(opp)]
        rng = random.Random(seed)
        out = []
        tot = None
        for t in range(tables):
            seats = [me] + [rng.choice(pool) for _ in range(7)]
            res, stats, slots = _table(seats, cfg, seed * 1000 + t)
            st = stats[0]
            out.append({"place": res.places[0] or 8, "points": res.points[0], "hands": res.hands_played})
            if tot is None:
                tot = st
            else:
                tot.merge(st)
        return {"ok": True, "tables": out, "avg_points": sum(x["points"] for x in out) / len(out),
                "decisions": tot.decisions, "timeouts": tot.timeouts, "errors": tot.errors, "illegal": tot.invalid,
                "avg_ms": round(1000 * tot.time_total / max(1, tot.decisions), 3),
                "max_ms": round(1000 * tot.time_max, 3), "error_samples": list(tot.error_samples)}


def pack(src_dir, out_zip, variant=DEFAULT_VARIANT):
    src_dir = os.path.abspath(src_dir)
    if not os.path.isfile(os.path.join(src_dir, "agent.py")):
        return {"ok": False, "problem": f"{src_dir} has no agent.py at its root"}
    entries = []
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__"
                         and not os.path.islink(os.path.join(root, d)))   # skip symlinked folders
        for f in sorted(files):
            if f.startswith("."):
                continue
            p = os.path.join(root, f)
            if os.path.islink(p):
                return {"ok": False, "problem": f"refusing symlinked file {os.path.relpath(p, src_dir)}"}
            entries.append(p)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for p in entries:
            info = zipfile.ZipInfo(os.path.relpath(p, src_dir).replace(os.sep, "/"), date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(p, "rb") as f:
                z.writestr(info, f.read())
    r = check(out_zip, variant)
    r["zip"] = out_zip
    r["entries"] = len(entries)
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(prog="poker-agent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("play", help="play local tables (100 ms bank)")
    p.add_argument("agent")
    p.add_argument("--opponents", nargs="*")
    p.add_argument("--tables", type=int, default=10)
    p.add_argument("--variant", default=DEFAULT_VARIANT)
    p.add_argument("--seed", type=int, default=1)
    c = sub.add_parser("check", help="run the upload test match")
    c.add_argument("agent")
    c.add_argument("--variant", default=DEFAULT_VARIANT)
    k = sub.add_parser("pack", help="zip a bot folder reproducibly, then check it")
    k.add_argument("dir")
    k.add_argument("out")
    k.add_argument("--variant", default=DEFAULT_VARIANT)
    a = ap.parse_args(argv)
    if a.cmd == "play":
        r = play(a.agent, a.opponents, a.tables, a.variant, a.seed)
    elif a.cmd == "check":
        r = check(a.agent, a.variant)
    else:
        r = pack(a.dir, a.out, a.variant)
    real = getattr(sys, "__stdout__", sys.stdout)
    real.write(json.dumps(r, indent=1) + "\n")
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
