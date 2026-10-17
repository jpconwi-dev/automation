import json
import math
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = {"title": "How a bicycle works", "description": "Pedals turn wheels.",
          "hook": "Ever wondered how a bike moves?",
          "scenes": [{"narration": f"Scene {i} explains one step of how pedals, a chain and gears turn your legs into motion. It is simple.",
                      "visual_description": "A cyclist riding on a road", "on_screen_text": f"Step {i}",
                      "subtitle_text": "subtitle text", "duration_seconds": 7, "search_keywords": "bicycle road"} for i in range(1, 5)],
          "closing_summary": "Pedals turn wheels."}


class Mock(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers["content-length"]))
        body = json.dumps({"message": {"content": json.dumps(SCRIPT)}}).encode()
        self.send_response(200); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a): pass


srv = HTTPServer(("127.0.0.1", 0), Mock)
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ.update(STORAGE_DIR=tempfile.mkdtemp(), OLLAMA_URL=f"http://127.0.0.1:{srv.server_port}",
                  TTS_BACKEND="windows", IMAGE_BACKEND="pixabay", PIXABAY_API_KEY="test", STORAGE_LIMIT_MB="800")
os.environ.setdefault("KEN_BURNS", "false")

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402
from app.core import db, storage  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import stock, tts  # noqa: E402

CALLS = []
_STORAGE = tempfile.mkdtemp()


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    """conftest.py creates `settings` before this module's os.environ lines run, so set what these tests need here."""
    for k, v in dict(storage_dir=Path(_STORAGE), llm_backend="ollama", ollama_url=f"http://127.0.0.1:{srv.server_port}",
                     tts_backend="windows", image_backend="pixabay", pixabay_api_key="test", storage_limit_mb=800,
                     ken_burns=False, music_path=None, access_key=None).items():
        monkeypatch.setattr(settings, k, v)


def fake_voice(text, language, out_path, voice="auto", speed=1.0):  # 22.05 kHz mono tone; longer text and slower speed take longer
    CALLS.append((voice, speed))
    rate, secs = 22050, max(1.0, len(text.split()) * 0.55 / speed)
    n = int(rate * secs)
    pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 300 * i / rate))) for i in range(n))
    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)
    return secs


tts.synthesize = fake_voice


def fake_fetch(queries, aspect, index, clip_path, thumb_path, context="", must=None, alts=None):
    if index == 3:
        return None  # exercise the labeled-placeholder path
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2",
                    "-pix_fmt", "yuv420p", str(clip_path)], check=True)
    Image.new("RGB", (320, 180), (10, 80, 40)).save(thumb_path)
    return {"ai_generated": False, "source": "pixabay", "query": queries[0]}


_real_fetch = stock.fetch_clip
stock.fetch_clip = fake_fetch


def run_job(c, body, timeout=240):
    p = c.post("/api/projects", json=body).json()
    job = c.post(f"/api/projects/{p['id']}/generate").json()
    stages = []
    for _ in range(timeout):
        job = c.get(f"/api/jobs/{job['id']}").json()
        if not stages or stages[-1] != job["stage"]:
            stages.append(job["stage"])
        if job["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(1)
    return p, job, stages


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True).stdout
    return float(out)


def mean_volume(path):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    return float(re.search(r"mean_volume:\s*(-?[\d.]+)", r.stderr).group(1))


def test_validation_and_traversal():
    with TestClient(app) as c:
        assert c.post("/api/projects", json={"topic": "x" * 501, "audience": "kids"}).status_code == 422
        r = c.post("/api/projects", json={"mode": "prompt", "prompt": "short"})
        assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"
        assert c.post("/api/projects", json={"topic": "bikes", "audience": "kids", "voice": "bad;voice"}).status_code == 422
        assert c.get("/api/jobs/" + "0" * 32).json()["error"]["code"] == "job_not_found"
        assert c.get("/api/voices?language=xx").status_code == 422


