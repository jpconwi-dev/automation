#!/usr/bin/env python3
"""Daily auto-post: pick a topic -> your Render API makes the video -> publish to a Facebook Page.

Runs in one pass (Render free wipes files on sleep, so generate + upload must happen together).
Env vars: see .github/workflows/daily-post.yml
"""
import datetime as dt
import difflib
import json
import os
import pathlib
import re
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
HISTORY = pathlib.Path(os.environ.get("HISTORY_FILE", "data/posted.json"))  # every topic already posted


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def ok(r: httpx.Response, what: str) -> dict:
    if r.status_code >= 400:
        sys.exit(f"{what} failed ({r.status_code}): {r.text[:600]}")
    return r.json()


# ---------- 0. memory of what was already posted (so nothing repeats) ----------
STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "are", "how", "why", "what", "can",
        "does", "do", "with", "your", "you", "it", "its", "that", "this", "from", "by", "at", "as"}


def load_history() -> list[dict]:
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8")).get("posts", [])
    except (OSError, ValueError):
        return []


def save_history(posts: list[dict]) -> None:
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps({"posts": posts}, indent=2, ensure_ascii=False), encoding="utf-8")


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP}


def too_similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    if wa == wb:
        return True
    jaccard = len(wa & wb) / len(wa | wb)
    ratio = difflib.SequenceMatcher(None, " ".join(sorted(wa)), " ".join(sorted(wb))).ratio()
    return jaccard >= 0.6 or ratio >= 0.85


def already_covered(candidate: str, history: list[dict]) -> bool:
    for p in history:
        for old in (p.get("topic", ""), p.get("title", "")):
            if old and too_similar(candidate, old):
                return True
    return False


# ---------- 1. topic (the automatic content idea) ----------
def pick_topic(history: list[dict]) -> dict:
    today = dt.date.today()
    if NICHE and LLM_KEY:
        covered = [x for p in history[-80:] for x in (p.get("topic", ""), p.get("title", "")) if x]
        for attempt in range(5):  # ask again if the idea is a repeat
            try:
                avoid = "; ".join(dict.fromkeys(covered))[:3000] or "(nothing yet)"
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
                                f"Already covered, so never repeat or closely overlap with these: {avoid}. "
                                'JSON: {"topic": "...", "audience": "...", "hashtags": ["#a","#b","#c"]}')},
                        ],
                    },
                )
                data = r.json()
                if "choices" not in data:
                    log(f"LLM topic error ({r.status_code}):", r.text[:300])
                    break
                t = json.loads(data["choices"][0]["message"]["content"])
                topic = t.get("topic", "").strip()
                if len(topic) < 3:
                    continue
                if already_covered(topic, history):
                    log("topic is a repeat, asking again:", topic)
                    covered.append(topic)
                    continue
                t["topic"] = topic
                t["audience"] = t.get("audience") or AUDIENCE
                return t
            except Exception as e:  # fall back to the list
                log("LLM topic failed:", e)
                break
    lines = [l.strip() for l in pathlib.Path("scripts/topics.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    fresh = [l for l in lines if not already_covered(l, history)]
    if not fresh:
        sys.exit("No unused topics left (the AI topic step failed and topics.txt is used up). Add new lines to scripts/topics.txt. Nothing was posted.")
    return {"topic": fresh[today.toordinal() % len(fresh)], "audience": AUDIENCE, "hashtags": []}


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


# ---------- 3a. make sure the token really belongs to the Page ----------
def check_token():
    r = httpx.get(f"{GRAPH}/me", params={"fields": "id,name", "access_token": PAGE_TOKEN}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"FB_PAGE_TOKEN was rejected by Facebook ({r.status_code}): {r.text[:200]}")
    me = r.json()
    log(f"token belongs to: {me.get('name')} (id {me.get('id')}) | FB_PAGE_ID is {PAGE_ID}")
    if str(me.get("id")) != str(PAGE_ID).strip():
        sys.exit("FB_PAGE_TOKEN is not the Page token for FB_PAGE_ID. If the name above is a person, you saved a "
                 "USER token. Get the Page token from me/accounts and update the FB_PAGE_TOKEN secret. Nothing was posted.")


# ---------- 3. publish to the Facebook Page ----------
def post_reel(path: pathlib.Path, caption: str, record):
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
    record(vid)  # remembered immediately, even if the status check below fails
    for _ in range(30):  # wait for Facebook to process it
        s = ok(httpx.get(f"{GRAPH}/{vid}", params={"fields": "status", "access_token": PAGE_TOKEN}, timeout=60), "status")
        log("fb status:", s.get("status"))
        st = json.dumps(s.get("status", {}))
        if '"ready"' in st or '"published"' in st or "error" in st:
            break
        time.sleep(10)


def post_video(path: pathlib.Path, title: str, caption: str, record):
    with path.open("rb") as f:
        r = httpx.post(f"{GRAPH}/{PAGE_ID}/videos", timeout=900, files={"source": f}, data={
            "title": title[:100], "description": caption, "published": "true" if FB_STATE == "PUBLISHED" else "false",
            "access_token": PAGE_TOKEN})
    res = ok(r, "video post")
    log("video posted:", res)
    record(res.get("id", ""))


def main():
    if not DRY_RUN:
        check_token()  # fail fast, before spending minutes on a video
    history = load_history()
    log(f"{len(history)} earlier posts on record")
    t = pick_topic(history)
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
    history = load_history()  # last check right before posting, in case another run finished meanwhile
    if already_covered(t["topic"], history) or already_covered(title, history):
        sys.exit("This topic was already posted. Skipping so nothing repeats.")

    def record(video_id: str):
        save_history(load_history() + [{
            "topic": t["topic"], "title": title, "video_id": video_id, "state": FB_STATE,
            "mode": FB_MODE, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}])
        log("recorded in", HISTORY)

    if FB_MODE == "reel":
        post_reel(path, caption, record)
    else:
        post_video(path, title, caption, record)
    log("done")


if __name__ == "__main__":
    main()