import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

from ..core import db, storage
from ..core.config import settings
from ..core.errors import Cancelled, PipelineError
from ..services import images, render, soundtrack, tts, video_ai, voices
from ..services.script import WORDS_PER_SECOND, generate_script

log = logging.getLogger("pipeline")
_executor = ThreadPoolExecutor(max_workers=settings.max_concurrent_jobs, thread_name_prefix="job")
_cancels: dict[str, threading.Event] = {}
FPS = 24
GAP = 0.45  # breathing room after each scene's narration, in seconds
CLOSING = 1.0  # extra hold on the last scene (room for the closing chime)


def submit(job_id: str, project_id: str) -> None:
    _cancels[job_id] = threading.Event()
    _executor.submit(_run, job_id, project_id)


def request_cancel(job_id: str) -> None:
    if job_id in _cancels:
        _cancels[job_id].set()
    db.update_job(job_id, cancel_requested=True)


def plan_durations(speech: list[float], duration: int) -> list[float]:
    """Scene lengths that add up to exactly `duration` seconds (frame-aligned). Short scripts get longer holds;
    if narration is too long for the slot, the scene is shortened proportionally (the voice is sped up before this)."""
    base = [s + GAP for s in speech]
    base[-1] += CLOSING
    scale = duration / sum(base)
    d = [round(b * scale * FPS) / FPS for b in base]
    d[-1] = round((duration - sum(d[:-1])) * FPS) / FPS
    return d


