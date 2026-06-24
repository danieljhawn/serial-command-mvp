import argparse
import json
import sys
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import serial


DEFAULT_CONFIG_PATH = Path(__file__).with_name("device_configure.json")
VIEWER_PATH = Path(__file__).with_name("configure_viewer.html")


@dataclass(frozen=True)
class ConfigureConfig:
    ports: list[str]
    apply_commands: list[str]
    verify_commands: list[str]
    baud: int
    timeout: float
    prompt_prefix: str
    line_ending: str
    log_dir: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply configuration commands to one or more serial devices and verify the result."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"Path to JSON config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--port", help="Configure one serial port instead of configured ports, e.g. COM11")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt before sending apply commands.",
    )
    parser.add_argument(
        "--no-viewer",
        action="store_true",
        help="Do not generate and open the HTML viewer after configuring.",
    )
    return parser.parse_args()


def load_config(path: Path, port_override: str | None = None) -> ConfigureConfig:
    try:
        raw_config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"Config file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON config '{path}': {exc}") from None

    apply_commands = require_string_list(raw_config, "apply_commands")
    verify_commands = require_string_list(raw_config, "verify_commands")
    if not apply_commands:
        raise ValueError("Config value 'apply_commands' must include at least one command.")
    if not verify_commands:
        raise ValueError("Config value 'verify_commands' must include at least one command.")

    line_ending = str(raw_config.get("line_ending", "cr")).lower()
    if line_ending not in {"none", "cr", "lf", "crlf"}:
        raise ValueError("Config value 'line_ending' must be one of: none, cr, lf, crlf.")

    ports = [port_override] if port_override else get_ports(raw_config)
    if not ports:
        raise ValueError("Config value 'ports' must include at least one COM port, or pass --port.")

    return ConfigureConfig(
        ports=ports,
        apply_commands=apply_commands,
        verify_commands=verify_commands,
        baud=int(raw_config.get("baud", 4096)),
        timeout=float(raw_config.get("timeout", 10.0)),
        prompt_prefix=str(raw_config.get("prompt_prefix", "# SGS")),
        line_ending=line_ending,
        log_dir=Path(str(raw_config.get("log_dir", "logs"))),
    )


def get_ports(config: dict[str, Any]) -> list[str]:
    if "ports" in config:
        return require_string_list(config, "ports")
    port = str(config.get("port", "")).strip()
    return [port] if port else []


def require_string_list(config: dict[str, Any], key: str) -> list[str]:
    value = config.get(key)
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


def wait_for_ready_prompt(ser: serial.Serial, config: ConfigureConfig) -> dict[str, Any]:
    started_at = time.monotonic()
    try:
        ser.write(get_line_ending_bytes(config.line_ending))
        ser.flush()
        response, prompt_seen, elapsed = read_until_prompt(
            ser,
            config.timeout,
            config.prompt_prefix,
        )
    except Exception as exc:
        elapsed = time.monotonic() - started_at
        return {
            "timestamp": timestamp(),
            "phase": "startup",
            "command": "",
            "status": "ERROR",
            "elapsed_seconds": round(elapsed, 3),
            "response_bytes": 0,
            "response_text": [],
            "response_fields": {},
            "error": str(exc),
        }
    response_lines = clean_response_lines(response, config.prompt_prefix)
    return {
        "timestamp": timestamp(),
        "phase": "startup",
        "command": "",
        "status": "OK" if prompt_seen else "TIMEOUT",
        "elapsed_seconds": round(elapsed, 3),
        "response_bytes": len(response),
        "response_text": response_lines,
        "response_fields": parse_response_fields(response_lines),
        "error": "" if prompt_seen else f"Prompt '{config.prompt_prefix}' did not return within {config.timeout:g} seconds.",
    }


def open_serial(port: str, config: ConfigureConfig) -> serial.Serial:
    return serial.Serial(
        port=port,
        baudrate=config.baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0,
        write_timeout=config.timeout,
    )


