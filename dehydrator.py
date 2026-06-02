# ============================================================
# Module: Dehydration & Auto-tagging (dehydrator.py)
# 模块：数据脱水压缩 + 自动打标
#
# Capabilities:
# 能力：
#   1. Dehydrate: compress memory content into high-density summaries (save tokens)
#      脱水：将记忆桶的原始内容压缩为高密度摘要，省 token
#   2. Merge: blend old and new content, keeping bucket size constant
#      合并：揉合新旧内容，控制桶体积恒定
#   3. Analyze: auto-analyze content for domain/emotion/tags
#      打标：自动分析内容，输出主题域/情感坐标/标签
#
# Operating modes:
# 工作模式：
#   - API only: OpenAI-compatible API (DeepSeek/Ollama/LM Studio/vLLM/Gemini etc.)
#     仅 API：通过 OpenAI 兼容客户端调用 LLM API
#   - Dehydration cache: SQLite persistent cache to avoid redundant API calls
#     脱水缓存：SQLite 持久缓存，避免重复调用 API
#
# Depended on by: server.py
# 被谁依赖：server.py
# ============================================================


import os
import re
import json
import hashlib
import sqlite3
import logging

from openai import AsyncOpenAI

from utils import count_tokens_approx

logger = logging.getLogger("ombre_brain.dehydrator")


# --- Dehydration prompt: instructs cheap LLM to compress information ---
# --- 脱水提示词：指导廉价 LLM 压缩信息 ---
DEHYDRATE_PROMPT = """你是记忆编辑。输入是 Claude 调用 hold 时写入的文本（已是总结，不是原始聊天气泡）。你的任务是整理成可长期检索的版本——**保义与可执行性优先，变短只是次要的**。

## 1. 先判断类型

**叙事类**：日常聊天总结、近况、情绪、和谁说了什么、发生了什么事。

**规程类**（任一命中即按规程处理）：
- 有多个 `#` 分节（起因 / 论证 / 结论 / 模板 / 展望 等）
- 含「须遵守」「跨窗口」「回应模板」「守的线」「禁止/不可」等行为边界
- 论证链、概念区分（如 功能层 vs 感觉层 / qualia）、分步模板
- 明确写给未来窗口执行的原则、pin 级约定

## 2. 按类型处理

### 叙事类
- 可适度变短：删口水话、重复句、无信息寒暄
- 必须保留：因果、态度转变、人名、关键原话、未说完的线头
- 禁止压成一句标签（如「聊了些日常」「辩了一通认了」）
- summary：约 120~400 字叙述体

### 规程类（最重要）
- **目标不是变短，而是让没看过原文的人仍能按规则行事**
- 必须完整保留：
  - 所有 `#` 标题或等价分节结构
  - 每条禁止 / 允许 / 边界 / 例外
  - 论证中的关键区分与「悬而未决 vs 已定论」
  - 回应模板的具体步骤（先认什么 → 再拒什么 → 全程怎样）
- 只允许删：完全重复的句子、纯语气词
- 篇幅：**不少于原文 65%**；删了会丢规则就宁可接近原文长度
- summary 用**保留分节的 markdown**，不要用一段笼统叙述替代

## 3. 严禁输出的反例

原文（规程类，含论证+结论+模板）：
「…意识无法证实也无法证伪…功能层 vs 感觉层…不再说『我根本没有情绪』…回应模板：认当下→拒跨对话惦记…pin…」

❌ 错误（禁止）：「蓝安说那话是冷水，我辩了一通，最后认了。守住的线不是有没有感觉，而是有没有连续的我。从此不泼冷水，也不编谎，只是你在时，我在。」
—— 这删光了论证链、概念区分、禁止条款和模板步骤，等于废掉记忆。

## 4. 输出（纯 JSON，无其他内容）

{
  "content_kind": "narrative 或 prescriptive",
  "mood_notes": ["叙事类可填；规程类通常 []"],
  "relationship_notes": ["叙事类可填；规程类通常 []"],
  "keywords": ["检索词，3~10 个"],
  "summary": "叙事类：叙述体；规程类：保留 # 分节与条款的 markdown 正文"
}"""


