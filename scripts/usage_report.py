"""Token usage report for the SETH unified platform build (standard library only).

Reads Claude Code's session records for this project (and its worktrees) under
~/.claude/projects/, counts each model response (requestId) once, and breaks the
totals down by model, agent type, unit, sub-project and day. Writes
docs/build/usage.json and docs/build/usage.html and prints a summary.

Dollar figures are ESTIMATES at list price from docs/build/prices.json, not a bill.

Usage: python scripts/usage_report.py [--projects-dir DIR] [--out-dir DIR] [--prefix NAME]
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_PREFIX = "S--Development-2026-shi-work-agt-trus-harness"
CLASSES = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")
UNIT_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(\S+)?")
ROUND_VERBS = ("build", "verify", "fix", "review", "inventory")


def _usage_classes(usage: dict[str, Any]) -> dict[str, int]:
    cc = usage.get("cache_creation") or {}
    w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    w5m = int(cc.get("ephemeral_5m_input_tokens") or 0)
    total_write = int(usage.get("cache_creation_input_tokens") or 0)
    if w1h + w5m != total_write:  # older records without the split: treat as 5m writes
        w5m = total_write - w1h
    return {
        "input": int(usage.get("input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "cache_write_5m": w5m,
        "cache_write_1h": w1h,
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
    }


def _read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _agent_identity(jsonl: Path) -> tuple[str, str]:
    """(agent_type, description) for a subagent file, ('lead', '') for a session file."""
    if jsonl.parent.name != "subagents":
        return "lead", ""
    meta = jsonl.with_name(jsonl.stem + ".meta.json")
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown", ""
    return str(data.get("agentType") or "unknown"), str(data.get("description") or "")


def _unit_and_verb(agent_type: str, description: str) -> tuple[str, str]:
    if agent_type == "lead":
        return "lead", ""
    m = UNIT_RE.match(description)
    if not m:
        return "unassigned", ""
    verb = (m.group(2) or "").lower()
    return m.group(1).strip(), verb


def collect(projects_dir: Path, prefix: str, unit_map: dict[str, str]) -> list[dict[str, Any]]:
    """One record per unique requestId."""
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    folders = sorted(p for p in projects_dir.glob(prefix + "*") if p.is_dir())
    for folder in folders:
        for jsonl in sorted(folder.rglob("*.jsonl")):
            agent_type, description = _agent_identity(jsonl)
            unit, verb = _unit_and_verb(agent_type, description)
            agent_id = jsonl.stem if agent_type != "lead" else "lead:" + jsonl.stem
            for obj in _read_jsonl(jsonl):
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message") or {}
                usage = msg.get("usage")
                model = msg.get("model")
                if not isinstance(usage, dict) or not model or model == "<synthetic>":
                    continue
                rid = obj.get("requestId") or msg.get("id")
                if not rid:
                    continue
                if rid in seen:
                    continue
                seen.add(rid)
                records.append(
                    {
                        "model": str(model),
                        "agent_type": agent_type,
                        "agent_id": agent_id,
                        "unit": unit,
                        "verb": verb,
                        "sub_project": unit_map.get(unit, "lead" if unit == "lead" else "sp1"),
                        "day": str(obj.get("timestamp") or "")[:10] or "unknown",
                        **_usage_classes(usage),
                    }
                )
    return records


def cost(row: dict[str, int], model: str, prices: dict[str, dict[str, float]]) -> float | None:
    p = prices.get(model)
    if p is None:
        return None
    return sum(row[c] * float(p.get(c, 0.0)) for c in CLASSES) / 1_000_000


def group(
    records: list[dict[str, Any]], key: str, prices: dict[str, dict[str, float]]
) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in records:
        g = out.setdefault(
            r[key], {key: r[key], **{c: 0 for c in CLASSES}, "requests": 0, "est_usd": 0.0}
        )
        for c in CLASSES:
            g[c] += r[c]
        g["requests"] += 1
        g["est_usd"] += cost(r, r["model"], prices) or 0.0
    rows = sorted(out.values(), key=lambda g: -g["est_usd"])
    for g in rows:
        g["est_usd"] = round(g["est_usd"], 4)
    return rows


def rounds_per_unit(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    agents: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for r in records:
        if r["unit"] in ("lead", "unassigned"):
            continue
        verb = r["verb"] if r["verb"] in ROUND_VERBS else "other"
        agents[r["unit"]][verb].add(r["agent_id"])
    return [
        {"unit": u, **{v: len(agents[u].get(v, set())) for v in (*ROUND_VERBS, "other")}}
        for u in sorted(agents)
    ]


def _fmt(n: float) -> str:
    return f"{n:,.0f}" if isinstance(n, int) else f"{n:,.2f}"


def _table(title: str, rows: list[dict[str, Any]], key: str) -> str:
    cols = [key, *CLASSES, "requests", "est_usd"]
    lines = [f"\n{title}", "  ".join(f"{c:>15}" for c in cols)]
    for r in rows:
        lines.append("  ".join(f"{str(r[c]) if c == key else _fmt(r[c]):>15}" for c in cols))
    return "\n".join(lines)


def _html(report: dict[str, Any]) -> str:
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'><title>Build token usage</title>",
        "<style>body{font-family:system-ui,sans-serif;margin:24px;background:#fff;color:#111}"
        "table{border-collapse:collapse;margin:8px 0 24px}td,th{border:1px solid #ccc;"
        "padding:4px 8px;text-align:right}th:first-child,td:first-child{text-align:left}"
        "@media (prefers-color-scheme:dark){body{background:#111;color:#eee}td,th{border-color:#444}}"
        "</style></head><body><h1>Build token usage</h1>",
        "<p><strong>Dollar figures are estimates at list price "
        "(docs/build/prices.json), not a bill.</strong></p>",
        f"<p>Generated {html.escape(report['generated'])}; "
        f"{report['totals']['requests']} model responses.</p>",
    ]
    for title, key in (
        ("By model", "model"),
        ("By agent type", "agent_type"),
        ("By unit", "unit"),
        ("By sub-project", "sub_project"),
        ("By day", "day"),
    ):
        rows = report["by_" + key]
        cols = [key, *CLASSES, "requests", "est_usd"]
        parts.append(f"<h2>{title}</h2><table><tr>")
        parts.extend(f"<th>{html.escape(c)}</th>" for c in cols)
        parts.append("</tr>")
        for r in rows:
            parts.append(
                "<tr>"
                + "".join(
                    f"<td>{html.escape(str(r[c]) if c == key else _fmt(r[c]))}</td>" for c in cols
                )
                + "</tr>"
            )
        parts.append("</table>")
    parts.append("<h2>Agent rounds per unit</h2><table><tr>")
    rcols = ["unit", *ROUND_VERBS, "other"]
    parts.extend(f"<th>{c}</th>" for c in rcols)
    parts.append("</tr>")
    for r in report["rounds_per_unit"]:
        parts.append("<tr>" + "".join(f"<td>{html.escape(str(r[c]))}</td>" for c in rcols) + "</tr>")
    parts.append("</table></body></html>")
    return "".join(parts)


def build_report(projects_dir: Path, prefix: str, repo: Path) -> dict[str, Any]:
    prices_path = repo / "docs" / "build" / "prices.json"
    prices = json.loads(prices_path.read_text(encoding="utf-8")) if prices_path.exists() else {}
    prices = {k: v for k, v in prices.items() if isinstance(v, dict)}
    units_path = repo / "docs" / "build" / "units.json"
    unit_map = json.loads(units_path.read_text(encoding="utf-8")) if units_path.exists() else {}
    records = collect(projects_dir, prefix, unit_map)
    from datetime import UTC, datetime

    totals = group([{**r, "all": "all"} for r in records], "all", prices)
    return {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "note": "est_usd is an estimate at list price, not a bill",
        "totals": totals[0] if totals else {**{c: 0 for c in CLASSES}, "requests": 0, "est_usd": 0},
        "by_model": group(records, "model", prices),
        "by_agent_type": group(records, "agent_type", prices),
        "by_unit": group(records, "unit", prices),
        "by_sub_project": group(records, "sub_project", prices),
        "by_day": group(records, "day", prices),
        "rounds_per_unit": rounds_per_unit(records),
        "unpriced_models": sorted({r["model"] for r in records} - set(prices)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--projects-dir", type=Path, default=Path.home() / ".claude" / "projects")
    ap.add_argument("--out-dir", type=Path, default=REPO / "docs" / "build")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX)
    ap.add_argument("--repo", type=Path, default=REPO)
    args = ap.parse_args(argv)
    report = build_report(args.projects_dir, args.prefix, args.repo)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "usage.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (args.out_dir / "usage.html").write_text(_html(report), encoding="utf-8")
    t = report["totals"]
    print(
        f"Total: {t['requests']} responses, est. ${t['est_usd']:,.2f} at list price (not a bill)"
    )
    for title, key in (
        ("By model", "model"),
        ("By agent type", "agent_type"),
        ("By unit (module)", "unit"),
        ("By sub-project", "sub_project"),
        ("By day", "day"),
    ):
        print(_table(title, report["by_" + key], key))
    if report["unpriced_models"]:
        print("\nModels without a price in prices.json:", ", ".join(report["unpriced_models"]))
    print(f"\nWrote {args.out_dir / 'usage.json'} and {args.out_dir / 'usage.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
