import argparse
import sys
import time

import serial


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send one serial command over a serial port and print raw/decoded response."
    )
    parser.add_argument("--port", required=True, help="Serial port name, e.g. COM5")
    parser.add_argument("--cmd", required=True, help="AT command text, e.g. AT")
    parser.add_argument("--baud", type=int, default=4096, help="Baud rate (default: 4096)")
    parser.add_argument(
        "--timeout",
        type=float,
        default=3,
        help="Read timeout in seconds (default: 3)",
    )
    parser.add_argument(
        "--startup-wait",
        type=float,
        default=0.0,
        help="Seconds to collect startup text before sending command (default: 0.0)",
    )
    parser.add_argument(
        "--wait-for-prompt",
        action="store_true",
        help="During startup wait, stop early when prompt marker is seen",
    )
    parser.add_argument(
        "--prompt-marker",
        default=">>",
        help="Prompt marker text used with --wait-for-prompt (default: >>)",
    )
    parser.add_argument(
        "--line-ending",
        choices=("none", "cr", "lf", "crlf"),
        default="cr",
        help="Line ending to append (default: cr)",
    )
    parser.add_argument(
        "--pre-cmd",
        help="Optional command to send first (for example: con)",
    )
    parser.add_argument(
        "--pre-cmd-wait",
        type=float,
        default=1.0,
        help="Seconds to wait for response after --pre-cmd (default: 1.0)",
    )
    return parser.parse_args()


def get_line_ending_bytes(mode: str) -> bytes:
    mapping = {
        "none": b"",
        "cr": b"\r",
        "lf": b"\n",
        "crlf": b"\r\n",
    }
    return mapping[mode]


def read_until_timeout(ser: serial.Serial, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    chunks = bytearray()

    while time.monotonic() < deadline:
        waiting = ser.in_waiting
        if waiting:
            chunks.extend(ser.read(waiting))
            continue
        time.sleep(0.05)

    waiting = ser.in_waiting
    if waiting:
        chunks.extend(ser.read(waiting))

    return bytes(chunks)


def read_with_optional_marker(
    ser: serial.Serial, timeout: float, marker: bytes | None = None
) -> bytes:
    deadline = time.monotonic() + timeout
    chunks = bytearray()

    while time.monotonic() < deadline:
        waiting = ser.in_waiting
        if waiting:
            chunks.extend(ser.read(waiting))
            if marker and marker in chunks:
                break
            continue
        time.sleep(0.05)

    waiting = ser.in_waiting
    if waiting:
        chunks.extend(ser.read(waiting))

    return bytes(chunks)


def build_command_bytes(cmd: str, line_ending: str) -> bytes:
    return cmd.encode("ascii", errors="strict") + get_line_ending_bytes(line_ending)


def main() -> int:
    args = parse_args()

    command_bytes = build_command_bytes(args.cmd, args.line_ending)

    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=args.timeout,
            write_timeout=args.timeout,
        )
    except serial.SerialException as exc:
        print(f"ERROR: Could not open serial port '{args.port}': {exc}", file=sys.stderr)
        return 1

    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        startup_data = b""
        if args.startup_wait > 0:
            marker = args.prompt_marker.encode("ascii", errors="ignore")
            startup_data = read_with_optional_marker(
                ser,
                args.startup_wait,
                marker if args.wait_for_prompt else None,
            )
            print(f"startup raw response bytes ({len(startup_data)}): {startup_data!r}")
            if startup_data:
                print("startup decoded text:")
                print(startup_data.decode("utf-8", errors="replace"))

        if args.pre_cmd:
            pre_cmd_bytes = build_command_bytes(args.pre_cmd, args.line_ending)
            pre_sent_count = ser.write(pre_cmd_bytes)
            ser.flush()
            pre_response = read_until_timeout(ser, args.pre_cmd_wait)
            print(f"pre-cmd bytes sent ({pre_sent_count}): {pre_cmd_bytes!r}")
            print(f"pre-cmd raw response bytes ({len(pre_response)}): {pre_response!r}")

        sent_count = ser.write(command_bytes)
        ser.flush()

        response = read_until_timeout(ser, args.timeout)

        print(f"bytes sent ({sent_count}): {command_bytes!r}")
        print(f"raw response bytes ({len(response)}): {response!r}")
        print("raw response hex:")
        print(response.hex(" "))

        if not response:
            print(
                "WARNING: No response received before timeout. "
                "Check port, wiring, baud rate, and command."
            )
            return 2

        decoded = response.decode("utf-8", errors="replace")
        print("decoded response text:")
        print(decoded)

        return 0
    except serial.SerialException as exc:
        print(f"ERROR: Serial communication failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: Unexpected failure: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            ser.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
