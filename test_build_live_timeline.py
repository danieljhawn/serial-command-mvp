"""Timeline conversion checks without serial hardware or a desktop."""

import base64
import json
import tempfile
import unittest
from pathlib import Path

from build_live_timeline import create_timeline


class LiveTimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write_session(self, name="live_serial_sample.jsonl", *, interrupted=False):
        events = [{"kind": "session_start", "ports": ["COM1", "COM2"], "test_suite": "Test suite"}]
        for batch, command, replies in (
            (1, "con", [b"IME", b"I: 12345\r\nModel #: SGS\r\n# SGS"]),
            (2, "eve", [b"1: First event\r\n# SGS"]),
            (3, "sta", [b"State: old\r\n* Sleeping\r\n# SGS"]),
            (4, "eve", [b"2: New event\r\n1: First event\r\n# SGS"]),
        ):
            events.append({"kind": "batch_start", "batch_id": batch, "commands": [command]})
            for port in ("COM1", "COM2"):
                common = {"batch_id": batch, "port": port, "step": 1}
                events.append({**common, "kind": "tx", "command": command, "text": command})
                for reply in replies:
                    events.append({**common, "kind": "rx", "text": reply.decode(),
                                   "bytes_base64": base64.b64encode(reply).decode()})
                events.append({**common, "kind": "command_result", "command": command,
                               "status": "TIMEOUT" if batch == 3 and port == "COM2" else "OK"})
            events.append({"kind": "batch_end", "batch_id": batch, "status": "ERROR" if batch == 3 else "OK"})
        if not interrupted:
            events.append({"kind": "session_end"})
        path = self.root / name
        path.write_text("\n".join(json.dumps({"timestamp": f"2026-11-09T10:00:{i:02d}-06:00", **e})
                                  for i, e in enumerate(events)) + ('\n{"kind":' if interrupted else ''), encoding="utf-8")
        return path

    def read_index(self, report):
        return json.loads(report.with_name(report.name.replace("_report.html", "_index.json")).read_text())

    def test_batches_parse_split_responses_and_keep_eve_baseline_across_other_commands(self):
        path = self.write_session()
        original = path.read_bytes()
        report = create_timeline(path)
        index = self.read_index(report)
        self.assertEqual(len(index["logs"]), 5)
        self.assertEqual(index["logs"][1]["devices"][0]["imei"], "12345")
        self.assertEqual(index["logs"][3]["devices"][0]["state"], "Sleeping")
        self.assertEqual(index["logs"][3]["error_count"], 1)
        delta = index["logs"][4]["devices"][0]
        self.assertEqual(delta["imei"], "12345")
        self.assertTrue(delta["previous_event_log_seen"])
        self.assertEqual(delta["new_event_log_entries"], [{"id": "2", "text": "New event"}])
        self.assertTrue((report.parent / index["logs"][0]["viewer_file"]).exists())
        self.assertEqual(path.read_bytes(), original)

    def test_folder_csv_custom_output_and_interrupted_capture(self):
        self.write_session(interrupted=True)
        self.write_session("live_serial_second.jsonl")
        csv_path = self.root / "tests.csv"
        csv_path.write_text("Timestamp,Fault test,Expected Result,12345,Notes\n11/09/2026 10:00:00,Trip,Trip,PASS,Example\n")
        report = create_timeline(self.root, csv_path=csv_path, report=self.root / "output" / "timeline_report.html")
        index = self.read_index(report)
        self.assertEqual(len(index["tests"]), 1)
        self.assertEqual(len(index["logs"]), 10)
        self.assertEqual(len({e["id"] for e in index["events"]}), 11)
        self.assertEqual(len(index["warnings"]), 2)
        self.assertEqual(index["logs"][0]["status"], "INCOMPLETE")
        self.assertTrue(index["logs"][0]["viewer_file"].startswith("../"))
        self.assertIn("previous_test_id", index["logs"][1])

    def test_empty_folder_fails_clearly(self):
        with self.assertRaisesRegex(ValueError, "No live_serial"):
            create_timeline(self.root)

    def test_serial_metadata_cannot_close_embedded_script(self):
        path = self.write_session()
        path.write_text(path.read_text().replace("Test suite", "</script><script>alert(1)</script>"))
        report = create_timeline(path)
        html = report.read_text(encoding="utf-8")
        self.assertNotIn("</script><script>alert(1)</script>", html)
        self.assertIn(r"\u003c/script>", html)


if __name__ == "__main__":
    unittest.main()
