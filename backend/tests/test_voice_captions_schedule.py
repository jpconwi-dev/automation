"""Voice with feeling, creator-style captions and headline, and the best-times posting schedule."""
import datetime as dt
import importlib
import pathlib
import re
import sys
import wave

import pytest

from app.services import captions, delivery
from app.workers import pipeline

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
post_schedule = importlib.import_module("post_schedule")
PHT = post_schedule.PHT


# ---------- voice ----------

def test_segments_give_each_sentence_its_own_feeling():
    segs = delivery.segment("It sits on a branch. One bite is deadly. Their hearts beat 1,200 times a minute. "
                            "Which fact surprised you most?", None, True, 1.0)
    assert [s.emotion for s in segs] == ["neutral", "serious", "awe", "question"]
    assert segs[-1].absolute and segs[-1].speed <= delivery.QUESTION_SPEED_CEIL and segs[-1].gap_before >= 0.4
    assert segs[1].pitch < 0 < segs[2].gain          # serious sits lower, amazement is a little louder
    assert segs[2].gap_before > segs[1].gap_before   # the amazed line is given room to land first


def test_same_feeling_runs_stay_together_and_mid_video_questions_are_only_curious():
    segs = delivery.segment("It sits on a branch. It waits for rain.", None, False)
    assert len(segs) == 1
    mid = delivery.segment("Ever wondered how it flies?", None, False)
    assert mid[0].emotion == "curious" and not mid[0].absolute


def test_the_writers_feeling_colours_flat_sentences():
    segs = delivery.segment("It crosses two oceans. It never stops.", "awe", False)
    assert [s.emotion for s in segs] == ["awe"]


def _wav(path, secs, rate=24000, value=2000):
    import math
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(int(value * math.sin(i / 20)).to_bytes(2, "little", signed=True) for i in range(int(rate * secs))))


