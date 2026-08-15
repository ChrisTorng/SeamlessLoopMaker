#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

import seamless_loop_core as core


def build_preview_timeline(overlay_path, playhead_path, width, height, source_frame_count, fps, selection, repeats):
    if repeats < 2:
        raise ValueError("Timeline preview needs at least 2 repeats")

    n = source_frame_count
    s = selection["start_frame"]
    e = selection["end_frame"]
    k = selection["transition_frames"]
    source_duration = n / fps
    transition = k / fps
    output_duration = (e - s - k) / fps
    total_preview = output_duration * repeats

    margin = max(10, round(width * .012))
    earliest = -(s + k) / fps
    latest = (repeats - 1) * output_duration - (s + k) / fps + source_duration
    span = max(.001, latest - earliest)

    def px(t):
        return int(round(margin + (t - earliest) / span * (width - 1 - 2 * margin)))

    y_upper = height - 29
    y_lower = height - 16
    ys = [y_upper if i % 2 == 0 else y_lower for i in range(repeats)]
    if repeats >= 3:
        ys[2] = y_upper + 2

    overlay = np.zeros((height, width, 4), np.uint8)
    faint = (235, 235, 235, 70)
    kept = (250, 250, 250, 185)
    transition_color = (255, 255, 255, 225)
    boundary_color = (245, 245, 245, 125)

    for i in range(repeats):
        y = ys[i]
        anchor = i * output_duration - (s + k) / fps
        full_start = px(anchor)
        full_end = px(anchor + source_duration)
        kept_start = px(anchor + s / fps)
        kept_end = px(anchor + e / fps)

        if full_start < kept_start:
            cv2.line(overlay, (full_start, y), (kept_start, y), faint, 1, cv2.LINE_AA)
        cv2.line(overlay, (kept_start, y), (kept_end, y), kept, 2, cv2.LINE_AA)
        if kept_end < full_end:
            cv2.line(overlay, (kept_end, y), (full_end, y), faint, 1, cv2.LINE_AA)

        trans_start = px((i + 1) * output_duration - transition)
        trans_end = px((i + 1) * output_duration)
        cv2.line(overlay, (trans_start, y), (trans_end, y), transition_color, 3, cv2.LINE_AA)

    for i in range(repeats - 1):
        boundary = (i + 1) * output_duration
        x0 = px(boundary - transition)
        x1 = px(boundary)
        cv2.line(overlay, (x0, ys[i]), (x1, ys[i + 1]), transition_color, 1, cv2.LINE_AA)

    if repeats >= 3:
        x0 = px(total_preview - transition)
        x1 = px(total_preview)
        wrap_target_y = y_upper - 3
        cv2.line(overlay, (x0, ys[-1]), (x1, wrap_target_y), transition_color, 1, cv2.LINE_AA)
        first_x = px(0.0)
        first_stub_start = max(0, first_x - max(5, round((x1 - x0) * .22)))
        cv2.line(overlay, (first_stub_start, wrap_target_y), (first_x, ys[0]), transition_color, 1, cv2.LINE_AA)

    tick_top = min(ys) - 6
    tick_bottom = max(ys) + 6
    for i in range(repeats + 1):
        x = px(i * output_duration)
        cv2.line(overlay, (x, tick_top), (x, tick_bottom), boundary_color, 1, cv2.LINE_AA)

    if not cv2.imwrite(str(overlay_path), overlay):
        raise RuntimeError(f"Could not write timeline overlay: {overlay_path}")

    playhead = np.zeros((height, 5, 4), np.uint8)
    cv2.line(playhead, (2, tick_top - 2), (2, tick_bottom + 2), (255, 255, 255, 245), 2, cv2.LINE_AA)
    if not cv2.imwrite(str(playhead_path), playhead):
        raise RuntimeError(f"Could not write playhead image: {playhead_path}")

    return {
        "overlay_style": "compact-transparent",
        "boundary_start_x": px(0.0),
        "boundary_end_x": px(total_preview),
        "source_duration": source_duration,
        "output_cycle_duration": output_duration,
        "preview_duration": total_preview,
        "head_trim_seconds": s / fps,
        "head_trim_frames": s,
        "tail_trim_seconds": (n - e) / fps,
        "tail_trim_frames": n - e,
        "transition_start_in_cycle": max(0.0, output_duration - transition),
        "transition_end_in_cycle": output_duration,
        "transition_seconds": transition,
        "transition_frames": k,
        "video_blend_curve": core.VIDEO_BLEND_CURVE,
        "audio_blend_curve": core.AUDIO_BLEND_CURVE,
    }


