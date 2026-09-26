"""大纲 Agent：生成多级大纲树并按内容体量分配字数。

两遍生成，各司其职：

1. **结构与标题** —— 结构恒等于 paper_types.PAPER_TEMPLATES 的按类型骨架，LLM 只
   为每一节填写贴合选题的具体标题。这样论文类型不会跑偏，同一选题两次生成的
   结构也保持一致。
2. **字数分配** —— 单独一遍，专门让 LLM 按「这一节实际要承载多少内容」分配篇幅。

第 2 遍单列的理由：此前是让模型在同一次输出里既拟标题又猜字数，它只能挑一组
凑整的数字去命中总数，结果是文献综述的「结论与展望」与核心主题节一样长 —— 那是
AI 写作工具凑字数的「固定配额」套路，不是学术写作的篇幅逻辑。

**本模块里凡是「按字符数截断模型输出」的地方都走 `writing_lang.cap_chars`**：那些
上限是按中文定的（60 字、100 字），英文下会把一个正常的标题**截断在词中间**，而症状
只是「英文标题总是缺半句」，看不出与字数上限有关。骨架、类型说明、字数单位同理跟随
语言；提示词本身的措辞不跟随（见 app.writing_lang 的模块说明）。
"""
from __future__ import annotations

import statistics
from typing import Any, Callable

from app import paper_types
from app.llm import llm
from app.writing_lang import DEFAULT_LANG, cap_chars, output_rule, words_unit

OUTLINE_PROMPT = """你是学术论文大纲专家。请为下面的选题拟定一份多级大纲。

**第一步：先写下这篇论文唯一的核心研究问题。** 把选题收敛成一个可以被回答的问题，
一句话写进 core_question。全篇大纲都将围绕它组织。

**第二步：填写章节标题。** 必须严格采用下方给定的章节骨架：章与节的标题一一对应、
顺序一致，数量完全相同。不得增删、合并、重排章节，**也不要自行增设子节(subsection)**
——骨架里列出的每一节就是最终的最细粒度节点。

标题必须满足：

1. **全篇只围绕 core_question 展开**，不得引入与它无关的议题。
2. **不得把某一篇文献的具体行业、技术、情境或案例当成章节主题。** 例如选题若为
   「企业社交媒体与隐性知识共享的双刃剑效应」，就不能出现「零售4.0」「数字化转型」
   这类节标题——那是某篇文献自身的语境，只能作为正文里的论据，不能升格成章节。
3. **章节之间要能自然接续，连成一条链**：引言提出 core_question → 概念界定限定它的
   术语 → 主题脉络围绕它的维度组织文献 → 述评针对这些维度找不足 → 结论回答它。
4. **同一章内各节必须同属一个层次**，不要一节辨析概念、下一节跳到实证结果。
5. 【重要】下方骨架已按「{paper_type}」的学术规范选定，**严禁把其他论文类型的
   结构混入**。
{type_note}
6. 标题要具体贴题，能看出这一节要论证什么；不要用「本节将……」这类占位措辞。
7. **自检**：填完每个标题后问自己「这一节与 core_question 是什么关系」，
   讲不出关系的必须改。

严格输出 JSON，不要任何解释。

输入：
- 选题：{topic}
- 论文类型：{paper_type}

必须遵循的章节骨架（章数与节数均为最终结构）：
{skeleton}
{ideas_block}{design_block}{literature_block}
输出 JSON 结构（仅 JSON）：
{{
  "core_question": "企业社交媒体的使用如何影响隐性知识共享，其双刃剑效应通过什么机制产生？",
  "chapters": [
    {{
      "title": "第一章 引言",
      "sections": [
        {{"title": "1.1 研究背景", "subsections": []}}
      ]
    }}
  ]
}}
注意：subsections 一律留空数组。
{output_rule}"""

# 模型输出里几处按**字符数**定的上限。中文下的取值就是原来的数字（逐字节不变），
# 英文下经 cap_chars 放宽三倍。
#
# **提示词与解析侧必须同源**：提示词写「不超过 60 字」而解析侧按 180 字符截断，
# 模型会照 60 写，放宽等于白放；反过来提示词放宽而解析侧没收宽，模型写满的标题
# 会被切掉尾巴。所以两边读同一个常量。
_RATIONALE_CAP_ZH = 30       # 提示词里「每节的依据」长度
_RETITLE_CAP_ZH = 40         # 提示词里修订标题的长度
_TITLE_CAP_ZH = 60           # 采纳模型标题的上限（_clean_title 与 _parse_retitles 共用）
_CORE_QUESTION_CAP_ZH = 100  # 核心研究问题
_RATIONALE_KEEP_ZH = 60      # 依据落库前再截一次（界面上只显示一行）

