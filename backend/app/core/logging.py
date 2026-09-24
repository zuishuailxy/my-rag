"""统一日志初始化

## 为什么需要这个模块

Python 的 `logging` 默认状态是「root 无 handler、level=WARNING」。不显式初始化的话：

1. 按名字取到的业务 logger（项目约定 `logging.getLogger("DocumentService")`）的
   **有效级别继承 root 的 WARNING** → 所有 `logger.info(...)` 被静默丢弃；
2. `logger.warning/error(...)` 倒是能出现在终端，但那是 `logging.lastResort` 兜底
   handler 干的（level=WARNING，裸 message 输出到 stderr）—— **没有时间戳、没有级别、
   没有 logger 名**，看起来就像自己 print 的一行字。

FastAPI / uvicorn 都不会替我们配置 root：实测 uvicorn 的默认 `LOGGING_CONFIG`
只声明了 `uvicorn` / `uvicorn.error` / `uvicorn.access` 三个 logger
（`"" not in LOGGING_CONFIG["loggers"]` 为 True）。这就是「WARN/ERROR 能看到、
INFO 全都看不到」的确切成因。

## 调用时机

`app/main.py` 模块顶层、import 之后立刻调用（早于 `create_app()`）。
uvicorn 是在 `import_from_string(app)` **之前** 完成它自己那份 dictConfig 的
（`Config.__init__` → `configure_logging()` 早于 `Server.serve()` → `config.load()`），
所以这里配置的 root 不会被 uvicorn 覆盖；反过来 `--reload` 子进程重新 import 时
也会重新走一遍本函数（dictConfig 是替换语义，不会叠加 handler）。

## 设计说明

- **级别由 `LOG_LEVEL` 配置项控制**：默认 INFO，加开关是为了排查时不用改代码
  （`LOG_LEVEL=DEBUG` 即可）。
- **uvicorn 自身日志并入统一格式**：Python 这边若不统一，
  启动日志会是 uvicorn 自己的 `INFO: Started server process` 样式，与业务日志
  两种风格混排，排查时很难读。
"""

import logging
import logging.config

from app.core.config import settings

# 输出格式：`2026-09-18 21:00:00 INFO [DocumentService] 中文消息 key=value`
# logger 名即类名（项目约定 `getLogger("DocumentService")`），所以 `[%(name)s]`
# 天然就是 `[ClassName]` 前缀，无需额外字段。
_LOG_FORMAT = "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 第三方库降噪：它们在 INFO 级别按「每个请求一行」写日志（httpx 发一次、ES 客户端
# 再来一次、mem0 内部还一次），会把业务日志淹掉。业务 logger 不在此名单内，一律
# 继承 root 的 LOG_LEVEL。名单只收「日志噪声确定大于价值」的库。
# ⚠️ 刻意**不含 sqlalchemy**：SQL 输出由引擎侧的 `DB_ECHO` 控制（见 config.py / db/session.py）。
# 把 `sqlalchemy.engine` 放进来会是个**假开关** —— SQLAlchemy 的 echo 为真时，
# `InstanceLogger.log()` 直接取 `_echo_map[echo]`（True→INFO）作为判定级别并调
# `logger._log()`，绕过 `Logger.isEnabledFor()`，因此无论把级别调到 WARNING 还是
# CRITICAL 都照样输出（实测）。其余 sqlalchemy 子 logger（pool / dialect）默认继承
# SQLAlchemy 自己设的 `sqlalchemy`=WARNING，无需重复声明。
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "elastic_transport",
    "elasticsearch",
    "neo4j",
    "aio_pika",
    "aiormq",
    "pymongo",
    "motor",
    "mem0",
    "openai",
    "langchain",
    "langchain_core",
    "asyncio",
)


def _resolve_level(raw: str) -> int:
    """把配置里的级别名解析为 logging 常量

    刻意**不抛异常**：日志级别拼错（`LOG_LEVEL=verbose`）不该让服务起不来，
    回退 INFO 即可。大小写不敏感（`debug` 等价 `DEBUG`）。
    """
    level = logging.getLevelNamesMapping().get(raw.strip().upper())
    return level if level is not None else logging.INFO


def setup_logging() -> None:
    """幂等初始化 root logger（已被别处配置过则不越权覆盖 formatter）"""
    root = logging.getLogger()
    level = _resolve_level(settings.LOG_LEVEL)

    if root.handlers:
        # 已经有 handler = 当前环境先被别的机制配过了（容器注入的 log config、
        # 宿主程序自带的 logging 设置等）。此时只把级别抬到配置值，不动别人的格式。
        root.setLevel(level)
        return

    logging.config.dictConfig(
        {
            "version": 1,
            # ⚠️ 必须 False。置 True 会把「已存在」的 logger 全部禁用，而 uvicorn 在
            # import app 之前就已经建好了 uvicorn / uvicorn.error / uvicorn.access，
            # 置 True 会让启动日志整批消失（且不报错）。
            "disable_existing_loggers": False,
            "formatters": {
                "default": {"format": _LOG_FORMAT, "datefmt": _DATE_FORMAT}
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "stream": "ext://sys.stderr",
                }
            },
            "root": {"handlers": ["console"], "level": level},
            "loggers": {
                # uvicorn 三个 logger 原本各挂自己的 handler 与 formatter
                # （启动日志形如 `INFO: Uvicorn running on ...`）。这里清掉 handler
                # 并打开向 root 的传播，让它们并入统一格式。
                # ⚠️ 刻意**不写 level**：dictConfig 省略 level 时保持日志器原值，这样
                # 命令行的 `uvicorn --log-level` 依然生效；写了就会把它静默覆盖掉。
                "uvicorn": {"handlers": [], "propagate": True},
                "uvicorn.error": {"handlers": [], "propagate": True},
                "uvicorn.access": {"handlers": [], "propagate": True},
            },
        }
    )

    # 降噪放在 dictConfig 之后，避免被上面的配置整段覆盖（dictConfig 只处理它声明过的
    # logger，未声明的保持原样，所以这里 setLevel 是安全的）。
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
