"""项目路由 · 文献与材料与研究设计域。

文献上传 / 解析 / 列表 / 编辑 / 删除、作者自有材料的上传 / 分析、研究设计的
保存 / 提炼。文献解析跑在后台任务里（逐篇入库）；材料与设计不走后台任务
（见下方各组头部注释）。
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, File, HTTPException, UploadFile

from app import db, material_parser, metadata, paper_types, tasks
from app.agents import design_agent, material_agent, schedule_agent
from app.agents.parse_agent import extract_metadata, merge_hard_wraps
from app.citation_format import SOURCE_TYPES
from app.llm import llm
from app.pdf_parser import extract_pdf_text

from .projects_common import (
    DOC_FIELD_LIMITS,
    DesignSave,
    DocumentPatch,
    MaterialText,
    _BUSY_MESSAGE,
    _GEN_BUSY_MESSAGE,
    _analysis_key,
    _busy_task,
    _collect_ordered_sections,
    _design_key,
    _documents_key,
    _gen_running,
    _lang_of,
    _write_task,
)

# 不带 prefix / tags：本 router 只经 projects.py 门面 include 对外，而 FastAPI 0.115
# 的 include_router 会把**宿主** router 的 prefix 与 tags 再各叠一遍 —— 这里若写
# prefix="/api" 会变成 /api/api/...，写 tags 会得到 ['projects', 'projects']。
# 两者都由门面统一提供。
router = APIRouter()


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
