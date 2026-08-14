# SeamlessLoopMaker

Automatically turn a short video into a smoother seamless loop while preserving as much of the original clip as possible.

The tool is intended especially for short AI-generated background animations such as waves, fire, fog, light particles, abstract motion, and projection backgrounds.

## Key behavior

The default strategy is **preserve-first** instead of aggressively searching for a shorter sub-clip:

1. Keep the entire source span and try only a head/tail overlap transition.
2. If that is not motion-balanced enough, trim the smallest possible number of source frames.
3. Only if a small trim still cannot produce a good seam, use the broader SSIM + optical-flow fallback search.
4. Render the result and verify the actual encoded loop boundary.
5. If encoding makes the seam worse than expected, automatically retry once with stricter seam limits and keep the better result.

This prevents a slightly higher analysis score from unnecessarily shortening an already-good 10-second AI animation.

## Features

- Preserve-first selection: no source trim when the original span can already loop well.
- Minimal-trim second stage: searches by total trim count and stops at the first acceptable level.
- Automatic transition duration by default, testing several values from about 0.25 to 0.75 seconds.
- Transition scoring based on frame-to-frame motion cadence rather than only static frame similarity.
- SSIM and Farneback optical flow in the fallback search.
- Low-motion / near-static section penalty in fallback search.
- Post-encode verification with one automatic stricter retry when needed.
- Frame-accurate FFmpeg trim and overlap dissolve.
- Default output is muted.
- Optional synchronized audio crossfade using the same trim and transition positions as video.
- JSON report with selection, verification, and retry attempts.
- Optional repeated preview for visually checking the loop seam.

## Requirements

- Windows
- Python 3.11+
- FFmpeg / FFprobe available in `PATH`

Verify FFmpeg:

```powershell
ffmpeg -version
ffprobe -version
```

Install Python dependencies:

```powershell
py -m pip install -r requirements.txt
```

## Basic usage

```powershell
py seamless_loop.py input.mp4
```

Default outputs:

```text
input_loop.mp4
input_loop.json
```

Default behavior:

- Audio: muted
- Transition: `auto`
- Small-trim budget: up to 8% of source frames before fallback search
- Fallback duration range: 75% to 98% of source
- Video codec: H.264
- CRF: 18

`auto` tries several transition lengths and selects the best motion-balanced candidate while preserving the source span first.

## Keep audio with synchronized crossfade

```powershell
py seamless_loop.py input.mp4 output.mp4 --audio crossfade
```

The audio uses the same selected source range and overlap duration as the video. The loop seam is rebuilt with fades and mixing rather than a hard audio cut.

## Generate a repeated seam-check preview

```powershell
py seamless_loop.py input.mp4 output.mp4 --preview-repeats 3
```

This also creates:

```text
output_preview_x3.mp4
```

A repeated preview makes the loop boundary much easier to judge because it occurs several times in succession.

## Force a specific transition length

```powershell
py seamless_loop.py input.mp4 --transition 0.5
```

Or:

```powershell
py seamless_loop.py input.mp4 --transition 0.75
```

The default `auto` mode is normally preferred.

## Preserve-first tuning

Allow at most 5% source trimming before fallback:

```powershell
py seamless_loop.py input.mp4 --small-trim-ratio 0.05
```

The broad fallback duration controls apply only when full-span and minimal-trim strategies cannot produce an acceptable seam:

```powershell
py seamless_loop.py input.mp4 --min-duration-ratio 0.85 --max-duration-ratio 0.99
```

## Post-encode verification

The source-frame simulation is not treated as final truth. The rendered MP4 is decoded again and the actual loop boundary is compared with normal internal frame-to-frame motion.

If the encoded boundary falls outside the default verification range, SeamlessLoopMaker performs one stricter re-selection and render, then keeps the better result.

The relevant advanced options are:

```text
--verify-min-ratio
--verify-max-ratio
--retry-seam-min-ratio
--retry-seam-max-ratio
```

Normally these should be left at their defaults.

## JSON report

Every run produces a JSON report containing information such as:

- selection mode: `preserve`, `small-trim`, or `fallback`
- selected start/end frame
- transition frame count and seconds
- source-span retention ratio
- transition motion naturalness
- predicted loop-boundary motion ratio
- actual encoded loop-boundary verification ratio
- render attempts when a stricter retry was needed
- SSIM / optical-flow data when fallback search is used

Specify a custom report path with:

```powershell
py seamless_loop.py input.mp4 --report result.json
```

## Algorithm overview

### Stage 1: preserve

The complete source span is tested with several circular overlap transitions. The tool compares transition frame differences with the median internal motion of the source and prefers a loop seam whose motion is neither an abrupt jump nor an obvious slowdown.

### Stage 2: minimal trim

If the complete source span is not acceptable, the tool tries total trim counts in ascending order: 1 frame, 2 frames, 3 frames, and so on. It stops as soon as that trim level contains an acceptable candidate. This makes duration preservation a decision rule, not merely a low-weight scoring preference.

### Stage 3: fallback search

Only when the first two stages fail, the tool performs the broader candidate search using visual correlation, SSIM, Farneback optical flow, boundary motion level, low-motion penalties, and duration scoring.

### Rendering

FFmpeg performs frame-accurate trim, separates the head/middle/tail, overlaps the tail and head with a dissolve, then concatenates the middle and blended transition into the final loop.

Because the head and tail are overlapped, even a no-source-trim result is shorter than the source by approximately the selected transition duration. This is overlap time, not discarded source content.

## Current real-video test cases

The preserve-first version was tested on three approximately 10-second, 24 fps AI background clips:

- Clip 1: no source frames trimmed; 0.75-second transition; about 9.29-second output.
- Clip 2: initial no-trim render required post-encode retry; final result trimmed only 1 source frame and produced about 9.33 seconds.
- Earlier test clip: only 5 source frames (about 0.21 seconds) needed trimming; final output about 9.08 seconds, compared with the older algorithm's 8.625-second result.

The audio-preserving path was also verified after an automatic retry; video and AAC audio durations matched to within the codec timebase rounding interval.

## License

MIT License. See [LICENSE](LICENSE).
