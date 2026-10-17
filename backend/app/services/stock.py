"""Free stock video. IMAGE_BACKEND can be one provider ("pexels", "pixabay", "nasa", "wikimedia", "archive",
"smithsonian") or "stock", which searches every provider listed in STOCK_PROVIDERS and picks the best match."""
import hashlib
import io
import json
import logging
import re
import subprocess
import time
from collections import Counter
from urllib.parse import quote

import httpx
from PIL import Image

from ..core.config import settings
from ..core.errors import PipelineError
from .render import _preset

STOP = set("""a an and are as at be been but by can for from has have how in into is it its more most not of on or our so
that the their them then there these they this to up use used uses using was were what when where which while who why will with
about after also because before between both each during over under very your you than just like make made makes making""".split())
_used: dict[str, set] = {}  # clips already used per project, so scenes don't repeat footage
_prov_count: dict[str, Counter] = {}  # clips taken per provider in this project; ties go to the least-used source
log = logging.getLogger("stock")
UA = "ExplainerVideoBot/1.0 (educational video generator; contact: set STOCK_CONTACT in backend/.env)"
MAX_DL = 60 * 1024 * 1024
VIDEO_EXT = (".mp4", ".webm", ".mov", ".m4v")


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


def _ua() -> str:
    return UA.replace("set STOCK_CONTACT in backend/.env", settings.stock_contact) if settings.stock_contact else UA


