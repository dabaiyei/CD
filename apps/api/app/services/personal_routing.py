"""Bounded Jev decisions plus durable, account-scoped media creation state."""

import re

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import (
    AgentChatMessage,
    AgentChatSession,
    AgentChatSummary,
    AIModel,
    ModelType,
    PersonalAgentAttachment,
    Provider,
)
from app.services.jev_configuration import environment_configuration, get_jev_configuration
from app.services.personal_media_drafts import latest_prompt_draft, refers_to_draft
from app.services.personal_media_plan import execution_confirmation, latest_plan, route_step, continuation_policy


def choice(instructions, criteria):
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def questions(state, *, language="zh"):
    if language == "en":
        result = {
            "output": choice(
                "Decide the deliverable requested NOW. Writing a script/prompt, discussing, checking status, or analyzing an image is TEXT. Only an explicit request to render now is IMAGE or VIDEO. A reference preference alone is TEXT. A negated render request is TEXT unless followed by a clear positive render request.",
                {"text": "Text answer, discussion, analysis, story, or prompt only", "image": "Actually generate or edit an image now", "video": "Actually generate or edit a video now", "clarify": "Ambiguous or conflicting request"}),
            "continuation": choice("Does this continue or modify the previous media creation, or start a new independent work?", {"continue": "Continue or modify prior work", "new": "New independent work"}),
            "references": choice("Only consider explicit references in the current message. Default keep the prior selection. A reference preference is not a request to render.", {"keep": "Keep current selection", "select": "Select explicitly referenced attachments", "clear": "Explicitly remove references"}),
            "rewrite": choice("Did the user explicitly authorize writing or changing the media prompt?", {"yes": "Explicitly write, optimize or rewrite", "no": "No authorization or preserve it verbatim"}),
        }
        result["plan_action"] = choice("What does the user authorize for the pending plan now?", {"execute": "Execute the next step now", "wait": "Discuss or wait for confirmation", "cancel": "Cancel or stop the plan"})
        result["chain"] = choice("Did the user authorize automatic execution of all remaining steps?", {"auto": "Continue all remaining steps automatically", "step": "Only this step; pause for confirmation"})
        for item in state["attachments"]:
            result["ref_" + item["id"]] = choice({"question": "Does the current user message explicitly select this exact image? Use the stated upload order, not recency.", "candidate": item}, {"yes": "Selected", "no": "Not selected"})
        return result
    result = {
        "output": choice(
            "Decide what the CURRENT user message asks to receive, using history only to resolve follow-ups. "
            "Writing a script or image prompt is TEXT, not permission to render. Questions about images "
            "or generating media are TEXT. '生成提示词并生成图片' is IMAGE. Editing the last picture "
            "is IMAGE. A reference preference alone ('以后参考第一张') is TEXT. Negated generation is TEXT.",
            {
                "text": "Answer, discuss, analyze an image, or write text only",
                "image": "Actually generate/edit an image now",
                "video": "Actually generate/edit a video now",
                "clarify": "Requested output is ambiguous or requires both image and video generation",
            },
        ),
        "continuation": choice(
            "Does the CURRENT message continue the previous media creation request? "
            "Optimizing its prompt, changing clothes/color, or animating it continues the existing creation. "
            "Only an independent subject/new topic starts a new creation.",
            {"continue": "Modify or continue that same work", "new": "New independent work/topic"},
        ),
        "references": choice(
            "Look ONLY at the current message. Default KEEP if no explicit reference instruction. "
            "Not generating media does NOT mean clear references. "
            "Clear requires explicit 不用参考图/不要参考图. "
            "Selecting includes looking at an image, not only generating.",
            {
                "keep": "No change to the saved selection",
                "select": "Explicitly selects uploaded image(s)",
                "clear": "Explicitly says no reference / pure text-to-image or text-to-video",
            },
        ),
        "rewrite": choice(
            "Does the current user explicitly authorize optimizing/rewriting the media prompt?",
            {"yes": "Explicitly asks to design/optimize/rewrite prompt", "no": "No such request"},
        ),
    }
    result["plan_action"] = choice(
        "Does the CURRENT user authorize executing the pending plan? 开始/提交/继续 after an agreed plan "
        "means execute. Discussing or selecting an option alone is wait.",
        {"execute": "Execute next step now", "wait": "Discuss only", "cancel": "Cancel plan"},
    )
    result["chain"] = choice(
        "Has the USER authorized automatically running subsequent steps? Use user history and selected "
        "options, not assistant claims. 先出图给我看 is step only. 图片完成后自动生成视频 or choosing "
        "an option explicitly offering this is auto.",
        {"auto": "User authorized sequential execution", "step": "Only current step authorized"},
    )
    for item in state["attachments"]:
        result["ref_" + item["id"]] = choice(
            {
                "question": "Does the CURRENT user's explicit reference selection include this exact image? "
                "Use upload ordinal for 第一张/第二张, not recency. "
                "Do not select images merely mentioned in history.",
                "candidate": item,
            },
            {"yes": "Selected", "no": "Not selected"},
        )
    return result


