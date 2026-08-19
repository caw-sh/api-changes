#!/usr/bin/env python3
"""Print a markdown summary of what the collector has seen. Run: python scripts/report.py"""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load(name):
    """Load a jsonl file, or every jsonl in a directory of daily partitions."""
    f = DATA / name
    files = sorted(f.glob("*.jsonl")) if f.is_dir() else ([f] if f.exists() else [])
    rows = []
    for p in files:
        rows += [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return rows


changes, signals, obs = load("changes.jsonl"), load("signals.jsonl"), load("observations")
runs = len({o["ts"] for o in obs})
tracked = len({o["id"] for o in obs})

breaking = [c for c in changes if c["counts"]["breaking"]]
additive_only = [c for c in changes if not c["counts"]["breaking"]]
kinds = Counter(d["kind"] for c in changes for d in c["changes"])
total_diffs = sum(kinds.values())

print(f"# caw — collection summary\n")
print(f"- **{tracked}** endpoints tracked over **{runs}** runs")
print(f"- **{len(changes)}** change events — {len(breaking)} with a breaking change, "
      f"{len(additive_only)} additive only")
print(f"- **{len(signals)}** deprecation / version signal headers seen\n")

if total_diffs:
    print("## Change types\n")
    print("| kind | count | share |")
    print("|---|---:|---:|")
    for k, n in kinds.most_common():
        print(f"| {k} | {n} | {n / total_diffs:.0%} |")
    print()

if changes:
    print("## Most active endpoints\n")
    print("| endpoint | vendor | events | breaking |")
    print("|---|---|---:|---:|")
    per = Counter(c["id"] for c in changes)
    vend = {c["id"]: c["vendor"] for c in changes}
    brk = Counter(c["id"] for c in breaking)
    for eid, n in per.most_common(15):
        print(f"| `{eid}` | {vend[eid]} | {n} | {brk.get(eid, 0)} |")
    print()

    print("## Recent changes\n")
    for c in changes[-15:][::-1]:
        print(f"**{c['vendor']}** · `{c['id']}` · {c['ts']}  ")
        for d in c["changes"][:6]:
            arrow = f" {d.get('from')} → {d.get('to')}" if d.get("from") else ""
            print(f"- `{d['kind']}` `{d['path']}`{arrow}")
        if len(c["changes"]) > 6:
            print(f"- …{len(c['changes']) - 6} more")
        print()

if signals:
    print("## Deprecation / version signals\n")
    for s in signals[-20:][::-1]:
        for k, v in s["headers"].items():
            print(f"- **{s['vendor']}** `{k}: {v[:120]}`")