def _clean(text: str | None) -> str:
    """Strip HTML tags and extra whitespace from API text."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


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
        r = httpx.get(url, timeout=30, headers={"User-Agent": _ua(), **(headers or {})}, params=params,
                      follow_redirects=True)
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


def _nasa(query: str, aspect: str) -> list[dict]:
    """NASA Image and Video Library. No key. Mostly public domain (see nasa.gov/nasa-brand-center/images-and-media)."""
    data = _get_json("NASA", f"{settings.nasa_base_url}/search", {"q": query, "media_type": "video", "page_size": 30})
    out = []
    for it in (data.get("collection") or {}).get("items", []):
        d = (it.get("data") or [{}])[0]
        nid = d.get("nasa_id")
        if not nid or d.get("media_type") != "video":
            continue
        thumb = next((l.get("href") for l in it.get("links", []) if l.get("render") == "image"), None)
        tags = " ".join([d.get("title") or "", " ".join(d.get("keywords") or [])])
        out.append({"id": f"nasa{nid}", "provider": "nasa", "thumb": thumb, "tags": tags,
                    "page": f"https://images.nasa.gov/details-{quote(nid)}",
                    "files": (lambda nid=nid: _nasa_files(nid)),
                    "credit": f"NASA ({d.get('center') or 'NASA Image and Video Library'})", "credit_required": False})
    return out


def _nasa_files(nasa_id: str) -> list[dict]:
    data = _get_json("NASA", f"{settings.nasa_base_url}/asset/{quote(nasa_id)}", {})
    size = {"large": (1280, 720), "medium": (960, 540), "small": (640, 360), "mobile": (480, 270)}  # nominal sizes
    files, orig = [], []
    for it in (data.get("collection") or {}).get("items", []):
        href = (it.get("href") or "").replace("http://", "https://").replace(" ", "%20")
        low = href.lower()
        if not low.endswith(".mp4"):
            continue
        label = low.rsplit("~", 1)[-1][:-4] if "~" in low else ""
        if label in size:
            files.append({"link": href, "width": size[label][0], "height": size[label][1]})
        elif label == "orig":
            orig.append({"link": href, "width": 1920, "height": 1080})  # can be huge; only used if nothing else exists
    return files or orig


def _license_class(name: str) -> str | None:
    """'pd' | 'cc0' | 'by' | 'by-sa' for licences we can use in a video, None for NC/ND/unknown."""
    n = name.lower().replace("_", "-")
    if "public domain" in n or n.startswith("pd") or "no known copyright" in n:
        return "pd"
    if "cc0" in n or "cc-zero" in n:
        return "cc0"
    if n.startswith("cc by"):
        if "-nc" in n or "-nd" in n or " nc" in n or " nd" in n:
            return None
        return "by-sa" if "-sa" in n or " sa" in n else "by"
    return None


def _wikimedia(query: str, aspect: str) -> list[dict]:
    """Wikimedia Commons videos. No key, but a descriptive User-Agent is required. Licences vary per file, so each
    file is checked against WIKIMEDIA_LICENSES and its credit line is kept for the caption."""
    allowed = {x.strip().lower() for x in settings.wikimedia_licenses.split(",") if x.strip()}
    data = _get_json("Wikimedia Commons", settings.wikimedia_api_url,
                     {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6, "gsrlimit": 25,
                      "gsrsearch": f"filetype:video {query}", "prop": "videoinfo", "viurlwidth": 640,
                      "viprop": "url|size|mime|extmetadata|derivatives|thumburl"})
    out = []
    for p in ((data.get("query") or {}).get("pages") or {}).values():
        vi = (p.get("videoinfo") or [{}])[0]
        meta = vi.get("extmetadata") or {}
        lic = (meta.get("LicenseShortName") or {}).get("value") or ""
        cls = _license_class(lic)
        if cls not in allowed:
            continue
        files = []
        for dv in vi.get("derivatives") or []:
            t = (dv.get("type") or "").lower()
            if t.startswith("video/") and "audio" not in t and dv.get("src") and dv.get("width") and dv.get("height"):
                files.append({"link": dv["src"], "width": int(dv["width"]), "height": int(dv["height"])})
        if vi.get("url") and (vi.get("mime") or "").startswith("video/") and (vi.get("size") or 0) <= 50 * 1024 * 1024 \
                and vi.get("width") and vi.get("height"):
            files.append({"link": vi["url"], "width": int(vi["width"]), "height": int(vi["height"])})
        if not files:
            continue
        title = re.sub(r"\.\w+$", "", (p.get("title") or "").removeprefix("File:")).replace("_", " ")
        cats = (meta.get("Categories") or {}).get("value") or ""
        tags = " ".join([title, _clean((meta.get("ImageDescription") or {}).get("value")), cats.replace("|", " ")])
        artist = _clean((meta.get("Artist") or {}).get("value")) or "Wikimedia Commons contributor"
        out.append({"id": f"wikimedia{p.get('pageid')}", "provider": "wikimedia", "files": files,
                    "thumb": vi.get("thumburl"), "tags": tags, "license": lic,
                    "page": vi.get("descriptionurl") or f"https://commons.wikimedia.org/wiki/{quote(p.get('title', ''))}",
                    "credit": f"{artist} / Wikimedia Commons ({lic})", "credit_required": cls not in ("pd", "cc0")})
    return out


def _archive(query: str, aspect: str) -> list[dict]:
    """Internet Archive, limited to public-domain collections (ARCHIVE_COLLECTIONS, default Prelinger + NASA).
    Search is one request; the file list is only fetched for the clip that is actually picked."""
    words = " ".join(sorted(tokens(query)))
    colls = " OR ".join(c.strip() for c in settings.archive_collections.split(",") if c.strip())
    if not words or not colls:
        return []
    data = _get_json("Internet Archive", f"{settings.archive_base_url}/advancedsearch.php",
                     {"q": f"(title:({words}) OR subject:({words})) AND mediatype:movies AND collection:({colls})",
                      "fl[]": ["identifier", "title", "subject"], "rows": 25, "output": "json",
                      "sort[]": "downloads desc"})
    out = []
    for d in (data.get("response") or {}).get("docs", []):
        ident = d.get("identifier")
        if not ident:
            continue
        subj = d.get("subject") or []
        subj = " ".join(subj) if isinstance(subj, list) else str(subj)
        out.append({"id": f"archive{ident}", "provider": "archive", "tags": f"{d.get('title') or ''} {subj}",
                    "thumb": f"{settings.archive_base_url}/services/img/{quote(ident)}",
                    "page": f"{settings.archive_base_url}/details/{quote(ident)}",
                    "files": (lambda ident=ident: _archive_files(ident)),
                    "credit": f"Internet Archive ({ident})", "credit_required": False})
    return out


def _archive_files(ident: str) -> list[dict]:
    data = _get_json("Internet Archive", f"{settings.archive_base_url}/metadata/{quote(ident)}", {})
    files = []
    for f in data.get("files") or []:
        name = f.get("name") or ""
        try:
            size = int(f.get("size") or 0)
        except ValueError:
            size = 0
        if not name.lower().endswith(".mp4") or not size or size > 55 * 1024 * 1024 or "/" in name:
            continue
        try:
            w, h = int(f.get("width") or 640), int(f.get("height") or 480)
        except ValueError:
            w, h = 640, 480
        files.append({"link": f"{settings.archive_base_url}/download/{quote(ident)}/{quote(name)}", "width": w, "height": h})
    return files


def _smithsonian(query: str, aspect: str) -> list[dict]:
    """Smithsonian Open Access (CC0 only). Key from https://api.data.gov/signup/ ; DEMO_KEY works but is tiny."""
    key = settings.smithsonian_api_key or "DEMO_KEY"
    data = _get_json("Smithsonian", f"{settings.smithsonian_base_url}/search",
                     {"api_key": key, "q": f'{query} AND online_media_type:"Videos"', "rows": 30})
    out = []
    for row in (data.get("response") or {}).get("rows", []):
        c = row.get("content") or {}
        dnr = c.get("descriptiveNonRepeating") or {}
        media = next((m for m in (dnr.get("online_media") or {}).get("media", [])
                      if (m.get("type") or "").lower().startswith("video") and (m.get("content") or "").lower().split("?")[0].endswith(VIDEO_EXT)
                      and (m.get("usage") or {}).get("access") == "CC0"), None)
        if not media:
            continue
        idx = c.get("indexedStructured") or {}
        title = ((dnr.get("title") or {}).get("content")) or row.get("title") or ""
        tags = " ".join([title, " ".join(idx.get("topic") or []), " ".join(idx.get("object_type") or [])])
        out.append({"id": f"smithsonian{row.get('id')}", "provider": "smithsonian", "tags": tags,
                    "files": [{"link": media["content"], "width": 1280, "height": 720}],  # real size unknown
                    "thumb": media.get("thumbnail"), "page": dnr.get("record_link") or dnr.get("guid"),
                    "credit": f"Smithsonian Institution ({dnr.get('data_source') or dnr.get('unit_code') or 'Open Access'}), CC0",
                    "credit_required": False})
    return out


