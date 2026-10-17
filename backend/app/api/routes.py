import importlib.util
import re
import subprocess
import uuid
from functools import lru_cache
from pathlib import Path

import httpx
from typing import Annotated

from fastapi import APIRouter, Path as P, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ..core import db, storage
from ..core.config import settings
from ..core.errors import ApiError, PipelineError
from ..schemas.schemas import LANGUAGES, VOICE_RE, JobOut, ProjectCreate, ProjectOut, Tone
from ..services import cloudflare, video_ai, voices
from ..workers import pipeline

router = APIRouter(prefix="/api")
Id = Annotated[str, P(pattern=r"^[0-9a-f]{32}$")]
AssetName = Annotated[str, P(max_length=40)]
ASSET_RE = re.compile(r"^(scene_\d{2}\.(png|wav)|subtitles\.srt|video\.mp4)$")


@lru_cache
def _ffmpeg_caps() -> dict:
    def out(*a):
        try:
            return subprocess.run([settings.ffmpeg_path, "-hide_banner", *a], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return ""
    enc, fil = out("-encoders"), out("-filters")
    return {"found": bool(enc), "libx264": " libx264 " in enc, "aac": " aac " in enc,
            "subtitles": " subtitles " in fil, "xfade": " xfade " in fil}


@router.get("/health")
def health():
    deps = {}
    caps = _ffmpeg_caps()
    need = ["libx264", "aac", "subtitles", "xfade"]
    missing = [k for k in need if not caps.get(k)]
    deps["ffmpeg"] = {"ok": caps["found"] and not missing,
                      "detail": "ready" if caps["found"] and not missing else
                      "not installed" if not caps["found"] else f"build lacks: {', '.join(missing)}"}
    if settings.llm_backend == "api":
        deps["llm"] = {"ok": bool(settings.llm_api_key),
                       "detail": f"{settings.llm_model} (+ fallbacks) via {settings.llm_base_url}" if settings.llm_api_key
                       else "LLM_API_KEY missing (free key at console.groq.com)"}
    elif settings.llm_backend == "hf_api":
        deps["llm"] = {"ok": bool(settings.hf_token), "detail": f"Hugging Face API: {settings.hf_llm_model} (needs paid HF credits)" if settings.hf_token else "HF_TOKEN missing in backend/.env"}
    else:
      try:
        names = [m["name"] for m in httpx.get(f"{settings.ollama_url}/api/tags", timeout=2).json().get("models", [])]
        have = any(n == settings.ollama_model or n.split(":")[0] == settings.ollama_model for n in names)
        deps["ollama"] = {"ok": have, "detail": f"model {settings.ollama_model} " + ("installed" if have else f"missing: run `ollama pull {settings.ollama_model}`")}
      except (httpx.HTTPError, ValueError):
        deps["ollama"] = {"ok": False, "detail": f"not reachable at {settings.ollama_url}"}
    if settings.tts_backend == "none":
        deps["tts"] = {"ok": True, "detail": "disabled (silent videos)"}
    elif settings.tts_backend == "windows":
        import os
        deps["tts"] = {"ok": os.name == "nt", "detail": "Windows built-in voices" if os.name == "nt" else "TTS_BACKEND=windows needs Windows"}
    elif settings.tts_backend == "cloudflare":
        ok = cloudflare.configured()
        deps["tts"] = {"ok": ok, "detail": f"MeloTTS on Cloudflare Workers AI (free): {settings.cloudflare_tts_model}" if ok
                       else "CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN missing (free at dash.cloudflare.com)"}
    else:
        ok = importlib.util.find_spec("kokoro") is not None
        deps["tts"] = {"ok": ok, "detail": "kokoro ready" if ok else "kokoro not installed (requirements-ai.txt)"}
    if settings.image_backend == "hf_api":
        deps["images"] = {"ok": bool(settings.hf_token), "detail": f"Hugging Face API: {settings.hf_image_model}" if settings.hf_token else "HF_TOKEN missing in backend/.env"}
    elif settings.image_backend == "cloudflare":
        ok = cloudflare.configured()
        deps["images"] = {"ok": ok, "detail": f"Cloudflare Workers AI (free): {settings.cloudflare_image_model}" if ok
                          else "CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN missing (free at dash.cloudflare.com)"}
    elif settings.image_backend == "pixabay":
        deps["images"] = {"ok": bool(settings.pixabay_api_key), "detail": "Pixabay stock video" if settings.pixabay_api_key else "PIXABAY_API_KEY missing in backend/.env (free: pixabay.com/api/docs)"}
    elif settings.image_backend == "pexels":
        deps["images"] = {"ok": bool(settings.pexels_api_key), "detail": "Pexels stock video" if settings.pexels_api_key else "PEXELS_API_KEY missing in backend/.env (free at pexels.com/api)"}
    elif settings.image_backend == "stock" or settings.image_backend in ("nasa", "wikimedia", "archive", "smithsonian"):
        from ..services import stock
        names = stock.active_providers()
        bad = [n for n in names if n not in stock.PROVIDER_NAMES]
        keyless = [n for n in names if n in stock._KEYED and not stock._KEYED[n]()]
        ready = [n for n in names if n not in bad and n not in keyless]
        detail = "stock footage from " + (", ".join(ready) or "nothing")
        if keyless:
            detail += f" (no key, skipped: {', '.join(keyless)})"
        if bad:
            detail += f" (unknown: {', '.join(bad)})"
        if "smithsonian" in ready and not settings.smithsonian_api_key:
            detail += " (smithsonian uses DEMO_KEY: get a free key at api.data.gov/signup)"
        deps["images"] = {"ok": bool(ready) and not bad, "detail": detail}
    elif settings.image_backend == "diffusers":
        ok = importlib.util.find_spec("diffusers") is not None
        deps["images"] = {"ok": ok, "detail": f"diffusers: {settings.image_model}" if ok else "diffusers not installed (requirements-ai.txt)"}
    else:
        deps["images"] = {"ok": True, "detail": "labeled placeholder graphics (not AI-generated)"}
    if video_ai.enabled():
        if settings.video_backend == "hf_space":
            ok = bool(settings.hf_video_space)
            detail = f"AI video clips (max {settings.video_max_clips}) via Space {settings.hf_video_space}" if ok else "HF_VIDEO_SPACE missing"
        else:
            ok = bool(settings.hf_token)
            detail = f"AI video clips (max {settings.video_max_clips}) via {settings.hf_video_model}" if ok else "HF_TOKEN missing"
        deps["video"] = {"ok": ok, "detail": detail}
    elif settings.video_backend != "none":
        deps["video"] = {"ok": False, "detail": f"Unknown VIDEO_BACKEND '{settings.video_backend}'. Use none, hf_space or hf_inference."}
    used, cap = storage.usage_bytes() / storage.MB, settings.storage_limit_mb
    deps["storage"] = {"ok": used <= cap, "detail": f"{used:.0f} MB of {cap} MB used (oldest videos are removed automatically)"}
    return {"status": "ok" if all(d["ok"] for d in deps.values()) else "degraded", "dependencies": deps,
            "auth_required": bool(settings.access_key)}


def _project(pid: str) -> dict:
    p = db.get_project(pid)
    if not p:
        raise ApiError(404, "project_not_found", "Project not found.")
    return p


@router.post("/projects", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate):
    p = {**body.model_dump(), "id": uuid.uuid4().hex, "status": "created", "script": None, "video_ready": False,
         "latest_job_id": None, "assets_meta": {}, "silent": False, "video_seconds": None}
    db.save_project(p)
    return p


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: Id):
    return _project(project_id)


