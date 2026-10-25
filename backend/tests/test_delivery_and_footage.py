"""Closing-question delivery, no clipped narration, and look-alike footage (a moth must never stand in for a hummingbird)."""
import wave

import pytest

from app.core.config import settings
from app.core.errors import PipelineError
from app.services import delivery, stock, vision
from app.workers import pipeline


# ---------- closing question ----------

def test_question_is_split_off_and_slow():
    body, q = delivery.split_question("Their hearts beat over 1,200 times a minute. Which fact surprised you most?")
    assert body == "Their hearts beat over 1,200 times a minute."
    assert q == "Which fact surprised you most?"
    d = delivery.plan(q, "question", 1.0)
    assert d.emotion == "question" and d.speed < 0.95 and d.lead_pause >= 0.4 and d.hold >= 1.0


def test_question_speed_is_never_rushed_even_for_a_fast_voice():
    assert delivery.plan("Why?", "question", 1.3).speed <= delivery.QUESTION_SPEED_CEIL


def test_no_question_is_left_alone():
    assert delivery.split_question("They are fast.") == ("They are fast.", None)
    long_q = "Would you rather " + "really " * 20 + "know?"
    assert delivery.split_question(long_q)[1] is None


def test_emotion_from_the_sentence_when_the_writer_gave_none():
    assert delivery.classify("Which one would you pick?") == "question"
    assert delivery.classify("It is the fastest animal on Earth, at 389 km per hour.") == "awe"
    assert delivery.classify("One bite is deadly.") == "serious"
    assert delivery.classify("It sits on a branch.") == "neutral"
    assert delivery.classify("It sits on a branch.", "excited") == "excited"
    assert delivery.classify("It sits on a branch.", "nonsense") == "neutral"


def test_question_text_has_exactly_one_question_mark():
    assert delivery.question_text("What do you think??") == "What do you think?"
    assert delivery.question_text("What do you think.") == "What do you think?"


def test_join_wavs_adds_the_pause(tmp_path):
    def mk(name, secs):
        p = tmp_path / name
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(b"\x01\x00" * int(24000 * secs))
        return p
    total = delivery.join_wavs([mk("a.wav", 2.0), mk("b.wav", 1.0)], tmp_path / "o.wav", [0.0, 0.5])
    assert total == pytest.approx(3.5, abs=0.01)


@pytest.mark.parametrize("speech,duration", [([5.0] * 6, 60), ([9.0] * 6, 60), ([9.7, 9.7, 9.7, 9.7, 9.7, 9.7], 60)])
def test_plan_durations_never_cuts_the_last_scene(speech, duration):
    d = pipeline.plan_durations(speech, duration, pipeline.QUESTION_TAIL)
    assert abs(sum(d) - duration) < 1e-6
    assert d[-1] >= speech[-1] + 0.2          # the question and a beat after it always fit
    assert all(x >= s for x, s in zip(d, speech)) or sum(speech) + 6 * 0.2 > duration


# ---------- look-alike footage ----------

def test_moth_footage_is_never_used_for_a_hummingbird():
    bad = vision.conflict_words({"hummingbird"})
    assert vision.has_conflict("hummingbird hawk moth flower", bad)
    assert vision.has_conflict("hummingbird insect pollinator", bad)
    assert not vision.has_conflict("hummingbird feeding flower", bad)


def test_other_lookalikes():
    assert vision.has_conflict("alligator swamp", vision.conflict_words({"crocodile"}))
    assert not vision.has_conflict("crocodile river", vision.conflict_words({"crocodile"}))
    assert vision.conflict_words({"black", "bear"}) == set()


def _fresh_stock():
    """A private copy of the stock module: test_pipeline.py replaces stock.fetch_clip for the whole session when it is
    imported, so the real function has to be loaded again to be tested."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("app.services._stock_under_test", stock.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_stock_search_skips_lookalike_tagged_clips(monkeypatch, tmp_path):
    st = _fresh_stock()
    monkeypatch.setattr(settings, "storage_dir", tmp_path)
    files = [{"link": "x", "width": 10, "height": 10}]
    clips = [{"id": "p1", "tags": "hummingbird hawk moth flower", "files": files, "thumb": None, "provider": "pixabay"},
             {"id": "p2", "tags": "hummingbird flower", "files": files, "thumb": None, "provider": "pixabay"}]
    monkeypatch.setattr(st, "_search", lambda q, a, only=None: [dict(c) for c in clips])

    def no_download(link, dl, prov):
        raise PipelineError("stop here")  # we only want to know which clips were attempted
    monkeypatch.setattr(st, "_download", no_download)
    out = st.fetch_clip(["hummingbird flower"], "9:16", 2, tmp_path / "scene_02_clip.mp4", tmp_path / "scene_02.png",
                        must={"hummingbird"})
    assert out is None
    assert "p1" not in st._bad  # filtered by its tags, never even downloaded
    assert "p2" in st._bad      # the clean clip was attempted (it only failed because the download is stubbed)


# ---------- vision judging ----------

@pytest.mark.parametrize("answer,expected", [
    ("A hummingbird.", True), ("a hummingbird hovering at a flower", True),
    ("A moth.", False), ("a hawk moth on a pink flower", False),
    ("a flower", None), ("a hummingbird moth", True),  # names both: the subject word is there, tags decide
])
def test_judge(answer, expected):
    subject = {"hummingbird"}
    assert vision.judge(answer, subject, [], vision.conflict_words(subject)) is expected


def test_vision_rejects_a_moth_and_accepts_a_bird(monkeypatch):
    monkeypatch.setattr(settings, "footage_vision", "auto")
    monkeypatch.setattr(vision, "enabled", lambda: True)
    monkeypatch.setattr(vision, "_frame", lambda clip, at: b"jpeg")
    monkeypatch.setattr(vision, "_ask", lambda jpeg: "A moth.")
    with pytest.raises(PipelineError):
        vision.verify("x.mp4", 10, {"hummingbird"}, None)
    monkeypatch.setattr(vision, "_ask", lambda jpeg: "A hummingbird.")
    assert "hummingbird" in vision.verify("x.mp4", 10, {"hummingbird"}, None).lower()


def test_vision_failure_never_blocks_a_video(monkeypatch):
    monkeypatch.setattr(vision, "enabled", lambda: True)
    monkeypatch.setattr(vision, "_frame", lambda clip, at: b"jpeg")
    def boom(jpeg):
        raise PipelineError("Cloudflare is down")
    monkeypatch.setattr(vision, "_ask", boom)
    assert vision.verify("x.mp4", 10, {"hummingbird"}, None) == "vision: unavailable"


def test_strict_mode_needs_confirmation(monkeypatch):
    monkeypatch.setattr(settings, "footage_vision", "strict")
    monkeypatch.setattr(vision, "enabled", lambda: True)
    monkeypatch.setattr(vision, "_frame", lambda clip, at: b"jpeg")
    monkeypatch.setattr(vision, "_ask", lambda jpeg: "a flower")
    with pytest.raises(PipelineError):
        vision.verify("x.mp4", 10, {"hummingbird"}, None)
    assert vision.verify("x.mp4", 10, {"hummingbird"}, None, confirm=False).startswith("vision:")
