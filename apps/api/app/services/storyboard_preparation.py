"""Versioned local evidence packets for single-inference storyboard generation."""
import hashlib
import json
import re

VERSION = "prepared-storyboards-v1"
# Local preparation budget for one storyboard generation/repair request.
# This is intentionally larger than the provider-independent default so a
# single repair batch can keep its complete saved review context. The
# provider's own context limit is still enforced by the provider adapter.
INPUT_BUDGET = 200000


class StoryboardInputBudgetError(ValueError):
    """Deterministic local failure; retrying the same packet cannot help."""


def budget_blocked(message):
    return '[storyboard_input_budget]' in str(message or '')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def logical_files(request):
    groups = {}
    for file in request.project_files:
        key = re.sub(r"-part-\d+$", "", file.id)
        groups.setdefault(key, []).append(file)
    result = {}
    for key, files in groups.items():
        parts = sorted((f for f in files if f.id != key), key=lambda f: f.id)
        result[key] = "".join(f.content for f in parts) if parts else files[0].content
    return result


def reference_sections(text):
    heading, current = "本阶段参考", []
    for line in text.splitlines():
        if line.startswith("#"):
            if current:
                yield heading, "\n".join(current)
                current = []
            heading = line.lstrip("# ")
        current.append(line)
        if len("\n".join(current)) >= 1200:
            yield heading, "\n".join(current)
            current = []
    if current:
        yield heading, "\n".join(current)


def prepare(request, state):
    files = logical_files(request)
    contents = {key: text for key, text in files.items()
                if key.startswith(("retrieval-task-rules", "retrieval-task-references", "retrieval-task-memory"))}
    fingerprint = digest([VERSION, contents, request.prompt_versions, request.skill_versions])
    existing = state.get("preparation", {})
    if existing.get("fingerprint") == fingerprint:
        return existing
    rules = contents.get("retrieval-task-rules", request.system_prompt)
    sections = []
    for key, text in contents.items():
        if key == "retrieval-task-rules":
            continue
        for title, body in reference_sections(text):
            sections.append({"id": digest([key, title, body])[:16], "title": title,
                             "text": body, "kind": key})
    packet = {"version": VERSION, "fingerprint": fingerprint, "rules": rules, "sections": sections}
    state["preparation"] = packet
    return packet


def source_units(batch_key, source):
    """Stable source spans, not an LLM summary that can silently omit events."""
    units = []
    prefix = "src-" + digest([batch_key, source])[:8]
    for match in re.finditer(r"[^。！？!?；;\n]+[。！？!?；;]?", source):
        text = match.group().strip()
        if not text:
            continue
        for start in range(0, len(text), 400):
            piece = text[start:start + 400]
            units.append({"id": f"{prefix}-{len(units) + 1:03d}", "text": piece})
    return units