# 第二遍：专做字数分配。这里给的「依据」不是修辞——它逼模型按内容体量推算，
# 而不是拿总数除以节数。
#
# **这一遍也要带 `{output_rule}`**（第一遍的 OUTLINE_PROMPT 早就带了）：它不只算字数，
# 还产出 `retitles`，而那批标题会被 _apply_allocations 覆盖到叶子上、**印进论文**。
# 漏掉的话，英文项目会在这里被改出中文节标题（模型只看到中文指令，就按中文回话）。
# 占位符紧跟在 `{literature_block}` 后面、**不另起一行**：output_rule 自己以 "\n\n"
# 开头，而中文返回空串 —— 同一行的写法让中文提示词逐字节不变（另起一行会多一个
# 尾部换行）。
#
# **首行必须保持「你是学术论文的篇幅规划专家」原样**：tests/test_outline_agent.py 的
# `_stub_llm` 按这个中文串分发提示词，约四十个用例走这条路。改它等于让那些用例
# 一起走错分支 —— 而它们全都会「通过」，只是通过的是另一条路。
ALLOCATION_PROMPT = """你是学术论文的篇幅规划专家。下面是一份**已定稿的论文大纲**，请完成两件事：
为每一节分配正文写作字数（全篇合计 **{target_words} {word_unit}**），并复核标题是否偏离主线。

分配的依据是「这一节实际需要承载多少内容」，具体看：
- 该节需要综述或引证的**文献数量与分量**（覆盖文献多、结论有分歧的节需要更大篇幅）；
- 该节论点本身的**复杂度与论证链条长度**；
- 该节在全文论证中的**地位**：核心论证章节应显著重于铺垫章节与收束章节。

必须遵守：
1. **禁止均分，也禁止凑整。** 不要给出 200、300、500 这类齐整数字——那是在凑总数，
   不是在规划篇幅。请依据上面三条推算到具体数值（例如 613、487、742）。
2. **「结论与展望」这类收束性章节不得成为全文最长的一节。** 结论只需回应前文已论证
   的论点，不展开新内容。
3. 以下学术写作常规比例可作参照（这是规范，不是配额，不必严格命中）：
   核心论证章节合计一般不低于全文一半；铺垫与收束章节合计一般不超过三成。
4. 每一节都要给出，不得遗漏；index 与下面清单的编号逐条对应。
5. 每节附一句不超过 {rationale_cap} 字的依据，说明「为什么是这么多」。

复核标题（第二件事）：
6. 逐个检查每节标题与**核心研究问题**的关系。若某节已经跑偏——最典型的是把某篇文献
   自身的行业、技术或情境当成了综述主题——就在 retitles 里给出修订后的标题（不超过 {retitle_cap} 字）。
   **没有跑偏的节不要出现在 retitles 里**；全部标题都贴题时给空数组。

输出 JSON（仅 JSON）：
{{
  "allocations": [
    {{"index": 1, "words": 218, "rationale": "只交代研究背景，不展开论证"}}
  ],
  "retitles": [
    {{"index": 6, "title": "修订后的贴题标题"}}
  ]
}}

输入：
- 选题：{topic}
- 论文类型：{paper_type}
- 核心研究问题：{core_question}
- 目标总字数：{target_words} {word_unit}（各节之和须等于此数）

大纲清单（index 即你要回填的编号）：
{menu}
{literature_block}{output_rule}"""


