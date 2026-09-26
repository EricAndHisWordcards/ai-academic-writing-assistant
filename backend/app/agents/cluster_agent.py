"""文献主题聚类 Agent：把上传的文献归纳成若干研究主题维度。

服务的是**结构**而不是正文：文献综述的章节主题本就该从文献里长出来，而不是让
模型对着一个空题目凭空拟。与引用调度（谁的文献绑到哪一节）、材料分析（作者自己
的数据放进哪一节）并列的第三条线，产物去向不同——它进的是**大纲提示词**。

用户要的不是「提示词里多写一句」，而是一份**能看、能改、能确认的中间产物**：
结果落盘到 projects.clusters_json，在界面上逐簇展示、允许改名，作者确认后才进
大纲提示词，且确认过的簇带**优先级**（高于模型自己的归纳）。

**按文献 id 归类，不按章节标题归类**：章节标题会被字数分配那一遍改写
（见 outline_agent._parse_retitles），一旦拿它当键，聚类就会静默失配。
"""
from __future__ import annotations

from app.agents.outline_agent import build_literature_context
from app.llm import llm
from app.writing_lang import DEFAULT_LANG, cap_chars, output_rule

# 送入提示词的文献篇数上限，与大纲那边保持同一个量级。
MAX_DOCS = 30
# 簇数下限与上限。下限 2：一个主题的「综述」没有归纳可言；上限 6：再多就无法
# 逐簇对应到骨架的主题章（文献综述的主题章只有 3 节）。
MIN_CLUSTERS = 2
MAX_CLUSTERS = 6
# 簇标题长度上限（中文），与 outline_agent._clean_title 同一把尺子、同样按语言
# 放宽：这些标题会直接成为备选节标题，英文下一条正常的主题维度名（十来个词）
# 轻易超过 60 个字符，卡在原地上就会把整簇丢掉。
MAX_TITLE_CHARS = 60

CLUSTER_PROMPT = """你是文献综述的主题分析专家。下面是一位作者为综述上传的文献清单，
请把它们归纳成若干**研究主题维度**。

论文信息：
- 论文类型：{paper_type}
- 论文选题：{topic}
- 核心研究问题：{core_question}

已上传文献：
{documents}

要求：
1. 归纳出**{count_text}**。每个维度要能回答核心研究问题的一个侧面，维度之间彼此
   并列、同属一个层次，合起来构成一个完整的回答。
2. **不要把某一篇文献自己的行业、技术、情境或案例当作主题维度** —— 它与那篇文献
   相关，不等于它与核心研究问题相关。维度名要是**学术议题**（例如「促进与抑制
   效应的并存」），不是某篇文献标题的缩写，也不是它的研究场景。
3. 每个维度都要说明它涵盖哪些文献。`doc_ids` 只填上面**【id=…】里给出的 id**，
   不要自己编号、不要臆造 id。**每一篇文献都至少要归入一个维度**；确实与核心问题
   无关的才不归入，并在 `unassigned` 里说明原因。
4. `summary` 用一两句话说明这个维度在讨论什么、内部有哪些分歧或演进。
5. `key_points` 逐条列出该维度下**值得写进综述的具体发现或观点**（可带作者或年份），
   供撰稿人使用；宁可少而准，不要空泛概括。
6. 维度名不超过 40 字。

输出格式（仅 JSON，不要任何其他文字）：
{{
  "notes": "这批文献整体格局的一句话总述",
  "clusters": [
    {{"title": "主题维度的名称",
      "summary": "这个维度在讨论什么",
      "doc_ids": ["d1", "d3"],
      "key_points": ["具体发现或观点"]}}
  ],
  "unassigned": [{{"doc_id": "d7", "reason": "为什么不归入任何维度"}}]
}}
{output_rule}"""


