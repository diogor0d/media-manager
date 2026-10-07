from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from test_api import running_client
from test_processor import _generate_fixtures, _tool

from media_manager.app import create_app
from media_manager.config import AuthMode, Settings

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def _ready(client: httpx.AsyncClient, path: str) -> dict:
    async with asyncio.timeout(30):
        while True:
            response = await client.get(path)
            assert response.status_code == 200
            job = response.json()
            assert job["state"] != "failed", job.get("error")
            if job["state"] == "ready":
                return job
            await asyncio.sleep(0.02)


async def test_real_results_remain_downloadable_after_restart(tmp_path: Path) -> None:
    ffmpeg, ffprobe = _tool("ffmpeg"), _tool("ffprobe")
    source = _generate_fixtures(tmp_path, ffmpeg)["image"].read_bytes()
    settings = Settings(
        work_dir=tmp_path / "work",
        auth_mode=AuthMode.DISABLED,
        public_base_url="http://testserver",
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        max_live_jobs=1,
        max_upload_bytes=1024 * 1024,
        max_output_bytes=1024 * 1024,
    )
    results = []
    async with running_client(create_app(settings)) as client:
        saved = await client.put("/v1/history/settings", json={"retention_days": 9})
        assert saved.status_code == 200
        for route, params in [("jobs", {"target": "image-webp"}), ("compressions", {})]:
            response = await client.post(
                f"/v1/{route}", params=params, content=source,
                headers={"Upload-Filename": "holiday.png", "Content-Type": "image/png"},
            )
            assert response.status_code == 202
            path = f"/v1/{route}/{response.json()['id']}"
            job = await _ready(client, path)
            results.append((path, job))
            remaining = datetime.fromisoformat(job["expires_at"]).timestamp()
            assert 9 * 86400 - 30 < remaining - datetime.now().timestamp() <= 9 * 86400
            assert not (settings.work_dir / job["id"] / "input").exists()
        history = (await client.get("/v1/history")).json()
        assert history["total"] == 2
        assert {entry["kind"] for entry in history["items"]} == {"conversion", "compression"}

    async with running_client(create_app(settings)) as client:
        assert (await client.get("/v1/history/settings")).json() == {"retention_days": 9}
        assert (await client.get("/v1/history")).json()["total"] == 2
        for path, original in results:
            job = (await client.get(path)).json()
            assert job == original
            content = await client.get(f"{path}/content")
            assert content.status_code == 200
            assert len(content.content) == job["output"]["bytes"]
            downloaded = tmp_path / job["output"]["filename"]
            downloaded.write_bytes(content.content)
            inspected = subprocess.run(
                [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(downloaded)],
                check=True, capture_output=True, text=True,
            )
            streams = json.loads(inspected.stdout)["streams"]
            assert streams[0]["codec_name"] == "webp"
            assert (streams[0]["width"], streams[0]["height"]) == (64, 48)
            assert (await client.delete(path)).status_code == 204
            assert not (settings.work_dir / job["id"]).exists()
        assert (await client.get("/v1/history")).json() == {"items": [], "total": 0}
