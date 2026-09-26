"""论文类型定义：类型清单、大纲骨架、工序编排差异。

这里是**大纲结构与工序的单一事实来源**。此前类型清单散落在 4 处（db.PAPER_TYPES、
main.py 的 /api/meta、outline_agent 的提示词与兜底模板），容易漂移；大纲骨架
更是只存在于「未配置 LLM 时的兜底」里，真正走 LLM 时完全没被用上，导致文献
综述经常拿到含「研究设计 / 假设检验」的实证结构。现在统一从这里取。

工序同理：后端的闸门（谁必须先有文献、谁必须有引用绑定、谁需要先给研究设计）
与前端步骤条是同一件事的两面。前端不硬编码顺序，只渲染 /api/meta 下发的 steps，
否则「前端让点、后端 400」只是时间问题。

**存储用短名，显示用长名**。库里 projects.paper_type 存的是短名（如「课程论文/小论文」），
`TYPE_LABELS` 给出对应的长名（如「课程论文/小论文（全学科通用[短篇]）」），前者是标识、
后者是给用户看的。分开的理由很实在：括号里的适用范围是**帮助用户选对类型**的说明文字，
它会随认识变化而调整，而存储标识一旦改动就得迁移数据。字面量式地把两者绑在一起，
等于每次改一句说明都要碰用户数据。

短名里出现 `/` 都是安全的——它们不进 URL、不做 CSS 类名、不参与正则、不参与文件名，
作为 JSON key / JS 下标 / SQLite TEXT 都无需转义。**但短名与长名都绝不能出现 `{}`**：
它们都会被插进 OUTLINE_PROMPT.format(...)，一个大括号会让 `format` 在后台任务里抛
KeyError，而用户看到的只是「大纲生成失败」。

**哪些文本跟随写作语言，哪些不跟随**（这是本模块里最容易搞混的一条线）：

- 跟随：骨架的章标题与节标题、`structure_note` / `writing_note`、`theme_chapter`。
  理由是它们**会变成论文里的一行字**，或者**直接点名那些章**——英文骨架下再让模型
  读「研究设计章必须有「研究假设」」，它按中文名去找章节，而骨架里根本没有。
- 不跟随：`TYPE_LABELS`（下拉框长名）、`design_label` / `design_fields`（表单标签）、
  `flow_hint`（类型下拉框下方那句提示）。它们只给用户看，不进论文。所以这些选择器
  **刻意不接受 `lang`** —— 加一个不改变结果的形参，只会让人以为它真的分了语言。
"""
from __future__ import annotations

from typing import Any

from app import writing_lang
from app.writing_lang import DEFAULT_LANG

# 七类论文的**存储短名**。同时出现在三处：清单、工序表、骨架表，先立常量避免抄错。
LITERATURE_REVIEW = "文献综述"
QUANTITATIVE = "定量/实证研究"
QUALITATIVE = "质性研究/案例分析"
ENGINEERING_DESIGN = "工程设计/系统实现"
THEORETICAL = "理论推导/数理建模"
COURSE_PAPER = "课程论文/小论文"
TECH_REPORT = "技术/工程报告"

# 权威类型清单，顺序即前端下拉框顺序
PAPER_TYPES = [
    QUANTITATIVE,
    QUALITATIVE,
    ENGINEERING_DESIGN,
    THEORETICAL,
    LITERATURE_REVIEW,
    COURSE_PAPER,
    TECH_REPORT,
]

# 短名 → 前端下拉框里显示的长名。括号里是适用范围，用户据此判断该选哪个。
TYPE_LABELS = {
    QUANTITATIVE: "定量/实证研究（社科、经管、医学、部分理科）",
    QUALITATIVE: "质性研究/案例分析（人文社科、新闻、教育、公共管理）",
    ENGINEERING_DESIGN: "工程设计/系统实现（计算机、电子、机械、建筑等工科）",
    THEORETICAL: "理论推导/数理建模（数学、理论物理、理论经济学）",
    LITERATURE_REVIEW: "文献综述（全学科通用）",
    COURSE_PAPER: "课程论文/小论文（全学科通用[短篇]）",
    TECH_REPORT: "技术/工程报告（工科、实践类项目）",
}

# 历史类型名 → 现短名。库里存着改名前的项目，db.init_db 会按这份映射逐条 UPDATE。
#
# **键不得与 PAPER_TYPES 相交**（否则每次启动都会把有效行改写一遍），有测试守着。
# 映射定义在这里而不是 db.py，是因为「类型名有哪些」这件事只该有一个出处；
# db 只负责执行，不该持有类型学知识。
PAPER_TYPE_RENAMES = {
    "实证研究": QUANTITATIVE,
    "课程论文": COURSE_PAPER,
    "技术报告": TECH_REPORT,
}

# 工序里出现的全部步骤 key。任何一种类型的 steps 都必须是它的子集，
# 且**必须包含全部 key** —— 缺一个会让前端 statusToStepKey 定位不到锚点。
# 「引用调度」这一步尤其不能省：即便某类不需要引用，跳过也是走这一步的动作，
# 少了它 CITATION_CONFIRMED 会挂在一个不存在的步骤上。
STEP_KEYS = ("topic", "design", "outline", "resources", "citation", "generate", "export")

