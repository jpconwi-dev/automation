"""Subtitles in the style short-video creators use, instead of one grey block of text per sentence.

* 2 to 4 words at a time, never a wall of text, so the eye stays on the picture.
* Bold Poppins with a thick dark outline and a soft shadow (readable on any footage).
* The word being spoken lights up in amber; numbers stay amber. The closing question is coral and held a little longer.
* A quick pop-in on every card.

Timing: the voice is made sentence by sentence (see delivery.segment), so every cue knows when it starts and how long
it lasts. Inside a cue each word gets a share of the time by its length (commas and full stops add a little extra).
Written as an ASS file, which FFmpeg's `subtitles` filter burns into the video together with the bundled font.
"""
import re

AMBER = "&H00C4FF&"   # ASS colours are BGR: this is RGB(255, 196, 0)
CORAL = "&H7A5CFF&"   # RGB(255, 92, 122)
WHITE = "&HFFFFFF&"
INK = "&H1A0E0C&"     # RGB(12, 14, 26)

# (font size as a share of the video height, widest card in characters, bottom margin share, side margin share)
LAYOUT = {"9:16": (0.05, 22, 0.21, 0.07), "16:9": (0.085, 34, 0.07, 0.07), "1:1": (0.07, 26, 0.07, 0.07)}


