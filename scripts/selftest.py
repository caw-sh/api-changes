#!/usr/bin/env python3
"""Regression tests for the collector's noise filters.

Each case here is a real false positive that appeared in the public dataset.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent))
import collect as c

fails = []


def check(name, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'}  {name:<34} got={got!r}")
    if not ok:
        fails.append(f"{name}: got {got!r}, want {want!r}")


# 1. CoinGecko: 5.00 serialises as 5. Not a change.
check("numeric int/float", len(c.classify({"p": ["number"]}, {"p": ["integer"]})[0]), 0)

# 1b. ...but a spec declaring integer instead of number IS a change.
check("spec int/float is real",
      [d["kind"] for d in c.classify({"p": ["number"]}, {"p": ["integer"]}, sampled=False)[0]],
      ["TYPE_CHANGED"])

# 2. Cloudflare: a nullable field finally observed null.
check("first null sighting",
      [d["severity"] for d in c.classify({"x": ["string"]}, {"x": ["null"]})[0]],
      ["warning"])

# 3. NASA APOD: 8 fields that are all strings is a struct, not a map.
apod = {k: "s" for k in ["date", "explanation", "hdurl", "media_type",
                         "service_version", "title", "url", "copyright"]}
check("nasa apod is not a map", c.is_map(apod), False)

# 4. ...but a currency map must still collapse.
cur = {k: 1.5 for k in ("aed ars aud bch bdt bhd bmd brl btc cad chf clp cny czk "
                        "dkk eth eur gbp hkd huf idr ils inr jpy").split()}
check("currency map collapses", c.is_map(cur), True)

# 4b. Objects keyed by id collapse at the lower threshold.
recs = {f"id{i}": {"a": 1, "b": "x"} for i in range(8)}
check("record map collapses", c.is_map(recs), True)

# 5. Status page: incidents[] empty -> populated is a first observation.
ch, _, fo = c.classify({"incidents": ["array"]},
                       {"incidents": ["array"], "incidents[]": ["object"],
                        "incidents[].id": ["string"]})
check("empty container filled", (len(ch), len(fo)), (0, 2))

# 6. Real breaking changes must still fire.
check("real type change",
      [(d["kind"], d["severity"]) for d in c.classify({"id": ["string"]}, {"id": ["integer"]})[0]],
      [("TYPE_CHANGED", "breaking")])

check("real removal",
      [(d["kind"], d["severity"]) for d in c.classify({"a": ["string"], "b": ["string"]},
                                                      {"a": ["string"]})[0]],
      [("FIELD_REMOVED", "breaking")])

check("real addition",
      [d["kind"] for d in c.classify({"a": ["string"]},
                                     {"a": ["string"], "b": ["string"]})[0]],
      ["FIELD_ADDED"])

print()
if fails:
    print(f"{len(fails)} FAILURE(S)")
    for f in fails:
        print("  " + f)
    sys.exit(1)
print("all checks passed")