def test_explainer_exact_length_music_effects_and_cleanup():
    CALLS.clear()
    with TestClient(app) as c:
        p, job, stages = run_job(c, {"topic": "How a bicycle works", "audience": "kids", "duration": 30, "style": "whiteboard",
                                     "aspect_ratio": "9:16", "tone": "energetic", "voice": "Test Voice", "voice_speed": 1.2,
                                     "background_music": True, "music_style": "auto", "sound_effects": True})
        assert job["status"] == "completed", job.get("error")
        assert stages[-1] == "completed"
        v = c.get(f"/api/projects/{p['id']}/video")
        assert v.status_code == 200 and v.headers["content-type"] == "video/mp4"
        vid = Path(settings.projects_dir / p["id"] / "video.mp4")
        assert abs(probe(vid) - 30.0) < 0.15, probe(vid)          # the UI promises exactly 0:30
        assert mean_volume(vid) > -50                               # narration + music + effects are audible
        assert any(vc == "Test Voice" for vc, _ in CALLS) and max(sp for _, sp in CALLS) > 1.2   # voice + speed + tone reach the TTS
        proj = c.get(f"/api/projects/{p['id']}").json()
        assert proj["video_seconds"] == 30.0 and proj["tone"] == "energetic" and proj["sound_effects"] is True
        left = [f.name for f in (settings.projects_dir / p["id"]).iterdir() if f.name.endswith(("_clip.mp4", "_bg.png", "_a.png", "_b.png", "_text.png"))]
        assert left == []                                           # intermediates are deleted after rendering
        assert storage.usage_bytes() <= storage.limit_bytes()
        assert c.get(f"/api/projects/{p['id']}/assets/..%2fapp.db").status_code in (404, 422)


def test_prompt_mode_and_silent_music_only():
    with TestClient(app) as c:
        p, job, _ = run_job(c, {"mode": "prompt", "prompt": "A cheerful video about a day in the life of a coffee farmer",
                                "duration": 30, "background_music": False, "sound_effects": False, "tone": "calm"})
        assert job["status"] == "completed", job.get("error")
        assert abs(probe(settings.projects_dir / p["id"] / "video.mp4") - 30.0) < 0.15


def test_storage_eviction_keeps_limit_and_protected_project():
    old = settings.storage_limit_mb
    try:
        with TestClient(app):
            for i in range(4):
                pid = f"{i:032x}"
                d = settings.projects_dir / pid
                d.mkdir(parents=True, exist_ok=True)
                (d / "video.mp4").write_bytes(b"0" * (2 * storage.MB))
                os.utime(d, (1000 + i, 1000 + i))
                db.save_project({"id": pid, "video_ready": True, "status": "completed"})
            keep = f"{3:032x}"
            base = storage.usage_bytes()
            settings.storage_limit_mb = int(base / storage.MB) - 3          # force eviction of ~3+ MB
            assert storage.enforce({keep})
            assert storage.usage_bytes() <= storage.limit_bytes()
            assert (settings.projects_dir / keep).exists()                  # protected project survives
            assert not (settings.projects_dir / f"{0:032x}").exists()       # oldest was removed
            assert db.get_project(f"{0:032x}")["status"] == "expired"
    finally:
        settings.storage_limit_mb = old


def test_voices_endpoints_when_narration_is_off():
    with TestClient(app) as c:
        settings.tts_backend = "none"
        try:
            assert c.get("/api/voices?language=en-us").json() == {"backend": "none", "voices": []}
            r = c.get("/api/voices/preview?language=en-us&voice=auto&tone=calm&speed=1")
            assert r.status_code == 400 and r.json()["error"]["code"] == "preview_failed"
        finally:
            settings.tts_backend = "windows"
        settings.tts_backend = "kokoro"
        try:
            v = c.get("/api/voices?language=en-gb").json()
            assert v["backend"] == "kokoro" and v["voices"][0]["id"] == "bf_emma"
        finally:
            settings.tts_backend = "windows"