async def generate_outline(
    topic: str,
    paper_type: str,
    target_words: int,
    literature_context: str = "",
    document_count: int = 0,
    on_phase: Callable[[str, dict], None] | None = None,
    design_context: str = "",
    design_label: str = "",
    clusters: list[dict] | None = None,
    theme_sections: int = 0,
    writing_ideas: str = "",
    writing_lang: str = DEFAULT_LANG,
) -> dict:
    """生成大纲树并分配字数。

    结构恒等于该论文类型的骨架；LLM 负责填标题措辞，并按内容体量分配字数。
    未配置 LLM 时直接用骨架模板。

    writing_lang 决定骨架取哪张表、类型说明取哪一份、字数用什么单位、以及几处字符
    上限放宽到多少（见模块 docstring）。追加在末尾且默认中文（`DEFAULT_LANG`），
    既有调用与断言因此不受影响。

    design_context 是作者已给出的研究设计 / 技术方案（由调用方按字段拼好），
    实证研究不先给这个，第四章「实证分析」就只能靠编。

    clusters 是作者**已确认**的主题聚类（仅文献综述），theme_sections 是骨架里
    主题章能容纳的节数。二者一起进第一遍，且只进第一遍。

    writing_ideas 是作者写下的写作思路 / 论证思路（选题那一步填的）。它同样只进
    第一遍：第一遍要写 core_question，而作者写了思路时，那句话就不再由模型自己定。


    on_phase 在两遍的边界被回调（"structure" → "allocating"），供调用方落盘进度。
    它只做通知，agent 本身不碰数据库；回调抛错也不能让生成失败（进度是尽力而为）。

    **新参数一律追加在末尾**：测试里存在按位置传参的调用
    （test_outline_agent._outline 把 on_phase 传在第 6 位），插在中间会静默错位。
    """

    def report(phase: str, info: dict | None = None) -> None:
        if on_phase is None:
            return
        try:
            on_phase(phase, info or {})
        except Exception:
            pass

    if not llm.is_configured:
        return _fallback_outline(paper_type, target_words, writing_lang)

    # 第一遍：结构 + 标题。这一遍失败要如实抛出——它一失败就意味着 LLM 路径
    # 整体不可用（如 key 配错），此时静默返回通用骨架只会掩盖问题。
    report("structure")
    prompt = OUTLINE_PROMPT.format(
        topic=topic,
        paper_type=paper_types.label_for(paper_type),
        type_note=_type_note_block(
            paper_types.structure_note_for(paper_type, writing_lang)
        ),
        skeleton="\n".join(paper_types.skeleton_lines(paper_type, writing_lang)),
        ideas_block=_ideas_block(writing_ideas),
        design_block=_design_block(design_context, design_label),
        literature_block=_literature_block(
            literature_context, document_count,
            clusters=clusters, theme_sections=theme_sections,
        ),
        output_rule=output_rule(writing_lang),
    )
    data = await llm.chat_json([{"role": "user", "content": prompt}])
    if not isinstance(data, dict):
        return _fallback_outline(paper_type, target_words, writing_lang)

    # 结构由骨架保证；LLM 输出的缺漏/多余都在 _merge_with_skeleton 内部就地兜住
    structure = _merge_with_skeleton(data, paper_type, target_words, writing_lang)

    # 第二遍：字数分配 + 标题复核。必须串行——它要读到定稿后的标题才谈得上
    # 「按内容分配」。这一遍失败只该让字数退回骨架比例，不该让整个大纲生成失败。
    leaves = _iter_leaves(structure)
    report("allocating", {
        "chapters": len(structure["chapters"]),
        "sections": len(leaves),
    })
    allocations, retitles = await _allocate_words(
        structure, topic, paper_type, target_words, literature_context, document_count,
        writing_lang,
    )
    _apply_allocations(structure, allocations, paper_type, target_words, retitles)
    return structure


def _type_note_block(note: str) -> str:
    """把该论文类型的「该有什么章节、绝不许出现什么章节」包成提示词里的一段。

    空串（未知类型、或类型没配这一项）时返回 ""，整段消失。
    为什么需要逐类写：七类共用一句「严禁混入其他类型的结构」是不够的——模型知道
    「实证论文长什么样」，却不知道「访谈论文不该有假设检验」。必须逐类给正反两面，
    且禁例要落到具体章节名上。
    """
    if not note or not note.strip():
        return ""
    return f"   {note.strip()}\n"


def _clusters_block(clusters: list[dict], theme_sections: int = 0) -> str:
    """把**作者已确认**的主题聚类包成提示词里的一段。

    措辞里带明确的优先级（「优先于你自己的归纳」），与 _design_block 的
    「作者本人的事实优先于任何文献结论」是同一套说法：两者的正当性来自同一处——
    这是作者确认过的东西，不是模型的推断。

    簇数与节数不一致时必须给出**具体怎么办**，否则模型要么强行塞进去、要么自己
    另立主题，两种都不是作者确认的结果。
    """
    items = [c for c in (clusters or []) if isinstance(c, dict) and c.get("title")]
    if not items:
        return ""
    n = len(items)
    lines = ["\n**作者已确认的主题聚类（优先于你自己的归纳）**："]
    for i, c in enumerate(items, 1):
        lines.append(f"{i}. {c['title']}")
        summary = (c.get("summary") or "").strip()
        if summary:
            lines.append(f"   概述：{summary}")
        points = [p for p in (c.get("key_points") or []) if isinstance(p, str) and p.strip()]
        for p in points[:5]:
            lines.append(f"   - {p.strip()}")

    if not theme_sections:
        mapping = "请让主题类章节对应上面的维度，一个维度一节。"
    elif n == theme_sections:
        mapping = (
            f"骨架中主题类章节正好有 {n} 节，**一簇对应一节**，逐簇采用上面的维度名。"
        )
    elif n > theme_sections:
        mapping = (
            f"骨架中主题类章节只有 {theme_sections} 节，而上面有 {n} 个维度："
            f"请把关系最近的维度合并，最终仍然是 {theme_sections} 节。"
        )
    else:
        mapping = (
            f"骨架中主题类章节有 {theme_sections} 节，而上面只有 {n} 个维度："
            f"请把内容最丰富的维度拆成多节，最终仍然是 {theme_sections} 节。"
        )
    lines.append(mapping)
    lines.append(
        "作者已逐条确认过这些维度，**不要另立一个不在其中的主题**；"
        "维度名可以直接用作节标题，也可以在不改变含义的前提下写得更贴题。"
    )
    return "\n".join(lines) + "\n"


