"""Rewrite an image prompt the upstream refused, keeping the intent intact.

Platforms reject prompts for policy reasons that are often about incidental
phrasing — a weapon named in a fight scene, a body description, a real-world
brand — rather than the asset being requested. Failing the task there wastes
the generation and leaves the user with an error they cannot act on, so the
prompt is rephrased once with the text model and submitted again.
"""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# Managed prompt code; its template carries the rewriting rules and is editable
# in the admin console like every other stage prompt.
REWRITE_CODE = "image-prompt-safety-rewrite"


class RewrittenPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    prompt: str = Field(min_length=1, max_length=20_000)
    note: str = Field(default="", max_length=600)


def rewrite_prompt_text(source: str) -> str:
    """The per-request half of the rewrite; the rules live in the template."""
    return (
        "被拒绝的提示词如下，请按输出契约改写：\n"
        + source
        + "\n\n输出结构："
        + json.dumps(RewrittenPrompt.model_json_schema(), ensure_ascii=False)
    )


def is_usable_rewrite(original: str, candidate: str) -> bool:
    """Reject a rewrite that would not actually change the outcome.

    An identical or near-identical prompt is already known to be refused, and an
    empty one carries no image intent; both would waste a second generation.
    """
    cleaned = " ".join(candidate.split())
    if not cleaned:
        return False
    if cleaned == " ".join(original.split()):
        return False
    # A rewrite that dropped most of the prompt has lost the asset's identity.
    return len(cleaned) >= max(20, int(len(" ".join(original.split())) * 0.35))


def parse_rewrite(raw: str) -> str:
    """Read the rewritten prompt from the model's reply."""
    from app.services.task_worker import parse_json_object

    try:
        return RewrittenPrompt.model_validate(parse_json_object(raw)).prompt
    except (RuntimeError, ValidationError):
        # Some models answer with the bare prompt despite the JSON contract.
        text = raw.strip()
        fence = re.match(r"^```(?:json|text)?\s*(.*?)\s*```$", text, re.S)
        return (fence.group(1) if fence else text).strip()


def reattach_identity_lock(rewritten: str, identity_suffix: str) -> str:
    """Put the caller's identity constraint back onto a rewritten prompt.

    A rewrite replaces the whole prompt, so the identity constraint the platform
    had attached is missing from the model's reply. Submitting that as the retry
    would let the model redraw the face -- the very drift the constraint prevents
    -- so it is re-appended unless the rewrite already carried it.
    """
    cleaned = rewritten.rstrip()
    suffix = identity_suffix.strip()
    if not suffix or suffix in cleaned:
        return cleaned
    return f"{cleaned}{identity_suffix}"
