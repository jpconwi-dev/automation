"""Optional AI text-to-video for a few scenes (VIDEO_BACKEND=hf_space or hf_inference).

Reality check, as of October 2026: there is no free text-to-video API. The realistic free route is the daily
ZeroGPU quota of a public Hugging Face Space (a few clips a day), so only VIDEO_MAX_CLIPS scenes per video get an
AI clip (spread evenly, always including the opening scene). Every other scene stays a still image with motion.
If the AI backend fails or its quota runs out, the pipeline keeps going with images and says so in the job message;
it never silently switches to a paid service.

The clip is written exactly where the stock-footage path writes its clips (scene_NN_clip.mp4 + caption layer), so the
FFmpeg renderer needs no changes.
"""
import inspect
import json
import shutil
import uuid
from pathlib import Path

from ..core.config import settings
from ..core.errors import PipelineError
from .images import STYLE_PROMPTS, _text_layer
from .render import DIMS
from .stock import _shrink

VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv")
CLIP_SIZE = {"16:9": (768, 448), "9:16": (448, 768), "1:1": (512, 512)}  # multiples of 32 for the video models


def enabled() -> bool:
    return settings.video_backend in ("hf_space", "hf_inference")


def pick_scenes(n: int, k: int) -> set[int]:
    """1-based scene numbers that get an AI clip: spread evenly, always including scene 1 (the hook)."""
    k = max(0, min(k, n))
    if k == 0:
        return set()
    if k == 1:
        return {1}
    return {1 + round(i * (n - 1) / (k - 1)) for i in range(k)}


def _find_video(obj) -> str | None:
    """Gradio returns a path, a dict, or a tuple/list holding one. Find the first video file path."""
    if isinstance(obj, str):
        return obj if obj.lower().endswith(VIDEO_EXT) else None
    if isinstance(obj, dict):
        for key in ("video", "path", "value", "url"):
            if key in obj and (found := _find_video(obj[key])):
                return found
        obj = list(obj.values())
    if isinstance(obj, (list, tuple)):
        for item in obj:
            if found := _find_video(item):
                return found
    return None


def space_kwargs(prompt: str, seed: int, width: int, height: int, duration: int = 4) -> dict:
    """Fill the HF_VIDEO_PARAMS JSON template. A value that is exactly '{seed}' etc. keeps its number type."""
    try:
        template = json.loads(settings.hf_video_params)
    except ValueError:
        raise PipelineError("HF_VIDEO_PARAMS isn't valid JSON. Example: {\"prompt\": \"{prompt}\", \"seed\": \"{seed}\"}")
    subs = {"{prompt}": prompt, "{seed}": seed, "{width}": width, "{height}": height, "{duration}": duration}

    def fill(v):
        if isinstance(v, str):
            if v in subs:
                return subs[v]
            for k, x in subs.items():
                v = v.replace(k, str(x))
            return v
        if isinstance(v, dict):
            return {a: fill(b) for a, b in v.items()}
        if isinstance(v, list):
            return [fill(x) for x in v]
        return v

    out = fill(template)
    if not isinstance(out, dict):
        raise PipelineError("HF_VIDEO_PARAMS must be a JSON object of argument names to values.")
    return out


def _friendly(e: Exception, what: str) -> PipelineError:
    msg = str(e)
    low = msg.lower()
    if "quota" in low or "402" in low or "credit" in low or "payment" in low:
        return PipelineError(f"{what}: the free GPU quota or credits are used up for now. {msg[:160]}")
    if "401" in low or "403" in low or "unauthorized" in low or "token" in low:
        return PipelineError(f"{what}: Hugging Face rejected the request. Check HF_TOKEN. {msg[:160]}")
    return PipelineError(f"{what} failed: {msg[:240]}")


