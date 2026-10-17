from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    storage_dir: Path = Path("storage")
    max_body_bytes: int = 65536

    # Optional shared password for the whole API. Strongly recommended once the backend is on a public URL:
    # without it anyone who finds the URL can spend your free API quotas.
    access_key: str | None = None

    # Language model: "api" (any OpenAI-compatible endpoint, e.g. Groq's free tier), "ollama" (local),
    # or "hf_api" (Hugging Face Inference Providers: free accounts have NO included credits since 2026, see DEPLOY.md)
    llm_backend: str = "ollama"
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_api_key: str | None = None
    llm_model: str = "llama-3.3-70b-versatile"
    llm_fallback_models: str = "qwen/qwen3-32b"  # comma separated; tried in order when a model is rate-limited
    llm_timeout: int = 120
    llm_max_tokens: int = 3500
    hf_token: str | None = None
    hf_base_url: str = "https://router.huggingface.co"
    hf_llm_model: str = "Qwen/Qwen2.5-7B-Instruct"
    hf_image_model: str = "black-forest-labs/FLUX.1-schnell"
    hf_timeout: int = 180

    # Ollama (local only)
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    ollama_timeout: int = 600

    # Cloudflare Workers AI (free: 10,000 Neurons/day, no card). Used by IMAGE_BACKEND=cloudflare and TTS_BACKEND=cloudflare.
    cloudflare_account_id: str | None = None
    cloudflare_api_token: str | None = None
    cloudflare_base_url: str = "https://api.cloudflare.com/client/v4"
    cloudflare_image_model: str = "@cf/black-forest-labs/flux-1-schnell"  # FLUX.1 [schnell], Apache-2.0 weights
    cloudflare_tts_model: str = "@cf/myshell-ai/melotts"  # MeloTTS, MIT
    cloudflare_steps: int = 4
    cloudflare_timeout: int = 120

    # Optional AI text-to-video for a few scenes (the rest stay images with motion). No free text-to-video API exists:
    # "hf_space" uses the free daily ZeroGPU quota of a public Hugging Face Space, "hf_inference" needs paid HF credits.
    video_backend: str = "none"  # none | hf_space | hf_inference
    video_max_clips: int = 2  # AI clips per video, spread evenly; keeps within the small free GPU quota
    video_timeout: int = 600
    hf_video_space: str = ""  # e.g. "owner/space-name"; copy the endpoint from the Space's "Use via API" page
    hf_video_api_name: str = "/generate"
    # JSON arguments for the Space endpoint. Placeholders: {prompt} {seed} {width} {height} {duration}
    hf_video_params: str = '{"prompt": "{prompt}"}'
    hf_video_model: str = "Lightricks/LTX-Video"  # for hf_inference
    hf_video_provider: str = "auto"

    # Images: "diffusers" (AI model) or "fallback" (local placeholder graphics, labeled as such)
    image_backend: str = "fallback"
    image_model: str = "black-forest-labs/FLUX.1-schnell"
    image_steps: int = 4
    image_guidance: float = 0.0
    image_max_side: int = 768

    # Stock footage (IMAGE_BACKEND=pexels): free API key from https://www.pexels.com/api/
    pexels_api_key: str | None = None
    pexels_base_url: str = "https://api.pexels.com"
    # Stock footage (IMAGE_BACKEND=pixabay): free key shown at https://pixabay.com/api/docs/ after you sign in
    pixabay_api_key: str | None = None
    pixabay_base_url: str = "https://pixabay.com/api"

    # Narration: "cloudflare" (free MeloTTS, no local model), "windows" (built-in Windows voices), "kokoro", or "none"
    tts_backend: str = "kokoro"
    windows_voice: str | None = None  # optional exact voice name, e.g. "Microsoft Zira Desktop"

    # Rendering
    ffmpeg_path: str = "ffmpeg"
    ken_burns: bool = True
    video_quality: str = "standard"  # "standard" = 720p (the old look) | "high" = 1080p, sharper and bigger file. Env: VIDEO_QUALITY
    ffmpeg_threads: int = 0  # 0 = FFmpeg default. Use 1 on small servers: fewer threads means much less RAM
    music_path: Path | None = None
    render_timeout: int = 3600

    # Capacity and disk budget (everything under STORAGE_DIR: projects, caches, temp files)
    storage_limit_mb: int = 800
    max_concurrent_jobs: int = 1
    max_queue: int = 4

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def projects_dir(self) -> Path:
        return self.storage_dir / "projects"

    @property
    def tmp_dir(self) -> Path:
        return self.storage_dir / "tmp"

    @property
    def cache_dir(self) -> Path:
        return self.storage_dir / "cache" / "images"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()