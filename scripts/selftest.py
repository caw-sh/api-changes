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

# 4c. Real dynamic maps whose key count sits below any sane threshold.
check("npm devDependencies is a map", c.is_map(
    {k: "^1.0.0" for k in ("after connect-redis cookie-parser cookie-session ejs "
                           "eslint express-session hbs marked morgan multiparty "
                           "pbkdf2-password supertest vhost").split()}), True)

check("composer require-dev is a map", c.is_map(
    {k: "^1.0" for k in ("aws/aws-sdk-php doctrine/couchdb ext-json graylog2/gelf-php "
                         "guzzlehttp/guzzle mongodb/mongodb php-amqplib/php-amqplib "
                         "phpstan/phpstan predis/predis symfony/mailer").split()}), True)

check("package exports is a map", c.is_map(
    {k: "./x.js" for k in ["./", "./package.json", "./unstable/ast", "./unstable/ast/clone",
                           "./unstable/ast/factory", "./unstable/ast/is", "./lib",
                           "./lib/x", "./types"]}), True)

# 4d. ...and a fixed struct of URL fields is NOT a map, at the same key count.
check("sprite struct is not a map", c.is_map(
    {k: "https://x/y.png" for k in ["back_default", "back_shiny", "back_transparent",
                                    "front_default", "front_shiny", "front_transparent",
                                    "back_gray", "front_gray"]}), False)

# 5. Status page: incidents[] empty -> populated is a first observation.
ch, _, fo, _st = c.classify({"incidents": ["array"]},
                       {"incidents": ["array"], "incidents[]": ["object"],
                        "incidents[].id": ["string"]})
check("empty container filled", (len(ch), len(fo)), (0, 2))

# 6. Real breaking changes must still fire.
check("real type change",
      [(d["kind"], d["severity"]) for d in c.classify({"id": ["string"]}, {"id": ["integer"]})[0]],
      [("TYPE_CHANGED", "breaking")])

# A removal must be sustained. One absence is a blink; three is a decision.
old, new, st = {"a": ["string"], "b": ["string"]}, {"a": ["string"]}, {}
seen = []
for _ in range(3):
    ch, carried, _fo, st = c.classify(old, new, True, st)
    seen.append([(d["kind"], d["severity"]) for d in ch])
    old = {**new, **carried}
check("removal held on run 1", seen[0], [])
check("removal held on run 2", seen[1], [])
check("removal fires on run 3", seen[2], [("FIELD_REMOVED", "breaking")])

# ...and a field that comes back is optional forever after.
old, new, st = {"a": ["string"], "b": ["string"]}, {"a": ["string"]}, {}
ch, carried, _fo, st = c.classify(old, new, True, st)          # b absent once
old = {**new, **carried}
_, _, _, st = c.classify(old, {"a": ["string"], "b": ["string"]}, True, st)   # b returns
check("returning field marked optional", st["optional"], ["b"])
ch, _, _, st = c.classify({"a": ["string"], "b": ["string"]}, {"a": ["string"]}, True, st)
check("optional field never reported", ch, [])

# A spec is a contract: absence there is immediate and real.
check("spec removal is immediate",
      [d["kind"] for d in c.classify({"a": ["string"], "b": ["string"]},
                                     {"a": ["string"]}, sampled=False)[0]],
      ["FIELD_REMOVED"])

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
