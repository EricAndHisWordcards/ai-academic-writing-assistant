"""解析 Agent：对上传文献做结构化信息提取。

实际在 MVP 中，PDF 文本提取由 pdf_parser 完成；
本 Agent 负责基于首屏文本用 LLM 提取元数据
（题名/题名英译/作者/年份/来源/卷期页/类型/摘要）。

**为什么这些字段要一起要**：文末参考文献列表按 GB/T 7714-2015 著录期刊条目，形态是
`刊名, 年, 卷(期): 起止页码.`。卷、期、页码、类型标识原先提示词里一个字都没提，
所以它们在源头就不存在 —— 渲染侧再合规也无米下锅（`citation_format._journal_segment`
的降级矩阵就是为「抽不到」准备的）。

**本 Agent 刻意不接 `writing_lang.output_rule`**（其余几个 Agent 都接了）：它抽取的
是**文献原文的元数据**——题名、作者、刊名、摘要，这些在文末列表里要保留原文（英文
论文引用一篇中文文献时，APA 的形态是「原文题名 [English translation]」，原题名仍在）。
给这里加一条「必须用英文撰写」，模型会把抽取到的原文题名、作者名一起翻译掉，
而这类翻译错误极难被作者发现：文末列表看着通顺，只是那条文献查不到了。
"""
from __future__ import annotations

import re

from app import metadata
from app.llm import llm

# 抽取用的正文上限。原来只有 2000 字：英文期刊首页是「大标题 + 长摘要 + 版权/投稿
# 信息」，作者行与刊名行常落在 2000 字之外 —— 这正是英文文献作者普遍为空的成因之一。
# 8000 字对提示词成本无实质影响（元数据只在前两页），却把整页摘要连同页脚都装进来了。
MAX_EXTRACT_CHARS = 8000

# PDF 硬换行的合并规则，见 merge_hard_wraps。
# 判据一律看**这一行末尾能不能算一句的结尾**，而不是看下一行以什么开头 ——
# 后者会把最常见的 `John K. Smith, Anna\nDoe, R. Roe`（下一行以大写开头）
# 判成「新段落」而漏合并，那恰好是它最该处理的形态。
_HYPHEN_WRAP = re.compile(r"(?<=[A-Za-z])-\n[ \t]*(?=[a-z])")
_LOWER_WRAP = re.compile(r"(?<=[a-z])\n[ \t]*(?=\S)")
_LIST_WRAP = re.compile(r"(?<=[,;:，；：、])\n[ \t]*")
# 汉字（含扩展区与兼容区）与中文标点、全角符号
_CJK = "㐀-䶿一-鿿豈-﫿"
_CJK_NEXT = f"[{_CJK}　-〿！-～]"
_CJK_WRAP = re.compile(rf"(?<=[{_CJK}])\n[ \t]*(?={_CJK_NEXT})")


def _space_unless_cjk(m: re.Match) -> str:
    """`_LIST_WRAP` 的替换：后面是汉字就不加空格。"""
    nxt = m.string[m.end():m.end() + 1]
    return "" if nxt and re.match(_CJK_NEXT, nxt) else " "


def merge_hard_wraps(text: str) -> str:
    """把 PDF 抽出来的硬换行接回句读，供元数据抽取用。

    pypdf 的输出一行一个断句（**不是**一段一行），作者列表、刊名、卷期页常被切在
    换行两侧。不合并，模型看到的是

        John K. Smith, Anna
        Doe, R. Roe

    这种形态，能不能接上全靠它自己猜；而这次要求它同时给出卷期页，猜错的代价
    直接落进参考文献列表。四条规则：

    1. `字母-\\n小写字母` → 去掉连字符直接接上（英文 PDF 的断词连字符）
    2. 行以**小写字母**结尾 → 换成空格接上（英文句子不会以小写字母结尾，
       所以这一行一定没写完 —— 哪怕下一行以大写开头，那是名字，不是新段落）
    3. 行以 **`,` `;` `:`** 或中文 `，；：、` 结尾 → 接上；后面是汉字就不加空格
    4. `汉字\\n汉字（或中文标点）` → 直接接上，**不加空格**（中文行内断行是常态，
       而中文之间加空格是错的）

    第 2、4 条都**刻意保守**：第 2 条只认小写字母，第 4 条要求两侧都在汉字/中文
    标点范围内。**不以 `。` `！` `?` `.` 结尾的行没有一条规则会碰它**，所以
    「一句已说完 + 下一行是新段落/新小节/下一条参考文献」这个真实边界永远保得住；
    宁可漏合并也不误合并，漏合并模型还看得懂，误合并不可逆。

    中文那条不是顺手加的：中文文献的作者串同样会被换行切开（`张三, 李四,\\n王五`），
    而本轮要抽的卷期页在中文期刊首页也一样存在。

    三点边界，都是有意为之：
    - 连字符复合词真的被排到行尾（`self-\\nesteem`）会被误接成 `selfesteem`。
      这种情形在题名/作者区极少，而 PDF 断词极多，只能这么选。
    - 汉字行尾接非汉字（`…研究\n[J]. 教育研究`）不合并，留原样。
    - 合并后文本变长，`extract_metadata` 的截断点也随之改变 —— 所以那边是
      **先合并再截断**，否则合并恰好落在截断点外等于白做。
    - 各条规则都吃掉换行两侧的行内空白（`[ \\t]*`）：pypdf 的续行常带缩进，
      不吃掉的话第 2 条会因为下一行以空格开头而判成 `\\S` 不匹配、整条失效。

    只用于抽取。落库的 pages 保持逐页原文不动：引用定位依赖逐页字符偏移
    （见 pdf_parser），在这里改会错位。
    """
    if not text:
        return ""
    text = _HYPHEN_WRAP.sub("", text)
    text = _LOWER_WRAP.sub(" ", text)
    text = _LIST_WRAP.sub(_space_unless_cjk, text)
    return _CJK_WRAP.sub("", text)


