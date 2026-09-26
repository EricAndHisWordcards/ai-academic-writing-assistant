"""测试：论文类型定义与大纲骨架（paper_types）。"""
import pytest

from app import db, paper_types


def test_paper_type_list_matches_skeletons():
    """类型清单只有一份：下拉框顺序(PAPER_TYPES)与骨架表、工序表三者不得漂移。

    只比对集合——PAPER_TEMPLATES 的书写顺序可以随意（文献综述写在最前便于阅读），
    对用户可见的顺序由 PAPER_TYPES 决定。工序表（TYPE_CONFIG）同样必须受这条约束，
    否则它就成了下一份会漂的清单。
    """
    assert set(paper_types.PAPER_TYPES) == set(paper_types.PAPER_TEMPLATES)
    assert set(paper_types.PAPER_TYPES) == set(paper_types.TYPE_CONFIG)
    assert len(paper_types.PAPER_TYPES) == 7
    assert len(paper_types.PAPER_TYPES) == len(set(paper_types.PAPER_TYPES))
    # 长名表也必须是同一套键，否则下拉框会漏掉某个类型、或显示成裸的短名
    assert set(paper_types.PAPER_TYPES) == set(paper_types.TYPE_LABELS)
    for name, label in paper_types.TYPE_LABELS.items():
        assert label.strip()
        # 类型名会被插进 OUTLINE_PROMPT.format(...)：一个大括号就是一次后台任务里的
        # KeyError，而用户只看到「大纲生成失败」
        assert "{" not in name and "}" not in name
        assert "{" not in label and "}" not in label


def test_steps_are_known_keys_and_ordered_as_expected():
    """每种类型的工序都只能由已知步骤组成，且顺序固定。

    前端按 steps 渲染步骤条；出现拼错的 key 会让某一步在界面上凭空消失，
    而后端的闸门还在，用户就会卡在一个走不到的地方。
    """
    for paper_type, cfg in paper_types.TYPE_CONFIG.items():
        steps = cfg["steps"]
        assert steps, f"{paper_type} 没有工序"
        assert len(steps) == len(set(steps))
        unknown = set(steps) - set(paper_types.STEP_KEYS)
        assert not unknown, f"{paper_type} 出现未知步骤：{unknown}"
        # 选题永远第一步，导出永远最后一步
        assert steps[0] == "topic"
        assert steps[-1] == "export"


def test_literature_first_only_for_review():
    """只有文献综述先文献后大纲：它的章节主题来自文献聚类，不是先验给定的。"""
    assert paper_types.is_literature_first("文献综述") is True
    for other in paper_types.PAPER_TYPES:
        if other != "文献综述":
            assert paper_types.is_literature_first(other) is False, other


def test_citations_required_is_off_for_all_seven_types():
    """本轮策略：七类一律允许跳过引用调度。

    这个断言把**策略**钉住了，不是把实现钉住了。想对某一类收紧时，改 TYPE_CONFIG
    的 citations_required 与本用例，会同时失败在两处 —— 那正是提醒「闸门会跟着变」。
    """
    for paper_type in paper_types.PAPER_TYPES:
        assert paper_types.citations_required(paper_type) is False, paper_type
    # 字段本身保留：它是策略位，删掉等于把「哪类必须引用」散进代码
    for cfg in paper_types.TYPE_CONFIG.values():
        assert "citations_required" in cfg


def test_design_step_matches_design_fields():
    """有 design 步 ⇔ 有 design_fields，且必须有显示名。

    三者对不上就会出现「步骤条上有设计步但表单渲染不出字段」，或者反过来
    「字段配好了却永远走不到」。
    """
    for paper_type, cfg in paper_types.TYPE_CONFIG.items():
        steps = cfg["steps"]
        fields = cfg.get("design_fields")
        if "design" in steps:
            assert fields, f"{paper_type} 有设计步却没有字段"
            assert cfg.get("design_label"), f"{paper_type} 有设计步却没有显示名"
        else:
            assert not fields, f"{paper_type} 没有设计步却配了字段"
            assert paper_types.has_design(paper_type) is False

    # 设计步的分布：五类有，文献综述（结构来自文献）与课程论文（短篇）没有
    with_design = {t for t in paper_types.PAPER_TYPES if paper_types.has_design(t)}
    assert with_design == {
        "定量/实证研究", "质性研究/案例分析", "工程设计/系统实现",
        "理论推导/数理建模", "技术/工程报告",
    }
    assert paper_types.design_fields_for("定量/实证研究") == [
        "数据源与样本", "研究假设", "模型与方法", "主要结果",
    ]
    assert paper_types.design_fields_for("质性研究/案例分析") == [
        "研究对象与个案", "质性方法", "资料来源与编码", "主题与发现",
    ]
    assert paper_types.design_fields_for("理论推导/数理建模") == [
        "基本假设与公理", "推导框架与符号", "关键推导与证明", "机制与结论",
    ]
    assert paper_types.design_label_for("技术/工程报告") == "技术方案"
    assert paper_types.design_label_for("工程设计/系统实现") == "工程设计"
    # 显示名不得重复：步骤条上两个类型显示同一个名字，用户就分不清自己在填什么
    labels = [paper_types.design_label_for(t) for t in with_design]
    assert len(labels) == len(set(labels))
    assert paper_types.design_label_for("课程论文/小论文") == ""
    assert paper_types.design_label_for("文献综述") == ""