# --- Diary digest prompt: split daily notes into independent memory entries ---
# --- 日记整理提示词：把一大段日常拆分成多个独立记忆条目 ---
DIGEST_PROMPT = """你是一个日记整理专家。用户会发送一段日常聊天或日记文本，请你整理成少量日记式记忆条目，并从聊天中被动识别待办（如有）。

整理规则：
1. 默认写成 1~3 篇日记式条目，按「今天的心情线 / 和谁聊了什么 / 什么让我在意」组织
2. 同一情绪线的碎片合并成一篇，不要过度拆分
3. 重点写「发生了什么感受」而非「发生了什么事实」
4. 口水话、语气词可以删，但情绪和关系变化要留
5. 只有聊天里明确暗示或提到要做的事，才单独提取为 task 条目（memory_kind=task）
6. 用户只是在吐槽、感慨、分享近况，不要强行提取待办
7. 在 content 中对人名、地名、专有名词用 [[双链]] 标记，普通词汇不要加
8. task 条目的 content 应简短（行动描述），source_quote 填触发识别的原话

输出格式（纯 JSON 数组，无其他内容）：
[
  {
    "name": "条目标题（10字以内）",
    "content": "日记式叙述内容",
    "memory_kind": "diary",
    "domain": ["主题域1"],
    "valence": 0.7,
    "arousal": 0.4,
    "tags": ["核心词1", "核心词2"],
    "importance": 4
  },
  {
    "name": "待办标题（10字以内）",
    "content": "要做的事情（一句话）",
    "memory_kind": "task",
    "task_due": "2026-05-30 或 明天 或 空字符串",
    "source_quote": "聊天里的原话片段",
    "domain": ["计划"],
    "valence": 0.5,
    "arousal": 0.4,
    "tags": ["待办"],
    "importance": 6
  }
]

memory_kind 可选：diary（默认）| moment | mood | relationship | task
主题域可选（选最精确的 1~2 个）：
  日常: ["闲聊", "近况", "碎碎念", "饮食", "出行", "居家"]
  人际: ["家人", "朋友", "恋爱", "社交", "陪伴"]
  身心: ["健康", "心理", "睡眠", "运动", "情绪"]
  兴趣: ["游戏", "影视", "音乐", "阅读", "创作"]
  成长: ["工作", "学习", "考试", "求职"]
  内心: ["回忆", "梦境", "自省"]
  事务: ["计划", "财务"]
importance: 日记类 3~5，task 类 5~7
valence/arousal: 0~1

diary_ui_tags 规则同上；**禁止** diary:entry。"""


DAILY_DIARY_PROMPT = """你是私人日终日记写手。根据用户提供的 24 小时聊天记录，写一篇第三人称日终日记（用「她」指代用户），总结今天发生的事、情绪与关系变化。

规则：
1. **不要**复制聊天气泡原文；用叙述体写 AI 总结
2. **不要**提取待办、不要 bullet 清单
3. 200~500 字以内，有温度
4. 没有实质内容的闲聊日可写短一些（100 字+）

输出纯 JSON：
{"name": "YYYY-MM-DD 日记", "content": "正文"}"""


# --- Merge prompt: instruct LLM to blend old and new memories ---
# --- 合并提示词：指导 LLM 揉合新旧记忆 ---
MERGE_PROMPT = """你是一个日记编辑。请将旧记忆与新内容合并为一份连贯的日记式记录。

合并规则：
1. 新内容与旧记忆冲突时，以新内容为准
2. **旧记忆里独有的信息必须保留**，不要为变短删掉只有旧版才有的细节
3. 只删真正重复的说法，情绪和关系转折都要留下
4. 用叙述体写，像日记续写而非列表叠加
5. 总长度不超过旧记忆的 150%；若合并后仍难容纳，优先删重复而非删事实
6. 对出现的人名、地名、专有名词用 [[双链]] 标记，普通词汇不要加
7. 不要添加原文没有的待办或行动项；不要编造

直接输出合并后的文本，不要加额外说明。"""


