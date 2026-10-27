"""Continuous serial capture and synchronized command batches for the live monitor.

Each worker owns one port and reads it throughout the session, including while
idle. Received bytes are never cleaned or discarded. A batch advances only when
every port returns its prompt; failure cancels the remaining commands.
"""

import base64
import html
import json
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from serial_at_test import SerialConfig, build_command_bytes, open_serial, prompt_has_returned, timestamp


class EventLog:
    """Append and flush each event before publishing it to the live display."""

    def __init__(self, path: Path, stopping: threading.Event) -> None:
        self.path = path
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._file = path.open("x", encoding="utf-8")
        self._lock = threading.Lock()
        self._sequence = 0
        self._stopping = stopping
        self.failure = ""

    def record(self, kind: str, port: str = "", **details: Any) -> None:
        with self._lock:
            if self.failure:
                return
            self._sequence += 1
            event = {
                "id": self._sequence, "timestamp": timestamp(),
                "kind": kind, "port": port, **details,
            }
            try:
                self._file.write(json.dumps(event, ensure_ascii=False) + "\n")
                self._file.flush()
            except OSError as exc:
                self.failure = f"Capture stopped because the log could not be written: {exc}"
                self._stopping.set()
                self.events.put({
                    "id": self._sequence, "timestamp": timestamp(),
                    "kind": "error", "port": "", "message": self.failure,
                })
                return
            self.events.put(event)

    def traffic(self, kind: str, port: str, data: bytes, **details: Any) -> None:
        self.record(
            kind, port, text=data.decode("utf-8", errors="replace"),
            bytes_base64=base64.b64encode(data).decode("ascii"),
            byte_count=len(data), **details,
        )

    def close(self) -> None:
        with self._lock:
            self._file.close()


@dataclass
class CommandRequest:
    command: str
    batch_id: int
    step: int
    barrier: threading.Barrier
    done: threading.Event = field(default_factory=threading.Event)
    status: str = "PENDING"


class PortWorker(threading.Thread):
    """One owner for serial I/O; no startup buffer reset or idle read gaps."""

    def __init__(self, port: str, session: "LiveSession") -> None:
        super().__init__(name=f"live-{port}", daemon=True)
        self.port = port
        self.session = session
        self.requests: queue.Queue[CommandRequest] = queue.Queue()
        self.connected = threading.Event()

    def run(self) -> None:
        ser = None
        active = None
        try:
            ser = self.session.opener(self.port, self.session.config)
            self.connected.set()
            self.session.log.record("connection", self.port, status="CONNECTED")
            while not self.session.stopping.is_set():
                if active is None:
                    try:
                        request = self.requests.get_nowait()
                    except queue.Empty:
                        request = None
                    if request is not None:
                        active = request
                        # Capture queued unsolicited data before sending the command.
                        self.receive(ser, None)
                        request.barrier.wait(timeout=self.session.config.timeout)
                        if self.session.stopping.is_set():
                            break
                        data = build_command_bytes(request.command, self.session.config.line_ending)
                        sent_at = time.monotonic()
                        written = ser.write(data)
                        self.session.log.traffic(
                            "tx", self.port, data[:written], command=request.command,
                            batch_id=request.batch_id, step=request.step,
                        )
                        if written != len(data):
                            raise OSError(f"Only {written} of {len(data)} command bytes were written.")
                        response = bytearray()
                incoming = self.receive(ser, active)
                if active is not None:
                    response.extend(incoming)
                    # Only prompt detection needs buffering; full traffic is on disk.
                    if prompt_has_returned(bytes(response), self.session.config.prompt_prefix):
                        self.complete(active, "OK")
                        active = None
                    elif time.monotonic() - sent_at >= self.session.config.timeout:
                        self.complete(active, "TIMEOUT", "Prompt did not return before timeout.")
                        active = None
                    elif len(response) > 8192:
                        del response[:-4096]
                self.session.stopping.wait(0.01)
            # Include bytes already buffered when Stop was requested.
            self.receive(ser, active)
        except threading.BrokenBarrierError:
            if active is not None:
                self.complete(active, "CANCELLED", "Command start was cancelled or timed out.")
                active = None
        except Exception as exc:
            self.session.log.record("error", self.port, message=str(exc))
            if active is not None:
                active.barrier.abort()
                self.complete(active, "ERROR", str(exc))
                active = None
        finally:
            if active is not None:
                self.complete(active, "CANCELLED", "Session stopped.")
            # Coordinate disconnection with batch enqueueing, so no request can
            # arrive after the worker's last queue drain and wait forever.
            with self.session._lock:
                self.connected.clear()
                while True:
                    try:
                        request = self.requests.get_nowait()
                    except queue.Empty:
                        break
                    request.barrier.abort()
                    self.complete(request, "CANCELLED", "Device disconnected.")
            if ser is not None:
                try:
                    ser.close()
                except Exception as exc:
                    self.session.log.record("error", self.port, message=f"Close failed: {exc}")
            self.session.log.record("connection", self.port, status="DISCONNECTED")

    def receive(self, ser: Any, active: CommandRequest | None) -> bytes:
        waiting = ser.in_waiting
        if not waiting:
            return b""
        data = ser.read(min(waiting, 65536))
        if data:
            self.session.log.traffic(
                "rx", self.port, data,
                batch_id=active.batch_id if active else None,
                step=active.step if active else None,
            )
        return data

    def complete(self, request: CommandRequest, status: str, message: str = "") -> None:
        self.session.log.record(
            "command_result", self.port, command=request.command,
            batch_id=request.batch_id, step=request.step, status=status, message=message,
        )
        request.status = status
        request.done.set()