def _ts(s: float) -> str:
    cs = max(0, int(round(s * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02}:{cs // 100 % 60:02}.{cs % 100:02}"


def _weight(word: str) -> float:
    w = len(re.sub(r"\W", "", word)) or 1
    if word.endswith((",", ";", ":")):
        w += 2
    elif word.endswith((".", "!", "?")):
        w += 4
    if any(c.isdigit() for c in word):
        w += 2
    return w + 1.0


def split_cards(words: list[str], max_chars: int, max_words: int = 4) -> list[list[str]]:
    """Group words into short cards. A card ends at a comma or full stop once it has two words, and a number is kept
    together with the word after it ('44,000 miles') when that still fits."""
    cards: list[list[str]] = []
    cur: list[str] = []
    for w in words:
        trial = " ".join(cur + [w])
        if cur and (len(trial) > max_chars or len(cur) >= max_words):
            if any(c.isdigit() for c in cur[-1]) and len(trial) <= max_chars + 8:
                pass  # keep a number with its unit
            else:
                cards.append(cur)
                cur = []
        cur.append(w)
        if w.endswith((".", "!", "?", ",", ";", ":")) and len(cur) >= 2:
            cards.append(cur)
            cur = []
    if cur:
        cards.append(cur)
    if len(cards) > 1 and len(cards[-1]) == 1 and len(" ".join(cards[-2] + cards[-1])) <= max_chars + 4:
        stray = cards.pop()  # never leave a single stray word on its own card
        cards[-1] = cards[-1] + stray
    return cards


def _esc(w: str) -> str:
    return w.replace("\\", "").replace("{", "(").replace("}", ")")


def events(cues: list[tuple[str, float, float, str]], max_chars: int, hold_to: float | None = None) -> list[tuple[float, float, list[str], int, str]]:
    """cues: (text, start, length, emotion), start in video seconds. Returns one event per spoken word:
    (start, end, the card's words, index of the word being spoken, emotion). Events never overlap: a card stays up a beat
    after its last word, but never past the start of the next card or the next cue."""
    out = []
    for ci, (text, start, length, emotion) in enumerate(cues):
        words = text.split()
        if not words or length <= 0:
            continue
        weights = [_weight(w) for w in words]
        total = sum(weights)
        t, spans = start, []
        for w, wt in zip(words, weights):
            d = length * wt / total
            spans.append((t, t + d))
            t += d
        nxt_cue = cues[ci + 1][1] if ci + 1 < len(cues) else hold_to
        cards = split_cards(words, max_chars)
        pos = 0
        for ki, card in enumerate(cards):
            last = pos + len(card) - 1
            nxt_start = spans[last + 1][0] if ki + 1 < len(cards) else nxt_cue  # where the following card (or cue) begins
            for j in range(len(card)):
                s0, s1 = spans[pos + j]
                if j == len(card) - 1:
                    s1 = spans[last][1] + (0.45 if emotion == "question" else 0.18)
                    s1 = max(s1, s0 + 0.05)  # long enough to be seen...
                    if nxt_start is not None:
                        s1 = min(s1, nxt_start)  # ...but never into the next card
                if s1 - s0 < 0.005:
                    continue  # a word too short to show at all
                out.append((s0, s1, card, j, emotion))
            pos += len(card)
    return out


def _header(W: int, H: int, aspect: str, scale: float = 1.0) -> str:
    size_r, _, bottom, side = LAYOUT.get(aspect, LAYOUT["9:16"])
    size = max(10, round(H * size_r * scale))
    outline, shadow = max(2, round(H * 0.0045 * scale)), max(1, round(H * 0.0017 * scale))
    ink = INK.replace("&H", "&H00").rstrip("&")
    margins = f"{round(W * side)},{round(W * side)},{round(H * bottom)}"
    return (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 2\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {W}\nPlayResY: {H}\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
        "MarginV, Encoding\n"
        f"Style: Main,Poppins,{size},&H00FFFFFF,&H00FFFFFF,{ink},&H96000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},2,{margins},1\n"
        f"Style: Question,Poppins,{round(size * 1.08)},&H00FFFFFF,&H00FFFFFF,{ink},&H96000000,-1,0,0,0,100,100,0,0,1,"
        f"{outline},{shadow},2,{margins},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")


def widest_card(cues, max_chars: int) -> str:
    cards = [" ".join(e[2]) for e in events(cues, max_chars)]
    return max(cards, key=len) if cards else ""


def fit_scale(text: str, W: int, H: int, aspect: str, workdir, ffmpeg: str = "ffmpeg") -> float:
    """How much to shrink the caption font so the widest card fits the screen. Measured by really drawing it with
    FFmpeg and the fonts in workdir/fonts, so it holds whichever font libass ends up using: if Poppins is missing on the
    machine and a wider fallback font is used, the text is made smaller instead of running off the edges. 1.0 = no change."""
    import subprocess
    from pathlib import Path
    from PIL import Image
    if not text:
        return 1.0
    workdir = Path(workdir)
    _, _, _, side = LAYOUT.get(aspect, LAYOUT["9:16"])
    try:
        (workdir / "fit.ass").write_text(_header(W, H, aspect) + f"Dialogue: 0,0:00:00.00,0:00:02.00,Question,,0,0,0,,{_esc(text)}\n",
                                         encoding="utf-8")
        r = subprocess.run([ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:d=1:r=1",
                            "-vf", "subtitles=fit.ass:fontsdir=fonts", "-frames:v", "1", "fit.png"],
                           cwd=workdir, capture_output=True, timeout=60)
        if r.returncode != 0:
            return 1.0
        box = Image.open(workdir / "fit.png").convert("L").point(lambda v: 255 if v > 200 else 0).getbbox()
        if not box:
            return 1.0
        width = box[2] - box[0]
        room = W * (1 - 2 * side) * 0.97
        return round(max(0.45, min(1.0, room / width)), 3) if width > room else 1.0
    except Exception:  # noqa: BLE001  measuring is a safety net, never a reason to lose the video
        return 1.0
    finally:
        for n in ("fit.ass", "fit.png"):
            (workdir / n).unlink(missing_ok=True)


def build_ass(cues: list[tuple[str, float, float, str]], W: int, H: int, aspect: str, total: float, scale: float = 1.0) -> str:
    _, max_chars, _, _ = LAYOUT.get(aspect, LAYOUT["9:16"])
    head = _header(W, H, aspect, scale)
    lines = []
    for s0, s1, card, j, emotion in events(cues, max_chars, total):
        q = emotion == "question"
        hot = CORAL if q else AMBER
        parts = []
        for k, w in enumerate(card):
            colour = hot if k == j else (AMBER if any(c.isdigit() for c in w) else WHITE)
            parts.append(f"{{\\1c{colour}}}{_esc(w)}")
        anim = ""
        if j == 0:
            anim = "{\\fad(60,0)\\fscx88\\fscy88\\t(0,%d,\\fscx100\\fscy100)}" % (190 if q else 110)
        lines.append(f"Dialogue: 0,{_ts(s0)},{_ts(s1)},{'Question' if q else 'Main'},,0,0,0,,{anim}{' '.join(parts)}")
    return head + "\n".join(lines) + "\n"


def cues_from_narration(narration: str, start: float, speech: float, last: bool) -> list[tuple[str, float, float, str]]:
    """Fallback when the voice step did not report exact timings: one cue per sentence, shared out by length."""
    sents = [x for x in re.split(r"(?<=[.!?。！？])\s+", narration.strip()) if x] or [narration]
    total, t, out = sum(len(x) for x in sents) or 1, start + 0.1, []
    for i, x in enumerate(sents):
        d = speech * len(x) / total
        out.append((x, t, d, "question" if last and i == len(sents) - 1 and x.endswith("?") else "neutral"))
        t += d
    return out