def _from_space(prompt: str, seed: int, size: tuple[int, int], dest: Path) -> None:
    if not settings.hf_video_space:
        raise PipelineError("VIDEO_BACKEND=hf_space needs HF_VIDEO_SPACE (a Space's 'owner/name'). See DEPLOY.md.")
    try:
        from gradio_client import Client
    except ImportError:
        raise PipelineError("gradio_client isn't installed. Run `pip install -r requirements-video.txt`.")
    kw = {}
    if settings.hf_token:  # signed-in users get a bigger daily ZeroGPU quota than anonymous callers
        kw["token" if "token" in inspect.signature(Client.__init__).parameters else "hf_token"] = settings.hf_token
    try:
        client = Client(settings.hf_video_space, **kw)
        job = client.submit(api_name=settings.hf_video_api_name, **space_kwargs(prompt, seed, size[0], size[1]))
        result = job.result(timeout=settings.video_timeout)
    except PipelineError:
        raise
    except Exception as e:  # gradio_client raises many different types
        raise _friendly(e, "The Hugging Face Space")
    path = _find_video(result)
    if not path or not Path(path).is_file():
        raise PipelineError("The Space finished but returned no video file. Check HF_VIDEO_API_NAME and HF_VIDEO_PARAMS "
                            "against the Space's 'Use via API' page.")
    shutil.copyfile(path, dest)


def _from_inference(prompt: str, dest: Path) -> None:
    if not settings.hf_token:
        raise PipelineError("VIDEO_BACKEND=hf_inference needs HF_TOKEN, and credits on your Hugging Face account.")
    try:
        from huggingface_hub import InferenceClient
    except ImportError:
        raise PipelineError("huggingface_hub isn't installed. Run `pip install -r requirements-video.txt`.")
    try:
        client = InferenceClient(provider=settings.hf_video_provider, api_key=settings.hf_token,
                                 timeout=settings.video_timeout)
        data = client.text_to_video(prompt, model=settings.hf_video_model)
    except Exception as e:
        raise _friendly(e, "Hugging Face text-to-video")
    if isinstance(data, (bytes, bytearray)) and data:
        dest.write_bytes(bytes(data))
    elif isinstance(data, (str, Path)) and Path(data).is_file():
        shutil.copyfile(data, dest)
    else:
        raise PipelineError("Hugging Face returned no video data. Try another HF_VIDEO_MODEL or provider.")


def make_clip(project: dict, script, scene, index: int, pdir: Path) -> dict:
    """Create scene_NN_clip.mp4, scene_NN.png (thumbnail) and scene_NN_text.png. Raises PipelineError on any failure,
    leaving no partial files behind."""
    import subprocess

    n = f"scene_{index:02d}"
    aspect = project["aspect_ratio"]
    W, H = DIMS[aspect]
    tmp = settings.tmp_dir / f"ai_{uuid.uuid4().hex}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        prompt = (f"{STYLE_PROMPTS[project['style']]}. {scene.visual_description}. Smooth natural camera motion. "
                  "No text, no letters, no watermark.")
        seed = int(project["id"][:8], 16) % 2147483000 + index
        raw = tmp / "raw.mp4"
        if settings.video_backend == "hf_space":
            _from_space(prompt, seed, CLIP_SIZE[aspect], raw)
        else:
            _from_inference(prompt, raw)
        clip = tmp / "clip.mp4"
        _shrink(raw, clip, aspect)  # 720p-class H.264, at most 16 s, no audio
        thumb = tmp / "thumb.png"
        r = subprocess.run([settings.ffmpeg_path, "-y", "-v", "error", "-i", str(clip), "-frames:v", "1",
                            "-vf", "scale=640:-2", str(thumb)], capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not thumb.is_file():
            raise PipelineError(f"Couldn't read the AI clip: {r.stderr[-200:]}")
        _text_layer((W, H), scene.on_screen_text, None).save(tmp / "text.png")
        shutil.move(str(clip), pdir / f"{n}_clip.mp4")
        shutil.move(str(thumb), pdir / f"{n}.png")
        shutil.move(str(tmp / "text.png"), pdir / f"{n}_text.png")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    model = settings.hf_video_space if settings.video_backend == "hf_space" else settings.hf_video_model
    return {"ai_generated": True, "kind": "video", "model": model}
