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
import random
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
FB_MODE = os.environ.get("FB_MODE", "auto")        # auto = Reel when 90s or less, normal video when longer | reel | video
FB_STATE = os.environ.get("FB_STATE", "PUBLISHED") # set DRAFT to test without going public
DRY_RUN = os.environ.get("DRY_RUN", "") == "true"  # make the video but do not post
DURATIONS = [int(x) for x in re.split(r"[,\s]+", os.environ.get("DURATIONS", "30,60,90")) if x in ("30", "60", "90", "120")] or [60]
DURATION_CHOICE = os.environ.get("DURATION", "").strip()   # "30" | "60" | "90" | "120" | "" / "auto" = random from DURATIONS
TOPIC_OVERRIDE = os.environ.get("TOPIC", "").strip()       # type a topic in "Run workflow" to force it: "black bear" or "topic | subject"
STYLE = os.environ.get("STYLE", "cinematic")
TONE = os.environ.get("TONE", "energetic")
MUSIC_STYLE = os.environ.get("MUSIC_STYLE", "cinematic")
FB_ASPECT = os.environ.get("FB_ASPECT", "9:16")
GRAPH = "https://graph.facebook.com/v25.0"
RUPLOAD = "https://rupload.facebook.com/video-upload/v25.0"
H = {"X-Access-Key": KEY}
HISTORY = pathlib.Path(os.environ.get("HISTORY_FILE", "data/posted.json"))  # every topic already posted


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# Details of this run, written to result.json at the end so notify.py can send a full report.
RESULT_FILE = pathlib.Path(os.environ.get("RESULT_FILE", "result.json"))
R: dict = {"started_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "stage": "starting",
           "dry_run": False, "fb_state": os.environ.get("FB_STATE", "PUBLISHED")}


