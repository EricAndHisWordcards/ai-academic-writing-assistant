"""测试：项目状态机与数据库 CRUD（db）。"""
import json

import pytest

from app import db, metadata
from app.routers import projects


def test_generation_state_and_references_roundtrip(tmp_db):
    """两个新列必须能原样存取 —— 漏加白名单或漏加解码元组都会静默失败。

    update_project 对不在白名单里的字段是 `continue`（不报错），
    _row_to_project 漏解码则会返回 JSON 字符串而非对象。
    """
    tmp_db.create_project("p1")
    state = {"status": "running", "done": 3, "total": 11, "eta_seconds": 40}
    refs = [{"num": 1, "doc_title": "文献一", "formatted": "[1] 文献一."}]

    tmp_db.update_project("p1", generation_state_json=state, references_json=refs)
    p = tmp_db.get_project("p1")

    assert p["generation_state_json"] == state
    assert isinstance(p["generation_state_json"], dict), "必须已解码，不能是字符串"
    assert p["references_json"] == refs


def test_reset_stale_generating(tmp_db):
    """服务重启后，僵死的「生成中」要被打断，但已生成的章节不能丢。"""
    tmp_db.create_project("p1")
    done_sections = [{"section_title": "1.1 背景", "actual_words": 300, "content": "正文"}]
    tmp_db.update_project(
        "p1",
        status=db.ProjectStatus.GENERATING,
        sections_json=done_sections,
        generation_state_json={"status": "running", "done": 1, "total": 5},
    )
    # 另一个已完成的项目不该被动到
    tmp_db.create_project("p2")
    tmp_db.update_project("p2", status=db.ProjectStatus.COMPLETED)

    assert tmp_db.reset_stale_generating() == 1

    p = tmp_db.get_project("p1")
    assert p["status"] == db.ProjectStatus.CITATION_CONFIRMED, "应退回可重试的状态"
    assert p["generation_state_json"]["status"] == "interrupted"
    assert p["generation_state_json"]["error"]
    assert p["sections_json"] == done_sections, "已完成的章节必须保留"

    assert tmp_db.get_project("p2")["status"] == db.ProjectStatus.COMPLETED
    # 幂等：再跑一遍无事发生
    assert tmp_db.reset_stale_generating() == 0


def test_task_state_roundtrip(tmp_db):
    """task_state_json 同样必须过白名单 + 解码元组两道关，且与生成状态互不干扰。"""
    tmp_db.create_project("p1")
    task = {
        "kind": "outline", "status": "running",
        "message": "正在拟定章节结构与标题…",
        "done": None, "total": None, "current": "",
        "started_at": "2026-09-17T05:00:00+00:00",
        "updated_at": "2026-09-17T05:00:10+00:00",
        "error": None,
    }
    tmp_db.update_project("p1", task_state_json=task)

    p = tmp_db.get_project("p1")
    assert isinstance(p["task_state_json"], dict), "必须已解码，不能是字符串"
    assert p["task_state_json"] == task
    # 两个状态列各管各的：写任务状态不该动到生成进度
    assert p["generation_state_json"] is None


def test_reset_stale_tasks(tmp_db):
    """服务重启后，僵死的任务要标为 interrupted，否则前端一直转圈。"""
    tmp_db.create_project("p1")
    tmp_db.update_project("p1", task_state_json={
        "kind": "documents", "status": "running", "message": "正在解析文献 1/3",
        "done": 1, "total": 3, "started_at": "2026-09-17T05:00:00+00:00",
    })
    # 已完成的任务不该被动到
    tmp_db.create_project("p2")
    tmp_db.update_project("p2", task_state_json={
        "kind": "outline", "status": "done", "message": "大纲已生成",
    })

    assert tmp_db.reset_stale_tasks() == 1

    p = tmp_db.get_project("p1")
    assert p["task_state_json"]["status"] == "interrupted"
    assert p["task_state_json"]["error"]
    # started_at 保留，前端仍能算出「已等待」的时长
    assert p["task_state_json"]["started_at"] == "2026-09-17T05:00:00+00:00"

    assert tmp_db.get_project("p2")["task_state_json"]["status"] == "done"
    # 幂等：再跑一遍无事发生
    assert tmp_db.reset_stale_tasks() == 0


def test_create_and_get_project(tmp_db):
    p = tmp_db.create_project("proj1", "测试论文")
    assert p["id"] == "proj1"
    assert p["status"] == db.ProjectStatus.DRAFT
    assert p["title"] == "测试论文"


def test_project_status_transitions(tmp_db):
    p = tmp_db.create_project("proj1")
    # 选题
    p = tmp_db.update_project("proj1", status=db.ProjectStatus.TOPIC_SET,
                              topic="选题", paper_type="定量/实证研究", target_words=3000)
    assert p["status"] == db.ProjectStatus.TOPIC_SET
    # 大纲确认
    p = tmp_db.update_project("proj1", status=db.ProjectStatus.OUTLINE_CONFIRMED)
    assert p["status"] == db.ProjectStatus.OUTLINE_CONFIRMED
    # 引用确认
    p = tmp_db.update_project("proj1", status=db.ProjectStatus.CITATION_CONFIRMED)
    assert p["status"] == db.ProjectStatus.CITATION_CONFIRMED


def test_json_fields_roundtrip(tmp_db):
    """JSON 字段（大纲/绑定）能正确序列化与反序列化。"""
    tmp_db.create_project("proj1")
    outline = {"chapters": [{"title": "章1", "sections": [{"title": "1.1", "word_budget": 100}]}]}
    tmp_db.update_project("proj1", outline_json=outline)
    p = tmp_db.get_project("proj1")
    assert p["outline_json"] == outline


