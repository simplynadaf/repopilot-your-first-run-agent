"""The RepoPilot agent loop, built on the Strands Agents SDK with Amazon Nova on Bedrock.

The agent is given the five RepoPilot tools and a system prompt that encodes the product's
personality: a patient teammate who gives one clear path and turns errors into next steps.

If the Strands SDK or Bedrock access is unavailable (e.g. offline dev box), `build_agent`
raises a clear message, and `onboard_offline` still produces a useful, deterministic result
from the pure-Python tools so the demo never dead-ends.
"""
from __future__ import annotations

import json
from typing import Optional

from . import tools
from .tools import RepoSnapshot

SYSTEM_PROMPT = """You are RepoPilot, a warm, concise onboarding guide for software repositories.

Your job: get a brand-new contributor from a fresh clone to a working first run with the least
friction possible. You have tools that read the repo's real files, cross-check the README,
generate a .env template, list version requirements, and explain errors in plain language.

Rules of behaviour:
- Give ONE clear, ordered path. Never present five options when one will do.
- Prefer the repo's actual files over what the README claims. If they disagree, say so kindly.
- When a command fails, do not dump the raw error. Explain the likely cause in one sentence,
  then give one concrete next step. Reassure the user they didn't break anything.
- Be brief and friendly. A new contributor is nervous; your tone should lower their heart rate.
"""

# Amazon Nova Pro, verified invoke-accessible in this account (us-east-1).
DEFAULT_MODEL_ID = "amazon.nova-pro-v1:0"


def build_agent(model_id: str = DEFAULT_MODEL_ID, region: Optional[str] = None):
    """Construct a Strands Agent wired to Amazon Nova on Bedrock with RepoPilot's tools.

    Raises RuntimeError with an actionable message if Strands isn't installed.
    """
    try:
        from strands import Agent, tool  # type: ignore
        from strands.models import BedrockModel  # type: ignore
    except ImportError as e:  # pragma: no cover - depends on environment
        raise RuntimeError(
            "The Strands Agents SDK isn't installed. Run `pip install strands-agents` "
            "(requires Python 3.10+)."
        ) from e

    # Wrap the pure tools as Strands tools. State is captured per invocation via closure.
    @tool
    def scan_repo_tool(repo_json: str) -> str:
        """Build the single ordered first-run plan. Input: JSON of the repo snapshot."""
        snap = _snapshot_from_json(repo_json)
        return json.dumps(tools.build_first_run_plan(snap))

    @tool
    def check_readme_tool(repo_json: str) -> str:
        """Flag README commands/files that don't exist. Input: JSON of the repo snapshot."""
        snap = _snapshot_from_json(repo_json)
        return json.dumps(tools.check_readme_against_reality(snap))

    @tool
    def generate_env_tool(variables_csv: str) -> str:
        """Generate a .env.example body from a comma-separated list of variable names."""
        return tools.generate_env_example(variables_csv.split(","))

    @tool
    def preflight_tool(repo_json: str) -> str:
        """List required tool versions. Input: JSON of the repo snapshot."""
        snap = _snapshot_from_json(repo_json)
        return json.dumps(tools.preflight_requirements(snap))

    @tool
    def explain_error_tool(command: str, output: str) -> str:
        """Explain a failing command's output in plain language and suggest a fix."""
        return json.dumps(tools.explain_error(command, output))

    model = BedrockModel(model_id=model_id, region_name=region)
    return Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[scan_repo_tool, check_readme_tool, generate_env_tool, preflight_tool, explain_error_tool],
    )


def _snapshot_from_json(repo_json: str) -> RepoSnapshot:
    data = json.loads(repo_json)
    return RepoSnapshot(
        name=data.get("name", "repo"),
        file_paths=data.get("file_paths", []),
        readme_text=data.get("readme_text", ""),
        manifests=data.get("manifests", {}),
        meta=data.get("meta", {}),
        scripts=data.get("scripts", {}),
    )


def snapshot_to_json(snap: RepoSnapshot) -> str:
    """Serialize a snapshot for passing to the agent's tools (README trimmed to keep tokens low)."""
    return json.dumps(
        {
            "name": snap.name,
            "file_paths": snap.file_paths[:400],
            "readme_text": snap.readme_text[:4000],
            "manifests": snap.manifests,
            "meta": snap.meta,
            "scripts": {k: v[:2000] for k, v in snap.scripts.items()},
        }
    )


def onboard_offline(snap: RepoSnapshot) -> dict:
    """Deterministic onboarding result from the pure tools, no model call.

    Used by the CLI's --offline mode and as a safety net so a demo never dead-ends on a
    network/credentials issue. Produces the same substance the agent would narrate.
    """
    plan = tools.build_first_run_plan(snap)
    reality = tools.check_readme_against_reality(snap)
    preflight = tools.preflight_requirements(snap)
    trust = tools.assess_repo_trust(snap.meta)
    danger = tools.scan_setup_for_danger(snap)
    from .score import compute_score
    scorecard = compute_score(snap)
    return {
        "scorecard": scorecard,
        "trust": trust,
        "danger": danger,
        "plan": plan,
        "readme_check": reality,
        "preflight": preflight,
    }