def _ideas_block(writing_ideas: str) -> str:
    """把作者写下的写作思路 / 论证思路包成提示词里的一段；没填时返回空串。

    规则写在这里而不是 OUTLINE_PROMPT 里，是为了让「没填思路」时整段彻底消失 ——
    提示词逐字节不变，既有的提示词断言与实测基线才不会被搅动（与 _design_block 同）。

    为什么它压过 core_question：选题只是一个标题，装不下「打算怎么论证」。第一遍本来
    就要求模型自己写下一句 core_question —— 那一步此前是**替作者定主线**。作者既然写下
    了思路，这件事就该由作者说了算，模型的职责退回到「把这段思路收敛成一句话」。

    只在第一遍用：第二遍做的是字数分配与标题复核，而它复核所依据的 core_question
    已经在第一遍里吸收了这段思路（与设计块、聚类块同一条纪律）。
    """
    if not writing_ideas or not writing_ideas.strip():
        return ""
    return (
        f"\n作者本人写下的写作思路与论证思路"
        f"（**作者本人的意图，优先于你自己的归纳**）：\n"
        f"{writing_ideas.strip()}\n"
        f"由此产生以下硬性约束：\n"
        f"1. 第一步的 core_question 必须**收敛上面这段思路**，不得另立一个与它无关的"
        f"主线；两者若有出入，以作者写下的这段为准。\n"
        f"2. 作者点到的论证角度与主题偏向要在节标题上体现出来：写到的侧面不能被漏掉，"
        f"没打算展开的方向也不要自己加上。\n"
        f"3. 章节骨架仍然严格照下方给定：这段思路只决定「各节论证什么」，"
        f"不改变章数与节数，也不得增设子节。\n"
    )


def _design_block(design_context: str, design_label: str = "") -> str:
    """把作者已给出的设计字段（研究设计 / 质性设计 / 工程设计 / 理论模型 / 技术方案）
    包成提示词里的一段；无设计时返回空串。

    规则写在这里而不是 OUTLINE_PROMPT 里，是为了让「没有设计」时整段彻底消失 ——
    提示词逐字节不变，既有的提示词断言与实测基线才不会被搅动。

    为什么必须硬约束：分析类章节（「实证分析」「资料分析与发现」「系统实现」「推导与
    证明」）原本的输入只有选题、类型名与骨架，模型只能编——编变量、编编码、编证明，
    且这份虚构在确认点①上看起来完全合理，会一路带进正文。给了设计字段之后，这些章节
    的写作依据就换成作者自己的方法与结果。

    第 1 条的节关键词必须**覆盖五类各自的分析章**：「编码」「推导」「实现」是质性研究、
    理论推导与工程设计的分析章标题里真正会出现的词；只列「分析/结果/检验」的话，
    规则对这三类形同虚设。
    """
    if not design_context.strip():
        return ""
    label = design_label or "研究设计"
    return (
        f"\n作者已给出的{label}（**作者本人的事实，优先于任何文献结论**）：\n"
        f"{design_context}\n"
        f"由此产生以下硬性约束：\n"
        f"1. 「分析」「结果」「检验」「测试」「验证」「推导」「证明」「编码」「实现」"
        f"一类的节，其标题与论证**只能依据**上面给出的内容，严禁编造数据、变量与模型、"
        f"显著性水平、访谈原话、定理或证明、性能指标或结论。\n"
        f"2. 上面未覆盖、而该节又确实需要的部分，请在标题末尾如实标出「需作者补充」，"
        f"不要补一个看起来合理的假结果。\n"
        f"3. 与上面冲突的文献结论不得写进这些节：文献只能用来对照与解释，"
        f"不能用来替换作者自己的结果。\n"
    )