def test_cite_style_default(tmp_db):
    """cite_style 默认值为 bracket。"""
    p = tmp_db.create_project("proj1")
    assert p.get("cite_style") == "bracket"


def test_cite_style_update(tmp_db):
    """cite_style 可更新。"""
    tmp_db.create_project("proj1")
    p = tmp_db.update_project("proj1", cite_style="superscript")
    assert p["cite_style"] == "superscript"


def test_document_crud(tmp_db):
    """文献增删查。"""
    tmp_db.create_project("proj1")
    doc = {
        "id": "doc1", "filename": "a.pdf", "title": "文献A",
        "authors": "张三", "year": "2023", "source": "期刊",
        "summary": "摘要", "pages": [{"page": 1, "snippet": "内容"}],
    }
    tmp_db.add_document("proj1", doc)
    docs = tmp_db.list_documents("proj1")
    assert len(docs) == 1
    assert docs[0]["pages"] == [{"page": 1, "snippet": "内容"}]

    tmp_db.delete_document("doc1")
    assert tmp_db.list_documents("proj1") == []


def test_delete_project_cascades(tmp_db):
    """删除项目级联删除文献。"""
    tmp_db.create_project("proj1")
    tmp_db.add_document("proj1", {"id": "doc1", "filename": "a.pdf"})
    tmp_db.delete_project("proj1")
    assert tmp_db.get_project("proj1") is None
    assert tmp_db.list_documents("proj1") == []


def test_materials_plan_roundtrip(tmp_db):
    """materials_plan_json 是后加的列，三处（迁移/解码/白名单）漏一处就静默失效。"""
    tmp_db.create_project("proj1")
    plan = {"notes": "n", "placements": [{"section_title": "1.1", "material_ids": ["m1"]}]}
    tmp_db.update_project("proj1", materials_plan_json=plan)
    assert tmp_db.get_project("proj1")["materials_plan_json"] == plan

    # 置空要真的写 NULL，而不是留下一个 "null" 字符串
    tmp_db.update_project("proj1", materials_plan_json=None)
    assert tmp_db.get_project("proj1")["materials_plan_json"] is None


def test_design_roundtrip(tmp_db):
    """design_json 同样是后加的列：迁移/解码/白名单漏一处就是静默不写。"""
    tmp_db.create_project("proj1")
    design = {"fields": {"数据源与样本": "312 份问卷", "主要结果": "β=0.42"}, "notes": "n"}
    tmp_db.update_project("proj1", design_json=design)
    assert tmp_db.get_project("proj1")["design_json"] == design

    tmp_db.update_project("proj1", design_json=None)
    assert tmp_db.get_project("proj1")["design_json"] is None


def test_writing_ideas_roundtrip(tmp_db):
    """writing_ideas 是后加的列：迁移/白名单/建表漏一处就是静默不写。

    它是**纯 TEXT 而不是 JSON**：刻意不在 _row_to_project 的解码名单里 —— 进去就会在
    json.loads 上抛 JSONDecodeError（一段中文不是合法 JSON）。这条用例就是那个判断的护栏。
    """
    tmp_db.create_project("proj1")
    ideas = "先辨析 A 与 B 两个概念，再从机制层面论证二者互补而非替代"
    tmp_db.update_project("proj1", writing_ideas=ideas)
    assert tmp_db.get_project("proj1")["writing_ideas"] == ideas

    # 清空要真的写 NULL，而不是留下一个 "" —— 库里的「没写过」只留一种表示
    tmp_db.update_project("proj1", writing_ideas=None)
    assert tmp_db.get_project("proj1")["writing_ideas"] is None


def test_clusters_json_roundtrip(tmp_db):
    """clusters_json 是后加的列：迁移/解码/白名单漏一处就是静默不写。

    它按 doc id 组织（不是章节标题），所以大纲改标题不会让它失效 —— 这也是加粗
    「置空」这条断言的原因：删除文献时真的要把整份聚类置为 NULL。
    """
    tmp_db.create_project("proj1")
    data = {
        "clusters": [{
            "id": "c1", "title": "主题一", "summary": "s",
            "doc_ids": ["doc1"], "doc_titles": ["文献A"], "key_points": ["k"],
        }],
        "notes": "n",
        "unassigned": [{"id": "doc9", "title": "文献Z", "reason": "模型未归类"}],
        "generated_at": "2026-09-17T05:00:00+00:00",
        "confirmed_at": None,
    }
    tmp_db.update_project("proj1", clusters_json=data)
    assert tmp_db.get_project("proj1")["clusters_json"] == data

    tmp_db.update_project("proj1", clusters_json=None)
    assert tmp_db.get_project("proj1")["clusters_json"] is None


