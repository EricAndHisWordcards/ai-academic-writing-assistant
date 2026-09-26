"""测试：前端兜底镜像与后端 paper_types 必须逐字段一致。

App.jsx 里的 FALLBACK_PAPER_TYPES / FALLBACK_TYPE_LABELS / FALLBACK_TYPE_CONFIG 是
/api/meta 到达前（或它失败时）前端**唯一**的依据。两边各存一份清单是必要的 ——
元信息是异步来的，首屏必须有东西可渲染 —— 而这份必要性正是漂移的来源：改了后端
忘了改前端，下拉框会少一个类型、步骤条会缺一步，界面看上去却一切正常。

写作语言与著录格式的几张 FALLBACK_* 表同理（见各自的用例）。

**位置不参与断言**：这些表住在 `App.jsx` 还是 `constants.js` 都可以，夹具读的是整个
`src/` 树（见 `jsx`）。要说的是数据一不一致，不是它被摆在哪个文件里。

所以这里把前端源码当**文本**读，不跑 node：它本来就是一份数据镜像，要断言的是数据。
"""
import re
from pathlib import Path

import pytest

from app import paper_types

APP_JSX = Path(__file__).resolve().parents[2] / "frontend" / "src" / "App.jsx"
SRC_DIR = APP_JSX.parent

# 前端只镜像它真用得上的字段。structure_note / writing_note 是进提示词的文本，
# 前端从不读，抄一份过去是纯粹的漂移来源（App.jsx 里有同一条注释）。
FRONTEND_FIELDS = [
    "design_label", "design_fields", "structure_from_literature",
    "citations_required", "flow_hint",
]


@pytest.fixture(scope="module")
def jsx() -> str:
    """**整个前端源码树**拼接（App.jsx 排在最后）。

    这些断言要证的是"前端那份镜像与后端逐字段一致"，而这件事与那份镜像住在哪个文件
    无关。v1.32 把 App.jsx 拆成多个文件（常量搬到了 `constants.js`）之后，只读 App.jsx
    会直接找不到 `const NAME = ...` 而报错；读全树则一条都不用改。

    与 test_frontend_conventions.py 那份同名夹具的差别：这里**不去注释** —— 这些
    helper 解析的是数据表本身，注释本来就在表体内（例如 FALLBACK_TYPE_CONFIG 每个类型
    条目上方的说明），去掉反而会改变解析结果。

    App.jsx 排最后：`_config_entries` 用 `tail.index("\n}\n", ...)` 找表尾，把 App 放在
    末尾可以让"最后一个表"的右边界与拆分前保持一致。
    """
    files = sorted(p for p in SRC_DIR.rglob("*") if p.suffix in (".js", ".jsx"))
    ordered = [p for p in files if p.name != "App.jsx"] + [APP_JSX]
    parts = []
    for path in ordered:
        assert path.exists(), f"找不到前端源码：{path}"
        parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _const_list(jsx: str, name: str) -> list[str]:
    m = re.search(rf"const {name} = \[(.*?)\]", jsx, re.S)
    assert m, f"App.jsx 里找不到 {name}"
    return re.findall(r"'([^']*)'", m.group(1))


def _config_entries(jsx: str) -> dict[str, str]:
    """把 FALLBACK_TYPE_CONFIG 拆成 {类型名: 该类型那几行}。

    按「两个空格缩进的 '类型名': {」切分，而不是真的解析 JS —— 要断言的是数据，
    为此引一个 JS 解析器不划算，也不该让测试跟着前端语法变。
    """
    tail = jsx[jsx.index("const FALLBACK_TYPE_CONFIG = {"):]
    entries = list(re.finditer(r"\n  '([^']+)': \{\n", tail))
    assert entries, "FALLBACK_TYPE_CONFIG 里一个类型条目都没找到"
    out: dict[str, str] = {}
    for i, m in enumerate(entries):
        end = entries[i + 1].start() if i + 1 < len(entries) else tail.index("\n}\n", m.end())
        out[m.group(1)] = tail[m.end():end]
    return out


def _steps_of(body: str, jsx: str) -> list[str]:
    m = re.search(r"steps: (DESIGN_STEPS|\[[^\]]*\])", body)
    assert m, "类型条目里没有 steps"
    return _const_list(jsx, "DESIGN_STEPS") if m.group(1) == "DESIGN_STEPS" \
        else re.findall(r"'([^']*)'", m.group(1))


