"""引用相关性 Agent：判断哪篇文献该出现在哪一节。

与 schedule_agent 的分工是这里最要紧的一件事：**本模块只给判断，不做补齐**。它返回
的清单允许残缺（漏了几篇、或者把某一节留空），补齐由 schedule_agent.fill_gaps 负责。

这样切分换来两件实在的好处：
1. schedule_agent 保持「一次模型都不调用」的纯确定性身份（ARCHITECTURE 点名的那一个），
   于是模型不可用 / 未配置 / 超时 / 输出不可解析时，退回它仍是一条**完整**的路 ——
   而不是把「无遗漏」这件承诺委托给一个可能不回答的模型。
2. 「每篇文献都进正文、每节都有引用」这两条是**代码**保证的（fill_gaps 里可读可测），
   不靠提示词里的祈使句撑着。

**模型只能从库内文献里挑、只能挑大纲里已有的章节**：两侧都按白名单校验，编出来的
id 与标题一律丢弃 —— 与 cluster_agent._parse_clusters 同一把尺子（「0 幻觉引用」）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app import paper_types
from app.agents import schedule_agent
from app.agents.outline_agent import build_literature_context
from app.llm import llm

logger = logging.getLogger(__name__)

# 一次调用最多发多少篇文献：提示词长度与输出长度的硬边界。超出的文献不进提示词，
# 由 fill_gaps 兜底归属 —— 它们照样出现在正文与参考文献里，只是归属不是模型给的。
# 这个数不进提示词，所以模型不会以为「只有这些文献」。
MAX_DOCS = 50

# 理由在界面上是一行说明，不是一段话。
MAX_REASON_CHARS = 40

# 引用密度天然低的两类：提示词里给更小的每节建议篇数。清单依据是 paper_types 里
# 既有的逐类型口径（COURSE_PAPER 的 structure_note 写明「短篇」、TECH_REPORT 的
# writing_note 写明「对参考文献依赖很低，不必为了引用而引用」），这里只是把那句话
# 换成给模型的操作建议。因此不值得往 TYPE_CONFIG 加字段 —— 那要同步前端镜像与
# tests/test_frontend_mirror.py，为一个措辞动结构性配置不划算。
SHORT_TYPES = (paper_types.COURSE_PAPER, paper_types.TECH_REPORT)

# 一次调用必须有个上限：openai 客户端默认超时 600s 且默认重试 2 次，而这跑在**同步
# 请求**里（不登记后台任务），没有上限时一个按钮能转十几分钟。超时即退回确定性兜底，
# 用户照样拿到一份可确认的绑定。
# 「不登记后台任务」这半句**v1.20 起就不准**（调用点把它派进 tasks._TASKS 再 await 它
# 自己），v1.29 起它还会把进度写进槽里。所以这个常量的理由要收窄成它真正的那一条：
# **调用方一直等着**（同步请求，不点火即走），于是等待必须有界。反过来，这个上限也是
# 前端那句「最长 120 秒」的出处 —— 改这里就要一起改那句话（projects.py 里那两处文案）。
REQUEST_TIMEOUT = 120.0

RELEVANCE_PROMPT = """你是学术论文的引用编排专家。下面是一篇论文的大纲与作者上传的文献清单，\
请判断每一篇文献最该出现在哪一节。

论文信息：
- 论文类型：{paper_type}
- 选题：{topic}
- 核心研究问题：{core_question}

大纲的章节（下面的 section 只能从这些标题里逐字选一个）：
{sections}

作者上传的文献：
{documents}

要求：
1. 每一篇文献都要给出一个归属，不要遗漏；`section` 必须与上面某个章节标题**逐字相同**。
2. 判断依据是**主题贴切度**：这篇文献研究的对象、问题或结论，与哪一节的标题最贴合。
   讨论同一主题的文献应当归到同一节，不要为了「每节都有」而把它们摊开到不相关的章节。
3. 一篇文献只能归一处。一节可以没有文献，也可以有多篇。{scale_hint}
4. `reason` 用一句话说明为什么归这一节（不超过 40 字）。它会显示给论文作者看，
   所以要具体（提到该文献的研究对象或结论），不要写「相关」「符合主题」这类空话。
5. 只输出 JSON，不要任何其他文字。

