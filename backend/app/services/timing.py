"""Makes the finished video exactly as long as the length the user chose.

Narration length is whatever the voice happens to produce, so after speaking every scene we:
  1. speed the narration up (max 1.3x) or slow it down (min 0.88x) if it is far from the target,
  2. spread the remaining difference over the scenes as pauses between them,
  3. give every scene a duration so that the durations add up to the target to the millisecond.
"""

MIN_SCENE = 2.0        # a scene is never shorter than this (the crossfade is 0.5 s)
MIN_GAP = 0.15         # shortest pause after a scene's narration
GAP = 0.6              # normal pause after a scene's narration
MAX_SPEEDUP = 1.3
MAX_SLOWDOWN = 0.88


def choose_tempo(speech: list[float], target: float) -> float:
    """Playback speed to apply to every narration clip (1.0 = leave as is)."""
    n, total = len(speech), sum(speech)
    if total <= 0:
        return 1.0
    if total + n * GAP > target:                      # too long: speed up just enough to leave small pauses
        room = target - n * (GAP / 2)
        return min(MAX_SPEEDUP, max(1.0, total / room)) if room > 0 else MAX_SPEEDUP
    extra = target - (total + n * GAP)                # too short: only slow down if the shortfall is large
    if extra > 0.10 * target:
        return max(MAX_SLOWDOWN, total / (total + 0.5 * extra))
    return 1.0


def layout(speech: list[float], target: float) -> list[float]:
    """Scene durations (narration + pause) that add up to `target` seconds."""
    n = len(speech)
    gap = max(MIN_GAP, (target - sum(speech)) / n)
    durations = [max(MIN_SCENE, s + gap) for s in speech]
    diff = target - sum(durations)
    if diff > 0:                                       # rounding, or a scene was raised to MIN_SCENE
        durations[-1] += diff
    elif diff < -1e-6:                                 # still too long: trim pauses from the longest scenes
        for i in sorted(range(n), key=lambda k: -durations[k]):
            cut = min(-diff, max(0.0, durations[i] - max(MIN_SCENE, speech[i] + MIN_GAP)))
            durations[i] -= cut
            diff += cut
            if diff >= -1e-6:
                break
    return durations
