"""测试：LLM 本地配置（config 路由 + local_config）。"""
import asyncio

import pytest

from app.local_config import save_local_llm_config, get_local_llm_config


def test_local_config_roundtrip(tmp_path, monkeypatch):
    """本地配置保存与读取。"""
    from app import local_config
    monkeypatch.setattr(local_config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(local_config, "CONFIG_DIR", tmp_path)

    save_local_llm_config({
        "llm_api_key": "sk-test-123",
        "llm_base_url": "https://api.deepseek.com/v1",
        "llm_model": "deepseek-chat",
    })
    cfg = get_local_llm_config()
    assert cfg["llm_api_key"] == "sk-test-123"
    assert cfg["llm_base_url"] == "https://api.deepseek.com/v1"


def test_local_config_default_when_missing(tmp_path, monkeypatch):
    """配置文件不存在时返回默认值。"""
    from app import local_config
    monkeypatch.setattr(local_config, "CONFIG_FILE", tmp_path / "nonexist.json")
    cfg = get_local_llm_config()
    assert cfg["llm_api_key"] == ""
    assert cfg["llm_model"] == "deepseek-chat"


def test_config_api_get_and_update(tmp_db, monkeypatch, tmp_path):
    """config 路由：GET 查看 + POST 更新（用临时配置目录，避免污染用户配置）。"""
    from app import local_config
    monkeypatch.setattr(local_config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(local_config, "CONFIG_DIR", tmp_path)

    from app.main import app
    from fastapi.testclient import TestClient
    client = TestClient(app)

    # 初始未配置
    r = client.get("/api/config/llm")
    assert r.status_code == 200
    assert r.json()["llm_configured"] is False

    # 更新配置
    r = client.post("/api/config/llm", json={
        "api_key": "sk-new-key",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["llm_configured"] is True
    # Key 脱敏
    assert "sk-new-key" not in r.json()["llm_api_key_masked"]

    # 再次 GET 确认已配置
    r = client.get("/api/config/llm")
    assert r.json()["llm_configured"] is True


def test_config_api_key_masked(tmp_db, monkeypatch, tmp_path):
    """API Key 脱敏显示。"""
    from app import local_config
    monkeypatch.setattr(local_config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(local_config, "CONFIG_DIR", tmp_path)

    from app.main import app
    from fastapi.testclient import TestClient
    client = TestClient(app)

    client.post("/api/config/llm", json={"api_key": "sk-abcdef1234567890"})
    r = client.get("/api/config/llm")
    masked = r.json()["llm_api_key_masked"]
    # 只显示前4后4，中间脱敏
    assert masked.startswith("sk-a")
    assert masked.endswith("7890")
    assert "abcdef1234567890" not in masked


def test_config_update_without_key_keeps_runtime_key(tmp_db, monkeypatch, tmp_path):
    """只改模型、不动 Key 的保存，必须不碰运行时那份 Key（既有缺陷的回归测试）。

    Key 是脱敏下发的（GET 只给 llm_api_key_masked），前端拿不回原文、也就无法把它
    回传。若把「没传」当作「清空」，用户打开设置面板什么都不填、只点一下保存，就会把
    **来自环境变量 / .env 的** Key 抹掉：模型当场退化成演示模式，而且重启也救不回来
    —— 本地配置文件里本就没有这个 Key，运行时值是唯一一份。
    """
    from app import local_config
    monkeypatch.setattr(local_config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(local_config, "CONFIG_DIR", tmp_path)

    from app.config import settings
    from app.llm import llm
    # 模拟「Key 来自环境变量 / .env」：运行时已有一份，本地配置文件里没有
    monkeypatch.setattr(settings, "llm_api_key", "sk-from-env")
    monkeypatch.setattr(settings, "llm_base_url", "https://from-env/v1")
    assert llm.is_configured is True

    from app.main import app
    from fastapi.testclient import TestClient
    client = TestClient(app)

    # 只改模型：Key 与 base_url 都没传
    r = client.post("/api/config/llm", json={"model": "deepseek-reasoner"})
    assert r.status_code == 200
    assert r.json()["llm_configured"] is True, "点一下保存不该把 Key 弄没"
    assert settings.llm_api_key == "sk-from-env"
    assert settings.llm_base_url == "https://from-env/v1", "没传的字段一律不动"
    assert settings.llm_model == "deepseek-reasoner"
    assert llm.is_configured is True

    # 本地配置文件里也不该凭空多出一份 Key
    assert get_local_llm_config()["llm_api_key"] == ""

    # 显式传空串才是「清空」——那是调用方明确表达的意思
    r = client.post("/api/config/llm", json={"api_key": ""})
    assert r.json()["llm_configured"] is False
    assert settings.llm_api_key == ""


def test_config_update_keeps_local_file_key(tmp_db, monkeypatch, tmp_path):
    """没传 Key 时，磁盘上已有的 Key 不能被顺手擦掉。"""
    from app import local_config
    monkeypatch.setattr(local_config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(local_config, "CONFIG_DIR", tmp_path)
    save_local_llm_config({"llm_api_key": "sk-on-disk", "llm_model": "deepseek-chat"})

    from app.main import app
    from fastapi.testclient import TestClient
    client = TestClient(app)
    assert client.post("/api/config/llm", json={"model": "deepseek-reasoner"}).status_code == 200

    assert get_local_llm_config()["llm_api_key"] == "sk-on-disk"


def test_llm_close_closes_and_clears_client():
    """LLMClient.close() 关掉旧连接池再清空缓存（D11）。

    旧 reload() 只置 None，旧的 AsyncOpenAI 连同它的 httpx 连接池一起被丢弃、只能
    靠 GC 兜底，每次保存配置漏一个池子。AsyncOpenAI.close() 是 async 的，所以
    close() 也是 async —— 同步测试里用 asyncio.run 跑它。
    """
    from app.llm import llm

    closed = []

    class _FakeClient:
        async def close(self):
            closed.append(True)

    llm._client = _FakeClient()
    try:
        asyncio.run(llm.close())
        assert closed == [True], "close() 必须真的关掉底层 client"
        assert llm._client is None, "关掉后要清空缓存，下次调用才按新配置重建"
    finally:
        llm._client = None


def test_config_update_closes_old_client(tmp_db, monkeypatch, tmp_path):
    """保存配置要关掉旧连接池（D11 端到端）：POST /api/config/llm 会 close 旧 client。

    这条是真正的回归护栏：它锁的是「路由调用的是 close 而不是 reload」。若有人把
    await llm.close() 改回 llm.reload()，上面的单测照样绿，但泄漏又回来了 —— 只有
    这一条能抓住。
    """
    from app import local_config
    monkeypatch.setattr(local_config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(local_config, "CONFIG_DIR", tmp_path)

    from app.llm import llm
    closed = []

    class _FakeClient:
        async def close(self):
            closed.append(True)

    llm._client = _FakeClient()
    try:
        from app.main import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        r = client.post("/api/config/llm", json={"api_key": "sk-new", "model": "deepseek-chat"})
        assert r.status_code == 200
        assert closed == [True], "保存配置要把旧 client 关掉"
        assert llm._client is None
    finally:
        llm._client = None
