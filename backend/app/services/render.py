import re
import shutil
import subprocess
import time
from pathlib import Path

from ..core.config import settings
from ..core.errors import Cancelled, PipelineError
from . import captions

# Quality presets. "standard" is exactly what was used before, so nothing changes unless VIDEO_QUALITY=high is set.
PRESETS = {
    "standard": {"dims": {"16:9": (1280, 720), "9:16": (720, 1280), "1:1": (1024, 1024)},
                 "crf": "26", "maxrate": "2200k", "bufsize": "4400k", "abitrate": "128k", "oversample": 2.0},
    "high": {"dims": {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)},
             "crf": "22", "maxrate": "5000k", "bufsize": "10000k", "abitrate": "160k", "oversample": 1.5},
}


DIMS = PRESETS["standard"]["dims"]  # kept: images.py, stock.py and video_ai.py import it, so it must still exist


def _preset():
    return PRESETS.get((settings.video_quality or "standard").strip().lower(), PRESETS["standard"])


SUB_SIZE = {"16:9": 14, "9:16": 9, "1:1": 12}
# Subtitle margins in libass units (384x288 grid). For 9:16 the text sits above the username/caption/buttons that
# Facebook and Instagram draw over the bottom ~25% of a Reel, and away from the sides that tall phones crop.
SUB_MARGIN = {"16:9": "MarginV=22", "9:16": "MarginV=72,MarginL=40,MarginR=58", "1:1": "MarginV=22"}
FPS, T = 24, 0.5  # frame rate, crossfade seconds


