# SeamlessLoopMaker

Automatically turn a short video into a smoother seamless loop by searching for a better start/end frame pair, evaluating motion continuity, trimming weak sections, and rebuilding the loop seam with a controlled crossfade.

This tool is intended especially for short AI-generated background animations such as waves, fire, fog, light particles, abstract motion, and projection backgrounds.

## Features

- Searches for a better loop start/end pair instead of assuming the original first and last frames are optimal.
- Uses SSIM / structural similarity for frame appearance matching.
- Uses Farneback optical flow to evaluate motion direction and velocity continuity near the loop boundary.
- Penalizes candidates containing unusually slow or nearly static motion.
- Simulates the loop transition and scores whether the transition motion is close to the normal frame-to-frame motion of the source video.
- Prefers longer clips when multiple candidates have similar quality.
- Rebuilds the seam with frame-accurate FFmpeg trimming and overlap dissolve.
- Default output is muted.
- Optional audio mode uses the same trim points and transition duration as the video, with audio crossfade instead of a hard cut.
- Outputs a JSON analysis report.
- Can generate a repeated preview file for visually checking the loop seam.

## Requirements

- Windows
- Python 3.11+
- FFmpeg / FFprobe available in `PATH`

Verify FFmpeg first:

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
- Transition: 0.50 seconds
- Candidate duration: 75% to 98% of the source video
- Video codec: H.264
- CRF: 18

## Keep audio with synchronized crossfade

```powershell
py seamless_loop.py input.mp4 output.mp4 --audio crossfade
```

The audio uses the same loop start/end positions and overlap duration as the video. The seam is rebuilt with an audio fade/mix rather than a direct cut.

## Generate a repeated seam-check preview

```powershell
py seamless_loop.py input.mp4 output.mp4 --preview-repeats 3
```

This also creates:

```text
output_preview_x3.mp4
```

A repeated preview is useful because the loop boundary becomes much easier to judge when it occurs several times in succession.

## Common adjustments

Keep more of the original duration:

```powershell
py seamless_loop.py input.mp4 --min-duration-ratio 0.85 --max-duration-ratio 0.99
```

Use a shorter transition:

```powershell
py seamless_loop.py input.mp4 --transition 0.375
```

Use a softer, longer transition:

```powershell
py seamless_loop.py input.mp4 --transition 0.75
```

For complex motion, a longer transition can reduce an abrupt seam, but a transition that is too long may create visible double-image blending. Around 0.3 to 0.75 seconds is usually a useful range for short animated backgrounds.

## JSON report

Every run produces a JSON report containing information such as:

- Selected start/end frame
- Transition frame count
- SSIM
- Optical-flow similarity
- Transition motion naturalness
- Estimated and actual output duration
- Loop-boundary frame-difference ratio relative to normal internal frame differences

Specify a custom report path with:

```powershell
py seamless_loop.py input.mp4 --report result.json
```

## Algorithm overview

1. Decode the video frame by frame at reduced resolution for analysis.
2. Calculate Farneback optical flow and build block-based motion descriptors for adjacent frames.
3. Quickly shortlist candidate `(start, end)` pairs using visual correlation, optical-flow similarity, motion level, and duration constraints.
4. Calculate SSIM for shortlisted candidates.
5. Simulate the overlap dissolve and compare transition frame-to-frame motion with the source video's normal motion cadence.
6. Score the candidates and select the best continuous region.
7. Use FFmpeg for frame-accurate trimming and rebuild the loop seam with overlap dissolve.
8. If `--audio crossfade` is enabled, use the same start/end/transition values for `atrim`, fades, and audio mixing.
9. Re-read the result and write verification metrics into the JSON report.

## Example test result

The initial implementation was verified using an actual 10.04-second, 24 fps, 241-frame source clip. The selected result was 8.625 seconds with a 12-frame / 0.5-second loop transition. The tool also verified an audio-preserving version where both video and audio durations were exactly 8.625 seconds.

## License

MIT License. See [LICENSE](LICENSE).