def render_preview_timeline(out, preview, info, selection, verification, source_frame_count, repeats, args):
    overlay_path = preview.with_name(preview.stem + ".__timeline.png")
    playhead_path = preview.with_name(preview.stem + ".__playhead.png")
    timeline = build_preview_timeline(
        overlay_path, playhead_path, info["width"], info["height"],
        source_frame_count, info["fps"], selection, repeats,
    )

    x0 = timeline["boundary_start_x"]
    x1 = timeline["boundary_end_x"]
    total = timeline["preview_duration"]
    playhead_expr = f"{x0}+(t/{total:.12f})*({x1-x0})-2"
    graph = (
        "[0:v][1:v]overlay=0:0:shortest=1[tl];"
        f"[tl][2:v]overlay=x='{playhead_expr}':y=0:eval=frame:shortest=1[outv]"
    )

    cmd = [
        args.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-stream_loop", str(repeats - 1), "-i", str(out),
        "-loop", "1", "-i", str(overlay_path),
        "-loop", "1", "-i", str(playhead_path),
        "-filter_complex", graph, "-map", "[outv]",
    ]
    if info["has_audio"] and args.audio == "crossfade":
        cmd += ["-map", "0:a?", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    cmd += [
        "-t", f"{total:.12f}",
        "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(preview),
    ]
    result = core.run(cmd)
    overlay_path.unlink(missing_ok=True)
    playhead_path.unlink(missing_ok=True)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return timeline


def _result_paths(argv):
    value_options = {
        "--audio", "--transition", "--small-trim-ratio", "--seam-min-ratio", "--seam-max-ratio",
        "--min-transition-naturalness", "--verify-min-ratio", "--verify-max-ratio",
        "--retry-seam-min-ratio", "--retry-seam-max-ratio", "--min-duration-ratio",
        "--max-duration-ratio", "--analysis-width", "--shortlist", "--preview-repeats",
        "--preview-timeline-height", "--report", "--crf", "--preset", "--ffmpeg", "--ffprobe",
    }
    positionals = []
    report = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in value_options:
            if arg == "--report" and i + 1 < len(argv):
                report = Path(argv[i + 1])
            i += 2
            continue
        if arg.startswith("--"):
            i += 1
            continue
        positionals.append(Path(arg))
        i += 1

    if not positionals:
        return None, report
    src = positionals[0]
    out = positionals[1] if len(positionals) > 1 else src.with_name(src.stem + "_loop.mp4")
    out = out.resolve()
    return out, (report.resolve() if report else out.with_suffix(".json"))


def print_edit_summary(report_path):
    if not report_path or not report_path.exists():
        return
    report = json.loads(report_path.read_text(encoding="utf-8"))
    info = report["media"]
    selection = report["selection"]
    fps = float(info["fps"])
    n = int(round(float(info["duration"]) * fps))
    s = int(selection["start_frame"])
    e = int(selection["end_frame"])
    k = int(selection["transition_frames"])
    selected_frames = e - s
    output_frames = selected_frames - k
    output_duration = output_frames / fps
    transition = k / fps
    audio_mode = report.get("audio_mode")
    if audio_mode == "mute":
        audio = "mute"
    elif info.get("has_audio"):
        audio = f"crossfade ({core.AUDIO_BLEND_CURVE})"
    else:
        audio = "source has no audio"

    print("Loop edit summary:")
    print(f"  Source:           {n/fps:.3f} s / {n} frames")
    print(f"  Head trim:        {s/fps:.3f} s / {s} frames")
    print(f"  Tail trim:        {(n-e)/fps:.3f} s / {n-e} frames")
    print(f"  Selected span:    {selected_frames/fps:.3f} s / {selected_frames} frames")
    print(f"  Transition:       {transition:.3f} s / {k} frames")
    print(f"  Transition range: {max(0.0, output_duration-transition):.3f} -> {output_duration:.3f} s in output cycle")
    print(f"  Output cycle:     {output_duration:.3f} s / {output_frames} frames")
    print(f"  Video blend:      {core.VIDEO_BLEND_CURVE}")
    print(f"  Audio:            {audio}")


def main():
    core.build_preview_timeline = build_preview_timeline
    core.render_preview_timeline = render_preview_timeline
    _, report_path = _result_paths(sys.argv[1:])
    result = core.main()
    if result == 0:
        print_edit_summary(report_path)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