def output_capacity(binding):
    value = binding.get("max_tokens")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return max(1, min(4, int(value) // 2200))
    return 3  # Unknown provider defaults: small output pages, not a guessed API parameter.


def source_window(spans, missing, budget=2400):
    """Take a sequential prefix; subsequent pages retain every remaining span."""
    result, used = [], 0
    for span in spans:
        if span['id'] not in missing:
            continue
        cost = len(json.dumps(span, ensure_ascii=False)) + 2
        if result and used + cost > budget:
            break
        result.append(span)
        used += cost
    return result


def compact_generation_input(prompt):
    """Bound repair context while retaining source and shot-count evidence."""
    value = prompt
    scene_marker = "\n本场正文：\n"
    start = value.find(scene_marker)
    if start >= 0:
        ends = [value.find(marker, start + len(scene_marker)) for marker in
                ("\n跨批次衔接契约", "\n原分镜中本场")]
        ends = [item for item in ends if item >= 0]
        end = min(ends) if ends else len(value)
        value = value[:start] + "\n本场正文已由下方定向源文窗口提供。" + value[end:]
    existing_marker = "\n原分镜中本场"
    start = value.find(existing_marker)
    if start >= 0:
        end_markers = ("\n跨批次衔接契约", "\n本场必须")
        ends = [value.find(marker, start + len(existing_marker)) for marker in end_markers]
        ends = [item for item in ends if item >= 0]
        end = min(ends) if ends else len(value)
        raw_start = value.find("：\n", start, end)
        summary = "\n已有镜头仅保留结构摘要，完整内容已在平台断点中保存；不得删除或减少这些镜头："
        if raw_start >= 0:
            try:
                rows = json.loads(value[raw_start + 2:end])
                if isinstance(rows, list):
                    rows = [{key: row.get(key) for key in ("order_index", "title", "duration_seconds")
                             if key in row} for row in rows if isinstance(row, dict)]
                    summary += json.dumps(rows, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                summary += "请保持原有镜头数量"
        value = value[:start] + summary + value[end:]
    return value


def _terms(text):
    words = set(re.findall(r"[a-zA-Z0-9_-]{3,}", text.lower()))
    for word in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(word[i:i + 2] for i in range(len(word) - 1))
    return words


def direct_request(request, prompt, preparation, query, *, requested=()):
    """Retrieve exact reference excerpts in code; the generator gets no tools."""
    sections = preparation["sections"]
    by_id = {s["id"]: s for s in sections}
    if any(identifier not in by_id for identifier in requested):
        raise ValueError("模型请求了不存在的资料编号；不能自由扩展检索范围")
    terms = _terms(query)
    ranked = sorted(sections, key=lambda s: len(terms & _terms(s["title"] + s["text"])), reverse=True)
    system = (preparation["rules"] + "\n【定向生成执行契约】本次证据由平台准备，覆盖旧的工具检索要求。"
              "不调用工具、不搜索文件、不输出计划，直接返回JSON。模型能力与原文优先；手册片段仅作创作约束。"
              "本次局部输出/分页协议优先于上文通用模板。请求原文分页时，shots每项必须含source_ids字符串数组；"
              "这属于平台必填溯源字段，即使旧模板Schema未列出也必须输出，精确复制待覆盖原文id。"
              "不能把一个原文编号标为已覆盖却没有呈现对应事件、动作或台词。"
              "若确实缺少某项适用规则，只返回{\"needs_context\":[\"资料id\"],\"reason\":\"具体缺项\"}，"
              "仅能请求下方索引中确切id，不能要求整章、全部手册或无关资料。")
    # The index is navigation, not evidence. Repeating previews of every manual
    # consumed 18K characters in a real chapter before any excerpts were selected.
    # Keep the caller's complete source and repair context intact. If the
    # enlarged local budget is exceeded, fail deterministically instead of
    # silently deleting source text from the request.
    room = INPUT_BUDGET - len(system) - len(prompt) - 200
    if room < 0:
        raise StoryboardInputBudgetError(
            f"[storyboard_input_budget] 本批原文与硬约束超过上下文预算："
            f"硬约束{len(system)}字，本批输入{len(prompt)}字，上限{INPUT_BUDGET}字；"
            "已保留成果，需调整输入后继续，重复请求无法解决")
    required = [by_id[i] for i in dict.fromkeys(requested)]
    required_cost = sum(len(json.dumps(s, ensure_ascii=False)) + 2 for s in required)
    index_budget = min(2400, max(0, room - max(4000, required_cost)))
    index, index_used = [], 2
    for section in required + ranked:
        if any(item['id'] == section['id'] for item in index):
            continue
        item = {'id': section['id'], 'title': section['title'][:80]}
        cost = len(json.dumps(item, ensure_ascii=False)) + 2
        if index_used + cost <= index_budget:
            index.append(item)
            index_used += cost
    prefix = prompt + "\n可定向补充的本阶段相关资料索引：" + json.dumps(index, ensure_ascii=False)
    available = min(11000, INPUT_BUDGET - len(system) - len(prefix) - 200)
    selected, used = [], 0
    for section in [by_id[i] for i in dict.fromkeys(requested)] + ranked:
        if section in selected:
            continue
        cost = len(json.dumps(section, ensure_ascii=False))
        if used + cost > available:
            if section["id"] in requested:
                raise StoryboardInputBudgetError("[storyboard_input_budget] 指定资料超过本批上下文预算，需缩小本批源段")
            continue
        selected.append(section)
        used += cost
    if available < 0:
        raise StoryboardInputBudgetError("[storyboard_input_budget] 本批原文与硬约束超过上下文预算，需缩小本批源段")
    text = prefix + "\n本批已检索的规则原文：" + json.dumps(selected, ensure_ascii=False)
    return request.model_copy(update={"system_prompt": system, "prompt": text,
        "tool_mode": "none", "state_mode": "ephemeral", "project_files": [], "skills": [],
        "memory_context": [], "recent_messages": [], "conversation_summary": None})


def coverage(rows, units):
    expected = {u["id"] for u in units}
    covered = set()
    for row in rows:
        ids = row.get("source_ids") if isinstance(row, dict) else None
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in expected for i in ids):
            raise ValueError("每个新镜头须引用本批实际source_ids，不能用镜头数量代替原文覆盖")
        covered.update(ids)
    return expected - covered


def mapping_request(request, rows, units):
    prompt = ('只补原文关联，不改写镜头。对每个现有片段标出实际呈现的原文id，不能虚报覆盖。'
              '未呈现的原文留空，不能为了通过校验标记。'
              '返回JSON {"mappings":[{"row":1,"source_ids":["精确原文id"]}]}，row从1开始。'
              '\n原文：' + json.dumps(units, ensure_ascii=False)
              + '\n现有片段：' + json.dumps(rows, ensure_ascii=False))
    if len(prompt) > INPUT_BUDGET:
        raise ValueError('本页关联修复输入过长，已保留已有输出')
    return request.model_copy(update={'prompt': prompt, 'system_prompt': '你只负责基于实际内容补充原文关联，返回指定JSON。',
        'tool_mode': 'none', 'state_mode': 'ephemeral', 'project_files': [], 'skills': [],
        'memory_context': [], 'recent_messages': [], 'conversation_summary': None,
        'documents': [], 'attachments': []})


def merge_mapping(rows, units, raw):
    value = json.loads(raw.strip().removeprefix('```json').removesuffix('```').strip())
    mappings = value.get('mappings', []) if isinstance(value, dict) else []
    if (not isinstance(mappings, list) or len(mappings) != len(rows)
            or any(not isinstance(m, dict) or type(m.get('row')) is not int for m in mappings)
            or {m['row'] for m in mappings} != set(range(1, len(rows) + 1))):
        raise ValueError('原文关联修复未逐一定位现有片段')
    by_row = {m['row']: m.get('source_ids') for m in mappings}
    merged = [{**r, 'source_ids': by_row[i]} for i, r in enumerate(rows, 1)]
    coverage(merged, units)
    return merged
