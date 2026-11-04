"""The Book: analytics across every saved run (results/*.json).

Builds spreadsheet-style sheets:
  Total      - every bot across all variants and tournaments (the combined book)
  Runs       - one row per tournament: variant, winner, margin, field size, ...
  Variants   - every known variant (played or not) with its champions
  Matrix     - bot x variant average percentile
  Versions   - every code version of every bot (current + legacy versions)
  Entries    - raw bot-in-run rows (one per bot per tournament)
  <variant>  - one book per variant that has been played

Scores from different runs aren't directly comparable (different point schemes, field sizes,
weights), so cross-run analytics use:
  percentile  = 1 - (rank - 1) / (field - 1)        (1.0 = won, 0.0 = last)
  normalized  = weighted avg points / field mean     (1.0 = average bot in that run)
  Elo         = pairwise multiplayer Elo over runs in chronological order (K=32, start 1500)
"""
import json
import math
import os
import time

from .variants import V as ALL_VARIANTS, get_variant

ELO_START = 1500.0
ELO_K = 32.0


def _col(key, label, type_="num", better=None, digits=2, width=None):
    c = {"key": key, "label": label, "type": type_, "better": better, "digits": digits}
    if width:
        c["width"] = width
    return c


def load_runs(results_dir):
    runs = []
    if not os.path.isdir(results_dir):
        return runs
    for fn in sorted(os.listdir(results_dir)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(results_dir, fn)
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            continue
        if not d.get("leaderboard"):
            continue
        cfg = d.get("config", {})
        vid = d.get("variant") or cfg.get("variant") or "nlhe"
        v = get_variant(vid)
        d["_id"] = fn[:-5]
        d["_variant"] = v.id
        d["_variant_name"] = d.get("variant_name") or v.name
        d["_category"] = d.get("variant_category") or v.category
        d["_betting"] = d.get("betting") or cfg.get("betting") or v.betting
        d["_practice"] = bool(cfg.get("practice"))
        runs.append(d)
    runs.sort(key=lambda r: r.get("started", 0))
    return runs


def _entries(runs):
    """One row per bot per run."""
    out = []
    for r in runs:
        lb = r["leaderboard"]
        n = len(lb)
        mean = sum(e.get("weighted_avg", 0) for e in lb) / n if n else 0
        versions = r.get("bot_versions", {})
        for e in lb:
            rank = e.get("rank", 0)
            vers = versions.get(e["id"]) or []
            out.append({
                "run": r["_id"], "date": r.get("started", 0), "variant": r["_variant"],
                "variant_name": r["_variant_name"], "category": r["_category"], "betting": r["_betting"],
                "practice": r["_practice"], "bot": e["id"], "name": e.get("name", e["id"]),
                "version": vers[-1] if vers else "unknown", "rank": rank, "field": n,
                "percentile": 1 - (rank - 1) / (n - 1) if n > 1 else 1.0,
                "weighted": e.get("weighted", 0), "avg_points": e.get("weighted_avg", 0),
                "normalized": (e.get("weighted_avg", 0) / mean) if mean > 0 else 0.0,
                "title": rank == 1, "podium": rank <= 3,
                "table_win_rate": e.get("win_rate", 0), "timeout_rate": e.get("timeout_rate", 0),
                "errors": e.get("errors", 0), "tables": e.get("tables", 0),
            })
    return out


def _elo(runs, entries_by_run):
    rating = {}
    for r in runs:
        rows = entries_by_run.get(r["_id"], [])
        n = len(rows)
        if n < 2:
            continue
        for e in rows:
            rating.setdefault(e["bot"], ELO_START)
        delta = {e["bot"]: 0.0 for e in rows}
        for a in rows:
            for b in rows:
                if a is b:
                    continue
                ra, rb = rating[a["bot"]], rating[b["bot"]]
                exp = 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))
                s = 1.0 if a["rank"] < b["rank"] else (0.5 if a["rank"] == b["rank"] else 0.0)
                delta[a["bot"]] += ELO_K * (s - exp) / (n - 1)
        for k, d in delta.items():
            rating[k] += d
    return rating


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _std(xs):
    if len(xs) < 2:
        return None
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _bot_rows(entries, runs, include_variant_breakdown=True):
    by_bot = {}
    for e in entries:
        by_bot.setdefault(e["bot"], []).append(e)
    by_run = {}
    for e in entries:
        by_run.setdefault(e["run"], []).append(e)
    elo = _elo(runs, by_run)
    rows = []
    for bot, es in by_bot.items():
        es.sort(key=lambda e: e["date"])
        pct = [e["percentile"] for e in es]
        norm = [e["normalized"] for e in es]
        sd = _std(norm)
        last5 = pct[-5:]
        prev = pct[:-5][-5:]
        best_v = worst_v = None
        if include_variant_breakdown:
            per_v = {}
            for e in es:
                per_v.setdefault(e["variant_name"], []).append(e["percentile"])
            if per_v:
                ranked = sorted(per_v.items(), key=lambda kv: _mean(kv[1]))
                worst_v, best_v = ranked[0][0], ranked[-1][0]
        versions = []
        for e in es:
            if e["version"] not in versions:
                versions.append(e["version"])
        decisions_weighted_timeout = _mean([e["timeout_rate"] for e in es])
        rows.append({
            "bot": bot, "name": es[-1]["name"], "runs": len(es),
            "variants": len({e["variant"] for e in es}),
            "titles": sum(1 for e in es if e["title"]), "podiums": sum(1 for e in es if e["podium"]),
            "title_rate": sum(1 for e in es if e["title"]) / len(es),
            "avg_finish": _mean([e["rank"] for e in es]), "avg_field": _mean([e["field"] for e in es]),
            "avg_percentile": _mean(pct), "avg_normalized": _mean(norm),
            "ci95": 1.96 * sd / math.sqrt(len(norm)) if sd is not None else None,
            "consistency": sd, "elo": elo.get(bot, ELO_START),
            "best_variant": best_v, "worst_variant": worst_v,
            "best_finish": min(e["rank"] for e in es),
            "table_win_rate": _mean([e["table_win_rate"] for e in es]),
            "timeout_rate": decisions_weighted_timeout, "errors": sum(e["errors"] for e in es),
            "versions": len(versions), "current_version": versions[-1],
            "last5": _mean(last5), "trend": (_mean(last5) - _mean(prev)) if prev else None,
            "last_played": es[-1]["date"],
        })
    rows.sort(key=lambda r: -r["elo"])
    for i, r in enumerate(rows):
        r["pos"] = i + 1
    return rows


