from __future__ import annotations

import math
import shutil
import struct
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, IO

from vutil.models import EditRequest, EditResult
from vutil.runner import probe_duration_seconds


ProgressCallback = Callable[[dict[str, str], float | None], None]
CommandCallback = Callable[[list[str]], None]

ANALYSIS_SAMPLE_RATE = 8_000
FEATURE_FRAME_SECONDS = 0.08
FEATURE_HOP_SECONDS = 0.04
COARSE_SEARCH_STEP_SECONDS = 0.12
FINE_SEARCH_RADIUS_SECONDS = 1.0
MIN_ALIGNMENT_CONFIDENCE = 0.32
MAX_COARSE_CANDIDATES = 5
WAVEFORM_DOWNSAMPLE_FACTOR = 8
WAVEFORM_REFINE_STEP_SECONDS = 0.02
FEATURE_SPECTRUM_BINS = 48
FEATURE_CHROMA_BINS = 12
FEATURE_SUBSAMPLE_LIMIT = 320
WINDOWED_ALIGNMENT_THRESHOLD_SECONDS = 18.0
MIN_ALIGNMENT_WINDOW_SECONDS = 6.0
MAX_ALIGNMENT_WINDOW_SECONDS = 12.0
MAX_ALIGNMENT_WINDOWS = 4
ALIGNMENT_CONSENSUS_TOLERANCE_SECONDS = 0.75
MAX_WINDOW_CANDIDATES = 6
DEDUP_CANDIDATE_TOLERANCE_SECONDS = 0.35

DEFAULT_EXACT_VIDEO_CODEC_BY_CONTAINER = {
    "mp4": ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"],
    "mov": ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"],
    "mkv": ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"],
    "webm": ["-c:v", "libvpx-vp9", "-deadline", "good", "-cpu-used", "4", "-row-mt", "1", "-crf", "18", "-b:v", "0"],
}

AUDIO_CODEC_ARGS = {
    "aac": ["-c:a", "aac"],
    "opus": ["-c:a", "libopus"],
    "mp3": ["-c:a", "libmp3lame"],
    "flac": ["-c:a", "flac"],
    "copy": ["-c:a", "copy"],
}


@dataclass(frozen=True, slots=True)
class AlignmentMatch:
    offset_seconds: float
    confidence: float


@dataclass(frozen=True, slots=True)
class WindowAlignmentMatch:
    global_offset_seconds: float
    local_offset_seconds: float
    confidence: float
    window_start_seconds: float
    window_duration_seconds: float


@dataclass(frozen=True, slots=True)
class AlignmentCandidate:
    offset_seconds: float
    score: float
    confidence: float


def run_edit(
    request: EditRequest,
    *,
    progress_callback: ProgressCallback | None = None,
    command_callback: CommandCallback | None = None,
) -> EditResult:
    request.validate()
    _ensure_ffmpeg_available()
    _ensure_edit_paths(request)

    input_duration = probe_duration_seconds(request.input_path)
    if input_duration is None or input_duration <= 0:
        raise RuntimeError("Could not determine the input video duration.")

    start_time = request.start_time or 0.0
    end_time = request.end_time if request.end_time is not None else input_duration
    if end_time > input_duration:
        end_time = input_duration
    if end_time <= start_time:
        raise ValueError("End time must be greater than start time.")

    output_duration = end_time - start_time
    if output_duration <= 0:
        raise ValueError("Trim duration must be greater than zero.")

    warnings: list[str] = []
    alignment_offset_seconds: float | None = None
    alignment_confidence: float | None = None
    audio_stream_copied = True
    video_stream_copied = True
    effective_trim_mode = _resolve_effective_trim_mode(request)

    with tempfile.TemporaryDirectory(prefix="vutil-edit-") as tmp_dir:
        temp_dir = Path(tmp_dir)

        if request.replacement_audio_path is not None:
            if request.audio_offset is not None:
                alignment_offset_seconds = request.audio_offset
                alignment_confidence = 1.0
            else:
                match = _align_replacement_audio(
                    request=request,
                    start_time=start_time,
                    output_duration=output_duration,
                    temp_dir=temp_dir,
                    command_callback=command_callback,
                )
                alignment_offset_seconds = match.offset_seconds
                alignment_confidence = match.confidence
                if alignment_confidence < MIN_ALIGNMENT_CONFIDENCE:
                    raise RuntimeError(
                        "Could not confidently align the replacement audio. "
                        f"Best offset {alignment_offset_seconds:.3f}s with confidence {alignment_confidence:.2f}."
                    )

            final_output_path = temp_dir / f"edited-output.{request.container}"
            command, video_stream_copied, audio_stream_copied = _build_replace_audio_command(
                request=request,
                start_time=start_time,
                output_duration=output_duration,
                aligned_audio_offset=alignment_offset_seconds,
                output_path=final_output_path,
                effective_trim_mode=effective_trim_mode,
            )
        else:
            final_output_path = temp_dir / f"edited-output.{request.container}"
            command, video_stream_copied, audio_stream_copied = _build_trim_command(
                request=request,
                start_time=start_time,
                output_duration=output_duration,
                output_path=final_output_path,
                effective_trim_mode=effective_trim_mode,
            )

        if effective_trim_mode == "exact":
            warnings.append("Exact trim required re-encoding the edited streams.")

        if command_callback is not None:
            command_callback(command)

        _run_ffmpeg_command(
            command,
            progress_duration_seconds=output_duration,
            progress_callback=progress_callback,
        )

        _move_output_to_final_path(final_output_path, request.output_path_obj)

    return EditResult(
        command=command,
        input_size_bytes=request.input_path_obj.stat().st_size,
        output_size_bytes=request.output_path_obj.stat().st_size,
        duration_seconds=output_duration,
        video_stream_copied=video_stream_copied,
        audio_stream_copied=audio_stream_copied,
        alignment_offset_seconds=alignment_offset_seconds,
        alignment_confidence=alignment_confidence,
        warnings=warnings,
    )


