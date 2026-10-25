#!/usr/bin/env python3
"""Upload video.mp4 to YouTube (as a Short) using the title and caption saved in result.json.

Needs three GitHub secrets: YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN.
Writes youtube.json (success or the reason for failure) so notify.py can report it.
Exit code 0 = uploaded, 1 = failed, 2 = skipped (no secrets set).

Optional env: YT_PRIVACY (public | unlisted | private, default public), YT_CATEGORY (default 27 = Education),
YT_LANGUAGE (default en). YT_TOKEN_URL / YT_API_BASE exist only so the script can be tested against a fake server.
"""
import datetime as dt
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = os.environ.get("YT_TOKEN_URL", "https://oauth2.googleapis.com/token")
API_BASE = os.environ.get("YT_API_BASE", "https://www.googleapis.com")
VIDEO = pathlib.Path(os.environ.get("VIDEO_FILE", "video.mp4"))
RESULT = pathlib.Path(os.environ.get("RESULT_FILE", "result.json"))
OUT = pathlib.Path(os.environ.get("YT_RESULT_FILE", "youtube.json"))


def log(*a):
    print(dt.datetime.now().strftime("%H:%M:%S"), "youtube:", *a, flush=True)


class YTError(Exception):
    def __init__(self, message: str, reason: str = ""):
        super().__init__(message)
        self.reason = reason


def http(method: str, url: str, headers: dict, data: bytes | None = None, timeout: int = 600):
    """Returns (status, response headers, body bytes). HTTP errors are returned, not raised."""
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise YTError(f"Could not reach Google: {e}", "network")


def google_error(status: int, body: bytes) -> YTError:
    """Turn a Google error reply into a short message plus a machine-readable reason."""
    try:
        j = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return YTError(f"HTTP {status}: {body[:200]!r}", "unknown")
    if isinstance(j.get("error"), str):  # OAuth style: {"error": "invalid_grant", "error_description": "..."}
        return YTError(f"{j['error']}: {j.get('error_description', '')}".strip(": "), j["error"])
    err = j.get("error") or {}
    errors = err.get("errors") or [{}]
    reason = errors[0].get("reason") or err.get("status") or "unknown"
    return YTError(f"HTTP {status} {reason}: {err.get('message', '')}".strip(), reason)


def access_token(cid: str, secret: str, refresh: str) -> str:
    body = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "refresh_token": refresh,
                                   "grant_type": "refresh_token"}).encode()
    status, _, resp = http("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body, 60)
    if status != 200:
        raise google_error(status, resp)
    return json.loads(resp)["access_token"]


def clean(text: str, limit: int) -> str:
    text = re.sub(r"[<>]", "", text or "").strip()
    return text[:limit].rstrip()


def build_metadata(r: dict, privacy: str) -> dict:
    title = clean(re.sub(r"\s*#\w+", "", r.get("title") or r.get("topic") or "New video"), 88)
    if "#shorts" not in title.lower():
        title += " #Shorts"  # lets YouTube know it is a Short
    caption = (r.get("caption") or "").strip()
    description = clean(caption + ("\n\n" if caption else "") + "#Shorts", 4900)
    tags, used = [], 0
    for t in [x.lstrip("#") for x in (r.get("hashtags") or [])] + ([r["subject"]] if r.get("subject") else []):
        t = re.sub(r"[<>,]", "", t).strip()
        if t and t.lower() not in [x.lower() for x in tags] and used + len(t) + 1 <= 450:
            tags.append(t)
            used += len(t) + 1
    return {
        "snippet": {"title": title, "description": description, "tags": tags,
                    "categoryId": os.environ.get("YT_CATEGORY", "27"),
                    "defaultLanguage": os.environ.get("YT_LANGUAGE", "en"),
                    "defaultAudioLanguage": os.environ.get("YT_LANGUAGE", "en")},
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "embeddable": True,
                   "publicStatsViewable": True},
    }


def upload(token: str, meta: dict) -> dict:
    size = VIDEO.stat().st_size
    init_url = f"{API_BASE}/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
               "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"}
    status, hdrs, body = http("POST", init_url, headers, json.dumps(meta).encode(), 120)
    if status != 200 or not hdrs.get("Location"):
        raise google_error(status, body)
    log(f"upload started ({size / 1048576:.1f} MB)")
    status, _, body = http("PUT", hdrs["Location"], {"Content-Type": "video/mp4", "Content-Length": str(size)},
                           VIDEO.read_bytes(), 900)
    if status not in (200, 201):
        raise google_error(status, body)
    return json.loads(body)


