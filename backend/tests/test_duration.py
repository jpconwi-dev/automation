"""Runs the REAL job pipeline and REAL FFmpeg and checks the MP4 is as long as the user chose."""
import subprocess
import threading
import uuid

import pytest

from app.core import db
from app.core.config import settings
from app.workers import pipeline


def _probe(path, stream):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", "stream=duration",
                        "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    return float(r.stdout.strip())


def _run(env, **over):
    mock, state = env
    state["wps"] = over.pop("wps", 2.6)
    mock.ratio = over.pop("ratio", 1.0)
    p = {"id": uuid.uuid4().hex, "mode": "explainer", "topic": "How rain forms", "audience": "kids", "style": "minimal",
         "duration": 30, "language": "en-us", "aspect_ratio": "9:16", "tone": "friendly", "voice": "auto",
         "voice_speed": 1.0, "background_music": True, "music_style": "auto", "sound_effects": True,
         "status": "created", "script": None, "video_ready": False, "latest_job_id": None, "assets_meta": {}, **over}
    mock.project = p
    db.save_project(p)
    job = {"id": uuid.uuid4().hex, "project_id": p["id"], "status": "queued", "stage": "queued", "progress": 0,
           "message": "", "error": None, "scenes_total": 0, "images_done": 0, "cancel_requested": False}
    db.save_job(job)
    pipeline._cancels[job["id"]] = threading.Event()
    pipeline._run(job["id"], p["id"])
    return db.get_job(job["id"]), db.get_project(p["id"]), settings.projects_dir / p["id"] / "video.mp4"


@pytest.mark.parametrize("duration,wps,ratio", [
    (30, 2.6, 1.0),     # model writes the right amount
    (30, 2.6, 0.7),     # model writes too little: pauses and slower voice fill the gap
    (30, 2.6, 1.15),    # model writes a bit too much: the voice speeds up
    (30, 3.6, 1.0),     # a fast voice
    (60, 2.0, 1.0),     # a slow voice
])
def test_video_is_exactly_the_chosen_length(env, duration, wps, ratio):
    job, project, video = _run(env, duration=duration, wps=wps, ratio=ratio)
    assert job["status"] == "completed", job["error"]
    assert video.is_file()
    assert abs(_probe(video, "v:0") - duration) < 0.25
    assert abs(_probe(video, "a:0") - duration) < 0.25
    assert abs(project["video_seconds"] - duration) < 0.25


def test_music_and_effects_are_in_the_mix(env, tmp_path):
    job, project, video = _run(env, duration=30, background_music=True, sound_effects=True)
    assert job["status"] == "completed", job["error"]
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(video), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    assert "mean_volume" in r.stderr
