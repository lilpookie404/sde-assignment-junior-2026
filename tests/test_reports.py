import csv
import json
from datetime import timedelta

import pytest

from solution import db, reports, secure


def test_parse_timestamp_converts_utc_offset_and_naive_to_ist():
    assert reports.parse_timestamp("2026-10-03T00:00:00Z").isoformat() == (
        "2026-10-03T05:30:00+05:30"
    )
    assert reports.parse_timestamp("2026-10-03T10:00:00+02:00").isoformat() == (
        "2026-10-03T13:30:00+05:30"
    )
    naive = reports.parse_timestamp("2026-10-03 10:01:00")
    assert naive.isoformat() == "2026-10-03T10:01:00+05:30"
    assert naive.utcoffset() == timedelta(hours=5, minutes=30)


def test_effective_disposition_prefers_latest_override_then_latest_set():
    values = [
        {
            "event_type": "disposition.set",
            "occurred_at": "2026-10-03T10:03:00Z",
            "code": "callback_requested",
        },
        {
            "event_type": "disposition.override",
            "occurred_at": "2026-10-03T10:02:00Z",
            "code": "renewed",
        },
        {
            "event_type": "disposition.override",
            "occurred_at": "2026-10-03T10:04:00Z",
            "code": "not_interested",
        },
    ]

    assert reports.effective_disposition(values) == "not_interested"
    assert reports.effective_disposition(values[:1]) == "callback_requested"
    assert reports.effective_disposition([]) is None


@pytest.mark.parametrize(
    ("calls", "expected"),
    [
        ([], "yet_to_reach_out"),
        ([{"terminal": True, "telephony_failed": True}], "completed"),
        (
            [
                {"terminal": False, "telephony_failed": True},
                {"terminal": False, "telephony_failed": True},
            ],
            "in_progress_telephony_failed",
        ),
        (
            [
                {"terminal": False, "telephony_failed": True},
                {"terminal": False, "telephony_failed": False},
            ],
            "in_progress_dialed",
        ),
    ],
)
def test_lead_bucket_precedence(calls, expected):
    assert reports.lead_bucket(calls) == expected


def _insert_lead(connection, key, workflow, reference, batch, phone):
    cursor = connection.execute(
        """
        INSERT INTO leads (
            workflow_id, phone_lookup, phone_encrypted,
            customer_name_encrypted, batch_id, external_ref,
            normalized_fields_json
        ) VALUES (?, ?, ?, ?, ?, ?, '{}')
        """,
        (
            workflow,
            secure.phone_lookup_digest(phone, key),
            secure.encrypt_pii(phone, key),
            secure.encrypt_pii("Test Person", key),
            batch,
            reference,
        ),
    )
    return cursor.lastrowid


def _insert_event(
    connection,
    source,
    event_id,
    event_type,
    workflow,
    call_id,
    occurred_at,
    lead_id,
    payload=None,
):
    connection.execute(
        """
        INSERT INTO events (
            event_id, event_type, workflow_id, call_id, occurred_at,
            phone_lookup, payload_json, lead_id, source_path, status
        ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, 'active')
        """,
        (
            event_id,
            event_type,
            workflow,
            call_id,
            occurred_at,
            json.dumps(payload or {}),
            lead_id,
            source,
        ),
    )