async def cluster_documents(
    documents: list[dict],
    topic: str,
    paper_type: str,
    core_question: str = "",
    target_clusters: int = 0,
    writing_lang: str = DEFAULT_LANG,
) -> dict:
    """把文献归纳成主题维度。

    返回 {"clusters": [...], "notes": str, "unassigned": [...]}。
    **任何一步拿不到可用结果都返回空 clusters**，而不是抛异常或返回半份结果：
    调用方据此把任务标成失败（「模型没有归纳出任何主题聚类」），用户看到的才不会是
    一句「已聚类」盖着一份没变的东西。LLM 未配置同理。

    target_clusters 是骨架里主题章能容纳的节数（由调用方查 paper_types 得到）；
    给了就要求模型恰好给出这么多，0 表示不指定、按 2–6 自由归纳。

    writing_lang 决定簇标题用哪种语言（它们会成为文献综述的备选节标题，是产物文字）、
    以及簇标题的长度上限（英文放宽三倍）。追加在末尾且默认中文，既有调用不受影响。
    """
    empty = {"clusters": [], "notes": "", "unassigned": []}
    if not documents:
        return empty
    if not llm.is_configured:
        return empty

    shown = documents[:MAX_DOCS]
    if target_clusters and target_clusters >= MIN_CLUSTERS:
        count_text = f"恰好 {target_clusters} 个研究主题维度"
    else:
        count_text = f"{MIN_CLUSTERS}–{MAX_CLUSTERS} 个研究主题维度"

    prompt = CLUSTER_PROMPT.format(
        paper_type=paper_type or "（未指定）",
        topic=topic or "（未指定）",
        core_question=core_question or "（未明确给出，请从选题推断）",
        documents=build_literature_context(shown, with_ids=True),
        count_text=count_text,
        output_rule=output_rule(writing_lang),
    )
    data = await llm.chat_json([{"role": "user", "content": prompt}])
    if not isinstance(data, dict):
        return empty

    plan = _parse_clusters(data, documents, shown_count=len(shown), lang=writing_lang)
    plan["notes"] = data.get("notes", "") if isinstance(data.get("notes"), str) else ""
    return plan


def _doc_title(d: dict) -> str:
    return (d.get("title") or d.get("filename") or "（未命名文献）").strip()


def _parse_clusters(
    data: dict,
    documents: list[dict],
    shown_count: int,
    lang: str = DEFAULT_LANG,
) -> dict:
    """校验模型给的聚类。

    `doc_ids` 必须落在**实际文献集合**内，模型编出来的 id 一律丢弃 —— 这不是格式
    问题，而是「聚类里出现一篇不存在的文献」：界面上会显示一篇查无此文的文献，
    大纲也会照着一个不存在的主题去组织章节。

    `unassigned` 由**服务端算**，不采信模型的版本：只要有一篇文献没被任何簇覆盖
    就列出来。否则模型悄悄漏掉一半文献，用户看到的结果与「全都归好了」毫无区别。

    标题上限走 `cap_chars`（= outline_agent._clean_title 的同一把尺子），按语言放宽：
    这里一旦多丢一簇，界面与提示词里就少一个主题维度，而大纲那边什么都不会报。
    """
    max_title = cap_chars(lang, MAX_TITLE_CHARS)
    known = {d["id"]: _doc_title(d) for d in documents if d.get("id")}
    clusters: list[dict] = []
    for item in data.get("clusters") or []:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        title = title.strip() if isinstance(title, str) else ""
        if not title or len(title) > max_title:
            continue
        ids: list[str] = []
        for did in item.get("doc_ids") or []:
            if isinstance(did, str) and did in known and did not in ids:
                ids.append(did)
        if not ids:
            # 一个簇名下没有任何真实文献，等于空壳
            continue
        clusters.append({
            "id": f"c{len(clusters) + 1}",
            "title": title,
            "summary": item.get("summary", "") if isinstance(item.get("summary"), str) else "",
            "doc_ids": ids,
            "doc_titles": [known[i] for i in ids],
            "key_points": [
                p.strip() for p in (item.get("key_points") or [])
                if isinstance(p, str) and p.strip()
            ],
        })
        if len(clusters) >= MAX_CLUSTERS:
            break

    used = {did for c in clusters for did in c["doc_ids"]}
    reported: dict[str, str] = {}
    for item in data.get("unassigned") or []:
        if isinstance(item, dict) and isinstance(item.get("doc_id"), str):
            reason = item.get("reason")
            reported[item["doc_id"]] = reason if isinstance(reason, str) else ""

    unassigned = []
    for i, d in enumerate(documents):
        did = d.get("id")
        if not did or did in used:
            continue
        if i >= shown_count:
            # 这些文献压根没进提示词，说「模型未归入」是假话
            reason = f"超出本次分析范围（每次最多分析 {shown_count} 篇）"
        else:
            reason = reported.get(did) or "模型未归入任何主题"
        unassigned.append({"doc_id": did, "title": _doc_title(d), "reason": reason})

    return {"clusters": clusters, "notes": "", "unassigned": unassigned}