def test_paper_type_rename_migration_is_explicit_and_idempotent(tmp_db):
    """改名前的类型行被就地更新为现名，且跑第二遍无事发生。

    不迁移的后果是静默的：config_for 对未知类型退化为课程论文，老项目的工序、
    设计字段与生成闸门会全部悄悄变样。
    """
    from app import paper_types

    # 绕过 create_project 的默认值，直接写一行「改名前的类型」
    tmp_db.create_project("old1")
    with tmp_db.get_conn() as conn:
        conn.execute(
            "UPDATE projects SET paper_type = ? WHERE id = ?", ("实证研究", "old1")
        )
    # 混入两个不该被碰的行：一个已是现名，一个从未在映射里出现过
    tmp_db.create_project("new1")
    tmp_db.update_project("new1", paper_type="定量/实证研究")
    tmp_db.create_project("gone1")
    with tmp_db.get_conn() as conn:
        conn.execute(
            "UPDATE projects SET paper_type = ? WHERE id = ?", ("毕业论文", "gone1")
        )

    assert tmp_db._migrate_paper_types() == 1

    assert tmp_db.get_project("old1")["paper_type"] == "定量/实证研究"
    # 已被迁移过的名字不该再被匹配，且映射之外的旧名原样保留（前端会兜底）
    assert tmp_db.get_project("new1")["paper_type"] == "定量/实证研究"
    assert tmp_db.get_project("gone1")["paper_type"] == "毕业论文"
    # 幂等：再跑一遍匹配 0 行
    assert tmp_db._migrate_paper_types() == 0

    # 每条旧名都真的能迁到有效类型上（映射指向已下线的名字是最隐蔽的一类错）
    for old, new in paper_types.PAPER_TYPE_RENAMES.items():
        assert paper_types.is_valid(new), f"{old} → {new} 迁到了一个无效类型"


def test_migrate_document_meta_clears_placeholders_only(tmp_db):
    """只清占位词：真实值与空值逐字不动，且第二次跑返回 0（幂等）。

    误伤真实作者名比漏掉一个占位词严重得多 —— 这是本用例的重点。
    """
    tmp_db.create_project("p1")
    tmp_db.add_document("p1", {
        "id": "d1", "filename": "a.pdf", "title": "A",
        "authors": "未提及", "year": "Not specified", "source": "未知",
    })
    tmp_db.add_document("p1", {
        "id": "d2", "filename": "b.pdf", "title": "B",
        "authors": "张三", "year": "2023", "source": "教育研究",
    })
    tmp_db.add_document("p1", {
        "id": "d3", "filename": "c.pdf", "title": "C",
        "authors": "", "year": "", "source": "",
    })
    # 含「无」但不是占位词的来源名，绝不能连带被清
    tmp_db.add_document("p1", {
        "id": "d4", "filename": "d.pdf", "title": "D",
        "authors": "佚名", "year": "2024", "source": "无糖食品消费行为",
    })

    assert tmp_db._migrate_document_meta() == 3  # 只有 d1 的三列

    docs = {d["id"]: d for d in tmp_db.list_documents("p1")}
    assert (docs["d1"]["authors"], docs["d1"]["year"], docs["d1"]["source"]) == ("", "", "")
    assert (docs["d2"]["authors"], docs["d2"]["year"], docs["d2"]["source"]) == (
        "张三", "2023", "教育研究",
    )
    assert (docs["d3"]["authors"], docs["d3"]["year"], docs["d3"]["source"]) == ("", "", "")
    assert (docs["d4"]["authors"], docs["d4"]["year"], docs["d4"]["source"]) == (
        "佚名", "2024", "无糖食品消费行为",
    )

    assert tmp_db._migrate_document_meta() == 0, "置空后不该再匹配到"


def test_migrate_reference_snapshots_recomputes_formatted(tmp_db):
    """存量快照里的 formatted 是生成时烘死的，必须重算，否则用户不重新生成就永远看到脏列表。"""
    tmp_db.create_project("p1")
    tmp_db.add_document("p1", {
        "id": "d1", "filename": "a.pdf", "title": "巧用机器学习",
        "authors": "未提及", "year": "未提及", "source": "未知",
        "pages": [{"page": 1, "snippet": "片段"}],
    })
    dirty = [{
        "num": 1, "doc_id": "d1", "doc_title": "巧用机器学习",
        "authors": "未提及", "year": "未提及", "source": "未知",
        "formatted": "[1] 未提及. 巧用机器学习[J]. 未知, 未提及.",
    }]
    tmp_db.update_project(
        "p1", citation_format="gb7714", cite_style="bracket", references_json=dirty
    )

    # 迁移顺序就是这样：先清 documents，再重算快照
    assert tmp_db._migrate_document_meta() == 3
    assert tmp_db._migrate_reference_snapshots() == 1

    refs = tmp_db.get_project("p1")["references_json"]
    assert refs[0]["formatted"] == "[1] 巧用机器学习[J]."
    assert "未提及" not in json.dumps(refs, ensure_ascii=False)
    # 只重写派生字段，引用关系与其它字段原样
    assert refs[0]["doc_id"] == "d1"
    assert refs[0]["num"] == 1
    assert refs[0]["doc_title"] == "巧用机器学习"

    assert tmp_db._migrate_reference_snapshots() == 0, "重算结果逐字相同就不该写库"


def test_migrate_reference_snapshots_leaves_clean_ones_alone(tmp_db):
    """已经是干净的条目一字都不能改 —— 重算只该动被占位词污染的那些。

    张冠李戴的「顺手修正」比不修更糟：用户会以为自己的文献列表被换过。
    """
    tmp_db.create_project("p1")
    tmp_db.add_document("p1", {
        "id": "d1", "filename": "a.pdf", "title": "题名",
        "authors": "张三", "year": "2023", "source": "教育研究",
    })
    clean = [{
        "num": 1, "doc_id": "d1", "doc_title": "题名",
        "authors": "张三", "year": "2023", "source": "教育研究",
        "formatted": "[1] 张三. 题名[J]. 教育研究, 2023.",
    }]
    before = json.dumps(clean, ensure_ascii=False)
    tmp_db.update_project("p1", references_json=clean)

    assert tmp_db._migrate_reference_snapshots() == 0
    assert json.dumps(
        tmp_db.get_project("p1")["references_json"], ensure_ascii=False
    ) == before


