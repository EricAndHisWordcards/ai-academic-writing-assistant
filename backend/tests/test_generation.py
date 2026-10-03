"""测试：后台分段生成（逐节落盘 / 断点续写 / 引用编号稳定性）。

这些用例直接驱动服务层的协程，不经过 HTTP —— 后台任务的行为（落盘时机、
失败后残留什么、续写从哪里接）用 TestClient 反而更难断言得准。
"""
import asyncio

import pytest

from app import db
from app.routers import projects as projects_router
from app.routers import projects_generation as generation_router


# ---------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------
def _doc(doc_id: str, title: str) -> dict:
    return {
        "id": doc_id,
        "filename": f"{doc_id}.pdf",
        "title": title,
        "authors": "张三",
        "year": "2023",
        "source": "某期刊",
        "summary": f"{title} 的摘要",
        "pages": [{"page": 1, "snippet": f"{title} 的首页片段"}],
    }


def _project(paper_type="课程论文/小论文", target_words=1000) -> str:
    """建一个「大纲已确认 + 已排引用」的项目，返回 project_id。"""
    pid = db.create_project("p1")["id"]
    db.update_project(pid, paper_type=paper_type, target_words=target_words,
                      topic="测试选题")
    outline = {"chapters": [
        {"title": "第一章 引言", "sections": [
            {"title": "1.1 背景", "word_budget": 333, "subsections": []},
            {"title": "1.2 目的", "word_budget": 333, "subsections": []},
        ]},
        {"title": "第二章 正文", "sections": [
            {"title": "2.1 问题分析", "word_budget": 334, "subsections": []},
        ]},
    ]}
    db.update_project(pid, outline_json=outline)

    db.add_document(pid, _doc("d1", "文献一"))
    db.add_document(pid, _doc("d2", "文献二"))
    # 1.2 刻意绑两篇，用来验证全局编号按大纲顺序分配
    binding = {
        "1.1 背景": [{"doc_id": "d1", "doc_title": "文献一", "page": 1}],
        "1.2 目的": [
            {"doc_id": "d2", "doc_title": "文献二", "page": 1},
            {"doc_id": "d1", "doc_title": "文献一", "page": 1},
        ],
    }
    db.update_project(pid, citation_binding_json=binding)
    return pid


def _fake_generate(calls: list, fail_on: int | None = None, seen_materials: dict | None = None,
                   seen_thread: dict | None = None, seen_meta: dict | None = None,
                   seen_lang: dict | None = None):
    """假的章节生成器：记录调用顺序，可选在第 N 次抛错。

    签名必须与 generate_agent.generate_section 对齐（含 materials / design_context /
    design_label / paper_type / writing_note / writing_ideas / core_question /
    writing_lang）。
    **`**_ignored` 兜不住漏传**：真实调用点少传一个关键字参数时它照样收下，断言只有
    显式写出参数名才照得出来 —— 所以每加一个要断言"确实传下来了"的参数，都在这里
    显式列一笔。seen_* 传字典时，顺带记下每节到底收到了什么。
    """
    async def _fn(section_title, word_budget, refs, context, topic,
                  cite_style="bracket", materials=None, design_context="", design_label="",
                  paper_type="", writing_note="", writing_ideas="", core_question="",
                  writing_lang="", **_ignored):
        calls.append(section_title)
        if seen_materials is not None:
            seen_materials[section_title] = materials
        if seen_thread is not None:
            seen_thread[section_title] = (writing_ideas, core_question)
        if seen_meta is not None:
            seen_meta[section_title] = (paper_type, writing_note, design_label, design_context)
        if seen_lang is not None:
            seen_lang[section_title] = writing_lang
        if fail_on is not None and len(calls) == fail_on:
            raise RuntimeError("模拟 LLM 调用失败")
        return {
            "section_title": section_title,
            "word_budget": word_budget,
            "actual_words": len(section_title) * 10,
            "content": f"{section_title} 的正文",
            "references": refs,
        }
    return _fn


