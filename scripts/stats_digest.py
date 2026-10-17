#!/usr/bin/env python3
"""Send a Telegram digest of reactions, comments and views for your recent Facebook posts.

Runs on a schedule (see .github/workflows/stats-digest.yml). It remembers the last numbers in data/stats.json,
so it can say what is NEW. It only sends a message when something changed, plus one short message a day so
you know it is alive. If Facebook refuses to give the numbers, you get ONE message explaining why.

Env: FB_PAGE_TOKEN, FB_PAGE_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Optional: STATS_DAYS (how far back to look, default 14), STATS_MAX (posts to show, default 6), STATS_PRINT=1 (print only).
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

GRAPH = os.environ.get("GRAPH_BASE", "https://graph.facebook.com/v21.0")
TG_API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
TOKEN = os.environ.get("FB_PAGE_TOKEN", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
DAYS = int(os.environ.get("STATS_DAYS", "14") or 14)
MAX_POSTS = int(os.environ.get("STATS_MAX", "6") or 6)
PRINT_ONLY = os.environ.get("STATS_PRINT") == "1"
HISTORY = pathlib.Path(os.environ.get("HISTORY_FILE", "data/posted.json"))
STATE = pathlib.Path(os.environ.get("STATS_FILE", "data/stats.json"))
PHT = dt.timezone(dt.timedelta(hours=8))  # Philippines time

# Reel / video numbers Facebook may offer. Each is tried on its own; the ones Facebook refuses are skipped.
INSIGHTS = {"plays": ["blue_reels_play_count", "post_video_views", "total_video_views"],
            "reach": ["post_impressions_unique"],
            "avg_watch_ms": ["post_video_avg_time_watched"]}


def get(path: str, params: dict) -> tuple[int, dict]:
    url = f"{GRAPH}/{path}?" + urllib.parse.urlencode({**params, "access_token": TOKEN})
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": {"message": f"network: {e}"}}


def err_text(body: dict) -> str:
    e = body.get("error") or {}
    return re.sub(r"[A-Za-z0-9_\-]{40,}", "[hidden]", f"({e.get('code', '?')}) {e.get('message', 'unknown error')}")[:200]


def load_json(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def insight_value(vid: str, metric: str):
    code, body = get(f"{vid}/video_insights", {"metric": metric})
    if code != 200:
        return None
    try:
        v = body["data"][0]["values"][0]["value"]
    except (KeyError, IndexError, TypeError):
        return None
    return v if isinstance(v, (int, float)) else None


def fetch(vid: str) -> tuple[dict | None, str]:
    """Numbers for one video, or (None, reason)."""
    code, body = get(vid, {"fields": "permalink_url,created_time,reactions.summary(total_count).limit(0),"
                                     "comments.summary(total_count).limit(0)"})
    if code != 200:
        return None, err_text(body)
    out = {"reactions": (body.get("reactions") or {}).get("summary", {}).get("total_count", 0),
           "comments": (body.get("comments") or {}).get("summary", {}).get("total_count", 0),
           "url": body.get("permalink_url", "")}
    if out["url"].startswith("/"):
        out["url"] = "https://www.facebook.com" + out["url"]
    for key, metrics in INSIGHTS.items():
        for m in metrics:
            v = insight_value(vid, m)
            if v is not None:
                out[key] = v
                break
    return out, ""


def when(iso: str) -> str:
    try:
        d = dt.datetime.fromisoformat(iso).astimezone(PHT)
        return d.strftime("%d %b, %I:%M %p").lstrip("0").replace(" 0", " ")
    except (ValueError, TypeError):
        return "?"


def delta(new, old) -> str:
    return f" (+{int(new - old)})" if old is not None and new > old else ""


def num(n) -> str:
    return f"{int(n):,}"


def plural(n, word: str) -> str:
    return f"{num(n)} {word}{'' if int(n) == 1 else 's'}"


def send(text: str) -> None:
    if PRINT_ONLY:
        print(text)
        return
    if not TG_TOKEN or not TG_CHAT:
        print("Telegram secrets not set, skipping message.")
        return
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text[:3900]}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{TG_API}/bot{TG_TOKEN}/sendMessage", data=data), timeout=30) as r:
            print("telegram status:", r.status)
    except Exception as e:  # never fail the workflow because of a message
        print("telegram failed:", re.sub(r"bot[0-9]+:[A-Za-z0-9_\-]+", "bot[hidden]", str(e)))


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    state = load_json(STATE, {})
    old = state.get("posts", {})
    if not TOKEN:
        print("FB_PAGE_TOKEN not set, nothing to do.")
        return 0
    cutoff = now - dt.timedelta(days=DAYS)
    posts = []
    for p in load_json(HISTORY, {}).get("posts", []):
        try:
            at = dt.datetime.fromisoformat(p.get("at", ""))
        except ValueError:
            continue
        if p.get("video_id") and (p.get("state") or "PUBLISHED").upper() == "PUBLISHED" and at >= cutoff:
            posts.append({**p, "_at": at})
    posts = sorted(posts, key=lambda p: p["_at"], reverse=True)[:MAX_POSTS]
    if not posts:
        print("No published posts in the last", DAYS, "days.")
        return 0

    results, errors = [], []
    for p in posts:
        data, why = fetch(p["video_id"])
        (results if data else errors).append((p, data or why))

    # Facebook refused every request: say why, once, instead of every few hours.
    if not results:
        reason = errors[0][1]
        if state.get("last_error") != reason:
            send("⚠️ Facebook numbers unavailable\n"
                 f"Facebook refused to give reactions and views: {reason}\n\n"
                 "Likely cause: the Page token is missing the pages_read_engagement permission, or has expired. "
                 "Posting is not affected. Create a new Page token with that permission and update the FB_PAGE_TOKEN secret.")
        state.update(last_error=reason)
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        return 0

    new_state, lines, tot_new = {}, [], {"reactions": 0, "comments": 0, "plays": 0}
    tot_all = {"reactions": 0, "comments": 0}
    for i, (p, d) in enumerate(results, 1):
        vid = p["video_id"]
        prev = old.get(vid)
        new_state[vid] = {k: d[k] for k in ("reactions", "comments", "plays", "reach", "avg_watch_ms") if k in d}
        for k in ("reactions", "comments", "plays"):
            if k in d and prev is not None and d[k] > prev.get(k, 0):
                tot_new[k] += d[k] - prev.get(k, 0)
        tot_all["reactions"] += d["reactions"]
        tot_all["comments"] += d["comments"]
        title = (p.get("title") or p.get("topic") or "Untitled")[:70]
        stats = [f"👍 {plural(d['reactions'], 'reaction')}{delta(d['reactions'], (prev or {}).get('reactions'))}",
                 f"💬 {plural(d['comments'], 'comment')}{delta(d['comments'], (prev or {}).get('comments'))}"]
        if "plays" in d:
            stats.append(f"▶ {plural(d['plays'], 'play')}{delta(d['plays'], (prev or {}).get('plays'))}")
        extra = []
        if "reach" in d:
            extra.append(f"reach {num(d['reach'])}")
        if "avg_watch_ms" in d:
            extra.append(f"average watch {d['avg_watch_ms'] / 1000:.1f} s")
        lines += [f"{i}) {title}", f"   posted {when(p.get('at'))}", "   " + "   ".join(stats)]
        if extra:
            lines.append("   " + ", ".join(extra))
        if d.get("url"):
            lines.append(f"   {d['url']}")
        lines.append("")

    first_run = not old
    changed = any(tot_new.values())
    last_sent = state.get("last_sent")
    due_daily = not last_sent or (now - dt.datetime.fromisoformat(last_sent)) >= dt.timedelta(hours=24)
    if first_run:
        head = "Tracking started. These are your current numbers."
    elif changed:
        bits = [f"+{plural(tot_new['reactions'], 'reaction')}" if tot_new["reactions"] else "",
                f"+{plural(tot_new['comments'], 'comment')}" if tot_new["comments"] else "",
                f"+{plural(tot_new['plays'], 'play')}" if tot_new["plays"] else ""]
        head = "New since the last check: " + ", ".join(b for b in bits if b)
    else:
        head = "No new activity since the last check."

    skipped = f"\n(Could not read {len(errors)} post(s): {errors[0][1]})" if errors else ""
    text = (f"📊 Facebook update, {now.astimezone(PHT).strftime('%a %d %b, %I:%M %p').replace(' 0', ' ')} (Philippines time)\n"
            f"{head}\n\n" + "\n".join(lines) +
            f"\nTotal on these {len(results)} posts: {plural(tot_all['reactions'], 'reaction')}, {plural(tot_all['comments'], 'comment')}{skipped}")
    if first_run or changed or due_daily:
        send(text)
        state["last_sent"] = now.isoformat(timespec="seconds")
    else:
        print("No change and a message was sent less than 24 hours ago, staying quiet.")
    state.update(posts=new_state, last_error="")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # the digest must never turn a run red
        print("stats digest error:", type(e).__name__, e)
        sys.exit(0)
