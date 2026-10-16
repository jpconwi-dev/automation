import pytest
from pydantic import ValidationError

from app.schemas.schemas import ProjectCreate


def test_explainer_needs_topic_and_audience():
    ProjectCreate(topic="How rain forms", audience="kids")
    with pytest.raises(ValidationError):
        ProjectCreate(topic="hi", audience="kids")


def test_prompt_mode_fills_topic_and_audience():
    p = ProjectCreate(mode="prompt", prompt="A cheerful ad for a bakery that opens at dawn")
    assert p.topic.startswith("A cheerful ad") and p.audience == "a general audience"
    with pytest.raises(ValidationError):
        ProjectCreate(mode="prompt", prompt="short")


def test_new_defaults_and_limits():
    p = ProjectCreate(topic="Volcanoes", audience="students", duration=300)
    assert p.background_music and p.sound_effects and p.tone == "friendly" and p.voice == "auto"
    with pytest.raises(ValidationError):
        ProjectCreate(topic="Volcanoes", audience="students", voice_speed=3)
    with pytest.raises(ValidationError):
        ProjectCreate(topic="Volcanoes", audience="students", voice="x; rm -rf /")
    ProjectCreate(topic="Volcanoes", audience="students", voice="Microsoft Zira Desktop")
