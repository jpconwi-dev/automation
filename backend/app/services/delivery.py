"""How a line should be SPOKEN: pace, pauses and how long to hold afterwards.

The free voices (MeloTTS on Cloudflare, Kokoro) have no emotion control, so "sentiment" here means the things that do
change how a line lands: speaking speed, a short pause before the line, and a hold after it. Each scene gets an
emotion, either from the script writer (the `emotion` key) or, when that is missing, from a small rule-based reading of
the sentence itself (question mark, exclamation, big numbers, awe words, warning words).

The closing question gets special care: it is spoken on its own, slower than the rest and never sped up to fit the
video length, with a beat of silence before it so it does not run into the sentence before.
"""
import re
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

from ..core.config import settings

EMOTIONS = ("neutral", "curious", "excited", "awe", "serious", "playful", "question")

# speed multiplier (on top of the voice speed), pause before the line, extra hold after it, pitch shift in semitones,
# gain in dB. The free voices cannot act, so these small shifts in pace, pitch and loudness are what make one sentence
# sound curious and the next one amazed instead of everything arriving in the same flat tone.
_PROFILE = {
    "neutral": (1.00, 0.00, 0.00, 0.0, 0.0),
    "curious": (0.98, 0.10, 0.10, 0.8, 0.0),
    "excited": (1.03, 0.00, 0.00, 1.2, 1.0),
    "awe": (0.96, 0.12, 0.25, -0.4, 1.0),
    "serious": (0.97, 0.08, 0.15, -1.0, -0.5),
    "playful": (1.02, 0.00, 0.00, 1.0, 0.5),
    "question": (0.88, 0.55, 1.30, 1.0, 0.5),
}
# The pace multipliers are deliberately small: the voice used to swing between fast and slow from sentence to sentence.
# Feeling comes from pitch, loudness and pauses; pace stays within a few percent of the video's steady speaking rate.
QUESTION_SPEED_FLOOR, QUESTION_SPEED_CEIL = 0.82, 0.98  # a question is never rushed, whatever the video length

_AWE = {"incredible", "astonishing", "impossible", "unbelievable", "massive", "enormous", "billion", "million",
        "trillion", "fastest", "largest", "biggest", "deadliest", "amazing", "mind", "vast", "tiny", "unreal",
        "never", "forever", "record", "faster", "farther"}
_SERIOUS = {"danger", "dangerous", "deadly", "kill", "kills", "killed", "die", "dies", "death", "toxic", "risk", "warning",
            "extinct", "endangered", "threat", "disaster", "crisis", "lose", "lost", "fatal"}
_EXCITED = {"wow", "boom", "bursts", "burst", "explodes", "explode", "blazing", "zoom", "zooms", "racing", "races",
            "rockets", "launches", "slams", "snaps", "snap", "flash"}
_PLAYFUL = {"funny", "silly", "weird", "oddly", "strange", "quirky", "bizarre", "goofy"}


@dataclass(frozen=True)
class Delivery:
    emotion: str
    speed: float      # multiplier on the voice speed
    lead_pause: float  # seconds of silence before the line
    hold: float       # extra seconds the scene should stay after the line is finished


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", text.lower())}


def classify(text: str, hint: str | None = None) -> str:
    """Pick an emotion for a line. A valid hint from the script writer wins; otherwise read the sentence."""
    hint = (hint or "").strip().lower()
    if hint in EMOTIONS and hint != "neutral":
        return hint
    t = text.strip()
    if t.endswith("?"):
        return "question"
    w = _words(t)
    score = {"serious": len(w & _SERIOUS), "awe": len(w & _AWE), "excited": len(w & _EXCITED),
             "playful": len(w & _PLAYFUL)}
    if t.endswith("!"):
        score["excited"] += 1
    if re.search(r"\d[\d,\.]{3,}|\b\d+\s?(?:times|percent|%|km|mph|bpm|degrees)\b", t, re.I):
        score["awe"] += 1  # a big, specific number is usually the point of the line
    best = max(score, key=lambda k: (score[k], k == "awe"))
    return best if score[best] > 0 else "neutral"


