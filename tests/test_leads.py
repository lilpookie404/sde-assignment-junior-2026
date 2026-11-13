import csv
import json
import sqlite3
from pathlib import Path

import pytest

from solution import db, leads, secure


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "assignment-a-data/schemas/motor_renewal.json"
STANDARD_HEADER = [
    "customer_name",
    "phone",
    "policy_number",
    "policy_expiry",
    "premium_amount",
    "preferred_language",
]


@pytest.fixture
def schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def connection(tmp_path):
    value = db.connect_database(tmp_path / "ledger.sqlite")
    yield value
    value.close()


@pytest.fixture
def key(tmp_path):
    return secure.load_or_create_key(tmp_path / "pii.key")


def write_csv(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(header)
        writer.writerows(rows)


def test_ingest_matches_reordered_alias_headers_and_protects_pii(
    tmp_path, connection, schema, key
):
    path = tmp_path / "batch_003.csv"
    write_csv(
        path,
        [
            "Premium",
            "Policy No",
            "Mobile No",
            "Insured Name",
            "Expiry Date",
            "Language",
            "agent_notes",
        ],
        [
            [
                "₹1,234.567",
                "POL-001",
                "+91 98765-43210",
                " Alice ",
                "01-dec-2026",
                "HI",
                "private note",
            ]
        ],
    )

    result = leads.ingest_lead_file(connection, path, schema, key)
    row = connection.execute(
        """
        SELECT phone_lookup, phone_encrypted, customer_name_encrypted,
               batch_id, external_ref, normalized_fields_json
        FROM leads
        """
    ).fetchone()
    normalized = json.loads(row[5])

    assert result == {
        "lead_rows_total": 1,
        "lead_rows_rejected": 0,
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
    }
    assert row[0] == secure.phone_lookup_digest("+919876543210", key)
    assert row[1] != "+919876543210"
    assert row[2] != "Alice"
    assert secure.decrypt_pii(row[1], key) == "+919876543210"
    assert secure.decrypt_pii(row[2], key) == "Alice"
    assert row[3:5] == ("batch_003", "POL-001")
    assert normalized == {
        "policy_expiry": "2026-12-01",
        "preferred_language": "hi",
        "premium_amount": "1234.57",
    }
    assert "private note" not in row[5]
    assert "Alice" not in row[5]
    assert "+919876543210" not in row[5]


def test_ingest_uses_first_rejection_reason_and_nonblank_row_numbers(
    tmp_path, connection, schema, key
):
    path = tmp_path / "batch_reject.csv"
    write_csv(
        path,
        STANDARD_HEADER,
        [
            [" ", "", "", "", "", ""],
            [" ", "bad phone", "", "31/02/2026", "bad", "xx"],
            ["Alice", "555", "", "", "", ""],
        ],
    )

    result = leads.ingest_lead_file(connection, path, schema, key)
    rejections = connection.execute(
        "SELECT row_number, reason FROM lead_rejections ORDER BY row_number"
    ).fetchall()
    rejection_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(lead_rejections)")
    }

    assert result == {
        "lead_rows_total": 2,
        "lead_rows_rejected": 2,
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
    }
    assert rejections == [
        (1, "missing_required:customer_name"),
        (2, "invalid_phone"),
    ]
    assert rejection_columns == {
        "source_path",
        "workflow_id",
        "batch_id",
        "row_number",
        "reason",
    }
    assert connection.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0


def test_ingest_counts_optional_warnings_and_stores_nulls(
    tmp_path, connection, schema, key
):
    path = tmp_path / "batch_warning.csv"
    write_csv(
        path,
        STANDARD_HEADER,
        [["Alice", "9876543210", "POL-001", "soon", "bad", "Hindi"]],
    )

    result = leads.ingest_lead_file(connection, path, schema, key)
    normalized = json.loads(
        connection.execute(
            "SELECT normalized_fields_json FROM leads"
        ).fetchone()[0]
    )

    assert result["field_warnings"] == 3
    assert normalized == {
        "policy_expiry": None,
        "preferred_language": "en",
        "premium_amount": None,
    }