BOT_COLUMNS = [
    _col("pos", "#", "int"), _col("name", "Bot", "text", width=210),
    _col("elo", "Elo", "num", "high", 0), _col("runs", "Runs", "int"),
    _col("variants", "Variants", "int"), _col("titles", "Titles", "int", "high"),
    _col("podiums", "Podiums", "int", "high"), _col("title_rate", "Title %", "pct", "high"),
    _col("avg_finish", "Avg finish", "num", "low", 2), _col("best_finish", "Best", "int", "low"),
    _col("avg_percentile", "Avg pctile", "pct", "high"),
    _col("avg_normalized", "Norm. score", "num", "high", 3), _col("ci95", "± 95% CI", "num", None, 3),
    _col("consistency", "Std dev", "num", "low", 3),
    _col("last5", "Last-5 pctile", "pct", "high"), _col("trend", "Trend", "pct", "high"),
    _col("table_win_rate", "Table win %", "pct", "high"), _col("timeout_rate", "Timeout %", "pct", "low"),
    _col("errors", "Errors", "int", "low"), _col("best_variant", "Best variant", "text", width=170),
    _col("worst_variant", "Worst variant", "text", width=170), _col("versions", "Versions", "int"),
    _col("current_version", "Current ver.", "text"), _col("last_played", "Last played", "date"),
]


