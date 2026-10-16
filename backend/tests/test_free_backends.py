"""Tests for the free-tier backends (OpenAI-compatible LLM, Cloudflare images + voice), the optional AI video
helpers and the access key. Remote services are replaced by small local HTTP servers that follow the documented
request/response shapes, so nothing here touches the network or spends quota."""
import base64
import io
import json
import subprocess
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.core.config import settings
from app.core.errors import PipelineError
from app.services import cloudflare, images, script as script_mod, tts, video_ai


class Server:
    """Local HTTP server. `handler(path, body_dict)` returns (status, json_or_bytes, content_type)."""

    def __init__(self, handler):
        outer = self
        self.calls: list[tuple[str, dict]] = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                outer.calls.append((self.path, body))
                status, payload, ctype = handler(self.path, body, dict(self.headers))
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self):
        self.httpd.shutdown()


def ok(content):
    return 200, {"choices": [{"message": {"content": content}}]}, "application/json"


# ---------------------------------------------------------------- OpenAI-compatible LLM

def test_llm_walks_fallback_models_when_rate_limited():
    seen = []

    def handler(path, body, headers):
        seen.append(body["model"])
        assert headers.get("Authorization") == "Bearer k123"
        return (429, {"error": {"message": "rate limit"}}, "application/json") if body["model"] == "big" else ok('{"x":1}')

    srv = Server(handler)
    try:
        out = script_mod._chat_openai([{"role": "user", "content": "hi"}], srv.url, "k123", ["big", "small"], "test", 10, 100)
    finally:
        srv.close()
    assert out == '{"x":1}' and seen == ["big", "small"]


def test_llm_retries_without_json_mode_when_rejected():
    def handler(path, body, headers):
        if "response_format" in body:
            return 400, {"error": {"message": "response_format unsupported"}}, "application/json"
        return ok("plain")

    srv = Server(handler)
    try:
        assert script_mod._chat_openai([], srv.url, "k", ["m"], "test", 10, 100) == "plain"
        assert len(srv.calls) == 2
    finally:
        srv.close()


def test_llm_bad_key_stops_immediately_and_missing_key_is_explained():
    srv = Server(lambda *a: (401, {"error": "no"}, "application/json"))
    try:
        with pytest.raises(PipelineError, match="rejected"):
            script_mod._chat_openai([], srv.url, "bad", ["a", "b"], "test", 10, 100)
        assert len(srv.calls) == 1  # did not keep trying other models with a rejected key
    finally:
        srv.close()
    with pytest.raises(PipelineError, match="API key"):
        script_mod._chat_openai([], "http://127.0.0.1:1", None, ["a"], "test", 10, 100)


def test_llm_all_models_limited_reports_the_rate_limit():
    srv = Server(lambda *a: (429, {}, "application/json"))
    try:
        with pytest.raises(PipelineError, match="rate limit|quota"):
            script_mod._chat_openai([], srv.url, "k", ["a", "b"], "test", 10, 100)
    finally:
        srv.close()


def test_generate_script_end_to_end_with_api_backend(monkeypatch):
    p = {"mode": "explainer", "topic": "How rain forms", "audience": "kids", "style": "minimal", "duration": 30,
         "language": "en-us", "tone": "friendly"}
    n = script_mod.SCENE_COUNT[30]
    per = script_mod.word_target(p) // n
    scenes = [{"narration": " ".join(["word"] * per), "visual_description": "a cloud over hills",
               "on_screen_text": f"Step {i}", "subtitle_text": "x" * 8, "duration_seconds": 7, "search_keywords": "cloud"}
              for i in range(n)]
    body = {"title": "Rain", "description": "How rain forms", "hook": "Ever wondered?", "scenes": scenes,
            "closing_summary": "Now you know."}
    # reasoning models put <think> blocks before the JSON; Qwen3 does this on Groq
    srv = Server(lambda *a: ok("<think>hmm</think>\n```json\n" + json.dumps(body) + "\n```"))
    monkeypatch.setattr(settings, "llm_backend", "api")
    monkeypatch.setattr(settings, "llm_base_url", srv.url)
    monkeypatch.setattr(settings, "llm_api_key", "k")
    try:
        result = script_mod.generate_script(p, lambda: None)
    finally:
        srv.close()
    assert result.title == "Rain" and len(result.scenes) == n


