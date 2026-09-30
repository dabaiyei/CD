"""Rebuild a real failed chat on a disposable DB copy; never call a model or retry the task."""

import argparse
import asyncio
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument(
        "--route",
        action="store_true",
        help="Re-evaluate JEV on the copy; no media generation",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "apps/api"))
    sys.path.insert(0, str(root / "services/agent-runtime/agentscope"))
    with tempfile.TemporaryDirectory(prefix="chat-contract-") as directory:
        copied = Path(directory) / "probe.db"
        with (
            closing(
                sqlite3.connect((root / "cineforge.db").as_uri() + "?mode=ro", uri=True)
            ) as source,
            closing(sqlite3.connect(copied)) as target,
        ):
            source.backup(target)
        os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + copied.as_posix()
        from app.db.models import AITask, TaskStatus
        from app.db.session import SessionLocal, engine
        from app.services.task_worker import WORKER_ID, agent_chat_runtime_request
        from runtime.contracts import AgentRunRequest

        async def run():
            try:
                async with SessionLocal() as db:
                    task = await db.get(AITask, args.task_id)
                    if task is None:
                        raise RuntimeError("Task not found")
                    task.status = TaskStatus.RUNNING
                    task.worker_id = WORKER_ID
                    if args.route:
                        task.request_payload = {
                            k: v
                            for k, v in task.request_payload.items()
                            if k != "jev_route"
                        }
                        task.request_payload = {**task.request_payload, "mode": "chat"}
                    await db.commit()
                    if args.route:
                        from app.services.personal_routing import prepare_route

                        route = await prepare_route(db, task)
                        print(
                            "Route:",
                            route["output"],
                            "plan_steps:",
                            len((route.get("media_plan") or {}).get("steps", [])),
                        )
                        print(
                            "References:",
                            route["creation"].get("reference_attachment_ids"),
                        )
                        print(
                            "Prompt chars:",
                            len(route.get("prompt") or ""),
                            "source:",
                            route.get("source"),
                        )
                request, _, _ = await agent_chat_runtime_request(args.task_id)
                AgentRunRequest.model_validate(request.model_dump(mode="json"))
                print("Actual task request: runtime contract valid")
                print("History messages:", len(request.recent_messages))
                print(
                    "Files:",
                    len(request.project_files),
                    "Images:",
                    len(request.attachments),
                )
            finally:
                await engine.dispose()

        asyncio.run(run())


if __name__ == "__main__":
    main()
