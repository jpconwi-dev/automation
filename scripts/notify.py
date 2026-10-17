#!/usr/bin/env python3
"""Send a detailed Telegram report after every run of the daily post.

Reads result.json (written by daily_post.py) and run.log. Never fails the workflow.
Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OUTCOME (result of the posting step), RUN_URL, FB_PAGE_ID.
Set NOTIFY_PRINT=1 to print the message instead of sending it (for testing).
"""
import datetime as dt
import json
import os
import pathlib
import re
import sys
import urllib.parse
import urllib.request

PHT = dt.timezone(dt.timedelta(hours=8))          # Philippines time
SCHEDULE_PHT = [(9, 0), (14, 0)]                   # 9:00 AM and 2:00 PM, same as the workflow cron
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
OUTCOME = os.environ.get("OUTCOME", "")
RUN_URL = os.environ.get("RUN_URL", "")
PAGE_ID = os.environ.get("FB_PAGE_ID", "")
PRINT_ONLY = os.environ.get("NOTIFY_PRINT") == "1"

RESULT = pathlib.Path(os.environ.get("RESULT_FILE", "result.json"))
LOG = pathlib.Path(os.environ.get("RUN_LOG", "run.log"))
YT_RESULT = pathlib.Path(os.environ.get("YT_RESULT_FILE", "youtube.json"))


