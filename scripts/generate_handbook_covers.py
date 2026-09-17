"""Generate missing pack covers through an existing configured application image model."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps/api"))

from PIL import Image

from app.db.models import AIModel, ModelType, Provider
from app.db.session import SessionLocal
from app.services.media_gateway import ImageGenerationRequest
from app.services.task_worker import default_gateway_factory


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", required=True, help="Database model ID, not provider model name")
    parser.add_argument("--kind", choices=("visual", "director"), default="visual")
    args = parser.parse_args()
    pack = ROOT / f"resources/{args.kind}-handbooks/expanded-2026-09"
    output = ROOT / f"output/imagegen/{args.kind}-handbooks-2026-09"
    output.mkdir(parents=True, exist_ok=True)
    jobs = [json.loads(line) for line in (pack / "cover-prompts.jsonl").read_text(encoding="utf-8").splitlines()]
    async with SessionLocal() as session:
        model = await session.get(AIModel, args.model_id)
        if model is None or not model.enabled or model.model_type != ModelType.IMAGE:
            raise RuntimeError("Enabled image model required")
        provider = await session.get(Provider, model.provider_id)
        if provider is None or not provider.enabled:
            raise RuntimeError("Provider unavailable")
        gateway = default_gateway_factory(provider)
        semaphore = asyncio.Semaphore(min(2, max(1, provider.max_concurrency)))

    async def generate(job):
        target = output / Path(job["out"]).name
        if target.exists():
            with Image.open(target) as existing:
                existing.verify()
            print(f"Preserved {target.name}", flush=True)
            return
        async with semaphore:
            print(f"Generating {target.name}", flush=True)
            digest = hashlib.sha256(job["prompt"].encode()).hexdigest()[:24]
            data = await gateway.generate_image(ImageGenerationRequest(
                model=model.model_id, prompt=job["prompt"], resolution="1K", aspect_ratio="3:2",
                capabilities=dict(model.capabilities or {}), idempotency_key=f"handbook-{digest}",
            ))
            with Image.open(BytesIO(data)) as image:
                image.load()
                # Normalize the format; providers may return WebP despite a PNG filename.
                image.convert("RGB").save(target, "PNG")
            print(f"Saved {target.name}", flush=True)

    results = await asyncio.gather(*(generate(job) for job in jobs), return_exceptions=True)
    errors = [(job["out"], type(result).__name__, str(result)[:200])
              for job, result in zip(jobs, results, strict=True) if isinstance(result, Exception)]
    if errors:
        for name, kind, detail in errors:
            print(f"Failed {name}: {kind}: {detail}", flush=True)
        raise SystemExit(1)
    print("All 8 cover files ready", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
