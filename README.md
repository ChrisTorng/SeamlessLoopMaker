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
- **Audio is preserved by default** when the source has audio.
- Audio uses the same trim and transition positions as video and is rebuilt with synchronized crossfade.
- `--mute` removes audio explicitly.
- JSON report with selection, verification, retry attempts, blend curves, and preview timeline data.
- Optional repeated preview for visually checking the loop seam.
- Optional diagnostic timeline appended below the repeated preview, including trim lengths, transition ranges, repeated source bars, loop boundaries, and a moving playhead.

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

- Audio: preserved with synchronized crossfade when present
- Transition: `auto`
- Small-trim budget: up to 8% of source frames before fallback search
- Fallback duration range: 75% to 98% of source
- Video codec: H.264
- CRF: 18

`auto` tries several transition lengths and selects the best motion-balanced candidate while preserving the source span first.

## Mute the output

Audio is now kept by default. Use `--mute` only when a silent loop is wanted:

```powershell
py seamless_loop.py input.mp4 output.mp4 --mute
```

For backward-compatible explicit control, this also works:

```powershell
py seamless_loop.py input.mp4 output.mp4 --audio mute
py seamless_loop.py input.mp4 output.mp4 --audio crossfade
```

When audio is preserved, the same selected source range and transition duration are applied to audio. The loop seam is rebuilt with `qsin` fades and mixing instead of a hard audio cut.

## Generate a repeated seam-check preview

```powershell
py seamless_loop.py input.mp4 output.mp4 --preview-repeats 3
```

This creates:

```text
output_preview_x3.mp4
```

A repeated preview makes the loop boundary much easier to judge because it occurs several times in succession.

## Diagnostic three-loop timeline preview

Use the repeated preview together with `--preview-timeline`:

```powershell
py seamless_loop.py input.mp4 output.mp4 --preview-repeats 3 --preview-timeline
```

This creates:

```text
output_preview_x3_timeline.mp4
```

If `--preview-timeline` is supplied without a repeat count, the tool automatically uses three repeats.

The diagnostic panel is appended below the video and intentionally uses a taller-than-normal playback bar. It shows:

- source copy 1 and 3 on the upper row and source copy 2 on the lower row
- the **complete original source duration** for every copy as a thin line
- the actually retained source span as a thick line
- head and tail trimmed regions as thin source-line sections
- four vertical output-cycle boundaries for a three-repeat preview: start, 1→2, 2→3, and 3→1
- transition intervals and transition frame counts
- a connector between source rows that reflects the actual video blend curve
- an explicit wrap connector from the third repeat back to the first repeat
- head-trim and tail-trim duration plus frame counts
- transition start/end position within one output cycle
- total transition seconds and frames
- source duration / source frame count
- output-cycle duration / output frame count
- moving playback-position line
- numeric current playback timestamp when the installed FFmpeg build supports `drawtext`

The current rendering curves are:

- Video: **linear** blend (`A * (1-alpha) + B * alpha`)
- Audio: **qsin** fade curves

Therefore the 1→2 and 2→3 video connectors are drawn as straight diagonal lines. If the video blend algorithm changes later, the diagnostic curve should change with it rather than merely being decorative.

The diagnostic preview itself is still a loopable three-repeat video. The final third-repeat boundary is the same real loop seam as the other boundaries, and the panel includes an explicit 3→1 wrap indicator so the final return to the first repeat is visible.

The panel height can be changed if needed:

```powershell
py seamless_loop.py input.mp4 --preview-timeline --preview-timeline-height 320
```

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
- video and audio blend-curve names
- diagnostic-preview layout values when `--preview-timeline` is enabled

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

The video overlap currently uses a **linear alpha blend**. Preserved audio uses **qsin fades** before mixing.

## Current real-video test cases

The preserve-first version was tested using the default analysis and encoding settings on three approximately 10-second, 24 fps AI background clips:

- Clip 1: no source frames trimmed; 0.75-second transition; about 9.29-second output.
- Clip 2: no source frames trimmed; 0.625-second transition; about 9.50-second output.
- Earlier test clip: only 7 source frames (about 0.29 seconds) trimmed; 0.75-second transition; about 9.00-second output. The older algorithm had shortened this same source to 8.625 seconds.

The new default-audio path and the diagnostic timeline preview were also verified on the earlier test clip. The loop output contains AAC audio by default, and the three-repeat diagnostic preview contains both video and audio for exactly 27 seconds. A `--mute` test was also verified to contain only a video stream.

## License

MIT License. See [LICENSE](LICENSE).