# ---------------------------------------------------------------- Cloudflare

@pytest.fixture
def cf(monkeypatch, tmp_path):
    state = {"mode": "ok"}

    def handler(path, body, headers):
        assert headers.get("Authorization") == "Bearer tok"
        if state["mode"] == "quota":
            return 429, {"success": False, "errors": [{"message": "daily free allocation of 10,000 neurons exceeded"}]}, "application/json"
        if "flux" in path:
            buf = io.BytesIO()
            Image.new("RGB", (1024, 1024), (200, 30, 90)).save(buf, "JPEG")
            return 200, {"success": True, "result": {"image": base64.b64encode(buf.getvalue()).decode()}}, "application/json"
        buf = io.BytesIO()  # a short WAV stands in for the MP3: FFmpeg detects the format from the content
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050); w.writeframes(b"\x10\x00" * 22050 * 2)
        return 200, {"success": True, "result": {"audio": base64.b64encode(buf.getvalue()).decode()}}, "application/json"

    srv = Server(handler)
    monkeypatch.setattr(settings, "cloudflare_base_url", srv.url)
    monkeypatch.setattr(settings, "cloudflare_account_id", "acct")
    monkeypatch.setattr(settings, "cloudflare_api_token", "tok")
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    yield srv, state
    srv.close()


def test_cloudflare_image_is_cropped_to_the_video_aspect_and_cached(cf):
    srv, _ = cf
    size = images.image_size("9:16")
    img = images._generate_cloudflare("a red leaf", 42, size)
    assert img.size == size and size[0] < size[1]
    path, body = srv.calls[0]
    assert "/accounts/acct/ai/run/@cf/black-forest-labs/flux-1-schnell" in path
    assert body["prompt"] == "a red leaf" and body["steps"] == settings.cloudflare_steps
    images._generate_cloudflare("a red leaf", 42, size)
    assert len(srv.calls) == 1  # second call came from the cache: no quota spent


def test_cloudflare_quota_message_is_clear(cf):
    _, state = cf
    state["mode"] = "quota"
    with pytest.raises(PipelineError, match="resets at 00:00 UTC"):
        images._generate_cloudflare("x", 1, (256, 256))


def test_cloudflare_needs_credentials(monkeypatch):
    monkeypatch.setattr(settings, "cloudflare_api_token", None)
    with pytest.raises(PipelineError, match="CLOUDFLARE_API_TOKEN"):
        cloudflare.run("@cf/x", {})


def test_cloudflare_tts_makes_a_wav_and_applies_speed(cf, tmp_path):
    out = tmp_path / "n.wav"
    normal = tts._synth_cloudflare("Hello there", "en-us", out, 1.0)
    fast = tts._synth_cloudflare("Hello there", "en-us", out, 1.5)
    assert 1.8 < normal < 2.2          # the stand-in audio is 2 s long
    assert fast < normal * 0.75        # atempo=1.5 shortens it
    with wave.open(str(out)) as w:
        assert w.getnchannels() == 1 and w.getframerate() == tts.SAMPLE_RATE
    assert not out.with_suffix(".mp3").exists()  # temp file cleaned up


def test_cloudflare_tts_rejects_languages_melotts_lacks(cf, tmp_path):
    with pytest.raises(PipelineError, match="no 'hi' voice"):
        tts._synth_cloudflare("namaste", "hi", tmp_path / "n.wav", 1.0)


# ---------------------------------------------------------------- AI video helpers

def test_pick_scenes_spreads_clips_and_always_includes_the_hook():
    assert video_ai.pick_scenes(10, 0) == set()
    assert video_ai.pick_scenes(10, 1) == {1}
    assert video_ai.pick_scenes(10, 2) == {1, 10}
    assert video_ai.pick_scenes(10, 3) == {1, 5, 10}
    assert video_ai.pick_scenes(4, 9) == {1, 2, 3, 4}  # never more clips than scenes


