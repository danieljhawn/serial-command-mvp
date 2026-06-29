import argparse
import json
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import serial


DEFAULT_CONFIG_PATH = Path(__file__).with_name("collection_config.json")
VIEWER_PATH = Path(__file__).with_name("log_viewer.html")


@dataclass(frozen=True)
class SerialConfig:
    test_suite: str
    run_type: str
    status_check_command: str
    selected_sequence_name: str
    selected_sequence_value: str
    ports: list[str]
    commands: list[str]
    pre_commands: list[str]
    post_commands: list[str]
    baud: int
    timeout: float
    prompt_prefix: str
    line_ending: str
    log_dir: Path


class ThreadSafeJsonLogger:
    def __init__(self, path: Path, config: SerialConfig, config_path: Path) -> None:
        self.lock = threading.Lock()
        self.path = path
        self.data: dict[str, Any] = {
            "started_at": timestamp(),
            "finished_at": None,
            "test_suite": config.test_suite,
            "run_type": config.run_type,
            "status_check_command": config.status_check_command,
            "selected_sequence": {
                "name": config.selected_sequence_name,
                "value": config.selected_sequence_value,
            },
            "config_path": str(config_path),
            "settings": {
                "test_suite": config.test_suite,
                "run_type": config.run_type,
                "status_check_command": config.status_check_command,
                "selected_sequence": config.selected_sequence_name,
                "baud": config.baud,
                "timeout": config.timeout,
                "prompt_prefix": config.prompt_prefix,
                "line_ending": config.line_ending,
            },
            "devices": {
                port: {
                    "port": port,
                    "status": "PENDING",
                    "commands": [],
                    "errors": [],
                }
                for port in config.ports
            },
        }

    def add_command(self, port: str, record: dict[str, Any]) -> None:
        with self.lock:
            self.data["devices"][port]["commands"].append(record)

    def add_error(self, port: str, message: str) -> None:
        with self.lock:
            self.data["devices"][port]["status"] = "ERROR"
            self.data["devices"][port]["errors"].append(
                {
                    "timestamp": timestamp(),
                    "message": message,
                }
            )

    def set_device_status(self, port: str, status: str) -> None:
        with self.lock:
            self.data["devices"][port]["status"] = status

    def save(self) -> None:
        with self.lock:
            self.data["finished_at"] = timestamp()
            self.path.write_text(
                json.dumps(self.data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll one or more serial ports from a JSON config and log responses."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to JSON config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--no-viewer",
        action="store_true",
        help="Do not generate and open the HTML viewer after polling.",
    )
    parser.add_argument(
        "--sequence",
        help="Test sequence name from collection_config.json to associate with this run.",
    )
    parser.add_argument(
        "--status-check",
        metavar="COMMAND",
        help="Run one command as a status check instead of associating the run with a test sequence.",
    )
    parser.add_argument(
        "--list-sequences",
        action="store_true",
        help="List configured test sequences and exit.",
    )
    return parser.parse_args()


def load_raw_config(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"Config file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON config '{path}': {exc}") from None


def load_config(path: Path, sequence_name: str = "", status_check_command: str = "") -> SerialConfig:
    raw_config = load_raw_config(path)
    ports = require_string_list(raw_config, "ports")
    commands = [status_check_command] if status_check_command else require_string_list(raw_config, "commands")
    test_sequences = get_test_sequences(raw_config)
    selected_sequence_name = sequence_name
    selected_sequence_value = ""
    run_type = "status_check" if status_check_command else "collection"

    if selected_sequence_name:
        if selected_sequence_name not in test_sequences:
            raise ValueError(
                f"Unknown sequence '{selected_sequence_name}'. "
                f"Use --list-sequences to see configured names."
            )
        selected_sequence_value = test_sequences[selected_sequence_name]

    if not ports:
        raise ValueError("Config value 'ports' must include at least one COM port.")
    if not commands:
        raise ValueError("Config value 'commands' must include at least one command.")

    line_ending = str(raw_config.get("line_ending", "cr")).lower()
    if line_ending not in {"none", "cr", "lf", "crlf"}:
        raise ValueError("Config value 'line_ending' must be one of: none, cr, lf, crlf.")

    return SerialConfig(
        test_suite=str(raw_config.get("test_suite", "Serial Log Viewer")),
        run_type=run_type,
        status_check_command=status_check_command,
        selected_sequence_name=selected_sequence_name,
        selected_sequence_value=selected_sequence_value,
        ports=ports,
        commands=commands,
        pre_commands=optional_string_list(raw_config, "pre_commands"),
        post_commands=optional_string_list(raw_config, "post_commands"),
        baud=int(raw_config.get("baud", 4096)),
        timeout=float(raw_config.get("timeout", 10.0)),
        prompt_prefix=str(raw_config.get("prompt_prefix", "# SGS")),
        line_ending=line_ending,
        log_dir=Path(str(raw_config.get("log_dir", "logs"))),
    )


def get_test_sequences(config: dict[str, Any]) -> dict[str, str]:
    value = config.get("test_sequences", {})
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError("Config value 'test_sequences' must be an object of string names and string sequences.")
    return value


def choose_sequence(raw_config: dict[str, Any], requested_sequence: str = "") -> str:
    test_sequences = get_test_sequences(raw_config)
    if requested_sequence or not test_sequences:
        return requested_sequence

    names = list(test_sequences)
    print("Select test sequence for this collection run:")
    for index, name in enumerate(names, start=1):
        print(f"  {index}. {name}")

    while True:
        answer = input("Sequence number or exact name: ").strip()
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(names):
                return names[index - 1]
        if answer in test_sequences:
            return answer
        print("Invalid selection. Try again.")


def print_sequences(raw_config: dict[str, Any]) -> None:
    test_sequences = get_test_sequences(raw_config)
    if not test_sequences:
        print("No test sequences configured.")
        return
    for name, sequence in test_sequences.items():
        print(f"{name}: {sequence}")


def require_string_list(config: dict[str, Any], key: str) -> list[str]:
    if key not in config:
        raise ValueError(f"Config value '{key}' is required.")
    return coerce_string_list(config[key], key)


def optional_string_list(config: dict[str, Any], key: str) -> list[str]:
    if key not in config:
        return []
    return coerce_string_list(config[key], key)


def coerce_string_list(value: Any, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Config value '{key}' must be a list of strings.")
    return value


def get_line_ending_bytes(mode: str) -> bytes:
    mapping = {
        "none": b"",
        "cr": b"\r",
        "lf": b"\n",
        "crlf": b"\r\n",
    }
    return mapping[mode]


def build_command_bytes(command: str, line_ending: str) -> bytes:
    return command.encode("ascii", errors="strict") + get_line_ending_bytes(line_ending)


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def prompt_has_returned(response: bytes, prompt_prefix: str) -> bool:
    text = response.decode("utf-8", errors="replace")
    for line in text.replace("\r", "\n").split("\n"):
        if line.lstrip().startswith(prompt_prefix):
            return True
    return False


def clean_response_lines(response: bytes, prompt_prefix: str) -> list[str]:
    text = response.decode("utf-8", errors="replace")
    lines: list[str] = []

    for line in text.replace("\r", "\n").split("\n"):
        line = line.strip()
        if not line or line.startswith(prompt_prefix) or ">>" in line:
            continue
        lines.append(line)

    return lines


def parse_response_fields(lines: list[str]) -> dict[str, str | list[str]]:
    parsed: dict[str, str | list[str]] = {}

    for line in lines:
        if ":" not in line:
            continue

        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if key not in parsed:
            parsed[key] = value
            continue
        existing_value = parsed[key]
        if isinstance(existing_value, list):
            existing_value.append(value)
        else:
            parsed[key] = [existing_value, value]

    return parsed


def read_until_prompt(
    ser: serial.Serial,
    timeout: float,
    prompt_prefix: str,
) -> tuple[bytes, bool, float]:
    deadline = time.monotonic() + timeout
    started_at = time.monotonic()
    chunks = bytearray()

    while time.monotonic() < deadline:
        waiting = ser.in_waiting
        if waiting:
            chunks.extend(ser.read(waiting))
            if prompt_has_returned(bytes(chunks), prompt_prefix):
                return bytes(chunks), True, time.monotonic() - started_at
            continue
        time.sleep(0.02)

    waiting = ser.in_waiting
    if waiting:
        chunks.extend(ser.read(waiting))

    return bytes(chunks), prompt_has_returned(bytes(chunks), prompt_prefix), time.monotonic() - started_at


def open_serial(port: str, config: SerialConfig) -> serial.Serial:
    return serial.Serial(
        port=port,
        baudrate=config.baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0,
        write_timeout=config.timeout,
    )


def command_plan(config: SerialConfig) -> list[tuple[str, str]]:
    plan: list[tuple[str, str]] = []
    plan.extend(("pre", command) for command in config.pre_commands)
    plan.extend(("test", command) for command in config.commands)
    plan.extend(("post", command) for command in config.post_commands)
    return plan


def run_command(
    ser: serial.Serial,
    logger: ThreadSafeJsonLogger,
    port: str,
    phase: str,
    command: str,
    config: SerialConfig,
) -> bool:
    command_bytes = build_command_bytes(command, config.line_ending)
    sent_at = timestamp()
    started_at = time.monotonic()

    try:
        ser.write(command_bytes)
        ser.flush()
        response, prompt_seen, elapsed = read_until_prompt(
            ser,
            config.timeout,
            config.prompt_prefix,
        )
    except Exception as exc:
        elapsed = time.monotonic() - started_at
        logger.add_command(
            port,
            {
                "timestamp": sent_at,
                "phase": phase,
                "command": command,
                "status": "ERROR",
                "elapsed_seconds": round(elapsed, 3),
                "response_bytes": 0,
                "response_text": [],
                "response_fields": {},
                "error": str(exc),
            }
        )
        return False

    status = "OK" if prompt_seen else "TIMEOUT"
    error = "" if prompt_seen else f"Prompt '{config.prompt_prefix}' did not return within {config.timeout:g} seconds."
    response_lines = clean_response_lines(response, config.prompt_prefix)
    logger.add_command(
        port,
        {
            "timestamp": timestamp(),
            "phase": phase,
            "command": command,
            "status": status,
            "elapsed_seconds": round(elapsed, 3),
            "response_bytes": len(response),
            "response_text": response_lines,
            "response_fields": parse_response_fields(response_lines),
            "error": error,
        }
    )

    if not prompt_seen:
        print(f"[{port}] ERROR: {error}", file=sys.stderr)
        return False

    print(f"[{port}] {phase}: {command!r} -> {len(response)} bytes in {elapsed:.3f}s")
    return True


def run_port(
    port: str,
    config: SerialConfig,
    logger: ThreadSafeJsonLogger,
    start_barrier: threading.Barrier,
    failures: list[str],
    failures_lock: threading.Lock,
) -> None:
    try:
        ser = open_serial(port, config)
    except serial.SerialException as exc:
        message = f"[{port}] Could not open serial port: {exc}"
        print(f"ERROR: {message}", file=sys.stderr)
        start_barrier.abort()
        with failures_lock:
            failures.append(message)
        logger.add_error(port, message)
        logger.add_command(
            port,
            {
                "timestamp": timestamp(),
                "phase": "open",
                "command": "",
                "status": "ERROR",
                "elapsed_seconds": 0.0,
                "response_bytes": 0,
                "response_text": [],
                "response_fields": {},
                "error": str(exc),
            }
        )
        return

    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        start_barrier.wait()

        for phase, command in command_plan(config):
            if not run_command(ser, logger, port, phase, command, config):
                with failures_lock:
                    failures.append(f"[{port}] {phase} command failed: {command}")
                logger.set_device_status(port, "ERROR")
                break
        else:
            logger.set_device_status(port, "OK")
    except threading.BrokenBarrierError:
        message = f"[{port}] Start barrier failed."
        print(f"ERROR: {message}", file=sys.stderr)
        with failures_lock:
            failures.append(message)
        logger.add_error(port, message)
    finally:
        try:
            ser.close()
        except Exception:
            pass


def make_log_path(config: SerialConfig, config_path: Path) -> Path:
    log_dir = config.log_dir
    if not log_dir.is_absolute():
        log_dir = config_path.parent / log_dir

    log_dir.mkdir(parents=True, exist_ok=True)
    filename = f"serial_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return log_dir / filename


def make_report_path(log_path: Path) -> Path:
    return log_path.with_name(f"{log_path.stem}_viewer.html")


def create_viewer_report(log_path: Path) -> Path:
    if not VIEWER_PATH.exists():
        raise FileNotFoundError(f"Viewer template not found: {VIEWER_PATH}")

    viewer_html = VIEWER_PATH.read_text(encoding="utf-8")
    log_json = log_path.read_text(encoding="utf-8")
    embedded_script = (
        f"window.SERIAL_LOG_DATA = {log_json};\n"
        f"window.SERIAL_LOG_FILE = {json.dumps(log_path.name)};\n"
    )

    marker = "// SERIAL_LOG_DATA"
    if marker not in viewer_html:
        raise ValueError(f"Viewer template is missing marker: {marker}")

    report_html = viewer_html.replace(marker, embedded_script, 1)
    report_path = make_report_path(log_path)
    report_path.write_text(report_html, encoding="utf-8")
    return report_path


def open_viewer_report(log_path: Path) -> None:
    report_path = create_viewer_report(log_path)
    webbrowser.open(report_path.resolve().as_uri())
    print(f"Opened viewer: {report_path}")


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()

    if args.status_check and args.sequence:
        print("ERROR: Use either --status-check or --sequence, not both.", file=sys.stderr)
        return 1

    try:
        raw_config = load_raw_config(config_path)
        if args.list_sequences:
            print_sequences(raw_config)
            return 0
        selected_sequence = "" if args.status_check else choose_sequence(raw_config, args.sequence or "")
        config = load_config(config_path, selected_sequence, args.status_check or "")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    log_path = make_log_path(config, config_path)
    logger = ThreadSafeJsonLogger(log_path, config, config_path)
    start_barrier = threading.Barrier(len(config.ports))
    failures: list[str] = []
    failures_lock = threading.Lock()
    threads = [
        threading.Thread(
            target=run_port,
            args=(port, config, logger, start_barrier, failures, failures_lock),
            name=f"serial-{port}",
        )
        for port in config.ports
    ]

    print(f"Writing log to {log_path}")
    print(f"Polling {len(config.ports)} port(s): {', '.join(config.ports)}")
    if config.run_type == "status_check":
        print(f"Status check: {config.status_check_command}")
    if config.selected_sequence_name:
        print(f"Sequence: {config.selected_sequence_name}")

    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        logger.save()

    if not args.no_viewer:
        try:
            open_viewer_report(log_path)
        except Exception as exc:
            print(f"WARNING: Could not open viewer: {exc}", file=sys.stderr)

    if failures:
        print(f"Completed with {len(failures)} failure(s). See log: {log_path}", file=sys.stderr)
        return 1

    print(f"Completed successfully. See log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
