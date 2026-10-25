"""When the daily video goes out: the best Shorts / Reels times per weekday.

Times are Philippine time (UTC+8), taken from the peak-times table (the start of each peak window). Change SLOTS to
move them, then run `python scripts/post_schedule.py` and paste its output over the `cron:` lines in
.github/workflows/daily-post.yml (a test fails if the two disagree).

How it works: GitHub starts the job LEAD_MIN minutes before the slot so there is time to make the video, and
daily_post.py holds the finished video until the slot minute, then posts. Manual runs post straight away.
"""
import datetime as dt

PHT = dt.timezone(dt.timedelta(hours=8))
LEAD_MIN = 25  # render takes ~10 minutes; the rest is a buffer for GitHub's cron delay

# weekday (Monday = 0) -> post times (hour, minute) in Philippine time
SLOTS: dict[int, list[tuple[int, int]]] = {
    0: [(15, 30), (19, 0)],           # Mon  3-5 pm, 7 pm
    1: [(16, 0)],                     # Tue  3-6 pm
    2: [(15, 30), (19, 30)],          # Wed  3-5 pm, 7-9 pm
    3: [(17, 0)],                     # Thu  4-7 pm
    4: [(16, 0), (19, 0)],            # Fri  4 pm, 6 pm, 7 pm (best day)
    5: [(10, 30), (17, 30)],          # Sat  10 am-1 pm, 5-7 pm
    6: [(10, 30), (18, 0)],           # Sun  10 am-1 pm, 6 pm
}


def cron_lines() -> list[str]:
    """One cron expression (UTC) per distinct start time, with the weekdays that share it."""
    groups: dict[tuple[int, int], list[int]] = {}
    ref = dt.datetime(2026, 10, 26, tzinfo=PHT)  # a Monday; only used to do the time-zone arithmetic
    for wd, slots in SLOTS.items():
        for h, m in slots:
            start = (ref + dt.timedelta(days=wd, hours=h, minutes=m - LEAD_MIN)).astimezone(dt.timezone.utc)
            groups.setdefault((start.minute, start.hour), []).append((start.weekday() + 1) % 7)  # cron: Sunday = 0
    return [f'{m} {h} * * {",".join(str(d) for d in sorted(set(days)))}' for (m, h), days in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0]))]


def targets_around(now: dt.datetime) -> list[dt.datetime]:
    now = now.astimezone(PHT)
    out = []
    for back in (-1, 0, 1):
        day = now + dt.timedelta(days=back)
        for h, m in SLOTS.get(day.weekday(), []):
            out.append(day.replace(hour=h, minute=m, second=0, microsecond=0))
    return sorted(out)


def current_target(now: dt.datetime, lead_min: int = LEAD_MIN, slack_min: int = 20) -> dt.datetime | None:
    """The slot this run was started for: the next one at most lead+slack minutes away (or one that began up to 10 minutes
    ago). None when the run was not started for a slot, for example a manual run."""
    best = None
    for t in targets_around(now):
        delta = (t - now.astimezone(PHT)).total_seconds() / 60
        if -10 <= delta <= lead_min + slack_min:
            best = t if best is None or abs(delta) < abs((best - now.astimezone(PHT)).total_seconds() / 60) else best
    return best


def next_post(now: dt.datetime) -> dt.datetime | None:
    """The next slot after `now`, for the 'Next scheduled run' line of the report."""
    now = now.astimezone(PHT)
    for d in range(0, 8):
        day = now + dt.timedelta(days=d)
        for h, m in SLOTS.get(day.weekday(), []):
            t = day.replace(hour=h, minute=m, second=0, microsecond=0)
            if t > now:
                return t
    return None


if __name__ == "__main__":
    print("    # paste under `schedule:` in .github/workflows/daily-post.yml (UTC)")
    for line in cron_lines():
        print(f'    - cron: "{line}"')