def _run(job_id: str, project_id: str) -> None:
    cancel = _cancels[job_id]
    pdir, tmp = settings.projects_dir / project_id, settings.tmp_dir / job_id
    protect = {project_id, job_id}

    def check():
        if cancel.is_set():
            raise Cancelled()

    def upd(**kw):
        db.update_job(job_id, **kw)

    try:
        check()
        project = db.get_project(project_id)
        for d in (pdir, tmp):
            d.mkdir(parents=True, exist_ok=True)
        if not storage.enforce(protect, storage.reserve_bytes(project["duration"])):
            raise PipelineError(f"Not enough free space inside the {settings.storage_limit_mb} MB storage limit. "
                                "Raise STORAGE_LIMIT_MB in backend/.env or free disk space.")
        project.update(status="generating", video_ready=False)
        db.save_project(project)

        # --- script ---
        upd(status="running", stage="script", progress=2, message="Writing the script")
        script = generate_script(project, check)
        project["script"] = script.model_dump()
        db.save_project(project)
        for old in pdir.glob("scene_*"):
            old.unlink(missing_ok=True)
        for old in (pdir / "video.mp4", pdir / "subtitles.srt"):
            old.unlink(missing_ok=True)
        n = len(script.scenes)
        upd(scenes_total=n, images_done=0)

        # --- visuals ---
        meta = {}
        upd(stage="images", progress=10)
        ai_scenes = video_ai.pick_scenes(n, settings.video_max_clips) if video_ai.enabled() else set()
        ai_error = None  # once the AI video backend fails (usually: free quota used up) the rest use images
        for i, sc in enumerate(script.scenes, 1):
            check()
            upd(message=f"Finding visuals for scene {i} of {n}", progress=10 + int(40 * (i - 1) / n))
            scene_meta = None
            if i in ai_scenes and ai_error is None:
                upd(message=f"Generating an AI video clip for scene {i} of {n} (this can take a few minutes)")
                try:
                    scene_meta = video_ai.make_clip(project, script, sc, i, pdir)
                except PipelineError as e:
                    ai_error = str(e)
                    log.warning("AI video failed on scene %s: %s", i, ai_error)
                    upd(message=f"AI video unavailable, continuing with images. {ai_error[:160]}")
            if scene_meta is None:
                scene_meta = images.make_scene_image(project, script, sc, i, pdir)
                if i in ai_scenes:
                    scene_meta = {**scene_meta, "note": f"AI video unavailable; image used instead. {ai_error or ''}".strip()}
            meta[f"scene_{i:02d}.png"] = scene_meta
            storage.guard(protect)
            upd(images_done=i, progress=10 + int(40 * i / n))

        # --- narration (exactly as long as the chosen duration) ---
        upd(stage="narration", progress=50)
        silent = settings.tts_backend == "none"
        if not silent and settings.tts_backend not in ("kokoro", "windows", "cloudflare"):
            raise PipelineError(f"Unknown TTS_BACKEND '{settings.tts_backend}'. Use 'cloudflare', 'windows', 'kokoro' or 'none'.")
        target = project["duration"]
        speed = voices.effective_speed(project["tone"], project["voice_speed"])
        speech: list[float] = []
        for attempt in range(4):
            speech = []
            for i, sc in enumerate(script.scenes, 1):
                check()
                upd(message=(f"Creating narration {i} of {n}" if not silent else f"Timing scene {i} of {n} (silent video)")
                    + (" (speeding up to fit)" if attempt else ""), progress=50 + int(15 * (i - 1) / n))
                wav = pdir / f"scene_{i:02d}.wav"
                if silent:
                    s = max(len(sc.narration.split()) / WORDS_PER_SECOND, 1.0)
                    tts.write_silence(wav, s)
                else:
                    s = tts.synthesize(sc.narration, project["language"], wav, voice=project["voice"], speed=speed)
                speech.append(s)
            need = sum(speech) + GAP * n + CLOSING
            if silent or need <= target or speed >= 1.6:
                break
            speed = min(1.6, speed * max(1.08, need / target))  # too long for the slot: speak faster and redo
        durations = plan_durations(speech, target)
        for i, d in enumerate(durations, 1):
            tts.pad_wav(pdir / f"scene_{i:02d}.wav", d)  # pads with silence (or trims) to the exact scene length
        storage.guard(protect)

        # --- music and effects ---
        upd(stage="audio", progress=65, message="Composing music and sound effects")
        music = sfx = None
        if project["background_music"]:
            check()
            if settings.music_path and settings.music_path.is_file():
                music = settings.music_path
            else:
                style = soundtrack.resolve_style(project["music_style"], project["tone"])
                music = soundtrack.make_music(style, tmp / "music_loop.wav")
        if project["sound_effects"]:
            check()
            sfx = soundtrack.make_sfx(tmp)

        # --- render ---
        check()
        upd(stage="rendering", progress=72, message="Rendering video with FFmpeg")
        out = render.render_video(project, script.scenes, durations, speech, pdir, tmp, check,
                                  lambda f: upd(progress=72 + int(27 * f)), music=music, sfx=sfx)
        if storage.usage_bytes() + out.stat().st_size > storage.limit_bytes():
            storage.enforce(protect, reserve=out.stat().st_size)
        shutil.move(str(out), pdir / "video.mp4")

        project.update(status="completed", video_ready=True, assets_meta=meta, silent=silent,
                       video_seconds=float(target))
        db.save_project(project)
        upd(status="completed", stage="completed", progress=100, message="Your video is ready")
    except Cancelled:
        _finish(project_id, job_id, "cancelled", None)
    except PipelineError as e:
        _finish(project_id, job_id, "failed", str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("job %s crashed", job_id)
        _finish(project_id, job_id, "failed", f"Unexpected error: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        storage.cleanup_intermediates(pdir)  # clips and animation layers are only needed while rendering
        _cancels.pop(job_id, None)
        try:
            storage.enforce({project_id})  # trim caches/oldest videos if we ended over budget
        except Exception:  # noqa: BLE001
            log.exception("storage trim failed")


def _finish(project_id: str, job_id: str, status: str, error: str | None) -> None:
    db.update_job(job_id, status=status, error=error, message="Cancelled" if status == "cancelled" else "")
    p = db.get_project(project_id)
    if p:
        p["status"] = status
        db.save_project(p)
