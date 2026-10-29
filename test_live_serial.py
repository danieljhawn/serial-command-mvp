"""Live capture checks with thread-safe fake devices; no hardware is opened."""

import base64
import json
import tempfile
import threading
import time
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from live_serial import LiveSession, SerialReplyDecoder, create_report
from serial_at_test import load_config


class ReportTranscripts(HTMLParser):
    """Read visible transcript text as the browser would, across inline spans."""

    def __init__(self):
        super().__init__()
        self.transcripts = {}
        self.section = None
        self.in_transcript = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section":
            self.section = attrs["id"]
        if tag == "pre" and attrs.get("class") == "transcript":
            self.in_transcript = True
            self.transcripts[self.section] = ""

    def handle_endtag(self, tag):
        if tag == "pre":
            self.in_transcript = False

    def handle_data(self, data):
        if self.in_transcript:
            self.transcripts[self.section] += data


class FakeDevice:
    def __init__(self, *, respond=True, fail_write=False, delay=0):
        self.lock = threading.Lock()
        self.buffer = bytearray(b"boot message\r\n# SGS")
        self.writes = []
        self.respond = respond
        self.fail_write = fail_write
        self.delay = delay
        self.closed = False

    def inject(self, data):
        with self.lock:
            self.buffer.extend(data)

    @property
    def in_waiting(self):
        with self.lock:
            return len(self.buffer)

    def read(self, count):
        with self.lock:
            data = bytes(self.buffer[:count])
            del self.buffer[:count]
            return data

    def write(self, data):
        if self.fail_write:
            raise OSError("simulated disconnection")
        self.writes.append(data)
        if self.respond:
            response = b">> " + data + b"\r\nValue: <unexpected>\r\n# SGS"
            if self.delay:
                timer = threading.Timer(self.delay, self.inject, args=(response,))
                timer.daemon = True
                timer.start()
            else:
                self.inject(response)
        return len(data)

    def close(self):
        self.closed = True


class SerialReplyDecoderTests(unittest.TestCase):
    def test_partial_reads_do_not_add_display_lines(self):
        decoder = SerialReplyDecoder()
        chunks = [b"Mod", b"el: SGS\r", b"\nSta", b"te: Ready\r\n", b"# S", b"GS"]
        self.assertEqual(
            "".join(decoder.feed(chunk) for chunk in chunks),
            "Model: SGS\nState: Ready\n# SGS",
        )

    def test_line_endings_and_blank_lines_are_preserved(self):
        decoder = SerialReplyDecoder()
        self.assertEqual(decoder.feed(b"a\rb\n\r\n\r\nc"), "a\nb\n\n\nc")

    def test_utf8_character_split_across_reads(self):
        decoder = SerialReplyDecoder()
        self.assertEqual(decoder.feed(b"Temperature: 20\xc2"), "Temperature: 20")
        self.assertEqual(decoder.feed(b"\xb0C\r\n"), "\u00b0C\n")

    def test_unfinished_character_is_visible_at_disconnect(self):
        decoder = SerialReplyDecoder()
        self.assertEqual(decoder.feed(b"message\xc2"), "message")
        self.assertEqual(decoder.feed(b"", final=True), "\ufffd")


class LiveSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sessions = []
        self.devices = {"COM1": FakeDevice(), "COM2": FakeDevice()}
        self.raw = {
            "ports": list(self.devices), "commands": ["con", "sta"],
            "timeout": 0.2, "log_dir": "logs",
        }
        self.addCleanup(self.stop_sessions)

    def stop_sessions(self):
        for session in self.sessions:
            session.stop()

    def wait_for(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.005)
        self.fail("Timed out waiting for simulated serial activity")

    def start(self, opener=None, ready=True):
        session = LiveSession(
            load_config(self.raw), self.root / "config.json",
            opener=opener or (lambda port, config: self.devices[port]),
        )
        self.sessions.append(session)
        session.start()
        if ready:
            self.wait_for(lambda: all(worker.connected.is_set() for worker in session.workers.values()))
        return session

    def events(self, session):
        return [json.loads(line) for line in session.log.path.read_text(encoding="utf-8").splitlines()]

    def test_idle_and_post_command_bytes_are_preserved_without_filtering(self):
        session = self.start()
        self.wait_for(lambda: any(e["kind"] == "rx" for e in self.events(session)))
        self.devices["COM1"].inject(b"\r\n\r\n>> unsolicited\xff\x00\r\n")
        self.wait_for(lambda: any("unsolicited" in e.get("text", "") for e in self.events(session)))
        session.send(["sta"])
        self.wait_for(lambda: not session.busy)
        self.devices["COM1"].inject(b"later notification\r\n")
        self.wait_for(lambda: any("later notification" in e.get("text", "") for e in self.events(session)))
        report = session.stop()
        events = self.events(session)
        raw = b"".join(base64.b64decode(e["bytes_base64"]) for e in events if e["kind"] == "rx" and e["port"] == "COM1")
        self.assertIn(b"boot message\r\n# SGS", raw)
        self.assertIn(b"\r\n\r\n>> unsolicited\xff\x00\r\n", raw)
        self.assertIn(b"later notification\r\n", raw)
        self.assertEqual([e["id"] for e in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(e["timestamp"] for e in events))
        view = report.read_text(encoding="utf-8")
        self.assertIn("&lt;unexpected&gt;", view)
        self.assertNotIn("<unexpected>", view)
        self.assertTrue(all(device.closed for device in self.devices.values()))

    def test_batch_waits_for_every_device_before_advancing(self):
        self.devices["COM2"].delay = 0.06
        session = self.start()
        session.send(["con", "sta", "eve"])
        self.wait_for(lambda: not session.busy)
        session.stop()
        events = self.events(session)
        for device in self.devices.values():
            self.assertEqual(device.writes, [b"con\r", b"sta\r", b"eve\r"])
        for step in (2, 3):
            previous_results = [e["id"] for e in events if e["kind"] == "command_result" and e["step"] == step - 1]
            next_sends = [e["id"] for e in events if e["kind"] == "tx" and e["step"] == step]
            self.assertEqual(len(previous_results), 2)
            self.assertLess(max(previous_results), min(next_sends))

    def test_timeout_cancels_remaining_list_but_keeps_listening(self):
        self.devices["COM2"].respond = False
        session = self.start()
        session.send(["sta", "eve"])
        self.wait_for(lambda: not session.busy)
        for device in self.devices.values():
            self.assertEqual(device.writes, [b"sta\r"])
        self.assertTrue(all(worker.connected.is_set() for worker in session.workers.values()))
        self.devices["COM2"].inject(b"late output\r# SGS")
        self.wait_for(lambda: any("late output" in e.get("text", "") for e in self.events(session)))
        self.devices["COM2"].respond = True
        session.send(["con"])
        self.wait_for(lambda: not session.busy)
        session.stop()
        results = [e["status"] for e in self.events(session) if e["kind"] == "batch_end"]
        self.assertEqual(results, ["ERROR", "OK"])

    def test_rejects_invalid_and_overlapping_batches_before_sending(self):
        self.devices["COM2"].respond = False
        session = self.start()
        for commands in ([], [""], ["sta", "bad\rcommand"], ["sta", "caf\u00e9"]):
            with self.subTest(commands=commands), self.assertRaises(ValueError):
                session.send(commands)
        self.assertTrue(all(not device.writes for device in self.devices.values()))
        session.send(["sta"])
        with self.assertRaisesRegex(ValueError, "current command list"):
            session.send(["con"])
        self.wait_for(lambda: not session.busy)

    def test_write_failure_resolves_batch_and_saves_error(self):
        self.devices["COM1"].fail_write = True
        session = self.start()
        session.send(["con", "sta"])
        self.wait_for(lambda: not session.busy)
        session.stop()
        events = self.events(session)
        self.assertTrue(any(e["kind"] == "error" and "disconnection" in e["message"] for e in events))
        self.assertFalse(any(e["kind"] == "tx" and e["command"] == "sta" for e in events))

    def test_open_failure_blocks_sending_to_partial_device_set(self):
        def opener(port, config):
            if port == "COM2":
                raise OSError("unavailable")
            return self.devices[port]

        session = self.start(opener=opener, ready=False)
        self.wait_for(lambda: not session.workers["COM2"].is_alive())
        with self.assertRaisesRegex(ValueError, "All configured devices"):
            session.send(["sta"])
        session.stop()
        self.assertFalse(self.devices["COM1"].writes)

    def test_stop_cancels_inflight_wait_and_report_can_be_rebuilt(self):
        self.raw["timeout"] = 30
        self.devices["COM2"].respond = False
        session = self.start()
        session.send(["sta", "eve"])
        self.wait_for(lambda: bool(self.devices["COM2"].writes))
        started = time.monotonic()
        session.stop()
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(self.events(session)[-1]["kind"], "session_end")
        self.assertTrue(create_report(session.log.path).exists())
        with self.assertRaisesRegex(ValueError, "stopped"):
            session.send(["con"])

    def test_report_recovers_complete_records_before_interrupted_write(self):
        path = self.root / "interrupted.jsonl"
        path.write_text(
            json.dumps({"id": 1, "timestamp": "2026-10-27T10:00:00-05:00", "kind": "rx", "port": "COM1", "text": "boot"})
            + '\n{"timestamp":', encoding="utf-8",
        )
        view = create_report(path).read_text(encoding="utf-8")
        self.assertIn("boot", view)
        self.assertIn("Incomplete log record", view)

    def test_disk_failure_stops_capture_and_surfaces_error(self):
        session = self.start()
        with patch.object(session.log._file, "write", side_effect=OSError("disk full")):
            session.log.record("test_event")
        self.assertTrue(session.stopping.is_set())
        self.assertIn("disk full", session.log.failure)
        with self.assertRaisesRegex(OSError, "disk full"):
            session.stop()
        self.assertTrue(session.closed)
        self.assertTrue(all(device.closed for device in self.devices.values()))
        self.assertTrue(create_report(session.log.path).exists())

    def test_report_groups_devices_and_joins_interleaved_partial_replies(self):
        at = "2026-10-29T10:00:00.123-05:00"
        events = [{"kind": "session_start", "ports": ["COM1", "COM2", "COM3"]}]

        def rx(port, data):
            return {"kind": "rx", "port": port, "text": data.decode("utf-8", errors="replace"),
                    "bytes_base64": base64.b64encode(data).decode("ascii")}

        events.extend([
            {"kind": "tx", "port": "COM1", "command": "sta"},
            rx("COM1", b"Temper"),
            rx("COM2", b"Other device\r"),
            rx("COM1", b"ature: 20\xc2"),
            {"kind": "command_result", "port": "COM1", "status": "OK", "command": "sta"},
            rx("COM1", b"\xb0C\r"),
            rx("COM2", b"\n\r\n# SGS"),
            rx("COM1", b"\nState: <ready>\r\n\r\n# SGS"),
        ])
        path = self.root / "split.jsonl"
        path.write_text("\n".join(json.dumps({"timestamp": at, **event}) for event in events), encoding="utf-8")
        original = path.read_bytes()
        view = create_report(path).read_text(encoding="utf-8")
        parsed = ReportTranscripts()
        parsed.feed(view)
        self.assertEqual(parsed.transcripts, {
            "device-0": f"[{at}] >>> sta\nTemperature: 20\u00b0C\nState: <ready>\n\n# SGS",
            "device-1": "Other device\n\n# SGS",
            "device-2": "",
        })
        self.assertIn(f'title="Received {at}"', view)
        self.assertIn("&lt;ready&gt;", view)
        self.assertNotIn("<ready>", view)
        self.assertEqual(path.read_bytes(), original)

    def test_report_joins_legacy_text_records_without_raw_bytes(self):
        path = self.root / "legacy.jsonl"
        events = [{"kind": "rx", "port": "COM1", "timestamp": "time", "text": text}
                  for text in ("State: Re", "ady\r", "\n# SGS")]
        path.write_text("\n".join(json.dumps(event) for event in events), encoding="utf-8")
        parsed = ReportTranscripts()
        parsed.feed(create_report(path).read_text(encoding="utf-8"))
        self.assertEqual(parsed.transcripts["device-0"], "State: Ready\n# SGS")


if __name__ == "__main__":
    unittest.main()
