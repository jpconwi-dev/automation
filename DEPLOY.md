# Deploy: Render (backend) + Vercel (frontend), free models

Facts below were checked on **16 Oct 2026**. Free tiers change often, so re-check the linked pages before relying on a number.

## What runs where

| Job | Free service | Model | Limit (free) |
|---|---|---|---|
| Script | Groq (OpenAI-compatible API) | Llama 3.3 70B, fallback Qwen3 32B (open weights, also on Hugging Face) | no card; ~1,000 requests/day on the large models |
| Images | Cloudflare Workers AI | FLUX.1 [schnell] | 10,000 Neurons/day, roughly 100+ images |
| Voice | Cloudflare Workers AI | MeloTTS (English, Spanish, French, Japanese, Chinese only) | same 10,000 Neurons/day, very cheap per minute |
| Video assembly | FFmpeg inside the Render container | none | uses your Render CPU/RAM |
| AI video clips | optional, see below | Wan / LTX via a Hugging Face Space | a few clips per day at best |

**Why not the Hugging Face Inference API?** Per Hugging Face's pricing page, free accounts now get **no** included Inference Providers credits (PRO gets $2/month). The old `hf_api` backend still exists but needs paid credits. Hugging Face *models* are used; the free *hosting* comes from Groq and Cloudflare.

**Why not run models on Render?** The free instance is 512 MB RAM and 0.1 CPU. I measured Kokoro (82M voice model, int8 ONNX) on one full CPU: slower than real time and 421 MB RAM. On 0.1 CPU that is minutes per scene and likely out of memory. So all AI runs on remote free APIs and Render only orchestrates and renders.

## 1. Get the free keys (about 10 minutes)

1. **Groq**: console.groq.com, create an API key. Put it in `LLM_API_KEY`.
2. **Cloudflare**: dash.cloudflare.com, sign up. Copy your **Account ID** (right side of the dashboard home). My Profile > API Tokens > Create Token > "Workers AI" template. Put them in `CLOUDFLARE_ACCOUNT_ID` / `CLOUDFLARE_API_TOKEN`.
3. Choose a long password for `ACCESS_KEY`. Without it, anyone who finds your Render URL can spend your free quotas.

## 2. Backend on Render

1. Push this repo to GitHub (it is already at `jpconwi/explainer`). **Never commit `backend/.env`** (it is git-ignored; yours was not tracked, good).
2. Render dashboard: **New + > Blueprint**, pick the repo. It reads `render.yaml`.
3. Fill the prompted secrets. For `CORS_ORIGINS` use your Vercel URL (you can edit it after step 3 below).
4. Deploy. Check `https://<service>.onrender.com/api/health`: every dependency should say `ok: true`.

### Be realistic about the free instance
- **Sleeps after 15 min idle**, about 1 minute to wake. Files are **wiped** on every sleep/restart/redeploy (videos, SQLite database). Download videos right away.
- **Memory:** in my sandbox (1 CPU) a 60 s 16:9 video of AI stills peaked at about **250-280 MB** FFmpeg memory and took 22-74 s. At 0.1 CPU expect roughly 10x longer (several minutes). That fits 512 MB with little room. Stock-footage (`pexels`/`pixabay`) and placeholder modes peaked at **~645 MB**, which will be killed on the free plan. 5-minute videos were not measured; use 30/60/120 s on free.
- If renders fail with out-of-memory, move to the **Standard** plan (2 GB, $25/month) by changing `plan:` in `render.yaml`. Starter ($7) is still 512 MB, so it only removes the sleep.

## 3. Frontend on Vercel

1. New Project > import the repo > **Root Directory: `frontend`**. Framework is auto-detected (Vite).
2. Environment variable: `VITE_API_URL=https://<service>.onrender.com`.
3. Deploy, then put the Vercel URL into Render's `CORS_ORIGINS` and redeploy the backend.
4. Open the site. It asks for your `ACCESS_KEY` once (stored in the browser).

## AI video model (optional, be careful)

**There is no free text-to-video API.** Options, honestly:
- `VIDEO_BACKEND=hf_space`: calls a public Hugging Face ZeroGPU Space (Wan or LTX demos) with your `HF_TOKEN`. Free accounts get a small daily GPU quota (sources I found quote about 3.5 minutes/day), so only `VIDEO_MAX_CLIPS` scenes (default 2: the opening and the closing scene) become AI clips; everything else stays a still image with motion. Set `HF_VIDEO_SPACE`, then open that Space's **"Use via API"** page and copy the endpoint name into `HF_VIDEO_API_NAME` and the argument names into `HF_VIDEO_PARAMS` (placeholders `{prompt}` `{seed}` `{width}` `{height}` `{duration}`).
- `VIDEO_BACKEND=hf_inference`: Hugging Face Inference Providers. Needs paid credits.
- If the AI step fails or the quota is gone, the job continues with images and says so in the progress message and the scene's metadata. It never switches to a paid service.
- **Not tested against live Hugging Face.** The sandbox could not reach it. The code is tested with stand-ins for `gradio_client`; expect to adjust `HF_VIDEO_PARAMS` on first run.
- AI clips are a few seconds and loop under longer narration.

## Daily auto-posting later (what to know now)

- **YouTube:** videos uploaded through the API from an unaudited Google Cloud project are **forced private** until the project passes YouTube's compliance audit (a form that takes weeks). Plan the audit before building the uploader. Default quota is about 100 uploads/day, so quota is not the problem.
- **Facebook Page:** I have not checked Meta's current Graph API requirements; do that before designing it.
- **Disclosure:** both platforms have rules on labeling realistic AI-generated content. Check them before posting.
- **Architecture:** Render free sleeps and loses files, so the automation should generate and upload in one run. A scheduled **GitHub Actions** job (free runner: more CPU/RAM than Render free) calling `POST /api/projects` then `/generate` with the `X-Access-Key` header, then uploading, is the natural next step.

## Local development
```
cp backend/.env.example backend/.env   # fill the keys
./scripts/dev.sh
```