# 各类型的工序与差异。frontend 直接渲染 steps，不再自己拼顺序。
#
# flow_hint：该类型的核心流程重点，一句话，显示在类型下拉框下方。用户选类型时
#   真正想知道的是「这一类论文是按什么逻辑写的」——不是七类共用一句泛泛的说明。
# structure_note：该类论文**该有什么章节、绝不许出现什么章节**，进第一遍大纲提示词。
#   七类共用一句「严禁混入其他类型的结构」是不够的：模型知道「实证论文长什么样」，
#   却不知道「访谈论文不该有假设检验」。所以逐类写清正反两面。
# writing_note：该类论文**该怎么写**，进正文生成提示词。大纲定了骨架，可每一句话
#   是在生成环节写的；只给骨架不给写法，七类论文写出来是同一个腔调。
#
# citations_required 的**本轮策略是七类一律 False**（用户定：都允许显式跳过引用调度），
# 因此两处按它判定的闸门当前恒不触发。字段仍然保留——它是「本类型是否必须有引用」
# 的策略位，一字即可改回；删掉等于把策略散进代码，将来想收紧任何一类都得改结构。
# 引用可选的代价是「零引用论文」变得可达，唯一的兜底是 is_literature_first 类型
# 必须仍有文献（见 projects._require_generatable）。
TYPE_CONFIG: dict[str, dict] = {
    QUANTITATIVE: {
        # 实证的第四章要写的是作者自己的数据与结果。不先把这些交给模型，
        # 它只能编——编变量、编显著性、编结论，且这份虚构会一路带进正文。
        "steps": ["topic", "design", "outline", "resources", "citation", "generate", "export"],
        "structure_from_literature": False,
        "citations_required": False,
        "design_label": "实证设计",
        "design_fields": ["数据源与样本", "研究假设", "模型与方法", "主要结果"],
        "flow_hint": "选题 → 变量与数据/假设 → 拟合模型 → 回归/统计分析",
        "structure_note": (
            "本类型是定量/实证研究：研究设计章必须有「研究假设」「变量与数据（样本与测量）」"
            "「模型与方法」，分析章必须有描述性统计、回归或统计检验、结果讨论，"
            "结论章须回应假设是否成立。**绝不许出现**质性编码、访谈资料、个案叙事、"
            "扎根理论一类章节——本类型不做质性分析。"
        ),
        "writing_note": (
            "以定量研究的规范写法展开：先交代变量如何测量、样本如何得来，再报告方法与结果，"
            "讨论时区分「本研究发现」与「既有文献的结论」。"
            "凡涉及数字、系数、显著性水平，只能使用上方作者已给出的内容，不得臆造。"
        ),
    },
    QUALITATIVE: {
        # 质性的发现章写的是作者自己收集的资料与编码结果。不先拿到这些，模型会给
        # 访谈论文套上「假设检验」「变量与数据」——结构错位，一路带进正文。
        "steps": ["topic", "design", "outline", "resources", "citation", "generate", "export"],
        "structure_from_literature": False,
        "citations_required": False,
        "design_label": "质性设计",
        "design_fields": ["研究对象与个案", "质性方法", "资料来源与编码", "主题与发现"],
        "flow_hint": "选题 → 质性方法（访谈/扎根/案例）→ 编码/主题分析",
        "structure_note": (
            "本类型是质性研究/案例分析：必须有「研究方法与策略（访谈/扎根理论/案例研究）」"
            "「研究对象与个案选择」「资料收集与分析（编码过程、主题提炼）」这类章节，"
            "发现章以主题命名。**绝不许出现**研究假设、变量与数据、假设检验、"
            "回归模型、显著性检验一类章节——质性研究不做统计检验。"
        ),
        "writing_note": (
            "以质性研究的规范写法展开：用研究者的口吻呈现资料与个案，引用受访者原话或田野记录时"
            "用引号标明；分析要紧扣编码与主题，说明主题是怎么从资料里提炼出来的。"
            "**不得使用「显著」「系数」「p 值」这类统计检验措辞**，也不得编造访谈材料或受访者的话。"
        ),
    },
    ENGINEERING_DESIGN: {
        # 工科实现类论文的立身之本是自己做的需求、设计、实现与测试。
        # 不先拿到这些，模型只能给出一份悬浮的「方案概述」。
        "steps": ["topic", "design", "outline", "resources", "citation", "generate", "export"],
        "structure_from_literature": False,
        "citations_required": False,
        "design_label": "工程设计",
        "design_fields": ["需求分析", "架构设计", "功能实现", "测试验证"],
        "flow_hint": "选题 → 需求分析 → 架构设计 → 功能实现 → 测试验证",
        "structure_note": (
            "本类型是工程设计/系统实现：必须有「需求分析」「系统（总体与模块）设计」"
            "「系统实现」「测试与验证」这类章节，实现章落到具体技术选型与关键模块，"
            "测试章给出测试方案与结果。**绝不许出现**研究假设、变量与数据、描述性统计、"
            "假设检验、回归分析一类章节——工程实现类论文不做实证回归。"
        ),
        "writing_note": (
            "以工程设计/系统实现的规范写法展开：描述设计时给出结构、模块划分与关键接口，"
            "描述实现时给出具体技术选型、参数与算法流程，描述测试时给出用例、指标与实测结果。"
            "只使用上方作者已给出的技术与参数，不得臆造性能指标或测试数据。"
        ),
    },
    THEORETICAL: {
        # 数理类论文的推导章写的是作者自己的假设与推导。不先拿到这些，模型会套上
        # 「问卷」「样本」「描述性统计」——那是实证研究的结构，不是数学论文的。
        "steps": ["topic", "design", "outline", "resources", "citation", "generate", "export"],
        "structure_from_literature": False,
        "citations_required": False,
        "design_label": "理论模型",
        "design_fields": ["基本假设与公理", "推导框架与符号", "关键推导与证明", "机制与结论"],
        "flow_hint": "选题 → 基本假设与公理 → 理论推导/证明 → 机制分析",
        "structure_note": (
            "本类型是理论推导/数理建模：必须有「基本假设与模型设定」「推导与证明"
            "（命题/定理及其证明）」「机制分析」这类章节，推导章要有明确的命题或定理陈述。"
            "**绝不许出现**问卷、样本与抽样、描述性统计、假设检验、回归分析一类章节——"
            "数理建模不涉及经验数据。"
        ),
        "writing_note": (
            "以理论/数理研究的规范写法展开：先把假设与符号约定交代清楚，再逐步展开推导并"
            "指出每一步的依据；结论要说明它依赖哪些假设，并解释背后的机制。"
            "只使用上方作者已给出的假设、框架与结论，不得臆造未给出的定理、证明或数值结果。"
        ),
    },
    LITERATURE_REVIEW: {
        # 章节主题来自文献聚类，不是先验给定的 —— 所以先注入文献再出大纲
        "steps": ["topic", "resources", "outline", "citation", "generate", "export"],
        "structure_from_literature": True,
        "citations_required": False,
        # 主题脉络章的**骨架标题**（用于定位是第几章、有几节）。标题本身会被模型改写，
        # 所以这里存的是骨架里的原文，不是改写后的。
        "theme_chapter": "研究主题脉络",
        "flow_hint": "选题 → 上传文献 → 聚类生成大纲 → 引用映射 → 生成",
        "structure_note": (
            "本类型是文献综述：章节按研究主题脉络组织（核心概念界定、研究主题脉络、"
            "研究述评、结论与展望），主题脉络章的各节由文献聚类出的主题决定。"
            "**绝不许出现**研究设计、研究假设、变量与数据、描述性统计、假设检验、"
            "实证分析一类章节——综述不产生一手数据。"
        ),
        "writing_note": (
            "以文献综述的规范写法展开：按主题脉络组织行文，把同一主题下的不同研究放在一起"
            "比较、归纳其演进与分歧，**不要逐篇罗列文献摘要**；评价已有研究时指出其不足与"
            "本综述的定位。观点须来自上文给出的文献。"
        ),
    },
    COURSE_PAPER: {
        "steps": ["topic", "outline", "resources", "citation", "generate", "export"],
        "structure_from_literature": False,
        "citations_required": False,
        "flow_hint": "选题 → 快速生成大纲 → 文献匹配 → 生成",
        "structure_note": (
            "本类型是课程论文/小论文（短篇）：结构精简，通常只有「引言（背景与目的）」"
            "「正文（问题分析与对策讨论）」「结论」三章。**绝不许出现**研究设计、实证设计、"
            "变量与数据、假设检验一类章节——短篇论文没有做实证研究的篇幅。"
        ),
        "writing_note": (
            "以短篇论文的写法展开：开门见山，围绕一个问题说清来龙去脉，篇幅紧凑。"
            "不要堆砌与主题无关的背景，也不要写成文献综述或研究报告。"
        ),
    },
    TECH_REPORT: {
        # 技术报告靠的是技术方案、工程参数与实测结果，对参考文献依赖很低。
        "steps": ["topic", "design", "outline", "resources", "citation", "generate", "export"],
        "structure_from_literature": False,
        "citations_required": False,
        "design_label": "技术方案",
        "design_fields": ["项目背景与目标", "技术选型与架构", "关键参数", "实测结果"],
        "flow_hint": "选题 → 输入工程方案/实验参数 → 生成报告",
        "structure_note": (
            "本类型是技术/工程报告：必须有「技术方案（总体设计、关键技术实现）」"
            "「实验与测试（测试条件、结果与数据）」这类章节，围绕方案与工程参数展开。"
            "**绝不许出现**研究假设、变量与数据、描述性统计、假设检验一类章节，"
            "也不要写成综述式的「研究主题脉络」。"
        ),
        "writing_note": (
            "以技术报告的规范写法展开：把方案、参数与实测数据讲清楚，用可核对的数字说话。"
            "只使用上方作者已给出的技术选型与实测结果，不得臆造参数或性能指标；"
            "对参考文献依赖很低，不必为了引用而引用。"
        ),
    },
}

