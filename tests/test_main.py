import csv
import json

import pytest

from solution import main, secure


def test_run_command_ingests_drops_and_writes_all_reports(tmp_path):
    drops = [tmp_path / "drop_1", tmp_path / "drop_2"]
    for drop in drops:
        (drop / "events").mkdir(parents=True)
    event = {
        "event_id": "event-1",
        "event_type": "call.initiated",
        "workflow_id": "motor_renewal",
        "call_id": "call-1",
        "occurred_at": "2026-10-03T00:00:00Z",
        "phone": "9876543210",
        "payload": {},
    }
    (drops[0] / "events" / "events_0001.jsonl").write_text(
        json.dumps(event) + "\n", encoding="utf-8"
    )
    leads = drops[1] / "leads" / "motor_renewal"
    leads.mkdir(parents=True)
    with (leads / "batch_003.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(
            [
                "customer_name",
                "phone",
                "policy_number",
                "policy_expiry",
                "premium_amount",
                "preferred_language",
            ]
        )
        writer.writerow(["Alice", "9876543210", "POL-1", "", "", "en"])

    output = tmp_path / "reports"
    arguments = [
        "run",
        *(str(drop) for drop in drops),
        "--db",
        str(tmp_path / "ledger.sqlite"),
        "--key",
        str(tmp_path / "pii.key"),
        "--out",
        str(output),
    ]
    result = main.main(arguments)

    assert result == 0
    assert sorted(path.name for path in output.iterdir()) == [
        "batch_summary.csv",
        "daily_overview.csv",
        "data_quality.json",
        "lead_report.csv",
        "rejections.csv",
    ]
    assert (output / "lead_report.csv").read_text().splitlines()[1] == (
        "motor_renewal,POL-1,batch_003,+91******3210,in_progress_dialed,1,0,"
        "2026-10-03T05:30:00+05:30,,false"
    )

    before = {path.name: path.read_bytes() for path in output.iterdir()}
    assert main.main(arguments) == 0
    assert {path.name: path.read_bytes() for path in output.iterdir()} == before

    other_key = tmp_path / "other.key"
    secure.load_or_create_key(other_key)
    wrong_key_arguments = list(arguments)
    wrong_key_arguments[wrong_key_arguments.index(str(tmp_path / "pii.key"))] = str(
        other_key
    )
    with pytest.raises(ValueError, match="does not match this database"):
        main.main(wrong_key_arguments)