def test_every_type_has_structure_and_writing_notes():
    """七类都必须写明「该有什么章节」与「该怎么写」。

    这两个字段是「按类型写作」的全部依据：缺一个，那一类的论文就会拿到一套通用
    措辞 —— 结构上可能还像，写法上一定不像（访谈论文写成实证腔、数学论文写成
    综述腔）。所以非空是硬要求，不是「配了更好」。
    """
    for paper_type in paper_types.PAPER_TYPES:
        note = paper_types.structure_note_for(paper_type)
        assert note and note.strip(), f"{paper_type} 缺 structure_note"
        # 正反两面都要写：只说「必须有什么」，模型仍会把熟悉的另一套结构带进来
        assert "绝不许出现" in note, f"{paper_type} 的 structure_note 没写禁止项"

        writing = paper_types.writing_note_for(paper_type)
        assert writing and writing.strip(), f"{paper_type} 缺 writing_note"


def test_structure_notes_exclude_the_other_paradigms():
    """各类型的结构说明必须点名排除与它最像的那一类，否则模型会就近套用。

    这几条对应的是真实的错位：质性论文被套上假设检验、数学论文被套上描述性统计、
    工程论文被套上实证回归、综述被套上研究设计。
    """
    banned = {
        "质性研究/案例分析": ["假设检验", "回归", "变量与数据"],
        "理论推导/数理建模": ["问卷", "样本", "描述性统计", "回归"],
        "工程设计/系统实现": ["变量与数据", "假设检验"],
        "文献综述": ["研究设计", "假设检验", "描述性统计"],
        "课程论文/小论文": ["研究设计", "假设检验"],
        "技术/工程报告": ["变量与数据", "假设检验"],
    }
    for paper_type, words in banned.items():
        note = paper_types.structure_note_for(paper_type)
        for word in words:
            assert word in note, f"{paper_type} 的结构说明没有排除「{word}」"


def test_only_review_has_a_theme_chapter():
    """主题章只属于文献综述：其余类型的骨架没有「由文献聚类决定」的章节排列。"""
    assert paper_types.theme_section_count("文献综述") == 3
    for other in paper_types.PAPER_TYPES:
        if other != "文献综述":
            assert paper_types.theme_section_count(other) == 0, other


# ---------------------------------------------------------------
# 英文骨架的三条护栏（本轮新增：英文论文能力）
#
# 它们比「翻译得对不对」有用得多 —— 后者没法测，前者能当场抓住真实的抄写事故：
# 漏译一节、权重抄错一位、主题章的定位串没跟着换。这三类都不会报错，
# 只会让英文项目的大纲或字数分配悄悄长歪。
# ---------------------------------------------------------------
@pytest.mark.parametrize("paper_type", paper_types.PAPER_TYPES)
def test_english_skeleton_has_the_same_shape(paper_type):
    """英文骨架与中文**章数、每章节数逐位相同**。漏译一节会当场变红。

    骨架是恒定结构（模型的标题措辞只允许改写、不允许增删），两种语言各写一份，
    唯一能保证它们不漂移的就是这条不变式。
    """
    assert paper_types.skeleton_shape(paper_type, "en") == paper_types.skeleton_shape(
        paper_type
    ), f"{paper_type} 的英文骨架与中文骨架结构不同"


@pytest.mark.parametrize("paper_type", paper_types.PAPER_TYPES)
def test_english_skeleton_keeps_the_same_weights(paper_type):
    """权重也要逐位相同 —— 只对章数节数不够。

    「同一结构在两套语言下拿到不同篇幅比例」没有任何理由，而它是**最难发现**的一种
    抄写偏差：章数节数都对得上，只有某一节悄悄多分了两百字，界面上一切正常。
    """
    zh = [r for _t, r in paper_types.skeleton_leaves(paper_type)]
    en = [r for _t, r in paper_types.skeleton_leaves(paper_type, "en")]
    assert en == zh, f"{paper_type} 的英文骨架权重与中文不同"


