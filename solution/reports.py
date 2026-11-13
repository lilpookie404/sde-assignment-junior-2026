import csv
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from solution.secure import decrypt_pii


IST = timezone(timedelta(hours=5, minutes=30))

BATCH_SUMMARY_HEADER = (
    "workflow_id",
    "batch_id",
    "total_leads",
    "yet_to_reach_out",
    "in_progress_telephony_failed",
    "in_progress_dialed",
    "completed",
)
DAILY_OVERVIEW_HEADER = (
    "workflow_id",
    "date",
    "total_calls",
    "connected_calls",
    "connectivity_rate",
    "success_leads",
)
LEAD_REPORT_HEADER = (
    "workflow_id",
    "external_ref",
    "batch_id",
    "phone_masked",
    "bucket",
    "total_calls",
    "connected_calls",
    "last_call_at",
    "current_disposition",
    "ever_success",
)
REJECTIONS_HEADER = ("workflow_id", "batch_id", "row_number", "reason")


def parse_timestamp(value):
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def effective_disposition(events):
    overrides = [event for event in events if event["event_type"] == "disposition.override"]
    candidates = overrides or [
        event for event in events if event["event_type"] == "disposition.set"
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda event: parse_timestamp(event["occurred_at"]))[
        "code"
    ]


def lead_bucket(calls):
    if not calls:
        return "yet_to_reach_out"
    if any(call["terminal"] for call in calls):
        return "completed"
    if all(call["telephony_failed"] for call in calls):
        return "in_progress_telephony_failed"
    return "in_progress_dialed"


def _calls(connection):
    catalog = {
        (workflow, code): (bool(terminal), bool(success))
        for workflow, code, terminal, success in connection.execute(
            """
            SELECT workflow_id, code, is_terminal, is_success
            FROM disposition_codes
            """
        )
    }
    grouped = defaultdict(list)
    rows = connection.execute(
        """
        SELECT event_id, event_type, workflow_id, call_id, occurred_at,
               payload_json, lead_id
        FROM events
        WHERE status = 'active' AND lead_id IS NOT NULL
        ORDER BY rowid
        """
    )
    for event_id, event_type, workflow, call_id, occurred_at, payload, lead_id in rows:
        grouped[(lead_id, workflow, call_id or event_id)].append(
            {
                "event_id": event_id,
                "event_type": event_type,
                "occurred_at": occurred_at,
                "payload": json.loads(payload),
            }
        )

    calls = []
    for (lead_id, workflow, call_id), events in grouped.items():
        initiated = [event for event in events if event["event_type"] == "call.initiated"]
        if not initiated:
            continue
        initiated_at = max(parse_timestamp(event["occurred_at"]) for event in initiated)
        dispositions = [
            {
                "event_type": event["event_type"],
                "occurred_at": event["occurred_at"],
                "code": event["payload"].get("disposition_code"),
            }
            for event in events
            if event["event_type"] in {"disposition.set", "disposition.override"}
        ]
        disposition = effective_disposition(dispositions)
        terminal, success = catalog.get((workflow, disposition), (False, False))
        calls.append(
            {
                "lead_id": lead_id,
                "workflow_id": workflow,
                "call_id": call_id,
                "initiated_at": initiated_at,
                "connected": any(
                    event["event_type"] == "call.connected" for event in events
                ),
                "telephony_failed": any(
                    event["event_type"] == "call.telephony_failed" for event in events
                ),
                "disposition": disposition,
                "terminal": terminal,
                "success": success,
            }
        )
    return calls


