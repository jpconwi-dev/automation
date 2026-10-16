"""Tone, voice and music profiles shared by the script writer, the narrator and the sound designer."""

TONES = ("friendly", "professional", "energetic", "calm", "humorous", "dramatic")

# How the script is written
TONE_HINTS = {
    "friendly": "warm, friendly and encouraging, like a kind teacher talking to one person",
    "professional": "clear, polished and authoritative, like a corporate trainer; no slang",
    "energetic": "upbeat, enthusiastic and fast-paced, with short punchy sentences",
    "calm": "slow, soothing and reassuring, with gentle phrasing",
    "humorous": "light-hearted and witty, with a few harmless jokes, but still accurate",
    "dramatic": "cinematic and gripping, like a movie-trailer narrator, with strong imagery",
}
# How fast the voice speaks (multiplied with the user's own speed slider)
TONE_SPEED = {"friendly": 1.0, "professional": 0.98, "energetic": 1.08, "calm": 0.9, "humorous": 1.03, "dramatic": 0.92}
# Pitch change in percent. Only Windows voices can do this; Kokoro changes speed and voice instead.
TONE_PITCH = {"friendly": 3, "professional": -2, "energetic": 8, "calm": -5, "humorous": 6, "dramatic": -10}
# Background music used when the music style is "auto"
TONE_MUSIC = {"friendly": "playful", "professional": "calm", "energetic": "upbeat", "calm": "calm",
              "humorous": "playful", "dramatic": "cinematic"}
MUSIC_STYLES = ("calm", "upbeat", "cinematic", "playful")


def tone_of(p: dict) -> str:
    t = p.get("tone", "friendly")
    return t if t in TONES else "friendly"


def effective_speed(p: dict) -> float:
    s = float(p.get("voice_speed", 1.0)) * TONE_SPEED[tone_of(p)]
    return max(0.7, min(1.5, s))


def music_style(p: dict) -> str:
    m = p.get("music_style", "auto")
    return m if m in MUSIC_STYLES else TONE_MUSIC[tone_of(p)]
