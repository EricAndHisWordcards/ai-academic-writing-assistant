"""桌面应用后端启动入口（供 PyInstaller 打包）。

直接运行此脚本会启动 uvicorn 服务。
"""
import os
import sys

# 确保能 import app 包（打包后路径处理）
if getattr(sys, "frozen", False):
    # PyInstaller 打包模式：资源在 _MEIPASS
    bundle_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    sys.path.insert(0, bundle_dir)

    # 窗口模式（run_desktop.spec 里 console=False）下 Python 没有控制台，
    # sys.stdout / sys.stderr 是 None —— 而 uvicorn 自己的日志与 app.main 的
    # logging.basicConfig 都要往 stderr 写，第一次写就
    # AttributeError: 'NoneType' object has no attribute 'write'，进程在
    # "Application startup complete" 之前就带着一个 traceback 弹窗退出，
    # 表现是「双击没反应 / 后端起不来」而日志里什么都没有。
    # 给它们一个真实可写的去处：落到 cwd 下的 backend.log。
    #
    # 注意这条兜底**只在直接双击 exe 时**生效。被 Electron 拉起时 stdio 是管道
    # （main.cjs 的 stdio: 'pipe'），stdout 不是 None，于是日志顺着管道交给主进程，
    # 由它抄一份到 ~/.academic_writer/backend.log —— 两条路径下都有文件可查，
    # 只是位置不同（那里同时也是 config.json 与数据库所在）。
    if sys.stdout is None or sys.stderr is None:
        try:
            # 行缓冲：崩溃时已经写下的那几行不会留在缓冲区里丢掉
            _log_stream = open(
                os.path.join(os.getcwd(), "backend.log"),
                "a", encoding="utf-8", buffering=1,
            )
        except OSError:
            _log_stream = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = _log_stream
        if sys.stderr is None:
            sys.stderr = _log_stream

import uvicorn  # noqa: E402

# 「真正跑 Python 的那个进程」的 pid 落点。目录与 main.cjs 的 backend.log 同一个
# （~/.academic_writer），这样桌面版要在用户目录里留什么东西，只有这一处。
PID_FILE = os.path.join(os.path.expanduser("~"), ".academic_writer", "backend.pid")


def _write_backend_pid() -> None:
    """把当前进程的 pid 写到 `PID_FILE`，供 Electron 关闭时精确收掉。

    **为什么需要它**：PyInstaller 的 onefile 是**两层进程**。外层是 bootloader，它把
    资源解压到 `%TEMP%\\_MEIxxxxxx`，起一个同名的子进程去跑真正的 Python，并在子进程
    退出后**由它自己**删掉那个目录。而 Electron 原先走的是
    `taskkill /PID <父> /T /F` —— `/T` 把整棵树一次打死，bootloader 没机会收尾，
    于是**每开关一次应用就在 `%TEMP%` 里留下约 28 MB**（实测）。只杀内层这一个 pid，
    bootloader 就能正常做完它那一步。

    本函数写在 `sys.frozen` 判断**之外**是有意的：源码态直跑时也写文件。于是
    `stop-backend.cjs` 那条「镜像名必须是 academic_backend.exe」的核对会把它排除掉
    （源码态那个进程叫 python.exe），自动回落成 `/T /F` —— 那正是这条核对的作用，
    见那边的注释（进程号会被复用，不能只信 pid）。

    **失败一律不抛**：写不成只是让关闭时回落成 `/T /F`、`%TEMP%` 里多留一份解压目录，
    与加这个函数之前完全一样；而在这里抛出去会让整个后端起不来，代价大一个量级。
    """
    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, "w", encoding="ascii") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    _write_backend_pid()
    uvicorn.run("app.main:app", host="127.0.0.1", port=port, log_level="info")
