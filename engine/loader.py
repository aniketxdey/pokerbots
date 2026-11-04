"""Discover bot files and freeze snapshots of their source code."""
import ast
import hashlib
import os
import time

MAX_BOTS = 64
MAX_FILE_BYTES = 100 * 1024 * 1024  # official max file size: 100mb (§3.7)
META_KEYS = ("BOT_NAME", "BOT_AUTHOR", "BOT_SOURCE", "BOT_DESCRIPTION", "BOT_LICENSE")


def bot_id_from_path(path):
    return os.path.splitext(os.path.basename(path))[0]


def list_bot_files(bots_dir):
    out = []
    if not os.path.isdir(bots_dir):
        return out
    for fn in sorted(os.listdir(bots_dir)):
        if fn.endswith(".py") and not fn.startswith("_"):
            out.append(os.path.join(bots_dir, fn))
    return out


def inspect_bot(path):
    """Read metadata without executing the bot."""
    info = {"id": bot_id_from_path(path), "file": os.path.basename(path), "path": path,
            "name": bot_id_from_path(path), "author": "", "source": "", "description": "",
            "license": "", "valid": False, "problem": "", "size": 0, "mtime": 0}
    try:
        st = os.stat(path)
        info["size"] = st.st_size
        info["mtime"] = st.st_mtime
        if st.st_size > MAX_FILE_BYTES:
            info["problem"] = "file larger than 100 MB"
            return info
        with open(path, "r", encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        info["problem"] = f"syntax error line {e.lineno}: {e.msg}"
        return info
    except Exception as e:
        info["problem"] = f"cannot read: {e}"
        return info
    has_agent = False
    seen = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Agent":
            has_agent = True
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    if t.id == "Agent":
                        has_agent = True
                    if (t.id in META_KEYS and t.id not in seen and isinstance(node.value, ast.Constant)
                            and isinstance(node.value.value, str)):
                        seen.add(t.id)  # first assignment wins (header metadata)
                        info[t.id[4:].lower()] = node.value.value
    if not has_agent:
        info["problem"] = "no top-level `Agent` class"
        return info
    info["valid"] = True
    return info


def snapshot(paths):
    """Freeze the current source of each bot (official rules §4.2: the active bot is frozen
    at the start of each scored tournament). Returns a list of dicts."""
    snap = []
    for p in paths:
        info = inspect_bot(p)
        if not info["valid"]:
            continue
        with open(p, "r", encoding="utf-8") as f:
            info["code"] = f.read()
        info["hash"] = hashlib.sha1(info["code"].encode("utf-8")).hexdigest()[:10]  # bot version
        info["frozen_at"] = time.time()
        snap.append(info)
    return snap[:MAX_BOTS]
