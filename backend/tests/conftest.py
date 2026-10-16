import json
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

from app.core import db
from app.core.config import settings
from app.services import script as script_mod
from app.services import tts


class MockOllama:
    """Tiny stand-in for Ollama. `ratio` makes the 'model' write too much or too little narration."""

    def __init__(self):
        self.ratio, self.project = 1.0, None
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length") or 0))
                p = outer.project
                n = script_mod.SCENE_COUNT[p["duration"]]
                per = max(6, round(script_mod.word_target(p) / n * outer.ratio))
                scenes = [{"narration": " ".join(["alpha beta gamma delta epsilon zeta eta theta"] * 20).split()[:per] and
                           " ".join((["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"] * 20)[:per]),
                           "visual_description": "a friendly drawing of a leaf", "on_screen_text": f"Step {i + 1}",
                           "subtitle_text": "x" * 10, "duration_seconds": 10, "search_keywords": "leaf"} for i in range(n)]
                body = {"title": "Test video", "description": "A test", "hook": "Hook here", "scenes": scenes,
                        "closing_summary": "Bye"}
                data = json.dumps({"message": {"content": json.dumps(body)}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    mock = MockOllama()
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    monkeypatch.setattr(settings, "llm_backend", "ollama")
    monkeypatch.setattr(settings, "ollama_url", mock.url)
    monkeypatch.setattr(settings, "image_backend", "fallback")
    monkeypatch.setattr(settings, "tts_backend", "kokoro")
    monkeypatch.setattr(settings, "music_path", None)
    monkeypatch.setattr(settings, "ken_burns", False)
    db.init_db()
    state = {"wps": 2.6}

    def fake_synth(text, language, out_path, *, voice="auto", speed=1.0, pitch=0):
        """Pretends to be a voice: a quiet hum whose length depends on the word count."""
        secs = len(text.split()) / (state["wps"] * speed)
        t = np.arange(int(secs * 24000)) / 24000
        pcm = (0.25 * np.sin(2 * np.pi * 180 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t)) * 32767).astype("<i2")
        with wave.open(str(out_path), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm.tobytes())
        return secs

    monkeypatch.setattr(tts, "synthesize", fake_synth)
    return mock, state
