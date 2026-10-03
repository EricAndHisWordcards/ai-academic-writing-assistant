"""项目路由 · 引用调度与参考文献一致性域。

引用调度 / 确认 / 跳过、文末列表重排、正文作废，以及整套「正文与当前引用设置
是否一致」的判定（_refs_diff 一族）。生成入口（projects_generation）与项目详情
（projects.py 门面）都消费这里算出的事实。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app import db, metadata, paper_types, tasks, writing_lang
from app.agents import relevance_agent, schedule_agent
from app.citation_format import (
    DEFAULT_FORMAT,
    SUPPORTED_CITE_STYLES,
    SUPPORTED_FORMATS,
    citation_formats_for,
    cite_style_names,
    effective_format,
    format_names,
    format_reference,
    reference_runs,
)
from app.writing_lang import DEFAULT_LANG

from .projects_common import (
    CitationConfirm,
    _BUSY_MESSAGE,
    _DONE_STATUSES,
    _GEN_BUSY_MESSAGE,
    _busy_task,
    _citations_key,
    _collect_ordered_sections,
    _gen_running,
    _lang_of,
    _status_after_change,
    _write_task,
)
from .projects_documents import _binding_doc_ids, _binding_entries

# 不带 prefix / tags：本 router 只经 projects.py 门面 include 对外，而 FastAPI 0.115
# 的 include_router 会把**宿主** router 的 prefix 与 tags 再各叠一遍 —— 这里若写
# prefix="/api" 会变成 /api/api/...，写 tags 会得到 ['projects', 'projects']。
# 两者都由门面统一提供。
router = APIRouter()


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
