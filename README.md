# api-changes

An open dataset of how public APIs change over time.

Every hour, a script checks a list of public, unauthenticated API endpoints and records
the **shape** of each response — field names, types, nullability — plus any
`Deprecation` ([RFC 9745](https://www.rfc-editor.org/rfc/rfc9745.html)) and
`Sunset` ([RFC 8594](https://www.rfc-editor.org/rfc/rfc8594.html)) headers.
When a shape changes, the change is classified and recorded.

**No response values are ever stored.** Only structure.

## What's here

| Path | Contents |
|---|---|
| `endpoints.json` | The list of tracked endpoints |
| `snapshots/` | Current known shape of each endpoint |
| `data/changes.jsonl` | Every detected change, classified |
| `data/signals.jsonl` | Deprecation / sunset / version headers observed |
| `data/observations.jsonl` | One row per endpoint per run (status, latency, field count) |

## How changes are classified

| Kind | Severity | Meaning |
|---|---|---|
| `FIELD_ADDED` | info | A new field appeared. Backward compatible. |
| `FIELD_REMOVED` | breaking | A field disappeared. |
| `TYPE_CHANGED` | breaking | A field changed type, e.g. integer → string. |
| `NULLABILITY` | warning | A field started or stopped being null. |

## Avoiding false positives

Naive schema diffing is extremely noisy. Two filters are applied:

1. **Dynamic-key collapsing.** An object whose keys are ids, dates or symbols
   (a map, not a struct) is collapsed to a single `{*}` path — so a newly listed
   coin is not reported as a schema change.
2. **Two-run confirmation.** A difference is only recorded once the *same*
   difference is seen on two consecutive runs. Volatile payloads produce a
   different diff every run and therefore never confirm.

## Running it yourself

```bash
python scripts/collect.py          # all endpoints
python scripts/collect.py stripe   # filter by id or vendor
python scripts/report.py           # markdown summary
```

Standard library only, except PyYAML for OpenAPI specs written in YAML.

## Being a good citizen

Read-only `GET` requests. One request per endpoint per hour. Identifies itself as
`caw-bot/0.1 (+https://caw.sh/bot)`. No authentication, no private endpoints.
If you maintain one of these APIs and would rather not be included,
open an issue and it will be removed.

---

Built while working on [caw](https://caw.sh) — know when the APIs you depend on change.
