"""Chinese, bounded System One requests; independent of Jev's prompt/calibration."""
import re
import httpx

# Output gate selected on 16 local calibration examples, then held out on 12.
# Other gates are conservative defaults, not claims of calibrated accuracy.
GATES = {"output": (0.95, 0.50), "continuation": (0.95, 0.50), "plan_action": (0.95, 0.50),
         "chain": (0.95, 0.50), "rewrite": (0.95, 0.50)}


def question(instructions, criteria):
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def explicit_generation_request(message):
    if re.search(r"如果|假如|怎么|如何|哪些|哪个模型|是什么|能不能|可以吗|失败了吗|进度|状态|查一下|检查一下", message):
        return False
    positive_text = re.sub(r"(?:不要|不用|别)(?:实际)?(?:再)?(?:生成|画|制作|做|出)?[^。！？!?\n]{0,8}(?:图片|图像|插画|海报|视频|短片|片段)", "", message)
    return bool(re.search(
        r"(?:帮我|请|直接|现在|给我)?\s*(?:生成|画|绘制|制作|做|出|生图)"
        r"(?:一张|一个|一段|一幅|张|个|段)?[^。！？!?\n]{0,24}"
        r"(?:图片|图像|插画|海报|視頻|视频|短片|片段|圖|图)", positive_text
    ) or re.search(r"(?:出图|生图|出片)(?:吧|啊|即可)?", positive_text))


def request_for(state, *, language="zh"):
    message = state["message"]
    actual_generation = explicit_generation_request(message)
    # Never silently truncate a user's negation/authorization at the tail.
    if len(message) > 1200:
        raise ValueError("Laya message budget exceeded; use fallback router")
    if language == "en":
        wire = {"Current user message": message}
        qs = {"output": question("What does the user want NOW? Writing a prompt/story or asking about media is text; only an actual render request is media.",
            {"text": "Text answer, discussion, analysis, story, or prompt only", "image": "Actually generate or edit an image now", "video": "Actually generate a video now", "clarify": "Intent is ambiguous"})}
    else:
        wire = {"本轮用户原话": message}
        qs = {"output": question("用户本轮要求交付什么？写提示词、故事、解释图片都是文字。",
            {"text": "文字回答、讨论、文案或提示词", "image": "实际生成或修改图片", "video": "实际生成视频", "clarify": "无法确定要什么"})}
    if state.get("creation"):
        qs["continuation"] = question("Does this message continue or modify the previous media work? A different subject is new.",
            {"continue": "Continue or modify the previous work", "new": "Start an independent new work"}) if language == "en" else question("本轮是否继续之前的作品？另一个主题是新任务。",
            {"continue": "沿用之前作品并修改或继续", "new": "全新的独立内容"})
        # Only a reference follow-up gets prior context. Don't contaminate new requests.
        if re.search(r"之前|刚才|上[一面]|这[个张段]|它|继续|改成|换成", message):
            previous = state.get("creation") or {}
            wire["Previous work type" if language == "en" else "已有作品类型"] = previous.get("type")
            wire["Previous work summary" if language == "en" else "已有作品摘要"] = str(previous.get("prompt") or "")[:160]
    plan = state.get("media_plan") or {}
    if plan and plan.get("status") not in {"completed", "cancelled"}:
        wire["Pending steps" if language == "en" else "待办步骤"] = [step.get("type") for step in plan.get("steps", [])][:8]
        wire["Next step number" if language == "en" else "下一步序号"] = int(plan.get("index", 0)) + 1
        qs["plan_action"] = question("What does the user want to do with the pending plan now?",
            {"execute": "Start or continue the next step now", "wait": "Discuss or wait for confirmation", "cancel": "Stop or cancel the plan"}) if language == "en" else question("用户现在要如何处理待办任务？",
            {"execute": "现在开始执行", "wait": "先讨论或等待确认", "cancel": "停止或取消任务"})
        qs["chain"] = question("Has the user authorized automatic execution of all later steps without pausing?",
            {"auto": "Run all remaining steps automatically", "step": "Run only this step and wait"}) if language == "en" else question("用户是否授权自动完成后续步骤？",
            {"auto": "全部执行，中途不等用户确认", "step": "只执行当前一步，后续等确认"})
    if re.search(r"提示词|优化|润色|改写|设计", message):
        qs["rewrite"] = question("Did the user explicitly ask the AI to write or change the media prompt?",
            {"yes": "Explicitly write, optimize, or rewrite it", "no": "No; or preserve it unchanged"}) if language == "en" else question("用户有没有明确要求AI编写或修改提示词？",
            {"yes": "明确要求编写、优化或改写", "no": "没要求改写，或要求保持原文"})
    return {"model": "auto", "state": wire, "questions": qs}