# 各类型的大纲骨架：章 -> [(节, 权重)]。
# 权重只需是合理的比例（内部会归一化到目标字数），但保持和为 1.0 便于阅读。
#
# 节标题是**通用占位**：真正的标题由模型按选题与文献改写（_merge_with_skeleton
# 按 (章序号, 节序号) 取值，结构永远赢、标题允许改写）。
PAPER_TEMPLATES: dict[str, list[tuple[str, list[tuple[str, float]]]]] = {
    QUANTITATIVE: [
        ("第一章 引言", [("1.1 研究背景", 0.07), ("1.2 研究问题与意义", 0.06)]),
        ("第二章 文献综述", [("2.1 理论基础", 0.09), ("2.2 相关研究现状", 0.11)]),
        ("第三章 研究设计", [
            ("3.1 研究假设", 0.07),
            ("3.2 变量与数据", 0.09),
            ("3.3 模型与方法", 0.08),
        ]),
        # 稳健性/敏感性检验是社科·经管·医学统计类论文的标准组成：
        # 只报一个基准回归，评审第一个问的就是「换个测度还成立吗」。
        ("第四章 实证分析", [
            ("4.1 描述性统计", 0.06),
            ("4.2 基准回归与假设检验", 0.11),
            ("4.3 稳健性检验", 0.06),
            ("4.4 结果讨论", 0.06),
        ]),
        ("第五章 结论与展望", [("5.1 研究结论", 0.09), ("5.2 研究局限与展望", 0.05)]),
    ],
    QUALITATIVE: [
        ("第一章 绪论", [("1.1 研究背景与问题", 0.07), ("1.2 研究意义与个案选择", 0.06)]),
        ("第二章 文献综述与理论框架", [
            ("2.1 核心概念界定", 0.08),
            ("2.2 相关研究与理论视角", 0.10),
        ]),
        ("第三章 研究设计", [
            ("3.1 研究对象与个案", 0.07),
            ("3.2 质性方法与资料收集", 0.09),
            ("3.3 资料编码与分析策略", 0.07),
        ]),
        ("第四章 资料分析与主题发现", [
            ("4.1 编码过程与范畴提炼", 0.10),
            ("4.2 主题一：核心议题", 0.12),
            ("4.3 主题二：延伸议题", 0.09),
        ]),
        ("第五章 结论与讨论", [
            ("5.1 研究结论与理论对话", 0.09),
            ("5.2 研究局限与展望", 0.06),
        ]),
    ],
    ENGINEERING_DESIGN: [
        ("第一章 绪论", [
            ("1.1 项目背景与问题", 0.07),
            ("1.2 国内外研究与应用现状", 0.08),
            ("1.3 本文工作与组织结构", 0.05),
        ]),
        ("第二章 需求分析", [("2.1 功能需求", 0.08), ("2.2 非功能需求与约束", 0.06)]),
        ("第三章 系统架构设计", [
            ("3.1 总体架构设计", 0.09),
            ("3.2 关键模块设计", 0.09),
            ("3.3 数据与接口设计", 0.07),
        ]),
        ("第四章 系统实现", [("4.1 关键功能实现", 0.11), ("4.2 关键技术与难点", 0.08)]),
        ("第五章 测试与验证", [("5.1 测试方案与用例", 0.06), ("5.2 测试结果与分析", 0.07)]),
        ("第六章 总结与展望", [("6.1 工作总结", 0.05), ("6.2 不足与展望", 0.04)]),
    ],
    THEORETICAL: [
        ("第一章 引言", [("1.1 研究背景与问题", 0.08), ("1.2 研究思路与结构安排", 0.05)]),
        ("第二章 文献综述与预备知识", [
            ("2.1 相关理论回顾", 0.09),
            ("2.2 预备知识与符号约定", 0.08),
        ]),
        ("第三章 模型设定与基本假设", [
            ("3.1 基本假设与公理", 0.10),
            ("3.2 模型框架与符号定义", 0.09),
        ]),
        ("第四章 理论推导与证明", [("4.1 主要命题的推导", 0.13), ("4.2 命题的证明", 0.11)]),
        ("第五章 机制分析与讨论", [
            ("5.1 机制分析与主要发现", 0.10),
            ("5.2 与已有理论的比较", 0.06),
        ]),
        ("第六章 结论", [("6.1 主要结论", 0.07), ("6.2 局限与展望", 0.04)]),
    ],
    LITERATURE_REVIEW: [
        ("第一章 引言", [
            ("1.1 研究背景与意义", 0.10),
            ("1.2 综述范围与方法", 0.08),
        ]),
        ("第二章 核心概念界定", [
            ("2.1 关键概念辨析", 0.12),
            ("2.2 理论视角梳理", 0.10),
        ]),
        ("第三章 研究主题脉络", [
            ("3.1 主题一：研究演进", 0.14),
            ("3.2 主题二：主要流派与观点", 0.13),
            ("3.3 主题三：争议与分歧", 0.09),
        ]),
        ("第四章 研究述评", [
            ("4.1 已有研究的不足", 0.10),
            ("4.2 本综述的定位", 0.04),
        ]),
        ("第五章 结论与展望", [
            ("5.1 主要结论", 0.05),
            ("5.2 未来研究方向", 0.05),
        ]),
    ],
    COURSE_PAPER: [
        ("第一章 引言", [("1.1 研究背景与问题", 0.13), ("1.2 研究目的与意义", 0.08)]),
        ("第二章 主体论述", [("2.1 现状与问题分析", 0.30), ("2.2 原因与对策讨论", 0.27)]),
        ("第三章 结论", [("3.1 总结与展望", 0.22)]),
    ],
    TECH_REPORT: [
        ("第一章 概述", [("1.1 背景与目标", 0.12), ("1.2 报告范围与依据", 0.06)]),
        ("第二章 技术方案", [("2.1 总体设计", 0.17), ("2.2 关键技术实现", 0.19)]),
        ("第三章 实验与测试", [
            ("3.1 实验参数与测试条件", 0.13),
            ("3.2 测试结果与分析", 0.13),
        ]),
        ("第四章 结论与建议", [("4.1 总结与建议", 0.20)]),
    ],
}

