
import sqlite3


def connect_database(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS pipeline_metadata (
            name TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            phone_lookup TEXT NOT NULL,
            phone_encrypted TEXT NOT NULL,
            customer_name_encrypted TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            external_ref TEXT NOT NULL,
            normalized_fields_json TEXT NOT NULL,
            UNIQUE (workflow_id, phone_lookup)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_lead_files (
            source_path TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            lead_rows_total INTEGER NOT NULL,
            lead_rows_rejected INTEGER NOT NULL,
            duplicate_lead_rows_merged INTEGER NOT NULL,
            field_warnings INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS lead_rejections (
            source_path TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            batch_id TEXT NOT NULL,
            row_number INTEGER NOT NULL,
            reason TEXT NOT NULL,
            PRIMARY KEY (source_path, row_number),
            FOREIGN KEY (source_path) REFERENCES processed_lead_files(source_path)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS disposition_codes (
            workflow_id TEXT NOT NULL,
            code TEXT NOT NULL,
            is_terminal INTEGER NOT NULL DEFAULT 0,
            is_success INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (workflow_id, code)
        )
        """
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(disposition_codes)")
    }
    for name in ("is_terminal", "is_success"):
        if name not in columns:
            connection.execute(
                f"ALTER TABLE disposition_codes "
                f"ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
            )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_event_files (
            source_path TEXT PRIMARY KEY,
            events_total INTEGER NOT NULL,
            malformed_events INTEGER NOT NULL,
            duplicate_events_dropped INTEGER NOT NULL,
            orphan_events INTEGER NOT NULL,
            unknown_disposition_events INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT,
            workflow_id TEXT,
            call_id TEXT,
            occurred_at TEXT,
            phone_lookup TEXT,
            payload_json TEXT NOT NULL,
            lead_id INTEGER,
            source_path TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('active', 'orphan', 'unknown_disposition')
            ),
            FOREIGN KEY (lead_id) REFERENCES leads(id),
            FOREIGN KEY (source_path) REFERENCES processed_event_files(source_path)
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS events_call_id ON events(call_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS events_phone_lookup ON events(phone_lookup)"
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS events_facts_immutable
        BEFORE UPDATE OF event_id, event_type, workflow_id, call_id, occurred_at,
                         phone_lookup, payload_json, source_path
        ON events
        BEGIN
            SELECT RAISE(ABORT, 'event facts are immutable');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS events_no_delete
        BEFORE DELETE ON events
        BEGIN
            SELECT RAISE(ABORT, 'events cannot be deleted');
        END
        """
    )
    return connection