def _build_trim_command(
    *,
    request: EditRequest,
    start_time: float,
    output_duration: float,
    output_path: Path,
    effective_trim_mode: str,
) -> tuple[list[str], bool, bool]:
    if effective_trim_mode == "exact":
        return _build_exact_trim_command(
            request=request,
            start_time=start_time,
            output_duration=output_duration,
            output_path=output_path,
        )

    command = ["ffmpeg", "-hide_banner", "-y" if request.overwrite else "-n"]
    if start_time > 0:
        command.extend(["-ss", _format_time_value(start_time)])
    command.extend(["-t", _format_time_value(output_duration), "-i", request.input_path])
    command.extend(["-c", "copy", str(output_path)])
    return command, True, True


def _build_replace_audio_command(
    *,
    request: EditRequest,
    start_time: float,
    output_duration: float,
    aligned_audio_offset: float,
    output_path: Path,
    effective_trim_mode: str,
) -> tuple[list[str], bool, bool]:
    if effective_trim_mode == "exact":
        return _build_exact_replace_audio_command(
            request=request,
            start_time=start_time,
            output_duration=output_duration,
            aligned_audio_offset=aligned_audio_offset,
            output_path=output_path,
        )

    command = ["ffmpeg", "-hide_banner", "-y" if request.overwrite else "-n"]
    if start_time > 0:
        command.extend(["-ss", _format_time_value(start_time)])
    command.extend(["-t", _format_time_value(output_duration), "-i", request.input_path])
    command.extend(
        [
            "-ss",
            _format_time_value(aligned_audio_offset),
            "-t",
            _format_time_value(output_duration),
            "-i",
            str(request.replacement_audio_path_obj),
        ]
    )
    command.extend(["-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy"])

    audio_codec = _resolve_edit_audio_codec(request)
    audio_stream_copied = audio_codec == "copy"
    command.extend(AUDIO_CODEC_ARGS[audio_codec])
    if audio_codec in {"aac", "opus", "mp3"} and request.audio_bitrate is not None:
        command.extend(["-b:a", request.audio_bitrate])
    if request.sample_rate is not None:
        command.extend(["-ar", str(request.sample_rate)])
    if request.audio_channels is not None:
        command.extend(["-ac", str(request.audio_channels)])

    command.extend(["-shortest", str(output_path)])
    return command, True, audio_stream_copied


def _build_exact_trim_command(
    *,
    request: EditRequest,
    start_time: float,
    output_duration: float,
    output_path: Path,
) -> tuple[list[str], bool, bool]:
    end_time = start_time + output_duration
    command = ["ffmpeg", "-hide_banner", "-y" if request.overwrite else "-n", "-i", request.input_path]
    filter_parts = [f"[0:v]trim=start={_format_time_value(start_time)}:end={_format_time_value(end_time)},setpts=PTS-STARTPTS[v]"]
    command.extend(["-filter_complex", ";".join(filter_parts)])
    command.extend(["-map", "[v]"])
    command.extend(DEFAULT_EXACT_VIDEO_CODEC_BY_CONTAINER[request.container])

    has_audio = _has_audio_stream(request.input_path)
    audio_stream_copied = False
    if has_audio:
        filter_parts.append(
            f"[0:a]atrim=start={_format_time_value(start_time)}:end={_format_time_value(end_time)},asetpts=PTS-STARTPTS[a]"
        )
        command = ["ffmpeg", "-hide_banner", "-y" if request.overwrite else "-n", "-i", request.input_path]
        command.extend(["-filter_complex", ";".join(filter_parts)])
        command.extend(["-map", "[v]", "-map", "[a]"])
        command.extend(DEFAULT_EXACT_VIDEO_CODEC_BY_CONTAINER[request.container])
        audio_codec = _resolve_reencoded_audio_codec(request, prefer_original=False)
        command.extend(AUDIO_CODEC_ARGS[audio_codec])
        if audio_codec in {"aac", "opus", "mp3"} and request.audio_bitrate is not None:
            command.extend(["-b:a", request.audio_bitrate])
        if request.sample_rate is not None:
            command.extend(["-ar", str(request.sample_rate)])
        if request.audio_channels is not None:
            command.extend(["-ac", str(request.audio_channels)])
    else:
        audio_stream_copied = True

    command.append(str(output_path))
    return command, False, audio_stream_copied