def _literature_block(literature_context: str, document_count: int, sizing: bool = False,
                      clusters: list[dict] | None = None,
                      theme_sections: int = 0) -> str:
    """把文献清单包成提示词里的一段；无文献时返回空串。

    sizing=True 用于字数分配：各主题下文献的实际分量，是「按内容体量分配」唯一
    可靠的客观信号。sizing=False 用于拟标题：只让模型归纳出与核心研究问题相关的
    主题维度，**不得**把单篇文献的行业/技术/案例情境升格成综述主题。

    clusters 非空（作者已确认聚类）时，**取代**「归纳 2–4 个主题维度」那句，而不是
    与之并列：两句同时在场等于给了模型两个互相矛盾的指令，它会挑对自己更省事的那个
    （自己重新归纳），作者确认的聚类就白确认了。
    """
    if not literature_context.strip():
        return ""
    if sizing:
        # 第二遍看不到聚类：它只管篇幅与贴题。与设计块同一条纪律
        # （见 test_allocation_prompt_gets_no_design_block）。
        lead = (
            f"\n已上传文献（共 {document_count} 篇）。请据此判断每一节实际要覆盖的文献量：\n"
            f"覆盖文献多、结论有分歧的小节篇幅应更大，只有零星文献支撑的小节应更小。\n"
        )
    else:
        headline = f"\n已上传文献（共 {document_count} 篇）。"
        # 这里曾经写的是「把节标题替换为从这些文献中归纳出的具体主题名」。那等于
        # 邀请模型把某一篇文献自己的窄情境（行业、技术、案例）提升成综述主题，
        # 实测产出了「3.2 数字化转型与零售4.0语境下……」这类与核心问题无关的节。
        caution = (
            f"**不要把某篇文献的具体行业、技术、情境或案例当作主题维度** —— 它与那篇文献\n"
            f"相关，不等于它与核心研究问题相关；这些具体情境只能作为正文里的论据。\n"
        )
        cluster_block = _clusters_block(clusters or [], theme_sections)
        if cluster_block:
            lead = headline + "\n" + cluster_block + caution
        else:
            lead = (
                f"{headline}请从中归纳出 **2–4 个与核心研究问题\n"
                f"直接相关的主题维度**，用它们作为「研究主题脉络」各节的标题。\n"
                f"{caution}"
                f"各主题维度之间必须并列、同属一个层次，能共同构成对该核心问题的回答。\n"
            )
    return lead + literature_context + "\n"


def build_literature_context(documents: list[dict], max_docs: int = 30,
                             with_ids: bool = False) -> str:
    """把已解析文献压成供大纲生成使用的「文献主题清单」。

    优先用 parse_agent 提取的摘要（≤50 字，语义密度最高）；摘要缺失时退回
    首页片段。总量按 max_docs 截断，保证提示词有界。

    with_ids=True 时给每篇加上「【id=…】」前缀。只有要让模型**回填文献 id**
    的调用方才需要（主题聚类）——拟标题那条路只要标题，多给 id 是噪声。
    做成开关而不是第二份格式化函数：摘要回退策略与截断策略只该有一处实现，
    两份迟早会漂移。
    """
    if not documents:
        return ""
    lines: list[str] = []
    for i, d in enumerate(documents[:max_docs], 1):
        title = (d.get("title") or d.get("filename") or "").strip()
        prefix = f"【id={d['id']}】" if with_ids and d.get("id") else f"{i}. "
        parts = [f"{prefix}{title}"]
        meta = " · ".join(
            str(x).strip()
            for x in (d.get("authors"), d.get("year"), d.get("source"))
            if x and str(x).strip()
        )
        if meta:
            parts.append(f"   {meta}")
        summary = (d.get("summary") or "").strip()
        if not summary:
            pages = d.get("pages") or []
            if pages:
                summary = (pages[0].get("snippet") or "").strip()[:200]
        if summary:
            parts.append(f"   摘要：{summary}")
        lines.append("\n".join(parts))

    text = "\n".join(lines)
    rest = len(documents) - max_docs
    if rest > 0:
        text += f"\n（另有 {rest} 篇文献未列出）"
    return text


