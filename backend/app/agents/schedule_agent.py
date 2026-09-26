"""引用调度 Agent：文献归属 + 确定性兜底 + 页码标注（**唯一不调用模型的模块**）。

核心目标：
1. 归属 —— 由 relevance_agent 按主题相关性判断「哪篇文献属于哪一节」。
2. 兜底 —— 判断不可用时 uniform_distribute 按上传顺序均匀铺开，仍是一条完整的路；
   判断可用时 fill_gaps 把它的缺口补齐（无遗漏、全章节覆盖、同章不重复）。
3. 确定性标注 —— 每篇文献的引用绑定到具体页码，可追溯。

本模块一次 LLM 调用都没有，也不该有：「每篇文献都进正文」「每节都有引用」这类承诺
必须由代码保证（理由见 fill_gaps），于是模型未配置 / 超时 / 输出不可解析时，整条流程
照常跑完，只是归属于谁由兜底算法给出。
"""
from __future__ import annotations


def _collect_sections(outline: dict) -> list[dict]:
    """收集大纲的所有可引用叶子章节。"""
    sections: list[dict] = []
    for ch in outline.get("chapters", []):
        for sec in ch.get("sections", []):
            if sec.get("subsections"):
                for sub in sec["subsections"]:
                    sections.append({"title": sub.get("title", ""), "word_budget": sub.get("word_budget", 0)})
            else:
                sections.append({"title": sec.get("title", ""), "word_budget": sec.get("word_budget", 0)})
    return sections