def test_pixabay_parsing_and_relevance(monkeypatch):
    import httpx
    body = {"hits": [{"id": 7, "pageURL": "https://pixabay.com/videos/x-7/", "tags": "bicycle, road",
                      "videos": {"large": {"url": "", "width": 1920, "height": 1080},
                                 "medium": {"url": "https://cdn/m.mp4", "width": 1280, "height": 720, "thumbnail": "https://cdn/t.jpg"},
                                 "tiny": {"url": "https://cdn/t.mp4", "width": 640, "height": 360}}}]}

    class R:
        status_code = 200
        def json(self): return body
    monkeypatch.setattr(httpx, "get", lambda *a, **k: R())
    res = stock._pixabay("bicycle unique-query", "16:9")
    assert res[0]["id"] == "pixabay7" and stock._pick_file(res[0]["files"], "16:9")["width"] == 1280
    hits = [{"id": "a", "files": [{"link": "x", "width": 1280, "height": 720}], "thumb": None, "page": "p", "tags": "lava, volcano"}]
    monkeypatch.setattr(stock, "_pixabay", lambda q, a: hits)
    d = Path(tempfile.mkdtemp()) / "proj1"; d.mkdir()
    assert _real_fetch(["nuclear reactor"], "16:9", 1, d / "c.mp4", d / "t.png", context="reactor core") is None


def _ai_setup(monkeypatch, behave):
    from app.services import video_ai
    monkeypatch.setattr(settings, "video_backend", "hf_space")
    monkeypatch.setattr(settings, "hf_video_space", "owner/space")
    monkeypatch.setattr(settings, "video_max_clips", 2)
    monkeypatch.setattr(video_ai, "make_clip", behave)


def test_ai_video_failure_keeps_the_job_going_with_images(monkeypatch):
    from app.core.errors import PipelineError
    calls = []

    def boom(project, script, scene, index, pdir):
        calls.append(index)
        raise PipelineError("free GPU quota used up")

    _ai_setup(monkeypatch, boom)
    with TestClient(app) as c:
        p, job, _ = run_job(c, {"topic": "How a bicycle works", "audience": "kids", "duration": 30})
        assert job["status"] == "completed", job.get("error")
        assets = {a["name"]: a for a in c.get(f"/api/projects/{p['id']}/assets").json()["assets"]}
    assert calls == [1]  # circuit breaker: after the first failure the 2nd planned clip is not attempted
    assert "AI video unavailable" in assets["scene_01.png"]["note"]
    assert "AI video unavailable" in assets["scene_04.png"]["note"]


def test_ai_video_clip_is_rendered_into_the_final_video(monkeypatch):
    from PIL import Image

    def good(project, script, scene, index, pdir):
        n = f"scene_{index:02d}"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=2",
                        "-pix_fmt", "yuv420p", str(pdir / f"{n}_clip.mp4")], check=True)
        Image.new("RGB", (320, 180), (10, 80, 40)).save(pdir / f"{n}.png")
        Image.new("RGBA", (720, 1280), (0, 0, 0, 0)).save(pdir / f"{n}_text.png")
        return {"ai_generated": True, "kind": "video", "model": "owner/space"}

    _ai_setup(monkeypatch, good)
    with TestClient(app) as c:
        p, job, _ = run_job(c, {"topic": "How a bicycle works", "audience": "kids", "duration": 30, "aspect_ratio": "9:16"})
        assert job["status"] == "completed", job.get("error")
        assets = {a["name"]: a for a in c.get(f"/api/projects/{p['id']}/assets").json()["assets"]}
        assert c.get(f"/api/projects/{p['id']}/video").status_code == 200
    assert assets["scene_01.png"]["ai_generated"] is True and assets["scene_01.png"]["kind"] == "video"


# ---------- extra footage sources (mocked: no network) ----------
def _fake_http(monkeypatch, routes):
    """Route httpx.get by URL substring to canned JSON."""
    import httpx

    class R:
        status_code = 200
        def __init__(self, body): self._b = body
        def json(self): return self._b

    def get(url, **k):
        for key, body in routes.items():
            if key in url:
                return R(body)
        raise AssertionError(f"unexpected request {url}")
    monkeypatch.setattr(httpx, "get", get)


