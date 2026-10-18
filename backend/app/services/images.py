import hashlib
import random
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


def _font(size: int):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/arialbd.ttf",
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


ACCENT_BAR = (255, 196, 0, 255)  # amber stripe on the title card


def _draw_card(layer: Image.Image, box: tuple[float, float, float, float], pad: float) -> None:
    """Title card: soft drop shadow, dark navy glass panel, thin light border and an amber accent stripe."""
    from PIL import ImageFilter
    x0, y0, x1, y1 = box
    shadow = Image.new("RGBA", layer.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([x0 + 3, y0 + 8, x1 + 3, y1 + 8], radius=pad, fill=(0, 0, 0, 150))
    layer.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(max(6, pad // 2))))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle([x0, y0, x1, y1], radius=pad, fill=(11, 19, 43, 214), outline=(255, 255, 255, 70), width=2)
    bar = max(7, int(pad * 0.5))
    d.rounded_rectangle([x0 + pad * 0.7, y0 + pad * 0.8, x0 + pad * 0.7 + bar, y1 - pad * 0.8], radius=bar // 2, fill=ACCENT_BAR)


def _draw_title_text(d: ImageDraw.ImageDraw, x: float, y: float, line: str, font) -> None:
    d.text((x + 2, y + 3), line, font=font, fill=(0, 0, 0, 170))  # soft shadow keeps it readable on any footage
    d.text((x, y), line, font=font, fill=(255, 255, 255, 255))


def _chip_portrait(layer: Image.Image, text: str) -> None:
    """Title card for 9:16. Phones with tall screens (iPhone) crop the sides of a Reel and Facebook draws its own
    buttons at the top and bottom, so the card is centred, kept 11% in from each side and 14% below the top edge,
    wrapped by real pixel width, and the font shrinks until it fits in 3 lines."""
    w, h = layer.size
    d = ImageDraw.Draw(layer)
    pad, side, top = max(16, w // 45), int(w * 0.11), int(h * 0.14)
    bar = max(7, int(pad * 0.5))
    inner = pad * 0.7 + bar + pad * 0.8  # room taken by the accent stripe on the left
    max_w = w - 2 * side - inner - pad
    size = max(24, w // 17)
    while True:
        font = _font(size)
        lines = _wrap_px(d, text, font, max_w)
        if (len(lines) <= 3 and all(d.textlength(l, font=font) <= max_w for l in lines)) or size <= 18:
            break
        size -= 2
    lines = lines[:3]
    tw = max(d.textlength(l, font=font) for l in lines)
    lh = size * 1.3
    card_w = inner + tw + pad * 1.5
    x0 = (w - card_w) / 2
    y1 = top + 2 * pad + lh * len(lines) - pad * 0.3
    _draw_card(layer, (x0, top, x0 + card_w, y1), pad)
    d = ImageDraw.Draw(layer)
    for i, line in enumerate(lines):
        _draw_title_text(d, x0 + inner, top + pad + i * lh, line, font)


def _text_layer(size: tuple[int, int], text: str, label: str | None) -> Image.Image:
    """Transparent RGBA layer with the on-screen caption chip (and placeholder label)."""
    w, h = size
    if h > w * 1.2 and text.strip():
        layer = Image.new("RGBA", size, (0, 0, 0, 0))
        _chip_portrait(layer, text)
        if label:
            d = ImageDraw.Draw(layer)
            f = _font(max(12, w // 60))
            lw = d.textlength(label, font=f)
            d.rounded_rectangle([w - lw - 26, 10, w - 10, 34], radius=8, fill=(10, 10, 14, 175))
            d.text((w - lw - 18, 14), label, font=f, fill=(255, 190, 220, 255))
        return layer
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    if text.strip():
        font, pad = _font(max(22, w // 20)), max(16, w // 45)
        bar = max(7, int(pad * 0.5))
        inner = pad * 0.7 + bar + pad * 0.8
        lines = _wrap_px(d, text, font, w * 0.6)[:3]
        tw = max(d.textlength(line, font=font) for line in lines)
        lh = font.size * 1.3 if hasattr(font, "size") else 28
        x0, y0 = pad * 1.5, pad * 1.5
        _draw_card(layer, (x0, y0, x0 + inner + tw + pad * 1.5, y0 + 2 * pad + lh * len(lines) - pad * 0.3), pad)
        d = ImageDraw.Draw(layer)
        for i, line in enumerate(lines):
            _draw_title_text(d, x0 + inner, y0 + pad + i * lh, line, font)
    if label:
        f = _font(max(12, w // 60))
        lw = d.textlength(label, font=f)
        d.rounded_rectangle([w - lw - 26, 10, w - 10, 14 + f.size + 8 if hasattr(f, "size") else 34], radius=8, fill=(10, 10, 14, 175))
        d.text((w - lw - 18, 14), label, font=f, fill=(255, 190, 220, 255))
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


def _ai_scene_fallback(project: dict, script, scene, size: tuple[int, int], seed: int, index: int, pdir: Path) -> dict | None:
    """When no stock clip matches a scene, draw a picture of it with Cloudflare FLUX (free daily allowance) so the
    video never shows gradient shapes. Returns scene metadata, or None when AI is off or fails (placeholder then)."""
    from . import cloudflare
    if not settings.stock_ai_fallback or not cloudflare.configured():
        return None
    subject = (project.get("subject") or "").strip() or script.title
    prompt = (f"Photorealistic nature documentary photograph of {subject}. {scene.visual_description}. "
              "Sharp focus, natural light, high detail, realistic anatomy. No text, no letters, no watermark, no logo.")
    try:
        img = _generate_cloudflare(prompt, seed + index, size)
    except Exception as e:  # noqa: BLE001  quota used up, network, bad reply: fall back to the placeholder
        import logging
        logging.getLogger("images").warning("AI picture for scene %s failed: %s", index, e)
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
        from . import stock
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
                                context=context, must=must, alts=alts)
        if meta is None:  # nothing matched: draw a real picture of the scene instead of purple shapes
            diag = stock.last_diag
            ai = _ai_scene_fallback(project, script, scene, size, seed, index, pdir)
            if ai is not None:
                return {**ai, "searched": diag.removeprefix("searched: "), "note": f"no stock footage matched; AI picture used. {diag}"}
            _make_layered(project, scene, index, pdir, seed)
            return {"ai_generated": False, "model": None, "note": f"no stock footage found; placeholder used. {diag}"}
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