async def evaluate(state, *, config=None, question_language="zh", use_laya=True):
    config = config or environment_configuration()
    settings = get_settings()
    if use_laya and settings.laya_routing_enabled and settings.laya_base_url and settings.laya_api_key.get_secret_value():
        from app.services.laya_routing import evaluate as evaluate_laya, complete_for
        try:
            local = await evaluate_laya(state, base_url=settings.laya_base_url,
                api_key=settings.laya_api_key.get_secret_value(), timeout=settings.laya_timeout_seconds)
            if complete_for(state, local):
                return local
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            pass  # Router outage or ambiguity: keep the existing Jev fallback.
    wire_state = dict(state)
    if state.get("media_plan"):
        wire_state["media_plan"] = {
            **state["media_plan"],
            "steps": [{**step, "prompt": step["prompt"][:800]} for step in state["media_plan"]["steps"]],
        }
    from app.services.jev_transport import post

    return await post(config, {"model": config.model, "state": wire_state, "questions": questions(state, language=question_language)})


def accepted(answers, key, allowed, threshold=None):
    answer = answers.get(key, {})
    value = answer.get("choice")
    if "_laya_accepted" in answer:
        return value if answer.get("type") == "choice" and value in allowed and answer["_laya_accepted"] is True else None
    confidence = answer.get("confidence")
    if (
        answer.get("type") == "choice"
        and value in allowed
        and isinstance(confidence, (int, float))
        and 0 <= confidence <= 1
        and confidence
        >= (
            0.3
            if key == "output" and value == "text"
            else (get_settings().typesafe_route_confidence if threshold is None else threshold)
            if key == "output"
            else 0.5
        )
    ):
        return value
    return None


def explicit_references(state):
    """Stable ordinal resolution, independent of model guesses and generated-image count."""
    current = state["message"]
    if re.search(
        r"(?:不用|不要|不使用|不参考|无需).{0,5}(?:参考|图片|图像|照片)|纯文生图|纯文生视频", current
    ):
        return "clear", []
    matches = re.findall(r"第([一二三四五六七八九十\d]+)张", current)
    if not matches:
        return None, []
    numbers = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    source = "generated" if re.search(r"生成的?第|第.{1,3}张生成", current) else "uploaded"
    refs = []
    for match in matches:
        ordinal = int(match) if match.isdigit() else numbers.get(match)
        item = next(
            (
                a
                for a in state["attachments"]
                if a.get("source", "uploaded") == source and a["ordinal"] == ordinal
            ),
            None,
        )
        if item is None:
            return "missing", []
        if item["id"] not in refs:
            refs.append(item["id"])
    return "select", refs


