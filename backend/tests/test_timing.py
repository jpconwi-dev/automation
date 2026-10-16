import random

from app.services import timing


def test_layout_adds_up_exactly():
    random.seed(3)
    for target, n in [(30, 4), (60, 6), (120, 10), (300, 18)]:
        for scale in (0.5, 0.8, 1.0, 1.15):
            speech = [(target / n - timing.GAP) * scale * random.uniform(0.8, 1.2) for _ in range(n)]
            tempo = timing.choose_tempo(speech, target)
            assert timing.MAX_SLOWDOWN <= tempo <= timing.MAX_SPEEDUP
            d = timing.layout([s / tempo for s in speech], target)
            assert abs(sum(d) - target) < 1e-6
            assert min(d) >= timing.MIN_SCENE


def test_tempo_speeds_up_long_narration_and_leaves_fitting_alone():
    assert timing.choose_tempo([20] * 6, 60) > 1.0          # 120 s of speech into 60 s
    assert timing.choose_tempo([9.4] * 6, 60) == 1.0        # already fits
