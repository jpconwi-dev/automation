"""Makes a cover image for the video: the sharpest, most colourful frame, a little brighter and more saturated,
with the title in big white-and-yellow letters. Used as the Reel / Short thumbnail where the platform accepts it."""
import pathlib
import re
import subprocess
import tempfile

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
         "DejaVuSans-Bold.ttf", "arialbd.ttf"]
YELLOW = (255, 214, 0)


def _font(size: int):
    for f in FONTS:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _duration(video: pathlib.Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                       capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def _score(im: Image.Image) -> float:
    """Higher = sharper, more colourful, not too dark or washed out."""
    g = im.convert("L").resize((96, 170))
    px = list(g.getdata())
    mean = sum(px) / len(px)
    if mean < 35 or mean > 225:
        return 0.0
    edges = sum(abs(px[i] - px[i + 1]) for i in range(len(px) - 1)) / len(px)
    sat = sum(im.convert("HSV").resize((48, 85)).getchannel("S").getdata()) / (48 * 85)
    return edges + 0.35 * sat


def _best_frame(video: pathlib.Path, tmp: pathlib.Path) -> Image.Image:
    dur = _duration(video)
    best, best_score = None, -1.0
    for i, frac in enumerate((0.08, 0.2, 0.32, 0.44, 0.56, 0.68, 0.8)):
        out = tmp / f"f{i}.jpg"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(dur * frac, 0.3):.2f}", "-i", str(video),
                        "-frames:v", "1", "-q:v", "2", str(out)], capture_output=True, timeout=90)
        if not out.exists():
            continue
        im = Image.open(out).convert("RGB")
        s = _score(im)
        if s > best_score:
            best, best_score = im, s
    if best is None:
        raise RuntimeError("could not read a frame from the video")
    return best


def _hook_text(title: str) -> str:
    t = re.sub(r"#\w+", "", title or "").strip(" -:|")
    words = t.split()
    return " ".join(words[:7]).upper()


def _wrap(d: ImageDraw.ImageDraw, text: str, font, max_w: float) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        trial = (cur + " " + w).strip()
        if cur and d.textlength(trial, font=font, features=None) > max_w:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def make_thumbnail(video: pathlib.Path, title: str, out: pathlib.Path, size: tuple[int, int] = (720, 1280)) -> None:
    W, H = size
    with tempfile.TemporaryDirectory() as td:
        im = _best_frame(video, pathlib.Path(td))
    # fill the 9:16 canvas, then make it pop: more colour and contrast, slightly sharper
    scale = max(W / im.width, H / im.height)
    im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
    im = im.crop(((im.width - W) // 2, (im.height - H) // 2, (im.width - W) // 2 + W, (im.height - H) // 2 + H))
    im = ImageEnhance.Color(im).enhance(1.25)
    im = ImageEnhance.Contrast(im).enhance(1.12)
    im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=80, threshold=3)).convert("RGBA")
    # dark gradient behind the text so it reads on any picture
    grad = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    start = int(H * 0.42)
    for y in range(start, H):
        gd.line([(0, y), (W, y)], fill=(0, 0, 0, int(215 * ((y - start) / (H - start)) ** 1.4)))
    im = Image.alpha_composite(im, grad)
    d = ImageDraw.Draw(im)
    text = _hook_text(title)
    if text:
        margin = int(W * 0.09)
        size_px = int(W * 0.12)
        while True:
            font = _font(size_px)
            lines = _wrap(d, text, font, W - 2 * margin)
            if len(lines) <= 4 and all(d.textlength(l, font=font) <= W - 2 * margin for l in lines) or size_px <= 36:
                break
            size_px -= 4
        lh = int(size_px * 1.18)
        y = int(H * 0.80) - lh * len(lines) // 2 - int(H * 0.03)
        for i, line in enumerate(lines):
            colour = YELLOW if i == len(lines) - 1 else (255, 255, 255)  # last line in yellow draws the eye
            x = (W - d.textlength(line, font=font)) / 2
            d.text((x, y + i * lh), line, font=font, fill=colour, stroke_width=max(4, size_px // 14), stroke_fill=(0, 0, 0))
    im.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
