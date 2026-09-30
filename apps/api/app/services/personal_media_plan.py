"""Verbatim staged media proposals and durable, independently billed continuations."""

import re

from app.services.personal_media_drafts import extract_prompt, prompt_options


def execution_confirmation(text):
    return bool(
        re.fullmatch(
            r"\s*(?:废话少说[,， ]*|好[的了]?[,， ]*)?(?:那就|那你|你)?(?:倒是|直接|赶紧|现在|请)?(?:全部|全都|都)?"
            r"(?:开始|继续|执行|提交|按默认来)(?:执行|生成|推进)?"
            r"(?:任务|下一步|剩余步骤)?(?:吧|啊|呀)?[。！!\s]*",
            text,
        )
    )


def continuation_policy(state):
    """Only user instructions may authorize a chain; assistant promises are not consent."""
    texts = [str(item.get("content", "")) for item in state.get("history", [])
             if str(item.get("role", "")).lower() in {"user", "agentmessagerole.user"}]
    texts.append(state["message"])
    for text in reversed(texts):
        if re.search(r"只(?:先)?(?:做|生成|执行)|先.{0,16}(?:给我看|让我看)|(?:等我|我来)确认|不要自动|别自动|(?:不要|别)全部", text):
            return False
        if re.search(r"(?:全部|全都).{0,4}(?:开始|执行|生成)|自动.{0,6}(?:继续|续接|执行|生成)|(?:图片|生图).{0,10}(?:完成|生成后).{0,12}(?:视频|下一步)|先.{0,12}(?:出图|生成.{0,4}图).{0,8}(?:然后|再).{0,8}(?:生成)?视频", text):
            return True
    return None


def authorizes_plan_start(text):
    if execution_confirmation(text):
        return True
    # A request for a plan/prompt is not permission to render its proposed steps.
    if re.search(r"提示词|方案|不要|别生成|先写|只写|等我|给我看", text):
        return False
    return bool(re.search(r"先.{0,12}(?:出图|生成.{0,4}图).{0,8}(?:然后|再).{0,8}(?:生成)?视频", text))


def latest_plan(messages, catalog, models):
    for message in reversed(messages):
        if getattr(message.role, "value", message.role) != "assistant":
            continue
        if (message.runtime_manifest or {}).get("generated_media"):
            continue
        sections = list(
            re.finditer(r"^#{1,3}\s*第\s*([1-8一二三四五六七八])\s*步[^\n]*", message.content, re.M)
        )
        steps = []
        for index, heading in enumerate(sections):
            section = message.content[
                heading.start() : sections[index + 1].start()
                if index + 1 < len(sections)
                else len(message.content)
            ]
            prompt = extract_prompt(section)
            if not prompt or not 20 <= len(prompt) <= 12000:
                steps = []
                break
            kind = (
                "video"
                if "视频" in heading.group()
                else "image"
                if re.search("图|首帧", heading.group())
                else None
            )
            if not kind:
                steps = []
                break
            ids = list(dict.fromkeys(re.findall(r"\b[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\b", section)))
            references = ids
            needs_reference = bool(re.search(r"参考图|参考图片|图生图", section))
            # Mentioning the absence of references does not require an upload.
            no_reference = bool(re.search(r"text_to_image|文生图|(?:参考图|参考图片)(?:\s*ID)?\s*[:：]\s*无|无任何附件", section))
            if no_reference and not references and not re.search(r"图生图|image_to_image", section):
                needs_reference = False
            if not references and needs_reference and index == 0:
                numbers = re.findall(r"参考图\s*([1-9一二三四五六七八九])", section)
                for number in numbers:
                    ordinal = int(number) if number.isdigit() else "一二三四五六七八九".index(number) + 1
                    match = next(
                        (
                            item
                            for item in catalog
                            if item.get("source") == "uploaded" and item.get("ordinal") == ordinal
                        ),
                        None,
                    )
                    references.append(match["id"] if match else f"missing-upload-{ordinal}")
                if not numbers and len(catalog) == 1:
                    references = [catalog[0]["id"]]
            model = next(
                (
                    m["id"]
                    for m in sorted(
                        models, key=lambda m: (len(m["model_id"]), bool(m.get("is_default"))), reverse=True
                    )
                    if m["type"] == kind and (m["model_id"] in section or m["name"] in section)
                ),
                None,
            )
            steps.append(
                {
                    "type": kind,
                    "prompt": prompt,
                    "options": prompt_options(section, kind),
                    "model_id": model,
                    "reference_attachment_ids": references,
                    "missing_reference": index == 0 and needs_reference and not references,
                    "use_previous_image": index > 0 and bool(re.search(r"上一步|第\s*1\s*步|首帧", section)),
                    "rewrite": False,
                    "source_message_id": message.id,
                }
            )
        if steps:
            return {
                "source_message_id": message.id,
                "steps": steps,
                "index": 0,
                "status": "proposed",
                "auto_continue": False,
            }
    return None


