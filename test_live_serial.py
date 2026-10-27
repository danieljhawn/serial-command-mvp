"""Live capture checks with thread-safe fake devices; no hardware is opened."""

import base64
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from live_serial import LiveSession, create_report
from serial_at_test import load_config


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


if __name__ == "__main__":
    unittest.main()
