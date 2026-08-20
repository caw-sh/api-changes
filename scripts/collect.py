#!/usr/bin/env python3
"""caw collector — watches public APIs and records how their response shapes change.

Stdlib only. No dependencies, no API keys, read-only requests.
Stores response *shape* (field names + types) — never response values.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENDPOINTS = ROOT / "endpoints.json"
SNAPDIR = ROOT / "snapshots"
DATADIR = ROOT / "data"

UA = "caw-bot/0.1 (+https://caw.sh/bot)"
TIMEOUT = 20
MAX_BYTES = 64 * 1024 * 1024   # some vendor OpenAPI specs are 10-40 MB
ARRAY_SAMPLE = 20         # union this many array elements
MAX_DEPTH = 14
MAX_PATHS = 25000         # guard against pathological payloads
MAP_MIN_KEYS = 8          # objects with >= this many same-shaped keys are treated as maps

# Headers that signal an upcoming change. The first two are the whole point.
WATCH_HEADERS = [
    "deprecation",        # RFC 9745
    "sunset",             # RFC 8594
    "x-api-version",
    "api-version",
    "stripe-version",
    "anthropic-version",
    "openai-version",
    "x-api-deprecation-date",
    "x-api-warn",
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# schema extraction
# --------------------------------------------------------------------------

def type_of(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "unknown"


def shape_sig(v, depth: int = 0) -> str:
    """Cheap structural signature, used to detect map-like objects."""
    if depth > 3:
        return "..."
    if isinstance(v, dict):
        return "{" + ",".join(sorted(v)[:12]) + "}"
    if isinstance(v, list):
        return "[" + (shape_sig(v[0], depth + 1) if v else "") + "]"
    return type_of(v)


SCALAR_MAP_MIN_KEYS = 20   # scalar-valued objects need far more keys to look dynamic


def is_map(node: dict) -> bool:
    """True when an object is a dictionary of records keyed by id/name/date/symbol
    rather than a fixed struct. Keys like these change constantly (a new coin is
    listed, a new date appears) and must not be reported as schema changes.

    The threshold depends on what the values are, and this matters more than it
    looks. NASA's APOD response is a fixed struct of eight fields that all happen
    to be strings -- identical value shapes, so the naive test called it a map and
    collapsed every field into `{*}`. The next run it had seven fields, was not a
    map, and every field appeared to have been deleted and re-added.

    An object whose values are all *scalars* is usually a struct, so it needs many
    more keys before we believe it is a map. An object whose values are all
    identically-shaped *objects or arrays* is a much stronger signal.
    """
    if len(node) < MAP_MIN_KEYS:
        return False
    vals = list(node.values())[:40]
    sigs = {shape_sig(v) for v in vals}
    if len(sigs) != 1:
        return False
    if all(not isinstance(v, (dict, list)) for v in vals):
        return len(node) >= SCALAR_MAP_MIN_KEYS
    return True


def walk(node, path: str, out: dict, depth: int = 0) -> None:
    """Flatten a JSON document into {path: set-of-types}."""
    if depth > MAX_DEPTH or len(out) > MAX_PATHS:
        return
    t = type_of(node)
    out.setdefault(path, set()).add(t)

    if isinstance(node, dict):
        if is_map(node):
            # collapse dynamic keys: union the value shapes under a single path
            for v in list(node.values())[:ARRAY_SAMPLE]:
                walk(v, f"{path}.{{*}}" if path else "{*}", out, depth + 1)
            return
        for k, v in node.items():
            walk(v, f"{path}.{k}" if path else k, out, depth + 1)
    elif isinstance(node, list):
        for item in node[:ARRAY_SAMPLE]:
            walk(item, f"{path}[]", out, depth + 1)


def schema_of(doc) -> dict:
    raw: dict = {}
    walk(doc, "", raw)
    return {p: sorted(ts) for p, ts in sorted(raw.items()) if p}


def _parent(path: str) -> str:
    """The container that holds `path`.  a.b[] -> a.b   a.b -> a   a.{*} -> a"""
    if path.endswith("[]"):
        return path[:-2]
    if "." in path:
        return path.rsplit(".", 1)[0]
    return ""


def unobserved(schema: dict) -> set:
    """Containers that were present this run but empty (`[]` or `{}`), so their
    children could not be seen.

    This is the difference between "the vendor removed these fields" and "there
    was nothing in the list today".  A status page with no open incidents still
    returns `incidents: []` — every field we previously inferred from an incident
    object is unobserved, not deleted."""
    parents = {_parent(p) for p in schema}
    return {p for p, ts in schema.items()
            if p not in parents and ({"array", "object"} & set(ts))}


def shadowed(path: str, blind: set) -> bool:
    """True when `path` sits under a container that was empty this run."""
    while path:
        path = _parent(path)
        if path in blind:
            return True
    return False


def merge_baseline(old: dict, new: dict, carried: dict, kind: str | None) -> dict:
    """The baseline to carry forward: what we saw, plus what we could not see.

    Without `carried`, an emptied list would drop its children from the baseline
    and then re-report all of them as FIELD_ADDED the moment the list refills."""
    merged = dict(new)
    merged.update(carried)
    if kind != "spec":
        # Sampled responses expose a different slice each run. Remember every
        # type a field has ever held so the baseline stops oscillating.
        for p in set(old) & set(new):
            union = sorted(set(old[p]) | set(new[p]))
            if union != new[p]:
                merged[p] = union
    return merged


HTTP_METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")


def spec_schema(doc) -> dict:
    """For an OpenAPI document, describe it at the level people actually care
    about: which operations exist, and roughly how big each one's contract is.
    Diffing whole spec trees produces unreadable noise; operations do not."""
    out: dict = {}
    paths = doc.get("paths") or {}
    for route, ops in sorted(paths.items()):
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method.lower() not in HTTP_METHODS or not isinstance(op, dict):
                continue
            key = f"{method.upper()} {route}"
            facts = [f"params:{len(op.get('parameters') or [])}",
                     f"responses:{len(op.get('responses') or {})}"]
            if op.get("deprecated"):
                facts.append("DEPRECATED")
            if op.get("requestBody"):
                facts.append("body")
            out[key] = sorted(facts)

    schemas = ((doc.get("components") or {}).get("schemas")
               or doc.get("definitions") or {})
    for name, sch in sorted(schemas.items()):
        if isinstance(sch, dict):
            props = sch.get("properties") or {}
            out[f"schema:{name}"] = sorted(
                [f"props:{len(props)}"] + [f"req:{len(sch.get('required') or [])}"])
    return out


def parse_body(body: bytes, url: str):
    """JSON first; fall back to YAML when the URL looks like a spec."""
    try:
        return json.loads(body)
    except Exception:
        pass
    if url.endswith((".yaml", ".yml")):
        try:
            import yaml  # optional; installed in CI
        except ImportError:
            raise ValueError("yaml endpoint but PyYAML not installed")
        return yaml.safe_load(body)
    raise ValueError("not json")


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def fetch(url: str) -> dict:
    headers = {"User-Agent": UA, "Accept": "application/json, */*"}
    # CI runners share IPs; authenticating lifts GitHub's 60/hr anonymous limit
    tok = os.environ.get("GITHUB_TOKEN")
    if tok and url.startswith("https://api.github.com"):
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, headers=headers)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read(MAX_BYTES)
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return {
                "ok": True,
                "status": resp.status,
                "ms": int((time.time() - started) * 1000),
                "headers": headers,
                "body": body,
            }
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read(MAX_BYTES)
        except Exception:
            pass
        return {
            "ok": False,
            "status": e.code,
            "ms": int((time.time() - started) * 1000),
            "headers": {k.lower(): v for k, v in (e.headers or {}).items()},
            "body": body,
            "error": f"HTTP {e.code}",
        }
    except Exception as e:
        return {
            "ok": False,
            "status": 0,
            "ms": int((time.time() - started) * 1000),
            "headers": {},
            "body": b"",
            "error": f"{type(e).__name__}: {e}"[:200],
        }


DEPRECATION_WORDS = re.compile(r"deprecat|sunset|obsolete|end.of.life|retir", re.I)


def watched_headers(headers: dict) -> dict:
    found = {h: headers[h] for h in WATCH_HEADERS if h in headers}
    # Link and Warning are mostly noise; keep them only when they actually
    # carry a deprecation signal (rel="sunset", rel="deprecation", etc.)
    for h in ("link", "warning"):
        v = headers.get(h, "")
        if v and DEPRECATION_WORDS.search(v):
            found[h] = v
    return found


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------

NUMERIC = {"integer", "number"}


def norm_types(ts, sampled: bool) -> set:
    """Types as the *contract* sees them, not as one sample happened to serialise.

    JSON has a single number type. A price of 5.20 serialises as `5.2` and a
    price of 5.00 serialises as `5`, so Python reads one as float and the other
    as int -- and a naive diff reports a breaking type change every time a value
    lands on a round number. That is data variation, not drift.

    Specs are exempt: there `integer` and `number` are genuinely different
    declarations and a vendor changing one to the other is a real event.
    """
    ts = set(ts)
    if sampled and ts & NUMERIC:
        ts = (ts - NUMERIC) | {"number"}
    return ts


def classify(old: dict, new: dict, sampled: bool = True) -> tuple:
    """Returns (changes, carried, first_seen).

    `carried` holds old paths we deliberately did not report as removed because
    this run had no data to observe them in. `first_seen` holds paths observed
    for the first time only because a container that was empty before now has
    contents -- those are first observations, not additions.

    `sampled` is True for live payloads, where each run sees a different slice of
    the data, and False for OpenAPI specs, where the document is the contract and
    anything that disappears really is gone."""
    blind_new = unobserved(new)
    blind_old = unobserved(old)
    changes, carried, first_obs = [], {}, []

    for path in sorted(set(new) - set(old)):
        # A status page returns `incidents: []` until something breaks. When an
        # incident finally appears, forty fields materialise at once. Nothing
        # was added -- we simply had never seen inside that container. Reporting
        # it as drift would mean every outage looks like an API change.
        if sampled and shadowed(path, blind_old):
            first_obs.append(path)
            continue
        changes.append({"kind": "FIELD_ADDED", "path": path, "to": new[path], "severity": "info"})

    for path in sorted(set(old) - set(new)):
        if shadowed(path, blind_new):
            carried[path] = old[path]
            continue
        changes.append({"kind": "FIELD_REMOVED", "path": path, "from": old[path], "severity": "breaking"})

    for path in sorted(set(old) & set(new)):
        a_raw, b_raw = old[path], new[path]
        if a_raw == b_raw:
            continue
        a, b = norm_types(a_raw, sampled), norm_types(b_raw, sampled)
        if a == b:
            continue                      # int/float serialisation only
        if sampled and b < a:
            # we simply did not draw that variant this run; baseline keeps both
            continue
        if a < b:
            # a variant we had not sampled before; the old contract still holds
            kind, sev = "TYPE_WIDENED", "info"
        elif a - {"null"} == b - {"null"}:
            kind, sev = "NULLABILITY", "warning"
        elif sampled and b == {"null"}:
            # The field was always nullable; today is simply the first time we
            # drew a null. Calling this breaking would flag every optional field
            # in every API the first time it happens to be empty.
            kind, sev = "NULLABILITY", "warning"
        else:
            kind, sev = "TYPE_CHANGED", "breaking"
        changes.append({"kind": kind, "path": path,
                        "from": sorted(a_raw), "to": sorted(b_raw), "severity": sev})
    return changes, carried, first_obs


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def append_jsonl(path: Path, rows: list) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    endpoints = json.loads(ENDPOINTS.read_text())
    if only:
        endpoints = [e for e in endpoints if only in e["id"] or only in e.get("vendor", "")]

    SNAPDIR.mkdir(exist_ok=True)
    DATADIR.mkdir(exist_ok=True)

    ts = now()
    observations, change_rows, header_rows = [], [], []
    stats = {"ok": 0, "failed": 0, "changed": 0, "new": 0, "headers": 0, "pending": 0, "written": 0}

    for ep in endpoints:
        eid = ep["id"]
        res = fetch(ep["url"])
        obs = {
            "ts": ts,
            "id": eid,
            "vendor": ep.get("vendor", eid),
            "status": res["status"],
            "ms": res["ms"],
        }

        hdrs = watched_headers(res["headers"])
        if hdrs:
            stats["headers"] += 1
            obs["signal_headers"] = hdrs
            header_rows.append({"ts": ts, "id": eid, "vendor": ep.get("vendor", eid), "headers": hdrs})

        if not res["ok"] or not res["body"]:
            obs["error"] = res.get("error", "empty body")
            stats["failed"] += 1
            observations.append(obs)
            print(f"  FAIL {eid}: {obs['error']}")
            continue

        try:
            doc = parse_body(res["body"], ep["url"])
        except Exception as e:
            obs["error"] = f"parse: {e}"
            stats["failed"] += 1
            observations.append(obs)
            print(f"  FAIL {eid}: {e}")
            continue

        schema = spec_schema(doc) if ep.get("kind") == "spec" else schema_of(doc)
        if len(schema) >= MAX_PATHS:
            # Truncated payloads can produce phantom diffs later. Flag, don't silently accept.
            obs["truncated"] = True
            print(f"  WARN  {eid}: hit the {MAX_PATHS}-path cap — consider a narrower URL")
        obs["fields"] = len(schema)
        stats["ok"] += 1

        snap_path = SNAPDIR / f"{eid}.json"
        confirmed = schema          # what we treat as the baseline going forward
        pending: list = []
        if snap_path.exists():
            prev = json.loads(snap_path.read_text())
            baseline = prev.get("schema", {})
            diffs, carried, first_obs = classify(baseline, schema, ep.get("kind") != "spec")
            if first_obs:
                print(f"  observed {eid}: {len(first_obs)} field(s) seen for the first time "
                      f"(container was empty before) -- not reported as drift")
            confirmed = merge_baseline(baseline, schema, carried, ep.get("kind"))
            keys = sorted(f"{d['kind']}:{d['path']}" for d in diffs)

            # A difference is only real once we have seen the SAME difference on two
            # consecutive runs. Volatile payloads (a different earthquake, a newly
            # listed coin) produce a different diff each time and never confirm,
            # so they never generate an alert. This is the noise filter.
            if diffs and keys != prev.get("pending_keys"):
                pending = keys
                confirmed = baseline           # hold the old baseline, do not report
                diffs = []
                stats["pending"] += 1
                print(f"  pending  {eid}: {len(keys)} unconfirmed difference(s)")

            if diffs:
                stats["changed"] += 1
                breaking = sum(1 for d in diffs if d["severity"] == "breaking")
                change_rows.append({
                    "ts": ts,
                    "id": eid,
                    "vendor": ep.get("vendor", eid),
                    "url": ep["url"],
                    "first_seen": prev.get("first_seen"),
                    "prev_ts": prev.get("last_changed") or prev.get("first_seen"),
                    "counts": {
                        "total": len(diffs),
                        "breaking": breaking,
                        "additive": sum(1 for d in diffs if d["kind"] == "FIELD_ADDED"),
                        "first_observations": len(first_obs),
                    },
                    "changes": diffs[:200],
                })
                mark = "BREAKING" if breaking else "changed "
                print(f"  {mark} {eid}: {len(diffs)} change(s), {breaking} breaking")
            first_seen = prev.get("first_seen", ts)
            last_changed = ts if diffs else prev.get("last_changed", first_seen)
        else:
            stats["new"] += 1
            prev = {}
            first_seen = last_changed = ts
            print(f"  new      {eid}: {len(schema)} fields")

        snapshot = {
            "id": eid,
            "vendor": ep.get("vendor", eid),
            "category": ep.get("category"),
            "url": ep["url"],
            "first_seen": first_seen,
            "last_changed": last_changed,
            "field_count": len(confirmed),
            "pending_keys": pending,
            "schema": confirmed,
        }
        # Only touch the file when something genuinely differs. Rewriting an
        # identical snapshot every hour would bloat the repo and — worse — make
        # `git log` on a snapshot useless for seeing when an API really changed.
        blob = json.dumps(snapshot, indent=1, sort_keys=True)
        if not snap_path.exists() or snap_path.read_text() != blob:
            snap_path.write_text(blob)
            stats["written"] += 1

        observations.append(obs)

    day = ts[:10]
    append_jsonl(DATADIR / "observations" / f"{day}.jsonl", observations)
    append_jsonl(DATADIR / "changes.jsonl", change_rows)
    append_jsonl(DATADIR / "signals.jsonl", header_rows)

    (DATADIR / "last_run.json").write_text(json.dumps(
        {"ts": ts, "endpoints": len(endpoints), **stats}, indent=1))

    print(f"\n{ts}  {len(endpoints)} endpoints  "
          f"ok={stats['ok']} failed={stats['failed']} new={stats['new']} "
          f"changed={stats['changed']} pending={stats['pending']} "
          f"signal-headers={stats['headers']} files-written={stats['written']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