# 英文项目的大纲骨架。**与 PAPER_TEMPLATES 平行、键集相同、形状逐位相同**
# （章数、每章节数、每节权重都要一致），由 tests/test_paper_types.py 的三条护栏钉住：
# 形状不变式（漏译一节会当场变红）、正文不含 CJK、以及主题章节数的语言不变性。
#
# 权重与中文表**逐字节相同**地抄下来，而不是各配一套：「同样结构的论文在两套语言下
# 拿到不同篇幅比例」没有任何理由，而它是抄错时最难发现的一种偏差——章数节数都对，
# 只有某一节多分了两百字。护栏把权重也一起断言了。
#
# `Chapter N` / `N.M` 的编号形态保持不变：正文角标与字数统计都按纯文本处理，
# 编号是给人看的，罗马数字或字母序在这里不带来任何好处。
PAPER_TEMPLATES_EN: dict[str, list[tuple[str, list[tuple[str, float]]]]] = {
    QUANTITATIVE: [
        ("Chapter 1 Introduction", [
            ("1.1 Research Background", 0.07),
            ("1.2 Research Questions and Significance", 0.06),
        ]),
        ("Chapter 2 Literature Review", [
            ("2.1 Theoretical Foundation", 0.09),
            ("2.2 Related Work", 0.11),
        ]),
        ("Chapter 3 Research Design", [
            ("3.1 Research Hypotheses", 0.07),
            ("3.2 Variables and Data", 0.09),
            ("3.3 Model and Methods", 0.08),
        ]),
        ("Chapter 4 Empirical Analysis", [
            ("4.1 Descriptive Statistics", 0.06),
            ("4.2 Baseline Regression and Hypothesis Testing", 0.11),
            ("4.3 Robustness Checks", 0.06),
            ("4.4 Discussion of Results", 0.06),
        ]),
        ("Chapter 5 Conclusion and Outlook", [
            ("5.1 Research Conclusions", 0.09),
            ("5.2 Limitations and Future Work", 0.05),
        ]),
    ],
    QUALITATIVE: [
        ("Chapter 1 Introduction", [
            ("1.1 Research Background and Questions", 0.07),
            ("1.2 Research Significance and Case Selection", 0.06),
        ]),
        ("Chapter 2 Literature Review and Theoretical Framework", [
            ("2.1 Definition of Core Concepts", 0.08),
            ("2.2 Related Studies and Theoretical Perspectives", 0.10),
        ]),
        ("Chapter 3 Research Design", [
            ("3.1 Research Subjects and Cases", 0.07),
            ("3.2 Qualitative Methods and Data Collection", 0.09),
            ("3.3 Data Coding and Analysis Strategy", 0.07),
        ]),
        ("Chapter 4 Data Analysis and Thematic Findings", [
            ("4.1 Coding Process and Category Development", 0.10),
            ("4.2 Theme One: Core Issues", 0.12),
            ("4.3 Theme Two: Extended Issues", 0.09),
        ]),
        ("Chapter 5 Conclusion and Discussion", [
            ("5.1 Conclusions and Theoretical Dialogue", 0.09),
            ("5.2 Limitations and Future Work", 0.06),
        ]),
    ],
    ENGINEERING_DESIGN: [
        ("Chapter 1 Introduction", [
            ("1.1 Project Background and Problem", 0.07),
            ("1.2 Related Research and Applications", 0.08),
            ("1.3 Scope of This Work and Organization", 0.05),
        ]),
        ("Chapter 2 Requirements Analysis", [
            ("2.1 Functional Requirements", 0.08),
            ("2.2 Non-functional Requirements and Constraints", 0.06),
        ]),
        ("Chapter 3 System Architecture Design", [
            ("3.1 Overall Architecture", 0.09),
            ("3.2 Key Module Design", 0.09),
            ("3.3 Data and Interface Design", 0.07),
        ]),
        ("Chapter 4 System Implementation", [
            ("4.1 Implementation of Key Functions", 0.11),
            ("4.2 Key Technologies and Challenges", 0.08),
        ]),
        ("Chapter 5 Testing and Validation", [
            ("5.1 Test Plan and Test Cases", 0.06),
            ("5.2 Test Results and Analysis", 0.07),
        ]),
        ("Chapter 6 Conclusion and Outlook", [
            ("6.1 Summary of the Work", 0.05),
            ("6.2 Limitations and Future Work", 0.04),
        ]),
    ],
    THEORETICAL: [
        ("Chapter 1 Introduction", [
            ("1.1 Research Background and Problem", 0.08),
            ("1.2 Research Approach and Structure", 0.05),
        ]),
        ("Chapter 2 Literature Review and Preliminaries", [
            ("2.1 Review of Related Theory", 0.09),
            ("2.2 Preliminaries and Notation", 0.08),
        ]),
        ("Chapter 3 Model Setup and Basic Assumptions", [
            ("3.1 Basic Assumptions and Axioms", 0.10),
            ("3.2 Model Framework and Notation", 0.09),
        ]),
        ("Chapter 4 Theoretical Derivation and Proof", [
            ("4.1 Derivation of the Main Propositions", 0.13),
            ("4.2 Proofs of the Propositions", 0.11),
        ]),
        ("Chapter 5 Mechanism Analysis and Discussion", [
            ("5.1 Mechanism Analysis and Main Findings", 0.10),
            ("5.2 Comparison with Existing Theory", 0.06),
        ]),
        ("Chapter 6 Conclusion", [
            ("6.1 Main Conclusions", 0.07),
            ("6.2 Limitations and Outlook", 0.04),
        ]),
    ],
    LITERATURE_REVIEW: [
        ("Chapter 1 Introduction", [
            ("1.1 Research Background and Significance", 0.10),
            ("1.2 Scope and Method of the Review", 0.08),
        ]),
        ("Chapter 2 Definition of Core Concepts", [
            ("2.1 Clarification of Key Concepts", 0.12),
            ("2.2 Review of Theoretical Perspectives", 0.10),
        ]),
        ("Chapter 3 Research Theme Landscape", [
            ("3.1 Theme One: Research Evolution", 0.14),
            ("3.2 Theme Two: Main Schools and Views", 0.13),
            ("3.3 Theme Three: Controversies and Divergences", 0.09),
        ]),
        ("Chapter 4 Critical Review", [
            ("4.1 Gaps in Existing Research", 0.10),
            ("4.2 Positioning of This Review", 0.04),
        ]),
        ("Chapter 5 Conclusion and Outlook", [
            ("5.1 Main Conclusions", 0.05),
            ("5.2 Future Research Directions", 0.05),
        ]),
    ],
    COURSE_PAPER: [
        ("Chapter 1 Introduction", [
            ("1.1 Research Background and Problem", 0.13),
            ("1.2 Research Purpose and Significance", 0.08),
        ]),
        ("Chapter 2 Main Discussion", [
            ("2.1 Analysis of the Current Situation and Problems", 0.30),
            ("2.2 Causes and Countermeasures", 0.27),
        ]),
        ("Chapter 3 Conclusion", [("3.1 Summary and Outlook", 0.22)]),
    ],
    TECH_REPORT: [
        ("Chapter 1 Overview", [
            ("1.1 Background and Objectives", 0.12),
            ("1.2 Scope and Basis of the Report", 0.06),
        ]),
        ("Chapter 2 Technical Solution", [
            ("2.1 Overall Design", 0.17),
            ("2.2 Implementation of Key Technologies", 0.19),
        ]),
        ("Chapter 3 Experiments and Testing", [
            ("3.1 Experimental Parameters and Test Conditions", 0.13),
            ("3.2 Test Results and Analysis", 0.13),
        ]),
        ("Chapter 4 Conclusion and Recommendations", [
            ("4.1 Summary and Recommendations", 0.20),
        ]),
    ],
}

