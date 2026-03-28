from __future__ import annotations

import math
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from vutil.models import AudioCodec, CompressionProfile, Container, PixelFormat, Preset, VideoCodec
from vutil.runner import CompressionResult, probe_duration_seconds, run_compression


ProgressCallback = Callable[[dict[str, str], float | None], None]
AttemptCallback = Callable[[int, int, CompressionProfile], None]
FinalEncodeCallback = Callable[[int, CompressionProfile], None]

CRF_RANGES: dict[VideoCodec, tuple[int, int]] = {
    "h264": (0, 51),
    "h265": (0, 51),
    "vp9": (0, 63),
    "av1": (0, 63),
}

SAMPLE_CHUNK_DURATION_SECONDS = 2.0
MAX_SAMPLE_WINDOWS = 3
SAMPLE_ESTIMATE_SAFETY_RATIO = 0.92
MAX_SAMPLE_PROBES = 3
MAX_ESTIMATE_OVERSHOOT_BEFORE_MAX_CRF = 1.12

DEFAULT_START_CRF: dict[VideoCodec, int] = {
    "h264": 23,
    "h265": 28,
    "vp9": 32,
    "av1": 32,
}

DEFAULT_CRF_STEPS_PER_HALVING: dict[VideoCodec, float] = {
    "h264": 6.0,
    "h265": 6.0,
    "vp9": 8.0,
    "av1": 8.0,
}

MAX_FINAL_CRF_EXTRAPOLATION_STEPS = 3

@dataclass(slots=True)
class AutoCompressionRequest:
    input_path: str
    output_path: str
    max_size_bytes: int
    container: Container
    video_codec: VideoCodec
    audio_codec: AudioCodec
    audio_bitrate: str | None
    resolution: str | None
    fps: int | None
    pixel_format: PixelFormat
    preset: Preset
    gop: int | None
    sample_rate: int | None
    audio_channels: int | None
    threads: int
    overwrite: bool

    def validate(self) -> None:
        if self.max_size_bytes <= 0:
            raise ValueError("Maximum size must be greater than zero.")

        if self.video_codec not in CRF_RANGES:
            raise ValueError("Automatic max-size mode currently supports h264, h265, vp9, and av1.")

        if self.audio_codec == "copy":
            input_size = Path(self.input_path).stat().st_size
            if input_size > self.max_size_bytes:
                raise ValueError(
                    "Audio copy may keep the file too large for the chosen size cap. "
                    "Use a compressible audio codec for auto mode."
                )


@dataclass(slots=True)
class AutoCompressionResult:
    result: CompressionResult
    final_profile: CompressionProfile
    selected_crf: int
    attempts: int
    max_size_bytes: int
    sampled_seconds: float
    size_cap_exceeded: bool


