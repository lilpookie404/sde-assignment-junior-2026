# Assignment A solution

From the repository root, after generating the two drops, run:

```bash
python -m solution.main run data/drop_1 data/drop_2 \
  --db data/assignment-a.sqlite \
  --key /tmp/assignment-a-pii.key \
  --out reports
```

The key path must be absolute and outside the repository. The command ingests drops in the
order supplied, skips files already recorded in SQLite, and rewrites the five reports from the
current ledger state. Re-running the same command is idempotent.
