import sqlite3

import pytest

from solution import db


LEAD_COLUMNS = {
    "id",
    "workflow_id",
    "phone_lookup",
    "phone_encrypted",
    "customer_name_encrypted",
    "batch_id",
    "external_ref",
    "normalized_fields_json",
}


def test_connect_database_creates_leads_table_without_plaintext_pii(tmp_path):
    with db.connect_database(tmp_path / "ledger.sqlite") as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(leads)")
        }

    assert columns == LEAD_COLUMNS


def test_lead_identity_is_unique_within_workflow(tmp_path):
    lead = (
        "motor_renewal",
        "lookup-digest",
        "encrypted-phone",
        "encrypted-name",
        "batch_001",
        "POL-001",
        '{"preferred_language":"en"}',
    )
    insert = """
        INSERT INTO leads (
            workflow_id, phone_lookup, phone_encrypted,
            customer_name_encrypted, batch_id, external_ref,
            normalized_fields_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """

    with db.connect_database(tmp_path / "ledger.sqlite") as connection:
        connection.execute(insert, lead)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(insert, lead)

        connection.execute(insert, ("policy_feedback", *lead[1:]))


def test_connect_database_migrates_existing_disposition_catalog(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE disposition_codes (
                workflow_id TEXT NOT NULL,
                code TEXT NOT NULL,
                PRIMARY KEY (workflow_id, code)
            )
            """
        )
        connection.execute(
            "INSERT INTO disposition_codes VALUES ('motor_renewal', 'renewed')"
        )

    with db.connect_database(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(disposition_codes)")
        }
        row = connection.execute(
            """
            SELECT workflow_id, code, is_terminal, is_success
            FROM disposition_codes
            """
        ).fetchone()

    assert {"is_terminal", "is_success"} <= columns
    assert row == ("motor_renewal", "renewed", 0, 0)


def test_event_facts_cannot_be_updated_or_deleted(tmp_path):
    with db.connect_database(tmp_path / "ledger.sqlite") as connection:
        connection.execute(
            "INSERT INTO processed_event_files VALUES ('source', 1, 0, 0, 1, 0)"
        )
        connection.execute(
            """
            INSERT INTO events (
                event_id, event_type, workflow_id, call_id, occurred_at,
                phone_lookup, payload_json, lead_id, source_path, status
            ) VALUES (
                'event-1', 'call.initiated', 'motor_renewal', 'call-1',
                '2026-10-03T00:00:00Z', 'digest', '{}', NULL, 'source', 'orphan'
            )
            """
        )

        connection.execute(
            "UPDATE events SET status = 'active' WHERE event_id = 'event-1'"
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE events SET payload_json = '{\"changed\":true}' "
                "WHERE event_id = 'event-1'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute("DELETE FROM events WHERE event_id = 'event-1'")
