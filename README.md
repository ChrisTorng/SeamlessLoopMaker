# SeamlessLoopMaker

Automatically turn a short video into a smoother seamless loop while preserving as much of the original clip as possible.

Designed especially for short AI-generated backgrounds such as waves, fire, fog, light particles, abstract motion, and projection backgrounds.

## Key behavior

The default strategy is **preserve-first**:

1. Keep the full source span and try only a head/tail overlap transition.
2. If the seam is not balanced enough, trim the smallest possible number of source frames.
3. Only if a small trim still cannot produce a good seam, use the broader SSIM + optical-flow fallback search.
4. Render the result and verify the actual encoded loop boundary.
5. If encoding makes the seam worse than expected, automatically retry once with stricter limits and keep the better result.

## Audio

Audio is preserved by default when the source contains audio. The audio uses the same selected source range and transition duration as the video and is crossfaded with a `qsin` fade curve.

Mute explicitly with:

```powershell
py seamless_loop.py input.mp4 --mute
```

The older explicit form remains supported:

```powershell
py seamless_loop.py input.mp4 --audio mute
py seamless_loop.py input.mp4 --audio crossfade
```

## Requirements

- Windows
- Python 3.11+
- FFmpeg / FFprobe available in `PATH`

```powershell
ffmpeg -version
ffprobe -version
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

The CLI always prints the final edit information, regardless of whether a repeated preview is requested. It includes:

- source duration / frames
- head trim duration / frames
- tail trim duration / frames
- selected source span
- transition duration / frames
- transition start/end position inside one output cycle
- final output-cycle duration / frames
- video blend curve
- audio mode / curve

Example:

```text
Loop edit summary:
  Source:           10.042 s / 241 frames
  Head trim:        0.292 s / 7 frames
  Tail trim:        0.000 s / 0 frames
  Selected span:    9.750 s / 234 frames
  Transition:       0.750 s / 18 frames
  Transition range: 8.250 -> 9.000 s in output cycle
  Output cycle:     9.000 s / 216 frames
  Video blend:      linear
  Audio:            crossfade (qsin)
```

## Repeated seam-check preview

Create a plain repeated preview:

```powershell
py seamless_loop.py input.mp4 --preview-repeats 3
```

Create a repeated preview with the compact diagnostic timeline overlaid **inside the original video frame**:

```powershell
py seamless_loop.py input.mp4 --preview-repeats 3 --preview-timeline
```

Or simply:

```powershell
py seamless_loop.py input.mp4 --preview-timeline
```

`--preview-timeline` automatically uses three repeats when no repeat count is supplied.

The diagnostic overlay:

- does **not** change output width, height, or aspect ratio
- has no solid panel/background
- contains no text labels inside the video
- uses three compact source tracks, with copies 1 and 3 on the upper row and copy 2 on the lower row
- shows only actually trimmed head/tail regions as faint thin line segments
- shows retained regions as brighter thin segments
- shows the transition regions and short linear blend connectors
- shows short output-cycle boundary ticks
- shows a moving playhead
- represents the final 3 -> 1 wrap with short edge stubs instead of a long curve across the frame

The underlying video remains visible everywhere except the few pixels occupied by the diagnostic lines.

## Transition selection

Default:

```powershell
--transition auto
```

`auto` tries several overlap lengths, approximately 0.25 to 0.75 seconds, and selects the best motion-balanced candidate while preserving the source span first.

Force a specific transition:

```powershell
py seamless_loop.py input.mp4 --transition 0.5
```

The current video blend is linear. Audio crossfade uses `qsin`.

## Preserve-first tuning

Allow at most 5% source trimming before fallback:

```powershell
py seamless_loop.py input.mp4 --small-trim-ratio 0.05
```

Fallback range, used only when preserve/minimal-trim stages fail:

```powershell
py seamless_loop.py input.mp4 --min-duration-ratio 0.85 --max-duration-ratio 0.99
```

## JSON report

Every run writes a JSON report containing the final selection, predicted transition quality, encoded-output verification, retry attempts, audio mode, and preview metadata.

Specify a custom path:

```powershell
py seamless_loop.py input.mp4 --report result.json
```

## Algorithm overview

### Stage 1: preserve

Test the complete source span with several circular overlap transitions. Transition frame differences are compared with normal internal motion so the seam does not become an abrupt jump or obvious slowdown.

### Stage 2: minimal trim

Try total trim counts in ascending order: 1 frame, 2 frames, 3 frames, and so on. Stop at the first trim level that contains an acceptable candidate.

### Stage 3: fallback

Only when the first two stages fail, perform the broader search using visual correlation, SSIM, Farneback optical flow, boundary motion level, low-motion penalties, and duration scoring.

### Rendering

FFmpeg performs frame-accurate trim, separates the head/middle/tail, overlaps the tail and head, and concatenates the result into one loop period. The video overlap uses a linear blend. When audio is enabled, audio uses the same source range and overlap length with `qsin` fades.

Because the head and tail are overlapped, a no-source-trim result is still shorter than the source by approximately the transition duration. That time is overlapped, not discarded source content.

## License

MIT License. See [LICENSE](LICENSE).