def test_migrate_reference_snapshots_skips_broken_json(tmp_db):
    """库里的坏 JSON 不该让启动失败：跳过它，其它项目照常迁移。"""
    tmp_db.create_project("p1")
    tmp_db.create_project("p2")
    with tmp_db.get_conn() as conn:
        conn.execute(
            "UPDATE projects SET references_json = ? WHERE id = ?", ("{不是 JSON", "p1")
        )
    tmp_db.add_document("p2", {
        "id": "d1", "filename": "a.pdf", "title": "题名", "authors": "未提及",
        "year": "", "source": "",
    })
    tmp_db.update_project("p2", references_json=[{
        "num": 1, "doc_id": "d1", "doc_title": "题名", "authors": "未提及",
        "year": "", "source": "", "formatted": "[1] 未提及. 题名[J].",
    }])

    tmp_db._migrate_document_meta()
    assert tmp_db._migrate_reference_snapshots() == 1  # p1 跳过，p2 重算
    assert tmp_db.get_project("p2")["references_json"][0]["formatted"] == "[1] 题名[J]."


def test_migrate_project_titles_backfills_and_is_idempotent(tmp_db):
    """存量项目的占位标题换成选题；用户自己起过的名字一字不动；跑两遍第二遍是 0。

    现场覆盖四种「这个名字不是用户起的」的形态（写死的占位词、没写过、只有空白、
    另一个占位词）+ 一种必须放过的（真标题）+ 一种同值的边界（占位词恰好等于选题）。
    """
    cases = {
        "p1": ("未命名论文", "真实选题一", "真实选题一"),
        "p2": (None, "选题二", "选题二"),
        "p3": ("未命名论文", None, None),          # 没有选题可补 → 置 NULL
        "p4": ("论文", "选题四", "选题四"),
        "p5": ("我自己起的名字", "选题五", "我自己起的名字"),  # 用户起的名，不许动
        # 标题恰好就是占位词、而选题也叫这个名字：目标值与现值相同，不该被算作改动，
        # 否则每次启动都会匹配到它、白打一条日志
        "p6": ("未命名论文", "未命名论文", "未命名论文"),
    }
    for pid, (title, topic, _) in cases.items():
        tmp_db.create_project(pid, title)
        tmp_db.update_project(pid, topic=topic)

    assert tmp_db._migrate_project_titles() == 4
    for pid, (_, _, want) in cases.items():
        assert tmp_db.get_project(pid)["title"] == want, pid
    assert tmp_db._migrate_project_titles() == 0


def test_migrate_project_titles_does_not_touch_updated_at(tmp_db):
    """迁移不碰 updated_at。

    list_projects 按 updated_at DESC 排序（侧栏就是按它排的），迁移若写这个列，
    用户下次打开软件会看到这批项目整体跳到列表最前 —— 而他什么都没做。这是
    「派生字段的清理不改 updated_at」那条既有纪律（_migrate_reference_snapshots）
    在这里最硬的一个理由。
    """
    for pid in ("p1", "p2"):
        tmp_db.create_project(pid, "未命名论文")
        tmp_db.update_project(pid, topic=f"{pid} 的选题")
    before = {pid: tmp_db.get_project(pid)["updated_at"] for pid in ("p1", "p2")}

    assert tmp_db._migrate_project_titles() == 2

    after = {pid: tmp_db.get_project(pid)["updated_at"] for pid in ("p1", "p2")}
    assert after == before


def test_material_crud(tmp_db):
    """研究材料增删查。"""
    tmp_db.create_project("proj1")
    tmp_db.add_material("proj1", {
        "id": "m1", "label": "数据.xlsx", "kind": "file", "text": "样本 312",
    })
    tmp_db.add_material("proj1", {
        "id": "m2", "label": "我的思路", "kind": "text", "text": "核心论点是……",
    })

    mats = tmp_db.list_materials("proj1")
    assert [m["label"] for m in mats] == ["数据.xlsx", "我的思路"]
    assert mats[0]["char_count"] == len("样本 312")
    # 列表默认不带正文（材料可能十几万字，列表用不上）
    assert all("text" not in m for m in mats)

    full = tmp_db.list_materials("proj1", with_text=True)
    assert full[1]["text"] == "核心论点是……"

    assert tmp_db.get_material("m1")["text"] == "样本 312"
    tmp_db.delete_material("m1")
    assert [m["id"] for m in tmp_db.list_materials("proj1")] == ["m2"]
    assert tmp_db.get_material("m1") is None


# ---------------------------------------------------------------
# 连接的存活期（临时库删不掉的那条路）
# ---------------------------------------------------------------
def test_get_conn_closes_on_exit(tmp_db):
    """退出 with 就把连接关掉 —— 不是只提交。

    `sqlite3.Connection.__exit__` 只做 commit / rollback，**不关闭连接**。get_conn
    原先返回裸连接，于是连接只能等循环 GC 回收：Windows 上句柄没释放期间那个 .db
    文件删不掉（[WinError 32]），而夹具当时外面套着 `except OSError: pass` ——
    「每跑一轮测试就往 TEMP 里留下一批回收不掉的临时库」就这么被静默盖住了几轮。
    服务端同一形态的代价是每处理一个请求多留一个连接。
    """
    import sqlite3

    with tmp_db.get_conn() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_get_conn_rolls_back_on_exception(tmp_db):
    """抛异常时回滚 —— 换成 contextmanager 不能把事务语义一起换掉。

    `with conn:` 的语义是「正常退出提交、抛异常回滚」，这里逐字保持不变，只是多了
    一次关闭。锁住它，是因为这个替换很容易顺手写成 `finally: conn.close()` 而把
    回滚漏掉 —— 那会把「写坏一半的事务」变成可见数据。
    """
    tmp_db.create_project("p1")
    before = tmp_db.get_project("p1")["title"]

    with pytest.raises(RuntimeError):
        with tmp_db.get_conn() as conn:
            conn.execute("UPDATE projects SET title = '改了一半' WHERE id = 'p1'")
            raise RuntimeError("boom")

    assert tmp_db.get_project("p1")["title"] == before


