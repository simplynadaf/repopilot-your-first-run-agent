"""RepoPilot tools.

Each function here is a capability the agent can call. They are deliberately pure-Python and
side-effect-light so they can be unit-tested without AWS or network access. The agent (see
agent.py) decides when to call them.

Pain → tool mapping:
  * scattered setup path        -> scan_repo + build_first_run_plan
  * READMEs that lie            -> check_readme_against_reality
  * env misconfiguration        -> generate_env_example
  * env drift / wrong versions  -> preflight_requirements
  * the ticking-clock error     -> explain_error  (the delightful detail)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------------------
# Repo model
# --------------------------------------------------------------------------------------
@dataclass
class RepoSnapshot:
    """A minimal, serializable view of a repository the tools reason over."""

    name: str
    file_paths: list[str] = field(default_factory=list)
    readme_text: str = ""
    manifests: dict[str, str] = field(default_factory=dict)  # filename -> raw contents
    meta: dict = field(default_factory=dict)                 # repo metadata (age, stars, ...)
    scripts: dict[str, str] = field(default_factory=dict)    # install-script name -> contents


# --------------------------------------------------------------------------------------
# Ecosystem detection
# --------------------------------------------------------------------------------------
# Ordered so the most specific signal wins. Each entry: (ecosystem, manifest, install, run)
_ECOSYSTEMS = [
    ("node", "package.json", "npm install", "npm start"),
    ("python-poetry", "pyproject.toml", "poetry install", "poetry run python -m <package>"),
    ("python-pip", "requirements.txt", "pip install -r requirements.txt", "python main.py"),
    ("go", "go.mod", "go mod download", "go run ."),
    ("rust", "Cargo.toml", "cargo build", "cargo run"),
    ("ruby", "Gemfile", "bundle install", "bundle exec ruby app.rb"),
    ("java-maven", "pom.xml", "mvn install", "mvn exec:java"),
]


def detect_ecosystems(snapshot: RepoSnapshot) -> list[dict]:
    """Identify which language ecosystems the repo uses, based on manifest files present.

    Kills part of the 'scattered setup path' pain: the user no longer has to guess whether
    this is a Node, Python, Go, ... project.

    Only ROOT-level manifests count. A stray requirements.txt or a nested package.json deep in
    the tree does not define how you run the project, and counting it produces misleading stacks
    (e.g. the Linux kernel looking like a Python project because of one helper script's deps).
    """
    root_files = {p for p in snapshot.file_paths if "/" not in p}
    present = set(snapshot.manifests) | root_files
    found = []
    for eco, manifest, install, run in _ECOSYSTEMS:
        if manifest in present:
            found.append(
                {"ecosystem": eco, "manifest": manifest, "install": install, "run": run}
            )
    return found


# --------------------------------------------------------------------------------------
# Tool 1 + 2: a single, ordered first-run plan from scattered sources
# --------------------------------------------------------------------------------------
def build_first_run_plan(snapshot: RepoSnapshot) -> dict:
    """Produce ONE ordered path from clone to a working first run.

    Returns a dict with ordered `steps` and the detected `ecosystems`. The agent turns this
    into friendly prose; keeping the structure here makes it testable.
    """
    ecosystems = detect_ecosystems(snapshot)
    steps: list[str] = [f"git clone the repo and `cd {snapshot.name}`"]

    if not ecosystems:
        steps.append(
            "No standard manifest (package.json, requirements.txt, go.mod, ...) was found. "
            "Open the README and look for a 'Getting Started' or 'Install' section."
        )
        return {"ecosystems": [], "steps": steps, "confidence": "low"}

    env_files = [p for p in snapshot.file_paths if p.split("/")[-1] in (".env.example", ".env.sample")]
    if env_files:
        steps.append(f"copy `{env_files[0].split('/')[-1]}` to `.env` and fill in the values")

    for eco in ecosystems:
        steps.append(f"[{eco['ecosystem']}] install dependencies: `{eco['install']}`")

    docker = [p for p in snapshot.file_paths if p.split("/")[-1] in ("docker-compose.yml", "docker-compose.yaml", "Dockerfile")]
    if docker:
        steps.append("this repo ships Docker, `docker compose up` may be the fastest path")

    for eco in ecosystems:
        steps.append(f"[{eco['ecosystem']}] run it: `{eco['run']}`")

    return {
        "ecosystems": [e["ecosystem"] for e in ecosystems],
        "steps": steps,
        "confidence": "high" if len(ecosystems) == 1 else "medium",
    }


def check_readme_against_reality(snapshot: RepoSnapshot) -> dict:
    """Flag commands/files the README references that DO NOT exist in the repo.

    Kills the 'READMEs lie / decay' pain. If the README says `npm run build` but there is no
    package.json, or references a file that is gone, we surface it instead of letting the user
    discover it the hard way.
    """
    issues: list[str] = []
    readme = snapshot.readme_text or ""
    present_files = {p.split("/")[-1] for p in snapshot.file_paths} | set(snapshot.manifests)

    # 1. npm scripts referenced in README but no package.json
    if re.search(r"`?npm (run |start|install)", readme) and "package.json" not in present_files:
        issues.append("README mentions npm commands, but there is no package.json in the repo.")

    # 2. pip/requirements referenced but no requirements file
    if re.search(r"requirements\.txt", readme) and "requirements.txt" not in present_files:
        issues.append("README references requirements.txt, but that file is not in the repo.")

    # 3. explicit file references (`path/to/file`) that are missing
    for match in re.findall(r"`([\w./-]+\.(?:py|js|ts|yml|yaml|json|sh|env))`", readme):
        base = match.split("/")[-1]
        if base not in present_files and match not in snapshot.file_paths:
            issues.append(f"README references `{match}`, which was not found in the repo.")

    return {"ok": not issues, "issues": issues}


# --------------------------------------------------------------------------------------
# Tool 3: generate a filled .env.example
# --------------------------------------------------------------------------------------
# Common env var name -> a safe, obviously-placeholder default + short hint.
_ENV_HINTS = {
    "PORT": ("3000", "the port the app listens on"),
    "NODE_ENV": ("development", "runtime environment"),
    "DATABASE_URL": ("postgres://user:pass@localhost:5432/dbname", "your database connection string"),
    "AWS_REGION": ("us-east-1", "AWS region"),
    "LOG_LEVEL": ("info", "logging verbosity"),
}


def generate_env_example(detected_vars: list[str]) -> str:
    """Create a .env.example body from a list of referenced variable names.

    Kills the 'env misconfiguration' pain: the user gets a template with safe placeholders and
    a one-line hint per variable, instead of a blank wall.
    """
    lines = ["# Generated by RepoPilot, fill in real values, never commit secrets.", ""]
    for var in dict.fromkeys(v.strip() for v in detected_vars if v.strip()):  # dedupe, keep order
        default, hint = _ENV_HINTS.get(var.upper(), ("", "set me"))
        lines.append(f"# {hint}")
        lines.append(f"{var}={default}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------------------
# Tool 4: preflight requirement check
# --------------------------------------------------------------------------------------
def preflight_requirements(snapshot: RepoSnapshot) -> dict:
    """Extract the tool/version requirements the repo expects so the user can check up front.

    Kills the 'env drift / wrong versions' pain. We read engines from package.json, python
    version from pyproject/.python-version, and the go directive from go.mod.
    """
    reqs: list[dict] = []

    pkg = snapshot.manifests.get("package.json", "")
    m = re.search(r'"node"\s*:\s*"([^"]+)"', pkg)
    if m:
        reqs.append({"tool": "node", "required": m.group(1), "check": "node --version"})

    pyproject = snapshot.manifests.get("pyproject.toml", "")
    m = re.search(r'python\s*=\s*"([^"]+)"', pyproject)
    if m:
        reqs.append({"tool": "python", "required": m.group(1), "check": "python --version"})

    gomod = snapshot.manifests.get("go.mod", "")
    m = re.search(r"^go\s+([\d.]+)", gomod, re.MULTILINE)
    if m:
        reqs.append({"tool": "go", "required": m.group(1), "check": "go version"})

    return {"requirements": reqs, "has_requirements": bool(reqs)}


# --------------------------------------------------------------------------------------
# Tool 5: the delightful detail, plain-language error recovery
# --------------------------------------------------------------------------------------
# Pattern -> (plain-language cause, suggested fix). Ordered; first match wins.
_ERROR_PATTERNS: list[tuple[str, str, str]] = [
    (
        r"command not found|: not found",
        "A required tool isn't installed or isn't on your PATH.",
        "Install the missing tool (check the repo's preflight requirements), then re-open your terminal.",
    ),
    (
        r"EACCES|permission denied",
        "You don't have permission to write where the command is trying to write.",
        "Avoid sudo for project installs; fix the folder owner or use a local virtual environment / nvm.",
    ),
    (
        r"ECONNREFUSED|connection refused",
        "The app tried to reach a service (like a database) that isn't running.",
        "Start the dependency first (e.g. `docker compose up db`) or point the .env at a running instance.",
    ),
    (
        r"ModuleNotFoundError|Cannot find module|No module named",
        "A dependency the code imports hasn't been installed.",
        "Run the install step again in the right environment (activate your venv first for Python).",
    ),
    (
        r"EADDRINUSE|address already in use",
        "The port the app wants is already taken by another process.",
        "Stop the other process or set a different PORT in your .env.",
    ),
    (
        r"version .* required|engine .* incompatible|requires node|unsupported engine",
        "Your installed version of a tool is too old or too new for this repo.",
        "Match the version in the repo's preflight requirements (use nvm/pyenv to switch).",
    ),
]


def explain_error(command: str, output: str) -> dict:
    """Turn a failing command's raw output into a plain-language cause + a concrete next step.

    This is RepoPilot's delightful detail. Instead of a wall of red text, the user gets a short,
    kind, actionable explanation, the brief's own example of a delightful 'helpful error message'.
    """
    text = output or ""
    for pattern, cause, fix in _ERROR_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return {
                "recognized": True,
                "command": command,
                "cause": cause,
                "fix": fix,
                "tone": "You're close, this is a common first-run snag, not something you broke.",
            }
    return {
        "recognized": False,
        "command": command,
        "cause": "I don't recognize this specific error yet.",
        "fix": "Paste the last 5 lines of the output and I'll reason about it with the model.",
        "tone": "No worries, let's figure it out together.",
    }


# --------------------------------------------------------------------------------------
# Tool 6: trust read: should you even trust this repo? (the "found it in a reel" check)
# --------------------------------------------------------------------------------------
# Grounded in the 2026 "FakeGit / AgentBaiting" research: 17k+ malicious repos posed as AI
# agent / MCP / skill repos, using copied code, lookalike owners, and convincing READMEs to
# get blindly cloned. The strongest tells are documented below.
def _parse_iso(ts: str):
    """Parse a GitHub ISO8601 timestamp to a datetime, or None."""
    if not ts:
        return None
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def assess_repo_trust(meta: dict) -> dict:
    """A plain-language 'should you trust this repo' read from public metadata.

    Returns a letter grade, a short list of flags (each a specific, cited reason), and a
    reassurance line. This is a signal, never a verdict: the user still decides.
    """
    from datetime import datetime, timezone

    flags: list[str] = []
    good: list[str] = []
    now = datetime.now(timezone.utc)

    created = _parse_iso(meta.get("created_at", ""))
    pushed = _parse_iso(meta.get("pushed_at", ""))
    stars = int(meta.get("stargazers", 0) or 0)
    archived = bool(meta.get("archived", False))
    license_name = meta.get("license") or ""
    owner = meta.get("owner", "")
    owner_type = meta.get("owner_type", "")

    age_days = (now - created).days if created else None
    stale_days = (now - pushed).days if pushed else None

    # 1. New repo + lots of stars = classic bought-star / fake signal.
    if age_days is not None and age_days < 60 and stars > 2000:
        flags.append(
            f"Created only {age_days} days ago but already has {stars:,} stars. A sudden star "
            "spike on a young repo is a known fake-star pattern. Look closer before running anything."
        )
    # 2. Archived = unmaintained; vulnerabilities accumulate.
    if archived:
        flags.append("This repo is archived (read-only). It is no longer maintained, so expect unpatched issues.")
    # 3. Stale: no push in a long time.
    if stale_days is not None and stale_days > 540:
        flags.append(f"Last updated about {stale_days // 30} months ago. It may be abandoned and bit-rotted.")
    # 4. No license = you have no legal right to use it, and serious projects usually have one.
    if not license_name:
        flags.append("No license file. You have no explicit permission to use the code, and real projects usually ship one.")
    # 5. Personal account (not an org) is not itself bad, but worth noting for 'from a reel' repos.
    if owner_type and owner_type.lower() == "user":
        good.append(f"Owned by a personal account ({owner}). Fine, but check the account looks real (history, followers).")

    # Positives worth stating so the read is honest, not alarmist.
    if age_days is not None and age_days > 365:
        good.append(f"Around for {age_days // 365}+ year(s). Longevity is a mild trust signal.")
    if license_name:
        good.append(f"Ships a {license_name} license.")
    if stale_days is not None and stale_days < 90:
        good.append("Updated recently, so it is actively maintained.")

    # Grade: start at B, dock for each flag, bump for solid positives.
    score = 80 - 18 * len(flags) + 4 * len(good)
    score = max(0, min(100, score))
    grade = "A" if score >= 85 else "B" if score >= 70 else "C" if score >= 50 else "D"

    if flags:
        reassure = "Not a verdict, just things to check. When in doubt, read the install steps before you run them."
    else:
        reassure = "Nothing alarming jumped out, but still skim the install steps below before running them."

    return {"grade": grade, "score": score, "flags": flags, "good": good, "reassurance": reassure}


# --------------------------------------------------------------------------------------
# Tool 7: scan the setup for dangerous steps BEFORE you run them
# --------------------------------------------------------------------------------------
# Each entry: (regex, plain-language warning). Ordered; all matches are reported.
_DANGER_PATTERNS: list[tuple[str, str]] = [
    (r"curl[^\n|]*\|\s*(sudo\s+)?(bash|sh|zsh)",
     "Runs a remote script straight from the internet (`curl ... | bash`). You cannot see what it does before it runs. Download it and read it first."),
    (r"wget[^\n|]*\|\s*(sudo\s+)?(bash|sh|zsh)",
     "Pipes a downloaded script directly into a shell (`wget ... | sh`). Same risk as curl-pipe-bash. Inspect it first."),
    (r'"(pre|post)install"\s*:',
     "Has an npm pre/postinstall hook, which runs code automatically on `npm install`. A common malware delivery point. Read what it runs."),
    (r"\beval\s*\(",
     "Uses `eval` in a setup path, which executes dynamic code. Legitimate uses exist, but it is a classic obfuscation spot."),
    (r"base64\s+(-d|--decode)|atob\(|b64decode",
     "Decodes a base64 blob during setup. Hidden/encoded payloads are a red flag. Decode and read it before trusting it."),
    (r"sudo\s+(rm|chmod\s+777|chown)",
     "Runs a destructive or over-permissive `sudo` command during setup. Be sure you understand it before allowing it."),
    (r"\bhttps?://(?!127\.0\.0\.1|0\.0\.0\.0|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)\d{1,3}(\.\d{1,3}){3}",
     "Setup reaches a raw public IP address instead of a named domain. Unusual for a legitimate installer."),
]


def scan_setup_for_danger(snapshot: RepoSnapshot) -> dict:
    """Scan the README and install scripts for patterns you should NOT run blindly.

    Returns a list of plain-language findings. Empty findings is a genuinely good sign (but
    not a guarantee). This is the heart of the 'before you blindly clone that reel repo' check.
    """
    import re

    haystacks: list[str] = []
    if snapshot.readme_text:
        haystacks.append(snapshot.readme_text)
    for content in snapshot.scripts.values():
        haystacks.append(content)
    for name, content in snapshot.manifests.items():
        if name == "package.json":  # scripts/postinstall live here
            haystacks.append(content)
    blob = "\n".join(haystacks)

    findings: list[str] = []
    for pattern, warning in _DANGER_PATTERNS:
        if re.search(pattern, blob, re.IGNORECASE):
            if warning not in findings:
                findings.append(warning)

    return {"safe_looking": not findings, "findings": findings}
