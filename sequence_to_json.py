#!/usr/bin/env python3
"""
Convert text test sequences to validated JSON.

Input format, one sequence per line:

    NAME = TIME AMPERAGE [TIME AMPERAGE ...]

Examples:

    SOTF = 200ms 500a 10s 0a
    ten min ten amps = 600s 10a

Rules:
- Names must be unique, ignoring capitalization and extra whitespace.
- Every sequence must contain TIME/AMPERAGE pairs.
- Time must be a whole number followed by s or ms.
- Amperage must be a non-negative number followed by a or A.
- Duplicate sequences are rejected after normalization:
  1s is equivalent to 1000ms, and 30A is equivalent to 30a.
- Blank lines and lines beginning with # are ignored.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


TIME_RE = re.compile(r"^(?P<value>\d+)(?P<unit>ms|s)$", re.IGNORECASE)
AMP_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)a$", re.IGNORECASE)


class SequenceValidationError(ValueError):
    """Raised when one or more sequence definitions are invalid."""


@dataclass(frozen=True)
class Step:
    duration_ms: int
    amperage: Decimal
    duration_token: str
    amperage_token: str


def decimal_to_json_number(value: Decimal) -> int | float:
    """Return an int where possible, otherwise a JSON-compatible float."""
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def normalize_decimal(value: Decimal) -> str:
    """Format a Decimal without unnecessary trailing zeroes."""
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_duration(total_ms: int) -> str:
    """Format milliseconds as an exact, human-readable duration."""
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)

    parts: list[str] = []

    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")

    if milliseconds:
        parts.append(f"{seconds}.{milliseconds:03d}s")
    elif seconds or not parts:
        parts.append(f"{seconds}s")

    return " ".join(parts)


def parse_sequence_line(
    line: str,
    line_number: int,
) -> tuple[str, list[Step]]:
    """Parse and validate one nonblank input line."""
    if line.count("=") != 1:
        raise SequenceValidationError(
            f"line {line_number}: expected exactly one '='"
        )

    raw_name, raw_sequence = line.split("=", 1)
    name = " ".join(raw_name.split())

    if not name:
        raise SequenceValidationError(
            f"line {line_number}: sequence name is empty"
        )

    tokens = raw_sequence.split()

    if not tokens:
        raise SequenceValidationError(
            f"line {line_number} ({name!r}): sequence is empty"
        )

    if len(tokens) % 2:
        raise SequenceValidationError(
            f"line {line_number} ({name!r}): expected TIME AMPERAGE "
            f"pairs, but found {len(tokens)} values"
        )

    steps: list[Step] = []

    for index in range(0, len(tokens), 2):
        time_token = tokens[index]
        amperage_token = tokens[index + 1]
        step_number = index // 2 + 1

        time_match = TIME_RE.fullmatch(time_token)
        if not time_match:
            raise SequenceValidationError(
                f"line {line_number} ({name!r}), step {step_number}: "
                f"invalid time {time_token!r}; use a whole number "
                "followed by 's' or 'ms'"
            )

        time_value = int(time_match.group("value"))
        if time_value <= 0:
            raise SequenceValidationError(
                f"line {line_number} ({name!r}), step {step_number}: "
                "duration must be greater than zero"
            )

        time_unit = time_match.group("unit").lower()
        duration_ms = (
            time_value
            if time_unit == "ms"
            else time_value * 1_000
        )

        amperage_match = AMP_RE.fullmatch(amperage_token)
        if not amperage_match:
            raise SequenceValidationError(
                f"line {line_number} ({name!r}), step {step_number}: "
                f"invalid amperage {amperage_token!r}; use a "
                "non-negative number followed by 'a'"
            )

        amperage = Decimal(amperage_match.group("value"))

        steps.append(
            Step(
                duration_ms=duration_ms,
                amperage=amperage,
                duration_token=f"{time_value}{time_unit}",
                amperage_token=f"{normalize_decimal(amperage)}a",
            )
        )

    return name, steps


def convert_text(text: str) -> dict[str, Any]:
    """
    Validate sequence text and return a JSON-compatible dictionary.

    All validation errors are collected and reported together. Nothing is
    returned or written when validation fails.
    """
    errors: list[str] = []

    test_sequences: dict[str, str] = {}
    sequence_metadata: dict[str, dict[str, Any]] = {}

    names_seen: dict[str, tuple[str, int]] = {}
    sequences_seen: dict[
        tuple[tuple[int, Decimal], ...],
        tuple[str, int],
    ] = {}

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()

        if not line or line.startswith("#"):
            continue

        try:
            name, steps = parse_sequence_line(line, line_number)
        except SequenceValidationError as exc:
            errors.append(str(exc))
            continue

        # Name comparison ignores capitalization and repeated whitespace.
        name_key = name.casefold()

        if name_key in names_seen:
            previous_name, previous_line = names_seen[name_key]
            errors.append(
                f"line {line_number}: duplicate name {name!r}; "
                f"already used as {previous_name!r} on line "
                f"{previous_line}"
            )
            continue

        # Sequence comparison uses milliseconds and numeric amperage.
        # This makes 1s equal to 1000ms and 30A equal to 30a.
        sequence_key = tuple(
            (step.duration_ms, step.amperage.normalize())
            for step in steps
        )

        if sequence_key in sequences_seen:
            previous_name, previous_line = sequences_seen[sequence_key]
            errors.append(
                f"line {line_number} ({name!r}): duplicate sequence; "
                "same normalized TIME/AMPERAGE steps as "
                f"{previous_name!r} on line {previous_line}"
            )
            continue

        names_seen[name_key] = (name, line_number)
        sequences_seen[sequence_key] = (name, line_number)

        normalized_sequence = " ".join(
            f"{step.duration_token} {step.amperage_token}"
            for step in steps
        )
        total_duration_ms = sum(step.duration_ms for step in steps)

        test_sequences[name] = normalized_sequence
        sequence_metadata[name] = {
            "total_duration_ms": total_duration_ms,
            "total_duration_seconds": round(
                total_duration_ms / 1_000,
                3,
            ),
            "total_duration_human": format_duration(
                total_duration_ms
            ),
            "step_count": len(steps),
            "steps": [
                {
                    "duration_ms": step.duration_ms,
                    "amperage_a": decimal_to_json_number(
                        step.amperage
                    ),
                }
                for step in steps
            ],
        }

    if errors:
        raise SequenceValidationError("\n".join(errors))

    if not test_sequences:
        raise SequenceValidationError(
            "No test sequences were found in the input."
        )

    return {
        "schema_version": 1,
        "test_sequences": test_sequences,
        "sequence_metadata": sequence_metadata,
    }


def read_input(input_name: str) -> str:
    """Read UTF-8 text from a file, or stdin when input_name is '-'."""
    if input_name == "-":
        return sys.stdin.read()

    return Path(input_name).read_text(encoding="utf-8-sig")


def write_output(
    data: dict[str, Any],
    output_name: str | None,
) -> None:
    """Write formatted JSON to a file, or stdout when no file is given."""
    json_text = json.dumps(
        data,
        indent=2,
        ensure_ascii=False,
    ) + "\n"

    if output_name is None:
        sys.stdout.write(json_text)
        return

    Path(output_name).write_text(json_text, encoding="utf-8")


def default_output_name(input_name: str) -> str | None:
    """Choose INPUT.json unless input is stdin."""
    if input_name == "-":
        return None

    return str(Path(input_name).with_suffix(".json"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate text test sequences and convert them to JSON."
        )
    )
    parser.add_argument(
        "input",
        help="Input text file, or '-' to read from stdin.",
    )
    parser.add_argument(
        "-o",
        "--output",
        help=(
            "Output JSON file. Defaults to the input filename "
            "with a .json extension."
        ),
    )
    args = parser.parse_args()

    output_name = (
        args.output
        if args.output is not None
        else default_output_name(args.input)
    )

    try:
        text = read_input(args.input)
        data = convert_text(text)
        write_output(data, output_name)
    except SequenceValidationError as exc:
        print("Validation failed:", file=sys.stderr)
        for error in str(exc).splitlines():
            print(f"  - {error}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"File error: {exc}", file=sys.stderr)
        return 2

    destination = output_name or "standard output"
    print(
        f"Validated {len(data['test_sequences'])} sequences; "
        f"wrote JSON to {destination}.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
