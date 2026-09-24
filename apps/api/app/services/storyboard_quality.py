"""Deterministic storyboard gates adapted from eternityspring/shuohao-skills.

The upstream project validates its storyboard with scripts instead of trusting
the model to police itself. Only the rules that can be judged mechanically
without inventing story facts are ported here; everything else stays as written
guidance in ``storyboard_skills/quality-gates.md``.

Two project-specific constraints shaped this module:

1. One storyboard row is a *clip* submitted as a single video generation call and
   may contain several internal cuts, so per-row camera rules cannot be derived
   from prose without false positives. Camera and staging advice therefore stays
   advisory prose; only the dialogue-timing rule is mechanical.
2. The gates are deliberately advisory and never flip an approval. They surface
   evidence into the review the user already reads, next to the model's own
   findings, and the repair stage decides what to change. Auto-rejecting here
   would change which boards the pipeline accepts, which is a behaviour change
   this task must not introduce.

Measured against the project's own active boards, the dialogue-to-duration ratio
has a median near 1.0 and a 90th percentile near 1.4, because narration, multiple
speakers and carry-over across a cut are all normal. The reporting threshold sits
above that band so it flags only lines no natural delivery could fit.
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.services.video_text import video_text_tracks

CHARS_PER_SECOND = Decimal("4.5")
# Only report a clear overrun; see the module docstring for the measured band.
DIALOGUE_ALERT_RATIO = Decimal("1.5")
UNIFORM_DURATION_RATIO = 0.8
UNIFORM_MIN_SHOTS = 6
# The review payload caps findings, so the deterministic half must stay bounded.
MAX_GATE_FINDINGS = 60

_SPEAKER = re.compile(r"^\s*([^：:]{1,20})[：:]\s*(.*)$")
# On-screen text is not spoken, and the project already draws that line in
# video_text. Editorially labelled subtitles also arrive as bare "字幕：" lines
# rather than the bracketed form, so both are excluded before counting time.
_SCREEN_TEXT_SPEAKERS = ("字幕", "画面文字", "后期文字")


def _dialogue_seconds(text: str) -> Decimal:
    """Count spoken characters the way the upstream duration model does.

    Punctuation is part of the timing (a pause is still time), the speaker label
    is not, on-screen text is not spoken, bracketed stage directions are skipped,
    and digits count because a number is read out syllable by syllable.
    """
    total = 0
    for line in text.splitlines():
        line = (video_text_tracks(line.strip())[0] or "").strip()
        if not line:
            continue
        match = _SPEAKER.match(line)
        if match:
            label = match.group(1).strip()
            if label in _SCREEN_TEXT_SPEAKERS or "字幕" in label:
                continue
            line = match.group(2)
        line = re.sub(r"[（(\[【][^）)\]】]*[）)\]】]", "", line)
        total += sum(1 for char in line if not char.isspace())
    return Decimal(total) / CHARS_PER_SECOND


def evaluate(shots: list[dict]) -> list[dict]:
    """Return advisory findings for one board, using the review finding shape."""
    findings: list[dict] = []
    for shot in shots:
        index = shot.get("order_index", "")
        try:
            duration = Decimal(str(shot.get("duration_seconds") or 0))
        except (ArithmeticError, ValueError, TypeError):
            # A stored board is validated on write, so this only guards the
            # review path: a malformed value must not fail the whole task.
            continue
        if duration <= 0:
            continue
        spoken = _dialogue_seconds(str(shot.get("dialogue") or ""))
        if spoken > duration * DIALOGUE_ALERT_RATIO:
            findings.append({
                "severity": "minor",
                "location": f"镜头 {index}",
                "issue": (
                    f"台词约 {spoken:.1f} 秒（按每分钟语速折算），本镜只有 {duration:g} 秒，"
                    "按常规语速念不完。"
                ),
                "suggestion": (
                    "确认语速与分配；必要时延长到能念完的合法时长，或按语义把台词拆到相邻镜头。"
                    "不要改写台词原文，也不要为了塞台词加快到听不清。"
                ),
            })
    findings.extend(_uniform_pacing(shots))
    return findings


def _uniform_pacing(shots: list[dict]) -> list[dict]:
    durations = []
    for shot in shots:
        try:
            durations.append(Decimal(str(shot.get("duration_seconds") or 0)))
        except (ArithmeticError, ValueError, TypeError):
            continue
    durations = [value for value in durations if value > 0]
    if len(durations) < UNIFORM_MIN_SHOTS:
        return []
    common = max(set(durations), key=durations.count)
    if durations.count(common) / len(durations) < UNIFORM_DURATION_RATIO:
        return []
    return [{
        "severity": "minor",
        "location": "整批镜头",
        "issue": (
            f"{len(durations)} 个镜头里有 {durations.count(common)} 个都是 {common:g} 秒，"
            "节奏会出现均匀病。"
        ),
        "suggestion": "按内容分配：短档给瞬间反应，长档给需要完整过程的动作与台词；不要机械取整。",
    }]


def merge(review: dict, shots: list[dict]) -> dict:
    """Add deterministic findings to a model review, keeping the model's verdict."""
    findings = list(review.get("findings") or [])
    known = {(item.get("severity"), item.get("location"), item.get("issue")) for item in findings}
    try:
        gates = evaluate(shots)
    except Exception:  # noqa: BLE001 - advisory gates must never fail a review
        return {**review, "findings": findings}
    for finding in gates:
        if len(findings) >= MAX_GATE_FINDINGS:
            break
        key = (finding["severity"], finding["location"], finding["issue"])
        if key not in known:
            findings.append(finding)
            known.add(key)
    return {**review, "findings": findings}


def apply_coverage_gate(review: dict, shots: list[dict], segments: list[dict]) -> dict:
    """Force the verdict to false when the board cannot cover every scene.

    Unlike the advisory gates above, this one changes the verdict. Each scene
    needs at least one clip, so a board with fewer shots than scenes is
    provably incomplete; letting it through silently drops story, which is the
    failure this gate exists to stop.
    """
    from app.services.storyboard_generation import coverage_finding

    try:
        finding = coverage_finding(shots, segments)
    except Exception:  # noqa: BLE001 - a gate must never fail the review task
        return review
    if finding is None:
        return review
    summary = str(review.get("summary") or "").strip()
    return {
        **review,
        "approved": False,
        "findings": [finding, *(review.get("findings") or [])],
        "summary": f"{finding['issue']} 原有审核结论：{summary}".strip(),
    }