def _decode_js(raw: str):
    if raw.startswith("'"):
        return raw[1:-1]
    if raw.startswith("["):
        return re.findall(r"'([^']*)'", raw)
    return raw == "true"


def _balanced(jsx: str, name: str, opener: str, closer: str) -> str:
    """把 `const NAME = <opener> ... <closer>` 的表体切出来。

    这里数字符配平，不找「行首的收尾」—— 这几张表有的写成单行
    （`{ zh: '中文', en: '英文' }`），那个收尾的 `}` 根本不在行首，按行切会切出空串，
    于是断言以「键少了」的形式红，看不出是解析器的问题。

    两种括号各要一份：多数表是对象（`FALLBACK_WRITING_LANG_LABELS`），而
    `DOC_EDIT_FIELDS` 是**数组**（每项一个 `{ key, label, hint }`，顺序即界面顺序）。
    只支持一种的话，另一种会以 `substring not found` 的形式红 —— 一条看不懂的红。
    """
    head = f"const {name} = {opener}"
    start = jsx.index(head) + len(head)
    depth = 1
    for i, ch in enumerate(jsx[start:], start):
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return jsx[start:i]
    raise AssertionError(f"App.jsx 里 {name} 的括号没配平")


def _object_block(jsx: str, name: str) -> str:
    """`const NAME = { ... }` 的表体。"""
    return _balanced(jsx, name, "{", "}")


def _array_block(jsx: str, name: str) -> str:
    """`const NAME = [ ... ]` 的表体。"""
    return _balanced(jsx, name, "[", "]")


def _bare_pairs(block: str) -> dict[str, str]:
    """`{ key: 'value' }` 形状（键是裸标识符、与 FALLBACK_TYPE_LABELS 的引号键不同）。"""
    return dict(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*'([^']*)'", block))


def _by_lang_pairs(block: str) -> dict[str, list[str]]:
    """`{ zh: ['a', 'b'], en: ['a'] }` 形状 → {语言: [格式…]}，顺序照原样保留。"""
    return {
        lang: re.findall(r"'([^']*)'", items)
        for lang, items in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*\[([^\]]*)\]", block)
    }


def test_fallback_paper_types_match_the_backend(jsx):
    """兜底类型清单与后端 PAPER_TYPES 一致，**顺序也算**。

    顺序是下拉框的显示顺序（用户按自己的专业找类型），与骨架表的书写顺序无关 ——
    所以比的是列表相等，不是集合相等。
    """
    assert _const_list(jsx, "FALLBACK_PAPER_TYPES") == paper_types.PAPER_TYPES


def test_fallback_type_labels_match_the_backend(jsx):
    """短名 → 长名的映射整份一致：漏一个类型，它的下拉项就会显示成裸的短名。"""
    tail = jsx[jsx.index("const FALLBACK_TYPE_LABELS = {"):]
    block = tail[: tail.index("\n}\n")]
    pairs = dict(re.findall(r"^\s*'([^']+)': '([^']*)',?$", block, re.M))
    assert pairs == paper_types.TYPE_LABELS


def test_default_type_is_a_key_of_both_tables(jsx):
    """DEFAULT_TYPE_KEY 必须在两张表里都存在。

    configFor 兜底时用它：它若是个已下线的名字，兜底本身就会返回 undefined，
    界面上出现的是一个没有步骤的类型。
    """
    m = re.search(r"const DEFAULT_TYPE_KEY = '([^']*)'", jsx)
    assert m, "App.jsx 里找不到 DEFAULT_TYPE_KEY"
    key = m.group(1)
    assert key in paper_types.PAPER_TYPES
    assert key in _config_entries(jsx)


@pytest.mark.parametrize("paper_type", paper_types.PAPER_TYPES)
def test_fallback_config_matches_the_backend(jsx, paper_type):
    """每一类的每个字段都要与 TYPE_CONFIG 对上，逐字段比而不是比整块。

    比整块会在**加字段**时逼着人改这一行断言（然后顺手把镜像也抄错）；逐字段比
    则让新字段自动纳入检查 —— 这正是这类镜像最容易漂的地方。
    """
    body = _config_entries(jsx)[paper_type]
    cfg = paper_types.config_for(paper_type)

    assert _steps_of(body, jsx) == cfg["steps"], f"{paper_type} 的工序前后端不一致"

    for field in FRONTEND_FIELDS:
        expected = cfg.get(field)
        m = re.search(rf"{field}: ('[^']*'|\[[^\]]*\]|true|false)", body)
        # 后端为假值时前端可以干脆不写：JS 里 undefined 与 false/""/[] 同为假，
        # 而「省略」正是这两份清单读起来不啰嗦的原因。
        if not expected:
            actual = _decode_js(m.group(1)) if m else None
            assert not actual, f"{paper_type}.{field} 后端为假值，前端却写着 {actual!r}"
            continue
        assert m, f"{paper_type}.{field} 前端没有镜像"
        assert _decode_js(m.group(1)) == expected, f"{paper_type}.{field} 前后端不一致"