class LiveSession:
    """Keep all configured ports listening and permit one ordered batch at a time."""

    def __init__(
        self, config: SerialConfig, config_path: Path,
        opener: Callable = open_serial,
    ) -> None:
        self.config = config
        self.opener = opener
        self.stopping = threading.Event()
        self._lock = threading.Lock()
        self._batch_id = 0
        self._batch_thread: threading.Thread | None = None
        self._barrier: threading.Barrier | None = None
        self._closed = False
        folder = config.log_dir
        if not folder.is_absolute():
            folder = config_path.parent / folder
        folder.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("live_serial_%Y%m%d_%H%M%S_%f.jsonl")
        self.log = EventLog(folder / name, self.stopping)
        self.log.record(
            "session_start", schema_version=1, test_suite=config.test_suite,
            config_path=str(config_path), ports=config.ports, baud=config.baud,
            timeout=config.timeout, prompt_prefix=config.prompt_prefix,
            line_ending=config.line_ending,
        )
        self.workers = {port: PortWorker(port, self) for port in config.ports}

    @property
    def busy(self) -> bool:
        return self._batch_thread is not None and self._batch_thread.is_alive()

    @property
    def closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        for worker in self.workers.values():
            worker.start()

    def send(self, commands: list[str]) -> None:
        """Validate a whole batch before sending anything, then dispatch in parallel."""
        if not commands or any(not command.strip() for command in commands):
            raise ValueError("Enter at least one nonblank command.")
        for command in commands:
            if "\r" in command or "\n" in command:
                raise ValueError("Enter each command on its own line, without embedded line endings.")
            try:
                build_command_bytes(command, self.config.line_ending)
            except UnicodeEncodeError:
                raise ValueError("Commands must contain only ASCII characters.") from None
        with self._lock:
            if self.stopping.is_set() or self._closed:
                raise ValueError("The session has stopped.")
            if self.busy:
                raise ValueError("Wait for the current command list to finish.")
            missing = [port for port, worker in self.workers.items() if not worker.connected.is_set()]
            if missing:
                raise ValueError(f"All configured devices must be connected. Unavailable: {', '.join(missing)}")
            self._batch_id += 1
            self._batch_thread = threading.Thread(
                target=self._send_batch, args=(list(commands), self._batch_id),
                name="live-batch", daemon=True,
            )
            self._batch_thread.start()

    def _send_batch(self, commands: list[str], batch_id: int) -> None:
        self.log.record("batch_start", batch_id=batch_id, commands=commands)
        status = "OK"
        try:
            for step, command in enumerate(commands, 1):
                with self._lock:
                    if self.stopping.is_set():
                        status = "CANCELLED"
                        break
                    self._barrier = threading.Barrier(len(self.workers))
                    requests = []
                    for worker in self.workers.values():
                        request = CommandRequest(command, batch_id, step, self._barrier)
                        if not worker.connected.is_set():
                            request.status = "ERROR"
                            request.done.set()
                            self._barrier.abort()
                        else:
                            worker.requests.put(request)
                        requests.append(request)
                for request in requests:
                    while not request.done.wait(0.05):
                        if self.stopping.is_set():
                            break
                if any(request.status != "OK" for request in requests):
                    status = "CANCELLED" if self.stopping.is_set() else "ERROR"
                    break
        except Exception as exc:
            status = "ERROR"
            if self._barrier is not None:
                self._barrier.abort()
            self.log.record("error", message=f"Batch failed: {exc}")
        finally:
            self.log.record("batch_end", batch_id=batch_id, status=status)

    def stop(self) -> Path:
        """Called outside the UI thread; finish capture and build a standalone view."""
        with self._lock:
            if self._closed:
                return report_path(self.log.path)
            self.stopping.set()
            if self._barrier is not None:
                self._barrier.abort()
        for worker in self.workers.values():
            if worker.ident is not None:
                worker.join()
        if self._batch_thread is not None:
            self._batch_thread.join()
        self.log.record("session_end")
        self._closed = True
        self.log.close()
        if self.log.failure:
            raise OSError(self.log.failure)
        return create_report(self.log.path)


