"""应用配置：从环境变量 / .env / 本地配置文件加载。

优先级：环境变量 > .env > 本地配置文件（~/.academic_writer/config.json）> 默认值。
支持运行时更新 LLM 配置（桌面应用场景）。
"""
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.local_config import get_local_llm_config


class Settings(BaseSettings):
    """全局配置项。"""

    # LLM
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.3

    # 服务
    host: str = "127.0.0.1"
    port: int = 8000

    # 数据库
    database_path: str = "./data/app.db"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def apply_local_config(self) -> None:
        """用本地配置文件兜底填充未设置（空值）的 LLM 配置。"""
        local = get_local_llm_config()
        if not self.llm_api_key and local.get("llm_api_key"):
            self.llm_api_key = local["llm_api_key"]
        if not self.llm_base_url and local.get("llm_base_url"):
            self.llm_base_url = local["llm_base_url"]
        if not self.llm_model and local.get("llm_model"):
            self.llm_model = local["llm_model"]

    def update_llm(self, api_key: str | None = None,
                   base_url: str | None = None,
                   model: str | None = None) -> None:
        """运行时更新 LLM 配置。"""
        if api_key is not None:
            self.llm_api_key = api_key
        if base_url is not None:
            self.llm_base_url = base_url
        if model is not None:
            self.llm_model = model


settings = Settings()
settings.apply_local_config()