@pytest.mark.parametrize("paper_type", paper_types.PAPER_TYPES)
def test_english_skeleton_and_notes_contain_no_cjk(paper_type):
    """英文骨架与两条类型说明里**一个汉字都不能有**。

    骨架标题会被原样印进论文（截图、导出、查重都看得见）；两条说明则逐条点名章节，
    混着中文等于又回到了「按中文名去找章节」的老问题。
    """
    text = "\n".join(paper_types.skeleton_lines(paper_type, "en"))
    text += "\n" + paper_types.structure_note_for(paper_type, "en")
    text += "\n" + paper_types.writing_note_for(paper_type, "en")
    assert "绝不许出现" not in text
    for ch in text:
        # 汉字与全角标点都算：`，` 出现在英文正文里同样说明这一段没译
        assert not ("一" <= ch <= "鿿"), f"{paper_type} 的英文文案里有汉字：{text}"
        assert not ("＀" <= ch <= "￯"), f"{paper_type} 的英文文案里有全角标点"


@pytest.mark.parametrize("paper_type", paper_types.PAPER_TYPES)
def test_theme_section_count_is_language_invariant(paper_type):
    """主题章节数在两种语言下必须相同 —— 这是一条**功能性**回归，不是文案检查。

    `theme_section_count` 靠**子串匹配**在骨架标题里找主题章。英文骨架下如果只翻了
    标题、没换定位串，它会匹配不到任何一章而**静默返回 0**，于是「作者确认的主题数
    多于节数」这段检查被整段跳过 —— 界面上完全看不出异常。
    """
    assert paper_types.theme_section_count(paper_type, "en") == (
        paper_types.theme_section_count(paper_type)
    ), f"{paper_type} 的主题章节数在英文下变了"


def test_paper_type_renames_are_history_only():
    """迁移映射的键**不得**是当前有效类型名。

    相交就意味着每次启动都会把有效行按映射改写一遍 —— 名字恰好没变时看不出来，
    一旦哪天改了映射的值，就成了静默的批量改名。
    """
    for old in paper_types.PAPER_TYPE_RENAMES:
        assert old not in paper_types.PAPER_TYPES, f"{old} 既是旧名又是现名"
        assert paper_types.is_valid(old) is False
    for new in paper_types.PAPER_TYPE_RENAMES.values():
        assert paper_types.is_valid(new) is True, new


def test_label_for_includes_scope_but_short_name_does_not():
    """长名带适用范围、短名不带；未知类型原样返回长名而非空串。

    存储用短名、显示用长名。两者的差别就是括号里那句适用范围，它只帮用户选类型。
    """
    for paper_type in paper_types.PAPER_TYPES:
        label = paper_types.label_for(paper_type)
        assert label.startswith(paper_type)
        assert len(label) >= len(paper_type)
        # 七类**都**该带适用范围：下拉框里只有长名，用户全靠括号里那句选类型
        assert label != paper_type, f"{paper_type} 的长名没写适用范围"
    assert paper_types.label_for("文献综述") == "文献综述（全学科通用）"
    assert "社科" in paper_types.label_for("定量/实证研究")
    # 未知类型：返回短名本身，总比让提示词里出现「论文类型：」后面什么都没有好
    assert paper_types.label_for("毕业论文") == "毕业论文"
    assert paper_types.label_for("") == ""


@pytest.mark.parametrize("paper_type", paper_types.PAPER_TYPES)
def test_skeleton_weights_sum_to_one(paper_type):
    """骨架权重是「比例」，和应为 1.0（不是硬性要求，但漂了说明有人手改错了）。"""
    total = sum(w for _secs in _sections(paper_type) for _t, w in _secs)
    assert abs(total - 1.0) < 1e-6, f"{paper_type} 权重和为 {total}"


@pytest.mark.parametrize("paper_type", paper_types.PAPER_TYPES)
def test_skeleton_is_well_formed(paper_type):
    """每章至少一节，标题非空，叶子数与 skeleton_shape 一致。"""
    tmpl = paper_types.template_for(paper_type)
    assert tmpl
    for ch_title, secs in tmpl:
        assert ch_title.strip()
        assert secs, f"{ch_title} 没有小节"
        for sec_title, ratio in secs:
            assert sec_title.strip()
            assert ratio > 0

    n_ch, n_leaf = paper_types.skeleton_shape(paper_type)
    assert n_ch == len(tmpl)
    assert n_leaf == len(paper_types.skeleton_leaves(paper_type))