# 英文骨架下的两条类型说明。**与 TYPE_CONFIG 平行存放，不塞进它的字典里**：
# `config_for` 的整个 dict 会随 `/api/meta` 下发给前端（前端只读 steps /
# design_fields / flow_hint），把两条提示词文案混进去，等于每次启动都多发一份
# 前端用不上、也不该被前端读到的文本。
_TYPE_NOTES_EN: dict[str, dict[str, str]] = {
    QUANTITATIVE: {
        "structure_note": (
            "This type is a quantitative/empirical study: the research design chapter must "
            "contain \"research hypotheses\", \"variables and data (sample and measurement)\" "
            "and \"model and methods\"; the analysis chapter must contain descriptive "
            "statistics, regression or statistical tests, and a discussion of results; the "
            "conclusion chapter must state whether the hypotheses are supported. **Never "
            "include** chapters on qualitative coding, interview material, case narratives or "
            "grounded theory — this type does no qualitative analysis."
        ),
        "writing_note": (
            "Follow the conventions of quantitative research: first explain how the variables "
            "are measured and how the sample was obtained, then report the methods and the "
            "results; in the discussion, separate \"what this study finds\" from \"what the "
            "existing literature concludes\". Any figure, coefficient or significance level "
            "must come from the author's material above — never invent one."
        ),
    },
    QUALITATIVE: {
        "structure_note": (
            "This type is a qualitative study/case analysis: it must contain chapters such as "
            "\"research methods and strategy (interviews / grounded theory / case study)\", "
            "\"research subjects and case selection\", and \"data collection and analysis "
            "(coding process, theme development)\"; the findings chapter is named after the "
            "themes. **Never include** chapters on research hypotheses, variables and data, "
            "hypothesis testing, regression models or significance tests — qualitative "
            "research performs no statistical testing."
        ),
        "writing_note": (
            "Follow the conventions of qualitative research: present the material and the "
            "cases in the researcher's voice, and mark quotations from interviewees or field "
            "notes with quotation marks; the analysis must stay close to the coding and the "
            "themes and explain how each theme was distilled from the material. **Do not use "
            "statistical-testing vocabulary such as \"significant\", \"coefficient\" or "
            "\"p-value\"**, and never fabricate interview material or interviewee statements."
        ),
    },
    ENGINEERING_DESIGN: {
        "structure_note": (
            "This type is engineering design/system implementation: it must contain chapters "
            "such as \"requirements analysis\", \"system (overall and module) design\", "
            "\"system implementation\" and \"testing and validation\"; the implementation "
            "chapter must come down to concrete technology choices and key modules, and the "
            "testing chapter must give a test plan and results. **Never include** chapters on "
            "research hypotheses, variables and data, descriptive statistics, hypothesis "
            "testing or regression analysis — engineering papers do no empirical regression."
        ),
        "writing_note": (
            "Follow the conventions of engineering design/system implementation: when "
            "describing the design, give the structure, the module breakdown and the key "
            "interfaces; when describing the implementation, give concrete technology "
            "choices, parameters and algorithm flows; when describing testing, give test "
            "cases, metrics and measured results. Use only the technologies and parameters "
            "the author provided above — never invent performance figures or test data."
        ),
    },
    THEORETICAL: {
        "structure_note": (
            "This type is theoretical derivation/mathematical modelling: it must contain "
            "chapters such as \"basic assumptions and model setup\", \"derivation and proof "
            "(propositions/theorems and their proofs)\" and \"mechanism analysis\"; the "
            "derivation chapter must state explicit propositions or theorems. **Never "
            "include** chapters on questionnaires, samples and sampling, descriptive "
            "statistics, hypothesis testing or regression analysis — mathematical modelling "
            "involves no empirical data."
        ),
        "writing_note": (
            "Follow the conventions of theoretical/mathematical research: set out the "
            "assumptions and the notation first, then develop the derivation step by step and "
            "state the basis for each step; the conclusion must say which assumptions it "
            "relies on and explain the mechanism behind it. Use only the assumptions, "
            "framework and conclusions the author provided above — never invent a theorem, a "
            "proof or a numerical result that was not given."
        ),
    },
    LITERATURE_REVIEW: {
        "structure_note": (
            "This type is a literature review: the chapters are organised along research "
            "themes (definition of core concepts, research theme landscape, critical review, "
            "conclusion and outlook), and the sections of the theme-landscape chapter are "
            "decided by the themes clustered from the literature. **Never include** chapters "
            "on research design, research hypotheses, variables and data, descriptive "
            "statistics, hypothesis testing or empirical analysis — a review produces no "
            "primary data."
        ),
        "writing_note": (
            "Follow the conventions of a literature review: organise the text along thematic "
            "lines, put studies on the same theme side by side to compare them and summarise "
            "their evolution and disagreements, and **do not list article-by-article "
            "summaries**; when evaluating existing research, point out its gaps and the place "
            "of this review. Every view must come from the literature given above."
        ),
    },
    COURSE_PAPER: {
        "structure_note": (
            "This type is a course paper/short essay: the structure is lean, usually only "
            "three chapters — \"introduction (background and purpose)\", \"main discussion "
            "(analysis of the problem and countermeasures)\" and \"conclusion\". **Never "
            "include** chapters on research design, empirical design, variables and data or "
            "hypothesis testing — a short paper has no room for empirical research."
        ),
        "writing_note": (
            "Write in the manner of a short paper: come straight to the point, explain one "
            "problem clearly from cause to consequence, and keep it compact. Do not pile up "
            "background unrelated to the topic, and do not write it as a literature review or "
            "a research report."
        ),
    },
    TECH_REPORT: {
        "structure_note": (
            "This type is a technical/engineering report: it must contain chapters such as "
            "\"technical solution (overall design, implementation of key technologies)\" and "
            "\"experiments and testing (test conditions, results and data)\", built around the "
            "solution and the engineering parameters. **Never include** chapters on research "
            "hypotheses, variables and data, descriptive statistics or hypothesis testing, and "
            "do not write a review-style \"research theme landscape\"."
        ),
        "writing_note": (
            "Follow the conventions of a technical report: state the solution, the parameters "
            "and the measured data clearly, and let verifiable numbers speak. Use only the "
            "technology choices and measured results the author provided above — never invent "
            "parameters or performance figures. Dependence on references is low, so do not "
            "cite merely for the sake of citing."
        ),
    },
}

