"""Real bounded JEV probes only; no project edits or paid media generation."""
import asyncio
import json

from app.db.models import User, UserRole
from app.db.session import SessionLocal
from app.services.project_jev import decide
from sqlalchemy import select


async def main():
    async with SessionLocal() as db:
        user = await db.scalar(select(User).where(User.role == UserRole.ADMIN, User.is_active.is_(True)))
        tenant = user.tenant_id
    cases = [
        ('project-chat', {'request': '只讨论上一章节奏，不生成图片、视频或修改文件'}),
        ('script-generation', {'request': '续写下一章，承接上一章揭露背叛的结尾，不要重写前文'}),
        ('storyboard-generation', {'action': '两位剑客实力相当，连续高速挥砍、格挡、闪避与反击，攻守交替'}),
        ('script-review', {'source': '主角在第一场已经离开酒馆。第二场他到达城门。',
                           'script': '第二场主角仍在酒馆与掌柜交谈，没有离开。'}),
        ('storyboard-review', {'shots': [{'order_index': 3, 'scene_description': '人物始终站在门外',
                                         'action_description': '人物全程坐在屋内椅子上，没有起身或移动'}]}),
        ('storyboard-repair', {'shot': {'order_index': 3, 'scene_description': '右手持剑',
                                      'image_prompt': '左手持剑'},
                               'findings': ['修复同一镜头持剑手前后矛盾'], 'allowed_fields': ['image_prompt']}),
        ('video-prompt-generation', {'action_description': '强者蓄力召唤巨型神龙，神龙冲出法阵，一击轰碎敌人护盾，特写爆发'}),
    ]
    for stage, evidence in cases:
        result = await decide(tenant, stage, evidence)
        print(json.dumps({'stage': stage, **result}, ensure_ascii=False), flush=True)
        assert result['status'] == 'classified', result


if __name__ == '__main__':
    asyncio.run(main())