async def save_proposed_plan(db, chat, message):
    """Persist an assistant proposal immediately, without authorizing paid execution."""
    if not re.search(r"^#{1,3}\s*第\s*[1-8一二三四五六七八]\s*步", message.content, re.M):
        return None
    from sqlalchemy import select
    from app.db.models import AIModel, Provider, PersonalAgentAttachment, AgentChatMessage

    attachments = (await db.execute(select(PersonalAgentAttachment, AgentChatMessage.role).join(
        AgentChatMessage, AgentChatMessage.id == PersonalAgentAttachment.message_id,
    ).where(
        PersonalAgentAttachment.session_id == chat.id,
        PersonalAgentAttachment.tenant_id == chat.tenant_id,
        PersonalAgentAttachment.user_id == chat.user_id,
        PersonalAgentAttachment.mime_type.like("image/%"),
    ).order_by(PersonalAgentAttachment.created_at, PersonalAgentAttachment.id))).all()
    models = (await db.scalars(select(AIModel).join(Provider).where(
        AIModel.tenant_id == chat.tenant_id, Provider.tenant_id == chat.tenant_id,
        AIModel.enabled.is_(True), Provider.enabled.is_(True),
    ))).all()
    # UUID references are validated again by the router before execution.
    catalog = []
    ordinals = {"uploaded": 0, "generated": 0}
    for attachment, role in attachments:
        source = "uploaded" if getattr(role, "value", role) == "user" else "generated"
        ordinals[source] += 1
        catalog.append({"id": attachment.id, "source": source, "ordinal": ordinals[source]})
    plan = latest_plan([message], catalog, [
        {"id": m.id, "name": m.name, "model_id": m.model_id,
         "type": m.model_type.value, "is_default": m.is_default} for m in models
    ])
    if plan:
        from app.db.models import AgentMessageRole
        history = (await db.scalars(select(AgentChatMessage).where(
            AgentChatMessage.session_id == chat.id,
            AgentChatMessage.tenant_id == chat.tenant_id,
            AgentChatMessage.user_id == chat.user_id,
            AgentChatMessage.role == AgentMessageRole.USER,
        ).order_by(AgentChatMessage.created_at.desc()).limit(10))).all()
        policy = continuation_policy({"message": "", "history": [
            {"role": "user", "content": item.content} for item in reversed(history)
        ]})
        if policy is not None:
            plan["auto_continue"] = policy
        chat.runtime_manifest = {**(chat.runtime_manifest or {}), "media_plan": plan}
    return plan


def route_step(plan, output=None):
    index = int(plan.get("index", 0))
    steps = plan["steps"]
    if output and steps[index]["type"] != output:
        return None
    step = dict(steps[index])
    return {
        "output": step["type"],
        "creation": step,
        "prompt": step["prompt"],
        "rewrite": False,
        "review_prompt": steps[index - 1]["prompt"] if index > 0 and step.get("use_previous_image") else None,
        "media_plan": plan,
        "confidence": 1.0,
        "source": "confirmed_media_plan",
    }


