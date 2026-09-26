"""项目与工作流 API 路由。"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from app import db, material_parser, paper_types, tasks, writing_lang
from app import metadata
from app.writing_lang import DEFAULT_LANG
from app.agents import (
    cluster_agent,
    design_agent,
    generate_agent,
    material_agent,
    outline_agent,
    relevance_agent,
    schedule_agent,
    topic_agent,
)
from app.agents.generate_agent import count_units
from app.agents.parse_agent import extract_metadata, merge_hard_wraps
from app.agents.polish_agent import polish
from app.citation_format import (
    DEFAULT_FORMAT,
    SOURCE_TYPES,
    SUPPORTED_CITE_STYLES,
    SUPPORTED_FORMATS,
    citation_formats_for,
    cite_style_names,
    effective_format,
    format_names,
    format_reference,
    reference_runs,
)
from app.llm import llm
from app.pdf_parser import extract_pdf_text

router = APIRouter(prefix="/api", tags=["projects"])


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
# 项目
# ---------------------------------------------------------------
@router.post("/projects")
def create_project(body: ProjectCreate):
    project_id = uuid.uuid4().hex[:12]
    return _with_display_title(db.create_project(project_id, body.title))


@router.get("/projects")
def list_projects():
    # display_title 是本接口**算出来的**非列字段（同 get_project 的 citations_stale /
    # unbound_documents）。列表这里只算这一个：详细页那两条都要查文献（正文与编号是否
    # 一致、文献库与绑定是不是同一批），为一张侧栏卡片白查一遍不划算。侧栏也确实不
    # 读它们 —— 那两条提示都只出现在项目页内。
    return [_with_display_title(p) for p in db.list_projects()]


@router.get("/projects/{project_id}")
def get_project(project_id: str):
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    # 附上两个**算出来的**字段（不是库里的列）：正文是否还按当初那套引用编号
    # （citations_stale），以及文献库里还有几篇没被绑定覆盖（unbound_documents）。
    # 引用设置不再因为「正文已生成」被拦（用户 2026-09-18 定的策略），所以「改完
    # 到底生效没有」必须有地方明说 —— 界面据它在每个步骤都提示，刷新页面也不丢。
    # 无正文 / 无大纲的项目由两个函数各自短路，免得为一份草稿白查一遍文献。
    # （_citation_facts 定义在本文件后半段：函数体引用全局名，调用时才解析。）
    p.update(_citation_facts(p))
    # body_outline_mismatch 独立于引用三件套：正文 vs 大纲标题是否分家。终态项目改过
    # 标题、正文没重写时如实报出来（见 _body_outline_mismatch 的 docstring）。
    p["body_outline_mismatch"] = _body_outline_mismatch(p)
    # referenceRuns 是给 docx 排斜体用的片段形态（见 _with_reference_runs）：
    # 与 display_title 一样是**算出来的**非列字段，走同一条包装。
    return _with_reference_runs(_with_display_title(p))


@router.post("/projects/{project_id}/title")
def set_project_title(project_id: str, body: ProjectTitle):
    """给项目改个名（标题 = 论文标题，列表与导出文件名都用它）。

    刻意**不做忙闲检查**，这与 delete_project / set_topic 那两个守卫不是一回事：
    改名不写 status、不作废任何产物（不碰大纲、正文、引用快照），生成进行中也无害。
    照抄一个 409 上去，只会让用户在一个纯改名的动作上看到「正文正在生成」。

    空标题入库时归一成 NULL（而不是空串）：库里的「没有名字」只留一种表示，与
    create_project 的默认值一致，`_migrate_project_titles` 的两条判据也才对得上。
    """
    if db.get_project(project_id) is None:
        raise HTTPException(404, "项目不存在")
    title = body.title.strip()
    if len(title) > MAX_TITLE_CHARS:
        raise HTTPException(400, f"标题最长 {MAX_TITLE_CHARS} 字")
    return _with_display_title(db.update_project(project_id, title=title or None))


@router.delete("/projects/{project_id}")
def delete_project(project_id: str):
    """删除项目（外键级联删掉它的文献与研究材料）。

    有任务在跑就拒绝：后台任务还在往这个项目逐节写进度与正文，删掉只会让它们
    写进一个不存在的行（是静默 no-op），用户却以为任务仍在正常推进。
    """
    if db.get_project(project_id) is None:
        raise HTTPException(404, "项目不存在")
    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])
    if _gen_running(project_id):
        raise HTTPException(409, _GEN_BUSY_MESSAGE)
    db.delete_project(project_id)
    return {"ok": True}


# ---------------------------------------------------------------
# 阶段一：选题
# ---------------------------------------------------------------
@router.post("/projects/{project_id}/topics/recommend")
async def recommend_topics(project_id: str, body: TopicRecommend):
    p = db.get_project(project_id)
    # 候选选题的标题会成为论文标题，所以语言必须按**用户此刻在表单里选的那份**给：
    # 项目还没确认过时，库里的 writing_lang 还是建项目时的默认值（中文），拿它去给
    # 英文论文出选题就是中文选题 —— 而这一页上看不出任何异常。
    # 请求里没带（空串）才退回库里那份；项目不存在时同样退回默认语言，不因此 404 ——
    # 这个接口只做推荐、不写库，旧行为对不存在的项目也是照常推荐。
    lang = writing_lang.normalize(body.writing_lang) if body.writing_lang else _lang_of(p or {})
    topics = await topic_agent.recommend_topics(
        body.domain, body.paper_type, body.target_words, body.count,
        writing_lang=lang,
    )
    return {"topics": topics}


@router.post("/projects/{project_id}/topic")
def set_topic(project_id: str, body: TopicSet):
    if not paper_types.is_valid(body.paper_type):
        # 此前不校验，拼错的类型会静默退化成课程论文大纲
        raise HTTPException(400, f"不支持的论文类型：{body.paper_type}")
    p = db.get_project(project_id)
    if p is None:
        # 此前不检查：ID 不存在时 UPDATE 影响 0 行，接口返回 null，调用方却以为成功了
        raise HTTPException(404, "项目不存在")

    # 语言先归一化：这个字段是**产出物的语言**（见 app/writing_lang 的模块说明），
    # 库里存的是它的规范形，未知值一律收敛到中文 —— 脏值不该让整篇论文的语言悬空。
    # **空串是哨兵**：请求里没带这个字段时沿用库里的那份（= 不改语言），而不是落成
    # 中文 —— 后者会把英文项目掰回中文，并连带作废它的全部产物（见 TopicSet 那段）。
    # 归一化只在"真的发了"时才做，两种情形读的是同一个 `_lang_of` / `normalize`。
    # 放在最前面：下面两处错误文案都要用到它的字数单位。
    lang = writing_lang.normalize(body.writing_lang) if body.writing_lang else _lang_of(p)

    if body.target_words < MIN_TARGET_WORDS:
        # 与标题、写作思路的上限同类：请求体自己就不合法，不必先看库里有什么。
        # **只拦这一个入口**（写库的那一个）。选题推荐（recommend_topics）也收这个
        # 字段，但它不落库、只经提示词影响那份推荐的质量；那条路上由**前端**在按钮
        # 上拦（「输入先校验再消费」，而且只有它能在按钮旁边说清为什么），后端不重复
        # 拦 —— 这个接口的契约是「把推荐给我」，不是「校验我的设定」。
        # 单位跟着语言：英文项目说「不能少于 1000 字」是错的，它数的是 words。
        unit = writing_lang.words_unit(lang)
        raise HTTPException(
            400,
            f"目标总字数不能少于 {MIN_TARGET_WORDS} {unit}，当前 {body.target_words} {unit}",
        )

    # 写作思路先归一化一次，下面三处共用同一个值：长度校验、changed 判定、写库。
    # **必须归一化**：这是个多行输入框，用户敲完一段顺手回车，末尾就带一个 "\n" ——
    # 那是常态而不是边界；裸比的话，一次「只是换行」的点击就会把大纲、引用绑定、
    # 正文、参考文献快照与生成进度整份覆盖式作废掉。
    # 只 strip、不折叠内部空白（别照抄 _display_title 的 " ".join(raw.split()) ——
    # 那是给一行标题用的，会把这段论证的段落结构压平成一整段）。
    ideas = (body.writing_ideas or "").strip()
    if len(ideas) > MAX_IDEAS_CHARS:
        # 路由内 400，不用 pydantic max_length：后者抛 422，前端 api.js 的
        # new Error(err.detail) 会把 detail 数组渲染成 [object Object]。
        raise HTTPException(
            400, f"写作思路最长 {MAX_IDEAS_CHARS} 字，当前 {len(ideas)} 字"
        )
    # 有任务在跑时不许改选题 —— 不是礼貌，是「改了也白改」：在飞的任务结束时会把
    # 产物写回库，盖掉这里刚做的作废。分段生成尤其致命：它在结尾用内存里的 results
    # 重写 sections_json，会把刚作废的正文原样复活，而那份正文的引用编号是按**旧**
    # 选题算的。
    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(
            409, f"{_BUSY_MESSAGE.get(busy, busy)}，请等它结束后再修改选题与设定"
        )
    if project_id in _GEN_TASKS:
        raise HTTPException(409, "正文正在生成中，请等它结束后再修改选题与设定")

    # 选题/类型/字数/写作思路都是大纲的上游。**真的改了才作废下游**：用户点
    # 「确认选题」很可能只是路过（或改了个错别字又改回来），不加这个判断就会平白
    # 作废一份好大纲与正文。
    # 写作思路与前三者**同一口径**：选题只是标题，装不下「打算怎么论证」；思路变了，
    # 库里那份正文就是按旧思路写的，留着就是埋雷（与「按旧选题算引用编号」同类）。
    changed = (
        (p.get("topic") or "") != body.topic
        or (p.get("paper_type") or "") != body.paper_type
        or int(p.get("target_words") or 0) != int(body.target_words or 0)
        or (p.get("writing_ideas") or "") != ideas
        # 语言与前三者同一口径：换语言等于换掉全篇的产出物语言，库里那份按旧语言写的
        # 大纲与正文留着就是错的（界面上章节标题一个都没变，看不出异常）。
        or _lang_of(p) != lang
    )
    fields = {
        "paper_type": body.paper_type,
        "target_words": body.target_words,
        "topic": body.topic,
        # 空串落成 NULL：库里的「没写过」只留一种表示（同 set_project_title 的
        # `title or None`）。于是"清空思路"与"从没写过"在后续判定里是同一回事。
        "writing_ideas": ideas or None,
        "writing_lang": lang,
    }
    # 语言与格式的级联：`en + gb7714` 是本轮明确要消灭的组合（英文写作不可能用
    # GB/T 7714），这里**就地改正而不是 400** —— 用户没做错事，是他上一次的选择
    # 在新语言下不再合法。判据取**结果语言**（而不是「语言变了没」），乱改过的库
    # 也会被顺手修正。反向 en→zh **不自动改回来**：APA/MLA 在中文写作下是允许的
    # （三项都在，只是配一句提示），擅自改掉等于替用户做了选择。
    stored_fmt = p.get("citation_format") or DEFAULT_FORMAT
    fmt = effective_format(lang, stored_fmt)
    if fmt != stored_fmt:
        fields["citation_format"] = fmt
    # 标题跟随选题：只在「这个名字还不是用户自己起的」时才跟。判据是 title 为空、
    # 或恰好还等于**旧**选题 —— 新建项目时 title 是空的，第一次提交选题后它就等于
    # 旧选题，于是第二次改选题仍然跟得上；而用户手动改过名之后就不再动它（他刚说
    # 过自己要叫什么，下一次改选题就把它顶掉，等于没给过这个功能）。改名接口把标题
    # 置空，就是「回到跟随」这条路上的回车键。
    # 放在 changed 判定之外：原样重提时它要么不写、要么写成同一个值，不会把已经
    # 走到下游的状态打回（那件事由下面的 changed 单独负责）。
    old_title = (p.get("title") or "").strip()
    if not old_title or old_title == (p.get("topic") or "").strip():
        fields["title"] = body.topic
    if changed:
        # status 只在真改了时才写。原样重提也写的话，一次「路过点击」会把已经走到
        # outline_confirmed / citation_confirmed 的项目打回 topic_set —— 刷新后
        # statusToStepKey 就把用户送回上一步，和 ⑥ 是同一类「状态倒退」。
        fields["status"] = db.ProjectStatus.TOPIC_SET
        # 覆盖式作废（用户已确认此策略）：大纲、引用绑定、材料计划、正文、参考文献
        # 快照、生成进度一并清空。宁可让用户重做一遍，也不允许带着「按旧选题写的正文
        # + 按新选题算的引用编号」往下走——那种错配在界面上看不出来（章节标题一个都
        # 没变），只会静默印进论文里。
        fields.update(
            outline_json=None,
            citation_binding_json=None,
            materials_plan_json=None,
            sections_json=None,
            references_json=None,
            generation_state_json=None,
        )
        # 设计表单只在**类型**变了时才作废：字段名随类型走（TYPE_CONFIG.design_fields），
        # 旧类型的字段留在库里不会再被界面渲染，却会在下次保存时被原样写回，
        # 变成一份看不见、也删不掉的残留。
        if (p.get("paper_type") or "") != body.paper_type:
            fields["design_json"] = None
        # 主题聚类**只在语言变了时才作废**。改选题/改类型时它刻意留着：它由文献本身
        # 决定，与选题无关，改选题不该逼用户重聚类；类型改成非文献类时它只是闲置，
        # 改回来还能接着用。但语言变了性质就不同 —— 聚类产出的主题名与概述会成为
        # 文献综述的**节标题与节内容**，是产物文字，留着一份中文聚类去写英文论文
        # 正是本轮要消灭的那类 bug。
        if _lang_of(p) != lang:
            fields["clusters_json"] = None
    db.update_project(project_id, **fields)
    return _with_display_title(db.get_project(project_id))


# ---------------------------------------------------------------
# 阶段二：大纲
# ---------------------------------------------------------------
@router.post("/projects/{project_id}/outline/generate")
async def generate_outline(project_id: str):
    """触发生成后立即返回；两遍 LLM 约 36 秒，实际工作交给后台任务。

    必须保持 async def —— def 路由跑在线程池里，没有运行中的事件循环，
    tasks.spawn 里的 asyncio.create_task 会直接抛 RuntimeError。
    """
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    if not p.get("topic"):
        raise HTTPException(400, "请先完成选题")

    # 「结构由文献决定」的类型（文献综述）：章节主题来自文献聚类，不是先验给定的，
    # 因此必须先注入文献。前端据此把「文献注入」排到「大纲」之前，此处是服务端兜底。
    paper_type = p.get("paper_type") or ""
    literature_context = ""
    doc_count = 0
    clusters: list[dict] = []
    theme_sections = 0
    if paper_types.is_literature_first(paper_type):
        docs = db.list_documents(project_id)
        if not docs:
            # 消息里带上类型短名，不写死「文献综述」：哪一类 literature-first 由
            # 类型表决定，以后多一类时这里不该变成假话。
            raise HTTPException(400, f"{paper_type}需先上传文献，再生成大纲")
        literature_context = outline_agent.build_literature_context(docs)
        doc_count = len(docs)
        # 作者确认过的主题聚类随大纲一起进提示词。未确认时为 []，_clusters_block
        # 返回空串、提示词逐字节退回旧版——所以「不确认聚类」不是错误状态，
        # 只是让模型自己归纳主题（前端会先弹确认框说明后果）。
        clusters = _confirmed_clusters(p)
        theme_sections = paper_types.theme_section_count(paper_type)

    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])
    # **反向也要查**：正文生成登记在 _GEN_TASKS 里，_busy_task 看不见它（此前只有
    # /documents 补了这一条，这里是漏掉的那处）。放行的后果不是「两件事并行」这么轻：
    # _run_outline 收尾会把 status 写成 outline_pending，把 generating 顶掉 —— 界面按
    # status 渲染的那一步当场跳回大纲，而生成任务还在往 sections_json 里写正文；同时
    # 新大纲的章节标题与那个生成任务正要用的引用绑定全部失配，生成时静默产出零引用章节。
    if _gen_running(project_id):
        raise HTTPException(409, _GEN_BUSY_MESSAGE)

    _write_task(project_id, "outline", "正在拟定章节结构与标题…")
    tasks.spawn(
        _outline_key(project_id),
        _run_outline(
            project_id, literature_context, doc_count, p,
            _design_context(p), paper_types.design_label_for(paper_type),
            clusters, theme_sections,
        ),
    )
    return {"status": "running"}


def _confirmed_clusters(p: dict) -> list[dict]:
    """取已确认的主题聚类；未生成、未确认、或形状不对时返回空列表。

    只认 confirmed_at 非空的产物：作者看过并点了确认，才谈得上「优先于你自己的
    归纳」。没确认就送进去，等于把一份用户从没看过的推断说成他的决定。
    """
    data = p.get("clusters_json") or {}
    if not isinstance(data, dict) or not data.get("confirmed_at"):
        return []
    clusters = data.get("clusters")
    return [c for c in clusters if isinstance(c, dict)] if isinstance(clusters, list) else []


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


async def _run_outline(
    project_id: str,
    literature_context: str,
    doc_count: int,
    p: dict,
    design_context: str = "",
    design_label: str = "",
    clusters: list[dict] | None = None,
    theme_sections: int = 0,
) -> None:
    """后台生成大纲：两遍 LLM，逐遍推进度。

    失败必须落库：桌面版 console=False，不写库就等于彻底看不见错误。
    """
    try:
        def on_phase(phase: str, info: dict) -> None:
            if phase == "structure":
                _write_task(project_id, "outline", "正在拟定章节结构与标题…")
            else:
                _write_task(
                    project_id,
                    "outline",
                    f"已定 {info.get('chapters', 0)} 章 {info.get('sections', 0)} 节，"
                    f"正在按内容体量分配字数并复核主线…",
                )

        outline = await outline_agent.generate_outline(
            p["topic"],
            p["paper_type"],
            p["target_words"],
            literature_context=literature_context,
            document_count=doc_count,
            on_phase=on_phase,
            design_context=design_context,
            design_label=design_label,
            clusters=clusters,
            theme_sections=theme_sections,
            # 作者写下的写作思路从**这个 p** 里读，不再往上加形参：_run_outline 本来就
            # 收到 p，而这个函数族是按位置传参的（generate_outline 的 docstring 里
            # 记着那次错位的教训），少加一个参数就少一次错位的机会。
            writing_ideas=p.get("writing_ideas") or "",
            # 与 writing_ideas 同理从 p 里读，不往上加形参：骨架取哪张表、类型说明取
            # 哪一份、字数单位、几处字符上限，全都由它决定。
            writing_lang=_lang_of(p),
        )
        db.update_project(
            project_id,
            outline_json=outline,
            status=db.ProjectStatus.OUTLINE_PENDING,
        )
        _write_task(project_id, "outline", "大纲已生成", status="done")
    except asyncio.CancelledError:
        # 必须先于 Exception 接住：CancelledError 继承 BaseException，
        # 漏了它项目就会永久停在「运行中」
        _write_task(
            project_id, "outline", "生成已中断", status="error",
            error="任务被中断（服务或窗口已关闭）",
        )
        raise
    except Exception as exc:  # noqa: BLE001
        _write_task(
            project_id, "outline", "大纲生成失败", status="error",
            error=f"{type(exc).__name__}: {exc}",
        )
        # 退回可重试的状态：选题仍在，用户改一下配置就能再来一次
        db.update_project(project_id, status=db.ProjectStatus.TOPIC_SET)


# 「确认大纲」允许在哪些状态下写入。它与确认点② 的 _CITATION_EDITABLE 是同一把尺子
# 的上游版本 —— 能在这里盖章的地方，就是还没往下走进正文的地方：
#   - outline_pending：正常入口（刚生成完大纲）
#   - outline_confirmed：允许反复确认（第一次调字数预算、第二次只改标题）
#   - resources_loading / citation_pending / citation_confirmed：回到第一步重看大纲
#     本身不该被拦，它只是把同一份大纲再盖一次章（改标题时下面那两段作废逻辑照常生效）
#   - completed / exported：正文已成之后同样允许（与 _CITATION_EDITABLE 同一个决定）
# generating 不在其中：那是「正文已经在写了」，从这里确认等于把一份正在成文的稿子
# 打回大纲阶段 —— 正是本组要消灭的状态倒退。
_OUTLINE_CONFIRMABLE = {
    db.ProjectStatus.OUTLINE_PENDING,
    db.ProjectStatus.OUTLINE_CONFIRMED,
    db.ProjectStatus.RESOURCES_LOADING,
    db.ProjectStatus.CITATION_PENDING,
    db.ProjectStatus.CITATION_CONFIRMED,
    db.ProjectStatus.COMPLETED,
    db.ProjectStatus.EXPORTED,
}


def _require_outline_confirmable(project_id: str) -> dict:
    """确认大纲（人工确认点①）的闸门，形状照 _require_citation_editable。

    此前这条路由**一句校验都没有**，于是有三条路径：

    1. 项目不存在时，下一行 `p["target_words"]` 直接 TypeError —— 500 而不是 404
       （同级入口全都先 404）。
    2. 本项目有任务在跑时照样放行。而 _run_outline 收尾是**无条件**写
       outline_json + status=OUTLINE_PENDING 的：用户在「生成大纲」转圈时点确认，
       几秒后收尾把 status 写回 outline_pending，连同他刚确认的那份大纲、以及那次
       确认顺带作废掉的绑定与材料计划，一起被换成没人看过的一版。
    3. 正文生成中（_GEN_TASKS）放行 —— 同一族里最重的一条：确认会把 status 从
       generating 写成 outline_confirmed，在飞的生成任务还在往 sections_json 里写
       正文，界面按 status 渲染的那一步当场跳回大纲；而新大纲的章节标题与生成任务
       正在用的那份引用绑定已经全部失配。

    闸门必须在后端：界面上的禁用只是提示（决策 20）。
    """
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])
    if _gen_running(project_id):
        raise HTTPException(409, _GEN_BUSY_MESSAGE)
    status = p.get("status")
    if status == db.ProjectStatus.GENERATING:
        # 真有一个生成任务在飞时上面那条已经拦下了；这里管的是残留的 generating
        # （服务重启等极端情况留下的行）—— 也不该从这一步往下盖章。
        raise HTTPException(409, _GEN_BUSY_MESSAGE)
    if status not in _OUTLINE_CONFIRMABLE:
        raise HTTPException(400, "请先生成大纲，再确认")
    return p


@router.post("/projects/{project_id}/outline/confirm")
def confirm_outline(project_id: str, body: OutlineConfirm):
    # 人工确认点①：用户调整后的大纲，校验字数总和
    p = _require_outline_confirmable(project_id)
    total = _sum_budget(body.outline)
    target = p["target_words"]

    # 字数不匹配要先判断、一个字都不写。此前是「先写库后判断」：total != target 时也
    # 照样 update_project（盖章大纲 + 推进状态 + 可能作废绑定），然后才在返回里说
    # matched=False —— 前端看到「字数总和不一致」就停在大纲步，库里却已经走下去，
    # 刷新后界面自己往前跳、用户以为没动过。这里把判断提到写库之前，不匹配直接返回。
    if total != target:
        return {
            "ok": False,
            "total_budget": total,
            "target_words": target,
            "matched": False,
            "binding_invalidated": False,
            "material_plan_invalidated": False,
        }

    # 状态走 _status_after_change 而不是无条件写 outline_confirmed：_OUTLINE_CONFIRMABLE
    # 有意放行 completed / exported（回看大纲不是回退）。已成稿的项目在这里把 status
    # 打回 outline_confirmed，会像引用那条路一样把一份明明还在库里的正文挡在导出外面
    # （「正文还没有生成完成」——与事实相反）。所以已完成的项目只改大纲与作废下游、
    # 状态停在原处；与引用三个入口共用同一条「终态不回退」的规则。
    fields: dict = {
        "outline_json": body.outline,
        "status": _status_after_change(p, db.ProjectStatus.OUTLINE_CONFIRMED),
    }
    # 引用绑定以「章节标题字符串」为 join key（见本文件生成环节的 binding.get）。
    # 大纲一旦改了标题，旧绑定会全部失配，生成时静默产出「零引用」的章节。
    # 这里主动失效，逼用户重新走一次引用调度。
    binding = p.get("citation_binding_json")
    new_titles = {s["title"] for s in _collect_ordered_sections(body.outline)}
    stale = False
    if binding:
        if set(binding.keys()) != new_titles:
            fields["citation_binding_json"] = None
            stale = True

    # 材料分析计划同样以章节标题为键，同样会因改标题而失配。失效后材料不会进入
    # 正文，却仍显示「已分析」——所以一并作废，让用户重跑一次分析（一次 LLM 调用）。
    plan = p.get("materials_plan_json")
    plan_stale = False
    if plan:
        placed_titles = {i.get("section_title") for i in plan.get("placements") or []}
        if not placed_titles <= new_titles:
            fields["materials_plan_json"] = None
            plan_stale = True
    updated = db.update_project(project_id, **fields)

    return {
        "ok": True,
        "total_budget": total,
        "target_words": target,
        "matched": total == target,
        "binding_invalidated": stale,
        "material_plan_invalidated": plan_stale,
        # 终态项目改标题、正文没重写时如实回报「分家」；前端据它出常驻提示条。
        "body_outline_mismatch": _body_outline_mismatch(updated),
    }


# ---------------------------------------------------------------
# 阶段三：文献上传与解析
# ---------------------------------------------------------------
@router.post("/projects/{project_id}/documents")
async def upload_documents(project_id: str, files: list[UploadFile] = File(...)):
    """接管上传后立即返回；逐篇解析跑在后台任务里。

    N 篇 PDF = N 次串行 LLM 调用，可能好几分钟，远超任何读超时。此前这一步同时
    有两个毛病：界面全程静止；一旦超时或断连，**已经解析好的文献全部丢失**。
    改为后台任务后，每篇解析完立刻入库，中途断掉也不白干。
    """
    # 存在性放在最前（此前这一条路由**根本没查过项目在不在**）：项目不存在时，
    # 上传会先返回 200 + running，然后整批在 db.add_document 那一步撞上
    # documents.project_id 的外键约束（get_conn 开了 PRAGMA foreign_keys）——
    # 那颗 IntegrityError 不在逐篇 try 里，会冲出 for 循环、被 _run_parse 最外层
    # 接住写成 error；而那份 error 也写不进一个不存在的项目（update_project 影响
    # 0 行）。用户手里只剩一条永远不动的进度。同级入口全部先 404，这里补上。
    if db.get_project(project_id) is None:
        raise HTTPException(404, "项目不存在")

    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])
    # **反向也要查**：正文生成中不允许再传文献。上面那个 _busy_task 只查通用任务登记表，
    # 看不见 _GEN_TASKS；而 _run_parse 落库后会写 status=RESOURCES_LOADING（新文献让
    # 旧的聚类失效），这一笔会把 generating 顶掉，可生成任务还在往 sections_json 里写
    # 正文 —— 界面按 status 渲染的那一步当场跳走，用户在「文献注入」步看着一篇正在
    # 成文的稿子。两套忙闲登记都在同一个项目上，任一侧遗漏就是这类错配。
    # （_GEN_TASKS 定义在本文件后半段：函数体引用全局名，调用时才解析，无碍。）
    if _gen_running(project_id):
        raise HTTPException(409, _GEN_BUSY_MESSAGE)

    # UploadFile 在响应结束（即本函数返回）后会被框架关闭，后台任务不能再碰它，
    # 所以先把字节全部读进内存。代价是解析期间这些 PDF 的字节驻留内存 ——
    # 桌面版单机场景可以接受（一次最多几十篇、每篇几 MB）。这一趟也顺便让上面那道
    # 检查「先便宜地fail 一次」，不必读完几十 MB 才说忙。
    payload: list[tuple[str, bytes]] = []
    for f in files:
        payload.append((f.filename or "", await f.read()))

    # **再查一遍，这一遍才是权威的**。上面那次检查与下面的登记之间隔着一串
    # `await f.read()`，事件循环完全可以在那期间让第二个上传请求也通过第一道检查：
    # 两个请求都以为自己是唯一的那一个，后到的覆盖掉 _TASKS 里的登记 —— 先起的那个
    # 从此没人能查、没人能取消，也不再被 _busy_task 看见；两个 _run_parse 同时读同一批
    # PDF、同时调模型、各自往同一个项目里 add_document，于是每篇文献入库两次、解析耗时
    # 翻倍（LLM 调用也翻倍），task_state_json 的进度在两条文案之间来回跳。
    # 内容指纹（_run_parse 里的重复判定）**挡不住这一条**：它的查库与入库之间隔着一次
    # `await extract_metadata`，两条协程完全都能在对方落库之前通过判定 —— 那是一段
    # check-then-act，需要的是这道忙闲互斥，不是判重。别把这段检查当成刀枪不入。
    # 这里「查忙闲 → 写任务状态 → 登记」三步之间**一个 await 都没有**，事件循环不会
    # 在中间切走，所以这一次是原子的。别把 _write_task 挪到上面去，那会重新拆开它。
    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])
    if _gen_running(project_id):
        raise HTTPException(409, _GEN_BUSY_MESSAGE)

    _write_task(
        project_id, "documents", f"正在解析文献 0/{len(payload)}…",
        done=0, total=len(payload),
    )
    tasks.spawn(_documents_key(project_id), _run_parse(project_id, payload))
    return {"status": "running", "count": len(payload)}


async def _run_parse(project_id: str, payload: list[tuple[str, bytes]]) -> None:
    """后台逐篇解析文献，每篇解析完立刻入库。

    单篇失败不中断整批：追加进 failed 列表（保持原有语义），最终状态里带上。
    内容重复的（同一篇传了两遍）不落库，计入 skipped —— 与 failed 分开报：
    「没发进来」和「本来就在库里」是两件不同的事，合成一句用户会以为缺文献。
    **第三种是 partial**：文件进来了，但其中几页没提取到文本（见 pdf_parser）。
    它既不是「没进来」也不是「重复」，同样得分着报 —— 页码索引在那几页上是空的，
    用户据以写正文的清单缺了一块，而「解析成功」这四个字会把它盖住。
    """
    total = len(payload)
    failed: list[dict] = []
    skipped: list[dict] = []
    partial: list[dict] = []
    # 本批内部已见过的指纹 → 展示名。同一个文件被多选两次时两篇都还没入库，
    # 只查库查不出来，得靠这份登记。
    seen: dict[str, str] = {}
    saved = 0
    try:
        for i, (filename, data) in enumerate(payload, 1):
            _write_task(
                project_id, "documents",
                f"正在解析文献 {i}/{total}：{filename or '(未命名)'}",
                done=i - 1, total=total, current=filename,
            )
            if not filename or not filename.lower().endswith(".pdf"):
                failed.append({"filename": filename, "reason": "非 PDF 文件"})
                continue
            try:
                pages = extract_pdf_text(data)
            except Exception as e:  # noqa: BLE001
                failed.append({"filename": filename, "reason": f"解析失败: {e}"})
                continue
            blank = [p["page"] for p in pages if not p["text"]]
            if len(blank) == len(pages):
                # 每一页都没文本。两种成因都落到这里，但**原因不同不能合成一句**：
                # 扫描版是文件本身没有文本层，逐页提取失败是解析器的问题。用同一句
                # 「可能为扫描版」盖住后者，用户会去重新找一份 PDF，而问题不在文件。
                reason = (
                    "每一页的文本提取都失败"
                    if any(p["failed"] for p in pages)
                    else "未提取到文本（可能为扫描版）"
                )
                failed.append({"filename": filename, "reason": reason})
                continue
            # 去重判在**调模型取元数据之前**：指纹只由刚抽出的正文决定，与元数据无关，
            # 于是重复的那一篇不必花掉一次 LLM 调用（一篇几秒，20 篇里一半是重复的话
            # 这里是几分钟）。指纹打的形态与落库形态一致，判据只有 db.document_fingerprint
            # 一处 —— 那份 pages 也正是下面要写进 doc 的那一份，不另建一遍。
            stored_pages = [
                {"page": p["page"], "snippet": p["text"][:300]} for p in pages
            ]
            fingerprint = db.document_fingerprint(stored_pages)
            duplicate = seen.get(fingerprint)
            if duplicate is None:
                duplicate = db.existing_document_title(project_id, fingerprint)
            if duplicate is not None:
                skipped.append({
                    "filename": filename,
                    "reason": f"与《{duplicate}》内容相同",
                })
                continue
            # 抽取用的正文：**前两页**合并硬换行后拼接。取两页而不是一页，是因为英文
            # 期刊 PDF 的首页被大标题、长摘要、版权/投稿信息占满，作者行与刊名行常被挤
            # 到第二页；合并硬换行是因为 pypdf 一行一个断句，作者列表跨行时模型只能自己
            # 猜怎么接（见 parse_agent.merge_hard_wraps）。
            #
            # 上限在 extract_metadata 里（MAX_EXTRACT_CHARS）。**这里不截断**：截断点
            # 只有一个，分散在两处的话以后调上限会漏掉一处。
            #
            # 只动抽取这一个用途：落库的 stored_pages（上面那份）与内容指纹都取原文，
            # 所以已入库文献的判重行为不受影响。
            first_text = merge_hard_wraps(
                "\n".join(p["text"] for p in pages[:2])
            )
            # 逐篇 try 与上面 extract_pdf_text 同型：元数据提取是 LLM 调用，是会失败的
            # 一步，而它原来**没有**自己的 try，异常会冲出 for 循环把整批中止 —— 后面
            # 那些还没解析的文献全部作废。原来这颗雷被 parse_agent 里一个吞掉一切异常
            # 的裸 except 盖住了；那颗 except 同时还在把「提取失败」伪装成「提取成功
            # （标题=文件名、其余全空）」，所以两边必须同时改：先在这里承接失败，再去
            # 掉那颗 except，才不至于把「吞掉」换成「炸掉整批」。
            try:
                meta = await extract_metadata(first_text, filename)
            except Exception as e:  # noqa: BLE001
                failed.append({"filename": filename, "reason": f"元数据提取失败: {e}"})
                continue
            # 落库前再清洗一遍作者/年份/来源，以及卷期页/类型标识/题名英译。这里是**所有**文献
            # 进库的必经之处，而提取器是可替换、可打桩的（测试就打桩它），边界不该替它
            # 担保输出干净。与 parse_agent 调的是同一个 clean_meta_value，重复清洗是幂等的。
            #
            # 这里**刻意不使用** clean_source_type 的越界回落：那个函数属于渲染侧，
            # 是把「模型给了个怪字母」收敛成 J。库里原样存下模型说的，降级留给渲染，
            # 这样用户打开编辑界面能看见模型到底写了什么，而不是看见一个被改写过的 J。
            meta = metadata.clean_reference_fields(metadata.clean_meta_fields(dict(meta)))
            doc = {
                "id": uuid.uuid4().hex[:10],
                "filename": filename,
                "title": meta.get("title", filename),
                "authors": meta.get("authors", ""),
                "year": meta.get("year", ""),
                "source": meta.get("source", ""),
                # 文末著录的几项（期刊四项 + 题名英译 + 非期刊三项）：缺键也补 ""
                # （clean_reference_fields 已保证键存在，这里的 get 兜底是为了
                # 「提取器被打桩成不返回这些键」的测试场景）。
                "volume": meta.get("volume", ""),
                "issue": meta.get("issue", ""),
                "page_range": meta.get("page_range", ""),
                "source_type": meta.get("source_type", ""),
                "title_en": meta.get("title_en", ""),
                "place": meta.get("place", ""),
                "edition": meta.get("edition", ""),
                "publish_date": meta.get("publish_date", ""),
                "summary": meta.get("summary", ""),
                "pages": stored_pages,
            }
            db.add_document(project_id, doc)
            seen[fingerprint] = doc["title"] or filename
            saved += 1
            # 页面级的部分失败：文件收下了，但有几页没提取到文本。点在页码上，
            # 因为「是哪几页」正是用户要去原文里补的那几处；计数由任务文案负责。
            missed = [p["page"] for p in pages if p["failed"]]
            if missed:
                partial.append({
                    "filename": filename,
                    "reason": "第 " + "、".join(str(n) for n in missed) + " 页文本提取失败",
                })

        # 新文献进来说明文献集合变了，之前的主题聚类不再覆盖全部文献；
        # 与材料计划失效同理，宁可让用户重跑一次聚类，也不要留一份看着完整、
        # 实则漏掉新文献的旧聚类。
        if saved:
            db.update_project(
                project_id,
                status=db.ProjectStatus.RESOURCES_LOADING,
                clusters_json=None,
            )
        # saved == 0：一篇都没入库（全重复/全失败）。文献集合没变，库里的聚类、正文
        # 都还覆盖得住，什么都不能写 —— 尤其不能写 status=RESOURCES_LOADING：那会把一个
        # completed/exported 的成稿项目推回「文献注入」，导出随即被「正文还没有生成完成」
        # 这句与事实相反的假理由挡住（D3）。任务仍照常落 done，saved=0 由 extra 报给前端。
        # 只报计数，不在这里复述是哪几篇：任务文案一行放不下，点名在 extra.skipped 里，
        # 由前端拼进提示条（那边的「跳过重复」与这里的「跳过 N 篇重复」各说一半，不重复）。
        tails = []
        if skipped:
            tails.append(f"跳过 {len(skipped)} 篇重复")
        if failed:
            tails.append(f"{len(failed)} 篇未解析成功")
        if partial:
            tails.append(f"{len(partial)} 篇有页面未提取到")
        tail = ("，" + "，".join(tails)) if tails else ""
        _write_task(
            project_id, "documents",
            f"已解析 {saved}/{total} 篇文献{tail}",
            status="done", done=total, total=total,
            extra={"saved": saved, "failed": failed, "skipped": skipped,
                   "partial": partial},
        )
    except asyncio.CancelledError:
        _write_task(
            project_id, "documents", "解析已中断", status="error",
            done=saved, total=total,
            error="任务被中断（服务或窗口已关闭），已解析的文献已保留",
        )
        raise
    except Exception as exc:  # noqa: BLE001
        _write_task(
            project_id, "documents", "文献解析失败", status="error",
            done=saved, total=total,
            error=f"{type(exc).__name__}: {exc}",
        )


@router.get("/projects/{project_id}/task/progress")
def task_progress(project_id: str):
    """通用任务进度查询（大纲生成 / 文献解析）。

    /generate/progress 保持独立：它在 done 时还要带上 sections/references，
    形状不同，合并只会把两件事搅在一起。
    """
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    return {"task": p.get("task_state_json") or {"status": "idle"}}


@router.get("/projects/{project_id}/documents")
def list_documents(project_id: str):
    return db.list_documents(project_id)


def _binding_entries(binding: dict | None, section_title: str) -> list[dict]:
    """某一章的绑定条目；读不懂的形状一律跳过。**读这份绑定的唯一入口。**

    **形状必须是全的，不能只防 None**：`CitationConfirm.binding` 只由 pydantic 保证
    最外层是 dict，内层结构自由 —— `{"绪论": [null]}`、`{"绪论": [1]}`、`{"绪论": 5}`
    都进得来。而绑定是会被**落库**的（confirm_citations 先存后算派生事实），所以一处
    疏忽的代价不是一次 500，而是这个项目**此后每次 GET 详情都 500**。

    此前读它的地方有三处、各写一遍判据，其中两处直接在条目上 .get：
      · `_binding_doc_ids` 修过（那是「删文献」那条路暴露出来的）；
      · `_plan_citations` 与生成循环里那份 refs 没修 —— 同一个疏漏只修了一半，
        而没修的那一处正好在读取路径上。
    现在收条目的判据只留这一份：**非空 doc_id 才收**，三处必然给出同一个答案。

    跳过读不懂的条目是安全的退化方向：算出来「没绑上」只会让界面请用户重排一次，
    而重排本来就会用一份合法绑定覆盖掉它。
    """
    entries = (binding or {}).get(section_title)
    if not isinstance(entries, list):
        return []
    return [r for r in entries if isinstance(r, dict) and r.get("doc_id")]


def _binding_doc_ids(binding: dict | None) -> set[str]:
    """绑定里出现过的全部 doc_id。读不懂的条目一律跳过（判据见 _binding_entries）。

    必须是**集合**，两个理由都真实存在：
      · 同一篇文献可以出现在**多个章节**里 —— 文献数少于章节数时，调度算法就是轮询着
        把每篇铺到多个章节的（schedule_agent.uniform_distribute 的 else 分支）；
      · 绑定是请求体给的，同一篇写几遍也没人拦。
    用列表求差会把这两种情况数成「多篇未绑定」。
    """
    ids: set[str] = set()
    for title in (binding or {}):
        ids.update(r["doc_id"] for r in _binding_entries(binding, title))
    return ids


def _binding_uses(binding: dict | None, doc_id: str) -> bool:
    """绑定关系里是否引用了这篇文献（绑定以章节标题为键，值是文献条目列表）。"""
    return doc_id in _binding_doc_ids(binding)


def _clusters_use(clusters_json: dict | None, doc_id: str) -> bool:
    """主题聚类里是否引用了这篇文献。

    与 _binding_uses 同一条道理，但键不同：聚类按 doc_id 归类（不按章节标题），
    所以这里直接看簇的 doc_ids。删掉被归入某簇的文献后，那一簇就少一篇却在界面上
    毫无变化 —— 用户以为自己确认过的聚类还在，其实它已经不完整了。
    """
    for c in (clusters_json or {}).get("clusters") or []:
        if isinstance(c, dict) and doc_id in (c.get("doc_ids") or []):
            return True
    return False


@router.delete("/projects/{project_id}/documents/{doc_id}")
def delete_document(project_id: str, doc_id: str):
    """删除一篇文献。

    引用绑定以章节标题为键、doc_id 为值。若删掉的文献已被绑定引用，绑定里就会留下
    悬空 doc_id：生成时 `doc_map.get(doc_id, {})` 取不到页码与原文片段，模型拿不到可
    转述的材料，只能凭空编 —— 恰好击穿「0 幻觉引用」这条底线。所以沿用
    /outline/confirm 的先例：一旦被引用的文献被删，就把绑定整体作废并退回待调度，
    让用户重跑一次引用调度（纯确定性 Python，不到 1 秒）。删的是尚未被引用的文献
    （例如刚上传就发现传错了）则绑定原样保留。

    **路径里那个项目 id 不是装饰，它是一道守卫**（原先的路由是平铺的
    `/documents/{doc_id}`，项目 id 只能从文献行里反推）。前端那份文献列表可能是
    **上一个项目**的：`loadProject` 里这次拉取有条件，换项目时列表不清就是上一个
    项目的文献，而每条右边的「删除」只拿得到 doc id —— 反推出来的项目是**那篇文献
    真正的项目**，于是「在 Y 的界面上点删除」会真删掉 X 的文献、并把 X 的引用绑定
    整体作废（见 v1.19 修订记录：前端那一半已修，这里是后端那一半）。
    路径与文献行不一致时按「文献不存在」返回 404，**不区分**「没这篇」与「不属于
    这个项目」—— 否则这个接口就成了「拿 id 探测别的项目里有什么」的工具。
    """
    doc = db.get_document(doc_id)
    if doc is None or (doc.get("project_id") or "") != project_id:
        raise HTTPException(404, "文献不存在")

    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])
    if _gen_running(project_id):
        raise HTTPException(409, f"{_GEN_BUSY_MESSAGE}，无法删除文献")

    db.delete_document(doc_id)

    p = db.get_project(project_id) or {}
    invalidated = False
    if _binding_uses(p.get("citation_binding_json"), doc_id):
        db.update_project(
            project_id,
            citation_binding_json=None,
            status=db.ProjectStatus.RESOURCES_LOADING,
        )
        invalidated = True
    # 聚类与绑定各失效各的：删掉一篇未被引用、但被归入某簇的文献时，绑定该留就留，
    # 聚类该作废就作废。作废后 confirmed_at 一并清掉，于是大纲那边不再拿到它。
    clusters_invalidated = False
    if _clusters_use(p.get("clusters_json"), doc_id):
        db.update_project(project_id, clusters_json=None)
        clusters_invalidated = True
    return {
        "ok": True,
        "binding_invalidated": invalidated,
        "clusters_invalidated": clusters_invalidated,
    }


def _validated_doc_patch(body: DocumentPatch) -> dict:
    """把请求体收敛成「这次要写的列」，越界抛 400。

    返回的 dict **只含请求体里真的带了的字段**（值为 None 的一律不出现）：这正是
    DocumentPatch 那套「None = 不修改」语义的落地处，也是 UPDATE 语句的列清单来源。
    值统一 strip（首尾空白不是元数据的一部分），空串照旧保留 —— 它是「清空」。
    """
    fields: dict[str, str] = {}
    for name, (limit, label) in DOC_FIELD_LIMITS.items():
        raw = getattr(body, name)
        if raw is None:
            continue
        value = raw.strip()
        if len(value) > limit:
            raise HTTPException(400, f"{label}最多 {limit} 个字符")
        fields[name] = value
    st = fields.get("source_type")
    if st:
        # 大小写归一**只在这一条路径上做**（抽取侧是「库里原样存模型说的」，好让用户
        # 看见模型到底写了什么）。这里不同：值来自一个白名单下拉框，`m` 与 `M` 是同
        # 一个字母的两种写法，而存成小写会让下拉框匹配不上任何选项 —— 界面上表现为
        # 「没有选中项」，用户再点一次保存就会把它清空。
        fields["source_type"] = st.upper()
        if fields["source_type"] not in SOURCE_TYPES:
            raise HTTPException(
                400,
                "文献类型只能是 " + "/".join(SOURCE_TYPES) + " 之一，或留空",
            )
    return fields


def _refresh_binding_title(project_id: str, doc_id: str, doc: dict) -> bool:
    """文献改名后，刷新绑定条目里那份冻结的 `doc_title`；返回是否真的写回了。

    改的是**整份** binding（update_project 对 *_json 列是整体覆盖），所以整份读出来、
    就地改那几个条目、再整份写回。新值取自 `schedule_agent.binding_title` —— 与调度
    时的推导同一处，否则「编辑后重排」与「重新调度一次」会得到两份不同的文末列表。

    只改 doc_title：编号、顺序、章节归属、`page`、`reason` 一个字都不动（改题名不该
    动引用结构，这也是它能被 _refs_diff 判为 render、从而复用现成重排的前提）。
    """
    p = db.get_project(project_id) or {}
    binding = p.get("citation_binding_json")
    if not isinstance(binding, dict):
        return False
    want = schedule_agent.binding_title(doc)
    dirty = False
    for entries in binding.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and entry.get("doc_id") == doc_id:
                if entry.get("doc_title") != want:
                    entry["doc_title"] = want
                    dirty = True
    if dirty:
        db.update_project(project_id, citation_binding_json=binding)
    return dirty


@router.patch("/projects/{project_id}/documents/{doc_id}")
def update_document(project_id: str, doc_id: str, body: DocumentPatch):
    """编辑一篇文献的元数据（题名/作者/年份/来源/卷/期/页码/类型标识）。

    **为什么需要这个接口**：元数据全部由一次 LLM 调用产出，抽错是常态。此前改正的唯一
    办法是删掉重传，而重传会被内容指纹判重**静默跳过**（同一份 PDF 的正文一个字没变）
    —— 也就是说抽错的文献其实改不掉（见 db.document_fingerprint 与 _run_parse 的判重）。
    这条路径把「改正」变成一次普通编辑。

    改完**不自动重排文末列表**、更不动正文与编号：这里是元数据的写入点，不是引用的重排
    点。改动的效果由既有的 `citations_stale` 提示链承载 —— 元数据一变，目标快照与已落盘
    的 references_json 就不再相等，`_refs_diff` 判为 render（编号与指向都没变，只有渲染
    文本不同），用户按现成的「重排文末列表」按钮零成本刷新，正文逐字节不动。

    唯一的例外是**题名**：绑定条目里冻着一份 `doc_title`，而 `_plan_citations` 取的是
    绑定里那份（不是 documents.title）。不跟着刷新的话，改题名连 render 级的差异都产生
    不了 —— 目标快照与已落盘的完全相等，`_refs_diff` 判 none，界面一声不吭，用户会以为
    保存失败。所以题名变了且该文献已被绑定引用时，把绑定里那份一起改掉（见上）。

    聚类里那份 `doc_titles` **刻意不刷新**：它是「确认聚类」那一次的产物、只作为拟大纲
    的上下文，重新确认一次聚类即可刷新；绑定不一样 —— 它是文末列表的直接来源。

    路径里的项目 id 是守卫，理由与 delete_document 相同（前端那份列表可能属于上一个
    项目，只拿得到 doc id）。
    """
    doc = db.get_document(doc_id)
    if doc is None or (doc.get("project_id") or "") != project_id:
        raise HTTPException(404, "文献不存在")

    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])
    if _gen_running(project_id):
        raise HTTPException(409, f"{_GEN_BUSY_MESSAGE}，无法编辑文献")

    fields = _validated_doc_patch(body)
    # 「题名确实变了」是刷新绑定的**前提**，不是「请求体里带了 title」：原样提交一份
    # 没改过的题名不该去写绑定（那会把一次只读的往返变成一次写入）。
    old_title = doc.get("title") or ""
    title_changed = "title" in fields and fields["title"] != old_title

    updated = db.update_document_meta(doc_id, fields)
    refreshed = (
        _refresh_binding_title(project_id, doc_id, updated or {})
        if title_changed else False
    )
    # 返回整行（与 GET /documents 的单篇同形状）：前端据此就地更新那一条，不必再拉一次
    # 列表。binding_title_refreshed 只在要求「说人话」时用得上，前端默认不弹。
    return {
        "ok": True,
        "document": updated,
        "binding_title_refreshed": refreshed,
    }


# ---------------------------------------------------------------
# 作者自有研究材料（数据 / 成果 / 核心思路）
# ---------------------------------------------------------------
# 这一组接口**不走后台任务**，与文献上传刻意不同：文献解析每篇都要一次 LLM 调用
# 来提取元数据，N 篇就是 N 次串行往返；而材料解析是纯本地文本提取，毫秒级完成。
# 给一件不慢的事套上进度轮询，只会白白多出「任务互斥」「刷新续上」这些复杂度。
@router.post("/projects/{project_id}/materials/files")
async def upload_materials(project_id: str, files: list[UploadFile] = File(...)):
    """上传研究材料（PDF / Word / Excel / txt 等）。

    单份失败不中断整批：追加进 failed 并给出原因，与文献上传的行为一致。
    """
    if db.get_project(project_id) is None:
        raise HTTPException(404, "项目不存在")

    saved = 0
    failed: list[dict] = []
    for f in files:
        label = f.filename or ""
        try:
            text = material_parser.extract_text(label, await f.read())
        except material_parser.UnsupportedMaterial as e:
            failed.append({"filename": label, "reason": str(e)})
            continue
        except Exception as e:  # noqa: BLE001
            failed.append({"filename": label, "reason": f"解析失败: {e}"})
            continue
        db.add_material(project_id, {
            "id": uuid.uuid4().hex[:10], "label": label, "kind": "file", "text": text,
        })
        saved += 1

    if saved:
        _invalidate_material_plan(project_id)
    return {
        "saved": saved,
        "failed": failed,
        "materials": db.list_materials(project_id),
    }


@router.post("/projects/{project_id}/materials/text")
def add_material_text(project_id: str, body: MaterialText):
    """添加一段直接粘贴的材料（研究思路、结论草稿等）。"""
    if db.get_project(project_id) is None:
        raise HTTPException(404, "项目不存在")
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "材料内容为空")
    if len(text) > material_parser.MAX_CHARS:
        text = text[: material_parser.MAX_CHARS]
    label = (body.label or "").strip() or "粘贴的研究思路"
    db.add_material(project_id, {
        "id": uuid.uuid4().hex[:10], "label": label, "kind": "text", "text": text,
    })
    _invalidate_material_plan(project_id)
    return db.list_materials(project_id)


@router.get("/projects/{project_id}/materials")
def list_materials(project_id: str):
    """材料列表（不含正文，正文动辄十几万字，列表用不上）。"""
    return db.list_materials(project_id)


@router.delete("/projects/{project_id}/materials/{material_id}")
def delete_material(project_id: str, material_id: str):
    """删除一份研究材料。

    **路径里那个项目 id 与文献删除是同一道守卫**（原先的路由是平铺的
    `/materials/{material_id}`，项目 id 只能从材料行里反推）。前端那份材料列表
    也可能是上一个项目的：换项目时那次拉取有条件，列表不清就是上一个项目的材料，
    而每条右边的「删除」只拿得到 material id —— 反推出来的项目是**那份材料真正的
    项目**，于是「在 Y 的界面上点删除」会真删掉 X 的材料（D4）。路径与材料行
    不一致时按「材料不存在」返回 404，**不区分**「没这份」与「不属于这个项目」
    —— 否则这个接口就成了「拿 id 探测别的项目里有什么」的工具。
    """
    m = db.get_material(material_id)
    if m is None or (m.get("project_id") or "") != project_id:
        raise HTTPException(404, "材料不存在")
    db.delete_material(material_id)
    # 分析计划里按 material_id 引用材料，删掉后计划即失效。重新分析只要一次
    # LLM 调用，比重算「哪几条 placement 该删」更简单也更不容易出错。
    _invalidate_material_plan(project_id)
    return {"ok": True}


@router.post("/projects/{project_id}/materials/analyze")
async def analyze_materials(project_id: str):
    """让 LLM 分析这些材料该放进哪些章节、如何融入。"""
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    if not p.get("outline_json"):
        raise HTTPException(400, "请先生成大纲，材料需要按章节归位")
    if not llm.is_configured:
        raise HTTPException(400, "未配置 LLM，无法分析材料（可在「设置」中配置）")
    materials = db.list_materials(project_id, with_text=True)
    if not materials:
        raise HTTPException(400, "请先添加研究材料")

    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])

    _write_task(project_id, "material_analysis", "正在分析研究材料如何融入正文…")
    tasks.spawn(_analysis_key(project_id), _run_analyze(project_id, materials, p))
    return {"status": "running"}


async def _run_analyze(project_id: str, materials: list[dict], p: dict) -> None:
    try:
        outline = p.get("outline_json") or {}
        plan = await material_agent.analyze_materials(
            materials,
            [s["title"] for s in _collect_ordered_sections(outline)],
            paper_types.label_for(p.get("paper_type") or ""),
            p.get("topic", ""),
            core_question=outline.get("core_question", ""),
            writing_lang=_lang_of(p),
        )
        if not plan["placements"]:
            # 空计划若静默写库，界面会显示「已分析」却什么都没变 —— 宁可显式失败
            _write_task(
                project_id, "material_analysis", "未能给出可用的材料使用建议",
                status="error",
                error="模型没有把任何材料归入章节（可能材料与选题不相关）。"
                      "可以补充材料后重试，或跳过分析直接生成正文。",
            )
            return
        # **落库前确认这次分析的前提还在**。分析是一次 LLM 调用，几秒到几十秒里用户
        # 完全可以删掉一份材料、或改掉大纲标题 —— 这两件事都会当场把旧计划作废
        # （_invalidate_material_plan / confirm_outline 的 plan_stale 分支），可本任务
        # 手里还攥着开跑时的那份快照，一写完就把刚作废的计划又立了起来：界面显示
        # 「已分析·已把 N 份材料归入 M 个章节」，其中用的是已经不存在的 material_id，
        # 生成时 _materials_by_section 只能把它悄悄跳过 —— 一份「已分析」的计划配一份
        # 没有它的正文，正是本项目一路在消灭的那种静默失配。宁可丢弃本次结果。
        if _analysis_targets_changed(project_id, materials, p):
            _write_task(
                project_id, "material_analysis", "材料或大纲已变化，本次分析结果作废",
                status="error",
                error="分析期间材料或大纲被改动，本次结果已丢弃（它引用的是改动前的"
                      "材料与章节）。请重新点击「分析材料」。",
            )
            return
        db.update_project(project_id, materials_plan_json=plan)
        placed = len({mid for item in plan["placements"] for mid in item["material_ids"]})
        _write_task(
            project_id, "material_analysis",
            f"已把 {placed} 份材料归入 {len(plan['placements'])} 个章节",
            status="done",
        )
    except asyncio.CancelledError:
        _write_task(
            project_id, "material_analysis", "材料分析已中断", status="error",
            error="任务被中断（服务或窗口已关闭），可重新发起分析",
        )
        raise
    except Exception as exc:  # noqa: BLE001
        _write_task(
            project_id, "material_analysis", "材料分析失败", status="error",
            error=f"{type(exc).__name__}: {exc}",
        )


def _invalidate_material_plan(project_id: str) -> None:
    """材料集合一变，旧的分析计划就不再成立，直接作废。"""
    if project_id:
        db.update_project(project_id, materials_plan_json=None)


def _materials_set_changed(project_id: str, materials: list[dict]) -> bool:
    """开跑时那批材料，现在还在不在库里（增一删一都算变了）。

    材料分析计划与设计提炼都按 material_id 引用材料；这两件事都是一次 LLM 调用，
    几秒到几十秒里用户完全可以增删材料。开跑后集合变了，手里这份结果引用的就是
    已经不存在的（或已不全的）材料 —— 落库等于把「已分析/已提炼」立起来却配一份
    没有它的正文。宁可丢弃本次结果。
    """
    asked = {m["id"] for m in materials}
    now = {m["id"] for m in db.list_materials(project_id)}
    return now != asked


def _analysis_targets_changed(project_id: str, materials: list[dict], p: dict) -> bool:
    """本次材料分析的**两个前提**是否已经变了：材料集合、大纲章节标题。

    两样都是这份计划的 join key —— placements 按 material_id 引材料、按
    section_title 归章节 —— 所以只比对这两个键本身，不看内容：材料正文改了不影响
    「它属于哪一章」，章节顺序调整也不影响（计划的键是标题，标题跟着章节走）。
    作废的判据与 confirm_outline 里那道 plan_stale 用的是同一套（集合比较）。
    """
    if _materials_set_changed(project_id, materials):
        return True
    current = db.get_project(project_id) or {}
    asked_titles = {s["title"] for s in _collect_ordered_sections(p.get("outline_json") or {})}
    now_titles = {
        s["title"] for s in _collect_ordered_sections(current.get("outline_json") or {})
    }
    return now_titles != asked_titles


# ---------------------------------------------------------------
# 研究设计 / 技术方案（实证研究、技术报告）
# ---------------------------------------------------------------
# 为什么排在大纲之前：实证研究第四章「实证分析」要写的就是作者自己的数据、方法与
# 结果。此前大纲提示词的输入只有选题、类型名与骨架 —— 模型只能编变量、编显著性、
# 编结论，而这份虚构在确认点①上看起来完全合理，且会一路带进正文。
#
# 附件不另存一份：设计步上传的文件直接进 materials 表。于是「材料融入分析 → 按节
# 注入正文」这条已验证的链路一行都不用改，用户后文生成照样拿到原始数据；设计步
# 只负责把附件里的东西提炼成结构化字段，喂给大纲。
def _design_context(p: dict) -> str:
    """把设计字段拼成提示词里的文本（按该类型的字段顺序，跳过空字段）。

    全空时返回空串 —— 提示词随之逐字节退回旧版，没填设计的项目行为不变。
    """
    fields = paper_types.design_fields_for(p.get("paper_type") or "")
    if not fields:
        return ""
    saved = (p.get("design_json") or {}).get("fields") or {}
    lines = []
    for name in fields:
        value = (saved.get(name) or "").strip()
        if value:
            lines.append(f"{name}：{value}")
    return "\n".join(lines)


@router.post("/projects/{project_id}/design")
def save_design(project_id: str, body: DesignSave):
    """保存（手改后的）设计字段。

    只按该类型的字段名白名单存：前端表单结构由 paper_types 决定，不能被请求体改写。
    内容为空也照存 —— 用户清空某字段是明确意图（宁可空着让模型标「需作者补充」，
    也不要它自己猜）。
    """
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    fields = paper_types.design_fields_for(p.get("paper_type") or "")
    if not fields:
        raise HTTPException(400, "该论文类型无需填写研究设计")

    saved = {
        name: (body.fields.get(name) or "").strip()[: design_agent.FIELD_CHARS]
        for name in fields
    }
    db.update_project(project_id, design_json={
        "fields": saved,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    })
    return {"ok": True, "design": db.get_project(project_id).get("design_json")}


@router.post("/projects/{project_id}/design/extract")
async def extract_design(project_id: str):
    """把已入库的材料提炼成设计字段（后台任务，一次 LLM 调用）。"""
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    if not paper_types.has_design(p.get("paper_type") or ""):
        raise HTTPException(400, "该论文类型无需填写研究设计")
    if not llm.is_configured:
        raise HTTPException(400, "未配置 LLM，无法自动提炼（可在「设置」中配置）")
    materials = db.list_materials(project_id, with_text=True)
    if not materials:
        raise HTTPException(400, "请先添加研究设计材料")

    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])

    _write_task(project_id, "design_extract", "正在从材料中提炼研究设计…")
    tasks.spawn(_design_key(project_id), _run_extract_design(project_id, materials, p))
    return {"status": "running"}


async def _run_extract_design(project_id: str, materials: list[dict], p: dict) -> None:
    try:
        paper_type = p.get("paper_type") or ""
        fields = paper_types.design_fields_for(paper_type)
        result = await design_agent.extract_design(
            materials,
            fields,
            # 提示词里给长名：括号里的适用范围是模型判断该按哪套规范提炼的有效信息
            paper_type=paper_types.label_for(paper_type),
            design_label=paper_types.design_label_for(paper_type),
            topic=p.get("topic") or "",
            # 字段名仍是中文（它是前端表单的结构），变的是**取值**的语言 ——
            # 那些取值会逐字注入每一节正文提示词。
            writing_lang=_lang_of(p),
        )
        filled = {k: v for k, v in result["fields"].items() if v}
        if not filled:
            # 空结果静默写库会让界面显示「已提炼」却什么都没变，与材料分析的既有
            # 处理保持一致：宁可显式失败，让用户手填或补材料。
            _write_task(
                project_id, "design_extract", "未能从材料中提炼出设计内容",
                status="error",
                error=result.get("notes")
                or "模型没有从材料里找到可用的设计信息。可以补充材料后重试，或直接手填。",
            )
            return
        # 落库前确认本次提炼的前提还在：提炼是一次 LLM 调用，几秒到几十秒里用户
        # 完全可以删掉一份数据材料，而这份结果里的数字正是从那份材料里来的。不复查
        # 就落库，被删材料的数字照样进 design_json、再进大纲与每一节正文 —— 与材料
        # 分析那道 _analysis_targets_changed 是同一个道理（D7）。
        if _materials_set_changed(project_id, materials):
            _write_task(
                project_id, "design_extract", "材料已变化，本次提炼结果作废",
                status="error",
                error="提炼期间材料被增删，本次结果已丢弃（它引用的是改动前的材料）。"
                      "请重新点击「提炼设计」。",
            )
            return
        db.update_project(project_id, design_json={
            "fields": result["fields"],
            "notes": result.get("notes", ""),
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        })
        _write_task(
            project_id, "design_extract",
            f"已提炼 {len(filled)}/{len(fields)} 个字段，可自行修改",
            status="done",
        )
    except asyncio.CancelledError:
        _write_task(
            project_id, "design_extract", "设计提炼已中断", status="error",
            error="任务被中断（服务或窗口已关闭），可重新发起提炼",
        )
        raise
    except Exception as exc:  # noqa: BLE001
        _write_task(
            project_id, "design_extract", "设计提炼失败", status="error",
            error=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------
# 文献主题聚类（文献综述：进大纲之前的显式中间产物）
# ---------------------------------------------------------------
# 为什么做成一份能看、能改、能确认的产物，而不是提示词里多写一句：文献综述的
# 章节主题本来就该从文献里长出来。让模型在生成大纲时顺手归纳，用户永远看不到
# 它到底是按什么分的，也就无从纠正 —— 一份跑偏的归纳会直接变成章节结构。
#
# 聚类**不是独立步骤**（不占 STEP_KEYS 的一位），就放在大纲步里：用户说的
# 「聚类生成大纲」本就是一个阶段，而新增步骤键会牵动前端的锚点定位。
@router.post("/projects/{project_id}/clusters/generate")
async def generate_clusters(project_id: str):
    """触发生成主题聚类（后台任务，一次 LLM 调用）。仅文献综述可用。"""
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    paper_type = p.get("paper_type") or ""
    if not paper_types.is_literature_first(paper_type):
        raise HTTPException(400, "该论文类型的大纲结构不由文献聚类决定")
    docs = db.list_documents(project_id)
    if not docs:
        raise HTTPException(400, "请先上传并解析文献")
    if not llm.is_configured:
        raise HTTPException(400, "未配置 LLM，无法自动聚类（可在「设置」中配置）")

    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])

    _write_task(project_id, "clusters", "正在归纳文献的主题维度…")
    tasks.spawn(_clusters_key(project_id), _run_cluster(project_id, docs, p))
    return {"status": "running"}


async def _run_cluster(project_id: str, docs: list[dict], p: dict) -> None:
    try:
        paper_type = p.get("paper_type") or ""
        outline = p.get("outline_json") or {}
        plan = await cluster_agent.cluster_documents(
            docs,
            topic=p.get("topic") or "",
            paper_type=paper_types.label_for(paper_type),
            core_question=outline.get("core_question", ""),
            # 告诉模型骨架的主题章能容纳几节，它才好按「一簇一节」来分。
            # 给了之后簇数与节数通常就对得上，界面上的数量提醒也就不会吓人。
            # **必须带上语言**：这个数字靠子串匹配在骨架标题里找主题章，英文骨架下
            # 不给语言会静默返回 0，于是这句「恰好 N 个」退回「2–6 个自由归纳」——
            # 界面上看不出任何异常，只是簇数常与主题章的节数对不上。
            target_clusters=paper_types.theme_section_count(paper_type, _lang_of(p)),
            writing_lang=_lang_of(p),
        )
        if not plan["clusters"]:
            # 空结果静默写库会让界面显示「已聚类」却什么都没变 —— 与材料分析、
            # 设计提炼的既有处理一致：宁可显式失败，让用户重试或直接生成大纲。
            _write_task(
                project_id, "clusters", "未能归纳出主题聚类", status="error",
                error="模型没有把文献归入任何主题。可以重新生成，"
                      "或跳过聚类直接生成大纲（大纲将由模型自行归纳主题）。",
            )
            return
        db.update_project(project_id, clusters_json={
            "clusters": plan["clusters"],
            "notes": plan["notes"],
            "unassigned": plan["unassigned"],
            "generated_at": datetime.now(timezone.utc).isoformat(),
            # 重新生成即作废上一次的确认：新的簇用户还没看过
            "confirmed_at": None,
        })
        tail = f"，另有 {len(plan['unassigned'])} 篇未归入" if plan["unassigned"] else ""
        _write_task(
            project_id, "clusters",
            f"已归纳 {len(plan['clusters'])} 个主题维度{tail}，可修改后确认",
            status="done",
        )
    except asyncio.CancelledError:
        _write_task(
            project_id, "clusters", "主题聚类已中断", status="error",
            error="任务被中断（服务或窗口已关闭），可重新发起",
        )
        raise
    except Exception as exc:  # noqa: BLE001
        _write_task(
            project_id, "clusters", "主题聚类失败", status="error",
            error=f"{type(exc).__name__}: {exc}",
        )


@router.post("/projects/{project_id}/clusters/confirm")
def confirm_clusters(project_id: str, body: ClustersConfirm):
    """保存作者改过的主题聚类并确认。

    doc_ids 再白名单一遍：聚类生成之后作者可能删过文献，此时前端的列表已经过期，
    直接落库会让大纲照着一个不存在的主题组织章节。
    若已有大纲，只回一句 outline_stale 提示，**不动大纲** —— 与「设计未填告警放行」
    同一条原则：护栏该拦退化，不该拦用户已被告知后果的明确选择。
    """
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    paper_type = p.get("paper_type") or ""
    if not paper_types.is_literature_first(paper_type):
        raise HTTPException(400, "该论文类型的大纲结构不由文献聚类决定")
    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])

    known = {d["id"]: (d.get("title") or d.get("filename") or "") for d in db.list_documents(project_id)}
    clusters = []
    dropped = 0
    for item in body.clusters:
        title = (item.get("title") or "").strip()[: cluster_agent.MAX_TITLE_CHARS]
        if not title:
            continue
        ids = []
        for did in item.get("doc_ids") or []:
            if did in known and did not in ids:
                ids.append(did)
        if not ids:
            dropped += 1
            continue
        clusters.append({
            "id": f"c{len(clusters) + 1}",
            "title": title,
            "summary": (item.get("summary") or "").strip(),
            "doc_ids": ids,
            "doc_titles": [known[i] for i in ids],
            "key_points": [
                pt.strip() for pt in (item.get("key_points") or []) if pt.strip()
            ],
        })
    if not clusters:
        raise HTTPException(400, "聚类引用的文献都已被删除，请重新生成聚类")

    db.update_project(project_id, clusters_json={
        "clusters": clusters,
        "notes": (body.notes or "").strip(),
        "unassigned": _unassigned_of(p),
        "generated_at": (p.get("clusters_json") or {}).get("generated_at"),
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
    })
    theme_sections = paper_types.theme_section_count(paper_type)
    return {
        "ok": True,
        # 非破坏性提示：聚类变了，旧大纲未必还对得上，但改不改由用户决定
        "outline_stale": bool(p.get("outline_json")),
        "cluster_count": len(clusters),
        "theme_sections": theme_sections,
        "dropped": dropped,
    }


def _unassigned_of(p: dict) -> list[dict]:
    """沿用已存产物里的「未归入」清单；它由服务端算，前端改簇不影响它。"""
    data = p.get("clusters_json") or {}
    items = data.get("unassigned")
    return items if isinstance(items, list) else []


# ---------------------------------------------------------------
# 阶段四：引用调度
# ---------------------------------------------------------------
async def _build_binding(p: dict, docs: list[dict]) -> tuple[dict, str]:
    """这一轮要落库的引用绑定，以及它按哪种方式得来（"relevance" | "uniform"）。

    两条路：模型按主题相关性判断归属（relevance_agent），判断不可用时退回
    schedule_agent.uniform_distribute（确定性、一次模型都不调）。

    **先算兜底再问模型** —— 这不是顺序洁癖：兜底既是失败时的退路，也是修复判断的
    基准，而 fill_gaps 需要的正是「按上传顺序铺满」这份基线。于是两条路上落库的绑定
    都满足那四条承诺（无遗漏 / 同章不重复 / 全章节覆盖 / 页码确定），差别只在**归属
    由谁给出**，而这个差别如实写在返回值里。

    为什么不做成「点火即走」的后台任务：这是一次小调用（输入有界、输出一行一篇），
    结果被同一步的「确认引用绑定」立即消费，而失败必然退回确定性兜底 —— 用户永远
    拿到一份可确认的绑定，并且从 method 知道它是哪一种。所以它不需要「重启可恢复」
    那套机器（**路由仍然是同步的**：调用方一直等到这里算完，这条没有变）。
    但**它仍然要登记**：调用点把这段协程派进 `tasks._TASKS` 再 await 它自己（见
    schedule_citations），走的是同一条登记路径。
    「只省掉进度落库」这半句**v1.29 起也不成立**：进度也落了，理由与代价两头都变了 ——
    原先那 120 秒里界面一个字都不给，而用户能做的只有盯着一个灰按钮（PRD §8.1 记过这条
    盲区）。所以现在这里在**动手问模型之前**先写一笔，让界面知道"已经走在最慢那一段上"。
    但**给它一个假的百分比仍然是禁止的**：整段等待就是这一次调用，没有任何可数的单位，
    所以那一笔的 done/total 一律留 None，界面按不确定条渲染。
    """
    outline = p["outline_json"] or {}
    baseline = schedule_agent.uniform_distribute(outline, docs)
    # ② 最慢的那一段即将开始（兜底基线已经算完，是一瞬间的事）。这是界面上唯一一句
    # 能说明"现在在等什么"的话，所以必须写在 await 之前 —— 写在之后就等于没有。
    # 「最长 120 秒」说的就是下面那个上限（relevance_agent.REQUEST_TIMEOUT）。
    _write_task(
        p["id"], "citations", "正在让模型逐篇判断文献该归到哪一节（最长 120 秒）…"
    )
    assignments = await relevance_agent.assign_by_relevance(
        outline,
        docs,
        paper_type=p.get("paper_type") or "",
        topic=p.get("topic") or "",
        core_question=outline.get("core_question") or "",
    )
    if not assignments:
        return baseline, "uniform"

    by_section: dict[str, list[dict]] = {}
    for a in assignments:
        by_section.setdefault(a["section"], []).append(
            {"doc_id": a["doc_id"], "reason": a["reason"]}
        )
    merged = schedule_agent.fill_gaps(by_section, outline, docs)
    # 没有叶子章节时两条路都返回 {}（调度本身无从下手），取基线保持与旧行为一致。
    return (merged, "relevance") if merged else (baseline, "uniform")


def _schedule_done_message(binding: dict, method: str) -> str:
    """调度成功那条进度文案。

    只陈述**已经落在库里的事实**（章节数就是 binding 的 key 数、方法就是 method），
    不替用户解释为什么走到这一条 —— uniform 有两个来源（模型没给出可用结果 /
    fill_gaps 补不齐），拆不开就不能挑一个说成原因。
    """
    if not binding:
        # 没有叶子章节时两条路都返回 {}（见 _build_binding 末尾），这时"归入 0 个章节"
        # 读起来像失败而其实是没地方可归，据实说。
        return "调度完成：没有可归入的章节，绑定为空"
    if method == "relevance":
        return f"调度完成：模型逐篇判定完成，归入 {len(binding)} 个章节"
    return f"调度完成：本次未采用模型判断，已按上传顺序铺开，归入 {len(binding)} 个章节"


@router.post("/projects/{project_id}/citations/schedule")
async def schedule_citations(project_id: str):
    # 与 /citations/confirm、/citations/skip 共用同一道闸：三者写的是同一个阶段、
    # 同一个 status（调度写 citation_pending，另两个写 citation_confirmed），闸门就
    # 该是同一道。**此前它只查「outline_json 在不在」，既不查忙闲也不查状态** ——
    # 于是正文生成中回退到本步点一下「执行引用调度」，就能把 status 从 generating
    # 顶成 citation_pending：界面按 status 渲染的那一步当场跳走，而生成任务还在写正文。
    p = _require_citation_editable(project_id)
    docs = db.list_documents(project_id)
    if not docs:
        raise HTTPException(400, "请先上传文献")

    # 相关性判断是一次模型调用（_build_binding 里有 120s 上限，超时即退回兜底）。
    # 这段等待**必须登记**：它是全后端最后一段「有写入在进行、而其它入口完全不知道」
    # 的窗口 —— 此前那 120 秒里点火正文生成、重生成大纲、删文献、改选题全都照常放行，
    # 而这条路由收尾时用的是 await **之前**取的那份 p 写状态，于是它会把别人刚写下的
    # status 顶回 citation_pending，把别人的产物当成没发生过。
    # 做法：把它派成后台任务、再 await 它自己。登记表在整段等待期间都是「在跑」，而
    # _busy_task 查的正是这张表 —— 其余十来个入口一行都不用改就都看得见它（两套忙闲
    # 登记里，它属 tasks._TASKS 这一侧，所以不必逐个闸门去补第三套判据）。
    # 唯一的代价是那个绑定值：_guard 此前把协程的返回值丢掉，现在原样传出来。
    # ① 进度条的第一笔。**必须写在上面那道闸与「有没有文献」之后**：被拒的这一次不能
    # 留痕（本仓既有约定，界面上也不该冒出一条属于被拒请求的进度）。
    _write_task(project_id, "citations", "正在准备引用调度…")
    result = await tasks.spawn(_citations_key(project_id), _build_binding(p, docs))

    try:
        if result is None:
            # _guard 只在协程抛异常时返回 None。_build_binding 设计上不抛（判断不可用即
            # 退回确定性兜底），所以这是兜底分支：不写库、不推进状态，让用户重试。
            raise HTTPException(500, "引用调度未能完成，请重试")
        binding, method = result

        # 等回来之后**重取、重过一遍闸**（这一步同时替换掉原来那份旧快照）：上面那段登记
        # 挡住的是应用内的其它入口，而状态也可能被别的东西改过；更要紧的是写 status 时
        # 必须用**新**取的那份 p —— 沿用 await 之前的快照正是这类错配的根。
        p2 = _require_citation_editable(project_id)
        # 大纲在等待期间被换掉：绑定以章节标题为 key，落下去就是一份错位的绑定（生成时
        # 静默产出「零引用」的章节），而这正是 confirm_outline 要主动作废的那种状态。
        # 绑定与状态一个字节都不写，让用户重新调度。
        if p2.get("outline_json") != p.get("outline_json"):
            raise HTTPException(
                409, "大纲在调度期间被改动，本次绑定未写入，请重新执行引用调度"
            )
        updated = db.update_project(
            project_id,
            citation_binding_json=binding,
            status=_status_after_change(p2, db.ProjectStatus.CITATION_PENDING),
        )
        if updated is None:
            raise HTTPException(404, "项目不存在")
    except Exception as exc:
        # 这一段里**唯一的错误写者**，四条出口（500 / 重过闸被拒 / 大纲被换 / 项目没了）
        # 共用它。不在这里逐条写，是因为同一个出口写两遍时第二遍会盖掉前一遍的具体文案、
        # 还会把 started_at 重置一次；也因为将来新加一条出口时，忘写的那一条会把进度条
        # 永远留在"正在跑"上 —— 而 update_project 那一条正好是最容易漏的（它抛 404 时
        # 行已经没了，写入本身是空操作，但**前端看到的是没有终态的进度条**）。
        # 必须是 try/except 而不是 try/finally：finally 会把成功那条 done 盖成 error。
        # asyncio.CancelledError 不是 Exception（服务关闭/客户端断开），所以那种情况下
        # 槽可能留在 running —— 这正是 db.reset_stale_tasks 的职责，别指望这里兜住。
        detail = (
            exc.detail if isinstance(exc, HTTPException) else f"{type(exc).__name__}: {exc}"
        )
        # message 取一句**在所有出口上都为真**的话（四条的共同点就是"没做成"），
        # 具体原因放 error：既不替用户猜原因，也不把后端给的确切原因丢掉。
        _write_task(
            project_id, "citations", "引用调度未能完成", status="error", error=detail
        )
        raise

    # ③ 终态。**必须写在 update_project 与它那条 404 之后**：否则轮询会先看到"完成"，
    # 再去取一份还没写上绑定的项目 —— 那一瞬的界面是旧的，而用户正是在这一瞬最可能去
    # 点「确认引用绑定」（确认提交的是前端那份绑定）。
    _write_task(
        project_id, "citations", _schedule_done_message(binding, method), status="done"
    )
    # 已生成过正文的项目重排一次调度，正文里的角标与新的文末列表就分属两套编号。
    # 把这件事**如实挂在返回值上**，界面据此逐步提示 —— 它同时也是生成入口那条
    # 拒绝的理由，两处读的是同一个 _refs_diff。本次重排同时也把 unbound_documents
    # 清零（新绑定覆盖了库里全部文献），三个事实一起回报。
    return {"binding": binding, "method": method, **_citation_facts(updated)}


# 「引用调度 / 确认 / 跳过」允许在哪些状态下写入，等于允许从哪里进入生成，所以列成
# 白名单：
#   - outline_confirmed / resources_loading / citation_pending：三个正常入口
#     （确认大纲后 / 传完文献后 / 调度完之后）
#   - citation_confirmed：允许在生成前反复微调角标样式与绑定
#   - completed / exported：**正文已生成之后也允许改**（用户 2026-09-18 定）。改一份
#     已成稿的引用设置不会当场毁掉什么，拦着反而让人连一处笔误都修不了；代价是
#     「正文按旧编号、设置按新绑定」这个中间态会真实存在，于是这套策略由三处合成，
#     动其中一处必须同时看另两处：
#       · 写状态时回退的规则（_status_after_change）：已完成的项目只改设置，
#         不把 status 打回上游；
#       · 中间态由 citations_stale 明说（_citations_stale），界面逐步提示；
#       · 生成入口拒绝「复用旧正文 + 换一套文末列表」（_require_refs_consistent）。
# 其余一律拒绝。最关键的是 outline_pending：放行的话，一次「确认引用」就能把 status
# 从 outline_pending 直接写成 citation_confirmed —— 人工确认点①（确认大纲）被绕过，
# 而 citation_confirmed 本身就在生成白名单里，于是**没确认过的大纲**照样能生成出
# 正文，两个人工闸门废掉一个。
_CITATION_EDITABLE = {
    db.ProjectStatus.OUTLINE_CONFIRMED,
    db.ProjectStatus.RESOURCES_LOADING,
    db.ProjectStatus.CITATION_PENDING,
    db.ProjectStatus.CITATION_CONFIRMED,
    db.ProjectStatus.COMPLETED,
    db.ProjectStatus.EXPORTED,
}

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


def _require_citation_editable(project_id: str) -> dict:
    """引用调度 / 确认 / 跳过三个入口共用的闸门，返回项目行。

    此前 /citations/confirm 一句校验都没有：项目不存在时 UPDATE 影响 0 行、接口照样
    返回 {"ok": true}（调用方以为确认成功），状态是「草稿」时也能把 status 直接写成
    citation_confirmed。而它是**唯一一个不带任何前置校验就写 CITATION_CONFIRMED 的
    入口** —— 判状态类缺陷里最容易踩的那条最短路径就长在这里。
    """
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])
    if _gen_running(project_id):
        raise HTTPException(409, _GEN_BUSY_MESSAGE)
    if not p.get("outline_json"):
        raise HTTPException(400, "请先确认大纲")
    if p.get("status") not in _CITATION_EDITABLE:
        raise HTTPException(400, "请先确认大纲，再进行引用确认")
    return p


@router.post("/projects/{project_id}/citations/confirm")
def confirm_citations(project_id: str, body: CitationConfirm):
    # 人工确认点②：用户微调后的绑定关系 + 角标样式 + 参考文献格式
    p = _require_citation_editable(project_id)

    # 三条**路由内 400**（不用 pydantic Field 约束：那会返回 422，而前端 api.js 的
    # `new Error(err.detail)` 会把 detail 数组渲染成 [object Object]）。
    #
    # 为什么前端已经按语言收敛了选项、这里还要拦：这个接口是外部可调用的，界面上的
    # 收敛只保护界面；而且「闸门要在写状态的那一侧」是本项目已有的教训 —— 状态一旦
    # 落库，后面每一个渲染入口都得替它兜底。
    if body.cite_style not in SUPPORTED_CITE_STYLES:
        raise HTTPException(
            400,
            f"不支持的角标样式：{body.cite_style}"
            f"（可选：{cite_style_names(SUPPORTED_CITE_STYLES)}）",
        )
    if body.citation_format not in SUPPORTED_FORMATS:
        raise HTTPException(
            400,
            f"不支持的参考文献格式：{body.citation_format}"
            f"（可选：{format_names(SUPPORTED_FORMATS)}）",
        )
    # 「英文写作不可能选 GB/T 7714」的后端侧兜底。判据取自 `citation_formats_for`
    # 而不是写死 `en != gb7714`：那张表才是「哪种语言能用哪种格式」的唯一来源，
    # 将来给某种语言增删格式时，这里与下拉框会一起跟着变。
    #
    # 文案里的格式名走 `format_names`（`GB/T 7714、APA（第 7 版）`），不印内部标识：
    # 用户在界面上见到的就是这些名字，报错时说的名字必须与界面上说的是同一个。
    allowed = citation_formats_for(_lang_of(p))
    if body.citation_format not in allowed:
        lang_label = writing_lang.LANG_LABELS[_lang_of(p)]
        # 格式名与「格式」之间**不加空格**：格式名以右全角括号收尾（`GB/T 7714（gb7714）`），
        # 而中文排版里那个空格是给「紧挨着一个西文记号」留的。先前写的是
        # `不支持 gb7714 格式`（收尾是 `4`），那时空格是对的 —— 改文案时别顺手加回来。
        raise HTTPException(
            400,
            f"{lang_label}论文不支持 {format_names([body.citation_format])}格式"
            f"（可选：{format_names(allowed)}）",
        )

    updated = db.update_project(
        project_id,
        citation_binding_json=body.binding,
        cite_style=body.cite_style,
        citation_format=body.citation_format,
        status=_status_after_change(p, db.ProjectStatus.CITATION_CONFIRMED),
    )
    # 只改角标样式或列表格式也会让**已写成文字**的正文与文末列表分家（正文里的角标
    # 是烘死的，改样式只能靠重新生成），所以这里和重排调度一样要如实回报。
    return {"ok": True, **_citation_facts(updated)}


@router.post("/projects/{project_id}/citations/skip")
def skip_citations(project_id: str):
    """跳过引用调度：本文不产生参考文献列表。

    技术/工程报告的立身之本是技术方案与工程参数，不是参考文献，不该被一句
    「请先上传文献」挡在生成之外。绑定保持缺省（None）即可，**不需要哨兵值**：
    生成环节的失配检查是 `if binding and not any(...)`，缺省时自然跳过；
    大纲与生成也不再把引用当作必需项。

    **七类都开放**（用户定的策略）。跳过的后果由前端确认框逐类说明——跳过等于
    交出一篇零引用的论文，这件事必须由用户明确认下，而不是由我们替他决定。
    citations_required 那道闸门留着，只是当前无类型命中。

    唯一在此拦住的是文献综述**零文献**：那不是选择而是退化（它的结构本就来自
    文献），不过这条的主守卫在 _require_generatable（生成总闸），这里只是让用户
    更早、更靠近操作点地看到原因。

    前四道（存在性 / 忙闲 / 有大纲 / 状态白名单）与 /citations/schedule、/citations/confirm
    共用同一个 _require_citation_editable：三者写的是**同一个阶段、同一个 status**，
    闸门就该是同一道。
    """
    p = _require_citation_editable(project_id)
    if paper_types.citations_required(p.get("paper_type") or ""):
        raise HTTPException(400, "该论文类型必须完成引用调度")
    if paper_types.is_literature_first(p.get("paper_type") or "") and not db.list_documents(
        project_id
    ):
        raise HTTPException(
            400, "本类型论文的引用调度不能跳过：它的结构与论证都建立在这些文献上。"
                 "请先在「文献注入」步重新上传文献"
        )

    updated = db.update_project(
        project_id,
        citation_binding_json=None,
        status=_status_after_change(p, db.ProjectStatus.CITATION_CONFIRMED),
    )
    # 已生成过正文的项目再点「跳过」，正文里的角标仍在、文末列表却要清空 —— 同样
    # 属于「正文与当前设置不一致」，同样是如实回报而不是拦着不让点。
    # 跳过也把绑定清零，于是 unbound_documents 变成「库里有几篇」——这正是要的：
    # 跳过的项目后来加了文献，只有这个计数看得见（citations_stale 在那条路上恒为假）。
    return {"ok": True, **_citation_facts(updated)}


# ---------------------------------------------------------------
# 补救：引用不一致时的两个动作（与改动相称）
# ---------------------------------------------------------------
# 「正文与当前引用设置不一致」有两类，后果差一个量级，出路也必须分开：
#   · 只有排版变了（列表格式 / 角标样式）→ 重排文末列表，正文一个字不用动；
#   · 编号或成员变了 → 只能重写正文。
# 此前只有后一条路、而且实现方式是「回选题重做一遍」，代价是把大纲、引用绑定、设计、
# 材料计划一起覆盖式作废 —— 与「改了个列表格式」完全不相称。判据与文案都只有一份
# （_refs_diff / _refs_stale_message），这两个接口各自只做自己那一类允许的事。
_REFS_RERENDER_REFUSED = (
    "重排只适用于「编号与指向都没变、只有文末列表排版变了」的情形。这次改动动了编号"
    "（正文角标与列表条目的对应关系已经变了），重排会让两者指向不同的文献，"
    "必须重写正文：请到「分段生成」步点「作废正文并重写」。"
)


@router.post("/projects/{project_id}/references/rerender")
def rerender_references(project_id: str):
    """按当前引用设置重排文末参考文献列表（不碰正文、不调模型）。

    准入的门槛就是**安全性本身**：只有 kind == "render"（编号与指向一一对应、仅渲染
    文本不同）才允许重排。编号变过时重排等于亲手做出「正文角标指着 A、列表第 3 条写着
    B」这种静默错配 —— 那正是 _require_refs_consistent 守在生成门口的理由，不能从这边
    的按钮绕过去。所以这里**先判再写**，不做「反正用户想点就让他点」。

    写下去的值来自 _refs_target，与护栏比对用的是同一个函数，于是重排完成即
    citations_stale 为假（不是碰巧）。零 LLM 调用、不碰 sections_json。
    """
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])
    if _gen_running(project_id):
        raise HTTPException(409, _GEN_BUSY_MESSAGE)

    change = _refs_diff(p)
    if change["kind"] == "none":
        raise HTTPException(400, "文末列表与当前引用设置已经一致，无需重排")
    if change["kind"] != "render":
        raise HTTPException(400, _REFS_RERENDER_REFUSED)

    updated = db.update_project(project_id, references_json=_refs_target(p))
    return {"ok": True, **_citation_facts(updated)}


@router.post("/projects/{project_id}/sections/reset")
def reset_sections(project_id: str):
    """作废已生成的正文，保留其它一切产物。

    此前**没有这个动作**：全仓唯一能清掉 sections_json 的写入点是 POST /topic（还要求
    选题 / 类型 / 字数真的变了），所以「正文与当前引用设置不一致」时界面只能把用户赶回
    「选题与设定」重做一遍 —— 一次下拉框的改动要赔上大纲、引用绑定、设计、材料计划。
    清正文不需要动任何上游产物：_require_refs_consistent 拦的只是「**复用**旧正文 + 重写
    文末列表」，把正文清空正好落进它的「整篇重写」那条早退分支（那里连 references_json
    的基线都不用比）。这也补上了另一个此前完全不可达的诉求：想重写一版正文。

    状态取 citation_confirmed：它与 db.reset_stale_generating（服务重启造成的「正文不在了」）
    用的是同一个状态 —— 都是「已过引用确认点、还没有正文」。不取 completed：那个状态会
    让导出步与步骤条按「已完成」渲染，而这时的正文是空的。
    """
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])
    # 两道忙闲登记都要查（生成任务不进 tasks._TASKS，_busy_task 看不见它）
    if _gen_running(project_id):
        raise HTTPException(409, _GEN_BUSY_MESSAGE)
    if not p.get("sections_json"):
        raise HTTPException(400, "当前没有已生成的正文，无需作废")

    # 只清这三项：正文、与正文配套的文末列表基线、生成进度。
    # 显式不动 outline_json / citation_binding_json / materials_plan_json / design_json
    # 与文献材料 —— 作废的只有正文，这正是它比「回选题重做」便宜的地方。
    updated = db.update_project(
        project_id,
        sections_json=None,
        references_json=None,
        generation_state_json=None,
        status=db.ProjectStatus.CITATION_CONFIRMED,
    )
    return {"ok": True, **_citation_facts(updated)}


# ---------------------------------------------------------------
# 阶段五：分段生成（后台任务 + 进度轮询）
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

# 单节硬超时：一次卡死的 LLM 调用不应拖垮整个任务（超时会向上抛，
# 已完成的章节都已落盘，用户点「继续生成」即可从断点续写）。
_SECTION_TIMEOUT = 600.0


# 允许「进入或续写」正文生成的状态白名单。前四项与 _CITATION_EDITABLE 重合不是巧合：
# 生成正是从引用那一步往下走的，能改引用的地方就是能点击「开始分段生成」的地方。
#   - completed / exported：已生成的稿子再点一次生成是无害的（章节标题全部命中、跳过，
#     编号没变就原样重写一遍）。**编号变了**那一种由 _require_refs_consistent 给出确切
#     理由；拦在这道闸上只会让用户看到一句与真实原因无关的「请先确认大纲」。
#   - generating：服务重启后 db.reset_stale_generating 会把它改回 citation_confirmed，
#     所以库里还停在 generating 时通常真有一个进程内任务在跑（_gen_running 先 409）；
#     真残留时放行，正好让它从断点续写。
_GENERATABLE = {
    db.ProjectStatus.OUTLINE_CONFIRMED,
    db.ProjectStatus.RESOURCES_LOADING,
    db.ProjectStatus.CITATION_PENDING,
    db.ProjectStatus.CITATION_CONFIRMED,
    db.ProjectStatus.COMPLETED,
    db.ProjectStatus.EXPORTED,
    db.ProjectStatus.GENERATING,
}


def _require_generatable(project_id: str) -> dict:
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    if not p.get("outline_json"):
        raise HTTPException(400, "缺少大纲")
    # 到这里为止原本只查「大纲在不在」—— 而「在」既包含**已确认**，也包含「刚生成、
    # 还没点确认」，于是不点人工确认点①也能一路生成出正文（写状态的下游入口只要
    # 存在一条不查状态的捷径，闸门就废掉一个）。所以补一道状态白名单：能进生成的
    # 只有「走到过确认点」的那几个状态。
    if p.get("status") not in _GENERATABLE:
        raise HTTPException(400, "请先确认大纲，再进行正文生成")
    # 引用绑定并非所有类型都必需：七类都可以走 /citations/skip 直接生成
    # （见 paper_types.TYPE_CONFIG）。仍按 citations_required 拦一道，是为了让
    # 策略位保留着：将来想收紧任何一类，改一个字就生效。
    if not p.get("citation_binding_json") and paper_types.citations_required(
        p.get("paper_type") or ""
    ):
        raise HTTPException(400, "缺少引用绑定，请先完成引用调度")

    # **引用可以没有，文献不能没有** —— 对「结构由文献决定」的类型。
    # 零引用的实证研究是用户知情后的选择；零文献的文献综述是范畴错误：它的骨架
    # 本就由文献聚类生成。这条路是可达的：确认大纲后再把文献逐条删光（删文献不
    # 检查是否 literature-first），然后跳过引用调度，一路走到这里，产出与成功
    # 完全无法区分 —— 所以守卫必须落在生成这道总闸上，而不是 /citations/skip 上。
    if paper_types.is_literature_first(p.get("paper_type") or "") and not db.list_documents(
        project_id
    ):
        raise HTTPException(
            400, "本类型论文的正文必须以文献为基础，当前项目已没有文献，"
                 "请先在「文献注入」步重新上传文献"
        )
    return p


def _doc_field(d: dict, key: str) -> str:
    """从一行文献记录里取一个著录字段，**NULL 归一成空串**。

    `d.get(key, "")` 是不够的：`.get` 的默认值**只在键不存在时**生效，而列存在、
    值为 NULL 时 sqlite3 给回来的是 `None`。存量行（补列之前建的）与新加的列都会
    走到这一支，所以这里不能省。

    后果不是显示错 —— `None` 与 `""` 渲染出来一模一样 —— 而是**运行期写下的快照
    与迁移重算出来的不是同一份**：迁移那一侧走 `db._snapshot_field` →
    `metadata.clean_meta_value`，把 `None` 收敛成 `""`。于是这一轮写进
    `references_json` 的 `null`，下次启动会被 `_migrate_reference_snapshots` 判成
    「变了」并整条重写一遍 —— 正是 `db._SNAPSHOT_META_FIELDS` 那段注释预言的症状，
    也正是 `test_runtime_reference_snapshot_is_already_final_for_the_migration`
    要钉住的判据（那条用例此前一直红着，本函数是对它的修复）。

    判据**过同一个 `clean_meta_value`**、而不是自己写个 `or ""`：两侧必须同一个
    判定点，否则今天对齐了 `None`，明天仍会在占位词上再分一次家。顺带一个好处 ——
    存量库里万一存着「未提及」这类占位词，到这里就被抹平，与迁移侧同源。
    """
    return metadata.clean_meta_value(d.get(key))


def _plan_citations(
    sections: list[dict], binding: dict, doc_map: dict[str, dict]
) -> tuple[dict[str, int], list[dict]]:
    """按大纲顺序预扫描，一次性分配文献的全局编号。

    预扫描而非「生成到哪算到哪」：续写时跳过已完成的章节，若按实际遍历顺序
    编号，续写那一轮的编号会与首次运行错开，正文角标和文末列表就对不上了。
    大纲顺序恰好等于文献在成文中的首次出现顺序，两种情况下都稳定。

    **畸形绑定不在这里挑形状**：条目一律经 `_binding_entries` 取，读不懂的跳过。
    这一处原先直接 `binding.get(sec["title"], [])` 再 `r.get(...)`，而它在**读取
    路径**上 —— 畸形的绑定会被 /citations/confirm 先落库，此后每次 GET 详情算派生
    事实都会走到这里，一次请求换来的是一个项目永久 500。判据只有 _binding_entries
    一份，与 _binding_doc_ids 必然一致（少算一篇只会让文末列表少一条，界面会请用户
    重排一次，而重排本就会用一份合法绑定覆盖掉它）。

    **这里取的元数据字段与 `db._SNAPSHOT_META_FIELDS` 必须逐项对齐**（authors / year /
    source / volume / issue / page_range / source_type / title_en / place / edition /
    publish_date）：不对齐的话，启动时重算出的 formatted 与「这一轮会写出的」就不是
    同一个字符串，`citations_stale` 会常年亮着，而用户按了重排也修不好（`title_en`
    曾经漏掉过一次，症状正是这个）。

    漏掉 place/edition/publish_date 的症状与 title_en 那一次**恰好相反、也更好混**：
    迁移那一侧读得到它们（它走 `_SNAPSHOT_META_FIELDS` 与自己的 SELECT），而这里读不到
    —— 于是非期刊模板只在重算出来的那份里出现。两边都要有，且都取自文献库。
    """
    num_by_doc: dict[str, int] = {}
    ref_list: list[dict] = []
    for sec in sections:
        for r in _binding_entries(binding, sec["title"]):
            doc_id = r.get("doc_id")
            if doc_id in num_by_doc:
                continue
            d = doc_map.get(doc_id, {})
            num_by_doc[doc_id] = len(ref_list) + 1
            ref_list.append({
                "num": num_by_doc[doc_id],
                "doc_id": doc_id,
                # 题名**绑定优先** —— 绑定里那份是引用调度时冻进去的。这条优先顺序
                # 有两个后果：① 只改 documents.title 不会让这条 ref 变化，所以
                # PATCH 改题名时必须同步刷新绑定（见 update_document）；② 只改题名
                # 不动编号，_refs_diff 会把它判成 render，用现成的重排即可修复。
                "doc_title": r.get("doc_title") or d.get("title") or d.get("filename", ""),
                "authors": _doc_field(d, "authors"),
                "year": _doc_field(d, "year"),
                "source": _doc_field(d, "source"),
                "volume": _doc_field(d, "volume"),
                "issue": _doc_field(d, "issue"),
                # 键名在这里换一次：库里叫 page_range（避开 list_documents 会覆盖的
                # pages），而 citation_format 读 ref["pages"]（_apa / _mla 也一样）。
                "pages": _doc_field(d, "page_range"),
                "source_type": _doc_field(d, "source_type"),
                # 题名的英译（APA 7 §9.38 的方括号）。**取自文献库、不是取自绑定**：
                # 题名的权威是绑定（见上面那条注释），但 title_en 不在绑定里 ——
                # db._snapshot_field 对它是「文献还在就用库里的」，迁移那一侧读的就是
                # documents.title_en。两边必须同一个来源，否则 §9.38 的方括号只会在
                # 重算出来的那份里出现。
                "title_en": _doc_field(d, "title_en"),
                # 非期刊类型的三项著录字段（GB/T 的 [M]/[D]/[C]/[N] 各需一项）。
                # 与 title_en 同一条优先顺序：**取自文献库**，不在绑定里。
                "place": _doc_field(d, "place"),
                "edition": _doc_field(d, "edition"),
                "publish_date": _doc_field(d, "publish_date"),
            })
    return num_by_doc, ref_list


def _format_refs(
    ref_list: list[dict],
    citation_format: str,
    cite_style: str,
    lang: str = DEFAULT_LANG,
) -> list[dict]:
    """给编号表补上渲染文本，得到**将要落库**的那份文末列表。

    单独成一个函数，是为了让「写进 references_json 的东西」与「拿来比对的东西」是
    同一份实现：生成循环落盘、生成前的基线校验、正文与设置是否一致的判断三处都调它，
    于是比对和落盘不可能各算各的（两份实现迟早漂移，本项目已有先例）。

    `lang` 一路传到 `format_reference`（那里的 `effective_format` 是格式解析的唯一
    入口）：落盘用 zh 的默认值、比对用 en，两份快照就会差一个字符串，citations_stale
    会常年亮着而用户按了重排也修不好。
    """
    return [
        {**r, "formatted": format_reference(r, citation_format, cite_style, lang)}
        for r in ref_list
    ]


def _refs_snapshot(
    sections: list[dict],
    binding: dict,
    doc_map: dict[str, dict],
    citation_format: str,
    cite_style: str,
    lang: str = DEFAULT_LANG,
) -> list[dict]:
    """「当前引用设置 + 当前大纲 + 当前文献库」会产出怎样一份文末列表（逐字）。

    纯确定性、零 LLM、不写库。编号走的是生成循环开头同一个 _plan_citations（同一份
    大纲顺序、同一套首次出现编号），所以「生成时会写成什么」在生成之前就能知道 ——
    这正是能在入口处拦住错配的前提。
    """
    _, ref_list = _plan_citations(sections, binding, doc_map)
    return _format_refs(ref_list, citation_format, cite_style, lang)


def _refs_key(rows: list[dict] | None) -> list[tuple]:
    """文末列表的可比指纹：编号 + 文献 + 渲染文本，按顺序。

    只取这三项而不是逐字典比较：库里旧快照可能多带几个键（卷期页码等），键序也不
    保证，整字典相等会把「其实一模一样」的两份报成不一致。
    """
    return [(r.get("num"), r.get("doc_id"), r.get("formatted")) for r in rows or []]


def _refs_mismatch(existing: list[dict] | None, target: list[dict]) -> bool:
    """已落盘的那份与当前设置会产出的那份，是不是两套。

    `None`（从没写过）与 `[]`（写过，且那一轮确实没有参考文献）不是一回事：前者没有
    基线，判不了；后者是**明确的「这份正文按零引用写成」**，于是「跳过引用生成完之后
    又加了引用、再续写」这种也能判出来（_refs_key(None) 也是 []，所以这个区分必须
    显式写出来，不能靠比较）。
    """
    if existing is None:
        return False
    return _refs_key(existing) != _refs_key(target)


def _project_refs_snapshot(p: dict, sections: list[dict]) -> list[dict]:
    """按项目当前设置算出「这一轮会写出的文末列表」。

    文献走 list_document_meta（只取快照真正读到的十四列）而不是 list_documents：
    页码全文是每篇文献最重的一列，而 _plan_citations / format_reference 从不读它。

    **这里原先写的是「十一个字段」，v1.28 加 `place` / `edition` / `publish_date`
    三项之后就成了一句假话**（数字型的注释与现实脱钩时不会报错，只会让人照着一句
    错的前提去判断"快照读了哪些字段"）。要回答那个问题，以 `db._SNAPSHOT_META_FIELDS`
    与 `db.list_document_meta` 的 SELECT 为准 —— 那两个地方才是真的，这里只是提要。
    """
    lang = _lang_of(p)
    return _refs_snapshot(
        sections,
        p.get("citation_binding_json") or {},
        {d["id"]: d for d in db.list_document_meta(p["id"])},
        # 格式过 effective_format：与生成循环落盘时**同一个解析入口**，否则同一份
        # 设置会算出两串不一样的文本（这就是 citations_stale 常亮的形状）。
        effective_format(lang, p.get("citation_format") or DEFAULT_FORMAT),
        p.get("cite_style") or "bracket",
        lang,
    )


def _refs_target(p: dict) -> list[dict]:
    """按项目当前设置算出「这一轮会写出的文末列表」（含取哪份大纲）。

    多这一层是为了让**比对**与**重排**取的是同一个表达式：_refs_diff 拿它当比对对象、
    POST /references/rerender 拿它当写下去的值，同一个函数 —— 于是重排完成之后
    citations_stale 必然为假，不是碰巧对上。
    """
    return _project_refs_snapshot(p, _collect_ordered_sections(p.get("outline_json") or {}))


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


def _reference_runs_of(p: dict) -> list[list[dict]]:
    """项目**当前设置**下文末列表的片段形态（逐条 `[{text, italic}]`）。

    **设置解析只此一处**：格式/角标/语言三项与 `_migrate_reference_snapshots`
    和 `_format_refs` 用同一套取法与同一个 `effective_format`。各写一份的话，
    英文项目下算出来的斜体范围会与它自己的著录串对不上（那份走的是 en + APA）。

    渲染的是 `references_json` 里**存着的那份 ref**，与 `formatted` 同一来源 ——
    所以片段拼起来必然等于界面上印的那一行字（后端侧由
    `test_citation_format.py` 那条 78 条的不变量钉住）。
    """
    refs = p.get("references_json") or []
    if not refs:
        return []
    lang = _lang_of(p)
    fmt = effective_format(lang, p.get("citation_format") or DEFAULT_FORMAT)
    style = p.get("cite_style") or "bracket"
    return [reference_runs(r, fmt, style, lang) for r in refs]


def _with_reference_runs(p: dict) -> dict:
    """给一个项目 dict 挂上 `referenceRuns` 后返回。

    **并行字段，`references_json` 一字不动**：那一列里的 `formatted` 是快照的比对
    基准（`_refs_key` / `_refs_diff` / 存量迁移都认它），而斜体只是"这一段印成斜体"，
    是**渲染属性**——txt / md 是纯文本，同一份数据在那边没有斜体可言。所以片段只在
    下发响应时现算、不落库；前端 docx builder 在顶部归一化一次，md/txt 继续只读
    `references`（见 frontend/src/exporters.js）。

    挂载点**只有两处**：GET 详情与 GET 生成进度 —— 前端拿正文的两条路。两处都要挂，
    漏一处就会"进度页导出的 docx 没有斜体、详情页有"这种半生效。
    """
    return {**p, "referenceRuns": _reference_runs_of(p)}


def _refs_style(rows: list[dict] | None) -> str:
    """从渲染文本推出这份列表是按哪种角标样式写成的（方括号 / 纯数字上标）。

    _num_prefix 是 "[1] " 与 "1. " 这两串前缀的唯一来源，所以读第一个 token 就够 ——
    不必新增一列去记「生成时用的是哪种样式」。空列表返回空串（无从判断）。
    """
    for r in rows or []:
        text = (r.get("formatted") or "").strip()
        if text:
            return "bracket" if text.startswith("[") else "superscript"
    return ""


def _name_one(titles: list[str]) -> str:
    """点名一篇（标题缺失时不点 —— 宁可不举例，也不写《》这种空壳）。"""
    return f"（如《{titles[0]}》）" if titles and titles[0] else ""


def _refs_change_summary(
    kind: str, style_changed: bool, added: list[str], removed: list[str]
) -> str:
    """把这次改动写成一句人话，供前端告警条与生成被拒的 400 直接渲染。

    为什么由服务端拼、而不是前端按 added/removed 自己拼：这句话有两个消费点（App.jsx
    的常驻告警条、_refs_stale_message 的 400），两处各拼一遍就是两份迟早漂移的实现、
    并且会各自演化出不同的说法 —— 与 display_title 那条纪律同一个理由（派生值只有一个
    推导点，前端只渲染）。
    """
    if kind == "render":
        if style_changed:
            # 「只有排版不同」也不止一种。改角标样式这一类有一条**重排修不掉的残留**：
            # 文末列表会换成新样式，正文里的角标字形仍是生成时那一种（角标在正文里，
            # 只有重写正文才会变）。如实说出来，别让用户重排完以为样式没生效。
            return (
                "只有文末列表的排版变了（编号与指向都没变）：角标样式改过，"
                "重排后文末列表会换成新样式，正文里的角标字形仍是生成时那一种"
            )
        # **并列穷举，不猜是哪一种**（项目既有约定：文案不说自己判断不出的原因）。
        # 两个成因的后果完全相同（都是一次零成本重排），所以合并成一句是对的；
        # 但只写「改的是参考文献列表格式」在本轮之后就成了假原因 —— 编辑一篇文献的
        # 元数据（题名/作者/年份/来源/卷期页）同样只动渲染文本，编号与指向都没变。
        return (
            "只有文末列表的排版变了（编号与指向都没变）：改的是参考文献列表格式，"
            "或某篇文献的元数据"
        )
    if kind == "numbering":
        parts = []
        if removed:
            parts.append(f"少了 {len(removed)} 篇" + _name_one(removed))
        if added:
            parts.append(f"多了 {len(added)} 篇" + _name_one(added))
        if not parts:
            # 两侧篇数与文献都一致、却仍判 numbering：重新调度把归属换了，编号跟着换。
            # 这是按下「执行引用调度」之后最常见的形态，要单独有一句，不能说成「变了 0 篇」。
            return "还是那几篇文献，但章节归属或编号顺序变了"
        return "引用绑定变了：" + "、".join(parts)
    return ""


def _refs_diff(p: dict) -> dict:
    """正文与「当前引用设置会写出的文末列表」到底差在哪 —— 分两种，后果差一个量级。

    与 _citations_stale 同源同判（它现在就是本函数的 kind != "none"），差别只在把
    「不相等」拆成两类，因为两类的补救完全不相称：

    - "numbering"：正文角标与列表条目的对应关系变了。正文里的 [3] 与列表第 3 条不再是
      同一篇文献 —— 「引用可溯源」这条底线被静默破坏，只能重写正文。
    - "render"：两侧 (num, doc_id) 一一对应，只有渲染文本不同。角标指的还是原来那篇
      文献，变的只是文末列表的排版（列表格式 / 角标样式），重排一次列表即可，正文
      一个字都不用动。

    没有正文、或没有基线（references_json is None，本条护栏之前的历史数据）时一律
    "none"：无从错配，也就没有什么可补救 —— 与 _citations_stale 的短路口径完全一致
    （前两行短路只为省一次快照计算，真正的判定仍在 _refs_mismatch 里）。

    这不是库里的列，是**当下算出来的事实**：随 _citation_facts 一起下发，理由与
    citations_stale / unbound_documents 完全相同（落成列就要在每个改写点记得同步）。
    """
    none = {
        "kind": "none",
        "style_changed": False,
        "added_count": 0,
        "removed_count": 0,
        "summary": "",
    }
    if not p.get("sections_json"):
        return none
    existing = p.get("references_json")
    if existing is None:
        return none
    target = _refs_target(p)
    if not _refs_mismatch(existing, target):
        return none

    old_key, new_key = _refs_key(existing), _refs_key(target)
    # 两侧 (num, doc_id) 逐位相同 ⟹ 变的只有渲染文本；否则是编号/成员/归属变了。
    kind = "render" if [r[:2] for r in old_key] == [r[:2] for r in new_key] else "numbering"

    titles = {r.get("doc_id"): (r.get("doc_title") or "") for r in target}
    for r in existing:
        # 只在目标里没有时补：标题以**当前**文献库为准（改名后要说的是新名字）
        titles.setdefault(r.get("doc_id"), r.get("doc_title") or "")
    old_ids = [r[1] for r in old_key]
    new_ids = [r[1] for r in new_key]
    added = [titles.get(i, "") for i in new_ids if i not in set(old_ids)]
    removed = [titles.get(i, "") for i in old_ids if i not in set(new_ids)]
    style_changed = _refs_style(existing) != _refs_style(target)
    return {
        "kind": kind,
        "style_changed": style_changed,
        "added_count": len(added),
        "removed_count": len(removed),
        "summary": _refs_change_summary(kind, style_changed, added, removed),
    }


def _citations_stale(p: dict) -> bool:
    """正文已写成，而按当前引用设置重算出的文末列表与正文当初用的不是同一套。

    判据是「已有正文 + 已有基线 + 两者不等」，缺一即假：没有正文就无从错配；没有
    基线说明这份正文写在本条护栏之前（历史数据），此时宁可放行也不凭猜想报警。
    改引用设置**不再被拦**（用户 2026-09-18 定的策略），所以「生效与否」必须由这个
    事实说清楚：界面在每个步骤都据此提示，生成入口再用它给出拒绝理由。

    判定实现只有一份：委托 _refs_diff，这个布尔就是它 kind != "none" 的读法。带分类
    的那份（是编号错了还是只有排版变了）与补救动作一一对应，见 _refs_diff 与
    _refs_stale_message。
    """
    return _refs_diff(p)["kind"] != "none"


def _unbound_documents(p: dict) -> int:
    """文献库里有多少篇**没有被当前引用绑定覆盖**。

    为什么算得准：调度算法把传进去的每一篇文献都绑出去（n_doc >= n_sec 时每章先来
    一篇、剩下的走 _pick_section；n_doc < n_sec 时轮询铺满所有章节），而
    /citations/schedule 传的正是 db.list_documents(project_id) —— 所以调度完成的那一刻
    「绑定里的 doc_id 集合」与「库里的 id 集合」必然相等。此后出现差集，只可能是文献
    集合动过了（传了新文献 / 删过文献）。这是全仓第一处、也是唯一一处把两者对上。

    方向写死：算的是「库 − 绑定」，不是对称差 —— 绑定里有一篇库里已不存在的文献
    （悬空 doc_id）时这里看不见。那种 id 只可能来自手工构造的 /citations/confirm
    请求体（本步界面上没有绑定编辑器，前端从不自己编一份），是有意留下的盲区。

    绑定缺省（NULL）与空绑定（{}）都按「什么都没绑」处理：两者都是假值，且对用户是
    同一件事 —— 一篇文献都没被引用。于是「跳过引用调度之后又加了文献」自然落成
    「N 篇未绑定」，不需要哨兵值、也不需要记住跳过时的文献集合。这一条很重要：
    跳过一次之后 references_json 会被写成 []（明确的零引用基线），此后 _citations_stale
    算出来恒为假，「后加的文献永远进不了正文」在那个标志上完全不可见。

    为什么是一个计数而不是 bool：文案要说「有 N 篇」，而 N 只有算的人知道。前端不能
    拿自己的 documents.length 去数 —— 那份列表在解析中途是滞后的，这里读的是库里的行。

    为什么不是库里的列：它与 citations_stale 同类，是**当下算出来的事实**（由 GET 现算，
    随写接口的回执一起返回，见 _citation_facts）。落成列就要在每个会改文献或改绑定的
    地方记得跟着写 —— 而那正是 citations_stale 当初漏掉三个写入接口的原因。

    **没有叶子章节时一律 0**：调度算法在章节数为 0 时返回 {}，此状态重排一次也修不好，
    不短路就会变成一条永远清不掉的提示、指着一个按了也没用的按钮。短路条件用的
    _collect_ordered_sections 与 schedule_agent._collect_sections 逐行等价，所以它与
    「算法会不会返回空」问的是同一件事。
    """
    # 与 _citations_stale 一样先短路：连大纲都还没有的草稿项目，不必白查一遍文献。
    if not _collect_ordered_sections(p.get("outline_json") or {}):
        return 0
    bound = _binding_doc_ids(p.get("citation_binding_json"))
    return sum(1 for doc_id in db.list_document_ids(p["id"]) if doc_id not in bound)


def _citation_facts(p: dict) -> dict:
    """「此刻的引用设置还算不算数」这一组事实，一处算、一处返回。

    三个字段回答的是**三个不同的问题**，不能合成一个：citations_stale 讲「正文 vs
    编号」（补救＝重新生成）、unbound_documents 讲「文献 vs 绑定」（补救＝重新调度）、
    refs_change 讲「**到底哪一样变了**」（补救＝与改动相称的那一个：只改了排版就重排，
    动了编号才重写正文）。合成一句话，用户会按错的那条去修。

    但它们必须**同时**出现。此前 citations_stale 只挂在 get_project 上，几个引用写接口
    各自手写一遍 —— 于是那几个写入接口的返回值与详情接口的形状对不上（_with_display_title
    的 docstring 把这件事记成了先例，当初的代价是「少一条提示」）。这里返回一个 dict
    供 ** 展开，正是为了让它能在「返回整行」的详情接口与「返回自定义体」的五个引用
    写接口（调度 / 确认 / 跳过 / 重排列表 / 作废章节）上用同一份实现 ——
    ARCHITECTURE.md §八 第 9 条把「挂载点数应为 6」当作断言，加写接口时两处一起数：
        p.update(_citation_facts(p))
        return {"ok": True, **_citation_facts(updated)}

    citations_stale 由 refs_change 现算（不再单独算一遍快照）：它是同一个时刻的同一份
    比对，算两遍既浪费又给了「两个字段哪天说不到一起去」的机会。
    """
    change = _refs_diff(p)
    return {
        "citations_stale": change["kind"] != "none",
        "unbound_documents": _unbound_documents(p),
        "refs_change": change,
    }


def _body_outline_mismatch(p: dict) -> bool:
    """正文的章节标题与当前大纲的章节标题是否已经分家。

    「终态不回退」规则（confirm_outline 有意放行 completed / exported，改了标题也不退
    状态）留下的唯一缺口：标题改掉之后，正文（sections_json）还按旧标题躺着，导出给出
    的是旧标题的正文，而大纲页显示的是新标题。引用侧有 citations_stale 说「正文 vs 引用
    编号」，大纲侧此前没有对应的一句 —— 于是这个分家完全静默：状态停在 completed、
    导出闸门照样放行，用户拿到一份标题对不上的稿子。修 D1 时暴露，本函数是补上的
    那句提示（只提示，不粗暴作废 —— 与引用侧「放开 + 说清楚」同一条策略）。

    判定只在「终态」有意义：completed / exported 才有「正文已经写成、大纲又被改」这个
    状态。更早的状态下正文要么还没有、要么正在按当前大纲重写，标题集合不相等是「写到
    一半」的正常现象，不是分家 —— 所以**先按状态短路**，不去白算。

    与 citations_stale 同类：不是库里的列，是当下算出来的事实，随 get_project 下发、
    confirm_outline 的回执也带。落成列就要在每个改写点记得同步 —— 那正是 citations_stale
    当初漏掉三个写入接口的原因，这里从一开始就只算不存。
    """
    if p.get("status") not in (db.ProjectStatus.COMPLETED, db.ProjectStatus.EXPORTED):
        return False
    body = p.get("sections_json") or []
    if not body:
        return False
    outline = p.get("outline_json") or {}
    if not outline:
        return False
    body_titles = {r.get("section_title") for r in body}
    outline_titles = {s["title"] for s in _collect_ordered_sections(outline)}
    return body_titles != outline_titles


def _refs_stale_message(change: dict) -> str:
    """生成入口被拒时给用户的那句话，按改动种类分两版。

    **它是前端那条常驻告警条（App.jsx 的 refs_change 分支）的双胞胎**：两处必须说同一
    件事、指向同一个按钮，改措辞时一起改。事实那一句（到底哪一样变了）由
    refs_change.summary 提供，两处渲染的是同一份文本 —— 只有「去哪儿、点哪个按钮」这段
    由各自补齐（这里说步骤名，界面上说按钮名）。
    """
    if change["kind"] == "render":
        return (
            f"已生成的正文与当前引用设置不一致，但{change['summary']}。直接续写会让文末"
            "列表换成新排版、正文里的角标却还是旧的。要让改动生效，请到「分段生成」步点"
            "一下『按当前设置重排文末列表』—— 确定性重排、不调用模型，正文一个字都不用动。"
        )
    return (
        f"已生成的正文是按改动前的引用编号写成的（{change['summary']}）。直接续写会让"
        "被跳过的章节保留旧角标、文末列表却换成新编号，两者指的不是同一批文献。要让改动"
        "生效，请到「分段生成」步点一下『作废正文并重写』—— 大纲、引用绑定、设计、"
        "材料计划都会保留，只作废正文。"
    )


def _require_refs_consistent(p: dict, sections: list[dict]) -> None:
    """生成前的最后一道校验：不允许「复用旧正文 + 重写文末列表」。

    断点续写靠「章节标题已在 sections_json 里就跳过」实现（见 _run_generation），而
    文末列表每一轮结束都按当前绑定重算 —— 两件事单独看都对，合起来就是：只要绑定或
    列表格式在生成之后变过（删掉一篇被引文献、重排一次调度、换一种角标样式…），再点
    一次生成就会得到「正文角标照旧、文末列表全新」。两份输出都长得像正常产物，可
    [3] 与列表第 3 条已经不是同一篇文献了 —— 「0 幻觉引用」的底线正是引用可溯源，
    编号错位让溯源指向了别的文献，而且是静默的。

    判据因此不是「本轮生成几节」，而是**会不会复用**：整篇重写时（没有被跳过的章节）
    不存在错配，放行；只要有章节从盘上复用，就要求当前设置算出来的列表与正文当初用
    的那份相同，否则明确拒绝并告诉用户出路 —— 说清是「只改了排版」（重排即可）还是
    「动了编号」（必须重写正文），拒绝理由与他要做的动作对齐（_refs_stale_message）。

    两条边界都落在 _refs_diff 里，这里不再各写一遍：
    - 没有正文 → 判 none、放行，那正是 POST /sections/reset 作废正文之后能直接重写的原因；
    - 没有基线（references_json is None，这份正文写在本条护栏之前的历史数据）→ 同样判
      none、放行：宁可放行，也不凭猜想拒绝一次合法的断点续写（新数据从「首节与基线同一次
      落库」起都有基线）。
    """
    done_titles = {r.get("section_title") for r in (p.get("sections_json") or [])}
    if not any(s["title"] in done_titles for s in sections):
        return  # 整篇重写：正文与列表是同一轮、同一套编号，无从错配
    change = _refs_diff(p)
    if change["kind"] != "none":
        raise HTTPException(400, _refs_stale_message(change))


@router.post("/projects/{project_id}/generate")
async def generate_paper(project_id: str):
    """触发生成后立即返回；实际工作交给后台任务。

    必须保持 async def —— def 路由跑在线程池里，没有运行中的事件循环，
    asyncio.create_task 会直接抛 RuntimeError。
    """
    p = _require_generatable(project_id)

    # **两套忙闲登记都要查**，缺一侧就有一条错配路径：
    # _GEN_TASKS 管「这个项目正在生成正文」，_busy_task 管「这个项目有别的后台任务在跑」。
    # 此前这里只查前者，于是「正在解析文献」与「正在生成正文」可以同时成立：那几个任务
    # 里有几个会在收尾时写 status（解析成功写 RESOURCES_LOADING、大纲成功写
    # OUTLINE_PENDING），这一笔把 generating 顶掉之后，界面按 status 渲染的那一步就跳回
    # 上游，而生成任务仍在写正文 —— 用户看到的进度与真实进度从此分叉。
    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])

    if _gen_running(project_id):
        raise HTTPException(409, _GEN_BUSY_MESSAGE)

    sections = _collect_ordered_sections(p["outline_json"])
    if not sections:
        raise HTTPException(400, "大纲中没有可生成的章节")

    # ① 最后一道校验，也是本入口最要紧的一条：**复用旧正文时不允许重写文末列表**。
    # 放在点火之前（而不是循环里），是为了让用户拿到一条干净的 400 与确切理由，
    # 并且一个字节都不落库 —— 若放进后台任务，得先把 status 写成 generating、
    # 再回退，界面照样演一遍「开始了又失败」，那是另一种形式的含糊。
    _require_refs_consistent(p, sections)

    done = len(p.get("sections_json") or [])
    db.update_project(
        project_id,
        status=db.ProjectStatus.GENERATING,
        generation_state_json={
            "status": "running",
            "done": done,
            "total": len(sections),
            "current": sections[done]["title"] if done < len(sections) else "",
            "words_so_far": _words_of(p.get("sections_json") or []),
            "words_target": p.get("target_words") or 0,
            "eta_seconds": None,
            "error": None,
        },
    )
    _GEN_TASKS[project_id] = asyncio.create_task(_run_generation(project_id))
    return {"status": "running", "total": len(sections), "done": done}


@router.get("/projects/{project_id}/generate/progress")
def generate_progress(project_id: str):
    """轻量进度查询。运行中不返回正文（几千字正文每 1.5 秒传一次毫无意义）。"""
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")

    state = dict(p.get("generation_state_json") or {})
    sections = _collect_ordered_sections(p.get("outline_json") or {})
    done = state.get("done")
    if done is None:
        done = len(p.get("sections_json") or [])

    resp = {
        "status": state.get("status") or "idle",
        "done": done,
        "total": state.get("total") or len(sections),
        "current": state.get("current") or "",
        "words_so_far": state.get("words_so_far") or 0,
        "words_target": state.get("words_target") or (p.get("target_words") or 0),
        "eta_seconds": state.get("eta_seconds"),
        "error": state.get("error"),
    }
    if resp["status"] == "done" or p.get("status") == db.ProjectStatus.COMPLETED:
        refs = p.get("references_json") or []
        resp["sections"] = p.get("sections_json") or []
        resp["references"] = refs
        # 片段形态与 references 并排下发，给 docx 排斜体（见 _with_reference_runs）。
        # 这一处是"进度页/详情页两条路都挂"里的第二条 —— 少挂它，生成完直接导出
        # 的那份 docx 就没有斜体，而刷新一下再导出又有，是最难查的那种半生效。
        resp["referenceRuns"] = _reference_runs_of(p)
        resp["cite_style"] = p.get("cite_style") or "bracket"
        # 报给前端的是**该语言下真正生效**的格式：前端按语言渲染格式下拉，若这里
        # 报一个不在选项里的值（如英文项目报 gb7714），下拉框会显示成空选。
        resp["citation_format"] = effective_format(
            _lang_of(p), p.get("citation_format") or DEFAULT_FORMAT
        )
        resp["total_words"] = _words_of(resp["sections"])
    return resp


def _words_of(sections: list[dict]) -> int:
    return sum(int(s.get("actual_words") or 0) for s in sections)


def _materials_by_section(plan: dict | None, materials: list[dict]) -> dict[str, list[dict]]:
    """把材料分析计划整理成 {章节标题: [材料条目]}，供生成时按节取用。

    计划里按 material_id 引用材料；取不到说明材料已被删除（计划随即被作废，
    但旧库或并发删除都可能留下悬空 id），跳过而不是塞一份空材料进去。
    """
    if not plan:
        return {}
    by_id = {m["id"]: m for m in materials}
    out: dict[str, list[dict]] = {}
    for item in plan.get("placements") or []:
        title = item.get("section_title")
        if not title:
            continue
        entries = []
        for mid in item.get("material_ids") or []:
            m = by_id.get(mid)
            if m is None:
                continue
            entries.append({
                "label": m.get("label") or "研究材料",
                "text": m.get("text") or "",
                "usage": item.get("usage", ""),
                "key_points": item.get("key_points") or [],
            })
        if entries:
            out[title] = entries
    return out


async def _run_generation(project_id: str) -> None:
    """后台生成主循环：逐节生成、逐节落盘、逐节推进度。

    每节结束就写 sections_json，是为了修一个既有缺陷：此前只在整轮循环结束后
    写一次，一旦请求超时或进程被杀，已生成的章节全部丢失。
    """
    started = time.monotonic()
    produced = 0
    try:
        p = db.get_project(project_id)
        if p is None:
            return
        sections = _collect_ordered_sections(p.get("outline_json") or {})
        binding = p.get("citation_binding_json") or {}
        lang = _lang_of(p)
        cite_style = p.get("cite_style") or "bracket"
        # 格式过一遍 effective_format：库里的组合可能是非法的（乱改过的旧库、
        # 或迁移前留下的 en + gb7714），文末列表按「该语言的默认格式」渲染，
        # 而不是照着一个用不出来的格式硬写。
        citation_format = effective_format(lang, p.get("citation_format") or DEFAULT_FORMAT)
        target_words = p.get("target_words") or 0
        total = len(sections)

        # 引用绑定以「章节标题字符串」为 join key。改过大纲却没重排引用时，
        # 全部章节都会匹配不到文献，安静地生成出一篇零引用的论文——和成功
        # 完全无法区分。宁可显式失败，也别交出这种东西。
        if binding and not any(binding.get(s["title"]) for s in sections):
            raise RuntimeError("引用绑定与当前大纲不匹配，请重新执行引用调度")

        docs = db.list_documents(project_id)
        doc_map = {d["id"]: d for d in docs}
        num_by_doc, ref_list = _plan_citations(sections, binding, doc_map)
        # 本轮会写出的文末列表，先算好：它既用作**首节落库时的那份基线**，也用作
        # 循环结束时的正常写入。两处必须是同一份 —— 见 _format_refs 的注释。
        formatted_refs = _format_refs(ref_list, citation_format, cite_style, lang)

        # 作者自有的研究材料（非文献综述类型才有）。整轮只读一次库 —— 材料正文
        # 可能很大，逐节去查会把同一份材料反复读 N 遍。
        materials_used_map = _materials_by_section(
            p.get("materials_plan_json"),
            db.list_materials(project_id, with_text=True),
        )
        # 设计字段同样整轮只算一次，且必须进**正文**而不只是进大纲：大纲只定骨架，
        # 第四章的每一句话是在这里写的。只在提示词里给大纲，手填的设计照样管不住正文。
        design_context = _design_context(p)
        paper_type = p.get("paper_type") or ""
        design_label = paper_types.design_label_for(paper_type)
        # 该类型论文该怎么写（质性以研究者口吻、数理先声明假设、工程给具体参数…）。
        # 七类共用一套生成提示词，不给这句，写出来就是同一个腔调。
        # 语言同样从这一个 p 快照读：它决定骨架、类型说明、字数口径与输出语言宣告。
        writing_note = paper_types.writing_note_for(paper_type, lang)
        # 全篇主线（大纲第一遍定下的那句话）与作者写下的写作思路，同样整轮只算一次。
        # **此前这两条一条都到不了正文**：章节标题是被模型改写过的一句话，从它反推
        # 「这篇论文在论证什么」，与大纲页上显示的那一句未必是同一件事。
        # 与 sections（2124 行）读的是**同一个 p 快照**：逐节重读会做出「章节来自大纲
        # v1、核心问题来自大纲 v2」的错配。空值必须容忍——core_question 只在生过大纲
        # 之后才有，且改选题会随 outline_json 一起作废。
        core_question = (p.get("outline_json") or {}).get("core_question") or ""
        writing_ideas = p.get("writing_ideas") or ""

        # 断点续写：已落盘的章节直接跳过（关窗会杀掉后端进程，重启后从这里接着跑）
        #
        # 只保留**属于当前大纲**的章节：改过标题再重新生成时，旧大纲留下的那几节既不是
        # 本轮要写的（标题已不在大纲里，跳过判断永远命中不了它们），又会在结尾被原样
        # 写回 sections_json —— 于是得到「旧章节 + 新章节」两套并存的稿子，两套都像正常
        # 正文，导出时一起印出来。它与「续写」只差一个标题有没有被改过，所以清理必须
        # 发生在续写判断之前。
        outline_titles = {s["title"] for s in sections}
        results = [
            r for r in (p.get("sections_json") or [])
            if r.get("section_title") in outline_titles
        ]
        done_titles = {r.get("section_title") for r in results}
        # 本轮是续写还是从零写起。**必须在循环前定下来**：results 在循环里会追加，
        # 循环中途再问就永远是「已有正文」了。
        resuming = bool(results)
        context = results[-1].get("content", "")[-500:] if results else ""

        for idx, sec in enumerate(sections):
            if sec["title"] in done_titles:
                continue

            refs = []
            # 与 _plan_citations 同一个入口取条目：畸形的绑定不许在这里把整轮生成
            # 打断（它在后台任务里抛出来，代价是这一批生成全废）。
            for r in _binding_entries(binding, sec["title"]):
                d = doc_map.get(r.get("doc_id"), {})
                refs.append({
                    "num": num_by_doc.get(r.get("doc_id"), 0),
                    "doc_id": r.get("doc_id"),
                    "doc_title": r.get("doc_title") or d.get("title", ""),
                    "page": r.get("page"),
                    "snippet": _find_page_snippet(d, r.get("page")),
                })

            result = await asyncio.wait_for(
                generate_agent.generate_section(
                    section_title=sec["title"],
                    word_budget=sec["word_budget"],
                    refs=refs,
                    context=context,
                    topic=p.get("topic", ""),
                    cite_style=cite_style,
                    materials=materials_used_map.get(sec["title"]),
                    design_context=design_context,
                    design_label=design_label,
                    paper_type=paper_type,
                    writing_note=writing_note,
                    writing_ideas=writing_ideas,
                    core_question=core_question,
                    writing_lang=lang,
                ),
                timeout=_SECTION_TIMEOUT,
            )
            # 接入润色 Agent：生成后学术化润色（去 AI 痕迹 / 降重）
            drafted = result["content"]
            if result.get("fallback"):
                # 占位文本是给用户看的失败说明，不是正文。送去润色只会被改写成一段
                # 读起来像正文的话——实测「当前环境未配置大模型」被润色成了流畅的
                # 学术句子，失败就此被伪装成了正常段落。
                polished = drafted
            else:
                # 语言一并传下去：这一段的输入输出都是正文本身，润色环节换了语言
                # 就等于把刚写好的英文正文整段换掉，而下游只看到「正文变了」。
                polished = await asyncio.wait_for(
                    polish(drafted, lang), timeout=_SECTION_TIMEOUT
                )
            result["content"] = polished
            # **必须走 count_units**（与 generate_section 同一个分发点）：这里此前写死
            # count_chars，英文项目下会把润色后的正文按字符重算一遍，把生成环节算好的
            # 词数覆盖成六倍左右 —— 界面上「已写字数」虚高，进度条与 ETA 跟着失真。
            result["actual_words"] = count_units(polished, lang)
            # 「确实换了一段文字」才算润色过。polish 在三类情况下**原样返回**：结果为空、
            # 长度严重偏离、角标被动过，那样这一节就是未经润色的原稿，标成已润色是虚报；
            # 占位文本同理（它根本没送去润色）。
            result["polished"] = not result.get("fallback") and polished != drafted
            results.append(result)
            produced += 1
            # 继承上下文：用本节末尾作为下一节的上下文。占位文本不进上下文 ——
            # 把「模型没返回正文」这段说明塞给下一节，只会污染下一节的写作。
            if not result.get("fallback"):
                context = polished[-500:]

            # 先落正文再报进度：崩在两者之间只会让进度少一格，不会丢内容
            if produced == 1 and not resuming:
                # 首节落库时把**本轮编号快照**一并写进 references_json：它是「这份
                # 正文是按哪一版编号写的」的唯一凭证（_require_refs_consistent 与
                # _citations_stale 都拿它当基线），所以必须与首节同一次写入 —— 分两笔
                # 落，中间崩掉就会留下一份没有基线的正文。
                # `not resuming` 不能省：续写时无条件重写，等于当场把「生成完之后改了
                # 引用设置」那个状态洗白（基线被换成新编号），两处校验从此形同虚设。
                db.update_project(
                    project_id, sections_json=results, references_json=formatted_refs
                )
            else:
                db.update_project(project_id, sections_json=results)
            nxt = next(
                (s["title"] for s in sections[idx + 1:] if s["title"] not in done_titles),
                "",
            )
            db.update_project(project_id, generation_state_json={
                "status": "running",
                "done": len(results),
                "total": total,
                "current": nxt,
                "words_so_far": _words_of(results),
                "words_target": target_words,
                "eta_seconds": _eta(started, produced, total - len(results)),
                "error": None,
            })

        # 一整轮跑完：落正文 + 文末列表 + 状态。能走到这里说明本轮要么整篇重写（正文
        # 与列表同一轮算出的），要么续写时编号与基线逐字相同（入口已校验），所以这次
        # 写入不会把「正文角标 / 文末列表」拆成两套 —— 那正是 _require_refs_consistent
        # 守在门口的原因。
        db.update_project(
            project_id,
            sections_json=results,
            references_json=formatted_refs,
            status=db.ProjectStatus.COMPLETED,
            generation_state_json={
                "status": "done",
                "done": len(results),
                "total": total,
                "current": "",
                "words_so_far": _words_of(results),
                "words_target": target_words,
                "eta_seconds": 0,
                "error": None,
            },
        )
    except asyncio.CancelledError:
        # CancelledError 继承自 BaseException，必须排在 Exception 之前单独接住，
        # 否则被取消的任务会把项目永远留在「生成中」
        _fail_generation(project_id, "生成任务被中断（服务或窗口已关闭）")
        raise
    except asyncio.TimeoutError:
        # 单节硬超时（_SECTION_TIMEOUT）同样要排在 Exception 之前，且必须写一句人话：
        # TimeoutError 的 str() 是空串，走下面那条通用分支会落成 "TimeoutError: " 这种
        # 没有内容的失败原因。已完成章节都已逐节落盘，退回可重试状态、用户点「继续生成」
        # 即可从断点续写。
        _fail_generation(project_id, "单节生成超时（LLM 长时间未响应），已完成章节已保留，可继续生成")
    except Exception as exc:  # noqa: BLE001
        # 桌面版 console=False，异常不落库就等于彻底看不见
        _fail_generation(project_id, f"{type(exc).__name__}: {exc}")
    finally:
        _GEN_TASKS.pop(project_id, None)


def _fail_generation(project_id: str, message: str) -> None:
    """把任务标记为失败，并退回可重试的状态。已生成的章节原样保留。"""
    p = db.get_project(project_id) or {}
    state = dict(p.get("generation_state_json") or {})
    state.update({"status": "error", "error": message})
    db.update_project(
        project_id,
        status=db.ProjectStatus.CITATION_CONFIRMED,
        generation_state_json=state,
    )


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


# ---------------------------------------------------------------
# 阶段六：导出回执
# ---------------------------------------------------------------

@router.post("/projects/{project_id}/export")
def mark_exported(project_id: str):
    """把「这份稿子已经导出过」记进状态机：completed → exported。

    **只记状态，不产文件**。Word / txt / Markdown 三种格式全部在浏览器里拼出来
    （frontend/src/exporters.js），服务端一个字节都不参与，所以这个接口没有文件可返回，
    返回值就是那份更新过的项目行。名字叫 /export 而不是 /exported，是因为它对外的身份
    仍是「导出这个动作的回执」；读这个接口的人只要记住它不碰正文、不碰产物。

    为什么必须有这个写入点：`db.ProjectStatus.EXPORTED` 此前**只有读者没有作者**——
    枚举里定义着、_CITATION_EDITABLE / _GENERATABLE 里列着、界面按它认「走到过导出」，
    但全仓没有一个地方写它。状态机上一个永远到不了的终态，等于在步骤条上摆一格永远
    点不亮的格子（前端 STATUS_ANCHOR 给它锚在 'export'、STEP_DONE_STATUSES 里也有它）。

    状态白名单只有 completed，三条分支各有各的理由：
      · completed → 写 exported（唯一的正常路径）；
      · exported  → 原样返回成功。**幂等是必须的**：用户换个格式再导一次、或者刷新后
        又点一次，第二次报错只会让人以为第一次没成功；
      · 其余      → 400。generating 尤其：正文正在被逐节重写，此时记下的「已导出」记的
        是上一版正文的导出，而这一版写完之后没有任何东西会再记一次 —— 那是一句会在
        界面上长期留着的假话。sections_json 为空也一并拒掉：没有正文的「已导出」同样
        是假话，而它在库里看不出区别。

    忙闲两道闸都要查（与 /sections/reset 同一个理由）：写的是 status，而 tasks._TASKS
    里的任务（大纲、解析、材料、提炼、聚类、引用调度）有几个收尾时会无条件写 status，
    _GEN_TASKS 里的生成任务更是每一节完成后都在写 —— 这里记下的 exported 会被它们
    下一笔顶掉，用户看到的是导出步刚打上勾又消失。
    """
    p = db.get_project(project_id)
    if p is None:
        raise HTTPException(404, "项目不存在")
    busy = _busy_task(project_id)
    if busy:
        raise HTTPException(409, _BUSY_MESSAGE[busy])
    if _gen_running(project_id):
        raise HTTPException(409, _GEN_BUSY_MESSAGE)

    status = p.get("status")
    if status == db.ProjectStatus.EXPORTED:
        return _with_display_title(p)
    if status != db.ProjectStatus.COMPLETED or not p.get("sections_json"):
        raise HTTPException(400, "正文还没有生成完成，无法导出")

    return _with_display_title(
        db.update_project(project_id, status=db.ProjectStatus.EXPORTED)
    )
