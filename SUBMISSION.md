# Submission

## Candidate
- **Name:** Vaishnavi Awadhiya
- **Assignment chosen:** A (Data)
- **Time spent (honest estimate):** Approximately 4 hours, AI-assisted and not stopwatch-tracked

## How to run
```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
python -m solution.main run data/drop_1 data/drop_2 \
  --db data/assignment-a.sqlite \
  --key /tmp/assignment-a-pii.key \
  --out reports
python -m pytest -q
```

## Design
SQLite is the durable ledger. `leads` has one row per `(workflow_id, phone_lookup)` and keeps
encrypted phone/name values plus normalized non-PII JSON. `events` keeps one sanitized row per
`event_id`; linked, orphan, and unknown-disposition states allow late lead arrivals to be
reconciled without storing raw event lines or plaintext phones. Event fact columns cannot be
updated or deleted; only the derived `lead_id` and `status` projection changes during late
linking. Disposition terminal/success flags are stored as reference data for reporting.

Each input file and its counters are committed in the same transaction as its rows. Processed
file markers make retries no-ops. Reports are derived from the current ledger, so a lead arriving
in drop 2 automatically makes eligible drop-1 events visible.

PII uses Fernet authenticated encryption. Identity matching uses HMAC-SHA256 with a stable key
derived from the Fernet key and the purpose label `phone-lookup-v1`; the persistent key is
required to live outside the repository.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| One `run` command accepts ordered drops | Small interface that preserves required ingestion order and produces reports immediately | Separate `ingest` and `report` commands |
| Process each drop's leads before its events | Maximizes immediate links; later lead ingestion still reconciles earlier orphans | Events first, which creates avoidable temporary orphans |
| Sanitize event payloads with a field allowlist | Prevents unexpected PII from being persisted | Store the original payload and attempt recursive redaction |
| Bind a key fingerprint to each database | Rejects a wrong key before any new rows can commit | Discover the mismatch while decrypting reports |
| Treat an event without `call_id` as its own group | Keeps malformed-but-accepted events deterministic without merging unrelated calls | Add validation not required by the event contract |
| Generate aggregations in Python | Clear implementation for the required small dataset | More complex SQL aggregation |

## Failure modes considered
- Malformed JSON and missing event IDs are counted and skipped; duplicate event IDs are dropped.
- Invalid lead rows are rejected once with the first schema-ordered reason. Invalid optional
  fields become null and increment warnings.
- File transactions prevent partial row/counter commits after a crash. A retry can safely ingest
  the file again.
- Orphans remain sanitized in the ledger and are relinked when a later lead arrives. Overrides
  link through another event for the same call.
- The database contains ciphertext and digests, not plaintext PII; report masking decrypts only
  long enough to recover the final four digits. A stored fingerprint rejects a different key
  before ingestion starts.
- Known limits: timestamps are not validated during ingestion, so an unsupported value fails
  report generation; report files are replaced individually rather than atomically as one
  five-file snapshot; concurrent writers are not coordinated beyond SQLite's own locking.

## Testing
Focused tests cover normalization, encryption/HMAC stability, schema constraints, transactional
lead/event ingestion, aliases, merge/idempotency rules, malformed/duplicate/orphan events,
disposition flags, IST timestamp parsing, effective disposition, bucket precedence, exact report
headers/content/sorts, and the end-to-end command. I did not benchmark the optional large scale.

## Results
Seed 42 small data completed in 0.78 seconds on the development machine. It produced 1,892 lead
rows in `lead_report.csv` from 2,057 nonblank lead rows, with 108 rejections and 57 duplicate
merges. It read 8,745 event lines: 8 malformed, 237 duplicates, 462 final orphans, and 18 unknown
dispositions. A second identical run produced byte-identical report checksums.

## If I had another day
1. Publish reports through a temporary directory and atomic rename so readers see one snapshot.
2. Add explicit timestamp validation/quarantine during event ingestion.
3. Benchmark the large dataset, then add the indexes or SQL aggregation justified by the profile.
4. Add operational scheduling, metrics/alerts, backups, and controlled key rotation.
