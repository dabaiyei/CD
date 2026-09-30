"""Recover one deleted replica row from retained WAL frames into a separate JSON file.

Never writes to the live database. The recovered row must be reviewed before restore.
"""

import argparse
import json
import sqlite3
import struct
import tempfile
from contextlib import closing
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("task_id")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    wal = Path(str(args.database) + "-wal").read_bytes()
    base = args.database.read_bytes()
    page_size = struct.unpack(">I", wal[8:12])[0]
    frame_size = page_size + 24
    frames = []
    for offset in range(32, len(wal) - frame_size + 1, frame_size):
        header = wal[offset : offset + 24]
        if header[8:16] != wal[16:24]:
            break
        page, size = struct.unpack(">II", header[:8])
        frames.append((page, size, wal[offset + 24 : offset + frame_size]))
    needle = args.task_id.encode()
    matches = [i for i, (_, _, data) in enumerate(frames) if needle in data]
    print("WAL frames", len(frames), "matching frames", len(matches))
    candidates = set()
    for match in matches:
        commit = next((i for i in range(match, len(frames)) if frames[i][1]), None)
        if commit is not None:
            candidates.add(commit)
    for commit in sorted(candidates, reverse=True):
        with tempfile.TemporaryDirectory(prefix="replica-recovery-") as folder:
            target = Path(folder) / "snapshot.db"
            with target.open("wb") as output:
                output.write(base)
                for page, _, data in frames[: commit + 1]:
                    output.seek((page - 1) * page_size)
                    output.write(data)
                output.truncate(frames[commit][1] * page_size)
            try:
                with closing(sqlite3.connect(target)) as db:
                    db.row_factory = sqlite3.Row
                    row = db.execute(
                        "select * from ai_tasks where id=?", (args.task_id,)
                    ).fetchone()
                    if row and row["task_type"].startswith("video_replica_"):
                        args.output.write_text(
                            json.dumps(dict(row), ensure_ascii=False), encoding="utf-8"
                        )
                        state = json.loads(row["result_payload"] or "{}")
                        print(
                            "Recovered",
                            row["id"],
                            row["status"],
                            "commit frame",
                            commit,
                            "clips",
                            [
                                (k, v.get("status"), bool(v.get("key")))
                                for k, v in state.get("clips", {}).items()
                            ],
                        )
                        return
            except sqlite3.DatabaseError:
                continue
    raise SystemExit("No recoverable committed task row remains in this WAL")


if __name__ == "__main__":
    main()
