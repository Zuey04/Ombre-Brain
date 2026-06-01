# Diary Hub tags (aligned with LanClaude DiarySectionTags)

DIARY_TAG_PREFIX = "diary:"

ABOUT_VIEW = "diary:about_view"
MOOD_TURN = "diary:mood_turn"
HEALTH = "diary:health"
PERIOD = "diary:period"
ENTRY = "diary:entry"
OPEN_THREAD = "diary:open_thread"
DEBATE = "diary:debate"
USER_QUOTE = "diary:user_quote"
SPECIAL_CLAUDE = "diary:special_claude"
SPECIAL_USER = "diary:special_user"
DAILY = "diary:daily"  # marker for auto midnight diary

ALL_DIARY_TAGS = {
    ABOUT_VIEW,
    MOOD_TURN,
    HEALTH,
    PERIOD,
    ENTRY,
    OPEN_THREAD,
    DEBATE,
    USER_QUOTE,
    SPECIAL_CLAUDE,
    SPECIAL_USER,
    DAILY,
}

# Tags that mirror hold() into permanent diary ledger (not memory-only)
LEDGER_MIRROR_TAGS = ALL_DIARY_TAGS - {ENTRY, DAILY}

# Memory decay lambda multiplier per tag (>1 = forget faster, <1 = slower)
TAG_DECAY_LAMBDA_MULT = {
    USER_QUOTE: 1.75,
    MOOD_TURN: 1.15,
    DEBATE: 0.85,           # 交锋/翻车：记久一点，防装懂
    OPEN_THREAD: 0.92,      # 悬着的事：resolved 后才开始时间衰减（见 decay_engine）
    ABOUT_VIEW: 0.9,
    HEALTH: 0.95,
    PERIOD: 0.95,
    SPECIAL_CLAUDE: 0.88,
    SPECIAL_USER: 0.88,
}

SECTION_LABELS = {
    ABOUT_VIEW: "眼中的你",
    MOOD_TURN: "心情",
    HEALTH: "健康",
    PERIOD: "经期",
    ENTRY: "日记",
    OPEN_THREAD: "悬着的事",
    DEBATE: "交锋与翻车",
    USER_QUOTE: "随口句",
    SPECIAL_CLAUDE: "双写·我",
    SPECIAL_USER: "双写·你",
}


def parse_tags_param(tags: str | list | None) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, list):
        return [str(t).strip() for t in tags if str(t).strip()]
    return [t.strip() for t in str(tags).split(",") if t.strip()]


def ledger_mirror_tags(tag_list: list[str]) -> list[str]:
    return [t for t in tag_list if t in LEDGER_MIRROR_TAGS]


def diary_section_label(tags: list[str]) -> str:
    for t in tags:
        if t in SECTION_LABELS:
            return SECTION_LABELS[t]
    for t in tags:
        if t.startswith(DIARY_TAG_PREFIX):
            return t.replace(DIARY_TAG_PREFIX, "")
    return ""


def decay_lambda_multiplier(tags: list[str]) -> float:
    """多 tag 时取 min：留得久的那个 tag 说了算。"""
    matched = [TAG_DECAY_LAMBDA_MULT[t] for t in tags if t in TAG_DECAY_LAMBDA_MULT]
    if not matched:
        return 1.0
    return min(matched)


def open_thread_decay_frozen(metadata: dict) -> bool:
    """悬着的事：未 resolved 时不做时间衰减。"""
    tags = metadata.get("tags", [])
    if not isinstance(tags, list):
        return False
    return OPEN_THREAD in tags and not metadata.get("resolved", False)