@pytest.fixture()
def offline(monkeypatch):
    """绕开真实 LLM：假生成器 + 恒等润色。

    替身的签名必须与真身的对齐（这里含尾参 `writing_lang`）——调用点是
    `polish(drafted, lang)` **按位置**传的，只收一个参数的替身会当场 TypeError，
    而那个异常会被 `_run_generation` 的兜底分支吞成「生成失败」+ 退回可重试状态。
    同一个纪律见 `_fake_generate` 的 docstring。
    """
    from app.agents import generate_agent

    # 必须是协程函数：调用点用 asyncio.wait_for(polish(...)) 包了超时，
    # 同步函数会因「不可 await」直接抛 TypeError。
    async def _identity(text: str, writing_lang: str = "zh") -> str:
        return text

    monkeypatch.setattr(generation_router, "polish", _identity)
    return generate_agent


def _run(pid: str) -> None:
    asyncio.run(projects_router._run_generation(pid))


def test_placeholder_section_is_not_polished(tmp_db, offline, monkeypatch):
    """占位文本不得送进润色环节。

    润色是 LLM 调用，会把「模型没返回正文」这段失败说明改写成读起来像正文的
    学术句子 —— 实测中它被改成了「因当前环境未配置大模型，暂以占位文本呈现」，
    失败就此被伪装成正常段落。
    """
    pid = _project()
    polished_inputs: list[str] = []

    async def _spy(text: str, writing_lang: str = "zh") -> str:
        polished_inputs.append(text)
        return "POLISHED:" + text

    async def _gen(section_title, word_budget, refs, context, topic,
                   cite_style="bracket", materials=None, design_context="",
                   design_label="", **_ignored):
        base = {
            "section_title": section_title,
            "word_budget": word_budget,
            "actual_words": 10,
            "references": refs,
        }
        if section_title == "1.2 目的":
            return {**base, "content": "（占位）模型未返回正文", "fallback": True}
        return {**base, "content": f"{section_title} 的正文"}

    monkeypatch.setattr(offline, "generate_section", _gen)
    monkeypatch.setattr(generation_router, "polish", _spy)

    _run(pid)

    by_title = {s["section_title"]: s for s in db.get_project(pid)["sections_json"]}
    assert by_title["1.2 目的"]["content"] == "（占位）模型未返回正文"
    assert by_title["1.2 目的"]["polished"] is False, "没润色就别标成已润色"
    assert by_title["1.1 背景"]["content"].startswith("POLISHED:")
    assert by_title["1.1 背景"]["polished"] is True
    assert "（占位）模型未返回正文" not in polished_inputs
    # 占位文本也不该成为下一节的上下文
    assert not any("占位" in (s.get("content") or "") for s in
                   db.get_project(pid)["sections_json"] if s["section_title"] == "2.1 问题分析")


# ---------------------------------------------------------------
# 逐节落盘
# ---------------------------------------------------------------
def test_incremental_persist_on_failure(tmp_db, offline, monkeypatch):
    """中途失败时，已完成章节必须已落盘。

    这是对既有缺陷的回归测试：此前 sections_json 只在整轮循环结束后写一次，
    请求超时或进程被杀就全部丢失。
    """
    pid = _project()
    calls: list = []
    monkeypatch.setattr(
        offline, "generate_section", _fake_generate(calls, fail_on=3)
    )

    _run(pid)

    p = db.get_project(pid)
    assert len(calls) == 3, "应恰好尝试了 3 节"
    assert len(p["sections_json"]) == 2, "失败前完成的两节必须保留"
    # 失败后退回可重试状态，而不是把项目永久留在 generating
    assert p["status"] == db.ProjectStatus.CITATION_CONFIRMED
    state = p["generation_state_json"]
    assert state["status"] == "error"
    assert "模拟 LLM 调用失败" in state["error"]


