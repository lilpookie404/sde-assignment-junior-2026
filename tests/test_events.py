import csv
import json
import sqlite3
from pathlib import Path

import pytest

from solution import db, events, leads, secure


ROOT = Path(__file__).resolve().parents[1]
MOTOR_SCHEMA_PATH = ROOT / "assignment-a-data/schemas/motor_renewal.json"
DISPOSITIONS_PATH = ROOT / "assignment-a-data/schemas/dispositions.csv"
LEAD_HEADER = [
    "customer_name",
    "phone",
    "policy_number",
    "policy_expiry",
    "premium_amount",
    "preferred_language",
]


@pytest.fixture
def connection(tmp_path):
    value = db.connect_database(tmp_path / "ledger.sqlite")
    yield value
    value.close()


@pytest.fixture
def key(tmp_path):
    return secure.load_or_create_key(tmp_path / "pii.key")


@pytest.fixture
def dispositions():
    return events.load_dispositions(DISPOSITIONS_PATH)


def write_jsonl(path, lines):
    with path.open("w", encoding="utf-8") as output:
        for line in lines:
            output.write(line if isinstance(line, str) else json.dumps(line))
            output.write("\n")


def seed_lead(tmp_path, connection, key, phone="9876543210"):
    schema = json.loads(MOTOR_SCHEMA_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "batch_001.csv"
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(LEAD_HEADER)
        writer.writerow(["Alice", phone, "POL-001", "", "", "en"])
    leads.ingest_lead_file(connection, path, schema, key)


def event(event_id="evt-1", event_type="call.initiated", **changes):
    value = {
        "event_id": event_id,
        "event_type": event_type,
        "workflow_id": "motor_renewal",
        "call_id": "call-1",
        "occurred_at": "2026-10-03T10:01:00Z",
        "phone": "+91 98765-43210",
        "payload": {},
    }
    value.update(changes)
    return value


def test_ingest_counts_malformed_lines_and_sanitizes_phone_data(
    tmp_path, connection, key, dispositions
):
    seed_lead(tmp_path, connection, key)
    path = tmp_path / "events_0001.jsonl"
    valid = event(payload={"phone": "9876543210", "customer_name": "Alice"})
    write_jsonl(path, ["not-json", {"event_type": "call.initiated"}, valid])

    result = events.ingest_event_file(connection, path, key, dispositions)
    row = connection.execute(
        """
        SELECT event_id, event_type, workflow_id, call_id, occurred_at,
               phone_lookup, payload_json, lead_id, source_path, status
        FROM events
        """
    ).fetchone()
    persisted = "|".join("" if value is None else str(value) for value in row)

    assert result == {
        "events_total": 3,
        "malformed_events": 2,
        "duplicate_events_dropped": 0,
        "orphan_events": 0,
        "unknown_disposition_events": 0,
    }
    assert row[5] == secure.phone_lookup_digest("+919876543210", key)
    assert json.loads(row[6]) == {}
    assert row[7] is not None
    assert row[9] == "active"
    assert "+919876543210" not in persisted
    assert "9876543210" not in persisted
    assert "Alice" not in persisted
    assert events.ingest_event_file(connection, path, key, dispositions) is None


def test_disposition_catalog_persists_terminal_and_success_flags(
    tmp_path, connection, key
):
    path = tmp_path / "events_0001.jsonl"
    write_jsonl(path, [])
    dispositions = events.load_dispositions(DISPOSITIONS_PATH)

    events.ingest_event_file(connection, path, key, dispositions)
    rows = connection.execute(
        """
        SELECT code, is_terminal, is_success
        FROM disposition_codes
        WHERE workflow_id = 'motor_renewal'
          AND code IN ('renewed', 'payment_link_sent')
        ORDER BY code
        """
    ).fetchall()

    assert rows == [("payment_link_sent", 0, 0), ("renewed", 1, 1)]


def test_ingest_drops_repeated_event_ids_across_files(
    tmp_path, connection, key, dispositions
):
    seed_lead(tmp_path, connection, key)
    first_path = tmp_path / "events_0001.jsonl"
    second_path = tmp_path / "events_0002.jsonl"
    write_jsonl(first_path, [event()])
    write_jsonl(second_path, [event(phone="9123456789")])

    events.ingest_event_file(connection, first_path, key, dispositions)
    result = events.ingest_event_file(connection, second_path, key, dispositions)

    assert result["duplicate_events_dropped"] == 1
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert connection.execute(
        "SELECT SUM(duplicate_events_dropped) FROM processed_event_files"
    ).fetchone()[0] == 1


def test_ingest_marks_unknown_dispositions_ignored(
    tmp_path, connection, key, dispositions
):
    seed_lead(tmp_path, connection, key)
    path = tmp_path / "events_0001.jsonl"
    write_jsonl(
        path,
        [
            event(
                event_type="disposition.set",
                payload={"disposition_code": "not_a_real_code", "phone": "secret"},
            )
        ],
    )

    result = events.ingest_event_file(connection, path, key, dispositions)
    row = connection.execute(
        "SELECT status, payload_json FROM events"
    ).fetchone()

    assert result["unknown_disposition_events"] == 1
    assert row[0] == "unknown_disposition"
    assert json.loads(row[1]) == {"disposition_code": "not_a_real_code"}


def test_orphan_event_links_when_lead_arrives_later(
    tmp_path, connection, key, dispositions
):
    path = tmp_path / "events_0001.jsonl"
    write_jsonl(path, [event()])

    result = events.ingest_event_file(connection, path, key, dispositions)
    assert result["orphan_events"] == 1
    assert connection.execute(
        "SELECT lead_id FROM events"
    ).fetchone()[0] is None

    seed_lead(tmp_path, connection, key)

    row = connection.execute("SELECT lead_id, status FROM events").fetchone()
    orphan_count = connection.execute(
        "SELECT orphan_events FROM processed_event_files"
    ).fetchone()[0]
    assert row[0] is not None
    assert row[1] == "active"
    assert orphan_count == 0


def test_orphan_unknown_disposition_reclassifies_after_lead_arrives(
    tmp_path, connection, key, dispositions
):
    path = tmp_path / "events_0001.jsonl"
    write_jsonl(
        path,
        [
            event(
                event_type="disposition.set",
                payload={"disposition_code": "not_a_real_code"},
            )
        ],
    )

    result = events.ingest_event_file(connection, path, key, dispositions)
    assert result["orphan_events"] == 1
    assert result["unknown_disposition_events"] == 0

    seed_lead(tmp_path, connection, key)

    row = connection.execute("SELECT lead_id, status FROM events").fetchone()
    counters = connection.execute(
        """
        SELECT orphan_events, unknown_disposition_events
        FROM processed_event_files
        """
    ).fetchone()
    assert row[0] is not None
    assert row[1] == "unknown_disposition"
    assert counters == (0, 1)


def test_override_links_through_other_events_for_the_call(
    tmp_path, connection, key, dispositions
):
    seed_lead(tmp_path, connection, key)
    path = tmp_path / "events_0001.jsonl"
    override = event(
        event_id="evt-override",
        event_type="disposition.override",
        phone=None,
        payload={
            "disposition_code": "renewed",
            "overridden_by": "supervisor",
            "reason": "corrected",
        },
    )
    override.pop("phone")
    write_jsonl(path, [override, event(event_id="evt-call")])

    events.ingest_event_file(connection, path, key, dispositions)
    rows = connection.execute(
        "SELECT event_id, lead_id, status FROM events ORDER BY event_id"
    ).fetchall()

    assert rows[0][1] == rows[1][1]
    assert rows[0][1] is not None
    assert {row[2] for row in rows} == {"active"}


def test_event_ingest_rolls_back_marker_and_rows_on_failure(
    tmp_path, connection, key, dispositions
):
    path = tmp_path / "events_0001.jsonl"
    write_jsonl(path, [event(event_id="evt-ok"), event(event_id="evt-fail")])
    connection.execute(
        """
        CREATE TRIGGER fail_event
        BEFORE INSERT ON events
        WHEN NEW.event_id = 'evt-fail'
        BEGIN
            SELECT RAISE(ABORT, 'forced event failure');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced event failure"):
        events.ingest_event_file(connection, path, key, dispositions)

    assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert connection.execute(
        "SELECT COUNT(*) FROM processed_event_files"
    ).fetchone()[0] == 0

    connection.execute("DROP TRIGGER fail_event")
    result = events.ingest_event_file(connection, path, key, dispositions)
    assert result["events_total"] == 2
    assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
