"""个人著者的规范化：GB/T 7714-2015 / APA 7 / MLA 9 三种形态。

**切分姓名的那一层是共用的，渲染形态分三份。** 一份规范化输出不可能同时满足三种：
GB/T 要 `SMITH J K`（姓全大写、名缩写不加点），APA 7 要 `Smith, J. K., & Doe, A.`
（姓前名后、缩写带点、末位用 `&`），MLA 9 要 `Smith, John K., and Alice Doe.`
（名全写、末位用 `and`）。硬做成通用的只会让三种格式都错，所以共用的是
「哪一截是姓、哪一截是名」这个判断（`_split_person`），三份渲染各写各的。

规则（GB/T 7714-2015 顺序编码制）：

- 姓前名后，**不因语言而变**：`John K. Smith` → `SMITH J K`
- 姓全大写；名缩写为首字母、大写、**不加缩写点**、多个首字母之间用空格分隔
- 著者之间用逗号，**不用** `and` / `&` / `和`
- ≤3 人全部列出；>3 人只列前 3 位，后接 `, 等`（中文）或 `, et al.`（英文，正体）
- `等` / `et al.` 按**该条文献的语言**选，不按界面语言 —— 于是中文作者写的外文
  文献（`ZHANG Wei, LI Ming`）正确地得到 `et al.`，而中文文献得到 `等`

APA 与 MLA 的两条人数据规则（都不是「三位截断」那一种，别串了）：

- **APA**：≤20 人全列，末位前用 `&`；≥21 人只列**前 19 位 + `…` + 末位**（§9.8），
  且此时末位前**不加 `&`**
- **MLA**：1 人只列本人；2 人 = 首位倒装 + `, and ` + 次位**自然序**；
  ≥3 人 = 首位倒装 + `, et al.`

**中文著者在三种格式里都原样保留**（`张三` 不缩写、不倒装、不罗马化）：程序没有
把中文名转写成拼音的能力，硬拆会把「张三」拆成姓张名三而印成 `三 张`。这条与
「中文期刊的 GB/T 条目本来就写中文名」是一致的。

**输入形态的三种来源**（真实语料里混着出现，必须都能认）：

1. 自然序原文（英文 PDF 首屏的常见形态）：`John K. Smith, Anna Doe, and R. Roe`
2. 逗号倒装（APA 风格或数据库导出）：`Smith, J. K., Doe, A., & Roe, R.`
3. 已规范（本程序自己写过一轮的，或作者手工填的）：`SMITH J K, DOE A`

第 3 种意味着 APA / MLA 拿到手的常常是**全大写且只有缩写**的姓名（用户先看过
GB/T 版本、或者库里存的就是那一份）。所以两种渲染都要把全大写的词还原成常见写法
（`SMITH` → `Smith`，`J` → `J.`）—— 不还原的话 APA 会印出 `SMITH, J. K., & DOE, A.`，
读者一眼就知道是个没处理过的字段。代价是 `McDonald` 这类**内部有大写**的写法一旦
被全大写化就回不来了（`MCDONALD` → `Mcdonald`）；这是有损的，但比整串全大写好。

**刻意不处理的边界**（写在这里，别当成漏了）：

- 单个词、且非 CJK 的著者串**原样保留**。它既可能是机构著者
  （`World Health Organization`，GB/T 要求保持原形、不全大写），也可能是只写了
  姓的个人 —— 后者印成 `Smith` 而不是 `SMITH` 是这条启发式的代价。宁可如此，
  也不要把机构名拧成 `ORGANIZATION WORLD HEALTH`。
- 三个以上纯单词、且一个缩写点都没有的串，同样按机构处理、原样保留。
- 「逗号隔开、两边都是裸姓」无法与倒装区分：`Smith, Jones` 会被读成**一个人**
  （`SMITH J`）。这条与「`Smith, John K.` 是一个人」直接冲突，而后者常见得多，
  所以取后者。两位著者的英文文献几乎都写成 `Smith, J., & Jones, A.` 或
  `Smith; Jones`，两种形态都不受影响。
- 世代后缀会被当成另一个人：`John Smith, Jr.` → `SMITH J, Jr.`。本轮不处理。
"""
from __future__ import annotations

import re
from typing import NamedTuple

