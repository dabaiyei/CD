"""Bounded, resumable storyboard reviews with explicit repair targets."""
from __future__ import annotations

import hashlib
import asyncio
import json
import re
import time

import httpx

from app.services.storyboard_generation import REPAIR_FIELDS, match_existing_shots, parse_finding_targets, segment_script

REVIEW_RULES = """分批分镜审核协议（优先于模板中的整章输出描述）：
仅审核本批待审核镜头；相邻镜头和资产沿革只用作证据，不是待修改对象。
逐条对照提供的剧本正文，检查资产身份、首帧与动作、空间、时间轴、打斗、表情。
source 是候选剧本源段，匹配仅为检索线索，不代表严格一一对应。同一场景可能跨多个批次，
不能因本批未讲完整场就报遗漏。coverage_only 批次只检查该源段在候选镜头中的覆盖，
若关联不确定，不猜测遗漏。只对有明确证据的遗漏给出补充位置和具体内容。
review_fragment 是超长镜头的原始 JSON 文本分片，只审核该片中可见的问题，不能将分片外的字段判为缺失。
一个视频片段允许多个内部短镜头，模型最低时长约束视频片段总长，不约束内部切镜。
当前审核的是分镜规划：video_prompt 为空是正确状态；最终视频提示词和详细打斗编排由后续阶段生成。
审核战斗时间窗、人物关系、动作意图和结果，不得要求在此提前写出最终逐招提示词。
不因个人审美或数字字符串/数字的表示差异报错。不要为凑问题重复提出无实质影响的意见。
每条问题给出 shot_indices（确实需要修改的全章镜号）、fields（需要修改的字段名）、
context_shot_indices（仅供对照的镜号）、location、issue、suggestion、severity。
只输出 JSON：{"approved":true,"summary":"结论","findings":[]}。
存在 blocking/major 时 approved 必须为 false；建议应是最小可执行修复，不输出新分镜。
"""


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()


class ReviewScopeError(ValueError):
    """Requires source evidence; a format-only repair cannot invent targets."""


class ReviewFieldError(ValueError):
    """Repair the field labels in the saved review, not its source evidence."""


class ReviewIncompleteError(ReviewScopeError):
    """The model stopped before producing an evidence-backed verdict."""


REVIEW_ATTEMPT_TIMEOUT = 300.0
REVIEW_PROGRESS_INTERVAL = 30.0


async def run_review_attempt(runtime, request, progress, label):
    """Bound one model/tool run and show activity without exposing tool bodies."""
    started = time.monotonic()
    activity = {"phase": "等待模型响应", "models": 0, "tools": 0}

    async def on_event(event):
        kind = event.get("type")
        if kind == "MODEL_CALL_START":
            activity["models"] += 1
            activity["phase"] = "模型分析中"
        elif kind == "TOOL_CALL_START":
            activity["tools"] += 1
            activity["phase"] = "读取证据：" + str(event.get("tool_call_name") or "工具")[:40]
        elif kind == "TOOL_RESULT_END":
            activity["phase"] = "证据已返回，等待模型判断"
        elif kind == "TEXT_BLOCK_DELTA":
            activity["phase"] = "正在输出审核结论"

    async def execute():
        if hasattr(runtime, "run_stream"):
            return await runtime.run_stream(request, on_event)
        return await runtime.run(request)

    pending = asyncio.create_task(execute())
    try:
        while True:
            remaining = REVIEW_ATTEMPT_TIMEOUT - (time.monotonic() - started)
            if remaining <= 0:
                raise RuntimeError(f"单批审核超过 {REVIEW_ATTEMPT_TIMEOUT:g} 秒，已中断本次调用，保留其他批次")
            done, _ = await asyncio.wait({pending}, timeout=min(REVIEW_PROGRESS_INTERVAL, remaining))
            if done:
                return await pending
            elapsed = int(time.monotonic() - started)
            await progress(f"{label} · {activity['phase']} · 已等待 {elapsed} 秒"
                           f"（模型 {activity['models']} 轮 / 工具 {activity['tools']} 次）")
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


