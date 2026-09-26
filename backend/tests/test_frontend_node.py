"""测试：前端纯 JS 模块的 node:test 用例（exporters / api / wait-for-backend / stop-backend）。

前端没有 JS 测试运行器，但 Node 24 内置 node:test，且 exporters.js / api.js /
wait-for-backend.cjs / stop-backend.cjs 都是不含 React/DOM/electron 的纯模块，可以直接
驱动。这些模块正是「改坏了也不报错、只会让导出的文件坏掉 / 请求发错 / 桌面版后端起不来
或关不干净」的那类静默缺陷 —— pytest 读 App.jsx 文本只能钉住前端**约定**，钉不住它们的
**行为**。
"""
import subprocess
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
TEST_DIR = FRONTEND / "tests"


def test_frontend_node_unit_tests_pass():
    """四个 node:test 文件必须全绿。"""
    # 逐个点名，不传目录：`node --test <目录>` 在 Windows 上会把目录当成要 require 的
    # 模块（报 MODULE_NOT_FOUND）。相对路径以 cwd=frontend 为基准。
    files = [
        "tests/exporters.test.mjs",
        "tests/api.test.mjs",
        "tests/wait-for-backend.test.mjs",
        "tests/stop-backend.test.mjs",
    ]
    for f in files:
        assert (FRONTEND / f).exists(), f"缺少前端单测文件：{f}"

    proc = subprocess.run(
        ["node", "--test", *files],
        cwd=str(FRONTEND),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, (
        f"前端 node:test 失败（exit {proc.returncode}）：\n{proc.stdout}\n{proc.stderr}"
    )