def _build_exact_replace_audio_command(
    *,
    request: EditRequest,
    start_time: float,
    output_duration: float,
    aligned_audio_offset: float,
    output_path: Path,
) -> tuple[list[str], bool, bool]:
    end_time = start_time + output_duration
    audio_end_time = aligned_audio_offset + output_duration
    command = [
        "ffmpeg",
        "-hide_banner",
        "-y" if request.overwrite else "-n",
        "-i",
        request.input_path,
        "-i",
        str(request.replacement_audio_path_obj),
    ]
    filter_parts = [
        f"[0:v]trim=start={_format_time_value(start_time)}:end={_format_time_value(end_time)},setpts=PTS-STARTPTS[v]",
        (
            f"[1:a]atrim=start={_format_time_value(aligned_audio_offset)}:"
            f"end={_format_time_value(audio_end_time)},asetpts=PTS-STARTPTS[a]"
        ),
    ]
    command.extend(["-filter_complex", ";".join(filter_parts)])
    command.extend(["-map", "[v]", "-map", "[a]"])
    command.extend(DEFAULT_EXACT_VIDEO_CODEC_BY_CONTAINER[request.container])
    audio_codec = _resolve_reencoded_audio_codec(request, prefer_original=False)
    command.extend(AUDIO_CODEC_ARGS[audio_codec])
    if audio_codec in {"aac", "opus", "mp3"} and request.audio_bitrate is not None:
        command.extend(["-b:a", request.audio_bitrate])
    if request.sample_rate is not None:
        command.extend(["-ar", str(request.sample_rate)])
    if request.audio_channels is not None:
        command.extend(["-ac", str(request.audio_channels)])
    command.extend(["-shortest", str(output_path)])
    return command, False, False


def _resolve_edit_audio_codec(request: EditRequest) -> str:
    if request.audio_codec is not None:
        return request.audio_codec

    if request.audio_bitrate is not None or request.sample_rate is not None or request.audio_channels is not None:
        return "opus" if request.container == "webm" else "aac"

    replacement_codec = _probe_primary_audio_codec(str(request.replacement_audio_path_obj))
    if replacement_codec is not None and _is_audio_codec_copy_safe(replacement_codec, request.container):
        return "copy"

    return "opus" if request.container == "webm" else "aac"


def _resolve_reencoded_audio_codec(request: EditRequest, *, prefer_original: bool) -> str:
    if request.audio_codec == "copy":
        raise ValueError("Audio copy is not supported when exact trim requires audio filtering.")

    if request.audio_codec is not None:
        return request.audio_codec

    if request.audio_bitrate is not None or request.sample_rate is not None or request.audio_channels is not None:
        return "opus" if request.container == "webm" else "aac"

    if prefer_original and request.replacement_audio_path_obj is not None:
        replacement_codec = _probe_primary_audio_codec(str(request.replacement_audio_path_obj))
        if replacement_codec in {"aac", "opus", "mp3", "flac"}:
            return replacement_codec

    return "opus" if request.container == "webm" else "aac"


def _resolve_effective_trim_mode(request: EditRequest) -> str:
    if request.trim_mode != "smart":
        return request.trim_mode

    if request.replacement_audio_path is not None and (request.start_time is not None or request.end_time is not None):
        return "exact"

    return "copy"