PROVIDER_NAMES = ("pixabay", "pexels", "nasa", "wikimedia", "archive", "smithsonian")
_KEYED = {"pixabay": lambda: settings.pixabay_api_key, "pexels": lambda: settings.pexels_api_key}


def is_stock_backend(backend: str) -> bool:
    return backend == "stock" or backend in PROVIDER_NAMES


def active_providers() -> list[str]:
    b = settings.image_backend
    if b == "stock":
        return [n.strip().lower() for n in settings.stock_providers.split(",") if n.strip()]
    return [b] if b in PROVIDER_NAMES else []


def _search(query: str, aspect: str) -> list[dict]:
    """Search every active provider. With IMAGE_BACKEND=stock a provider that fails (or has no key) is skipped
    so one flaky source never stops a video; with a single provider its error is shown as before."""
    multi = settings.image_backend == "stock"
    out = []
    for name in active_providers():
        fn = globals().get("_" + name) if name in PROVIDER_NAMES else None
        if fn is None:
            log.warning("unknown stock provider %r in STOCK_PROVIDERS (use: %s)", name, ", ".join(PROVIDER_NAMES))
            continue
        if multi and name in _KEYED and not _KEYED[name]():
            continue
        try:
            for v in fn(query, aspect):
                v.setdefault("provider", name)
                out.append(v)
        except PipelineError as e:
            if not multi:
                raise
            log.warning("%s search failed, skipping it: %s", name, e)
    return out