def test_generate_reports_writes_exact_sorted_contract(tmp_path):
    key = secure.load_or_create_key(tmp_path / "pii.key")
    connection = db.connect_database(tmp_path / "ledger.sqlite")
    source = str(tmp_path / "events.jsonl")

    connection.executemany(
        """
        INSERT INTO disposition_codes (
            workflow_id, code, is_terminal, is_success
        ) VALUES (?, ?, ?, ?)
        """,
        [
            ("motor_renewal", "renewed", 1, 1),
            ("motor_renewal", "payment_link_sent", 0, 0),
            ("policy_feedback", "partial_feedback", 0, 0),
        ],
    )
    connection.executemany(
        """
        INSERT INTO processed_lead_files VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("lead-a", "motor_renewal", "batch_001", 3, 0, 1, 2),
            ("lead-b", "motor_renewal", "batch_002", 2, 1, 0, 0),
            ("lead-c", "policy_feedback", "batch_003", 2, 1, 0, 1),
        ],
    )
    connection.executemany(
        """
        INSERT INTO lead_rejections VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("lead-c", "policy_feedback", "batch_003", 2, "invalid_phone"),
            (
                "lead-b",
                "motor_renewal",
                "batch_002",
                1,
                "missing_required:customer_name",
            ),
        ],
    )
    connection.execute(
        "INSERT INTO processed_event_files VALUES (?, 12, 1, 2, 1, 1)",
        (source,),
    )

    completed = _insert_lead(
        connection, key, "motor_renewal", "M-001", "batch_001", "+919876541111"
    )
    untouched = _insert_lead(
        connection, key, "motor_renewal", "M-002", "batch_002", "+919876542222"
    )
    failed = _insert_lead(
        connection, key, "motor_renewal", "M-003", "batch_001", "+919876543333"
    )
    dialed = _insert_lead(
        connection,
        key,
        "policy_feedback",
        "O-001",
        "batch_003",
        "+919876544444",
    )

    _insert_event(
        connection,
        source,
        "e1",
        "call.initiated",
        "motor_renewal",
        "c1",
        "2026-10-01T20:00:00Z",
        completed,
    )
    _insert_event(
        connection,
        source,
        "e2",
        "call.connected",
        "motor_renewal",
        "c1",
        "2026-10-01T20:01:00Z",
        completed,
    )
    _insert_event(
        connection,
        source,
        "e3",
        "disposition.set",
        "motor_renewal",
        "c1",
        "2026-10-01T20:03:00Z",
        completed,
        {"disposition_code": "payment_link_sent"},
    )
    _insert_event(
        connection,
        source,
        "e4",
        "disposition.override",
        "motor_renewal",
        "c1",
        "2026-10-01T20:02:00Z",
        completed,
        {"disposition_code": "renewed"},
    )
    _insert_event(
        connection,
        source,
        "e5",
        "call.initiated",
        "motor_renewal",
        "c2",
        "2026-10-02 09:00:00",
        failed,
    )
    _insert_event(
        connection,
        source,
        "e6",
        "call.telephony_failed",
        "motor_renewal",
        "c2",
        "2026-10-02 09:00:01",
        failed,
    )
    _insert_event(
        connection,
        source,
        "e7",
        "call.initiated",
        "policy_feedback",
        "c3",
        "2026-10-02T08:00:00+02:00",
        dialed,
    )
    _insert_event(
        connection,
        source,
        "e8",
        "disposition.set",
        "policy_feedback",
        "c3",
        "2026-10-02T08:02:00+02:00",
        dialed,
        {"disposition_code": "partial_feedback"},
    )
    connection.commit()

    output = tmp_path / "reports"
    reports.generate_reports(connection, output, key)

    assert (output / "batch_summary.csv").read_text() == (
        "workflow_id,batch_id,total_leads,yet_to_reach_out,"
        "in_progress_telephony_failed,in_progress_dialed,completed\n"
        "motor_renewal,batch_001,2,0,1,0,1\n"
        "motor_renewal,batch_002,1,1,0,0,0\n"
        "policy_feedback,batch_003,1,0,0,1,0\n"
    )
    assert (output / "daily_overview.csv").read_text() == (
        "workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads\n"
        "motor_renewal,2026-10-02,2,1,0.5000,1\n"
        "policy_feedback,2026-10-02,1,0,0.0000,0\n"
    )
    assert (output / "lead_report.csv").read_text() == (
        "workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,"
        "connected_calls,last_call_at,current_disposition,ever_success\n"
        "motor_renewal,M-001,batch_001,+91******1111,completed,1,1,"
        "2026-10-02T01:30:00+05:30,renewed,true\n"
        "motor_renewal,M-002,batch_002,+91******2222,yet_to_reach_out,0,0,,,false\n"
        "motor_renewal,M-003,batch_001,+91******3333,in_progress_telephony_failed,"
        "1,0,2026-10-02T09:00:00+05:30,,false\n"
        "policy_feedback,O-001,batch_003,+91******4444,in_progress_dialed,1,0,"
        "2026-10-02T11:30:00+05:30,partial_feedback,false\n"
    )
    assert (output / "rejections.csv").read_text() == (
        "workflow_id,batch_id,row_number,reason\n"
        "motor_renewal,batch_002,1,missing_required:customer_name\n"
        "policy_feedback,batch_003,2,invalid_phone\n"
    )
    assert json.loads((output / "data_quality.json").read_text()) == {
        "lead_rows_total": 7,
        "lead_rows_rejected": 2,
        "rejections_by_reason": {
            "invalid_phone": 1,
            "missing_required:customer_name": 1,
        },
        "duplicate_lead_rows_merged": 1,
        "field_warnings": 3,
        "events_total": 12,
        "malformed_events": 1,
        "duplicate_events_dropped": 2,
        "orphan_events": 1,
        "unknown_disposition_events": 1,
    }
    assert sorted(path.name for path in output.iterdir()) == [
        "batch_summary.csv",
        "daily_overview.csv",
        "data_quality.json",
        "lead_report.csv",
        "rejections.csv",
    ]
    connection.close()
