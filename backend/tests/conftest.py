"""pytest 共享夹具：隔离测试数据库，避免污染开发数据。"""
import gc
import os
import sys
import tempfile
from pathlib import Path

import pytest

# 确保 backend 目录在 import 路径中（tests 目录的父目录）
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture(autouse=True)
def _isolate_llm_config(monkeypatch):
    """测试默认离线：清空环境（.env / 本地配置）带入的 LLM 配置。

    若本机 backend/.env 配置了真实 API Key，未隔离时测试会真的发起网络请求，
    既慢又不可重复（且会消耗真实额度）。需要 LLM 的用例在本用例内自行设置
    settings 即可，monkeypatch 会在用例结束后自动还原。
    """
    from app.config import settings
    from app.llm import llm

    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_base_url", "")
    monkeypatch.setattr(settings, "llm_model", "deepseek-chat")
    llm.reload()
    yield
    llm.reload()


@pytest.fixture()
def tmp_db(monkeypatch):
    """使用临时数据库，测试结束后自动清理。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    # 真正生效的是下面这行 setattr，不是 setenv：settings 是 import 期就构造好的模块级
    # 单例（config.py 末尾 `settings = Settings()`），此后没人再读环境变量。
    from app.config import settings
    monkeypatch.setattr(settings, "database_path", path)
    # 临时库一律不用 WAL：WAL 下每个连接干净关闭都要建/拆一次 -wal / -shm，一个用例光
    # init_db() 就是 6 次连接，237 个建库用例把全量 pytest 从约 5 分钟拖到约 24 分钟。
    # **必须排在 init_db() 之前** —— 那 6 次连接里的第一次就把模式写进库文件了。
    monkeypatch.setattr(settings, "database_wal", False)

    from app import db
    db.init_db()
    yield db
    # 清理。**刻意不吞 OSError**：删不掉就说明还有连接没关。这里原先写的是
    # `except OSError: pass`，而当时 `get_conn()` 是「退出不关闭、靠 GC 回收」的形态，
    # Windows 上句柄没释放时 os.remove 抛 [WinError 32] —— 于是「每跑一轮测试就往
    # TEMP 里留下几百个删不掉的临时库」被这行 pass 静默盖住了整整几轮。
    # 一个删不掉的临时文件本身就是信号，不能当没看见：先给 GC 一次机会兜底，仍然
    # 删不掉就报错点名路径，让人能顺着它去找那个没关的连接。
    try:
        os.remove(path)
    except OSError as e:
        gc.collect()
        try:
            os.remove(path)
        except OSError:
            raise AssertionError(
                f"临时数据库删不掉，说明仍有连接未关闭：{path}（首次失败：{e}）"
            ) from None
