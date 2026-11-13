"""Shared date and test-suite folders for captured serial logs."""

import re
from datetime import datetime
from pathlib import Path


def suite_folder_name(test_suite: str) -> str:
    """Keep suite labels readable while making one valid Windows folder name."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', test_suite).strip().rstrip('. ')
    if not name:
        return "Unnamed suite"
    if name.split('.')[0].upper() in {
        'CON', 'PRN', 'AUX', 'NUL',
        *(f'COM{i}' for i in range(1, 10)),
        *(f'LPT{i}' for i in range(1, 10)),
    }:
        name = '_' + name
    return name


def log_folder(log_dir: Path, config_path: Path, test_suite: str, started_at: datetime) -> Path:
    base = log_dir if log_dir.is_absolute() else config_path.parent / log_dir
    folder = base / started_at.strftime('%Y-%m-%d') / suite_folder_name(test_suite)
    folder.mkdir(parents=True, exist_ok=True)
    return folder
