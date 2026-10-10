"""Ensure Bot API cache expiry cannot remove sessions or escape its volume."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "telegram_service", Path(__file__).parents[1] / "deploy/telegram/service.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_cache_cleanup_preserves_sessions_new_files_and_symlink_targets(tmp_path: Path) -> None:
    root = tmp_path / "telegram"
    media = root / "1234:synthetic-token" / "documents"
    media.mkdir(parents=True)
    old = media / "old.mp4"
    fresh = media / "fresh.mp4"
    database = root / "1234:synthetic-token" / "db.sqlite"
    outside = tmp_path / "outside.mp4"
    for path in (old, fresh, database, outside):
        path.write_bytes(b"private-data")
    os.utime(old, (0, 0))
    os.utime(database, (0, 0))
    os.utime(outside, (0, 0))
    (media / "linked.mp4").symlink_to(outside)
    (root / "5678").symlink_to(tmp_path, target_is_directory=True)
    assert module.clean_cache(root, 3600) == 1
    assert not old.exists()
    assert fresh.is_file() and database.is_file() and outside.is_file()
    assert (media / "linked.mp4").is_symlink()