def _write_csv(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _quality(connection):
    lead = connection.execute(
        """
        SELECT COALESCE(SUM(lead_rows_total), 0),
               COALESCE(SUM(lead_rows_rejected), 0),
               COALESCE(SUM(duplicate_lead_rows_merged), 0),
               COALESCE(SUM(field_warnings), 0)
        FROM processed_lead_files
        """
    ).fetchone()
    event = connection.execute(
        """
        SELECT COALESCE(SUM(events_total), 0),
               COALESCE(SUM(malformed_events), 0),
               COALESCE(SUM(duplicate_events_dropped), 0),
               COALESCE(SUM(orphan_events), 0),
               COALESCE(SUM(unknown_disposition_events), 0)
        FROM processed_event_files
        """
    ).fetchone()
    reasons = dict(
        connection.execute(
            """
            SELECT reason, COUNT(*) FROM lead_rejections
            GROUP BY reason ORDER BY reason
            """
        )
    )
    return {
        "lead_rows_total": lead[0],
        "lead_rows_rejected": lead[1],
        "rejections_by_reason": reasons,
        "duplicate_lead_rows_merged": lead[2],
        "field_warnings": lead[3],
        "events_total": event[0],
        "malformed_events": event[1],
        "duplicate_events_dropped": event[2],
        "orphan_events": event[3],
        "unknown_disposition_events": event[4],
    }


def generate_reports(connection, output_dir, key):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    calls = _calls(connection)
    calls_by_lead = defaultdict(list)
    for call in calls:
        calls_by_lead[call["lead_id"]].append(call)

    lead_rows = []
    batch_counts = defaultdict(lambda: defaultdict(int))
    leads = connection.execute(
        """
        SELECT id, workflow_id, external_ref, batch_id, phone_encrypted
        FROM leads
        ORDER BY workflow_id, external_ref
        """
    )
    for lead_id, workflow, reference, batch, encrypted_phone in leads:
        lead_calls = calls_by_lead[lead_id]
        bucket = lead_bucket(lead_calls)
        batch_counts[(workflow, batch)][bucket] += 1
        recent = max(lead_calls, key=lambda call: call["initiated_at"]) if lead_calls else None
        disposition_calls = [call for call in lead_calls if call["disposition"]]
        current = (
            max(disposition_calls, key=lambda call: call["initiated_at"])["disposition"]
            if disposition_calls
            else ""
        )
        phone = decrypt_pii(encrypted_phone, key)
        lead_rows.append(
            (
                workflow,
                reference,
                batch,
                f"+91******{phone[-4:]}",
                bucket,
                len(lead_calls),
                sum(call["connected"] for call in lead_calls),
                recent["initiated_at"].isoformat() if recent else "",
                current,
                str(any(call["success"] for call in lead_calls)).lower(),
            )
        )

    batch_rows = []
    for (workflow, batch), counts in sorted(batch_counts.items()):
        batch_rows.append(
            (
                workflow,
                batch,
                sum(counts.values()),
                counts["yet_to_reach_out"],
                counts["in_progress_telephony_failed"],
                counts["in_progress_dialed"],
                counts["completed"],
            )
        )

    daily = defaultdict(list)
    for call in calls:
        daily[(call["workflow_id"], call["initiated_at"].date().isoformat())].append(
            call
        )
    daily_rows = []
    for (workflow, date), day_calls in sorted(daily.items()):
        connected = sum(call["connected"] for call in day_calls)
        success_leads = len(
            {call["lead_id"] for call in day_calls if call["success"]}
        )
        daily_rows.append(
            (
                workflow,
                date,
                len(day_calls),
                connected,
                f"{connected / len(day_calls):.4f}",
                success_leads,
            )
        )

    rejection_rows = connection.execute(
        """
        SELECT workflow_id, batch_id, row_number, reason
        FROM lead_rejections
        ORDER BY workflow_id, batch_id, row_number
        """
    ).fetchall()
    _write_csv(output_dir / "batch_summary.csv", BATCH_SUMMARY_HEADER, batch_rows)
    _write_csv(output_dir / "daily_overview.csv", DAILY_OVERVIEW_HEADER, daily_rows)
    _write_csv(output_dir / "lead_report.csv", LEAD_REPORT_HEADER, lead_rows)
    _write_csv(output_dir / "rejections.csv", REJECTIONS_HEADER, rejection_rows)
    (output_dir / "data_quality.json").write_text(
        json.dumps(_quality(connection), indent=2) + "\n", encoding="utf-8"
    )
