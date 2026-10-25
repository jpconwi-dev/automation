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
    (start, end, the card's words, index of the word being spoken, emotion)."""
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
        nxt = cues[ci + 1][1] if ci + 1 < len(cues) else hold_to
        pos = 0
        for card in split_cards(words, max_chars):
            first, last = pos, pos + len(card) - 1
            for j in range(len(card)):
                s0, s1 = spans[pos + j]
                if j == len(card) - 1:  # the card stays up a beat after its last word, never into the next cue
                    s1 = min(spans[last][1] + (0.45 if emotion == "question" else 0.18), nxt if nxt else 1e9)
                    s1 = max(s1, s0 + 0.05)
                out.append((s0, s1, card, j, emotion))
            pos += len(card)
    return out


def build_ass(cues: list[tuple[str, float, float, str]], W: int, H: int, aspect: str, total: float) -> str:
    size_r, max_chars, bottom, side = LAYOUT.get(aspect, LAYOUT["9:16"])
    size = round(H * size_r)
    outline, shadow = max(3, round(H * 0.0045)), max(1, round(H * 0.0017))
    head = (
        "[Script Info]\nScriptType: v4.00+\nWrapStyle: 2\nScaledBorderAndShadow: yes\n"
        f"PlayResX: {W}\nPlayResY: {H}\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, "
        "MarginV, Encoding\n"
        f"Style: Main,Poppins,{size},{WHITE.replace('&H', '&H00').rstrip('&')},&H00FFFFFF,{INK.replace('&H', '&H00').rstrip('&')},"
        f"&H96000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},2,{round(W * side)},{round(W * side)},{round(H * bottom)},1\n"
        f"Style: Question,Poppins,{round(size * 1.08)},&H00FFFFFF,&H00FFFFFF,{INK.replace('&H', '&H00').rstrip('&')},"
        f"&H96000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},2,{round(W * side)},{round(W * side)},{round(H * bottom)},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
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
