"""Free stock video from Pexels or Pixabay. Used when IMAGE_BACKEND is "pexels" or "pixabay"."""
import hashlib
import io
import json
import subprocess
import time

import httpx
from PIL import Image

from ..core.config import settings
from ..core.errors import PipelineError
from .render import DIMS

STOP = set("""a an and are as at be been but by can for from has have how in into is it its more most not of on or our so
that the their them then there these they this to up use used uses using was were what when where which while who why will with
about after also because before between both each during over under very your you than just like make made makes making""".split())
_used: dict[str, set] = {}  # clips already used per project, so scenes don't repeat footage


def tokens(text: str) -> set[str]:
    out = set()
    for w in "".join(c.lower() if c.isalnum() else " " for c in text).split():
        if len(w) >= 3 and w not in STOP:
            out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def score(video: dict, qtok: set[str], ctok: set[str]) -> int:
    """How well a clip's tags match the search words (x3) and the rest of the scene text (x1)."""
    tags = tokens(video.get("tags", ""))
    return 3 * len(tags & qtok) + len(tags & ctok)


ORIENT = {"16:9": "landscape", "9:16": "portrait", "1:1": "square"}
CACHE_TTL = 24 * 3600  # Pixabay requires search results to be cached for 24 hours


def _http_error(provider: str, r: httpx.Response) -> PipelineError:
    hints = {401: f"Your {provider} API key was rejected. Check the key in backend/.env.",
             403: f"{provider} refused the request. Check your API key.",
             429: f"{provider} rate limit reached. Wait a minute and try again."}
    return PipelineError(hints.get(r.status_code, f"{provider} returned {r.status_code}: {r.text[:200]}"))


def _get_json(provider: str, url: str, params: dict, headers: dict | None = None) -> dict:
    """GET with a 24h on-disk cache (the API key is not part of the cache key)."""
    cache = settings.storage_dir / "cache" / "search"
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / (hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest() + ".json")
    if f.exists() and time.time() - f.stat().st_mtime < CACHE_TTL:
        return json.loads(f.read_text(encoding="utf-8"))
    try:
        r = httpx.get(url, timeout=30, headers=headers or {}, params=params)
    except httpx.HTTPError as e:
        raise PipelineError(f"Couldn't reach {provider}: {e}")
    if r.status_code >= 400:
        raise _http_error(provider, r)
    data = r.json()
    f.write_text(json.dumps(data), encoding="utf-8")
    return data


def _pexels(query: str, aspect: str) -> list[dict]:
    if not settings.pexels_api_key:
        raise PipelineError("IMAGE_BACKEND=pexels needs PEXELS_API_KEY in backend/.env.")
    data = _get_json("Pexels", f"{settings.pexels_base_url}/videos/search",
                     {"query": query, "per_page": 30, "orientation": ORIENT[aspect], "size": "medium"},
                     {"Authorization": settings.pexels_api_key})
    out = []
    for v in data.get("videos", []):
        files = [{"link": f["link"], "width": f["width"], "height": f["height"]} for f in v.get("video_files", [])
                 if f.get("file_type") == "video/mp4" and f.get("width") and f.get("height") and f.get("link")]
        if files:
            out.append({"id": f"pexels{v['id']}", "files": files, "thumb": v.get("image"), "page": v.get("url"),
                        "tags": (v.get("url") or "").rstrip("/").rsplit("/", 1)[-1].replace("-", " ")})
    return out


