"""Defaults, model prices and rival personas for the practice-event simulator."""

# Ollama exposes an OpenAI-compatible API. With the desktop app signed in, cloud models are
# served through the local server; without it, use https://ollama.com/v1 plus an API key.
# Default: OpenAI's API (no 3-request cap like Ollama Pro; GPT-5.6 Terra is newer, faster and
# smarter than o3). Needs an API key: paste it in the setup form or set OPENAI_API_KEY.
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_CONCURRENCY = 7
OLLAMA_BASE_URL = "http://localhost:11434/v1"

# USD per million tokens (input, cached input, output), from ollama.com/pricing as of
# 2026-10-07. Prices change: edit here or in the UI. Unknown models fall back to "default".
PRICES = {
    # OpenAI API (developers.openai.com model pages, 2026-11)
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.6-sol": (5.00, 0.50, 30.00),
    "o3": (2.00, 0.50, 8.00),
    "gpt-oss:120b": (0.15, 0.014, 0.60),
    "gpt-oss:20b": (0.07, 0.035, 0.30),
    "deepseek-v4.1-flash": (0.30, 0.006, 1.20),
    "deepseek-v4-pro": (1.32, 0.044, 3.96),
    "gemma4": (0.14, 0.05, 0.40),
    "glm-5.3": (1.40, 0.26, 4.40),
    "glm-5.3-flash": (0.15, 0.03, 0.50),
    "glm-5.2": (1.40, 0.26, 4.40),
    "kimi-k2.6": (0.95, 0.16, 4.00),
    "kimi-k2.7-code": (0.95, 0.19, 4.00),
    "kimi-k3": (3.00, 0.30, 15.00),
    "minimax-m2.7": (0.30, 0.06, 1.20),
    "nemotron-3-super": (0.015, 0.015, 0.60),
    "mock": (0.0, 0.0, 0.0),
    "default": (1.00, 0.10, 4.00),
}


def price_for(model):
    m = (model or "").lower()
    for suffix in ("-cloud", ":cloud"):
        if m.endswith(suffix):
            m = m[: -len(suffix)]
    if m in PRICES:
        return PRICES[m]
    base = m.split(":")[0]
    for k in PRICES:
        if k == base or k.startswith(base + ":"):
            return PRICES[k]
    return PRICES["default"]


# The official schedule (minutes): 60-minute build with an unscored practice round every 10
# minutes, then six scored rounds 20 minutes apart. Each round freezes the current submission.
BUILD_MINUTES = 60
PRACTICE_EVERY = 10
SCORED_ROUNDS = 6
SCORED_GAP = 20
UPLOAD_COOLDOWN_S = 30           # "Your team can upload once every 30 seconds"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
HAND_CAP = 250                   # Boston format page
HANDS_PER_LEVEL = 20
TIME_BANK_MS = 100.0             # per-table bank for decision overruns
SCORED_ITERATIONS = 5000         # "at least 5,000 iterations"
PRACTICE_ITERATIONS = 1000       # "about 1,000 iterations every 10 minutes"

# Each rival team prompts its AI differently, as real teams do. Personas only set the team's
# starting instructions; every team gets the same rules, starter kit and data.
PERSONAS = [
    ("Team Atlas", "You are methodical. Start from a solid tight-aggressive baseline and improve it with "
                   "careful analysis of the hand histories. Prefer measured, evidence-backed changes."),
    ("Team Blitz", "You believe aggression wins sit-and-gos. Build a bot that applies pressure, steals blinds "
                   "and shoves often, then tune it from the data."),
    ("Team Cipher", "You are a math-first team: equity calculations, pot odds and push/fold charts. Keep the bot "
                    "fast and precise."),
    ("Team Drift", "You are an exploitative team: study what the other bots do in the downloads and build "
                   "counter-strategies against the most common weaknesses."),
    ("Team Ember", "You value simplicity and robustness: a small set of clear rules that never crash or time "
                   "out, adjusted round by round."),
    ("Team Flux", "You focus on the variant's special mechanic (discard and reveal) and try to gain an edge "
                  "from how cards are kept, revealed and read."),
    ("Team Gale", "You are a balanced, game-theory-minded team: avoid being exploitable, mix strategies, and "
                  "only deviate when the data clearly supports it."),
]
