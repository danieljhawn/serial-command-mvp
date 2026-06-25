import argparse
import csv
import json
import re
import sys
import webbrowser
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parent
DEFAULT_CSV_PATH = ROOT / "condensedTestResults.csv"
DEFAULT_LOG_DIR = ROOT / "logs"
DEFAULT_VIEWER_PATH = ROOT / "timeline_viewer.html"
DEFAULT_INDEX_PATH = ROOT / "timeline_index.json"
DEFAULT_REPORT_PATH = ROOT / "timeline_report.html"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a timeline report from condensed test results and serial JSON logs."
    )
    parser.add_argument(
        "folder",
        nargs="?",
        help="Folder to scan recursively for condensed CSV, serial logs, and configure logs.",
    )
    parser.add_argument("--csv", help="Condensed test results CSV.")
    parser.add_argument("--logs", help="Directory to scan recursively for serial/configure JSON logs.")
    parser.add_argument("--index", help="Output timeline index JSON.")
    parser.add_argument("--report", help="Output embedded timeline HTML report.")
    parser.add_argument(
        "--no-viewer",
        action="store_true",
        help="Do not open the generated timeline report in the browser.",
    )
    return parser.parse_args()


def parse_datetime(value: str) -> datetime:
    value = value.strip()
    formats = (
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %I:%M:%S %p",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
    )
    for date_format in formats:
        try:
            parsed = datetime.strptime(value, date_format)
            return parsed.astimezone() if parsed.tzinfo else parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        except ValueError:
            pass
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone() if parsed.tzinfo else parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)


def to_iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def load_tests(csv_path: Path) -> list[dict[str, Any]]:
    with csv_path.open(newline="", encoding="utf-8-sig") as csv_file:
        rows = list(csv.DictReader(csv_file))

    tests: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        timestamp = parse_datetime(row["Timestamp"])
        device_results = {
            key: clean_cell(value)
            for key, value in row.items()
            if key and key.isdigit() and clean_cell(value)
        }
        tests.append(
            {
                "id": f"test-{index}",
                "kind": "test",
                "timestamp": to_iso(timestamp),
                "title": clean_cell(row.get("Fault test", "")),
                "expected_result": clean_cell(row.get("Expected Result", "")),
                "device_results": device_results,
                "notes": clean_cell(row.get("Notes", "")),
            }
        )
    return tests


def clean_cell(value: Any) -> str:
    if value is None:
        return ""
    return "\n".join(line.strip() for line in str(value).splitlines()).strip()


def find_csv(folder: Path) -> Path:
    if not folder.exists():
        raise FileNotFoundError(f"Folder not found: {folder}")
    if not folder.is_dir():
        raise FileNotFoundError(f"Path is not a folder: {folder}")

    direct_preferred = folder / "condensedTestResults.csv"
    if direct_preferred.exists():
        return direct_preferred

    direct_candidates = sorted(folder.glob("*.csv"))
    if len(direct_candidates) == 1:
        return direct_candidates[0]
    if len(direct_candidates) > 1:
        names = ", ".join(path.name for path in direct_candidates[:5])
        raise ValueError(
            f"Multiple CSV files found directly in {folder}. Pass --csv explicitly. Found: {names}"
        )

    preferred = list(folder.rglob("condensedTestResults.csv"))
    if preferred:
        return sorted(preferred)[0]

    candidates = sorted(folder.rglob("*.csv"))
    if not candidates:
        raise FileNotFoundError(f"No CSV file found in folder: {folder}")
    if len(candidates) > 1:
        names = ", ".join(str(path.relative_to(folder)) for path in candidates[:5])
        raise ValueError(
            f"Multiple CSV files found in {folder}. Pass --csv explicitly. Found: {names}"
        )
    return candidates[0]


def find_log_files(log_dir: Path) -> list[Path]:
    patterns = ("serial_log_*.json", "configure_log_*.json")
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(log_dir.rglob(pattern))
    return sorted(
        path
        for path in paths
        if not path.name.endswith("_viewer.json")
    )


def load_logs(log_dir: Path, link_base_dir: Path) -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    for path in find_log_files(log_dir):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "devices" not in data:
            continue

        logs.append(summarize_log(path, data, link_base_dir))
    return logs


