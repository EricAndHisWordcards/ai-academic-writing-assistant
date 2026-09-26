"""FastAPI 应用入口。"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import db, paper_types
from app.llm import llm
from app.routers import config, projects

# 进程级的日志配置只能有一个入口，放在模块级：startup 里再配就晚于 import 期日志。
# 在此之前全仓的 logger 都是 logging.getLogger(__name__) 而根日志器从未配置过 ——
# Python 的兜底（lastResort）只处理 WARNING 及以上，于是 db.py 里那三条 INFO
# （「已迁移 N 个项目的论文类型名」「已清除 N 处文献元数据占位词」「已重算 N 个项目的
# 参考文献快照」）在真跑起来时全部被静默丢弃。它们记的恰恰是**自动改动了用户数据**
# 这件事，看不见就等于没有痕迹 —— 桌面版更是连控制台都没有。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="AI 学术写作辅助系统", version="1.0.0")

# CORS：允许前端开发服务器与打包后的桌面应用（file://）访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(config.router)


@app.on_event("startup")
def startup() -> None:
    db.init_db()
    # 后台任务不跨进程存活，重启后需把僵死的进度状态收拾干净，否则前端一直转圈
    db.reset_stale_generating()
    db.reset_stale_tasks()


@app.on_event("shutdown")
async def shutdown() -> None:
    # 进程退出时关掉 LLM 连接池。没有这一步，最后一个 AsyncOpenAI 连同它的 httpx
    # 池子一起随进程消失、不被主动 close —— 保存配置会 close 旧池（见 D11），但
    # 「退出时还握着的那一个」此前从没人关。
    await llm.close()


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/meta")
def meta():
    from app.citation_format import (
        CITE_STYLE_LABELS, FORMAT_LABELS, SUPPORTED_CITE_STYLES, SUPPORTED_FORMATS,
        citation_formats_for, default_format_for, format_note,
    )
    from app.config import settings
    from app.writing_lang import LANG_LABELS, SUPPORTED_LANGS, words_unit
    return {
        "llm_configured": bool(settings.llm_api_key),
        "llm_model": settings.llm_model,
        "paper_types": paper_types.PAPER_TYPES,
        # 短名 → 下拉框里显示的长名（含适用范围）。库里存的是短名，括号里的适用范围
        # 只是帮用户选对类型，所以它单独下发、不进数据库。
        "paper_type_labels": {
            name: paper_types.label_for(name) for name in paper_types.PAPER_TYPES
        },
        # 各类型的工序与差异（步骤顺序、是否必须有引用、设计字段名）整包下发。
        # 前端只渲染，不再自己拼顺序：后端的闸门（谁必须先有文献、谁必须有引用绑定）
        # 与前端步骤条是同一件事的两面，清单散落两处迟早漂移成「前端让点、后端 400」。
        #
        # theme_sections 是**从骨架现算**的（主题脉络章有几节），不是配在 TYPE_CONFIG
        # 里的常量：换算成常量就得在骨架改动时手工同步，而这正是它唯一会变的原因。
        # 前端拿它跟「作者确认了几个主题」比对，多出的会被合并、少了会被拆分。
        "paper_type_config": {
            name: {
                **paper_types.config_for(name),
                "theme_sections": paper_types.theme_section_count(name),
            }
            for name in paper_types.PAPER_TYPES
        },
        "citation_formats": SUPPORTED_FORMATS,
        # 写作语言（zh / en）与它的界面文案。选了英文就只剩 APA / MLA —— 这条判据
        # 的**唯一数据源**是 citation_formats_by_lang，前端不另立一份格式清单：
        # 抄一份过去的话，界面上会留着一个后端会 400 拒掉的选项，而它看起来完全正常。
        "writing_langs": SUPPORTED_LANGS,
        "writing_lang_labels": LANG_LABELS,
        "citation_formats_by_lang": {
            lang: citation_formats_for(lang) for lang in SUPPORTED_LANGS
        },
        "default_citation_format_by_lang": {
            lang: default_format_for(lang) for lang in SUPPORTED_LANGS
        },
        # 格式标识 → 选项文字。与 citation_formats_by_lang 配对使用：前者决定
        # 「能选哪些」，后者决定「选中后显示成什么」。「（默认）」由前端按上面那张
        # default 表现判（它随语言变，写死在标签里会在另一种语言下印错）。
        "citation_format_labels": FORMAT_LABELS,
        # 「中文写作下 APA / MLA 仍可选，但中文期刊极少用」这类提示。**是提示不是拦**：
        # 空串表示这个组合没什么要说的（英文下两种格式都常用）。
        # 内层遍历的是 `SUPPORTED_FORMATS`（全部格式）而不是 `citation_formats_for(lang)`
        # —— 两种语言的键集因此相同，其中「该语言选不到」的那几个也带着说明下发，
        # 界面用它来解释自己为什么不在下拉框里（见 citation_format._FORMAT_NOTES_BY_LANG）。
        "citation_format_notes_by_lang": {
            lang: {fmt: format_note(lang, fmt) for fmt in SUPPORTED_FORMATS}
            for lang in SUPPORTED_LANGS
        },
        # 角标样式：白名单与它的显示名，与格式那两项同款下发。它不随语言变（两种语言
        # 都用方括号角标），所以只有一张表、没有 by_lang。
        #
        # 下发它的理由与格式那张表**不一样**：格式下拉是「选项随语言变」所以必须下发；
        # 这里是为了让界面上的名字与后端那道 400 里的名字同源 —— 后端在报错时说的是
        # 「[1] 方括号角标」，而先前前端那份手写的 `<option>` 恰好也这么写，两边是
        # 靠人抄一致的。抄一致的东西迟早会不一致，而它对不上时用户拿着报错文案找不
        # 到自己选过的那一项。
        "cite_styles": SUPPORTED_CITE_STYLES,
        "cite_style_labels": CITE_STYLE_LABELS,
        # 目标字数的单位：中文按字、英文按词。界面直接显示它 —— 不显示的话用户会给
        # 英文论文填「5000 字」，而实际产出的是 5000 个词（约等于中文七千多字的体量）。
        "words_unit_by_lang": {lang: words_unit(lang) for lang in SUPPORTED_LANGS},
        # 目标总字数的下限。前端要在输入框旁边把它显示出来、并在按钮上拦一道，
        # 而下限是**后端一个真相**（projects.MIN_TARGET_WORDS）—— 在这里下发，
        # 前端就不必抄一份自己的数字（同「短名存库、长名下发」与 steps 那条）：
        # 抄一份的话，改了后端、界面上的提示会静静地继续报旧数。
        "min_target_words": projects.MIN_TARGET_WORDS,
    }