# Keep this outside REVIEW_RULES so paid, completed review cache keys survive.
REVIEW_COMPLETION_CONTRACT = (
    "\n必须完成本批审核后再结束回复，只返回审核 JSON；"
    "‘我将读取/检查文件’等计划或进度说明不是审核结果。"
    "未发现具体问题时 approved=true、findings=[]；"
    "判定不通过必须列出有证据的具体问题、镜号与修复字段。"
    "不要用 approved=false、findings=[] 表示尚未读取或尚未完成。"
)


# Separate from REVIEW_RULES so existing paid review fingerprints remain valid.
# This only clarifies representation; it does not change the review criteria.
FIELD_OUTPUT_CONTRACT = (
    "\nfields 必须是字段名字符串数组，即使只有一个字段也使用数组。合法字段仅限："
    + ", ".join(sorted(REPAIR_FIELDS))
    + '。资产绑定修改统一写 ["asset_names"]；输入中的 asset_ids 是已持久化绑定，'
      '对应同一资产选择，不是新的修复字段。嵌套问题填写顶层字段，例如 '
      '["frame_layout"]，不要填写 frame_layout.spatial_relations。不得删除审核问题以绕过格式校验。'
)


def normalize_review_fields(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ReviewFieldError("审核 fields 必须为字段名数组。" + FIELD_OUTPUT_CONTRACT)
    result = []
    for field in value:
        if not isinstance(field, str):
            raise ReviewFieldError("审核 fields 中每项必须是字段名字符串。" + FIELD_OUTPUT_CONTRACT)
        field = field.strip().strip('`')
        # Map storage/creation names to the same binding; never infer a target
        # from prose or silently drop an unknown field.
        field = "asset_names" if field == "asset_ids" else field
        nested = re.fullmatch(r"(frame_layout|combat_plan|emotion_plan|internal_shots)(?:\.[A-Za-z_]\w*|\[\d+\])+", field)
        if nested:
            field = nested[1]
        if field not in REPAIR_FIELDS:
            raise ReviewFieldError(f"审核字段 {field!r} 无法识别。" + FIELD_OUTPUT_CONTRACT)
        if field not in result:
            result.append(field)
    return result


def preserve_review_findings(original: str, repaired: dict) -> None:
    """A JSON repair cannot erase recoverable findings or downgrade severity."""
    decoder = json.JSONDecoder()
    findings = []
    for position, character in enumerate(original):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(original[position:])
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        if value.get("approved") is False and repaired["approved"]:
            raise ReviewScopeError("格式修复不能将不通过结论改为通过，需重新审核本批")
        if isinstance(value.get("issue"), str) and value.get("severity") in {"blocking", "major", "minor"}:
            findings.append(value)
    levels = {"minor": 0, "major": 1, "blocking": 2}
    for finding in findings:
        if not any(other["issue"] == finding["issue"]
                   and levels[other["severity"]] >= levels[finding["severity"]]
                   for other in repaired["findings"]):
            raise ReviewScopeError("格式修复删除或降级了已有问题，需重新审核本批")


def compact_neighbor(row: dict) -> dict:
    return {key: (value[:300] if isinstance(value, str) else
                  json.dumps(value, ensure_ascii=False)[:500] if isinstance(value, (dict, list)) else value)
            for key, value in row.items()
            if key in {"order_index", "title", "scene_description", "action_description",
                       "asset_names", "continuity_group", "frame_layout", "dialogue",
                       "combat_plan", "emotion_plan", "internal_shots"}}


def review_units(shots: list[dict], script: str, *, budget: int = 14000, max_shots: int = 6) -> list[dict]:
    """Each shot and every source scene is reviewed; boundaries overlap read-only."""
    segments = segment_script(script)
    ownership = match_existing_shots(segments, shots)
    groups = [(segment["content"], ownership[segment["key"]]) for segment in segments]
    if not groups:
        groups = [("", shots)]
    positions = {row["order_index"]: i for i, row in enumerate(shots)}
    units = []
    for group_index, (source, rows) in enumerate(groups):
        coverage_only = not rows
        if not rows:
            # Retrieval is heuristic. An unmatched paragraph is not proof of a
            # missing scene; ask for evidence against nearby candidate shots.
            before = [r for _, previous in groups[:group_index] for r in previous][-3:]
            after = [r for _, following in groups[group_index + 1:] for r in following][:3]
            rows = before + after or shots[:6]
        chunks, current, size = [], [], len(source)
        for row in rows:
            row = {key: value for key, value in row.items() if key not in {"video_prompt", "combat_design"}}
            serialized = json.dumps(row, ensure_ascii=False)
            length = len(serialized)
            if length + len(source) > budget:
                if current:
                    chunks.append(current)
                    current, size = [], len(source)
                # Preserve every byte of unusually long shot descriptions without
                # letting one shot defeat the review's request budget.
                width = max(1000, (budget - len(source) - 1000) // 2)
                pieces = [serialized[i:i + width] for i in range(0, len(serialized), width)]
                for part, piece in enumerate(pieces, 1):
                    chunks.append([{"order_index": row["order_index"], "title": str(row.get("title", ""))[:120],
                                    "review_fragment": piece, "fragment_index": part,
                                    "fragment_count": len(pieces)}])
                continue
            if current and (len(current) >= max_shots or size + length > budget):
                chunks.append(current)
                current, size = [], len(source)
            current.append(row)
            size += length
        if current:
            chunks.append(current)
        for chunk in chunks:
            neighbors = []
            if chunk:
                first, last = positions[chunk[0]["order_index"]], positions[chunk[-1]["order_index"]]
                neighbors = [compact_neighbor(shots[i]) for i in (first - 1, last + 1)
                             if 0 <= i < len(shots)]
            first_index = chunk[0]["order_index"]
            assets = {name for row in chunk for name in row.get("asset_names", [])}
            history = {}
            for row in shots[:positions[first_index]]:
                for name in assets.intersection(row.get("asset_names", [])):
                    history[name] = row["order_index"]
            # One previous observation per referenced asset, not all earlier shots.
            previous_indices = sorted(set(history.values()))[-6:]
            units.append({"source": source, "shots": chunk, "neighbors": neighbors,
                          "asset_history": [compact_neighbor(shots[positions[i]]) for i in previous_indices],
                          "coverage_only": coverage_only,
                          "scene_shot_indices": [row["order_index"] for row in rows]})
    return units


def parse_review(text: str, indices: set[int], all_count: int) -> dict:
    from app.services.task_worker import DirectorReviewPayload, parse_json_object

    try:
        payload = parse_json_object(text)
    except RuntimeError as exc:
        # A planning sentence is not a malformed review. Formatting it with no
        # evidence fabricates a verdict and repeatedly poisons the raw cache.
        if not re.search(r'"(?:approved|issue)"\s*:', text):
            raise ReviewIncompleteError("模型只返回了计划或说明，尚未给出审核结论") from exc
        raise
    if ("approved" not in payload and not payload.get("findings")
            and not re.search(r'"(?:approved|issue)"\s*:', text)):
        raise ReviewIncompleteError("模型尚未给出审核结论")
    if isinstance(payload.get("findings"), list):
        for finding in payload["findings"]:
            if isinstance(finding, dict) and "fields" in finding:
                finding["fields"] = normalize_review_fields(finding["fields"])
    review = DirectorReviewPayload.model_validate(payload).model_dump(mode="json")
    for finding in review["findings"]:
        named = set(parse_finding_targets([finding], all_count))
        if not named or not named <= indices:
            raise ReviewScopeError("审核问题需明确定位本批待审核镜号，不能把对照镜头或整片作为修改对象")
        finding["shot_indices"] = sorted(named)
        finding["location"] = "镜头 " + "、".join(str(i) for i in sorted(named))
    if any(f["severity"] in {"blocking", "major"} for f in review["findings"]):
        review["approved"] = False
    if not review["approved"] and not review["findings"]:
        raise ReviewIncompleteError("未通过审核必须给出具体问题，当前审核尚未完成")
    return review


async def review_board(request, runtime_factory, *, shots, script, state, save, progress, concurrency=2):
    """Checkpoint each review immediately; fingerprints include adjacent context and rules."""
    from app.services.task_worker import DirectorReviewPayload

    shots = [{**row, "order_index": index} for index, row in enumerate(shots, 1)]
    if request.tool_mode == "retrieval":
        # Scope the filesystem as well as the visible prompt. Other shots are
        # exposed only through this unit's continuity evidence below.
        request = request.model_copy(update={"project_files": [file for file in request.project_files
            if not file.id.startswith(("retrieval-chapter-shots", "retrieval-chapter-script", "retrieval-chapter-source"))]})
        catalog = next((file for file in request.project_files if file.id == "retrieval-project-assets"), None)
        if catalog:
            names = {item["id"]: item["name"] for item in json.loads(catalog.content)}
            shots = [{**row, "asset_names": row.get("asset_names") or [names[key] for key in row.get("asset_ids", []) if key in names]}
                     for row in shots]
    if not shots:
        raise ValueError("没有可审核的分镜，不能将空分镜判定为通过")
    # No arbitrary crop of shot fields: frame/combat/emotion plans remain intact.
    units = review_units(shots, script, max_shots=1 if request.tool_mode == "retrieval" else 6)
    state["total_units"] = len(units)
    cached = state.setdefault("completed", {})
    raw_results = state.setdefault("raw", {})
    manifest = state.get("manifest", {})
    rules_key = fingerprint([REVIEW_RULES, request.system_prompt, request.prompt, request.model_binding,
                             request.skill_versions, request.prompt_versions, request.memory_context])
    if request.tool_mode == "retrieval":
        rules_key = fingerprint([rules_key, [(file.id, file.sha256) for file in request.project_files]])
    def unit_key(unit):
        key = fingerprint([rules_key, unit])
        if request.tool_mode == "retrieval" and unit["coverage_only"]:
            coverage_rows = [row for row in shots if row["order_index"] in unit["scene_shot_indices"]]
            key = fingerprint([key, coverage_rows])
        return key

    used_keys = {unit_key(unit) for unit in units}
    # Older board/rule fingerprints are not completed units of this review.
    # Keep all matching results, but do not count obsolete cache entries.
    cached = state["completed"] = {key: value for key, value in cached.items() if key in used_keys}
    raw_results = state["raw"] = {key: value for key, value in raw_results.items() if key in used_keys}
    state["attempt_errors"] = {key: value for key, value in state.get("attempt_errors", {}).items()
                               if key in used_keys}
    all_indices = {row["order_index"] for row in shots}
    save_lock = asyncio.Lock()

    async def persist():
        async with save_lock:
            state["manifest"] = manifest
            await save(state)

    async def check_unit(number, unit):
        nonlocal manifest
        indices = {row["order_index"] for row in unit["shots"]}
        key = unit_key(unit)
        coverage_rows = [row for row in shots if row["order_index"] in unit["scene_shot_indices"]]
        if key in cached:
            try:
                review = parse_review(json.dumps(cached[key], ensure_ascii=False), indices, len(shots))
                await progress(f"复用第 {number}/{len(units)} 批审核结果")
                return review
            except (ValueError, TypeError, RuntimeError):
                cached.pop(key, None)
        scoped_request = request
        visible_unit = unit
        if request.tool_mode == "retrieval":
            from app.services.retrieval_context import evidence_file, json_evidence, mount
            source = evidence_file("review-source", unit["source"])
            neighbors = json_evidence("review-context", {"neighbors": unit["neighbors"], "asset_history": unit["asset_history"]})
            target = json_evidence("review-shot", unit["shots"])
            scoped_request = mount(request, source, neighbors, target)
            coverage_file = None
            if unit["coverage_only"]:
                coverage_file = json_evidence("review-coverage", coverage_rows)
                scoped_request = mount(scoped_request, coverage_file)
            visible_unit = {"shot_indices": sorted(indices), "shot_file": target.path,
                "source_file": source.path, "continuity_file": neighbors.path,
                "coverage_only": unit["coverage_only"], "scene_shot_indices": unit["scene_shot_indices"],
                "coverage_evidence_file": coverage_file.path if coverage_file else None}
        prompt = request.prompt + "\n" + REVIEW_RULES + FIELD_OUTPUT_CONTRACT + "\n本批数据：" + json.dumps(visible_unit, ensure_ascii=False)
        if request.tool_mode == "retrieval":
            prompt += "\n只审核指定的一个镜头：优先使用下方预取证据；未提供预取证据时 Read shot_file，按疑点检索 source_file。仅当检查衔接或资产沿革且预取证据不足时读取 continuity_file。coverage_only 时先检索 coverage_evidence_file 中的其他候选镜头排除已覆盖内容，再定位本镜问题；不要因当前一个镜头没有讲完场景就报告遗漏。缺失证据时继续搜索，不得把未读内容判定为遗漏。"
            # Deliver one small, complete evidence unit, never an entire board.
            # Larger units remain searchable files instead of being truncated.
            evidence = json.dumps(unit, ensure_ascii=False)
            if len(evidence) <= 12000:
                prompt += ("\n当前镜头证据已预取如下，此内容与上述文件一致，无须再次 Read 相同内容。"
                           "仅对尚缺的规则、具体疑点继续检索；不重复浏览目录或全部手册。\n"
                           "<current-shot-evidence>\n" + evidence + "\n</current-shot-evidence>")
        hint = ""
        incomplete_response = False
        for attempt in range(3):
            raw = raw_results.get(key)
            format_error = None
            if raw:
                try:
                    review = parse_review(raw["response"], indices, len(shots))
                    manifest = raw.get("manifest", manifest)
                    break
                except ReviewScopeError as error:
                    # A wrong/missing target needs a real review, not a guessed
                    # mirror number or the silent removal of a blocking issue.
                    hint = f"\n上次响应错误，仅重新审核本批并修正输出：{str(error)[:1000]}"
                    incomplete_response = isinstance(error, ReviewIncompleteError)
                    raw_results.pop(key, None)
                    raw = None
                    await persist()
                except (ValueError, TypeError, RuntimeError) as error:
                    format_error = str(error)[:1500]
                    if len(raw["response"]) > 24000:
                        raw_results.pop(key, None)
                        raw = None
                        await persist()
            await progress(f"审核第 {number}/{len(units)} 批（镜头 {min(indices)}–{max(indices)}）"
                           + (f"，本批重试 {attempt}/2" if attempt else ""))
            current = scoped_request.model_copy(update={
                "prompt": prompt + hint,
                "system_prompt": request.system_prompt + "\n" + REVIEW_RULES + FIELD_OUTPUT_CONTRACT + REVIEW_COMPLETION_CONTRACT,
                "session_id": f"{request.session_id}-review-{key[:12]}",
                "state_mode": "ephemeral", "tool_mode": request.tool_mode, "recent_messages": [],
                "conversation_summary": None,
            })
            if incomplete_response and "<current-shot-evidence>" not in prompt:
                # Some relay models end after announcing Read without invoking
                # it. Supply only this bounded unit, never the entire board or
                # all handbooks; retrieval remains available for other evidence.
                evidence = json.dumps(unit, ensure_ascii=False)
                if len(evidence) <= 18000:
                    current = current.model_copy(update={"prompt": current.prompt
                        + "\n本批审核未完成，以下直接提供本批证据，请完成判断，不再仅说明读取计划：\n"
                        + evidence})
            if raw and format_error:
                await progress(f"仅修复第 {number}/{len(units)} 批审核返回格式，不重新审核分镜")
                current = current.model_copy(update={
                    "system_prompt": "你是JSON格式修复器。只整理已有审核结论，不进行创作或重新审核。"
                        "保留每一项问题、严重程度和不通过结论，不得删除问题或杜撰镜号。"
                        "无法确定的内容保持原样，由平台重新审核。只返回符合Schema的JSON。",
                    "prompt": "审核返回格式修复\n输出Schema："
                        + json.dumps(DirectorReviewPayload.model_json_schema(), ensure_ascii=False)
                        + FIELD_OUTPUT_CONTRACT
                        + f"\n校验错误：{format_error}\n已有审核输出：\n{raw['response']}",
                    "memory_context": [], "skills": [], "project_files": [], "attachments": [],
                    "tool_mode": "none",
                })
            try:
                response = await run_review_attempt(runtime_factory(), current, progress,
                    f"审核第 {number}/{len(units)} 批 · 第 {attempt + 1}/3 次尝试")
                manifest = response.manifest
                try:
                    review = parse_review(response.final_response, indices, len(shots))
                except (RuntimeError, ValueError, TypeError) as parse_error:
                    diagnostics = state.setdefault("attempt_errors", {})
                    diagnostics[key] = {
                        "batch": number, "shot_indices": sorted(indices), "attempt": attempt + 1,
                        "error": str(parse_error)[:500],
                        "finish_reason": getattr(response, "finish_reason", None),
                        "tool_calls": sum(event.get("type") == "TOOL_CALL_START"
                                          for event in getattr(response, "events", [])),
                        "response_preview": response.final_response[:800],
                    }
                    if hasattr(runtime_factory, "invalid_output"):
                        runtime_factory.invalid_output()
                    # Do not replace the original malformed review with another
                    # malformed format repair; keep its original conclusions.
                    if isinstance(parse_error, ReviewIncompleteError):
                        incomplete_response = True
                        raw_results.pop(key, None)
                    elif not (raw and format_error):
                        raw_results[key] = {"response": response.final_response, "manifest": manifest}
                    await persist()
                    raise
                if raw and format_error:
                    try:
                        preserve_review_findings(raw["response"], review)
                    except ReviewScopeError:
                        raw_results.pop(key, None)
                        await persist()
                        raise
                break
            except (RuntimeError, ValueError, TypeError, httpx.TransportError) as error:
                if "已停止" in str(error) or "上下文已失效" in str(error):
                    raise
                diagnostics = state.setdefault("attempt_errors", {})
                previous = diagnostics.get(key, {})
                diagnostics[key] = {
                    **(previous if previous.get("attempt") == attempt + 1 else {}),
                    "batch": number, "shot_indices": sorted(indices),
                    "attempt": attempt + 1, "error": str(error)[:500],
                }
                await persist()
                if attempt == 2:
                    raise RuntimeError(f"第 {number} 批审核暂未完成，已保存其他批次：{error}") from error
                await progress(f"第 {number}/{len(units)} 批未完成：{str(error).splitlines()[0][:160]}；仅重试本批")
                hint = f"\n上次响应错误，仅重新审核本批并修正输出：{str(error)[:1000]}"
        async with save_lock:
            cached[key] = review
            state.get("attempt_errors", {}).pop(key, None)
            raw_results.pop(key, None)
            state["manifest"] = manifest
            await save(state)
        await progress(f"第 {number}/{len(units)} 批已保存："
                       + ("审核通过" if review["approved"] else f"发现 {len(review['findings'])} 项问题")
                       + f"；当前已保存 {len(cached)}/{len(units)} 批")
        return review

    pending = iter(enumerate(units, 1))
    results, errors = {}, []
    stopped = False
    consecutive_failures = 0

    async def worker():
        nonlocal stopped, consecutive_failures
        while not stopped:
            item = next(pending, None)
            if item is None:
                return
            number, unit = item
            try:
                results[number] = await check_unit(number, unit)
                consecutive_failures = 0
            except Exception as error:
                # Reviews are independent: a persistently failing batch must
                # not prevent later batches from finishing and checkpointing.
                # Cancellation/context loss and storage errors still halt work.
                if not isinstance(error, RuntimeError) or "已停止" in str(error) or "上下文已失效" in str(error):
                    stopped = True
                consecutive_failures += 1
                if consecutive_failures >= 2:
                    # A provider-wide outage must not spend three retries on
                    # every remaining batch in a long chapter.
                    stopped = True
                errors.append(error)

    await asyncio.gather(*(worker() for _ in range(max(1, min(concurrency, 8)))))
    if errors:
        raise errors[0]
    reviews = [results[number] for number in sorted(results)]
    findings, seen = [], set()
    for review in reviews:
        for finding in review["findings"]:
            identity = fingerprint([finding.get("shot_indices"), finding["issue"]])
            if identity not in seen:
                seen.add(identity)
                findings.append(finding)
    approved = all(review["approved"] for review in reviews)
    summary = f"已完成 {len(units)} 批、{len(all_indices)} 镜审核，发现 {len(findings)} 项问题"
    # Keep all blocking findings first if the public contract's limit is reached.
    findings.sort(key=lambda f: {"blocking": 0, "major": 1, "minor": 2}[f["severity"]])
    output = DirectorReviewPayload.model_validate({"approved": approved, "summary": summary,
                                                   "findings": findings[:100]})
    state["completed"] = {key: cached[key] for key in used_keys if key in cached}
    state["raw"] = {key: value for key, value in raw_results.items() if key in used_keys}
    await save(state)
    return output, manifest