def build_book(results_dir):
    runs = load_runs(results_dir)
    entries = _entries(runs)
    sheets = []

    # ------------------------------------------------------------------ Total
    total_rows = _bot_rows(entries, runs)
    sheets.append({"name": "Total", "kind": "total",
                   "title": "Total book - every bot across all variants and tournaments",
                   "columns": BOT_COLUMNS, "rows": total_rows})

    # ------------------------------------------------------------------ Runs
    run_rows = []
    by_run = {}
    for e in entries:
        by_run.setdefault(e["run"], []).append(e)
    for r in runs:
        lb = sorted(r["leaderboard"], key=lambda e: e.get("rank", 99))
        cfg = r.get("config", {})
        w = lb[0] if lb else {}
        ru = lb[1] if len(lb) > 1 else {}
        run_rows.append({
            "run": r["_id"], "date": r.get("started", 0), "variant": r["_variant_name"],
            "category": r["_category"], "betting": r["_betting"].replace("_", "-"),
            "bots": len(lb), "rounds": len(r.get("rounds", [])), "iterations": cfg.get("iterations"),
            "practice": "yes" if r["_practice"] else "", "winner": w.get("name", ""),
            "winner_score": w.get("weighted"), "runner_up": ru.get("name", ""),
            "margin": (w.get("weighted", 0) - ru.get("weighted", 0)) if ru else None,
            "winner_title_pct": w.get("win_rate"),
            "duration": (r.get("finished", 0) - r.get("started", 0)) if r.get("finished") else None,
            "points": ",".join(str(int(p)) if float(p).is_integer() else str(p) for p in cfg.get("points", [])),
            "time_limit": cfg.get("time_limit_ms"), "seed": r.get("seed"),
        })
    run_rows.reverse()
    sheets.append({"name": "Runs", "kind": "runs", "title": "Every tournament run (newest first)",
                   "columns": [
                       _col("date", "Date", "date"), _col("variant", "Variant", "text", width=190),
                       _col("category", "Family", "text", width=150), _col("betting", "Betting", "text"),
                       _col("bots", "Bots", "int"), _col("rounds", "Rounds", "int"),
                       _col("iterations", "Iterations", "int"), _col("practice", "Practice", "text"),
                       _col("winner", "Winner", "text", width=200), _col("winner_score", "Winner score", "num", "high"),
                       _col("runner_up", "Runner-up", "text", width=200), _col("margin", "Margin", "num", "high"),
                       _col("winner_title_pct", "Winner table win %", "pct", "high"),
                       _col("duration", "Duration (s)", "num", None, 0), _col("points", "Points", "text"),
                       _col("time_limit", "ms limit", "num", None, 1), _col("seed", "Seed", "int"),
                       _col("run", "Run file", "text", width=170)],
                   "rows": run_rows})

    # ------------------------------------------------------------------ per-variant books
    variant_sheets = []
    variant_summary = []
    for v in ALL_VARIANTS:
        es = [e for e in entries if e["variant"] == v.id]
        vruns = [r for r in runs if r["_variant"] == v.id]
        row = {"variant": v.name, "category": v.category, "betting": v.betting.replace("_", "-"),
               "legacy": v.legacy or "", "runs": len(vruns), "bots": len({e["bot"] for e in es}),
               "last_played": max((r.get("started", 0) for r in vruns), default=None),
               "top_rated": "", "top_elo": None, "most_titles": "", "titles": None,
               "last_winner": "", "avg_field": _mean([len(r["leaderboard"]) for r in vruns]),
               "tables": sum(e["tables"] for e in es) or None, "description": v.description}
        if es:
            rows = _bot_rows(es, vruns, include_variant_breakdown=False)
            row["top_rated"], row["top_elo"] = rows[0]["name"], rows[0]["elo"]
            mt = max(rows, key=lambda r: (r["titles"], r["elo"]))
            row["most_titles"], row["titles"] = mt["name"], mt["titles"]
            lw = sorted(vruns, key=lambda r: r.get("started", 0))[-1]
            row["last_winner"] = min(lw["leaderboard"], key=lambda e: e.get("rank", 99)).get("name", "")
            cols = [c for c in BOT_COLUMNS if c["key"] not in ("variants", "best_variant", "worst_variant")]
            variant_sheets.append({"name": v.name, "kind": "variant", "variant": v.id,
                                   "title": f"{v.name} - {v.category}: {v.description}",
                                   "columns": cols, "rows": rows})
        variant_summary.append(row)
    variant_summary.sort(key=lambda r: (-r["runs"], r["category"], r["variant"]))
    sheets.append({"name": "Variants", "kind": "variants",
                   "title": "All variants: runs, champions and top-rated bots (unplayed variants included)",
                   "columns": [
                       _col("variant", "Variant", "text", width=200), _col("category", "Family", "text", width=170),
                       _col("betting", "Default betting", "text"), _col("legacy", "MIT year", "text"),
                       _col("runs", "Runs", "int"), _col("bots", "Bots", "int"),
                       _col("top_rated", "Top rated (Elo)", "text", width=200),
                       _col("top_elo", "Top Elo", "num", None, 0),
                       _col("most_titles", "Most titles", "text", width=200), _col("titles", "Titles", "int"),
                       _col("last_winner", "Last winner", "text", width=200),
                       _col("avg_field", "Avg field", "num", None, 1), _col("tables", "Tables played", "int"),
                       _col("last_played", "Last played", "date"),
                       _col("description", "Rules", "text", width=420)],
                   "rows": variant_summary})

    # ------------------------------------------------------------------ Matrix
    played = [v for v in ALL_VARIANTS if any(e["variant"] == v.id for e in entries)]
    mrows = []
    for tr in total_rows:
        row = {"name": tr["name"], "overall": tr["avg_percentile"], "elo": tr["elo"]}
        for v in played:
            ps = [e["percentile"] for e in entries if e["bot"] == tr["bot"] and e["variant"] == v.id]
            row["v_" + v.id] = _mean(ps)
        mrows.append(row)
    sheets.append({"name": "Matrix", "kind": "matrix",
                   "title": "Average percentile finish by variant (1.0 = always wins, 0.0 = always last)",
                   "columns": [_col("name", "Bot", "text", width=210), _col("elo", "Elo", "num", "high", 0),
                               _col("overall", "All variants", "pct", "high")]
                   + [_col("v_" + v.id, v.name, "pct", "high", width=120) for v in played],
                   "rows": mrows})

    # ------------------------------------------------------------------ Versions (legacy)
    vrows = []
    by_bv = {}
    for e in entries:
        by_bv.setdefault((e["bot"], e["version"]), []).append(e)
    latest = {}
    for (bot, ver), es in by_bv.items():
        last = max(e["date"] for e in es)
        if bot not in latest or last > latest[bot][1]:
            latest[bot] = (ver, last)
    order = {}
    for (bot, ver), es in sorted(by_bv.items(), key=lambda kv: min(e["date"] for e in kv[1])):
        order.setdefault(bot, []).append(ver)
    for (bot, ver), es in by_bv.items():
        vrows.append({
            "name": es[-1]["name"], "label": f"v{order[bot].index(ver) + 1}", "hash": ver,
            "status": "current" if latest[bot][0] == ver else "legacy",
            "first_seen": min(e["date"] for e in es), "last_seen": max(e["date"] for e in es),
            "runs": len(es), "titles": sum(1 for e in es if e["title"]),
            "avg_percentile": _mean([e["percentile"] for e in es]),
            "avg_normalized": _mean([e["normalized"] for e in es]),
            "avg_finish": _mean([e["rank"] for e in es]),
            "variants": ", ".join(sorted({e["variant_name"] for e in es})),
            "timeout_rate": _mean([e["timeout_rate"] for e in es]),
        })
    vrows.sort(key=lambda r: (r["name"], r["first_seen"]))
    sheets.append({"name": "Versions", "kind": "versions",
                   "title": "Every code version of every bot (a new version is recorded whenever a bot file changes)",
                   "columns": [_col("name", "Bot", "text", width=210), _col("label", "Version", "text"),
                               _col("status", "Status", "text"), _col("hash", "Code hash", "text"),
                               _col("first_seen", "First seen", "date"), _col("last_seen", "Last seen", "date"),
                               _col("runs", "Runs", "int"), _col("titles", "Titles", "int", "high"),
                               _col("avg_finish", "Avg finish", "num", "low"),
                               _col("avg_percentile", "Avg pctile", "pct", "high"),
                               _col("avg_normalized", "Norm. score", "num", "high", 3),
                               _col("timeout_rate", "Timeout %", "pct", "low"),
                               _col("variants", "Variants played", "text", width=300)],
                   "rows": vrows})

    # ------------------------------------------------------------------ Entries
    erows = sorted(entries, key=lambda e: (-e["date"], e["rank"]))
    sheets.append({"name": "Entries", "kind": "entries",
                   "title": "Raw data: one row per bot per tournament",
                   "columns": [_col("date", "Date", "date"), _col("variant_name", "Variant", "text", width=190),
                               _col("name", "Bot", "text", width=210), _col("version", "Version", "text"),
                               _col("rank", "Finish", "int", "low"), _col("field", "Field", "int"),
                               _col("percentile", "Pctile", "pct", "high"),
                               _col("weighted", "Weighted", "num", "high"),
                               _col("avg_points", "Avg pts", "num", "high"),
                               _col("normalized", "Norm.", "num", "high", 3),
                               _col("table_win_rate", "Table win %", "pct", "high"),
                               _col("timeout_rate", "Timeout %", "pct", "low"),
                               _col("errors", "Errors", "int", "low"), _col("run", "Run file", "text", width=170)],
                   "rows": erows})

    sheets.extend(variant_sheets)
    summary = {
        "runs": len(runs), "bots": len({e["bot"] for e in entries}),
        "variants_played": len(played), "variants_known": len(ALL_VARIANTS),
        "entries": len(entries), "generated": time.time(),
        "leader": total_rows[0]["name"] if total_rows else None,
    }
    return {"summary": summary, "sheets": sheets}
