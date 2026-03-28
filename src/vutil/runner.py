from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, IO

from vutil.ffmpeg_builder import build_ffmpeg_command
from vutil.models import CompressionProfile


ProgressCallback = Callable[[dict[str, str], float | None], None]


@dataclass(slots=True)
class CompressionResult:
    command: list[str]
    input_size_bytes: int
    output_size_bytes: int
    duration_seconds: float | None

    @property
    def bytes_saved(self) -> int:
        return self.input_size_bytes - self.output_size_bytes

    @property
    def compression_ratio(self) -> float | None:
        if self.input_size_bytes <= 0:
            return None
        return self.output_size_bytes / self.input_size_bytes


def run_compression(
    profile: CompressionProfile,
    progress_callback: ProgressCallback | None = None,
) -> CompressionResult:
    profile.validate()
    _ensure_ffmpeg_available()
    _ensure_paths(profile)

    duration_seconds = probe_duration_seconds(profile.input_path)
    command = build_ffmpeg_command(profile, progress=True)

    stderr_lines: list[str] = []
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    stderr_thread = threading.Thread(
        target=_read_stderr,
        args=(process.stderr, stderr_lines),
        daemon=True,
    )
    stderr_thread.start()

    progress_data: dict[str, str] = {}
    assert process.stdout is not None

    for raw_line in process.stdout:
        line = raw_line.strip()
        if not line or "=" not in line:
            continue

        key, value = line.split("=", maxsplit=1)
        progress_data[key] = value

        if key == "progress":
            percentage = _calculate_progress(progress_data, duration_seconds)
            if progress_callback is not None:
                progress_callback(dict(progress_data), percentage)

    return_code = process.wait()
    stderr_thread.join()

    if return_code != 0:
        error_text = "\n".join(stderr_lines).strip() or "ffmpeg failed without an error message."
        raise RuntimeError(error_text)

    output_size_bytes = profile.output_path_obj.stat().st_size
    input_size_bytes = profile.input_path_obj.stat().st_size
    return CompressionResult(
        command=command,
        input_size_bytes=input_size_bytes,
        output_size_bytes=output_size_bytes,
        duration_seconds=duration_seconds,
    )


def probe_duration_seconds(input_path: str) -> float | None:
    if shutil.which("ffprobe") is None:
        return None

    probe_command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        input_path,
    ]
    completed = subprocess.run(
        probe_command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return None

    output = completed.stdout.strip()
    if not output or output == "N/A":
        return None

    try:
        return float(output)
    except ValueError:
        return None


def _ensure_ffmpeg_available() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is not installed or not available on PATH.")


def _ensure_paths(profile: CompressionProfile) -> None:
    input_path = profile.input_path_obj
    output_path = profile.output_path_obj

    if not input_path.is_file():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    output_dir = output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    if output_path.exists() and not profile.overwrite:
        raise FileExistsError(f"Output file already exists: {output_path}")


def _read_stderr(stream: IO[str] | None, destination: list[str]) -> None:
    if stream is None:
        return

    try:
        for line in stream:
            stripped = line.strip()
            if stripped:
                destination.append(stripped)
    finally:
        stream.close()


def _calculate_progress(progress_data: dict[str, str], duration_seconds: float | None) -> float | None:
    if duration_seconds is None or duration_seconds <= 0:
        return None

    out_time_value = progress_data.get("out_time_us") or progress_data.get("out_time_ms")
    if out_time_value is None:
        return None

    try:
        encoded_microseconds = int(out_time_value)
    except ValueError:
        return None

    ratio = min(max(encoded_microseconds / (duration_seconds * 1_000_000), 0.0), 1.0)
    return ratio * 100.0