# ---------------------------------------------------------------
# 文末著录的八个列与元数据编辑
# ---------------------------------------------------------------
# 每列一个样本值。**这份表就是下面那条 roundtrip 用例的输入** —— 于是它自己漏一列，
# 那条用例会毫无反应地继续全绿，而漏掉的那一列恰恰是没人查的那一列。所以另有一条
# 断言（见下）专门把「漏」变成红色。
_REFERENCE_COLUMN_SAMPLES = (
    ("volume", "32"),
    ("issue", "1"),
    ("page_range", "56-75"),
    ("source_type", "J"),
    ("title_en", "The Application of AI in Education"),
    ("place", "北京"),
    ("edition", "第3版"),
    ("publish_date", "2023-05-04"),
)


def test_reference_column_samples_cover_every_reference_field():
    """样本表必须与 `metadata.REFERENCE_FIELDS` **同集合**（不需要数据库）。

    为什么值得单写一条看着多余的断言：下面那条 roundtrip 是**遍历这份样本表**的。
    往 `REFERENCE_FIELDS` 里加一列而忘了在这里加样本值，那条用例不会报任何错 ——
    而新增列最需要被走一遍三处读取的时刻，正是它刚被加进来的时候。这条断言把
    沉默变成一条指名道姓的红。
    """
    assert {column for column, _ in _REFERENCE_COLUMN_SAMPLES} == set(metadata.REFERENCE_FIELDS)


def test_reference_columns_roundtrip_everywhere(tmp_db):
    """这八个列要**三处查询都读得到**（外加落库那一处 INSERT）。

    四处各是一段手写 SQL，漏一处就是一次静默失败（详见 db.list_document_meta 的
    docstring）。这里一次把三处查询都走一遍 —— 分开测容易漏掉最不显眼的那一处。

    第三处 `list_document_meta` 的症状最隐蔽：它**只被 `_project_refs_snapshot` 用**，
    漏了不会表现为「界面上看不到值」，而是文末列表恒少一项、`citations_stale` 常年亮着。
    """
    tmp_db.create_project("p1")
    doc = {
        "id": "d1", "filename": "a.pdf", "title": "题名", "authors": "张三",
        "year": "2023", "source": "教育研究",
        "pages": [{"page": 1, "snippet": "片段"}],
    }
    doc.update(dict(_REFERENCE_COLUMN_SAMPLES))
    tmp_db.add_document("p1", doc)

    for name, row in (
        ("get_document", tmp_db.get_document("d1")),
        ("list_documents", tmp_db.list_documents("p1")[0]),
        ("list_document_meta", tmp_db.list_document_meta("p1")[0]),
    ):
        for column, expected in _REFERENCE_COLUMN_SAMPLES:
            assert row[column] == expected, f"{name} 读不到 {column}"


def test_page_range_is_not_overwritten_by_the_pdf_pages_column(tmp_db):
    """`page_range` 这一列不能被 pdf 逐页数组顶掉。

    这是列名**必须叫 page_range**的原因：get_document / list_documents 都会执行
    `d["pages"] = json.loads(d.pop("pages_json"))`，如果这一列也叫 `pages`，它会被
    逐页文本静默覆盖 —— 而生成循环正是走 list_documents 建 doc_map，要到
    _plan_citations 才发现卷期页全空了。
    """
    tmp_db.create_project("p1")
    tmp_db.add_document("p1", {
        "id": "d1", "filename": "a.pdf", "title": "题名", "page_range": "56-75",
        "pages": [{"page": 1, "snippet": "第一页"}, {"page": 2, "snippet": "第二页"}],
    })
    row = tmp_db.get_document("d1")
    assert row["page_range"] == "56-75"
    assert [p["page"] for p in row["pages"]] == [1, 2]


def test_update_document_meta_writes_only_the_given_columns(tmp_db):
    """元数据编辑是**列白名单**，且只写传进来的那几列。"""
    tmp_db.create_project("p1")
    tmp_db.add_document("p1", {
        "id": "d1", "filename": "a.pdf", "title": "旧题名", "authors": "未提及",
        "year": "2023", "source": "教育研究", "volume": "32",
        "pages": [{"page": 1, "snippet": "片段"}],
    })

    out = tmp_db.update_document_meta("d1", {"title": "新题名", "authors": "张三"})
    assert (out["title"], out["authors"]) == ("新题名", "张三")
    # 没传的列一个字都不动
    assert (out["year"], out["source"], out["volume"]) == ("2023", "教育研究", "32")
    # 身份与判重判据这两类列不在白名单里 —— 越界要**抛错**，不是静默忽略
    for bad in ("content_hash", "pages_json", "project_id", "id", "filename"):
        with pytest.raises(ValueError):
            tmp_db.update_document_meta("d1", {bad: "x"})
    assert tmp_db.get_document("d1")["filename"] == "a.pdf"

    # 空串是「清空」，不是「不修改」
    assert tmp_db.update_document_meta("d1", {"authors": ""})["authors"] == ""


