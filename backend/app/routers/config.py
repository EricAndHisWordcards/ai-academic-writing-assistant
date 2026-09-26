"""配置路由：LLM API Key 的查看与更新（桌面应用场景）。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings
from app.llm import llm
from app.local_config import get_local_llm_config, save_local_llm_config

router = APIRouter(prefix="/api/config", tags=["config"])


class LLMConfigUpdate(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None


def _mask_key(key: str) -> str:
    """脱敏显示 API Key（只显示前 4 后 4 位）。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}...{key[-4:]}"


@router.get("/llm")
def get_llm_config():
    """返回当前 LLM 配置（Key 脱敏）。"""
    return {
        "llm_configured": bool(settings.llm_api_key),
        "llm_model": settings.llm_model,
        "llm_base_url": settings.llm_base_url,
        "llm_api_key_masked": _mask_key(settings.llm_api_key),
    }


@router.post("/llm")
async def update_llm_config(body: LLMConfigUpdate):
    """更新 LLM 配置并持久化到本地配置文件。

    语义是**「没传的字段 = 不动」，不是「没传的字段 = 清空」**。这一条对 Key 尤其
    要命：Key 会被脱敏后才下发（GET 只给 llm_api_key_masked），前端永远拿不回原文，
    也就永远没法把这个字段回传给我们。若把「没传」当成「清空」，用户打开设置面板、
    什么都不填、只点一下保存，就会把**来自环境变量 / .env 的** Key 当场抹掉 ——
    模型立刻退化成演示模式，而且重启也救不回来（本地配置文件里本就没有这个 Key，
    运行时值是唯一一份）。原实现在这里正是如此：拿本地文件里的空串去覆盖运行时值。

    所以两件事分开：**磁盘上保留文件里的原值**（不因一次保存擦掉文件里的配置），
    **运行时只应用调用方真正传了的字段**（update_llm 收到 None 就跳过该字段）。
    显式传空串仍然是「清空」——那是调用方明确表达的意思。
    """
    current = get_local_llm_config()
    new_base_url = body.base_url if body.base_url is not None else current.get("llm_base_url", "")
    new_model = body.model if body.model is not None else current.get("llm_model", "deepseek-chat")

    # 保存到本地配置（Key 未传时沿用文件里的原值，没有就写空串）
    save_local_llm_config({
        "llm_api_key": body.api_key if body.api_key is not None else current.get("llm_api_key", ""),
        "llm_base_url": new_base_url,
        "llm_model": new_model,
    })

    # 运行时更新 settings 并关闭旧连接池、清空缓存（下次调用按新配置重建）——
    # 只应用真正传了的字段
    settings.update_llm(
        api_key=body.api_key,
        base_url=body.base_url,
        model=body.model,
    )
    await llm.close()

    return {
        "ok": True,
        "llm_configured": bool(settings.llm_api_key),
        "llm_model": settings.llm_model,
        "llm_api_key_masked": _mask_key(settings.llm_api_key),
    }