def set_thumbnail(token: str, video_id: str) -> str:
    """Best effort: custom thumbnails need a verified channel and the full youtube scope, so this can be refused."""
    img = pathlib.Path(os.environ.get("THUMB_FILE", "thumbnail.jpg"))
    if not img.is_file():
        return "no thumbnail file"
    try:
        status, _, body = http("POST", f"{API_BASE}/upload/youtube/v3/thumbnails/set?videoId={video_id}",
                               {"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"}, img.read_bytes(), 120)
    except Exception as e:  # noqa: BLE001
        return f"failed ({type(e).__name__})"
    if status == 200:
        return "set"
    return f"not accepted (HTTP {status}): {body[:160].decode('utf-8', 'replace')}"


def save(d: dict) -> None:
    d["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        OUT.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


HISTORY = pathlib.Path(os.environ.get("HISTORY_FILE", "data/posted.json"))


def record_topic(r: dict, video_id: str) -> None:
    """When Facebook did not take the video but YouTube did, the topic still counts as posted, otherwise tomorrow's run
    would make the same video again. Nothing is recorded for a dry-run test upload or when Facebook already recorded it."""
    if r.get("dry_run") or r.get("posted_at") or not r.get("topic"):
        return
    try:
        try:
            posts = json.loads(HISTORY.read_text(encoding="utf-8")).get("posts", [])
        except (OSError, ValueError):
            posts = []
        posts.append({"topic": r.get("topic", ""), "subject": r.get("subject", ""), "title": r.get("title", ""),
                      "video_id": "", "youtube_id": video_id, "state": "YOUTUBE_ONLY", "mode": "short",
                      "seconds": r.get("duration_s"), "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        HISTORY.write_text(json.dumps({"posts": posts}, indent=2, ensure_ascii=False), encoding="utf-8")
        log("topic recorded in", HISTORY)
    except OSError as e:  # recording is a convenience: never turn a finished upload into a failure
        log("could not record the topic:", e)


def main() -> int:
    cid, secret, refresh = (os.environ.get(k, "") for k in ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN"))
    if not (cid and secret and refresh):
        log("YT_CLIENT_ID / YT_CLIENT_SECRET / YT_REFRESH_TOKEN not set, skipping.")
        return 2
    try:
        r = json.loads(RESULT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        r = {}
    if not r.get("youtube_ready"):  # daily_post.py sets this only for a video that passed every footage and caption check
        log("this run made no publishable video (it stopped early or skipped a repeat), skipping.")
        return 2
    privacy = os.environ.get("YT_PRIVACY", "public").lower()
    if privacy not in ("public", "unlisted", "private"):
        privacy = "public"
    try:
        if not VIDEO.is_file():
            raise YTError(f"{VIDEO} was not found, so there is nothing to upload.", "nofile")
        meta = build_metadata(r, privacy)
        log("title:", meta["snippet"]["title"])
        token = access_token(cid, secret, refresh)
        res = upload(token, meta)
        vid = res.get("id", "")
        actual = (res.get("status") or {}).get("privacyStatus", "")
        url = f"https://www.youtube.com/shorts/{vid}"
        log(f"uploaded: {url} (privacy: {actual or 'unknown'})")
        if actual and actual != privacy:
            log(f"WARNING: asked for {privacy} but YouTube set {actual}")
        thumb = set_thumbnail(token, vid) if vid else "skipped"
        log("youtube thumbnail:", thumb)
        record_topic(r, vid)
        save({"ok": True, "id": vid, "url": url, "thumbnail": thumb, "title": meta["snippet"]["title"], "privacy_requested": privacy,
              "privacy_actual": actual, "uploaded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
        return 0
    except YTError as e:
        log("FAILED:", e)
        save({"ok": False, "error": str(e)[:600], "reason": e.reason})
        return 1
    except Exception as e:  # never leave the workflow without a report
        log("FAILED:", type(e).__name__, e)
        save({"ok": False, "error": f"{type(e).__name__}: {e}"[:600], "reason": "unexpected"})
        return 1


if __name__ == "__main__":
    sys.exit(main())