def test_ingest_merges_duplicates_without_changing_original_batch(
    tmp_path, connection, schema, key
):
    first_path = tmp_path / "batch_001.csv"
    second_path = tmp_path / "batch_002.csv"
    write_csv(
        first_path,
        STANDARD_HEADER,
        [["First Name", "9876543210", "POL-001", "", "100", ""]],
    )
    write_csv(
        second_path,
        STANDARD_HEADER,
        [
            [
                "Second Name",
                "+91 98765-43210",
                "POL-002",
                "01/12/2026",
                "",
                "HI",
            ]
        ],
    )

    results = leads.ingest_lead_files(
        connection, [second_path, first_path], schema, key
    )
    row = connection.execute(
        """
        SELECT customer_name_encrypted, batch_id, external_ref,
               normalized_fields_json
        FROM leads
        """
    ).fetchone()

    assert sum(result["duplicate_lead_rows_merged"] for result in results) == 1
    assert secure.decrypt_pii(row[0], key) == "Second Name"
    assert row[1:3] == ("batch_001", "POL-002")
    assert json.loads(row[3]) == {
        "policy_expiry": "2026-12-01",
        "preferred_language": "hi",
        "premium_amount": "100.00",
    }


def test_reingesting_same_file_does_not_repeat_rows_or_quality_counts(
    tmp_path, connection, schema, key
):
    path = tmp_path / "batch_001.csv"
    write_csv(
        path,
        STANDARD_HEADER,
        [
            ["Alice", "9876543210", "POL-001", "soon", "100", "en"],
            ["", "9123456789", "POL-002", "", "", ""],
        ],
    )

    first = leads.ingest_lead_file(connection, path, schema, key)
    second = leads.ingest_lead_file(connection, path, schema, key)
    counts = connection.execute(
        """
        SELECT lead_rows_total, lead_rows_rejected,
               duplicate_lead_rows_merged, field_warnings
        FROM processed_lead_files
        """
    ).fetchall()

    assert first == {
        "lead_rows_total": 2,
        "lead_rows_rejected": 1,
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 1,
    }
    assert second is None
    assert counts == [(2, 1, 0, 1)]
    assert connection.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1
    assert (
        connection.execute("SELECT COUNT(*) FROM lead_rejections").fetchone()[0]
        == 1
    )


def test_ingest_rolls_back_marker_and_rows_after_mid_file_failure(
    tmp_path, connection, schema, key
):
    path = tmp_path / "batch_001.csv"
    write_csv(
        path,
        STANDARD_HEADER,
        [
            ["", "9000000000", "POL-REJECT", "", "", ""],
            ["Alice", "9876543210", "POL-001", "", "", ""],
            ["Bob", "9123456789", "POL-FAIL", "", "", ""],
        ],
    )
    connection.execute(
        """
        CREATE TRIGGER fail_lead
        BEFORE INSERT ON leads
        WHEN NEW.external_ref = 'POL-FAIL'
        BEGIN
            SELECT RAISE(ABORT, 'forced failure');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced failure"):
        leads.ingest_lead_file(connection, path, schema, key)

    assert connection.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
    assert (
        connection.execute("SELECT COUNT(*) FROM processed_lead_files").fetchone()[0]
        == 0
    )
    assert (
        connection.execute("SELECT COUNT(*) FROM lead_rejections").fetchone()[0]
        == 0
    )

    connection.execute("DROP TRIGGER fail_lead")
    result = leads.ingest_lead_file(connection, path, schema, key)

    assert result["lead_rows_total"] == 3
    assert result["lead_rows_rejected"] == 1
    assert connection.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 2
    assert (
        connection.execute("SELECT COUNT(*) FROM lead_rejections").fetchone()[0]
        == 1
    )