def test_success_writes_references_and_done_state(tmp_db, offline, monkeypatch):
    """成功后落盘参考文献列表并标记 done。"""
    pid = _project()
    monkeypatch.setattr(offline, "generate_section", _fake_generate([]))

    _run(pid)

    p = db.get_project(pid)
    assert p["status"] == db.ProjectStatus.COMPLETED
    assert p["generation_state_json"]["status"] == "done"
    assert p["generation_state_json"]["done"] == 3
    refs = p["references_json"]
    assert [r["num"] for r in refs] == [1, 2]
    assert all(r["formatted"] for r in refs)


# ---------------------------------------------------------------
# 断点续写
# ---------------------------------------------------------------
def test_resume_skips_completed_sections(tmp_db, offline, monkeypatch):
    """已落盘的章节不重跑，也不会被重复追加。"""
    pid = _project()
    pre_done = [{
        "section_title": "1.1 背景",
        "word_budget": 333,
        "actual_words": 300,
        "content": "上一轮写好的正文",
        "references": [],
    }]
    db.update_project(pid, sections_json=pre_done)

    calls: list = []
    monkeypatch.setattr(offline, "generate_section", _fake_generate(calls))

    _run(pid)

    assert calls == ["1.2 目的", "2.1 问题分析"], "只应续写剩下的章节"
    sections = db.get_project(pid)["sections_json"]
    assert len(sections) == 3
    assert sections[0]["content"] == "上一轮写好的正文", "旧内容应原样保留"
    assert sections[0]["section_title"] == "1.1 背景"


def test_citation_numbering_is_stable_across_resume(tmp_db):
    """预扫描编号：续写时跳过的章节不会让后文的角标错位。

    若按「实际遍历顺序」编号，续写那一轮 d2 会从 1 号开始，与正文里已写好的
    [2] 冲突。
    """
    pid = _project()
    p = db.get_project(pid)
    sections = projects_router._collect_ordered_sections(p["outline_json"])
    doc_map = {d["id"]: d for d in db.list_documents(pid)}

    num_by_doc, ref_list = projects_router._plan_citations(
        sections, p["citation_binding_json"], doc_map
    )
    assert num_by_doc == {"d1": 1, "d2": 2}
    assert [r["num"] for r in ref_list] == [1, 2]

    # 第二次运行（前两节已跳过）仍得到同一套编号
    again, _ = projects_router._plan_citations(
        sections, p["citation_binding_json"], doc_map
    )
    assert again == num_by_doc


# ---------------------------------------------------------------
# 作者自有研究材料按节下发
# ---------------------------------------------------------------
def _material(mid="m1", label="问卷数据.xlsx", text="有效样本 312 份"):
    return {"id": mid, "label": label, "kind": "file", "text": text}


def test_materials_reach_the_matching_section(tmp_db, offline, monkeypatch):
    """计划里挂在哪一节，材料就只进哪一节。"""
    pid = _project()
    db.add_material(pid, _material())
    db.update_project(pid, materials_plan_json={"placements": [
        {"section_title": "1.2 目的", "material_ids": ["m1"],
         "usage": "用样本量交代调研规模", "key_points": ["有效样本 312 份"]},
    ], "unused": []})

    seen: dict = {}
    monkeypatch.setattr(offline, "generate_section", _fake_generate([], seen_materials=seen))

    _run(pid)

    entry = seen["1.2 目的"][0]
    assert entry["label"] == "问卷数据.xlsx"
    assert entry["text"] == "有效样本 312 份"
    assert entry["usage"] == "用样本量交代调研规模"
    assert entry["key_points"] == ["有效样本 312 份"]
    assert seen["1.1 背景"] is None, "没被安排材料的章节不该收到材料"
    assert seen["2.1 问题分析"] is None


def test_no_material_plan_means_no_materials(tmp_db, offline, monkeypatch):
    """没做材料分析时，各节收到的 materials 必须是 None —— 提示词与加此功能前一致。"""
    pid = _project()
    db.add_material(pid, _material())

    seen: dict = {}
    monkeypatch.setattr(offline, "generate_section", _fake_generate([], seen_materials=seen))

    _run(pid)

    assert seen and all(v is None for v in seen.values())