def compress_with_max_size(
    request: AutoCompressionRequest,
    *,
    progress_callback: ProgressCallback | None = None,
    attempt_callback: AttemptCallback | None = None,
    final_encode_callback: FinalEncodeCallback | None = None,
) -> AutoCompressionResult:
    request.validate()
    _ensure_final_output_ready(request.output_path, request.overwrite)

    duration_seconds = probe_duration_seconds(request.input_path)
    if duration_seconds is None or duration_seconds <= 0:
        raise RuntimeError(
            "Automatic max-size mode requires a video with a valid, probeable duration. "
            "Could not determine the input duration."
        )

    min_crf, max_crf = CRF_RANGES[request.video_codec]
    sample_windows = _build_sample_windows(duration_seconds)
    sampled_seconds = sum(window.duration_seconds for window in sample_windows)
    estimated_sizes_by_crf: dict[int, int] = {}
    attempts = 0
    safe_size_limit = int(request.max_size_bytes * SAMPLE_ESTIMATE_SAFETY_RATIO)

    with tempfile.TemporaryDirectory(prefix="vutil-") as tmp_dir:
        temp_dir = Path(tmp_dir)
        
        def measure_sample(crf: int) -> int:
            nonlocal attempts

            if crf in estimated_sizes_by_crf:
                return estimated_sizes_by_crf[crf]

            attempts += 1
            sample_profile = _build_sample_profile(
                request,
                temp_dir / f"sample-crf-{crf}-preview.{request.container}",
                crf,
                sample_windows[0],
            )

            if attempt_callback is not None:
                attempt_callback(attempts, crf, sample_profile)

            estimated_output_size = _estimate_full_output_size(
                request=request,
                crf=crf,
                duration_seconds=duration_seconds,
                sample_windows=sample_windows,
                temp_dir=temp_dir,
            )
            estimated_sizes_by_crf[crf] = estimated_output_size
            return estimated_output_size

        initial_crf = _clamp_crf(DEFAULT_START_CRF[request.video_codec], min_crf, max_crf)
        measure_sample(initial_crf)

        while len(estimated_sizes_by_crf) < MAX_SAMPLE_PROBES:
            next_crf = _predict_crf_from_samples(
                estimated_sizes_by_crf=estimated_sizes_by_crf,
                target_size_bytes=safe_size_limit,
                video_codec=request.video_codec,
                min_crf=min_crf,
                max_crf=max_crf,
            )
            if next_crf in estimated_sizes_by_crf:
                next_crf = _choose_refinement_probe(
                    estimated_sizes_by_crf=estimated_sizes_by_crf,
                    target_size_bytes=safe_size_limit,
                    video_codec=request.video_codec,
                    min_crf=min_crf,
                    max_crf=max_crf,
                )
            if next_crf is None or next_crf in estimated_sizes_by_crf:
                break
            measure_sample(next_crf)

        final_step_size = max(1, round(DEFAULT_CRF_STEPS_PER_HALVING[request.video_codec] / 2))

        if any(size_bytes <= request.max_size_bytes for size_bytes in estimated_sizes_by_crf.values()):
            selected_crf = _predict_crf_from_samples(
                estimated_sizes_by_crf=estimated_sizes_by_crf,
                target_size_bytes=request.max_size_bytes,
                video_codec=request.video_codec,
                min_crf=min_crf,
                max_crf=max_crf,
                for_final_encode=True,
            )
        else:
            highest_sampled_estimate = estimated_sizes_by_crf[max(estimated_sizes_by_crf)]
            if highest_sampled_estimate > int(request.max_size_bytes * MAX_ESTIMATE_OVERSHOOT_BEFORE_MAX_CRF):
                selected_crf = max_crf
            else:
                raw_final_prediction = _predict_from_nearest_samples(
                    estimated_sizes_by_crf=estimated_sizes_by_crf,
                    target_size_bytes=request.max_size_bytes,
                    fallback_video_codec=request.video_codec,
                )
                if raw_final_prediction >= max_crf:
                    selected_crf = max_crf
                else:
                    selected_crf = _clamp_final_extrapolated_crf(
                        predicted=raw_final_prediction,
                        measured_sizes_by_crf=estimated_sizes_by_crf,
                        target_size_bytes=request.max_size_bytes,
                        max_jump=final_step_size * MAX_FINAL_CRF_EXTRAPOLATION_STEPS,
                        min_crf=min_crf,
                        max_crf=max_crf,
                    )

        final_output_path = temp_dir / f"final-crf-{selected_crf}.{request.container}"
        final_profile = _build_full_profile(request, final_output_path, selected_crf)
        if final_encode_callback is not None:
            final_encode_callback(selected_crf, final_profile)
        final_result = run_compression(final_profile, progress_callback=progress_callback)
        attempts += 1
        size_cap_exceeded = final_result.output_size_bytes > request.max_size_bytes

        final_output_path = Path(request.output_path)
        _move_output_to_final_path(temp_dir / f"final-crf-{selected_crf}.{request.container}", final_output_path)

    return AutoCompressionResult(
        result=CompressionResult(
            command=final_result.command,
            input_size_bytes=final_result.input_size_bytes,
            output_size_bytes=Path(request.output_path).stat().st_size,
            duration_seconds=final_result.duration_seconds,
        ),
        final_profile=final_profile,
        selected_crf=selected_crf,
        attempts=attempts,
        max_size_bytes=request.max_size_bytes,
        sampled_seconds=sampled_seconds,
        size_cap_exceeded=size_cap_exceeded,
    )