def _pixabay(query: str, aspect: str) -> list[dict]:
    if not settings.pixabay_api_key:
        raise PipelineError("IMAGE_BACKEND=pixabay needs PIXABAY_API_KEY in backend/.env "
                            "(free: sign in at pixabay.com, then open pixabay.com/api/docs).")
    data = _get_json("Pixabay", f"{settings.pixabay_base_url}/videos/",
                     {"key": settings.pixabay_api_key, "q": query, "per_page": 30, "safesearch": "true"})
    out = []
    for h in data.get("hits", []):
        sizes = h.get("videos", {})
        files = [{"link": s["url"], "width": s["width"], "height": s["height"]} for s in sizes.values()
                 if s.get("url") and s.get("width") and s.get("height")]
        thumb = next((s.get("thumbnail") for s in sizes.values() if s.get("thumbnail")), None)
        if files:
            out.append({"id": f"pixabay{h['id']}", "files": files, "thumb": thumb, "page": h.get("pageURL"),
                        "tags": h.get("tags", "")})
    return out


def _pick_file(files: list[dict], aspect: str) -> dict:
    W, H = DIMS[aspect]
    big = [f for f in files if f["width"] >= W and f["height"] >= H]
    return min(big, key=lambda f: f["width"] * f["height"]) if big else max(files, key=lambda f: f["width"] * f["height"])  # smallest that is big enough


def _shrink(src, dst, aspect: str) -> None:
    W, H = DIMS[aspect]
    cmd = [settings.ffmpeg_path, "-y", "-v", "error", "-i", str(src), "-t", "16", "-an",
           "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=24",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-pix_fmt", "yuv420p", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        raise PipelineError(f"Couldn't process a stock clip: {e}")
    if r.returncode != 0:
        raise PipelineError(f"FFmpeg couldn't process a stock clip: {r.stderr[-300:]}")


def fetch_clip(queries: list[str], aspect: str, index: int, clip_path, thumb_path, context: str = "") -> dict | None:
    """Download the best-matching unused clip. Clips whose tags don't relate to the search are rejected,
    so a scene gets no footage (and a labeled placeholder) rather than unrelated footage."""
    provider = settings.image_backend
    search = _pexels if provider == "pexels" else _pixabay
    cache = settings.storage_dir / "cache" / "clips"
    cache.mkdir(parents=True, exist_ok=True)
    pid = clip_path.parent.name
    if index == 1:
        _used[pid] = set()
    used = _used.setdefault(pid, set())
    ctok = tokens(context)
    for q in queries:
        qtok = tokens(q)
        if not qtok:
            continue
        ranked = sorted(((score(v, qtok, ctok), v) for v in search(q, aspect) if v["id"] not in used),
                        key=lambda t: -t[0])
        ranked = [(sc, v) for sc, v in ranked if sc >= 3]  # at least one search word must appear in the clip's tags
        if not ranked:
            continue
        v = ranked[0][1]
        used.add(v["id"])
        f = _pick_file(v["files"], aspect)
        dl = settings.tmp_dir / f"dl_{pid}_{index}.mp4"
        dl.parent.mkdir(parents=True, exist_ok=True)
        try:
            with httpx.stream("GET", f["link"], timeout=90, follow_redirects=True) as r:
                if r.status_code >= 400:
                    raise PipelineError(f"Couldn't download a {provider} clip (HTTP {r.status_code}).")
                size = 0
                with open(dl, "wb") as out:
                    for chunk in r.iter_bytes(1 << 16):
                        size += len(chunk)
                        if size > 60 * 1024 * 1024:
                            raise PipelineError(f"A {provider} clip was larger than 60 MB; skipped for safety.")
                        out.write(chunk)
            _shrink(dl, clip_path, aspect)  # keeps the project small: at most 16 s, 720p-class, no audio
        except httpx.HTTPError as e:
            raise PipelineError(f"Couldn't download a {provider} clip: {e}")
        finally:
            dl.unlink(missing_ok=True)
        try:
            img = httpx.get(v["thumb"], timeout=30).content
            im = Image.open(io.BytesIO(img)).convert("RGB")
            im.thumbnail((640, 640))
            im.save(thumb_path)
        except Exception:  # thumbnail is cosmetic
            Image.new("RGB", (320, 180), (30, 30, 40)).save(thumb_path)
        return {"ai_generated": False, "source": provider, "query": q, "tags": v.get("tags"), "source_url": v.get("page")}
    return None
