from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


VideoCodec = Literal["h264", "h265", "vp9", "av1", "prores", "ffv1"]
AudioCodec = Literal["aac", "opus", "mp3", "flac", "copy"]
Container = Literal["mp4", "mkv", "webm", "mov"]
PixelFormat = Literal["yuv420p", "yuv422p", "yuv444p"]
AlignmentPolicy = Literal["error"]
TrimMode = Literal["smart", "copy", "exact"]
Preset = Literal[
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
]


def default_thread_count() -> int:
    cpu_total = os.cpu_count() or 1
    return max(cpu_total - 2, 1)


@dataclass(slots=True)
class CompressionProfile:
    input_path: str
    output_path: str
    container: Container = "mp4"
    video_codec: VideoCodec = "h264"
    audio_codec: AudioCodec = "aac"
    lossless: bool = False
    crf: int | None = 23
    video_bitrate: str | None = None
    audio_bitrate: str | None = "128k"
    resolution: str | None = None
    fps: int | None = None
    pixel_format: PixelFormat = "yuv420p"
    preset: Preset = "medium"
    gop: int | None = None
    sample_rate: int | None = None
    audio_channels: int | None = None
    trim_start: float | None = None
    trim_duration: float | None = None
    disable_audio: bool = False
    threads: int = field(default_factory=default_thread_count)
    overwrite: bool = False

    def validate(self) -> None:
        if not self.input_path:
            raise ValueError("Input path is required.")

        if not self.output_path:
            raise ValueError("Output path is required.")

        if self.input_path == self.output_path:
            raise ValueError("Input and output paths must be different.")

        if self.crf is not None and self.crf < 0:
            raise ValueError("CRF must be a non-negative integer.")

        if self.fps is not None and self.fps <= 0:
            raise ValueError("FPS must be a positive integer.")

        if self.gop is not None and self.gop <= 0:
            raise ValueError("GOP must be a positive integer.")

        if self.sample_rate is not None and self.sample_rate <= 0:
            raise ValueError("Sample rate must be a positive integer.")

        if self.audio_channels is not None and self.audio_channels <= 0:
            raise ValueError("Audio channels must be a positive integer.")

        if self.trim_start is not None and self.trim_start < 0:
            raise ValueError("Trim start must be a non-negative number.")

        if self.trim_duration is not None and self.trim_duration <= 0:
            raise ValueError("Trim duration must be a positive number.")

        if self.threads <= 0:
            raise ValueError("Threads must be a positive integer.")

        if self.video_bitrate and self.crf is not None:
            raise ValueError("Choose either CRF or target video bitrate, not both.")

        if self.lossless and self.video_bitrate is not None:
            raise ValueError("Lossless mode cannot be combined with a target video bitrate.")

        if self.container == "webm" and self.video_codec not in {"vp9", "av1"}:
            raise ValueError("WebM output should use VP9 or AV1.")

        if self.container == "webm" and self.audio_codec not in {"opus", "copy"}:
            raise ValueError("WebM output should use Opus audio or audio copy.")

        if self.video_codec == "ffv1" and self.container != "mkv":
            raise ValueError("FFV1 is best paired with MKV in this starter implementation.")

        if self.video_codec == "prores" and self.container not in {"mov", "mkv"}:
            raise ValueError("ProRes output should use MOV or MKV.")

        if self.lossless and self.video_codec == "prores":
            raise ValueError("ProRes is not treated as a true lossless codec in this tool.")

        if self.lossless and self.video_codec not in {"h264", "h265", "vp9", "av1", "ffv1"}:
            raise ValueError(f"Lossless mode is not supported for codec '{self.video_codec}'.")

        if self.audio_codec == "copy" and (
            self.audio_bitrate is not None or self.sample_rate is not None or self.audio_channels is not None
        ):
            raise ValueError("Audio copy cannot be combined with bitrate, sample rate, or channel changes.")

        if self.video_codec in {"ffv1", "prores"} and self.video_bitrate is not None:
            raise ValueError(f"Target video bitrate is not supported for codec '{self.video_codec}'.")

        if self.video_codec in {"ffv1", "prores"} and self.crf is not None:
            raise ValueError(f"CRF is not supported for codec '{self.video_codec}'.")

    @property
    def input_path_obj(self) -> Path:
        return Path(self.input_path)

    @property
    def output_path_obj(self) -> Path:
        return Path(self.output_path)


@dataclass(slots=True)
class EditRequest:
    input_path: str
    output_path: str
    container: Container = "mp4"
    start_time: float | None = None
    end_time: float | None = None
    replacement_audio_path: str | None = None
    audio_offset: float | None = None
    trim_mode: TrimMode = "smart"
    audio_codec: AudioCodec | None = None
    audio_bitrate: str | None = None
    sample_rate: int | None = None
    audio_channels: int | None = None
    overwrite: bool = False
    on_align_fail: AlignmentPolicy = "error"

    def validate(self) -> None:
        if not self.input_path:
            raise ValueError("Input path is required.")

        if not self.output_path:
            raise ValueError("Output path is required.")

        if self.input_path == self.output_path:
            raise ValueError("Input and output paths must be different.")

        if self.start_time is not None and self.start_time < 0:
            raise ValueError("Start time must be a non-negative number.")

        if self.end_time is not None and self.end_time <= 0:
            raise ValueError("End time must be greater than zero.")

        if self.start_time is not None and self.end_time is not None and self.end_time <= self.start_time:
            raise ValueError("End time must be greater than start time.")

        if self.audio_offset is not None and self.audio_offset < 0:
            raise ValueError("Audio offset must be a non-negative number.")

        if self.sample_rate is not None and self.sample_rate <= 0:
            raise ValueError("Sample rate must be a positive integer.")

        if self.audio_channels is not None and self.audio_channels <= 0:
            raise ValueError("Audio channels must be a positive integer.")

        if self.audio_codec == "copy" and (
            self.audio_bitrate is not None or self.sample_rate is not None or self.audio_channels is not None
        ):
            raise ValueError("Audio copy cannot be combined with bitrate, sample rate, or channel changes.")

        if self.trim_mode not in {"smart", "copy", "exact"}:
            raise ValueError(f"Unsupported trim mode '{self.trim_mode}'.")

        if self.on_align_fail != "error":
            raise ValueError(f"Unsupported alignment failure policy '{self.on_align_fail}'.")

        if not self.has_edit_operation:
            raise ValueError("No edit operation was requested.")

    @property
    def has_edit_operation(self) -> bool:
        return self.start_time is not None or self.end_time is not None or self.replacement_audio_path is not None

    @property
    def input_path_obj(self) -> Path:
        return Path(self.input_path)

    @property
    def output_path_obj(self) -> Path:
        return Path(self.output_path)

    @property
    def replacement_audio_path_obj(self) -> Path | None:
        if self.replacement_audio_path is None:
            return None
        return Path(self.replacement_audio_path)


@dataclass(slots=True)
class EditResult:
    command: list[str]
    input_size_bytes: int
    output_size_bytes: int
    duration_seconds: float | None
    video_stream_copied: bool
    audio_stream_copied: bool
    alignment_offset_seconds: float | None
    alignment_confidence: float | None
    warnings: list[str]

    @property
    def bytes_saved(self) -> int:
        return self.input_size_bytes - self.output_size_bytes

    @property
    def compression_ratio(self) -> float | None:
        if self.input_size_bytes <= 0:
            return None
        return self.output_size_bytes / self.input_size_bytes
