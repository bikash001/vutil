# Video Editing Feature Design

## Goal

Add a lightweight editing workflow to `vutil` that supports:

1. time trimming with user-provided start and end times
2. replacing a video's audio with an external audio file
3. aligning the replacement audio automatically to the trimmed video segment
4. doing trim and/or audio replacement without requiring compression

This feature should work as a standalone editing mode and should also compose cleanly with the existing compression flow when the user wants both editing and compression in one command.

## Core requirements

- The user can specify `start` and `end` timestamps.
- The tool trims the input video to that time range.
- The user can optionally provide an external audio file.
- If external audio is provided, the tool finds the portion of that audio that best matches the trimmed video's original audio.
- The tool replaces the video's audio with the aligned segment from the external audio.
- The user must be able to do this without compression as the primary workflow.

## Product shape

The editing feature should be modeled as a separate pipeline from compression:

- `edit-only mode`
  - trim and/or audio replacement
  - preserve original streams when possible
- `edit + compress mode`
  - run editing first
  - then feed the edited asset into the existing compression pipeline

This keeps the mental model clear:

- editing changes content and timing
- compression changes encoding characteristics

## User-facing operations

The tool should support these cases:

1. `trim only`
   - output a subclip between `start` and `end`

2. `replace audio only`
   - keep full video duration
   - align external audio to the original video audio
   - swap the audio track

3. `trim + replace audio`
   - trim the video
   - align external audio to the trimmed segment
   - replace the trimmed segment's audio

4. `trim and/or replace audio, then compress`
   - same as above, followed by the current compression flow

## Proposed CLI shape

These flags are enough to express the feature cleanly:

- `--start TIME`
  - start timestamp of the desired output segment
- `--end TIME`
  - end timestamp of the desired output segment
- `--replace-audio PATH`
  - external audio source to use
- `--align-audio`
  - enable automatic alignment between the video's original audio and the external audio
- `--audio-offset TIME`
  - optional manual override for external audio start offset
- `--edit-only`
  - perform trim and/or audio replacement without compression
- `--trim-mode {smart,copy,exact}`
  - policy for how trim behaves when stream-copy and exact boundaries disagree
- `--on-align-fail {error,keep-original,use-start,use-offset}`
  - behavior when alignment confidence is too low

Example intentions:

```bash
vutil input.mp4 output.mp4 --start 00:01:10 --end 00:01:42 --edit-only
```

```bash
vutil input.mp4 output.mp4 --start 00:01:10 --end 00:01:42 --replace-audio dub.wav --align-audio --edit-only
```

```bash
vutil input.mp4 output.mp4 --start 00:01:10 --end 00:01:42 --replace-audio dub.wav --align-audio --max-size-mb 25
```

## Editing modes

### 1. Edit-only mode

This is the new primary mode for the requested feature.

Behavior:

- no deliberate video compression
- no automatic quality changes
- no CRF or bitrate search
- prefer stream copy whenever possible

This mode should preserve the original video stream unless the requested operation makes pure stream copy impossible or inaccurate.

### 2. Edit + compress mode

If the user combines editing options with compression options:

1. perform edit planning
2. produce an intermediate edited asset or filter graph
3. run the existing compression pipeline on the edited result

This lets one command express:

- content selection
- audio replacement
- final size or quality goals

## Architectural changes

The current architecture is compression-centric. The new feature should introduce an editing layer above encoding.

Recommended layers:

- `CLI layer`
  - parse edit and compression options
- `EditRequest`
  - strongly typed edit settings
- `Edit planner`
  - decide whether stream copy, remux, or re-encode is required
- `Audio alignment engine`
  - detect offset and confidence
- `Edit executor`
  - run trimming, audio extraction, alignment, and muxing
- `Compression pipeline`
  - optional final stage only if the user requests compression

## Data model changes

Add a dedicated edit model instead of overloading `CompressionProfile`.

Example:

```text
EditRequest
  input_video_path
  output_path
  start_time
  end_time
  replacement_audio_path
  auto_align_audio
  manual_audio_offset
  edit_only
  trim_mode
  on_align_fail
```

And an execution result:

```text
EditResult
  output_path
  duration_seconds
  video_stream_copied
  audio_stream_copied
  alignment_offset_seconds
  alignment_confidence
  warnings[]
```

If compression is also requested, `EditResult` becomes the input to the compression stage.

## Trim design

### Inputs

- `start`
- `end`

Validation rules:

- `start >= 0`
- `end > start`
- `end <= input duration`, unless relaxed to clamp automatically

### Trim strategies

#### `copy`

Use stream copy where possible.

Pros:

- fastest
- no quality loss

Cons:

- trim points may land on nearby keyframes rather than exact frame boundaries
- output may start slightly earlier or later than requested

#### `exact`

Aim for exact trim boundaries.

Pros:

- precise timestamps

Cons:

- may require re-encoding the video stream

#### `smart`

Default policy.

Behavior:

- try copy-based trim first
- if the result is acceptably close to requested boundaries, keep it
- otherwise switch only the necessary parts to a more accurate path