def test_nasa_parsing(monkeypatch):
    _fake_http(monkeypatch, {
        "/search": {"collection": {"items": [{"data": [{"nasa_id": "apollo 11", "title": "Apollo 11 launch", "keywords": ["moon", "rocket"],
                                                          "media_type": "video", "center": "KSC"}],
                                              "links": [{"href": "https://t/x.jpg", "render": "image"}]}]}},
        "/asset/": {"collection": {"items": [{"href": "http://a/apollo~orig.mp4"}, {"href": "http://a/apollo~medium.mp4"},
                                             {"href": "http://a/apollo~small.mp4"}, {"href": "http://a/apollo.srt"}]}}})
    res = stock._nasa("moon launch unique-nasa", "16:9")
    assert res[0]["id"] == "nasaapollo 11" and "moon" in res[0]["tags"]
    files = res[0]["files"]()
    assert [f["width"] for f in files] == [960, 640] and files[0]["link"].startswith("https://")  # orig skipped, https forced


def test_wikimedia_license_filter_and_credit(monkeypatch):
    def page(pid, lic, title):
        return {"pageid": pid, "title": f"File:{title}.webm", "videoinfo": [{
            "url": "https://u/f.webm", "mime": "video/webm", "width": 1280, "height": 720, "size": 1000, "thumburl": "https://u/t.jpg",
            "derivatives": [{"src": "https://u/f.720p.webm", "type": "video/webm; codecs=vp9", "width": 1280, "height": 720}],
            "extmetadata": {"LicenseShortName": {"value": lic}, "Artist": {"value": "<a href='x'>Jane</a>"},
                            "Categories": {"value": "Volcanoes|Lava"}}}]}
    _fake_http(monkeypatch, {"commons.wikimedia.org": {"query": {"pages": {
        "1": page(1, "CC BY 4.0", "Lava_flow"), "2": page(2, "CC BY-NC 2.0", "Lava_nc"),
        "3": page(3, "CC BY-SA 3.0", "Lava_sa"), "4": page(4, "Public domain", "Lava_pd")}}}})
    res = stock._wikimedia("lava unique-wiki", "16:9")
    assert sorted(v["id"] for v in res) == ["wikimedia1", "wikimedia4"]  # NC and share-alike are dropped by default
    by = next(v for v in res if v["id"] == "wikimedia1")
    assert by["credit_required"] and "Jane" in by["credit"] and "<" not in by["credit"] and "lava" in by["tags"].lower()


def test_archive_and_smithsonian_parsing(monkeypatch):
    _fake_http(monkeypatch, {
        "advancedsearch.php": {"response": {"docs": [{"identifier": "volcano_film", "title": "Volcano eruption film", "subject": ["lava", "volcano"]}]}},
        "/metadata/": {"files": [{"name": "v.mp4", "size": "5000000", "width": "640", "height": "480"},
                                 {"name": "big.mp4", "size": str(900 * 1024 * 1024)}, {"name": "v.ogv", "size": "10"}]},
        "api.si.edu": {"response": {"rows": [
            {"id": "edanmdm:1", "content": {"descriptiveNonRepeating": {"title": {"content": "Volcano lab film"}, "record_link": "https://si/1",
                                                                         "online_media": {"media": [{"type": "Videos", "content": "https://si/v.mp4", "thumbnail": "https://si/t.jpg", "usage": {"access": "CC0"}}]}},
                                            "indexedStructured": {"topic": ["Volcanoes"]}}},
            {"id": "edanmdm:2", "content": {"descriptiveNonRepeating": {"title": {"content": "Not free"},
                                                                         "online_media": {"media": [{"type": "Videos", "content": "https://si/n.mp4", "usage": {"access": "Restricted"}}]}}}}]}}})
    a = stock._archive("volcano eruption unique-archive", "16:9")
    assert a[0]["id"] == "archivevolcano_film" and [f["link"].rsplit("/", 1)[-1] for f in a[0]["files"]()] == ["v.mp4"]
    s = stock._smithsonian("volcano unique-si", "16:9")
    assert [v["id"] for v in s] == ["smithsonianedanmdm:1"] and "volcanoes" in s[0]["tags"].lower()


