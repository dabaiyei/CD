"""Explicitly resume one paused storyboard review after investigating its cause.

Uses normal queue/account/checkpoint services. Never approves findings, resets
review history, changes content, or touches an already running workflow.
"""
import argparse
import asyncio
import json
import copy

from sqlalchemy import select
from app.db.models import (DirectorWorkflowRun, DirectorWorkflowStatus,
    DirectorWorkflowStage, DirectorDecisionRequest, DirectorChildRun, AITask, TaskStatus)
from app.db.session import SessionLocal, engine
from app.services.director_orchestration import _queue_child, _queue_review, _commit_and_dispatch


async def main(args):
    async with SessionLocal() as session:
        workflow = await session.get(DirectorWorkflowRun, args.workflow_id)
        if workflow is None:
            raise ValueError("Workflow not found")
        if args.latest_review:
            live = await session.scalar(select(AITask.id).join(
                DirectorChildRun, DirectorChildRun.task_id == AITask.id).where(
                    DirectorChildRun.workflow_id == workflow.id,
                    AITask.status.in_([TaskStatus.QUEUED, TaskStatus.RUNNING])).limit(1))
            review_task = await session.get(AITask, args.latest_review)
            if (workflow.status != DirectorWorkflowStatus.FAILED or workflow.current_task_id
                    or workflow.stop_requested or live or review_task is None
                    or review_task.status != TaskStatus.SUCCEEDED
                    or review_task.task_type != "director_storyboard_review"
                    or review_task.request_payload.get("workflow_id") != workflow.id
                    or review_task.request_payload.get("storyboard_version_id") != workflow.storyboard_version_id
                    or review_task.tenant_id != workflow.tenant_id or review_task.user_id != workflow.user_id):
                raise ValueError("Requires an idle failed workflow and a completed review of its current board")
            verdict = (review_task.result_payload or {}).get("review", review_task.result_payload or {})
            if verdict.get("approved") or not verdict.get("findings"):
                raise ValueError("Review must contain unresolved findings")
            if not args.apply:
                print(json.dumps({"dry_run": True, "review_task": review_task.id}))
                return
            parent = await session.get(DirectorChildRun, review_task.request_payload["child_run_id"])
            workflow.stage = DirectorWorkflowStage.STORYBOARD_REPAIRING
            task, event, _ = await _queue_child(session, workflow, kind="storyboard_repair",
                task_type="director_storyboard_repair", request_payload={
                    "chapter_id": workflow.chapter_id, "repair_mode": "partial", "feedback": args.reason,
                    "script_version_id": workflow.script_version_id,
                    "storyboard_version_id": workflow.storyboard_version_id,
                    "review": copy.deepcopy(verdict), "automatic_review": True,
                    "version_count": int((workflow.context_snapshot or {}).get("storyboard_version_count") or 1),
                    "diagnostic_resume_reason": args.reason,
                }, message="重复调度修复后，按最新分镜审核继续定点修复", parent=parent)
            await _commit_and_dispatch(session, [(task, event)])
            print(json.dumps({"workflow": workflow.id, "task": task.id, "status": "queued"}))
            return
        if args.failed_task:
            failed = await session.get(AITask, args.failed_task)
            if (workflow.status != DirectorWorkflowStatus.FAILED or workflow.current_task_id
                    or workflow.stop_requested or failed is None or failed.status != TaskStatus.FAILED
                    or failed.task_type != "director_storyboard_repair"
                    or failed.request_payload.get("workflow_id") != workflow.id
                    or failed.tenant_id != workflow.tenant_id or failed.user_id != workflow.user_id):
                raise ValueError("Only the failed repair in this idle workflow may resume")
            child = await session.get(DirectorChildRun, failed.request_payload["child_run_id"])
            parent = await session.get(DirectorChildRun, child.parent_child_run_id) if child.parent_child_run_id else None
            if not args.apply:
                print(json.dumps({"dry_run": True, "failed_task": failed.id}))
                return
            workflow.stage = DirectorWorkflowStage.STORYBOARD_REPAIRING
            task, event, _ = await _queue_child(session, workflow, kind="storyboard_repair",
                task_type="director_storyboard_repair", request_payload=copy.deepcopy(failed.request_payload),
                message="运行时修正后从断点继续分镜修复", parent=parent)
            payload = copy.deepcopy(task.request_payload)
            payload["diagnostic_resume_reason"] = args.reason
            if args.direct_output_shot:
                state = payload["storyboard_generation"]["state"]
                if args.direct_output_shot in state.get("repaired_indices", []):
                    raise ValueError("Cannot change response mode of an already repaired shot")
                state.setdefault("direct_output_repairs", {})[str(args.direct_output_shot)] = True
            task.request_payload = payload
            await _commit_and_dispatch(session, [(task, event)])
            print(json.dumps({"workflow": workflow.id, "task": task.id, "status": "queued"}))
            return
        if (workflow.status != DirectorWorkflowStatus.WAITING_USER
                or workflow.stage != DirectorWorkflowStage.AWAITING_STORYBOARD_DECISION
                or workflow.current_task_id or workflow.stop_requested):
            raise ValueError("Only an idle storyboard-review decision can be resumed")
        decision = await session.scalar(select(DirectorDecisionRequest).where(
            DirectorDecisionRequest.workflow_id == workflow.id,
            DirectorDecisionRequest.resolved.is_(False),
            DirectorDecisionRequest.decision_type == "storyboard_review"))
        if decision is None:
            raise ValueError("No pending storyboard review decision")
        parent = await session.get(DirectorChildRun, decision.child_run_id)
        if parent is None:
            raise ValueError("Review child run missing")
        payload = {"chapter_id": workflow.chapter_id, "repair_mode": "partial",
            "feedback": args.reason, "script_version_id": workflow.script_version_id,
            "storyboard_version_id": workflow.storyboard_version_id,
            "review": parent.details, "automatic_review": True,
            "version_count": int((workflow.context_snapshot or {}).get("storyboard_version_count") or 1)}
        if args.additional_shot:
            # Explicit diagnostic targets supplement, never rewrite, the saved
            # review. The normal patch/validation/review path still owns writes.
            payload["review"] = copy.deepcopy(payload["review"])
            verdict = payload["review"].get("review", payload["review"])
            for index in args.additional_shot:
                verdict.setdefault("findings", []).append({"severity": "major",
                    "shot_indices": [index], "context_shot_indices": [],
                    "fields": args.additional_fields, "location": "诊断定位",
                    "issue": args.reason, "suggestion": args.reason})
        if not args.apply:
            print(json.dumps({"workflow": workflow.id, "action": "partial_repair",
                              "decision": decision.id, "dry_run": True}))
            return
        decision.resolved = True
        decision.selected_option = "provide_feedback" if args.recheck else "partial_repair"
        decision.feedback = args.reason
        if args.recheck:
            task, event = await _queue_review(session, workflow, target="storyboard", parent=parent)
            await _commit_and_dispatch(session, [(task, event)])
            print(json.dumps({"workflow": workflow.id, "task": task.id, "status": "queued_review"}))
            return
        workflow.stage = DirectorWorkflowStage.STORYBOARD_REPAIRING
        task, event, _ = await _queue_child(session, workflow, kind="storyboard_repair",
            task_type="director_storyboard_repair", request_payload=payload,
            message="故障修正后继续分镜定点修复", parent=parent)
        await _commit_and_dispatch(session, [(task, event)])
        print(json.dumps({"workflow": workflow.id, "task": task.id, "status": "queued"}))
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow_id")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--failed-task")
    parser.add_argument("--latest-review", help="Resume an idle failed run using a verified current-board review")
    parser.add_argument("--direct-output-shot", type=int)
    parser.add_argument("--recheck", action="store_true")
    parser.add_argument("--additional-shot", type=int, action="append", default=[])
    parser.add_argument("--additional-fields", nargs="+", default=[])
    asyncio.run(main(parser.parse_args()))
