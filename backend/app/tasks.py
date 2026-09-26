"""通用后台任务登记表。

分段生成（routers/projects._GEN_TASKS）已经验证过「后台任务 + 逐级落盘 + 前端轮询」
这套机制能扛住几分钟的耗时：POST 只点火后立即返回，真正的循环跑在 asyncio 任务里，
进度写进 projects.task_state_json，前端轮询读。

大纲生成与文献解析是同一种形状的等待，所以把登记与生命周期抽到这里，而不是把
_GEN_TASKS 那套抄三遍。两者共用一套代码，但进度字段各写各的列——generation_state_json
有逐节 words/eta/续写等专用字段，任务状态列只放通用字段，互不干扰。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Coroutine

logger = logging.getLogger(__name__)

# 只在事件循环线程里读写（路由与任务的 finally 同线程），无竞态
_TASKS: dict[str, asyncio.Task] = {}


def is_running(key: str) -> bool:
    """该 key 是否已有任务在跑 —— 路由据此返回 409，避免同一项目跑两遍。"""
    task = _TASKS.get(key)
    return task is not None and not task.done()


def spawn(key: str, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """起一个后台任务并登记；任务结束（无论成败）自动从登记表摘除。

    返回的 Task 可以 await：它的结果就是协程的返回值（协程抛异常时是 None —— _guard
    把异常吞掉并记日志了）。绝大多数调用方是「点火即走」，不看返回值；引用的相关性
    调度是唯一一个「派出去、等它、结果当场要用」的调用点，于是走同一条登记路径，
    而不是为它另建一套登记。

    必须从 async def 路由里调用 —— def 路由跑在线程池里，没有运行中的事件循环，
    asyncio.create_task 会直接抛 RuntimeError。
    """
    task = asyncio.create_task(_guard(key, coro))
    _TASKS[key] = task
    return task


async def _guard(key: str, coro: Coroutine[Any, Any, Any]) -> Any:
    """跑协程、保证登记表不泄漏，并把协程的返回值原样传出来。

    「传出来」这条存在的理由只有一个：调用方可以 `await tasks.spawn(...)` 后拿到结果
    （见 spawn 的 docstring）。抛异常时返回 None，调用方**必须自己判 None**，别把
    它当成合法结果。
    """
    try:
        return await coro
    except asyncio.CancelledError:
        # CancelledError 继承自 BaseException，`except Exception` 抓不到它。不单独
        # 接住的话它会绕过 finally 之外的任何处理，把任务永久留在「运行中」。
        raise
    except Exception:  # noqa: BLE001
        # 任务自身的失败处理由传入的协程负责（它才知道该把项目退回哪个状态）。
        # 这里只保证登记表不泄漏。仍记一笔日志：协程里若在写失败状态时自己炸了，
        # 否则这条线索就彻底消失了。
        logger.exception("后台任务 %s 异常退出", key)
    finally:
        _TASKS.pop(key, None)
