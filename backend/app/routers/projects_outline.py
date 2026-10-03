"""项目路由 · 大纲与主题聚类域。

大纲生成 / 确认（人工确认点①）与文献主题聚类的生成 / 确认。聚类不是独立步骤，
就放在大纲步里（见下文段头注释）；确认大纲时作的废引用绑定与材料计划，
其「分家」事实由 projects_citations 的 _body_outline_mismatch 报出。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app import db, paper_types, tasks
from app.agents import cluster_agent, outline_agent
from app.llm import llm

from .projects_common import (
    ClustersConfirm,
    OutlineConfirm,
    _BUSY_MESSAGE,
    _GEN_BUSY_MESSAGE,
    _busy_task,
    _clusters_key,
    _collect_ordered_sections,
    _gen_running,
    _lang_of,
    _outline_key,
    _status_after_change,
    _sum_budget,
    _write_task,
)
from .projects_citations import _body_outline_mismatch
from .projects_documents import _design_context

# 不带 prefix / tags：本 router 只经 projects.py 门面 include 对外，而 FastAPI 0.115
# 的 include_router 会把**宿主** router 的 prefix 与 tags 再各叠一遍 —— 这里若写
# prefix="/api" 会变成 /api/api/...，写 tags 会得到 ['projects', 'projects']。
# 两者都由门面统一提供。
router = APIRouter()


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