def test_stock_mode_skips_failing_and_keyless_providers(monkeypatch):
    from app.core.errors import PipelineError
    monkeypatch.setattr(settings, "image_backend", "stock")
    monkeypatch.setattr(settings, "stock_providers", "nasa,pixabay,wikimedia,bogus")
    monkeypatch.setattr(settings, "pixabay_api_key", None)

    def boom(q, a): raise PipelineError("NASA is down")
    ok = {"id": "wikimedia9", "files": [{"link": "x", "width": 1280, "height": 720}], "thumb": None, "page": "p", "tags": "lava volcano"}
    monkeypatch.setattr(stock, "_nasa", boom)
    monkeypatch.setattr(stock, "_wikimedia", lambda q, a: [dict(ok)])
    res = stock._search("lava", "16:9")
    assert [v["id"] for v in res] == ["wikimedia9"] and res[0]["provider"] == "wikimedia"
    monkeypatch.setattr(settings, "image_backend", "nasa")  # a single provider still reports its error
    with pytest.raises(PipelineError):
        stock._search("lava", "16:9")


def test_credit_line_goes_above_hashtags():
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("daily_post", pathlib.Path(__file__).parents[2] / "scripts" / "daily_post.py")
    src = spec.loader.get_source("daily_post")
    ns = {}
    start = src.index("def add_credits"); end = src.index("def build_caption")
    exec(src[start:end], ns)
    cap = ns["add_credits"]("Title\n\nBody text?\n\n#a #b", {"scene_01.png": {"credit_required": True, "credit": "Jane / Wikimedia Commons (CC BY 4.0)"},
                                                            "scene_02.png": {"credit_required": False, "credit": "NASA"}})
    assert cap.splitlines()[-1] == "#a #b" and "Footage: Jane / Wikimedia Commons (CC BY 4.0)" in cap and "NASA" not in cap


# ---------- aliases, framing, phone-safe text ----------
def test_alias_match_never_accepts_a_different_animal(monkeypatch):
    must, alts = stock.tokens("pistol shrimp"), [stock.tokens("snapping shrimp")]
    snapping = {"tags": "snapping shrimp, coral reef"}
    other = {"tags": "mantis shrimp, coral reef"}          # a different animal that shares the word 'shrimp'
    assert stock.has_subject(snapping, must, alts) and not stock.has_subject(other, must, alts)
    assert stock.has_subject({"tags": "pistol shrimp"}, must, alts) and not stock.has_subject(snapping, must, None)


def test_fetch_clip_uses_alias_when_subject_name_finds_nothing(monkeypatch):
    seen = []
    hit = {"id": "w1", "provider": "wikimedia", "files": [], "thumb": None, "page": "p", "tags": "snapping shrimp reef"}

    def fake_search(q, a):
        seen.append(q)
        return [dict(hit)] if "snapping" in q else []
    monkeypatch.setattr(stock, "_search", fake_search)
    monkeypatch.setattr(stock, "_download", lambda *a, **k: (_ for _ in ()).throw(stock.PipelineError("stop here")))
    d = Path(tempfile.mkdtemp()) / "p"; d.mkdir()
    r = _real_fetch(["pistol shrimp", "snapping shrimp"], "9:16", 1, d / "c.mp4", d / "t.png",
                    must=stock.tokens("pistol shrimp"), alts=[stock.tokens("snapping shrimp")])
    assert r is None and "snapping shrimp" in seen  # the alias clip was found and tried (download stubbed to fail)


def test_frame_filter_modes():
    f = stock.frame_filter(1280, 720, 720, 1280)            # 16:9 footage into a 9:16 Reel
    assert "gblur" in f and "scale=720:720" in f            # panel keeps a square-ish part of the picture
    assert "gblur" not in stock.frame_filter(720, 1280, 720, 1280)    # already the right shape: plain fill
    assert "gblur" not in stock.frame_filter(0, 0, 720, 1280)         # unknown size: plain fill
    old = settings.footage_framing
    try:
        settings.footage_framing = "fill"
        assert "gblur" not in stock.frame_filter(1280, 720, 720, 1280)
    finally:
        settings.footage_framing = old