def load_result() -> dict:
    try:
        return json.loads(RESULT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_youtube() -> dict:
    try:
        return json.loads(YT_RESULT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# YouTube error reason -> (title, cause, what to do)
YT_ERRORS = {
    "invalid_grant": ("YouTube login expired", "Google no longer accepts the saved YouTube token. This happens if the Google app is still in "
                      "'Testing' mode (tokens last about 7 days), or if access was removed.",
                      "Set the OAuth app to 'In production', create a new refresh token and update the YT_REFRESH_TOKEN secret."),
    "invalid_client": ("YouTube client details wrong", "The client ID or client secret was not accepted.",
                       "Check the YT_CLIENT_ID and YT_CLIENT_SECRET secrets."),
    "unauthorized_client": ("YouTube client not allowed", "This Google client is not allowed to use that token.",
                            "Create the token again with the same client ID and secret that are saved in GitHub."),
    "quotaExceeded": ("YouTube daily limit reached", "The Google project used up its daily YouTube quota (an upload costs 1,600 units).",
                      "It resets at midnight Pacific time. Request a quota extension if you need more uploads."),
    "uploadLimitExceeded": ("YouTube upload limit reached", "The channel hit YouTube's daily upload limit.",
                            "Try again later; verifying the channel with a phone number can raise the limit."),
    "forbidden": ("YouTube refused the upload", "The token may be missing the upload permission, or the channel cannot upload.",
                  "Create the token again and tick the youtube.upload permission."),
    "insufficientPermissions": ("YouTube permission missing", "The token does not have the youtube.upload permission.",
                                "Create the token again and tick the youtube.upload permission."),
    "youtubeSignupRequired": ("No YouTube channel", "The Google account used has no YouTube channel.",
                              "Create the channel on youtube.com, then create the token again."),
    "network": ("Could not reach Google", "The runner could not connect to Google.", "Nothing to do; the next run will try again."),
    "nofile": ("Video file missing", "There was no video file to upload.", "Open the run log to see why the video was not made."),
}


def youtube_lines(y: dict) -> list:
    if not y:
        return []
    out = [""]
    if y.get("ok"):
        want, got = y.get("privacy_requested"), y.get("privacy_actual")
        if got and want and got != want:
            out += [f"▶️ YouTube: uploaded, but YouTube set it to {got.upper()} instead of {want}.",
                    "Cause: your Google project has not passed YouTube's audit yet, so uploads stay private.",
                    "What to do: make the video public by hand in YouTube Studio, or finish the audit request.",
                    f"Link: {y.get('url', '')}"]
        else:
            out += [f"▶️ YouTube: uploaded as a Short ({got or want or 'unknown'}).",
                    f"Title: {y.get('title', '-')}", f"Link: {y.get('url', '')}"]
    else:
        title, cause, fix = YT_ERRORS.get(y.get("reason", ""), ("YouTube upload failed", "YouTube did not accept the upload.",
                                                               "Open the run log for the full message."))
        out += [f"⚠️ YouTube upload failed: {title}", f"Cause: {cause}", f"What to do: {fix}",
                "Technical detail: " + hide_secrets(y.get("error", ""))[:300]]
    return out


def load_log() -> str:
    try:
        return LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse(ts: str | None):
    try:
        return dt.datetime.fromisoformat(ts).astimezone(PHT) if ts else None
    except ValueError:
        return None


def when(d) -> str:
    return d.strftime("%a %d %b %Y, %I:%M %p").replace(" 0", " ") + " (Philippines time)" if d else "unknown"


def span(seconds) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    return f"{m} min {s} s" if m else f"{s} s"


def hide_secrets(text: str) -> str:
    text = re.sub(r"[A-Za-z0-9_\-]{40,}", "[hidden]", text or "")
    return re.sub(r"(access_token|token|key)=\S+", r"\1=[hidden]", text, flags=re.I)


def next_run(now):
    for day in (0, 1):
        for h, m in SCHEDULE_PHT:
            c = (now + dt.timedelta(days=day)).replace(hour=h, minute=m, second=0, microsecond=0)
            if c > now:
                return c
    return None


def fb_message(text: str) -> str:
    m = re.search(r'"message"\s*:\s*"(.*?)"', text or "")
    return m.group(1) if m else ""


# (pattern, short title, plain explanation of the cause, what to do)
ERRORS = [
    (r"^status failed|status failed \(",
     "Reel sent, but its status could not be read",
     "Facebook accepted the video. The step that came after, asking Facebook how far the processing is, was refused, "
     "usually because the token cannot read video details. This is not a posting failure.",
     "Check your Page to confirm the Reel appears. To stop the warning, use a Page token that has pages_read_engagement."),
    (r"allowance for today|daily free allocation|neurons",
     "Cloudflare free limit reached",
     "The voice (narration) is made with Cloudflare Workers AI. Your free allowance of 10,000 neurons for the day was already used up. "
     "Something else on the same Cloudflare account (for example a backend still generating images with Cloudflare) may have spent it.",
     "It resets at 8:00 AM Philippines time and the next scheduled run will try again. Check Cloudflare > AI > Workers AI usage to see what used it."),
    (r"FB_PAGE_TOKEN is not the Page token",
     "Wrong Facebook token type",
     "The saved token belongs to a person, not to your Page, so Facebook would not let it post as the Page.",
     "Get the Page token (from me/accounts in the Graph API Explorer) and update the FB_PAGE_TOKEN secret."),
    (r"FB_PAGE_TOKEN was rejected|OAuthException|\"code\"\s*:\s*190|access token|session has expired|Error validating",
     "Facebook token rejected or expired",
     "Facebook refused the Page access token. It has probably expired, been revoked, or was copied incompletely.",
     "Create a new long-lived Page token and update the FB_PAGE_TOKEN secret in GitHub."),
    (r"\"code\"\s*:\s*(4|17|32|613)\b|rate limit.*facebook|facebook.*rate limit",
     "Facebook rate limit",
     "Facebook temporarily limited how often your app can call it.",
     "Wait a while. The next scheduled run will try again."),
    (r"\"code\"\s*:\s*368|temporarily blocked|policy violation",
     "Facebook blocked the post",
     "Facebook temporarily blocked posting, usually because of a policy or spam check.",
     "Open your Page's Support Inbox or Page Quality in Facebook to see why."),
    (r"\"code\"\s*:\s*(10|200|283)\b|permission|\(#200\)",
     "Facebook permission missing",
     "Your Facebook app or token does not have permission to post videos or Reels to this Page.",
     "In your Meta app, make sure the pages_manage_posts and pages_read_engagement permissions are on the token, then create a new Page token."),
    (r"no matching footage",
     "No matching footage",
     "Some scenes found no stock video whose tags match the topic's subject, so the video was not posted rather than show wrong animals or a placeholder.",
     "Nothing is lost: the topic was not used up and the next run picks another. A broader subject finds footage more easily."),
    (r"Pixabay.*(key was rejected|refused)|PIXABAY_API_KEY",
     "Pixabay key problem",
     "Pixabay rejected the API key or it is missing.",
     "Check the PIXABAY_API_KEY secret in GitHub."),
    (r"Pixabay.*rate limit|Couldn't reach Pixabay",
     "Pixabay unavailable or rate limited",
     "Pixabay did not answer or refused too many requests.",
     "Wait a few minutes. The next run will try again."),
    (r"429|rate limit|Groq|LLM",
     "Writing service limit (Groq)",
     "The service that writes the topic and script (Groq) hit its free usage limit or failed.",
     "Wait and retry. If it keeps happening, check the LLM_API_KEY secret and your Groq usage."),
    (r"timed out waiting for render|backend never woke up",
     "Video making took too long",
     "The video was not finished in time, or the backend on the runner did not start.",
     "Open the run log and the 'Backend log' step to see where it stuck."),
    (r"FFmpeg|video download failed|Couldn't process",
     "Video processing error",
     "FFmpeg could not build the video from the footage and narration.",
     "Open the run log for the FFmpeg message. Run again; it can be one bad clip."),
    (r"No unused topics left",
     "No new topics available",
     "Topics could not be invented (missing NICHE or LLM_API_KEY, or the writing service failed) and the fallback list in topics.txt is used up.",
     "Set the NICHE variable and LLM_API_KEY secret, or add lines to scripts/topics.txt."),
    (r"already posted",
     "Topic already posted",
     "The chosen topic is too similar to one already posted, so it was skipped to avoid repeats.",
     "Nothing to do. The next run picks a new topic."),
    (r"reel (start|upload|publish) failed|video post failed|status.*failed",
     "Facebook upload failed",
     "Facebook did not accept the video during upload or publishing.",
     "Check the Facebook message below and the run log. The next run will try again."),
]


def explain(text: str):
    for pat, title, cause, fix in ERRORS:
        if re.search(pat, text or "", re.I):
            return title, cause, fix
    return ("Unexpected error",
            "The run stopped with a message I do not recognise.",
            "Open the run link below and read the last lines of the log.")


def build() -> str:
    r = load_result()
    log = load_log()
    start, end, posted = parse(r.get("started_at")), parse(r.get("finished_at")), parse(r.get("posted_at"))
    total = (end - start).total_seconds() if start and end else None
    err = r.get("error") or ""
    failed = bool(err) or (OUTCOME not in ("success", "") and not r.get("dry_run"))
    unknown_stop = False
    if failed and not err:  # the script never wrote an error: it crashed or an earlier step failed
        unknown_stop = True
        lines = [l for l in log.splitlines() if l.strip()]
        err = " | ".join(lines[-4:]) if lines else "No log was produced."
    out = []
    now = end or dt.datetime.now(PHT)
    nxt = next_run(now.astimezone(PHT))

    if failed:
        if unknown_stop and not r:
            title = "Run stopped before posting"
            cause = ("A step before the posting script failed (for example installing FFmpeg or the Python packages, "
                     "or a missing secret), so the script never got to write its own report.")
            fix = "Open the run link below and look for the first red step."
        else:
            title, cause, fix = explain(err + "\n" + log[-3000:])
        out.append("❌ VIDEO FAILED TO POST")
        out.append(f"Problem: {title}")
        out.append(f"When: {when(end or start)}")
        out.append(f"Stopped at: {r.get('stage') or 'before the video started'}")
        if r.get("topic"):
            out.append(f"Topic: {r['topic']}")
        if r.get("duration_s"):
            out.append(f"Planned: {r.get('format', '')} of {r['duration_s']} seconds")
        out.append("")
        out.append(f"Cause: {cause}")
        out.append(f"What to do: {fix}")
        fbm = fb_message(err)
        if fbm:
            out.append(f"Facebook said: {fbm}")
        if r.get("video_id"):
            out.append("")
            out.append(f"Note: the video was already sent to Facebook (id {r['video_id']}), so it may be on your Page. Please check before posting again.")
        else:
            out.append("")
            out.append("Nothing was posted to your Page.")
        if total is not None:
            out.append(f"Time spent before failing: {span(total)}")
        out.append("")
        out.append("Technical detail: " + hide_secrets(err)[:600])
    elif r.get("dry_run"):
        out.append("🧪 TEST RUN FINISHED (dry run)")
        out.append("The video was made but NOT posted to Facebook.")
        out.append(f"When: {when(end)}")
        add_details(out, r, total, posted, show_fb=False)
        out += youtube_lines(load_youtube())
        if "WARNING" in log:
            out.append("")
            out.append("⚠️ Some scenes have no matching footage. A real run would not post this video.")
    elif r.get("video_id") or r.get("stage") == "done":
        draft = (r.get("fb_state") or "").upper() != "PUBLISHED"
        out.append("✅ VIDEO POSTED TO YOUR PAGE" if not draft else "📝 VIDEO SAVED AS A DRAFT ON YOUR PAGE (not public)")
        out.append(f"Title: {r.get('title', '-')}")
        out.append(f"Posted: {when(posted or end)}")
        if r.get("page_name"):
            out.append(f"Page: {r['page_name']}")
        add_details(out, r, total, posted, show_fb=True)
        out += youtube_lines(load_youtube())
    else:
        out.append("ℹ️ The run finished but I could not confirm a post. Please open the run and check.")
        out.append(f"When: {when(end or start)}")
        if r.get("topic"):
            out.append(f"Topic: {r['topic']}")

    out.append("")
    if nxt:
        out.append(f"Next scheduled run: {when(nxt)}")
    if RUN_URL:
        out.append(f"Run details: {RUN_URL}")
    return "\n".join(out)


def add_details(out, r, total, posted, show_fb):
    out.append("")
    if not show_fb:
        out.append(f"Title (would be): {r.get('title', '-')}")
    out.append(f"Topic: {r.get('topic', '-')}")
    if r.get("subject"):
        out.append(f"Main subject: {r['subject']}")
    out.append(f"Format: {r.get('format', '-')}, {r.get('duration_s', '?')} seconds ({r.get('duration_s', 0) / 60:.1f} min) planned length")
    if show_fb:
        state = "Published (public)" if (r.get("fb_state") or "").upper() == "PUBLISHED" else "Draft (only you can see it)"
        out.append(f"Status: {state}")
        if r.get("fb_status"):
            out.append(f"Facebook processing: {hide_secrets(r['fb_status'])[:200]}")
        vid = r.get("video_id")
        if vid:
            link = f"https://www.facebook.com/reel/{vid}" if r.get("format") == "Reel" else f"https://www.facebook.com/watch/?v={vid}"
            out.append(f"Video id: {vid}")
            out.append(f"Link: {link}")
    out.append("")
    out.append("Time it took:")
    out.append(f"  Making the video: {span(r.get('render_seconds'))}")
    if show_fb:
        out.append(f"  Uploading to Facebook: {span(r.get('upload_seconds'))}")
    out.append(f"  Whole run: {span(total)}")
    if r.get("video_mb") is not None:
        out.append(f"File size: {r['video_mb']} MB")
    if r.get("scenes"):
        out.append("")
        out.append(f"Footage used ({len(r['scenes'])} scene{'s' if len(r['scenes']) != 1 else ''}):")
        for s in r["scenes"][:12]:
            out.append(f"  {s['scene']}: {s['footage']}")
    tags = r.get("hashtags") or []
    if tags:
        out.append("")
        out.append("Hashtags: " + " ".join(tags))
    cap = (r.get("caption") or "").split("\n\n")
    if len(cap) >= 2:
        out.append("Caption: " + cap[1][:350])
    if show_fb and r.get("earlier_posts") is not None:
        out.append("")
        out.append(f"Total videos posted so far: {r['earlier_posts'] + 1}")


def send(text: str) -> None:
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for c in chunks:
        data = urllib.parse.urlencode({"chat_id": CHAT, "text": c}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendMessage", data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                print("telegram status:", resp.status)
        except Exception as e:  # never fail the workflow because of a notification
            print("telegram failed:", hide_secrets(str(e)))


if __name__ == "__main__":
    try:
        msg = build()
    except Exception as e:  # last resort, so a report always goes out
        msg = f"ℹ️ The run ended but the report could not be built ({type(e).__name__}). Open the run: {RUN_URL}"
    if PRINT_ONLY:
        print(msg)
    elif not TOKEN or not CHAT:
        print("Telegram secrets not set, skipping message.")
    else:
        send(msg)
    sys.exit(0)