"""AI rival teams: each is a tool-using coding agent that works on its own bot.

An agent iterates only when there is something new to look at: once at the start of the event
(the starter kit) and once after every round (practice and scored) when that round's downloads
arrive. Each iteration is a fresh conversation; the team keeps continuity through its NOTES.md
file and its workspace, like a team that reopens its coding assistant after each round.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback

from . import config as C
from .game import add_spend, run_proc
from .llm import LLMError, OutOfCreditsError, make_client

MAX_STEPS = 40            # tool calls per iteration (a long Codex-style session)
READ_LIMIT = 400          # lines per read_file call
OUT_LIMIT = 12000         # characters of tool output returned to the model

TOOLS = [
    {"type": "function", "function": {
        "name": "list_files", "description": "List files and folders in your workspace (or a subfolder).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": f"Read a text file with line numbers. Up to {READ_LIMIT} lines per call; use offset for more.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "offset": {"type": "integer", "description": "first line, 1-based"},
            "limit": {"type": "integer"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "search", "description": "Search files for a regular expression; returns matching lines.",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Create or overwrite a file in your workspace.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "edit_file", "description": "Replace an exact, unique piece of text in a file.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
            "required": ["path", "old", "new"]}}},
    {"type": "function", "function": {
        "name": "run_python",
        "description": "Run a Python script you wrote (e.g. to analyze hand_history.jsonl). Working directory is "
                       "your workspace. 60 s limit. Returns stdout and stderr.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "poker_agent",
        "description": "Run the starter kit's poker-agent command. 'play' plays local tables vs examples/ "
                       "(100 ms bank) and reports places, failed decisions and timings; 'check' runs the upload "
                       "test match (heads-up, 1 ms, no bank).",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "enum": ["play", "check"]}, "path": {"type": "string"},
            "tables": {"type": "integer"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "submit",
        "description": "Upload a bot: agent.py, or a folder (zipped for you) with agent.py at its root. One upload "
                       "every 30 s. It plays a heads-up test match; if it passes it becomes your active bot "
                       "(used from the next round). Returns the result and the validation log.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "set_active",
        "description": "Make an earlier upload (that passed its test match) your active bot.",
        "parameters": {"type": "object", "properties": {"version": {"type": "integer"}},
                       "required": ["version"]}}},
    {"type": "function", "function": {
        "name": "finish", "description": "End this work session with a short summary of what you did.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
]


def _fmt_t(s):
    s = max(0, int(s))
    return f"{s // 60}:{s % 60:02d}"


class RivalAgent:
    def __init__(self, game, team, settings):
        self.game = game
        self.team = team
        self.ws = os.path.abspath(game.workspace(team))
        self.base_url = settings.get("base_url") or C.DEFAULT_BASE_URL
        self.model = settings.get("model") or C.DEFAULT_MODEL
        self.api_key = settings.get("api_key") or ""
        self.prices = C.price_for(self.model)
        self.sem = settings["_semaphore"]
        self.status = "waiting"
        self.detail = ""
        self.iterations = 0
        self.steps = 0
        self.tokens_in = 0
        self.tokens_cached = 0
        self.tokens_out = 0
        self.cost = 0.0
        self.last_summary = ""
        self.pending = []
        self.cv = threading.Condition()
        self.transcript = []      # [{t, role, text}]
        self.blocked_event = None
        self.stop = False
        game.listeners.append(self.on_event)
        threading.Thread(target=self._run, daemon=True).start()

    # ------------------------------------------------------------ triggers
    def on_event(self, ev):
        if ev["type"] not in ("start", "round"):
            return
        if ev["type"] == "round" and self.game.next_event() is None:
            return  # nothing left to submit for: don't spend tokens
        with self.cv:
            # if new data arrives while we're busy, only the latest drop is worked on next
            self.pending = [ev]
            self.cv.notify()

    def _run(self):
        while not self.stop:
            with self.cv:
                while not self.pending and not self.stop:
                    self.cv.wait(1.0)
                    if self.game.finished:
                        self.status = "done"
                        if not self.pending:
                            return
                if self.stop:
                    return
                ev = self.pending.pop()
            if self.game.finished and ev["type"] != "start":
                self.status = "done"
                return
            try:
                self._iterate(ev)
            except OutOfCreditsError as e:
                self.status = "out_of_credits"
                self.detail = str(e)[:300]
                self.blocked_event = ev          # resumed by "Retry" once credits are added
                self._note("system", "OUT OF CREDITS " + str(e))
                self.game._log(f"{self.team.name}: model refused the request - out of credits / usage limit")
                continue
            except Exception as e:
                self.status = "error"
                self.detail = f"{type(e).__name__}: {e}"
                self._note("system", "ERROR " + traceback.format_exc(limit=2))
            if self.stop or self.game.stopped:
                self.status = "stopped"
                return
            if self.status not in ("error",):
                self.status = "waiting"
                self.detail = "waiting for the next round's data"

    # ------------------------------------------------------------ one work session
    def _iterate(self, ev):
        self.iterations += 1
        self.status = "working"
        g = self.game
        if ev["type"] == "start":
            news = ("The variant has just been announced and the build phase started. Read README.md, RULES.md, "
                    "AGENTS.md and the template agent.py, then build and submit your first bot. Practice runs every "
                    f"{g.practice_every:g} minutes; scored round 1 starts at minute {g.build_minutes:g}. "
                    "A team without an active bot forfeits a round.")
        else:
            rd = ev["round"]
            tag = f"{rd['kind']}_{rd['n']}"
            news = (f"{rd['label']} results are in. Downloads are in downloads/{tag}/ (results.json, "
                    f"my_bot_stats.json, hand_history_subset.jsonl.gz and, if enabled, hand_history_full.jsonl.gz - read them with hand-history/reader.py). "
                    f"Use them to improve your bot.")
        nxt = g.next_event()
        clock = (f"Event time {_fmt_t(g.event_time())}. "
                 + (f"Next: {nxt and ('Practice round %d' % nxt['n'] if nxt['kind'] == 'practice' else 'Scored round %d' % nxt['n'])}"
                    f" freezes submissions in {_fmt_t(nxt['at'] - g.event_time())}." if nxt else "No rounds left."))
        system = (
            f"You are the AI coding assistant of {self.team.name}, a team in a pokerbot competition (the No Limit "
            f"regional). You work autonomously with the tools provided, inside the team's workspace.\n\n"
            f"Team style: {self.team.persona}\n\n"
            "Goal: finish as high as possible on the final weighted leaderboard. The rules are in RULES.md. Your bot "
            "is agent.py implementing act(state, memory) from poker_agent_sdk (see README.md, RULES.md, AGENTS.md), "
            "1 ms per decision. Test with poker_agent (check, play) before submitting. Keep NOTES.md up to date with what you tried and what the data showed (it is your memory "
            "between sessions). When you are done for now, call finish with a short summary.\n\n"
            "Time matters like at the real event: each round freezes your ACTIVE bot at its start, and a team "
            "without one forfeits (0 points). So in your first session, get a legal bot uploaded fast (a few "
            "tool calls: read README.md/RULES.md/agent.py, make agent.py legal for the variant phases, run "
            "poker_agent check, submit) BEFORE exploring further, then improve. Don't study the engine "
            "internals in sdk/engine unless you need a specific detail. Every tool result ends with the event "
            "clock and the time left until the next freeze; plan around it. Hand histories are gzipped: read "
            "them with a run_python script (see hand-history/reader.py), not read_file.")
        user = (f"{news}\n\n{clock}\n"
                f"Your last session's summary: {self.last_summary or '(none - first session)'}")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        self._note("user", user)
        client = make_client(self.base_url, self.model, self.api_key)
        nudged = False
        for step in range(MAX_STEPS):
            if g.stopped or self.stop:
                self.status = "stopped"
                return
            if not nudged and self.team.active is None and step >= 6:
                nudged = True
                nxt = g.next_event()
                left = _fmt_t(nxt["at"] - g.event_time()) if nxt else "?"
                messages.append({"role": "user", "content":
                                 f"Reminder: you still have NO active bot and the next round freezes in {left}. "
                                 "Upload a legal agent.py now (poker_agent check, then submit), then keep improving."})
            self.detail = f"session {self.iterations}, step {step + 1}"
            self.status = "queued"
            with self.sem:
                if g.stopped or self.stop:  # reset while queued: never send the request
                    self.status = "stopped"
                    return
                self.status = "working"
                msg, usage = client.chat(messages, TOOLS)
            self._account(usage)  # a request already in flight at reset is still billed
            if g.stopped or self.stop:
                self.status = "stopped"
                return
            calls = msg.get("tool_calls") or []
            out_msg = {"role": "assistant", "content": msg.get("content") or ""}
            if calls:
                out_msg["tool_calls"] = calls
            messages.append(out_msg)
            if msg.get("content"):
                self._note("assistant", msg["content"])
            if not calls:
                self.last_summary = (msg.get("content") or "")[:800]
                return
            finished = False
            for call in calls:
                if g.stopped or self.stop:
                    self.status = "stopped"
                    return
                fn = call.get("function", {})
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                self.steps += 1
                result = self._tool(name, args)
                self._note("tool", f"{name}({_short(args)}) -> {result[:300]}")
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "name": name,
                                 "content": result[:OUT_LIMIT] + self._clock_line()})
                if name == "finish":
                    self.last_summary = str(args.get("summary", ""))[:800]
                    finished = True
            if finished:
                return
        self.last_summary = "(session ended at the step limit)"

    # ------------------------------------------------------------ tools
    def _path(self, p):
        p = (p or ".").strip()
        full = os.path.abspath(os.path.join(self.ws, p))
        if not (full == self.ws or full.startswith(self.ws + os.sep)):
            raise ValueError("path is outside your workspace")
        return full

    def _tool(self, name, a):
        try:
            if name == "list_files":
                base = self._path(a.get("path", "."))
                out = []
                for root, dirs, files in os.walk(base):
                    dirs[:] = sorted(d for d in dirs if d != "__pycache__")
                    rel = os.path.relpath(root, self.ws)
                    for fn in sorted(files):
                        p = os.path.join(root, fn)
                        out.append(f"{os.path.normpath(os.path.join(rel, fn))}  ({os.path.getsize(p)} bytes)")
                return "\n".join(out[:400]) or "(empty)"
            if name == "read_file":
                p = self._path(a["path"])
                off = max(1, int(a.get("offset") or 1))
                lim = max(1, min(READ_LIMIT, int(a.get("limit") or READ_LIMIT)))
                with open(p, errors="replace") as f:
                    lines = f.readlines()
                chunk = lines[off - 1: off - 1 + lim]
                body = "".join(f"{off + i:5d}|{ln[:2000]}" for i, ln in enumerate(chunk))
                more = len(lines) - (off - 1 + len(chunk))
                return body + (f"\n... {more} more lines (use offset)" if more > 0 else "")
            if name == "search":
                rx = re.compile(a["pattern"])
                base = self._path(a.get("path", "."))
                hits = []
                files = [base] if os.path.isfile(base) else [
                    os.path.join(r, f) for r, _, fs in os.walk(base) for f in fs]
                for p in files:
                    try:
                        with open(p, errors="replace") as f:
                            for i, ln in enumerate(f, 1):
                                if rx.search(ln):
                                    hits.append(f"{os.path.relpath(p, self.ws)}:{i}: {ln.rstrip()[:300]}")
                                    if len(hits) >= 200:
                                        break
                    except Exception:
                        continue
                    if len(hits) >= 200:
                        break
                return "\n".join(hits) or "no matches"
            if name == "write_file":
                p = self._path(a["path"])
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as f:
                    f.write(a.get("content", ""))
                return f"wrote {len(a.get('content', ''))} characters to {a['path']}"
            if name == "edit_file":
                p = self._path(a["path"])
                with open(p) as f:
                    src = f.read()
                n = src.count(a["old"])
                if n != 1:
                    return f"edit failed: the text occurs {n} times (it must occur exactly once)"
                with open(p, "w") as f:
                    f.write(src.replace(a["old"], a["new"], 1))
                return "edited"
            if name == "run_python":
                p = self._path(a["path"])
                code, out, err = run_proc([sys.executable, p], self.ws, 60, env=self._env())
                return f"exit code {code}\n--- stdout\n{out[-6000:]}\n--- stderr\n{err[-3000:]}"
            if name == "poker_agent":
                cmd = a.get("command", "play")
                p = self._path(a.get("path") or "agent.py")
                args = [sys.executable, "-m", "poker_agent_sdk", cmd, p, "--variant", self.game.variant]
                if cmd == "play":
                    args += ["--tables", str(max(1, min(20, int(a.get("tables") or 5))))]
                self.detail = f"running poker-agent {cmd}"
                code, out, err = run_proc(args, self.ws, 300, env=self._env())
                return (out or err)[-OUT_LIMIT:]
            if name == "submit":
                p = self._path(a.get("path") or "agent.py")
                if os.path.isdir(p):
                    import tempfile
                    from poker_agent_sdk.cli import pack
                    z = os.path.join(tempfile.mkdtemp(prefix="pack_"), "bot.zip")
                    r = pack(p, z, self.game.variant)
                    if not os.path.exists(z):
                        return json.dumps(r)
                    p = z
                with open(p, "rb") as f:
                    data = f.read()
                self.detail = "uploading (test match)"
                return json.dumps(self.game.submit(self.team.id, data, os.path.basename(p)))
            if name == "set_active":
                return json.dumps(self.game.set_active(self.team.id, a.get("version")))
            if name == "finish":
                return "session finished"
            return f"unknown tool {name}"
        except subprocess.TimeoutExpired:
            return "timed out after 60 s"
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"

    def _clock_line(self):
        g = self.game
        t = g.event_time()
        nxt = g.next_event()
        tm = self.team
        act = f"active bot v{tm.active}" if tm.active else "NO active bot yet"
        if nxt:
            lbl = ("practice round %d" % nxt["n"]) if nxt["kind"] == "practice" else ("scored round %d" % nxt["n"])
            return f"\n[event clock {_fmt_t(t)} | {lbl} freezes in {_fmt_t(nxt['at'] - t)} | {act}]"
        return f"\n[event clock {_fmt_t(t)} | no rounds left | {act}]"

    def _env(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = os.path.join(self.ws, "sdk")
        return env

    # ------------------------------------------------------------ bookkeeping
    def _account(self, usage):
        pin = int(usage.get("prompt_tokens") or 0)
        pout = int(usage.get("completion_tokens") or 0)
        cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
        cached = min(cached, pin)
        p_in, p_cache, p_out = self.prices
        usd = ((pin - cached) * p_in + cached * p_cache + pout * p_out) / 1e6
        self.tokens_in += pin
        self.tokens_cached += cached
        self.tokens_out += pout
        self.cost += usd
        add_spend(usd)

    def _note(self, role, text):
        self.transcript.append({"t": round(self.game.event_time()), "role": role, "text": str(text)[:4000]})
        self.transcript = self.transcript[-400:]

    def retry(self):
        """Resume after credits were added: redo the session that was refused."""
        if self.status != "out_of_credits":
            return False
        ev, self.blocked_event = self.blocked_event, None
        self.status = "waiting"
        self.detail = "retrying"
        if ev is not None:
            with self.cv:
                if not self.pending:
                    self.pending = [ev]
                self.cv.notify()
        return True

    def to_dict(self):
        return {"team": self.team.id, "name": self.team.name, "persona": self.team.persona, "status": self.status,
                "detail": self.detail, "iterations": self.iterations, "steps": self.steps,
                "tokens_in": self.tokens_in, "tokens_cached": self.tokens_cached, "tokens_out": self.tokens_out,
                "cost": self.cost, "last_summary": self.last_summary,
                "submissions": len([s for s in self.team.submissions if s.get("status") in ("ready", "active")])}


def _short(args):
    d = {k: (v if not isinstance(v, str) or len(v) < 60 else v[:57] + "...") for k, v in args.items()}
    return json.dumps(d)[:200]