def test_space_kwargs_fill_placeholders_and_keep_number_types(monkeypatch):
    monkeypatch.setattr(settings, "hf_video_params",
                        '{"prompt": "{prompt}", "seed": "{seed}", "size": {"w": "{width}", "h": "{height}"}, "note": "{prompt}!"}')
    kw = video_ai.space_kwargs("a cat", 7, 768, 448)
    assert kw == {"prompt": "a cat", "seed": 7, "size": {"w": 768, "h": 448}, "note": "a cat!"}
    monkeypatch.setattr(settings, "hf_video_params", "not json")
    with pytest.raises(PipelineError, match="valid JSON"):
        video_ai.space_kwargs("a", 1, 1, 1)


def test_find_video_handles_the_shapes_gradio_returns():
    assert video_ai._find_video("/tmp/a.mp4") == "/tmp/a.mp4"
    assert video_ai._find_video({"video": {"path": "/tmp/b.webm"}}) == "/tmp/b.webm"
    assert video_ai._find_video(("/tmp/seed.txt", {"video": "/tmp/c.mp4"})) == "/tmp/c.mp4"
    assert video_ai._find_video(["nothing", 3]) is None


def _fake_ai_video(path, seconds=2):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=12",
                    "-pix_fmt", "yuv420p", str(path)], check=True)


def test_make_clip_writes_what_the_renderer_expects(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "video_backend", "hf_space")
    monkeypatch.setattr(settings, "hf_video_space", "owner/space")
    monkeypatch.setattr(video_ai, "_from_space", lambda prompt, seed, size, dest: _fake_ai_video(dest))
    project = {"id": "ab" * 16, "aspect_ratio": "9:16", "style": "minimal"}
    scene = type("S", (), {"visual_description": "a leaf falling", "on_screen_text": "Leaves fall"})()
    pdir = tmp_path / "p"
    pdir.mkdir()
    meta = video_ai.make_clip(project, None, scene, 3, pdir)
    assert meta == {"ai_generated": True, "kind": "video", "model": "owner/space"}
    for name in ("scene_03_clip.mp4", "scene_03.png", "scene_03_text.png"):
        assert (pdir / name).is_file(), name
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0",
                            str(pdir / "scene_03_clip.mp4")], capture_output=True, text=True).stdout.strip()
    assert probe == "720,1280"  # scaled and cropped to the video's frame
    assert not list((tmp_path / "tmp").glob("ai_*"))  # temp dir removed


def test_make_clip_failure_leaves_no_partial_files(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "video_backend", "hf_space")

    def boom(*a):
        raise PipelineError("quota used up")

    monkeypatch.setattr(video_ai, "_from_space", boom)
    pdir = tmp_path / "p"
    pdir.mkdir()
    scene = type("S", (), {"visual_description": "x" * 10, "on_screen_text": "t"})()
    with pytest.raises(PipelineError):
        video_ai.make_clip({"id": "cd" * 16, "aspect_ratio": "16:9", "style": "minimal"}, None, scene, 1, pdir)
    assert list(pdir.iterdir()) == []


def test_space_without_a_name_explains_what_to_set(monkeypatch):
    monkeypatch.setattr(settings, "hf_video_space", "")
    with pytest.raises(PipelineError, match="HF_VIDEO_SPACE"):
        video_ai._from_space("p", 1, (768, 448), None)