def summarize_log(path: Path, data: dict[str, Any], link_base_dir: Path) -> dict[str, Any]:
    viewer_path = path.with_name(f"{path.stem}_viewer.html")
    devices = [summarize_device(port, device) for port, device in sorted(data.get("devices", {}).items())]
    commands = sorted(
        {
            command.get("command", "")
            for device in data.get("devices", {}).values()
            for command in device.get("commands", [])
            if command.get("command")
        }
    )
    kind = "configure" if path.name.startswith("configure_log_") else "log"
    relative_file = path.relative_to(link_base_dir).as_posix() if path.is_relative_to(link_base_dir) else path.name
    relative_viewer = viewer_path.relative_to(link_base_dir).as_posix() if viewer_path.exists() and viewer_path.is_relative_to(link_base_dir) else ""

    return {
        "id": path.stem,
        "kind": kind,
        "timestamp": data.get("started_at") or data.get("finished_at"),
        "finished_at": data.get("finished_at"),
        "title": path.stem,
        "test_suite": data.get("test_suite") or data.get("settings", {}).get("test_suite", ""),
        "file": relative_file,
        "viewer_file": relative_viewer,
        "commands": commands,
        "devices": devices,
        "device_count": len(devices),
        "error_count": sum(1 for device in devices if device["status"] != "OK"),
    }


def summarize_device(port: str, device: dict[str, Any]) -> dict[str, Any]:
    fields_by_command = {
        command.get("command", ""): command.get("response_fields", {})
        for command in device.get("commands", [])
    }
    con_fields = fields_by_command.get("con", {})
    sta_fields = fields_by_command.get("sta", {})
    eve_fields = fields_by_command.get("eve", {})
    imei = str(con_fields.get("IMEI", ""))

    return {
        "port": device.get("port", port),
        "imei": imei,
        "status": device.get("status", ""),
        "version": value_as_text(con_fields.get("Version", "")),
        "model": value_as_text(con_fields.get("Model #", "")),
        "state": value_as_text(sta_fields.get("State", "")),
        "signal": value_as_text(con_fields.get("Signal", sta_fields.get("Cell Signal", ""))),
        "pfault_count": value_as_text(eve_fields.get("PFault Count", "")),
        "tfault_count": value_as_text(eve_fields.get("TFault Count", "")),
        "wdt_resets": value_as_text(eve_fields.get("WDT Resets", "")),
        "commands": [command.get("command", "") for command in device.get("commands", [])],
    }


def value_as_text(value: Any) -> str:
    if isinstance(value, list):
        return " | ".join(str(item) for item in value)
    return str(value) if value is not None else ""


def attach_log_context(tests: list[dict[str, Any]], logs: list[dict[str, Any]]) -> None:
    if not tests:
        return

    parsed_tests = [(parse_datetime(test["timestamp"]), test) for test in tests]
    for log in logs:
        log_time = parse_datetime(log["timestamp"])
        before = [item for item in parsed_tests if item[0] <= log_time]
        after = [item for item in parsed_tests if item[0] > log_time]
        nearest_before = before[-1][1] if before else None
        nearest_after = after[0][1] if after else None
        log["previous_test_id"] = nearest_before["id"] if nearest_before else ""
        log["next_test_id"] = nearest_after["id"] if nearest_after else ""
        log["position_label"] = build_position_label(log_time, nearest_before, nearest_after)


