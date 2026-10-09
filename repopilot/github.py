"""Fetch a public GitHub repo into a RepoSnapshot using the unauthenticated REST API.

Kept separate from tools.py so the tools stay pure and testable. Network failures degrade
gracefully into a helpful message rather than a stack trace (consistent with the product's
plain-language-error philosophy).
"""
from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.request
from typing import Optional

from .tools import RepoSnapshot

_API = "https://api.github.com"
_MANIFESTS = (
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "go.mod",
    "Cargo.toml",
    "Gemfile",
    "pom.xml",
)


def parse_repo_url(url: str) -> Optional[tuple[str, str]]:
    """Return (owner, repo) from a GitHub URL or 'owner/repo' shorthand, else None."""
    url = url.strip()
    if url.endswith(".git"):
        url = url[:-4]
    m = re.search(r"github\.com[/:]([\w.-]+)/([\w.-]+)", url)
    if m:
        return m.group(1), m.group(2)
    m = re.fullmatch(r"([\w.-]+)/([\w.-]+)", url)
    if m:
        return m.group(1), m.group(2)
    return None


def parse_username(url: str) -> Optional[str]:
    """Return a bare GitHub username from input like 'simplynadaf' or a profile URL, else None.

    Only matches when the input is a SINGLE path segment (a user), not 'owner/repo'.
    """
    s = url.strip().rstrip("/")
    m = re.fullmatch(r"https?://github\.com/([\w-]+)", s)
    if m:
        return m.group(1)
    if re.fullmatch(r"@?[\w-]+", s) and "/" not in s:
        return s.lstrip("@")
    return None


class _Resp:
    """Tiny response wrapper so callers can use .ok / .status_code / .json()."""

    def __init__(self, status: int, data: bytes):
        self.status_code = status
        self._data = data

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        return json.loads(self._data.decode("utf-8"))