def test_gradio_client_call_shape(monkeypatch, tmp_path):
    """Uses a stand-in for gradio_client.Client so the test needs no network or GPU."""
    import sys
    import types

    clip = tmp_path / "result.mp4"
    _fake_ai_video(clip)
    calls = {}

    class FakeJob:
        def result(self, timeout=None):
            calls["timeout"] = timeout
            return ({"video": str(clip), "subtitles": None}, 123)

    class FakeClient:
        def __init__(self, src, token=None):
            calls["src"], calls["token"] = src, token

        def submit(self, api_name=None, **kw):
            calls["api_name"], calls["kw"] = api_name, kw
            return FakeJob()

    mod = types.ModuleType("gradio_client")
    mod.Client = FakeClient
    monkeypatch.setitem(sys.modules, "gradio_client", mod)
    monkeypatch.setattr(settings, "hf_video_space", "owner/space")
    monkeypatch.setattr(settings, "hf_video_api_name", "/infer")
    monkeypatch.setattr(settings, "hf_token", "hf_secret")
    dest = tmp_path / "dest.mp4"
    video_ai._from_space("a cat", 5, (768, 448), dest)
    assert dest.is_file()
    assert calls["src"] == "owner/space" and calls["api_name"] == "/infer" and calls["token"] == "hf_secret"
    assert calls["kw"] == {"prompt": "a cat"} and calls["timeout"] == settings.video_timeout


# ---------------------------------------------------------------- access key + health

@pytest.fixture
def client(monkeypatch, tmp_path):
    from app.core import db
    from app.main import app
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    db.init_db()
    return TestClient(app)


def test_api_is_open_when_no_access_key_is_set(client, monkeypatch):
    monkeypatch.setattr(settings, "access_key", None)
    assert client.get("/api/voices?language=en-us").status_code == 200
    assert client.get("/api/health").json()["auth_required"] is False


def test_access_key_protects_everything_except_health(client, monkeypatch):
    monkeypatch.setattr(settings, "access_key", "s3cret")
    assert client.get("/api/health").json()["auth_required"] is True
    assert client.get("/api/health").status_code == 200
    r = client.get("/api/voices?language=en-us")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert client.get("/api/voices?language=en-us", headers={"X-Access-Key": "wrong"}).status_code == 401
    assert client.get("/api/voices?language=en-us", headers={"X-Access-Key": "s3cret"}).status_code == 200
    assert client.get("/api/voices?language=en-us&key=s3cret").status_code == 200  # for <video>/<img> tags
    assert client.post("/api/projects", json={"topic": "Rain", "audience": "kids"}).status_code == 401


def test_a_401_still_carries_cors_headers_so_the_browser_can_read_it(client, monkeypatch):
    monkeypatch.setattr(settings, "access_key", "s3cret")
    origin = settings.origins[0]
    r = client.get("/api/voices?language=en-us", headers={"Origin": origin})
    assert r.status_code == 401 and r.headers.get("access-control-allow-origin") == origin
    pre = client.options("/api/voices", headers={"Origin": origin, "Access-Control-Request-Method": "GET",
                                                 "Access-Control-Request-Headers": "x-access-key"})
    assert pre.status_code == 200 and "x-access-key" in pre.headers["access-control-allow-headers"].lower()


def test_health_reports_missing_free_provider_keys(client, monkeypatch):
    monkeypatch.setattr(settings, "llm_backend", "api")
    monkeypatch.setattr(settings, "llm_api_key", None)
    monkeypatch.setattr(settings, "tts_backend", "cloudflare")
    monkeypatch.setattr(settings, "image_backend", "cloudflare")
    monkeypatch.setattr(settings, "cloudflare_account_id", None)
    monkeypatch.setattr(settings, "video_backend", "hf_space")
    monkeypatch.setattr(settings, "hf_video_space", "")
    deps = client.get("/api/health").json()["dependencies"]
    assert not deps["llm"]["ok"] and "LLM_API_KEY" in deps["llm"]["detail"]
    assert not deps["tts"]["ok"] and "CLOUDFLARE" in deps["tts"]["detail"]
    assert not deps["images"]["ok"] and "CLOUDFLARE" in deps["images"]["detail"]
    assert not deps["video"]["ok"] and "HF_VIDEO_SPACE" in deps["video"]["detail"]
    monkeypatch.setattr(settings, "llm_api_key", "k")
    monkeypatch.setattr(settings, "cloudflare_account_id", "a")
    monkeypatch.setattr(settings, "cloudflare_api_token", "t")
    monkeypatch.setattr(settings, "hf_video_space", "o/s")
    deps = client.get("/api/health").json()["dependencies"]
    assert deps["llm"]["ok"] and deps["tts"]["ok"] and deps["images"]["ok"] and deps["video"]["ok"]