def decisions(state, response):
    from app.services.personal_media_plan import execution_confirmation, continuation_policy
    from app.services.personal_routing import explicit_references

    result = {"model": response.get("model"), "answers": {}, "usage": response.get("usage", {})}
    for key, answer in response.get("answers", {}).items():
        if key not in GATES or not isinstance(answer, dict):
            continue
        probabilities = answer.get("probabilities") or {}
        choice = answer.get("choice")
        score = probabilities.get(choice, 0)
        others = [v for k, v in probabilities.items() if k != choice and isinstance(v, (float, int))]
        floor, margin = GATES[key]
        result["answers"][key] = {**answer, "_laya_accepted": bool(
            isinstance(score, (float, int)) and 0 <= score <= 1 and score >= floor
            and score - max(others, default=1) >= margin)}
        # Consent and cancellation must not be authorized by this small classifier.
        if key in {"plan_action", "chain", "rewrite"}:
            result["answers"][key]["_laya_accepted"] = False

    def fixed(key, value):
        result["answers"][key] = {"type": "choice", "choice": value,
            "_laya_accepted": True, "source": "explicit_user_rule"}

    message = state["message"]
    actual_generation = explicit_generation_request(message)
    if re.fullmatch(r"\s*(?:请|先)?(?:取消|停止|暂停)(?:当前|这个|全部)?(?:任务|生成)?[，,。\s]*(?:不要继续生成)?[。！!\s]*", message):
        fixed("plan_action", "cancel")
        fixed("output", "text")
    elif execution_confirmation(message) and state.get("media_plan"):
        fixed("plan_action", "execute")
    # Protect explicit text deliverables and negated rendering without suppressing
    # 'write a prompt AND generate the image' instructions.
    status_question = bool(re.search(r"(?:失败了吗|完成了吗|進度|进度|任务状态|生成状态|查一下|检查一下).{0,8}(?:吗|了|如何|怎样)?", message))
    reference_preference = bool(re.fullmatch(r"(?:以后|后续)?(?:都)?参考第[一二三四1234]张(?:图|图片)[。！!\s]*", message))
    explicit_text = bool(re.search(r"(?:不要|不用|别)(?:实际)?生成(?:图片|视频)", message)
        or re.search(r"(?:只|先).{0,4}(?:写|讨论|分析)", message)
        or re.search(r"(?:写|生成).{0,12}(?:文案|故事|提示词)", message))
    if reference_preference or status_question or (explicit_text and not actual_generation):
        fixed("output", "text")
        fixed("plan_action", "wait")
    policy = continuation_policy(state)
    if policy is not None:
        fixed("chain", "auto" if policy else "step")
    change, ids = explicit_references(state)
    fixed("references", "select" if change == "select" else "clear" if change == "clear" else "keep")
    for ref in ids:
        fixed("ref_" + ref, "yes")
    return result


def complete_for(state, result):
    """Otherwise fall back as a whole; never mix contradictory model decisions."""
    from app.services.personal_routing import accepted
    answers = result.get("answers", {})
    action = answers.get("plan_action", {})
    plan = state.get("media_plan") or {}
    if (action.get("source") == "explicit_user_rule"
            and action.get("choice") in {"cancel", "execute"}
            and plan and plan.get("status") not in {"completed", "cancelled"}):
        return True
    output = accepted(answers, "output", {"text", "image", "video", "clarify"})
    if not output or output == "clarify":
        return False
    if state.get("media_plan"):
        # Let the existing plan-aware path resolve consent, even for short replies.
        return False
    if output in {"image", "video"}:
        if state.get("creation") and not accepted(answers, "continuation", {"continue", "new"}):
            return False
        if re.search(r"参考|第.{0,3}张|基于", state["message"]):
            return False
    return True


async def evaluate(state, *, base_url, api_key, timeout=8):
    payload = request_for(state)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        response = await client.post(base_url.rstrip("/") + "/v1/systemone",
            headers={"Authorization": "Bearer " + api_key}, json=payload)
        response.raise_for_status()
        return decisions(state, response.json())