@router.post("/projects/{project_id}/generate", response_model=JobOut, status_code=202)
def generate(project_id: Id):
    p = _project(project_id)
    active = db.active_jobs()
    if any(j["project_id"] == project_id for j in active):
        raise ApiError(409, "already_running", "This project is already generating.")
    if len(active) >= settings.max_queue + settings.max_concurrent_jobs:
        raise ApiError(429, "queue_full", "The server is busy. Try again in a few minutes.")
    if not storage.enforce({project_id}, storage.reserve_bytes(p["duration"])):
        raise ApiError(507, "storage_full", f"Not enough space inside the {settings.storage_limit_mb} MB storage limit. "
                       "Raise STORAGE_LIMIT_MB in backend/.env or free disk space.")
    job = {"id": uuid.uuid4().hex, "project_id": project_id, "status": "queued", "stage": "queued", "progress": 0,
           "message": "Waiting for a free worker", "error": None, "scenes_total": 0, "images_done": 0,
           "cancel_requested": False}
    db.save_job(job)
    p.update(latest_job_id=job["id"], status="queued")
    db.save_project(p)
    pipeline.submit(job["id"], project_id)
    return job


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: Id):
    j = db.get_job(job_id)
    if not j:
        raise ApiError(404, "job_not_found", "Job not found.")
    return j


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel(job_id: Id):
    j = get_job(job_id)
    if j["status"] in ("queued", "running"):
        pipeline.request_cancel(job_id)
        j = db.get_job(job_id)
    return j


@router.get("/projects/{project_id}/assets")
def list_assets(project_id: Id):
    p = _project(project_id)
    d = settings.projects_dir / project_id
    meta = p.get("assets_meta", {})
    files = sorted(f for f in d.iterdir() if ASSET_RE.match(f.name)) if d.is_dir() else []
    return {"assets": [{"name": f.name, "size": f.stat().st_size, "url": f"/api/projects/{project_id}/assets/{f.name}",
                        **meta.get(f.name, {})} for f in files]}


@router.get("/projects/{project_id}/assets/{name}")
def get_asset(project_id: Id, name: AssetName):
    _project(project_id)
    path = settings.projects_dir / project_id / name
    if not ASSET_RE.match(name) or not path.is_file():
        raise ApiError(404, "asset_not_found", "Asset not found.")
    return FileResponse(path)


@router.get("/projects/{project_id}/video")
def get_video(project_id: Id, download: bool = False):
    p = _project(project_id)
    path = settings.projects_dir / project_id / "video.mp4"
    if not p.get("video_ready") or not path.is_file():
        raise ApiError(404, "video_not_ready", "The video hasn't been generated yet.")
    return FileResponse(path, media_type="video/mp4", filename=f"explainer-{project_id[:8]}.mp4",
                        content_disposition_type="attachment" if download else "inline")


@router.get("/voices")
def list_voices(language: str = Query("en-us")):
    if language not in LANGUAGES:
        raise ApiError(422, "validation_error", f"Unsupported language. Choose one of {', '.join(LANGUAGES)}.")
    return voices.list_voices(language)


@router.get("/voices/preview")
def voice_preview(language: str = Query("en-us"), voice: str = Query("auto", max_length=80),
                  tone: Tone = Query("friendly"), speed: float = Query(1.0, ge=0.8, le=1.3)):
    if language not in LANGUAGES:
        raise ApiError(422, "validation_error", f"Unsupported language. Choose one of {', '.join(LANGUAGES)}.")
    if not VOICE_RE.match(voice):
        raise ApiError(422, "validation_error", "Invalid voice id.")
    try:
        path = voices.make_preview(language, voice, tone, speed)
    except PipelineError as e:
        raise ApiError(400, "preview_failed", str(e))
    return FileResponse(path, media_type="audio/wav", background=BackgroundTask(lambda: path.unlink(missing_ok=True)))
