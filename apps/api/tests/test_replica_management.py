import asyncio

from sqlalchemy import select

from app.db.models import AITask, TaskStatus, User
from app.db.session import SessionLocal
from app.services.video_replica import ANALYZE


def test_task_center_cleanup_preserves_replica_checkpoint(client, admin_headers, creator_headers):
    async def setup():
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == "admin@cineforge.local"))
            task = AITask(
                tenant_id=user.tenant_id,
                user_id=user.id,
                task_type="video_replica_render",
                status=TaskStatus.FAILED,
                cost=0,
                request_payload={"analysis_id": "saved-parent"},
                result_payload={"clips": {"0": {"status": "succeeded", "key": "saved-video"}}},
            )
            db.add(task)
            await db.commit()
            return task.id

    task_id = asyncio.run(setup())
    path = f"/api/v1/tasks/{task_id}"
    assert client.delete(path, headers=creator_headers).status_code == 404
    for operation in (path, "/api/v1/tasks?group=terminal"):
        assert client.delete(operation, headers=admin_headers).status_code == 204
        assert task_id not in {
            x["id"] for x in client.get("/api/v1/tasks", headers=admin_headers).json()["items"]
        }
        saved = client.get(path, headers=admin_headers)
        assert saved.status_code == 200
        assert saved.json()["result_payload"]["clips"]["0"]["key"] == "saved-video"
        assert task_id in {
            x["id"] for x in client.get("/api/v1/video-replicas", headers=admin_headers).json()
        }
        resumed = client.post(path + "/retry", headers=admin_headers)
        assert resumed.status_code == 202, resumed.text
        assert task_id in {
            x["id"] for x in client.get("/api/v1/tasks", headers=admin_headers).json()["items"]
        }

        async def fail_again():
            async with SessionLocal() as db:
                task = await db.get(AITask, task_id)
                task.status = TaskStatus.FAILED
                await db.commit()

        asyncio.run(fail_again())


def test_replica_management_persistence_and_isolation(client, admin_headers, creator_headers):
    async def setup():
        async with SessionLocal() as db:
            user = await db.scalar(select(User).where(User.email == "admin@cineforge.local"))
            tasks = [
                AITask(
                    tenant_id=user.tenant_id,
                    user_id=user.id,
                    task_type=kind,
                    status=status,
                    cost=0,
                    request_payload={"filename": "original.mp4", "source_key": "keep"},
                    result_payload={"plan": {"summary": "keep"}},
                )
                for kind, status in [
                    (ANALYZE, TaskStatus.SUCCEEDED),
                    (ANALYZE, TaskStatus.RUNNING),
                    ("other", TaskStatus.SUCCEEDED),
                ]
            ]
            db.add_all(tasks)
            await db.commit()
            return [t.id for t in tasks]

    done, running, other = asyncio.run(setup())
    url = f"/api/v1/video-replicas/{done}"
    assert client.patch(url, headers=creator_headers, json={"display_name": "stolen"}).status_code == 404
    assert client.patch(url, headers=admin_headers, json={"display_name": "   "}).status_code == 422
    response = client.patch(url, headers=admin_headers, json={"display_name": " 我的作品 ", "archived": True})
    assert response.status_code == 200, response.text
    saved = client.get(f"/api/v1/tasks/{done}", headers=admin_headers).json()
    assert saved["request_payload"]["display_name"] == "我的作品"
    assert saved["request_payload"]["archived"] is True
    assert saved["request_payload"]["source_key"] == "keep"
    assert saved["result_payload"]["plan"]["summary"] == "keep"
    assert (
        client.patch(url, headers=admin_headers, json={"archived": False}).json()["request_payload"][
            "archived"
        ]
        is False
    )
    assert (
        client.patch(
            f"/api/v1/video-replicas/{running}", headers=admin_headers, json={"archived": True}
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/v1/video-replicas/{other}", headers=admin_headers, json={"display_name": "wrong"}
        ).status_code
        == 404
    )
    listing = client.get("/api/v1/video-replicas?limit=1", headers=admin_headers)
    assert listing.status_code == 200 and len(listing.json()) == 1
    assert client.get("/api/v1/video-replicas?limit=301", headers=admin_headers).status_code == 422
