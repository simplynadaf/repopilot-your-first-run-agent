"""RepoPilot CLI, the friendly first-run experience.

Usage:
    python -m repopilot.cli <github-repo-url> [--offline] [--region us-east-1]

By default it uses the Strands + Bedrock agent. With --offline (or if the agent can't be
built), it falls back to the deterministic pure-Python onboarding so you always get value.
"""
from __future__ import annotations

import argparse
import sys
import time

from . import agent as agent_mod
from . import github


# ---- tiny, dependency-free styling (delightful first impression) ---------------------
def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if sys.stdout.isatty() else text


def _bold(t: str) -> str:
    return _c(t, "1")


def _green(t: str) -> str:
    return _c(t, "32")


def _yellow(t: str) -> str:
    return _c(t, "33")


def _red(t: str) -> str:
    return _c(t, "31")


def _dim(t: str) -> str:
    return _c(t, "2")


def _hello(repo_name: str) -> None:
    print()
    print(_bold("🧭 RepoPilot") + _dim(", let's get you from clone to running."))
    print(_dim(f"   Looking at: {repo_name}"))
    print()


def _render_offline(repo_name: str, result: dict) -> None:
    # --- RepoPilot Score FIRST (the inspector verdict) ---
    sc = result.get("scorecard")
    if sc:
        grade = sc["grade"]
        gcolor = _green if grade in ("A", "B") else _yellow if grade == "C" else _red
        bar_n = int(sc["overall"] / 5)
        bar = gcolor("█" * bar_n) + _dim("░" * (20 - bar_n))
        print(_bold("RepoPilot Score  ") + gcolor(f"{sc['overall']}/100  {grade} ({sc['label']})"))
        print("  " + bar)
        print("  " + _dim(sc["verdict"]))
        print()
        print(_bold("  Dimensions"))
        for d in sc["dimensions"]:
            dn = int(d["score"] / 10)
            dc = _green if d["score"] >= 75 else _yellow if d["score"] >= 50 else _red
            dbar = dc("▰" * dn) + _dim("▱" * (10 - dn))
            print(f"    {d['name']:<13} {dbar} {d['score']:>3}  {_dim('(' + str(d['weight']) + '%)')}")
        if sc["critical_issues"]:
            print()
            print(_bold("  Critical issues"))
            for c in sc["critical_issues"]:
                sev = _red(f"[{c['severity']}]") if c["severity"] in ("CRITICAL", "HIGH") else _yellow(f"[{c['severity']}]")
                print(f"    {sev} {c['issue']} {_dim('(' + str(c['impact']) + ')')}")
        print()

    plan = result["plan"]
    eco = ", ".join(plan["ecosystems"]) or "unknown stack"
    print(_bold(f"Here's your one path to a first run ({eco}):"))
    for i, step in enumerate(plan["steps"], 1):
        print(f"  {_green(str(i) + '.')} {step}")
    print()

    reality = result["readme_check"]
    if not reality["ok"]:
        print(_yellow("⚠ Heads up, the README and the repo don't quite agree:"))
        for issue in reality["issues"]:
            print(f"   • {issue}")
        print(_dim("   (I'll trust the actual files over the README.)"))
        print()

    pf = result["preflight"]
    if pf["has_requirements"]:
        print(_bold("Before you start, check these versions:"))
        for r in pf["requirements"]:
            print(f"   • {r['tool']} needs {r['required']}  →  run `{r['check']}`")
        print()

    print(_dim("Hit an error? Run again with the failing command and I'll translate it."))


def _render_profile(p: dict) -> None:
    def fmt(n):
        return f"{n/1000:.0f}k" if n >= 10000 else f"{n/1000:.1f}k" if n >= 1000 else str(n)
    meta = " · ".join(x for x in [p.get("company"), p.get("location"), f"since {p['since']}" if p.get("since") else ""] if x)
    print(_bold(f"{p['name']}") + _dim(f"  @{p['login']}") + (_dim("  " + meta) if meta else ""))
    if p.get("bio"):
        print(f"  {p['bio']}")
    print()
    print(f"  {_green(fmt(p['followers']))} followers   {_green(fmt(p['public_repos']))} repos   "
          f"{_green(fmt(p['total_stars']))} total stars   "
          + (_green(p['top_languages'][0]) + " main" if p.get('top_languages') else ""))
    print()
    print(_bold(f"  Top repositories{' (of first 100)' if p.get('truncated') else ''}:"))
    for r in p.get("top_repos", [])[:6]:
        lang = _dim(f"[{r['language']}]") if r['language'] else ""
        desc = _dim(f"  {r['description'][:50]}") if r['description'] else ""
        print(f"    {_green(fmt(r['stars']) + '★'):>8} {fmt(r['forks'])}⑂  {r['name']} {lang}{desc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="repopilot", description="Friendly GitHub repo inspector + onboarding agent.")
    parser.add_argument("target", help="GitHub owner/repo to inspect, or a username to profile")
    parser.add_argument("--offline", action="store_true", help="Use deterministic tools, no model call")
    parser.add_argument("--region", default=None, help="AWS region for Bedrock (e.g. us-east-1)")
    args = parser.parse_args(argv)

    started = time.time()

    # Dual mode: a bare username (no slash) profiles the developer.
    if github.parse_repo_url(args.target) is None and github.parse_username(args.target):
        try:
            profile = github.fetch_user_profile(github.parse_username(args.target))
        except ValueError as e:
            print(_yellow(f"\n🧭 {e}\n")); return 2
        except Exception as e:
            print(_yellow(f"\n🧭 I couldn't reach GitHub just now ({e}). Try again.\n")); return 2
        print()
        print(_bold("🧭 RepoPilot") + _dim("  developer profile"))
        print()
        _render_profile(profile)
        print(_dim(f"\n⏱  Ready in {time.time() - started:.1f}s."))
        return 0

    try:
        snap = github.fetch_snapshot(args.target)
    except ValueError as e:
        # Friendly, non-scary error, the product's whole philosophy.
        print(_yellow(f"\n🧭 {e}\n"))
        return 2
    except Exception as e:  # network, timeout, etc.
        print(_yellow(f"\n🧭 I couldn't reach GitHub just now ({e}). Check your connection and retry.\n"))
        return 2

    _hello(snap.name)

    if args.offline:
        _render_offline(snap.name, agent_mod.onboard_offline(snap))
    else:
        try:
            agent = agent_mod.build_agent(region=args.region)
        except RuntimeError as e:
            print(_dim(f"(Model agent unavailable: {e})"))
            print(_dim("Falling back to offline mode so you still get a plan.\n"))
            _render_offline(snap.name, agent_mod.onboard_offline(snap))
        else:
            prompt = (
                "A new contributor just cloned this repo and wants to get it running. "
                "Use your tools on this snapshot and give them one friendly, ordered first-run "
                f"path. Snapshot JSON:\n{agent_mod.snapshot_to_json(snap)}"
            )
            result = agent(prompt)
            print(result)

    print(_dim(f"\n⏱  Ready in {time.time() - started:.1f}s. Happy building!"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
