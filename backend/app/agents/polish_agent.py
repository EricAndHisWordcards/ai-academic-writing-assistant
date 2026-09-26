"""润色 Agent：学术化转述 + 去 AI 痕迹润色。

**它跑在每一节正文上**（`projects._run_generation` 里逐节润色），所以英文项目下必须
带上输出语言宣告：这一段的输入输出都是正文本身，若模型顺着中文指令用中文回话，
润色这一环就把刚写好的英文正文整段换掉，而下游只看到「正文变了」。
"""
from __future__ import annotations

import re
from collections import Counter

from app.llm import llm
from app.writing_lang import DEFAULT_LANG, output_rule

# 引用角标：方括号形式 [1]、[1,2]、[1-3] 与上标形式（¹ ² ³…）。
# 方括号里只认数字与分隔符，避免把「[注]」「[受访者 A]」这类正文内容当角标。
_SUPERSCRIPT = "⁰¹²³⁴⁵⁶⁷⁸⁹"
_MARKER_RE = re.compile(r"\[[\d\s,，、\-–~]+\]|[" + _SUPERSCRIPT + r"]+")
_INSIDE_WS = re.compile(r"\s+")


def markers(text: str) -> Counter:
    """数出这段文字里每种引用角标各出现了几次。

    比较用**多重集**而不是出现顺序：编号的含义由引用调度定死（[1] 永远是文末列表的
    第 1 条），同一节内先引哪条后引哪条不影响任何东西，改了也不必判异常。反过来，
    少一个、多一个、或者 [3] 变成 [2]，都是「正文与文末列表对不上」——必须拦住。
    角标内部的空白归一化（`[ 1 , 2 ]` 与 `[1,2]` 视为同一个），免得把格式微调当成改动。
    """
    counts: Counter = Counter()
    for raw in _MARKER_RE.findall(text or ""):
        counts[_INSIDE_WS.sub("", raw)] += 1
    return counts


POLISH_PROMPT = """你是学术论文润色专家。请对以下学术文本进行润色。

要求：
1. 学术化转述：将口语化、模板化表达转为严谨、客观的学术语言。
2. 去除"AI 痕迹"：消除空洞套话、机械排比、过度使用"首先/其次/最后/总而言之"等模板词；避免句式单一与重复用词。
3. 降重：在不改变原意的前提下调整措辞与句式结构，降低与常见表达的相似度。
4. 保持原意不变，不增删核心观点、数据与结论。
5. 引用角标（形如 [1]、[2]、[3] 或 上标 ¹²³）必须原样保留，不得增删或改动其编号与位置。
6. 保持字数基本不变（允许 ±10% 波动）。

原文：
{content}

请直接输出润色后的文本，不要任何解释、不要添加标题。
{output_rule}"""


async def polish(text: str, writing_lang: str = DEFAULT_LANG) -> str:
    """对文本进行学术化润色。未配置 LLM、文本为空或润色结果不可用时**原样返回**。

    writing_lang 只加一条输出语言宣告（见模块 docstring），追加在末尾且默认中文，
    既有调用与断言不受影响。
    """
    if not llm.is_configured:
        return text
    source = (text or "").strip()
    if not source:
        # 空文本交给模型时，模型会回“请提供需要润色的原文”之类的答复并被写入正文
        return text
    prompt = POLISH_PROMPT.format(content=source, output_rule=output_rule(writing_lang))
    result = (await llm.chat([{"role": "user", "content": prompt}])).strip()
    if not result or len(result) < len(source) * 0.5:
        # 结果为空或长度严重偏离（答非所问）时，保留原文
        return text
    # 角标用代码再核一遍，不能只靠提示词第 5 条。这是本项目的既有教训：模型「偶尔
    # 不听话」是常态，而这里不听话的代价特别隐蔽 —— 润色后掉了一个 [7]，正文照样
    # 通顺、文末列表照样完整，只有逐条核对才看得出来，读者却会以为那句话有出处。
    # 一次润色失败只影响这一节的可读性，编号错位却会让整篇的引用失去可信度，
    # 所以宁可交回未润色的原文。
    if markers(result) != markers(source):
        return text
    return result