def build_auto_request(
    *,
    input_path: str,
    output_path: str,
    max_size_mb: float,
    container: Container,
    video_codec: VideoCodec,
    audio_codec: AudioCodec,
    audio_bitrate: str | None,
    resolution: str | None,
    fps: int | None,
    pixel_format: PixelFormat,
    preset: Preset,
    gop: int | None,
    sample_rate: int | None,
    audio_channels: int | None,
    threads: int,
    overwrite: bool,
) -> AutoCompressionRequest:
    max_size_bytes = int(max_size_mb * 1024 * 1024)
    request = AutoCompressionRequest(
        input_path=input_path,
        output_path=output_path,
        max_size_bytes=max_size_bytes,
        container=container,
        video_codec=video_codec,
        audio_codec=audio_codec,
        audio_bitrate=_resolve_auto_audio_bitrate(
            input_path=input_path,
            max_size_bytes=max_size_bytes,
            audio_codec=audio_codec,
            explicit_audio_bitrate=audio_bitrate,
        ),
        resolution=resolution,
        fps=fps,
        pixel_format=pixel_format,
        preset=preset,
        gop=gop,
        sample_rate=sample_rate,
        audio_channels=audio_channels,
        threads=threads,
        overwrite=overwrite,
    )
    request.validate()
    return request


def choose_auto_video_codec(container: Container, explicit_codec: VideoCodec | None) -> VideoCodec:
    if explicit_codec is not None:
        return explicit_codec

    if container == "webm":
        return "vp9"

    return "h265"


def choose_auto_audio_codec(container: Container, explicit_audio_codec: AudioCodec | None) -> AudioCodec:
    if explicit_audio_codec is not None:
        return explicit_audio_codec

    if container == "webm":
        return "opus"

    return "aac"


@dataclass(frozen=True, slots=True)
class SampleWindow:
    start_seconds: float
    duration_seconds: float


def _build_profile_for_attempt(
    request: AutoCompressionRequest,
    output_path: Path,
    crf: int,
) -> CompressionProfile:
    return CompressionProfile(
        input_path=request.input_path,
        output_path=str(output_path),
        container=request.container,
        video_codec=request.video_codec,
        audio_codec=request.audio_codec,
        lossless=False,
        crf=crf,
        video_bitrate=None,
        audio_bitrate=request.audio_bitrate,
        resolution=request.resolution,
        fps=request.fps,
        pixel_format=request.pixel_format,
        preset=request.preset,
        gop=request.gop,
        sample_rate=request.sample_rate,
        audio_channels=request.audio_channels,
        threads=request.threads,
        overwrite=True,
    )


def _build_sample_profile(
    request: AutoCompressionRequest,
    output_path: Path,
    crf: int,
    window: SampleWindow,
) -> CompressionProfile:
    profile = _build_profile_for_attempt(request, output_path, crf)
    profile.trim_start = window.start_seconds
    profile.trim_duration = window.duration_seconds
    profile.disable_audio = True
    return profile


def _build_full_profile(
    request: AutoCompressionRequest,
    output_path: Path,
    crf: int,
) -> CompressionProfile:
    return _build_profile_for_attempt(request, output_path, crf)


def _build_sample_windows(duration_seconds: float) -> list[SampleWindow]:
    if duration_seconds <= SAMPLE_CHUNK_DURATION_SECONDS:
        return [SampleWindow(start_seconds=0.0, duration_seconds=duration_seconds)]

    chunk_duration = min(SAMPLE_CHUNK_DURATION_SECONDS, duration_seconds / MAX_SAMPLE_WINDOWS)
    if duration_seconds <= chunk_duration * 2:
        positions = [0.0, duration_seconds - chunk_duration]
    else:
        positions = [
            duration_seconds * 0.10,
            duration_seconds * 0.50 - chunk_duration / 2,
            duration_seconds * 0.85 - chunk_duration / 2,
        ]

    windows: list[SampleWindow] = []
    max_start = max(duration_seconds - chunk_duration, 0.0)
    for position in positions:
        start = min(max(position, 0.0), max_start)
        if any(abs(existing.start_seconds - start) < 0.25 for existing in windows):
            continue
        windows.append(SampleWindow(start_seconds=start, duration_seconds=chunk_duration))

    if not windows:
        windows.append(SampleWindow(start_seconds=0.0, duration_seconds=chunk_duration))

    return windows