def _align_replacement_audio(
    *,
    request: EditRequest,
    start_time: float,
    output_duration: float,
    temp_dir: Path,
    command_callback: CommandCallback | None,
) -> AlignmentMatch:
    if not _has_audio_stream(request.input_path):
        raise RuntimeError("The input video does not contain an audio stream, so audio alignment is not possible.")

    source_analysis_path = temp_dir / "source-analysis.wav"
    replacement_analysis_path = temp_dir / "replacement-analysis.wav"

    source_command = _build_audio_analysis_command(
        input_path=request.input_path,
        output_path=source_analysis_path,
        start_time=start_time,
        duration_seconds=output_duration,
    )
    replacement_command = _build_audio_analysis_command(
        input_path=str(request.replacement_audio_path_obj),
        output_path=replacement_analysis_path,
        start_time=None,
        duration_seconds=None,
    )

    if command_callback is not None:
        command_callback(source_command)
        command_callback(replacement_command)

    _run_ffmpeg_command(source_command, progress_duration_seconds=None, progress_callback=None)
    _run_ffmpeg_command(replacement_command, progress_duration_seconds=None, progress_callback=None)

    source_samples, source_rate = _load_wav_samples(source_analysis_path)
    replacement_samples, replacement_rate = _load_wav_samples(replacement_analysis_path)
    if source_rate != replacement_rate:
        raise RuntimeError("Audio analysis sample rates do not match.")

    source_features, hop_seconds = _extract_alignment_features(source_samples, source_rate)
    replacement_features, replacement_hop_seconds = _extract_alignment_features(replacement_samples, replacement_rate)
    if not source_features or not replacement_features:
        raise RuntimeError("Could not extract enough audio content for alignment.")
    if abs(hop_seconds - replacement_hop_seconds) > 1e-9:
        raise RuntimeError("Audio analysis hop sizes do not match.")
    if len(replacement_features) < len(source_features):
        raise RuntimeError("Replacement audio is too short to cover the requested video segment.")

    if output_duration >= WINDOWED_ALIGNMENT_THRESHOLD_SECONDS:
        return _find_windowed_alignment_match(
            source_samples=source_samples,
            replacement_samples=replacement_samples,
            sample_rate=source_rate,
            total_duration_seconds=output_duration,
        )

    return _find_best_alignment_match(
        source_features,
        replacement_features,
        hop_seconds,
        source_samples,
        replacement_samples,
        source_rate,
    )


def _find_windowed_alignment_match(
    *,
    source_samples: list[int],
    replacement_samples: list[int],
    sample_rate: int,
    total_duration_seconds: float,
) -> AlignmentMatch:
    windows = _build_alignment_windows(total_duration_seconds)
    matches: list[WindowAlignmentMatch] = []
    replacement_features, replacement_hop_seconds = _extract_alignment_features(replacement_samples, sample_rate)
    if not replacement_features:
        raise RuntimeError("Could not extract replacement audio features for matching.")

    for window_start_seconds, window_duration_seconds in windows:
        start_sample = round(window_start_seconds * sample_rate)
        end_sample = min(len(source_samples), start_sample + round(window_duration_seconds * sample_rate))
        source_window = source_samples[start_sample:end_sample]
        if len(source_window) < round(MIN_ALIGNMENT_WINDOW_SECONDS * sample_rate * 0.75):
            continue

        source_features, hop_seconds = _extract_alignment_features(source_window, sample_rate)
        if not source_features:
            continue
        if abs(hop_seconds - replacement_hop_seconds) > 1e-9:
            continue
        if len(replacement_features) < len(source_features):
            continue

        local_matches = _find_alignment_candidates(
            source_features,
            replacement_features,
            hop_seconds,
            source_window,
            replacement_samples,
            sample_rate,
            max_candidates=MAX_WINDOW_CANDIDATES,
        )
        for local_match in local_matches:
            matches.append(
                WindowAlignmentMatch(
                    global_offset_seconds=local_match.offset_seconds - window_start_seconds,
                    local_offset_seconds=local_match.offset_seconds,
                    confidence=local_match.confidence,
                    window_start_seconds=window_start_seconds,
                    window_duration_seconds=window_duration_seconds,
                )
            )

    if not matches:
        raise RuntimeError("Could not extract enough aligned audio windows for matching.")

    best_cluster = _select_alignment_cluster(matches)
    weighted_offset_sum = sum(match.global_offset_seconds * match.confidence for match in best_cluster)
    confidence_sum = sum(match.confidence for match in best_cluster)
    if confidence_sum <= 1e-9:
        return AlignmentMatch(offset_seconds=max(best_cluster[0].global_offset_seconds, 0.0), confidence=0.0)

    clustered_offset = weighted_offset_sum / confidence_sum
    absolute_scores = sum(match.confidence for match in best_cluster) / len(best_cluster)
    coverage_bonus = min(len(best_cluster) / max(len(matches), 1), 1.0)
    confidence = max(min((absolute_scores * 0.75) + (coverage_bonus * 0.25), 1.0), 0.0)
    return AlignmentMatch(offset_seconds=max(clustered_offset, 0.0), confidence=confidence)