# 英文骨架里的「主题脉络章」标题。**它是功能性的,不是文案**：theme_section_count 靠
# 子串匹配找到那一章、数出它有几节，用来判断作者确认的主题聚类多了还是少了。少了这条
# 英文版,英文项目下它会**静默返回 0**（匹配不到任何一章），聚类合并检查被整段跳过,
# 而界面上一切正常——这正是要加护栏的那类缺陷。
_THEME_CHAPTER_EN = {
    LITERATURE_REVIEW: "Research Theme Landscape",
}

# 未知类型退化为课程论文（保持既有行为）。
# **必须是 TYPE_CONFIG 与 PAPER_TEMPLATES 的公共键**：config_for/template_for 的
# 兜底表达式是立即求值的，这个键不存在会让**所有**类型（不只是未知类型）抛 KeyError。
DEFAULT_TEMPLATE_KEY = COURSE_PAPER


def is_valid(paper_type: str) -> bool:
    """类型是否在权威清单内。"""
    return paper_type in PAPER_TYPES


def label_for(paper_type: str) -> str:
    """取该类型给用户看的长名；未知类型原样返回。

    **提示词里用长名**：括号里的适用范围（「社科、经管、医学、部分理科」）是模型
    判断该按哪套学术规范写的有效信息，丢掉它等于把用户当初选这个类型的理由丢了。
    未知类型返回短名本身，总比返回空串让提示词里出现「论文类型：」后面什么都没有好。

    **不接受 `lang`**：这是给用户看的界面文案，不进论文（见模块 docstring 的那条线）。
    """
    return TYPE_LABELS.get(paper_type) or paper_type