def _pick_file(files: list[dict], aspect: str) -> dict:
    W, H = _preset()["dims"][aspect]
    big = [f for f in files if f["width"] >= W and f["height"] >= H]
    return min(big, key=lambda f: f["width"] * f["height"]) if big else max(files, key=lambda f: f["width"] * f["height"])  # smallest that is big enough


def _probe_duration(src) -> float:
    """Length in seconds from FFmpeg's banner (no ffprobe needed). 0 when unknown."""
    try:
        r = subprocess.run([settings.ffmpeg_path, "-hide_banner", "-i", str(src)], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return 0.0
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0


def _probe_size(src) -> tuple[int, int]:
    """Width and height of the first video stream, from FFmpeg's banner. (0, 0) when unknown."""
    try:
        r = subprocess.run([settings.ffmpeg_path, "-hide_banner", "-i", str(src)], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return 0, 0
    m = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", r.stderr or "")
    return (int(m[1]), int(m[2])) if m else (0, 0)


def frame_filter(sw: int, sh: int, W: int, H: int) -> str:
    """How a source clip is fitted into the video frame.
    Same shape as the frame (or FOOTAGE_FRAMING=fill): fill the frame and crop the edges.
    Very different shape (e.g. 16:9 footage in a 9:16 Reel): a plain crop keeps only the middle third of the picture,
    so animals and objects look huge and get cut off. Instead the clip is shown in a panel that keeps much more of
    the picture, over a blurred, darkened copy of itself that fills the rest of the screen."""
    fill = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=24"
    if settings.footage_framing != "smart" or not sw or not sh:
        return fill
    sar, oar = sw / sh, W / H
    if max(sar / oar, oar / sar) < 1.3:
        return fill
    tar = (sar * oar) ** 0.5  # panel shape: halfway (geometric mean) between the clip's shape and the frame's
    bw, bh = (W, W / tar) if sar > oar else (H * tar, H)
    bw, bh = int(bw) // 2 * 2, int(bh) // 2 * 2
    return (f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=28,eq=brightness=-0.12[bg];"
            f"[b]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh}[fg];"
            f"[bg][fg]overlay=(main_w-overlay_w)/2:(main_h-overlay_h)/2,fps=24")


def _shrink(src, dst, aspect: str) -> None:
    W, H = _preset()["dims"][aspect]
    total = _probe_duration(src)
    # Long films (Internet Archive, NASA) start with leaders and title cards: take the 16 s from about a third in.
    start = min(total * 0.33, total - 16) if total > 45 else 0
    sw, sh = _probe_size(src)
    cmd = [settings.ffmpeg_path, "-y", "-v", "error", *(["-ss", f"{start:.2f}"] if start > 0 else []), "-i", str(src),
           "-t", "16", "-an", "-vf", frame_filter(sw, sh, W, H),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "27", "-pix_fmt", "yuv420p", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    except (OSError, subprocess.SubprocessError) as e:
        raise PipelineError(f"Couldn't process a stock clip: {e}")
    if r.returncode != 0:
        raise PipelineError(f"FFmpeg couldn't process a stock clip: {r.stderr[-300:]}")


def has_subject(video: dict, must: set[str], alts: list[set[str]] | None) -> bool:
    """True when the clip is tagged with every word of the subject, or every word of one of its other names."""
    return has_all(video, must) or any(a and has_all(video, a) for a in (alts or []))


def has_all(video: dict, must: set[str]) -> bool:
    """True when the clip's tags contain every subject word, so 'black bear' never gets 'polar bear' footage."""
    return must <= tokens(video.get("tags", ""))


def _download(link: str, dl, provider: str) -> None:
    try:
        with httpx.stream("GET", link, timeout=90, follow_redirects=True, headers={"User-Agent": _ua()}) as r:
            if r.status_code >= 400:
                raise PipelineError(f"Couldn't download a {provider} clip (HTTP {r.status_code}).")
            size = 0
            with open(dl, "wb") as out:
                for chunk in r.iter_bytes(1 << 16):
                    size += len(chunk)
                    if size > MAX_DL:
                        raise PipelineError(f"A {provider} clip was larger than 60 MB; skipped for safety.")
                    out.write(chunk)
    except httpx.HTTPError as e:
        raise PipelineError(f"Couldn't download a {provider} clip: {e}")


def fetch_clip(queries: list[str], aspect: str, index: int, clip_path, thumb_path, context: str = "",
               must: set[str] | None = None, alts: list[set[str]] | None = None) -> dict | None:
    """Download the best-matching unused clip from the active provider(s). Clips whose tags don't relate to the
    search are rejected, so a scene gets no footage (and a labeled placeholder) rather than unrelated footage.
    With several providers the best tag match wins; ties go to the source used least in this video, so a video
    mixes NASA, archive and stock shots instead of repeating one look."""
    cache = settings.storage_dir / "cache" / "clips"
    cache.mkdir(parents=True, exist_ok=True)
    pid = clip_path.parent.name
    if index == 1:
        _used[pid] = set()
        _prov_count[pid] = Counter()
    used = _used.setdefault(pid, set())
    counts = _prov_count.setdefault(pid, Counter())
    ctok = tokens(context)
    # With a subject: first look for a clip not used yet, then (before giving up) reuse one that was already used.
    for allow_used in ((False, True) if must else (False,)):
      for q in queries:
        qtok = tokens(q)
        if not qtok:
            continue
        ranked = sorted(((score(v, qtok, ctok), v) for v in _search(q, aspect) if allow_used or v["id"] not in used),
                        key=lambda t: (-t[0], counts[t[1].get("provider", "")]))
        ranked = [(sc, v) for sc, v in ranked if sc >= 3]  # at least one search word must appear in the clip's tags
        if must:
            ranked = [(sc, v) for sc, v in ranked if has_subject(v, must, alts)]  # and ALL words of the subject (or of an alias)
        for _, v in ranked[:4]:  # a candidate can fail (dead link, too big): try the next best one
            prov = v.get("provider", settings.image_backend)
            dl = settings.tmp_dir / f"dl_{pid}_{index}.mp4"
            dl.parent.mkdir(parents=True, exist_ok=True)
            try:
                files = v["files"]() if callable(v["files"]) else v["files"]
                if not files:
                    raise PipelineError(f"No usable video file for a {prov} clip.")
                _download(_pick_file(files, aspect)["link"], dl, prov)
                _shrink(dl, clip_path, aspect)  # keeps the project small: at most 16 s, 720p-class, no audio
            except PipelineError as e:
                log.warning("skipping %s clip %s: %s", prov, v["id"], e)
                continue
            finally:
                dl.unlink(missing_ok=True)
            used.add(v["id"])
            counts[prov] += 1
            try:
                img = httpx.get(v["thumb"], timeout=30, headers={"User-Agent": _ua()}, follow_redirects=True).content
                im = Image.open(io.BytesIO(img)).convert("RGB")
                im.thumbnail((640, 640))
                im.save(thumb_path)
            except Exception:  # thumbnail is cosmetic
                Image.new("RGB", (320, 180), (30, 30, 40)).save(thumb_path)
            return {"ai_generated": False, "source": prov, "query": q, "tags": v.get("tags"), "source_url": v.get("page"),
                    "credit": v.get("credit"), "credit_required": bool(v.get("credit_required")), "license": v.get("license")}
    return None
