# Clickcast: AI explainer video generator

> **Deploying to Render + Vercel with free models? Read [DEPLOY.md](DEPLOY.md).** It covers the free script (Groq),
> image + voice (Cloudflare Workers AI) and optional AI-video backends, the password gate, and the real limits.
> New backend values: `LLM_BACKEND=api`, `IMAGE_BACKEND=cloudflare`, `TTS_BACKEND=cloudflare`, `VIDEO_BACKEND=hf_space|hf_inference`.
> The Hugging Face Inference API (`hf_api`) now needs paid credits for free accounts.

React + Vite + TypeScript + Tailwind frontend, FastAPI backend. Everything runs on your own machine:
Ollama writes the script, Kokoro narrates, Diffusers illustrates (optional), FFmpeg renders the MP4.
No paid AI APIs are called, and nothing silently falls back to one.

## What was tested
Backend tests (`cd backend && python -m pytest`) run the **real job pipeline and real FFmpeg** (xfade transitions, Ken Burns,
burned-in subtitles, H.264 + AAC MP4) against a small mock Ollama server, with `TTS_BACKEND=none` and placeholder images.
The frontend type-checks and builds. **Not tested here (no GPU, 4 GB RAM, no Ollama):** real Qwen output, Kokoro narration
and Diffusers image generation. Those code paths are written against the libraries' documented APIs, so expect to fix small
issues on first run, and tell me what you see.

## Quick start
```bash
./scripts/setup.sh          # add --ai to also install Kokoro + Diffusers (install PyTorch first)
ollama pull qwen2.5:7b-instruct
./scripts/dev.sh            # backend :8000, frontend :5173
```
Requirements: Python 3.11+, Node 20+, FFmpeg with libx264, AAC, libass (`subtitles`) and `xfade`, Ollama, and `espeak-ng` for non-English Kokoro voices.
The page shows a banner listing anything missing (it reads `GET /api/health`).

## Modes (backend/.env)
| Setting | Options | Notes |
|---|---|---|
| `TTS_BACKEND` | `windows`, `kokoro`, `none` | `windows` uses voices built into Windows (no download, robotic; add languages in Settings > Speech). `none` makes silent videos. |
| `IMAGE_BACKEND` | `pixabay`, `pexels`, `hf_api`, `diffusers`, `fallback` | `pexels` plays real stock video per scene (free key, `PEXELS_API_KEY`); scenes with no match get a labeled placeholder. Check Pexels' license terms before publishing. |
| (details) | |  `fallback` draws animated local shapes (drifting layers, caption reveal) stamped "Placeholder graphic - not AI-generated". |
| `OLLAMA_MODEL` | any Ollama model | Use `qwen2.5:1.5b-instruct` on small machines (quality drops). |
| `IMAGE_MODEL` | any Diffusers text-to-image repo | Tune `IMAGE_STEPS`/`IMAGE_GUIDANCE` for the model (FLUX.1-schnell: 4 steps, guidance 0). |

Missing models give an error with the fix (for example `ollama pull ...`). They never switch to a paid service.
Without a GPU, image generation with large models is very slow; use `fallback` or a small model.

## API
| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | App and dependency status |
| POST | `/api/projects` | Create project (topic ≤ 500 chars, audience, style, duration 30/60/120/300, language, aspect ratio) |
| POST | `/api/projects/{id}/generate` | Start a job (202). 409 if already running, 429 if the queue is full |
| GET | `/api/jobs/{id}` | Status, stage, percent, message, error |
| POST | `/api/jobs/{id}/cancel` | Request cancellation (checked between steps; FFmpeg is killed mid-render) |
| GET | `/api/projects/{id}` | Project plus validated script |
| GET | `/api/projects/{id}/video` | MP4 (`?download=true` forces download) |
| GET | `/api/projects/{id}/assets` | Lists scene images, narration WAVs, subtitles, video (with `ai_generated` flags) |
| GET | `/api/projects/{id}/assets/{name}` | One asset, allow-listed filenames only |

Errors always look like `{"error": {"code", "message", "details?"}}`. Progress comes from real stages: script (0-10%),
illustrations (10-50%), narration (50-70%), FFmpeg render (70-99%, read from FFmpeg's own progress output). The UI polls every 1.5 s.

## Security notes
CORS is limited to `CORS_ORIGINS`; request bodies over 64 KB are rejected; IDs are 32-char hex; assets are served by
allow-listed name from the project folder only; at most `MAX_CONCURRENT_JOBS` run at once with `MAX_QUEUE` waiting.
There is no authentication. Before exposing this publicly add auth and rate limiting at the proxy, keep Ollama on
localhost (the compose file publishes no Ollama port), and terminate HTTPS at a reverse proxy, for example Caddy:
```
yourdomain.com {
    handle /api/* { reverse_proxy 127.0.0.1:8000 }
    handle { root * /srv/clickcast/frontend/dist
             try_files {path} /index.html
             file_server }
}
```
Build the frontend with `npm run build`. Set `CORS_ORIGINS=https://yourdomain.com`.

## Licenses and costs: you must verify
I could not check live model cards from this environment, so the list below is from memory and **may be out of date**.
Read each model card before use, especially for commercial or public hosting:
- Qwen2.5-Instruct: sizes differ. As I recall, 7B/14B/32B and 0.5B/1.5B are Apache-2.0, while the 3B and 72B use other Qwen licenses. That is why the default is 7B.
- Kokoro-82M: believed Apache-2.0 for the weights; check voice and dataset terms on its card.
- FLUX.1-schnell: believed Apache-2.0 (FLUX.1-dev is non-commercial). Some repos need you to accept terms on Hugging Face.
- Background music is off by default and ships with no tracks. Only set `MUSIC_PATH` to music you have rights to.

Local inference avoids per-request fees. Hardware, electricity, storage, bandwidth and hosting still cost money.

## Layout
`backend/app/{api,core,schemas,services,workers}` (routes, config/db/errors, Pydantic models, script/tts/images/render, job pipeline),
`frontend/src/{components,hooks,services,types}`. The spec's `app/models/` and `frontend/src/pages/` folders exist but are empty:
SQLite stores JSON blobs and the app is a single screen.


## Storage limit (800 MB)
`STORAGE_LIMIT_MB=800` caps everything inside `backend/storage` (finished videos, narration previews, caches, temp files).
Before a job starts, the app makes room for its worst case; between steps it re-checks, and the job stops with a clear
message rather than exceed the limit. When space is needed it deletes, in order: leftover temp files, cached searches,
cached images, then the oldest finished videos (those projects show as expired). Stock clips are trimmed to at most 16 s
at about 720p and deleted after rendering, and the final MP4 is bitrate-capped (a 5-minute video is roughly 90 MB).
The limit does **not** cover things outside `backend/storage`: `node_modules`, `.venv`, FFmpeg, Ollama models and any
Hugging Face model cache. Check them with `Get-ChildItem -Recurse | Measure-Object Length -Sum` in each folder.

## Voices, tone, music and effects
Voices come from the engine in `TTS_BACKEND` (`windows` lists the voices installed in Windows; `kokoro` has a fixed list).
Tone changes how the script is written and the speaking rate. Music (calm, upbeat, cinematic, playful) and sound effects
(whooshes, pops, a closing chime) are synthesised by FFmpeg for every video, so there are no downloads or licences.
Music is ducked under the narration. Video length is exactly the chosen duration: if narration is too long the voice is
sped up (up to 1.6x), and if it is short the scenes are held longer.