def build_position_label(
    log_time: datetime,
    nearest_before: dict[str, Any] | None,
    nearest_after: dict[str, Any] | None,
) -> str:
    if nearest_before and nearest_after:
        before_delta = int((log_time - parse_datetime(nearest_before["timestamp"])).total_seconds() // 60)
        after_delta = int((parse_datetime(nearest_after["timestamp"]) - log_time).total_seconds() // 60)
        return f"{before_delta} min after {nearest_before['title']} / {after_delta} min before {nearest_after['title']}"
    if nearest_before:
        before_delta = int((log_time - parse_datetime(nearest_before["timestamp"])).total_seconds() // 60)
        return f"{before_delta} min after {nearest_before['title']}"
    if nearest_after:
        after_delta = int((parse_datetime(nearest_after["timestamp"]) - log_time).total_seconds() // 60)
        return f"{after_delta} min before {nearest_after['title']}"
    return ""


def build_index(csv_path: Path, log_dir: Path) -> dict[str, Any]:
    tests = load_tests(csv_path)
    link_base_dir = log_dir.parent if log_dir.name.lower() == "logs" else log_dir
    logs = load_logs(log_dir, link_base_dir)
    attach_log_context(tests, logs)
    test_suite = choose_test_suite(logs)
    events = sorted(
        tests + logs,
        key=lambda event: (parse_datetime(event["timestamp"]), 0 if event["kind"] == "test" else 1),
    )
    return {
        "title": f"{test_suite} Timeline" if test_suite else "Serial Timeline Report",
        "test_suite": test_suite,
        "generated_at": to_iso(datetime.now().astimezone()),
        "source_csv": str(csv_path),
        "source_log_dir": str(log_dir),
        "tests": tests,
        "logs": logs,
        "events": events,
    }


def choose_test_suite(logs: list[dict[str, Any]]) -> str:
    names = [log["test_suite"] for log in logs if log.get("test_suite")]
    if not names:
        return ""
    return Counter(names).most_common(1)[0][0]


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._-")
    return cleaned or "timeline"


def resolve_inputs(args: argparse.Namespace) -> tuple[Path | None, Path, Path]:
    folder = resolve_folder(args.folder) if args.folder else None
    csv_path = Path(args.csv).resolve() if args.csv else (find_csv(folder) if folder else DEFAULT_CSV_PATH)
    log_dir = Path(args.logs).resolve() if args.logs else (folder if folder else DEFAULT_LOG_DIR)
    return folder, csv_path, log_dir


def resolve_folder(folder_arg: str) -> Path:
    folder = Path(folder_arg).resolve()
    if folder.exists():
        return folder

    logs_folder = (DEFAULT_LOG_DIR / folder_arg).resolve()
    if logs_folder.exists():
        return logs_folder

    return folder


def resolve_outputs(
    args: argparse.Namespace,
    folder: Path | None,
    log_dir: Path,
    index: dict[str, Any],
) -> tuple[Path, Path]:
    output_dir = folder if folder else log_dir
    filename_base = safe_filename(index.get("test_suite", "")) if index.get("test_suite") else "timeline"
    default_index_path = output_dir / f"{filename_base}_timeline_index.json"
    default_report_path = output_dir / f"{filename_base}_timeline_report.html"
    index_path = Path(args.index).resolve() if args.index else default_index_path
    report_path = Path(args.report).resolve() if args.report else default_report_path
    return index_path, report_path


def write_report(index: dict[str, Any], viewer_path: Path, report_path: Path) -> None:
    if not viewer_path.exists():
        raise FileNotFoundError(f"Timeline viewer template not found: {viewer_path}")
    viewer_html = viewer_path.read_text(encoding="utf-8")
    marker = "// TIMELINE_DATA"
    if marker not in viewer_html:
        raise ValueError(f"Timeline viewer template is missing marker: {marker}")
    embedded = f"window.TIMELINE_DATA = {json.dumps(index, indent=2)};"
    report_path.write_text(viewer_html.replace(marker, embedded, 1), encoding="utf-8")


def main() -> int:
    args = parse_args()
    try:
        folder, csv_path, log_dir = resolve_inputs(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    index = build_index(csv_path, log_dir)
    index_path, report_path = resolve_outputs(args, folder, log_dir, index)
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    write_report(index, DEFAULT_VIEWER_PATH, report_path)

    print(f"Wrote timeline index: {index_path}")
    print(f"Wrote timeline report: {report_path}")
    collection_count = sum(1 for log in index["logs"] if log["kind"] == "log")
    configure_count = sum(1 for log in index["logs"] if log["kind"] == "configure")
    print(f"Tests: {len(index['tests'])}; collection logs: {collection_count}; configure logs: {configure_count}")
    if not args.no_viewer:
        try:
            webbrowser.open(report_path.resolve().as_uri())
            print(f"Opened timeline report: {report_path}")
        except Exception as exc:
            print(f"WARNING: Could not open timeline report: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