def _get(url: str, timeout: int = 15) -> _Resp:
    """GET a URL with the stdlib. No third-party deps so it runs cleanly on Lambda py3.12.

    If a GITHUB_TOKEN (or GH_TOKEN) env var is set, it is sent as a bearer token, which raises
    the GitHub rate limit from 60/hour (unauthenticated) to 5000/hour. Optional: works fine without.
    """
    import os

    headers = {"Accept": "application/vnd.github+json", "User-Agent": "RepoPilot"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _Resp(r.status, r.read())
    except urllib.error.HTTPError as e:
        return _Resp(e.code, e.read() if hasattr(e, "read") else b"")


def fetch_snapshot(url: str) -> RepoSnapshot:
    """Build a RepoSnapshot from a public repo. Raises ValueError with a friendly message."""
    parsed = parse_repo_url(url)
    if not parsed:
        raise ValueError(
            f"'{url}' doesn't look like a GitHub repo. Try a URL like "
            "https://github.com/owner/repo or just owner/repo."
        )
    owner, repo = parsed

    meta = _get(f"{_API}/repos/{owner}/{repo}")
    if meta.status_code == 404:
        raise ValueError(f"Couldn't find {owner}/{repo}. Is it public and spelled correctly?")
    if meta.status_code == 403:
        raise ValueError("GitHub rate-limited this request. Wait a minute and try again.")
    if not meta.ok:
        raise ValueError(f"GitHub returned HTTP {meta.status_code} for {owner}/{repo}.")
    m = meta.json()
    default_branch = m.get("default_branch", "main")

    # Repo metadata for the trust read.
    repo_meta = {
        "created_at": m.get("created_at", ""),
        "pushed_at": m.get("pushed_at", ""),
        "stargazers": m.get("stargazers_count", 0),
        "forks": m.get("forks_count", 0),
        "open_issues": m.get("open_issues_count", 0),
        "archived": m.get("archived", False),
        "license": (m.get("license") or {}).get("spdx_id") or (m.get("license") or {}).get("name") or "",
        "owner": (m.get("owner") or {}).get("login", owner),
        "owner_type": (m.get("owner") or {}).get("type", ""),
        "description": m.get("description", ""),
    }

    # File tree (one call, recursive)
    tree_resp = _get(f"{_API}/repos/{owner}/{repo}/git/trees/{default_branch}?recursive=1")
    file_paths: list[str] = []
    if tree_resp.ok:
        file_paths = [n["path"] for n in tree_resp.json().get("tree", []) if n.get("type") == "blob"]

    # README
    readme_text = ""
    r = _get(f"{_API}/repos/{owner}/{repo}/readme")
    if r.ok:
        content = r.json().get("content", "")
        try:
            readme_text = base64.b64decode(content).decode("utf-8", errors="replace")
        except Exception:
            readme_text = ""

    # Manifests at repo root
    manifests: dict[str, str] = {}
    root = {p for p in file_paths if "/" not in p}
    for name in _MANIFESTS:
        if name in root:
            c = _get(f"{_API}/repos/{owner}/{repo}/contents/{name}")
            if c.ok:
                try:
                    manifests[name] = base64.b64decode(c.json().get("content", "")).decode(
                        "utf-8", errors="replace"
                    )
                except Exception:
                    pass

    # Install scripts at root (for the danger scan): any *.sh, install.*, Makefile.
    scripts: dict[str, str] = {}
    script_names = [p for p in root if p.endswith(".sh") or p in ("Makefile", "install.py", "setup.sh")][:4]
    for name in script_names:
        c = _get(f"{_API}/repos/{owner}/{repo}/contents/{name}")
        if c.ok:
            try:
                scripts[name] = base64.b64decode(c.json().get("content", "")).decode("utf-8", errors="replace")
            except Exception:
                pass

    return RepoSnapshot(
        name=repo, file_paths=file_paths, readme_text=readme_text,
        manifests=manifests, meta=repo_meta, scripts=scripts,
    )


def fetch_user_profile(username: str) -> dict:
    """Fetch a developer's public profile + their top repos (ranked by stars, then forks).

    Returns a dict ready to narrate. Raises ValueError with a friendly message on failure.
    GitHub's REST API cannot sort repos by stars, so we pull up to 100 and rank client-side;
    for very prolific users this is the top 100, which we label honestly.
    """
    from datetime import datetime, timezone

    u = username.strip().lstrip("@")
    resp = _get(f"{_API}/users/{u}")
    if resp.status_code == 404:
        raise ValueError(f"No GitHub user named '{u}'. Check the spelling.")
    if resp.status_code == 403:
        raise ValueError("GitHub rate-limited this request. Wait a minute and try again.")
    if not resp.ok:
        raise ValueError(f"GitHub returned HTTP {resp.status_code} for user '{u}'.")
    p = resp.json()

    repos_resp = _get(f"{_API}/users/{u}/repos?per_page=100&sort=updated&type=owner")
    repos = repos_resp.json() if repos_resp.ok else []
    # Exclude forks from "best projects" so we show their own work.
    own = [r for r in repos if not r.get("fork")]
    total_stars = sum(r.get("stargazers_count", 0) for r in own)
    total_forks = sum(r.get("forks_count", 0) for r in own)

    by_stars = sorted(own, key=lambda r: r.get("stargazers_count", 0), reverse=True)
    top = [
        {
            "name": r.get("name", ""),
            "stars": r.get("stargazers_count", 0),
            "forks": r.get("forks_count", 0),
            "language": r.get("language") or "",
            "description": (r.get("description") or "").strip(),
            "url": r.get("html_url", ""),
            "archived": r.get("archived", False),
        }
        for r in by_stars[:6]
    ]

    created = p.get("created_at", "")
    year = created[:4] if created else ""
    # languages across top repos
    langs: dict[str, int] = {}
    for r in own:
        lang = r.get("language")
        if lang:
            langs[lang] = langs.get(lang, 0) + 1
    top_langs = [k for k, _ in sorted(langs.items(), key=lambda kv: kv[1], reverse=True)[:3]]

    return {
        "kind": "profile",
        "login": p.get("login", u),
        "name": p.get("name") or p.get("login", u),
        "bio": (p.get("bio") or "").strip(),
        "company": (p.get("company") or "").strip(),
        "location": (p.get("location") or "").strip(),
        "avatar": p.get("avatar_url", ""),
        "followers": p.get("followers", 0),
        "following": p.get("following", 0),
        "public_repos": p.get("public_repos", 0),
        "since": year,
        "html_url": p.get("html_url", f"https://github.com/{u}"),
        "total_stars": total_stars,
        "total_forks": total_forks,
        "top_languages": top_langs,
        "top_repos": top,
        "repos_counted": len(own),
        "truncated": p.get("public_repos", 0) > 100,
    }
