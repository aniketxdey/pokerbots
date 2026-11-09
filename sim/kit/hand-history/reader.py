"""Streaming reader for hand-history downloads (gzipped JSON lines, one hand per line).

    from reader import iter_hands
    for hand in iter_hands("hand_history.jsonl.gz"):
        print(hand["hand"], hand["my_cards"], hand["deltas"][hand["my_seat"]])

    python reader.py hand_history.jsonl.gz      # quick summary
"""
import gzip
import json
import sys


def iter_hands(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def summary(path):
    n = 0
    chips = 0
    tables = set()
    for h in iter_hands(path):
        n += 1
        chips += h["deltas"][h["my_seat"]]
        tables.add((h.get("round"), h.get("table")))
    print(f"{n} hands at {len(tables)} tables; net chips {chips:+d}")


if __name__ == "__main__":
    summary(sys.argv[1])