# ---------------------------------------------------------------
# 第一遍：结构 + 标题
# ---------------------------------------------------------------
def _merge_with_skeleton(
    llm_data: dict,
    paper_type: str,
    target_words: int,
    writing_lang: str = DEFAULT_LANG,
) -> dict:
    """以骨架结构为准，采纳 LLM 给出的标题措辞。

    遍历骨架、按 (章序号, 节序号) 到 LLM 输出里取值 —— 而不是先校验「LLM 是否
    守约束」。这样骨架**永远**赢，而 LLM 的产出能被最大程度地用上：

    - 它多写了章节、或自作主张加了子节 —— 结构仍等于骨架，但我们照旧读到它的
      章标题与节标题（子节被忽略，骨架的节就是最细粒度）；
    - 它少写了章节、或某些字段是垃圾 —— 缺的位置回落到骨架标题与骨架比例；
    - 它整体返回垃圾 —— 结果恒等于 _fallback_outline。

    此前是「结构不一致就整体回退」，实测 LLM 很爱加子节，于是它写的贴题标题
    （如「3.1 主题一：自动辅导系统的研究进展」）会被整批丢掉，只剩骨架的通用措辞。

    字数在这里只是按骨架比例播种；真正的分配由第二遍的 _apply_allocations 完成。
    """
    tmpl = paper_types.template_for(paper_type, writing_lang)
    llm_chapters = llm_data.get("chapters") or []

    chapters = []
    for ci, (ch_title, secs) in enumerate(tmpl):
        llm_ch = llm_chapters[ci] if ci < len(llm_chapters) else {}
        if not isinstance(llm_ch, dict):
            llm_ch = {}
        llm_secs = llm_ch.get("sections") or []

        ch_out = {
            "title": _clean_title(llm_ch.get("title"), ch_title, writing_lang),
            "sections": [],
        }
        for si, (sec_title, ratio) in enumerate(secs):
            llm_sec = llm_secs[si] if si < len(llm_secs) else {}
            if not isinstance(llm_sec, dict):
                llm_sec = {}
            ch_out["sections"].append({
                "title": _clean_title(llm_sec.get("title"), sec_title, writing_lang),
                "word_budget": _seed_budget(ratio, target_words),
                "rationale": "",
                "subsections": [],
            })
        chapters.append(ch_out)

    outline = {
        "core_question": _clean_core_question(
            llm_data.get("core_question"), writing_lang
        ),
        "chapters": chapters,
    }
    _rebalance(outline, target_words)
    return outline


def _clean_title(candidate, fallback: str, lang: Any = DEFAULT_LANG) -> str:
    """采纳 LLM 标题；为空/非字符串/明显过长时用骨架标题。

    上限按语言给（中文 60 字符、英文 180）：60 个字符只有约十个英文词，而英文标题
    常在十到十五词 —— 不加宽就会把标题截断在词中间，而症状只是「英文标题缺半句」。
    """
    if isinstance(candidate, str):
        title = candidate.strip()
        if title and len(title) <= cap_chars(lang, _TITLE_CAP_ZH):
            return title
    return fallback


def _clean_core_question(candidate, lang: Any = DEFAULT_LANG) -> str:
    """采纳模型写下的核心研究问题；不像样就留空，前端随即不显示这一行。"""
    if isinstance(candidate, str):
        text = candidate.strip()
        if text and len(text) <= cap_chars(lang, _CORE_QUESTION_CAP_ZH):
            return text
    return ""


# ---------------------------------------------------------------
# 第二遍：按内容体量分配字数
# ---------------------------------------------------------------
async def _allocate_words(
    structure: dict,
    topic: str,
    paper_type: str,
    target_words: int,
    literature_context: str,
    document_count: int,
    writing_lang: str = DEFAULT_LANG,
) -> tuple[dict[int, dict], dict[int, str]]:
    """让模型按内容体量分配字数，顺带复核标题是否偏离主线。

    返回 (分配, 修订标题)，都是「叶子序号(从 1 起) → 值」。拿不到有用结果时返回
    一对空 dict。本函数**不抛异常**：分配失败只该让字数退回骨架比例，不该让整个
    大纲生成失败。（asyncio.CancelledError 继承 BaseException，不会被吞掉。）

    复核折进这一遍而非单开第三次调用：这一遍已经读着全部定稿标题和核心研究问题，
    多问一句不增加往返延迟。
    """
    menu = _leaf_menu(structure)
    if not menu.strip():
        return {}, {}
    prompt = ALLOCATION_PROMPT.format(
        topic=topic,
        paper_type=paper_types.label_for(paper_type),
        core_question=structure.get("core_question") or "（未明确给出，请从选题推断）",
        target_words=target_words,
        word_unit=words_unit(writing_lang),
        rationale_cap=cap_chars(writing_lang, _RATIONALE_CAP_ZH),
        retitle_cap=cap_chars(writing_lang, _RETITLE_CAP_ZH),
        menu=menu,
        literature_block=_literature_block(literature_context, document_count, sizing=True),
        output_rule=output_rule(writing_lang),
    )
    try:
        data = await llm.chat_json([{"role": "user", "content": prompt}])
    except Exception:
        return {}, {}
    return _parse_allocations(data, writing_lang), _parse_retitles(data, writing_lang)