def plan(text: str, hint: str | None = None, voice_speed: float = 1.0) -> Delivery:
    emotion = classify(text, hint)
    mult, lead, hold, _, _ = _PROFILE[emotion]
    speed = voice_speed * mult
    if emotion == "question":
        speed = max(QUESTION_SPEED_FLOOR, min(QUESTION_SPEED_CEIL, speed))
    return Delivery(emotion, round(speed, 3), lead, hold)


_SENT = re.compile(r"(?<=[.!?])\s+")


def split_question(narration: str, max_words: int = 18) -> tuple[str, str | None]:
    """('everything before', 'the closing question?') when the narration ends with a question sentence of a sensible
    length; (narration, None) otherwise. The question is spoken on its own so it can be slower and set apart."""
    text = " ".join(narration.split())
    if not text.endswith("?"):
        return text, None
    parts = _SENT.split(text)
    q = parts[-1].strip()
    if len(q.split()) > max_words or len(q.split()) < 2:
        return text, None
    body = " ".join(parts[:-1]).strip()
    return body, q


def question_text(q: str) -> str:
    """Text sent to the voice for the closing question: exactly one sentence that ends in a single '?', which is what
    makes the voice lift its pitch at the end."""
    q = q.strip().rstrip("?.! ").strip()
    return f"{q}?"


def _read(path) -> tuple[bytes, int, int, int]:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes()), w.getframerate(), w.getnchannels(), w.getsampwidth()