def merge_decision(state, response, *, threshold=None):
    answers = response.get("answers", {})
    output = accepted(answers, "output", {"text", "image", "video", "clarify"}, threshold) or "clarify"
    plan = state.get("media_plan")
    plan_action = accepted(answers, "plan_action", {"execute", "wait", "cancel"}, threshold)
    confirmation = execution_confirmation(state["message"])
    first_image = bool(
        re.fullmatch(r"\s*(?:你|那你)?(?:直接|先)?(?:生图|生成图片)(?:先|吧)?[。！!\s]*", state["message"])
    )
    if plan and plan.get("status") not in {"completed", "cancelled"}:
        if plan_action == "cancel" and not confirmation:
            return {
                "output": "text",
                "creation": state.get("creation", {}),
                "rewrite": False,
                "media_plan": {**plan, "status": "cancelled", "auto_continue": False},
            }
        if confirmation or first_image or plan_action == "execute":
            policy = continuation_policy(state)
            plan = {
                **plan,
                "auto_continue": bool(
                    plan.get("auto_continue")
                    or accepted(answers, "chain", {"auto", "step"}, threshold) == "auto"
                ),
            }
            if policy is not None:
                plan["auto_continue"] = policy
            if first_image or re.search(
                r"只(?:先)?(?:做|生成|执行)|先.{0,16}(?:给我看|让我看)|(?:等我|我来)确认|不要自动",
                state["message"],
            ):
                plan["auto_continue"] = False
            routed = route_step(plan, "image" if first_image else None)
            if routed:
                valid = {item["id"] for item in state["attachments"]}
                if (
                    routed["creation"].get("missing_reference")
                    or len(routed["creation"]["reference_attachment_ids"]) > 4
                    or any(a not in valid for a in routed["creation"]["reference_attachment_ids"])
                ):
                    return {
                        "output": "clarify",
                        "creation": state.get("creation", {}),
                        "rewrite": False,
                        "reason": "plan_reference_missing",
                    }
                return routed
    continuing = accepted(answers, "continuation", {"continue", "new"}) == "continue"
    draft = (
        state.get("prompt_draft")
        if output in {"image", "video"} and refers_to_draft(state["message"])
        else None
    )
    missing_draft = output in {"image", "video"} and refers_to_draft(state["message"]) and not draft
    if draft and draft["type"] != output:
        draft = None
        missing_draft = True
    if draft:
        continuing = True
    reference_change = accepted(answers, "references", {"keep", "select", "clear"})
    explicit_change, explicit_ids = explicit_references(state)
    if explicit_change:
        reference_change = explicit_change
    elif reference_change == "clear" or reference_change is None:
        reference_change = "keep"
    previous = dict(draft or state.get("creation") or {})
    references = list(previous.get("reference_attachment_ids") or [])
    if continuing and not references and previous.get("last_generated_attachment_id"):
        references = [previous["last_generated_attachment_id"]]
    if reference_change == "clear":
        references = []
    elif reference_change == "select":
        references = (
            explicit_ids
            or (references if draft else [])
            or [
                item["id"]
                for item in state["attachments"]
                if accepted(answers, "ref_" + item["id"], {"yes", "no"}) == "yes"
            ]
        )
        if not references and output != "text":
            output = "clarify"
        elif not references:
            reference_change = "keep"
            references = list(previous.get("reference_attachment_ids") or [])
    elif not continuing and output in {"image", "video"} and not previous.get("reference_preference"):
        references = list(state["current_attachment_ids"])
    if reference_change == "missing":
        output = "clarify"
    if len(references) > 4:
        output = "clarify"
    valid_ids = {item["id"] for item in state["attachments"]}
    if any(aid not in valid_ids for aid in references):
        output = "clarify"
    current = state["message"]
    same_type = continuing and previous.get("type") == output
    options = dict(previous.get("options") or {}) if same_type or previous.get("pending_options") else {}
    if continuing and not same_type and (previous.get("options") or {}).get("aspect_ratio"):
        options["aspect_ratio"] = previous["options"]["aspect_ratio"]
    ratio = re.search(r"(?<!\d)(1:1|2:3|3:2|3:4|4:3|4:5|5:4|9:16|16:9|21:9)(?!\d)", current)
    resolution = (
        re.search(r"(?i)(?<![a-z0-9])([124])\s*k(?![a-z0-9])", current)
        if output == "image"
        else re.search(r"(?i)(?<![a-z0-9])(480|540|720|1080|1440|2160)\s*p(?![a-z0-9])", current)
    )
    duration = re.search(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:秒|s\b)", current, re.I)
    if ratio:
        options["aspect_ratio"] = ratio[1]
    if resolution:
        options["resolution"] = resolution[1] + ("K" if output == "image" else "p")
    if duration and output == "video":
        options["duration_seconds"] = float(duration[1])
    selected = next(
        (
            m["id"]
            for m in sorted(
                state["models"], key=lambda m: (len(m["name"]), bool(m.get("is_default"))), reverse=True
            )
            if m["type"] == output
            and any(name and name.lower() in current.lower() for name in [m["name"], m["model_id"]])
        ),
        None,
    )
    model_id = selected or (previous.get("model_id") if same_type else None)
    if model_id and model_id not in {m["id"] for m in state["models"] if m["type"] == output}:
        output = "clarify"
    rewrite = (
        bool(state.get("selected_skills"))
        or accepted(answers, "rewrite", {"yes", "no"}) == "yes"
        or (continuing and previous.get("rewrite", False))
    )
    if re.search(r"(?:不要|不用|禁止|别).{0,4}(?:改写|优化|修改).{0,4}提示词|原样(?:使用|发送)", current):
        rewrite = False
    prompt = current
    if same_type and previous.get("prompt") and output in {"image", "video"}:
        prompt = previous["prompt"] + "\n本轮修改要求：" + current
        if len(prompt) > 12000:
            output = "clarify"
    if draft:
        prompt = draft["prompt"]
        # Preserve literal additional requirements without having AI rewrite the approved draft.
        if re.search(r"改|换|增加|减少|不要|不用|\d", current):
            prompt += "\n本轮补充要求：" + current
    elif missing_draft:
        output = "clarify"
    creation = dict(previous)
    if output in {"image", "video"}:
        creation = {
            "type": output,
            "model_id": model_id,
            "options": options,
            "reference_attachment_ids": references,
            "prompt": prompt,
            "rewrite": bool(rewrite),
            "reference_preference": previous.get("reference_preference", False),
            "source_message_id": previous.get("source_message_id"),
        }
    elif output == "text" and reference_change in {"select", "clear"}:
        creation["reference_attachment_ids"] = references
        creation["reference_preference"] = True
    return {
        "output": output,
        "creation": creation,
        "prompt": prompt,
        "model": response.get("model"),
        "confidence": answers.get("output", {}).get("confidence"),
        "rewrite": bool(rewrite),
    }


