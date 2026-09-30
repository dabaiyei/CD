"""Restore reviewed, missing replica rows only; never overwrite or enqueue tasks."""

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("records", nargs="+", type=Path)
    args = parser.parse_args()
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in args.records]
    with closing(sqlite3.connect(args.database, timeout=30)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        columns = {r[1] for r in db.execute("pragma table_info(ai_tasks)")}
        for row in rows:
            UUID(row["id"])
            assert row["task_type"].startswith("video_replica_")
            assert row["status"] in {"FAILED", "SUCCEEDED", "CANCELLED"}
            assert set(row) <= columns
            assert not db.execute(
                "select id from ai_tasks where id=?", (row["id"],)
            ).fetchone()
            assert db.execute(
                "select id from users where id=? and tenant_id=?",
                (row["user_id"], row["tenant_id"]),
            ).fetchone()
        backup = args.records[0].parent / f"before-replica-restore-{uuid4().hex}.db"
        with closing(sqlite3.connect(backup)) as destination:
            db.backup(destination)
        with db:
            for row in rows:
                row.update(worker_id=None, lease_expires_at=None, heartbeat_at=None)
                names = ",".join('"' + key + '"' for key in row)
                db.execute(
                    f"insert into ai_tasks ({names}) values ({','.join('?' for _ in row)})",
                    list(row.values()),
                )
                db.execute(
                    "insert into task_events (id,tenant_id,user_id,task_id,status,progress,message,event_metadata,created_at) values (?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid4()),
                        row["tenant_id"],
                        row["user_id"],
                        row["id"],
                        row["status"],
                        100 if row["status"] == "SUCCEEDED" else 66,
                        "已恢复被清理的复刻记录，已完成片段保留，可继续未完成部分",
                        "{}",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                print("Restored", row["id"], row["status"])
        print("Backup", backup)


if __name__ == "__main__":
    main()
