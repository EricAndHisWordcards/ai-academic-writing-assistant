"""项目路由 —— 聚合门面。

本文件只保留三件事：

1. 项目本体（新建 / 列表 / 详情 / 改名 / 删除）与选题两条路由；
2. `include_router` 聚合四个域模块：outline（大纲与聚类）/ documents（文献、材料、
   设计）/ citations（引用调度与参考文献一致性）/ generation（分段生成与导出回执）；
3. **重导出**测试打桩所需的名字（含下划线开头）—— 测试与 main.py 经
   `app.routers.projects` 的模块属性访问它们，门面必须继续提供同一批绑定；
   顶部三个模块别名（tasks / outline_agent / relevance_agent）即属此类。

`_GEN_TASKS` 全仓只有一个 dict 对象：定义在 `projects_common`，各域模块与门面
import 的都是同一个对象；`tasks._TASKS` 同理（定义在 `app.tasks`）。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from app import db, paper_types, writing_lang
from app import tasks  # noqa: F401  # 重导出：测试经 projects_router.tasks 直插 _TASKS
from app.agents import topic_agent
from app.agents import outline_agent, relevance_agent  # noqa: F401  # 重导出：测试 patch 模块方法
from app.citation_format import DEFAULT_FORMAT, effective_format

from .projects_common import (
    MAX_IDEAS_CHARS,
    MAX_TITLE_CHARS,
    MIN_TARGET_WORDS,
    ProjectCreate,
    ProjectTitle,
    TopicRecommend,
    TopicSet,
    _BUSY_MESSAGE,
    _GEN_BUSY_MESSAGE,
    _GEN_TASKS,
    _busy_task,
    _gen_running,
    _lang_of,
    _with_display_title,
)
# 以下名字门面自身不读，只为测试保留模块属性（test_api / test_db / test_generation /
# test_frontend_mirror 按 `projects_router.<name>` 取用）。
from .projects_common import (  # noqa: F401
    DOC_FIELD_LIMITS,
    _citations_key,
    _collect_ordered_sections,
    _eta,
)
from .projects_citations import _body_outline_mismatch, _citation_facts, _with_reference_runs
from .projects_citations import (  # noqa: F401  # 重导出
    _build_binding,
    _plan_citations,
    _project_refs_snapshot,
)
from .projects_generation import _run_generation  # noqa: F401  # 重导出

router = APIRouter(prefix="/api", tags=["projects"])


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
# 域路由聚合。注册顺序 = 门面自身路由在前，随后四个域模块（URL 全部互异，
# 无通配 / 参数遮蔽，顺序不影响匹配）。
# ---------------------------------------------------------------
from . import (  # noqa: E402
    projects_citations,
    projects_documents,
    projects_generation,
    projects_outline,
)

router.include_router(projects_outline.router)
router.include_router(projects_documents.router)
router.include_router(projects_citations.router)
router.include_router(projects_generation.router)