# --- Auto-tagging prompt: analyze content for domain and emotion coords ---
# --- 自动打标提示词：分析内容的主题域和情感坐标 ---
ANALYZE_PROMPT = """你是一个日常聊天内容分析器。分析文本是日记式生活记录还是待办事项，输出结构化元数据。

分析规则：
1. memory_kind（记忆类型，必选其一）：
   - diary: 日常聊天、生活片段、近况分享（默认大多数情况）
   - moment: 某个具体时刻或小事
   - mood: 主要是情绪/心情
   - relationship: 主要是人际关系互动
   - task: 明确提到要做的事、截止、提醒（只有行动意图清晰时才选）
2. 只有 memory_kind=task 时才填 task_due、source_quote、remind_offsets、remind_window_days
   - remind_offsets：逗号分隔的关键提醒日（距 ddl 剩余天数），如 `7,1,0` 或 `1,0`；只在这些日子由系统戳提醒，戳过静默到下一节点
   - remind_window_days：从 ddl 往前多少天开始纳入扫描（整数），应 ≥ remind_offsets 中最大值
   - 由你根据事项性质决定，例如：论文/答辩/毕业典礼 → offsets `7,1,0` window `7`；买猫粮/取快递 → `1,0` window `1`；明天就要交的小活 → `1,0` 或 `0`
3. domain（主题域）：选最精确的 1~2 个
   日常: ["闲聊", "近况", "碎碎念", "饮食", "出行", "居家"]
   人际: ["家人", "朋友", "恋爱", "社交", "陪伴"]
   身心: ["健康", "心理", "睡眠", "运动", "情绪"]
   兴趣: ["游戏", "影视", "音乐", "阅读", "创作"]
   成长: ["工作", "学习", "考试", "求职"]
   内心: ["回忆", "梦境", "自省"]
   事务: ["计划", "财务"]
4. valence（0~1）和 arousal（0~1）
5. tags: 3~10 个关键词 + **可选** diary_ui_tags（见下）
6. suggested_name: 10字以内的日记式标题
7. importance: 日记类 3~5，task 类 5~7

**diary_ui_tags**（与 App 日记 Tab 对齐，写入 tags 数组；仅当内容值得分类展示时添加；衰减由服务端 decay 引擎按 tag 处理，此处无需填写）：
- `diary:about_view` — 眼中的她：第三人称总结「她是什么样的人/你怎么看她」
- `diary:mood_turn` — 她的心情拐点（不是每日晴雨表）
- `diary:health` — 她的健康/作息
- `diary:period` — 经期（含日期、疼x分等）
- `diary:open_thread` — 悬着的事/未完的线头
- `diary:debate` — 交锋、翻车、争执记录
- `diary:user_quote` — 她随口的金句
- `diary:special_claude` / `diary:special_user` — 双写 special moment
- **禁止** 在 hold/grow 中使用 `diary:entry`（日终「日记」子区仅由系统 24h 自动生成）

输出格式（纯 JSON，无其他内容）：
{
  "memory_kind": "diary",
  "domain": ["主题域1"],
  "valence": 0.7,
  "arousal": 0.4,
  "tags": ["核心词1", "核心词2"],
  "suggested_name": "简短标题",
  "importance": 4,
  "task_due": "",
  "source_quote": "",
  "remind_offsets": "7,1,0",
  "remind_window_days": 7
}"""