def test_stale_material_id_in_plan_is_skipped(tmp_db, offline, monkeypatch):
    """计划引用的材料已被删除时跳过，而不是塞一份空材料进去。"""
    pid = _project()
    db.add_material(pid, _material("m1"))
    db.add_material(pid, _material("m2", label="访谈记录.docx", text="受访者 A 表示…"))
    db.update_project(pid, materials_plan_json={"placements": [
        {"section_title": "1.2 目的", "material_ids": ["m1", "已删除"], "usage": "x",
         "key_points": []},
    ], "unused": []})

    seen: dict = {}
    monkeypatch.setattr(offline, "generate_section", _fake_generate([], seen_materials=seen))

    _run(pid)

    assert [e["label"] for e in seen["1.2 目的"]] == ["问卷数据.xlsx"]


def test_material_for_unknown_section_is_ignored(tmp_db, offline, monkeypatch):
    """计划指向一个当前大纲里不存在的章节时，材料不会漏进别的节。"""
    pid = _project()
    db.add_material(pid, _material())
    db.update_project(pid, materials_plan_json={"placements": [
        {"section_title": "早已改名的章节", "material_ids": ["m1"], "usage": "x",
         "key_points": []},
    ], "unused": []})

    seen: dict = {}
    monkeypatch.setattr(offline, "generate_section", _fake_generate([], seen_materials=seen))

    _run(pid)

    assert all(v is None for v in seen.values())


# ---------------------------------------------------------------
# 写作思路与全篇主线进生成器（路由 → agent 这一段）
# ---------------------------------------------------------------
def test_ideas_and_core_question_reach_the_generator(tmp_db, offline, monkeypatch):
    """作者写的思路与大纲定下的核心研究问题要真的传到生成器。

    这条串的是**路由到 agent** 那一段：只写 agent 级用例（形参表里有、提示词里有）
    的话，_run_generation 里漏传一个实参照样全绿 —— 这正是「产物没进提示词」那一类
    缺陷的形状（v1.13 的 citation_binding 就是这么栽的）。
    顺带钉住来源：core_question 在 outline_json 里，不是 projects 的列。
    """
    pid = _project()
    outline = dict(db.get_project(pid)["outline_json"])
    outline["core_question"] = "二者是互补还是替代？"
    db.update_project(
        pid, outline_json=outline,
        writing_ideas="先辨析 A 与 B，再论证二者互补而非替代",
    )

    seen: dict = {}
    monkeypatch.setattr(offline, "generate_section", _fake_generate([], seen_thread=seen))

    _run(pid)

    assert seen, "应当逐节调用生成器"
    assert all(v == ("先辨析 A 与 B，再论证二者互补而非替代", "二者是互补还是替代？")
               for v in seen.values()), seen


def test_absent_ideas_and_core_question_pass_empty_strings(tmp_db, offline, monkeypatch):
    """没写过思路、大纲里也没有 core_question 时传**空串**，而不是 None。

    空串是「整段消失」那条纪律的触发条件（块内部判 `not x.strip()`）；None 会让
    它在 .strip() 上抛 AttributeError —— 而这条路存量项目全都在走。
    """
    pid = _project()  # 它的大纲没有 core_question，库里也没有写作思路

    seen: dict = {}
    monkeypatch.setattr(offline, "generate_section", _fake_generate([], seen_thread=seen))

    _run(pid)

    assert seen
    assert all(v == ("", "") for v in seen.values()), seen


# ---------------------------------------------------------------
# 绑定失配时显式失败
# ---------------------------------------------------------------
def test_mismatched_binding_fails_loudly(tmp_db, offline, monkeypatch):
    """绑定与大纲完全不匹配时必须报错，而不是安静地生成零引用的论文。"""
    pid = _project()
    db.update_project(pid, citation_binding_json={"早已不存在的章节": [
        {"doc_id": "d1", "doc_title": "文献一", "page": 1},
    ]})

    calls: list = []
    monkeypatch.setattr(offline, "generate_section", _fake_generate(calls))

    _run(pid)

    assert calls == [], "不该浪费 LLM 调用"
    p = db.get_project(pid)
    assert p["status"] == db.ProjectStatus.CITATION_CONFIRMED
    assert "引用调度" in p["generation_state_json"]["error"]