def test_shape_changes_the_sound_but_not_the_length(tmp_path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    _wav(a, 1.0); b.write_bytes(a.read_bytes())
    delivery.shape(b, semitones=1.2, gain_db=1.0)
    assert a.read_bytes() != b.read_bytes()
    with wave.open(str(b)) as w:
        assert abs(w.getnframes() / w.getframerate() - 1.0) < 0.08


def test_speak_scene_reports_exact_cue_timings(tmp_path, monkeypatch):
    def fake(text, language, out_path, *, voice="auto", speed=1.0, **_):
        n = len(text.split()) * 0.3
        _wav(out_path, n)
        return n
    monkeypatch.setattr(pipeline.tts, "synthesize", fake)
    project = {"language": "en-us", "voice": "auto", "voice_speed": 0.95}
    total, cues = pipeline._speak_scene("It sits on a branch. Their hearts beat 1,200 times a minute. Which fact surprised you most?",
                                        None, True, project, tmp_path / "s.wav", 1.0, False)
    assert [c[3] for c in cues] == ["neutral", "awe", "question"]
    starts = [c[1] for c in cues]
    assert starts == sorted(starts) and cues[-1][1] - (cues[-2][1] + cues[-2][2]) >= 0.5   # a beat before the question
    assert abs(cues[-1][1] + cues[-1][2] - total) < 0.05
    assert not list(tmp_path.glob("*.part*.wav"))                                          # temp pieces cleaned up


# ---------- captions ----------

def test_cards_are_short_and_keep_a_number_with_its_unit():
    cards = captions.split_cards("The Arctic tern flies up to 44,000 miles every single year, the longest migration known.".split(), 22)
    assert all(len(" ".join(c)) <= 30 and len(c) <= 4 for c in cards)
    flat = [" ".join(c) for c in cards]
    assert any("44,000 miles" in c for c in flat)
    assert all(len(c) >= 1 for c in cards)


def test_one_event_per_word_and_the_question_is_coral():
    cues = [("Their hearts beat 1,200 times a minute.", 0.2, 3.0, "awe"), ("Which fact surprised you most?", 3.6, 1.5, "question")]
    ev = captions.events(cues, 22, 6.0)
    assert len(ev) == len("Their hearts beat 1,200 times a minute.".split()) + len("Which fact surprised you most?".split())
    assert all(a[0] < a[1] for a in ev) and ev[0][0] == pytest.approx(0.2, abs=0.01)
    ass = captions.build_ass(cues, 720, 1280, "9:16", 6.0)
    assert "Poppins" in ass and "Style: Question" in ass and captions.CORAL in ass and captions.AMBER in ass
    assert ass.count("Dialogue:") == len(ev)
    first_q = [l for l in ass.splitlines() if "Question," in l and l.startswith("Dialogue")][0]
    assert "\\fad(60,0)" in first_q


def test_events_never_run_into_the_next_cue():
    cues = [("One two three four.", 0.0, 2.0, "neutral"), ("Five six seven.", 2.05, 1.5, "neutral")]
    ev = captions.events(cues, 22, 4.0)
    last_of_first = [e for e in ev if e[2][-1] == "four."][-1]
    assert last_of_first[1] <= 2.05 + 1e-6


def test_captions_font_ships_with_the_project():
    from app.services import render
    assert (render.FONT_DIR / "Poppins-Bold.ttf").is_file()


# ---------- headline ----------

def test_headline_highlights_the_number_and_uses_amber_not_a_box():
    from app.services import images
    layer = images._text_layer((720, 1280), "Arctic tern's 44,000-mile record migration", None)
    px = layer.getdata()
    assert any(p[3] == 255 and p[0] > 240 and 180 < p[1] < 210 and p[2] < 20 for p in px)       # amber letters
    box = [p for p in px if p[3] > 200 and p[0] < 30 and p[1] < 40 and p[2] < 50]                # only outlines are dark, no panel
    assert len(box) < 0.12 * 720 * 1280


# ---------- posting schedule ----------

def test_workflow_cron_matches_the_schedule_table():
    yml = (ROOT / ".github" / "workflows" / "daily-post.yml").read_text(encoding="utf-8")
    in_file = re.findall(r'- cron: "([^"]+)"', yml)
    assert in_file == post_schedule.cron_lines(), "run `python scripts/post_schedule.py` and paste the lines into the workflow"


def test_every_slot_is_started_early_enough_in_the_right_weekday():
    ref = dt.datetime(2026, 10, 26, tzinfo=PHT)  # a Monday
    expect = {  # weekday -> slots from the table
        0: [(15, 30), (19, 0)], 1: [(16, 0)], 2: [(15, 30), (19, 30)], 3: [(17, 0)], 4: [(16, 0), (19, 0)],
        5: [(10, 30), (17, 30)], 6: [(10, 30), (18, 0)]}
    assert post_schedule.SLOTS == expect
    lines = post_schedule.cron_lines()
    for wd, slots in expect.items():
        for h, m in slots:
            start = (ref + dt.timedelta(days=wd, hours=h, minutes=m - post_schedule.LEAD_MIN)).astimezone(dt.timezone.utc)
            cron_dow = (start.weekday() + 1) % 7
            assert any(l.startswith(f"{start.minute} {start.hour} * *") and str(cron_dow) in l.split()[-1].split(",") for l in lines), (wd, h, m)


def test_a_run_started_for_a_slot_knows_its_slot_and_a_manual_run_does_not():
    mon = dt.datetime(2026, 10, 26, 15, 8, tzinfo=PHT)                                  # started 22 min before Monday 15:30
    assert post_schedule.current_target(mon) == dt.datetime(2026, 10, 26, 15, 30, tzinfo=PHT)
    late = dt.datetime(2026, 10, 26, 15, 22, tzinfo=PHT)                                # GitHub started it 14 min late
    assert post_schedule.current_target(late) == dt.datetime(2026, 10, 26, 15, 30, tzinfo=PHT)
    assert post_schedule.current_target(dt.datetime(2026, 10, 26, 12, 0, tzinfo=PHT)) is None
    assert post_schedule.current_target(dt.datetime(2026, 10, 27, 21, 0, tzinfo=PHT)) is None


def test_next_post_after_the_last_slot_of_the_week_wraps_to_monday():
    sun_night = dt.datetime(2026, 11, 1, 20, 0, tzinfo=PHT)
    assert post_schedule.next_post(sun_night) == dt.datetime(2026, 11, 2, 15, 30, tzinfo=PHT)
    assert post_schedule.next_post(dt.datetime(2026, 10, 30, 17, 0, tzinfo=PHT)) == dt.datetime(2026, 10, 30, 19, 0, tzinfo=PHT)


def test_no_word_is_ever_lost_or_repeated_by_the_card_splitter():
    import random
    rnd = random.Random(4)
    vocab = ["a", "of", "44,000", "miles", "migration.", "fast,", "the", "Arctic", "tern", "heart", "beats", "1,200", "times?", "so"]
    for _ in range(400):
        words = [rnd.choice(vocab) for _ in range(rnd.randint(1, 18))]
        for limit in (16, 22, 34):
            cards = captions.split_cards(words, limit)
            assert [w for c in cards for w in c] == words
    # a trailing single word is folded into the card before it, and nothing else moves
    assert captions.split_cards("one two three four five".split(), 22) == [["one", "two", "three", "four", "five"]]
