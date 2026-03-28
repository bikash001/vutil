from __future__ import annotations

from typing import List

from vutil.models import CompressionProfile


VIDEO_CODEC_ARGS = {
    "h264": ["-c:v", "libx264"],
    "h265": ["-c:v", "libx265"],
    "vp9": ["-c:v", "libvpx-vp9"],
    "av1": ["-c:v", "libaom-av1"],
    "prores": ["-c:v", "prores_ks"],
    "ffv1": ["-c:v", "ffv1"],
}

LOSSLESS_AUDIO_CODEC_ARGS = {
    "aac": ["-c:a", "aac"],
    "opus": ["-c:a", "libopus"],
    "mp3": ["-c:a", "libmp3lame"],
    "flac": ["-c:a", "flac"],
    "copy": ["-c:a", "copy"],
}

VPX_SPEED_BY_PRESET = {
    "ultrafast": "8",
    "superfast": "8",
    "veryfast": "7",
    "faster": "6",
    "fast": "5",
    "medium": "4",
    "slow": "2",
    "slower": "1",
    "veryslow": "0",
}

def build_ffmpeg_command(
    profile: CompressionProfile,
    *,
    progress: bool = False,
) -> list[str]:
    profile.validate()

    command: List[str] = ["ffmpeg", "-hide_banner", "-y" if profile.overwrite else "-n"]
    if progress:
        command.extend(["-progress", "pipe:1", "-nostats", "-loglevel", "error"])
    command.extend(["-i", profile.input_path])
    if profile.trim_start is not None:
        command.extend(["-ss", _format_time_value(profile.trim_start)])
    if profile.trim_duration is not None:
        command.extend(["-t", _format_time_value(profile.trim_duration)])
    command.extend(["-threads", str(profile.threads)])
    command.extend(VIDEO_CODEC_ARGS[profile.video_codec])

    _append_video_options(command, profile)

    filters: list[str] = []
    if profile.resolution:
        width, height = _parse_resolution(profile.resolution)
        filters.append(f"scale={width}:{height}")
    if profile.fps:
        filters.append(f"fps={profile.fps}")
    if filters:
        command.extend(["-vf", ",".join(filters)])

    command.extend(["-pix_fmt", profile.pixel_format])

    if profile.gop:
        command.extend(["-g", str(profile.gop)])

    if profile.disable_audio:
        command.append("-an")
    else:
        command.extend(LOSSLESS_AUDIO_CODEC_ARGS[profile.audio_codec])
        if profile.audio_codec in {"aac", "opus", "mp3"} and profile.audio_bitrate:
            command.extend(["-b:a", profile.audio_bitrate])
        if profile.sample_rate:
            command.extend(["-ar", str(profile.sample_rate)])
        if profile.audio_channels:
            command.extend(["-ac", str(profile.audio_channels)])

    command.append(profile.output_path)
    return command


def _append_video_options(command: list[str], profile: CompressionProfile) -> None:
    codec = profile.video_codec

    if codec in {"h264", "h265"}:
        if profile.lossless:
            command.extend(["-preset", profile.preset, "-qp", "0"])
            return

        command.extend(["-preset", profile.preset])
        if profile.crf is not None:
            command.extend(["-crf", str(profile.crf)])
        elif profile.video_bitrate is not None:
            command.extend(["-b:v", profile.video_bitrate])
        return

    if codec in {"vp9", "av1"}:
        speed = VPX_SPEED_BY_PRESET[profile.preset]
        if codec == "vp9":
            command.extend(["-deadline", "good", "-cpu-used", speed, "-row-mt", "1"])
        else:
            command.extend(["-cpu-used", speed, "-row-mt", "1"])

        if profile.lossless:
            if codec == "vp9":
                command.extend(["-lossless", "1"])
            else:
                command.extend(["-crf", "0", "-b:v", "0"])
            return

        if profile.crf is not None:
            command.extend(["-crf", str(profile.crf), "-b:v", "0"])
        elif profile.video_bitrate is not None:
            command.extend(["-b:v", profile.video_bitrate])
        return

    if codec == "ffv1":
        command.extend(["-level", "3"])
        return

    if codec == "prores":
        command.extend(["-profile:v", "3"])
        return


def _parse_resolution(value: str) -> tuple[int, int]:
    try:
        width_str, height_str = value.lower().split("x", maxsplit=1)
        width = int(width_str)
        height = int(height_str)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid resolution '{value}'. Expected WIDTHxHEIGHT.") from exc

    if width <= 0 or height <= 0:
        raise ValueError("Resolution values must be positive integers.")

    return width, height


def _format_time_value(value: float) -> str:
    return f"{value:.3f}"