def _build_alignment_windows(total_duration_seconds: float) -> list[tuple[float, float]]:
    window_duration = min(MAX_ALIGNMENT_WINDOW_SECONDS, max(MIN_ALIGNMENT_WINDOW_SECONDS, total_duration_seconds / 6.0))
    if total_duration_seconds <= window_duration:
        return [(0.0, total_duration_seconds)]

    if total_duration_seconds <= window_duration * 2:
        return [(0.0, window_duration), (total_duration_seconds - window_duration, window_duration)]

    positions = [0.05, 0.30, 0.60, 0.82][:MAX_ALIGNMENT_WINDOWS]
    windows: list[tuple[float, float]] = []
    max_start = max(total_duration_seconds - window_duration, 0.0)
    for ratio in positions:
        start_seconds = min(max(ratio * total_duration_seconds, 0.0), max_start)
        if any(abs(existing_start - start_seconds) < 0.5 for existing_start, _ in windows):
            continue
        windows.append((start_seconds, window_duration))
    return windows


def _select_alignment_cluster(matches: list[WindowAlignmentMatch]) -> list[WindowAlignmentMatch]:
    distinct_window_count = len({match.window_start_seconds for match in matches})
    clustered_matches: list[tuple[list[WindowAlignmentMatch], int, float, float]] = []

    for pivot_match in matches:
        nearby_matches = [
            candidate
            for candidate in matches
            if abs(candidate.global_offset_seconds - pivot_match.global_offset_seconds)
            <= ALIGNMENT_CONSENSUS_TOLERANCE_SECONDS
        ]
        cluster = _deduplicate_cluster_by_window(nearby_matches)
        if not cluster:
            continue

        weighted_center = _weighted_cluster_offset(cluster)
        compactness = sum(abs(match.global_offset_seconds - weighted_center) for match in cluster) / len(cluster)
        clustered_matches.append((cluster, len(cluster), weighted_center, compactness))

    if not clustered_matches:
        return [max(matches, key=lambda match: match.confidence)]

    max_support = max(support for _, support, _, _ in clustered_matches)
    if max_support >= 2:
        clustered_matches = [
            cluster_data for cluster_data in clustered_matches if cluster_data[1] == max_support
        ]

    best_cluster: list[WindowAlignmentMatch] = []
    best_cluster_score = float("-inf")
    for cluster, support, weighted_center, compactness in clustered_matches:
        confidence_sum = sum(match.confidence for match in cluster)
        support_ratio = support / max(distinct_window_count, 1)
        cluster_score = confidence_sum
        cluster_score += support_ratio * 1.5
        cluster_score -= compactness * 0.35
        if weighted_center < 0:
            cluster_score -= 2.0 + abs(weighted_center)
        if cluster_score > best_cluster_score:
            best_cluster = cluster
            best_cluster_score = cluster_score

    return best_cluster or [max(matches, key=lambda match: match.confidence)]


def _deduplicate_cluster_by_window(cluster: list[WindowAlignmentMatch]) -> list[WindowAlignmentMatch]:
    best_by_window: dict[float, WindowAlignmentMatch] = {}
    for match in cluster:
        existing_match = best_by_window.get(match.window_start_seconds)
        if existing_match is None or match.confidence > existing_match.confidence:
            best_by_window[match.window_start_seconds] = match
    return sorted(best_by_window.values(), key=lambda match: match.window_start_seconds)


def _weighted_cluster_offset(cluster: list[WindowAlignmentMatch]) -> float:
    confidence_sum = sum(match.confidence for match in cluster)
    if confidence_sum <= 1e-9:
        return sum(match.global_offset_seconds for match in cluster) / len(cluster)
    return sum(match.global_offset_seconds * match.confidence for match in cluster) / confidence_sum