# 著者间分隔符：显式列举。`and` / `&` 是英文文献的常见写法，其余是中英文标点。
# **逗号不在这里** —— 它在英文里既当分隔符（`Smith, Doe`）又当姓名内部的倒装符
# （`Smith, J. K.`），必须先切再按形态合并，见 _split_authors。
_AUTHOR_SEPARATORS = re.compile(
    r"\s*;\s*|\s*；\s*|\s*、\s*|\s*&\s*|\s+and\s+|\s*和\s*", re.IGNORECASE
)

# CJK 统一表意文字（含扩展 A 与兼容区）。用它判断「这条文献是不是中文的」——
# 该判断同时决定第 4 步用 `等` 还是 `et al.`。
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")

# 单个缩写：一个字母 + 可选的点。`K` / `K.` 都算。
_INITIAL = re.compile(r"^[A-Za-z]\.?$")

# 连字符/长破折号：连字符名的分隔点。GB/T 按每段各取一个首字母，APA 保留连字符。
_NAME_HYPHEN = re.compile(r"[-‐-―−]")


class _Person(NamedTuple):
    """切分后的一个著者。

    `verbatim` 非空 ⇒ **整块原样输出**，三种格式都是（机构著者、单个词、中文姓名）。
    它是「切不开」这个判断的载体：切不开的时候不能假装切开了，否则机构名会被拧成
    `ORGANIZATION WORLD HEALTH` 这种一眼假的东西。
    """

    surname: list[str]
    given: list[str]
    verbatim: str


# --------------------------------------------------------------- 共用：切分

def _split_person(chunk: str) -> _Person:
    """把一个著者切成（姓, 名）或标记为不可切分。三种格式共用这一份判断。"""
    # 中文姓名本来就是姓前名后，不缩写、不全大写 —— 原样返回。
    if _CJK.search(chunk):
        return _Person([], [], chunk.strip())

    tokens = chunk.split()
    if not tokens:
        return _Person([], [], "")
    if len(tokens) == 1:
        # 机构名，或只写了姓。两者都无法与彼此区分，一律原样保留（见模块注释）。
        return _Person([], [], tokens[0])

    has_initial = any(_INITIAL.match(t) for t in tokens)

    if "," in chunk or "，" in chunk:
        # 倒装形态（`Smith, J. K.`）：逗号之前是姓，之后全是名。
        # 走到这里说明 _is_continuation 已经把「名」的碎片合进同一块了，
        # 所以这里的逗号只可能是倒装符，不是著者分隔符。
        parts = [p.strip() for p in re.split(r"[,，]", chunk) if p.strip()]
        surname = parts[0].split()
        given_tokens = [t for p in parts[1:] for t in p.split()]
    elif _INITIAL.match(tokens[-1]):
        # 末词是缩写 ⇒ 它**不可能是姓** ⇒ 这不是自然序，是「姓在前」的形态。
        # 这一条同时覆盖了 `SMITH J K`（已规范，规范化因此幂等）与作者手工填的
        # `Smith J.`，而且不需要去猜首词是不是全大写 —— `JOHN K SMITH` 这类
        # 全大写自然序因此能被正确读成 `SMITH J K`，全大写判据做不到。
        surname, given_tokens = [tokens[0]], tokens[1:]
    elif len(tokens) >= 3 and not has_initial:
        # 三个以上纯单词、无缩写点：当机构名，别动它。
        return _Person([], [], chunk.strip())
    else:
        # 自然序（`John K. Smith`）：姓是最后一个「词」，其前是名。
        # 荷兰/德语等前置小品词（van der Waals）跟着姓走。
        i = len(tokens) - 1
        surname = [tokens[i]]
        i -= 1
        while i >= 0 and (tokens[i][:1].islower() or tokens[i].lower() in _PARTICLES):
            surname.insert(0, tokens[i])
            i -= 1
        given_tokens = tokens[: i + 1]

    return _Person(surname, given_tokens, "")


def _people(raw: str) -> tuple[str, list[_Person]]:
    """从著者串得到（原始文本, 切好的著者表）。原始文本用于判「是不是中文文献」。"""
    text = (raw or "").strip()
    if not text:
        return "", []
    return text, [_split_person(c) for c in _split_authors(text)]


def _split_authors(text: str) -> list[str]:
    """切成一个个**人**。先按显式分隔符切，再在每一块里处理逗号。"""
    out: list[str] = []
    for chunk in _AUTHOR_SEPARATORS.split(text):
        chunk = (chunk or "").strip()
        if not chunk:
            continue
        out.extend(_split_intra_chunk(chunk))
    return out