def test_migrate_reference_snapshots_does_not_push_a_cleared_field_back(tmp_db):
    """**清空一个字段之后，启动迁移不许把它压回旧值。**

    这是 `_snapshot_field` 里那个坑的回归：原来的合并写法是
    `doc.get("authors") or ref.get("authors")` —— `or` 意味着**空串会退回快照里的
    旧值**。于是「把抽错的作者清空」这个动作永远落不了地（下次启动就被写回），
    而「填一个错的」反而能生效。判据必须是「行在不在」，不是「值真不真」。
    """
    tmp_db.create_project("p1")
    tmp_db.add_document("p1", {
        "id": "d1", "filename": "a.pdf", "title": "题名",
        "authors": "", "year": "", "source": "",   # 用户在界面上把它们清掉了
    })
    tmp_db.update_project("p1", references_json=[{
        "num": 1, "doc_id": "d1", "doc_title": "题名",
        "authors": "旧值", "year": "旧值", "source": "旧值",
        "formatted": "[1] 旧值. 题名[J]. 旧值, 旧值.",
    }])

    assert tmp_db._migrate_reference_snapshots() == 1
    refs = tmp_db.get_project("p1")["references_json"]
    assert (refs[0]["authors"], refs[0]["year"], refs[0]["source"]) == ("", "", "")
    assert refs[0]["formatted"] == "[1] 题名[J]."
    assert "旧值" not in json.dumps(refs, ensure_ascii=False)


def test_migrate_reference_snapshots_picks_up_edits_and_keeps_deleted_docs(tmp_db):
    """文献还在 → 用库里（编辑后）的值；文献已删 → 保留快照自带的。

    「用库里的值」这条让编辑能在下次启动时被重算进 formatted；「文献已删保留快照」
    这条则是删除路径的既有语义（那条引用的文字还在，只是不再有文献行）。
    """
    tmp_db.create_project("p1")
    tmp_db.add_document("p1", {
        "id": "d1", "filename": "a.pdf", "title": "新题名", "authors": "张三",
        "year": "2023", "source": "教育研究", "volume": "32", "issue": "1",
        "page_range": "56-75", "source_type": "J",
    })
    tmp_db.update_project("p1", references_json=[
        {
            "num": 1, "doc_id": "d1", "doc_title": "旧题名", "authors": "李四",
            "year": "2020", "source": "旧刊",
            "formatted": "[1] 李四. 旧题名[J]. 旧刊, 2020.",
        },
        {
            "num": 2, "doc_id": "已删除", "doc_title": "被删的文献",
            "authors": "王五", "year": "2019", "source": "某刊",
            "formatted": "[2] 王五. 被删的文献[J]. 某刊, 2019.",
        },
    ])

    assert tmp_db._migrate_reference_snapshots() == 1
    refs = tmp_db.get_project("p1")["references_json"]
    # 编号、指向、`doc_title` 一个字都不动（题名走绑定，由 projects 负责刷新）
    assert [r["num"] for r in refs] == [1, 2]
    assert [r["doc_id"] for r in refs] == ["d1", "已删除"]
    assert refs[0]["doc_title"] == "旧题名"
    # 元数据换成库里的值，formatted 随之重算（含卷期页）
    assert (refs[0]["authors"], refs[0]["year"]) == ("张三", "2023")
    assert refs[0]["formatted"] == "[1] 张三. 旧题名[J]. 教育研究, 2023, 32(1): 56-75."
    # 文献行没了的那条原样保留
    assert refs[1] == {
        "num": 2, "doc_id": "已删除", "doc_title": "被删的文献", "authors": "王五",
        "year": "2019", "source": "某刊",
        "formatted": "[2] 王五. 被删的文献[J]. 某刊, 2019.",
    }


# ---------------------------------------------------------------
# 写作语言与英译题名
# ---------------------------------------------------------------
def test_writing_lang_defaults_to_chinese(tmp_db):
    """新建项目落在中文上 —— 存量项目与新建项目走的是同一条默认路径。

    `create_project` 只 INSERT 五列，这个值是建表 DEFAULT 与 pydantic 模型各给的一份
    （两处同值）。测「默认值」而不只是「能写进去」，因为默认值错了整类项目都会歪。
    """
    p = tmp_db.create_project("p1")
    assert p["writing_lang"] == "zh"
    assert tmp_db.update_project("p1", writing_lang="en")["writing_lang"] == "en"
    assert tmp_db.get_project("p1")["writing_lang"] == "en"


def test_title_en_roundtrips_through_all_selects(tmp_db):
    """`title_en` 要**四处都读得到**：落库、单篇、列表、快照用的轻量查询。

    四处各是一段手写 SQL（add_document 的 INSERT、get_document / list_documents 的
    SELECT *、list_document_meta 的显式列清单），漏掉最后那一处就是一次静默失败：
    `_project_refs_snapshot` 算出来的文末列表恒少一个英译方括号，而「这一轮会写出的」
    与「重算出来的」从此不是同一个字符串 —— citations_stale 常年亮着，重排也修不好。
    """
    tmp_db.create_project("p1")
    tmp_db.add_document("p1", {
        "id": "d1", "filename": "a.pdf", "title": "人工智能在教育中的应用",
        "title_en": "Application of Artificial Intelligence in Education",
        "pages": [{"page": 1, "snippet": "片段"}],
    })

    for row in (
        tmp_db.get_document("d1"),
        tmp_db.list_documents("p1")[0],
        tmp_db.list_document_meta("p1")[0],
    ):
        assert row["title_en"] == "Application of Artificial Intelligence in Education"