PARSE_PROMPT = """你是文献信息提取专家。请从以下文献内容片段中提取结构化信息。

输出 JSON（仅 JSON）：
{{
  "title": "文献标题",
  "title_en": "题名英译",
  "authors": "作者",
  "year": "年份",
  "source": "期刊/会议/来源",
  "volume": "卷号",
  "issue": "期号",
  "page_range": "起止页码",
  "source_type": "文献类型标识",
  "place": "出版地",
  "edition": "版本项",
  "publish_date": "报纸出版日期",
  "summary": "一段50字以内的核心内容摘要"
}}

字段说明：
- authors：按原文顺序、**原样**给出全部作者，用逗号分隔。不要自行缩写姓名、
  不要调换姓名顺序（英文文献常见的 "John K. Smith" 照抄即可）—— 中文与外文的
  著录格式由下游统一处理，这里只负责把原样抄准。
- source_type：只填**一个字母**，取下列之一 —— 期刊论文 J、专著/图书 M、
  学位论文 D、会议论文 C、报纸 N、报告 R、标准 S、专利 P、汇编 G、其他 Z。
  拿不准是期刊就填 J。
- source：来源。期刊论文填**刊名**，专著填**出版社**，学位论文填**学位授予单位**，
  会议论文填**论文集名**，报纸填**报纸名**。出版社 / 学校 / 报纸名都写在这一项里，
  **不要写进 place**（见下一条）。
- place：**地点**（城市名，如 "北京"、"Boston"）。专著、学位论文、会议论文填；
  期刊论文与报纸留空。只填地点，不要在这里重复出版社或学校名。
- edition：**版本项**，只对**专著**填（如 "第3版"、"修订本"、"2nd ed."），
  其余类型留空。不要填 "初版"（第 1 版不必著录版本项）。
- publish_date：**报纸**（source_type = N）的出版日期，填**日**级精度、写成
  "2023-05-04"。其余类型留空 —— 不要因为抽出的是报纸就把日期塞进 year。
- page_range：只填页码本身（如 "56-75"），不要带 "pp."、"p." 或 "页"。
- volume / issue：只填编号本身，不要带 "Vol."、"No."、"第"、"期"、"卷" 这些字。
- year：只填 4 位年份，不要带月份或"出版于"之类的字。
- title_en：把 title **译成英文**（不是原文的另一种写法）。title 本身已经是英文时
  输出空字符串。中文文献要给出准确的英文译名 —— 它会被印进英文论文的参考文献列表
  （APA / MLA 对非英语文献要求「原文题名 [英译]」）。

片段里确实没有的信息，**一律输出空字符串**（如 "authors": ""），不要写
「未提及」「未提供」「未知」「不详」「Not specified」「N/A」这类占位词：
它们是普通文本，会被当成真实作者名、真实年份，直接印进论文的参考文献列表。

文献片段：
{content}
"""


async def extract_metadata(first_page_text: str, filename: str) -> dict:
    """提取文献元数据。若 LLM 未配置，返回基于文件名的兜底。

    **调用失败时抛异常，不吞**：失败必须让调用方知道。原来这里有个裸 except，
    把「提取失败」降级成「提取成功但除题名外全空」，于是一次失败的提取在界面上
    长得和一篇真的没有作者的文献一模一样，用户没有任何理由去重试。承接方在
    `routers/projects.py` 的 `_run_parse`：逐篇 try，失败计入 failed 并继续下一篇。

    注意兜底分支的分界：**未配置** LLM 是「本来就没有这个能力」，不是失败，
    所以它照旧返回基于文件名的兜底，不抛。
    """
    if not llm.is_configured:
        return {
            "title": filename.rsplit(".", 1)[0],
            "title_en": "",
            "authors": "",
            "year": "",
            "source": "",
            "volume": "",
            "issue": "",
            "page_range": "",
            "source_type": "",
            "place": "",
            "edition": "",
            "publish_date": "",
            "summary": "",
        }
    # 先合并硬换行再截断：反过来的话，一个被换行切开的作者串会正好落在截断点上，
    # 而合并又把它接回来 —— 白接。上限见 MAX_EXTRACT_CHARS 处的注释。
    content = merge_hard_wraps(first_page_text)[:MAX_EXTRACT_CHARS]
    prompt = PARSE_PROMPT.format(content=content)
    data = await llm.chat_json([{"role": "user", "content": prompt}])
    if not isinstance(data, dict):
        # 模型返回了非对象（比如一个数组）：这是坏输出，不能当成一份空元数据用 ——
        # 那等于把「模型没按要求回答」伪装成「这篇文献什么都没写」。
        raise ValueError(f"元数据提取返回了非对象：{type(data).__name__}")
    data.setdefault("title", filename)
    data.setdefault("summary", "")
    # authors / year / source 交给 clean_meta_fields 统一落值（缺键也会补成 ""），
    # 所以这里不必再 setdefault 一遍。**必须代码级清洗而不是只靠提示词约束**：
    # setdefault 只在「键不存在」时兜底，而「未提及」是个非空字符串，它拦不住；
    # 模型偶尔不听话本是常态（参见 polish_agent 只靠提示词保护角标的教训）。
    return metadata.clean_reference_fields(metadata.clean_meta_fields(data))
