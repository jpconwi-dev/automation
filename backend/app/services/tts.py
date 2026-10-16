import importlib.util
import os
import subprocess
import wave
from pathlib import Path

from ..core.config import settings
from ..core.errors import PipelineError

SAMPLE_RATE = 24000
# language -> (Kokoro lang_code, default voice). Voice files come from hexgrad/Kokoro-82M.
LANG = {"en-us": ("a", "af_heart"), "en-gb": ("b", "bf_emma"), "es": ("e", "ef_dora"), "fr": ("f", "ff_siwis"),
        "hi": ("h", "hf_alpha"), "it": ("i", "if_sara"), "ja": ("j", "jf_alpha"), "pt-br": ("p", "pf_dora"),
        "zh": ("z", "zf_xiaobei")}
# language -> (exact culture, prefix) for Windows voices
WIN_CULTURE = {"en-us": ("en-US", "en"), "en-gb": ("en-GB", "en"), "es": ("es-ES", "es"), "fr": ("fr-FR", "fr"),
               "hi": ("hi-IN", "hi"), "it": ("it-IT", "it"), "ja": ("ja-JP", "ja"), "pt-br": ("pt-BR", "pt"),
               "zh": ("zh-CN", "zh")}
_pipes: dict = {}

SAPI_SCRIPT = r"""
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices = @($s.GetInstalledVoices() | Where-Object { $_.Enabled })
$pick = $null
if ($env:TTS_VOICE) { $pick = $voices | Where-Object { $_.VoiceInfo.Name -eq $env:TTS_VOICE } | Select-Object -First 1 }
if (-not $pick) { $pick = $voices | Where-Object { $_.VoiceInfo.Culture.Name -eq $env:TTS_LANG } | Select-Object -First 1 }
if (-not $pick) { $pick = $voices | Where-Object { $_.VoiceInfo.Culture.Name -like ($env:TTS_PREFIX + "*") } | Select-Object -First 1 }
if (-not $pick) { [Console]::Error.WriteLine("NO_VOICE"); exit 3 }
$s.SelectVoice($pick.VoiceInfo.Name)
if ($env:TTS_RATE) { $s.Rate = [int]$env:TTS_RATE }
$s.SetOutputToWaveFile($env:TTS_OUT)
$s.Speak([System.IO.File]::ReadAllText($env:TTS_TEXTFILE, [System.Text.Encoding]::UTF8))
$s.Dispose()
"""