def _clamp_crf(value: int, min_crf: int, max_crf: int) -> int:
    return max(min(value, max_crf), min_crf)


def _predict_crf_from_samples(
    *,
    estimated_sizes_by_crf: dict[int, int],
    target_size_bytes: int,
    video_codec: VideoCodec,
    min_crf: int,
    max_crf: int,
    for_final_encode: bool = False,
) -> int:
    step_size = max(1, round(DEFAULT_CRF_STEPS_PER_HALVING[video_codec] / 2))

    if not estimated_sizes_by_crf:
        return _clamp_crf(DEFAULT_START_CRF[video_codec], min_crf, max_crf)

    if len(estimated_sizes_by_crf) == 1:
        sample_crf, sample_size = next(iter(estimated_sizes_by_crf.items()))
        predicted = _predict_from_single_sample(
            sample_crf=sample_crf,
            sample_size_bytes=sample_size,
            target_size_bytes=target_size_bytes,
            video_codec=video_codec,
        )
        return _clamp_probe_jump(
            predicted=predicted,
            measured_crfs=estimated_sizes_by_crf,
            max_jump=step_size,
            min_crf=min_crf,
            max_crf=max_crf,
        )

    bracket = _find_size_bracket(estimated_sizes_by_crf, target_size_bytes)
    if bracket is not None:
        lower_crf, lower_size, upper_crf, upper_size = bracket
        predicted = _interpolate_between_samples(
            lower_crf=lower_crf,
            lower_size_bytes=lower_size,
            upper_crf=upper_crf,
            upper_size_bytes=upper_size,
            target_size_bytes=target_size_bytes,
        )
        return _clamp_crf(predicted, min_crf, max_crf)

    predicted = _predict_from_nearest_samples(
        estimated_sizes_by_crf=estimated_sizes_by_crf,
        target_size_bytes=target_size_bytes,
        fallback_video_codec=video_codec,
    )
    if for_final_encode:
        return _clamp_final_extrapolated_crf(
            predicted=predicted,
            measured_sizes_by_crf=estimated_sizes_by_crf,
            target_size_bytes=target_size_bytes,
            max_jump=step_size * MAX_FINAL_CRF_EXTRAPOLATION_STEPS,
            min_crf=min_crf,
            max_crf=max_crf,
        )

    return _clamp_probe_jump(
        predicted=predicted,
        measured_crfs=estimated_sizes_by_crf,
        max_jump=step_size,
        min_crf=min_crf,
        max_crf=max_crf,
    )


def _predict_from_single_sample(
    *,
    sample_crf: int,
    sample_size_bytes: int,
    target_size_bytes: int,
    video_codec: VideoCodec,
) -> int:
    default_slope = math.log(0.5) / DEFAULT_CRF_STEPS_PER_HALVING[video_codec]
    size_ratio = math.log(max(target_size_bytes, 1) / max(sample_size_bytes, 1))
    return round(sample_crf + (size_ratio / default_slope))


def _find_size_bracket(
    estimated_sizes_by_crf: dict[int, int],
    target_size_bytes: int,
) -> tuple[int, int, int, int] | None:
    sorted_samples = sorted(estimated_sizes_by_crf.items())
    lower_candidate: tuple[int, int] | None = None
    upper_candidate: tuple[int, int] | None = None

    for crf, size_bytes in sorted_samples:
        if size_bytes >= target_size_bytes:
            if lower_candidate is None or crf > lower_candidate[0]:
                lower_candidate = (crf, size_bytes)
        if size_bytes <= target_size_bytes:
            if upper_candidate is None or crf < upper_candidate[0]:
                upper_candidate = (crf, size_bytes)

    if lower_candidate is None or upper_candidate is None:
        return None

    lower_crf, lower_size = lower_candidate
    upper_crf, upper_size = upper_candidate
    if lower_crf >= upper_crf:
        return None

    return lower_crf, lower_size, upper_crf, upper_size