def save_result() -> None:
    R["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        RESULT_FILE.write_text(json.dumps(R, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


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


# ---------- 0b. small helpers: ask the LLM, keep the word "AI" out of the post ----------
AI_WORDS = re.compile(r"\b(?:artificial intelligence|ai[- ]generated|ai[- ]made|ai[- ]powered|ai[- ]driven|made with ai|chat ?gpt|a\.i\.|ai|gpt|llm)\b", re.I)
AI_TAG = re.compile(r"^(?:ai|artificialintelligence|chatgpt|gpt)(?:art|video|tools?|tech|generated|made|content|news|facts|voice)?$", re.I)


def has_ai(text: str) -> bool:
    return bool(AI_WORDS.search(text or ""))


def safe(*options: str) -> str:
    """First option that does not mention AI. Removing the word would leave broken sentences, so whole options are skipped."""
    for o in options:
        o = " ".join((o or "").split())
        if o and not has_ai(o):
            return o
    return ""


def llm_json(system: str, user: str, temperature: float = 0.7) -> dict | None:
    if not LLM_KEY:
        return None
    try:
        r = httpx.post(f"{LLM_URL}/chat/completions", headers={"Authorization": f"Bearer {LLM_KEY}"}, timeout=60,
                       json={"model": LLM_MODEL, "temperature": temperature, "response_format": {"type": "json_object"},
                             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        data = r.json()
        if "choices" not in data:
            log(f"LLM error ({r.status_code}):", r.text[:300])
            return None
        return json.loads(data["choices"][0]["message"]["content"])
    except Exception as e:
        log("LLM call failed:", e)
        return None


# Words stock-video tags almost never contain. Keeping them would make the footage search find nothing.
FILLER = {"immortal", "ultra", "ultra-fast", "fast", "amazing", "incredible", "mysterious", "ancient", "legendary",
          "deadly", "famous", "tiny", "huge", "rare", "strange", "unique", "powerful", "mighty", "secret", "hidden"}


def clean_subject(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9 '\-]", " ", s or "")
    words = s.lower().split()[:3]
    kept = [w for w in words if w not in FILLER]
    return " ".join(kept or words).strip()


def derive_subject(topic: str) -> str:
    """The main visible thing the footage must show, e.g. 'black bear'. Empty when it can't be told."""
    d = llm_json("You pick stock-video search terms. Reply with JSON only.",
                 f'Topic: "{topic}". What is the ONE main visible subject that footage must show? Reply 1 to 3 simple, '
                 'common English words a stock-video site would tag, e.g. "black bear", "volcano", "honey bee". '
                 'Keep an adjective only if dropping it could show a different animal or thing (black bear, polar bear, red panda). '
                 'Drop adjectives stock sites never tag, such as immortal, giant, tiny, ancient, ultra-fast (immortal jellyfish -> jellyfish). '
                 'If the topic has no single visible subject, '
                 'reply an empty string. JSON: {"subject": "..."}', 0.2)
    return clean_subject((d or {}).get("subject", ""))


def subject_aliases(subject: str, topic: str) -> list[str]:
    """Other names stock sites might tag the same thing with (pistol shrimp -> snapping shrimp, alpheidae).
    Footage is only accepted if it is tagged with the full subject OR one full alias, so this widens the search
    without ever allowing a different animal or object."""
    if not subject:
        return []
    d = llm_json("You help find stock footage. Reply with JSON only.",
                 f'Topic: "{topic}". Main subject: "{subject}". List up to 4 OTHER names for exactly this same thing that '
                 'video sites, Wikipedia or museums use as tags: common alternate names, plural or singular forms, and the '
                 'scientific name if it is an animal or plant. Each is 1 to 3 words. Never list a related but different '
                 'thing (a different species, a bigger category, a similar object). If there are none, return an empty list. '
                 'JSON: {"aliases": ["..."]}', 0.1)
    out = []
    for a in (d or {}).get("aliases") or []:
        a = clean_subject(str(a))
        if a and a != subject and a not in out and not set(a.split()) < set(subject.split()):
            out.append(a)  # a strict subset of the subject words ("shrimp" for "pistol shrimp") would be too broad
    return out[:4]


def parse_topic_line(line: str) -> dict:
    """'topic | subject' or just 'topic' (subject is then worked out automatically)."""
    topic, _, subj = line.partition("|")
    topic, subj = topic.strip(), clean_subject(subj)
    return {"topic": topic, "subject": subj or derive_subject(topic), "audience": AUDIENCE, "hashtags": []}


def hashtag_list(raw: list, t: dict) -> list[str]:
    tags, seen = [], set()
    for h in list(raw or []):
        w = re.sub(r"[^A-Za-z0-9]", "", str(h))
        if not w or AI_TAG.match(w) or w.lower() in seen or len(w) > 30:
            continue
        seen.add(w.lower())
        tags.append("#" + w)
    if len(tags) < 4:  # top up from the subject / topic so the post is never left without tags
        subj_words = _words(t.get("subject", ""))
        extra = [t.get("subject", "").replace(" ", "")] + [w for w in sorted(_words(t["topic"]) - subj_words, key=len, reverse=True)[:3]]
        extra += ["didyouknow", "facts", "nature" if t.get("subject") else "science"]
        for w in extra:
            w = re.sub(r"[^A-Za-z0-9]", "", w)
            if w and not AI_TAG.match(w) and w.lower() not in seen and len(tags) < 6:
                seen.add(w.lower())
                tags.append("#" + w)
    return tags[:6]


def add_credits(caption: str, meta: dict) -> str:
    """Footage that requires attribution (e.g. Wikimedia CC BY) gets a credit line just above the hashtags."""
    credits = sorted({v["credit"] for v in meta.values() if v.get("credit_required") and v.get("credit")})
    if not credits:
        return caption
    line = "Footage: " + "; ".join(credits)
    blocks = caption.split("\n\n")
    blocks.insert(max(len(blocks) - 1, 1), line)
    return "\n\n".join(blocks)


def build_caption(t: dict, proj: dict) -> tuple[str, str]:
    """Search-friendly title + caption + hashtags. Never mentions AI."""
    sc = proj.get("script") or {}
    subject = t.get("subject", "")
    d = llm_json(
        "You write Facebook Reel captions that rank in Facebook search and make people watch to the end. Reply JSON only.",
        f"Topic: {t['topic']}\nMain subject: {subject or 'n/a'}\nWhat the video says: {sc.get('description', '')} {sc.get('hook', '')}\n"
        f"Audience: {t.get('audience', AUDIENCE)}\n"
        "Rules: 'title' is max 70 characters, starts with the main keyword, is a curiosity hook, no clickbait lies. "
        "'caption' is 2 short sentences (max 220 characters) that naturally use the main keyword and one related keyword, "
        "then ONE short question that invites comments. 'hashtags' is exactly 5 tags without '#': 2 specific to the subject, "
        "2 broader topic tags, 1 format tag like didyouknow. The question must be specific to the topic. "
        "Never ask people to like, share, follow or tag anyone, and never write 'comment YES'. Never use the words AI, artificial intelligence, generated, "
        "bot, or any AI-related hashtag. No emojis. "
        'JSON: {"title": "...", "caption": "...", "hashtags": ["..."]}', 0.6) or {}
    title = safe(d.get("title"), sc.get("title"), t["topic"], f"{subject.title()} explained" if subject else "", "Did you know?")[:90]
    body = safe(d.get("caption"), sc.get("description"),
                f"A quick look at {subject or 'this topic'} and what makes it so interesting. What should we cover next?")
    if subject and not (set(subject.split()) & _words(title)):
        title = f"{subject.title()}: {title}"[:90]
    tags = hashtag_list(d.get("hashtags") or t.get("hashtags") or [], t)
    return title, "\n\n".join(x for x in [title, body, " ".join(tags)] if x)


# ---------- 1. topic (the automatic content idea) ----------
def pick_topic(history: list[dict]) -> dict:
    today = dt.date.today()
    if TOPIC_OVERRIDE:
        return parse_topic_line(TOPIC_OVERRIDE)
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
                                f"Niche: {NICHE}. "
                                "Give ONE specific, well-established, factual, EVERGREEN video topic that is not generic, and not about artificial intelligence. "
                                "It must still be true and interesting in a year: no news, no dates, no 'this week', 'today', 'latest' or 'new'. "
                                "Pick a surprising angle that makes people curious, such as a record, a myth, or a 'how is this even possible' fact. "
                                f"Already covered, so never repeat or closely overlap with these: {avoid}. "
                                'JSON: {"topic": "...", "subject": "...", "audience": "...", "hashtags": ["a","b","c"]}. '
                                '"subject" is the ONE main visible thing, 1 to 3 simple common words as a stock-video site would tag it. '
                                'Keep an adjective only if dropping it could show a different animal or thing (e.g. "black bear", "polar bear", "red panda"). '
                                'Drop adjectives stock sites never tag, such as immortal, giant, tiny, ancient, ultra-fast '
                                '(e.g. "jellyfish", not "immortal jellyfish"; "volcano", "honey bee").')},
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
                t["subject"] = clean_subject(t.get("subject", "")) or derive_subject(topic)
                t["audience"] = AUDIENCE  # always your own AUDIENCE setting, never one the model made up
                return t
            except Exception as e:  # fall back to the list
                log("LLM topic failed:", e)
                break
    lines = [l.strip() for l in pathlib.Path("scripts/topics.txt").read_text().splitlines() if l.strip() and not l.startswith("#")]
    fresh = [l for l in lines if not already_covered(l.split("|")[0], history)]
    if not fresh:
        sys.exit("No unused topics left (the AI topic step failed and topics.txt is used up). Add new lines to scripts/topics.txt. Nothing was posted.")
    return parse_topic_line(fresh[today.toordinal() % len(fresh)])


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


def make_video(t: dict, duration: int) -> tuple[pathlib.Path, dict]:
    body = {
        "mode": "explainer", "topic": t["topic"][:500], "audience": t["audience"][:200],
        "style": STYLE, "duration": duration, "language": "en-us",
        "aspect_ratio": FB_ASPECT, "subject": t.get("subject", ""), "subject_aliases": t.get("aliases", []),
        "tone": TONE, "voice": "auto", "voice_speed": 1.0,
        "background_music": True, "music_style": MUSIC_STYLE, "sound_effects": True,
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
    R["page_name"] = me.get("name") or ""
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
        resp = httpx.get(f"{GRAPH}/{vid}", params={"fields": "status", "access_token": PAGE_TOKEN}, timeout=60)
        if resp.status_code >= 400:
            # The reel was already accepted and recorded above. Not being able to READ its processing status
            # (usually a missing permission) must not turn a successful post into a failed run.
            log(f"WARNING: could not read the reel's processing status (HTTP {resp.status_code}). "
                "The reel was accepted by Facebook; check your Page to confirm it appears.")
            R["fb_status"] = f"could not be checked (HTTP {resp.status_code}); Facebook accepted the reel and is processing it"
            break
        s = resp.json()
        log("fb status:", s.get("status"))
        R["fb_status"] = json.dumps(s.get("status", {}), ensure_ascii=False)[:400]
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


def pick_duration() -> int:
    return int(DURATION_CHOICE) if DURATION_CHOICE in ("30", "60", "90", "120") else random.choice(DURATIONS)


def main():
    R["dry_run"] = DRY_RUN
    R["stage"] = "checking the Facebook token"
    if not DRY_RUN:
        check_token()  # fail fast, before spending minutes on a video
    R["stage"] = "choosing a topic"
    history = load_history()
    log(f"{len(history)} earlier posts on record")
    R["earlier_posts"] = len(history)
    t = pick_topic(history)
    duration = pick_duration()
    as_reel = FB_MODE in ("reel", "auto") and duration <= 90  # Facebook Reels must be 3 to 90 seconds
    log(f"topic: {t['topic']} | subject: {t.get('subject') or '-'} | {duration}s | {'reel' if as_reel else 'normal video'}")
    R.update(topic=t["topic"], subject=t.get("subject", ""), duration_s=duration, format="Reel" if as_reel else "Normal video")
    t["aliases"] = subject_aliases(t.get("subject", ""), t["topic"])
    if t["aliases"]:
        log("other names for the subject:", ", ".join(t["aliases"]))
    R["stage"] = "making the video (voice, footage, music)"
    wake_backend()
    t0 = time.time()
    path, proj = make_video(t, duration)
    R["render_seconds"] = round(time.time() - t0)
    R["video_mb"] = round(path.stat().st_size / 1048576, 1)
    meta = proj.get("assets_meta") or {}
    R["scenes"] = [{"scene": k, "footage": str(v.get("tags") or v.get("note") or "-")[:90]} for k, v in sorted(meta.items())]
    for k, v in sorted(meta.items()):  # shows what footage each scene got, so you can check it matches the subject
        log(f"  {k}: {v.get('tags') or v.get('note') or '-'}")
    # A scene is real footage only when a stock source supplied it (or AI made it). Anything else is a placeholder
    # graphic, whichever backend produced it, and a video with one must never be posted.
    gaps = [k for k, v in meta.items() if not v.get("source") and not v.get("ai_generated")]
    if gaps and not DRY_RUN:
        sys.exit(f"{len(gaps)} scene(s) found no matching footage and would show a placeholder graphic. "
                 "Not posting this one; the topic is not used up, so the next run picks again.")
    if gaps:
        log(f"WARNING: {len(gaps)} scene(s) have no matching footage (a real run would not post this video)")
    R["stage"] = "writing the title, caption and hashtags"
    title, caption = build_caption(t, proj)
    caption = add_credits(caption, meta)
    log("caption:\n" + caption)
    R.update(title=title, caption=caption, hashtags=re.findall(r"#\w+", caption.splitlines()[-1] if caption else ""))
    if DRY_RUN:
        log("DRY_RUN on: not posting")
        return
    history = load_history()  # last check right before posting, in case another run finished meanwhile
    if already_covered(t["topic"], history) or already_covered(title, history):
        sys.exit("This topic was already posted. Skipping so nothing repeats.")

    def record(video_id: str):
        R["video_id"] = video_id
        R["posted_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        save_history(load_history() + [{
            "topic": t["topic"], "subject": t.get("subject", ""), "title": title, "video_id": video_id,
            "state": FB_STATE, "mode": "reel" if as_reel else "video", "seconds": duration,
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}])
        log("recorded in", HISTORY)

    R["stage"] = "uploading to Facebook"
    t1 = time.time()
    if as_reel:
        post_reel(path, caption, record)
    else:
        post_video(path, title, caption, record)
    R["upload_seconds"] = round(time.time() - t1)
    R["stage"] = "done"
    log("done")


if __name__ == "__main__":
    try:
        main()
        if R.get("stage") != "done" and R.get("dry_run"):
            R["stage"] = "done"
    except SystemExit as e:  # sys.exit("message") is how this script reports a failure
        if e.code not in (None, 0):
            R["error"] = str(e.code)[:1500]
        raise
    except Exception as e:
        R["error"] = f"{type(e).__name__}: {e}"[:1500]
        raise
    finally:
        save_result()