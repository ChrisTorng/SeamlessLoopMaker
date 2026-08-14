#!/usr/bin/env python3
from __future__ import annotations

import argparse, heapq, json, math, shutil, subprocess, sys
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np


def run(cmd):
    return subprocess.run(cmd, text=True, capture_output=True, encoding="utf-8", errors="replace")


def probe(path, ffprobe):
    p = run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    if p.returncode:
        raise RuntimeError(p.stderr)
    d = json.loads(p.stdout)
    v = next((x for x in d["streams"] if x.get("codec_type") == "video"), None)
    if not v:
        raise RuntimeError("Input has no video stream")
    rate = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/0"
    fps = float(Fraction(rate)) if rate != "0/0" else 0.0
    return {
        "fps": fps,
        "width": int(v.get("width", 0)),
        "height": int(v.get("height", 0)),
        "duration": float(v.get("duration") or d.get("format", {}).get("duration") or 0),
        "has_audio": any(x.get("codec_type") == "audio" for x in d["streams"]),
    }


def read_frames(path, width):
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open {path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
    out = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        h, w = f.shape[:2]
        if w > width:
            f = cv2.resize(f, (width, max(2, round(h * width / w))), interpolation=cv2.INTER_AREA)
        out.append(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY))
    cap.release()
    if len(out) < 12:
        raise RuntimeError("Video is too short")
    return np.stack(out), fps


def ssim(a, b):
    a, b = a.astype(np.float32), b.astype(np.float32)
    c1, c2 = (2.55 ** 2), (7.65 ** 2)
    ma, mb = cv2.GaussianBlur(a, (11, 11), 1.5), cv2.GaussianBlur(b, (11, 11), 1.5)
    va = cv2.GaussianBlur(a*a, (11, 11), 1.5) - ma*ma
    vb = cv2.GaussianBlur(b*b, (11, 11), 1.5) - mb*mb
    vab = cv2.GaussianBlur(a*b, (11, 11), 1.5) - ma*mb
    return float(np.mean(((2*ma*mb+c1)*(2*vab+c2))/((ma*ma+mb*mb+c1)*(va+vb+c2)+1e-12)))


