#!/usr/bin/env python3
"""Run disposable, real-MeTube workflow acceptance checks with Docker Compose."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "compose.validation.yaml"
API = os.getenv("WORKFLOW_API_URL", "http://api:8080").rstrip("/")
FIXTURE = os.getenv("WORKFLOW_FIXTURE_URL", "http://fixture:8082").rstrip("/")
SIDECAR = os.getenv("WORKFLOW_SIDECAR_URL", "http://sidecar:8080").rstrip("/")
SIDECAR_KEY = os.getenv("WORKFLOW_SIDECAR_KEY", "synthetic-workflow-validation-key")
OWNER = "local-development"
STATE = Path("/validation-state/workflow-validation.json")
TIMEOUT = 180


def request(method: str, path: str, body: dict | None = None, *, raw: bool = False):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = Request(f"{API}{path}", data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=20) as response:
            payload = response.read()
            return payload if raw else json.loads(payload) if payload else None
    except HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", "replace")
        raise RuntimeError(f"API {method} {path} returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"API {method} {path} failed: {exc.reason}") from exc


def sidecar_job(job_id: str) -> dict:
    req = Request(
        f"{SIDECAR}/jobs/{job_id}",
        headers={"X-Sidecar-Api-Key": SIDECAR_KEY},
    )
    with urlopen(req, timeout=10) as response:
        return json.loads(response.read())


def wait_for_download(download_id: str, wanted: set[str], timeout: int = TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = request("GET", f"/v1/downloads/{download_id}")
        if view["state"] in wanted:
            return view
        if view["state"] == "failed":
            try:
                upstream = sidecar_job(download_id)
                upstream_error = upstream.get("error")
            except Exception as exc:
                upstream = {}
                upstream_error = f"sidecar status unavailable ({type(exc).__name__})"
            if isinstance(upstream_error, str):
                upstream_error = re.sub(r"https?://[^\s\"']+", "<URL>", upstream_error)
            raise RuntimeError(
                f"Download {download_id} failed: {view.get('error')}; "
                f"sidecar status={upstream.get('status', 'unknown')}; "
                f"sidecar error={upstream_error}"
            )
        time.sleep(1)
    raise RuntimeError(f"Download {download_id} did not reach {sorted(wanted)}")


def wait_for_job(job_id: str, *, compression: bool = False, timeout: int = TIMEOUT) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        endpoint = "compressions" if compression else "jobs"
        view = request("GET", f"/v1/{endpoint}/{job_id}")
        if view["state"] in {"ready", "failed"}:
            if view["state"] == "failed":
                raise RuntimeError(f"Processing job {job_id} failed: {view.get('error')}")
            return view
        time.sleep(0.5)
    raise RuntimeError(f"Processing job {job_id} did not finish")


def create_download(workflow: str, *, auto_process: bool, **extra) -> dict:
    state = load_state() if STATE.exists() else {}
    page_number = int(state.get("fixture_page_sequence", 0)) + 1
    page = Path("/fixtures") / f"page-{page_number:02}.html"
    if not page.is_file():
        raise RuntimeError("Synthetic fixture page pool was exhausted")
    state["fixture_page_sequence"] = page_number
    save_state(state)
    body = {
        "url": f"{FIXTURE}/{page.name}",
        "workflow": workflow,
        "auto_process": auto_process,
        "quality": "best",
        "format": "ios",
        "download_type": "video",
        "codec": "h264",
        **extra,
    }
    return request("POST", "/v1/downloads", body)


def download_content(download_id: str) -> bytes:
    return request("GET", f"/v1/downloads/{download_id}/content?preview=true", raw=True)


def child_content(job_id: str, *, compression: bool = False) -> bytes:
    endpoint = "compressions" if compression else "jobs"
    return request("GET", f"/v1/{endpoint}/{job_id}/content?preview=true", raw=True)


def file_info(data: bytes, expected_format: str | None = None) -> dict:
    path = Path("/tmp/workflow-output")
    path.write_bytes(data)
    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration,format_name",
                "-show_entries",
                "stream=codec_type,codec_name,width,height",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        info = json.loads(probe.stdout)
    finally:
        path.unlink(missing_ok=True)
    streams = info.get("streams", [])
    if not streams:
        raise RuntimeError("ffprobe found no streams in pipeline output")
    if expected_format and expected_format not in info.get("format", {}).get("format_name", ""):
        raise RuntimeError(f"Expected {expected_format} container; got {info.get('format')}")
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "probe": info}


def check_output_metadata(
    job: dict, result: dict, *, no_audio: bool = False, max_height: int | None = None
) -> None:
    output = job.get("output")
    if not isinstance(output, dict) or output.get("bytes") != result["bytes"]:
        raise RuntimeError("Processing metadata byte count does not match downloaded output")
    streams = result["probe"].get("streams", [])
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if max_height is not None and (
        video is None or video.get("height", max_height + 1) > max_height
    ):
        raise RuntimeError(f"Final output exceeded the {max_height}p height cap: {video}")
    if no_audio and audio:
        raise RuntimeError(f"Final output retained audio despite the requested drop mode: {audio}")
    if output.get("width") != (video.get("width") if video else None):
        raise RuntimeError("Final output width metadata disagrees with ffprobe")
    if output.get("height") != (video.get("height") if video else None):
        raise RuntimeError("Final output height metadata disagrees with ffprobe")


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def load_state() -> dict:
    if not STATE.exists():
        raise RuntimeError("Validation state is missing; run the full validator")
    return json.loads(STATE.read_text(encoding="utf-8"))


def verify_history(parent_id: str, child_id: str | None = None) -> None:
    result = request("GET", "/v1/history?limit=100&offset=0")
    entries = result["items"]
    ids = {entry.get("job", {}).get("id") for entry in entries}
    ids.update(entry.get("id") for entry in entries)
    if parent_id not in ids or (child_id and child_id not in ids):
        raise RuntimeError(f"History is missing expected parent/child IDs: {parent_id}, {child_id}")


def report(label: str, data: dict) -> None:
    print(json.dumps({"check": label, **data}, sort_keys=True))


def phase_download_review() -> None:
    created = create_download("convert", auto_process=False)
    ready = wait_for_download(created["id"], {"awaiting_review"})
    content = download_content(ready["id"])
    original = file_info(content, "mp4")
    remote = sidecar_job(ready["id"])
    if remote.get("status") != "complete":
        raise RuntimeError(f"MeTube sidecar job is not complete: {remote.get('status')}")
    verify_history(ready["id"])
    state = load_state()
    state.update({"review_id": ready["id"], "review_sha256": original["sha256"]})
    save_state(state)
    report(
        "download-ready-review-persisted",
        {
            "download_id": ready["id"],
            "state": ready["state"],
            "source": original,
            "sidecar_state": remote.get("status"),
        },
    )


def phase_resume_review() -> None:
    state = load_state()
    parent_id = state["review_id"]
    before = request("GET", f"/v1/downloads/{parent_id}")
    if before["state"] != "awaiting_review":
        raise RuntimeError(f"Review was not restored after API restart: {before['state']}")
    source = file_info(download_content(parent_id), "mp4")
    if source["sha256"] != state["review_sha256"]:
        raise RuntimeError("Reviewed download changed across API restart")
    conversion = {
        "target": "video-webm",
        "quality": "balanced",
        "resolution": "source",
        "audio": "keep",
    }
    child = request(
        "POST",
        f"/v1/downloads/{parent_id}/process",
        {
            "conversion_options": conversion,
            "auto_start": False,
        },
    )
    estimate = request(
        "POST", f"/v1/jobs/{child['id']}/estimate", {"conversion_options": conversion}
    )
    deadline = time.monotonic() + 30
    while estimate.get("status") == "pending" and time.monotonic() < deadline:
        time.sleep(0.5)
        estimate = request("GET", f"/v1/jobs/{child['id']}/estimate")
    if estimate.get("status") not in {"ready", "fallback"}:
        raise RuntimeError(f"Expected a usable estimate before explicit start: {estimate}")
    request("POST", f"/v1/jobs/{child['id']}/start", {"conversion_options": conversion})
    finished = wait_for_job(child["id"])
    output = file_info(child_content(child["id"]), "matroska,webm")
    verify_history(parent_id, child["id"])
    state["review_child"] = child["id"]
    save_state(state)
    report(
        "review-estimate-explicit-convert-after-api-restart",
        {
            "parent_id": parent_id,
            "child_id": child["id"],
            "estimate_status": estimate["status"],
            "estimate_method": estimate.get("method"),
            "job_state": finished["state"],
            "output": output,
        },
    )


def run_automatic(label: str, workflow: str, options: dict, expected_format: str) -> dict:
    created = create_download(workflow, auto_process=True, **options)
    parent = wait_for_download(created["id"], {"ready"})
    if not parent.get("child_id"):
        raise RuntimeError(f"{workflow} did not create a child processing job")
    compression = workflow in {"compress", "convert_compress"}
    child = wait_for_job(parent["child_id"], compression=compression)
    output = file_info(child_content(child["id"], compression=compression), expected_format)
    check_output_metadata(child, output)
    verify_history(parent["id"], child["id"])
    data = {
        "parent_id": parent["id"],
        "child_id": child["id"],
        "parent_state": parent["state"],
        "job_state": child["state"],
        "output": output,
    }
    report(label, data)
    return data


def phase_auto_pipelines() -> None:
    compressed = run_automatic(
        "autocompress",
        "compress",
        {
            "compression_options": {
                "target_bytes": 500_000,
                "target": "video-mp4",
                "resolution": "source",
                "audio": "keep",
                "quality_percent": 48,
            },
        },
        "mp4",
    )
    converted = run_automatic(
        "autoconvert-compress",
        "convert_compress",
        {
            "conversion_options": {
                "target": "video-webm",
                "quality": "balanced",
                "resolution": "source",
                "audio": "keep",
            },
            "compression_options": {
                "target_bytes": 700_000,
                "target": "video-webm",
                "resolution": "480p",
                "audio": "drop",
                "quality_percent": 76,
            },
        },
        "matroska,webm",
    )
    combo_job = request("GET", f"/v1/compressions/{converted['child_id']}")
    check_output_metadata(combo_job, converted["output"], no_audio=True, max_height=480)
    state = load_state()
    state["auto_compress"] = compressed
    state["auto_convert_compress"] = converted
    save_state(state)


def phase_failure_and_copy_delete() -> None:
    # A thumbnail download followed by a video conversion is an intentional
    # processing-type mismatch; the completed MeTube original must remain usable.
    created = create_download(
        "convert",
        auto_process=True,
        download_type="thumbnail",
        format="jpg",
        codec="auto",
        conversion_options={
            "target": "video-webm",
            "quality": "balanced",
            "resolution": "source",
            "audio": "keep",
        },
    )
    parent = wait_for_download(created["id"], {"awaiting_review"})
    if not parent.get("error") or parent["error"].get("code") != "PROCESSING_FAILED":
        raise RuntimeError(f"Expected child-processing failure to preserve source: {parent}")
    original = download_content(parent["id"])
    original_info = file_info(original, "jpeg_pipe")
    original_path = (
        Path("/downloads/sidecar") / parent["id"] / (parent.get("source_filename") or "")
    )
    if not original_path.is_file():
        candidates = list((Path("/downloads/sidecar") / parent["id"]).glob("*"))
        candidates = [
            path
            for path in candidates
            if path.is_file() and not path.name.endswith((".part", ".ytdl"))
        ]
        if len(candidates) != 1:
            raise RuntimeError(
                "Unable to identify the original MeTube file before deleting its copy"
            )
        original_path = candidates[0]
    source_hash = hashlib.sha256(original_path.read_bytes()).hexdigest()
    if source_hash != original_info["sha256"]:
        raise RuntimeError("Sidecar copy does not match MeTube's retained original")
    request("DELETE", f"/v1/downloads/{parent['id']}")
    if original_path.is_symlink() or not original_path.is_file():
        raise RuntimeError("Deleting the sidecar copy removed or replaced MeTube's original")
    if hashlib.sha256(original_path.read_bytes()).hexdigest() != source_hash:
        raise RuntimeError("MeTube's original changed when deleting the independent sidecar copy")
    report(
        "failed-child-source-retention-and-copy-delete",
        {
            "parent_id": parent["id"],
            "failure": parent["error"]["code"],
            "original": original_info,
            "metube_original_after_copy_delete": "present and hash unchanged",
        },
    )


def phase_download_only() -> None:
    created = create_download("download", auto_process=False)
    ready = wait_for_download(created["id"], {"ready"})
    data = download_content(ready["id"])
    source = file_info(data, "mp4")
    verify_history(ready["id"])
    report("download-only", {"download_id": ready["id"], "state": ready["state"], "output": source})


def phase_ttl_cleanup() -> None:
    created = create_download("download", auto_process=False)
    ready = wait_for_download(created["id"], {"ready"})
    source_file = Path("/downloads/sidecar") / ready["id"] / (ready.get("source_filename") or "")
    if not source_file.is_file() or source_file.is_symlink():
        raise RuntimeError("MeTube source was unavailable before the TTL cleanup check")
    source_hash = hashlib.sha256(source_file.read_bytes()).hexdigest()
    time.sleep(7)
    for endpoint in (
        f"/v1/downloads/{ready['id']}",
        f"/v1/downloads/{ready['id']}/content?preview=true",
    ):
        try:
            request("GET", endpoint, raw=endpoint.endswith("preview=true"))
        except RuntimeError as exc:
            if "HTTP 404" not in str(exc):
                raise
        else:
            raise RuntimeError(
                "Expired download metadata or content remained available after the short test TTL"
            )
    if source_file.is_symlink() or not source_file.is_file():
        raise RuntimeError("TTL cleanup removed or replaced MeTube's original file")
    if hashlib.sha256(source_file.read_bytes()).hexdigest() != source_hash:
        raise RuntimeError("TTL cleanup changed MeTube's original file")
    history = request("GET", "/v1/history?limit=100&offset=0")
    ids = {entry.get("job", {}).get("id") for entry in history.get("items", [])}
    if ready["id"] in ids:
        raise RuntimeError("Expired download remained in user history")
    report(
        "short-ttl-copy-cleanup",
        {
            "download_id": ready["id"],
            "metadata_and_content": "404",
            "api_copy": "no longer retrievable; entry evicted from history",
            "metube_original": "present and hash unchanged",
        },
    )


def run_client_phase(name: str) -> None:
    actions = {
        "download-review": phase_download_review,
        "resume-review": phase_resume_review,
        "auto-pipelines": phase_auto_pipelines,
        "failure-copy-delete": phase_failure_and_copy_delete,
        "download-only": phase_download_only,
        "ttl-cleanup": phase_ttl_cleanup,
    }
    if name not in actions:
        raise RuntimeError(f"Unknown checker phase: {name}")
    actions[name]()


def compose(
    *args: str, env: dict[str, str] | None = None, capture: bool = False
) -> subprocess.CompletedProcess:
    command = [
        "docker",
        "--context",
        "desktop-linux",
        "compose",
        "--project-name",
        PROJECT,
        "-f",
        str(COMPOSE_FILE),
        *args,
    ]
    merged = os.environ.copy()
    merged.update(COMPOSE_ENV)
    if env:
        merged.update(env)
    return subprocess.run(
        command,
        cwd=ROOT,
        env=merged,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )


def run_stack() -> None:
    print(f"Starting isolated Compose project {PROJECT}", flush=True)
    compose("config", "--quiet")
    subprocess.run(
        ["docker", "--context", "desktop-linux", "build", "--pull", "--tag", MAIN_IMAGE, "."],
        cwd=ROOT,
        check=True,
    )
    compose("build", "sidecar")
    compose("up", "-d", "fixture", "metube", "sidecar", "api")
    compose("run", "--rm", "-T", "check", "download-only")
    compose("run", "--rm", "-T", "check", "download-review")
    compose("restart", "api")
    compose("run", "--rm", "-T", "check", "resume-review")
    compose("run", "--rm", "-T", "check", "auto-pipelines")
    compose("run", "--rm", "-T", "check", "failure-copy-delete")
    COMPOSE_ENV["WORKFLOW_RESULT_TTL_SECONDS"] = "5"
    compose("up", "-d", "--force-recreate", "api")
    compose("run", "--rm", "-T", "check", "ttl-cleanup")


def cleanup() -> None:
    result = subprocess.run(
        [
            "docker",
            "--context",
            "desktop-linux",
            "compose",
            "--project-name",
            PROJECT,
            "-f",
            str(COMPOSE_FILE),
            "down",
            "--volumes",
            "--remove-orphans",
        ],
        cwd=ROOT,
        env={**os.environ, **COMPOSE_ENV},
        capture_output=True,
        text=True,
    )
    if result.returncode:
        print(f"Disposable project cleanup failed: {result.stderr[-2000:]}", file=sys.stderr)
    for image in (MAIN_IMAGE, SIDECAR_IMAGE):
        removed = subprocess.run(
            ["docker", "--context", "desktop-linux", "image", "rm", image],
            capture_output=True,
            text=True,
        )
        if removed.returncode and "No such image" not in removed.stderr:
            print(f"Image cleanup warning for {image}: {removed.stderr.strip()}", file=sys.stderr)
    if result.returncode:
        raise SystemExit(1)


def print_sanitized_failure_logs() -> None:
    try:
        completed = compose("logs", "--no-color", capture=True)
    except Exception:
        return
    lines = completed.stdout.splitlines() if completed.stdout else []
    relevant = [line for line in lines if re.search(r"error|fail|traceback|exception", line, re.I)]
    sanitized = "\n".join(re.sub(r"https?://[^\s\"']+", "<URL>", line) for line in relevant[-80:])
    if sanitized:
        print(
            "Sanitized disposable-stack failure logs (URLs removed):\n" + sanitized, file=sys.stderr
        )


PROJECT = "media-workflow-synthetic-" + uuid.uuid4().hex[:12]
MAIN_IMAGE = "media-manager:workflow-validation"
SIDECAR_IMAGE = "media-manager-sidecar-workflow-validation:" + uuid.uuid4().hex[:12]
COMPOSE_ENV = {
    "WORKFLOW_SIDECAR_IMAGE": SIDECAR_IMAGE,
    "WORKFLOW_RESULT_TTL_SECONDS": "604800",
}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] != "check":
        run_client_phase(sys.argv[1])
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "check":
        run_client_phase(sys.argv[2] if len(sys.argv) > 2 else "download-only")
        return 0
    completed = False
    try:
        run_stack()
        completed = True
        print("All real-MeTube workflow checks passed.", flush=True)
    except Exception:
        print_sanitized_failure_logs()
        raise
    finally:
        cleanup()
    return 0 if completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
