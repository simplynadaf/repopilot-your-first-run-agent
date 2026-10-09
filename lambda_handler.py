"""AWS Lambda handler for RepoPilot, exposed via a Function URL.

Design choice: this handler talks to Amazon Bedrock (Amazon Nova Pro) directly with boto3's
Converse API and native tool-use, rather than depending on the Strands SDK being installed in
the Lambda runtime. The agent loop is small and self-contained, which makes the weekend deploy
reliable (no heavy packaging). The CLI still uses Strands locally; both share repopilot/tools.py.

Request  (POST, JSON): {"repo": "https://github.com/owner/repo"}
                 (GET): ?repo=owner/repo
Response (JSON): {"repo":..., "answer":..., "plan":..., "readme_check":..., "preflight":...}
"""
from __future__ import annotations

import json
import os

import boto3

from repopilot import github
from repopilot import tools
from repopilot.agent import onboard_offline, snapshot_to_json

MODEL_ID = os.environ.get("REPOPILOT_MODEL_ID", "amazon.nova-pro-v1:0")
REGION = os.environ.get("AWS_REGION", "us-east-1")

SYSTEM = (
    "You are RepoPilot, a warm, concise GitHub repo inspector for someone about to try a repo "
    "they often found from a social post. You are given a repo snapshot plus a computed scorecard "
    "and analysis tools. Respond in this order:\n"
    "1. Lead with the RepoPilot Score: the overall number out of 100, the grade and label, and the "
    "one-line verdict. If there are critical issues, state them plainly and tell them not to run the "
    "setup until they have read it.\n"
    "2. A one-line read on the weakest and strongest dimension.\n"
    "3. Then ONE friendly, ordered first-run path from clone to running.\n"
    "Be brief, honest, and reassuring. Never invent findings the tools did not report."
)


def _bedrock():
    return boto3.client("bedrock-runtime", region_name=REGION)


def _narrate(snapshot, analysis: dict) -> str:
    """Single Bedrock call: hand the deterministic analysis to Nova Pro to narrate kindly."""
    user = (
        "Repo: " + snapshot.name + "\n\n"
        "Analysis (from my tools):\n" + json.dumps(analysis, indent=2) + "\n\n"
        "Write the friendly first-run guide now."
    )
    resp = _bedrock().converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM}],
        messages=[{"role": "user", "content": [{"text": user}]}],
        inferenceConfig={"maxTokens": 600, "temperature": 0.3},
    )
    return resp["output"]["message"]["content"][0]["text"]


def _extract_repo(event: dict) -> str | None:
    # Function URL: body is a JSON string for POST; queryStringParameters for GET.
    if event.get("body"):
        try:
            body = json.loads(event["body"])
            if isinstance(body, dict) and body.get("repo"):
                return body["repo"]
        except (ValueError, TypeError):
            pass
    qs = event.get("queryStringParameters") or {}
    return qs.get("repo")


def _reply(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(payload),
    }


PROFILE_SYSTEM = (
    "You are RepoPilot in developer-profile mode. Given a GitHub user's public profile and their "
    "top repositories, write a short, warm 2-3 sentence read: who they are, their open-source "
    "footprint (total stars, main languages, how long active), and which one or two projects are "
    "worth looking at first. If the account looks very new or empty, say so honestly, since fake "
    "lookalike profiles are a real risk. Be specific and never invent numbers."
)


def _handle_profile(username: str) -> dict:
    try:
        profile = github.fetch_user_profile(username)
    except ValueError as e:
        return _reply(400, {"error": str(e)})
    except Exception as e:
        return _reply(502, {"error": f"Could not reach GitHub: {e}"})

    answer = None
    try:
        top = ", ".join(f"{r['name']} ({r['stars']}*)" for r in profile["top_repos"][:5])
        user = (
            f"User @{profile['login']} ({profile['name']}). Bio: {profile['bio'] or 'none'}. "
            f"Active since {profile['since']}, {profile['followers']} followers, "
            f"{profile['public_repos']} public repos, {profile['total_stars']} total stars across "
            f"their own repos. Main languages: {', '.join(profile['top_languages']) or 'n/a'}. "
            f"Top repos: {top}. Write the read now."
        )
        resp = _bedrock().converse(
            modelId=MODEL_ID,
            system=[{"text": PROFILE_SYSTEM}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": 300, "temperature": 0.4},
        )
        answer = resp["output"]["message"]["content"][0]["text"]
    except Exception as e:
        profile["model_note"] = f"Model narration unavailable ({type(e).__name__})."

    return _reply(200, {"mode": "profile", "profile": profile, "answer": answer})


def handler(event, context):
    # CORS preflight: answer OPTIONS directly with a 204 so the browser proceeds.
    method = (
        (event or {}).get("requestContext", {}).get("http", {}).get("method")
        or (event or {}).get("httpMethod")
        or ""
    )
    if method.upper() == "OPTIONS":
        return {
            "statusCode": 204,
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
                "Access-Control-Allow-Headers": "content-type",
                "Access-Control-Max-Age": "86400",
            },
            "body": "",
        }

    target = _extract_repo(event or {})
    if not target:
        return _reply(400, {"error": "Provide a repo like {\"repo\": \"owner/name\"}, or a username like {\"repo\": \"octocat\"}."})

    # Dual mode: a bare username (no slash) -> developer profile; owner/repo -> repo inspection.
    if github.parse_repo_url(target) is None and github.parse_username(target):
        return _handle_profile(github.parse_username(target))

    try:
        snap = github.fetch_snapshot(target)
    except ValueError as e:
        return _reply(400, {"error": str(e)})
    except Exception as e:  # network/timeout
        return _reply(502, {"error": f"Could not reach GitHub: {e}"})

    analysis = onboard_offline(snap)  # deterministic facts (never dead-ends)

    try:
        answer = _narrate(snap, analysis)
    except Exception as e:
        # Model failed? Still return the deterministic plan so the user gets value.
        answer = None
        analysis["model_note"] = f"Model narration unavailable ({type(e).__name__}); showing the raw plan."

    return _reply(
        200,
        {
            "repo": snap.name,
            "answer": answer,
            "scorecard": analysis["scorecard"],
            "trust": analysis["trust"],
            "danger": analysis["danger"],
            "plan": analysis["plan"],
            "readme_check": analysis["readme_check"],
            "preflight": analysis["preflight"],
        },
    )