def _split_intra_chunk(chunk: str) -> list[str]:
    """处理一块内部的逗号：切完之后把「续写」的碎片合回上一个人。

    `Smith, J. K.` 切出来是 `["Smith", "J. K."]`，而它是**一个人**；
    `SMITH J K, DOE A` 切出来是两个人。区别只在碎片形态，见 _is_continuation。

    中文单独走一条：中文姓名里不含逗号，所以逗号在中文串里**只可能是著者分隔符**，
    不必猜。分开走不是为了省事 —— 让中文串进下面那套启发式，`张三, 李四`（两个人）
    会撞上「两个碎片、首碎片只有一个词 ⇒ 倒装」那条规则而被读成一个人。
    """
    if "," not in chunk and "，" not in chunk:
        return [chunk]
    frags = [f.strip() for f in re.split(r"[,，]", chunk) if f.strip()]
    if not frags:
        return []
    if _CJK.search(chunk):
        return frags

    # 倒装形态的第一种：`Smith, J. K.` —— 逗号后是缩写，_is_continuation 认得出。
    # 第二种：`Smith, John K.` —— 名写全了、不带缩写点，认不出，只能用形态兜：
    # 「恰好两个碎片、且首碎片只有一个词」在英文文献里几乎只可能是倒装的一个人
    # （真实语料里两人之间要么写 and/&，要么后一个人带缩写点）。
    if len(frags) == 2 and len(frags[0].split()) == 1:
        return [f"{frags[0]}, {frags[1]}"]

    merged = [frags[0]]
    for frag in frags[1:]:
        if _is_continuation(frag):
            # **合并时必须留着逗号**：不留的话 `Smith, J. K.` 会变成 `Smith J. K.`，
            # 与自然序 `John K. Smith` 形态完全相同而姓的位置相反 —— 于是被当成
            # 自然序处理，输出 `K. S J` 这种把名字当姓的结果。
            merged[-1] = f"{merged[-1]}, {frag}"
        else:
            merged.append(frag)
    return merged


def _is_continuation(frag: str) -> bool:
    """逗号后面这一截，是上一截那个名字的续写（名/缩写），还是另一个人（姓）？

    两个信号：整段都是缩写（`J. K.` / `J K`），或小写开头（`van` / `de`）。
    真实姓氏几乎不以小写字母开头，所以这条判据很稳。
    """
    toks = frag.split()
    if toks and all(_INITIAL.match(t) for t in toks):
        return True
    return frag[:1].islower()


# --------------------------------------------------------------- 大小写还原

def _cased(token: str) -> str:
    """把全大写的姓/名还原成常见写法；本来就大小写混合的**原样不动**。

    只动全大写的词，是为了不碰 `McDonald` / `O'Brien` 这类作者自己就写对了的形态 ——
    回过头来看，正是「已经大小写正确」这件事让它们不必被处理。
    """
    if not token.isupper():
        return token
    if token.lower() in _PARTICLES:
        # 前置小品词保持小写（`VAN DER WAALS` → `Van der Waals`）
        return token.lower()
    return "-".join(
        seg[:1] + seg[1:].lower() for seg in _NAME_HYPHEN.split(token)
    )


def _cased_all(tokens: list[str]) -> str:
    return " ".join(_cased(t) for t in tokens)


# --------------------------------------------------------------- GB/T 7714

def normalize_authors(raw: str) -> str:
    """把著者串规范化成 GB/T 7714 形态。空串进、空串出。"""
    text, people = _people(raw)
    if not people:
        return ""
    normalized = [_gb7714_one(p) for p in people]
    if len(normalized) > 3:
        # GB/T：超过 3 人只著录前 3 位，其后加「, 等」/「, et al.」。
        # 4 人以上才截断，3 人整列 —— 这是最容易写错的一条（对 2 人也加 et al. 就错了）。
        marker = ", 等" if _CJK.search(text) else ", et al."
        return ", ".join(normalized[:3]) + marker
    return ", ".join(normalized)


def _gb7714_one(p: _Person) -> str:
    if p.verbatim:
        return p.verbatim
    surname_text = " ".join(p.surname).upper()
    initials = _initials(p.given)
    return f"{surname_text} {initials}".strip() if initials else surname_text


def _normalize_one(chunk: str) -> str:
    """规范化**一个**著者成 GB/T 形态（保留这个名字是为了少改调用点与注释）。"""
    return _gb7714_one(_split_person(chunk))