def join_wavs_timed(parts: list, out_path, gaps: list[float]) -> tuple[float, list[tuple[float, float]]]:
    """Concatenate WAV files (same format) with `gaps[i]` seconds of silence BEFORE part i. Returns the total seconds and
    (start, length) of every part inside the joined file, which the subtitles use to follow the voice exactly."""
    pcm, rate, ch, width, spans = b"", None, None, None, []
    for p, gap in zip(parts, gaps):
        data, r, c, w = _read(p)
        if rate is None:
            rate, ch, width = r, c, w
        frame = ch * width
        pcm += b"\x00" * (int(gap * rate) * frame)
        start = len(pcm) / (rate * frame)
        pcm += data
        spans.append((round(start, 3), round(len(data) / (rate * frame), 3)))
    with wave.open(str(Path(out_path)), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(pcm)
    return len(pcm) / (rate * ch * width), spans


def join_wavs(parts: list, out_path, gaps: list[float]) -> float:
    return join_wavs_timed(parts, out_path, gaps)[0]


def shape(path, semitones: float = 0.0, gain_db: float = 0.0) -> None:
    """Shift the pitch (length unchanged) and loudness of a WAV in place. Best effort: on any problem the plain voice stays."""
    if abs(semitones) < 0.15 and abs(gain_db) < 0.2:
        return
    path = Path(path)
    try:
        with wave.open(str(path), "rb") as w:
            rate, ch = w.getframerate(), w.getnchannels()
        filters = []
        if abs(semitones) >= 0.15:
            r = 2 ** (semitones / 12)
            filters.append(f"asetrate={int(rate * r)},aresample={rate},atempo={1 / r:.5f}")
        if abs(gain_db) >= 0.2:
            filters.append(f"volume={gain_db:.1f}dB")
        tmp = path.with_name(path.stem + ".shaped.wav")
        res = subprocess.run([settings.ffmpeg_path, "-y", "-v", "error", "-i", str(path), "-filter:a", ",".join(filters),
                              "-ar", str(rate), "-ac", str(ch), str(tmp)], capture_output=True, timeout=120)
        if res.returncode == 0 and tmp.exists():
            tmp.replace(path)
        else:
            tmp.unlink(missing_ok=True)
    except (OSError, subprocess.SubprocessError, wave.Error):
        pass


@dataclass(frozen=True)
class Segment:
    text: str         # what the voice says
    shown: str        # what the subtitles show (same words, the question keeps its natural form)
    emotion: str
    speed: float      # multiplier on the voice speed; for a question it is the ABSOLUTE speed (never sped up to fit)
    absolute: bool
    pitch: float
    gain: float
    gap_before: float


def segment(narration: str, hint: str | None, is_last: bool, voice_speed: float = 1.0) -> list[Segment]:
    """Split a scene's narration into the pieces that are spoken separately, each with its own emotion. Neighbouring
    sentences with the same emotion stay together (one natural run), a different emotion starts a new piece. Only the
    video's closing question is delivered as a question; a question in the middle is just curious."""
    text = " ".join(narration.split())
    sentences = [x for x in _SENT.split(text) if x]
    if not sentences:
        return []
    runs: list[tuple[str, list[str]]] = []
    for i, sent in enumerate(sentences):
        closing_q = is_last and i == len(sentences) - 1 and sent.endswith("?") and 2 <= len(sent.split()) <= 18
        emo = "question" if closing_q else classify(sent, None)
        if emo == "question" and not closing_q:
            emo = "curious"
        if emo == "neutral" and hint and hint != "neutral" and hint in EMOTIONS and hint != "question":
            emo = hint  # the writer's feeling for the scene colours sentences that carry no strong signal of their own
        if runs and runs[-1][0] == emo and emo != "question":
            runs[-1][1].append(sent)
        else:
            runs.append((emo, [sent]))
    out: list[Segment] = []
    for k, (emo, sents) in enumerate(runs):
        mult, lead, _, pitch, gain = _PROFILE[emo]
        body = " ".join(sents)
        if emo == "question":
            out.append(Segment(question_text(body), body, emo, mult, True, pitch, gain, lead if k else 0.0))
            continue
        prev = _PROFILE[runs[k - 1][0]][2] if k else 0.0  # an amazed line is given a moment to land before the next one
        out.append(Segment(body, body, emo, mult, False, pitch, gain, (0.14 + prev) if k else 0.0))
    return out


# ---------- a steady speaking rate ----------
# MeloTTS speaks short sentences faster and long ones slower, and every call differs a little, so a video made of many
# sentences used to sound rushed in one place and dragging in the next. Each piece is measured (syllables per second of
# real speech, silence at the edges ignored) and gently re-timed towards one rate for the whole video.

def syllables(text: str, cjk: bool = False) -> float:
    """A rough count of how much there is to say; digits count more than letters ('1,200' is four spoken words)."""
    if cjk:
        return float(max(1, len(re.sub(r"\s", "", text))))
    total = 0.0
    for w in text.split():
        digits = sum(c.isdigit() for c in w)
        if digits:
            total += max(2.0, digits * 1.6)
            continue
        groups = len(re.findall(r"[aeiouy]+", w.lower()))
        if w.lower().rstrip(".,!?;:").endswith("e") and groups > 1:
            groups -= 1
        total += max(1, groups)
    return max(1.0, total)


def _samples(path):
    import array
    with wave.open(str(path), "rb") as w:
        rate, ch, width, raw = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.readframes(w.getnframes())
    if width != 2:
        return None, rate, ch, raw
    a = array.array("h")
    a.frombytes(raw[: len(raw) // 2 * 2])
    return a, rate, ch, raw


def trim_silence(path, margin: float = 0.04) -> float:
    """Cut the silence the voice puts before and after a sentence (so pauses are the ones we plan, not random ones) and
    return the seconds of real speech. A file with no sound at all is left as it is."""
    a, rate, ch, raw = _samples(path)
    if a is None or not len(a):
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    peak = max(max(a), -min(a))
    if peak <= 0:
        return len(a) / (rate * ch)
    gate = peak * 0.03
    first = next((i for i, v in enumerate(a) if abs(v) > gate), 0) // ch
    last = (len(a) - 1 - next((i for i, v in enumerate(reversed(a)) if abs(v) > gate), 0)) // ch
    keep0, keep1 = max(0, first - int(margin * rate)), min(len(a) // ch, last + int(margin * rate) + 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(a[keep0 * ch: keep1 * ch].tobytes())
    return max(0.05, (last - first + 1) / rate)


def retime(src, dst, factor: float) -> None:
    """Speak `factor` times faster (pitch unchanged). Without ffmpeg or for a tiny change the file is copied as it is."""
    import shutil
    if abs(factor - 1.0) < 0.02:
        shutil.copy(src, dst)
        return
    r = subprocess.run([settings.ffmpeg_path, "-y", "-v", "error", "-i", str(src), "-filter:a",
                        f"atempo={max(0.5, min(2.0, factor)):.4f}", "-c:a", "pcm_s16le", str(dst)], capture_output=True, timeout=120)
    if r.returncode != 0 or not Path(dst).exists():
        shutil.copy(src, dst)
