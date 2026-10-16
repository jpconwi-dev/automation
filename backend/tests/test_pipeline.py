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


def fake_fetch(queries, aspect, index, clip_path, thumb_path, context=""):
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
