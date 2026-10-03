# PyInstaller 打包配置：将后端打包为独立可执行文件
# 用法：pyinstaller run_desktop.spec

# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

a = Analysis(
    ['run_desktop.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('app', 'app'),  # 打包 app 包（含源码）
    ],
    hiddenimports=[
        # uvicorn 通过字符串 "app.main:app" 加载应用，PyInstaller 静态分析
        # 不会跟进 app/main.py，必须显式声明，否则 fastapi / pydantic /
        # openai / pypdf 等依赖不会被打进 exe（运行时报 ModuleNotFoundError）。
        'app.main',
        'app.routers.projects',
        # D-34 拆分后的域模块：由 projects 静态导入，理论上会被分析跟进，
        # 显式声明是按「路由模块全部列入 hiddenimports」的惯例上保险。
        'app.routers.projects_common',
        'app.routers.projects_outline',
        'app.routers.projects_documents',
        'app.routers.projects_citations',
        'app.routers.projects_generation',
        'app.routers.config',
        'app.agents.topic_agent',
        'app.agents.outline_agent',
        'app.agents.parse_agent',
        'app.agents.schedule_agent',
        'app.agents.generate_agent',
        'app.agents.polish_agent',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='academic_backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 无控制台窗口
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