def test_frontend_mirrors_missing_meta_values(jsx):
    """文献元数据占位词清单前后端逐字一致。

    前端文献列表（ResourceStep）直接渲染库里的 authors / year，导出兜底分支也自己
    拼了一遍参考文献 —— 这两处若与后端判定不同源，同一条脏数据会在界面上活下来。
    两端一样都是**显式列举**，所以这里比的是集合相等，多一个少一个都算不一致。
    """
    from app import metadata

    assert set(_const_list(jsx, "MISSING_META_VALUES")) == metadata.MISSING_META_VALUES


def test_frontend_mirrors_source_type_whitelist(jsx):
    """文献类型标识白名单（含顺序）与后端同源。

    前端消费它的只有一处：文献编辑界面的类型下拉框，选项由它派生。**下拉框是这里最
    要紧的** —— 白名单抄错一个字母，用户就能从界面上选到一个后端会 400 拒掉的类型；
    少一个字母则那个合法类型永远选不到。两种都能在界面上看起来完全正常。

    （这里原先还断言 FALLBACK_SOURCE_TYPE，那是导出兜底自己拼参考文献时用的默认值。
    兜底拼接本轮已降级成一句提示、那个常量随之删掉；「越界回落 J」的判据现在只有后端
    `clean_source_type` 一份，不再有第二个落点可与它漂移。）
    """
    from app.citation_format import SOURCE_TYPES

    assert _const_list(jsx, "SOURCE_TYPE_LETTERS") == list(SOURCE_TYPES)


def test_fallback_min_target_words_matches_the_backend(jsx):
    """目标总字数的下限：前端的兜底值与后端那道 400 的判据同值。

    与文档里那句「长度上限前端一个字段都不镜像」不矛盾 —— 上限镜像成 maxLength 会
    静默吃掉粘贴的尾巴，下限镜像出来只是提前把结果告诉用户，后端那道闸照旧在。
    但既然前端确实抄了一份数字（元信息到达前要用），就得锁住：抄错了不会报错，
    只会让界面说出一个后端并不认的数字，用户照着它填、照样被 400 挡回来。
    """
    from app.routers import projects

    m = re.search(r"const FALLBACK_MIN_TARGET_WORDS = (\d+)", jsx)
    assert m, "App.jsx 里找不到 FALLBACK_MIN_TARGET_WORDS"
    assert int(m.group(1)) == projects.MIN_TARGET_WORDS


def test_doc_edit_fields_match_the_backend_limits(jsx):
    """文献编辑表单的字段表与后端 `DOC_FIELD_LIMITS` 逐项对齐（`source_type` 除外）。

    为什么这条值得单写：`DOC_EDIT_FIELDS` **同时是提交范围** —— 编辑界面的 buildPatch
    遍历它来构造 PATCH。所以后端收得下、而它里面没有的字段，用户**根本填不进去**：
    模型没抽到那一项，界面上就永远空着，而界面看起来完全正常（没有报错、没有缺一行的
    违和感，只是那个框不存在）。这是加列时最容易漏、也最难被发现的一环。

    两头对不上的两种方向都拦：
    - 后端有、前端没有 → 该字段在界面上不可填，功能等于没上线；
    - 前端有、后端没有 → `DocumentPatch` 收不下它，PATCH 被**静默忽略**
      （`_validated_doc_patch` 只遍历 `DOC_FIELD_LIMITS`），用户填了保存、刷新就没了。

    `source_type` 是唯一一处**有意不齐**：它是下拉框，单独渲染在字段表之外，
    所以不列在这张表里。这里是把它显式减掉、而不是放宽成「子集」，好让日后再出现
    一个「故意不齐」的字段时必须来改这一行 —— 那正是该被人看见的地方。
    """
    from app.routers import projects

    frontend = set(re.findall(r"key: '([^']+)'", _array_block(jsx, "DOC_EDIT_FIELDS")))
    assert frontend, "App.jsx 里没解析出 DOC_EDIT_FIELDS 的 key"
    assert frontend == set(projects.DOC_FIELD_LIMITS) - {"source_type"}


