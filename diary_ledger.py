# Permanent diary ledger — no decay, excluded from breath/context.

import json
import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils import now_iso

logger = logging.getLogger("ombre_brain.diary_ledger")


class DiaryLedger:
    def __init__(self, buckets_dir: str):
        self.db_path = str(Path(buckets_dir) / "diary_ledger.db")
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entries (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL,
                    diary_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_date ON entries(diary_date)"
            )
            conn.commit()

    @staticmethod
    def ledger_id(raw_id: str) -> str:
        return raw_id if raw_id.startswith("ledger_") else f"ledger_{raw_id}"

    @staticmethod
    def strip_prefix(ledger_id: str) -> str:
        return ledger_id[7:] if ledger_id.startswith("ledger_") else ledger_id

    async def create(
        self,
        content: str,
        tags: list[str],
        name: str = "",
        diary_date: str = "",
        source: str = "hold_mirror",
    ) -> str:
        entry_id = uuid.uuid4().hex[:12]
        created = now_iso()
        date = diary_date or created[:10]
        title = (name or "").strip() or _default_name(tags, date)
        tags_json = json.dumps(tags, ensure_ascii=False)

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO entries (id, name, content, tags, diary_date, source, created)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (entry_id, title, content.strip(), tags_json, date, source, created),
            )
            conn.commit()
        return self.ledger_id(entry_id)

    async def get(self, ledger_id: str) -> Optional[dict]:
        raw = self.strip_prefix(ledger_id)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM entries WHERE id = ?", (raw,)
            ).fetchone()
        if not row:
            return None
        return _row_to_dict(row)

    async def list_entries(
        self,
        date: str | None = None,
        tag_prefix: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        sql = "SELECT * FROM entries WHERE 1=1"
        params: list = []
        if date:
            sql += " AND diary_date = ?"
            params.append(date)
        sql += " ORDER BY created DESC LIMIT ?"
        params.append(limit)

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()

        items = [_row_to_dict(r) for r in rows]
        if tag_prefix:
            items = [
                i for i in items
                if any(t.startswith(tag_prefix) for t in i.get("tags", []))
            ]
        return items

    async def has_daily_for_date(self, diary_date: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT 1 FROM entries
                WHERE diary_date = ? AND tags LIKE ?
                LIMIT 1
                """,
                (diary_date, f"%diary:daily%"),
            ).fetchone()
        return row is not None


def _default_name(tags: list[str], diary_date: str) -> str:
    from diary_tags import SECTION_LABELS

    for t in tags:
        if t in SECTION_LABELS:
            return f"{SECTION_LABELS[t]} · {diary_date}"
    return f"日记 · {diary_date}"


def _row_to_dict(row: sqlite3.Row) -> dict:
    tags = json.loads(row["tags"]) if row["tags"] else []
    lid = DiaryLedger.ledger_id(row["id"])
    return {
        "id": lid,
        "name": row["name"],
        "content": row["content"],
        "tags": tags,
        "diary_date": row["diary_date"],
        "source": row["source"],
        "created": row["created"],
        "memory_kind": "ledger",
        "importance": 5,
        "resolved": False,
        "has_original": True,
        "content_compressed": False,
        "domain": [],
    }


def ledger_to_api_dict(entry: dict, include_content: bool = True) -> dict:
    from diary_tags import diary_section_label

    data = {
        "id": entry["id"],
        "name": entry.get("name", ""),
        "type": "ledger",
        "memory_kind": "ledger",
        "domain": [],
        "tags": entry.get("tags", []),
        "importance": 5,
        "resolved": False,
        "has_original": True,
        "content_compressed": False,
        "created": entry.get("created", ""),
        "last_active": entry.get("created", ""),
        "diary_section": diary_section_label(entry.get("tags", [])),
        "ledger_source": entry.get("source", ""),
        "score": 0,
    }
    if include_content:
        data["content"] = entry.get("content", "")
    else:
        data["content_preview"] = (entry.get("content", "") or "")[:200]
    return data
