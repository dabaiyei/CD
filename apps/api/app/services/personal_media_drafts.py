"""Recover verbatim prompt drafts from conversation, independently of summary memory."""

import re


def refers_to_draft(message):
    return bool(
        re.search(
            r"(?:这[个份段些]?|上[面轮述]|前面|刚才|之前|你写的).{0,18}提示词|按.{0,8}(?:这个|刚才|上面).{0,8}(?:生成|做)|直接(?:生成|出片)(?:吧|就行|即可)?[。！!\s]*$",
            message,
        )
    )


def prompt_options(prompt, kind):
    options = {}
    ratio = re.search(r"(?<!\d)(16:9|9:16|1:1|4:3|3:4|21:9)(?!\d)", prompt)
    resolution = re.search(
        r"(?i)(?<![a-z0-9])([124]K)(?![a-z0-9])"
        if kind == "image"
        else r"(?i)(?<![a-z0-9])((?:480|540|720|1080|1440|2160)P)(?![a-z0-9])",
        prompt,
    )
    # Duration metadata wins over action timeline fragments such as 0–4 seconds.
    duration = re.search(r"(?:时长|持续|共|全长|参数)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*秒", prompt)
    if not duration:
        duration = re.search(r"(?<![\d–—\-])\b(\d+(?:\.\d+)?)\s*秒(?:一镜到底|镜头|视频)", prompt)
    if ratio:
        options["aspect_ratio"] = ratio[1]
    if resolution:
        options["resolution"] = resolution[1].upper()
    if duration and kind == "video":
        options["duration_seconds"] = float(duration[1])
    return options


def extract_prompt(content):
    # Prefer the explicitly labelled Chinese primary prompt, without English variants/advice.
    label = re.search(r"(?:中文|主版本|主)提示词[^\n]*\n", content)
    source = content[label.end() :] if label else content
    blocks = re.findall(r"(?:^>[^\n]*(?:\n|$))+", source, flags=re.M)
    if blocks:
        return re.sub(r"^>\s?", "", blocks[0], flags=re.M).strip()
    fences = re.findall(r"```[^\n]*\n(.*?)```", source, flags=re.S)
    if fences:
        return fences[0].strip()
    if label:
        return re.split(r"\n(?:#{1,3}\s|\*\*(?:英文|备选|参数)|---)", source, maxsplit=1)[0].strip()
    return None


def latest_prompt_draft(messages):
    """Messages are oldest first; retain source IDs for audit and attachment ownership checks."""
    draft = None
    user = None
    refs = []
    for message in messages:
        role = getattr(message.role, "value", message.role)
        if role == "user":
            user = message
            uploaded = [
                a["id"]
                for a in (message.runtime_manifest or {}).get("attachments", [])
                if str(a.get("mime_type", "")).startswith("image/")
            ]
            if uploaded:
                refs = uploaded
            continue
        if role != "assistant" or not user or "提示词" not in user.content:
            continue
        if (message.runtime_manifest or {}).get("generated_media"):
            continue
        prompt = extract_prompt(message.content)
        if not prompt or len(prompt) < 20 or len(prompt) > 12000:
            continue
        kind = "video" if "视频" in user.content or "镜头" in user.content else "image"
        options = prompt_options(prompt, kind)
        for key, value in prompt_options(user.content, kind).items():
            options[key] = value
        draft = {
            "type": kind,
            "prompt": prompt,
            "options": options,
            "model_id": None,
            "reference_attachment_ids": list(refs),
            "rewrite": False,
            "source_message_id": message.id,
        }
    return draft