def test_fallback_writing_lang_mirrors_match_the_backend(jsx):
    """写作语言的兜底镜像与后端 writing_lang 同源。

    前端镜像它的理由是**下拉框不能是空的**：语言选项由 FALLBACK_WRITING_LANGS 派生，
    元信息到达前若派生不出东西，用户看到的是一个空下拉框，比给错一个选项还糟。既然
    抄了一份，就得锁住 —— 这里多一个语种，用户能选到一个后端 normalize 会默默收敛掉
    的语言（界面说英文、产出中文）；少一个语种则那个语言永远选不到，而界面上完全正常。

    `words_unit` 的镜像还多一层：它不只是显示单位，后端 count_units 用**同一个判据**
    决定正文按字还是按词数。镜像写成「英文也按字」，用户会给英文论文填 5000 字，
    拿到的是 5000 个词（约等于中文七千多字的体量），而界面上写着「目标 5000 字」。
    """
    from app import writing_lang

    assert _const_list(jsx, "FALLBACK_WRITING_LANGS") == writing_lang.SUPPORTED_LANGS
    assert _bare_pairs(_object_block(jsx, "FALLBACK_WRITING_LANG_LABELS")) \
        == writing_lang.LANG_LABELS

    m = re.search(r"const FALLBACK_DEFAULT_WRITING_LANG = '([^']*)'", jsx)
    assert m, "App.jsx 里找不到 FALLBACK_DEFAULT_WRITING_LANG"
    assert m.group(1) == writing_lang.DEFAULT_LANG

    assert _bare_pairs(_object_block(jsx, "FALLBACK_WORDS_UNIT_BY_LANG")) == {
        lang: writing_lang.words_unit(lang) for lang in writing_lang.SUPPORTED_LANGS
    }


def test_fallback_citation_formats_match_the_backend(jsx):
    """语言 → 可选著录格式：前端兜底表与后端 citation_format 逐语言一致。

    这是用户这轮那件事（"选英文时选项里不能出现 GB/T"）在**兜底路径**上的落点。元信息
    到达前用的是这张表，所以它若比后端宽（英文里留着 gb7714），用户会在第一帧里看到
    一个后端会 400 拒掉的选项；若比后端窄，一个合法格式会在首屏消失、meta 到了才冒出来。
    两种都不是崩溃，只是界面在一瞬间说了假话 —— 而这种一闪而过的假话没人会报 bug。

    比的是**列表相等**（含顺序）：顺序就是下拉框顺序，而顺序是后端定的。
    """
    from app import writing_lang
    from app.citation_format import FORMAT_LABELS, citation_formats_for, default_format_for

    assert _by_lang_pairs(_object_block(jsx, "FALLBACK_CITATION_FORMATS_BY_LANG")) == {
        lang: citation_formats_for(lang) for lang in writing_lang.SUPPORTED_LANGS
    }
    assert _bare_pairs(_object_block(jsx, "FALLBACK_DEFAULT_CITATION_FORMAT_BY_LANG")) == {
        lang: default_format_for(lang) for lang in writing_lang.SUPPORTED_LANGS
    }
    # 格式标识 → 选项文字。「（默认）」不在这张表里（默认格式随语言变），所以它与后端
    # FORMAT_LABELS 逐键逐值相等，不需要任何本地加工。
    assert _bare_pairs(_object_block(jsx, "FALLBACK_CITATION_FORMAT_LABELS")) == FORMAT_LABELS


def test_fallback_supported_formats_match_the_backend(jsx):
    """全部格式的兜底清单与后端 SUPPORTED_FORMATS 同源（含顺序）。

    它**不是**格式下拉的数据源 —— 那个随语言变，是上面那张
    FALLBACK_CITATION_FORMATS_BY_LANG。这张表的用途只有一个：减掉本语言可选的那几个，
    得到「被本语言排除掉的格式」，好让它们各自说出自己为什么不在下拉框里（英文下的
    GB/T 7714 就是靠它拿到那句话的）。

    所以它比后端**宽**的后果与刚才那张不同：宽出来的那一项会被当成"被排除的格式"，
    在界面多印一句莫名其妙的说明；窄一项则是某个真被排除的格式一句解释都没有。两种都
    不报错，只是一个多话、一个沉默。
    """
    from app.citation_format import SUPPORTED_FORMATS

    assert _const_list(jsx, "FALLBACK_SUPPORTED_FORMATS") == list(SUPPORTED_FORMATS)