This gives the best user experience because it preserves quality when possible and only pays extra cost when needed.

## Audio replacement design

### Inputs

- original video audio
- optional trim window
- replacement audio file

### Expected behavior

The replacement audio will usually contain the same spoken words or music as the original audio, but it may not be sample-identical.

So the system should align by similarity, not exact match.

### Alignment pipeline

#### Stage 1: normalize analysis inputs

Create temporary analysis versions of:

- the source video audio for the relevant time window
- the full replacement audio

Normalization for analysis:

- mono
- fixed sample rate
- PCM waveform
- optional loudness normalization or mean-volume normalization

#### Stage 2: coarse candidate search

Compute robust features for both audio inputs.

Good options:

- log-mel spectrogram
- MFCC features
- chroma-like features for music-heavy content
- simple energy envelope as a fallback

Then slide the source window over the replacement audio and score likely offsets.

Output:

- top candidate offsets
- rough similarity score

#### Stage 3: fine alignment

Around the best coarse candidate, run a finer-grained comparison using:

- spectrogram cross-correlation
- envelope correlation
- optional dynamic time warping if needed later

Output:

- `best_offset_seconds`
- `confidence_score`

### Alignment policy

Alignment should not silently replace audio if confidence is weak.

Recommended default:

- if confidence is high enough, use the aligned segment
- if confidence is low, fail with a clear message

Optional fallback policies:

- `keep-original`
- `use-start`
  - start from the beginning of the replacement audio
- `use-offset`
  - use the manual offset supplied by the user

## Muxing design

After alignment:

1. trim or select the video segment
2. trim the replacement audio to the same output duration
3. mux video + new audio into the output container

### Preferred stream handling

#### Video

Prefer stream copy in edit-only mode.

Only re-encode video if:

- the trim policy demands exact frame-accurate cutting
- the chosen container or pipeline cannot preserve the stream correctly

#### Audio

If the external audio codec is compatible with the output container and trim boundaries are acceptable, copy it.

Otherwise:

- re-encode audio only
- keep video copied if possible

This still satisfies the spirit of "without compressing the file" much better than re-encoding the video stream.

## Execution pipeline

### Case A: trim only, edit-only

1. probe video
2. validate time range
3. choose trim strategy
4. produce output clip

### Case B: replace audio only, edit-only

1. probe video and external audio
2. extract source audio for full video duration
3. align source audio against replacement audio
4. cut aligned external audio segment
5. mux original video stream with new audio

### Case C: trim + replace audio, edit-only

1. probe video and external audio
2. determine trim window
3. extract source audio from trimmed region
4. align trimmed source audio against replacement audio
5. trim video
6. cut matching external audio segment to trimmed duration
7. mux trimmed video with aligned replacement audio

### Case D: edit + compress

1. complete one of the edit-only flows above
2. pass the edited output into the existing compression pipeline

## Temporary file strategy

The implementation will likely need temporary files for:

- extracted source audio
- normalized analysis audio
- trimmed aligned replacement audio
- optional intermediate edited video

Guidelines:

- use a dedicated temporary work directory per run
- clean up on success
- keep files on failure only in debug mode

## Reporting and output

The tool should report edit decisions clearly.

Useful structured output:

- requested trim range
- actual output duration
- whether video was copied or re-encoded
- whether audio was copied or re-encoded
- alignment offset
- alignment confidence
- warnings if trim drifted to keyframe boundaries

Example messages:

- `Trimmed 00:01:10.000 to 00:01:42.000`
- `Video stream copied`
- `Audio replaced from external source at offset 00:14:03.420`
- `Alignment confidence: 0.91`
- `Warning: exact trim required re-encoding video`

## Validation and failure cases

The system should fail early for:

- missing input video
- invalid start/end timestamps
- replacement audio not found
- replacement audio shorter than required segment after alignment
- no audio stream in source video when auto-align is requested

The system should fail with detailed context for:

- alignment confidence below threshold
- no plausible matching audio segment found
- container/codec incompatibility during muxing

## Why not fold this directly into `CompressionProfile`

Editing and compression are different responsibilities:

- editing decides what content and timing go into the result
- compression decides how that result is encoded

Keeping a dedicated edit model makes validation, planning, and user messaging much easier.

## Recommended implementation order

1. Add `EditRequest` and CLI parsing for `start`, `end`, and `replace-audio`
2. Add edit-only trim workflow
3. Add audio extraction and manual-offset audio replacement
4. Add automatic audio alignment with confidence scoring
5. Add edit + compress composition
6. Add richer fallback policies and debug output

## Summary

The clean design is to add an editing pipeline that can operate independently from compression.

Default behavior should be:

- preserve video without compression whenever possible
- align external audio by similarity, not exact waveform equality
- replace audio only when confidence is acceptable
- allow the same edit plan to feed either an edit-only output or the existing compression flow

This keeps the new feature flexible while matching the requirement that trimming and audio replacement should not require compression.