def report_path(log_path: Path) -> Path:
    return log_path.with_name(f"{log_path.stem}_viewer.html")


def event_text(event: dict[str, Any]) -> str:
    if event["kind"] in {"tx", "rx"}:
        return event["text"]
    return " · ".join(
        f"{key}: {value}" for key, value in event.items()
        if key not in {"id", "timestamp", "port", "kind"}
    )


def create_report(log_path: Path) -> Path:
    """Read JSONL incrementally so interrupted sessions can also be reviewed."""
    path = report_path(log_path)
    with log_path.open(encoding="utf-8") as source, path.open("w", encoding="utf-8") as output:
        output.write("""<!doctype html><html lang="en"><meta charset="utf-8">
<title>Live serial session</title><style>
body{font:15px system-ui;background:#101820;color:#e7eef5;padding:24px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:10px;
border-bottom:1px solid #304252;vertical-align:top}th{position:sticky;top:0;background:#101820}
pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;font:13px Consolas,monospace}
.tx{color:#80d9ff}.rx{color:#a6e2a0}.error,.command_result{color:#f4cd80}
</style><h1>Live serial session</h1>
<p>Times show when the computer sent or read bytes. RX includes unsolicited output,
echoes, blank lines, and prompts. Rows are received chunks, not necessarily whole lines.
Exact bytes are preserved as base64 in the JSONL log.</p>""")
        output.write(f"<p>Source: {html.escape(log_path.name)}</p>")
        output.write("<table><thead><tr><th>Time</th><th>Port</th><th>Event</th><th>Content</th></tr></thead><tbody>")
        for line in source:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                output.write('<tr><td colspan="4">Incomplete log record skipped.</td></tr>')
                continue
            kind = html.escape(event["kind"], quote=True)
            values = (event["timestamp"], event.get("port", ""), event["kind"].upper(), event_text(event))
            output.write(f'<tr class="{kind}">')
            for value in values:
                output.write(f"<td><pre>{html.escape(str(value))}</pre></td>")
            output.write("</tr>\n")
        output.write("</tbody></table></html>")
    return path