def test_portrait_caption_stays_in_phone_safe_area():
    from app.services import images
    W, H = 720, 1280
    for text in ("Pistol shrimp readies its claw to snap", "Hi", "An extremely long caption that keeps going and going " * 3):
        layer = images._text_layer((W, H), text, None)
        x0, y0, x1, y1 = layer.getchannel("A").getbbox()
        assert x0 >= W * 0.06 and x1 <= W * 0.94 and y0 >= H * 0.13, (text[:20], (x0, y0, x1, y1))
        assert abs((x0 + x1) / 2 - W / 2) < 6  # centred, so a tall phone's side crop cuts both sides equally


def test_daily_post_guard_and_aliases(monkeypatch):
    import importlib.util, pathlib
    path = pathlib.Path(__file__).parents[2] / "scripts" / "daily_post.py"
    src = path.read_text(encoding="utf-8")
    ns = {"re": re, "log": lambda *a: None}
    for name in ("clean_subject", "subject_aliases"):
        a = src.index(f"def {name}"); b = src.index("\ndef ", a + 5)
        exec(src[a:b], ns)
    ns["FILLER"] = set()
    ns["llm_json"] = lambda *a, **k: {"aliases": ["Snapping Shrimp", "shrimp", "pistol shrimp", "Alpheidae", "snapping shrimp"]}
    # 'shrimp' is too broad (subset of the subject), the subject itself and duplicates are dropped
    assert ns["subject_aliases"]("pistol shrimp", "x") == ["snapping shrimp", "alpheidae"]
    assert ns["subject_aliases"]("", "x") == []
    # guard: a scene without a stock source and without AI is a placeholder, whichever backend made it
    meta = {"a": {"source": "nasa"}, "b": {"ai_generated": False, "model": None, "note": "placeholder graphic (IMAGE_BACKEND=fallback)"},
            "c": {"ai_generated": True}}
    assert [k for k, v in meta.items() if not v.get("source") and not v.get("ai_generated")] == ["b"]


def test_no_footage_explains_why(monkeypatch):
    from app.core.errors import PipelineError
    monkeypatch.setattr(settings, "image_backend", "stock")
    monkeypatch.setattr(settings, "stock_providers", "nasa,wikimedia,archive,pixabay")
    monkeypatch.setattr(settings, "pixabay_api_key", None)
    monkeypatch.setattr(stock, "_nasa", lambda q, a: [])
    monkeypatch.setattr(stock, "_wikimedia", lambda q, a: (_ for _ in ()).throw(PipelineError("Wikimedia returned 403")))
    monkeypatch.setattr(stock, "_archive", lambda q, a: [{"id": "a1", "files": [], "thumb": None, "page": "p", "tags": "bicycle"}])
    d = Path(tempfile.mkdtemp()) / "p2"; d.mkdir()
    assert _real_fetch(["mantis shrimp"], "9:16", 1, d / "c.mp4", d / "t.png", must=stock.tokens("mantis shrimp")) is None
    msg = stock.last_diag
    assert "nasa 0 clips" in msg and "wikimedia ERROR" in msg and "403" in msg and "pixabay ERROR: no API key" in msg
    assert "archive 1 clips" in msg and "none was tagged with the subject (mantis shrimp)" in msg


def test_real_api_flow_flags_placeholder_scene_for_daily_post():
    """The project record has no footage info; daily_post must read it from /assets or it can never see a placeholder."""
    import pathlib
    src = (pathlib.Path(__file__).parents[2] / "scripts" / "daily_post.py").read_text(encoding="utf-8")
    ns = {"re": re}
    for name in ("scenes_meta", "footage_gaps"):
        a = src.index(f"def {name}"); b = src.index("\ndef ", a + 5)
        exec(src[a:b], ns)
    with TestClient(app) as c:
        p, job, _ = run_job(c, {"topic": "How a bicycle works", "audience": "kids", "duration": 30})
        assert job["status"] == "completed", job.get("error")
        assert "assets_meta" not in c.get(f"/api/projects/{p['id']}").json()  # the reason the old guard saw nothing
        meta = ns["scenes_meta"](c.get(f"/api/projects/{p['id']}/assets").json()["assets"])
    assert meta and ns["footage_gaps"](meta) == ["scene_03.png"]  # fake_fetch gives scene 3 no footage
    assert ns["footage_gaps"]({}) == []  # empty is handled separately in main(): it refuses to post