# ---------------------------------------------------------------
# ETA
# ---------------------------------------------------------------
def test_eta_needs_a_completed_section():
    import time
    started = time.monotonic()
    assert projects_router._eta(started, 0, 5) is None   # 还没完成过任何一节
    assert projects_router._eta(started, 5, 0) is None   # 已无剩余
    assert projects_router._eta(started, 2, 4) == 0      # 刚起步，实数取整为 0


# ---------------------------------------------------------------
# 论文类型 / 写法 / 设计字段 / 单节超时 / 中断降级（T1、T4 的测试加固）
# ---------------------------------------------------------------
def test_paper_type_writing_note_and_design_reach_the_generator(tmp_db, offline, monkeypatch):
    """论文类型、类型写法与设计字段要真的传到生成器（T1）。

    只写 agent 级用例（形参表里有、提示词里有）的话，_run_generation 漏传 paper_type= /
    writing_note= / design_context= / design_label= 实参照样全绿 —— 七类差异化写法与
    作者的手填设计从每节正文里静默消失。seen_meta 逐节记下这四项，正是为了这一段。
    """
    from app import paper_types

    ptype = paper_types.QUALITATIVE
    pid = _project(paper_type=ptype)
    fields = paper_types.design_fields_for(ptype)
    design = {fields[0]: "三个社区", fields[1]: "半结构化访谈"}
    db.update_project(pid, design_json={"fields": design})

    seen: dict = {}
    monkeypatch.setattr(offline, "generate_section", _fake_generate([], seen_meta=seen))

    _run(pid)

    note = paper_types.writing_note_for(ptype)
    label = paper_types.design_label_for(ptype)
    assert seen, "应当逐节调用生成器"
    for v in seen.values():
        assert v[0] == ptype, v
        assert v[1] == note, v
        assert v[2] == label, v
        assert f"{fields[0]}：三个社区" in v[3], v
        assert f"{fields[1]}：半结构化访谈" in v[3], v


def test_section_timeout_marks_failure_with_a_readable_reason(tmp_db, offline, monkeypatch):
    """单节硬超时要落一句人话，而不是 "TimeoutError: "（T4）。

    asyncio.wait_for 超时抛的是 TimeoutError，而 str(TimeoutError()) 是空串，走通用
    `{type.__name__}: {exc}` 分支会写成 "TimeoutError: " 这种没有内容的失败原因。
    已完成章节都已逐节落盘，失败后应退回可重试状态并留一句可读的原因。
    """
    pid = _project()
    monkeypatch.setattr(generation_router, "_SECTION_TIMEOUT", 0.001)

    async def _stall(section_title, word_budget, refs, context, topic, **_ignored):
        await asyncio.sleep(1)

    monkeypatch.setattr(offline, "generate_section", _stall)

    _run(pid)

    p = db.get_project(pid)
    assert p["status"] == db.ProjectStatus.CITATION_CONFIRMED
    err = p["generation_state_json"]["error"]
    assert "超时" in err, err
    assert err != "TimeoutError: ", err


def test_interrupt_marks_failure_then_re_raises(tmp_db, offline, monkeypatch):
    """生成被取消要落「已中断」并向上抛（T4 的中断降级分支）。

    桌面版窗口关闭时后台任务被 cancel：必须先把项目退回可重试状态（否则永远卡在
    generating），再把 CancelledError 抛回去（吞掉它等于假装任务正常结束）。
    """
    pid = _project()

    async def _cancel(section_title, word_budget, refs, context, topic, **_ignored):
        raise asyncio.CancelledError()

    monkeypatch.setattr(offline, "generate_section", _cancel)

    with pytest.raises(asyncio.CancelledError):
        _run(pid)

    p = db.get_project(pid)
    assert p["status"] == db.ProjectStatus.CITATION_CONFIRMED
    assert "中断" in p["generation_state_json"]["error"]


