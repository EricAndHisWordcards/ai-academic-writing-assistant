"""材料分析 Agent：判断作者自有的研究材料该放进哪一节、如何融入。

与引用调度（schedule_agent）是两条并行的线：
- 引用调度把**别人的文献**确定性地绑到章节，产物进正文角标与文末参考文献列表；
- 材料分析把**作者自己的数据与成果**绑到章节，产物是一些写作指令（用哪份材料、
  放在哪个论点下、其中哪些数字必须出现），**不产生角标，也不进参考文献列表**。

刻意做成一次 LLM 调用通览全部材料后统一分配，而不是每份材料各自分析：只有同时
看到所有材料和全部章节，模型才可能做出「表 3 放 4.1 描述性统计、访谈记录放 4.3
结果讨论」这样的互斥安排。逐份分析会得到一堆彼此重复、都想去同一节的建议。
"""
from __future__ import annotations

from app.llm import llm
from app.material_parser import excerpt
from app.writing_lang import DEFAULT_LANG, output_rule

# 送入分析提示词时每份材料的摘录上限。分析要的是「这份材料里有什么」，
# 不是逐字精读；给全量正文只会烧掉上下文并挤掉章节清单。
ANALYZE_CHARS = 6000

ANALYZE_PROMPT = """你是学术写作指导专家。作者提供了一批**自己的一手研究材料**
（研究思路、实验数据、调研结果、访谈记录、图表说明等），请判断这些材料应该写入
论文的哪些章节、以及具体如何融入。

论文信息：
- 论文类型：{paper_type}
- 论文选题：{topic}
- 核心研究问题：{core_question}

论文大纲的各节标题（**只能从中选择**，不要自造章节）：
{sections}

作者提供的研究材料：
{materials}

分析要求：
1. 先把每份材料读明白：它提供的是什么（数据表 / 调研结论 / 案例 / 方法说明 / 观点）。
2. 再判断它回答了大纲里哪一节的写作任务。**材料必须落到具体的节**，不要笼统地说
   「可用于全文」。一份材料可以同时用于多节，但每节都要说清楚用途不同在哪。
3. `usage` 要写成**给撰稿人的可执行指令**，指明这份材料的哪个部分支撑这一节的哪个
   论点。反例（不合格）：「结合调研数据分析现状」。正例（合格）：「用问卷的 312 份
   有效样本与 92% 回收率交代样本构成，并说明样本在行业分布上偏向制造业，作为本节
   讨论外部效度的依据」。
4. `key_points` 逐条列出**必须出现在正文里的具体事实与数字**（样本量、系数、百分比、
   年份、案例名称）。这是本节写作时不能丢的信息，宁可少而准，不要含糊概括。
5. 与本研究核心问题无关的材料，放进 `unused` 并说明原因 —— 硬塞进去只会稀释论文。
6. 材料里若有前后矛盾或明显不足（如只有描述统计却要下因果结论），在 `notes` 里指出来。

输出格式（仅 JSON，不要任何其他文字）：
{{
  "notes": "材料使用策略的一句话总述；若有数据缺陷也在此说明",
  "placements": [
    {{"section_title": "必须与上面给出的某一节标题完全一致",
      "material_ids": ["材料 id"],
      "usage": "这一节该怎么用这份材料",
      "key_points": ["必须出现的事实或数字"]}}
  ],
  "unused": [{{"material_id": "材料 id", "reason": "为什么不建议使用"}}]
}}
{output_rule}"""


async def analyze_materials(
    materials: list[dict],
    sections: list[str],
    paper_type: str,
    topic: str,
    core_question: str = "",
    writing_lang: str = DEFAULT_LANG,
) -> dict:
    """分析材料如何融入正文。

    返回 {"notes": str, "placements": [...], "unused": [...]}。
    LLM 未配置或输出不可用时返回空计划 —— 生成环节对空计划是安全的（等同于
    「不使用自有材料」），不因此中断整条流程。

    writing_lang 决定 `usage` 与 `key_points` 的语言：`key_points` 是「必须出现在正文里
    的具体事实与数字」，逐条注入每一节的正文提示词，中文的要点会诱着模型在英文正文里
    写中文（材料本身的原文照旧原样进提示词，那部分不翻译）。
    """
    empty = {"notes": "", "placements": [], "unused": []}
    if not materials or not sections:
        return empty
    if not llm.is_configured:
        return empty

    prompt = ANALYZE_PROMPT.format(
        paper_type=paper_type or "（未指定）",
        topic=topic or "（未指定）",
        core_question=core_question or "（未明确给出，请从选题推断）",
        sections="\n".join(f"  - {s}" for s in sections),
        materials=_format_materials(materials),
        output_rule=output_rule(writing_lang),
    )
    data = await llm.chat_json([{"role": "user", "content": prompt}])
    if not isinstance(data, dict):
        return empty

    plan = _parse_plan(data, materials, sections)
    plan["notes"] = data.get("notes", "") if isinstance(data.get("notes"), str) else ""
    return plan


def _format_materials(materials: list[dict]) -> str:
    parts = []
    for m in materials:
        label = m.get("label") or "（未命名材料）"
        kind = "直接粘贴的文本" if m.get("kind") == "text" else "上传的文件"
        parts.append(
            f"【材料 id={m['id']}】{label}（{kind}）\n{excerpt(m.get('text') or '', ANALYZE_CHARS)}"
        )
    return "\n\n".join(parts)


def _parse_plan(data: dict, materials: list[dict], sections: list[str]) -> dict:
    """校验模型的分配结果。

    `section_title` 必须命中真实章节 —— 否则生成时按标题取用会永远取不到，用户看到
    的却是一份「已分析」的计划，这种静默失配比分析失败更难排查。`material_ids`
    同理，且必须去重。
    """
    known_ids = {m["id"] for m in materials}
    valid_titles = set(sections)
    placements = []
    for item in data.get("placements") or []:
        if not isinstance(item, dict):
            continue
        title = item.get("section_title")
        if not isinstance(title, str) or title not in valid_titles:
            continue
        ids = []
        for mid in item.get("material_ids") or []:
            if isinstance(mid, str) and mid in known_ids and mid not in ids:
                ids.append(mid)
        if not ids:
            continue
        points = [
            p.strip() for p in (item.get("key_points") or [])
            if isinstance(p, str) and p.strip()
        ]
        placements.append({
            "section_title": title,
            "material_ids": ids,
            "usage": item.get("usage", "") if isinstance(item.get("usage"), str) else "",
            "key_points": points,
        })

    used = {mid for p in placements for mid in p["material_ids"]}
    unused = []
    reported: dict[str, str] = {}
    for item in data.get("unused") or []:
        if isinstance(item, dict) and isinstance(item.get("material_id"), str):
            reason = item.get("reason")
            reported[item["material_id"]] = reason if isinstance(reason, str) else ""

    for m in materials:
        if m["id"] in used:
            continue
        unused.append({
            "material_id": m["id"],
            "label": m.get("label", ""),
            "reason": reported.get(m["id"]) or "模型未给出使用建议",
        })

    return {"placements": placements, "unused": unused}