def _write(path, pcm: bytes, rate: int = SAMPLE_RATE, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


# language -> MeloTTS language code on Cloudflare. MeloTTS has no Hindi, Italian or Portuguese voice.
CF_LANG = {"en-us": "en", "en-gb": "en", "es": "es", "fr": "fr", "ja": "ja", "zh": "zh"}


def synthesize(text: str, language: str, out_path, voice: str = "auto", speed: float = 1.0) -> float:
    """Write narration to out_path (16-bit PCM WAV). Returns speech length in seconds."""
    if settings.tts_backend == "windows":
        return _synth_windows(text, language, out_path, voice, speed)
    if settings.tts_backend == "cloudflare":
        return _synth_cloudflare(text, language, out_path, speed)
    return _synth_kokoro(text, language, out_path, voice, speed)


def _synth_cloudflare(text: str, language: str, out_path, speed: float = 1.0) -> float:
    """Free MeloTTS on Cloudflare Workers AI. It returns an MP3; FFmpeg converts it to WAV and applies the speed."""
    import base64
    import shutil

    from . import cloudflare
    if language not in CF_LANG:
        raise PipelineError(f"MeloTTS on Cloudflare has no '{language}' voice. Choose English, Spanish, French, "
                            "Japanese or Chinese, or use another TTS_BACKEND.")
    if shutil.which(settings.ffmpeg_path) is None:
        raise PipelineError("FFmpeg isn't installed or isn't on PATH.")
    res = cloudflare.run(settings.cloudflare_tts_model, {"prompt": text, "lang": CF_LANG[language]})
    mp3 = res.get("_bytes") or (base64.b64decode(res["audio"]) if res.get("audio") else b"")
    if not mp3:
        raise PipelineError("Cloudflare returned no audio for this narration.")
    out = Path(out_path).resolve()
    src = out.with_suffix(".mp3")
    src.write_bytes(mp3)
    tempo = max(0.5, min(2.0, speed))
    cmd = [settings.ffmpeg_path, "-y", "-v", "error", "-i", str(src), "-filter:a", f"atempo={tempo:.3f}",
           "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(out)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        raise PipelineError(f"Couldn't convert narration audio: {e}")
    finally:
        src.unlink(missing_ok=True)
    if r.returncode != 0 or not out.is_file():
        raise PipelineError(f"Couldn't convert narration audio: {r.stderr[-200:]}")
    return wav_seconds(out)


def _synth_windows(text: str, language: str, out_path, voice: str = "auto", speed: float = 1.0) -> float:
    if os.name != "nt":
        raise PipelineError("TTS_BACKEND=windows only works when the backend runs on Windows. Use kokoro or none.")
    out = Path(out_path).resolve()
    ps1 = (settings.tmp_dir / "sapi.ps1").resolve()
    ps1.parent.mkdir(parents=True, exist_ok=True)
    ps1.write_text(SAPI_SCRIPT, encoding="utf-8-sig")
    txt = out.with_suffix(".txt")
    txt.write_text(text, encoding="utf-8")
    exact, prefix = WIN_CULTURE[language]
    env = {**os.environ, "TTS_TEXTFILE": str(txt), "TTS_OUT": str(out), "TTS_LANG": exact, "TTS_PREFIX": prefix,
           "TTS_VOICE": (voice if voice != "auto" else settings.windows_voice) or "",
           "TTS_RATE": str(max(-10, min(10, round((speed - 1) * 10))))}
    try:  # values travel in environment variables, so spaces and quotes in paths or text can't break the call
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
                           capture_output=True, text=True, timeout=180, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        raise PipelineError(f"Couldn't run Windows speech: {e}")
    finally:
        txt.unlink(missing_ok=True)
    if r.returncode == 3:
        raise PipelineError(f"No Windows voice is installed for '{language}'. Add one in Settings > Time & language > "
                            "Speech > Add voices, or choose English.")
    if r.returncode != 0 or not out.is_file():
        raise PipelineError(f"Windows speech failed: {(r.stderr or r.stdout)[:300]}")
    return wav_seconds(out)


def _synth_kokoro(text: str, language: str, out_path, voice: str = "auto", speed: float = 1.0) -> float:
    if importlib.util.find_spec("kokoro") is None:
        raise PipelineError("Kokoro TTS isn't installed. Run `pip install -r requirements-ai.txt` and install "
                            "espeak-ng, or set TTS_BACKEND=windows (no download) or none.")
    code, default_voice = LANG[language]
    voice = voice if voice != "auto" and voice[:1] == code else default_voice
    try:
        import numpy as np
        from kokoro import KPipeline
        if code not in _pipes:
            _pipes[code] = KPipeline(lang_code=code)
        chunks = []
        for _, _, audio in _pipes[code](text, voice=voice, speed=speed):
            chunks.append(audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio))
        if not chunks:
            raise PipelineError("The voice model produced no audio for this narration.")
        pcm = (np.clip(np.concatenate(chunks), -1, 1) * 32767).astype("<i2").tobytes()
    except PipelineError:
        raise
    except Exception as e:
        raise PipelineError(f"Narration failed for language '{language}' (voice {voice}): {e}. "
                            "Non-English voices need espeak-ng and extra packages such as misaki[ja] or misaki[zh].")
    _write(out_path, pcm)
    return len(pcm) / 2 / SAMPLE_RATE


def wav_seconds(path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / w.getframerate()


def pad_wav(path, total_seconds: float) -> None:
    """Make the file exactly total_seconds long: pad with silence, or cut if it is longer."""
    with wave.open(str(path), "rb") as w:
        ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        pcm = w.readframes(w.getnframes())
    want = int(total_seconds * rate) * ch * width
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes((pcm + b"\x00" * max(0, want - len(pcm)))[:want])


def write_silence(path, seconds: float) -> None:
    _write(path, b"\x00" * (int(seconds * SAMPLE_RATE) * 2))
