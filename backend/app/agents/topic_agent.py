"""选题 Agent：基于领域与论文类型推荐候选选题。

候选选题的 `title` 会被用户挑中、写进 `projects.topic`，最终成为**论文的标题**；
`question` 同理会成为核心研究问题的来源。也就是说这个 Agent 虽然在第一步，产出的
却是实打实的产物文字 —— 英文项目下它必须用英文写，否则整篇论文从标题起就是中文。
"""
from __future__ import annotations

import json

from app.llm import llm
from app.writing_lang import DEFAULT_LANG, is_en, output_rule, words_unit

TOPIC_PROMPT = """你是学术写作指导专家。请根据用户提供的研究领域与论文类型，推荐 {count} 个候选选题。

要求：
1. 每个选题需包含：标题(title)、研究问题(question)、可行性说明(feasibility)、与目标字数的匹配度(match，0-100)。
2. 选题应具体、可研究、有学术价值。
3. 严格输出 JSON 数组格式。

用户输入：
- 研究领域：{domain}
- 论文类型：{paper_type}
- 目标字数：{target_words} {word_unit}

输出格式（仅 JSON，不要任何其他文字）：
[{{"title": "...", "question": "...", "feasibility": "...", "match": 80}}]
{output_rule}"""


async def recommend_topics(
    domain: str,
    paper_type: str,
    target_words: int,
    count: int = 3,
    writing_lang: str = DEFAULT_LANG,
) -> list[dict]:
    """推荐候选选题。若 LLM 未配置，返回兜底示例。

    writing_lang 决定候选标题/研究问题的语言，以及「目标字数」后面跟的单位（中文
    的字 / 英文的 words）。追加在末尾且默认中文，既有调用不受影响。
    """
    if not llm.is_configured:
        return _fallback_topics(domain, paper_type, writing_lang)

    prompt = TOPIC_PROMPT.format(
        domain=domain,
        paper_type=paper_type,
        target_words=target_words,
        word_unit=words_unit(writing_lang),
        count=count,
        output_rule=output_rule(writing_lang),
    )
    text = await llm.chat_json([{"role": "user", "content": prompt}])
    if isinstance(text, dict):
        text = text.get("topics", text)
    if not isinstance(text, list):
        # 两条兜底路径都必须带上语言：不带的话，英文项目在「模型回了不可用的东西」
        # 这一刻会拿到三条**中文**候选 —— 而它们正是用户会挑中、写进 topic、
        # 成为论文标题的那几条。与 is_configured 那条分支同一个判据。
        return _fallback_topics(domain, paper_type, writing_lang)
    # 一条可用的都没有 ⇒ 与调用失败同一种处理（退回兜底）。对调用方来说它们是同一
    # 件事：**这一刻的推荐不可用**。
    return parse_topics(text)[:count] or _fallback_topics(
        domain, paper_type, writing_lang
    )


def parse_topics(raw: list) -> list[dict]:
    """逐条校验模型给的候选选题，读不懂的条目丢弃。

    另外几个 agent 都逐条校验（同型：relevance_agent.parse_assignments /
    cluster_agent / material_agent），只有这里原先直接把 list 前 count 个返回 ——
    模型若回一串字符串（`["选题一", "选题二"]`），界面照单全收，渲染出来是一排
    **空白卡片**：卡片读的是 item.title / item.question，而字符串上取不到这两个键。

    **title 是唯一的硬要求**：卡片的名字、用户点选后写进项目的东西都是它，没有标题
    等于没有这张卡片。其余字段各按类型归一（非字符串一律留空串，不把模型给的数字、
    嵌套对象塞进 React 子节点）。

    `match` 只在**真的是数字**时保留：兜底选题刻意不给 match，界面按「有才显示」
    处理（`t.match != null`）。既然那个分数是用来跟别的东西比对出来的，模型回个
    "高" 或一个对象就不该被当成匹配度显示出来 —— 与「不知道总量就不给百分比」同一
    条规矩。
    """
    out: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        entry: dict = {"title": title.strip()}
        for key in ("question", "feasibility"):
            value = item.get(key)
            entry[key] = value.strip() if isinstance(value, str) else ""
        match = item.get("match")
        if isinstance(match, (int, float)) and not isinstance(match, bool):
            entry["match"] = match
        out.append(entry)
    return out


def _fallback_topics(
    domain: str, paper_type: str, writing_lang: str = DEFAULT_LANG
) -> list[dict]:
    """未配置 LLM 时的兜底选题。

    可行性文案按**请求到的类型**取，不再点名具体类型：七类里任何一个名字都可能被
    请求到，写死「适合实证研究」在用户选了质性研究时就是错的；标题里的
    「基于实证方法」同理，那是把一类论文的方法论塞进了所有类型的候选里。

    **不给 match**：原先每条都带着一个写死的 90/85/80，界面照印「匹配度 90」。
    可兜底选题是三个模板，它们没有跟任何东西比对过 —— 那个分数是编的。与进度条
    那里同一条规矩（不知道总量就不给百分比）：没有匹配这回事，就不该有匹配度。
    界面按「有才显示」处理，缺了这个键既不报错也不显示「undefined」。

    英文项目下模板本身也要是英文的：这几条会被用户挑中写进 `projects.topic`，
    中文模板会让一篇英文论文顶着中文标题开头 —— 而它恰恰是「没配 LLM」时用户
    唯一能看见的东西（见模块 docstring）。

    `feasibility` 里仍然嵌着**中文的类型名**（`paper_type` 是短名，与界面下拉框里
    那一串是同一个 token）：「类型名一律中文」是「选中文/英文只管产出物」这条决策的
    一部分，英文项目里它也不翻译 —— 用户在下拉框里刚看到的就是这一串。产物文字
    （title / question）才必须跟着语言走。
    """
    if is_en(writing_lang):
        kind = paper_type or "this type of paper"
        return [
            {
                "title": f"Current Research and Trends in {domain}",
                "question": f"What are the current research hotspots and the "
                            f"evolutionary path of {domain}?",
                "feasibility": f"An adequate body of literature exists; "
                               f"the scope fits {kind}.",
            },
            {
                "title": f"Influencing Factors and Mechanisms of {domain}",
                "question": f"Which factors influence {domain}, and through what "
                            f"mechanisms do they act?",
                "feasibility": f"The research object is well defined and workable "
                               f"within {kind}.",
            },
            {
                "title": f"Key Problems and Countermeasures in the Practice of {domain}",
                "question": f"What are the core problems {domain} faces in practice, "
                            f"and how can they be addressed?",
                "feasibility": f"Problem-driven and practically grounded; "
                               f"suitable for {kind}.",
            },
        ]

    kind = paper_type or "论文"
    return [
        {
            "title": f"{domain}研究现状与趋势分析",
            "question": f"{domain}领域当前的研究热点与演进路径是什么？",
            "feasibility": f"文献充足，适合作为{kind}的选题。",
        },
        {
            "title": f"{domain}的影响因素与作用机制研究",
            "question": f"哪些因素影响{domain}，其作用机制如何？",
            "feasibility": f"研究对象明确，适合作为{kind}的选题。",
        },
        {
            "title": f"{domain}实践中的关键问题与对策",
            "question": f"{domain}在实践中面临的核心问题与应对思路？",
            "feasibility": f"现实问题导向，适合作为{kind}的选题。",
        },
    ]