def flow_desc(flow, gx=8, gy=4):
    h, w = flow.shape[:2]
    a = []
    for y in range(gy):
        for x in range(gx):
            c = flow[y*h//gy:(y+1)*h//gy, x*w//gx:(x+1)*w//gx]
            m = np.linalg.norm(c, axis=2)
            a += [c[...,0].mean(), c[...,1].mean(), m.mean()]
    a = np.asarray(a, np.float32)
    return a / (np.linalg.norm(a) + 1e-8)


def features(frames):
    h, w = frames.shape[1:]
    fw, fh = min(160, w), max(2, round(h * min(160, w) / w))
    small = np.stack([cv2.resize(f, (fw, fh), interpolation=cv2.INTER_AREA) for f in frames])
    desc, mag = [], []
    for i in range(len(small)-1):
        fl = cv2.calcOpticalFlowFarneback(small[i], small[i+1], None, .5, 3, 15, 3, 5, 1.2, 0)
        desc.append(flow_desc(fl)); mag.append(np.linalg.norm(fl, axis=2).mean())
    arr = frames.astype(np.float32)/255
    mad = np.mean(np.abs(arr[1:]-arr[:-1]), axis=(1,2))
    tiny = np.stack([cv2.resize(f, (80,44), interpolation=cv2.INTER_AREA) for f in frames]).astype(np.float32)/255
    z = tiny.reshape(len(tiny), -1)
    z = (z-z.mean(1,keepdims=True))/(z.std(1,keepdims=True)+1e-6)/math.sqrt(z.shape[1])
    return np.stack(desc), np.asarray(mag), mad, z


def transition_score(frames, mad, s, e, k):
    clip = frames[s:e].astype(np.float32)/255
    head, tail = clip[:k], clip[-k:]
    a = np.linspace(0,1,k,np.float32)[:,None,None]
    blend = (1-a)*tail + a*head
    ds = [np.mean(np.abs(clip[len(clip)-k-1]-blend[0]))]
    ds += np.mean(np.abs(blend[1:]-blend[:-1]), axis=(1,2)).tolist()
    ds += [np.mean(np.abs(blend[-1]-clip[k]))]
    med = float(np.median(mad[s:e-1])) + 1e-9
    r = np.asarray(ds)/med
    natural = math.exp(-float(np.mean(np.abs(np.log(np.clip(r,1e-4,None))))))
    return natural, float(r.min()), float(r.max())


def choose(frames, fps, min_ratio, max_ratio, transition, shortlist):
    n, k, look = len(frames), max(2, round(transition*fps)), 3
    lo = max(2*k+12, round(n*min_ratio)); hi = min(n-look-1, round(n*max_ratio))
    if lo >= hi:
        raise RuntimeError("Duration constraints leave no search interval")
    desc, mag, mad, z = features(frames)
    heap = []
    for s in range(n-lo):
        for e in range(s+lo, min(s+hi, n-look-1)+1):
            corr = float(z[s] @ z[e])
            fs = float(np.mean(np.sum(desc[s:s+look]*desc[e:e+look], axis=1)))
            seg, med = mag[s:e], float(np.median(mag[s:e]))+1e-8
            seam = np.r_[mag[s:s+look], mag[e:e+look]]
            motion = float(np.clip(np.mean(np.minimum(seam/med, med/(seam+1e-8))),0,1))
            low = float(np.mean(seg < .6*med))
            q = .55*(corr+1)/2 + .25*(fs+1)/2 + .15*motion + .05*(1-low)
            item = (q,s,e,corr,fs,motion)
            if len(heap) < shortlist: heapq.heappush(heap,item)
            elif q > heap[0][0]: heapq.heapreplace(heap,item)
    best, top = None, []
    target = max(.1, n/fps*.87)
    for _,s,e,corr,fs,motion in sorted(heap, reverse=True):
        ss = ssim(frames[s],frames[e]); nat,rmin,rmax = transition_score(frames,mad,s,e,k)
        dur = (e-s-k)/fps
        worst = math.exp(-.5*max(abs(math.log(max(rmin,1e-4))),abs(math.log(max(rmax,1e-4)))))
        score = .34*ss + .23*(fs+1)/2 + .27*nat + .08*worst + .08*min(1,dur/target)
        x = {"start_frame":s,"end_frame":e,"transition_frames":k,"score":score,"ssim":ss,
             "flow_similarity":fs,"transition_naturalness":nat,"transition_min_ratio":rmin,
             "transition_max_ratio":rmax,"output_duration":dur,"fast_correlation":corr,
             "boundary_motion_level":motion}
        top.append(x)
        if best is None or score > best["score"]: best = x
    top.sort(key=lambda x:x["score"], reverse=True)
    return best, top[:10]


def filter_graph(info, x, audio):
    fps, s, e, k = info["fps"], x["start_frame"], x["end_frame"], x["transition_frames"]
    L, den = e-s, max(1,k-1)
    f = [
        f"[0:v]trim=start_frame={s}:end_frame={e},setpts=N/{fps:.12f}/TB[v]",
        "[v]split=3[vh][vm][vt]",
        f"[vh]trim=start_frame=0:end_frame={k},setpts=N/{fps:.12f}/TB[head]",
        f"[vm]trim=start_frame={k}:end_frame={L-k},setpts=N/{fps:.12f}/TB[mid]",
        f"[vt]trim=start_frame={L-k}:end_frame={L},setpts=N/{fps:.12f}/TB[tail]",
        f"[tail][head]blend=all_expr='A*(1-N/{den})+B*(N/{den})':shortest=1[blend]",
        f"[mid][blend]concat=n=2:v=1:a=0,fps={fps:.12f}[outv]",
    ]
    maps = ["-map","[outv]"]
    if audio == "crossfade" and info["has_audio"]:
        t0, dur, fade = s/fps, L/fps, k/fps
        f += [
            f"[0:a:0]atrim=start={t0+fade:.12f}:end={t0+dur-fade:.12f},asetpts=PTS-STARTPTS[amid]",
            f"[0:a:0]atrim=start={t0+dur-fade:.12f}:end={t0+dur:.12f},asetpts=PTS-STARTPTS,afade=t=out:st=0:d={fade:.12f}:curve=qsin[atail]",
            f"[0:a:0]atrim=start={t0:.12f}:end={t0+fade:.12f},asetpts=PTS-STARTPTS,afade=t=in:st=0:d={fade:.12f}:curve=qsin[ahead]",
            "[atail][ahead]amix=inputs=2:duration=shortest:dropout_transition=0:normalize=0,asetpts=PTS-STARTPTS[ablend]",
            "[amid][ablend]concat=n=2:v=0:a=1[outa]",
        ]; maps += ["-map","[outa]"]
    return ";".join(f), maps


def render(src, dst, info, x, args):
    fg, maps = filter_graph(info,x,args.audio)
    cmd = [args.ffmpeg,"-y","-hide_banner","-loglevel","error","-i",str(src),"-filter_complex",fg,*maps,
           "-c:v","libx264","-preset",args.preset,"-crf",str(args.crf),"-pix_fmt","yuv420p"]
    if args.audio == "crossfade" and info["has_audio"]: cmd += ["-c:a","aac","-b:a","192k"]
    else: cmd += ["-an"]
    p = run(cmd + ["-movflags","+faststart",str(dst)])
    if p.returncode: raise RuntimeError(p.stderr)


def verify(path, width):
    f,fps = read_frames(path,width); a=f.astype(np.float32)/255
    d=np.mean(np.abs(a[1:]-a[:-1]),axis=(1,2)); b=float(np.mean(np.abs(a[-1]-a[0]))); m=float(np.median(d))+1e-9
    return {"frames":len(f),"fps":fps,"duration":len(f)/fps,"boundary_diff":b,"internal_median_diff":m,
            "boundary_ratio":b/m,"min_internal_diff":float(d.min()),"max_internal_diff":float(d.max())}


def main():
    p=argparse.ArgumentParser(description="Automatically trim and crossfade a short video into a smoother seamless loop")
    p.add_argument("input",type=Path); p.add_argument("output",type=Path,nargs="?")
    p.add_argument("--audio",choices=("mute","crossfade"),default="mute")
    p.add_argument("--transition",type=float,default=.50); p.add_argument("--min-duration-ratio",type=float,default=.75)
    p.add_argument("--max-duration-ratio",type=float,default=.98); p.add_argument("--analysis-width",type=int,default=320)
    p.add_argument("--shortlist",type=int,default=150); p.add_argument("--preview-repeats",type=int,default=0)
    p.add_argument("--report",type=Path); p.add_argument("--crf",type=int,default=18); p.add_argument("--preset",default="medium")
    p.add_argument("--ffmpeg",default="ffmpeg"); p.add_argument("--ffprobe",default="ffprobe"); a=p.parse_args()
    if not a.input.exists(): return print("Input not found",file=sys.stderr) or 2
    if not shutil.which(a.ffmpeg) or not shutil.which(a.ffprobe): return print("ffmpeg/ffprobe not found",file=sys.stderr) or 2
    if not (.4 <= a.min_duration_ratio < a.max_duration_ratio <= 1): return print("Invalid duration ratios",file=sys.stderr) or 2
    out=(a.output or a.input.with_name(a.input.stem+"_loop.mp4")).resolve(); out.parent.mkdir(parents=True,exist_ok=True)
    try:
        info=probe(a.input,a.ffprobe); frames,cvfps=read_frames(a.input,a.analysis_width); info["fps"]=cvfps or info["fps"]
        print(f"Input: {info['width']}x{info['height']}, {info['fps']:.6g} fps, {len(frames)} frames, {info['duration']:.3f} s")
        x,top=choose(frames,info["fps"],a.min_duration_ratio,a.max_duration_ratio,a.transition,a.shortlist)
        print(f"Selected frames {x['start_frame']}..{x['end_frame']-1}, transition {x['transition_frames']} frames, output {x['output_duration']:.3f} s")
        render(a.input,out,info,x,a); v=verify(out,a.analysis_width)
        preview=None
        if a.preview_repeats>1:
            preview=out.with_name(f"{out.stem}_preview_x{a.preview_repeats}{out.suffix}")
            q=run([a.ffmpeg,"-y","-hide_banner","-loglevel","error","-stream_loop",str(a.preview_repeats-1),"-i",str(out),"-c","copy",str(preview)])
            if q.returncode: raise RuntimeError(q.stderr)
        report={"input":str(a.input.resolve()),"output":str(out),"media":info,"audio_mode":a.audio,"selection":x,"top_candidates":top,"verification":v,"preview":str(preview) if preview else None}
        rp=(a.report or out.with_suffix(".json")); rp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(f"Output: {out}\nBoundary / median frame-diff ratio: {v['boundary_ratio']:.3f}\nReport: {rp}")
        if preview: print(f"Preview: {preview}")
        return 0
    except Exception as ex:
        print(ex,file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