def _interpolate_between_samples(
    *,
    lower_crf: int,
    lower_size_bytes: int,
    upper_crf: int,
    upper_size_bytes: int,
    target_size_bytes: int,
) -> int:
    lower_log = math.log(max(lower_size_bytes, 1))
    upper_log = math.log(max(upper_size_bytes, 1))
    target_log = math.log(max(target_size_bytes, 1))

    if upper_log == lower_log:
        return round((lower_crf + upper_crf) / 2)

    fraction = (target_log - lower_log) / (upper_log - lower_log)
    interpolated = lower_crf + fraction * (upper_crf - lower_crf)
    return round(interpolated)


def _predict_from_nearest_samples(
    *,
    estimated_sizes_by_crf: dict[int, int],
    target_size_bytes: int,
    fallback_video_codec: VideoCodec,
) -> int:
    sorted_samples = sorted(estimated_sizes_by_crf.items())
    if len(sorted_samples) < 2:
        sample_crf, sample_size = sorted_samples[0]
        return _predict_from_single_sample(
            sample_crf=sample_crf,
            sample_size_bytes=sample_size,
            target_size_bytes=target_size_bytes,
            video_codec=fallback_video_codec,
        )

    if all(size_bytes > target_size_bytes for _, size_bytes in sorted_samples):
        sample_a, sample_b = sorted_samples[-2], sorted_samples[-1]
    elif all(size_bytes < target_size_bytes for _, size_bytes in sorted_samples):
        sample_a, sample_b = sorted_samples[0], sorted_samples[1]
    else:
        closest_samples = sorted(
            sorted_samples,
            key=lambda item: abs(item[1] - target_size_bytes),
        )[:2]
        sample_a, sample_b = sorted(closest_samples)

    first_crf, first_size = sample_a
    second_crf, second_size = sample_b
    return _interpolate_between_samples(
        lower_crf=first_crf,
        lower_size_bytes=first_size,
        upper_crf=second_crf,
        upper_size_bytes=second_size,
        target_size_bytes=target_size_bytes,
    )


def _clamp_probe_jump(
    *,
    predicted: int,
    measured_crfs: dict[int, int],
    max_jump: int,
    min_crf: int,
    max_crf: int,
) -> int:
    nearest_crf = min(measured_crfs, key=lambda crf: abs(crf - predicted))
    lower_bound = max(min_crf, nearest_crf - max_jump)
    upper_bound = min(max_crf, nearest_crf + max_jump)
    return max(min(predicted, upper_bound), lower_bound)


def _clamp_final_extrapolated_crf(
    *,
    predicted: int,
    measured_sizes_by_crf: dict[int, int],
    target_size_bytes: int,
    max_jump: int,
    min_crf: int,
    max_crf: int,
) -> int:
    if all(size_bytes > target_size_bytes for size_bytes in measured_sizes_by_crf.values()):
        anchor_crf = max(measured_sizes_by_crf)
        lower_bound = anchor_crf
        upper_bound = min(max_crf, anchor_crf + max_jump)
        return max(min(predicted, upper_bound), lower_bound)

    if all(size_bytes < target_size_bytes for size_bytes in measured_sizes_by_crf.values()):
        anchor_crf = min(measured_sizes_by_crf)
        lower_bound = max(min_crf, anchor_crf - max_jump)
        upper_bound = anchor_crf
        return max(min(predicted, upper_bound), lower_bound)

    return _clamp_probe_jump(
        predicted=predicted,
        measured_crfs=measured_sizes_by_crf,
        max_jump=max_jump,
        min_crf=min_crf,
        max_crf=max_crf,
    )


