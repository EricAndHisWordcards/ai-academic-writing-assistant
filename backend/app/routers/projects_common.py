"""项目路由的共享层：请求体模型、常量与跨域共用的判据 / 工具。

projects.py 原是单文件路由（41 个路由 + 约 60 个辅助函数），拆分后各域模块
（outline / documents / citations / generation）各自持有 router；凡是**两个及以上
域共用**的东西——请求体模型、忙闲登记、在飞登记键、语言 / 标题 / 章节判据——
都收在这里。只被单个域使用的函数留在那个域。

文件名**不带前导下划线**：.gitignore 的 `_*.py` 把下划线开头的 .py 当本地草稿
（`!__*.py` 只豁免双下划线的真模块），带下划线会被静默挡在仓库外，fresh clone
直接缺文件。

⚠️ `_GEN_TASKS` 是**进程内内存 dict**，全仓只能有这一个对象。各域模块与门面一律
`from .projects_common import _GEN_TASKS` 拿**同一个对象**；任何模块都不许对它整体
重新赋值（那会造出第二个 dict，409 互斥当场失效并双倍烧 token）。后端因此必须
单进程，见 DEPLOY.md §5.3。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from pydantic import BaseModel

from app import db, tasks, writing_lang


# ---------------------------------------------------------------
# 请求体模型
# ---------------------------------------------------------------
class ProjectCreate(BaseModel):
    title: str | None = None


# 标题长度上限。比 cluster_agent 的节标题（60 字）宽得多：那是一句小节标题，这是
# 一整篇论文的题名。只为挡住「整段粘进输入框」那种输入。
MAX_TITLE_CHARS = 200

# 写作思路（写作思路 / 论证思路）的长度上限。取值有现成锚点：material_parser.
# EXCERPT_CHARS 是「一份作者自有材料进单节提示词的字符上限」，也是 2000 ——
# 一条思路块 = 每节多带一份材料摘录，这个量级已经过实测。
# 上限只由**后端**拦（前端不镜像、不加 maxLength）：静默截断比拒绝更坏，
# 作者的方案被吃掉一半而永不知情，论文会照着另一条线写下去。
MAX_IDEAS_CHARS = 2000

# 目标总字数的下限。它是这套流程里**唯一会被按节分掉的**输入：大纲步拿它做字数预算，
# 逐节拆下去。低到一定量级以下，「按节分」这件事本身就失去意义 —— 每节只剩几十字，
# 大纲退化成一串标题、正文一节一句话，用户拿到的是一份看起来完整、实际没法用的稿子。
# 1000 字取在退化之前（课程论文量级的底线），而不是一个「技术上能算」的最小值。
#
# 与上限同一口径：**路由内 400，不用 pydantic 的 Field(ge=...)** —— 后者抛 422，
# 前端 api.js 的 `new Error(err.detail)` 会把 detail 数组渲染成 [object Object]。
# 前端另有一道同值的检查，但它的职责只是**把话说在按钮正上方**（App 级那条横幅在
# 步骤卡片之上，而这个卡片很高，用户按的按钮在卡片最下面），不是第二道闸：
# 真正拦住写入的只有这里这一道。
MIN_TARGET_WORDS = 1000


class ProjectTitle(BaseModel):
    """改名请求体。

    空串是**合法**输入：它的含义是「我不自己起名，跟随研究核心方向」，而不是
    「把标题清掉」——清空之后项目显示的是选题，界面上不会出现一个没有名字的项目。
    """
    title: str = ""


class TopicSet(BaseModel):
    """选题与设定。

    writing_ideas 是作者自己写的写作思路 / 论证思路（可空串 = 没写或不写了）。
    **与三个兄弟字段同为覆盖语义**：省略即当空串处理。三个兄弟字段是必填（省略会被
    pydantic 挡成 422，根本不存在"静默覆盖"），所以这里若单独开一套"省略 = 不改"
    的合并语义，就会让同一个接口里两套语义并存；而唯一真实客户端（前端
    confirmTopic）恒定四个字段一起发，那种分支一行也走不到。
    """
    paper_type: str
    target_words: int
    topic: str
    writing_ideas: str = ""
    # 第五个字段。上面那段"覆盖语义"的推理**在这里翻了个面**，理由如下：
    # 覆盖语义之所以安全，是因为省略时的默认值与「什么都没写过」同义 ——
    # writing_ideas 省去 = 空 = 库里没写过，四个字段各自都成立。而语言省去的默认值
    # **带破坏性**：默认成中文会把一个英文项目掰回中文，并且因为 `changed` 判真而把
    # 大纲、正文、引用绑定、材料计划、参考文献快照、主题聚类与生成进度**整份作废**
    # —— 用户没做错任何事，只因为调用方少发了一个字段。旧值 `"zh"` 就是踩了这个坑
    # （只有直接调接口的调用方到得了；前端 confirmTopic 恒定五个字段一起发）。
    # 所以这里用空串当哨兵：**省略 = 不改**，读库里的那份（见 set_topic 里的读法）。
    writing_lang: str = ""


class TopicRecommend(BaseModel):
    domain: str
    paper_type: str
    target_words: int
    count: int = 3
    # 写作语言的**草稿值**：这一步还没确认过项目，库里那份可能还是建项目时的默认值
    # （中文）。前端表单里选的语言优先，空串（= 没发）才回落到库里那份 ——
    # 与 TopicSet 的哨兵同款读法，但含义不同：那边是「没发就不改」，这里是「没发就用
    # 库里那份」。两条路都不写库，所以这个接口不进 changed 判定。
    writing_lang: str = ""


class OutlineConfirm(BaseModel):
    outline: dict


class MaterialText(BaseModel):
    text: str
    label: str | None = None


class CitationConfirm(BaseModel):
    binding: dict
    cite_style: str = "bracket"  # bracket=[1] 方括号；superscript=纯数字上标
    citation_format: str = "gb7714"  # gb7714 / apa / mla


class DesignSave(BaseModel):
    fields: dict[str, str]


class ClustersConfirm(BaseModel):
    """作者改过后的主题聚类。

    只收标题、概述、要点与所属文献 id：doc_titles 由服务端按 id 反查，不收 ——
    让请求体带回文献标题，等于让前端有机会把一篇文献的名字写错。
    """
    clusters: list[dict]
    notes: str = ""


class DocumentPatch(BaseModel):
    """文献元数据编辑请求体（字段清单与 `DOC_FIELD_LIMITS` 逐项对应）。

    **`None` = 不修改这个字段**，与 `TopicSet` 的「省略即当空串」刻意不同。那里的四个
    字段只有一个真实客户端、恒定整份发送，覆盖语义没有代价；而这里是「用户发现某一个
    字段被模型抽错了」——用覆盖语义的话，前端少发一个字段就等于把它清空，用户点一次
    保存会静默抹掉另外七个（题名被清空还会连带把绑定里那份 doc_title 换成文件名）。
    空串 = 明确清空该字段，是有意义的写入（见 db.update_document_meta）。

    **声明顺序无所谓真实保护范围**：字段是给 pydantic 认键用的，真正驱动校验与落库的是
    `DOC_FIELD_LIMITS` 与 `db.DOC_META_COLUMNS`。只在这里加一个字段而没加进那两处，
    请求会被静默忽略 —— 用户填了值、点保存、收到成功，而库里什么都没变。
    """
    title: str | None = None
    # 题名英译（`documents.title_en`）。只有 APA / MLA 会用到它：非英语文献按 APA 7
    # §9.38 著录成「原文题名 [English translation].」。GB/T 7714 一个字都不用它。
    title_en: str | None = None
    authors: str | None = None
    year: str | None = None
    source: str | None = None
    volume: str | None = None
    issue: str | None = None
    page_range: str | None = None
    source_type: str | None = None
    # 非期刊类型的三项著录字段（GB/T 7714 的 [M]/[D]/[C]/[N] 各需一项），见
    # db.documents 建表处的注释。只有 GB/T 那条分支读它们，其中 publish_date 会先过
    # citation_gb_types.normalize_publish_date 收敛成 YYYY-MM-DD 再印。
    place: str | None = None
    edition: str | None = None
    publish_date: str | None = None


# 各字段的长度上限与中文名（400 文案要用）。走**路由内 400**而不是 pydantic 的
# `Field(max_length=...)`：后者抛 422，而前端 api.js 的 `new Error(err.detail)` 会把
# detail 数组渲染成 [object Object]（与 MIN_TARGET_WORDS 同一个理由）。
#
# 尺度按真实值的长尾取，不按「一般看起来多长」：作者串要放得下 20 人以上的完整名单
# （GB/T 7714 只印前 3 位，但库里存的是原话），来源要放得下「河海大学学报: 自然科学版」
# 这类带分辑的写法。宽一点没有代价 —— 它们的下游是拼字符串，不是定长字段。
DOC_FIELD_LIMITS = {
    "title": (300, "题名"),
    "title_en": (300, "英译题名"),
    "authors": (300, "作者"),
    "year": (20, "年份"),
    "source": (200, "来源"),
    "volume": (20, "卷"),
    "issue": (20, "期"),
    "page_range": (40, "页码"),
    "source_type": (8, "文献类型"),
    # 非期刊类型的三项。上限按真实值的长尾取，与上面同一条尺度：`place` 要放得下
    # 「北京市」这类全称，`edition` 要放得下「第 3 版（修订本）」与「2nd ed.」，
    # `publish_date` 与 year 同档（归一化前的原始印法可能更长，如「2023 年 5 月 4 日」）。
    "place": (100, "出版地"),
    "edition": (40, "版本项"),
    "publish_date": (40, "出版日期"),
}


# ---------------------------------------------------------------
# 生成忙闲登记（跨域共享）
# ---------------------------------------------------------------
# 一次生成 = 串行几十次 LLM 调用，动辄数分钟，远超 nginx 的 300s 读超时。
# 所以 POST /generate 只「点火」后立即返回，真正的循环跑在 asyncio 后台任务里，
# 进度逐节落盘到 generation_state_json，前端轮询 /generate/progress 取用。
# 进度放在库里而非内存里，正是「刷新页面不丢进度」的前提。
_GEN_TASKS: dict[str, asyncio.Task] = {}
# _GEN_TASKS 只在事件循环线程里读写（/generate 路由与任务的 finally 同线程），无竞态。

# 生成任务的忙提示单独一条：用户此时要做的是等它跑完、或去「分段生成」步看进度，
# 而不是回到上游那一步重做点什么。文案与删除项目/删除文献两处保持一致，
# 各站点的差异只在后面追加的那半句（「无法删除文献」这样）。
_GEN_BUSY_MESSAGE = "该项目正在生成正文"


def _gen_running(project_id: str) -> bool:
    """本项目是否正在生成正文。

    与 `_busy_task` 是**两套独立登记**，必须分别查：生成任务不进 `tasks._TASKS`
    （它的进度写 `generation_state_json`，与 `task_state_json` 形状不同），所以
    `_busy_task` 永远看不见它，反之亦然。凡是会改这个项目状态、或改它上游产物的
    入口，都要把这一条补上 —— 漏掉的后果不是少了个提示，而是两件事同时写同一个
    status，界面按 status 渲染的那一步跳走，后台却还在写正文。
    """
    task = _GEN_TASKS.get(project_id)
    return task is not None and not task.done()


# 各类后台任务的在飞登记键。键函数族与 _busy_task 必须在同一处：每加一个后台任务，
# 两边要一起加，互斥才看得见它。
def _outline_key(project_id: str) -> str:
    return f"outline:{project_id}"


def _documents_key(project_id: str) -> str:
    return f"documents:{project_id}"


def _analysis_key(project_id: str) -> str:
    return f"material_analysis:{project_id}"


def _design_key(project_id: str) -> str:
    return f"design_extract:{project_id}"


def _clusters_key(project_id: str) -> str:
    return f"clusters:{project_id}"


def _citations_key(project_id: str) -> str:
    """引用调度的在飞登记键。

    它与上面那五个**形状不同**：引用调度不开「点火即走」的后台任务（它自己 await 一次
    相关性判断，结果当场被同一个请求写库并回报）。但它仍然要登记 —— 见 _busy_task 的注释。

    **v1.29 起它也写进度了**，上一段原来的说法是「所以它不写 task_state_json、界面上也
    没有进度条」：那句话在 v1.20 定下来时是对的，代价是那 120 秒里界面一个字都不给 ——
    按钮还亮着、点下去才拿到 409（PRD §8.1 曾把这条盲区记作"刻意留着"）。现在它按同一套
    _write_task 写两个阶段与终态（kind="citations"），前端 POLLED_TASK_KINDS 里也有它：
    **同一个槽、同一张表**，不是第二处真相。
    """
    return f"citations:{project_id}"


# 这些任务写的是同一列 task_state_json。若允许它们并行，各自的进度会互相覆盖，
# 前端看到的「正在做什么」会在两条文案之间跳。所以对同一个项目互斥。
_BUSY_MESSAGE = {
    "outline": "该项目的大纲正在生成中",
    "documents": "该项目的文献正在解析中",
    "material_analysis": "该项目的材料正在分析中",
    "design_extract": "该项目的设计正在提炼中",
    "clusters": "该项目的主题聚类正在生成中",
    "citations": "该项目的引用正在调度中",
}


def _busy_task(project_id: str) -> str | None:
    """本项目上正在跑的排他操作类型，无则 None。

    下面登记的是「占用这个项目的写操作」，不限于「会写 task_state_json 的后台任务」。
    每加一个都必须在这里加一行：漏掉不是「少了个提示」，而是两件事同时写同一个项目
    —— 进度列互相覆盖、前端按 kind 渲染的那一步在两条文案之间跳、确认按钮被点到一半
    的产物上，或者一个在飞的写入把上游刚定下的状态顶回去（引用调度就属于最后一类：
    它只登记、不写进度列）。
    「只登记、不写进度列」这半句**v1.29 起不成立**（它从此也写那一列，见 _citations_key）：
    但下面这张表一行都不用改 —— 它记的是「谁占用这个项目」，与占的人写不写进度无关，
    而这正是当初把它登记进来的理由。
    """
    if tasks.is_running(_outline_key(project_id)):
        return "outline"
    if tasks.is_running(_documents_key(project_id)):
        return "documents"
    if tasks.is_running(_analysis_key(project_id)):
        return "material_analysis"
    # 设计提炼必须在列：它写的是同一列 task_state_json，且用户完全可能
    # 在提炼没结束时就去点「生成大纲」，那样大纲拿到的还是一份空的 design_json ——
    # 也就是这个功能本要消灭的「编数据」。互斥在这里是正确性，不是礼貌。
    if tasks.is_running(_design_key(project_id)):
        return "design_extract"
    # 聚类同理：它跑着的时候若允许删文献，_run_cluster 落盘的簇会引用一篇
    # 已不存在的文献；若允许生成大纲，大纲会拿到一份还没确认的聚类。
    if tasks.is_running(_clusters_key(project_id)):
        return "clusters"
    # 引用调度：它写的是这个项目的 citation_binding_json 与 status，中间的等待长达
    # 120 秒（一次相关性判断，超时即退回兜底），而那段等待此前全后端无一人知道 ——
    # 期间点火正文生成、重生成大纲、删文献、改选题全部照常放行，等它收尾时用旧快照
    # 把别人刚写下的状态顶回去。登记在这里，上面十来个入口**一行都不用改**就都看得见。
    if tasks.is_running(_citations_key(project_id)):
        return "citations"
    return None


def _write_task(
    project_id: str,
    kind: str,
    message: str,
    *,
    status: str = "running",
    done: int | None = None,
    total: int | None = None,
    current: str = "",
    error: str | None = None,
    extra: dict | None = None,
) -> None:
    """写通用任务进度。

    started_at 只在任务开始那一次写：它是「已等待 N 秒」的锚点，后续每次刷新进度
    都必须沿用同一个时刻，否则计时会一次次归零。前端刷新页面后也靠它续上。
    """
    prev = db.get_project(project_id) or {}
    old = prev.get("task_state_json") or {}
    started_at = old.get("started_at") if old.get("status") == "running" else None
    if started_at is None:
        started_at = datetime.now(timezone.utc).isoformat()

    state = {
        "kind": kind,
        "status": status,
        "message": message,
        "done": done,
        "total": total,
        "current": current,
        "started_at": started_at,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "error": error,
    }
    if extra:
        state.update(extra)
    db.update_project(project_id, task_state_json=state)


# 正文已经生成过的两个状态。它们对「引用设置 / 大纲能不能改」的答案与其它状态不同
# （能改，但只改设置、status 不回退），所以单独有个名字，避免在多处重复写这个元组。
_DONE_STATUSES = (db.ProjectStatus.COMPLETED, db.ProjectStatus.EXPORTED)


def _status_after_change(p: dict, target: str) -> str:
    """改「上游设置」类的入口写 status 时统一取这个值（引用三入口 + 大纲确认共四处）。

    已完成/已导出的项目**只改设置、不改状态**：把 status 退回 citation_pending /
    citation_confirmed / outline_confirmed 会把一份已经生成好的稿子打回上游 —— 刷新
    页面后服务端前沿跟着退，用户在步骤条上再也点不回生成与导出（正文明明还在库里，
    看起来却像没生成过）。状态只回答「走到哪了」；「正文与当前设置不一致」是另一件
    事，由 citations_stale 明说，不借状态表达。
    """
    return p["status"] if p.get("status") in _DONE_STATUSES else target


def _lang_of(p: dict) -> str:
    """读一个项目的写作语言，**读侧只此一处**。

    库里可能存着 NULL（迁移前的行）或一段脏字符串，所以一律过
    `writing_lang.normalize`（未知值收敛到中文，永不抛）。散着写
    `p.get("writing_lang") or "zh"` 会让「收敛规则」有第二份实现，而两份实现
    迟早对不上 —— 一处当成英文、另一处当成中文，产出的东西就半中半英。
    """
    return writing_lang.normalize(p.get("writing_lang"))


def _display_title(p: dict) -> str:
    """项目的显示名，同时也是论文标题：用户起的名字优先，其次是他填的研究核心方向。

    为什么由服务端算一个字段、而不是让前端各自兜底：这个值有 5 个消费点（项目列表、
    页头、删除确认、导出 md 的一级标题、下载文件名），各写一遍 `|| '未命名论文'`
    就是五份迟早漂移的实现 —— 本项目已有实据：那 5 处里两处兜底词是「论文」、
    三处是「未命名论文」。推导只此一份，前端只渲染。

    最后把空白折叠成单空格，这不是美化：「确定研究核心方向」是个 3 行 textarea，
    粘一段带换行的文字进来，那个换行会直接落进 `# 标题`（把 markdown 的一级标题
    截成两行）和下载文件名。折叠发生在**显示边界**，库里的 title / topic 一个字不动。
    """
    raw = (p.get("title") or "").strip() or (p.get("topic") or "").strip()
    return " ".join(raw.split()) or "未命名论文"


def _with_display_title(p: dict) -> dict:
    """给一个项目 dict 挂上 display_title 后返回。

    凡是**会流进前端 `project` state** 的返回值都要走这里 —— 前端的 `loadProject()`
    是把服务端给的这一份直接 setProject 的。两个 GET 之外还有三个写入接口：POST
    /projects（新建完立刻要显示名字）、POST /topic（确认选题后页头与卡片要变成新
    选题）、POST /{id}/title（改名的回执）。只在 GET 上挂是不够的 —— 姊妹字段
    citations_stale 就漏了这几个写入接口，但它缺了只是少一条提示，display_title
    缺了是整个名字没了、退回到兜底词。"""
    return {**p, "display_title": _display_title(p)}


# ---------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------
def _sum_budget(outline: dict) -> int:
    total = 0
    for ch in outline.get("chapters", []):
        for sec in ch.get("sections", []):
            if sec.get("subsections"):
                total += sum(int(s.get("word_budget", 0)) for s in sec["subsections"])
            else:
                total += int(sec.get("word_budget", 0))
    return total


def _collect_ordered_sections(outline: dict) -> list[dict]:
    sections = []
    for ch in outline.get("chapters", []):
        for sec in ch.get("sections", []):
            if sec.get("subsections"):
                for sub in sec["subsections"]:
                    sections.append({"title": sub.get("title", ""), "word_budget": sub.get("word_budget", 0)})
            else:
                sections.append({"title": sec.get("title", ""), "word_budget": sec.get("word_budget", 0)})
    return sections


def _words_of(sections: list[dict]) -> int:
    return sum(int(s.get("actual_words") or 0) for s in sections)


def _eta(started: float, produced: int, remaining: int) -> int | None:
    """按「本轮已完成章节的平均耗时」外推剩余秒数。

    只统计本轮产出的章节，续写时才不会继承一段不属于自己的历史耗时。
    """
    if produced <= 0 or remaining <= 0:
        return None
    return max(0, int((time.monotonic() - started) / produced * remaining))


def _find_page_snippet(doc: dict, page: int | None) -> str:
    """根据页码从文献索引中查找对应片段。"""
    if not page:
        return ""
    for pg in doc.get("pages", []):
        if pg.get("page") == page:
            return pg.get("snippet", "")
    return ""
