# vutil

`vutil` is a real video-compression CLI built around `ffmpeg`.

It is designed to expose the main compression controls in one place:

- video codec
- lossy vs lossless mode
- bitrate / quality control
- resolution
- frame rate
- chroma subsampling / pixel format
- encoder preset
- audio codec and bitrate
- container format

This repository currently includes:

- a buildable design document in [`docs/design.md`](/home/bikash/work/vutil/docs/design.md)
- a Python CLI that validates settings, runs `ffmpeg`, shows progress, and prints a size summary

## Quick start

```bash
PYTHONPATH=src python3 -m vutil.cli input.mp4 output.mp4 --codec h265 --crf 24 --resolution 1280x720 --fps 30
```

Example with explicit bitrate:

```bash
PYTHONPATH=src python3 -m vutil.cli input.mov output.mp4 --codec h264 --video-bitrate 2500k --audio-bitrate 128k
```

Quality-first with a maximum size cap:

```bash
PYTHONPATH=src python3 -m vutil.cli input.mp4 output.mp4 --max-size-mb 25
```

Quality-first with a size cap and fixed resolution:

```bash
PYTHONPATH=src python3 -m vutil.cli input.mp4 output.mp4 --max-size-mb 25 --resolution 1280x720
```

Preview the exact `ffmpeg` command without running it:

```bash
PYTHONPATH=src python3 -m vutil.cli input.mp4 output.webm --codec vp9 --container webm --dry-run
```

Example lossless:

```bash
PYTHONPATH=src python3 -m vutil.cli input.mov output.mkv --codec ffv1 --container mkv --audio-codec flac --lossless
```

Useful flags:

- `--dry-run` prints the generated command without executing it
- `--show-command` prints the command and then runs it
- `--overwrite` replaces an existing output file
- `--max-size-mb` searches for the best-quality encode that stays under the size cap
- `--threads` overrides the default encoder thread count

Notes:

- execution is real by default
- with `--max-size-mb`, size is treated as a ceiling, not a fixed target
- with `--max-size-mb`, auto mode predicts CRF from a few short sample chunks, 2 seconds each by default, before doing one final full encode
- by default, encoding uses `CPU count - 2` threads, with a minimum of `1`
- `--threads` lets you override that default for both normal mode and auto mode
- WebM defaults to `Opus` audio so the container stays valid
- the CLI will block incompatible combinations like `WebM + AAC`

## CLI options

- `input_path`: source video file to read.
- `output_path`: destination file to write.
- `--container {mp4,mkv,webm,mov}`: output container format. Default: `mp4`.
- `--codec {h264,h265,vp9,av1,prores,ffv1}`: video codec to use. Default: `h264` in manual mode, auto-selected in max-size mode unless provided.
- `--audio-codec {aac,opus,mp3,flac,copy}`: audio codec to use. Default: `aac`, or `opus` for `webm`.
- `--lossless`: enable lossless encoding for supported codecs.
- `--crf N`: set CRF quality level for CRF-based codecs. Lower means higher quality and larger files.
- `--video-bitrate RATE`: set a target video bitrate such as `2500k` instead of using CRF.
- `--max-size-mb N`: quality-first auto mode. Tries to stay under this size cap in MB.
- `--audio-bitrate RATE`: set audio bitrate such as `128k`.
- `--resolution WIDTHxHEIGHT`: resize output video, for example `1280x720`.
- `--fps N`: change output frame rate.
- `--pixel-format {yuv420p,yuv422p,yuv444p}`: choose output pixel format / chroma subsampling. Default: `yuv420p`.
- `--preset {ultrafast,superfast,veryfast,faster,fast,medium,slow,slower,veryslow}`: encoder speed/efficiency tradeoff. Default: `medium`.
- `--gop N`: set keyframe interval in frames.
- `--sample-rate N`: set audio sample rate in Hz.
- `--audio-channels N`: set output audio channel count.
- `--threads N`: override encoder thread count. Default: `CPU count - 2`, minimum `1`.
- `--overwrite`: replace the output file if it already exists.
- `--dry-run`: print the generated `ffmpeg` command without running it. Not supported with `--max-size-mb`.
- `--show-command`: print the `ffmpeg` command before running it.
