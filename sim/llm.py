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


class OutOfCreditsError(LLMError):
    """The provider refused the request because the account's credits / usage limit ran out."""


_CREDIT_WORDS = ("credit", "quota", "usage limit", "limit reached", "reached your", "exceeded your",
                 "insufficient", "billing", "payment", "subscription", "upgrade", "balance", "plan limit")


def is_out_of_credits(code, detail):
    d = (detail or "").lower()
    if code == 402 or "insufficient_quota" in d:
        return True
    if "rate limit" in d or "rate_limit" in d:
        return False  # temporary per-minute limit: retried, not "out of credits"
    return code in (403, 429) and any(w in d for w in _CREDIT_WORDS)


class OpenAICompatible:
    def __init__(self, base_url, model, api_key="", timeout=180):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def chat(self, messages, tools=None):
        msgs = [{k: v for k, v in m.items() if not (m.get("role") == "tool" and k == "name")} for m in messages]
        body = {"model": self.model, "messages": msgs, "stream": False}
        if tools:
            body["tools"] = tools
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if not self.api_key and "api.openai.com" in self.base_url:
            import os
            self.api_key = os.environ.get("OPENAI_API_KEY", "")
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
                if is_out_of_credits(e.code, detail):
                    raise OutOfCreditsError(last)  # retrying won't help
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
        if self.model == "mock-nocredits":
            raise OutOfCreditsError("HTTP 402: you have run out of credits (simulated)")
        n_tool = sum(1 for m in messages if m.get("role") == "tool")
        usage = {"prompt_tokens": sum(len(str(m.get("content", ""))) for m in messages) // 4,
                 "completion_tokens": 60}
        calls = []
        if n_tool == 0:
            calls = [("list_files", {"path": "."})]
        elif n_tool == 1:
            calls = [("read_file", {"path": "agent.py"})]
        elif n_tool == 2:
            thr = round(self.rng.uniform(0.35, 0.75), 2)
            calls = [("write_file", {"path": "agent.py", "content": open_template().replace("THRESHOLD = 0.5", f"THRESHOLD = {thr}")})]
        elif n_tool == 3:
            calls = [("submit", {"path": "agent.py"})]
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
        "import poker_agent_sdk\n"
        "from poker_agent_sdk import Action\n\n"
        "THRESHOLD = 0.5\n"
        "RANKS = '23456789TJQKA'\n\n\n"
        "class Agent(poker_agent_sdk.Agent):\n"
        "    def act(self, state, memory):\n"
        "        if state.phase in ('discard', 'reveal'):\n"
        "            n = state.legal_actions[0].min_amount\n"
        "            cards = sorted(state.variant['down_cards'], key=lambda c: RANKS.index(c[0]))\n"
        "            return Action(state.phase, cards=cards[:n]), memory\n"
        "        s = sum(RANKS.index(c[0]) for c in state.cards) / (12.0 * max(1, len(state.cards)))\n"
        "        r = state.can('raise')\n"
        "        if s >= THRESHOLD and r:\n"
        "            return Action.raise_to(r.min_amount), memory\n"
        "        if state.can('check'):\n"
        "            return Action.check(), memory\n"
        "        if s >= THRESHOLD - 0.1 and state.can('call'):\n"
        "            return Action.call(), memory\n"
        "        return Action.fold(), memory\n"
    )


def make_client(base_url, model, api_key=""):
    if (model or "").lower().startswith("mock"):
        return MockLLM(model)
    return OpenAICompatible(base_url, model, api_key)
