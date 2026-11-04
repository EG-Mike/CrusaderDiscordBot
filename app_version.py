"""
Which build of the bot is running: commit, its title, build time and the
GitHub repository it came from.

Docker images have no .git folder, so the GitHub Actions build passes these
in as environment variables (APP_VERSION, APP_COMMIT_TITLE, APP_BUILT_AT,
APP_REPOSITORY - see the Dockerfile). A git checkout (e.g. the live bot
under AMP) reads them from git instead. Anything unknown is None.
"""

import os
import re
import subprocess
from typing import List, Optional

ROOT = os.path.dirname(os.path.abspath(__file__))


def _git(*args) -> Optional[str]:
    try:
        result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _repository_from_remote(url: Optional[str]) -> Optional[str]:
    """'https://user:token@github.com/Owner/Repo.git' -> 'Owner/Repo'. Only
    the owner/repo path is kept, so credentials in a remote URL never leak."""
    if not url:
        return None
    match = re.search(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url)
    return f"{match[1]}/{match[2]}" if match else None


def _load() -> dict:
    if os.environ.get("APP_VERSION"):
        return {
            "version": os.environ["APP_VERSION"][:12],
            "title": os.environ.get("APP_COMMIT_TITLE") or None,
            "built_at": os.environ.get("APP_BUILT_AT") or None,
            "repository": os.environ.get("APP_REPOSITORY") or None,
            "source": "image",
        }
    return {
        "version": _git("rev-parse", "--short", "HEAD") or "unknown",
        "title": _git("log", "-1", "--format=%s"),
        "built_at": _git("log", "-1", "--format=%cI"),
        "repository": _repository_from_remote(_git("remote", "get-url", "origin")),
        "source": "git" if _git("rev-parse", "HEAD") else "unknown",
    }


INFO = _load()
VERSION = INFO["version"]


def commit_url(version: Optional[str]) -> Optional[str]:
    if not INFO["repository"] or not version or version in ("unknown", "dev"):
        return None
    return f"https://github.com/{INFO['repository']}/commit/{version}"


def compare_url(old: Optional[str], new: Optional[str]) -> Optional[str]:
    if not INFO["repository"] or not old or not new or "unknown" in (old, new) or "dev" in (old, new):
        return None
    return f"https://github.com/{INFO['repository']}/compare/{old}...{new}"


def commits_between(old: str, new: str, limit: int = 20) -> List[dict]:
    """Commit titles between two versions, newest first - only possible in a
    git checkout that has both commits (empty otherwise)."""
    if INFO["source"] != "git" or not re.fullmatch(r"[0-9a-f]{4,40}", old or "") \
            or not re.fullmatch(r"[0-9a-f]{4,40}", new or ""):
        return []
    output = _git("log", f"--max-count={limit}", "--format=%h%x09%s", f"{old}..{new}")
    return [{"version": line.split("\t", 1)[0], "title": line.split("\t", 1)[-1]}
            for line in (output or "").splitlines()]
