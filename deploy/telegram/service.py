"""Start the official API with bounded cache cleanup; never print private values."""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

CACHE_DIRS = frozenset(
    {
        "animations",
        "audio",
        "documents",
        "photos",
        "stickers",
        "thumbnails",
        "videos",
        "video_notes",
        "voice",
        "voice_notes",
        "temp",
    }
)
BOT_DIRECTORY = re.compile(r"[1-9][0-9]*(?:[:~][A-Za-z0-9_-]{1,80}(?:[:~]T)?)?")


def clean_cache(root: Path, ttl: int, now: float | None = None) -> int:
    """Only disposable media caches; database/session files are never traversed."""
    cutoff = (now if now is not None else time.time()) - ttl
    visited = 0
    removed = 0
    for bot_dir in root.iterdir():
        if (
            not BOT_DIRECTORY.fullmatch(bot_dir.name)
            or bot_dir.is_symlink()
            or not bot_dir.is_dir()
        ):
            continue
        for cache in bot_dir.iterdir():
            if cache.name not in CACHE_DIRS or cache.is_symlink() or not cache.is_dir():
                continue
            for directory, dirs, files in os.walk(cache, followlinks=False):
                dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
                for name in files:
                    visited += 1
                    if visited > 10_000:
                        return removed
                    path = Path(directory) / name
                    try:
                        if (
                            not path.is_symlink()
                            and path.is_file()
                            and path.stat().st_mtime < cutoff
                        ):
                            path.unlink()
                            removed += 1
                    except OSError:
                        continue
    return removed


def main() -> None:
    if sys.argv[1:] == ["health"]:
        # TCP-only probe: no bot-token-bearing URL reaches logs or the health result.
        with socket.create_connection(("127.0.0.1", 8081), timeout=2):
            return
    if not os.getenv("TELEGRAM_API_ID") or not os.getenv("TELEGRAM_API_HASH"):
        raise SystemExit("Configure private Telegram application credentials before starting")
    ttl = int(os.getenv("TELEGRAM_CACHE_TTL_SECONDS", "604800"))
    if ttl < 3600:
        raise SystemExit("Cache retention must be at least one hour")
    clean_cache(Path("/telegram"), ttl)
    # API token is provided only in private HTTP traffic, never as a command-line argument.
    child = subprocess.Popen(
        [
            "telegram-bot-api",
            "--local",
            "--http-port=8081",
            "--dir=/telegram",
            "--temp-dir=/tmp",
            "--verbosity=0",
            "--log-max-file-size=10485760",
        ],
    )

    def forward(signum, _frame) -> None:
        child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    next_cleanup = time.monotonic() + 3600
    while child.poll() is None:
        try:
            child.wait(timeout=60)
        except subprocess.TimeoutExpired:
            if time.monotonic() >= next_cleanup:
                clean_cache(Path("/telegram"), ttl)
                next_cleanup = time.monotonic() + 3600
    raise SystemExit(child.returncode)


if __name__ == "__main__":
    main()
