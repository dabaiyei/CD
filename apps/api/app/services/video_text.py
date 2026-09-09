"""Separate editorial subtitle annotations from visible words and spoken dialogue."""

import json
import re

_TEXT = re.compile(r"【(?P<meta>(?:后期文字|画面文字|字幕)(?:[｜|][^】]*)?)】\s*(?P<text>[^【\r\n]+)")


def video_text_tracks(dialogue: str) -> tuple[str, list[dict[str, str]]]:
    tracks = []

    def extract(match):
        parts = re.split(r"[｜|]", match.group("meta"))
        tracks.append(
            {
                "text": match.group("text").strip(),
                "timing": next((part for part in parts[1:] if re.search(r"\d.*秒", part)), ""),
                "speaker_context": "｜".join(part for part in parts[1:] if not re.search(r"\d.*秒", part)),
            }
        )
        return ""

    return _TEXT.sub(extract, dialogue).strip(), tracks


def ensure_screen_text_locks(prompt: str, dialogue: str) -> str:
    _, tracks = video_text_tracks(dialogue)
    # Old prompts may already contain verbatim production labels.
    prompt = re.sub(r"【(?:后期文字|画面文字|字幕)(?:[｜|][^】]*)?】\s*", "", prompt)
    if not tracks:
        return prompt
    rule = (
        "画面文字输出规则：下列列表仅 text 字段的值是实际字幕，逐字显示其中文正文。"
        "timing 仅表示时间，speaker_context 仅表示人物或内心语境，均不得显示或朗读。"
        "不显示字段名、制作标签、方括号、时间码或人物标签；这些字幕不是新增配音指令。"
        "本条覆盖泛化的禁止字幕要求，但不得新增清单外的文字。" + json.dumps(tracks, ensure_ascii=False)
    )
    if rule in prompt:
        return prompt
    for marker in ("integrated_multimodal_description:", "detailed_description:"):
        if marker in prompt:
            return prompt.replace(marker, f"{marker} {rule}\n", 1)
    return f"{prompt}\n\n{rule}"
