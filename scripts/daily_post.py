#!/usr/bin/env python3
"""Daily auto-post: pick a topic -> your Render API makes the video -> publish to a Facebook Page.

Runs in one pass (Render free wipes files on sleep, so generate + upload must happen together).
Env vars: see .github/workflows/daily-post.yml
"""
import datetime as dt
import json
import os
import pathlib
import sys
import time

import httpx

API = os.environ["API_URL"].rstrip("/")            # https://<service>.onrender.com
KEY = os.environ.get("ACCESS_KEY", "")             # same ACCESS_KEY you set on Render
PAGE_ID = os.environ["FB_PAGE_ID"]
PAGE_TOKEN = os.environ["FB_PAGE_TOKEN"]           # long-lived PAGE access token
NICHE = os.environ.get("NICHE", "").strip()        # e.g. "personal finance tips for beginners"
AUDIENCE = os.environ.get("AUDIENCE", "a general audience")
LLM_KEY = os.environ.get("LLM_API_KEY", "")        # Groq key, used only to invent a fresh topic each day
LLM_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")
FB_MODE = os.environ.get("FB_MODE", "reel")        # reel (9:16, up to ~90s) | video (any aspect)
FB_STATE = os.environ.get("FB_STATE", "PUBLISHED") # set DRAFT to test without going public
DRY_RUN = os.environ.get("DRY_RUN", "") == "true"  # make the video but do not post
DURATION = int(os.environ.get("DURATION", "60"))
GRAPH = "https://graph.facebook.com/v25.0"
RUPLOAD = "https://rupload.facebook.com/video-upload/v25.0"
H = {"X-Access-Key": KEY}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def ok(r: httpx.Response, what: str) -> dict:
    if r.status_code >= 400:
        sys.exit(f"{what} failed ({r.status_code}): {r.text[:600]}")
    return r.json()


# ---------- 1. topic (the automatic content idea) ----------
def pick_topic() -> dict:
    today = dt.date.today()
    if NICHE and LLM_KEY:
        try:
            r = httpx.post(
                f"{LLM_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_KEY}"},
                timeout=60,
                json={
                    "model": LLM_MODEL,
                    "temperature": 1.0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": "You plan short educational social videos. Reply with JSON only."},
                        {"role": "user", "content": (
                            f"Niche: {NICHE}. Today is {today:%A %d %B %Y}. "
                            "Give ONE specific, surprising, factual video topic for today that is not generic. "
                            'JSON: {"topic": "...", "audience": "...", "hashtags": ["#a","#b","#c"]}')},
                    ],
                },
            )
            t = json.loads(r.json()["choices"][0]["message"]["content"])
            if len(t.get("topic", "")) >= 3:
                t["audience"] = t.get("audience") or AUDIENCE
                return t
        except Exception as e:  # fall back to the list
            log("LLM topic failed, using topics.txt:", e)
    lines = [l.strip() for l in pathlib.Path("scripts/topics.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    return {"topic": lines[today.toordinal() % len(lines)], "audience": AUDIENCE, "hashtags": []}


# ---------- 2. make the video on your Render backend ----------
def wake_backend():
    for i in range(12):  # free Render needs ~1 min to wake
        try:
            if httpx.get(f"{API}/api/health", timeout=90).status_code == 200:
                log("backend awake")
                return
        except httpx.HTTPError:
            pass
        log("waiting for backend...", i)
        time.sleep(20)
    sys.exit("backend never woke up")


def make_video(t: dict) -> tuple[pathlib.Path, dict]:
    body = {
        "mode": "explainer", "topic": t["topic"][:500], "audience": t["audience"][:200],
        "style": "illustrated", "duration": DURATION, "language": "en-us",
        "aspect_ratio": "9:16" if FB_MODE == "reel" else "16:9",
        "tone": "energetic", "voice": "auto", "voice_speed": 1.0,
        "background_music": True, "music_style": "auto", "sound_effects": True,
    }
    p = ok(httpx.post(f"{API}/api/projects", json=body, headers=H, timeout=60), "create project")
    pid = p["id"]
    job = ok(httpx.post(f"{API}/api/projects/{pid}/generate", headers=H, timeout=60), "start generation")
    log("job", job["id"], "started for:", t["topic"])
    deadline = time.time() + 75 * 60
    while time.time() < deadline:
        time.sleep(20)
        j = ok(httpx.get(f"{API}/api/jobs/{job['id']}", headers=H, timeout=60), "poll job")
        log(j["stage"], j["progress"], j["message"])
        if j["status"] == "completed":
            break
        if j["status"] in ("failed", "cancelled"):
            sys.exit(f"job {j['status']}: {j.get('error')}")
    else:
        sys.exit("timed out waiting for render")
    proj = ok(httpx.get(f"{API}/api/projects/{pid}", headers=H, timeout=60), "get project")
    out = pathlib.Path("video.mp4")
    with httpx.stream("GET", f"{API}/api/projects/{pid}/video", headers=H, timeout=300) as r:
        if r.status_code >= 400:
            sys.exit(f"video download failed ({r.status_code})")
        with out.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    log("video saved", out.stat().st_size // 1024, "KB")
    return out, proj


# ---------- 3. publish to the Facebook Page ----------
def post_reel(path: pathlib.Path, caption: str):
    start = ok(httpx.post(f"{GRAPH}/{PAGE_ID}/video_reels",
                          data={"upload_phase": "start", "access_token": PAGE_TOKEN}, timeout=60), "reel start")
    vid = start["video_id"]
    up = httpx.post(start["upload_url"], timeout=900, content=path.read_bytes(),
                    headers={"Authorization": f"OAuth {PAGE_TOKEN}", "offset": "0", "file_size": str(path.stat().st_size)})
    ok(up, "reel upload")
    fin = ok(httpx.post(f"{GRAPH}/{PAGE_ID}/video_reels", timeout=120, data={
        "upload_phase": "finish", "video_id": vid, "video_state": FB_STATE,
        "description": caption, "access_token": PAGE_TOKEN}), "reel publish")
    log("reel finish:", fin)
    for _ in range(30):  # wait for Facebook to process it
        s = ok(httpx.get(f"{GRAPH}/{vid}", params={"fields": "status", "access_token": PAGE_TOKEN}, timeout=60), "status")
        log("fb status:", s.get("status"))
        st = json.dumps(s.get("status", {}))
        if '"ready"' in st or '"published"' in st or "error" in st:
            break
        time.sleep(10)


def post_video(path: pathlib.Path, title: str, caption: str):
    with path.open("rb") as f:
        r = httpx.post(f"{GRAPH}/{PAGE_ID}/videos", timeout=900, files={"source": f}, data={
            "title": title[:100], "description": caption, "published": "true" if FB_STATE == "PUBLISHED" else "false",
            "access_token": PAGE_TOKEN})
    log("video posted:", ok(r, "video post"))


def main():
    t = pick_topic()
    wake_backend()
    path, proj = make_video(t)
    s = proj.get("script") or {}
    title = s.get("title") or t["topic"]
    caption = "\n\n".join(x for x in [title, s.get("description", ""), " ".join(t.get("hashtags", [])),
                                       "Made with AI."] if x)
    log("caption:\n" + caption)
    if DRY_RUN:
        log("DRY_RUN on: not posting")
        return
    (post_reel if FB_MODE == "reel" else lambda p, c: post_video(p, title, c))(path, caption)
    log("done")


if __name__ == "__main__":
    main()