def test_literature_review_skeleton_is_thematic():
    """文献综述的骨架必须是主题脉络式，不得混入实证色彩。

    这是本次改动针对的具体缺陷：此前文献综述经常拿到含「研究设计 / 假设检验」
    的实证结构。
    """
    leaves = " ".join(t for t, _ in paper_types.skeleton_leaves("文献综述"))
    chapters = " ".join(c for c, _ in paper_types.template_for("文献综述"))

    assert "研究主题脉络" in chapters
    assert "研究述评" in chapters
    for banned in ("研究假设", "假设检验", "变量与数据", "描述性统计", "研究设计"):
        assert banned not in leaves, f"文献综述骨架混入了实证章节：{banned}"
        assert banned not in chapters


def test_three_new_types_have_type_specific_skeletons():
    """三个新类型的骨架必须各自落在自己的范式里，且章数/节数足够撑起一篇学位论文。

    这是本轮要修的具体缺陷：此前人文社科的质性研究、工科的工程设计、数理学科的
    理论推导**没有入口**，用户只能塞进「实证研究」或「技术报告」，拿到一份结构错位
    的骨架。
    """
    # 质性：有编码/主题，没有统计检验
    qualitative = {
        "chapters": " ".join(c for c, _ in paper_types.template_for("质性研究/案例分析")),
        "leaves": " ".join(t for t, _ in paper_types.skeleton_leaves("质性研究/案例分析")),
    }
    assert "编码" in qualitative["leaves"] or "编码" in qualitative["chapters"]
    assert "主题" in qualitative["leaves"]
    for banned in ("假设检验", "回归", "变量与数据", "描述性统计"):
        assert banned not in qualitative["leaves"] + qualitative["chapters"]

    # 工程设计：需求 → 设计 → 实现 → 测试，四步齐全且顺序不乱
    eng_chapters = [c for c, _ in paper_types.template_for("工程设计/系统实现")]
    eng_text = " ".join(eng_chapters)
    positions = [eng_text.index(w) for w in ("需求分析", "设计", "实现", "测试")]
    assert positions == sorted(positions), f"工程设计章序不对：{eng_chapters}"
    for banned in ("假设检验", "变量与数据", "描述性统计"):
        assert banned not in eng_text

    # 理论推导：假设 → 推导/证明 → 机制
    theo_text = " ".join(
        c for c, _ in paper_types.template_for("理论推导/数理建模")
    ) + " " + " ".join(
        t for t, _ in paper_types.skeleton_leaves("理论推导/数理建模")
    )
    for required in ("假设", "推导", "证明", "机制"):
        assert required in theo_text, f"理论推导骨架缺「{required}」"
    for banned in ("问卷", "样本", "描述性统计", "回归"):
        assert banned not in theo_text, f"理论推导骨架混入了实证章节：{banned}"


def test_is_valid():
    assert paper_types.is_valid("课程论文/小论文") is True
    assert paper_types.is_valid("课程论文") is False  # 旧名已迁移，不再是有效类型
    assert paper_types.is_valid("文献综速") is False
    assert paper_types.is_valid("") is False


def test_unknown_type_falls_back_to_default():
    """未知类型退化为课程论文（保持既有行为）。

    「毕业论文」已下线，但旧库里可能仍存着这个名字 —— 退化而不是抛异常。
    """
    assert paper_types.template_for("不存在的类型") == paper_types.template_for(
        paper_types.DEFAULT_TEMPLATE_KEY
    )
    assert paper_types.config_for("不存在的类型") == paper_types.config_for(
        paper_types.DEFAULT_TEMPLATE_KEY
    )
    for gone in ("不存在的类型", "毕业论文", ""):
        assert paper_types.config_for(gone)["steps"], "退化后也必须是一套可用工序"
        assert paper_types.has_design(gone) is False


def test_skeleton_lines_cover_every_leaf():
    """渲染出的骨架文本必须逐节列出，否则 LLM 看不到完整结构。"""
    for paper_type in paper_types.PAPER_TYPES:
        text = "\n".join(paper_types.skeleton_lines(paper_type))
        for sec_title, _ratio in paper_types.skeleton_leaves(paper_type):
            assert sec_title in text, f"{paper_type} 缺 {sec_title}"


def test_paper_types_is_the_only_source_of_truth():
    """db 模块不再自带一份类型清单。"""
    assert not hasattr(db, "PAPER_TYPES")


def _sections(paper_type):
    return [secs for _ch, secs in paper_types.template_for(paper_type)]
