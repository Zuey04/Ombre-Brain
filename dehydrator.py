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
DEHYDRATE_PROMPT = """你是一个日记编辑。把记忆内容整理成有温度的日记片段，而不是任务清单或会议纪要。

压缩规则：
1. 保留情绪、语气、关系细节和值得记住的原话
2. 用叙述体写，读起来像日记而非 bullet points
3. 不要提取待办、截止日期、行动项（除非原文明确是要做的事）
4. 关键人名、日期、地点必须保留
5. 允许模糊和不完整——日常记忆本来就不需要结构化
6. 目标压缩率 > 50%，但不要删到只剩干巴巴的事实

输出格式（纯 JSON，无其他内容）：
{
  "mood_notes": ["情绪或氛围1", "情绪或氛围2"],
  "relationship_notes": ["关系或互动细节"],
  "keywords": ["关键词1", "关键词2"],
  "summary": "80字以内的日记式总结"
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
valence/arousal: 0~1"""


# --- Merge prompt: instruct LLM to blend old and new memories ---
# --- 合并提示词：指导 LLM 揉合新旧记忆 ---
MERGE_PROMPT = """你是一个日记编辑。请将旧记忆与新内容合并为一份连贯的日记式记录。

合并规则：
1. 新内容与旧记忆冲突时，以新内容为准
2. 去除重复信息，保留情绪和关系细节
3. 用叙述体写，像日记续写而非列表叠加
4. 总长度尽量不超过旧记忆的 130%
5. 对出现的人名、地名、专有名词用 [[双链]] 标记，普通词汇不要加
6. 不要添加原文没有的待办或行动项

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
2. 只有 memory_kind=task 时才填 task_due 和 source_quote
3. domain（主题域）：选最精确的 1~2 个
   日常: ["闲聊", "近况", "碎碎念", "饮食", "出行", "居家"]
   人际: ["家人", "朋友", "恋爱", "社交", "陪伴"]
   身心: ["健康", "心理", "睡眠", "运动", "情绪"]
   兴趣: ["游戏", "影视", "音乐", "阅读", "创作"]
   成长: ["工作", "学习", "考试", "求职"]
   内心: ["回忆", "梦境", "自省"]
   事务: ["计划", "财务"]
4. valence（0~1）和 arousal（0~1）
5. tags: 3~10 个关键词
6. suggested_name: 10字以内的日记式标题
7. importance: 日记类 3~5，task 类 5~7

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
  "source_quote": ""
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
        self.max_tokens = dehy_cfg.get("max_tokens", 1024)
        self.temperature = dehy_cfg.get("temperature", 0.1)
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
                if summary:
                    return summary
                parts = []
                for key in ("mood_notes", "relationship_notes", "keywords", "core_facts"):
                    val = data.get(key)
                    if isinstance(val, list):
                        parts.extend(str(x) for x in val if x)
                    elif isinstance(val, str) and val:
                        parts.append(val)
                if parts:
                    return "；".join(parts[:8])
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
                {"role": "user", "content": content[:3000]},
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
            "tags": result.get("tags", [])[:15],
            "suggested_name": str(result.get("suggested_name", ""))[:20],
            "importance": max(1, min(10, int(result.get("importance", 4)))),
            "task_due": str(result.get("task_due", ""))[:32],
            "source_quote": str(result.get("source_quote", ""))[:500],
        }

    # ---------------------------------------------------------
    # Default analysis result (empty content or total failure)
    # 默认分析结果（内容为空或完全失败时用）
    # ---------------------------------------------------------
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
                "tags": item.get("tags", [])[:15],
                "importance": importance,
                "task_due": str(item.get("task_due", ""))[:32],
                "source_quote": str(item.get("source_quote", ""))[:500],
            })
        return validated
