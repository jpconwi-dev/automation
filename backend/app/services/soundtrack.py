"""Original music and sound effects, synthesised with FFmpeg. Nothing is downloaded and nothing needs a licence.

The music is a 16-second chord loop (tiny file) that the renderer repeats; effects are three short mono WAVs.
"""
import subprocess
from pathlib import Path

from ..core.config import settings
from ..core.errors import PipelineError

AUTO_STYLE = {"friendly": "calm", "professional": "calm", "energetic": "upbeat", "calm": "calm",
              "humorous": "playful", "dramatic": "cinematic"}
# four chords of 4 seconds each (Hz); pulse = rhythmic beats per second, trem/depth = slow swell
PROGRESSIONS = {
    "calm": ([(130.81, 196.00, 261.63, 329.63), (110.00, 164.81, 220.00, 261.63), (87.31, 130.81, 174.61, 261.63),
              (98.00, 146.83, 196.00, 246.94)], dict(trem=0.2, depth=0.25, pulse=0, lp=1400)),
    "upbeat": ([(261.63, 329.63, 392.00, 523.25), (220.00, 261.63, 329.63, 440.00), (174.61, 261.63, 349.23, 440.00),
                (196.00, 246.94, 392.00, 493.88)], dict(trem=0, depth=0, pulse=2, lp=3000)),
    "cinematic": ([(110.00, 164.81, 220.00, 261.63), (87.31, 130.81, 174.61, 220.00), (130.81, 196.00, 261.63, 329.63),
                   (98.00, 146.83, 196.00, 293.66)], dict(trem=0.12, depth=0.4, pulse=0, lp=1100)),
    "playful": ([(392.00, 493.88, 587.33, 783.99), (349.23, 440.00, 523.25, 698.46), (329.63, 392.00, 493.88, 659.25),
                 (293.66, 392.00, 440.00, 587.33)], dict(trem=0, depth=0, pulse=4, lp=3500)),
}


def resolve_style(style: str, tone: str) -> str:
    return AUTO_STYLE.get(tone, "calm") if style == "auto" else style


def _ffmpeg(graph: str, out: Path, label: str = "o") -> None:
    cmd = [settings.ffmpeg_path, "-y", "-v", "error", "-filter_complex", graph, "-map", f"[{label}]",
           "-ac", "1", "-ar", "22050", "-c:a", "pcm_s16le", str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        raise PipelineError(f"Couldn't run FFmpeg to make sound: {e}")
    if r.returncode != 0:
        raise PipelineError(f"FFmpeg couldn't synthesise audio: {r.stderr[-300:]}")


def make_music(style: str, out: Path) -> Path:
    chords, p = PROGRESSIONS[style]
    parts = []
    for i, freqs in enumerate(chords):
        body = "+".join(f"0.12*sin(2*PI*{f}*t)" for f in freqs)
        if style == "cinematic":
            body += f"+0.18*sin(2*PI*{freqs[0] / 2}*t)"  # sub bass
        env = "1"
        if p["trem"]:
            env = f"(1-{p['depth']}+{p['depth']}*0.5*(1+sin(2*PI*{p['trem']}*t)))"
        if p["pulse"]:
            env = f"(0.45+0.55*pow(abs(sin(PI*{p['pulse']}*t)),0.6))"
        parts.append(f"aevalsrc='({body})*{env}':s=22050:d=4:c=mono,afade=t=in:d=0.08,afade=t=out:st=3.92:d=0.08[c{i}]")
    graph = ";".join(parts) + f";[c0][c1][c2][c3]concat=n=4:v=0:a=1,lowpass=f={p['lp']}[o]"
    _ffmpeg(graph, out)
    return out


def make_sfx(folder: Path) -> dict[str, Path]:
    folder.mkdir(parents=True, exist_ok=True)
    sources = {
        "whoosh": "anoisesrc=d=0.9:c=pink:r=22050:a=0.6,highpass=f=250,lowpass=f=3000,afade=t=in:d=0.4,afade=t=out:st=0.4:d=0.5[o]",
        "pop": "aevalsrc='0.5*sin(2*PI*(700+500*t/0.12)*t)*exp(-30*t)':d=0.15:s=22050:c=mono[o]",
        "chime": "aevalsrc='0.3*exp(-2.2*t)*(sin(2*PI*1046.5*t)+0.6*sin(2*PI*1568*t))':d=2.4:s=22050:c=mono[o]",
    }
    out = {}
    for name, graph in sources.items():
        out[name] = folder / f"sfx_{name}.wav"
        _ffmpeg(graph, out[name])
    return out
