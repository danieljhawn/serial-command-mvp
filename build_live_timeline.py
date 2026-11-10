"""Build the existing timeline view from live-monitor JSONL sessions."""

import argparse
import base64
import json
import os
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from build_timeline import (
    DEFAULT_VIEWER_PATH, attach_event_log_deltas, attach_log_context,
    attach_test_sequence_labels, choose_test_suite, load_tests, parse_datetime,
    safe_filename, summarize_device, write_report,
)
from live_serial import create_report, report_path
from serial_at_test import clean_response_lines, parse_response_fields


def relative_link(path: Path, output_dir: Path) -> str:
    try:
        return quote(Path(os.path.relpath(path, output_dir)).as_posix(), safe="/")
    except ValueError:  # Different Windows drives.
        return path.resolve().as_uri()


def load_session(path: Path, output_dir: Path) -> tuple[list[dict], list[str]]:
    """Read in capture order; join raw chunks before parsing each command."""
    metadata = {}
    batches = {}
    ports = {}
    warnings = []
    first = last = None
    ended = False
    errors = []
    with path.open(encoding="utf-8") as source:
        for number, line in enumerate(source, 1):
            try:
                event = json.loads(line)
                parse_datetime(event["timestamp"])
                kind = event["kind"]
            except (ValueError, KeyError, TypeError):
                warnings.append(f"{path.name}:{number}: incomplete or invalid record skipped")
                continue
            first = first or event["timestamp"]
            last = event["timestamp"]
            port = event.get("port")
            if port:
                ports.setdefault(port, {"port": port, "status": "OK", "commands": []})
            if kind == "session_start":
                metadata = event
                for configured in event.get("ports", []):
                    ports.setdefault(configured, {"port": configured, "status": "OK", "commands": []})
            if kind == "session_end":
                ended = True
            if kind == "error":
                errors.append(event.get("message", "Capture error"))
                if port:
                    ports[port]["status"] = "ERROR"
            batch_id = event.get("batch_id")
            if batch_id is None:
                continue
            batch = batches.setdefault(batch_id, {
                "timestamp": event["timestamp"], "finished_at": event["timestamp"],
                "commands": [], "devices": {}, "status": "INCOMPLETE",
            })
            batch["finished_at"] = event["timestamp"]
            if kind == "batch_start":
                batch["commands"] = event.get("commands", [])
            if kind == "batch_end":
                batch["status"] = event["status"]
            if kind not in {"tx", "rx", "command_result"} or not port:
                continue
            device = batch["devices"].setdefault(port, {})
            command = device.setdefault(event["step"], {
                "command": event.get("command", ""), "status": "INCOMPLETE", "raw": bytearray(),
            })
            if kind in {"tx", "command_result"}:
                command["command"] = event.get("command", command["command"])
            if kind == "command_result":
                command["status"] = event["status"]
            if kind == "rx":
                command["raw"].extend(base64.b64decode(event["bytes_base64"])
                                      if "bytes_base64" in event else event.get("text", "").encode())

    if first is None:
        raise ValueError(f"No valid timestamped records in {path}")
    if not ended:
        warnings.append(f"{path.name}: session has no end record; capture may be incomplete")
    viewer = report_path(path)
    create_report(path)
    common = {
        "kind": "log", "test_suite": metadata.get("test_suite", ""),
        "file": relative_link(path, output_dir),
        "viewer_file": relative_link(viewer, output_dir), "selected_sequence": {},
        "source_type": "live",
    }
    session = {
        **common, "id": f"{path.resolve()}#session", "timestamp": first,
        "finished_at": last, "title": f"{path.stem} — session",
        "commands": [], "devices": [summarize_device(p, d) for p, d in ports.items()],
        "status": "SAVED" if ended else "INCOMPLETE", "messages": errors,
    }
    logs = [session]
    identities = {}
    for batch_id, batch in batches.items():
        devices = []
        for port in ports:
            commands = []
            for command in batch["devices"].get(port, {}).values():
                lines = clean_response_lines(bytes(command.pop("raw")), metadata.get("prompt_prefix", "# SGS"))
                commands.append({**command, "response_text": lines, "response_fields": parse_response_fields(lines)})
            status = next((c["status"] for c in commands if c["status"] != "OK"), "OK" if commands else "NOT SENT")
            summary = summarize_device(port, {"status": status, "commands": commands})
            identity = identities.setdefault(port, {})
            for field in ("imei", "model", "version"):
                if summary[field]:
                    identity[field] = summary[field]
                elif field in identity:
                    summary[field] = identity[field]
            devices.append(summary)
        logs.append({
            **common, "id": f"{path.resolve()}#batch-{batch_id}",
            "title": f"{path.stem} — batch {batch_id}",
            "timestamp": batch["timestamp"], "finished_at": batch["finished_at"],
            "commands": batch["commands"], "devices": devices, "status": batch["status"],
        })
    for log in logs:
        log["device_count"] = len(log["devices"])
        log["error_count"] = sum(d["status"] != "OK" for d in log["devices"])
        if log["status"] not in {"OK", "SAVED"} or log.get("messages"):
            log["error_count"] = max(1, log["error_count"])
    return logs, warnings


def create_timeline(source: Path, *, csv_path: Path | None = None,
                    report: Path | None = None, index_path: Path | None = None) -> Path:
    source = source.resolve()
    if not source.exists():
        raise FileNotFoundError(f"Live log or folder not found: {source}")
    paths = sorted(source.rglob("live_serial_*.jsonl")) if source.is_dir() else [source]
    if not paths:
        raise ValueError(f"No live_serial_*.jsonl logs found in {source}")
    output_dir = source if source.is_dir() else source.parent
    report = (report or output_dir / f"{safe_filename(source.stem)}_timeline_report.html").resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    logs, warnings = [], []
    for path in paths:
        entries, notices = load_session(path, report.parent)
        logs.extend(entries)
        warnings.extend(notices)
    tests = load_tests(csv_path) if csv_path else []
    attach_log_context(tests, logs)
    # Only an actual eve response advances the event-log baseline.
    attach_event_log_deltas([
        {**log, "devices": [d for d in log["devices"] if "eve" in d["commands"] and d["status"] == "OK"]}
        for log in logs
    ])
    attach_test_sequence_labels(tests, logs)
    suite = choose_test_suite(logs)
    index = {
        "title": f"{suite or 'Live Serial'} Timeline", "test_suite": suite,
        "generated_at": datetime.now().astimezone().isoformat(),
        "source_csv": str(csv_path) if csv_path else "", "source_log_dir": str(source),
        "tests": tests, "logs": logs, "warnings": warnings,
        "events": sorted(tests + logs, key=lambda e: parse_datetime(e["timestamp"])),
    }
    index_path = index_path or report.with_name(report.name.replace("_report.html", "_index.json"))
    if index_path == report:
        index_path = report.with_suffix(".json")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    write_report(index, DEFAULT_VIEWER_PATH, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Live JSONL log or folder to scan recursively.")
    parser.add_argument("--csv", type=Path, help="Optional condensed test results CSV.")
    parser.add_argument("--report", type=Path, help="Output HTML path.")
    parser.add_argument("--index", type=Path, help="Output JSON index path.")
    parser.add_argument("--no-viewer", action="store_true")
    args = parser.parse_args()
    try:
        report = create_timeline(args.source, csv_path=args.csv, report=args.report, index_path=args.index)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Wrote live timeline: {report}")
    if not args.no_viewer:
        webbrowser.open(report.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
