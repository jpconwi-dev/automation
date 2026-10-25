"""Checks that a stock clip really shows the video's subject.

Stock sites tag clips loosely: Pixabay files a hummingbird hawk-moth under "hummingbird", so a tag match alone can put
a moth on screen while the narrator talks about a bird. Two guards stop that:

  1. LOOK-ALIKES: words that mean "a different thing that is often mistaken for the subject". A clip whose tags
     contain one of them is never used for that subject, even if the subject word is in the tags too.
  2. LOOKING: a few frames of the finished clip are shown to a small vision model on Cloudflare Workers AI (same free
     account and token as the narration), which names the main animal or object. If it names a look-alike and not the
     subject, the clip is rejected and the next candidate is tried.

FOOTAGE_VISION=off | auto | strict
  off     no looking, tags and look-alike words only
  auto    (default) reject a clip only when the model sees a look-alike. If the model is unavailable or unsure the clip
          is kept, so a Cloudflare outage never stops the daily video.
  strict  also reject a clip when the model never confirms the subject. Fewer wrong clips, more AI-picture fallbacks.
"""
import logging
import re
import subprocess

from ..core.config import settings
from ..core.errors import PipelineError

log = logging.getLogger("vision")


class LookAlike(PipelineError):
    """The footage or picture shows something else than the subject (as opposed to a technical failure)."""

_INSECTS = {"moth", "hawkmoth", "sphinx", "butterfly", "insect", "bug", "wasp", "hornet", "dragonfly", "hoverfly", "fly", "bee"}
_BIRDS = {"hummingbird", "sunbird", "eagle", "owl", "parrot", "sparrow", "finch", "robin", "swallow", "kingfisher",
          "hawk", "falcon", "bird"}

# subject word -> words that mean it is a different thing (single words, plural 's' removed, lower case)
CONFLICTS: dict[str, set[str]] = {
    "hummingbird": _INSECTS | {"sunbird", "bat"},
    "sunbird": _INSECTS | {"hummingbird"},
    "crocodile": {"alligator", "caiman", "gharial"},
    "alligator": {"crocodile", "caiman", "gharial"},
    "frog": {"toad", "salamander", "newt"},
    "toad": {"frog", "salamander", "newt"},
    "rabbit": {"hare"},
    "hare": {"rabbit"},
    "moth": {"butterfly"},
    "butterfly": {"moth"},
    "dolphin": {"porpoise", "shark"},
    "porpoise": {"dolphin"},
    "seal": {"sealion", "walrus", "otter"},
    "bee": {"wasp", "hornet", "hoverfly"},
    "wasp": {"bee", "hornet", "hoverfly"},
    "ant": {"termite"},
    "termite": {"ant"},
    "leopard": {"cheetah", "jaguar"},
    "cheetah": {"leopard", "jaguar"},
    "jaguar": {"leopard", "cheetah"},
    "crow": {"raven"},
    "raven": {"crow"},
    "octopus": {"squid", "cuttlefish"},
    "squid": {"octopus", "cuttlefish"},
    "snailfish": {"snail", "shell", "slug", "gastropod", "mollusk", "mollusc"},
    "snail": {"snailfish"},
    "catfish": {"cat"},
    "lionfish": {"lion"},
    "starfish": {"star"},
    "jellyfish": {"jelly"},
    "turtle": {"tortoise"},
    "tortoise": {"turtle"},
}
for _b in _BIRDS - {"hummingbird", "sunbird", "bird"}:
    CONFLICTS.setdefault(_b, {"moth", "butterfly", "insect", "bat"})


def _tok(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 and not w.endswith(("ss", "is", "us")) else w)
    return out


def conflict_words(subject_words: set[str] | None) -> set[str]:
    """Look-alike words for a subject (all its words, and the words of its other names, merged)."""
    out: set[str] = set()
    for w in subject_words or ():
        out |= CONFLICTS.get(w, set())
    return out - set(subject_words or ())


def has_conflict(tags: str, bad: set[str]) -> bool:
    return bool(bad and (_tok(tags) & bad))


