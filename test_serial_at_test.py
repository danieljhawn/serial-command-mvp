"""Collector regression checks using simulated ports, never real hardware."""

import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import serial

import serial_at_test as collector


class FakeSerial:
    def __init__(self, *, fail_reset=False, fail_write=False, response=b"State: Ready\r# SGS"):
        self.fail_reset = fail_reset
        self.fail_write = fail_write
        self.response = response
        self.pending = b""
        self.writes = []
        self.closed = False

    def reset_input_buffer(self):
        if self.fail_reset:
            raise serial.SerialException("buffer reset failed")
        self.pending = b""

    def reset_output_buffer(self):
        pass

    def write(self, data):
        if self.fail_write:
            raise serial.SerialException("write failed")
        self.writes.append(data)
        self.pending = self.response
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        return len(self.pending)

    def read(self, count):
        result, self.pending = self.pending[:count], self.pending[count:]
        return result

    def close(self):
        self.closed = True


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = {"ports": ["COM1", "COM2"], "commands": ["con", "sta"], "timeout": 0.1}
        self.config_path = self.root / "config.json"
        self.config_path.write_text(json.dumps(self.raw), encoding="utf-8")
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.enterContext(contextlib.redirect_stderr(self.output))

    def run_main(self, flags, devices):
        argv = ["serial_at_test.py", "--config", str(self.config_path), *flags]
        with patch("sys.argv", argv), patch.object(
            collector, "open_serial", side_effect=lambda port, config: devices[port]
        ), patch("builtins.input", side_effect=AssertionError("Unexpected sequence prompt")):
            result = collector.main()
        logs = list((self.root / "logs").glob("serial_log_*.json"))
        self.assertEqual(len(logs), 1)
        return result, logs[0], json.loads(logs[0].read_text(encoding="utf-8"))

    def test_sta_collects_only_status_and_saves_compatible_log(self):
        self.raw["test_sequences"] = {"Example": "1s 10a"}
        self.config_path.write_text(json.dumps(self.raw), encoding="utf-8")
        devices = {port: FakeSerial() for port in self.raw["ports"]}
        result, path, log = self.run_main(["--sta", "--no-viewer"], devices)
        self.assertEqual(result, 0)
        self.assertEqual(log["run_type"], "status_check")
        self.assertEqual(log["status_check_command"], "sta")
        self.assertEqual(log["selected_sequence"], {"name": "", "value": ""})
        self.assertFalse(collector.make_report_path(path).exists())
        for port, device in devices.items():
            self.assertEqual(device.writes, [b"sta\r"])
            self.assertTrue(device.closed)
            record = log["devices"][port]["commands"][0]
            self.assertEqual(record["status"], "OK")
            self.assertEqual(record["response_fields"]["State"], "Ready")
            self.assertEqual(record["timestamp"], record["completed_at"])
            self.assertLessEqual(record["sent_at"], record["completed_at"])

    def test_status_shortcut_keeps_optional_hooks(self):
        self.raw.update(pre_commands=[""], post_commands=["eve"])
        config = collector.load_config(self.raw, status_check_command="sta")
        self.assertEqual(collector.command_plan(config), [("pre", ""), ("test", "sta"), ("post", "eve")])
        self.assertEqual(collector.parse_args(["--status-check", "con"]).status_check, "con")

    def test_conflicting_modes_and_blank_status_command_are_rejected(self):
        for flags in (["--sta", "--sequence", "Example"], ["--sta", "--status-check", "sta"],
                      ["--sta", "--list-sequences"], ["--status-check", ""]):
            with self.subTest(flags=flags), self.assertRaises(SystemExit) as raised:
                collector.parse_args(flags)
            self.assertEqual(raised.exception.code, 2)

    def test_invalid_settings_fail_before_opening_ports(self):
        cases = [
            {"ports": ["COM1", "com1"]}, {"ports": [" COM1"]}, {"ports": []},
            {"timeout": -1}, {"timeout": float("nan")}, {"timeout": float("inf")},
            {"timeout": None}, {"baud": True}, {"baud": 4096.5},
            {"prompt_prefix": ""}, {"commands": ["st\u00e1"]}, {"commands": ["sta\r"]},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                collector.load_config({**self.raw, **changes})
        self.config_path.write_text('{"ports": ["COM1"], "commands": ["sta"], "timeout": -1}')
        with patch("sys.argv", ["serial_at_test.py", "--config", str(self.config_path)]), \
             patch.object(collector, "open_serial") as opener, patch("builtins.input") as prompt:
            self.assertEqual(collector.main(), 1)
            opener.assert_not_called()
            prompt.assert_not_called()

    def test_config_requires_object_and_accepts_bom(self):
        self.config_path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "JSON object"):
            collector.load_raw_config(self.config_path)
        self.config_path.write_text(json.dumps(self.raw), encoding="utf-8-sig")
        self.assertEqual(collector.load_raw_config(self.config_path), self.raw)

    def test_reset_failure_releases_waiting_worker(self):
        config = collector.load_config(self.raw)
        logger = collector.ThreadSafeJsonLogger(self.root / "log.json", config, self.config_path)
        barrier = threading.Barrier(2)
        failures = []
        lock = threading.Lock()
        devices = {"COM1": FakeSerial(), "COM2": FakeSerial(fail_reset=True)}
        with patch.object(collector, "open_serial", side_effect=lambda port, config: devices[port]):
            workers = [threading.Thread(target=collector.run_port, args=(port, config, logger, barrier, failures, lock),
                                        daemon=True) for port in config.ports]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=2)
            self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertTrue(barrier.broken)
        self.assertEqual(len(failures), 2)
        for port, device in devices.items():
            self.assertTrue(device.closed)
            self.assertEqual(device.writes, [])
            self.assertEqual(logger.data["devices"][port]["status"], "ERROR")

    def test_write_failure_stops_one_port_but_other_port_continues(self):
        devices = {"COM1": FakeSerial(fail_write=True), "COM2": FakeSerial()}
        result, _, log = self.run_main(["--no-viewer"], devices)
        self.assertEqual(result, 1)
        self.assertEqual(log["devices"]["COM1"]["commands"][0]["status"], "ERROR")
        self.assertTrue(log["devices"]["COM1"]["errors"])
        self.assertEqual(log["devices"]["COM2"]["status"], "OK")
        self.assertEqual(devices["COM2"].writes, [b"con\r", b"sta\r"])

    def test_open_failure_is_logged_and_cancels_shared_start(self):
        argv = ["serial_at_test.py", "--config", str(self.config_path), "--sta", "--no-viewer"]
        with patch("sys.argv", argv), patch.object(
            collector, "open_serial", side_effect=serial.SerialException("port unavailable")
        ):
            self.assertEqual(collector.main(), 1)
        path = next((self.root / "logs").glob("serial_log_*.json"))
        log = json.loads(path.read_text(encoding="utf-8"))
        for device in log["devices"].values():
            self.assertEqual(device["status"], "ERROR")
            self.assertEqual(device["commands"][0]["phase"], "open")

    def test_missing_peer_times_out_at_startup(self):
        config = collector.load_config(self.raw)
        logger = collector.ThreadSafeJsonLogger(self.root / "log.json", config, self.config_path)
        barrier = threading.Barrier(2)
        device = FakeSerial()
        failures = []
        with patch.object(collector, "open_serial", return_value=device):
            collector.run_port("COM1", config, logger, barrier, failures, threading.Lock())
        self.assertTrue(barrier.broken)
        self.assertTrue(device.closed)
        self.assertEqual(device.writes, [])
        self.assertTrue(failures)

    def test_save_failure_returns_error_without_opening_viewer(self):
        devices = {port: FakeSerial() for port in self.raw["ports"]}
        argv = ["serial_at_test.py", "--config", str(self.config_path), "--sta"]
        with patch("sys.argv", argv), patch.object(
            collector, "open_serial", side_effect=lambda port, config: devices[port]
        ), patch.object(collector.ThreadSafeJsonLogger, "save", side_effect=OSError("disk full")), \
             patch.object(collector, "open_viewer_report") as viewer:
            self.assertEqual(collector.main(), 1)
            viewer.assert_not_called()

    def test_viewer_failure_keeps_saved_log_and_successful_collection(self):
        devices = {port: FakeSerial() for port in self.raw["ports"]}
        with patch.object(collector, "open_viewer_report", side_effect=OSError("viewer unavailable")):
            result, _, log = self.run_main(["--sta"], devices)
        self.assertEqual(result, 0)
        self.assertIsNotNone(log["finished_at"])
        self.assertIn("WARNING: Could not open viewer", self.output.getvalue())

    def test_timeout_keeps_partial_response_and_skips_remaining_commands(self):
        devices = {"COM1": FakeSerial(response=b"State: Busy"), "COM2": FakeSerial()}
        result, _, log = self.run_main(["--no-viewer"], devices)
        self.assertEqual(result, 1)
        records = log["devices"]["COM1"]["commands"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "TIMEOUT")
        self.assertEqual(records[0]["response_text"], ["State: Busy"])

    def test_sequence_is_metadata_and_viewer_is_generated(self):
        self.raw["test_sequences"] = {"Example": "1s 10a"}
        self.config_path.write_text(json.dumps(self.raw), encoding="utf-8")
        devices = {port: FakeSerial() for port in self.raw["ports"]}
        with patch.object(collector.webbrowser, "open", return_value=True) as browser:
            result, path, log = self.run_main(["--sequence", "Example"], devices)
        self.assertEqual(result, 0)
        self.assertEqual(log["selected_sequence"], {"name": "Example", "value": "1s 10a"})
        self.assertTrue(collector.make_report_path(path).exists())
        browser.assert_called_once()
        self.assertEqual(devices["COM1"].writes, [b"con\r", b"sta\r"])

    def test_list_sequences_never_opens_ports_or_creates_logs(self):
        with patch("sys.argv", ["serial_at_test.py", "--config", str(self.config_path), "--list-sequences"]), \
             patch.object(collector, "open_serial") as opener:
            self.assertEqual(collector.main(), 0)
            opener.assert_not_called()
        self.assertFalse((self.root / "logs").exists())


if __name__ == "__main__":
    unittest.main()
