"""研究设计提炼 Agent：把作者给的附件与文字，提炼成结构化的设计字段。

为什么需要它：论文里最要紧的那一章写的往往就是作者自己做的东西 —— 实证研究的
数据与结果、质性研究的访谈与编码、工程设计的需求与实现、数理论文的假设与推导。
可大纲提示词的输入长期只有「选题 + 类型名 + 骨架」。模型拿不到这些，只能编——
编变量、编显著性、编结论，而这份虚构会一路带进正文，且用户在确认点①看到的是一份
看着完全合理的大纲。

字段随类型而变（见 paper_types.TYPE_CONFIG 的 design_fields），本 Agent 完全不
知道也不关心具体是哪四个 —— 它只按调用方给的字段名白名单提炼。这是它有别于
「专门给实证研究写个提炼器」的地方：加一个新类型只需要在类型表里配字段。

提炼结果落到 projects.design_json，同时供两处消费：
- 大纲生成：分析/结果类章节只能依据这些已给出的内容，缺的地方标「需作者补充」；
- 正文生成：与作者材料一起注入，保证写出来的第四章和字段里的一致。

**字段宁空勿猜**。推测出来的「主要结果」比留空危险得多：它会被当成作者的真实结果
写进正文，作者不一定看得出来，而这类错误在学术上是致命的。所以提示词明确要求填不出
就留空并写进 notes，`_parse` 也只接受白名单字段名。

提炼出的**取值**随写作语言变（英文项目写成英文），**字段名不变** —— 后者是前端表单
的结构，也是 `_parse` 的白名单键。
"""
from __future__ import annotations

from app.llm import llm
from app.material_parser import excerpt
from app.writing_lang import DEFAULT_LANG, output_rule

# 送入提炼提示词时每份材料的摘录上限，与 material_agent.ANALYZE_CHARS 同口径：
# 提炼要的是「这份材料里有什么」，不是逐字精读。
EXTRACT_CHARS = 6000

# 单个字段的取值上限。字段会进大纲提示词，也会进每一节的生成提示词，
# 不设上限的话一张附表就能把上下文挤满。
FIELD_CHARS = 1500

EXTRACT_PROMPT = """你是学术写作指导专家。作者为自己的论文提供了若干材料
（数据表、实验结果、技术方案文档、调研记录等），请从中提炼出下面这些字段，
供后续生成大纲与正文时作为**已知条件**使用。

论文信息：
- 论文类型：{paper_type}
- 论文选题：{topic}
- 本次要提炼的是：{design_label}

需要你填写的字段（**字段名必须原样使用，不要增删、不要改动措辞**）：
{fields}

作者提供的材料：
{materials}

提炼要求：
1. 每个字段都要在材料里找到明确依据再填。**材料里没有写明的，一律留空字符串**，
   并在 `notes` 里说明缺哪一项、建议作者补充什么。
2. **严禁推测或补全**：不要替作者假设任何一项「{design_label}」的内容 —— 上面列出的
   每个字段都必须有材料依据。一个凭空出现的取值会被当成作者的真实结果写进正文，
   且作者不一定看得出来 —— 这比留空危险得多。
3. 保留材料里的具体数字与专有名词（样本量、系数、百分比、指标名、模型名、工具名、
   案例名），这些正是后文写作时不能丢的信息。
4. 每个字段写成一段可直接阅读的说明文字（不是关键词堆叠），200 字以内。
5. 材料之间若有前后矛盾或明显不足（如口径不一致，或结论超出了材料能支持的范围），
   在 `notes` 里指出。

输出格式（仅 JSON，不要任何其他文字）：
{{
  "fields": {{"字段名": "该字段的内容；填不出来就留空字符串"}},
  "notes": "哪些字段缺依据、建议作者补充什么"
}}
{output_rule}"""


async def extract_design(
    materials: list[dict],
    fields: list[str],
    paper_type: str = "",
    design_label: str = "研究设计",
    topic: str = "",
    writing_lang: str = DEFAULT_LANG,
) -> dict:
    """从材料中提炼设计字段。

    返回 {"fields": {字段名: 值}, "notes": str}。
    LLM 未配置或输出不可用时返回全空字段 —— 前端对空结果是安全的（等同于「没提炼出
    东西，请手填」），不因此中断流程。

    writing_lang 只管**字段取值**的语言（见 app.writing_lang 的 output_rule）：这些取值
    会作为「作者已给出的研究设计」逐字注入每一节正文提示词，英文项目下若是中文，模型
    会顺着这份已知条件用中文写第四章。**字段名仍是中文**（它是前端表单的结构，白名单
    按名字匹配，不能被语言改动）。
    """
    empty = {"fields": {f: "" for f in fields}, "notes": ""}
    if not materials or not fields:
        return empty
    if not llm.is_configured:
        return empty

    prompt = EXTRACT_PROMPT.format(
        paper_type=paper_type or "（未指定）",
        topic=topic or "（未指定）",
        design_label=design_label or "研究设计",
        fields="\n".join(f"  - {f}" for f in fields),
        materials=_format_materials(materials),
        output_rule=output_rule(writing_lang),
    )
    data = await llm.chat_json([{"role": "user", "content": prompt}])
    if not isinstance(data, dict):
        return empty

    result = _parse(data, fields)
    notes = data.get("notes")
    result["notes"] = notes if isinstance(notes, str) else ""
    return result


def _format_materials(materials: list[dict]) -> str:
    parts = []
    for m in materials:
        label = m.get("label") or "（未命名材料）"
        kind = "直接粘贴的文本" if m.get("kind") == "text" else "上传的文件"
        parts.append(
            f"【材料 id={m['id']}】{label}（{kind}）\n{excerpt(m.get('text') or '', EXTRACT_CHARS)}"
        )
    return "\n\n".join(parts)


def _parse(data: dict, fields: list[str]) -> dict:
    """按字段名白名单过滤模型的输出。

    只认 fields 里的名字：模型多吐的键直接丢弃，缺失的补空串。字段名是前端表单的
    结构，不能由模型的自由输出决定。取值同时做去空白与截断。
    """
    raw = data.get("fields")
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, str] = {}
    for f in fields:
        val = raw.get(f)
        if isinstance(val, str):
            out[f] = val.strip()[:FIELD_CHARS]
        elif isinstance(val, (int, float)) and not isinstance(val, bool):
            out[f] = str(val)
        else:
            out[f] = ""
    return {"fields": out}