def _parse_allocations(data, lang: Any = DEFAULT_LANG) -> dict[int, dict]:
    """解析模型输出，逐条丢弃不可用的项 —— 不因为一条坏数据放弃整批。"""
    if not isinstance(data, dict):
        return {}
    raw = data.get("allocations")
    if not isinstance(raw, list):
        return {}
    out: dict[int, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
            words = int(round(float(item.get("words"))))
        except (TypeError, ValueError):
            continue
        if idx <= 0 or words <= 0:
            continue
        rationale = item.get("rationale")
        out[idx] = {
            "words": words,
            "rationale": (
                rationale.strip()[: cap_chars(lang, _RATIONALE_KEEP_ZH)]
                if isinstance(rationale, str)
                else ""
            ),
        }
    return out


def _parse_retitles(data, lang: Any = DEFAULT_LANG) -> dict[int, str]:
    """解析模型给出的标题修订建议；逐条校验，坏数据丢弃。

    用与 _clean_title 同一把尺子（非空、不超长），否则「修订」本身就成了新的跑偏 ——
    所以上限也从同一个常量取、同样按语言放宽。
    """
    if not isinstance(data, dict):
        return {}
    raw = data.get("retitles")
    if not isinstance(raw, list):
        return {}
    out: dict[int, str] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if idx <= 0:
            continue
        title = item.get("title")
        if isinstance(title, str) and 0 < len(title.strip()) <= cap_chars(
            lang, _TITLE_CAP_ZH
        ):
            out[idx] = title.strip()
    return out


# 单个字数的合理区间 —— 相对骨架种子值的倍数。区间**故意开得很宽**：
# 偏离骨架比例正是我们要的（骨架不知道这一节实际有多少文献要覆盖），它只用来
# 拦「给某一节 5000 字」这类离谱值，不构成配额。
_VALUE_BAND = (0.2, 5.0)

# 均分检测：整体离散系数低于此值，说明模型在均分（如一律 150 字）而非按内容分配。
# 旧实现用的是「偏离骨架比例就否决」，那会把我们想要的偏离一起否决掉；
# 这里反过来，只否决「几乎一样长」这一种退化。阈值取 0.05 而非更高，是为了
# 放过「有一定差异但不够悬殊」的正常结果 —— 只有近乎等长才判定为凑数。
_FLAT_CV = 0.05


def _apply_allocations(
    structure: dict,
    allocations: dict[int, dict],
    paper_type: str,
    target_words: int,
    retitles: dict[int, str] | None = None,
) -> None:
    """把模型的字数分配与标题修订写回叶子；退化输出退回骨架比例，永不整体失败。

    与 _merge_with_skeleton 同一种思路：逐项判断、就地兜底。模型给对 9 节、
    给错 2 节时，那 9 节应当被用上，而不是整批丢弃。

    标题修订与字数判定相互独立：字数被判为「均分凑数」而整批退回骨架时，贴题的
    标题修订照旧采纳 —— 那是两件事，不该一起否决。

    此处改标题是安全的：本函数只在大纲**生成**阶段跑，那时还没有 citation_binding_json，
    不存在改标题导致引用绑定失配的问题（那是 /outline/confirm 才需要管的，已有保护）。
    """
    leaves = _iter_leaves(structure)
    if not leaves:
        return

    for i, title in (retitles or {}).items():
        if 1 <= i <= len(leaves):
            leaves[i - 1]["title"] = title

    seeds = _seed_weights(leaves, paper_type, target_words)

    words: list[int | None] = [None] * len(leaves)
    reasons: list[str] = [""] * len(leaves)
    low, high = _VALUE_BAND
    for i, seed in enumerate(seeds):
        item = allocations.get(i + 1)
        if not item:
            continue
        if seed * low <= item["words"] <= seed * high:
            words[i] = item["words"]
            reasons[i] = item["rationale"]

    if _is_flat([w for w in words if w is not None]):
        words = [None] * len(leaves)
        reasons = [""] * len(leaves)

    weights = [w if w is not None else s for w, s in zip(words, seeds)]
    for leaf, budget, reason in zip(leaves, _apportion(weights, target_words), reasons):
        leaf["word_budget"] = budget
        leaf["rationale"] = reason


def _is_flat(values: list[int]) -> bool:
    """整体离散度过低 = 模型在均分，而不是按内容体量分配。"""
    if len(values) < 4:
        return False
    mean = statistics.fmean(values)
    if mean <= 0:
        return False
    return statistics.pstdev(values) / mean < _FLAT_CV


def _seed_weights(leaves: list[dict], paper_type: str, target_words: int) -> list[int]:
    """每节的骨架种子字数：模型没给分配时兜底，也兼作合理性的参照基准。"""
    ratios = [r for _t, r in paper_types.skeleton_leaves(paper_type)]
    if len(ratios) == len(leaves):
        return [_seed_budget(r, target_words) for r in ratios]
    # 结构与骨架对不上（例如旧项目遗留的带子节大纲）：退回已有预算，只求不把
    # 字数分配搞崩，不假装知道该类型的篇幅权重。
    return [max(1, int(l.get("word_budget", 0)) or 1) for l in leaves]


def _seed_budget(ratio: float, target_words: int) -> int:
    """骨架比例算出的种子字数 —— 只是兜底与参照，不再是配额。"""
    return max(1, int(round(target_words * ratio)))


# ---------------------------------------------------------------
# 字数对齐
# ---------------------------------------------------------------
def _iter_leaves(outline: dict) -> list[dict]:
    """按大纲顺序产出最细粒度节点（叶子）。

    顺序必须与 routers/projects._sum_budget、schedule_agent 的遍历一致：
    章 → 节 →（有子节则取子节，否则取节本身）。
    """
    leaves: list[dict] = []
    for ch in outline.get("chapters", []):
        for sec in ch.get("sections", []):
            subs = sec.get("subsections") or []
            if subs:
                leaves.extend(subs)
            else:
                leaves.append(sec)
    return leaves


def _leaf_menu(structure: dict) -> str:
    """把定稿大纲渲染成带序号的清单，编号与 _iter_leaves 严格对齐。"""
    lines: list[str] = []
    idx = 0
    for ch in structure.get("chapters", []):
        lines.append(f"  {ch.get('title', '')}")
        for sec in ch.get("sections", []):
            subs = sec.get("subsections") or []
            if subs:
                lines.append(f"    {sec.get('title', '')}")
                for sub in subs:
                    idx += 1
                    lines.append(f"      [{idx}] {sub.get('title', '')}")
            else:
                idx += 1
                lines.append(f"    [{idx}] {sec.get('title', '')}")
    return "\n".join(lines)


def _apportion(weights: list[int], target: int) -> list[int]:
    """按权重把 target 精确分配到各项（最大余数法）。

    余数给小数部分最大的项 —— 而不是像以前那样全堆在最后一节，那会让末节
    莫名其妙地比别人重（或轻）。附带效果：权重若非整十整百，结果也不会是
    整十整百，天然不像「配额」。
    """
    n = len(weights)
    if n == 0:
        return []
    if target <= 0:
        return [0] * n

    safe = [max(0, int(w)) for w in weights]
    total = sum(safe)
    if total <= 0:
        safe = [1] * n
        total = n

    exact = [w * target / total for w in safe]
    out = [int(x) for x in exact]
    leftover = target - sum(out)
    # 并列时权重大的优先，保证结果稳定可复现
    order = sorted(range(n), key=lambda i: (exact[i] - out[i], safe[i]), reverse=True)
    for i in order[:leftover]:
        out[i] += 1

    # 每节至少 1 字；从当前最大的项借，保持总和精确
    if target >= n:
        for i in range(n):
            if out[i] < 1:
                j = max(range(n), key=lambda k: out[k])
                if out[j] > 1:
                    out[j] -= 1
                    out[i] = 1
    return out


def _rebalance(outline: dict, target_words: int) -> None:
    """将大纲所有叶子节点的字数预算按比例缩放，使总和精确等于 target_words。"""
    leaves = _iter_leaves(outline)
    if not leaves:
        return
    weights = [int(l.get("word_budget", 0)) for l in leaves]
    for leaf, budget in zip(leaves, _apportion(weights, target_words)):
        leaf["word_budget"] = budget


def _fallback_outline(
    paper_type: str, target_words: int, writing_lang: str = DEFAULT_LANG
) -> dict:
    """按论文类型的内置骨架生成大纲（未配置 LLM 时的路径）。

    **刻意不收 topic**：它原先有个 `topic` 形参，函数体里一次都没用上 —— 骨架是
    逐类型固化的、字数按比例分，都不看选题。留着它比删掉更坏：读到这里的人（以及
    将来想改这个函数的人）会以为兜底大纲**反映**了用户填的选题，而它并不。真要让它
    用上选题，那是产品改动（兜底也写出一句 core_question），不是顺手加个参数。

    `writing_lang` 与 topic 的处境不同：它**真的**会改变这个函数的输出（骨架取哪张表）。
    """
    chapters = []
    for ch_title, secs in paper_types.template_for(paper_type, writing_lang):
        ch = {"title": ch_title, "sections": []}
        for sec_title, ratio in secs:
            ch["sections"].append({
                "title": sec_title,
                "word_budget": _seed_budget(ratio, target_words),
                "rationale": "",
                "subsections": [],
            })
        chapters.append(ch)

    # 无 LLM 可用，写不出核心研究问题；置空串，前端据此不显示该行
    outline = {"core_question": "", "chapters": chapters}
    _rebalance(outline, target_words)  # 修正舍入误差，使总和精确等于 target_words
    return outline
