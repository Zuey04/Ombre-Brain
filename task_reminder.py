# Task DDL reminder nodes — system-side only (not model-initiated).

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

DEFAULT_OFFSETS = (7, 1, 0)
DEFAULT_WINDOW_DAYS = 7


@dataclass
class ReminderCandidate:
    bucket_id: str
    name: str
    content: str
    task_due: str
    due_date: str  # ISO YYYY-MM-DD
    days_until: int
    offset_days: int  # node being triggered (7 / 1 / 0)
    remind_window_days: int
    remind_offsets: list[int]
    source_quote: str = ""


def parse_offsets(raw: str | None) -> list[int]:
    if not raw or not str(raw).strip():
        return list(DEFAULT_OFFSETS)
    out = []
    for part in str(raw).replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
            if n >= 0:
                out.append(n)
        except ValueError:
            continue
    return sorted(set(out), reverse=True) if out else list(DEFAULT_OFFSETS)


def infer_task_reminder_policy(name: str, content: str) -> tuple[str, int]:
    """
    Fallback when hold() did not pass remind_* — prefer model-set values.
    Returns (offsets_csv, window_days).
    """
    text = f"{name} {content}".lower()
    chore_hints = ("买", "取", "快递", "猫粮", "外卖", "缴费", "充值", "还书", "倒垃圾")
    if any(h in text for h in chore_hints) and len(text) < 80:
        return "1,0", 1
    long_hints = ("论文", "答辩", "毕业", "典礼", "考试", "面试", "签证", "搬家", "项目截止")
    if any(h in text for h in long_hints):
        return "7,1,0", 7
    return "7,1,0", 7


def finalize_task_reminder_fields(
    remind_offsets: str = "",
    remind_window_days: int = -1,
    analysis: dict | None = None,
    name: str = "",
    content: str = "",
) -> tuple[str, str]:
    """
    Resolve remind_offsets / remind_window_days for a new task bucket.
    Priority: explicit hold() args → dehydrator analyze → heuristic fallback.
    """
    off = (remind_offsets or "").strip()
    win_raw: str | int | None = remind_window_days if remind_window_days >= 0 else None

    if analysis:
        if not off:
            off = str(analysis.get("remind_offsets", "")).strip()
        if win_raw is None:
            aw = analysis.get("remind_window_days")
            if aw is not None and str(aw).strip() != "":
                win_raw = aw

    if not off:
        off, win_guess = infer_task_reminder_policy(name, content)
        if win_raw is None:
            win_raw = win_guess

    offsets = parse_offsets(off)
    window = parse_window_days(win_raw, offsets)
    offsets_csv = ",".join(str(x) for x in sorted(set(offsets), reverse=True))
    return offsets_csv, str(window)


def parse_window_days(raw, offsets: list[int]) -> int:
    if raw is not None and str(raw).strip().isdigit():
        return max(0, int(str(raw).strip()))
    return max(offsets) if offsets else DEFAULT_WINDOW_DAYS


def parse_task_due_to_date(task_due: str, ref: date | None = None) -> Optional[date]:
    """Best-effort parse task_due into a calendar date."""
    ref = ref or date.today()
    s = (task_due or "").strip()
    if not s:
        return None

    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.match(r"^(\d{4})/(\d{1,2})/(\d{1,2})$", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.match(r"^(\d{1,2})月(\d{1,2})日", s)
    if m:
        try:
            return date(ref.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None

    if "大后天" in s:
        return ref + timedelta(days=3)
    if "后天" in s:
        return ref + timedelta(days=2)
    if "明天" in s:
        return ref + timedelta(days=1)
    if "今天" in s or "今日" in s:
        return ref

    return None


def _last_reminded_offset(meta: dict) -> Optional[int]:
    raw = meta.get("last_reminded_offset")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def pick_reminder_node(
    days_until: int,
    offsets: list[int],
    window_days: int,
    last_reminded: Optional[int],
) -> Optional[int]:
    """
    Key nodes only: fire when days_until exactly matches an offset (7/1/0).
    After firing offset X, silent until a smaller offset node is due.
    """
    if days_until < 0:
        return None
    if days_until > window_days:
        return None

    for off in sorted(offsets, reverse=True):
        if days_until != off:
            continue
        if last_reminded is None:
            return off
        if off < last_reminded:
            return off
    return None


def list_reminder_candidates(
    buckets: list[dict],
    ref: date | None = None,
) -> list[ReminderCandidate]:
    ref = ref or date.today()
    out: list[ReminderCandidate] = []

    for b in buckets:
        meta = b.get("metadata") or {}
        if meta.get("memory_kind") != "task":
            continue
        if meta.get("task_status", "open") != "open":
            continue

        due_raw = str(meta.get("task_due", "")).strip()
        due = parse_task_due_to_date(due_raw, ref)
        if due is None:
            continue

        days_until = (due - ref).days
        offsets = parse_offsets(meta.get("remind_offsets"))
        window_days = parse_window_days(meta.get("remind_window_days"), offsets)
        last = _last_reminded_offset(meta)
        node = pick_reminder_node(days_until, offsets, window_days, last)
        if node is None:
            continue

        out.append(
            ReminderCandidate(
                bucket_id=b["id"],
                name=str(meta.get("name", b["id"])),
                content=(b.get("content") or "")[:500],
                task_due=due_raw,
                due_date=due.isoformat(),
                days_until=days_until,
                offset_days=node,
                remind_window_days=window_days,
                remind_offsets=offsets,
                source_quote=str(meta.get("source_quote", ""))[:200],
            ),
        )

    out.sort(key=lambda c: (c.days_until, c.offset_days))
    return out


def candidate_to_api_dict(c: ReminderCandidate) -> dict:
    label = when_label(c.offset_days, c.days_until)
    return {
        "bucket_id": c.bucket_id,
        "name": c.name,
        "content": c.content,
        "task_due": c.task_due,
        "due_date": c.due_date,
        "days_until": c.days_until,
        "offset_days": c.offset_days,
        "remind_window_days": c.remind_window_days,
        "remind_offsets": c.remind_offsets,
        "source_quote": c.source_quote,
        "node_label": label,
    }


def when_label(offset_days: int, days_until: int) -> str:
    if days_until == 0 or offset_days == 0:
        return "due_today"
    if offset_days == 1 or days_until == 1:
        return "due_tomorrow"
    return f"due_in_{days_until}_days"