输出格式：
{{
  "assignments": [
    {{"doc_id": "文献 id", "section": "章节标题", "reason": "为什么归这一节"}}
  ]
}}
"""


def build_section_context(outline: dict) -> tuple[str, set[str]]:
    """渲染提示词里的章节清单，并返回标题白名单。

    标题取自 schedule_agent._collect_sections —— **与绑定那份键集必须逐字相同**，
    所以这里不自己走一遍大纲（两次遍历迟早会漂移，届时模型选中的标题在绑定里不存在，
    会被静默丢弃）。代价是清单只有叶子标题、没有章节分组；标题里通常已带上「2.1」
    这类序号，结构感不至于丢。

    渲染用的标题**顺序即大纲顺序**（_collect_sections 就是按大纲遍历的）。
    """
    titles: list[str] = []
    for sec in schedule_agent._collect_sections(outline):
        title = (sec.get("title") or "").strip()
        if title and title not in titles:
            titles.append(title)
    return "\n".join(f"- {t}" for t in titles), set(titles)


def _scale_hint(paper_type: str) -> str:
    """按类型给一句「每节大概几篇」的操作建议。

    它只是**建议**：无遗漏与全章节覆盖由 fill_gaps 兜住，多出来的文献照样有归属。
    因此措辞里必须把这一点说清，否则模型会以为「可以少归几篇」。
    """
    if paper_type in SHORT_TYPES:
        return (
            "本类型篇幅短、章节少：每节 1~2 篇最贴切，多出来的文献归到最接近的那一节即可，"
            "不要为了让每节都有而把它们摊开。"
        )
    return "每节 1~4 篇通常比较均衡，多出来的文献归到最接近的那一节即可。"


def parse_assignments(
    data: Any, known_ids: set[str], allowed_titles: set[str]
) -> list[dict] | None:
    """校验模型给出的归属，两侧都按白名单丢弃编造的 id 与标题。

    同一篇文献只取**第一条**有效归属：模型偶尔会把一篇文献写两遍（改主意时留下的），
    收下后一条等于把前一条的判断静默改掉，而界面上只显示一份。

    没有任何一条可用时返回 None —— 与调用失败同一种处理（退回确定性兜底），因为对
    调用方来说它们是同一件事：**这一次的判断不可用**。
    """
    if not isinstance(data, dict):
        return None
    seen: set[str] = set()
    out: list[dict] = []
    raw = data.get("assignments")
    if not isinstance(raw, list):
        return None
    for item in raw:
        if not isinstance(item, dict):
            continue
        doc_id = item.get("doc_id")
        title = item.get("section")
        if not isinstance(doc_id, str) or not isinstance(title, str):
            continue
        doc_id, title = doc_id.strip(), title.strip()
        if doc_id not in known_ids or title not in allowed_titles or doc_id in seen:
            continue
        reason = item.get("reason")
        out.append({
            "doc_id": doc_id,
            "section": title,
            "reason": reason.strip()[:MAX_REASON_CHARS] if isinstance(reason, str) else "",
        })
        seen.add(doc_id)
    return out or None


async def assign_by_relevance(
    outline: dict,
    documents: list[dict],
    paper_type: str = "",
    topic: str = "",
    core_question: str = "",
) -> list[dict] | None:
    """让模型判断每篇文献该归到哪一节。

    返回 `[{"doc_id", "section", "reason"}]`；**拿不到可用结果时返回 None**，不抛异常
    （模型未配置、超时、输出不是 JSON、一条有效条目都没有 —— 对调用方都是同一件事：
    退回确定性算法）。

    paper_type 收**短名**（库里的值），提示词里用 label_for 换成给模型看的长名：类型
    判断用短名，长名只进提示词（与其他模块「提示词里用长名」的做法一致）。
    """
    if not documents or not llm.is_configured:
        return None
    section_text, allowed_titles = build_section_context(outline)
    if not allowed_titles:
        return None

    prompt = RELEVANCE_PROMPT.format(
        paper_type=paper_types.label_for(paper_type) or "（未指定）",
        topic=topic.strip() or "（未填写）",
        core_question=core_question.strip() or "（未明确给出，请从选题与文献标题推断）",
        sections=section_text,
        # 交全部文献给这个 helper（它自己按 max_docs 截断，并附一句「另有 N 篇未列出」）：
        # 与其自己切一刀，不如让模型知道自己看到的不是全部 —— 那 N 篇由 fill_gaps 兜底。
        documents=build_literature_context(documents, max_docs=MAX_DOCS, with_ids=True),
        scale_hint=_scale_hint(paper_type),
    )
    try:
        data = await asyncio.wait_for(
            llm.chat_json([{"role": "user", "content": prompt}]),
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 —— 任何失败都退回确定性兜底
        logger.warning("相关性归属判定未能完成，退回确定性兜底：%s", exc)
        return None

    parsed = parse_assignments(
        data,
        known_ids={d["id"] for d in documents if d.get("id")},
        allowed_titles=allowed_titles,
    )
    if not parsed:
        logger.warning("相关性归属判定没有一条可用结果，退回确定性兜底")
    return parsed
