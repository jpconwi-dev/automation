"""Voice lists and short previews for the voice picker."""
import os
import subprocess
import threading
import uuid
from functools import lru_cache
from pathlib import Path

from ..core.config import settings
from ..core.errors import PipelineError
from . import tts

TONE_RATE = {"friendly": 1.0, "professional": 1.0, "energetic": 1.0, "calm": 0.9, "humorous": 1.0, "dramatic": 0.92}
SAMPLE = {"en-us": "Hello! This is how your explainer video will sound.", "en-gb": "Hello! This is how your explainer video will sound.",
          "es": "¡Hola! Así sonará tu video explicativo.", "fr": "Bonjour ! Voici à quoi ressemblera votre vidéo explicative.",
          "hi": "नमस्ते! आपका वीडियो ऐसा सुनाई देगा।", "it": "Ciao! Ecco come suonerà il tuo video.",
          "ja": "こんにちは。これがあなたの動画の声です。", "pt-br": "Olá! É assim que o seu vídeo vai soar.",
          "zh": "你好！你的视频听起来就是这样。"}
KOKORO = {"en-us": [("af_heart", "Heart", "female"), ("af_bella", "Bella", "female"), ("af_nicole", "Nicole", "female"),
                    ("am_adam", "Adam", "male"), ("am_michael", "Michael", "male")],
          "en-gb": [("bf_emma", "Emma", "female"), ("bf_isabella", "Isabella", "female"), ("bm_george", "George", "male"),
                    ("bm_lewis", "Lewis", "male")],
          "es": [("ef_dora", "Dora", "female"), ("em_alex", "Alex", "male")], "fr": [("ff_siwis", "Siwis", "female")],
          "hi": [("hf_alpha", "Alpha", "female"), ("hm_omega", "Omega", "male")],
          "it": [("if_sara", "Sara", "female"), ("im_nicola", "Nicola", "male")],
          "ja": [("jf_alpha", "Alpha", "female"), ("jm_kumo", "Kumo", "male")],
          "pt-br": [("pf_dora", "Dora", "female"), ("pm_alex", "Alex", "male")],
          "zh": [("zf_xiaobei", "Xiaobei", "female"), ("zm_yunjian", "Yunjian", "male")]}
LIST_PS = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object { $i = $_.VoiceInfo; ("{0}|{1}|{2}" -f $i.Name, $i.Culture.Name, $i.Gender) }
"""
_lock = threading.Lock()


@lru_cache(maxsize=1)
def _windows_voices() -> tuple:
    if os.name != "nt":
        return ()
    ps1 = (settings.tmp_dir / "voices.ps1").resolve()
    ps1.parent.mkdir(parents=True, exist_ok=True)
    ps1.write_text(LIST_PS, encoding="utf-8-sig")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ()
    out = []
    for line in r.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 3:
            out.append((parts[0], parts[1], parts[2].lower()))
    return tuple(out)


def list_voices(language: str) -> dict:
    backend = settings.tts_backend
    if backend == "windows":
        exact, prefix = tts.WIN_CULTURE[language]
        rows = [v for v in _windows_voices() if v[1].lower().startswith(prefix)]
        rows.sort(key=lambda v: v[1] != exact)  # exact region first
        voices = [{"id": n, "label": f"{n.replace('Microsoft ', '').replace(' Desktop', '')} ({c})", "gender": g}
                  for n, c, g in rows]
    elif backend == "cloudflare":
        voices = []  # MeloTTS on Cloudflare has one voice per language; "auto" is the only choice
    elif backend == "kokoro":
        voices = [{"id": i, "label": n, "gender": g} for i, n, g in KOKORO.get(language, [])]
    else:
        voices = []
    return {"backend": backend, "voices": voices}


def effective_speed(tone: str, speed: float) -> float:
    return max(0.7, min(1.6, speed * TONE_RATE.get(tone, 1.0)))


def make_preview(language: str, voice: str, tone: str, speed: float) -> Path:
    if settings.tts_backend == "none":
        raise PipelineError("Narration is turned off on this server (TTS_BACKEND=none), so there is no voice to preview.")
    if voice != "auto" and voice not in {v["id"] for v in list_voices(language)["voices"]}:
        raise PipelineError("That voice isn't available for this language.")
    out = settings.tmp_dir / f"preview_{uuid.uuid4().hex}.wav"
    out.parent.mkdir(parents=True, exist_ok=True)
    with _lock:  # one preview at a time keeps a busy laptop responsive
        tts.synthesize(SAMPLE[language], language, out, voice, effective_speed(tone, speed))
    return out
