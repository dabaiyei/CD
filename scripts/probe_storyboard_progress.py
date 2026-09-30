"""Read-only workflow progress; excludes prompts and provider credentials."""
import json
import sqlite3
import sys
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "cineforge.db"
db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
db.row_factory = sqlite3.Row
workflow_id = sys.argv[1]
tasks = db.execute("SELECT id, task_type, status, request_payload, result_payload FROM ai_tasks "
    "WHERE json_extract(request_payload, '$.workflow_id') = ? ORDER BY created_at DESC LIMIT 4", (workflow_id,)).fetchall()
for task in tasks:
    payload = json.loads(task['request_payload'] or '{}')
    result = json.loads(task['result_payload'] or '{}')
    state = payload.get('storyboard_generation', {}).get('state', {})
    review = payload.get('storyboard_review_cache', {})
    print(json.dumps({'task': task['id'], 'type': task['task_type'], 'status': task['status'],
        'repaired_indices': state.get('repaired_indices', []),
        'review_completed': len(review.get('completed', {})),
        'review_total': review.get('total_units'),
        'result': {k: result[k] for k in ('approved', 'summary', 'error', 'shot_count') if k in result},
        'events': [dict(row) for row in db.execute(
            'SELECT message, created_at FROM task_events WHERE task_id=? ORDER BY created_at DESC LIMIT 3',
            (task['id'],))]}, ensure_ascii=False))
db.close()
