"""RepoPilot Score: a 0-100 repo health & trust score, computed like a credit score.

The score is a weighted average of five dimensions, each scored 0-100 from real signals in
the repo's metadata and files. Every point is traceable: the result lists the positive and
negative factors with their exact point impact, and ranks critical issues by severity.

A CRITICAL safety issue CAPS the overall grade (like a default caps a credit score), because
"it runs fine" means nothing if the setup step is designed to steal your keys.

Grounded in the 2026 FakeGit research (17k+ malicious agent-lookalike repos) and open-source
repo-quality frameworks (activity, maintenance, community, documentation, maturity).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .tools import RepoSnapshot, assess_repo_trust, scan_setup_for_danger

# Dimension weights (must sum to 1.0)
WEIGHTS = {
    "safety": 0.30,
    "trust": 0.25,
    "maintenance": 0.20,
    "maturity": 0.15,
    "documentation": 0.10,
}

# How much each dangerous-setup finding costs the Safety dimension, and its severity.
# (keyword in the finding text) -> (points subtracted, severity)
_DANGER_WEIGHTS = [
    ("remote script", 35, "CRITICAL"),      # curl|bash
    ("downloaded script", 35, "CRITICAL"),   # wget|sh
    ("base64", 30, "CRITICAL"),              # decode-and-run
    ("pre/postinstall", 22, "HIGH"),         # npm hooks
    ("raw public IP", 18, "HIGH"),
    ("destructive or over-permissive", 20, "HIGH"),  # sudo rm/chmod 777
    ("eval", 12, "MEDIUM"),
]


def _parse(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _clamp(x: float) -> int:
    return int(max(0, min(100, round(x))))


def compute_score(snap: RepoSnapshot) -> dict:
    """Compute the full RepoPilot scorecard for a repo snapshot.

    Returns a dict with: overall (0-100), grade, label, dimensions (each with score + weight),
    factors (positive[] / negative[] with points), and critical_issues[] ranked by severity.
    """
    meta = snap.meta or {}
    now = datetime.now(timezone.utc)
    created = _parse(meta.get("created_at", ""))
    pushed = _parse(meta.get("pushed_at", ""))
    stars = int(meta.get("stargazers", 0) or 0)
    forks = int(meta.get("forks", 0) or 0)
    open_issues = int(meta.get("open_issues", 0) or 0)
    archived = bool(meta.get("archived", False))
    license_name = meta.get("license") or ""
    owner_type = (meta.get("owner_type") or "").lower()
    age_days = (now - created).days if created else None
    stale_days = (now - pushed).days if pushed else None

    positive: list[dict] = []
    negative: list[dict] = []
    critical: list[dict] = []

    def pos(label, pts):
        positive.append({"label": label, "points": pts})

    def neg(label, pts, severity=None, dim=None):
        negative.append({"label": label, "points": -abs(pts)})
        if severity:
            critical.append({"severity": severity, "issue": label, "impact": -abs(pts), "dimension": dim})

    # ---------------- Safety (30%) ----------------
    danger = scan_setup_for_danger(snap)
    safety = 100
    for finding in danger.get("findings", []):
        matched = False
        for key, pts, sev in _DANGER_WEIGHTS:
            if key.lower() in finding.lower():
                safety -= pts
                neg(finding, pts, severity=sev, dim="Safety")
                matched = True
                break
        if not matched:
            safety -= 10
            neg(finding, 10, severity="MEDIUM", dim="Safety")
    if not danger.get("findings"):
        pos("No dangerous setup steps detected", 0)
    safety = _clamp(safety)

    # ---------------- Trust / authenticity (25%) ----------------
    trust_raw = assess_repo_trust(meta)
    trust = 100
    if age_days is not None and age_days < 60 and stars > 2000:
        trust -= 45
        neg(f"Fake-star pattern: {stars:,} stars on a {age_days}-day-old repo", 45,
            severity="CRITICAL", dim="Trust")
    elif age_days is not None and age_days < 180 and stars > 5000:
        trust -= 25
        neg(f"Suspicious: {stars:,} stars on a young ({age_days}d) repo", 25, severity="HIGH", dim="Trust")
    if owner_type == "organization":
        pos("Owned by an organization account", 8)
        trust = min(100, trust + 8)
    elif owner_type == "user":
        pos("Owned by a personal account (verify it looks real)", 0)
    trust = _clamp(trust)

    # ---------------- Maintenance (20%) ----------------
    maintenance = 100
    if archived:
        maintenance -= 55
        neg("Repository is archived (read-only, unmaintained)", 55, severity="HIGH", dim="Maintenance")
    if stale_days is not None:
        if stale_days > 730:
            maintenance -= 40
            neg(f"No update in ~{stale_days // 30} months (likely abandoned)", 40, severity="HIGH", dim="Maintenance")
        elif stale_days > 365:
            maintenance -= 20
            neg(f"No update in ~{stale_days // 30} months", 20, dim="Maintenance")
        elif stale_days < 90:
            pos("Actively maintained (updated recently)", 10)
    # a huge open-issue pile on a small repo is a mild signal
    if stars > 0 and open_issues > max(50, stars // 5):
        maintenance -= 10
        neg(f"{open_issues} open issues piling up relative to size", 10, dim="Maintenance")
    maintenance = _clamp(maintenance)

    # ---------------- Maturity (15%) ----------------
    maturity = 40  # neutral baseline
    if age_days is not None:
        if age_days > 365 * 3:
            maturity += 30; pos("Mature project (3+ years old)", 30)
        elif age_days > 365:
            maturity += 20; pos("Established (1+ year old)", 20)
        elif age_days > 90:
            maturity += 8
        else:
            maturity -= 5; neg("Very new repository (under 3 months)", 5, dim="Maturity")
    # genuine adoption (only count stars as positive when NOT flagged as fake above)
    fake_flagged = any(c["dimension"] == "Trust" for c in critical)
    if not fake_flagged:
        if stars > 10000: maturity += 25; pos(f"Strong genuine adoption ({stars:,} stars)", 25)
        elif stars > 1000: maturity += 15; pos(f"Good adoption ({stars:,} stars)", 15)
        elif stars > 100: maturity += 8
    if forks > 100:
        maturity += 5; pos(f"Actively forked ({forks:,} forks)", 5)
    maturity = _clamp(maturity)

    # ---------------- Documentation (10%) ----------------
    documentation = 0
    readme_len = len(snap.readme_text or "")
    if readme_len > 1500:
        documentation += 45; pos("Detailed README", 45)
    elif readme_len > 300:
        documentation += 25; pos("Has a README", 25)
    else:
        neg("Thin or missing README", 15, dim="Documentation")
    if license_name:
        documentation += 25; pos(f"Ships a {license_name} license", 25)
    else:
        neg("No license file", 10, severity="MEDIUM", dim="Documentation")
    files = {p.split("/")[-1] for p in snap.file_paths}
    if any(f in files for f in (".env.example", ".env.sample")):
        documentation += 15; pos("Provides a .env.example", 15)
    if any("test" in p.lower() for p in snap.file_paths):
        documentation += 15; pos("Has tests", 15)
    documentation = _clamp(documentation)

    # ---------------- Weighted overall ----------------
    dims = {
        "safety": safety, "trust": trust, "maintenance": maintenance,
        "maturity": maturity, "documentation": documentation,
    }
    overall = sum(dims[k] * WEIGHTS[k] for k in dims)
    overall = _clamp(overall)

    # CRITICAL cap: any CRITICAL issue caps the overall at 45 ("Risky") no matter what else.
    has_critical = any(c["severity"] == "CRITICAL" for c in critical)
    if has_critical:
        overall = min(overall, 45)

    grade, label = _grade(overall)

    # rank critical issues by severity
    order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    critical.sort(key=lambda c: order.get(c["severity"], 9))

    dimensions = [
        {"name": "Safety", "key": "safety", "score": safety, "weight": int(WEIGHTS["safety"] * 100)},
        {"name": "Trust", "key": "trust", "score": trust, "weight": int(WEIGHTS["trust"] * 100)},
        {"name": "Maintenance", "key": "maintenance", "score": maintenance, "weight": int(WEIGHTS["maintenance"] * 100)},
        {"name": "Maturity", "key": "maturity", "score": maturity, "weight": int(WEIGHTS["maturity"] * 100)},
        {"name": "Documentation", "key": "documentation", "score": documentation, "weight": int(WEIGHTS["documentation"] * 100)},
    ]

    verdict = _verdict(grade, has_critical)

    return {
        "overall": overall,
        "grade": grade,
        "label": label,
        "verdict": verdict,
        "capped_by_critical": has_critical,
        "dimensions": dimensions,
        "factors": {
            "positive": [p for p in positive if p["points"] > 0],
            "negative": negative,
        },
        "critical_issues": critical,
    }


def _grade(overall: int) -> tuple[str, str]:
    if overall >= 90: return "A", "Excellent"
    if overall >= 75: return "B", "Good"
    if overall >= 60: return "C", "Caution"
    if overall >= 40: return "D", "Risky"
    return "F", "Avoid"


def _verdict(grade: str, capped: bool) -> str:
    if capped:
        return "A critical safety issue was found. Do not run this repo's setup until you have read it line by line."
    return {
        "A": "Safe to explore. Still skim the setup steps before running them.",
        "B": "Looks solid. A couple of things worth a glance, then you are good.",
        "C": "Proceed with caution. Read the flags below before you clone.",
        "D": "Risky. Several signals suggest you should not run this blindly.",
        "F": "Avoid. The signals here match patterns used by malicious or dead repos.",
    }.get(grade, "")