def test_migrate_reference_snapshots_leaves_chinese_snapshots_byte_identical(tmp_db):
    """中文项目里那些**已经是终态**的快照，重算后必须逐字节不变。

    这条是「加了语言与 APA/MLA 之后，存量中文项目一个字都没变」这个保证在**迁移这一层**
    的哨兵：那条 formatted 是按 GB/T 7714 写死的，重算结果但凡差一个字符，迁移就会
    判成「变了」并把整份快照重写一遍 —— 而用户什么都没改，界面上的文末列表却动了。
    """
    tmp_db.create_project("p1")          # writing_lang 默认 zh、citation_format 默认 gb7714
    tmp_db.add_document("p1", {
        "id": "d1", "filename": "a.pdf", "title": "题名", "authors": "张三",
        "year": "2023", "source": "教育研究",
        "volume": "32", "issue": "1", "page_range": "56-75", "source_type": "J",
    })
    snapshot = [{
        "num": 1, "doc_id": "d1", "doc_title": "题名", "authors": "张三",
        "year": "2023", "source": "教育研究", "volume": "32", "issue": "1",
        "pages": "56-75", "source_type": "J",
        "formatted": "[1] 张三. 题名[J]. 教育研究, 2023, 32(1): 56-75.",
    }]
    tmp_db.update_project("p1", references_json=snapshot)
    before = tmp_db.get_project("p1")["references_json"]

    assert tmp_db._migrate_reference_snapshots() == 0, "终态快照不该被重写"
    assert tmp_db.get_project("p1")["references_json"] == before


def test_runtime_reference_snapshot_is_already_final_for_the_migration(tmp_db):
    """「这一轮会写出的文末列表」写进库之后，启动迁移必须原样放行（0 处改写）。

    这条是本轮 `title_en` 那个缺陷的护栏，也是**唯一**拦得住它的那类断言：
    `format_reference` 读 title_en 有单测、`_migrate_reference_snapshots` 写 title_en
    也有单测，两个端点各自都对，而中间那个「把 ref dict 交给 format_reference」的地方
    漏了一项 —— 只有让**运行期那份真的经过迁移一次**才照得出来。断言挂在数据交接处，
    不挂在任何一端。

    缺陷形态（修复前必红）：运行期少一个 title_en 键 → APA 下印不出 §9.38 的英译方括号
    → 迁移把它补上 → 此后**每次后端重启**都会把同一份列表重写一遍、`citations_stale`
    亮着而用户按了重排也修不好（重排写回运行期那一版，下次重启再翻回来）。
    """
    tmp_db.create_project("p1")
    tmp_db.update_project("p1", writing_lang="en", citation_format="apa")
    tmp_db.add_document("p1", {
        "id": "d1", "filename": "a.pdf", "title": "中文题名",
        "title_en": "The English Title",
        "authors": "张三", "year": "2023", "source": "教育研究",
        "volume": "32", "issue": "1", "page_range": "56-75", "source_type": "J",
    })
    p = tmp_db.get_project("p1")
    # 走**运行期那个入口**（生成循环落盘与引用重排都调它），不是手搓一份快照 ——
    # 手搓的那份必然带上这里想验的字段，也就验不到任何东西。
    p["citation_binding_json"] = {"1.1 研究背景": [{"doc_id": "d1", "doc_title": "中文题名"}]}
    snapshot = projects._project_refs_snapshot(p, [{"title": "1.1 研究背景"}])

    assert snapshot[0]["formatted"] == (
        "[1] 张三. (2023). 中文题名 [The English Title]. 教育研究, 32(1), 56-75."
    ), "运行期这一份必须已经带上 APA 7 §9.38 的英译方括号"
    assert snapshot[0]["title_en"] == "The English Title", \
        "少这一项，formatted 就少一对方括号，迁移随即判成「变了」"

    tmp_db.update_project("p1", references_json=snapshot)
    assert tmp_db._migrate_reference_snapshots() == 0, "终态快照不该被重写"


def test_null_columns_do_not_make_the_runtime_snapshot_stale(tmp_db):
    """列存在但值是 NULL 时，运行期快照与迁移重算出来的必须**是同一份**。

    缺陷形态（修复前必红）：`_plan_citations` 原用 `d.get(key, "")` 取字段，而 `.get`
    的默认值**只在键不存在时**生效 —— 列存在、值为 NULL 时它返回 `None`。存量行
    （补列之前建的）以及任何抽取没抽到的项都会走到这一支。运行期于是往快照里写下
    `null`，迁移那一侧过 `clean_meta_value` 收敛成 `""`，两边不是同一个值 →
    每次启动都把同一份 `references_json` 重写一遍。

    症状很轻（`None` 与 `""` 渲染出来一模一样，用户看不见），但它是「运行期写下的
    == 启动时重算出来的」这条不变量的破口，而且破在**判据有两把尺子**这件事上 ——
    与 title_en 那次同类，只是这次脏在值、不在键。修法是 `_doc_field` 让两侧过同一个
    `clean_meta_value`。

    用一个**什么都不填**的文献构造出全部列为 NULL 的形态：走运行期那个入口算快照，
    落库后再让迁移读一遍，必须 0 处改写。
    """
    tmp_db.create_project("p1")
    tmp_db.add_document("p1", {
        "id": "d1", "filename": "a.pdf", "title": "题名", "authors": "张三",
        "year": "2023", "source": "教育研究",
    })
    row = tmp_db.get_document("d1")
    for column in ("volume", "issue", "page_range", "source_type", "title_en",
                   "place", "edition", "publish_date"):
        assert row[column] is None, "这条用例的前提就是这些列真的落成了 NULL"

    p = tmp_db.get_project("p1")
    p["citation_binding_json"] = {"1.1 研究背景": [{"doc_id": "d1", "doc_title": "题名"}]}
    snapshot = projects._project_refs_snapshot(p, [{"title": "1.1 研究背景"}])
    for column in ("volume", "issue", "pages", "source_type", "title_en",
                   "place", "edition", "publish_date"):
        assert snapshot[0][column] == "", f"{column} 必须是空串，不能是 None"

    tmp_db.update_project("p1", references_json=snapshot)
    assert tmp_db._migrate_reference_snapshots() == 0, "终态快照不该被重写"


