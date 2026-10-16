"""Disk budget. Keeps everything under STORAGE_DIR at or below STORAGE_LIMIT_MB.

Eviction order when space is needed: leftover temp files, cached searches, cached images, then the oldest finished
projects (their video is deleted and the project is marked "expired"). The project being generated is never evicted.
Hard links are counted once.
"""
import os
import shutil
import time
from pathlib import Path

from . import db
from .config import settings
from .errors import PipelineError

MB = 1024 * 1024
INTERMEDIATE = ("_clip.mp4", "_bg.png", "_a.png", "_b.png", "_text.png")


def limit_bytes() -> int:
    return settings.storage_limit_mb * MB


def _files(root: Path):
    seen = set()
    for dp, _, names in os.walk(root):
        for n in names:
            p = Path(dp) / n
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_ino:
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
            yield p, st


def usage_bytes() -> int:
    return sum(st.st_size for _, st in _files(settings.storage_dir)) if settings.storage_dir.exists() else 0


def dir_bytes(path: Path) -> int:
    return sum(st.st_size for _, st in _files(path)) if path.exists() else 0


def reserve_bytes(duration: int) -> int:
    """Worst-case space one job needs: trimmed clips + narration + final MP4 (bitrate-capped)."""
    return int((60 + 0.6 * duration) * MB)


def _drop_oldest_files(root: Path, over: int) -> int:
    freed = 0
    if not root.exists():
        return 0
    for p, st in sorted(_files(root), key=lambda t: t[1].st_mtime):
        if freed >= over:
            break
        try:
            p.unlink()
            freed += st.st_size
        except OSError:
            pass
    return freed


def enforce(protect: set[str] = frozenset(), reserve: int = 0) -> bool:
    """Free space until usage + reserve fits the limit. Returns True when it does."""
    def over() -> int:
        return usage_bytes() + reserve - limit_bytes()

    if over() <= 0:
        return True
    tmp = settings.tmp_dir
    if tmp.exists():  # leftovers from crashed jobs
        for d in tmp.iterdir():
            if d.is_dir() and d.name not in protect and time.time() - d.stat().st_mtime > 300:
                shutil.rmtree(d, ignore_errors=True)
    if over() > 0:
        _drop_oldest_files(settings.storage_dir / "cache" / "search", over())
    if over() > 0:
        _drop_oldest_files(settings.cache_dir, over())
    if over() > 0 and settings.projects_dir.exists():
        dirs = [d for d in settings.projects_dir.iterdir() if d.is_dir() and d.name not in protect]
        for d in sorted(dirs, key=lambda d: d.stat().st_mtime):
            if over() <= 0:
                break
            shutil.rmtree(d, ignore_errors=True)
            p = db.get_project(d.name)
            if p:
                p.update(video_ready=False, status="expired")
                db.save_project(p)
    return over() <= 0


def guard(protect: set[str]) -> None:
    """Called between pipeline steps: trim if needed, fail the job rather than exceed the limit."""
    if usage_bytes() > limit_bytes() and not enforce(protect):
        raise PipelineError(f"The {settings.storage_limit_mb} MB storage limit was reached. Delete old videos or raise "
                            "STORAGE_LIMIT_MB in backend/.env.")


def cleanup_intermediates(pdir: Path) -> None:
    for f in pdir.glob("scene_*"):
        if f.name.endswith(INTERMEDIATE):
            f.unlink(missing_ok=True)
