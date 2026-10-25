import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Style = Literal["cinematic", "whiteboard", "minimal", "illustrated"]
Aspect = Literal["16:9", "9:16", "1:1"]
Mode = Literal["explainer", "prompt"]
Tone = Literal["friendly", "professional", "energetic", "calm", "humorous", "dramatic"]
MusicStyle = Literal["auto", "calm", "upbeat", "cinematic", "playful", "epic"]
LANGUAGES = ("en-us", "en-gb", "es", "fr", "hi", "it", "ja", "pt-br", "zh")
VOICE_RE = re.compile(r"^[\w .()'\-]{1,80}$")


class ProjectCreate(BaseModel):
    mode: Mode = "explainer"
    topic: str = Field(default="", max_length=500)
    audience: str = Field(default="", max_length=200)
    prompt: str = Field(default="", max_length=2000)
    style: Style = "illustrated"
    duration: Literal[30, 60, 90, 120, 300] = 60
    language: str = "en-us"
    aspect_ratio: Aspect = "16:9"
    tone: Tone = "friendly"
    voice: str = "auto"
    voice_speed: float = Field(default=1.0, ge=0.8, le=1.3)
    background_music: bool = True
    music_style: MusicStyle = "auto"
    sound_effects: bool = True
    subject: str = Field(default="", max_length=80)  # the exact thing the footage must show, e.g. "black bear"
    # Other names for the same subject, e.g. ["snapping shrimp", "alpheidae"] for "pistol shrimp". A clip tagged with
    # ANY of these counts as the right subject, so a niche subject still finds real footage (never unrelated footage).
    subject_aliases: list[str] = Field(default_factory=list, max_length=6)

    @field_validator("subject_aliases")
    @classmethod
    def tidy_aliases(cls, v: list[str]) -> list[str]:
        out = []
        for a in v:
            a = " ".join(str(a).split())[:40]
            if a and a.lower() not in [x.lower() for x in out]:
                out.append(a)
        return out

    @field_validator("topic", "audience", "prompt", "subject")
    @classmethod
    def tidy(cls, v: str) -> str:
        return " ".join(v.split())

    @field_validator("language")
    @classmethod
    def lang_ok(cls, v: str) -> str:
        if v not in LANGUAGES:
            raise ValueError(f"unsupported language; choose one of {', '.join(LANGUAGES)}")
        return v

    @field_validator("voice")
    @classmethod
    def voice_ok(cls, v: str) -> str:
        if not VOICE_RE.match(v):
            raise ValueError("invalid voice id")
        return v

    @model_validator(mode="after")
    def needs(self):
        if self.mode == "explainer":
            if len(self.topic) < 3:
                raise ValueError("topic must be at least 3 characters")
            if len(self.audience) < 2:
                raise ValueError("audience must be at least 2 characters")
        elif len(self.prompt) < 10:
            raise ValueError("prompt must be at least 10 characters")
        else:  # prompt mode: keep topic/audience non-empty for display and stock-footage search
            self.topic = self.topic or self.prompt[:500]
            self.audience = self.audience or "a general audience"
        return self


class Scene(BaseModel):
    narration: str = Field(min_length=5, max_length=900)
    visual_description: str = Field(min_length=5, max_length=500)
    on_screen_text: str = Field(max_length=120)
    subtitle_text: str = Field(min_length=3, max_length=900)
    duration_seconds: float = Field(ge=3, le=90)
    search_keywords: str = Field(default="", max_length=80)
    # How the narrator should sound saying this scene (pace and pauses are chosen from it). Unknown values become neutral.
    emotion: str = Field(default="neutral", max_length=20)

    @field_validator("emotion", mode="before")
    @classmethod
    def tidy_emotion(cls, v):
        v = str(v or "neutral").strip().lower()
        return v if v in ("neutral", "curious", "excited", "awe", "serious", "playful", "question") else "neutral"


class Script(BaseModel):
    title: str = Field(min_length=2, max_length=140)
    description: str = Field(min_length=5, max_length=600)
    hook: str = Field(min_length=3, max_length=400)
    scenes: list[Scene] = Field(min_length=1, max_length=30)
    closing_summary: str = Field(min_length=3, max_length=600)


class ProjectOut(ProjectCreate):
    id: str
    status: str
    script: Script | None = None
    video_ready: bool = False
    silent: bool = False
    latest_job_id: str | None = None
    video_seconds: float | None = None


class JobOut(BaseModel):
    id: str
    project_id: str
    status: Literal["queued", "running", "completed", "failed", "cancelled"]
    stage: Literal["queued", "script", "images", "narration", "audio", "rendering", "completed"]
    progress: int
    message: str = ""
    error: str | None = None
    scenes_total: int = 0
    images_done: int = 0
    cancel_requested: bool = False