def test_runtime_snapshot_with_non_journal_columns_survives_the_migration(tmp_db):
    """非期刊类型那三列**填满**时，运行期快照写进库、迁移必须原样放行（0 处改写）。

    为什么非要有这一条：既有的快照哨兵用的都是**新列全空**的文献 —— 而全空时那四份
    手写字段清单里凡是与这三列有关的错配全都照不出来，因为四条分支汇聚到同一条兜底
    路径上。只有让新列带上真值走一遍「运行期算 → 落库 → 迁移重算」，那四份清单才算
    真的被对过一遍。

    这里用 `[M]` 专著：它的形态与期刊不同（`出版地: 出版者, 年: 页码.`，且版本项
    自成一个著录项），所以这条同时是「新模板在迁移这一层也成立」的哨兵 —— 迁移那一侧
    读得到 place/edition，运行期这一侧漏了任何一列，两边就不是同一个字符串。
    """
    tmp_db.create_project("p1")
    tmp_db.add_document("p1", {
        "id": "d1", "filename": "a.pdf", "title": "人工智能导论",
        "authors": "张三", "year": "2020", "source": "高等教育出版社",
        "source_type": "M", "place": "北京", "edition": "第3版",
        "page_range": "56-75",
    })
    p = tmp_db.get_project("p1")
    p["citation_binding_json"] = {"1.1 研究背景": [{"doc_id": "d1", "doc_title": "人工智能导论"}]}
    snapshot = projects._project_refs_snapshot(p, [{"title": "1.1 研究背景"}])

    assert snapshot[0]["formatted"] == (
        "[1] 张三. 人工智能导论[M]. 第3版. 北京: 高等教育出版社, 2020: 56-75."
    ), "运行期这一份必须已经走的是专著模板，不是期刊兜底"
    assert (snapshot[0]["place"], snapshot[0]["edition"]) == ("北京", "第3版"), \
        "少任何一列，formatted 都会退回期刊形态，而迁移那边读得到它们"

    tmp_db.update_project("p1", references_json=snapshot)
    assert tmp_db._migrate_reference_snapshots() == 0, "终态快照不该被重写"


def test_migrate_reference_snapshots_uses_the_project_language(tmp_db):
    """迁移必须拿着项目自己的 writing_lang 去重算。

    英文项目里一条按 GB/T 写成的旧 formatted 要重算成 **APA**（`effective_format` 会
    把 en + gb7714 收敛成该语言的默认格式）—— 迁移漏传语言的话，这里会照旧算出
    GB/T 形态，而运行期写出的是 APA：同一份设置算出两串不一样的文本，`citations_stale`
    从此常年亮着，用户按了重排也修不好。
    """
    tmp_db.create_project("p1")
    tmp_db.update_project("p1", writing_lang="en")
    tmp_db.add_document("p1", {
        "id": "d1", "filename": "a.pdf", "title": "题名",
        "title_en": "The Title",
        "authors": "张三", "year": "2023", "source": "教育研究",
        "volume": "32", "issue": "1", "page_range": "56-75", "source_type": "J",
    })
    tmp_db.update_project("p1", references_json=[{
        "num": 1, "doc_id": "d1", "doc_title": "题名",
        "formatted": "[1] 张三. 题名[J]. 教育研究, 2023, 32(1): 56-75.",
    }])

    assert tmp_db._migrate_reference_snapshots() == 1
    formatted = tmp_db.get_project("p1")["references_json"][0]["formatted"]
    # 非英语文献在 APA 下是「原文题名 [英译]」，作者也是 APA 形态（姓前名后）
    assert formatted == "[1] 张三. (2023). 题名 [The Title]. 教育研究, 32(1), 56-75."


def test_migrate_reference_snapshots_never_leaves_an_empty_formatted(tmp_db):
    """重算之后每条都有 `formatted`，哪怕元数据全空、哪怕快照里原本没这个键。

    这条是前端那个兜底分支（「本条格式未生成，请到引用调度步重排」）在实际中不可达的
    依据：`[n]（未命名文献）` 这类条文至少印出一个编号，比一句「未生成」有用得多。
    """
    tmp_db.create_project("p1")
    tmp_db.add_document("p1", {"id": "d1", "filename": "a.pdf"})   # 元数据全空
    tmp_db.update_project("p1", references_json=[
        {"num": 1, "doc_id": "d1", "doc_title": "题名"},           # 连 formatted 都没有
        {"num": 2, "doc_id": "已经删掉的文献", "doc_title": ""},    # 文献行不存在
    ])

    tmp_db._migrate_reference_snapshots()
    refs = tmp_db.get_project("p1")["references_json"]
    assert all(r["formatted"].strip() for r in refs), refs
    # 题名走快照里冻着的那一份（它才是权威），元数据全空也照样印得出一条
    assert refs[0]["formatted"] == "[1] 题名[J]."
    # 连题名都没有（文献已删）时才是空壳分支 —— 至少还印得出一个编号
    assert refs[1]["formatted"] == "[2] （未命名文献）[J]."