async def advance_plan(db, task, chat, user, media):
    """Run inside the completed step's transaction: save output and queue next atomically."""
    plan = (task.request_payload.get("jev_route") or {}).get("media_plan")
    if not plan:
        return None
    index = int(plan.get("index", 0))
    completed = list(plan.get("completed", []))
    completed.append({"index": index, "task_id": task.id, "attachment_id": media["id"]})
    plan = {**plan, "completed": completed, "index": index + 1}
    if index + 1 >= len(plan["steps"]):
        plan["status"] = "completed"
    else:
        steps = [dict(step) for step in plan["steps"]]
        if steps[index + 1].get("use_previous_image"):
            if not str(media["mime_type"]).startswith("image/"):
                plan["status"] = "blocked"
                plan["reason"] = "下一步需要图片参考，当前步骤没有生成图片"
                chat.runtime_manifest = {**(chat.runtime_manifest or {}), "media_plan": plan}
                return None
            steps[index + 1]["reference_attachment_ids"] = [media["id"]]
        plan["steps"] = steps
        plan["status"] = "ready"
    chat.runtime_manifest = {**(chat.runtime_manifest or {}), "media_plan": plan}
    if plan["status"] != "ready" or not plan.get("auto_continue"):
        return None
    return await queue_plan_step(db, task, chat, user, plan)


async def queue_plan_step(db, task, chat, user, plan):
    """Queue the current saved step for both initial execution and continuation."""
    import hashlib
    from sqlalchemy import select
    from app.api.routes.agent_chat import default_personal_media_model
    from app.db.models import AgentChatMessage, AgentMessageRole, ModelType, PersonalAgentAttachment, new_id
    from app.services.billing import resolve_task_pricing
    from app.services.task_submission import create_queued_task

    if (task.result_payload or {}).get("media_plan_next_task_id"):
        return None
    index = int(plan.get("index", 0))
    if plan.get("status") not in {"ready", "proposed"} or not 0 <= index < len(plan["steps"]):
        return None
    step = plan["steps"][index]
    refs = step["reference_attachment_ids"]
    valid = set((await db.scalars(select(PersonalAgentAttachment.id).where(
        PersonalAgentAttachment.id.in_(refs), PersonalAgentAttachment.session_id == chat.id,
        PersonalAgentAttachment.user_id == user.id, PersonalAgentAttachment.tenant_id == user.tenant_id,
        PersonalAgentAttachment.mime_type.like("image/%"),
    ))).all()) if refs else set()
    if step.get("missing_reference") or len(refs) > 4 or any(ref not in valid for ref in refs):
        raise ValueError("当前步骤参考图片缺失，请重新选择参考图片")
    model, _ = await default_personal_media_model(
        db,
        tenant_id=task.tenant_id,
        model_type=ModelType.IMAGE if step["type"] == "image" else ModelType.VIDEO,
        model_id=step.get("model_id"),
        resolution=step["options"].get("resolution"),
    )
    pricing = await resolve_task_pricing(
        db,
        tenant_id=task.tenant_id,
        task_type="asset_image_generation" if step["type"] == "image" else "shot_video_generation",
    )
    label = "图片" if step["type"] == "image" else "视频"
    content = f"【系统自动续接】执行已授权方案第 {index + 1}/{len(plan['steps'])} 步：生成{label}。"
    message = AgentChatMessage(
        id=new_id(),
        tenant_id=user.tenant_id,
        user_id=user.id,
        session_id=chat.id,
        role=AgentMessageRole.USER,
        content=content,
        runtime_manifest={"automatic_continuation": True, "source_task_id": task.id},
    )
    db.add(message)
    route = route_step(plan)
    next_task, event = await create_queued_task(
        db,
        user=user,
        project_id=None,
        task_type="agent_chat_run",
        model_id=model.id,
        cost=pricing.total_cost,
        request_payload={
            **task.request_payload,
            "mode": step["type"],
            "user_message_id": message.id,
            "media_model_id": model.id,
            "media_options": step["options"],
            "media_generation_mode": ("image_to_" if step["reference_attachment_ids"] else "text_to_")
            + step["type"],
            "attachment_ids": step["reference_attachment_ids"],
            "jev_route": route,
            "prompt_hash": hashlib.sha256(content.encode()).hexdigest(),
            "pricing": pricing.as_payload(),
            "media_plan_parent_task_id": task.id,
            "media_plan_review_prompt": plan["steps"][index - 1]["prompt"]
            if index > 0 and step.get("use_previous_image")
            else None,
        },
        message=f"连续创作第 {index + 1}/{len(plan['steps'])} 步已排队",
    )
    message.run_id = next_task.id
    task.result_payload = {**(task.result_payload or {}), "media_plan_next_task_id": next_task.id}
    chat.runtime_manifest = {**(chat.runtime_manifest or {}), "media_plan": plan}
    return next_task, event
