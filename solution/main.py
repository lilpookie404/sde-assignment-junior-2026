import argparse
import json
from pathlib import Path

from solution import db, events, leads, reports, secure


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "assignment-a-data" / "schemas"


def _parser():
    parser = argparse.ArgumentParser(description="Ingest Assignment A drops and write reports")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("drops", nargs="+")
    run.add_argument("--db", required=True)
    run.add_argument("--key", required=True)
    run.add_argument("--out", required=True)
    return parser


def main(arguments=None):
    parser = _parser()
    args = parser.parse_args(arguments)
    drops = [Path(path) for path in args.drops]
    missing = [str(path) for path in drops if not path.is_dir()]
    if missing:
        parser.error(f"drop directory not found: {missing[0]}")

    schemas = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SCHEMAS.glob("*.json"))
    ]
    dispositions = events.load_dispositions(SCHEMAS / "dispositions.csv")
    database = Path(args.db)
    database.parent.mkdir(parents=True, exist_ok=True)
    key = secure.load_or_create_key(args.key)
    connection = db.connect_database(database)
    try:
        secure.bind_database_key(connection, key)
        for drop in drops:
            for schema in schemas:
                leads.ingest_lead_files(
                    connection,
                    (drop / "leads" / schema["workflow_id"]).glob("*.csv"),
                    schema,
                    key,
                )
            events.ingest_event_files(
                connection, (drop / "events").glob("*.jsonl"), key, dispositions
            )
        reports.generate_reports(connection, args.out, key)
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