def config_for(paper_type: str) -> dict:
    """取该类型的工序配置，未知类型退化为课程论文。

    与 template_for 的兜底一致：库里可能存着早期版本写下的类型名（例如已下线的
    「毕业论文」，或迁移前的「实证研究」），退化成课程论文也不能抛。
    """
    return TYPE_CONFIG.get(paper_type, TYPE_CONFIG[DEFAULT_TEMPLATE_KEY])


def is_literature_first(paper_type: str) -> bool:
    """该类型是否必须先注入文献、再生成大纲（结构由文献决定）。

    这也是该类型**身份**的声明：大纲结构来自文献，那么「没有文献」就不是一种
    选择而是退化——它在生成侧有专门的守卫。
    """
    return bool(config_for(paper_type).get("structure_from_literature"))


def citations_required(paper_type: str) -> bool:
    """该类型是否必须有引用绑定才能生成正文。

    本轮七类一律 False（见 TYPE_CONFIG 顶部说明），闸门保留待策略收紧。
    """
    return bool(config_for(paper_type).get("citations_required"))


def has_design(paper_type: str) -> bool:
    """该类型是否有「研究设计 / 技术方案」录入步。"""
    return bool(config_for(paper_type).get("design_fields"))


def design_fields_for(paper_type: str) -> list[str]:
    """该类型的设计字段名（顺序即前端表单顺序），无此步时返回空列表。

    **字段名是 design_json 的键**：改一个名字，已存项目里那个字段的内容会静默变空
    （save_design 只写白名单内的键，_design_context 只读白名单内的键），不报错、
    不告警。要改必须配一次数据迁移，见 tests/test_paper_types.py 的字段冻结断言。
    """
    return list(config_for(paper_type).get("design_fields") or [])