def test_fallback_cite_styles_match_the_backend(jsx):
    """角标样式的兜底镜像与后端 citation_format 同源。

    它不随语言变（两种语言都用方括号角标），所以这张表存在的理由与格式那张**不一样**：
    格式下拉是"选项随语言变"所以非镜像不可，这里是为了让**界面上的名字与后端那道 400 里
    的名字同源**。先前是两边各写一份（前端一个手写的 `<option>`、后端一句
    `'、'.join(SUPPORTED_CITE_STYLES)`），恰好写得一样；而报错文案与下拉框对不上时，
    用户拿着「可选：bracket、superscript」找不到自己选过的那一项。

    比集合相等、也比标签逐键逐值相等：多一个键，界面上就多一个后端会 400 拒掉的选项。
    """
    from app.citation_format import CITE_STYLE_LABELS, SUPPORTED_CITE_STYLES

    assert _const_list(jsx, "FALLBACK_CITE_STYLES") == SUPPORTED_CITE_STYLES
    assert _bare_pairs(_object_block(jsx, "FALLBACK_CITE_STYLE_LABELS")) \
        == CITE_STYLE_LABELS


def test_frontend_does_not_mirror_prompt_only_text(jsx):
    """前端不得镜像 structure_note / writing_note。

    它们是进提示词的文本，前端一个字节都不读。抄一份过来除了制造漂移没有别的作用，
    而且两份会长期长得一模一样，直到某天只改了一边。
    """
    for field in ("structure_note", "writing_note"):
        assert f"{field}:" not in jsx, f"{field} 不该出现在前端"


def _set_entries(jsx: str, name: str) -> list[str]:
    """把 `const NAME = new Set([...])` 里的字符串项取出来。"""
    m = re.search(rf"const {name} = new Set\(\[(.*?)\]\)", jsx, re.S)
    assert m, f"App.jsx 里找不到 {name}"
    return re.findall(r"'([^']*)'", m.group(1))


def _status_anchor(jsx: str) -> dict[str, str]:
    """把 STATUS_ANCHOR 拆成 {状态名: 锚点 step key}。键是未加引号的标识符。"""
    m = re.search(r"const STATUS_ANCHOR = \{(.*?)\n\}", jsx, re.S)
    assert m, "App.jsx 里找不到 STATUS_ANCHOR"
    return dict(re.findall(r"^\s*([a-z_]+):\s*'([a-z_]+)',?$", m.group(1), re.M))


def test_frontend_status_words_match_backend_project_status(jsx):
    """前端所有「状态词表」都必须与后端 ProjectStatus 对账（T5）。

    STATUS_ANCHOR 是全量状态→锚点映射，statusToStepKey 靠它找锚点；改错一个状态值
    （拼错、改名）会让用户刷新后 statusToStepKey 退回 topic 第一步，且无人报错。
    所以后端状态机的每个值都必须在表里、表里也不许有后端不认的值，锚点指向的 step
    也必须合法。ADVANCE_AFTER / STEP_DONE_STATUSES 是它的子集，逐一核对不外逃。
    """
    from app.db import ProjectStatus

    anchors = _status_anchor(jsx)
    assert set(anchors) == set(ProjectStatus.ALL), (
        f"STATUS_ANCHOR 与后端状态机不一致：缺 {set(ProjectStatus.ALL) - set(anchors)}，"
        f"多 {set(anchors) - set(ProjectStatus.ALL)}"
    )
    assert len(anchors) == len(ProjectStatus.ALL), "STATUS_ANCHOR 有重复键"

    for name in ("ADVANCE_AFTER", "STEP_DONE_STATUSES"):
        entries = set(_set_entries(jsx, name))
        assert entries <= set(ProjectStatus.ALL), (
            f"{name} 里有后端不认的状态：{entries - set(ProjectStatus.ALL)}"
        )

    assert set(anchors.values()) <= set(paper_types.STEP_KEYS), (
        f"STATUS_ANCHOR 的值里有非法步骤 key：{set(anchors.values()) - set(paper_types.STEP_KEYS)}"
    )