def _initials(tokens: list[str]) -> str:
    """名 → 首字母序列，空格分隔、不带缩写点。`John K.` → `J K`。

    连字符名按每一段各取一个首字母（GB/T 如此）：`Jean-Luc` → `J L`。
    """
    out: list[str] = []
    for tok in tokens:
        for seg in _NAME_HYPHEN.split(tok):
            seg = seg.strip(".")
            if seg:
                out.append(seg[0].upper())
    return " ".join(out)


# --------------------------------------------------------------- APA 7

def normalize_authors_apa(raw: str) -> str:
    """把著者串规范化成 APA 7 的形态（不含末尾句点，那是调用方补的）。

    `Smith, J. K., & Doe, A.` / 21 人以上 `A, B, …, Z`（前 19 位 + `…` + 末位，无 `&`）。
    """
    _, people = _people(raw)
    if not people:
        return ""
    rendered = [_apa_one(p) for p in people]
    if len(rendered) > 20:
        return ", ".join(rendered[:19]) + ", … " + rendered[-1]
    if len(rendered) == 1:
        return rendered[0]
    # 末位前用 `&`（不是 `and`、也不是逗号）—— APA 与 MLA 在这一处最容易串。
    return ", ".join(rendered[:-1]) + ", & " + rendered[-1]


def _apa_one(p: _Person) -> str:
    if p.verbatim:
        return p.verbatim
    surname = _cased_all(p.surname)
    initials = _apa_initials(p.given)
    return f"{surname}, {initials}" if initials else surname


def _apa_initials(tokens: list[str]) -> str:
    """名 → APA 的缩写：`John K.` → `J. K.`；连字符名保留连字符（`Jean-Luc` → `J.-L.`）。"""
    out: list[str] = []
    for tok in tokens:
        segs = [s.strip(".") for s in _NAME_HYPHEN.split(tok)]
        segs = [s for s in segs if s]
        if segs:
            out.append("-".join(f"{s[0].upper()}." for s in segs))
    return " ".join(out)


# --------------------------------------------------------------- MLA 9

def normalize_authors_mla(raw: str) -> str:
    """把著者串规范化成 MLA 9 的形态（不含末尾句点，那是调用方补的）。

    `Smith, John K.` / 2 人 `Smith, John K., and Alice Doe` /
    3 人以上 `Smith, John K., et al.`
    """
    _, people = _people(raw)
    if not people:
        return ""
    first = _mla_inverted(people[0])
    if len(people) == 1:
        return first
    if len(people) == 2:
        # 只有首位倒装，第二位**自然序** —— 两位都倒装是最常见的写错。
        return f"{first}, and {_mla_natural(people[1])}"
    return f"{first}, et al."


def _mla_inverted(p: _Person) -> str:
    if p.verbatim:
        return p.verbatim
    surname = _cased_all(p.surname)
    given = _mla_given(p.given)
    return f"{surname}, {given}" if given else surname


def _mla_natural(p: _Person) -> str:
    """名在前、姓在后（MLA 里除首位之外的著者如此，中文姓名也落在这条上）。"""
    if p.verbatim:
        return p.verbatim
    return f"{_mla_given(p.given)} {_cased_all(p.surname)}".strip()


def _mla_given(tokens: list[str]) -> str:
    """名前全写：`John K.` → `John K.`。

    单个字母仍然补点：库里存着 GB/T 那一轮规范化过的串（`SMITH J K`）时，名只剩
    一个字母，而 MLA 的缩写带点 —— 不补的话会印出 `Smith, J K` 这种谁都不用的写法。
    全写的名一律不动（`John` 不是缩写，不该变成 `J.`）。
    """
    out: list[str] = []
    for tok in tokens:
        segs = [_cased(s) for s in _NAME_HYPHEN.split(tok) if s]
        if not segs:
            continue
        if all(_INITIAL.match(s) for s in segs):
            out.append("-".join(s.rstrip(".") + "." for s in segs))
        else:
            out.append("-".join(segs))
    return " ".join(out)


# 姓氏前置小品词。它们小写时已经能被 `.islower()` 识别；这里额外收录首字母大写的
# 形态，用于 `Van Der Berg` 这类写在自然序里的小品词。
_PARTICLES = frozenset({
    "van", "von", "de", "del", "della", "der", "den", "la", "le", "di", "da",
    "dos", "das", "bin", "ibn", "ter", "ten", "op",
})