def enabled() -> bool:
    from . import cloudflare
    return settings.footage_vision in ("auto", "strict") and cloudflare.configured()


def _frame(clip, at: float) -> bytes:
    r = subprocess.run([settings.ffmpeg_path, "-v", "error", "-ss", f"{at:.2f}", "-i", str(clip), "-frames:v", "1",
                        "-vf", "scale=384:-2", "-q:v", "5", "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                       capture_output=True, timeout=60)
    return r.stdout


def _ask(jpeg: bytes) -> str:
    """One frame in, the model's short answer out. Raises PipelineError when Cloudflare cannot answer."""
    from . import cloudflare
    prompt = ("What is the main animal or object in this picture? Answer with only its name, "
              "for example 'a hummingbird' or 'a moth'.")
    model = settings.cloudflare_vision_model
    payload = {"image": list(jpeg), "prompt": prompt, "max_tokens": 24}
    if "llama" in model:  # Llama 3.2 vision takes chat messages (needs a one-time licence click in the Cloudflare dashboard)
        payload = {"image": list(jpeg), "messages": [{"role": "user", "content": prompt}], "max_tokens": 24}
    res = cloudflare.run(model, payload)
    return str(res.get("description") or res.get("response") or "").strip()


def judge(answer: str, subject: set[str], alts: list[set[str]], bad: set[str]) -> bool | None:
    """True = the model names the subject (or another name for it). False = it names a look-alike and not the
    subject. None = unclear (a flower, a blurry background...)."""
    a = _tok(answer)
    names = [subject, *[x for x in alts if x]]
    if any(n and n <= a for n in names):
        return True
    if a & bad:
        return False
    return None


def verify(clip, duration: float, subject: set[str], alts: list[set[str]] | None, confirm: bool = True) -> str:
    """Raise PipelineError when the clip shows a look-alike (or, in strict mode with confirm=True, never shows the
    subject). Returns a short note for the scene's metadata. Never raises because the model is down."""
    if not enabled() or not subject:
        return "vision: off"
    bad = conflict_words(subject.union(*(alts or [])))
    seen: list[str] = []
    verdicts: list[bool | None] = []
    for frac in (0.25, 0.7):
        try:
            jpeg = _frame(clip, max(0.2, duration * frac))
            if not jpeg:
                continue
            ans = _ask(jpeg)
        except (PipelineError, subprocess.SubprocessError, OSError) as e:
            log.warning("vision check skipped: %s", e)
            return "vision: unavailable"
        seen.append(ans)
        verdicts.append(judge(ans, subject, alts or [], bad))
    if not verdicts:
        return "vision: no frames"
    note = "vision: " + " | ".join(s[:40] for s in seen)
    if False in verdicts:
        raise PipelineError(f"the clip shows something else ({'; '.join(s[:40] for s in seen)}), not the subject")
    if settings.footage_vision == "strict" and confirm and True not in verdicts:
        raise PipelineError(f"the clip does not clearly show the subject ({'; '.join(s[:40] for s in seen)})")
    return note


def verify_image(img, subject: set[str], alts: list[set[str]] | None = None) -> str:
    """The same look-alike check for a picture drawn by the AI (a "snailfish" prompt can come back as a snail with a shell).
    Raises PipelineError when the model names a look-alike and not the subject; never because the model is unavailable."""
    if not enabled() or not subject:
        return "vision: off"
    import io
    bad = conflict_words(subject.union(*(alts or [])))
    if not bad:
        return "vision: no look-alikes known"
    try:
        small = img.convert("RGB")
        small.thumbnail((384, 384))
        buf = io.BytesIO()
        small.save(buf, "JPEG", quality=80)
        ans = _ask(buf.getvalue())
    except (PipelineError, OSError) as e:
        log.warning("picture check skipped: %s", e)
        return "vision: unavailable"
    if judge(ans, subject, alts or [], bad) is False:
        raise LookAlike(f"the picture shows something else ({ans[:40]}), not the subject")
    return f"vision: {ans[:40]}"
