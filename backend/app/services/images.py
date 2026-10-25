import hashlib
import random
import re
import shutil
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..core.config import settings
from ..core.errors import PipelineError

STYLE_PROMPTS = {
    "cinematic": "cinematic still, dramatic lighting, shallow depth of field, rich color grading",
    "whiteboard": "hand-drawn whiteboard illustration, black marker line art on white background, simple diagram",
    "minimal": "minimalist flat vector illustration, limited pastel palette, generous negative space",
    "illustrated": "colorful storybook illustration, soft painterly textures, friendly, warm lighting",
}
PALETTES = {"cinematic": ((18, 14, 36), (130, 30, 110)), "whiteboard": ((248, 248, 246), (222, 226, 238)),
            "minimal": ((236, 232, 250), (252, 228, 240)), "illustrated": ((255, 214, 170), (255, 150, 190))}
ACCENTS = [(255, 77, 157), (217, 70, 239), (139, 92, 246), (56, 189, 248)]
_pipe = None


def _is_stock(backend: str) -> bool:
    from .stock import is_stock_backend
    return is_stock_backend(backend)


def image_size(aspect: str) -> tuple[int, int]:
    m = settings.image_max_side
    r = lambda v: max(256, int(v) // 16 * 16)
    return {"16:9": (r(m), r(m * 9 / 16)), "9:16": (r(m * 9 / 16), r(m)), "1:1": (r(m), r(m))}[aspect]


BUNDLED_FONT = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "Poppins-Bold.ttf"  # SIL OFL, ships with the project


def _font(size: int):
    for p in (str(BUNDLED_FONT), "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf",
              "/System/Library/Fonts/Supplemental/Arial Bold.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _load_pipe():
    global _pipe
    if _pipe is None:
        try:
            import torch
            from diffusers import AutoPipelineForText2Image
            cuda = torch.cuda.is_available()
            _pipe = AutoPipelineForText2Image.from_pretrained(
                settings.image_model, torch_dtype=torch.bfloat16 if cuda else torch.float32)
            _pipe.enable_model_cpu_offload() if cuda else _pipe.to("cpu")
        except Exception as e:
            raise PipelineError(
                f"Couldn't load image model '{settings.image_model}': {e}. Install `requirements-ai.txt`, accept the "
                "model's license on Hugging Face if required, or set IMAGE_BACKEND=fallback for labeled placeholders.")
    return _pipe


def _generate_ai(prompt: str, seed: int, size: tuple[int, int]) -> Image.Image:
    key = hashlib.sha256(f"{settings.image_model}|{prompt}|{seed}|{size}".encode()).hexdigest()
    cached = settings.cache_dir / f"{key}.png"
    if cached.exists():
        return Image.open(cached).convert("RGB")
    import torch
    pipe = _load_pipe()
    img = pipe(prompt=prompt, width=size[0], height=size[1], num_inference_steps=settings.image_steps,
               guidance_scale=settings.image_guidance, generator=torch.Generator("cpu").manual_seed(seed)).images[0]
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    img.save(cached)
    return img.convert("RGB")


def _generate_hf(prompt: str, seed: int, size: tuple[int, int]) -> Image.Image:
    import io

    import httpx
    from .script import _hf_error
    if not settings.hf_token:
        raise PipelineError("IMAGE_BACKEND=hf_api needs HF_TOKEN in backend/.env.")
    key = hashlib.sha256(f"hf|{settings.hf_image_model}|{prompt}|{seed}|{size}".encode()).hexdigest()
    cached = settings.cache_dir / f"{key}.png"
    if cached.exists():
        return Image.open(cached).convert("RGB")
    try:
        r = httpx.post(f"{settings.hf_base_url.rstrip('/')}/hf-inference/models/{settings.hf_image_model}",
                       timeout=settings.hf_timeout, headers={"Authorization": f"Bearer {settings.hf_token}"},
                       json={"inputs": prompt, "parameters": {"width": size[0], "height": size[1], "seed": seed}})
    except httpx.HTTPError as e:
        raise PipelineError(f"Couldn't reach Hugging Face: {e}")
    if r.status_code >= 400:
        raise _hf_error(r, "image")
    try:
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        raise PipelineError("Hugging Face did not return an image. Try a different HF_IMAGE_MODEL.")
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    img.save(cached)
    return img


def _generate_cloudflare(prompt: str, seed: int, size: tuple[int, int]) -> Image.Image:
    """FLUX.1 schnell on Cloudflare Workers AI (free daily allowance). It only makes 1024x1024 squares,
    so the picture is cropped to the video's aspect ratio here; that keeps the caption chip inside the frame."""
    import base64
    import io

    from PIL import ImageOps

    from . import cloudflare
    key = hashlib.sha256(f"cf|{settings.cloudflare_image_model}|{prompt}|{seed}|{size}".encode()).hexdigest()
    cached = settings.cache_dir / f"{key}.png"
    if cached.exists():
        return Image.open(cached).convert("RGB")
    try:
        res = cloudflare.run(settings.cloudflare_image_model,
                             {"prompt": prompt[:2000], "steps": settings.cloudflare_steps, "seed": seed % 2147483647})
    except PipelineError as e:
        if "returned 400" not in str(e):
            raise
        res = cloudflare.run(settings.cloudflare_image_model, {"prompt": prompt[:2000]})  # model rejected an option
    raw = res.get("_bytes") or (base64.b64decode(res["image"]) if res.get("image") else b"")
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        raise PipelineError("Cloudflare did not return an image. Check CLOUDFLARE_IMAGE_MODEL.")
    img = ImageOps.fit(img, size, Image.LANCZOS)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    img.save(cached)
    return img


def _fallback(style: str, size: tuple[int, int], seed: int) -> Image.Image:
    w, h = size
    top, bottom = PALETTES[style]
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    rnd = random.Random(seed)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for _ in range(7):
        c, r = rnd.choice(ACCENTS), rnd.randint(min(w, h) // 8, min(w, h) // 3)
        x, y = rnd.randint(0, w), rnd.randint(0, h)
        if rnd.random() < 0.5:
            ld.ellipse([x - r, y - r, x + r, y + r], fill=c + (70,))
        else:
            ld.rounded_rectangle([x - r, y - r // 2, x + r, y + r // 2], radius=r // 5, fill=c + (70,))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def _wrap_px(d: ImageDraw.ImageDraw, text: str, font, max_w: float) -> list[str]:
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and d.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + ([cur] if cur else [])


ACCENT = (255, 196, 0, 255)       # amber: the one highlighted word and the small underline
INK = (12, 14, 26, 255)           # outline colour, a near-black navy that reads on any footage
_HL_SKIP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are", "how", "why", "what", "that", "this",
            "with", "from", "its", "it", "can", "does", "do", "your", "you", "by", "at", "as", "than", "more", "most"}


def _highlight_index(words: list[str]) -> int | None:
    """Which word gets the amber colour: the first number if there is one, else the longest meaningful word."""
    for i, w in enumerate(words):
        if any(ch.isdigit() for ch in w):
            return i
    best = [(len(re.sub(r"\W", "", w)), i) for i, w in enumerate(words) if re.sub(r"\W", "", w).lower() not in _HL_SKIP]
    return max(best)[1] if best and max(best)[0] >= 5 else None


def _headline(layer: Image.Image, text: str, portrait: bool) -> None:
    """On-screen headline in the style of short-video creators instead of a boxed card: big bold type with a thick dark
    outline and a soft shadow, one amber highlighted word, and a short amber underline. Centred, kept 11% in from the
    sides (tall phones crop the edges of a Reel) and 15% below the top edge (clear of the platform's own buttons)."""
    from PIL import ImageFilter
    w, h = layer.size
    probe = ImageDraw.Draw(layer)
    side, top = (int(w * 0.11), int(h * 0.15)) if portrait else (int(w * 0.12), int(h * 0.09))
    text = " ".join(text.split())
    if len(text) <= 24:
        text = text.upper()  # short punchy lines read better in capitals; long ones stay in normal case
    size = max(30, w // (11 if portrait else 17))
    while True:
        font, sw = _font(size), max(3, size // 11)
        lines = _wrap_px(probe, text, font, w - 2 * side - 2 * sw)
        if (len(lines) <= 3 and all(probe.textlength(l, font=font) <= w - 2 * side - 2 * sw for l in lines)) or size <= 18:
            break
        size -= 2
    lines = lines[:3]
    words = " ".join(lines).split()
    hl = _highlight_index(words)
    lh = int(size * 1.22)
    space = probe.textlength(" ", font=font)
    mask = Image.new("L", layer.size, 0)  # shadow shape
    md = ImageDraw.Draw(mask)
    placed, k = [], 0
    for li, line in enumerate(lines):
        lw = probe.textlength(line, font=font)
        x, y = (w - lw) / 2, top + li * lh
        for word in line.split():
            placed.append((x, y, word, k == hl))
            md.text((x, y + size * 0.07), word, font=font, fill=190, stroke_width=sw + 1)
            x += probe.textlength(word, font=font) + space
            k += 1
    layer.alpha_composite(Image.merge("RGBA", (Image.new("L", layer.size, 0),) * 3 + (mask.filter(ImageFilter.GaussianBlur(max(5, size // 5))),)))
    d = ImageDraw.Draw(layer)
    for x, y, word, hot in placed:
        d.text((x, y), word, font=font, fill=ACCENT if hot else (255, 255, 255, 255), stroke_width=sw, stroke_fill=INK)
    bar_w, bar_h = int(w * 0.16), max(5, size // 9)
    y = top + len(lines) * lh + int(size * 0.14)  # below the descenders (g, y, p)
    d.rounded_rectangle([(w - bar_w) / 2, y, (w + bar_w) / 2, y + bar_h], radius=bar_h // 2, fill=ACCENT)


def _label(layer: Image.Image, label: str) -> None:
    w, _ = layer.size
    d = ImageDraw.Draw(layer)
    f = _font(max(12, w // 60))
    lw = d.textlength(label, font=f)
    d.rounded_rectangle([w - lw - 26, 10, w - 10, 14 + f.size + 8 if hasattr(f, "size") else 34], radius=8, fill=(10, 10, 14, 175))
    d.text((w - lw - 18, 14), label, font=f, fill=(255, 190, 220, 255))


def _text_layer(size: tuple[int, int], text: str, label: str | None) -> Image.Image:
    """Transparent RGBA layer with the on-screen headline (and the placeholder label)."""
    w, h = size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    if text.strip():
        _headline(layer, text, portrait=h > w * 1.2)
    if label:
        _label(layer, label)
    return layer


def _overlay(img: Image.Image, text: str, label: str | None) -> Image.Image:
    return Image.alpha_composite(img.convert("RGBA"), _text_layer(img.size, text, label)).convert("RGB")


def _gradient(style: str, size: tuple[int, int]) -> Image.Image:
    w, h = size
    top, bottom = PALETTES[style]
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return img


def _shapes(size: tuple[int, int], seed: int, count: int, scale: float) -> Image.Image:
    """Transparent layer of soft translucent shapes. FFmpeg drifts these over the background."""
    w, h = size
    rnd = random.Random(seed)
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for _ in range(count):
        c, r = rnd.choice(ACCENTS), int(rnd.randint(min(w, h) // 10, min(w, h) // 4) * scale)
        x, y = rnd.randint(0, w), rnd.randint(0, h)
        if rnd.random() < 0.5:
            d.ellipse([x - r, y - r, x + r, y + r], fill=c + (95,))
        else:
            d.rounded_rectangle([x - r, y - r // 2, x + r, y + r // 2], radius=r // 5, fill=c + (95,))
    return layer


def _make_layered(project: dict, scene, index: int, pdir: Path, seed: int) -> None:
    """Placeholder visuals as separate layers so the renderer can animate them (parallax drift + text reveal)."""
    from .render import DIMS
    W, H = DIMS[project["aspect_ratio"]]
    label = "Placeholder graphic - not AI-generated"
    bg = _gradient(project["style"], (W, H))
    a = _shapes((int(W * 1.3), int(H * 1.3)), seed + index, 5, 1.0)
    b = _shapes((int(W * 1.3), int(H * 1.3)), seed + index * 7 + 1, 4, 0.8)
    txt = _text_layer((W, H), scene.on_screen_text, label)
    n = f"scene_{index:02d}"
    bg.save(pdir / f"{n}_bg.png")
    a.save(pdir / f"{n}_a.png")
    b.save(pdir / f"{n}_b.png")
    txt.save(pdir / f"{n}_text.png")
    still = bg.convert("RGBA")
    for layer in (a, b):
        still.paste(layer, ((W - layer.width) // 2, (H - layer.height) // 2), layer)
    Image.alpha_composite(still, txt).convert("RGB").save(pdir / f"{n}.png")  # still used for previews/thumbnails


ai_fail_reason = ""  # why the last AI picture failed, shown in the scene's note so the run log explains it


def _ai_scene_fallback(project: dict, script, scene, size: tuple[int, int], seed: int, index: int, pdir: Path) -> dict | None:
    """When no stock clip matches a scene, draw a picture of it with Cloudflare FLUX (free daily allowance) so the
    video never shows gradient shapes. Returns scene metadata, or None when AI is off or fails (placeholder then)."""
    from . import cloudflare
    if not settings.stock_ai_fallback or not cloudflare.configured():
        return None
    subject = (project.get("subject") or "").strip() or script.title
    prompt = (f"Photorealistic nature documentary photograph of {subject}. {scene.visual_description}. "
              "Sharp focus, natural light, high detail, realistic anatomy. No text, no letters, no watermark, no logo.")
    global ai_fail_reason
    img = None
    for attempt in (1, 2):  # a slow or busy moment on Cloudflare should not cost the scene its picture
        try:
            img = _generate_cloudflare(prompt, seed + index + (attempt - 1) * 7, size)
            break
        except Exception as e:  # noqa: BLE001  quota used up, network, bad reply
            import logging
            import time
            ai_fail_reason = f"{type(e).__name__}: {str(e)[:120]}"
            logging.getLogger("images").warning("AI picture for scene %s failed (try %s): %s", index, attempt, e)
            if "allowance" in str(e).lower() or "token" in str(e).lower():
                break  # used-up quota or a bad token will not fix itself in a few seconds
            if attempt == 1:
                time.sleep(4)
    if img is None:
        return None
    _overlay(img, scene.on_screen_text, None).save(pdir / f"scene_{index:02d}.png")
    return {"ai_generated": True, "model": settings.cloudflare_image_model, "source": "ai",
            "tags": f"AI picture of {subject}"}


def make_scene_image(project: dict, script, scene, index: int, pdir: Path) -> dict:
    """Create scene_NN.png in pdir. Returns metadata, including whether it is AI-generated."""
    size = image_size(project["aspect_ratio"])
    seed = int(project["id"][:8], 16)
    name = f"scene_{index:02d}.png"
    if settings.image_backend in ("diffusers", "hf_api", "cloudflare"):
        prompt = (f"{STYLE_PROMPTS[project['style']]}. {scene.visual_description}. Topic: {script.title}. "
                  "No text, no letters, no watermark.")
        gen = {"hf_api": _generate_hf, "cloudflare": _generate_cloudflare}.get(settings.image_backend, _generate_ai)
        img, ai, label = gen(prompt, seed, size), True, None
    elif _is_stock(settings.image_backend):
        from . import stock, vision
        from .render import DIMS
        W, H = DIMS[project["aspect_ratio"]]
        n = f"scene_{index:02d}"
        top = lambda text, k: " ".join(sorted(stock.tokens(text), key=lambda w: -len(w))[:k])
        queries = []
        subject = (project.get("subject") or "").strip()
        must = stock.tokens(subject) if subject else None
        if must:  # every search contains the subject, and a clip must be tagged with ALL its words (black AND bear)
            for e in (scene.search_keywords.strip(), top(scene.visual_description, 2), top(scene.on_screen_text, 2)):
                q = e if must <= stock.tokens(e) else f"{subject} {e}".strip()
                if q and q not in queries:
                    queries.append(q)
            for q in (subject, f"{subject} nature"):
                if q not in queries:
                    queries.append(q)
        else:
            for q in (scene.search_keywords.strip(), top(scene.visual_description, 3), top(scene.on_screen_text, 3),
                      top(project.get("topic") or project.get("prompt", ""), 3)):
                if q and q not in queries:
                    queries.append(q)
        # Other names for the subject (snapping shrimp = pistol shrimp). Tried only after the subject's own name found
        # nothing, and a clip must still be tagged with the full alias, so the footage can never be a different animal.
        aliases = [a for a in (project.get("subject_aliases") or []) if stock.tokens(a)]
        alts = [stock.tokens(a) for a in aliases] if must else None
        for a in aliases if must else []:
            for q in (a, f"{a} {top(scene.visual_description, 1)}".strip()):
                if q not in queries:
                    queries.append(q)
        context = f"{scene.visual_description} {scene.on_screen_text} {scene.search_keywords}"
        meta = stock.fetch_clip(queries, project["aspect_ratio"], index, pdir / f"{n}_clip.mp4", pdir / f"{n}.png",
                                context=context, must=must, alts=alts, subject=must)
        if meta is None:  # nothing matched: draw a real picture of the scene instead of purple shapes
            diag = stock.last_diag
            ai = _ai_scene_fallback(project, script, scene, size, seed, index, pdir)
            if ai is not None:
                return {**ai, "searched": diag.removeprefix("searched: "), "note": f"no stock footage matched; AI picture used. {diag}"}
            # No picture either (quota used up): a different, unused clip about the scene itself, without the subject.
            skip = set(must or set()).union(*(alts or []))
            broll = [q for q in dict.fromkeys(
                " ".join(w for w in stock.tokens(e) if w not in skip) for e in
                (scene.search_keywords, scene.visual_description, scene.on_screen_text)) if q][:3]
            # Related footage must still not show a look-alike of the subject (a hawk-moth in a hummingbird video).
            meta = stock.fetch_clip(broll, project["aspect_ratio"], index, pdir / f"{n}_clip.mp4", pdir / f"{n}.png",
                                    context=context, subject=must or None, confirm=False,
                                    avoid=vision.conflict_words(skip),
                                    # Related footage is only worth showing when it is clearly about the scene: curated
                                    # tag sources only (Wikimedia/Archive tags are whole descriptions, which let a
                                    # "complex adaptive systems" lecture stand in for a bird) and two matching words.
                                    providers={"pixabay", "pexels", "nasa"}, min_score=6) if broll else None
            if meta is not None:
                meta = {**meta, "note": f"no clip of the subject was left; related footage used. AI picture: "
                                        f"{ai_fail_reason or 'off'}. {diag}"}
            else:
                _make_layered(project, scene, index, pdir, seed)
                return {"ai_generated": False, "model": None,
                        "note": f"no stock footage found; placeholder used. AI picture: {ai_fail_reason or 'off'}. {diag}"}
        _text_layer((W, H), scene.on_screen_text, None).save(pdir / f"{n}_text.png")
        return meta
    elif settings.image_backend == "fallback":
        _make_layered(project, scene, index, pdir, seed)
        return {"ai_generated": False, "model": None, "note": "placeholder graphic (IMAGE_BACKEND=fallback)"}
    else:
        raise PipelineError(f"Unknown IMAGE_BACKEND '{settings.image_backend}'. Use 'stock' (several free sources), 'pixabay', 'pexels', 'nasa', 'wikimedia', 'archive', 'smithsonian', 'cloudflare', 'hf_api', 'diffusers' or 'fallback'.")
    _overlay(img, scene.on_screen_text, label).save(pdir / name)
    model = {"hf_api": settings.hf_image_model, "cloudflare": settings.cloudflare_image_model}.get(
        settings.image_backend, settings.image_model)
    return {"ai_generated": ai, "model": model if ai else None}