from __future__ import annotations

import argparse
import shlex
import sys
import tempfile
from pathlib import Path

from vutil.auto_compress import (
    AutoCompressionResult,
    build_auto_request,
    choose_auto_audio_codec,
    choose_auto_video_codec,
    compress_with_max_size,
)
from vutil.editing import run_edit
from vutil.ffmpeg_builder import build_ffmpeg_command
from vutil.models import CompressionProfile, EditRequest, EditResult, default_thread_count
from vutil.runner import CompressionResult, run_compression


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compress a video file using high-level settings backed by ffmpeg."
    )
    parser.add_argument("input_path")
    parser.add_argument("output_path")
    parser.add_argument("--start", help="Trim start time. Supports seconds or HH:MM:SS(.mmm).")
    parser.add_argument("--end", help="Trim end time. Supports seconds or HH:MM:SS(.mmm).")
    parser.add_argument("--replace-audio", help="Replace the video's audio with the given external audio file.")
    parser.add_argument("--audio-offset", help="Manual replacement-audio offset in seconds or HH:MM:SS(.mmm).")
    parser.add_argument("--edit-only", action="store_true", help="Run trim/audio replacement without video compression.")
    parser.add_argument(
        "--trim-mode",
        choices=["smart", "copy", "exact"],
        default="smart",
        help="Trim strategy: copy for fast keyframe-based cuts, exact for frame/sample-accurate cuts, smart to choose automatically.",
    )
    parser.add_argument("--container", choices=["mp4", "mkv", "webm", "mov"], default="mp4")
    parser.add_argument("--codec", choices=["h264", "h265", "vp9", "av1", "prores", "ffv1"])
    parser.add_argument("--audio-codec", choices=["aac", "opus", "mp3", "flac", "copy"])
    parser.add_argument("--lossless", action="store_true")
    parser.add_argument("--crf", type=int, help="Quality target for codecs that support CRF-style encoding.")
    parser.add_argument("--video-bitrate")
    parser.add_argument(
        "--max-size-mb",
        type=float,
        help="Maximum output size in MB. The tool will search for the highest quality result under this cap.",
    )
    parser.add_argument("--audio-bitrate")
    parser.add_argument("--resolution", help="Format: WIDTHxHEIGHT")
    parser.add_argument("--fps", type=int)
    parser.add_argument("--pixel-format", choices=["yuv420p", "yuv422p", "yuv444p"], default="yuv420p")
    parser.add_argument(
        "--preset",
        choices=[
            "ultrafast",
            "superfast",
            "veryfast",
            "faster",
            "fast",
            "medium",
            "slow",
            "slower",
            "veryslow",
        ],
        default="medium",
    )
    parser.add_argument("--gop", type=int)
    parser.add_argument("--sample-rate", type=int)
    parser.add_argument("--audio-channels", type=int)
    parser.add_argument("--threads", type=int, help="Override encoder thread count. Default is CPU count minus 2.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite the output file if it already exists.")
    parser.add_argument("--dry-run", action="store_true", help="Print the ffmpeg command without executing it.")
    parser.add_argument("--show-command", action="store_true", help="Print the ffmpeg command before execution.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        _validate_cli_options(args, parser)

        if _has_edit_request(args):
            return _run_edit_pipeline(args)

        if args.max_size_mb is not None:
            result = _run_auto_mode(args)
            _print_auto_summary(result, args.output_path)
            return 1 if result.size_cap_exceeded else 0

        codec = args.codec or "h264"
        crf = _resolve_crf(codec, args.crf, args.video_bitrate, args.lossless)
        audio_codec = _resolve_audio_codec(args.container, args.audio_codec)
        audio_bitrate = _resolve_audio_bitrate(audio_codec, args.audio_bitrate)
        threads = _resolve_threads(args.threads)

        profile = CompressionProfile(
            input_path=args.input_path,
            output_path=args.output_path,
            container=args.container,
            video_codec=codec,
            audio_codec=audio_codec,
            lossless=args.lossless,
            crf=crf,
            video_bitrate=args.video_bitrate,
            audio_bitrate=audio_bitrate,
            resolution=args.resolution,
            fps=args.fps,
            pixel_format=args.pixel_format,
            preset=args.preset,
            gop=args.gop,
            sample_rate=args.sample_rate,
            audio_channels=args.audio_channels,
            threads=threads,
            overwrite=args.overwrite,
        )

        if args.dry_run:
            command = build_ffmpeg_command(profile)
            print(shlex.join(command))
            return 0

        if args.show_command:
            command = build_ffmpeg_command(profile)
            print(shlex.join(command))

        result = run_compression(profile, progress_callback=_print_progress)
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    except (FileNotFoundError, FileExistsError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    _print_summary(result, profile.output_path)
    return 0


def _print_progress(progress_data: dict[str, str], percentage: float | None) -> None:
    progress_state = progress_data.get("progress", "continue")
    if progress_state == "end":
        print("Progress: 100.0%")
        return

    encoded_time = progress_data.get("out_time", "00:00:00.00")
    speed = progress_data.get("speed", "n/a")
    if percentage is None:
        print(f"Progress: encoded={encoded_time}, speed={speed}")
        return

    print(f"Progress: {percentage:5.1f}% | encoded={encoded_time} | speed={speed}")


def _print_summary(result: CompressionResult, output_path: str) -> None:
    input_size = _format_bytes(result.input_size_bytes)
    output_size = _format_bytes(result.output_size_bytes)
    saved = _format_bytes(abs(result.bytes_saved))
    ratio = result.compression_ratio

    print(f"Finished: {Path(output_path)}")
    print(f"Input size:  {input_size}")
    print(f"Output size: {output_size}")
    if result.bytes_saved >= 0:
        print(f"Saved:      {saved}")
    else:
        print(f"Grew by:    {saved}")
    if ratio is not None:
        print(f"Ratio:      {ratio:.2%} of original")


def _print_auto_summary(result: AutoCompressionResult, output_path: str) -> None:
    _print_summary(result.result, output_path)
    print(f"Selected CRF: {result.selected_crf}")
    print(f"Attempts:     {result.attempts}")
    print(f"Sampled:      {result.sampled_seconds:.1f}s")
    print(f"Size cap:     {_format_bytes(result.max_size_bytes)}")
    if result.size_cap_exceeded:
        _print_size_cap_warning(result, output_path)


def _print_edit_summary(result: EditResult, output_path: str) -> None:
    _print_summary(result, output_path)
    print(f"Video stream copied: {'yes' if result.video_stream_copied else 'no'}")
    print(f"Audio stream copied: {'yes' if result.audio_stream_copied else 'no'}")
    if result.alignment_offset_seconds is not None:
        print(f"Aligned audio offset: {result.alignment_offset_seconds:.3f}s")
    if result.alignment_confidence is not None:
        print(f"Alignment confidence: {result.alignment_confidence:.2f}")
    for warning in result.warnings:
        print(f"Warning: {warning}")


def _format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _print_size_cap_warning(result: AutoCompressionResult, output_path: str) -> None:
    profile = result.final_profile
    actual_size = _format_bytes(result.result.output_size_bytes)
    cap_size = _format_bytes(result.max_size_bytes)
    overshoot = _format_bytes(result.result.output_size_bytes - result.max_size_bytes)
    approximate_bitrate = _format_output_bitrate(result.result.output_size_bytes, result.result.duration_seconds)
    resolution = profile.resolution or "source"
    fps = str(profile.fps) if profile.fps is not None else "source"
    audio_bitrate = profile.audio_bitrate or "source/copy"

    print(
        (
            f"Error: output exceeds the requested size cap, but the file was kept at {Path(output_path)}. "
            f"Actual {actual_size} vs cap {cap_size} ({overshoot} over)."
        ),
        file=sys.stderr,
    )
    print(
        (
            "Final settings: "
            f"container={profile.container}, "
            f"video_codec={profile.video_codec}, "
            f"crf={profile.crf}, "
            f"audio_codec={profile.audio_codec}, "
            f"audio_bitrate={audio_bitrate}, "
            f"resolution={resolution}, "
            f"fps={fps}, "
            f"pixel_format={profile.pixel_format}, "
            f"preset={profile.preset}, "
            f"threads={profile.threads}, "
            f"approx_output_bitrate={approximate_bitrate}"
        ),
        file=sys.stderr,
    )


def _resolve_crf(codec: str, explicit_crf: int | None, video_bitrate: str | None, lossless: bool) -> int | None:
    if video_bitrate or lossless:
        return None

    if explicit_crf is not None:
        return explicit_crf

    if codec in {"h264", "h265", "vp9", "av1"}:
        return 23

    return None


def _resolve_audio_codec(container: str, explicit_audio_codec: str | None) -> str:
    if explicit_audio_codec is not None:
        return explicit_audio_codec

    if container == "webm":
        return "opus"

    return "aac"


def _resolve_audio_bitrate(audio_codec: str, explicit_audio_bitrate: str | None) -> str | None:
    if audio_codec == "copy":
        return None

    if explicit_audio_bitrate is not None:
        return explicit_audio_bitrate

    if audio_codec == "opus":
        return "96k"

    return "128k"


def _resolve_threads(explicit_threads: int | None) -> int:
    if explicit_threads is not None:
        return explicit_threads

    return default_thread_count()


def _format_output_bitrate(size_bytes: int, duration_seconds: float | None) -> str:
    if duration_seconds is None or duration_seconds <= 0:
        return "unknown"

    bitrate_kbps = (size_bytes * 8) / duration_seconds / 1000
    return f"{bitrate_kbps:.1f} kbps"


def _validate_cli_options(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if _has_edit_request(args) and args.dry_run:
        parser.error("--dry-run is not supported with trim or audio replacement.")

    if args.edit_only and _has_video_compression_request(args):
        parser.error("--edit-only cannot be combined with video compression options.")

    if args.max_size_mb is None:
        return

    if args.crf is not None:
        parser.error("Do not combine --max-size-mb with --crf. Auto mode chooses CRF for you.")

    if args.video_bitrate is not None:
        parser.error("Do not combine --max-size-mb with --video-bitrate. Auto mode is quality-first.")

    if args.lossless:
        parser.error("Do not combine --max-size-mb with --lossless.")

    if args.dry_run:
        parser.error("--dry-run is not supported with --max-size-mb because auto mode must encode trial outputs.")


def _run_auto_mode(args: argparse.Namespace, *, input_path: str | None = None) -> AutoCompressionResult:
    codec = choose_auto_video_codec(args.container, args.codec)
    audio_codec = choose_auto_audio_codec(args.container, args.audio_codec)
    request = build_auto_request(
        input_path=input_path or args.input_path,
        output_path=args.output_path,
        max_size_mb=args.max_size_mb,
        container=args.container,
        video_codec=codec,
        audio_codec=audio_codec,
        audio_bitrate=None if audio_codec == "copy" else args.audio_bitrate,
        resolution=args.resolution,
        fps=args.fps,
        pixel_format=args.pixel_format,
        preset=args.preset,
        gop=args.gop,
        sample_rate=args.sample_rate,
        audio_channels=args.audio_channels,
        threads=_resolve_threads(args.threads),
        overwrite=args.overwrite,
    )

    print(f"Auto mode: size cap {_format_bytes(request.max_size_bytes)}, codec={request.video_codec}, audio={request.audio_codec}")

    def on_attempt(attempt_number: int, crf: int, profile: CompressionProfile) -> None:
        print(f"Sample probe {attempt_number}: estimating CRF {crf}")
        if args.show_command:
            print(shlex.join(build_ffmpeg_command(profile)))

    def on_final_encode(crf: int, profile: CompressionProfile) -> None:
        print(f"Final encode: full file at CRF {crf}")
        if args.show_command:
            print(shlex.join(build_ffmpeg_command(profile)))

    return compress_with_max_size(
        request,
        progress_callback=_print_progress,
        attempt_callback=on_attempt,
        final_encode_callback=on_final_encode,
    )


def _run_edit_pipeline(args: argparse.Namespace) -> int:
    if args.edit_only or not _has_video_compression_request(args):
        result = _run_edit_only(args)
        _print_edit_summary(result, args.output_path)
        return 0

    with tempfile.TemporaryDirectory(prefix="vutil-edit-stage-") as tmp_dir:
        intermediate_path = Path(tmp_dir) / f"edited-input.{args.container}"
        edit_result = _run_edit_only(
            args,
            output_path=str(intermediate_path),
            overwrite=True,
            audio_codec=None,
            audio_bitrate=None,
            sample_rate=None,
            audio_channels=None,
        )

        print(f"Edit stage complete: {intermediate_path}")
        if edit_result.alignment_offset_seconds is not None:
            print(f"Edit alignment offset: {edit_result.alignment_offset_seconds:.3f}s")
        if edit_result.alignment_confidence is not None:
            print(f"Edit alignment confidence: {edit_result.alignment_confidence:.2f}")

        if args.max_size_mb is not None:
            result = _run_auto_mode(args, input_path=str(intermediate_path))
            _print_auto_summary(result, args.output_path)
            return 1 if result.size_cap_exceeded else 0

        codec = args.codec or "h264"
        crf = _resolve_crf(codec, args.crf, args.video_bitrate, args.lossless)
        audio_codec = _resolve_audio_codec(args.container, args.audio_codec)
        audio_bitrate = _resolve_audio_bitrate(audio_codec, args.audio_bitrate)
        profile = CompressionProfile(
            input_path=str(intermediate_path),
            output_path=args.output_path,
            container=args.container,
            video_codec=codec,
            audio_codec=audio_codec,
            lossless=args.lossless,
            crf=crf,
            video_bitrate=args.video_bitrate,
            audio_bitrate=audio_bitrate,
            resolution=args.resolution,
            fps=args.fps,
            pixel_format=args.pixel_format,
            preset=args.preset,
            gop=args.gop,
            sample_rate=args.sample_rate,
            audio_channels=args.audio_channels,
            threads=_resolve_threads(args.threads),
            overwrite=args.overwrite,
        )

        if args.show_command:
            print(shlex.join(build_ffmpeg_command(profile)))

        result = run_compression(profile, progress_callback=_print_progress)
        _print_summary(result, args.output_path)
        return 0


def _run_edit_only(
    args: argparse.Namespace,
    *,
    output_path: str | None = None,
    overwrite: bool | None = None,
    audio_codec: str | None = None,
    audio_bitrate: str | None = None,
    sample_rate: int | None = None,
    audio_channels: int | None = None,
) -> EditResult:
    request = EditRequest(
        input_path=args.input_path,
        output_path=output_path or args.output_path,
        container=args.container,
        start_time=_parse_time_argument(args.start, "start"),
        end_time=_parse_time_argument(args.end, "end"),
        replacement_audio_path=args.replace_audio,
        audio_offset=_parse_time_argument(args.audio_offset, "audio offset"),
        trim_mode=args.trim_mode,
        audio_codec=audio_codec if audio_codec is not None else args.audio_codec,
        audio_bitrate=audio_bitrate if audio_bitrate is not None else args.audio_bitrate,
        sample_rate=sample_rate if sample_rate is not None else args.sample_rate,
        audio_channels=audio_channels if audio_channels is not None else args.audio_channels,
        overwrite=args.overwrite if overwrite is None else overwrite,
    )

    def on_command(command: list[str]) -> None:
        if args.show_command:
            print(shlex.join(command))

    return run_edit(
        request,
        progress_callback=_print_progress,
        command_callback=on_command,
    )


def _has_edit_request(args: argparse.Namespace) -> bool:
    return args.start is not None or args.end is not None or args.replace_audio is not None


def _has_video_compression_request(args: argparse.Namespace) -> bool:
    return any(
        [
            args.codec is not None,
            args.lossless,
            args.crf is not None,
            args.video_bitrate is not None,
            args.max_size_mb is not None,
            args.resolution is not None,
            args.fps is not None,
            args.pixel_format != "yuv420p",
            args.preset != "medium",
            args.gop is not None,
        ]
    )


def _parse_time_argument(value: str | None, label: str) -> float | None:
    if value is None:
        return None

    text = value.strip()
    if not text:
        return None

    try:
        return float(text)
    except ValueError:
        pass

    parts = text.split(":")
    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise ValueError(f"Invalid {label} value '{value}'. Use seconds or HH:MM:SS(.mmm).") from exc

    if len(numbers) == 1:
        return numbers[0]
    if len(numbers) == 2:
        minutes, seconds = numbers
        return (minutes * 60.0) + seconds
    if len(numbers) == 3:
        hours, minutes, seconds = numbers
        return (hours * 3600.0) + (minutes * 60.0) + seconds

    raise ValueError(f"Invalid {label} value '{value}'. Use seconds or HH:MM:SS(.mmm).")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