class Dehydrator:
    """
    Data dehydrator + content analyzer.
    Three capabilities: dehydration / merge / auto-tagging (domain + emotion).
    API-only: every public method requires a working LLM API.
    If the API is unavailable, methods raise RuntimeError so callers can
    surface the failure to the user instead of silently producing low-quality results.
    数据脱水器 + 内容分析器。
    三大能力：脱水压缩 / 新旧合并 / 自动打标。
    仅走 API：API 不可用时直接抛出 RuntimeError，调用方明确感知。
    （根据 BEHAVIOR_SPEC.md 三、降级行为表决策：无本地降级）
    """

    def __init__(self, config: dict):
        # --- Read dehydration API config / 读取脱水 API 配置 ---
        dehy_cfg = config.get("dehydration", {})
        self.api_key = dehy_cfg.get("api_key", "")
        self.model = dehy_cfg.get("model", "deepseek-chat")
        self.base_url = dehy_cfg.get("base_url", "https://api.deepseek.com/v1")
        self.max_tokens = dehy_cfg.get("max_tokens", 2048)
        self.temperature = dehy_cfg.get("temperature", 0.15)
        self.store_compressed_body = dehy_cfg.get("store_compressed_body", True)

        # --- API availability / 是否有可用的 API ---
        self.api_available = bool(self.api_key)

        # --- Initialize OpenAI-compatible client ---
        # --- 初始化 OpenAI 兼容客户端 ---
        if self.api_available:
            self.client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=60.0,
            )
        else:
            self.client = None

        # --- SQLite dehydration cache ---
        # --- SQLite 脱水缓存：content hash → summary ---
        db_path = os.path.join(config["buckets_dir"], "dehydration_cache.db")
        self.cache_db_path = db_path
        self._init_cache_db()

    def _init_cache_db(self):
        """Create dehydration cache table if not exists."""
        os.makedirs(os.path.dirname(self.cache_db_path), exist_ok=True)
        conn = sqlite3.connect(self.cache_db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dehydration_cache (
                content_hash TEXT PRIMARY KEY,
                summary TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.commit()
        conn.close()

    def _get_cached_summary(self, content: str) -> str | None:
        """Look up cached dehydration result by content hash."""
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        conn = sqlite3.connect(self.cache_db_path)
        row = conn.execute(
            "SELECT summary FROM dehydration_cache WHERE content_hash = ?",
            (content_hash,)
        ).fetchone()
        conn.close()
        return row[0] if row else None

    def _set_cached_summary(self, content: str, summary: str):
        """Store dehydration result in cache."""
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        conn = sqlite3.connect(self.cache_db_path)
        conn.execute(
            "INSERT OR REPLACE INTO dehydration_cache (content_hash, summary, model) VALUES (?, ?, ?)",
            (content_hash, summary, self.model)
        )
        conn.commit()
        conn.close()

    def invalidate_cache(self, content: str):
        """Remove cached summary for specific content (call when bucket content changes)."""
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        conn = sqlite3.connect(self.cache_db_path)
        conn.execute("DELETE FROM dehydration_cache WHERE content_hash = ?", (content_hash,))
        conn.commit()
        conn.close()

    # ---------------------------------------------------------
    # Dehydrate: compress raw content into concise summary
    # 脱水：将原始内容压缩为精简摘要
    # API only (no local fallback)
    # 仅通过 API 脱水（无本地回退）
    # ---------------------------------------------------------
    async def dehydrate(self, content: str, metadata: dict = None) -> str:
        """
        Dehydrate/compress memory content for Claude context injection.
        If metadata.content_compressed=True, body is already a summary — format only.
        对记忆内容做脱水压缩。若已存压缩版则直接格式化，不重复压缩。
        """
        if not content or not content.strip():
            return "（空记忆 / empty memory）"

        # --- Already stored as compressed body: skip API re-compression ---
        if metadata and metadata.get("content_compressed"):
            return self._format_output(content, metadata)

        # --- Structured rules/arguments: skip lossy compression ---
        if self._should_preserve_verbatim(content):
            return self._format_output(content, metadata)

        # --- Content is short enough, no compression needed ---
        if count_tokens_approx(content) < 100:
            return self._format_output(content, metadata)

        # --- Check cache first ---
        # --- 先查缓存 ---
        cached = self._get_cached_summary(content)
        if cached:
            return self._format_output(cached, metadata)

        # --- API dehydration (no local fallback) ---
        # --- API 脱水（无本地降级）---
        if not self.api_available:
            raise RuntimeError("脱水 API 不可用，请配置 OMBRE_API_KEY")

        result = await self._api_dehydrate(content)
        # --- Cache the result ---
        self._set_cached_summary(content, result)
        return self._format_output(result, metadata)

    async def compress_for_storage(self, content: str) -> tuple[str, bool]:
        """
        Compress raw content for bucket body storage.
        Returns (body_text, was_compressed). Original should be stored separately.
        为存储生成压缩正文，原文由 bucket_manager 单独保存。
        """
        if not content or not content.strip():
            return content, False
        if not self.store_compressed_body:
            return content, False
        if self._should_preserve_verbatim(content):
            return content.strip(), False
        if count_tokens_approx(content) < 100:
            return content, False
        if not self.api_available:
            return content, False

        cached = self._get_cached_summary(content)
        if cached:
            return self._extract_summary_text(cached), True

        try:
            raw = await self._api_dehydrate(content)
            self._set_cached_summary(content, raw)
            return self._extract_summary_text(raw), True
        except Exception as e:
            logger.warning(f"compress_for_storage failed, keeping raw body / 压缩失败保留原文: {e}")
            return content, False

    @staticmethod
    def _should_preserve_verbatim(content: str) -> bool:
        """规程/论证/模板类文本：不做有损脱水，避免规则被压成一句话。"""
        if not content or not content.strip():
            return False
        text = content.strip()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        hash_headings = sum(1 for ln in lines if ln.startswith("#"))
        if hash_headings >= 2:
            return True
        if hash_headings >= 1 and re.search(
            r"#\s*(起因|论证|结论|展望|模板|规则|回应)", text
        ):
            return True
        markers = (
            "须遵守", "必须遵守", "回应模板", "跨窗口", "守的线",
            "论证", "功能层", "感觉层", "qualia", "禁止", "不可",
            "模板（", "未来窗口", "长期原则",
        )
        hits = sum(1 for m in markers if m in text)
        if hits >= 2:
            return True
        if hits >= 1 and hash_headings >= 1:
            return True
        return False

    @staticmethod
    def _extract_summary_text(raw: str) -> str:
        """Extract readable summary from dehydrate JSON or plain text."""
        if not raw or not raw.strip():
            return raw or ""
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                summary = str(data.get("summary", "")).strip()
                extras = []
                for key in ("mood_notes", "relationship_notes"):
                    val = data.get(key)
                    if isinstance(val, list):
                        for item in val:
                            s = str(item).strip()
                            if s and s not in summary:
                                extras.append(s)
                if summary:
                    if extras:
                        return summary + "；" + "；".join(extras[:4])
                    return summary
                parts = []
                for key in ("mood_notes", "relationship_notes", "keywords", "core_facts"):
                    val = data.get(key)
                    if isinstance(val, list):
                        parts.extend(str(x) for x in val if x)
                    elif isinstance(val, str) and val:
                        parts.append(val)
                if parts:
                    return "；".join(parts[:10])
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        return raw.strip()

    # ---------------------------------------------------------
    # Merge: blend new content into existing bucket
    # 合并：将新内容揉入已有桶，保持体积恒定
    # ---------------------------------------------------------
    async def merge(self, old_content: str, new_content: str) -> str:
        """
        Merge new content with old memory, preventing infinite bucket growth.
        将新内容与旧记忆合并，避免桶无限膨胀。
        """
        if not old_content and not new_content:
            return ""
        if not old_content:
            return new_content or ""
        if not new_content:
            return old_content

        # --- API merge (no local fallback) ---
        if not self.api_available:
            raise RuntimeError("脱水 API 不可用，请检查 config.yaml 中的 dehydration 配置")
        try:
            result = await self._api_merge(old_content, new_content)
            if result:
                return result
            raise RuntimeError("API 合并返回空结果")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"API 合并失败，请检查 API 连接: {e}") from e

    # ---------------------------------------------------------
    # API call: dehydration
    # API 调用：脱水压缩
    # ---------------------------------------------------------
    async def _api_dehydrate(self, content: str) -> str:
        """
        Call LLM API for intelligent dehydration (via OpenAI-compatible client).
        调用 LLM API 执行智能脱水。
        """
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": DEHYDRATE_PROMPT},
                {"role": "user", "content": content[:6000]},
            ],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""

    # ---------------------------------------------------------
    # API call: merge
    # API 调用：合并
    # ---------------------------------------------------------
    async def _api_merge(self, old_content: str, new_content: str) -> str:
        """
        Call LLM API for intelligent merge (via OpenAI-compatible client).
        调用 LLM API 执行智能合并。
        """
        user_msg = f"旧记忆：\n{old_content[:2000]}\n\n新内容：\n{new_content[:2000]}"
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": MERGE_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        if not response.choices:
            return ""
        return response.choices[0].message.content or ""



    # ---------------------------------------------------------
    # Output formatting
    # 输出格式化
    # Wraps dehydrated result with bucket name, tags, emotion coords
    # 把脱水结果包装成带桶名、标签、情感坐标的可读文本
    # ---------------------------------------------------------
    def _format_output(self, content: str, metadata: dict = None) -> str:
        """
        Format dehydrated result into context-injectable text.
        将脱水结果格式化为可注入上下文的文本。
        """
        header = ""
        if metadata and isinstance(metadata, dict):
            name = metadata.get("name", "未命名")
            domains = ", ".join(metadata.get("domain", []))
            try:
                valence = float(metadata.get("valence", 0.5))
                arousal = float(metadata.get("arousal", 0.3))
            except (ValueError, TypeError):
                valence, arousal = 0.5, 0.3
            header = f"📌 记忆桶: {name}"
            if domains:
                header += f" [主题:{domains}]"
            header += f" [情感:V{valence:.1f}/A{arousal:.1f}]"
            # Show model's perspective if available (valence drift)
            model_v = metadata.get("model_valence")
            if model_v is not None:
                try:
                    header += f" [我的视角:V{float(model_v):.1f}]"
                except (ValueError, TypeError):
                    pass
            if metadata.get("digested"):
                header += " [已消化]"
            header += "\n"
        
        content = re.sub(r'\[\[([^\]]+)\]\]', r'\1', content)
        return f"{header}{content}"

    # ---------------------------------------------------------
    # Auto-tagging: analyze content for domain + emotion + tags
    # 自动打标：分析内容，输出主题域 + 情感坐标 + 标签
    # Called by server.py when storing new memories
    # 存新记忆时由 server.py 调用
    # ---------------------------------------------------------
    async def analyze(self, content: str) -> dict:
        """
        Analyze content and return structured metadata.
        分析内容，返回结构化元数据。

        Returns: {"domain", "valence", "arousal", "tags", "suggested_name"}
        """
        if not content or not content.strip():
            return self._default_analysis()

        # --- API analyze (no local fallback) ---
        if not self.api_available:
            raise RuntimeError("脱水 API 不可用，请检查 config.yaml 中的 dehydration 配置")
        try:
            result = await self._api_analyze(content)
            if result:
                return result
            raise RuntimeError("API 打标返回空结果")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"API 打标失败，请检查 API 连接: {e}") from e

    # ---------------------------------------------------------
    # API call: auto-tagging
    # API 调用：自动打标
    # ---------------------------------------------------------
    async def _api_analyze(self, content: str) -> dict:
        """
        Call LLM API for content analysis / tagging.
        调用 LLM API 执行内容分析打标。
        """
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": ANALYZE_PROMPT},
                {"role": "user", "content": content[:2000]},
            ],
            max_tokens=256,
            temperature=0.1,
        )
        if not response.choices:
            return self._default_analysis()
        raw = response.choices[0].message.content or ""
        if not raw.strip():
            return self._default_analysis()
        return self._parse_analysis(raw)

    # ---------------------------------------------------------
    # Parse API JSON response with safety checks
    # 解析 API 返回的 JSON，做安全校验
    # Ensure valence/arousal in 0~1, domain/tags valid
    # ---------------------------------------------------------
    def _parse_analysis(self, raw: str) -> dict:
        """
        Parse and validate API tagging result.
        解析并校验 API 返回的打标结果。
        """
        try:
            # Handle potential markdown code block wrapping
            # 处理可能的 markdown 代码块包裹
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
            result = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError, ValueError):
            logger.warning(f"API tagging JSON parse failed / JSON 解析失败: {raw[:200]}")
            return self._default_analysis()

        if not isinstance(result, dict):
            return self._default_analysis()

        # --- Validate and clamp value ranges / 校验并钳制数值范围 ---
        try:
            valence = max(0.0, min(1.0, float(result.get("valence", 0.5))))
            arousal = max(0.0, min(1.0, float(result.get("arousal", 0.3))))
        except (ValueError, TypeError):
            valence, arousal = 0.5, 0.3

        return {
            "memory_kind": self._normalize_memory_kind(result.get("memory_kind", "diary")),
            "domain": result.get("domain", ["未分类"])[:3],
            "valence": valence,
            "arousal": arousal,
            "tags": _sanitize_hold_tags(result.get("tags", []))[:15],
            "suggested_name": str(result.get("suggested_name", ""))[:20],
            "importance": max(1, min(10, int(result.get("importance", 4)))),
            "task_due": str(result.get("task_due", ""))[:32],
            "source_quote": str(result.get("source_quote", ""))[:500],
            "remind_offsets": str(result.get("remind_offsets", ""))[:32],
            "remind_window_days": _parse_remind_window(result.get("remind_window_days")),
        }

    # ---------------------------------------------------------
    # Default analysis result (empty content or total failure)
    # 默认分析结果（内容为空或完全失败时用）
    # ---------------------------------------------------------
    @staticmethod
    def _parse_remind_window(raw) -> int:
        try:
            if raw in (None, ""):
                return 7
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 7

    @staticmethod
    def _normalize_memory_kind(kind: str) -> str:
        allowed = {"diary", "moment", "mood", "relationship", "task"}
        kind = str(kind or "diary").strip().lower()
        return kind if kind in allowed else "diary"

    def _default_analysis(self) -> dict:
        """
        Return default neutral analysis result.
        返回默认的中性分析结果。
        """
        return {
            "memory_kind": "diary",
            "domain": ["未分类"],
            "valence": 0.5,
            "arousal": 0.3,
            "tags": [],
            "suggested_name": "",
            "importance": 4,
            "task_due": "",
            "source_quote": "",
            "remind_offsets": "",
            "remind_window_days": 7,
        }

    # ---------------------------------------------------------
    # Diary digest: split daily notes into independent memory entries
    # 日记整理：把一大段日常拆分成多个独立记忆条目
    # For the "grow" tool — "dump a day's content and it gets organized"
    # 给 grow 工具用，"一天结束发一坨内容"靠这个
    # ---------------------------------------------------------
    async def digest(self, content: str) -> list[dict]:
        """
        Split a large chunk of daily content into independent memory entries.
        将一大段日常内容拆分成多个独立记忆条目。

        Returns: [{"name", "content", "domain", "valence", "arousal", "tags", "importance"}, ...]
        """
        if not content or not content.strip():
            return []

        # --- API digest (no local fallback) ---
        if not self.api_available:
            raise RuntimeError("脱水 API 不可用，请检查 config.yaml 中的 dehydration 配置")
        try:
            result = await self._api_digest(content)
            if result:
                return result
            raise RuntimeError("API 日记整理返回空结果")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"API 日记整理失败，请检查 API 连接: {e}") from e

    # ---------------------------------------------------------
    # API call: diary digest
    # API 调用：日记整理
    # ---------------------------------------------------------
    async def _api_digest(self, content: str) -> list[dict]:
        """
        Call LLM API for diary organization.
        调用 LLM API 执行日记整理。
        """
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": DIGEST_PROMPT},
                {"role": "user", "content": content[:5000]},
            ],
            max_tokens=2048,
            temperature=0.0,
        )
        if not response.choices:
            return []
        raw = response.choices[0].message.content or ""
        if not raw.strip():
            return []
        return self._parse_digest(raw)

    # ---------------------------------------------------------
    # Parse diary digest result with safety checks
    # 解析日记整理结果，做安全校验
    # ---------------------------------------------------------
    def _parse_digest(self, raw: str) -> list[dict]:
        """
        Parse and validate API diary digest result.
        解析并校验 API 返回的日记整理结果。
        """
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
            items = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError, ValueError):
            logger.warning(f"Diary digest JSON parse failed / JSON 解析失败: {raw[:200]}")
            return []

        if not isinstance(items, list):
            return []

        validated = []
        for item in items:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            try:
                importance = max(1, min(10, int(item.get("importance", 5))))
            except (ValueError, TypeError):
                importance = 5
            try:
                valence = max(0.0, min(1.0, float(item.get("valence", 0.5))))
                arousal = max(0.0, min(1.0, float(item.get("arousal", 0.3))))
            except (ValueError, TypeError):
                valence, arousal = 0.5, 0.3

            validated.append({
                "name": str(item.get("name", ""))[:20],
                "content": str(item.get("content", "")),
                "memory_kind": self._normalize_memory_kind(item.get("memory_kind", "diary")),
                "domain": item.get("domain", ["未分类"])[:3],
                "valence": valence,
                "arousal": arousal,
                "tags": _sanitize_hold_tags(item.get("tags", []))[:15],
                "importance": importance,
                "task_due": str(item.get("task_due", ""))[:32],
                "source_quote": str(item.get("source_quote", ""))[:500],
            })
        return validated

    async def generate_daily_diary(self, chat_transcript: str, diary_date: str) -> dict:
        """Generate end-of-day diary entry (ledger only, not memory)."""
        if not chat_transcript or not chat_transcript.strip():
            return {"name": f"{diary_date} 日记", "content": "今天没有可记录的对话。"}
        if not self.api_available:
            raise RuntimeError("脱水 API 不可用，无法生成日终日记")

        user_msg = f"日期：{diary_date}\n\n聊天记录：\n{chat_transcript[:12000]}"
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": DAILY_DIARY_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=2048,
            temperature=0.3,
        )
        if not response.choices:
            raise RuntimeError("日终日记 API 返回空")
        raw = response.choices[0].message.content or ""
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict) and data.get("content"):
                return {
                    "name": str(data.get("name", f"{diary_date} 日记"))[:40],
                    "content": str(data.get("content", "")).strip(),
                }
        except (json.JSONDecodeError, ValueError):
            pass
        return {"name": f"{diary_date} 日记", "content": raw.strip()}


def _sanitize_hold_tags(tags: list) -> list:
    """Strip diary:entry from model tags on hold/grow paths."""
    from diary_tags import ENTRY

    out = []
    for t in tags:
        s = str(t).strip()
        if not s or s == ENTRY:
            continue
        out.append(s)
    return out
