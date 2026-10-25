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
    assert segs[-1].absolute and segs[-1].speed < 0.95 and segs[-1].gap_before >= 0.4
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


# ---------- found in a real video: overlapping cards, text off the screen, snail instead of snailfish ----------

def test_caption_events_never_overlap():
    import random
    rnd = random.Random(9)
    words = "a jelly-like body bones 1,200 times a minute fast, deep. Which fact amazes you most?".split()
    for _ in range(300):
        t, cues = 0.3, []
        for _ in range(rnd.randint(1, 5)):
            text = " ".join(rnd.choice(words) for _ in range(rnd.randint(1, 16)))
            length = rnd.uniform(0.3, 5)
            cues.append((text, t, length, rnd.choice(["neutral", "awe", "question"])))
            t += length + rnd.uniform(0, 0.6)
        ev = sorted(captions.events(cues, 22, t + 1), key=lambda e: e[0])
        for a, b in zip(ev, ev[1:]):
            assert a[1] <= b[0] + 1e-6, (a, b)


def test_captions_shrink_to_fit_when_the_text_is_wider_than_the_screen(tmp_path):
    from app.services import render
    import shutil
    (tmp_path / "fonts").mkdir()
    for f in render.FONT_DIR.glob("*.ttf"):
        shutil.copy(f, tmp_path / "fonts" / f.name)
    assert captions.fit_scale("reaches about 1,100 atm", 720, 1280, "9:16", tmp_path) == 1.0       # fits as designed
    wide = captions.fit_scale("reaches about 1,100 atm daily, always", 720, 1280, "9:16", tmp_path)
    assert 0.45 <= wide < 1.0                                                                       # too wide: made smaller
    assert not (tmp_path / "fit.png").exists()                                                      # no scratch files left