def _choose_refinement_probe(
    *,
    estimated_sizes_by_crf: dict[int, int],
    target_size_bytes: int,
    video_codec: VideoCodec,
    min_crf: int,
    max_crf: int,
) -> int | None:
    if not estimated_sizes_by_crf:
        return None

    step_size = max(1, round(DEFAULT_CRF_STEPS_PER_HALVING[video_codec] / 2))
    bracket = _find_size_bracket(estimated_sizes_by_crf, target_size_bytes)
    if bracket is not None:
        lower_crf, _, upper_crf, _ = bracket
        candidate = round((lower_crf + upper_crf) / 2)
        candidate = _clamp_crf(candidate, min_crf, max_crf)
        if candidate in estimated_sizes_by_crf:
            return None
        return candidate

    if all(size_bytes > target_size_bytes for size_bytes in estimated_sizes_by_crf.values()):
        candidate = _clamp_crf(max(estimated_sizes_by_crf) + step_size, min_crf, max_crf)
    elif all(size_bytes < target_size_bytes for size_bytes in estimated_sizes_by_crf.values()):
        candidate = _clamp_crf(min(estimated_sizes_by_crf) - step_size, min_crf, max_crf)
    else:
        closest_crf = min(
            estimated_sizes_by_crf,
            key=lambda crf: abs(estimated_sizes_by_crf[crf] - target_size_bytes),
        )
        closest_size = estimated_sizes_by_crf[closest_crf]
        direction = step_size if closest_size > target_size_bytes else -step_size
        candidate = _clamp_crf(closest_crf + direction, min_crf, max_crf)

    if candidate in estimated_sizes_by_crf:
        return None
    return candidate


def _estimate_full_output_size(
    *,
    request: AutoCompressionRequest,
    crf: int,
    duration_seconds: float,
    sample_windows: list[SampleWindow],
    temp_dir: Path,
) -> int:
    total_sample_video_bytes = 0
    total_sample_duration = 0.0

    for index, window in enumerate(sample_windows, start=1):
        sample_output = temp_dir / f"sample-crf-{crf}-{index}.{request.container}"
        profile = _build_sample_profile(request, sample_output, crf, window)
        result = run_compression(profile, progress_callback=None)
        total_sample_video_bytes += result.output_size_bytes
        total_sample_duration += window.duration_seconds

    if total_sample_duration <= 0:
        raise RuntimeError("Sample duration must be greater than zero.")

    estimated_video_bytes = int((total_sample_video_bytes / total_sample_duration) * duration_seconds)
    estimated_audio_bytes = _estimate_audio_size_bytes(duration_seconds, request.audio_bitrate)
    return estimated_video_bytes + estimated_audio_bytes


def _estimate_audio_size_bytes(duration_seconds: float, audio_bitrate: str | None) -> int:
    if not audio_bitrate:
        return 0

    bitrate_bits_per_second = _parse_bitrate_to_bits_per_second(audio_bitrate)
    return int((bitrate_bits_per_second / 8.0) * duration_seconds)


def _parse_bitrate_to_bits_per_second(value: str) -> int:
    normalized = value.strip().lower()
    multipliers = {
        "k": 1_000,
        "m": 1_000_000,
        "g": 1_000_000_000,
    }

    suffix = normalized[-1]
    if suffix in multipliers:
        return int(float(normalized[:-1]) * multipliers[suffix])

    return int(float(normalized))


def _move_output_to_final_path(source_path: Path, final_output_path: Path) -> None:
    if final_output_path.exists():
        final_output_path.unlink()
    shutil.move(str(source_path), str(final_output_path))


def _ensure_final_output_ready(output_path: str, overwrite: bool) -> None:
    final_output_path = Path(output_path)
    final_output_path.parent.mkdir(parents=True, exist_ok=True)
    if final_output_path.exists() and not overwrite:
        raise FileExistsError(f"Output file already exists: {final_output_path}")


def _resolve_auto_audio_bitrate(
    *,
    input_path: str,
    max_size_bytes: int,
    audio_codec: AudioCodec,
    explicit_audio_bitrate: str | None,
) -> str | None:
    if audio_codec == "copy":
        return None

    if explicit_audio_bitrate is not None:
        return explicit_audio_bitrate

    duration_seconds = probe_duration_seconds(input_path)
    if duration_seconds is None or duration_seconds <= 0:
        return "96k" if audio_codec == "opus" else "128k"

    total_budget_kbps = (max_size_bytes * 8) / duration_seconds / 1000
    if audio_codec == "opus":
        if total_budget_kbps < 192:
            return "48k"
        if total_budget_kbps < 512:
            return "64k"
        return "96k"

    if total_budget_kbps < 192:
        return "64k"
    if total_budget_kbps < 512:
        return "96k"
    return "128k"
