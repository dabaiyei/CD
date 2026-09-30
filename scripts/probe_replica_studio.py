"""Local headless smoke test of the real Studio JS module graph; no model calls."""
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

from app.core.security import create_access_token
from app.db.models import AITask, User
from app.db.session import SessionLocal


async def main():
    async with SessionLocal() as db:
        task = await db.get(AITask, '8e88c5a7-d51b-45c9-a682-7a6294c4a8b7')
        user = await db.get(User, task.user_id)
        token = create_access_token(user_id=user.id, tenant_id=user.tenant_id,role=user.role.value)
    base = 'http://127.0.0.1:5173'
    if '--cut' in sys.argv:
        proc=await asyncio.create_subprocess_exec('node','scripts/probe_replica_cut.mjs',stdin=asyncio.subprocess.PIPE)
        await proc.communicate(json.dumps({'token':token,'executablePath':str(Path(os.environ['LOCALAPPDATA'])/'ms-playwright/chromium-1228/chrome-win64/chrome.exe')}).encode())
        if proc.returncode: raise RuntimeError('Cut UI smoke failed')
        return
    async with httpx.AsyncClient(timeout=180) as client:
        headers={'Authorization':'Bearer '+token}
        response=await client.post(f'{base}/api/v1/video-replicas/{task.id}/studio',headers=headers)
        response.raise_for_status()
        studio=response.json()
        try:
            proc=await asyncio.create_subprocess_exec('node','scripts/probe_replica_studio.mjs',stdin=asyncio.subprocess.PIPE)
            await proc.communicate(json.dumps({'url':base+studio['url'],
                'executablePath':str(Path(os.environ['LOCALAPPDATA'])/'ms-playwright/chromium-1228/chrome-win64/chrome.exe')}).encode())
            if proc.returncode: raise RuntimeError('Browser smoke failed')
        finally:
            await client.delete(f"{base}/api/v1/video-replicas/{task.id}/studio/{studio['id']}",headers=headers)


asyncio.run(main())
