"""Unit tests for RepoPilot's pure-Python tools.

Run with:  python -m pytest -q   (or)   python tests/test_tools.py
These tests prove the core logic works without AWS or network access, which is also the
evidence behind the article's 'how I know it worked' claims.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from repopilot.tools import (  # noqa: E402
    RepoSnapshot,
    assess_repo_trust,
    build_first_run_plan,
    check_readme_against_reality,
    detect_ecosystems,
    explain_error,
    generate_env_example,
    preflight_requirements,
    scan_setup_for_danger,
)


def _node_repo() -> RepoSnapshot:
    return RepoSnapshot(
        name="cool-app",
        file_paths=["package.json", "src/index.js", ".env.example", "Dockerfile"],
        readme_text="Run `npm install` then `npm start`. Config lives in `.env`.",
        manifests={"package.json": '{"engines": {"node": ">=20"}}'},
    )


def test_detect_single_ecosystem():
    assert [e["ecosystem"] for e in detect_ecosystems(_node_repo())] == ["node"]


def test_detect_ignores_nested_manifests():
    # A C project with a stray requirements.txt deep in the tree must NOT read as Python.
    snap = RepoSnapshot(
        name="kernel",
        file_paths=["Makefile", "kernel/main.c", "scripts/tools/requirements.txt", "tools/perf/package.json"],
        readme_text="Build with make.",
        manifests={},  # nothing at root
    )
    assert detect_ecosystems(snap) == []


def test_first_run_plan_is_ordered_and_mentions_env_and_docker():
    plan = build_first_run_plan(_node_repo())
    joined = " | ".join(plan["steps"])
    assert plan["steps"][0].startswith("git clone")
    assert ".env" in joined
    assert "npm install" in joined
    assert "Docker" in joined or "docker" in joined
    assert plan["confidence"] == "high"


def test_first_run_plan_handles_unknown_repo():
    snap = RepoSnapshot(name="mystery", file_paths=["LICENSE"], readme_text="")
    plan = build_first_run_plan(snap)
    assert plan["confidence"] == "low"
    assert plan["ecosystems"] == []


def test_readme_reality_flags_missing_package_json():
    snap = RepoSnapshot(
        name="x",
        file_paths=["main.py"],
        readme_text="First run `npm install`.",
        manifests={},
    )
    result = check_readme_against_reality(snap)
    assert result["ok"] is False
    assert any("package.json" in i for i in result["issues"])


def test_readme_reality_passes_clean_repo():
    assert check_readme_against_reality(_node_repo())["ok"] is True


def test_generate_env_example_has_placeholders_and_hints():
    body = generate_env_example(["PORT", "DATABASE_URL", "MY_SECRET"])
    assert "PORT=3000" in body
    assert "DATABASE_URL=" in body
    assert "MY_SECRET=" in body
    assert "never commit secrets" in body


def test_preflight_reads_node_engine():
    reqs = preflight_requirements(_node_repo())["requirements"]
    assert {"tool": "node", "required": ">=20", "check": "node --version"} in reqs


def test_explain_error_recognizes_module_not_found():
    r = explain_error("python main.py", "Traceback ... ModuleNotFoundError: No module named 'flask'")
    assert r["recognized"] is True
    assert "dependency" in r["cause"].lower()
    assert r["fix"]


def test_explain_error_recognizes_port_in_use():
    r = explain_error("npm start", "Error: listen EADDRINUSE: address already in use :::3000")
    assert r["recognized"] is True
    assert "port" in r["cause"].lower()


def test_explain_error_unknown_is_graceful():
    r = explain_error("make", "some weird output nobody has seen")
    assert r["recognized"] is False
    assert r["tone"]  # still kind, never a bare failure


# ---- trust & safety preflight (the "found it in a reel" checks) ----
def test_trust_flags_new_repo_with_many_stars():
    from datetime import datetime, timezone, timedelta
    recent = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    meta = {"created_at": recent, "pushed_at": recent, "stargazers": 30000,
            "archived": False, "license": "MIT", "owner": "x", "owner_type": "User"}
    r = assess_repo_trust(meta)
    assert any("star" in f.lower() for f in r["flags"])
    assert r["grade"] in ("C", "D", "B")  # docked for the mismatch


def test_trust_flags_archived_and_no_license():
    meta = {"created_at": "2019-01-01T00:00:00Z", "pushed_at": "2020-01-01T00:00:00Z",
            "stargazers": 50, "archived": True, "license": "", "owner": "x", "owner_type": "User"}
    r = assess_repo_trust(meta)
    assert any("archived" in f.lower() for f in r["flags"])
    assert any("license" in f.lower() for f in r["flags"])


def test_trust_clean_repo_scores_well():
    from datetime import datetime, timezone, timedelta
    old = (datetime.now(timezone.utc) - timedelta(days=1200)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    meta = {"created_at": old, "pushed_at": recent, "stargazers": 8000,
            "archived": False, "license": "Apache-2.0", "owner": "pallets", "owner_type": "Organization"}
    r = assess_repo_trust(meta)
    assert r["flags"] == []
    assert r["grade"] in ("A", "B")


def test_danger_scan_flags_curl_pipe_bash():
    snap = RepoSnapshot(name="x", readme_text="Install: `curl https://x.sh | bash`")
    r = scan_setup_for_danger(snap)
    assert r["safe_looking"] is False
    assert any("curl" in f.lower() or "remote script" in f.lower() for f in r["findings"])


def test_danger_scan_flags_postinstall_hook():
    snap = RepoSnapshot(name="x", manifests={"package.json": '{"scripts":{"postinstall":"node x.js"}}'})
    r = scan_setup_for_danger(snap)
    assert r["safe_looking"] is False
    assert any("postinstall" in f.lower() for f in r["findings"])


def test_danger_scan_clean_repo_passes():
    snap = RepoSnapshot(name="x", readme_text="Run `pip install -r requirements.txt` then `python app.py`.")
    r = scan_setup_for_danger(snap)
    assert r["safe_looking"] is True
    assert r["findings"] == []


if __name__ == "__main__":
    # Minimal runner so the suite works even without pytest installed.
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                passed += 1
                print(f"PASS {name}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {name}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
