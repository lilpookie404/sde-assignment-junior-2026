
import csv
import json
from pathlib import Path

from solution.normalize import (
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_header,
    normalize_phone,
)
from solution.secure import encrypt_pii, phone_lookup_digest


def _header_indexes(header, fields):
    names = {}
    for field in fields:
        for name in (field["name"], *field.get("aliases", [])):
            names[normalize_header(name)] = field["name"]

    indexes = {}
    for index, value in enumerate(header):
        field_name = names.get(normalize_header(value))
        if field_name is not None and field_name not in indexes:
            indexes[field_name] = index
    return indexes


def _normalize_value(value, field):
    field_type = field["type"]
    if field_type == "string":
        return value
    if field_type == "phone":
        return normalize_phone(value)
    if field_type == "date":
        return normalize_date(value)
    if field_type == "decimal":
        normalized = normalize_decimal(value)
        return str(normalized) if normalized is not None else None
    if field_type == "enum":
        return normalize_enum(value, field["values"])
    raise ValueError(f"unsupported field type: {field_type}")


def _normalize_row(row, indexes, fields):
    values = {}
    warnings = 0
    for field in fields:
        name = field["name"]
        index = indexes.get(name)
        raw = row[index].strip() if index is not None and index < len(row) else ""
        if not raw:
            values[name] = None
            if field.get("required"):
                return None, f"missing_required:{name}", 0
            continue

        normalized = _normalize_value(raw, field)
        values[name] = normalized
        if field["type"] == "phone" and normalized is None:
            return None, "invalid_phone", 0
        if normalized is None and not field.get("required"):
            warnings += 1
    return values, None, warnings


def _merged_non_pii(fields, values, existing=None):
    merged = dict(existing or {})
    stored_fields = [
        field
        for field in fields
        if not field.get("pii") and not field.get("reference")
    ]
    for field in stored_fields:
        name = field["name"]
        merged.setdefault(name, None)
        if values[name] is not None:
            merged[name] = values[name]
    for field in stored_fields:
        name = field["name"]
        if merged[name] is None and "default" in field:
            merged[name] = field["default"]
    return merged


def _store_lead(connection, workflow_id, batch_id, fields, values, key):
    phone = values["phone"]
    lookup = phone_lookup_digest(phone, key)
    existing = connection.execute(
        """
        SELECT id, normalized_fields_json
        FROM leads
        WHERE workflow_id = ? AND phone_lookup = ?
        """,
        (workflow_id, lookup),
    ).fetchone()
    reference_name = next(
        field["name"] for field in fields if field.get("reference")
    )
    existing_fields = json.loads(existing[1]) if existing else None
    normalized_fields = _merged_non_pii(fields, values, existing_fields)
    normalized_json = json.dumps(
        normalized_fields, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    encrypted_phone = encrypt_pii(phone, key)
    encrypted_name = encrypt_pii(values["customer_name"], key)

    if existing:
        connection.execute(
            """
            UPDATE leads
            SET phone_encrypted = ?, customer_name_encrypted = ?,
                external_ref = ?, normalized_fields_json = ?
            WHERE id = ?
            """,
            (
                encrypted_phone,
                encrypted_name,
                values[reference_name],
                normalized_json,
                existing[0],
            ),
        )
        return True

    connection.execute(
        """
        INSERT INTO leads (
            workflow_id, phone_lookup, phone_encrypted,
            customer_name_encrypted, batch_id, external_ref,
            normalized_fields_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            workflow_id,
            lookup,
            encrypted_phone,
            encrypted_name,
            batch_id,
            values[reference_name],
            normalized_json,
        ),
    )
    return False


def ingest_lead_file(connection, path, schema, key):
    path = Path(path)
    source_path = str(path.resolve())
    workflow_id = schema["workflow_id"]
    batch_id = path.stem
    fields = schema["fields"]
    stats = {
        "lead_rows_total": 0,
        "lead_rows_rejected": 0,
        "duplicate_lead_rows_merged": 0,
        "field_warnings": 0,
    }

    with connection:
        if connection.execute(
            "SELECT 1 FROM processed_lead_files WHERE source_path = ?",
            (source_path,),
        ).fetchone():
            return None

        connection.execute(
            """
            INSERT INTO processed_lead_files (
                source_path, workflow_id, batch_id, lead_rows_total,
                lead_rows_rejected, duplicate_lead_rows_merged, field_warnings
            ) VALUES (?, ?, ?, 0, 0, 0, 0)
            """,
            (source_path, workflow_id, batch_id),
        )

        with path.open(newline="", encoding="utf-8") as source:
            reader = csv.reader(source)
            indexes = _header_indexes(next(reader, []), fields)
            row_number = 0
            for row in reader:
                if all(not value.strip() for value in row):
                    continue
                row_number += 1
                stats["lead_rows_total"] += 1
                values, reason, warnings = _normalize_row(row, indexes, fields)
                if reason is not None:
                    stats["lead_rows_rejected"] += 1
                    connection.execute(
                        """
                        INSERT INTO lead_rejections (
                            source_path, workflow_id, batch_id, row_number, reason
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (source_path, workflow_id, batch_id, row_number, reason),
                    )
                    continue

                stats["field_warnings"] += warnings
                if _store_lead(
                    connection, workflow_id, batch_id, fields, values, key
                ):
                    stats["duplicate_lead_rows_merged"] += 1

        from solution.events import relink_orphan_events

        relink_orphan_events(connection)
        connection.execute(
            """
            UPDATE processed_lead_files
            SET lead_rows_total = ?, lead_rows_rejected = ?,
                duplicate_lead_rows_merged = ?, field_warnings = ?
            WHERE source_path = ?
            """,
            (
                stats["lead_rows_total"],
                stats["lead_rows_rejected"],
                stats["duplicate_lead_rows_merged"],
                stats["field_warnings"],
                source_path,
            ),
        )
    return stats


def ingest_lead_files(connection, paths, schema, key):
    results = []
    for path in sorted((Path(path) for path in paths), key=lambda value: value.name):
        result = ingest_lead_file(connection, path, schema, key)
        if result is not None:
            results.append(result)
    return results
