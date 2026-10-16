"""Audio self-check. Run from the backend folder with the venv active:

    python check_audio.py              # tests the voice, then the newest project's video
    python check_audio.py PROJECT_ID   # tests a specific project

It tells you which link in the chain is broken: voice settings, voice output, per-scene audio, or the final video.
"""
import array
import os
import re
import subprocess
import sys
import wave
from pathlib import Path

from app.core.config import settings
from app.core.errors import PipelineError
from app.services import tts


def wav_info(path: Path):
    with wave.open(str(path), "rb") as w:
        ch, rate, n = w.getnchannels(), w.getframerate(), w.getnframes()
        data = w.readframes(n)
    a = array.array("h")
    a.frombytes(data[: len(data) // 2 * 2])
    return rate, n / rate, max((abs(x) for x in a), default=0)


def verdict(peak: int) -> str:
    return "SILENT" if peak < 200 else "has sound"


print(f"TTS_BACKEND = {settings.tts_backend}   (read from backend/.env)")
if settings.tts_backend == "none":
    print("-> Narration is OFF by design. Set TTS_BACKEND=windows in backend/.env and restart the backend.")
    sys.exit(0)

print("\n[1] Testing the voice on its own...")
test = settings.tmp_dir / "voice_test.wav"
test.parent.mkdir(parents=True, exist_ok=True)
try:
    secs = tts.synthesize("This is a test of the narration voice.", "en-us", test)
    rate, dur, peak = wav_info(test)
    print(f"    wrote {test.name}: {dur:.1f}s at {rate} Hz, loudest sample {peak} -> {verdict(peak)}")
    if os.name == "nt":
        import winsound
        print("    Playing it now. Do you hear a voice?")
        winsound.PlaySound(str(test), winsound.SND_FILENAME)
except PipelineError as e:
    print(f"    VOICE FAILED: {e}")
    sys.exit(1)

print("\n[2] Checking the newest project...")
projects = sorted((p for p in settings.projects_dir.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
pid = sys.argv[1] if len(sys.argv) > 1 else (projects[-1].name if projects else None)
if not pid:
    print("    No projects yet. Generate a video first, then run this again.")
    sys.exit(0)
pdir = settings.projects_dir / pid
print(f"    project {pid}")
for w in sorted(pdir.glob("scene_??.wav")):
    rate, dur, peak = wav_info(w)
    print(f"    {w.name}: {dur:.1f}s, loudest {peak} -> {verdict(peak)}")
video = pdir / "video.mp4"
if not video.exists():
    print("    No video.mp4 in this project (generation may have failed).")
    sys.exit(0)
r = subprocess.run([settings.ffmpeg_path, "-hide_banner", "-i", str(video), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                   capture_output=True, text=True)
m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr)
has_audio = "Audio:" in r.stderr
print(f"    video.mp4 audio track: {'yes' if has_audio else 'NO'}", end="")
if m:
    mv = float(m.group(1))
    print(f", mean volume {mv} dB -> {'has sound' if mv > -60 else 'SILENT'}")
else:
    print()
print("\nHow to read this:")
print(" - [1] SILENT or FAILED  -> the Windows voice is the problem (see message above).")
print(" - [1] ok but scene wavs SILENT -> an old project made before TTS_BACKEND=windows; generate a new one.")
print(" - wavs ok but video SILENT -> send me this whole output.")
print(" - video has sound but you hear nothing in the browser -> unmute the player / raise the tab volume.")