def uniform_distribute(outline: dict, documents: list[dict]) -> dict:
    """均匀打散算法：将文档均匀分配到章节。

    这是**归属的兜底**（模型判断不可用时）与相关性调度的**基线**，也保证了
    「不配置模型也能跑完整条流程」。

    策略：
    - 若文档数 >= 章节数：每章至少绑定 1 篇，多余文档归到引用负担最轻的章节。
    - 若文档数 < 章节数：每篇文档可绑定到多个章节，按轮询保证覆盖所有章节。
    返回：{section_title: [{doc_id, doc_title, page}]}
    """
    sections = _collect_sections(outline)
    n_sec = len(sections)
    n_doc = len(documents)

    if n_sec == 0 or n_doc == 0:
        return {}

    binding: dict[str, list[dict]] = {s["title"]: [] for s in sections}

    if n_doc >= n_sec:
        # 每章至少一篇，先保证覆盖
        for i, sec in enumerate(sections):
            doc = documents[i % n_doc]
            binding[sec["title"]].append(_make_binding(doc, doc["pages"][0]["page"] if doc.get("pages") else 1))
        # 剩余文档按字数权重分配到章节
        remaining = documents[n_sec:] if n_doc > n_sec else []
        weights = [max(1, s["word_budget"]) for s in sections]
        total_w = sum(weights)
        for j, doc in enumerate(remaining):
            # 轮询 + 权重结合：优先分配给引用负担较轻的章节
            idx = _pick_section(binding, sections, doc["id"])
            page = _pick_page(doc, j)
            binding[sections[idx]["title"]].append(_make_binding(doc, page))
    else:
        # 文档少，每篇绑定到多个章节（轮询），保证所有章节有引用
        for i, sec in enumerate(sections):
            doc = documents[i % n_doc]
            page = _pick_page(doc, i // n_doc)
            binding[sec["title"]].append(_make_binding(doc, page))

    return binding


def _pick_section(binding: dict, sections: list[dict], exclude_doc_id: str) -> int:
    """选择引用负担最轻的章节（且不重复绑定同一文档）。"""
    best_idx = 0
    best_load = float("inf")
    for i, sec in enumerate(sections):
        load = len(binding[sec["title"]])
        has_same = any(b["doc_id"] == exclude_doc_id for b in binding[sec["title"]])
        if has_same:
            continue
        if load < best_load:
            best_load = load
            best_idx = i
    return best_idx


def _pick_page(doc: dict, offset: int) -> int:
    pages = doc.get("pages") or []
    if not pages:
        return 1
    idx = offset % len(pages)
    return pages[idx]["page"]


def binding_title(doc: dict) -> str:
    """绑定条目里那份文献题名的**唯一**推导点。

    题名缺失时退回文件名，所以「题名清空」不等于「这条引用没名字」。

    之所以是公开函数而不是写在 `_make_binding` 里：文献改名时
    （`routers.projects.update_document`）要把绑定里那份**冻住**的 `doc_title`
    一起刷新，否则 `_plan_citations` 读到的还是旧题名（它优先取绑定里的），用户
    改完题名会看不出任何反应。两边必须写出同一个字符串，否则「编辑后重排」与
    「重新调度一次」会得到不同的文末列表 —— 那是两份实现，会漂移。
    """
    return doc.get("title") or doc.get("filename", "")


def _make_binding(doc: dict, page: int) -> dict:
    return {
        "doc_id": doc["id"],
        "doc_title": binding_title(doc),
        "page": page,
    }


def _least_used_doc(documents: list[dict], binding: dict) -> dict | None:
    """当前被铺到最少章节的那一篇（并列时取文档顺序靠前的）。

    覆盖回填时用：空章节需要一篇文献，挑「已经重复得最少」的那篇，别让某篇在每一节
    里都出现。并列取靠前的，是为了让结果只取决于入参、不取决于字典遍历顺序。
    """
    uses: dict[str, int] = {}
    for items in binding.values():
        for it in items:
            uses[it["doc_id"]] = uses.get(it["doc_id"], 0) + 1
    best: dict | None = None
    best_uses = 0
    for doc in documents:
        doc_id = doc.get("id")
        if not doc_id:
            continue
        n = uses.get(doc_id, 0)
        if best is None or n < best_uses:
            best, best_uses = doc, n
    return best


def fill_gaps(binding: dict, outline: dict, documents: list[dict]) -> dict:
    """把一份（可能残缺的）归属补到满足四条承诺。

    相关性判断（relevance_agent）给的只是「哪篇文献属于哪一节」，它可能漏掉文献、也
    可能让某一节一篇都没有。本函数只补缺口、不推翻判断：

    - 库里还没出现在任何章节的文献 → 归到当前引用负担最轻的一节（_pick_section）；
    - 还是空着的章节 → 把被铺得最少的那篇文献再铺过去（同一篇文献可以跨节出现，
      「无同章重复」讲的是**同一节内**不重复，跨节复用是既有的合法形态）；
    - 模型在同一节里重复给同一篇 → 只留第一次。

    **为什么「无遗漏」不由模型负责**：模型判定「这篇与本大纲无关」之后，本步界面上
    没有任何地方能把文献加回来（这一步没有绑定编辑器），那篇文献用户就再也用不上了。
    保留无遗漏，等于不让模型拥有一个**用户无法挽回**的决定权。

    模型给的 reason 会带到最终绑定里（有则带）：它是界面上唯一能说明「为什么绑在这里」
    的信息，丢了就只剩一个标题。

    返回的 dict **按大纲顺序建键**：界面逐节展示绑定，键序乱了读起来就对不上大纲。
    传空 binding 时它退化成 uniform_distribute 的形态（每节一篇 + 补满剩余）。
    """
    sections = _collect_sections(outline)
    if not sections:
        return {}
    if not documents:
        return {s["title"]: [] for s in sections}

    known = {d["id"]: d for d in documents if d.get("id")}
    result: dict[str, list[dict]] = {s["title"]: [] for s in sections}
    placed: set[str] = set()

    # 一、原样收下模型给的归属（丢掉库外的 doc_id、大纲外的标题、同节重复）
    for sec in sections:
        items = binding.get(sec["title"])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            doc_id = item.get("doc_id")
            if doc_id not in known or doc_id in placed:
                continue
            entry = _make_binding(known[doc_id], _pick_page(known[doc_id], len(placed)))
            reason = item.get("reason")
            if isinstance(reason, str) and reason.strip():
                entry["reason"] = reason.strip()
            result[sec["title"]].append(entry)
            placed.add(doc_id)

    # 二、无遗漏：库里还没归到任何一节的文献，按负担最轻归位
    for doc in documents:
        doc_id = doc.get("id")
        if not doc_id or doc_id in placed:
            continue
        idx = _pick_section(result, sections, doc_id)
        result[sections[idx]["title"]].append(_make_binding(doc, _pick_page(doc, len(placed))))
        placed.add(doc_id)

    # 三、全章节覆盖：仍然空着的章节，复用一篇已归位的文献
    for i, sec in enumerate(sections):
        if result[sec["title"]]:
            continue
        doc = _least_used_doc(documents, result)
        if doc is None:
            break
        result[sec["title"]].append(_make_binding(doc, _pick_page(doc, i)))

    return result