def _ts(s: float) -> str:
    ms = int(round(s * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def _chunks(text: str) -> list[str]:
    out = []
    for s in re.split(r"(?<=[.!?。！？])\s+", text.strip()):
        while len(s) > 44:  # short lines stay in step with the voice and fit in two lines
            cut = s.rfind(" ", 0, 44)
            cut = cut if cut > 14 else 44
            out.append(s[:cut].strip())
            s = s[cut:].strip()
        if s:
            out.append(s)
    return out or [text]


def write_srt(path: Path, scenes, timings: list[tuple[float, float]]) -> None:
    lines, n = [], 1
    for sc, (start, speech) in zip(scenes, timings):
        parts = _chunks(sc.narration)  # subtitles always match what is spoken
        total, t = sum(len(p) for p in parts), start + 0.1
        for p in parts:
            dur = speech * len(p) / total
            lines += [str(n), f"{_ts(t)} --> {_ts(t + dur)}", p, ""]
            t, n = t + dur, n + 1
    path.write_text("\n".join(lines), encoding="utf-8")


def _run(cmd: list[str], cwd: Path, tmp: Path, total: float, check, on_progress) -> None:
    log, prog = tmp / "ffmpeg.log", tmp / "progress.txt"
    with open(log, "wb") as lf:
        proc = subprocess.Popen(cmd, cwd=Path(cwd).resolve(), stdout=lf, stderr=lf)
        started = time.time()
        while True:
            try:
                proc.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                pass
            try:
                check()
            except Cancelled:
                proc.kill(); proc.wait()
                raise
            if time.time() - started > settings.render_timeout:
                proc.kill(); proc.wait()
                raise PipelineError("Rendering timed out. Choose a shorter duration or raise RENDER_TIMEOUT.")
            try:
                m = re.findall(r"out_time_(?:us|ms)=(\d+)", prog.read_text())
                if m:
                    on_progress(min(0.99, int(m[-1]) / 1e6 / total))
            except OSError:
                pass
    if proc.returncode != 0:
        tail = log.read_text(errors="replace")[-600:]
        raise PipelineError(f"FFmpeg failed (exit {proc.returncode}). {tail}")


FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"


def write_ass(pdir: Path, scenes, starts: list[float], speech: list[float], cues, W: int, H: int, aspect: str, total: float) -> None:
    """Word-by-word highlighted captions (see captions.py) plus the bundled font next to them for FFmpeg to find."""
    flat: list[tuple[str, float, float, str]] = []
    for i, sc in enumerate(scenes):
        if cues and i < len(cues) and cues[i]:
            flat += [(text, starts[i] + off, dur, emo) for text, off, dur, emo in cues[i]]
        else:
            flat += captions.cues_from_narration(sc.narration, starts[i], speech[i], i == len(scenes) - 1)
    (pdir / "fonts").mkdir(exist_ok=True)
    for f in FONT_DIR.glob("*.ttf"):
        shutil.copy(f, pdir / "fonts" / f.name)
    (pdir / "subtitles.ass").write_text(captions.build_ass(flat, W, H, aspect, total), encoding="utf-8")


def render_video(project: dict, scenes, durations: list[float], speech: list[float], pdir: Path,
                 tmp: Path, check, on_progress, music: Path | None = None, sfx: dict | None = None,
                 music_external: bool = False, cues=None) -> Path:
    q = _preset()
    W, H = q["dims"][project["aspect_ratio"]]
    OW, OH = int(W * q["oversample"]) // 2 * 2, int(H * q["oversample"]) // 2 * 2  # zoom canvas (even numbers)
    n, total = len(scenes), sum(durations)
    starts = [sum(durations[:i]) for i in range(n)]
    write_srt(pdir / "subtitles.srt", scenes, list(zip(starts, speech)))  # plain copy for download; the video burns in the .ass
    write_ass(pdir, scenes, starts, speech, cues, W, H, project["aspect_ratio"], total)

    args: list[str] = []
    count = [0]

    def add(*a: str) -> int:
        args.extend(a)
        count[0] += 1
        return count[0] - 1

    def loop(name: str, length: float) -> int:
        return add("-loop", "1", "-framerate", str(FPS), "-t", f"{length:.3f}", "-i", name)

    f: list[str] = []
    for i, d in enumerate(durations):
        L, k = d + T, i + 1
        name = f"scene_{k:02d}"
        if (pdir / f"{name}_clip.mp4").exists():
            # Real stock footage under the caption chip. Loops if shorter than the scene.
            ic = add("-stream_loop", "-1", "-t", f"{L:.3f}", "-i", f"{name}_clip.mp4")
            it = loop(f"{name}_text.png", L)
            f += [
                f"[{ic}:v]setpts=PTS-STARTPTS,scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1,format=rgba[bg{i}]",
                f"[{it}:v]format=rgba,fade=t=in:st=0.2:d=0.6:alpha=1[tx{i}]",
                f"[bg{i}][tx{i}]overlay=x=0:y='18*(1-min(t/0.6,1))':format=auto,format=yuv420p,setsar=1[v{i}]",
            ]
        elif (pdir / f"{name}_bg.png").exists():
            # Placeholder visuals: animate separate layers (two shape layers drift at different speeds, caption fades in).
            ib, ia, ib2, it = (loop(f"{name}_{part}.png", L) for part in ("bg", "a", "b", "text"))
            pa, pb = 7 + (i % 3) * 1.5, 9 + (i % 4) * 1.2
            ax, ay, bx, by = 0.11 * W, 0.09 * H, 0.08 * W, 0.12 * H
            f += [
                f"[{ib}:v]scale={W}:{H},setsar=1,format=rgba[bg{i}]",
                f"[{ia}:v]format=rgba[sa{i}]",
                f"[{ib2}:v]format=rgba[sb{i}]",
                f"[{it}:v]format=rgba,fade=t=in:st=0.2:d=0.6:alpha=1[tx{i}]",
                f"[bg{i}][sa{i}]overlay=x='(main_w-overlay_w)/2+{ax:.1f}*sin(2*PI*t/{pa})':y='(main_h-overlay_h)/2+{ay:.1f}*cos(2*PI*t/{pa})':format=auto[m1_{i}]",
                f"[m1_{i}][sb{i}]overlay=x='(main_w-overlay_w)/2-{bx:.1f}*cos(2*PI*t/{pb})':y='(main_h-overlay_h)/2+{by:.1f}*sin(2*PI*t/{pb})':format=auto[m2_{i}]",
                f"[m2_{i}][tx{i}]overlay=x=0:y='18*(1-min(t/0.6,1))':format=auto,fps={FPS},format=yuv420p,setsar=1[v{i}]",
            ]
        else:
            idx = loop(f"{name}.png", L)
            base = f"[{idx}:v]scale={OW}:{OH}:force_original_aspect_ratio=increase,crop={OW}:{OH}"
            if settings.ken_burns:
                rate = 0.08 / max(1, int(L * FPS))
                f.append(f"{base},zoompan=z='1+{rate:.7f}*on':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={W}x{H}:fps={FPS},setsar=1,format=yuv420p[v{i}]")
            else:
                f.append(f"{base},scale={W}:{H},fps={FPS},setsar=1,format=yuv420p[v{i}]")

    last = "v0"
    for k in range(1, n):
        f.append(f"[{last}][v{k}]xfade=transition=fade:duration={T}:offset={starts[k]:.3f}[x{k}]")
        last = f"x{k}"
    f.append(f"[{last}]subtitles=subtitles.ass:fontsdir=fonts[vout]")

    a_idx = [add("-i", f"scene_{i + 1:02d}.wav") for i in range(n)]
    for i, ai in enumerate(a_idx):  # voices may use any sample rate or channel count; normalise before joining
        f.append(f"[{ai}:a]aresample=44100,aformat=channel_layouts=stereo[a{i}]")
    f.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[narr]")

    stereo = "aresample=44100,aformat=channel_layouts=stereo"
    mix = ["narr"]
    if music:  # original music, quieter than the voice and ducked while the narrator speaks
        mi = add("-stream_loop", "-1", "-i", str(Path(music).resolve()))
        f.append("[narr]asplit=2[nmain][nside]")
        norm = ",loudnorm=I=-24:LRA=7:TP=-3,aresample=44100" if music_external else ""  # your own tracks vary a lot in loudness
        f.append(f"[{mi}:a]{stereo}{norm},volume=0.45,atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
                 f"afade=t=in:d=1.5,afade=t=out:st={max(0, total - 2.5):.3f}:d=2.5[bgm]")
        f.append("[bgm][nside]sidechaincompress=threshold=0.03:ratio=10:attack=15:release=400[duck]")
        mix = ["nmain", "duck"]
    if sfx:
        events = {"whoosh": [(starts[k] - 0.2, 0.5) for k in range(1, n)],
                  "pop": [(starts[k] + 0.25, 0.3) for k in range(n)],
                  "chime": [(max(0.0, total - 2.4), 0.5)],
                  "impact": [(0.0, 0.55)],                                          # deep hit as the video opens
                  "riser": [(starts[n - 1] - 1.1, 0.3)] if n >= 3 else []}          # build-up into the last scene
        for kind, evs in events.items():
            if not evs or kind not in sfx:
                continue
            si = add("-i", str(Path(sfx[kind]).resolve()))
            f.append(f"[{si}:a]{stereo},asplit={len(evs)}" + "".join(f"[{kind}{j}]" for j in range(len(evs))))
            for j, (t0, vol) in enumerate(evs):
                ms = max(0, int(t0 * 1000))
                f.append(f"[{kind}{j}]adelay={ms}|{ms},volume={vol}[e_{kind}{j}]")
                mix.append(f"e_{kind}{j}")
    if len(mix) > 1:
        f.append("".join(f"[{m}]" for m in mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0,alimiter=limit=0.95[aout]")
    else:
        f.append("[narr]anull[aout]")

    out = (tmp / "out.mp4").resolve()
    nt = settings.ffmpeg_threads
    threads = ["-filter_threads", str(nt), "-filter_complex_threads", str(nt)] if nt > 0 else []
    enc_threads = ["-threads", str(nt)] if nt > 0 else []
    cmd = [settings.ffmpeg_path, "-y", "-hide_banner", "-nostats", "-progress", str((tmp / "progress.txt").resolve()),
           *threads, *args, "-filter_complex", ";".join(f), "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}",
           *enc_threads, "-c:v", "libx264", "-preset", "veryfast", "-crf", _preset()["crf"], "-maxrate", _preset()["maxrate"], "-bufsize", _preset()["bufsize"],
           "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", _preset()["abitrate"], "-movflags", "+faststart", str(out)]
    if shutil.which(settings.ffmpeg_path) is None:
        raise PipelineError("FFmpeg isn't installed or isn't on PATH. Install it (https://ffmpeg.org) or set FFMPEG_PATH.")
    _run(cmd, pdir, tmp, total, check, on_progress)
    return out