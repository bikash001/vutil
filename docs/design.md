# Video Compression Program Design

## Goal

Build a program that lets a user compress video files by controlling the major compression settings directly, without needing to memorize `ffmpeg` flags.

## Core user features

The program should let users configure:

1. Video codec
   - `h264`
   - `h265`
   - `vp9`
   - `av1`
   - `prores`
   - `ffv1`

2. Compression mode
   - lossy
   - lossless

3. Quality control
   - `CRF` or equivalent quality value
   - target video bitrate
   - optional 2-pass encoding for bitrate-driven output

4. Spatial compression controls
   - output resolution
   - encoder preset
   - pixel format / chroma subsampling such as `yuv420p`, `yuv422p`, `yuv444p`

5. Temporal compression controls
   - frame rate
   - GOP / keyframe interval
   - B-frames toggle or count in advanced mode

6. Audio compression controls
   - audio codec
   - audio bitrate
   - sample rate
   - channel count

7. Container selection
   - `mp4`
   - `mkv`
   - `webm`
   - `mov`

## Product shape

The cleanest design is a layered program:

- `UI layer`
  - CLI for power users and automation
  - optional desktop GUI later
- `Settings model`
  - strongly typed compression settings
- `Validation layer`
  - rejects incompatible combinations
- `Command builder`
  - translates settings into `ffmpeg` arguments
- `Job runner`
  - executes `ffmpeg`, tracks progress, captures logs
- `Preset system`
  - reusable profiles like "small file", "balanced", "archive", "web upload"

## Recommended architecture

### 1. Input model

Create a single `CompressionProfile` object with fields like:

```text
input_path
output_path
container
video_codec
audio_codec
lossless
crf
video_bitrate
audio_bitrate
resolution
fps
pixel_format
preset
gop
sample_rate
audio_channels
```

This becomes the contract between UI and encoding logic.

### 2. Validation rules

Examples:

- `lossless=true` should block incompatible lossy-only settings like CRF-based H.264 presets unless intentionally supported.
- `webm` should prefer `vp9` or `av1`.
- `ffv1` should generally target `mkv`.
- `yuv444p` should warn about playback compatibility.
- `h264` plus `mp4` plus `yuv420p` should be the safe default preset.

### 3. Command builder

Translate the profile into an `ffmpeg` command in a deterministic way.

Examples:

Lossy, quality-driven:

```bash
ffmpeg -i input.mp4 -c:v libx264 -preset medium -crf 23 -vf scale=1280:720,fps=30 -pix_fmt yuv420p -c:a aac -b:a 128k output.mp4
```

Lossy, bitrate-driven:

```bash
ffmpeg -i input.mp4 -c:v libx265 -preset slow -b:v 1800k -vf scale=1920:1080 -pix_fmt yuv420p -c:a aac -b:a 128k output.mp4
```

Lossless:

```bash
ffmpeg -i input.mov -c:v ffv1 -level 3 -c:a flac output.mkv
```

### 4. Job execution

The runner should:

- verify `ffmpeg` exists
- build the command
- execute it
- parse progress from stderr
- emit structured status updates like percentage, fps, speed, ETA

### 5. Presets

Suggested presets:

- `web_small`
  - `h264`, `yuv420p`, `crf=28`, `aac 96k`, 720p
- `balanced`
  - `h264`, `crf=23`, `aac 128k`
- `high_efficiency`
  - `h265` or `av1`, lower bitrate, slower preset
- `archive_lossless`
  - `ffv1`, `flac`, `mkv`
- `editing_intermediate`
  - `prores`, higher bitrate, lower compression

## How each compression setting maps to behavior

### Codec choice

Controls the compression algorithm.

- `h264`: best compatibility
- `h265`: smaller files than H.264 at similar quality
- `vp9`: strong web compression
- `av1`: best efficiency, slowest encode
- `prores`: editing-friendly, not mainly for small size
- `ffv1`: archival lossless

### Lossy vs lossless

- lossy removes information to save space
- lossless preserves full source fidelity

In the program, this should be a top-level mode because it changes the valid codec/output combinations.

### Bitrate vs quality mode

Users should choose one primary strategy:

- `quality mode`: CRF/CQ style control, better for general use
- `bitrate mode`: target a file size or streaming budget

The app should prevent conflicting choices unless advanced mode is enabled.

### Resolution

Lower resolution means fewer pixels per frame, which reduces size before encoding even starts.

### Frame rate

Lower frame rate reduces temporal information. This can be useful for screen recordings, lectures, or bandwidth-constrained delivery.

### Chroma subsampling

This is exposed through pixel formats:

- `yuv420p`: smallest and most compatible
- `yuv422p`: better color fidelity
- `yuv444p`: highest color detail, larger files

### Preset / speed

Faster presets produce larger files for the same quality target. Slower presets usually produce smaller output.

### Audio compression

The app should not ignore audio because it can meaningfully affect total output size.

## UI recommendation

If building a GUI later, use:

- Basic mode
  - file picker
  - output format
  - quality slider
  - resolution dropdown
  - frame rate dropdown
  - audio quality dropdown
- Advanced mode
  - codec selector
  - CRF / bitrate inputs
  - pixel format
  - GOP
  - preset
  - audio sample rate and channels

## Suggested roadmap

1. CLI that builds valid commands from settings
2. Runner that executes `ffmpeg` and reports progress
3. Preset library
4. Batch processing
5. Desktop UI
6. Hardware acceleration options

## Good default choices

For a safe default "compress this video" action:

- container: `mp4`
- video codec: `h264`
- quality mode: `crf=23`
- preset: `medium`
- resolution: keep original unless user changes it
- frame rate: keep original unless user changes it
- pixel format: `yuv420p`
- audio codec: `aac`
- audio bitrate: `128k`

## Why `ffmpeg`

This program should use `ffmpeg` as the encoding backend because it already supports nearly every practical codec and filter pipeline needed for these settings. The program's value is the settings model, validation, profiles, and user experience on top of that engine.