def run_command(ser: serial.Serial, phase: str, command: str, config: ConfigureConfig) -> dict[str, Any]:
    command_bytes = build_command_bytes(command, config.line_ending)
    started_at = time.monotonic()
    sent_at = timestamp()

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
        return {
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

    response_lines = clean_response_lines(response, config.prompt_prefix)
    prompt_error = "" if prompt_seen else f"Prompt '{config.prompt_prefix}' did not return within {config.timeout:g} seconds."
    return {
        "timestamp": timestamp(),
        "phase": phase,
        "command": command,
        "status": "OK" if prompt_seen else "TIMEOUT",
        "elapsed_seconds": round(elapsed, 3),
        "response_bytes": len(response),
        "response_text": response_lines,
        "response_fields": parse_response_fields(response_lines),
        "error": prompt_error,
    }


def make_log_path(config: ConfigureConfig, config_path: Path) -> Path:
    log_dir = config.log_dir
    if not log_dir.is_absolute():
        log_dir = config_path.parent / log_dir

    log_dir.mkdir(parents=True, exist_ok=True)
    filename = f"configure_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return log_dir / filename


def make_report_path(log_path: Path) -> Path:
    return log_path.with_name(f"{log_path.stem}_viewer.html")


def create_viewer_report(log_path: Path) -> Path:
    if not VIEWER_PATH.exists():
        raise FileNotFoundError(f"Viewer template not found: {VIEWER_PATH}")

    viewer_html = VIEWER_PATH.read_text(encoding="utf-8")
    log_json = log_path.read_text(encoding="utf-8")
    embedded_script = (
        f"window.CONFIGURE_LOG_DATA = {log_json};\n"
        f"window.CONFIGURE_LOG_FILE = {json.dumps(log_path.name)};\n"
    )

    marker = "// CONFIGURE_LOG_DATA"
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


def confirm_or_exit(config: ConfigureConfig, assume_yes: bool) -> None:
    print(f"About to configure {len(config.ports)} device(s): {', '.join(config.ports)}")
    print("Apply commands:")
    for command in config.apply_commands:
        print(f"  - {command}")
    print("Verify commands:")
    for command in config.verify_commands:
        print(f"  - {command}")

    if assume_yes:
        return

    answer = input("Type YES to send apply commands: ").strip()
    if answer != "YES":
        raise KeyboardInterrupt("Configuration cancelled.")


def make_device_log(port: str) -> dict[str, Any]:
    return {
        "port": port,
        "started_at": timestamp(),
        "finished_at": None,
        "commands": [],
        "status": "PENDING",
    }


def configure_port(port: str, config: ConfigureConfig) -> dict[str, Any]:
    device_log = make_device_log(port)

    try:
        ser = open_serial(port, config)
    except serial.SerialException as exc:
        device_log["status"] = "ERROR"
        device_log["error"] = f"Could not open serial port '{port}': {exc}"
        device_log["finished_at"] = timestamp()
        print(f"ERROR: {device_log['error']}", file=sys.stderr)
        return device_log

    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        startup_record = wait_for_ready_prompt(ser, config)
        device_log["commands"].append(startup_record)
        print(f"[{port}] [startup] prompt -> {startup_record['status']} in {startup_record['elapsed_seconds']:.3f}s")
        if startup_record["status"] != "OK":
            device_log["status"] = "ERROR"
            return device_log

        for command in config.apply_commands:
            record = run_command(ser, "apply", command, config)
            device_log["commands"].append(record)
            print(f"[{port}] [apply] {command!r} -> {record['status']} in {record['elapsed_seconds']:.3f}s")
            if record["status"] != "OK":
                device_log["status"] = "ERROR"
                return device_log

        for command in config.verify_commands:
            record = run_command(ser, "verify", command, config)
            device_log["commands"].append(record)
            print(f"[{port}] [verify] {command!r} -> {record['status']} in {record['elapsed_seconds']:.3f}s")
            if record["status"] != "OK":
                device_log["status"] = "ERROR"
                return device_log

        device_log["status"] = "OK"
        return device_log
    except serial.SerialException as exc:
        device_log["status"] = "ERROR"
        device_log["error"] = str(exc)
        print(f"ERROR: Serial communication failed on {port}: {exc}", file=sys.stderr)
        return device_log
    finally:
        try:
            ser.close()
        except Exception:
            pass
        device_log["finished_at"] = timestamp()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()

    try:
        config = load_config(config_path, args.port)
        confirm_or_exit(config, args.yes)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt as exc:
        print(str(exc) or "Configuration cancelled.", file=sys.stderr)
        return 130

    log_path = make_log_path(config, config_path)
    log: dict[str, Any] = {
        "started_at": timestamp(),
        "finished_at": None,
        "config_path": str(config_path),
        "ports": config.ports,
        "settings": {
            "baud": config.baud,
            "timeout": config.timeout,
            "prompt_prefix": config.prompt_prefix,
            "line_ending": config.line_ending,
        },
        "apply_commands": config.apply_commands,
        "verify_commands": config.verify_commands,
        "devices": {},
        "status": "PENDING",
    }

    for port in config.ports:
        print("")
        print(f"Configuring {port}...")
        device_log = configure_port(port, config)
        log["devices"][port] = device_log
        if device_log["status"] != "OK":
            log["status"] = "ERROR"
            break

    if log["status"] == "PENDING":
        log["status"] = "OK"

    log["finished_at"] = timestamp()
    log_path.write_text(json.dumps(log, indent=2), encoding="utf-8")
    print(f"Wrote log: {log_path}")

    if not args.no_viewer:
        try:
            open_viewer_report(log_path)
        except Exception as exc:
            print(f"WARNING: Could not open viewer: {exc}", file=sys.stderr)

    return 0 if log["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
