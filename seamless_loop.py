#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import heapq
import json
import math
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np

AUTO_TRANSITIONS = (0.25, 1 / 3, 0.375, 0.50, 0.625, 0.75)
VIDEO_BLEND_CURVE = "linear"
AUDIO_BLEND_CURVE = "qsin"


def run(cmd):
    return subprocess.run(cmd, text=True, capture_output=True, encoding="utf-8", errors="replace")


def probe(path, ffprobe):
    p = run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    if p.returncode:
        raise RuntimeError(p.stderr)
    data = json.loads(p.stdout)
    video = next((x for x in data["streams"] if x.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("Input has no video stream")
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/0"
    fps = float(Fraction(rate)) if rate != "0/0" else 0.0
    return {
        "fps": fps,
        "width": int(video.get("width", 0)),
        "height": int(video.get("height", 0)),
        "duration": float(video.get("duration") or data.get("format", {}).get("duration") or 0),
        "has_audio": any(x.get("codec_type") == "audio" for x in data["streams"]),
    }


def read_frames(path, width):
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open {path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
    out = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        h, w = frame.shape[:2]
        if w > width:
            frame = cv2.resize(frame, (width, max(2, round(h * width / w))), interpolation=cv2.INTER_AREA)
        out.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    cap.release()
    if len(out) < 12:
        raise RuntimeError("Video is too short")
    return np.stack(out), fps


def ssim(a, b):
    a, b = a.astype(np.float32), b.astype(np.float32)
    c1, c2 = (2.55 ** 2), (7.65 ** 2)
    ma, mb = cv2.GaussianBlur(a, (11, 11), 1.5), cv2.GaussianBlur(b, (11, 11), 1.5)
    va = cv2.GaussianBlur(a * a, (11, 11), 1.5) - ma * ma
    vb = cv2.GaussianBlur(b * b, (11, 11), 1.5) - mb * mb
    vab = cv2.GaussianBlur(a * b, (11, 11), 1.5) - ma * mb
    return float(np.mean(((2 * ma * mb + c1) * (2 * vab + c2)) / ((ma * ma + mb * mb + c1) * (va + vb + c2) + 1e-12)))


def base_frame_diffs(frames):
    arr = frames.astype(np.float32) / 255
    return np.mean(np.abs(arr[1:] - arr[:-1]), axis=(1, 2))


def simulate_transition(frames, frame_diffs, s, e, k):
    clip = frames[s:e].astype(np.float32) / 255
    if len(clip) <= 2 * k + 4:
        return None

    head, tail = clip[:k], clip[-k:]
    alpha = np.linspace(0, 1, k, dtype=np.float32)[:, None, None]
    blend = (1 - alpha) * tail + alpha * head

    baseline = float(np.median(frame_diffs[s:e - 1])) + 1e-9
    transition_diffs = [float(np.mean(np.abs(clip[-k - 1] - blend[0])))]
    transition_diffs += np.mean(np.abs(blend[1:] - blend[:-1]), axis=(1, 2)).tolist()
    transition_diffs += [float(np.mean(np.abs(blend[-1] - clip[k])))]
    ratios = np.asarray(transition_diffs, dtype=np.float32) / baseline

    boundary_ratio = transition_diffs[-1] / baseline
    naturalness = math.exp(-float(np.mean(np.abs(np.log(np.clip(ratios, 1e-4, None))))))
    seam_balance = math.exp(-abs(math.log(max(boundary_ratio, 1e-4))))

    return {
        "boundary_ratio": float(boundary_ratio),
        "transition_naturalness": float(naturalness),
        "transition_min_ratio": float(ratios.min()),
        "transition_max_ratio": float(ratios.max()),
        "seam_balance": float(seam_balance),
    }


def parse_transition_choices(value, fps):
    if value == "auto":
        seconds = AUTO_TRANSITIONS
    else:
        try:
            seconds = (float(value),)
        except ValueError as ex:
            raise ValueError("--transition must be 'auto' or a number of seconds") from ex
        if not 0.08 <= seconds[0] <= 2.0:
            raise ValueError("--transition seconds must be between 0.08 and 2.0")

    seen = set()
    choices = []
    for sec in seconds:
        frames = max(2, round(sec * fps))
        if frames not in seen:
            seen.add(frames)
            choices.append((frames, frames / fps))
    return choices


def acceptable(metrics, args):
    return (
        args.seam_min_ratio <= metrics["boundary_ratio"] <= args.seam_max_ratio
        and metrics["transition_naturalness"] >= args.min_transition_naturalness
    )


def preserve_quality(metrics, source_span_ratio, output_ratio):
    return (
        0.46 * metrics["seam_balance"]
        + 0.34 * metrics["transition_naturalness"]
        + 0.12 * source_span_ratio
        + 0.08 * output_ratio
    )


def candidate(frames, frame_diffs, fps, s, e, k, mode):
    metrics = simulate_transition(frames, frame_diffs, s, e, k)
    if metrics is None:
        return None
    n = len(frames)
    source_span_ratio = (e - s) / n
    output_frames = e - s - k
    output_ratio = output_frames / n
    result = {
        "mode": mode,
        "start_frame": int(s),
        "end_frame": int(e),
        "transition_frames": int(k),
        "transition_seconds": float(k / fps),
        "source_span_ratio": float(source_span_ratio),
        "output_duration": float(output_frames / fps),
        **metrics,
    }
    result["score"] = preserve_quality(metrics, source_span_ratio, output_ratio)
    return result


def choose_preserve_first(frames, fps, transition_choices, args):
    n = len(frames)
    frame_diffs = base_frame_diffs(frames)

    full = []
    for k, _ in transition_choices:
        x = candidate(frames, frame_diffs, fps, 0, n, k, "preserve")
        if x:
            full.append(x)
    accepted = [x for x in full if acceptable(x, args)]
    if accepted:
        return max(accepted, key=lambda x: x["score"]), sorted(full, key=lambda x: x["score"], reverse=True)

    max_total_trim = max(1, round(n * args.small_trim_ratio))
    small_all = list(full)
    for total_trim in range(1, max_total_trim + 1):
        level = []
        for start_trim in range(total_trim + 1):
            end_trim = total_trim - start_trim
            s, e = start_trim, n - end_trim
            for k, _ in transition_choices:
                x = candidate(frames, frame_diffs, fps, s, e, k, "small-trim")
                if x:
                    level.append(x)
                    small_all.append(x)
        accepted = [x for x in level if acceptable(x, args)]
        if accepted:
            return max(accepted, key=lambda x: x["score"]), sorted(small_all, key=lambda x: x["score"], reverse=True)[:20]

    fallback = choose_fallback(frames, fps, transition_choices, args)
    return fallback, sorted(small_all + [fallback], key=lambda x: x["score"], reverse=True)[:20]


def flow_desc(flow, gx=8, gy=4):
    h, w = flow.shape[:2]
    values = []
    for y in range(gy):
        for x in range(gx):
            cell = flow[y * h // gy:(y + 1) * h // gy, x * w // gx:(x + 1) * w // gx]
            mag = np.linalg.norm(cell, axis=2)
            values += [cell[..., 0].mean(), cell[..., 1].mean(), mag.mean()]
    values = np.asarray(values, np.float32)
    return values / (np.linalg.norm(values) + 1e-8)


def fallback_features(frames):
    h, w = frames.shape[1:]
    fw = min(160, w)
    fh = max(2, round(h * fw / w))
    small = np.stack([cv2.resize(f, (fw, fh), interpolation=cv2.INTER_AREA) for f in frames])
    desc, mag = [], []
    for i in range(len(small) - 1):
        flow = cv2.calcOpticalFlowFarneback(small[i], small[i + 1], None, .5, 3, 15, 3, 5, 1.2, 0)
        desc.append(flow_desc(flow))
        mag.append(np.linalg.norm(flow, axis=2).mean())

    tiny = np.stack([cv2.resize(f, (80, 44), interpolation=cv2.INTER_AREA) for f in frames]).astype(np.float32) / 255
    z = tiny.reshape(len(tiny), -1)
    z = (z - z.mean(1, keepdims=True)) / (z.std(1, keepdims=True) + 1e-6) / math.sqrt(z.shape[1])
    return np.stack(desc), np.asarray(mag), z


def choose_fallback(frames, fps, transition_choices, args):
    n, look = len(frames), 3
    min_span = round(n * args.min_duration_ratio)
    max_span = min(n - look - 1, round(n * args.max_duration_ratio))
    if min_span >= max_span:
        raise RuntimeError("Duration constraints leave no fallback search interval")

    frame_diffs = base_frame_diffs(frames)
    desc, mag, z = fallback_features(frames)
    heap = []
    max_k = max(k for k, _ in transition_choices)
    min_span = max(min_span, 2 * max_k + 12)

    for s in range(n - min_span):
        for e in range(s + min_span, min(s + max_span, n - look - 1) + 1):
            corr = float(z[s] @ z[e])
            flow_similarity = float(np.mean(np.sum(desc[s:s + look] * desc[e:e + look], axis=1)))
            segment = mag[s:e]
            med = float(np.median(segment)) + 1e-8
            seam = np.r_[mag[s:s + look], mag[e:e + look]]
            motion = float(np.clip(np.mean(np.minimum(seam / med, med / (seam + 1e-8))), 0, 1))
            low = float(np.mean(segment < .6 * med))
            quick = .55 * (corr + 1) / 2 + .25 * (flow_similarity + 1) / 2 + .15 * motion + .05 * (1 - low)
            item = (quick, s, e, corr, flow_similarity, motion)
            if len(heap) < args.shortlist:
                heapq.heappush(heap, item)
            elif quick > heap[0][0]:
                heapq.heapreplace(heap, item)

    best = None
    for _, s, e, corr, flow_similarity, motion in sorted(heap, reverse=True):
        for k, _ in transition_choices:
            x = candidate(frames, frame_diffs, fps, s, e, k, "fallback")
            if not x:
                continue
            static_similarity = ssim(frames[s], frames[e])
            duration_score = min(1.0, x["output_duration"] / max(.1, n / fps * .90))
            x.update({
                "ssim": static_similarity,
                "flow_similarity": flow_similarity,
                "fast_correlation": corr,
                "boundary_motion_level": motion,
            })
            x["score"] = (
                .28 * static_similarity
                + .20 * (flow_similarity + 1) / 2
                + .24 * x["transition_naturalness"]
                + .14 * x["seam_balance"]
                + .14 * duration_score
            )
            if best is None or x["score"] > best["score"]:
                best = x

    if best is None:
        raise RuntimeError("Could not find a fallback loop candidate")
    return best


def filter_graph(info, selection, audio):
    fps = info["fps"]
    s = selection["start_frame"]
    e = selection["end_frame"]
    k = selection["transition_frames"]
    length = e - s
    den = max(1, k - 1)

    filters = [
        f"[0:v]trim=start_frame={s}:end_frame={e},setpts=N/{fps:.12f}/TB[v]",
        "[v]split=3[vh][vm][vt]",
        f"[vh]trim=start_frame=0:end_frame={k},setpts=N/{fps:.12f}/TB[head]",
        f"[vm]trim=start_frame={k}:end_frame={length-k},setpts=N/{fps:.12f}/TB[mid]",
        f"[vt]trim=start_frame={length-k}:end_frame={length},setpts=N/{fps:.12f}/TB[tail]",
        f"[tail][head]blend=all_expr='A*(1-N/{den})+B*(N/{den})':shortest=1[blend]",
        f"[mid][blend]concat=n=2:v=1:a=0,fps={fps:.12f}[outv]",
    ]
    maps = ["-map", "[outv]"]

    if audio == "crossfade" and info["has_audio"]:
        t0 = s / fps
        duration = length / fps
        fade = k / fps
        filters += [
            f"[0:a:0]atrim=start={t0+fade:.12f}:end={t0+duration-fade:.12f},asetpts=PTS-STARTPTS[amid]",
            f"[0:a:0]atrim=start={t0+duration-fade:.12f}:end={t0+duration:.12f},asetpts=PTS-STARTPTS,afade=t=out:st=0:d={fade:.12f}:curve=qsin[atail]",
            f"[0:a:0]atrim=start={t0:.12f}:end={t0+fade:.12f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d={fade:.12f}:curve=qsin[ahead]",
            "[atail][ahead]amix=inputs=2:duration=shortest:dropout_transition=0:normalize=0,asetpts=PTS-STARTPTS[ablend]",
            "[amid][ablend]concat=n=2:v=0:a=1[outa]",
        ]
        maps += ["-map", "[outa]"]

    return ";".join(filters), maps


def render(src, dst, info, selection, args):
    graph, maps = filter_graph(info, selection, args.audio)
    cmd = [
        args.ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
        "-filter_complex", graph, *maps,
        "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf), "-pix_fmt", "yuv420p",
    ]
    if args.audio == "crossfade" and info["has_audio"]:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-an"]
    result = run(cmd + ["-movflags", "+faststart", str(dst)])
    if result.returncode:
        raise RuntimeError(result.stderr)


def verify(path, width):
    frames, fps = read_frames(path, width)
    arr = frames.astype(np.float32) / 255
    diffs = np.mean(np.abs(arr[1:] - arr[:-1]), axis=(1, 2))
    boundary = float(np.mean(np.abs(arr[-1] - arr[0])))
    median = float(np.median(diffs)) + 1e-9
    return {
        "frames": len(frames),
        "fps": fps,
        "duration": len(frames) / fps,
        "boundary_diff": boundary,
        "internal_median_diff": median,
        "boundary_ratio": boundary / median,
        "min_internal_diff": float(diffs.min()),
        "max_internal_diff": float(diffs.max()),
    }


def _text(img, text, x, y, scale=.48, color=(225, 225, 225), thickness=1):
    cv2.putText(img, text, (int(x), int(y)), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def _bezier_points(p0, p1, p2, count=80):
    t = np.linspace(0.0, 1.0, count, dtype=np.float32)[:, None]
    p0 = np.asarray(p0, dtype=np.float32)
    p1 = np.asarray(p1, dtype=np.float32)
    p2 = np.asarray(p2, dtype=np.float32)
    pts = (1 - t) ** 2 * p0 + 2 * (1 - t) * t * p1 + t ** 2 * p2
    return np.rint(pts).astype(np.int32).reshape(-1, 1, 2)


def build_preview_timeline(panel_path, playhead_path, width, panel_height, source_frame_count, fps, selection, repeats):
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
    head_trim = s / fps
    tail_trim = (n - e) / fps

    panel = np.full((panel_height, width, 3), 18, np.uint8)
    plot_left = max(18, round(width * .018))
    plot_right = width - plot_left
    earliest = -(s + k) / fps
    latest = (repeats - 1) * output_duration - (s + k) / fps + source_duration
    span = max(.001, latest - earliest)

    def px(t):
        return int(round(plot_left + (t - earliest) / span * (plot_right - plot_left)))

    y_top = 60
    y_bottom = 135 if panel_height >= 230 else max(105, panel_height // 2 + 15)
    ys = [y_top if i % 2 == 0 else y_bottom for i in range(repeats)]

    for boundary_index in range(1, repeats + 1):
        a = px(boundary_index * output_duration - transition)
        b = px(boundary_index * output_duration)
        if b > 0 and a < width:
            cv2.rectangle(panel, (max(0, a), 28), (min(width - 1, b), min(panel_height - 62, 170)), (34, 34, 34), -1)

    for i in range(repeats + 1):
        x = px(i * output_duration)
        cv2.line(panel, (x, 22), (x, min(panel_height - 58, 175)), (165, 165, 165), 1, cv2.LINE_AA)
        if i == 0:
            label = "0 / START"
        elif i == repeats:
            label = f"{i} / LOOP"
        else:
            label = f"{i}|{i+1}"
        tw = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, .43, 1)[0][0]
        label_x = min(width - tw - 2, max(2, x - tw // 2))
        _text(panel, label, label_x, 17, .43, (205, 205, 205))

    for i in range(repeats):
        y = ys[i]
        anchor = i * output_duration - (s + k) / fps
        full_start = px(anchor)
        full_end = px(anchor + source_duration)
        kept_start = px(anchor + s / fps)
        kept_end = px(anchor + e / fps)

        cv2.line(panel, (full_start, y), (full_end, y), (95, 95, 95), 2, cv2.LINE_AA)
        cv2.line(panel, (kept_start, y), (kept_end, y), (232, 232, 232), 10, cv2.LINE_AA)
        cv2.circle(panel, (full_start, y), 2, (120, 120, 120), -1, cv2.LINE_AA)
        cv2.circle(panel, (full_end, y), 2, (120, 120, 120), -1, cv2.LINE_AA)
        _text(panel, f"SRC {i+1}", max(2, full_start + 4), y - 15, .44, (195, 195, 195))

        trans_start = px((i + 1) * output_duration - transition)
        trans_end = px((i + 1) * output_duration)
        cv2.line(panel, (trans_start, y), (trans_end, y), (210, 210, 210), 14, cv2.LINE_AA)

    for i in range(repeats - 1):
        boundary = (i + 1) * output_duration
        x0 = px(boundary - transition)
        x1 = px(boundary)
        cv2.line(panel, (x0, ys[i]), (x1, ys[i + 1]), (245, 245, 245), 2, cv2.LINE_AA)
        _text(panel, f"{i+1}->{i+2} {k}f", x0 + 3, min(ys[i], ys[i + 1]) - 24, .38, (195, 195, 195))

    x_last = px(total_preview)
    x_first = px(0.0)
    wrap_y = panel_height - 40
    p0 = (x_last, ys[-1] + 10)
    p1 = ((x_last + x_first) // 2, wrap_y)
    p2 = (x_first, ys[0] + 10)
    cv2.polylines(panel, [_bezier_points(p0, p1, p2)], False, (135, 135, 135), 1, cv2.LINE_AA)
    _text(panel, f"{repeats}->1 loop", (x_first + x_last) // 2 - 38, wrap_y - 3, .40, (160, 160, 160))

    detail_y = min(panel_height - 47, 184)
    line1 = f"Head trim: {head_trim:.3f}s / {s}f    Tail trim: {tail_trim:.3f}s / {n-e}f"
    trans_start_out = max(0.0, output_duration - transition)
    line2 = (
        f"Transition: {trans_start_out:.3f}s -> {output_duration:.3f}s    "
        f"{k}f / {transition:.3f}s    Video: {VIDEO_BLEND_CURVE}    Audio: {AUDIO_BLEND_CURVE}"
    )
    line3 = f"Source: {source_duration:.3f}s / {n}f    Output cycle: {output_duration:.3f}s / {e-s-k}f"
    _text(panel, line1, 20, detail_y, .43, (220, 220, 220))
    _text(panel, line2, 20, detail_y + 20, .43, (220, 220, 220))
    if detail_y + 40 < panel_height - 5:
        _text(panel, line3, 20, detail_y + 40, .43, (185, 185, 185))

    if not cv2.imwrite(str(panel_path), panel):
        raise RuntimeError(f"Could not write timeline panel: {panel_path}")

    ph = np.zeros((panel_height, 7, 4), np.uint8)
    ph[:, 2:5, :3] = (255, 255, 255)
    ph[:, 2:5, 3] = 210
    cv2.circle(ph, (3, 26), 3, (255, 255, 255, 235), -1, cv2.LINE_AA)
    if not cv2.imwrite(str(playhead_path), ph):
        raise RuntimeError(f"Could not write playhead image: {playhead_path}")

    return {
        "panel_height": panel_height,
        "plot_time_min": earliest,
        "plot_time_max": latest,
        "boundary_start_x": px(0.0),
        "boundary_end_x": px(total_preview),
        "source_duration": source_duration,
        "output_cycle_duration": output_duration,
        "preview_duration": total_preview,
        "head_trim_seconds": head_trim,
        "head_trim_frames": s,
        "tail_trim_seconds": tail_trim,
        "tail_trim_frames": n - e,
        "transition_start_in_cycle": trans_start_out,
        "transition_end_in_cycle": output_duration,
        "transition_seconds": transition,
        "transition_frames": k,
        "video_blend_curve": VIDEO_BLEND_CURVE,
        "audio_blend_curve": AUDIO_BLEND_CURVE,
    }


def render_preview_plain(out, preview, repeats, args):
    q = run([
        args.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-stream_loop", str(repeats - 1), "-i", str(out), "-c", "copy", str(preview),
    ])
    if q.returncode:
        raise RuntimeError(q.stderr)


def render_preview_timeline(out, preview, info, selection, verification, source_frame_count, repeats, args):
    panel_height = max(180, args.preview_timeline_height)
    panel_path = preview.with_name(preview.stem + ".__timeline.png")
    playhead_path = preview.with_name(preview.stem + ".__playhead.png")
    timeline = build_preview_timeline(
        panel_path, playhead_path, info["width"], panel_height,
        source_frame_count, info["fps"], selection, repeats,
    )

    video_h = info["height"]
    x0 = timeline["boundary_start_x"]
    x1 = timeline["boundary_end_x"]
    total = timeline["preview_duration"]
    playhead_expr = f"{x0}+(t/{total:.12f})*({x1-x0})-3"

    base_graph = (
        f"[0:v]pad=iw:ih+{panel_height}:0:0:color=black[base];"
        f"[base][1:v]overlay=0:{video_h}:shortest=1[p];"
        f"[p][2:v]overlay=x='{playhead_expr}':y={video_h}:eval=frame:shortest=1[ph]"
    )
    draw = (
        f"[ph]drawtext=text='Play %{{pts\\:hms}} / {total:.3f}s':"
        f"x=w-tw-18:y={video_h + panel_height - 20}:fontsize=17:fontcolor=white:"
        f"box=1:boxcolor=black@0.45[outv]"
    )

    common = [
        args.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-stream_loop", str(repeats - 1), "-i", str(out),
        "-loop", "1", "-i", str(panel_path),
        "-loop", "1", "-i", str(playhead_path),
    ]

    def execute(graph):
        cmd = [*common, "-filter_complex", graph, "-map", "[outv]"]
        has_audio = info["has_audio"] and args.audio == "crossfade"
        if has_audio:
            cmd += ["-map", "0:a?", "-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-an"]
        cmd += [
            "-t", f"{total:.12f}",
            "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(preview),
        ]
        return run(cmd)

    result = execute(base_graph + ";" + draw)
    timeline["numeric_play_position"] = True
    if result.returncode:
        fallback = execute(base_graph + ";[ph]null[outv]")
        timeline["numeric_play_position"] = False
        if fallback.returncode:
            raise RuntimeError(fallback.stderr or result.stderr)

    panel_path.unlink(missing_ok=True)
    playhead_path.unlink(missing_ok=True)
    return timeline


def main():
    parser = argparse.ArgumentParser(description="Preserve-first automatic seamless loop maker for short videos")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path, nargs="?")
    parser.add_argument(
        "--audio", choices=("crossfade", "mute"), default=None,
        help="Explicit audio mode. Default: crossfade when source has audio",
    )
    parser.add_argument("--mute", action="store_true", help="Remove audio from output (default is to keep/crossfade audio)")
    parser.add_argument("--transition", default="auto", help="'auto' (default) or transition seconds, e.g. 0.5")
    parser.add_argument("--small-trim-ratio", type=float, default=.08, help="Maximum source frames removed before fallback search")
    parser.add_argument("--seam-min-ratio", type=float, default=.75)
    parser.add_argument("--seam-max-ratio", type=float, default=1.33)
    parser.add_argument("--min-transition-naturalness", type=float, default=.80)
    parser.add_argument("--verify-min-ratio", type=float, default=.60, help="Accepted encoded-output boundary ratio before retry")
    parser.add_argument("--verify-max-ratio", type=float, default=1.55, help="Accepted encoded-output boundary ratio before retry")
    parser.add_argument("--retry-seam-min-ratio", type=float, default=.90, help="Stricter simulated lower bound used only after failed output verification")
    parser.add_argument("--retry-seam-max-ratio", type=float, default=1.15, help="Stricter simulated upper bound used only after failed output verification")
    parser.add_argument("--min-duration-ratio", type=float, default=.75, help="Fallback search only")
    parser.add_argument("--max-duration-ratio", type=float, default=.98, help="Fallback search only")
    parser.add_argument("--analysis-width", type=int, default=320)
    parser.add_argument("--shortlist", type=int, default=150)
    parser.add_argument("--preview-repeats", type=int, default=0)
    parser.add_argument(
        "--preview-timeline", action="store_true",
        help="Append a diagnostic source/trim/transition timeline and moving playhead to the repeated preview",
    )
    parser.add_argument("--preview-timeline-height", type=int, default=260, help="Diagnostic timeline height in pixels")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()

    if args.mute:
        args.audio = "mute"
    elif args.audio is None:
        args.audio = "crossfade"

    if args.preview_timeline and args.preview_repeats <= 1:
        args.preview_repeats = 3

    if not args.input.exists():
        print("Input not found", file=sys.stderr)
        return 2
    if not shutil.which(args.ffmpeg) or not shutil.which(args.ffprobe):
        print("ffmpeg/ffprobe not found", file=sys.stderr)
        return 2
    if not (0 <= args.small_trim_ratio <= .25):
        print("--small-trim-ratio must be between 0 and 0.25", file=sys.stderr)
        return 2
    if not (0 < args.seam_min_ratio < args.seam_max_ratio):
        print("Invalid seam ratio range", file=sys.stderr)
        return 2
    if not (0 < args.verify_min_ratio < args.verify_max_ratio):
        print("Invalid verification ratio range", file=sys.stderr)
        return 2
    if not (.4 <= args.min_duration_ratio < args.max_duration_ratio <= 1):
        print("Invalid fallback duration ratios", file=sys.stderr)
        return 2
    if args.preview_repeats < 0:
        print("--preview-repeats must be >= 0", file=sys.stderr)
        return 2

    out = (args.output or args.input.with_name(args.input.stem + "_loop.mp4")).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        info = probe(args.input, args.ffprobe)
        frames, cvfps = read_frames(args.input, args.analysis_width)
        info["fps"] = cvfps or info["fps"]
        choices = parse_transition_choices(args.transition, info["fps"])

        print(f"Input: {info['width']}x{info['height']}, {info['fps']:.6g} fps, {len(frames)} frames, {info['duration']:.3f} s")
        print(f"Audio: {'mute' if args.audio == 'mute' else ('crossfade' if info['has_audio'] else 'source has no audio')}")
        selection, candidates = choose_preserve_first(frames, info["fps"], choices, args)
        source_trim = selection["start_frame"] + (len(frames) - selection["end_frame"])
        print(
            f"Selected {selection['mode']}: source frames {selection['start_frame']}..{selection['end_frame']-1}, "
            f"trim {source_trim} frame(s), transition {selection['transition_frames']} frame(s) "
            f"({selection['transition_seconds']:.3f} s), output {selection['output_duration']:.3f} s"
        )

        def temp_path(index):
            return out.with_name(f"{out.stem}.__candidate{index}{out.suffix}")

        def is_verified(v):
            return args.verify_min_ratio <= v["boundary_ratio"] <= args.verify_max_ratio

        def actual_score(v):
            seam = math.exp(-abs(math.log(max(v["boundary_ratio"], 1e-4))))
            duration = min(1.0, v["duration"] / max(info["duration"], 1e-6))
            return seam * .78 + duration * .22 + (1.0 if is_verified(v) else 0.0)

        attempts = []
        first_tmp = temp_path(1)
        first_tmp.unlink(missing_ok=True)
        render(args.input, first_tmp, info, selection, args)
        first_verification = verify(first_tmp, args.analysis_width)
        attempts.append({"selection": selection, "verification": first_verification})

        chosen_tmp = first_tmp
        final_selection = selection
        verification = first_verification

        if not is_verified(first_verification):
            retry_args = copy.copy(args)
            if first_verification["boundary_ratio"] > args.verify_max_ratio:
                retry_args.seam_max_ratio = min(args.seam_max_ratio, args.retry_seam_max_ratio)
            else:
                retry_args.seam_min_ratio = max(args.seam_min_ratio, args.retry_seam_min_ratio)

            retry_selection, retry_candidates = choose_preserve_first(frames, info["fps"], choices, retry_args)
            retry_key = (retry_selection["start_frame"], retry_selection["end_frame"], retry_selection["transition_frames"])
            first_key = (selection["start_frame"], selection["end_frame"], selection["transition_frames"])
            if retry_key != first_key:
                second_tmp = temp_path(2)
                second_tmp.unlink(missing_ok=True)
                render(args.input, second_tmp, info, retry_selection, args)
                second_verification = verify(second_tmp, args.analysis_width)
                attempts.append({"selection": retry_selection, "verification": second_verification})
                candidates = (candidates + retry_candidates)[:20]

                if actual_score(second_verification) > actual_score(first_verification):
                    chosen_tmp = second_tmp
                    final_selection = retry_selection
                    verification = second_verification
                else:
                    second_tmp.unlink(missing_ok=True)

        if out.exists():
            out.unlink()
        chosen_tmp.replace(out)
        for candidate_tmp in (temp_path(1), temp_path(2)):
            if candidate_tmp.exists() and candidate_tmp != out:
                candidate_tmp.unlink(missing_ok=True)

        selection = final_selection
        if len(attempts) > 1:
            print(
                f"Post-encode verification retried {len(attempts)-1} candidate(s); "
                f"kept {selection['mode']} with boundary ratio {verification['boundary_ratio']:.3f}"
            )

        preview = None
        preview_timeline = None
        if args.preview_repeats > 1:
            suffix = f"_preview_x{args.preview_repeats}"
            if args.preview_timeline:
                suffix += "_timeline"
            preview = out.with_name(f"{out.stem}{suffix}{out.suffix}")
            if args.preview_timeline:
                preview_timeline = render_preview_timeline(
                    out, preview, info, selection, verification, len(frames), args.preview_repeats, args,
                )
            else:
                render_preview_plain(out, preview, args.preview_repeats, args)

        report = {
            "input": str(args.input.resolve()),
            "output": str(out),
            "media": info,
            "audio_mode": args.audio,
            "video_blend_curve": VIDEO_BLEND_CURVE,
            "audio_blend_curve": AUDIO_BLEND_CURVE if args.audio == "crossfade" and info["has_audio"] else None,
            "selection": selection,
            "top_candidates": candidates[:10],
            "verification": verification,
            "render_attempts": attempts,
            "preview": str(preview) if preview else None,
            "preview_timeline": preview_timeline,
        }
        report_path = args.report or out.with_suffix(".json")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"Output: {out}")
        print(f"Boundary / median frame-diff ratio: {verification['boundary_ratio']:.3f}")
        print(f"Report: {report_path}")
        if preview:
            print(f"Preview: {preview}")
        return 0
    except Exception as ex:
        print(ex, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
