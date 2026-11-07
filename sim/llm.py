"""Minimal OpenAI-compatible chat client (works with Ollama, local or cloud) plus a mock model.

Standard library only. `chat()` returns (message_dict, usage_dict).
"""
import json
import random
import re
import time
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


class OpenAICompatible:
    def __init__(self, base_url, model, api_key="", timeout=600):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def chat(self, messages, tools=None):
        body = {"model": self.model, "messages": messages, "stream": False}
        if tools:
            body["tools"] = tools
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last = None
        for attempt in range(4):
            req = urllib.request.Request(self.base_url + "/chat/completions", data=data, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    out = json.loads(r.read().decode())
                msg = out["choices"][0]["message"]
                return msg, out.get("usage") or {}
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:400]
                last = f"HTTP {e.code}: {detail}"
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(3 * (attempt + 1))
                    continue
                raise LLMError(last)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = str(e)
                time.sleep(3 * (attempt + 1))
        raise LLMError(f"model unavailable after retries: {last}")


class MockLLM:
    """Offline stand-in used for testing the simulator without a model. It behaves like a tiny
    agent: looks at the downloads, makes one small parameter change to its bot, and submits."""

    def __init__(self, model="mock", seed=None):
        self.model = model
        self.rng = random.Random(seed)

    def chat(self, messages, tools=None):
        n_tool = sum(1 for m in messages if m.get("role") == "tool")
        usage = {"prompt_tokens": sum(len(str(m.get("content", ""))) for m in messages) // 4,
                 "completion_tokens": 60}
        calls = []
        if n_tool == 0:
            calls = [("list_files", {"path": "."})]
        elif n_tool == 1:
            calls = [("read_file", {"path": "bot.py"})]
        elif n_tool == 2:
            src = None
            for m in reversed(messages):
                if m.get("role") == "tool" and "class Agent" in str(m.get("content", "")):
                    src = re.sub(r"^\s*\d+\|", "", m["content"], flags=re.M)
                    break
            if src is None or "class Agent" not in src:
                src = open_template()
            thr = round(self.rng.uniform(0.35, 0.75), 2)
            src = re.sub(r"THRESHOLD = [0-9.]+", f"THRESHOLD = {thr}", src)
            if "THRESHOLD =" not in src:
                src = src.replace("class Agent", f"THRESHOLD = {thr}\n\n\nclass Agent", 1)
            calls = [("write_file", {"path": "bot.py", "content": src})]
        elif n_tool == 3:
            calls = [("submit", {"path": "bot.py"})]
        if calls:
            msg = {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call_{n_tool}_{i}", "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args)}}
                for i, (name, args) in enumerate(calls)]}
        else:
            msg = {"role": "assistant", "content": "Adjusted the strength threshold and submitted."}
        time.sleep(0.05)
        return msg, usage


def open_template():
    return (
        "THRESHOLD = 0.5\n\n\n"
        "class Agent:\n"
        "    def act(self, obs):\n"
        "        ranks = '23456789TJQKA'\n"
        "        s = sum(ranks.index(c[0]) for c in obs.hole_cards) / (12.0 * max(1, len(obs.hole_cards)))\n"
        "        if s >= THRESHOLD and 'raise' in obs.legal_actions:\n"
        "            return ('raise', obs.min_raise_to)\n"
        "        if obs.to_call == 0:\n"
        "            return 'check'\n"
        "        return 'call' if s >= THRESHOLD - 0.1 else 'fold'\n"
    )


def make_client(base_url, model, api_key=""):
    if (model or "").lower().startswith("mock"):
        return MockLLM(model)
    return OpenAICompatible(base_url, model, api_key)
