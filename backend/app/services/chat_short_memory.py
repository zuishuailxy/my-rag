"""对话短期记忆：Redis 热窗口

角色：只做「最近 N 条 human/ai 原文」的 Redis 缓存，**不查库**。
miss（key 不存在）或 Redis 故障时返回 None / 静默跳过，由调用方回落到
Postgres（kh_ai_message）回填后再 save —— 缓存不可用不能影响对话可用性。

只存原文：检索资料、思考块、工具消息、Mem0 系统消息都不进窗口，过滤口径见
services/chat_memory.py 的 is_working_message / db_rows_to_messages。两条来源
（Redis 热窗口、DB 回填）共用同一套过滤，避免回填时把一次性上下文灌进窗口。

key 形态：`{CHAT_SHORT_MEMORY_KEY_PREFIX}:{userId}:{sessionId}:messages`
值形态：`messages_to_dict()` 的 JSON 数组，TTL 默认 86400 秒。

刻意差异：
(...)`，Python 侧 RedisService 因 `del` 是关键字改名 `delete`。
- 序列化用 `json.dumps(..., ensure_ascii=False)`：
  非 ASCII 的行为，Redis 里存的是可读中文而不是 \\uXXXX。
(config.get(...))` 会把写错的环境变量变成 NaN 一路传到 Redis；
  本模块的配置项声明为 int（config.py），非法值在应用启动时即失败。
- 写入失败只 WARN 不抛。⚠️ 失败模式要清楚：一条消息若含不可 JSON 序列化
  的对象（response_metadata 里塞了 datetime 之类），会每轮都写入失败 → 每轮都 miss →
  每次都回落到 DB 回填。功能正确（不越权、不串话），但缓存退化成空转。
  本项目手工构造的消息只有 content，无此风险；若将来往 additional_kwargs /
  response_metadata 里塞对象，需在这里补 `default=str` 兜底。

未实现：跨会话共享、按用户维度的长期记忆
（那属于 Mem0 / 长期记忆模块，不是本模块职责）。
"""

import json
import logging

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.messages import messages_from_dict, messages_to_dict

from app.core.config import settings
from app.db.redis import redis_service
from app.services.chat_memory import is_working_message

logger = logging.getLogger("ChatShortMemoryService")

__all__ = ["ChatShortMemoryService", "chat_short_memory_service"]


class ChatShortMemoryService:
    """对话短期记忆（Redis 热窗口）"""

    def __init__(self) -> None:
        #)`：缺省值写在 config.py，此处直接读
        self._ttl_seconds = settings.CHAT_SHORT_MEMORY_TTL_SECONDS
        self._max_messages = settings.CHAT_SHORT_MEMORY_MAX_MESSAGES
        self._key_prefix = settings.CHAT_SHORT_MEMORY_KEY_PREFIX

    @property
    def window_size(self) -> int:
        """窗口容量（条）——)`，供调用方判断是否需回填"""
        return self._max_messages

    async def try_load(self, user_id: str, session_id: str) -> list[BaseMessage] | None:
        """尝试读取热窗口：命中返回消息列表，key 不存在或 Redis 故障返回 None。返回 None 的语义是「请回落到数据库」，
        因此**任何异常都吞掉转 WARN**，绝不向上抛。
        """
        try:
            raw = await redis_service.get(self._key(user_id, session_id))
            #(!raw) return null;` —— 空串与 None 同视作未命中
            if not raw:
                return None
            parsed: object = json.loads(raw)
            #(!Array.isArray(parsed)) return null;`
            if not isinstance(parsed, list):
                return None
            # 反序列化后再过滤一次：旧数据可能是早期版本写入的（含 system / tool），
            # 或跨语言部署时结构对不上——过滤保证调用方拿到的永远是干净窗口
            return [m for m in messages_from_dict(parsed) if is_working_message(m)]
        except Exception as err: # noqa: BLE001
            logger.warning("短期记忆 Redis 读取失败，改走数据库：%s", err)
            return None

    async def save(
        self,
        user_id: str,
        session_id: str,
        messages: list[BaseMessage],
    ) -> None:
        """写入热窗口：过滤有效消息 → 只留最近 maxMessages 条 → 带 TTL 落 Redis。写入失败只 WARN（缓存不可用不影响对话与落库）。
        """
        working = [m for m in messages if is_working_message(m)][-self._max_messages :]
        try:
            payload = json.dumps(messages_to_dict(working), ensure_ascii=False)
            await redis_service.set(
                self._key(user_id, session_id), payload, self._ttl_seconds
            )
        except Exception as err: # noqa: BLE001
            logger.warning("短期记忆 Redis 写入失败：%s", err)

    async def append_turn(
        self,
        user_id: str,
        session_id: str,
        history: list[BaseMessage],
        question: str,
        answer: str,
    ) -> None:
        """追加一轮问答后整体回写

        整体覆盖而非 RPUSH：窗口需要同时保证「只留最近 N 条」，用一次性写入更简单，
        也避免列表长度失控。
        """
        await self.save(
            user_id,
            session_id,
            [*history, HumanMessage(question), AIMessage(answer)],
        )

    async def clear(self, user_id: str, session_id: str) -> None:
        """删除热窗口)`：Python 侧方法名为 `delete`。
        删除失败只 WARN——至多让缓存多活一个 TTL，不影响会话本身。
        """
        try:
            await redis_service.delete(self._key(user_id, session_id))
        except Exception as err: # noqa: BLE001
            logger.warning("短期记忆 Redis 删除失败：%s", err)

    def _key(self, user_id: str, session_id: str) -> str:
        """窗口 key

        userId / sessionId 一律 str() 归一：调用方可能一处传 int 一处传 str
        （雪花 ID 在本项目里两种形态并存），不归一会在 Redis 里长出两个 key，
        表现为「明明存了却 miss」的诡异现象。
        """
        return f"{self._key_prefix}:{str(user_id)}:{str(session_id)}:messages"


# 全局单例
chat_short_memory_service = ChatShortMemoryService()