def design_label_for(paper_type: str) -> str:
    """该类型设计步的显示名（实证设计 / 质性设计 / 工程设计 / 理论模型 / 技术方案），
    无此步时为空串。"""
    return config_for(paper_type).get("design_label", "") if has_design(paper_type) else ""


def flow_hint_for(paper_type: str) -> str:
    """该类型的核心流程重点，一句话，显示在类型下拉框下方。"""
    return config_for(paper_type).get("flow_hint") or ""


def _note_for(paper_type: str, key: str, lang: Any) -> str:
    """取该类型的一条说明文案；英文骨架下取 `_en` 变体。

    **英文变体缺失时退回中文，而不是退回空串**：空串会让提示词里那一整段消失，
    模型随即失去「这一类论文不该有假设检验」这层约束，而界面上看不出任何异常。
    宁可让模型读到一段中文（它完全读得懂），也不要让它什么都读不到。
    """
    if writing_lang.is_en(lang):
        note = _TYPE_NOTES_EN.get(paper_type, {}).get(key)
        if note:
            return note
    return config_for(paper_type).get(key) or ""


def structure_note_for(paper_type: str, lang: Any = DEFAULT_LANG) -> str:
    """该类型「该有什么章节、绝不许出现什么章节」，进大纲提示词。

    跟随语言：这段文案**逐条点名章节**（「必须有「研究假设」」「绝不许出现假设检验」），
    英文骨架下那些章叫 `Research Hypotheses` —— 不跟着换，模型会按中文名去找章节，
    找不到就按自己的理解补，等于这段约束失效。
    """
    return _note_for(paper_type, "structure_note", lang)


def writing_note_for(paper_type: str, lang: Any = DEFAULT_LANG) -> str:
    """该类型「该怎么写」，进正文生成提示词。跟随语言，理由同 structure_note_for。"""
    return _note_for(paper_type, "writing_note", lang)


def template_for(
    paper_type: str, lang: Any = DEFAULT_LANG
) -> list[tuple[str, list[tuple[str, float]]]]:
    """取该类型的章节骨架，未知类型退化为课程论文。

    英文项目取 `PAPER_TEMPLATES_EN` —— 骨架标题会**原样印进论文**（模型只被允许改写
    措辞、结构恒定），所以它是最必须跟随语言的一处。
    """
    table = PAPER_TEMPLATES_EN if writing_lang.is_en(lang) else PAPER_TEMPLATES
    return table.get(paper_type, table[DEFAULT_TEMPLATE_KEY])


def theme_section_count(paper_type: str, lang: Any = DEFAULT_LANG) -> int:
    """该类型「主题脉络章」有几节；无此概念的类型返回 0。

    用途：确认过的主题聚类若多于节数，多出来的会被合并掉，用户得先知道。
    返回 0 表示「该类型没有主题章」，调用方据此跳过这一检查。

    判据是**在骨架标题里做子串匹配**，所以定位串必须与骨架同语言：拿中文串去
    英文骨架里找，结果恒为 0 —— 那不是「没有主题章」，是这一段检查静默失效。
    """
    if writing_lang.is_en(lang):
        chapter = _THEME_CHAPTER_EN.get(paper_type) or ""
    else:
        chapter = config_for(paper_type).get("theme_chapter") or ""
    if not chapter:
        return 0
    for ch_title, secs in template_for(paper_type, lang):
        if chapter in ch_title:
            return len(secs)
    return 0


def skeleton_lines(paper_type: str, lang: Any = DEFAULT_LANG) -> list[str]:
    """把骨架渲染成提示词里逐行罗列的章节结构文本。"""
    lines = []
    for ch_title, secs in template_for(paper_type, lang):
        lines.append(ch_title)
        for sec_title, ratio in secs:
            lines.append(f"  - {sec_title}")
    return lines


def skeleton_leaves(paper_type: str, lang: Any = DEFAULT_LANG) -> list[tuple[str, float]]:
    """展平骨架的最细粒度节点 [(节标题, 权重)]，顺序即大纲顺序。"""
    leaves: list[tuple[str, float]] = []
    for _ch_title, secs in template_for(paper_type, lang):
        leaves.extend(secs)
    return leaves


def skeleton_shape(paper_type: str, lang: Any = DEFAULT_LANG) -> tuple[int, int]:
    """骨架的 (章数, 叶子数)，供校验 LLM 输出是否跑偏。"""
    tmpl = template_for(paper_type, lang)
    return len(tmpl), sum(len(secs) for _ch, secs in tmpl)
