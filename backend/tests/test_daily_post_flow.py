"""A Facebook failure must not stop the YouTube upload, and the same animal must not come back a second time."""
import importlib
import json
import pathlib
import sys

import pytest

SCRIPTS = pathlib.Path(__file__).resolve().parents[2] / "scripts"


@pytest.fixture
def dp(monkeypatch, tmp_path):
    for k, v in {"API_URL": "http://x", "FB_PAGE_ID": "1", "FB_PAGE_TOKEN": "t", "YT_ENABLED": "true",
                 "HISTORY_FILE": str(tmp_path / "posted.json"), "RESULT_FILE": str(tmp_path / "result.json"),
                 "DRY_RUN": "", "TOPIC": "", "NICHE": ""}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    for m in ("daily_post", "youtube_upload", "thumbnail"):
        sys.modules.pop(m, None)
    return importlib.import_module("daily_post")


def test_same_subject_is_a_repeat_even_when_the_topic_is_worded_differently(dp):
    history = [{"topic": "Arctic tern: the bird that flies pole to pole every year", "title": "Pole to pole", "subject": "arctic tern"}]
    assert dp.subject_repeat("arctic tern", history)
    assert dp.subject_repeat("tern", [{"topic": "x", "title": "Arctic tern record", "subject": ""}])
    assert not dp.subject_repeat("octopus", history)
    assert not dp.subject_repeat("", history)


def _stub_video(dp, monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * 2048)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dp, "THUMB_FILE", tmp_path / "thumb.jpg")
    monkeypatch.setattr(dp, "check_token", lambda: None)
    monkeypatch.setattr(dp, "pick_topic", lambda h: {"topic": "Why octopuses have three hearts", "subject": "octopus",
                                                    "audience": "everyone", "hashtags": []})
    monkeypatch.setattr(dp, "subject_aliases", lambda s, t: [])
    monkeypatch.setattr(dp, "wake_backend", lambda: None)
    monkeypatch.setattr(dp, "make_video", lambda t, d: (video, {"assets_meta": {"scene_01.png": {"source": "pixabay", "tags": "octopus"}}}))
    monkeypatch.setattr(dp, "build_caption", lambda t, p: ("Three hearts", "Octopuses have three hearts. Why?\n\n#octopus #sea #ocean #facts #science"))


def test_facebook_failure_still_leaves_the_video_ready_for_youtube(dp, monkeypatch, tmp_path):
    _stub_video(dp, monkeypatch, tmp_path)

    def broken_post(path, caption, record):
        raise SystemExit("reel upload failed (400): token expired")
    monkeypatch.setattr(dp, "post_reel", broken_post)
    monkeypatch.setattr(dp, "pick_duration", lambda: 60)
    with pytest.raises(SystemExit) as e:
        dp.main()
    assert "Facebook posting failed" in str(e.value.code)   # the run is still reported as failed
    assert dp.R["youtube_ready"] is True                    # but YouTube may upload the finished video
    assert dp.R["facebook"] == "failed"
    assert dp.R["title"] == "Three hearts"                  # title and caption are saved for the YouTube step


def test_facebook_success_still_works(dp, monkeypatch, tmp_path):
    _stub_video(dp, monkeypatch, tmp_path)
    monkeypatch.setattr(dp, "post_reel", lambda path, caption, record: record("123"))
    monkeypatch.setattr(dp, "pick_duration", lambda: 60)
    dp.main()
    assert dp.R["stage"] == "done" and dp.R["youtube_ready"] is True and dp.R["facebook"] == "posted"
    assert json.loads(pathlib.Path(dp.HISTORY).read_text())["posts"][-1]["video_id"] == "123"


def test_bad_facebook_token_with_youtube_set_up_keeps_going(dp, monkeypatch, tmp_path):
    _stub_video(dp, monkeypatch, tmp_path)
    monkeypatch.setattr(dp, "check_token", lambda: (_ for _ in ()).throw(SystemExit("FB_PAGE_TOKEN was rejected (190)")))
    monkeypatch.setattr(dp, "pick_duration", lambda: 60)
    with pytest.raises(SystemExit):
        dp.main()
    assert dp.R["youtube_ready"] is True and "rejected" in dp.R["fb_error"]


def test_bad_facebook_token_without_youtube_stops_early(dp, monkeypatch, tmp_path):
    _stub_video(dp, monkeypatch, tmp_path)
    monkeypatch.setenv("YT_ENABLED", "false")
    monkeypatch.setattr(dp, "check_token", lambda: (_ for _ in ()).throw(SystemExit("FB_PAGE_TOKEN was rejected (190)")))
    called = []
    monkeypatch.setattr(dp, "wake_backend", lambda: called.append(1))
    with pytest.raises(SystemExit):
        dp.main()
    assert not called  # no minutes wasted rendering a video that cannot be posted anywhere


def test_a_repeat_found_at_the_last_moment_is_a_skip_not_a_failure(dp, monkeypatch, tmp_path):
    _stub_video(dp, monkeypatch, tmp_path)
    pathlib.Path(dp.HISTORY).write_text(json.dumps({"posts": [{"topic": "Why octopuses have three hearts", "title": "Three hearts"}]}))
    monkeypatch.setattr(dp, "pick_duration", lambda: 60)
    dp.main()  # returns normally: exit code 0
    assert dp.R["youtube_ready"] is False and dp.R["skipped"] == "duplicate topic"


# ---------- youtube_upload ----------

@pytest.fixture
def yt(monkeypatch, tmp_path):
    monkeypatch.setenv("HISTORY_FILE", str(tmp_path / "posted.json"))
    monkeypatch.setenv("RESULT_FILE", str(tmp_path / "result.json"))
    monkeypatch.setenv("VIDEO_FILE", str(tmp_path / "video.mp4"))
    monkeypatch.setenv("YT_RESULT_FILE", str(tmp_path / "youtube.json"))
    for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"):
        monkeypatch.setenv(k, "x")
    monkeypatch.syspath_prepend(str(SCRIPTS))
    sys.modules.pop("youtube_upload", None)
    return importlib.import_module("youtube_upload")


def test_youtube_skips_a_run_that_made_no_publishable_video(yt, tmp_path):
    (tmp_path / "video.mp4").write_bytes(b"x")
    (tmp_path / "result.json").write_text(json.dumps({"topic": "t", "error": "3 scenes found no footage"}))
    assert yt.main() == 2


def test_topic_is_recorded_when_only_youtube_took_the_video(yt, tmp_path):
    yt.record_topic({"topic": "Why octopuses have three hearts", "subject": "octopus", "title": "Three hearts"}, "YT1")
    posts = json.loads((tmp_path / "posted.json").read_text())["posts"]
    assert posts[-1]["youtube_id"] == "YT1" and posts[-1]["state"] == "YOUTUBE_ONLY"


def test_topic_is_not_recorded_twice_or_for_a_test_upload(yt, tmp_path):
    yt.record_topic({"topic": "t", "posted_at": "2026-10-24T12:00:00+00:00"}, "YT1")   # Facebook already recorded it
    yt.record_topic({"topic": "t", "dry_run": True}, "YT2")                            # private test upload
    assert not (tmp_path / "posted.json").exists()
