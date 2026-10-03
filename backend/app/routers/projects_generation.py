"""项目路由 · 分段生成与导出回执域。

正文分段生成（点火 / 进度轮询 / 后台主循环 / 失败回退）与「已导出」状态回执。
引用编号分配与文末列表渲染取自 projects_citations，绑定条目与设计上下文取自
projects_documents；_GEN_TASKS 只有一个对象，定义在 projects_common。
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException

from app import db, paper_types
from app.agents import generate_agent
from app.agents.generate_agent import count_units
from app.agents.polish_agent import polish
from app.citation_format import DEFAULT_FORMAT, effective_format

from .projects_common import (
    _BUSY_MESSAGE,
    _GEN_BUSY_MESSAGE,
    _GEN_TASKS,
    _busy_task,
    _collect_ordered_sections,
    _eta,
    _find_page_snippet,
    _gen_running,
    _lang_of,
    _with_display_title,
    _words_of,
)
from .projects_citations import (
    _format_refs,
    _plan_citations,
    _reference_runs_of,
    _require_refs_consistent,
)
from .projects_documents import _binding_entries, _design_context

# 不带 prefix / tags：本 router 只经 projects.py 门面 include 对外，而 FastAPI 0.115
# 的 include_router 会把**宿主** router 的 prefix 与 tags 再各叠一遍 —— 这里若写
# prefix="/api" 会变成 /api/api/...，写 tags 会得到 ['projects', 'projects']。
# 两者都由门面统一提供。
router = APIRouter()


# ---------------------------------------------------------------
# 阶段五：分段生成（后台任务 + 进度轮询）
# ---------------------------------------------------------------
# 一次生成 = 串行几十次 LLM 调用，动辄数分钟，远超 nginx 的 300s 读超时。
# 所以 POST /generate 只「点火」后立即返回，真正的循环跑在 asyncio 后台任务里，
# 进度逐节落盘到 generation_state_json，前端轮询 /generate/progress 取用。
# 进度放在库里而非内存里，正是「刷新页面不丢进度」的前提。

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
