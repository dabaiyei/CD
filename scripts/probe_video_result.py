"""Read an existing provider job, without submitting or changing any task."""

import asyncio
import sys
from urllib.parse import urlsplit

from app.db.models import AIModel, AITask, Provider
from app.db.session import SessionLocal
from app.services.task_worker import default_gateway_factory


def describe(value, path="response"):
    if isinstance(value, dict):
        for key, child in value.items():
            if any(
                word in key.lower()
                for word in ("token", "authorization", "secret", "prompt", "key")
            ):
                continue
            describe(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value[:5]):
            describe(child, f"{path}[{index}]")
    elif isinstance(value, str) and value.startswith(("http://", "https://")):
        url = urlsplit(value)
        print(path, "URL", url.hostname, url.path[-100:])
    else:
        print(path, str(value)[:220])


async def main():
    async with SessionLocal() as db:
        task = await db.get(AITask, sys.argv[1])
        if task is None:
            raise ValueError("Task not found")
        payload, state = task.request_payload or {}, task.result_payload or {}
        model = await db.get(
            AIModel,
            payload.get("media_model_id")
            or payload.get("options", {}).get("video_model_id")
            or task.model_id,
        )
        provider = await db.get(Provider, model.provider_id)
        gateway = default_gateway_factory(provider)
        job = state.get("provider_job_id")
        if not job:
            raise ValueError("No persisted provider job ID")
        print("model", model.model_id, "provider", provider.name, "job", job)
        if gateway.adapter and gateway.adapter.video:
            poll = gateway.adapter.video.poll
            if poll is None or poll.method != "GET":
                raise ValueError("Only configured read-only polling is supported")
            print("mapping", gateway.adapter.video.response.model_dump())
            body, client = await gateway._adapter_request(
                poll, {"job_id": job, "credentials": gateway.credentials}
            )
            try:
                describe(body)
                if "--verify" in sys.argv:
                    result = await gateway._adapter_video_result(
                        client,
                        body,
                        gateway.adapter.video.response,
                        fallback_job_id=job,
                    )
                    print(
                        "parsed",
                        result.status,
                        "bytes",
                        len(result.video_data or b""),
                        "content_type",
                        result.content_type,
                    )
            finally:
                await client.aclose()
        else:
            import httpx

            template = str(
                model.capabilities.get("status_endpoint") or "videos/{job_id}"
            )
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.get(
                    gateway.base_url
                    + "/"
                    + template.replace("{job_id}", job).lstrip("/"),
                    headers=gateway.headers,
                )
                response.raise_for_status()
                describe(response.json())


if __name__ == "__main__":
    asyncio.run(main())
