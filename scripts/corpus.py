#!/usr/bin/env python3
"""Corpus test — replay every stored snapshot through the current differ.

Written after the third heuristic change in a row created new false positives.
Each of those changes passed hand-written unit tests and then broke real
endpoints, because the tests were built from examples I invented and the
endpoints were not.

This runs against the 116 real payload schemas already on disk. A change that
fixes one vendor by breaking another fails here instead of in tomorrow's report.
"""
from __future__ import annotations
import json, sys, collections
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collect as c

ROOT = Path(__file__).resolve().parent.parent
SNAPDIR = ROOT / "snapshots"

# Endpoints whose payloads are keyed by data (package name, currency, symbol).
# Their schemas MUST contain a collapsed `{*}` marker -- if they do not, every
# new package or coin will be reported as a schema change.
# (crates.io looks like a candidate but is not one: it returns a struct with a
# versions ARRAY, not an object keyed by version. Arrays are unioned, not
# collapsed. This list is for objects whose KEYS come from data.)
MUST_COLLAPSE = ["npm-express", "npm-typescript", "packagist-monolog",
                 "coingecko-coin"]

# Endpoints that are fixed structs. A `{*}` here means real field names were
# swallowed, which is how NASA's APOD lost every field for two days.
MUST_NOT_COLLAPSE = ["nasa-apod", "usgs-quakes", "status-twilio-summary",
                     "status-cloudflare-summary"]

fails: list[str] = []
def fail(msg): fails.append(msg); print("FAIL  " + msg)


def load_all() -> dict[str, dict]:
    out = {}
    for f in sorted(SNAPDIR.glob("*.json")):
        try:
            out[f.stem] = json.loads(f.read_text())
        except ValueError as e:
            fail(f"{f.name}: unreadable ({e})")
    return out


def main() -> int:
    snaps = load_all()
    print(f"corpus: {len(snaps)} snapshots\n")

    # 1. Identity. Diffing a schema against itself must produce nothing.
    #    Anything else means the differ is not deterministic.
    for eid, s in snaps.items():
        schema = s.get("schema", {})
        ch, carried, fo, _st = c.classify(schema, schema, sampled=True)
        if ch:
            fail(f"{eid}: identity diff produced {len(ch)} change(s): {ch[:2]}")
    print(f"ok    identity diff is empty for all {len(snaps)} snapshots")

    # 2. Flip-flop detector. A schema must never hold both a collapsed marker
    #    and named siblings under the same parent -- that is the signature of a
    #    map heuristic that disagrees with itself between runs.
    for eid, s in snaps.items():
        by_parent = collections.defaultdict(set)
        for p in s.get("schema", {}):
            by_parent[c._parent(p)].add(p.rsplit(".", 1)[-1])
        for parent, kids in by_parent.items():
            if "{*}" in kids and len(kids) > 1:
                fail(f"{eid}: '{parent}' holds both {{*}} and named fields "
                     f"{sorted(kids - {'{*}'})[:3]} -- map heuristic is unstable")
    print("ok    no snapshot mixes {*} with named siblings")

    # 3. Data-keyed endpoints must be collapsed.
    for eid in MUST_COLLAPSE:
        s = snaps.get(eid)
        if s is None:
            print(f"skip  {eid} not collected yet")
            continue
        if not any("{*}" in p for p in s.get("schema", {})):
            fail(f"{eid}: no {{*}} marker -- data-keyed object is being tracked "
                 f"field by field, every new key will read as drift")
    print(f"ok    {len(MUST_COLLAPSE)} data-keyed endpoints stay collapsed")

    # 4. Fixed structs must not be collapsed.
    for eid in MUST_NOT_COLLAPSE:
        s = snaps.get(eid)
        if s is None:
            print(f"skip  {eid} not collected yet")
            continue
        bad = [p for p in s.get("schema", {}) if p.endswith("{*}")]
        if bad:
            fail(f"{eid}: collapsed {bad[:3]} -- real field names were swallowed")
    print(f"ok    {len(MUST_NOT_COLLAPSE)} struct endpoints stay expanded")

    # 5. Every snapshot must carry the state the optional-field logic needs.
    for eid, s in snaps.items():
        for key, typ in (("schema", dict), ("url", str), ("id", str)):
            if not isinstance(s.get(key), typ):
                fail(f"{eid}: snapshot missing or malformed '{key}'")
    print("ok    all snapshots well-formed")

    print()
    if fails:
        print(f"{len(fails)} FAILURE(S)")
        return 1
    print("corpus clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