def _build_audio_analysis_command(
    *,
    input_path: str,
    output_path: Path,
    start_time: float | None,
    duration_seconds: float | None,
) -> list[str]:
    command = ["ffmpeg", "-hide_banner", "-y"]
    if start_time is not None and start_time > 0:
        command.extend(["-ss", _format_time_value(start_time)])
    if duration_seconds is not None:
        command.extend(["-t", _format_time_value(duration_seconds)])
    command.extend(
        [
            "-i",
            input_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(ANALYSIS_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ]
    )
    return command


def _extract_alignment_features(samples: list[int], sample_rate: int) -> tuple[list[list[float]], float]:
    frame_size = max(1, int(sample_rate * FEATURE_FRAME_SECONDS))
    hop_size = max(1, int(sample_rate * FEATURE_HOP_SECONDS))
    feature_rows: list[list[float]] = []
    previous_spectrum: list[float] | None = None

    for frame_start in range(0, max(len(samples) - frame_size + 1, 1), hop_size):
        frame = samples[frame_start : frame_start + frame_size]
        if len(frame) < frame_size:
            break

        rms = math.sqrt(sum(sample * sample for sample in frame) / frame_size)
        diff = sum(abs(frame[index] - frame[index - 1]) for index in range(1, frame_size)) / frame_size
        zero_crossings = sum(
            1
            for index in range(1, frame_size)
            if (frame[index - 1] < 0 <= frame[index]) or (frame[index - 1] >= 0 > frame[index])
        ) / frame_size
        frame_for_spectrum, effective_sample_rate = _prepare_frame_for_spectrum(frame, sample_rate)
        spectrum = _compute_spectral_magnitudes(frame_for_spectrum)
        band_energies = _compute_band_energies(spectrum, effective_sample_rate, len(frame_for_spectrum))
        chroma = _compute_chroma_profile(spectrum, effective_sample_rate, len(frame_for_spectrum))
        spectral_flux = _compute_spectral_flux(previous_spectrum, spectrum)
        previous_spectrum = spectrum
        feature_rows.append(
            [
                math.log1p(rms),
                math.log1p(diff),
                zero_crossings,
                *band_energies,
                spectral_flux,
                *chroma,
            ]
        )

    if not feature_rows:
        return [], hop_size / sample_rate

    normalized_columns = [
        _normalize_series([row[column_index] for row in feature_rows])
        for column_index in range(len(feature_rows[0]))
    ]
    normalized_rows = [
        [normalized_columns[column_index][row_index] for column_index in range(len(normalized_columns))]
        for row_index in range(len(feature_rows))
    ]
    return _normalize_feature_vectors(normalized_rows), hop_size / sample_rate


def _normalize_series(values: list[float]) -> list[float]:
    if not values:
        return []

    mean_value = sum(values) / len(values)
    variance = sum((value - mean_value) ** 2 for value in values) / len(values)
    if variance <= 1e-9:
        return [0.0 for _ in values]

    stdev = math.sqrt(variance)
    return [(value - mean_value) / stdev for value in values]


def _normalize_feature_vectors(rows: list[list[float]]) -> list[list[float]]:
    normalized_rows: list[list[float]] = []
    for row in rows:
        magnitude = math.sqrt(sum(value * value for value in row))
        if magnitude <= 1e-9:
            normalized_rows.append([0.0 for _ in row])
        else:
            normalized_rows.append([value / magnitude for value in row])
    return normalized_rows


def _prepare_frame_for_spectrum(samples: list[int], sample_rate: int) -> tuple[list[float], float]:
    if len(samples) <= FEATURE_SUBSAMPLE_LIMIT:
        return [sample / 32768.0 for sample in samples], float(sample_rate)

    step = max(1, len(samples) // FEATURE_SUBSAMPLE_LIMIT)
    downsampled = [samples[index] / 32768.0 for index in range(0, len(samples), step)]
    return downsampled, sample_rate / step


def _compute_spectral_magnitudes(frame: list[float]) -> list[float]:
    sample_count = len(frame)
    if sample_count <= 1:
        return []

    magnitudes: list[float] = []
    max_bin = min(FEATURE_SPECTRUM_BINS, max(1, sample_count // 2 - 1))
    for bin_index in range(1, max_bin + 1):
        real = 0.0
        imaginary = 0.0
        for sample_index, sample in enumerate(frame):
            angle = (2.0 * math.pi * bin_index * sample_index) / sample_count
            real += sample * math.cos(angle)
            imaginary -= sample * math.sin(angle)
        magnitudes.append(math.sqrt(real * real + imaginary * imaginary))
    return magnitudes


def _compute_band_energies(spectrum: list[float], sample_rate: float, frame_size: int) -> list[float]:
    low = 0.0
    mid = 0.0
    high = 0.0
    for index, magnitude in enumerate(spectrum, start=1):
        frequency = (index * sample_rate) / frame_size
        if frequency < 250:
            low += magnitude
        elif frequency < 2_000:
            mid += magnitude
        else:
            high += magnitude
    return [math.log1p(low), math.log1p(mid), math.log1p(high)]


def _compute_chroma_profile(spectrum: list[float], sample_rate: float, frame_size: int) -> list[float]:
    chroma = [0.0 for _ in range(FEATURE_CHROMA_BINS)]
    for index, magnitude in enumerate(spectrum, start=1):
        frequency = (index * sample_rate) / frame_size
        if frequency < 55 or frequency > 2_000:
            continue
        midi_note = round(69 + (12 * math.log2(frequency / 440.0)))
        chroma[midi_note % FEATURE_CHROMA_BINS] += magnitude

    total = sum(chroma)
    if total <= 1e-9:
        return chroma
    return [value / total for value in chroma]


def _compute_spectral_flux(previous_spectrum: list[float] | None, current_spectrum: list[float]) -> float:
    if previous_spectrum is None or not current_spectrum:
        return 0.0

    flux = 0.0
    length = min(len(previous_spectrum), len(current_spectrum))
    for index in range(length):
        difference = current_spectrum[index] - previous_spectrum[index]
        if difference > 0:
            flux += difference
    return math.log1p(flux)


def _find_best_alignment_match(
    source_features: list[list[float]],
    replacement_features: list[list[float]],
    hop_seconds: float,
    source_samples: list[int],
    replacement_samples: list[int],
    sample_rate: int,
) -> AlignmentMatch:
    candidates = _find_alignment_candidates(
        source_features,
        replacement_features,
        hop_seconds,
        source_samples,
        replacement_samples,
        sample_rate,
        max_candidates=MAX_COARSE_CANDIDATES,
    )
    if not candidates:
        return AlignmentMatch(offset_seconds=0.0, confidence=0.0)

    best_candidate = candidates[0]
    second_best_score = candidates[1].score if len(candidates) > 1 else float("-inf")
    absolute_score = max(min((best_candidate.score + 1.0) / 2.0, 1.0), 0.0)
    margin_score = max(min(best_candidate.score - second_best_score, 1.0), 0.0)
    confidence = max(min((absolute_score * 0.7) + (margin_score * 0.3), 1.0), 0.0)
    return AlignmentMatch(offset_seconds=best_candidate.offset_seconds, confidence=confidence)


def _find_alignment_candidates(
    source_features: list[list[float]],
    replacement_features: list[list[float]],
    hop_seconds: float,
    source_samples: list[int],
    replacement_samples: list[int],
    sample_rate: int,
    *,
    max_candidates: int,
) -> list[AlignmentCandidate]:
    source_length = len(source_features)
    max_offset = len(replacement_features) - source_length
    coarse_step = max(1, round(COARSE_SEARCH_STEP_SECONDS / hop_seconds))

    coarse_candidates: list[tuple[float, int]] = []

    for offset in range(0, max_offset + 1, coarse_step):
        score = _score_feature_window(source_features, replacement_features, offset)
        coarse_candidates.append((score, offset))

    coarse_candidates.sort(key=lambda item: item[0], reverse=True)
    coarse_candidates = coarse_candidates[: max_candidates * 3]

    refined_candidates: list[tuple[float, float]] = []
    for _, coarse_offset in coarse_candidates:
        refined_offset_seconds, refined_score = _refine_waveform_match(
            source_samples=source_samples,
            replacement_samples=replacement_samples,
            sample_rate=sample_rate,
            center_offset_seconds=coarse_offset * hop_seconds,
        )
        if any(abs(existing_offset - refined_offset_seconds) <= DEDUP_CANDIDATE_TOLERANCE_SECONDS for _, existing_offset in refined_candidates):
            continue
        refined_candidates.append((refined_score, refined_offset_seconds))

    refined_candidates.sort(key=lambda item: item[0], reverse=True)
    refined_candidates = refined_candidates[:max_candidates]

    if not refined_candidates:
        return []

    best_score = refined_candidates[0][0]
    worst_score = refined_candidates[-1][0]
    score_span = best_score - worst_score
    candidates: list[AlignmentCandidate] = []
    for rank, (score, offset_seconds) in enumerate(refined_candidates):
        if score_span <= 1e-9:
            normalized_score = 1.0
        else:
            normalized_score = (score - worst_score) / score_span
        confidence = max(min((normalized_score * 0.75) + (max(0.0, 1.0 - rank * 0.12) * 0.25), 1.0), 0.0)
        candidates.append(AlignmentCandidate(offset_seconds=offset_seconds, score=score, confidence=confidence))

    return candidates


def _refine_waveform_match(
    *,
    source_samples: list[int],
    replacement_samples: list[int],
    sample_rate: int,
    center_offset_seconds: float,
) -> tuple[float, float]:
    source_signal = _prepare_waveform_signal(source_samples)
    if not source_signal:
        return center_offset_seconds, float("-inf")

    source_length = len(source_samples)
    center_offset_samples = round(center_offset_seconds * sample_rate)
    refine_radius_samples = round(FINE_SEARCH_RADIUS_SECONDS * sample_rate)
    refine_step_samples = max(1, round(WAVEFORM_REFINE_STEP_SECONDS * sample_rate))
    start_offset = max(0, center_offset_samples - refine_radius_samples)
    end_offset = min(len(replacement_samples) - source_length, center_offset_samples + refine_radius_samples)

    best_offset_samples = center_offset_samples
    best_score = float("-inf")
    for offset_samples in range(start_offset, end_offset + 1, refine_step_samples):
        candidate_signal = _prepare_waveform_signal(replacement_samples[offset_samples : offset_samples + source_length])
        score = _cosine_similarity(source_signal, candidate_signal)
        if score > best_score:
            best_score = score
            best_offset_samples = offset_samples

    return best_offset_samples / sample_rate, best_score


def _prepare_waveform_signal(samples: list[int]) -> list[float]:
    if not samples:
        return []

    downsampled = samples[::WAVEFORM_DOWNSAMPLE_FACTOR]
    if len(downsampled) < 2:
        return []

    mean_value = sum(downsampled) / len(downsampled)
    differentiated = [float(current - previous) for previous, current in zip(downsampled, downsampled[1:])]
    mean_diff = sum(differentiated) / len(differentiated)
    centered = [value - mean_diff for value in differentiated]
    magnitude = math.sqrt(sum(value * value for value in centered))
    if magnitude <= 1e-9:
        return [0.0 for _ in centered]
    return [value / magnitude for value in centered]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return float("-inf")

    return sum(left_value * right_value for left_value, right_value in zip(left, right, strict=True))


def _score_feature_window(
    source_features: list[list[float]],
    replacement_features: list[list[float]],
    offset: int,
) -> float:
    total_score = 0.0

    for index, source_feature in enumerate(source_features):
        replacement_feature = replacement_features[offset + index]
        total_score += _cosine_similarity(source_feature, replacement_feature)

    return total_score / len(source_features)


def _load_wav_samples(path: Path) -> tuple[list[int], int]:
    with wave.open(str(path), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        sample_width = wav_file.getsampwidth()
        channel_count = wav_file.getnchannels()
        frame_count = wav_file.getnframes()
        raw_data = wav_file.readframes(frame_count)

    if sample_width != 2:
        raise RuntimeError("Alignment analysis expected 16-bit PCM audio.")
    if channel_count != 1:
        raise RuntimeError("Alignment analysis expected mono audio.")

    sample_count = len(raw_data) // 2
    if sample_count == 0:
        return [], sample_rate

    samples = list(struct.unpack(f"<{sample_count}h", raw_data))
    return samples, sample_rate


def _probe_primary_audio_codec(input_path: str) -> str | None:
    if shutil.which("ffprobe") is None:
        return None

    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        input_path,
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode != 0:
        return None

    codec_name = completed.stdout.strip()
    return codec_name or None


def _has_audio_stream(input_path: str) -> bool:
    return _probe_primary_audio_codec(input_path) is not None


def _is_audio_codec_copy_safe(codec_name: str, container: str) -> bool:
    safe_codecs_by_container = {
        "mp4": {"aac", "mp3", "alac"},
        "mkv": {"aac", "mp3", "opus", "flac", "vorbis", "pcm_s16le", "pcm_s24le", "alac"},
        "webm": {"opus", "vorbis"},
        "mov": {"aac", "mp3", "alac", "pcm_s16le", "pcm_s24le"},
    }
    return codec_name in safe_codecs_by_container[container]


def _run_ffmpeg_command(
    command: list[str],
    *,
    progress_duration_seconds: float | None,
    progress_callback: ProgressCallback | None,
) -> None:
    process = subprocess.Popen(
        [*command[:1], *command[1:2], "-progress", "pipe:1", "-nostats", "-loglevel", "error", *command[2:]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )

    stderr_lines: list[str] = []
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
        if key == "progress" and progress_callback is not None:
            progress_callback(dict(progress_data), _calculate_progress(progress_data, progress_duration_seconds))

    return_code = process.wait()
    stderr_thread.join()
    if return_code != 0:
        error_text = "\n".join(stderr_lines).strip() or "ffmpeg failed without an error message."
        raise RuntimeError(error_text)


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


def _ensure_ffmpeg_available() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is not installed or not available on PATH.")

    if shutil.which("ffprobe") is None:
        raise RuntimeError("ffprobe is not installed or not available on PATH.")


def _ensure_edit_paths(request: EditRequest) -> None:
    if not request.input_path_obj.is_file():
        raise FileNotFoundError(f"Input file not found: {request.input_path_obj}")

    if request.replacement_audio_path_obj is not None and not request.replacement_audio_path_obj.is_file():
        raise FileNotFoundError(f"Replacement audio file not found: {request.replacement_audio_path_obj}")

    request.output_path_obj.parent.mkdir(parents=True, exist_ok=True)
    if request.output_path_obj.exists() and not request.overwrite:
        raise FileExistsError(f"Output file already exists: {request.output_path_obj}")


def _move_output_to_final_path(source_path: Path, final_output_path: Path) -> None:
    if final_output_path.exists():
        final_output_path.unlink()
    shutil.move(str(source_path), str(final_output_path))


def _format_time_value(value: float) -> str:
    return f"{value:.3f}"
