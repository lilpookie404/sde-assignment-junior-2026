
import csv
import json
from pathlib import Path

from solution.normalize import normalize_phone
from solution.secure import phone_lookup_digest


_PAYLOAD_FIELDS = {
    "call.initiated": (),
    "call.telephony_failed": ("reason",),
    "call.connected": (),
    "call.ended": ("duration_sec", "end_reason"),
    "disposition.set": ("disposition_code",),
    "disposition.override": ("disposition_code", "overridden_by", "reason"),
}


def load_dispositions(path):
    with Path(path).open(newline="", encoding="utf-8") as source:
        return [
            (
                row["workflow_id"],
                row["code"],
                int(row["is_terminal"].lower() == "true"),
                int(row["is_success"].lower() == "true"),
            )
            for row in csv.DictReader(source)
        ]


def _sanitize_payload(event_type, payload):
    if not isinstance(payload, dict):
        return {}
    return {
        name: payload[name]
        for name in _PAYLOAD_FIELDS.get(event_type, ())
        if name in payload
    }


def _linked_status(connection, event_type, workflow_id, payload):
    if not str(event_type or "").startswith("disposition."):
        return "active"
    code = payload.get("disposition_code")
    known = connection.execute(
        """
        SELECT 1 FROM disposition_codes
        WHERE workflow_id = ? AND code = ?
        """,
        (workflow_id, code),
    ).fetchone()
    return "active" if known else "unknown_disposition"


def _lead_for_phone(connection, workflow_id, lookup):
    if lookup is None:
        return None
    row = connection.execute(
        """
        SELECT id FROM leads
        WHERE workflow_id = ? AND phone_lookup = ?
        """,
        (workflow_id, lookup),
    ).fetchone()
    return row[0] if row else None


def _lead_for_call(connection, workflow_id, call_id):
    if call_id is None:
        return None
    row = connection.execute(
        """
        SELECT lead_id FROM events
        WHERE workflow_id = ? AND call_id = ? AND lead_id IS NOT NULL
        LIMIT 1
        """,
        (workflow_id, call_id),
    ).fetchone()
    return row[0] if row else None


def _refresh_status_counters(connection):
    connection.execute(
        """
        UPDATE processed_event_files
        SET orphan_events = (
                SELECT COUNT(*) FROM events
                WHERE events.source_path = processed_event_files.source_path
                  AND events.status = 'orphan'
            ),
            unknown_disposition_events = (
                SELECT COUNT(*) FROM events
                WHERE events.source_path = processed_event_files.source_path
                  AND events.status = 'unknown_disposition'
            )
        """
    )


def relink_orphan_events(connection):
    rows = connection.execute(
        """
        SELECT event_id, event_type, workflow_id, call_id,
               phone_lookup, payload_json
        FROM events
        WHERE status = 'orphan'
        ORDER BY rowid
        """
    ).fetchall()

    for event_id, event_type, workflow_id, _, lookup, payload_json in rows:
        if event_type == "disposition.override":
            continue
        lead_id = _lead_for_phone(connection, workflow_id, lookup)
        if lead_id is None:
            continue
        status = _linked_status(
            connection, event_type, workflow_id, json.loads(payload_json)
        )
        connection.execute(
            "UPDATE events SET lead_id = ?, status = ? WHERE event_id = ?",
            (lead_id, status, event_id),
        )

    overrides = connection.execute(
        """
        SELECT event_id, event_type, workflow_id, call_id, payload_json
        FROM events
        WHERE status = 'orphan' AND event_type = 'disposition.override'
        ORDER BY rowid
        """
    ).fetchall()
    for event_id, event_type, workflow_id, call_id, payload_json in overrides:
        lead_id = _lead_for_call(connection, workflow_id, call_id)
        if lead_id is None:
            continue
        status = _linked_status(
            connection, event_type, workflow_id, json.loads(payload_json)
        )
        connection.execute(
            "UPDATE events SET lead_id = ?, status = ? WHERE event_id = ?",
            (lead_id, status, event_id),
        )

    _refresh_status_counters(connection)


def ingest_event_file(connection, path, key, dispositions):
    path = Path(path)
    source_path = str(path.resolve())
    base_stats = {
        "events_total": 0,
        "malformed_events": 0,
        "duplicate_events_dropped": 0,
    }

    with connection:
        connection.executemany(
            """
            INSERT INTO disposition_codes (
                workflow_id, code, is_terminal, is_success
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT (workflow_id, code) DO UPDATE SET
                is_terminal = excluded.is_terminal,
                is_success = excluded.is_success
            """,
            sorted(dispositions),
        )
        if connection.execute(
            "SELECT 1 FROM processed_event_files WHERE source_path = ?",
            (source_path,),
        ).fetchone():
            return None
        connection.execute(
            """
            INSERT INTO processed_event_files (
                source_path, events_total, malformed_events,
                duplicate_events_dropped, orphan_events,
                unknown_disposition_events
            ) VALUES (?, 0, 0, 0, 0, 0)
            """,
            (source_path,),
        )

        with path.open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                base_stats["events_total"] += 1
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    base_stats["malformed_events"] += 1
                    continue
                if not isinstance(value, dict) or not value.get("event_id"):
                    base_stats["malformed_events"] += 1
                    continue

                event_id = str(value["event_id"])
                if connection.execute(
                    "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
                ).fetchone():
                    base_stats["duplicate_events_dropped"] += 1
                    continue

                event_type = value.get("event_type")
                workflow_id = value.get("workflow_id")
                call_id = value.get("call_id")
                payload = _sanitize_payload(event_type, value.get("payload"))
                if event_type == "disposition.override":
                    lookup = None
                    lead_id = _lead_for_call(connection, workflow_id, call_id)
                else:
                    raw_phone = value.get("phone")
                    phone = (
                        normalize_phone(str(raw_phone))
                        if raw_phone is not None
                        else None
                    )
                    lookup = phone_lookup_digest(phone, key) if phone else None
                    lead_id = _lead_for_phone(connection, workflow_id, lookup)

                status = (
                    "orphan"
                    if lead_id is None
                    else _linked_status(connection, event_type, workflow_id, payload)
                )
                connection.execute(
                    """
                    INSERT INTO events (
                        event_id, event_type, workflow_id, call_id, occurred_at,
                        phone_lookup, payload_json, lead_id, source_path, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        event_type,
                        workflow_id,
                        call_id,
                        value.get("occurred_at"),
                        lookup,
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        lead_id,
                        source_path,
                        status,
                    ),
                )

        connection.execute(
            """
            UPDATE processed_event_files
            SET events_total = ?, malformed_events = ?,
                duplicate_events_dropped = ?
            WHERE source_path = ?
            """,
            (
                base_stats["events_total"],
                base_stats["malformed_events"],
                base_stats["duplicate_events_dropped"],
                source_path,
            ),
        )
        relink_orphan_events(connection)
        row = connection.execute(
            """
            SELECT events_total, malformed_events, duplicate_events_dropped,
                   orphan_events, unknown_disposition_events
            FROM processed_event_files
            WHERE source_path = ?
            """,
            (source_path,),
        ).fetchone()

    return dict(
        zip(
            (
                "events_total",
                "malformed_events",
                "duplicate_events_dropped",
                "orphan_events",
                "unknown_disposition_events",
            ),
            row,
        )
    )


def ingest_event_files(connection, paths, key, dispositions):
    results = []
    for path in sorted((Path(path) for path in paths), key=lambda value: value.name):
        result = ingest_event_file(connection, path, key, dispositions)
        if result is not None:
            results.append(result)
    return results