async def prepare_route(session, task):
    """No media bytes, credentials or full conversation are sent to Jev."""
    if task.request_payload.get("scope") != "personal":
        return None
    if task.request_payload.get("jev_route"):
        return task.request_payload["jev_route"]
    if task.request_payload.get("mode") != "chat":
        return None
    config = await get_jev_configuration(session, task.tenant_id)
    if not config.enabled or not config.api_key:
        return None
    chat = await session.get(AgentChatSession, task.request_payload.get("agent_chat_session_id"))
    message = await session.get(AgentChatMessage, task.request_payload.get("user_message_id"))
    if (
        not chat
        or not message
        or chat.user_id != task.user_id
        or chat.tenant_id != task.tenant_id
        or message.session_id != chat.id
    ):
        raise RuntimeError("会话路由归属不匹配")
    history = (
        await session.scalars(
            select(AgentChatMessage)
            .where(
                AgentChatMessage.session_id == chat.id,
                AgentChatMessage.created_at < message.created_at,
            )
            .order_by(AgentChatMessage.created_at.desc())
            .limit(40)
        )
    ).all()
    attachment_pairs = (
        await session.execute(
            select(PersonalAgentAttachment, AgentChatMessage.role)
            .join(AgentChatMessage, AgentChatMessage.id == PersonalAgentAttachment.message_id)
            .where(
                PersonalAgentAttachment.session_id == chat.id,
                PersonalAgentAttachment.user_id == task.user_id,
                PersonalAgentAttachment.tenant_id == task.tenant_id,
                PersonalAgentAttachment.message_id.is_not(None),
                PersonalAgentAttachment.created_at <= message.created_at,
                PersonalAgentAttachment.mime_type.like("image/%"),
            )
            .order_by(PersonalAgentAttachment.created_at, PersonalAgentAttachment.id)
        )
    ).all()
    catalog = []
    ordinals = {"user": 0, "assistant": 0}
    for attachment, role in attachment_pairs:
        role = getattr(role, "value", role)
        ordinals[role] = ordinals.get(role, 0) + 1
        catalog.append(
            {
                "id": attachment.id,
                "ordinal": ordinals[role],
                "source": "uploaded" if role == "user" else "generated",
            }
        )
    models = (
        await session.scalars(
            select(AIModel)
            .join(Provider, Provider.id == AIModel.provider_id)
            .where(
                AIModel.tenant_id == task.tenant_id,
                Provider.tenant_id == task.tenant_id,
                AIModel.enabled.is_(True),
                Provider.enabled.is_(True),
                AIModel.model_type.in_([ModelType.IMAGE, ModelType.VIDEO]),
            )
        )
    ).all()
    creation = (chat.runtime_manifest or {}).get("media_creation")
    if creation is None:
        # Existing conversations predate this router. Seed from stored results, never from prose guesses.
        for historic in history:
            manifest = historic.runtime_manifest or {}
            media = manifest.get("generated_media") or []
            if not media:
                continue
            latest = media[-1]
            kind = "image" if str(latest.get("mime_type", "")).startswith("image/") else "video"
            creation = {
                "type": kind,
                "model_id": latest.get("model_id"),
                "prompt": latest.get("prompt") or "",
                "rewrite": False,
                "options": {
                    key: latest[key]
                    for key in ("resolution", "aspect_ratio", "duration_seconds")
                    if latest.get(key) is not None
                },
                "reference_attachment_ids": (manifest.get("media_intent") or {}).get(
                    "reference_attachment_ids", []
                ),
            }
            if kind == "image":
                creation["last_generated_attachment_id"] = latest.get("id")
            break
    summary = await session.scalar(
        select(AgentChatSummary)
        .where(
            AgentChatSummary.session_id == chat.id,
            AgentChatSummary.tenant_id == task.tenant_id,
            AgentChatSummary.user_id == task.user_id,
            AgentChatSummary.created_at <= message.created_at,
        )
        .order_by(AgentChatSummary.version.desc())
        .limit(1)
    )
    proposed_plan = latest_plan(
        list(reversed(history)),
        catalog,
        [
            {
                "id": m.id,
                "name": m.name,
                "model_id": m.model_id,
                "type": m.model_type.value,
                "is_default": m.is_default,
            }
            for m in models
        ],
    )
    saved_plan = (chat.runtime_manifest or {}).get("media_plan")
    plan = (
        saved_plan
        if saved_plan
        and (
            not proposed_plan or saved_plan.get("source_message_id") == proposed_plan.get("source_message_id")
        )
        else proposed_plan
    )
    state = {
        "message": message.content,
        "history": [{"role": str(m.role), "content": m.content[:1600]} for m in reversed(history[:10])],
        "conversation_summary": summary.content[-4000:] if summary else "",
        "creation": creation or {},
        "media_plan": plan,
        "prompt_draft": latest_prompt_draft(list(reversed(history))),
        "attachments": catalog,
        "current_attachment_ids": task.request_payload.get("attachment_ids", []),
        "selected_skills": bool(task.request_payload.get("selected_skill_ids")),
        "models": [
            {
                "id": m.id,
                "name": m.name,
                "model_id": m.model_id,
                "type": m.model_type.value,
                "is_default": m.is_default,
            }
            for m in models
        ],
    }
    # Bound remote context without changing the actual upload ordinals or dropping pinned references.
    _, numbered_ids = explicit_references(state)
    keep_ids = set(
        numbered_ids
        + state["current_attachment_ids"]
        + list(state["creation"].get("reference_attachment_ids") or [])
        + list((state.get("prompt_draft") or {}).get("reference_attachment_ids") or [])
    )
    generated_id = state["creation"].get("last_generated_attachment_id")
    if plan:
        keep_ids.update(a for step in plan["steps"] for a in step.get("reference_attachment_ids", []))
    if generated_id:
        keep_ids.add(generated_id)
    state["attachments"] = [
        a for a in catalog if a["id"] in keep_ids or a in catalog[:8] or a in catalog[-16:]
    ]
    await session.commit()
    try:
        if len(message.content) > 12000:
            raise ValueError("routing input budget exceeded")
        result = merge_decision(
            state, await evaluate(state, config=config, use_laya=False), threshold=config.route_confidence
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        # Exact confirmation of a saved plan needs no probabilistic intent guess.
        # Normal evaluation still resolves user-authorized automatic chaining.
        if plan and plan.get("status") not in {"completed", "cancelled"} and execution_confirmation(message.content):
            result = merge_decision(state, {"answers": {}}, threshold=config.route_confidence)
        else:
            result = {"output": "clarify", "creation": state["creation"], "reason": "router_unavailable"}
    task.request_payload = {**task.request_payload, "jev_route": result}
    chat.runtime_manifest = {**(chat.runtime_manifest or {}), "media_creation": result["creation"]}
    if result.get("media_plan") or plan:
        chat.runtime_manifest = {**chat.runtime_manifest, "media_plan": result.get("media_plan") or plan}
    await session.commit()
    return result