def test_ai_picture_of_a_look_alike_is_drawn_again(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from PIL import Image
    from app.core.config import settings
    from app.services import cloudflare, images, vision
    prompts = []
    monkeypatch.setattr(settings, "stock_ai_fallback", True)
    monkeypatch.setattr(cloudflare, "configured", lambda: True)
    monkeypatch.setattr(images, "_generate_cloudflare", lambda prompt, seed, size: (prompts.append(prompt), Image.new("RGB", size, (40, 60, 80)))[1])
    monkeypatch.setattr(vision, "enabled", lambda: True)
    answers = iter(["A snail with an orange shell.", "A snail.", "A translucent fish."])
    monkeypatch.setattr(vision, "_ask", lambda jpeg: next(answers))
    script = SimpleNamespace(title="Snailfish", subject_look="a small translucent pink deep-sea fish with a tadpole-shaped body; a fish, not a snail")
    scene = SimpleNamespace(visual_description="swimming near the seabed", on_screen_text="8,000 M")
    meta = images._ai_scene_fallback({"subject": "snailfish", "subject_aliases": []}, script, scene, (360, 640), 1, 2, tmp_path)
    assert meta and meta["vision"].startswith("vision: A translucent fish")
    assert len(prompts) == 3                                                  # two snails thrown away, the fish kept
    assert "translucent pink deep-sea fish" in prompts[0] and "not a snail" in prompts[0]   # appearance leads, not just the name


def test_a_picture_that_never_shows_the_subject_is_not_used(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from PIL import Image
    from app.core.config import settings
    from app.services import cloudflare, images, vision
    monkeypatch.setattr(settings, "stock_ai_fallback", True)
    monkeypatch.setattr(cloudflare, "configured", lambda: True)
    monkeypatch.setattr(images, "_generate_cloudflare", lambda prompt, seed, size: Image.new("RGB", size))
    monkeypatch.setattr(vision, "enabled", lambda: True)
    monkeypatch.setattr(vision, "_ask", lambda jpeg: "A snail.")
    script = SimpleNamespace(title="Snailfish", subject_look="")
    scene = SimpleNamespace(visual_description="x", on_screen_text="")
    assert images._ai_scene_fallback({"subject": "snailfish"}, script, scene, (360, 640), 1, 2, tmp_path) is None


def test_snailfish_look_alikes_are_known():
    assert {"snail", "shell"} <= vision_conflicts("snailfish")


def vision_conflicts(word):
    from app.services import vision
    return vision.conflict_words({word})


# ---------- one steady speaking rate ----------

def test_the_speaking_rate_is_levelled_across_sentences(tmp_path, monkeypatch):
    calls = []

    def uneven(text, language, out_path, *, voice="auto", speed=1.0, **_):
        # the voice is 25% fast on the first sentence and 25% slow on the third, as MeloTTS does
        k = len(calls); calls.append(text)
        secs = delivery.syllables(text) * 0.25 * (0.8, 1.0, 1.25)[k % 3]
        _wav(out_path, secs + 0.3)          # + silence-ish padding on both ends is trimmed away again
        return secs
    monkeypatch.setattr(pipeline.tts, "synthesize", uneven)
    project = {"language": "en-us", "voice": "auto", "voice_speed": 1.0}
    text = "It sits on a branch of the old tree today. One bite is deadly for most small fish. Their hearts beat 1,200 times a minute."
    total, cues = pipeline._speak_scene(text, None, False, project, tmp_path / "s.wav", 1.0, False)
    per_syllable = [ln / delivery.syllables(t) for t, st, ln, emo in cues]
    assert len(per_syllable) == 3
    assert max(per_syllable) / min(per_syllable) < 1.25      # was 1.56 (1.25 / 0.8) before levelling


def test_closing_question_stays_slower_than_the_rest_and_ignores_the_speed_up(tmp_path, monkeypatch):
    def steady(text, language, out_path, *, voice="auto", speed=1.0, **_):
        secs = delivery.syllables(text) * 0.25
        _wav(out_path, secs)
        return secs
    monkeypatch.setattr(pipeline.tts, "synthesize", steady)
    project = {"language": "en-us", "voice": "auto", "voice_speed": 0.95}
    text = "Their hearts beat 1,200 times a minute and that is the fastest of all the birds. Which fact amazed you the most?"
    rates = {}
    parts = pipeline._synth_parts(text, None, True, project, tmp_path / "s.wav", False)
    natural = pipeline._natural_rate([parts])
    for speed in (0.95, 1.15):                                # the second is the video being squeezed to fit its length
        _, cues = pipeline._assemble(parts, tmp_path / f"s{speed}.wav", natural, speed, 0.95, False)
        rates[speed] = [ln / delivery.syllables(t) for t, st, ln, emo in cues]
    assert rates[0.95][1] > rates[0.95][0] * 1.05             # the question is the slowest thing in the scene
    assert rates[1.15][1] == pytest.approx(rates[0.95][1], rel=0.06)   # and does not speed up with the rest
    assert rates[1.15][0] < rates[0.95][0]                    # while the rest does


def test_trim_silence_and_syllables(tmp_path):
    import math
    p = tmp_path / "a.wav"
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 12000 + b"".join(int(3000 * math.sin(i / 10)).to_bytes(2, "little", signed=True) for i in range(24000)) + b"\x00\x00" * 24000)
    voiced = delivery.trim_silence(p)
    assert voiced == pytest.approx(1.0, abs=0.02)
    with wave.open(str(p)) as w:
        assert w.getnframes() / 24000 == pytest.approx(1.08, abs=0.03)   # 1 s of speech plus two 40 ms margins
    assert delivery.syllables("The Arctic tern flies") == 5
    assert delivery.syllables("1,200") >= 4 and delivery.syllables("你好世界", cjk=True) == 4


def test_the_chosen_voice_speed_still_changes_how_fast_it_talks(tmp_path, monkeypatch):
    def steady(text, language, out_path, *, voice="auto", speed=1.0, **_):
        secs = delivery.syllables(text) * 0.25
        _wav(out_path, secs)
        return secs
    monkeypatch.setattr(pipeline.tts, "synthesize", steady)
    project = {"language": "en-us", "voice": "auto", "voice_speed": 1.0}
    text = "It sits on a branch of the old tree today and waits for the rain to come down."
    parts = pipeline._synth_parts(text, None, False, project, tmp_path / "s.wav", False)
    natural = pipeline._natural_rate([parts])
    slow, _ = pipeline._assemble(parts, tmp_path / "a.wav", natural, 0.9, 0.9, False)
    fast, _ = pipeline._assemble(parts, tmp_path / "b.wav", natural, 1.2, 1.2, False)
    assert fast < slow * 0.85