# ---------------------------------------------------------------
# 写作语言贯穿（路由 → agent → 落库）
# ---------------------------------------------------------------
def _en_project(**kwargs) -> str:
    """一个写作语言是英文的项目。**在 _project 之后改**，因为改语言的级联入口是
    set_topic 那条路（这里直接写库，走的是「存量项目」那种形态）。"""
    pid = _project(**kwargs)
    db.update_project(pid, writing_lang="en")
    return pid


# 40 个词、240 个字符（count_chars 去掉空白后计）—— 两个口径相差六倍，一眼可辨。
_EN_BODY = " ".join(["shared"] * 40)


def test_writing_lang_reaches_the_generator_and_polish(tmp_db, offline, monkeypatch):
    """写作语言要同时传到生成器与润色环节，且与项目当前值一致。

    润色的输入输出都是正文本身：语言传错就等于把刚写好的英文正文整段换成中文
    （或反过来），而下游只看到「正文变了」—— 一个字都不会报错。生成器那边更远：
    它决定骨架、类型说明与输出语言宣告。
    """
    pid = _en_project()

    seen: dict = {}
    seen_polish: list[str] = []
    monkeypatch.setattr(offline, "generate_section", _fake_generate([], seen_lang=seen))

    async def _spy(text: str, writing_lang: str = "zh") -> str:
        seen_polish.append(writing_lang)
        return text

    monkeypatch.setattr(generation_router, "polish", _spy)

    _run(pid)

    assert set(seen.values()) == {"en"}, seen
    assert seen_polish and set(seen_polish) == {"en"}, seen_polish


def test_english_section_words_are_counted_as_words(tmp_db, offline, monkeypatch):
    """落库的 actual_words 走 count_units（英文按词），不是按字符。

    这一处此前写死 count_chars：英文正文按字符算会虚高六倍左右，界面上「已写字数」
    与进度条一起失真，而生成环节内部明明算对过一次 —— 同一个数在两处用两种口径，
    正是要修的形态。
    """
    from app.agents.generate_agent import count_chars, count_units

    pid = _en_project()

    async def _gen(section_title, word_budget, refs, context, topic, **_ignored):
        return {
            "section_title": section_title,
            "word_budget": word_budget,
            "actual_words": 0,          # 故意填 0：断言的是路由这一层重算的结果
            "content": _EN_BODY,
            "references": refs,
        }

    monkeypatch.setattr(offline, "generate_section", _gen)

    _run(pid)

    sections = db.get_project(pid)["sections_json"]
    assert sections
    assert all(s["actual_words"] == 40 for s in sections), sections
    assert count_chars(_EN_BODY) == 240, "字符口径与词口径必须真的不同，否则这条断言没意义"
    assert count_units(_EN_BODY, "en") == 40


def test_english_project_renders_apa_references(tmp_db, offline, monkeypatch):
    """英文项目即使库里存着 gb7714，落盘的文末列表也必须是 APA。

    格式收敛的唯一入口是 effective_format：落盘这一侧漏掉它，「这一轮写出的列表」
    与启动时重算出来的就不是同一个字符串，citations_stale 会常年亮着。
    """
    pid = _en_project()
    monkeypatch.setattr(offline, "generate_section", _fake_generate([]))

    _run(pid)

    assert db.get_project(pid)["citation_format"] == "gb7714", "库里那一列没被改写"
    refs = db.get_project(pid)["references_json"]
    assert refs
    # GB/T 的两条特征各抓一个：类型标识 `[J]` 与「作者. 题名[J].」的字段序
    assert all("[J]" not in r["formatted"] for r in refs)
    assert refs[0]["formatted"].startswith("[1] 张三. (2023). 文献一.")

