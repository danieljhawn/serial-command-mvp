"""Log destinations and report links with dated test-suite folders."""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import live_serial
import serial_at_test
import serial_configure
from build_live_timeline import create_timeline
from log_paths import log_folder, suite_folder_name


class LogPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'config.json'
        self.started_at = datetime.fromisoformat('2026-11-13T23:59:59.123456-06:00')

    def test_folder_names_remain_one_valid_windows_component(self):
        for suite, expected in (
            ('Regression - Overhead', 'Regression - Overhead'),
            (' ../Suite: A/B\\C? ', '.._Suite_ A_B_C_'),
            ('..', 'Unnamed suite'), (' ', 'Unnamed suite'),
            ('CON', '_CON'), ('nul.txt', '_nul.txt'),
        ):
            with self.subTest(suite=suite):
                self.assertEqual(suite_folder_name(suite), expected)

    def test_relative_and_absolute_log_roots_and_suite_separation(self):
        for base in (Path('logs'), self.root / 'custom logs'):
            root = base if base.is_absolute() else self.root / base
            for suite in ('Overhead', 'Wireless'):
                folder = log_folder(base, self.config_path, suite, self.started_at)
                self.assertEqual(folder, root / '2026-11-13' / suite)
                self.assertTrue(folder.is_dir())

    def test_collectors_use_the_same_date_and_suite_as_their_filenames(self):
        config = serial_at_test.load_config({'ports': ['COM1'], 'commands': ['sta'], 'test_suite': 'Overhead'})
        self.config_path.write_text(json.dumps({
            'ports': ['COM1'], 'apply_commands': ['fast'],
            'verify_commands': ['sta'], 'test_suite': 'Overhead',
        }), encoding='utf-8')
        configure = serial_configure.load_config(self.config_path)
        for module, settings, prefix in (
            (serial_at_test, config, 'serial_log'),
            (serial_configure, configure, 'configure_log'),
        ):
            with self.subTest(module=module.__name__), patch.object(module, 'datetime') as clock:
                clock.now.return_value.astimezone.return_value = self.started_at
                path = module.make_log_path(settings, self.config_path)
                self.assertEqual(path, self.root / 'logs' / '2026-11-13' / 'Overhead' /
                                 f'{prefix}_20261113_235959.json')
        raw = json.loads(self.config_path.read_text(encoding='utf-8'))
        del raw['test_suite']
        self.config_path.write_text(json.dumps(raw), encoding='utf-8')
        self.assertEqual(serial_configure.load_config(self.config_path).test_suite, 'Device Configuration')

    def test_live_report_and_timeline_stay_beside_nested_capture(self):
        config = serial_at_test.load_config({'ports': ['COM1'], 'commands': ['sta'], 'test_suite': 'Overhead'})
        with patch.object(live_serial, 'datetime') as clock:
            clock.now.return_value.astimezone.return_value = self.started_at
            session = live_serial.LiveSession(config, self.config_path)
        report = session.stop()
        folder = self.root / 'logs' / '2026-11-13' / 'Overhead'
        self.assertEqual(session.log.path, folder / 'live_serial_20261113_235959_123456.jsonl')
        self.assertEqual(report.parent, folder)
        self.assertTrue(report.exists())
        timeline = create_timeline(self.root / 'logs')
        index = json.loads(timeline.with_name(timeline.name.replace('_report.html', '_index.json')).read_text())
        self.assertTrue((timeline.parent / index['logs'][0]['viewer_file']).exists())


if __name__ == '__main__':
    unittest.main()
