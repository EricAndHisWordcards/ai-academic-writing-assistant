"""LLM 抽象层：封装 OpenAI 兼容接口，统一调用入口。

用法：
    from app.llm import llm
    reply = await llm.chat(messages=[{"role": "user", "content": "..."}])

支持 JSON 模式输出（结构化抽取）。
"""
from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from app.config import settings


class LLMClient:
    """OpenAI 兼容的异步 LLM 客户端。"""

    def __init__(self) -> None:
        self._client: AsyncOpenAI | None = None

    def reload(self) -> None:
        """清空缓存的 client（下次调用时按新配置重建），**不关连接池**。

        只在测试里用：把 monkeypatch 过的 settings 或 stub 生效。同步上下文关不掉
        AsyncOpenAI（它的 close 是 async 的），而测试进程短命，漏掉的池子无所谓。
        生产路径（保存配置、进程退出）一律走 close()。
        """
        self._client = None

    async def close(self) -> None:
        """关掉底层连接池并清空缓存。

        AsyncOpenAI.close() 是 async 的，所以这里也是 async。旧实现保存配置只调
        reload()（置 None），旧的 AsyncOpenAI 连同它的 httpx 连接池一起被丢弃、只能
        靠 GC 兜底 —— 每次保存配置漏一个池子（D11）。改用它之后，生产路径不再调
        reload()。没创建过 client 时是 no-op（进程退出的 shutdown 也靠这个兜住）。
        """
        if self._client is not None:
            await self._client.close()
            self._client = None

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            kwargs: dict[str, Any] = {"api_key": settings.llm_api_key or "sk-dummy"}
            if settings.llm_base_url:
                kwargs["base_url"] = settings.llm_base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    @property
    def is_configured(self) -> bool:
        return bool(settings.llm_api_key)

    async def _create(
        self,
        messages: list[dict[str, str]],
        temperature: float | None,
        max_tokens: int | None,
        json_mode: bool,
    ) -> Any:
        """发起一次对话补全，返回原始响应对象。"""
        kwargs: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,  # type: ignore[arg-type]
            "temperature": settings.llm_temperature if temperature is None else temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        return await self.client.chat.completions.create(**kwargs)

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> str:
        """发起一次对话补全，返回文本内容。"""
        resp = await self._create(messages, temperature, max_tokens, json_mode)
        return resp.choices[0].message.content or ""

    async def chat_with_finish(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> tuple[str, str]:
        """对话补全，同时返回 (content, finish_reason)。

        finish_reason == "length" 表示输出被 max_tokens 截断（推理模型的思考
        token 同样计入 max_tokens，故推理模型更容易触发截断）。
        """
        resp = await self._create(messages, temperature, max_tokens, json_mode)
        choice = resp.choices[0]
        return (choice.message.content or ""), (choice.finish_reason or "")

    async def chat_json(self, messages: list[dict[str, str]], **kw: Any) -> Any:
        """对话并解析 JSON 输出。"""
        text = await self.chat(messages, json_mode=True, **kw)
        # 去除可能的 markdown 代码块包裹
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)


# 全局单例
llm = LLMClient()
