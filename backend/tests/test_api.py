"""测试：API 端到端流程（不依赖 LLM，走占位/兜底路径）。"""
import io

import pytest
from fastapi.testclient import TestClient

from app import paper_types


def _make_pdf(text_lines):
    """构造含文本层的最小合法 PDF。"""
    content = "BT /F1 14 Tf 72 740 Td 20 TL "
    for line in text_lines:
        content += f"({line}) Tj T* "
    content += "ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content.encode())} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(pdf))
        pdf += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(pdf)
    pdf += f"xref\n0 {len(objs)+1}\n".encode()
    pdf += b"0000000000 65535 f \n"
    for off in offsets:
        pdf += f"{off:010d} 00000 n \n".encode()
    pdf += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return pdf


def _make_client(tmp_db):
    """构造 TestClient，使用临时数据库。"""
    from app.main import app
    # app 已在 conftest 的 tmp_db fixture 中初始化 db
    return TestClient(app)


def _wait_task(client, pid, timeout=10.0):
    """轮询通用任务进度至终态，返回终态 task 字典。

    大纲生成与文献解析都改成了后台任务，响应里不再带结果，必须等任务落定。
    """
    import time

    deadline = time.monotonic() + timeout
    task: dict = {}
    while time.monotonic() < deadline:
        task = client.get(f"/api/projects/{pid}/task/progress").json()["task"]
        if task.get("status") != "running":
            return task
        time.sleep(0.02)
    return task


def _generate_outline(client, pid, timeout=10.0) -> dict:
    """触发大纲生成并等它跑完，返回落盘的大纲。"""
    r = client.post(f"/api/projects/{pid}/outline/generate")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running"
    task = _wait_task(client, pid, timeout)
    assert task.get("status") == "done", task
    return client.get(f"/api/projects/{pid}").json()["outline_json"]


def _upload_documents(client, pid, files, timeout=10.0) -> tuple[dict, list]:
    """上传文献并等解析跑完，返回 (终态 task, 入库文献列表)。"""
    r = client.post(f"/api/projects/{pid}/documents", files=files)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running"
    task = _wait_task(client, pid, timeout)
    return task, client.get(f"/api/projects/{pid}/documents").json()


def test_upload_documents_rechecks_busy_after_reading_files(tmp_db):
    """读完字节之后必须**再查一次**忙闲 —— 第一道检查挡不住这个竞态。

    /documents 与其它点火接口形状不同：它中间夹着一串 `await f.read()`。两个上传
    请求可以先后通过第一道 409 检查，再各自往下走，后到的把 _TASKS 里的登记覆盖掉：
    先起的那个从此不在 _busy_task 眼里（没人能查、没人能取消），两个 _run_parse 同时
    解析同一批 PDF，各自 add_document（doc id 是当场新生成的，全链路没有去重兜底），
    于是每篇文献入库两次；两条任务的进度还轮流覆盖 task_state_json。

    这里把「另一个请求在 await 期间占了任务位」直接做出来：让 read() 在返回前登记一个
    假任务 —— 那正是事件循环切走时会发生的事。断言第二道检查拦住了它，且这一次请求
    **没有留下任何痕迹**：不起任务、不写进度、不落文献。
    """
    from starlette.datastructures import UploadFile

    from app import db as db_module
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"title": "并发上传"}).json()["id"]

        class _FakeTask:
            def done(self) -> bool:
                return False

        key = f"documents:{pid}"
        original_read = UploadFile.read

        async def _read_then_race(self):
            data = await original_read(self)
            # 别的请求已经占了任务位 —— 第一道检查是在这之前跑的，看不见它
            projects_router.tasks._TASKS[key] = _FakeTask()
            return data

        UploadFile.read = _read_then_race
        try:
            r = client.post(
                f"/api/projects/{pid}/documents",
                files=[("files", ("a.pdf", _make_pdf(["content"]), "application/pdf"))],
            )
        finally:
            UploadFile.read = original_read
            projects_router.tasks._TASKS.pop(key, None)

        assert r.status_code == 409, r.text
        assert "文献正在解析中" in r.json()["detail"]
        # 被拒绝的这一次没有留下任何痕迹：没落文献、没写任务进度
        assert db_module.list_documents(pid) == []
        assert client.get(f"/api/projects/{pid}").json()["task_state_json"] is None

        # 守卫不能变成锁：任务位空出来之后，同一批文件必须能正常上传
        task, docs = _upload_documents(
            client, pid,
            files=[("files", ("a.pdf", _make_pdf(["content"]), "application/pdf"))],
        )
        assert task.get("status") == "done", task
        assert len(docs) == 1


def test_upload_documents_missing_project_is_404(tmp_db):
    """项目不存在时必须先 404 —— 这条路由此前**根本没查过项目在不在**。

    原来的结局是一条永远不动的进度：上传先返回 200 + running，随后整批在
    db.add_document 那一步撞上 documents.project_id 的外键约束（get_conn 开了
    PRAGMA foreign_keys），任务以 error 收场；而那份 error 也写不进一个不存在的
    项目（update_project 影响 0 行）。同级入口全部先 404。
    """
    from app.main import app

    with TestClient(app) as client:
        r = client.post(
            "/api/projects/nosuchproject/documents",
            files=[("files", ("a.pdf", _make_pdf(["content"]), "application/pdf"))],
        )
        assert r.status_code == 404, r.text
        assert "不存在" in r.json()["detail"]


def test_upload_skips_duplicate_document(tmp_db):
    """同一篇文献传两遍：第二遍不落库，并且**点名说是哪一篇**。

    判据是内容指纹（db.document_fingerprint），作用范围限同一个项目内。没有这道判重
    时，同一份 PDF 会占两个 doc_id —— 正文里两个角标指向同一篇文献、文末列表出现两条
    内容相同却编号不同的著录，而删掉其中一条另一条还在。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        pdf = _make_pdf(["Same paper."])
        _, docs = _upload_documents(
            client, pid, files=[("files", ("a.pdf", pdf, "application/pdf"))])
        assert len(docs) == 1

        # 换个文件名重传：文件名不是判据，内容才是
        task, docs = _upload_documents(
            client, pid, files=[("files", ("a-renamed.pdf", pdf, "application/pdf"))])
        assert task["status"] == "done", task
        assert task["saved"] == 0
        assert len(docs) == 1, "重传的那份没有被跳过"
        assert [s["filename"] for s in task["skipped"]] == ["a-renamed.pdf"]
        assert "与《a》内容相同" in task["skipped"][0]["reason"]


def test_upload_dedups_within_one_batch_without_touching_real_documents(tmp_db):
    """一次多选里把同一个文件选了两遍也要判出来（两篇都还没入库，只查库查不到），
    同时不能误伤真正不同的文献。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        first = _make_pdf(["Paper one."])
        second = _make_pdf(["Paper two."])
        task, docs = _upload_documents(client, pid, files=[
            ("files", ("one.pdf", first, "application/pdf")),
            ("files", ("one-again.pdf", first, "application/pdf")),
            ("files", ("two.pdf", second, "application/pdf")),
        ])
        assert task["status"] == "done", task
        assert task["saved"] == 2
        assert len(docs) == 2
        assert [s["filename"] for s in task["skipped"]] == ["one-again.pdf"]


def test_upload_dedup_catches_documents_uploaded_before_the_fingerprint(tmp_db):
    """改动之前就传过的老文献也要能被认出来。

    指纹打在**落库形态**（page + snippet）上而不是原始 PDF 字节上，唯一理由就是这个：
    PDF 字节读完就丢了（见 upload_documents），只有 pages_json 还留着，于是启动时的
    _migrate_document_hashes 能把老行反算出同一个指纹，用户重传一篇老文献照样会被跳过。
    若打在字节上，老文献这一列只能永远是空，判重对它们完全失效。
    """
    from app import db as db_module
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        pdf = _make_pdf(["Legacy paper."])
        _upload_documents(
            client, pid, files=[("files", ("old.pdf", pdf, "application/pdf"))])
        doc_id = client.get(f"/api/projects/{pid}/documents").json()[0]["id"]

        # 把这一行退回「迁移还没跑过」的样子：指纹列为空
        with db_module.get_conn() as conn:
            conn.execute(
                "UPDATE documents SET content_hash = NULL WHERE id = ?", (doc_id,))
        pages = db_module.get_document(doc_id)["pages"]
        assert db_module.existing_document_title(
            pid, db_module.document_fingerprint(pages)) is None, "清空后应当认不出来"

        assert db_module._migrate_document_hashes() == 1

        task, docs = _upload_documents(
            client, pid, files=[("files", ("old.pdf", pdf, "application/pdf"))])
        assert task["saved"] == 0
        assert len(docs) == 1
        assert [s["filename"] for s in task["skipped"]] == ["old.pdf"]


def test_full_flow(tmp_db):
    """完整流程（文献综述工序）：选题→文献→大纲→引用→生成。

    用「文献综述」而非其他类型，正是为了覆盖条件换序：它必须先有文献才能生成大纲。
    """
    import time

    from app.main import app

    # with 形式才会启动 lifespan 与常驻事件循环；否则请求结束即回收 portal，
    # 后台生成任务可能在跑完前就被拆掉。
    with TestClient(app) as client:
        # 1. 创建项目
        r = client.post("/api/projects", json={"title": "测试论文"})
        assert r.status_code == 200
        pid = r.json()["id"]

        # 2. 选题
        r = client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "文献综述", "target_words": 1000, "topic": "测试选题",
        })
        assert r.status_code == 200
        assert r.json()["status"] == "topic_set"

        # 3. 文献综述：大纲必须先有文献，此处应被拦截
        r = client.post(f"/api/projects/{pid}/outline/generate")
        assert r.status_code == 400
        assert "文献" in r.json()["detail"]

        # 4. 上传文献（后台解析，需等任务落定）
        pdf = _make_pdf(["Doc: AI in Education", "Author: Zhang, 2023", "Content text."])
        task, docs = _upload_documents(
            client, pid, files=[("files", ("doc.pdf", pdf, "application/pdf"))]
        )
        assert task["saved"] == 1
        assert len(docs) == 1

        # 5. 大纲生成 + 确认（现在放行）
        outline = _generate_outline(client, pid)
        r = client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        assert r.status_code == 200
        assert r.json()["matched"] is True

        # 6. 引用调度 + 确认（含角标样式）
        r = client.post(f"/api/projects/{pid}/citations/schedule")
        assert r.status_code == 200
        binding = r.json()["binding"]
        r = client.post(f"/api/projects/{pid}/citations/confirm",
                        json={"binding": binding, "cite_style": "bracket"})
        assert r.status_code == 200

        # 7. 触发生成：应立即返回，不等正文
        r = client.post(f"/api/projects/{pid}/generate")
        assert r.status_code == 200
        assert r.json()["status"] == "running"
        assert r.json()["total"] > 0

        # 8. 轮询至终态（无 LLM 时走占位文本，秒级完成）
        payload = {}
        for _ in range(200):
            payload = client.get(f"/api/projects/{pid}/generate/progress").json()
            if payload["status"] != "running":
                break
            time.sleep(0.02)

        assert payload["status"] == "done", payload
        assert payload["done"] == payload["total"]
        assert len(payload["sections"]) > 0
        assert len(payload["references"]) >= 1
        assert payload["cite_style"] == "bracket"
        assert payload["total_words"] > 0
        assert payload["eta_seconds"] == 0

        # 9. 项目状态已落定，且参考文献已持久化（刷新页面不再丢）
        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["status"] == "completed"
        assert proj["references_json"]


def test_upload_rejects_invalid_then_accepts_valid(tmp_db):
    """上传容错：非 PDF 文件被记为失败，不影响有效文件。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })

        pdf = _make_pdf(["Valid PDF content."])
        task, docs = _upload_documents(
            client, pid,
            files=[
                ("files", ("bad.txt", b"not a pdf", "text/plain")),
                ("files", ("good.pdf", pdf, "application/pdf")),
            ],
        )
        assert task["saved"] == 1  # 只有 valid 文件成功
        assert len(task["failed"]) == 1  # bad.txt 被记为失败
        assert len(docs) == 1


def test_health_and_meta(tmp_db):
    """健康检查与元信息接口。"""
    from app.main import app
    client = TestClient(app)
    assert client.get("/api/health").json()["status"] == "ok"
    meta = client.get("/api/meta").json()
    assert "llm_configured" in meta
    assert len(meta["paper_types"]) == 7
    assert "毕业论文" not in meta["paper_types"]
    # 改名前的旧类型名不得再出现在清单里（它们会被迁移掉）
    for gone in ("实证研究", "课程论文", "技术报告"):
        assert gone not in meta["paper_types"]

    # 前端只渲染这份工序表，不再自己拼顺序：后端闸门与步骤条是同一件事的两面
    cfg = meta["paper_type_config"]
    assert set(cfg) == set(meta["paper_types"])
    assert cfg["文献综述"]["steps"][:2] == ["topic", "resources"], "综述先文献后大纲"
    assert cfg["定量/实证研究"]["steps"][1] == "design", "实证设计必须排在大纲之前"
    assert cfg["定量/实证研究"]["design_label"] == "实证设计"
    assert cfg["技术/工程报告"]["citations_required"] is False
    assert cfg["课程论文/小论文"].get("design_fields") is None
    assert cfg["定量/实证研究"]["flow_hint"]

    # theme_sections 是从骨架现算的：前端拿它跟「作者确认了几个主题」比对
    assert cfg["文献综述"]["theme_sections"] == 3
    assert cfg["定量/实证研究"]["theme_sections"] == 0

    # 短名存库、长名显示：前端下拉框渲染 labels，提交回去的仍是短名
    labels = meta["paper_type_labels"]
    assert set(labels) == set(meta["paper_types"])
    assert labels["文献综述"] == "文献综述（全学科通用）"
    assert labels["定量/实证研究"].startswith("定量/实证研究（")


def test_meta_ships_the_structure_and_writing_notes(tmp_db):
    """两个新提示词字段必须随 /api/meta 下发。

    它们进的是大纲与正文提示词，前端不读；但「整包下发 config_for」这一条是
    前端的兜底镜像得以保持简单的依据 —— 一旦变成挑字段下发，前端就得自己拼。
    """
    from app.main import app

    meta = TestClient(app).get("/api/meta").json()
    for name, cfg in meta["paper_type_config"].items():
        assert cfg.get("structure_note"), f"{name} 缺 structure_note"
        assert cfg.get("writing_note"), f"{name} 缺 writing_note"
        assert cfg.get("flow_hint"), f"{name} 缺 flow_hint"


def test_recommend_topics_fallback_has_no_fabricated_match(tmp_db):
    """未配置 LLM 时的兜底选题不报「匹配度」。

    兜底是三个模板，没有跟任何东西比对过，先前却各自带着写死的 90/85/80，界面照印
    「匹配度 90」—— 一个凭空的分数会让用户以为系统评估过这些选题。同一条规矩在
    进度条那边也立过：不知道总量就不给百分比。这里连同「模型自己没给 match」一起
    兜住 —— 界面按「有才显示」，键缺失既不报错也不印 undefined。
    """
    from app.main import app

    client = TestClient(app)
    pid = client.post("/api/projects", json={}).json()["id"]
    r = client.post(f"/api/projects/{pid}/topics/recommend", json={
        "domain": "数字化转型", "paper_type": "课程论文/小论文",
        "target_words": 1000, "count": 3,
    })
    assert r.status_code == 200, r.text
    topics = r.json()["topics"]
    assert len(topics) == 3
    for t in topics:
        assert t["title"] and t["question"] and t["feasibility"]
        assert "match" not in t
    # 兜底文案按请求到的类型取，不能写死某一类的名字
    assert all("课程论文/小论文" in t["feasibility"] for t in topics)


def test_set_topic_rejects_unknown_paper_type(tmp_db):
    """拼错的论文类型应被拒绝，而不是静默退化成课程论文。"""
    from app.main import app
    client = TestClient(app)

    pid = client.post("/api/projects", json={}).json()["id"]
    r = client.post(f"/api/projects/{pid}/topic", json={
        "paper_type": "文献综速", "target_words": 1000, "topic": "t",
    })
    assert r.status_code == 400
    assert "文献综速" in r.json()["detail"]


def test_set_topic_cascade_invalidates_downstream(tmp_db):
    """改动选题/类型/字数会把下游产物整份作废（用户已确认的覆盖式策略）。

    守的是「正文按旧选题写、引用编号按新选题算」那类**看不见**的错配：章节标题
    一个都没变，界面上看不出异常，只会在导出的论文里对不上。
    """
    from app import db
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        same = {"paper_type": "课程论文/小论文", "target_words": 1000, "topic": "旧选题"}
        client.post(f"/api/projects/{pid}/topic", json=same)
        outline = _generate_outline(client, pid)
        client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        # 几份下游产物都直接落库造出「已生成完」的现场：引用调度有它自己的前置
        # （无文献的课程论文排不出绑定），那条链路由别的用例覆盖，这里只测作废。
        db.update_project(
            pid,
            citation_binding_json={"绪论": [1]},
            sections_json=[{"section_title": "绪论", "content": "旧正文"}],
            references_json=[{"title": "某文献"}],
            materials_plan_json={"plan": [{"section": "绪论"}]},
            generation_state_json={"status": "done", "done": 1, "total": 1},
        )
        before = client.get(f"/api/projects/{pid}").json()
        assert before["outline_json"] and before["citation_binding_json"]

        r = client.post(f"/api/projects/{pid}/topic", json={**same, "topic": "新选题"})
        assert r.status_code == 200
        after = r.json()
        assert after["status"] == "topic_set"
        for col in ("outline_json", "citation_binding_json", "materials_plan_json",
                    "sections_json", "references_json", "generation_state_json"):
            assert after[col] is None, f"{col} 应随选题改动一起作废"


def test_set_topic_unchanged_keeps_downstream(tmp_db):
    """原样重提不得作废任何东西。

    点「确认选题」很可能只是路过（或改个错别字又改回来）。不做这个判断，用户就会
    平白丢掉一份好大纲和已生成的正文 —— 而作废是**不可撤销**的。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        same = {"paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t"}
        client.post(f"/api/projects/{pid}/topic", json=same)
        outline = _generate_outline(client, pid)
        client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        before = client.get(f"/api/projects/{pid}").json()

        r = client.post(f"/api/projects/{pid}/topic", json=same)
        assert r.status_code == 200
        assert r.json()["outline_json"] == before["outline_json"], "原样重提不该动大纲"
        # status 也不能被打回 topic_set：否则刷新后 statusToStepKey 把用户送回上一步
        assert r.json()["status"] == before["status"]


def test_set_topic_ideas_change_invalidates_downstream(tmp_db):
    """只改写作思路也要作废下游六项 —— 与改选题同一口径。

    思路变了，库里那份正文就是按旧思路写的；留着比"重做一遍"更贵，且这种错配在
    界面上看不出来（章节标题一个都没变）。作废是覆盖式的，不新增第二套逻辑。
    """
    from app import db
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        same = {"paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
                "writing_ideas": "旧思路"}
        client.post(f"/api/projects/{pid}/topic", json=same)
        outline = _generate_outline(client, pid)
        client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        db.update_project(
            pid,
            citation_binding_json={"绪论": [1]},
            sections_json=[{"section_title": "绪论", "content": "旧正文"}],
            references_json=[{"title": "某文献"}],
            materials_plan_json={"plan": [{"section": "绪论"}]},
            generation_state_json={"status": "done", "done": 1, "total": 1},
        )
        assert client.get(f"/api/projects/{pid}").json()["outline_json"]

        r = client.post(f"/api/projects/{pid}/topic", json={**same, "writing_ideas": "新思路"})
        assert r.status_code == 200
        after = r.json()
        assert after["writing_ideas"] == "新思路"
        assert after["status"] == "topic_set"
        for col in ("outline_json", "citation_binding_json", "materials_plan_json",
                    "sections_json", "references_json", "generation_state_json"):
            assert after[col] is None, f"{col} 应随写作思路改动一起作废"


def test_set_topic_unchanged_ideas_keeps_downstream(tmp_db):
    """思路原样重提（含"只是末尾多了个换行"）不得作废任何东西。

    多行输入框里敲完一段顺手回车、末尾带一个 "\\n"，那是常态而不是边界。裸比的话，
    一次「只是换行」的点击就会把六项产物整份作废掉 —— 而作废不可撤销。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        same = {"paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
                "writing_ideas": "先辨析 A 与 B"}
        client.post(f"/api/projects/{pid}/topic", json=same)
        outline = _generate_outline(client, pid)
        client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        before = client.get(f"/api/projects/{pid}").json()
        # 选题接口的响应里必须带这一列：前端 loadProject 靠它播种那个输入框
        assert before["writing_ideas"] == "先辨析 A 与 B"

        r = client.post(f"/api/projects/{pid}/topic",
                        json={**same, "writing_ideas": "先辨析 A 与 B\n"})
        assert r.status_code == 200
        assert r.json()["outline_json"] == before["outline_json"], "原样重提不该动大纲"
        assert r.json()["status"] == before["status"]
        assert r.json()["writing_ideas"] == "先辨析 A 与 B", "归一化后落库，不留多余空白"


def test_set_topic_clearing_ideas_stores_null(tmp_db):
    """清空思路落成 NULL：库里的「没写过」只留一种表示（同 title 那条路）。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        base = {"paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t"}
        client.post(f"/api/projects/{pid}/topic", json={**base, "writing_ideas": "旧思路"})

        # 只填空格等于清空，不是"填了一段空白"
        r = client.post(f"/api/projects/{pid}/topic", json={**base, "writing_ideas": "   \n "})
        assert r.status_code == 200
        assert r.json()["writing_ideas"] is None


def test_set_topic_rejects_overlong_ideas_and_leaves_no_trace(tmp_db):
    """超过上限用路由内 400，且被拒的请求不留痕。

    不用 pydantic max_length：那会抛 422，而前端 api.js 的 new Error(err.detail) 会把
    detail 数组渲染成 [object Object]。消息里要带上实际字数 —— 前端刻意不镜像这个上限
    （不做 maxLength、不做计数器），用户唯一的提示就是这句话。
    """
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        base = {"paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t"}
        client.post(f"/api/projects/{pid}/topic", json={**base, "writing_ideas": "原思路"})

        too_long = "字" * (projects_router.MAX_IDEAS_CHARS + 1)
        r = client.post(f"/api/projects/{pid}/topic", json={**base, "writing_ideas": too_long})
        assert r.status_code == 400
        assert isinstance(r.json()["detail"], str), "detail 必须是字符串，不能是数组"
        detail = r.json()["detail"]
        assert str(projects_router.MAX_IDEAS_CHARS) in detail
        assert str(projects_router.MAX_IDEAS_CHARS + 1) in detail, "要说清当前多少字"
        assert client.get(f"/api/projects/{pid}").json()["writing_ideas"] == "原思路", \
            "被拒的请求不留痕"


def test_set_topic_rejects_too_few_words_and_leaves_no_trace(tmp_db):
    """低于下限用路由内 400，且被拒的请求不留痕。

    与上限同一口径（不用 pydantic 的 Field(ge=...)）：那会抛 422，而前端 api.js 的
    new Error(err.detail) 会把 detail 数组渲染成 [object Object]。消息里下限与当前值
    都要有 —— 用户改这个数的时候，两头的数都得看得见才知道往哪边挪。
    """
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        base = {"paper_type": "课程论文/小论文", "topic": "t"}
        client.post(f"/api/projects/{pid}/topic", json={**base, "target_words": 3000})

        too_few = projects_router.MIN_TARGET_WORDS - 1
        r = client.post(f"/api/projects/{pid}/topic", json={**base, "target_words": too_few})
        assert r.status_code == 400
        assert isinstance(r.json()["detail"], str), "detail 必须是字符串，不能是数组"
        detail = r.json()["detail"]
        assert str(projects_router.MIN_TARGET_WORDS) in detail, "要说清下限是多少"
        assert str(too_few) in detail, "要说清当前是多少"
        assert client.get(f"/api/projects/{pid}").json()["target_words"] == 3000, \
            "被拒的请求不留痕"


def test_set_topic_accepts_exactly_the_minimum(tmp_db):
    """下限本身是**合法**的：判据是 <，不是 <=。

    边界写反会让刚好填下限的用户被拒 —— 而那个值正是既有用例到处在用的 1000，
    所以这条顺带把「下限被谁悄悄抬高」也钉住了。
    """
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        r = client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "topic": "t",
            "target_words": projects_router.MIN_TARGET_WORDS,
        })
        assert r.status_code == 200, r.text
        assert r.json()["target_words"] == projects_router.MIN_TARGET_WORDS


def test_meta_ships_the_minimum_target_words(tmp_db):
    """下限随 /api/meta 下发。

    前端的提示文案、推荐按钮的禁用与提交前那道检查都读它。若前端改回自己抄一份，
    界面就能说出一个后端并不认的数字（用户照着填，照样被 400 挡回来）—— 「短名存库、
    长名下发」与 steps 是同一条规矩：前端的依据要么是库里的字段，要么是这里下发的。
    """
    from app.main import app
    from app.routers import projects as projects_router

    meta = TestClient(app).get("/api/meta").json()
    assert meta["min_target_words"] == projects_router.MIN_TARGET_WORDS


def test_recommend_topics_is_not_gated_by_the_minimum(tmp_db):
    """选题推荐**不**受字数下限约束。

    它不落库，只经提示词影响那份推荐的质量 —— 而「想先看看有什么题、字数还没定」
    是一条正常的路，拦它纯是摩擦。前端另在按钮上拦了一道：那拦的不是这个接口，
    而是一份按 0 字评出来的、用户看不出哪里不对的推荐。
    """
    from app.main import app
    from app.routers import projects as projects_router

    client = TestClient(app)
    pid = client.post("/api/projects", json={}).json()["id"]
    r = client.post(f"/api/projects/{pid}/topics/recommend", json={
        "domain": "数字化转型", "paper_type": "课程论文/小论文",
        "target_words": projects_router.MIN_TARGET_WORDS - 1, "count": 3,
    })
    assert r.status_code == 200, r.text


def test_recommend_topics_follows_the_language_the_form_sends(tmp_db, monkeypatch):
    """推荐选题的语言按**请求里那份草稿**走，不是按库里那份。

    新建项目的 `writing_lang` 还是默认的中文，而用户在选题表单里可能已经选了英文 ——
    在确认项目之前点「推荐」，拿回来的就是中文选题（而这些标题会成为论文标题，
    这一页上看不出任何异常）。原先的注释写着「语言在这一步之前由同一个表单设置，
    两者总是同源」，**那句是假话**，这个缺陷正是被它盖住的。

    回落的另一半同样要钉：请求里没带（空串哨兵）时用库里那份。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        calls = _capture_llm(monkeypatch, {"topics": []})
        body = {"domain": "数字化转型", "paper_type": "课程论文/小论文",
                "target_words": 1000, "count": 3}

        # 库里是 zh（默认）、请求里说英文 → 按英文
        client.post(f"/api/projects/{pid}/topics/recommend",
                    json={**body, "writing_lang": "en"})
        assert "输出语言" in calls[0], "表单里选的英文没有到达提示词"

        # 不带这个字段 → 回落库里的 zh
        calls.clear()
        client.post(f"/api/projects/{pid}/topics/recommend", json=body)
        assert "输出语言" not in calls[0], "没发这个字段时应当用库里的语言"

        # 库里也是英文之后，同样不带 → 回落库里的 en
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
            "writing_lang": "en",
        })
        calls.clear()
        client.post(f"/api/projects/{pid}/topics/recommend", json=body)
        assert "输出语言" in calls[0], "回落读的不是库里的语言"


def test_set_topic_without_writing_lang_means_do_not_change(tmp_db):
    """省去 `writing_lang` 不是「改成中文」，而是「不改」—— 请求里那份优先，空串才读库里的。

    旧默认值是 `"zh"`：任何只发四个字段的调用方（`/topic` 是外部可调用的接口）都会让
    库里的**英文**项目被判成「语言变了」，于是大纲、正文、引用绑定、材料计划、参考文献
    快照、主题聚类与生成进度**整份作废** —— 用户没做错任何事，只因为调用方少发了一个
    字段。修法用空串当哨兵，判据仍是「库里的语言有没有变」。

    两半都要钉：省去 = 不改（上半），显式发 `"zh"` 仍然是一次真正的语言变更（下半）——
    只钉上半的话，把这个字段做成「永远忽略」也照样绿，而那就把改语言这个功能删了。
    """
    from app.main import app

    with TestClient(app) as client:
        # 上半：产物齐全的英文项目，原样重提四个字段
        tmp_db.create_project("p1")
        tmp_db.update_project(
            "p1", writing_lang="en", citation_format="apa", topic="t",
            paper_type="课程论文/小论文", target_words=1000,
            outline_json={"chapters": [{"title": "第一章", "sections": []}]},
            sections_json=[{"section_title": "1.1", "content": "body"}],
            citation_binding_json={"1.1": [{"doc_id": "d1"}]},
            clusters_json={"clusters": [{"name": "主题"}]},
            references_json=[{"num": 1, "doc_id": "d1", "formatted": "[1] X."}],
        )
        r = client.post("/api/projects/p1/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        assert r.status_code == 200, r.text
        p = tmp_db.get_project("p1")
        assert p["writing_lang"] == "en", "省去该字段不该把语言掰回中文"
        assert p["sections_json"], "更不该因此作废正文"
        assert p["citation_binding_json"] and p["clusters_json"]
        assert p["references_json"] and p["outline_json"]
        assert p["citation_format"] == "apa"

        # 下半：显式发 zh —— 这是一次真的语言变更，覆盖式作废照旧
        tmp_db.create_project("p2")
        tmp_db.update_project(
            "p2", writing_lang="en", citation_format="apa", topic="t",
            paper_type="课程论文/小论文", target_words=1000,
            sections_json=[{"section_title": "1.1", "content": "body"}],
            clusters_json={"clusters": [{"name": "主题"}]},
        )
        client.post("/api/projects/p2/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
            "writing_lang": "zh",
        })
        p2 = tmp_db.get_project("p2")
        assert p2["writing_lang"] == "zh"
        assert p2["sections_json"] is None, "改语言仍然要作废下游产物"
        assert p2["clusters_json"] is None
        # en→zh **不**把格式改回来：APA 在中文写作下合法，擅自改掉等于替用户做选择
        assert p2["citation_format"] == "apa"


def test_writing_ideas_reach_the_outline_prompt(tmp_db, monkeypatch):
    """提交的思路要真的进大纲第一遍的提示词，且只进第一遍。

    前面那些用例各自只钉住 agent 自己那一层（形参表里有、块写好了），而「路由把
    参数塞进 generate_outline 的那一刻漏了」只有端到端才照得出来。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        ideas = "先辨析 A 与 B 两个概念，再论证二者互补而非替代"
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
            "writing_ideas": ideas,
        })
        # LLM 必须在项目建好之后再打开（见 _capture_llm 的注释）
        calls = _capture_llm(monkeypatch, {"core_question": "问题？", "chapters": []})

        _generate_outline(client, pid)

        assert len(calls) >= 2, f"大纲应当跑两遍，实际收到 {len(calls)} 次调用"
        assert ideas in calls[0]
        assert ideas not in calls[1], "第二遍只管篇幅与贴题"


def test_set_topic_type_change_clears_design_but_topic_change_keeps_it(tmp_db):
    """换类型要清设计表单，只改选题不动它。

    设计字段名随类型走：旧类型的字段留在库里不会再被界面渲染，却会在下次保存时被
    原样写回，成为一份看不见也删不掉的残留。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _design_project(client)  # 定量/实证研究，四个设计字段
        client.post(f"/api/projects/{pid}/design",
                    json={"fields": {"数据源与样本": "312 份问卷"}})

        # 只改选题：设计是上游产物，与选题无关，必须留着
        r = client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "定量/实证研究", "target_words": 1000, "topic": "换了个选题",
        })
        # 落库的是白名单过滤后的四个字段（未填的为空串），只看填过的那一个
        assert r.json()["design_json"]["fields"]["数据源与样本"] == "312 份问卷"

        # 换类型：字段名整套换掉，旧表单必须清空
        r = client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "质性研究/案例分析", "target_words": 1000, "topic": "换了个选题",
        })
        assert r.status_code == 200
        assert r.json()["design_json"] is None


def test_set_topic_rejected_while_task_running(tmp_db):
    """有任务在跑时不许改选题 —— 在飞任务结束后会把刚作废的产物写回来。

    分段生成尤其致命：它在结尾用内存里的 results 重写 sections_json，会让刚作废的
    正文原样复活，而那份正文的引用编号是按**旧**选题算的。
    """
    from app.main import app
    from app.routers import projects as projects_router

    class _FakeTask:
        def done(self) -> bool:
            return False

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        body = {"paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t"}
        assert client.post(f"/api/projects/{pid}/topic", json=body).status_code == 200

        # 通用后台任务（写 task_state_json）
        key = f"outline:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/topic", json={**body, "topic": "t2"})
            assert r.status_code == 409
            assert "大纲正在生成中" in r.json()["detail"]
        finally:
            projects_router.tasks._TASKS.pop(key, None)

        # 分段生成走另一条线（_GEN_TASKS，不写 task_state_json），同样要挡
        projects_router._GEN_TASKS[pid] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/topic", json={**body, "topic": "t3"})
            assert r.status_code == 409
            assert "正文正在生成中" in r.json()["detail"]
        finally:
            projects_router._GEN_TASKS.pop(pid, None)

        # 任务结束后必须能正常提交：守卫不能变成一把锁不上的锁
        assert client.post(f"/api/projects/{pid}/topic",
                           json={**body, "topic": "t4"}).status_code == 200


def test_set_topic_missing_project_is_404(tmp_db):
    """ID 不存在要报 404，而不是 UPDATE 影响 0 行后返回 null。"""
    from app.main import app

    r = TestClient(app).post("/api/projects/nope/topic", json={
        "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
    })
    assert r.status_code == 404


def test_generate_outline_missing_project_is_404(tmp_db):
    """/outline/generate 对不存在的项目也要 404，与其余同级入口一致。

    旧实现把「项目不存在」和「项目存在但还没选题」合并成一个 400，客户端无从区分
    「id 拼错了」与「还差一步没做」。全仓同级入口（topic / title / citations/…）
    都是先 404 存在性、再各自校验，这条是漏网的一处（D12）。
    """
    from app.main import app

    r = TestClient(app).post("/api/projects/nope/outline/generate")
    assert r.status_code == 404


def test_generate_outline_without_topic_is_400(tmp_db):
    """项目存在但还没选题：仍是 400「请先完成选题」，别在拆 404 时把它弄丢。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        r = client.post(f"/api/projects/{pid}/outline/generate")
        assert r.status_code == 400
        assert r.json()["detail"] == "请先完成选题"


def test_display_title_falls_back_from_name_to_topic(tmp_db):
    """项目显示名：用户起的名字 > 研究核心方向 > 「未命名论文」。

    它是列表卡片、页头、删除确认、导出 md 一级标题与下载文件名这 5 处的**唯一**
    来源。三个来源各断言一次，列表与详情两条查询路径都要带上它（列表走的是
    db.list_projects 的 SELECT *，与详情不是同一条路）。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        # 还没有选题：这时「未命名论文」是实话
        assert client.get(f"/api/projects/{pid}").json()["display_title"] == "未命名论文"

        r = client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000,
            "topic": "城市社区养老服务的供需错配",
        })
        # 确认选题的**响应本身**也要带 display_title：前端拿它直接 setProject，
        # 页头与侧栏卡片就地显示新名字（若只有 GET 带，这一屏会退回兜底词）
        assert r.json()["display_title"] == "城市社区养老服务的供需错配"
        assert r.json()["title"] == "城市社区养老服务的供需错配"

        rows = client.get("/api/projects").json()
        assert [p["display_title"] for p in rows if p["id"] == pid] == [
            "城市社区养老服务的供需错配"
        ]

        client.post(f"/api/projects/{pid}/title", json={"title": "养老错配"})
        detail = client.get(f"/api/projects/{pid}").json()
        assert detail["display_title"] == "养老错配"
        assert detail["topic"] == "城市社区养老服务的供需错配", "改名不动选题本身"


def test_display_title_collapses_a_multiline_topic(tmp_db):
    """多行的研究核心方向要折成一行，否则 `# 标题` 会被截断、文件名里会带换行。

    「确定研究核心方向」是个 3 行 textarea，粘一段带换行的文字进来是常态。折叠在
    显示边界做，库里的 topic 一个字不动。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        topic = "城市社区养老服务的供需错配\n——以 S 市 12 个社区为例"
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": topic,
        })

        detail = client.get(f"/api/projects/{pid}").json()
        assert "\n" not in detail["display_title"]
        assert detail["display_title"] == "城市社区养老服务的供需错配 ——以 S 市 12 个社区为例"
        assert detail["topic"] == topic, "库里的原文要保持 3 行，折叠只发生在显示上"


def test_title_follows_topic_until_the_user_names_it(tmp_db):
    """标题跟着选题走，直到用户自己起过名；清空标题就回到跟随。

    判据是「title 为空、或恰好还等于旧选题」（两者都说明这个名字不是用户起的），
    不新增「是否手动改过」的标记列。最后一步钉的是「清空 = 回到跟随」这条退路。
    """
    from app.main import app

    body = {"paper_type": "课程论文/小论文", "target_words": 1000, "topic": "选题一"}
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        # 新建时 title 是空的（前端不再写死 '未命名论文'），第一次提交选题就该填上
        assert client.post(f"/api/projects/{pid}/topic", json=body).json()["title"] == "选题一"

        # 还没自己起过名：标题继续跟着换
        r = client.post(f"/api/projects/{pid}/topic", json={**body, "topic": "选题二"})
        assert r.json()["title"] == "选题二"

        # 起过名之后，改选题不再动标题（否则等于没给过这个功能）
        client.post(f"/api/projects/{pid}/title", json={"title": "我的短名"})
        r = client.post(f"/api/projects/{pid}/topic", json={**body, "topic": "选题三"})
        assert r.json()["topic"] == "选题三"
        assert r.json()["title"] == "我的短名"
        assert r.json()["display_title"] == "我的短名"

        # 清空 = 回到跟随：名字立刻变回当前选题，下一次改选题继续跟
        r = client.post(f"/api/projects/{pid}/title", json={"title": ""})
        assert r.status_code == 200
        assert r.json()["title"] is None, "库里的「没有名字」只留 NULL 一种表示"
        assert r.json()["display_title"] == "选题三"
        r = client.post(f"/api/projects/{pid}/topic", json={**body, "topic": "选题四"})
        assert r.json()["title"] == "选题四"


def test_rename_project_title_trims_and_validates(tmp_db):
    """改名去掉首尾空白；超长给 400 且 detail 是字符串；项目不存在给 404。

    长度刻意用路由内的 400 而不是 pydantic 的 max_length：后者抛 422，而前端
    api.js 是 `new Error(err.detail || '请求失败')` —— 422 的 detail 是个数组，
    会在错误横幅里渲染成 [object Object]。
    """
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        assert client.post("/api/projects/nope/title",
                           json={"title": "x"}).status_code == 404

        pid = client.post("/api/projects", json={}).json()["id"]
        r = client.post(f"/api/projects/{pid}/title", json={"title": "  养老错配  "})
        assert r.status_code == 200
        assert r.json()["title"] == "养老错配"

        r = client.post(
            f"/api/projects/{pid}/title",
            json={"title": "长" * (projects_router.MAX_TITLE_CHARS + 1)},
        )
        assert r.status_code == 400
        assert isinstance(r.json()["detail"], str)
        assert client.get(f"/api/projects/{pid}").json()["title"] == "养老错配", "被拒的请求不留痕"


def test_rename_project_keeps_products_and_is_allowed_while_busy(tmp_db):
    """改名不作废任何产物，也不被忙闲守卫拦。

    与 delete_project / set_topic 那两道守卫不是一回事：改名不写 status、不碰大纲 /
    正文 / 引用快照 / 材料计划，生成期间改名也无害。照抄一个 409 上去，用户只会在
    一个纯改名的动作上看到「正文正在生成」。
    """
    from app import db
    from app.main import app
    from app.routers import projects as projects_router

    class _FakeTask:
        def done(self) -> bool:
            return False

    body = {"paper_type": "课程论文/小论文", "target_words": 1000, "topic": "选题"}
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json=body)
        # 直接落库造出「已生成完」的现场（照抄上面作废那条用例的写法）
        db.update_project(
            pid,
            citation_binding_json={"绪论": [1]},
            sections_json=[{"section_title": "绪论", "content": "正文"}],
            references_json=[{"title": "某文献"}],
            materials_plan_json={"plan": [{"section": "绪论"}]},
            generation_state_json={"status": "done", "done": 1, "total": 1},
            status=db.ProjectStatus.COMPLETED,
        )
        before = client.get(f"/api/projects/{pid}").json()

        # 两条忙闲线都挂上，同一个现场下改选题必须 409、改名必须放行
        key = f"outline:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        projects_router._GEN_TASKS[pid] = _FakeTask()
        try:
            assert client.post(f"/api/projects/{pid}/topic",
                               json={**body, "topic": "换个选题"}).status_code == 409
            r = client.post(f"/api/projects/{pid}/title", json={"title": "生成中改名"})
            assert r.status_code == 200
            assert r.json()["display_title"] == "生成中改名"
        finally:
            projects_router.tasks._TASKS.pop(key, None)
            projects_router._GEN_TASKS.pop(pid, None)

        after = client.get(f"/api/projects/{pid}").json()
        assert after["title"] == "生成中改名"
        for col in ("status", "outline_json", "citation_binding_json", "sections_json",
                    "references_json", "generation_state_json", "materials_plan_json"):
            assert after[col] == before[col], f"改名不该动 {col}"


def test_progress_before_generation_is_idle(tmp_db):
    """未生成过的项目，进度接口不应报错，也不该带上正文。"""
    from app.main import app
    client = TestClient(app)

    pid = client.post("/api/projects", json={}).json()["id"]
    data = client.get(f"/api/projects/{pid}/generate/progress").json()
    assert data["status"] == "idle"
    assert data["done"] == 0
    assert data["error"] is None
    # 运行中不返回 sections，避免每 1.5 秒重复传输几千字
    assert "sections" not in data


def test_confirm_outline_invalidates_stale_binding(tmp_db):
    """改了章节标题后，旧引用绑定必须作废。

    绑定以章节标题为 join key，改标题后旧的键全部失配 —— 留着它，界面会显示一份
    指向已不存在章节的绑定表，而生成时每一节都查不到引用，安静产出零引用正文。
    **但「零引用」本身已不再是错误**（七类都可跳过引用调度），所以本用例只钉
    「失效」这件事，不再断言生成被拦。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        outline = _generate_outline(client, pid)
        client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})

        pdf = _make_pdf(["Doc content."])
        _upload_documents(client, pid,
                          files=[("files", ("d.pdf", pdf, "application/pdf"))])
        client.post(f"/api/projects/{pid}/citations/schedule")

        # 修改一个叶子节点的标题，再确认
        outline["chapters"][0]["sections"][0]["title"] = "1.1 改过的标题"
        r = client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        assert r.status_code == 200
        assert r.json()["binding_invalidated"] is True

        # 失效必须落库，而不只是写在响应里：刷新页面后不能又冒出旧绑定
        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["citation_binding_json"] is None

        # 再原样确认一次不该「复活」绑定 —— 它已经没了，不是被标记为过期
        r = client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        assert r.json()["binding_invalidated"] is False
        assert client.get(f"/api/projects/{pid}").json()["citation_binding_json"] is None


def test_confirm_outline_keeps_binding_when_unchanged(tmp_db):
    """大纲原样确认时不应误伤已确认的绑定。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        outline = _generate_outline(client, pid)
        client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})

        pdf = _make_pdf(["Doc content."])
        _upload_documents(client, pid,
                          files=[("files", ("d.pdf", pdf, "application/pdf"))])
        client.post(f"/api/projects/{pid}/citations/schedule")

        r = client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        assert r.json()["binding_invalidated"] is False
        assert client.get(f"/api/projects/{pid}").json()["citation_binding_json"] is not None


def test_confirm_outline_rejects_mismatched_budget_without_writing(tmp_db):
    """字数不匹配的确认必须拒绝，且一个字都不写。

    这是 D2 的后半段：confirm_outline 曾「先写库后判断」—— total != target 时也照样
    update_project（盖章大纲 + 推进状态 + 可能作废绑定），然后才在返回里说 matched=False。
    前端看到「字数总和不一致」就停在大纲步，库里却已经走下去。修复后判断提到写库之前，
    不匹配直接返回、不碰任何字段。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        outline = _generate_outline(client, pid)
        before = client.get(f"/api/projects/{pid}").json()
        assert before["status"] == "outline_pending"

        # 把所有叶子预算砍成 0，总和 0 != 1000，构造一次失配
        for ch in outline["chapters"]:
            for sec in ch["sections"]:
                if sec.get("subsections"):
                    for sub in sec["subsections"]:
                        sub["word_budget"] = 0
                else:
                    sec["word_budget"] = 0

        r = client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        assert r.status_code == 200
        assert r.json()["matched"] is False

        after = client.get(f"/api/projects/{pid}").json()
        assert after["status"] == before["status"], "被拒的确认不得推进状态"
        assert after["outline_json"] == before["outline_json"], "被拒的确认不得盖章大纲"
        assert after["citation_binding_json"] == before["citation_binding_json"]


def test_concurrent_generate_rejected(tmp_db):
    """同一项目并发生成应返回 409（生成任务的忙闲登记在 _GEN_TASKS）。"""
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        outline = _generate_outline(client, pid)
        client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        pdf = _make_pdf(["Doc content."])
        _upload_documents(client, pid,
                          files=[("files", ("d.pdf", pdf, "application/pdf"))])
        client.post(f"/api/projects/{pid}/citations/schedule")

        # 伪造一个「仍在跑」的任务登记项（未完成的 Task 对象）
        class _FakeTask:
            def done(self) -> bool:
                return False

        projects_router._GEN_TASKS[pid] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/generate")
            assert r.status_code == 409
            assert "生成正文" in r.json()["detail"]
        finally:
            projects_router._GEN_TASKS.pop(pid, None)


def test_generate_rejected_while_other_task_running(tmp_db):
    """别的后台任务在跑时不许开生成 —— 它收尾时会把 status 从 generating 顶掉。

    两套忙闲登记是分开的：_GEN_TASKS 管「正在生成」，tasks._TASKS 管其余通用任务
    （大纲 / 解析 / 材料分析 / 设计提炼 / 聚类 / 引用调度）。
    /generate 此前只查前者，于是「正在解析文献」与「正在生成正文」可以同时成立，而
    _run_parse 落库后会写 status=RESOURCES_LOADING —— 界面按 status 渲染的那一步当场
    跳回上游，生成任务却仍在写正文。
    """
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        outline = _generate_outline(client, pid)
        client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        pdf = _make_pdf(["Doc content."])
        _upload_documents(client, pid,
                          files=[("files", ("d.pdf", pdf, "application/pdf"))])
        client.post(f"/api/projects/{pid}/citations/schedule")

        class _FakeTask:
            def done(self) -> bool:
                return False

        key = f"outline:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/generate")
            assert r.status_code == 409
            assert "大纲正在生成中" in r.json()["detail"]
        finally:
            projects_router.tasks._TASKS.pop(key, None)

        # 任务结束后必须能正常点火：守卫不能变成一把锁死不放的锁
        assert client.post(f"/api/projects/{pid}/generate").status_code == 200


def test_upload_documents_rejected_while_generating(tmp_db):
    """正文生成中不许再传文献 —— /documents 只查了通用任务，看不见 _GEN_TASKS。

    _run_parse 落库后会写 status=RESOURCES_LOADING（新文献让旧聚类失效），这一笔会把
    generating 顶掉，而生成任务还在往 sections_json 里写正文：用户在「文献注入」步
    看着一篇正在成文的稿子。
    """
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]

        class _FakeTask:
            def done(self) -> bool:
                return False

        projects_router._GEN_TASKS[pid] = _FakeTask()
        try:
            r = client.post(
                f"/api/projects/{pid}/documents",
                files=[("files", ("a.pdf", _make_pdf(["content"]), "application/pdf"))],
            )
            assert r.status_code == 409
            assert "生成" in r.json()["detail"]
            # 被挡住时不能留下任何副作用：没写进度槽，也没有文献入库
            assert client.get(f"/api/projects/{pid}/documents").json() == []
            prog = client.get(f"/api/projects/{pid}/task/progress").json()["task"]
            assert prog["status"] == "idle", "被挡住就不该在进度槽里留痕"
        finally:
            projects_router._GEN_TASKS.pop(pid, None)

        # 生成结束后必须能正常上传
        r = client.post(
            f"/api/projects/{pid}/documents",
            files=[("files", ("a.pdf", _make_pdf(["content"]), "application/pdf"))],
        )
        assert r.status_code == 200


def test_delete_project_cascades_documents_and_materials(tmp_db):
    """删除项目要连带删掉它的文献与研究材料（靠外键级联）。

    否则删掉的项目会在库里留下孤儿行，下一批文献的 id 撞上它们时行为不可预料。
    """
    from app import db
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        pdf = _make_pdf(["Doc content."])
        _upload_documents(client, pid,
                          files=[("files", ("d.pdf", pdf, "application/pdf"))])
        client.post(f"/api/projects/{pid}/materials/text",
                    json={"text": "访谈记录一", "label": "访谈"})
        assert len(db.list_documents(pid)) == 1
        assert len(db.list_materials(pid)) == 1

        assert client.delete(f"/api/projects/{pid}").json() == {"ok": True}

        assert client.get(f"/api/projects/{pid}").status_code == 404
        assert db.list_documents(pid) == []
        assert db.list_materials(pid) == []
        assert pid not in [p["id"] for p in client.get("/api/projects").json()]


def test_delete_missing_project_404(tmp_db):
    """删一个不存在的项目要明说，不能假装成功。

    项目列表可能已经过期（另一个窗口删掉了它），静默返回 ok 会让用户以为删掉了。
    """
    from app.main import app

    with TestClient(app) as client:
        r = client.delete("/api/projects/nosuchprojectid")
        assert r.status_code == 404
        assert r.json()["detail"] == "项目不存在"


def test_delete_project_rejected_while_task_running(tmp_db):
    """有后台任务在跑时不许删项目，返回 409 而不是删掉。

    任务还在逐节往这个项目写进度与正文，删掉只会让后续写入变成静默 no-op，
    用户却以为任务仍在推进。
    """
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })

        class _FakeTask:
            def done(self) -> bool:
                return False

        key = f"outline:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        try:
            r = client.delete(f"/api/projects/{pid}")
            assert r.status_code == 409
            assert "正在生成" in r.json()["detail"]
        finally:
            projects_router.tasks._TASKS.pop(key, None)

        # 拒绝之后项目必须还在，任务可以继续
        assert client.get(f"/api/projects/{pid}").status_code == 200


def test_delete_project_rejected_while_generating(tmp_db):
    """分段生成中同样不许删（生成任务不在通用任务表里，得单独判）。"""
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]

        class _FakeTask:
            def done(self) -> bool:
                return False

        projects_router._GEN_TASKS[pid] = _FakeTask()
        try:
            r = client.delete(f"/api/projects/{pid}")
            assert r.status_code == 409
            assert "生成正文" in r.json()["detail"]
        finally:
            projects_router._GEN_TASKS.pop(pid, None)

        assert client.get(f"/api/projects/{pid}").status_code == 200


def test_task_progress_idle_before_any_task(tmp_db):
    """没跑过任务的项目，进度接口要给 idle 而不是报错。"""
    from app.main import app
    client = TestClient(app)

    pid = client.post("/api/projects", json={}).json()["id"]
    assert client.get(f"/api/projects/{pid}/task/progress").json()["task"]["status"] == "idle"


def test_outline_task_reports_phases(tmp_db):
    """大纲生成的进度要落库并带上阶段文案与起始时刻。

    前端靠 message 显示「正在做什么」、靠 started_at 算「已等待 N 秒」，
    刷新页面后也从这个时刻续上。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })

        r = client.post(f"/api/projects/{pid}/outline/generate")
        assert r.json()["status"] == "running"

        task = _wait_task(client, pid)
        assert task["status"] == "done", task
        assert task["kind"] == "outline"
        assert task["message"] == "大纲已生成"
        assert task["started_at"] and task["updated_at"]
        assert task["error"] is None

        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["status"] == "outline_pending"
        assert proj["outline_json"]


def test_outline_failure_is_persisted_and_reverts_status(tmp_db, monkeypatch):
    """生成失败必须落库并退回可重试状态。

    桌面版 console=False，异常不落库就等于用户只看到「一直在转」，无从排查。
    """
    from app.main import app
    from app.routers import projects as projects_router

    async def boom(*args, **kwargs):
        raise RuntimeError("模型服务不可用")

    monkeypatch.setattr(projects_router.outline_agent, "generate_outline", boom)

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        assert client.post(f"/api/projects/{pid}/outline/generate").status_code == 200

        task = _wait_task(client, pid)
        assert task["status"] == "error", task
        assert "模型服务不可用" in task["error"]

        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["status"] == "topic_set", "退回可重试的状态"
        assert proj["outline_json"] is None
        # 选题仍在，用户改一下配置就能再来一次
        assert proj["topic"] == "t"


def test_concurrent_outline_generate_rejected(tmp_db):
    """大纲生成中再点一次应返回 409，而不是叠一次几十秒的重复调用。"""
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })

        class _FakeTask:
            def done(self) -> bool:
                return False

        key = f"outline:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/outline/generate")
            assert r.status_code == 409
            assert "正在生成" in r.json()["detail"]
        finally:
            projects_router.tasks._TASKS.pop(key, None)


def test_upload_cleans_placeholder_metadata(tmp_db, monkeypatch):
    """提取器返回占位词时，落库必须已经是空串。

    打的是提取器这一层（而不是 parse_agent 内部）：projects 的落库处是**所有**文献
    进库的必经之处，而提取器是可替换、可打桩的，边界不该替它担保输出干净。
    占位词一旦落库就会印进参考文献列表，且前端文献列表直接读库 —— 那时候再清就晚了。
    """
    from app.main import app
    from app.routers import projects as projects_router
    from app.routers import projects_documents as documents_router

    async def placeholder_meta(first_text, filename):
        return {
            "title": filename, "authors": "未提及", "year": "Not specified",
            "source": "未知", "summary": "未提及",
        }

    monkeypatch.setattr(documents_router, "extract_metadata", placeholder_meta)

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        assert client.post(
            f"/api/projects/{pid}/documents",
            files=[("files", ("a.pdf", _make_pdf(["Some content."]), "application/pdf"))],
        ).status_code == 200
        task = _wait_task(client, pid)
        assert task["status"] == "done", task

        docs = client.get(f"/api/projects/{pid}/documents").json()
        assert len(docs) == 1
        assert (docs[0]["authors"], docs[0]["year"], docs[0]["source"]) == ("", "", "")
        # 只清这三个字段：题名与摘要各归各自的规则管
        assert docs[0]["title"]
        assert docs[0]["summary"] == "未提及"


def test_parse_failure_keeps_already_parsed_documents(tmp_db, monkeypatch):
    """单篇元数据提取失败，既不丢已完成的部分，也不中止整批。

    两条既存缺陷的回归测试，合起来才成立：
    1) 此前 N 篇 PDF 全部解析完才写库，一旦超时或断连，整批成果全部丢失 ——
       改成逐篇入库后，中途断掉也不白干；
    2) 此前 `extract_metadata` **没有**自己的 try，它抛异常会冲出 for 循环把整批
       中止（第 2 篇失败 → 第 3 篇起全部丢弃，状态报 error）。那颗雷原本被
       `parse_agent` 里吞掉一切异常的裸 except 盖着，现在两头都拆了：失败在逐篇的
       try 里被接住、计入 failed，批次照常跑完并如实报 `已解析 1/2 篇，1 篇未解析成功`。
       —— 所以这里的 `status` 是 `done` 而不是 `error`，`done` 是**总数**而非成功数，
       成功数在 `saved` 里；`error` 从此只留给「整批都没跑起来」那种失败。
    """
    from app.main import app
    from app.routers import projects as projects_router
    from app.routers import projects_documents as documents_router

    calls = {"n": 0}

    async def flaky(first_text, filename):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("解析服务不可用")
        return {"title": filename, "authors": "", "year": "", "source": "", "summary": ""}

    monkeypatch.setattr(documents_router, "extract_metadata", flaky)

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        a = _make_pdf(["First doc content."])
        b = _make_pdf(["Second doc content."])
        assert client.post(
            f"/api/projects/{pid}/documents",
            files=[
                ("files", ("a.pdf", a, "application/pdf")),
                ("files", ("b.pdf", b, "application/pdf")),
            ],
        ).status_code == 200

        task = _wait_task(client, pid)
        assert task["status"] == "done", task
        assert task["done"] == 2, "批次跑完了，进度就该走到底"
        assert task["saved"] == 1
        assert [f["filename"] for f in task["failed"]] == ["b.pdf"]
        assert "元数据提取失败" in task["failed"][0]["reason"]

        docs = client.get(f"/api/projects/{pid}/documents").json()
        assert [d["filename"] for d in docs] == ["a.pdf"], "成功的那篇必须留在库里"


def test_concurrent_document_parse_rejected(tmp_db):
    """解析中再传一批应返回 409，避免两批文献交叉写进度。"""
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]

        class _FakeTask:
            def done(self) -> bool:
                return False

        key = f"documents:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        try:
            pdf = _make_pdf(["Content."])
            r = client.post(
                f"/api/projects/{pid}/documents",
                files=[("files", ("d.pdf", pdf, "application/pdf"))],
            )
            assert r.status_code == 409
            assert "解析中" in r.json()["detail"]
        finally:
            projects_router.tasks._TASKS.pop(key, None)


def test_outline_and_parse_are_mutually_exclusive(tmp_db):
    """两种任务共用一列 task_state_json，不能同时跑 —— 否则进度互相覆盖。

    界面已经把按钮都禁掉了，但 API 也得挡住：否则前端的「正在做什么」会在
    两条文案之间来回跳。
    """
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })

        class _FakeTask:
            def done(self) -> bool:
                return False

        # 解析在跑时不许生成大纲
        key = f"documents:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/outline/generate")
            assert r.status_code == 409
            assert "解析中" in r.json()["detail"]
        finally:
            projects_router.tasks._TASKS.pop(key, None)

        # 反向：大纲在跑时不许上传
        key = f"outline:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        try:
            pdf = _make_pdf(["Content."])
            r = client.post(
                f"/api/projects/{pid}/documents",
                files=[("files", ("d.pdf", pdf, "application/pdf"))],
            )
            assert r.status_code == 409
            assert "正在生成" in r.json()["detail"]
        finally:
            projects_router.tasks._TASKS.pop(key, None)


def test_review_outline_is_thematic(tmp_db):
    """文献综述的大纲不得出现实验设计类章节。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "文献综述", "target_words": 6000, "topic": "t",
        })
        pdf = _make_pdf(["Doc content."])
        _upload_documents(client, pid,
                          files=[("files", ("d.pdf", pdf, "application/pdf"))])

        outline = _generate_outline(client, pid)
    titles = " ".join(
        ch["title"] for ch in outline["chapters"]
    ) + " " + " ".join(
        s["title"] for ch in outline["chapters"] for s in ch["sections"]
    )
    # 主题式结构（章节名）+ 无任何实证色彩章节
    assert "研究主题脉络" in titles
    assert "研究述评" in titles
    for banned in ("研究假设", "假设检验", "变量与数据", "描述性统计", "研究设计"):
        assert banned not in titles


# ---------------------------------------------------------------
# 删除文献
# ---------------------------------------------------------------
def _doc_project(client) -> str:
    """建一个「大纲已确认 + 已上传一篇文献」的项目。"""
    pid = client.post("/api/projects", json={}).json()["id"]
    client.post(f"/api/projects/{pid}/topic", json={
        "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
    })
    outline = _generate_outline(client, pid)
    client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
    pdf = _make_pdf(["Doc content."])
    _upload_documents(client, pid, files=[("files", ("d.pdf", pdf, "application/pdf"))])
    return pid


def test_delete_document_removes_it(tmp_db):
    """删除文献后列表里不再有它。"""
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        doc_id = client.get(f"/api/projects/{pid}/documents").json()[0]["id"]

        r = client.delete(f"/api/projects/{pid}/documents/{doc_id}")
        assert r.status_code == 200
        # 两个「作废」标志都随响应返回：前端据此决定要不要清掉本地的绑定/聚类视图
        assert r.json() == {
            "ok": True, "binding_invalidated": False, "clusters_invalidated": False,
        }
        assert client.get(f"/api/projects/{pid}/documents").json() == []


def test_delete_document_missing_returns_404(tmp_db):
    """项目在、文献不在 —— 404。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        assert client.delete(f"/api/projects/{pid}/documents/不存在").status_code == 404


def test_delete_document_of_another_project_is_404(tmp_db):
    """拿 Y 的项目 id 去删 X 的文献，必须被拒 —— 而且 X 那篇要原样还在。

    这是 v1.19 修的那条数据丢失路径的后端一半：前端那份文献列表可能是**上一个
    项目**的（换项目时那次拉取有条件），用户在 Y 的界面上点「删除」，请求里的
    doc id 属于 X。原先路由是平铺的 `/documents/{doc_id}`，后端只能从文献行反推
    项目，于是它老老实实删掉了 X 的文献并把 X 的引用绑定整体作废 —— 用户全程
    没打开过 X。现在路径上必须带项目 id，对不上按「文献不存在」返回。
    """
    from app.main import app

    with TestClient(app) as client:
        victim = _doc_project(client)
        doc_id = client.get(f"/api/projects/{victim}/documents").json()[0]["id"]
        other = client.post("/api/projects", json={}).json()["id"]

        r = client.delete(f"/api/projects/{other}/documents/{doc_id}")
        assert r.status_code == 404
        # 关键的一条：被拒的请求不留任何痕 —— 那篇文献还在它自己的项目里
        assert [d["id"] for d in client.get(f"/api/projects/{victim}/documents").json()] == [doc_id]

        # 同一个 doc id 走它真正的项目，照常删得掉（守卫拦的是错配，不是这个动作）
        assert client.delete(f"/api/projects/{victim}/documents/{doc_id}").status_code == 200
        assert client.get(f"/api/projects/{victim}/documents").json() == []


def test_document_delete_route_is_project_scoped(tmp_db):
    """删除文献这个路由必须**带项目 id**，平铺的旧形状不许复活。

    上面那条用例守的是行为，这条守的是形状：只要还有一个「只报 doc id、项目由
    后端反推」的入口在，同一个数据丢失路径就是可达的 —— 换成别的调用方来走它
    而已。路由表是唯一能一次看全这件事的地方。
    """
    from app.main import app

    paths = {r.path for r in app.routes if hasattr(r, "path")}
    assert "/api/projects/{project_id}/documents/{doc_id}" in paths
    assert "/api/documents/{doc_id}" not in paths


def test_delete_unreferenced_document_keeps_binding(tmp_db):
    """删一篇没被引用的文献，不应连累已排好的绑定。"""
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        docs = client.get(f"/api/projects/{pid}/documents").json()
        binding = client.post(f"/api/projects/{pid}/citations/schedule").json()["binding"]
        # 只把第一篇排进绑定，便于观察
        client.post(f"/api/projects/{pid}/citations/confirm",
                    json={"binding": binding, "cite_style": "bracket"})

        r = client.delete(f"/api/projects/{pid}/documents/{docs[0]['id']}")
        assert r.json()["binding_invalidated"] is True, "它确实在绑定里"
        assert client.get(f"/api/projects/{pid}").json()["citation_binding_json"] is None


def test_delete_bound_document_invalidates_binding(tmp_db):
    """删掉被引用的文献必须作废绑定 —— 否则生成时会拿不到原文片段去编。"""
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        # 再加一篇，这样删掉一篇后另一篇仍能构成一次有效调度
        pdf = _make_pdf(["Another doc."])
        _upload_documents(client, pid, files=[("files", ("e.pdf", pdf, "application/pdf"))])
        docs = client.get(f"/api/projects/{pid}/documents").json()
        client.post(f"/api/projects/{pid}/citations/schedule")

        proj = client.get(f"/api/projects/{pid}").json()
        bound_ids = {
            r["doc_id"]
            for entries in proj["citation_binding_json"].values()
            for r in entries
        }
        target = next(d["id"] for d in docs if d["id"] in bound_ids)

        r = client.delete(f"/api/projects/{pid}/documents/{target}")
        assert r.json()["binding_invalidated"] is True

        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["citation_binding_json"] is None, "悬空 doc_id 必须清掉"
        assert proj["status"] == "resources_loading", "退回待调度状态"
        # 生成不再拦：绑定没了就是「本文不引用」，而不是「本文引用了一批取不到原文的
        # 文献」。要拦的那种坏结果（正文挂着 [1] 而文末列表是空的）由绑定清空本身避免。


def test_delete_document_rejected_while_generating(tmp_db):
    """生成中删文献会让本轮引用的原文对不上，直接拒绝。"""
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = _doc_project(client)
        doc_id = client.get(f"/api/projects/{pid}/documents").json()[0]["id"]

        class _FakeTask:
            def done(self) -> bool:
                return False

        projects_router._GEN_TASKS[pid] = _FakeTask()
        try:
            r = client.delete(f"/api/projects/{pid}/documents/{doc_id}")
            assert r.status_code == 409
            assert "生成" in r.json()["detail"]
        finally:
            projects_router._GEN_TASKS.pop(pid, None)
        assert len(client.get(f"/api/projects/{pid}/documents").json()) == 1


# ---------------------------------------------------------------
# 编辑文献元数据
# ---------------------------------------------------------------
def _patch_doc(client, pid, doc_id, **fields):
    return client.patch(f"/api/projects/{pid}/documents/{doc_id}", json=fields)


def _first_doc(client, pid) -> dict:
    return client.get(f"/api/projects/{pid}/documents").json()[0]


def _binding_entry(client, pid, doc_id) -> dict:
    """从绑定里挑出指向某一篇的条目。"""
    binding = client.get(f"/api/projects/{pid}").json()["citation_binding_json"]
    return next(e for es in binding.values() for e in es if e["doc_id"] == doc_id)


_FULL_META = {
    "title": "人工智能在教育中的应用",
    "title_en": "Application of Artificial Intelligence in Education",
    "authors": "John K. Smith, Anna Doe",
    "year": "2023",
    "source": "教育研究",
    "volume": "32",
    "issue": "1",
    "page_range": "56-75",
    "source_type": "J",
    # 非期刊类型的三项（本轮新增）。这里填的是**专著**的形态，而同一份 _FULL_META 另外
    # 还带着期刊那四项与 `source_type="J"` —— 这是刻意的不一致：它验的是「接口能收能回」，
    # 不是「渲染出来是什么样」（那是 citation_format 那组用例的活）。**不要**为了让这里
    # 「看起来合理」而把 source_type 改成 M：那会让下面那条「只改一个字段」的用例
    # 顺带验到一个模板形态，反而把两件事混在一起。
    "place": "北京",
    "edition": "第3版",
    "publish_date": "2023-05-04",
}


def test_patch_document_updates_all_twelve_fields(tmp_db):
    """十二项元数据都能改，改完 GET 回来是新值 —— 这是「抽错能改」这条路的入口。"""
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        doc_id = _first_doc(client, pid)["id"]

        r = _patch_doc(client, pid, doc_id, **_FULL_META)
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        doc = r.json()["document"]
        assert (doc["title"], doc["title_en"], doc["authors"], doc["year"],
                doc["source"]) == (
            _FULL_META["title"], _FULL_META["title_en"], _FULL_META["authors"],
            _FULL_META["year"], _FULL_META["source"],
        )
        assert (doc["volume"], doc["issue"], doc["page_range"], doc["source_type"]) == (
            "32", "1", "56-75", "J",
        )
        assert (doc["place"], doc["edition"], doc["publish_date"]) == (
            "北京", "第3版", "2023-05-04",
        )
        # 列表接口读到的也是新值（响应里那份不是只给自己看的回显）
        listed = _first_doc(client, pid)
        assert listed["title"] == _FULL_META["title"]
        # 文件名与判重指纹这两样**不在可编辑范围**里
        assert listed["filename"] == "d.pdf"
        assert listed["content_hash"] == doc["content_hash"]


def test_patch_document_only_touches_the_fields_it_was_given(tmp_db):
    """只发一个字段时，另外十一个一个字都不能动。

    这是 DocumentPatch「None = 不修改」那套语义的验收点。用覆盖语义的话，前端少发
    一个字段就等于把它清空 —— 用户只想改作者，点一次保存却把题名、年份、来源、卷期
    页一起抹了，而题名被抹还会连带把绑定里那份换成文件名。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        doc_id = _first_doc(client, pid)["id"]
        assert _patch_doc(client, pid, doc_id, **_FULL_META).status_code == 200

        r = _patch_doc(client, pid, doc_id, authors="张三")
        assert r.status_code == 200, r.text
        doc = r.json()["document"]
        assert doc["authors"] == "张三"
        for key, want in _FULL_META.items():
            if key != "authors":
                assert doc[key] == want, f"{key} 被顺手改了"

        # 一个字段都不带：不写库、也不谎报改了什么
        before = _first_doc(client, pid)
        assert _patch_doc(client, pid, doc_id).status_code == 200
        assert _first_doc(client, pid) == before


def test_patch_document_empty_string_clears_a_field(tmp_db):
    """空串是「清空」这个动作本身，与「不修改」必须分得开。

    `title_en` 在这里要一并钉住：它是一个**独立可清空**的字段（原题名已是英文的
    文献，模型抽出的英译是空串，用户把它改错之后得能改回来）。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        doc_id = _first_doc(client, pid)["id"]
        assert _patch_doc(client, pid, doc_id, **_FULL_META).status_code == 200

        doc = _patch_doc(
            client, pid, doc_id, authors="", page_range="", title_en=""
        ).json()["document"]
        assert doc["authors"] == "" and doc["page_range"] == ""
        assert doc["title_en"] == "", "英译题名必须能被清空"
        # 同一个请求里没提到的字段照旧
        assert doc["year"] == "2023" and doc["volume"] == "32"
        assert doc["title"] == _FULL_META["title"], "清空的是英译，不是原题名"
        assert _first_doc(client, pid)["authors"] == ""


def test_patch_document_rejects_out_of_range_values(tmp_db):
    """越界走**路由内 400**，不是 pydantic 的 422（前端会把 detail 数组渲染成
    [object Object]，用户看到的是一句乱码而不是原因），且被拒的请求不留痕。"""
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        doc_id = _first_doc(client, pid)["id"]

        r = _patch_doc(client, pid, doc_id, title="长" * 301)
        assert r.status_code == 400
        assert "题名" in r.json()["detail"] and "300" in r.json()["detail"]
        # 边界：正好 300 字要放行（上限是「最多多少」，不是「少于多少」）
        assert _patch_doc(client, pid, doc_id, title="长" * 300).status_code == 200

        # 类型标识不是自由文本：越界要拦，且要说清合法取值
        r = _patch_doc(client, pid, doc_id, source_type="期刊")
        assert r.status_code == 400
        assert "J/M/D" in r.json()["detail"]
        # 留空是合法的（= 没抽到类型，渲染侧回落 J）
        assert _patch_doc(client, pid, doc_id, source_type="").status_code == 200

        # 英译题名与题名同一条口径（它同样进提示词与文末列表）
        r = _patch_doc(client, pid, doc_id, title_en="x" * 301)
        assert r.status_code == 400
        assert "英译题名" in r.json()["detail"] and "300" in r.json()["detail"]

        # 非期刊类型的三项（本轮新增）各有自己的上限。**上限是逐字段写在
        # DOC_FIELD_LIMITS 里的**，所以这里要挑一个与 300 不同的数字，验的是
        # 「读的是这一项自己的上限」，不是「所有字段都按 300 拦」。
        r = _patch_doc(client, pid, doc_id, place="长" * 101)
        assert r.status_code == 400
        assert "出版地" in r.json()["detail"] and "100" in r.json()["detail"]
        assert _patch_doc(client, pid, doc_id, place="长" * 100).status_code == 200

        # 被拒的这几次都没写进去
        doc = _first_doc(client, pid)
        assert doc["title"] == "长" * 300
        assert doc["source_type"] == ""
        assert doc["title_en"] == ""
        assert doc["place"] == "长" * 100


def test_patch_document_normalizes_the_type_letter_case(tmp_db):
    """`m` 存成 `M`。

    这里与抽取侧刻意相反（那边是「库里原样存模型说的」，好让用户看见模型写了什么）：
    这个值来自一个白名单下拉框，`m` 与 `M` 是同一个字母的两种写法，而存成小写会让
    下拉框匹配不上任何选项 —— 界面上表现为「没有选中项」，用户再点一次保存就把它清空。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        doc_id = _first_doc(client, pid)["id"]
        assert _patch_doc(client, pid, doc_id, source_type="m").json()["document"][
            "source_type"
        ] == "M"


def test_patch_document_missing_or_other_project_is_404(tmp_db):
    """项目在、文献不在 → 404；拿别的项目的 doc id → 也是 404，且那篇原样还在。

    与删除同一条理由（见 test_delete_document_of_another_project_is_404）：前端那份
    列表可能属于上一个项目，只拿得到 doc id。
    """
    from app.main import app

    with TestClient(app) as client:
        victim = _doc_project(client)
        doc_id = _first_doc(client, victim)["id"]
        other = client.post("/api/projects", json={}).json()["id"]

        assert _patch_doc(client, other, doc_id, title="改").status_code == 404
        assert _patch_doc(client, victim, "不存在", title="改").status_code == 404
        # 关键：被拒的请求不留痕
        assert _first_doc(client, victim)["title"] == "d"


def test_patch_document_rejected_while_generating(tmp_db):
    """生成中不让改元数据：本轮引用的原文与元数据都可能对不上。"""
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = _doc_project(client)
        doc_id = _first_doc(client, pid)["id"]

        class _FakeTask:
            def done(self) -> bool:
                return False

        projects_router._GEN_TASKS[pid] = _FakeTask()
        try:
            r = _patch_doc(client, pid, doc_id, title="改")
            assert r.status_code == 409
            assert "生成" in r.json()["detail"]
        finally:
            projects_router._GEN_TASKS.pop(pid, None)
        assert _first_doc(client, pid)["title"] == "d"


def test_patch_title_refreshes_the_binding_copy(tmp_db):
    """题名改完，绑定里那份冻结的 doc_title 必须跟着换，编号与顺序一个字不动。

    `_plan_citations` 取的是**绑定里**那份 doc_title（不是 documents.title），所以
    不刷新的话改题名产生不了任何差异 —— 见下一条用例。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        doc_id = _first_doc(client, pid)["id"]
        client.post(f"/api/projects/{pid}/citations/schedule")
        before = _binding_entry(client, pid, doc_id)
        assert before["doc_title"] == "d"

        r = _patch_doc(client, pid, doc_id, title="新题名")
        assert r.json()["binding_title_refreshed"] is True

        after = _binding_entry(client, pid, doc_id)
        assert after["doc_title"] == "新题名"
        # 除题名外逐字段相同：page 不动、编号与章节归属都没动
        assert {k: v for k, v in after.items() if k != "doc_title"} == {
            k: v for k, v in before.items() if k != "doc_title"
        }
        # 只改别的字段时不去碰绑定
        assert _patch_doc(client, pid, doc_id, year="2024").json()[
            "binding_title_refreshed"
        ] is False


def test_metadata_edit_marks_the_reference_list_stale_as_render(tmp_db):
    """改元数据 → 判为 render（编号与指向没变，只有渲染文本变了）→ 现成的重排修好它。

    这是「编辑界面」与既有提示链的接缝：元数据一变，目标快照与已落盘的 references_json
    就不再相等。**判据本身不用改** —— _refs_diff 只看 (编号, doc_id) 那一串是不是逐位
    相同，而改作者/年份/来源/卷期页动不了它。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        doc_id = _first_doc(client, pid)["id"]
        proj = _generated(client, pid)
        assert proj["refs_change"]["kind"] == "none", "刚生成完，什么都对得上"

        assert _patch_doc(
            client, pid, doc_id,
            authors="张三", year="2023", source="教育研究",
            volume="32", issue="1", page_range="56-75",
        ).status_code == 200

        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["citations_stale"] is True
        assert proj["refs_change"]["kind"] == "render"
        # 文案是**并列穷举**：服务端判断不出是改格式还是改元数据，就不猜是哪一种
        assert "元数据" in proj["refs_change"]["summary"]
        assert proj["refs_change"]["added_count"] == 0
        assert proj["refs_change"]["removed_count"] == 0
        body_before = proj["sections_json"]

        assert client.post(f"/api/projects/{pid}/references/rerender").status_code == 200
        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["citations_stale"] is False
        assert proj["sections_json"] == body_before, "重排只动文末列表，正文一个字都不变"
        formatted = proj["references_json"][0]["formatted"]
        assert "张三" in formatted
        assert "32(1): 56-75" in formatted, formatted


def test_title_edit_is_not_a_no_op(tmp_db):
    """**改题名不能是静默无操作** —— 绑定里那份 doc_title 的验收点。

    绑定条目里冻着一份生成时的题名，而 _plan_citations 取的是它。不刷新绑定的话，
    改完题名目标快照与已落盘的 references_json **完全相等**：refs_change 判 none、
    citations_stale 恒假、界面一声不吭（不告警、不提示、重排按钮也不会亮）。用户会
    以为保存失败并反复重试 —— 这正是 v1.13/v1.14 那一类缺陷的形态。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        doc_id = _first_doc(client, pid)["id"]
        proj = _generated(client, pid)
        assert proj["references_json"][0]["doc_title"] == "d"

        assert _patch_doc(client, pid, doc_id, title="改过的题名").status_code == 200

        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["citations_stale"] is True, "改题名必须是能被看见的一次改动"
        assert proj["refs_change"]["kind"] == "render"
        # 正文里的角标与指向一个字都没变，所以不需要重写正文
        assert proj["refs_change"]["added_count"] == 0
        assert proj["refs_change"]["removed_count"] == 0

        assert client.post(f"/api/projects/{pid}/references/rerender").status_code == 200
        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["citations_stale"] is False
        formatted = proj["references_json"][0]["formatted"]
        assert "[1] 改过的题名[J]" in formatted, formatted
        assert ". d[J]" not in formatted, f"绑定里那份旧题名还在：{formatted}"

        # 反证「绑定那份才是判据」：绕过路由直接改库里的 title，文末列表**纹丝不动**。
        # 这就是为什么题名必须显式去刷新绑定 —— 只改 documents.title 产生不了任何差异，
        # 而界面只认差异（citations_stale）。上面那次编辑之所以能被看见，全靠这一步。
        tmp_db.update_document_meta(doc_id, {"title": "绕过路由改的题名"})
        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["citations_stale"] is False, "文末列表读的是绑定里那份，不是 documents.title"
        assert proj["references_json"][0]["formatted"] == formatted


# ---------------------------------------------------------------
# 作者自有研究材料
# ---------------------------------------------------------------
def test_material_text_roundtrip_and_delete(tmp_db):
    """粘贴一段材料 → 出现在列表里（且不带正文）→ 可删除。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]

        r = client.post(f"/api/projects/{pid}/materials/text", json={
            "label": "研究思路", "text": "本文的核心论点是……",
        })
        assert r.status_code == 200
        mats = r.json()
        assert len(mats) == 1
        assert mats[0]["label"] == "研究思路"
        assert mats[0]["char_count"] == len("本文的核心论点是……")
        # 列表接口不回传正文：材料动辄十几万字，每轮询一次传一遍毫无意义
        assert "text" not in mats[0]

        assert client.delete(
            f"/api/projects/{pid}/materials/{mats[0]['id']}").status_code == 200
        assert client.get(f"/api/projects/{pid}/materials").json() == []


def test_material_text_rejects_empty(tmp_db):
    from app.main import app
    client = TestClient(app)
    pid = client.post("/api/projects", json={}).json()["id"]
    r = client.post(f"/api/projects/{pid}/materials/text", json={"text": "   "})
    assert r.status_code == 400


def test_material_file_upload_tolerates_failures(tmp_db):
    """单份材料失败不影响其他材料，并给出原因。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        r = client.post(
            f"/api/projects/{pid}/materials/files",
            files=[
                ("files", ("旧数据.doc", b"\xd0\xcf\x11\xe0", "application/msword")),
                ("files", ("数据.csv", "样本量,312".encode("gb18030"), "text/csv")),
            ],
        )
        assert r.status_code == 200
        body = r.json()
        assert body["saved"] == 1
        assert len(body["failed"]) == 1
        assert ".doc" in body["failed"][0]["reason"]
        assert [m["label"] for m in body["materials"]] == ["数据.csv"]


def test_material_delete_missing_returns_404(tmp_db):
    from app.main import app
    client = TestClient(app)
    pid = client.post("/api/projects", json={}).json()["id"]
    assert client.delete(f"/api/projects/{pid}/materials/不存在").status_code == 404


def test_delete_material_of_another_project_is_404(tmp_db):
    """拿 Y 的项目 id 去删 X 的材料，必须被拒 —— 而且 X 那份要原样还在。

    这是 D4 那条数据丢失路径：材料删除曾是唯一只凭 id 就能跨项目删除的破坏性
    入口（文献那边 v1.19 已加守卫，材料漏了）。路径上必须带项目 id，对不上按
    「材料不存在」返回。
    """
    from app.main import app

    with TestClient(app) as client:
        victim = client.post("/api/projects", json={}).json()["id"]
        mats = client.post(f"/api/projects/{victim}/materials/text",
                           json={"text": "X 的材料"}).json()
        other = client.post("/api/projects", json={}).json()["id"]

        r = client.delete(f"/api/projects/{other}/materials/{mats[0]['id']}")
        assert r.status_code == 404
        # 被拒的请求不留任何痕 —— 那份材料还在它自己的项目里
        assert [m["id"] for m in client.get(
            f"/api/projects/{victim}/materials").json()] == [mats[0]["id"]]


def test_analyze_material_requires_outline_and_materials(tmp_db):
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        # 没有大纲
        assert client.post(f"/api/projects/{pid}/materials/analyze").status_code == 400

        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        _generate_outline(client, pid)
        # 有大纲但没有材料
        r = client.post(f"/api/projects/{pid}/materials/analyze")
        assert r.status_code == 400
        assert "材料" in r.json()["detail"]


def test_analyze_material_requires_llm(tmp_db):
    """未配置 LLM 时应明确报错，而不是转一圈回来给一份空计划。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        _generate_outline(client, pid)
        client.post(f"/api/projects/{pid}/materials/text", json={"text": "我的数据"})

        r = client.post(f"/api/projects/{pid}/materials/analyze")
        assert r.status_code == 400
        assert "LLM" in r.json()["detail"]


def _capture_llm(monkeypatch, payload):
    """在**项目与大纲都建好之后**才打开 LLM，打桩 chat_json，返回收到的提示词列表。

    顺序很要紧：先开 LLM 会让大纲生成真的发起网络请求（测试环境的 base_url 是
    假的），断言就会莫名其妙地失败在别处。
    """
    from app.config import settings
    from app.llm import llm

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "http://test.invalid")
    llm.reload()
    calls: list[str] = []

    async def fake(messages, **kw):
        calls.append(messages[0]["content"])
        return payload

    monkeypatch.setattr(llm, "chat_json", fake)
    return calls


def _enable_llm(monkeypatch, payload):
    """只关心「LLM 可用」，不关心它收到了什么。"""
    _capture_llm(monkeypatch, payload)


def _disable_llm(monkeypatch):
    """把 LLM 关回去，让之后的步骤重回确定性地路径。

    打桩的 chat_json 对**每一次**调用都返回同一份 payload：材料分析之后再往下走
    （引用调度、生成正文）时，那份 payload 里既没有 assignments 也没有 sections，
    两个环节都会判为「这次判断/输出不可用」，生成直接落到 status=error。测试环境的
    base_url 是假的，所以不能靠真发一次请求来关掉它 —— 只能显式关。
    """
    from app.config import settings
    from app.llm import llm

    monkeypatch.setattr(settings, "llm_api_key", "")
    llm.reload()


def _material_project(client) -> tuple[str, str]:
    """建一个「大纲已确认 + 有一段研究材料」的项目，返回 (pid, material_id)。"""
    pid = client.post("/api/projects", json={}).json()["id"]
    client.post(f"/api/projects/{pid}/topic", json={
        "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
    })
    _generate_outline(client, pid)
    mats = client.post(f"/api/projects/{pid}/materials/text",
                       json={"text": "样本 312 份"}).json()
    return pid, mats[0]["id"]


def test_analyze_material_stores_plan(tmp_db, monkeypatch):
    """分析成功后计划落库，任务状态为 done。"""
    from app.main import app

    with TestClient(app) as client:
        pid, mid = _material_project(client)
        # 节标题取自**实际生成的大纲**，不写死字符串：分析计划以章节标题为键，
        # 写死一个骨架里的字面量就等于把这条用例钉在了某一份骨架上（骨架一改，
        # 模型返回的放置项会被全部判为「对不上章节」而丢弃，报错还看不出来）。
        title = client.get(f"/api/projects/{pid}").json()[
            "outline_json"
        ]["chapters"][0]["sections"][0]["title"]
        _enable_llm(monkeypatch, {
            "notes": "数据放正文",
            "placements": [
                {"section_title": title, "material_ids": [mid],
                 "usage": "用样本量说明现状", "key_points": ["样本 312 份"]},
            ],
        })

        assert client.post(f"/api/projects/{pid}/materials/analyze").status_code == 200
        task = _wait_task(client, pid)
        assert task["status"] == "done", task
        assert task["kind"] == "material_analysis"

        plan = client.get(f"/api/projects/{pid}").json()["materials_plan_json"]
        assert plan["notes"] == "数据放正文"
        assert plan["placements"][0]["section_title"] == title
        assert plan["unused"] == []


def _stub_analysis(monkeypatch, pid, mid, title, side_effect):
    """打开 LLM 并把 chat_json 打桩成「先做点别的，再返回一份合法计划」。

    `side_effect` 模拟分析跑着的时候用户干的那件事 —— 那正是一次 LLM 调用期间
    事件循环能插进来的东西。计划本身是合法的（章节标题与材料 id 都取自开跑前的
    快照），所以它能不能落库，只取决于 `_analysis_targets_changed` 有没有发现前提变了。
    """
    from app.config import settings
    from app.llm import llm

    monkeypatch.setattr(settings, "llm_api_key", "test-key")
    monkeypatch.setattr(settings, "llm_base_url", "http://test.invalid")
    llm.reload()

    async def fake(messages, **kw):
        side_effect()
        return {
            "placements": [{"section_title": title, "material_ids": [mid],
                            "usage": "用样本量说明现状", "key_points": []}],
            "unused": [],
        }

    monkeypatch.setattr(llm, "chat_json", fake)


def test_material_plan_discarded_when_material_deleted_mid_analysis(tmp_db, monkeypatch):
    """分析期间删掉材料：结果必须丢弃，不能把刚作废的计划又立起来。

    _run_analyze 手里的 materials 是开跑时的快照，而删材料会把计划当场作废
    （_invalidate_material_plan）。若照写不误，界面显示「已分析 · 已把 N 份材料归入
    M 个章节」，其中那个 material_id 已经不存在，生成时 _materials_by_section 只能
    把它悄悄跳过 —— 一份「已分析」的计划配一份没有它的正文。
    """
    from app import db
    from app.main import app

    with TestClient(app) as client:
        pid, mid = _material_project(client)
        title = client.get(f"/api/projects/{pid}").json()[
            "outline_json"
        ]["chapters"][0]["sections"][0]["title"]
        _stub_analysis(monkeypatch, pid, mid, title, lambda: db.delete_material(mid))

        assert client.post(f"/api/projects/{pid}/materials/analyze").status_code == 200
        task = _wait_task(client, pid)
        assert task["status"] == "error", task
        assert "作废" in task["message"]
        assert "丢弃" in task["error"]
        assert client.get(f"/api/projects/{pid}").json()["materials_plan_json"] is None


def test_material_plan_discarded_when_outline_title_changed_mid_analysis(tmp_db, monkeypatch):
    """同上，另一半前提：分析期间大纲的章节标题被改掉。

    placements 以章节标题为键，标题一改就再也匹配不回任何章节（生成时按标题取用）。
    这与 confirm_outline 里那道 plan_stale 判据是同一件事，只是这次的改动发生在
    一次 LLM 调用的中间，_run_analyze 收尾时才知道。
    """
    from app import db
    from app.main import app

    with TestClient(app) as client:
        pid, mid = _material_project(client)
        title = client.get(f"/api/projects/{pid}").json()[
            "outline_json"
        ]["chapters"][0]["sections"][0]["title"]

        def rename_section():
            outline = db.get_project(pid)["outline_json"]
            outline["chapters"][0]["sections"][0]["title"] = "分析期间改掉的新标题"
            db.update_project(pid, outline_json=outline)

        _stub_analysis(monkeypatch, pid, mid, title, rename_section)

        assert client.post(f"/api/projects/{pid}/materials/analyze").status_code == 200
        task = _wait_task(client, pid)
        assert task["status"] == "error", task
        assert "作废" in task["message"]
        assert "丢弃" in task["error"]
        assert client.get(f"/api/projects/{pid}").json()["materials_plan_json"] is None


def test_analyze_material_with_no_placement_is_an_error(tmp_db, monkeypatch):
    """模型没把任何材料归入章节时显式失败，不能安静地写一份空计划。"""
    from app.main import app

    with TestClient(app) as client:
        pid, _mid = _material_project(client)
        _enable_llm(monkeypatch, {"placements": [], "unused": []})

        client.post(f"/api/projects/{pid}/materials/analyze")
        task = _wait_task(client, pid)
        assert task["status"] == "error", task
        assert client.get(f"/api/projects/{pid}").json()["materials_plan_json"] is None


def test_material_change_invalidates_plan(tmp_db):
    """材料列表一变，旧计划就失效（它按 material_id 引用材料）。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        mats = client.post(f"/api/projects/{pid}/materials/text",
                           json={"text": "甲"}).json()
        # 手工塞一份计划，模拟「已分析过」
        from app import db
        db.update_project(pid, materials_plan_json={"placements": [], "unused": []})
        assert client.post(f"/api/projects/{pid}/materials/text",
                           json={"text": "乙"}).status_code == 200
        assert client.get(f"/api/projects/{pid}").json()["materials_plan_json"] is None

        db.update_project(pid, materials_plan_json={"placements": [], "unused": []})
        client.delete(f"/api/projects/{pid}/materials/{mats[0]['id']}")
        assert client.get(f"/api/projects/{pid}").json()["materials_plan_json"] is None


def test_confirm_outline_invalidates_material_plan(tmp_db):
    """计划以章节标题为键，改标题后必须作废，否则材料静默进不了正文。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        outline = _generate_outline(client, pid)

        from app import db
        db.update_project(pid, materials_plan_json={
            "placements": [{"section_title": outline["chapters"][0]["sections"][0]["title"],
                            "material_ids": ["m1"], "usage": "x", "key_points": []}],
            "unused": [],
        })
        # 标题没动 -> 计划保留
        r = client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        assert r.json()["material_plan_invalidated"] is False
        assert client.get(f"/api/projects/{pid}").json()["materials_plan_json"] is not None

        # 改了被计划引用的那一节标题 -> 作废
        outline["chapters"][0]["sections"][0]["title"] = "1.1 改过的标题"
        r = client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        assert r.json()["material_plan_invalidated"] is True
        assert client.get(f"/api/projects/{pid}").json()["materials_plan_json"] is None


# ---------------------------------------------------------------
# 确认大纲（人工确认点①）的闸门
# ---------------------------------------------------------------
def test_outline_confirm_missing_project_is_404(tmp_db):
    """项目不存在时 404（此前是 500：下一行 `p["target_words"]` 直接 TypeError）。"""
    from app.main import app

    with TestClient(app) as client:
        r = client.post(
            "/api/projects/nosuchproject/outline/confirm",
            json={"outline": {"chapters": []}},
        )
        assert r.status_code == 404, r.text
        assert "不存在" in r.json()["detail"]


def test_outline_confirm_rejected_before_outline_generated(tmp_db):
    """没生成过大纲就确认 → 400，且不留痕。

    此前唯一挡着它的东西是前端按钮的 disabled；直接调接口时，一次确认就把
    status 写成了 outline_confirmed，而 outline_json 还是空的 —— 于是「确认点①
    被绕过」这件事在库里看不出来，下一步（引用确认）的状态白名单也照过。
    """
    from app.main import app

    with TestClient(app) as client:
        # 空白草稿
        pid = client.post("/api/projects", json={}).json()["id"]
        r = client.post(f"/api/projects/{pid}/outline/confirm",
                        json={"outline": {"chapters": []}})
        assert r.status_code == 400, r.text
        assert "生成大纲" in r.json()["detail"]

        # 已选题但还没生成大纲
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        r = client.post(f"/api/projects/{pid}/outline/confirm",
                        json={"outline": {"chapters": []}})
        assert r.status_code == 400, r.text
        after = client.get(f"/api/projects/{pid}").json()
        assert after["status"] == "topic_set"
        assert after["outline_json"] is None


def test_outline_confirm_rejected_while_outline_is_generating(tmp_db):
    """生成大纲跑到一半点确认：409，且一个字节都不写。

    _run_outline 收尾是**无条件**写 outline_json + status=outline_pending 的，放行的
    结局是用户刚盖的章被几秒后的收尾换成另一份没人看过的大纲，连这次确认顺带作废
    掉的引用绑定与材料计划一起白作废一次。
    """
    from app import db
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        outline = _generate_outline(client, pid)
        # 手工塞一份材料计划：一旦被放行，confirm_outline 的作废逻辑会把它清掉，
        # 于是「有没有留痕」这件事有个看得见的见证物。
        db.update_project(pid, materials_plan_json={"placements": [], "unused": []})
        before = client.get(f"/api/projects/{pid}").json()

        class _FakeTask:
            def done(self) -> bool:
                return False

        key = f"outline:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/outline/confirm",
                            json={"outline": outline})
        finally:
            projects_router.tasks._TASKS.pop(key, None)

        assert r.status_code == 409, r.text
        assert "大纲正在生成中" in r.json()["detail"]
        after = client.get(f"/api/projects/{pid}").json()
        assert after["status"] == before["status"]
        assert after["outline_json"] == before["outline_json"]
        assert after["materials_plan_json"] is not None

        # 守卫不能变成锁：任务位空出来之后必须能正常确认
        r = client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        assert r.status_code == 200, r.text


def test_outline_confirm_rejected_while_generating(tmp_db):
    """正文生成中确认大纲 = 状态倒退：会把 generating 写成 outline_confirmed。

    这两个状态指向同一个项目的同一件事，而生成任务还在往 sections_json 里写正文 ——
    界面按 status 渲染的那一步当场跳回大纲。同一族里另有两条入口（上传文献、执行
    引用调度）早已被挡，这一条是漏的。
    """
    from app import db
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = _doc_project(client)
        outline = client.get(f"/api/projects/{pid}").json()["outline_json"]

        class _FakeTask:
            def done(self) -> bool:
                return False

        projects_router._GEN_TASKS[pid] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/outline/confirm",
                            json={"outline": outline})
        finally:
            projects_router._GEN_TASKS.pop(pid, None)
        assert r.status_code == 409, r.text
        assert "生成正文" in r.json()["detail"]

        # 残留的 generating（服务重启等留下的行，没有活的生成任务）同样不从这一步
        # 往下盖章：那是一份已经生成好的正文被打回大纲阶段。
        db.update_project(pid, status="generating")
        r = client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        assert r.status_code == 409, r.text
        assert client.get(f"/api/projects/{pid}").json()["status"] == "generating"


def test_outline_generate_rejected_while_generating(tmp_db):
    """正文生成中重生成大纲也要挡住（此前只有 /documents 查了反向那一套）。

    放行的结局不是「两件事并行」这么轻：_run_outline 收尾写 status=outline_pending，
    把 generating 顶掉，界面当场跳回大纲，而生成任务还在往 sections_json 里写正文；
    同时新大纲的章节标题与那个任务正要用的引用绑定全部失配，生成时静默产出零引用章节。
    """
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = _doc_project(client)
        before = client.get(f"/api/projects/{pid}").json()

        class _FakeTask:
            def done(self) -> bool:
                return False

        projects_router._GEN_TASKS[pid] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/outline/generate")
        finally:
            projects_router._GEN_TASKS.pop(pid, None)

        assert r.status_code == 409, r.text
        assert "生成正文" in r.json()["detail"]
        # 没起火：没写任务进度、没登记后台任务
        after = client.get(f"/api/projects/{pid}").json()
        assert after["task_state_json"] == before["task_state_json"]
        assert not projects_router.tasks.is_running(f"outline:{pid}")

        # 守卫不能变成锁
        assert client.post(f"/api/projects/{pid}/outline/generate").status_code == 200
        assert _wait_task(client, pid).get("status") == "done"


# ---------------------------------------------------------------
# 研究设计 / 技术方案
# ---------------------------------------------------------------
def _design_project(client, paper_type="定量/实证研究") -> str:
    pid = client.post("/api/projects", json={}).json()["id"]
    client.post(f"/api/projects/{pid}/topic", json={
        "paper_type": paper_type, "target_words": 1000, "topic": "t",
    })
    return pid


def test_save_design_keeps_only_whitelisted_fields(tmp_db):
    """保存设计时按该类型的字段名白名单过滤，多余键丢弃。

    字段名是前端表单的结构，不能由请求体决定；否则界面渲染的是 4 个框，
    库里却存着第 5 个谁也没填过的字段。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _design_project(client)
        r = client.post(f"/api/projects/{pid}/design", json={"fields": {
            "数据源与样本": "某平台 312 份问卷",
            "主要结果": "β=0.42",
            "不存在的字段": "x",
        }})
        assert r.status_code == 200
        saved = client.get(f"/api/projects/{pid}").json()["design_json"]["fields"]
        assert saved["数据源与样本"] == "某平台 312 份问卷"
        assert saved["主要结果"] == "β=0.42"
        assert "不存在的字段" not in saved
        # 没填的字段补空串，前端表单才有完整的 4 个框
        assert saved["研究假设"] == ""


def test_save_design_rejected_for_type_without_design(tmp_db):
    """没有设计步的类型拒绝保存 —— 否则会存下一份永远没人读的字段。"""
    from app.main import app

    with TestClient(app) as client:
        pid = _design_project(client, "课程论文/小论文")
        r = client.post(f"/api/projects/{pid}/design",
                        json={"fields": {"数据源与样本": "x"}})
        assert r.status_code == 400
        assert "无需填写研究设计" in r.json()["detail"]


def test_extract_design_requires_materials(tmp_db, monkeypatch):
    """没有材料就没什么可提炼的，直接 400，而不是跑一次注定空手的 LLM 调用。"""
    from app.main import app

    with TestClient(app) as client:
        pid = _design_project(client)
        # 先把 LLM 打开，否则会先撞上「未配置 LLM」这道更前面的闸，测不到这一步
        _enable_llm(monkeypatch, {})
        r = client.post(f"/api/projects/{pid}/design/extract")
        assert r.status_code == 400
        assert "材料" in r.json()["detail"]


def test_extract_design_requires_designable_type(tmp_db):
    """课程论文没有设计步，不该提供提炼入口。"""
    from app.main import app

    with TestClient(app) as client:
        pid = _design_project(client, "课程论文/小论文")
        client.post(f"/api/projects/{pid}/materials/text", json={"text": "数据"})
        r = client.post(f"/api/projects/{pid}/design/extract")
        assert r.status_code == 400


def test_extract_design_stores_fields(tmp_db, monkeypatch):
    """提炼成功后字段落库、任务 kind 为 design_extract。"""
    from app.main import app

    with TestClient(app) as client:
        pid = _design_project(client)
        client.post(f"/api/projects/{pid}/materials/text",
                    json={"text": "样本 312 份，β=0.42"})
        _enable_llm(monkeypatch, {
            "fields": {"数据源与样本": "312 份问卷", "主要结果": "β=0.42",
                       "模型与方法": "", "多出来的键": "丢弃"},
            "notes": "研究假设缺依据，请作者补充",
        })

        assert client.post(f"/api/projects/{pid}/design/extract").status_code == 200
        task = _wait_task(client, pid)
        assert task["status"] == "done", task
        assert task["kind"] == "design_extract"

        design = client.get(f"/api/projects/{pid}").json()["design_json"]
        assert design["fields"]["数据源与样本"] == "312 份问卷"
        assert set(design["fields"]) == {
            "数据源与样本", "研究假设", "模型与方法", "主要结果",
        }
        assert design["notes"] == "研究假设缺依据，请作者补充"


def test_extract_design_empty_result_is_an_error(tmp_db, monkeypatch):
    """字段全空时显式失败：静默写库会让界面显示「已提炼」却什么都没变。"""
    from app.main import app

    with TestClient(app) as client:
        pid = _design_project(client)
        client.post(f"/api/projects/{pid}/materials/text", json={"text": "闲聊"})
        _enable_llm(monkeypatch, {
            "fields": {"数据源与样本": "", "研究假设": "", "模型与方法": "", "主要结果": ""},
            "notes": "材料里没有设计信息",
        })

        client.post(f"/api/projects/{pid}/design/extract")
        task = _wait_task(client, pid)
        assert task["status"] == "error", task
        assert "没有设计信息" in task["error"]
        assert client.get(f"/api/projects/{pid}").json()["design_json"] is None


def test_extract_design_discarded_when_material_deleted_mid_extract(tmp_db, monkeypatch):
    """提炼期间删掉材料：结果必须丢弃，不能让被删材料的数字进 design_json。

    这是 D7 那条缺陷：_run_extract_design 手里的 materials 是开跑时的快照，而提炼是
    一次 LLM 调用，几秒到几十秒里用户完全可以删掉一份数据材料。若不复查，被删材料的
    数字照样进 design_json、再进大纲与每一节正文 —— 与材料分析那道
    _analysis_targets_changed 是同一个道理。
    """
    from app import db
    from app.config import settings
    from app.llm import llm
    from app.main import app

    with TestClient(app) as client:
        pid = _design_project(client)
        mats = client.post(f"/api/projects/{pid}/materials/text",
                           json={"text": "样本 312 份，β=0.42"}).json()
        mid = mats[0]["id"]

        monkeypatch.setattr(settings, "llm_api_key", "test-key")
        monkeypatch.setattr(settings, "llm_base_url", "http://test.invalid")
        llm.reload()

        async def fake(messages, **kw):
            db.delete_material(mid)
            return {"fields": {"数据源与样本": "312 份问卷", "主要结果": "β=0.42"},
                    "notes": ""}

        monkeypatch.setattr(llm, "chat_json", fake)

        assert client.post(f"/api/projects/{pid}/design/extract").status_code == 200
        task = _wait_task(client, pid)
        assert task["status"] == "error", task
        assert "作废" in task["message"]
        assert "丢弃" in task["error"]
        assert client.get(f"/api/projects/{pid}").json()["design_json"] is None


def test_design_extract_is_mutually_exclusive_with_outline(tmp_db):
    """提炼中的项目不能同时生成大纲。

    否则两遍 LLM 并跑，进度文案互相覆盖，更糟的是大纲读到的还是一份空的
    design_json —— 也就是这个功能本要消灭的「编数据」。
    """
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = _design_project(client)

        class _FakeTask:
            def done(self) -> bool:
                return False

        key = f"design_extract:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/outline/generate")
            assert r.status_code == 409
            assert "设计" in r.json()["detail"]
        finally:
            projects_router.tasks._TASKS.pop(key, None)


# ---------------------------------------------------------------
# 一手内容类论文：文献与引用调度可跳过（七类全开）
# ---------------------------------------------------------------
def _report_project(client, paper_type="技术/工程报告") -> str:
    """建一个「指定类型 + 大纲已确认」的项目，默认技术/工程报告。"""
    pid = client.post("/api/projects", json={}).json()["id"]
    client.post(f"/api/projects/{pid}/topic", json={
        "paper_type": paper_type, "target_words": 1000, "topic": "t",
    })
    outline = _generate_outline(client, pid)
    client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
    return pid


def test_skip_citations_allows_generation_without_documents(tmp_db):
    """不传文献也能走到生成，且参考文献列表为空。

    「无引用」用绑定缺省表示，不需要哨兵值 —— 生成环节的失配检查在缺省时自然跳过，
    _plan_citations 也自然给出空的参考文献列表。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _report_project(client)

        r = client.post(f"/api/projects/{pid}/citations/skip")
        assert r.status_code == 200
        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["status"] == "citation_confirmed"
        assert proj["citation_binding_json"] is None, "跳过就是缺省，不塞哨兵值"

        import time

        assert client.post(f"/api/projects/{pid}/generate").status_code == 200
        data = {}
        for _ in range(200):
            data = client.get(f"/api/projects/{pid}/generate/progress").json()
            if data["status"] != "running":
                break
            time.sleep(0.02)
        assert data["status"] == "done", data
        assert data["references"] == []
        assert data["done"] == data["total"]
        assert proj["references_json"] is None


@pytest.mark.parametrize("paper_type", [
    t for t in paper_types.PAPER_TYPES if not paper_types.is_literature_first(t)
])
def test_skip_citations_is_open_to_every_non_review_type(tmp_db, paper_type):
    """除文献综述外，每一类都允许跳过引用调度（本轮的策略决定）。

    跳过之所以能成立，是因为这些类型的立身之本是一手内容（作者的数据、访谈、
    实现、假设、方案），文献只是佐证；零引用论文仍然是一篇真实的论文。
    文献综述不在此列 —— 它的结构与论证**就是**那些文献，见下一个用例。

    列表**从 paper_types.PAPER_TYPES 推导**而不是手抄：手抄的话，将来新增第 8 类
    时这个用例照旧绿，新类型的跳过闸门坏了也没人测到（T8）。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _report_project(client, paper_type)
        r = client.post(f"/api/projects/{pid}/citations/skip")
        assert r.status_code == 200, r.text
        assert client.get(f"/api/projects/{pid}").json()["status"] == "citation_confirmed"


def test_skip_citations_rejected_for_review_without_documents(tmp_db):
    """文献综述零文献时拒绝跳过：这不是用户的选择，而是结构性退化。

    「零文献的文献综述」无法成立 —— 它的章节主题本就由文献聚类生成。覆盖的路径是
    「生成大纲后把文献全删光」：那时大纲还在，跳过这一步看起来完全可行。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "文献综述", "target_words": 1000, "topic": "t",
        })
        pdf = _make_pdf(["Doc: theme", "Author: Li, 2022", "Body."])
        _upload_documents(client, pid, files=[("files", ("a.pdf", pdf, "application/pdf"))])
        client.post(f"/api/projects/{pid}/outline/confirm",
                    json={"outline": _generate_outline(client, pid)})

        # 有文献时跳过应当成功 —— 先确认这条守卫拦的是「零文献」而不是整个类型
        assert client.post(f"/api/projects/{pid}/citations/skip").status_code == 200

        # 把文献删光，再回到待调度状态
        doc_id = client.get(f"/api/projects/{pid}/documents").json()[0]["id"]
        client.delete(f"/api/projects/{pid}/documents/{doc_id}")
        r = client.post(f"/api/projects/{pid}/citations/skip")
        assert r.status_code == 400
        assert "文献" in r.json()["detail"]


def test_generate_rejected_for_review_without_documents(tmp_db):
    """文献综述在「大纲已确认但文献被删光」时必须拦住生成。

    这条守卫的落点是 _require_generatable 而不是 /citations/skip：/citations/confirm
    接受空绑定，所以「不点跳过、直接确认空绑定」也能走到生成。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "文献综述", "target_words": 1000, "topic": "t",
        })
        pdf = _make_pdf(["Doc: theme", "Author: Li, 2022", "Body."])
        _upload_documents(client, pid, files=[("files", ("a.pdf", pdf, "application/pdf"))])
        client.post(f"/api/projects/{pid}/outline/confirm",
                    json={"outline": _generate_outline(client, pid)})

        doc_id = client.get(f"/api/projects/{pid}/documents").json()[0]["id"]
        client.delete(f"/api/projects/{pid}/documents/{doc_id}")

        r = client.post(f"/api/projects/{pid}/citations/confirm", json={"binding": {}})
        assert r.status_code == 200

        r = client.post(f"/api/projects/{pid}/generate")
        assert r.status_code == 400
        assert "文献" in r.json()["detail"]


def test_skip_citations_requires_outline(tmp_db):
    """没有大纲就跳过引用，等于给一份还不存在的论文盖章。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "技术/工程报告", "target_words": 1000, "topic": "t",
        })
        r = client.post(f"/api/projects/{pid}/citations/skip")
        assert r.status_code == 400
        assert "大纲" in r.json()["detail"]


def test_confirm_citations_missing_project_is_404(tmp_db):
    """ID 不存在要报 404，而不是 UPDATE 影响 0 行后照样返回 {"ok": true}。

    既有缺陷：这是全仓**唯一一个不带任何前置校验就写 CITATION_CONFIRMED 的入口**，
    调用方拿到 ok 会以为确认成功，实际库里一行没动。
    """
    from app.main import app

    r = TestClient(app).post("/api/projects/nope/citations/confirm", json={"binding": {}})
    assert r.status_code == 404


def test_confirm_citations_requires_confirmed_outline(tmp_db):
    """没确认过大纲就不能确认引用 —— 否则这一步会顶掉大纲那道人工闸门。

    既有缺陷的回归测试：/citations/confirm 原先把 status 无条件写成
    citation_confirmed，而 _require_generatable 只查 outline_json 是否存在 —— 于是
    「大纲已生成但没确认」的项目点一下「确认引用」，就能一路生成出正文，两个人工
    确认点废掉一个。
    """
    from app.main import app

    with TestClient(app) as client:
        # 1) 连大纲都还没有
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        r = client.post(f"/api/projects/{pid}/citations/confirm", json={"binding": {}})
        assert r.status_code == 400
        assert "大纲" in r.json()["detail"]

        # 2) 大纲已生成、但**还没确认**：闸门真正被绕开的那条路
        outline = _generate_outline(client, pid)
        assert client.get(f"/api/projects/{pid}").json()["status"] == "outline_pending"
        r = client.post(f"/api/projects/{pid}/citations/confirm", json={"binding": {}})
        assert r.status_code == 400
        assert client.get(f"/api/projects/{pid}").json()["status"] == "outline_pending", \
            "被拒绝的确认不许动状态"

        # 3) 确认大纲之后放行（守卫不能变成一把锁死不放的锁）
        client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        r = client.post(f"/api/projects/{pid}/citations/confirm", json={"binding": {}})
        assert r.status_code == 200, r.text


def test_confirm_citations_rejected_while_generating(tmp_db):
    """正文生成中不许改引用确认：那一笔会把 status 从 generating 顶掉。"""
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = _doc_project(client)

        class _FakeTask:
            def done(self) -> bool:
                return False

        projects_router._GEN_TASKS[pid] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/citations/confirm", json={"binding": {}})
            assert r.status_code == 409
            assert "生成" in r.json()["detail"]
        finally:
            projects_router._GEN_TASKS.pop(pid, None)


def test_schedule_citations_rejected_while_generating(tmp_db):
    """生成中重排引用也要挡住 —— 它会把 status 从 generating 顶成 citation_pending。

    与 /generate、/documents 同一类：本步回退再点「执行引用调度」，界面按 status
    渲染的那一步就跳走，而生成任务还在往 sections_json 里写正文。
    """
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = _doc_project(client)

        class _FakeTask:
            def done(self) -> bool:
                return False

        projects_router._GEN_TASKS[pid] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/citations/schedule")
            assert r.status_code == 409
            assert "生成正文" in r.json()["detail"]
        finally:
            projects_router._GEN_TASKS.pop(pid, None)

        # 也挡住「有别的后台任务在跑」：调度会把刚该被作废的绑定重新写实
        key = f"documents:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/citations/schedule")
            assert r.status_code == 409
            assert "文献正在解析中" in r.json()["detail"]
        finally:
            projects_router.tasks._TASKS.pop(key, None)

        # 任务清空后必须能正常调度（守卫不能变成锁）
        assert client.post(f"/api/projects/{pid}/citations/schedule").status_code == 200


def test_citation_schedule_registers_itself_while_it_waits(tmp_db, monkeypatch):
    """调度的等待期间必须登记 —— 此前那 120 秒全后端无一人知道。

    打桩的 _build_binding 在「自己正等着」的这一刻回头看登记表：_busy_task 必须回报
    citations、登记表里那一条必须在跑；收尾之后必须不留残留（登记不能变成锁）。
    """
    from app.main import app
    from app.routers import projects as projects_router
    from app.routers import projects_citations as citations_router

    seen: dict = {}

    async def fake_build(p, docs):
        pid = p["id"]
        seen["busy"] = projects_router._busy_task(pid)
        seen["running"] = projects_router.tasks.is_running(
            projects_router._citations_key(pid)
        )
        title = p["outline_json"]["chapters"][0]["sections"][0]["title"]
        return {title: [{"doc_id": docs[0]["id"], "reason": "r"}]}, "relevance"

    monkeypatch.setattr(citations_router, "_build_binding", fake_build)

    with TestClient(app) as client:
        pid = _doc_project(client)
        r = client.post(f"/api/projects/{pid}/citations/schedule")
        assert r.status_code == 200, r.text
        assert r.json()["method"] == "relevance"
        assert seen == {"busy": "citations", "running": True}

        # 登记随任务一起消失，而且结果真的写进了库（取值那条路没被 _guard 吃掉）
        assert projects_router._busy_task(pid) is None
        assert not projects_router.tasks.is_running(projects_router._citations_key(pid))
        assert client.get(f"/api/projects/{pid}").json()[
            "citation_binding_json"
        ] == r.json()["binding"]


def test_citation_schedule_reports_progress_while_it_waits(tmp_db, monkeypatch):
    """等待期间槽里必须有具体阶段 —— 此前那 120 秒界面一个字都给不出。

    两笔写入各看一次，两次都读**真实**的一列（/task/progress 是把这一列逐字回出去，
    在这里直接读列即可：那个端点没有自己的逻辑可验，而在协程里再发一次请求会把唯一的
    事件循环线程堵死 —— TestClient 的每条请求都排在同一循环上）：

      · 进 _build_binding 的那一刻 → ①「正在准备引用调度」（闸门之后、点火之前那笔）
      · 即将问模型的那一刻（打桩的是**更里面**的 assign_by_relevance，所以 _build_binding
        本身是真的）→ ②「正在让模型逐篇判断…」

    打桩打在 assign_by_relevance 而不是 _build_binding 上，是因为 ② 就在 _build_binding
    里：把整个函数换掉，② 那笔写入也跟着被换掉，这一条就只测得到打桩自己了。

    done/total 必须是 None：整段等待就是那一次调用，没有任何可数的单位，给百分比就是
    编造（WorkingBar 自己的注释也是这条判据）。将来谁想补个假百分比，这条钉子会红。
    """
    from app import db
    from app.main import app
    from app.routers import projects as projects_router
    from app.routers import projects_citations as citations_router

    seen: dict = {}
    real_build = projects_router._build_binding

    async def fake_build(p, docs):
        seen["before"] = db.get_project(p["id"])["task_state_json"] or {}
        return await real_build(p, docs)

    async def fake_assign(outline, documents, **kwargs):
        seen["during"] = db.get_project(pid_holder["id"])["task_state_json"] or {}
        return None  # 判断不可用 → 退回确定性兜底（route 那边照常收尾）

    pid_holder: dict = {}

    monkeypatch.setattr(citations_router, "_build_binding", fake_build)
    monkeypatch.setattr(
        projects_router.relevance_agent, "assign_by_relevance", fake_assign
    )

    with TestClient(app) as client:
        pid = _doc_project(client)
        pid_holder["id"] = pid
        assert (
            client.get(f"/api/projects/{pid}/task/progress").json()["task"]["status"]
            == "done"  # 前提：上传那一趟的终态已经落好，下面读到的不是它
        )

        r = client.post(f"/api/projects/{pid}/citations/schedule")
        assert r.status_code == 200, r.text
        assert r.json()["method"] == "uniform"  # 打桩的模型判断回了 None

        for stage in ("before", "during"):
            got = seen[stage]
            assert got["kind"] == "citations", stage
            assert got["status"] == "running", stage
            assert got["message"], f"{stage}: 等待期间界面要有话可说"
            assert got["started_at"], f"{stage}: 「已等待 N 秒」要有锚点"
            assert got["done"] is None and got["total"] is None, (
                f"{stage}: 一次调用没有可数的单位，给百分比就是编造"
            )

        # ② 是"在等什么"那一句，不是①那句"正在准备"（后者写在等待之前，说出来等于没有）
        assert "正在准备引用调度" not in seen["during"]["message"]
        # started_at 沿用一个锚点：每写一笔都换时刻的话，「已等待 N 秒」会一次次归零
        assert seen["during"]["started_at"] == seen["before"]["started_at"]


def test_citation_schedule_marks_progress_done(tmp_db):
    """真路径（测试环境没有模型 → 兜底）跑完：终态落槽，且**不早于**绑定落库。

    终态若先落库，另一侧（另一个标签页 / 切走再切回）会先看到"完成"、再去取一份还没
    写上绑定的项目 —— 而用户正是在那一瞬最可能去点「确认引用绑定」，它提交的是前端
    手里那份绑定。所以这里的顺序由 update_project + updated is None 之后那笔写入保证。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        r = client.post(f"/api/projects/{pid}/citations/schedule")
        assert r.status_code == 200, r.text
        assert r.json()["method"] == "uniform", "测试环境没有模型，走的必然是兜底"

        task = client.get(f"/api/projects/{pid}/task/progress").json()["task"]
        assert task["kind"] == "citations"
        assert task["status"] == "done"
        assert task["error"] is None
        assert task["done"] is None and task["total"] is None
        binding = r.json()["binding"]
        assert len(binding) > 0, "前提：这份大纲有叶子章节，兜底铺得开"
        # 终态文案是"已落库事实"的复述：章节数与回执里那一份必须对得上
        assert str(len(binding)) in task["message"], task["message"]
        # 落库的绑定与回执里那一份是同一份
        assert client.get(f"/api/projects/{pid}").json()[
            "citation_binding_json"
        ] == binding


def test_citation_schedule_refuses_to_write_when_outline_changed_midway(tmp_db, monkeypatch):
    """等待期间大纲被换掉：**绑定与状态**一个字节都不写。

    绑定以章节标题为 key，落下去就是一份错位的绑定 —— 生成时静默产出「零引用」的
    章节，正是 confirm_outline 要主动作废的那种状态。旧实现里写库用的是 await 之前
    那份快照，连 status 都会照写。

    **v1.29 起界限要说清**：标题里那句原本是"一个字节都不写"，现在只有**项目数据**
    （绑定与 status）一个字节都不写 —— 进度槽是例外，而且**必须**写：那一笔是界面
    事实（"这一次失败了，原因是它"），不写的话进度条会永远转下去（轮询永远等不到
    终态）。它不落进项目数据，所以上面那两条断言照旧。
    """
    from app import db
    from app.main import app
    from app.routers import projects as projects_router
    from app.routers import projects_citations as citations_router

    async def fake_build(p, docs):
        outline = dict(p["outline_json"])
        outline["chapters"] = []  # 等待期间大纲被换成了另一份
        db.update_project(p["id"], outline_json=outline)
        return {"任意章节": []}, "uniform"

    monkeypatch.setattr(citations_router, "_build_binding", fake_build)

    with TestClient(app) as client:
        pid = _doc_project(client)
        before = client.get(f"/api/projects/{pid}").json()

        r = client.post(f"/api/projects/{pid}/citations/schedule")
        assert r.status_code == 409, r.text
        assert "大纲" in r.json()["detail"]

        after = client.get(f"/api/projects/{pid}").json()
        assert after["citation_binding_json"] is None
        assert after["status"] == before["status"]

        # 进度槽：有终态（否则那条进度条永远转），且没把后端给的确切原因丢掉。
        task = client.get(f"/api/projects/{pid}/task/progress").json()["task"]
        assert task["kind"] == "citations"
        assert task["status"] == "error", "被拒的这一次也必须给界面一个终态"
        assert "大纲" in task["error"], task["error"]


def test_citation_schedule_failure_does_not_leave_a_running_bar(tmp_db, monkeypatch):
    """_build_binding 抛异常：500，且槽里是 error 而不是 running。

    running 的结局是界面永远转圈（轮询等不到终态、按钮永远灰着），所以任何出口都必须
    给一个终态 —— 这条钉子钉住的就是那个**唯一的错误写者**（try/except，不是
    try/finally）。

    槽里的原因只能是路由自己那句"引用调度未能完成，请重试"：异常本身被 tasks._guard
    吞掉并记了日志、只回一个 None 出来（见 _guard 的文档串），所以 "RuntimeError: …"
    到不了这里。这是既有设计，不是为了这一轮省事。
    """
    from app.main import app
    from app.routers import projects as projects_router
    from app.routers import projects_citations as citations_router

    async def boom(p, docs):
        raise RuntimeError("模型连接被重置")

    monkeypatch.setattr(citations_router, "_build_binding", boom)

    with TestClient(app) as client:
        pid = _doc_project(client)
        before = client.get(f"/api/projects/{pid}").json()

        r = client.post(f"/api/projects/{pid}/citations/schedule")
        assert r.status_code == 500, r.text

        task = client.get(f"/api/projects/{pid}/task/progress").json()["task"]
        assert task["kind"] == "citations"
        assert task["status"] == "error", "失败路径必须给终态，否则进度条永远转"
        assert task["error"], "后端给的原因不能被吞掉"
        assert task["done"] is None and task["total"] is None

        # 失败的这一次不碰项目数据
        after = client.get(f"/api/projects/{pid}").json()
        assert after["citation_binding_json"] is None
        assert after["status"] == before["status"]


def test_citation_schedule_rejected_leaves_no_trace(tmp_db):
    """被闸门挡下的那一次不在进度槽里留痕 —— ① 那笔写在闸门**之后**。

    留痕不是"多一行无害的日志"：界面会把那条进度渲染成进度条，用户看到一条属于一次
    **被拒**请求的"正在准备引用调度…"，而那次调度根本没发生（本仓既有约定：被拒的
    请求不留副作用，上传那条路上有同形的用例）。

    这里比的是调用前后**逐字相等**：槽里本来就有一份别的终态（上传文献那一趟留下的），
    所以"没被覆盖成 running"这件事也一并钉住了。
    """
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = _doc_project(client)
        before = client.get(f"/api/projects/{pid}").json()["task_state_json"]
        assert before["status"] != "running", "前提：槽里是一份已结束的终态"

        class _FakeTask:
            def done(self) -> bool:
                return False

        key = f"documents:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/citations/schedule")
            assert r.status_code == 409, r.text
            assert "文献正在解析中" in r.json()["detail"]

            after = client.get(f"/api/projects/{pid}").json()["task_state_json"]
            assert after == before, "被拒的这一次不能在进度槽里留痕"
        finally:
            projects_router.tasks._TASKS.pop(key, None)


def test_citation_schedule_blocks_the_other_entries(tmp_db):
    """登记在 citations:<pid> 上，其余入口**一行都没改**就都看得见它。

    这是「共用一套判据」的可执行版本：改选题、生成大纲、确认大纲、上传文献、再点一次
    调度、跳过引用、重排参考文献列表、重置正文，在等待期间一律 409 —— 因为它们查的
    是同一张表（_busy_task）。
    """
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = _doc_project(client)
        outline = client.get(f"/api/projects/{pid}").json()["outline_json"]

        class _FakeTask:
            def done(self) -> bool:
                return False

        key = f"citations:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        try:
            calls = [
                (f"/api/projects/{pid}/topic",
                 {"paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t2"}),
                (f"/api/projects/{pid}/outline/generate", None),
                (f"/api/projects/{pid}/outline/confirm", {"outline": outline}),
                (f"/api/projects/{pid}/citations/schedule", None),
                (f"/api/projects/{pid}/citations/skip", None),
                (f"/api/projects/{pid}/references/rerender", None),
                (f"/api/projects/{pid}/sections/reset", None),
            ]
            for url, body in calls:
                r = client.post(url, **({"json": body} if body else {}))
                assert r.status_code == 409, (url, r.status_code, r.text)
                assert "引用正在调度中" in r.json()["detail"], (url, r.text)

            r = client.post(
                f"/api/projects/{pid}/documents",
                files=[("files", ("b.pdf", _make_pdf(["second"]), "application/pdf"))],
            )
            assert r.status_code == 409, r.text
            assert "引用正在调度中" in r.json()["detail"]
        finally:
            projects_router.tasks._TASKS.pop(key, None)

        # 守卫不能变成锁
        assert client.post(f"/api/projects/{pid}/citations/schedule").status_code == 200


def test_citation_schedule_blocks_generation_while_it_waits(tmp_db):
    """调度等着的时候点火正文生成 → 409。

    这正是 A4 的场景：生成会把 status 写成 generating，而调度收尾时按旧快照写回
    citation_pending —— 「正在生成」与「正在调度」互相顶掉，界面与库里各显示一套。
    """
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = _doc_project(client)
        # 先做一次真调度：/generate 的前置要求是「已有引用绑定」
        assert client.post(f"/api/projects/{pid}/citations/schedule").status_code == 200
        assert client.get(f"/api/projects/{pid}").json()["citation_binding_json"]

        class _FakeTask:
            def done(self) -> bool:
                return False

        key = f"citations:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/generate")
        finally:
            projects_router.tasks._TASKS.pop(key, None)

        assert r.status_code == 409, r.text
        assert "引用正在调度中" in r.json()["detail"]
        assert not projects_router._gen_running(pid)


def test_citation_edit_after_generation_keeps_status_and_marks_stale(tmp_db):
    """正文已生成后**允许**改引用设置，但必须如实回报「正文还是按旧编号写的」。

    用户 2026-09-18 定的策略：拦着不让改，连一处笔误都修不了，所以改成「放开 + 说
    清楚」。配套的两件事一起验：三个引用入口都不许把 status 打回上游（打回去会让
    一份已生成的稿子在步骤条上再也点不回生成与导出），而「改完到底生效没有」由
    citations_stale 明说 —— 界面据它逐步提示，生成入口据它给出拒绝理由。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        binding = client.post(f"/api/projects/{pid}/citations/schedule").json()["binding"]
        assert client.post(
            f"/api/projects/{pid}/citations/confirm",
            json={"binding": binding, "cite_style": "bracket"},
        ).status_code == 200

        assert client.post(f"/api/projects/{pid}/generate").status_code == 200
        import time

        for _ in range(200):
            if client.get(f"/api/projects/{pid}/generate/progress").json()["status"] != "running":
                break
            time.sleep(0.02)
        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["status"] == "completed"
        assert proj["references_json"], "生成完必须落下文末列表（它同时是编号基线）"
        assert proj["citations_stale"] is False, "刚生成完，正文与设置当然是一致的"

        # 改绑定：放行，且如实回报「正文是按旧绑定写的」
        r = client.post(f"/api/projects/{pid}/citations/confirm", json={"binding": {}})
        assert r.status_code == 200, r.text
        assert r.json()["citations_stale"] is True
        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["status"] == "completed", "改引用设置不许把已生成的稿子打回上游"
        assert proj["citations_stale"] is True, "刷新页面（重新取项目）后事实仍要在"

        # 生成入口用**确切原因**拒绝，而不是静默产出一份角标指错文献的稿子
        r = client.post(f"/api/projects/{pid}/generate")
        assert r.status_code == 400
        assert "引用编号" in r.json()["detail"]
        assert client.get(f"/api/projects/{pid}").json()["status"] == "completed", \
            "拒绝必须干净：不点火、不动状态"

        # 把绑定改回原样（重排调度是确定性的）→ 一致性自己恢复，标志不粘人
        assert client.post(f"/api/projects/{pid}/citations/schedule").status_code == 200
        assert client.get(f"/api/projects/{pid}").json()["citations_stale"] is False
        # 于是生成重新放行（编号没变 → 全部命中跳过 → 等于原样重写一遍）
        assert client.post(f"/api/projects/{pid}/generate").status_code == 200


def test_body_outline_mismatch_is_status_gated_computed_fact():
    """body_outline_mismatch 只在终态有意义：其它状态下一律假（哪怕标题确实不等）。

    生成中 / 大纲确认后，正文要么还没写完、要么正在按当前大纲重写 —— 标题集合不相等
    是「写到一半」的正常现象，不是分家。所以函数先按状态短路，只认 completed / exported。
    """
    import app.routers.projects as pr
    from app.db import ProjectStatus

    outline_a = {"chapters": [{"sections": [{"title": "绪论", "word_budget": 1000}]}]}
    outline_b = {"chapters": [{"sections": [{"title": "引言", "word_budget": 1000}]}]}
    body = [{"section_title": "绪论", "content": "正文"}]

    def p(status, sections=body, outline=outline_a):
        return {"status": status, "sections_json": sections, "outline_json": outline}

    assert pr._body_outline_mismatch(p(ProjectStatus.COMPLETED)) is False, "标题一致不算分家"
    assert pr._body_outline_mismatch(p(ProjectStatus.EXPORTED, outline=outline_b)) is True
    # 状态短路：这些状态哪怕标题确实不等也一律假
    for status in ("draft", "outline_confirmed", "citation_confirmed", "generating"):
        assert pr._body_outline_mismatch(p(status, outline=outline_b)) is False, status
    # 缺正文 / 缺大纲也短路
    assert pr._body_outline_mismatch(p(ProjectStatus.COMPLETED, sections=None)) is False
    assert pr._body_outline_mismatch(p(ProjectStatus.COMPLETED, outline=None)) is False


def test_body_outline_mismatch_reported_and_status_not_rolled_back(tmp_db):
    """终态项目改大纲标题：正文与大纲分家要如实报，但状态不退（提示，不粗暴作废）。

    修 D1 时暴露的缺口：completed / exported 项目改章节标题、确认大纲，confirm_outline
    只作废引用绑定与材料计划、不动 sections_json、也不退状态 —— 于是正文还按旧标题
    躺着，而大纲已换成新标题，导出给出旧标题的稿子。引用侧有 citations_stale 兜着，
    大纲侧补上 body_outline_mismatch：终态下标题集合不等即为真。
    """
    from app.main import app
    from app import db as db_module

    outline_a = {"chapters": [{"sections": [{"title": "绪论", "word_budget": 1000}]}]}
    outline_b = {"chapters": [{"sections": [{"title": "引言", "word_budget": 1000}]}]}
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        # 直接造到「成稿 + 正文标题与大纲一致」的终态
        db_module.update_project(
            pid,
            status=db_module.ProjectStatus.COMPLETED,
            outline_json=outline_a,
            sections_json=[{"section_title": "绪论", "content": "正文"}],
        )
        assert client.get(f"/api/projects/{pid}").json()["body_outline_mismatch"] is False, \
            "标题一致时不算分家"

        # 改标题再确认：预算不变、总数仍匹配；正文没重写，回执要如实报「分家」
        r = client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline_b})
        assert r.status_code == 200, r.text
        assert r.json()["body_outline_mismatch"] is True, "标题变了、正文没变，回执要报分家"
        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["status"] == "completed", "改大纲标题不许把成稿打回上游（提示，不粗暴作废）"
        assert proj["body_outline_mismatch"] is True, "刷新后事实仍要在"


def test_generate_refused_after_bound_document_deleted(tmp_db):
    """删掉被引文献后不能直接续写：编号会整体前移，正文角标就指错了文献。

    这是那条错配路最常走的一条：删文献 → 绑定作废 → 重排调度 → 再点一次生成。踩中
    它时输出与成功**完全一样**（每节标题命中跳过、零 LLM 调用、瞬间报「已全部
    完成」），所以必须由生成入口明确拒绝。同时验 citations_stale：删完还没重排时
    界面就该提示「正文与当前设置已经不一致」。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        binding = client.post(f"/api/projects/{pid}/citations/schedule").json()["binding"]
        assert client.post(
            f"/api/projects/{pid}/citations/confirm",
            json={"binding": binding, "cite_style": "bracket"},
        ).status_code == 200
        assert client.post(f"/api/projects/{pid}/generate").status_code == 200

        import time

        for _ in range(200):
            if client.get(f"/api/projects/{pid}/generate/progress").json()["status"] != "running":
                break
            time.sleep(0.02)
        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["status"] == "completed" and proj["references_json"]

        doc_id = client.get(f"/api/projects/{pid}/documents").json()[0]["id"]
        r = client.delete(f"/api/projects/{pid}/documents/{doc_id}")
        assert r.status_code == 200 and r.json()["binding_invalidated"] is True

        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["citation_binding_json"] is None
        assert proj["sections_json"], "正文不因删文献而丢"
        assert proj["citations_stale"] is True, "绑定已作废，正文仍按旧编号"

        r = client.post(f"/api/projects/{pid}/generate")
        assert r.status_code == 400
        assert "引用编号" in r.json()["detail"]


def _add_document(client, pid, name="e.pdf", text="Another doc."):
    """再传一篇文献（走真实上传路径，顺带覆盖 _run_parse 对绑定的态度）。"""
    _, docs = _upload_documents(
        client, pid, files=[("files", (name, _make_pdf([text]), "application/pdf"))]
    )
    return docs


def _assert_same_facts(client, pid, payload):
    """写入接口回报的三个事实必须与详情接口逐字一致。"""
    proj = client.get(f"/api/projects/{pid}").json()
    for key in ("citations_stale", "unbound_documents", "refs_change"):
        assert key in payload, f"写入接口的返回值少了 {key}"
        assert payload[key] == proj[key], f"{key} 与详情接口不一致"


def test_unbound_documents_counts_library_minus_binding(tmp_db):
    """「库里几篇没进绑定」是现算的计数 —— 那条路上 citations_stale 永远看不见。

    上传/删除文献都不碰绑定（_run_parse 只写 status 与聚类），所以**调度之后新传的
    文献永远不会被引用**，而 _citations_stale 看不见它：它按「绑定里的 doc_id × 当前
    文献库」重算文末列表，未进绑定的新文献对那份快照零影响，标志恒为假。这个计数是
    那个缺口唯一的出口，而它算得准是因为调度算法把传进去的每一篇都绑出去 —— 调度
    完成那一刻两个集合必然相等。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        # 有文献、从没调度过：绑定是缺省，库里那篇一篇都没被覆盖
        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["unbound_documents"] == 1
        assert proj["citations_stale"] is False, "两个事实互不代偿：没有正文就无从错配"

        client.post(f"/api/projects/{pid}/citations/schedule")
        assert client.get(f"/api/projects/{pid}").json()["unbound_documents"] == 0, \
            "调度完，库里每一篇都在绑定里"

        _add_document(client, pid)
        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["unbound_documents"] == 1, "新传的那篇进不了既有绑定"
        assert proj["citations_stale"] is False, "它没进快照，这条标志上永远看不见"

        # 重排一次即清零（确定性的均匀打散，覆盖全部文献）—— 标志不粘人
        client.post(f"/api/projects/{pid}/citations/schedule")
        assert client.get(f"/api/projects/{pid}").json()["unbound_documents"] == 0


def test_unbound_documents_after_deleting_bound_document(tmp_db):
    """删掉被绑文献 → 绑定整个作废 → 库里剩下的都成了未绑定。

    这条同时记录着本步此刻的界面状态：绑定没了、文献还在、也没「跳过」过，于是
    确认与前进两个按钮都不渲染，只剩一个白底的「执行引用调度」。计数非零正是让它
    变成主按钮、并把原因写出来的那个信号（此前那点解释只存在于一条 notice 里，
    而 notice 会被下一次操作清掉、刷新页面更是直接没了）。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        docs = _add_document(client, pid)
        assert len(docs) == 2
        client.post(f"/api/projects/{pid}/citations/schedule")
        assert client.get(f"/api/projects/{pid}").json()["unbound_documents"] == 0

        assert client.delete(f"/api/projects/{pid}/documents/{docs[0]['id']}").json()["binding_invalidated"] is True

        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["citation_binding_json"] is None, "删被引文献必然作废整份绑定"
        assert proj["unbound_documents"] == 1, "剩下那篇此刻谁也没绑"


def test_unbound_documents_after_skip_with_documents(tmp_db):
    """跳过引用调度之后又加文献，也必须看得见（用户 2026-09-18 定）。

    跳过不写哨兵值（绑定就是缺省），所以后端无从知道「当时有几篇」—— 也不需要知道：
    「库里几篇没进绑定」在跳过后就等于「库里有几篇」，加了文献数字自己会变大。
    而 citations_stale 在这条路上**恒为假**：那一轮落下的 references_json 是 []，
    重算出来的还是 []（_refs_mismatch 把非 None 的空列表当成一份真实的零引用基线），
    于是「后加的文献永远进不了正文」在那条标志上完全不可见 —— 这个计数是唯一出口。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        r = client.post(f"/api/projects/{pid}/citations/skip")
        assert r.status_code == 200, r.text
        assert r.json()["unbound_documents"] == 1

        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["citation_binding_json"] is None, "跳过就是缺省，不塞哨兵值"
        assert proj["unbound_documents"] == 1

        assert client.post(f"/api/projects/{pid}/generate").status_code == 200
        import time

        for _ in range(200):
            if client.get(f"/api/projects/{pid}/generate/progress").json()["status"] != "running":
                break
            time.sleep(0.02)
        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["status"] == "completed"
        assert proj["references_json"] == [], "零引用正文的基线是明确的空列表（不是 None）"

        _add_document(client, pid, name="f.pdf", text="After skip.")
        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["unbound_documents"] == 2, "两篇都在这份零引用的稿子之外"
        assert proj["citations_stale"] is False, "[] 与 [] 相等，这条标志认不出来"


def test_unbound_documents_zero_without_leaf_sections(tmp_db):
    """大纲里一个叶子章节都没有时一律 0：否则会留下一条永远清不掉的提示。

    调度算法在章节数为 0 时返回 {}，所以「N 篇未绑定」在这个状态下重排一次也修不好 ——
    点亮「执行引用调度」等于指着一个按了没用的按钮。宁可不说。
    """
    from app import db
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        # 直接造库里的现场：这份「已确认但一个叶子章节都没有」的大纲不是走
        # /outline/confirm 来的 —— 那条路现在要求真先生成过大纲（确认点①的闸门），
        # 而本用例要审的是 unbound_documents 的算法，不是那条路径。
        db.update_project(
            pid,
            outline_json={"chapters": [{"title": "一", "sections": []}]},
            status="outline_confirmed",
        )
        assert client.get(f"/api/projects/{pid}").json()["outline_json"], "大纲确实存下来了"
        _add_document(client, pid, name="g.pdf", text="Lonely doc.")

        r = client.post(f"/api/projects/{pid}/citations/schedule")
        assert r.status_code == 200, r.text
        assert r.json()["binding"] == {}, "没有叶子章节可绑"
        assert client.get(f"/api/projects/{pid}").json()["unbound_documents"] == 0


def test_citation_routes_report_the_same_facts(tmp_db):
    """三个引用入口与详情接口回报**同一组**事实 —— 这正是 _citation_facts 的理由。

    citations_stale 当初只挂在 get_project 上，三个写入接口各自手写一遍，于是那几个
    返回值与详情页的形状对不上（_with_display_title 的 docstring 把这件事记在先例里）。
    这条测试把对称性钉住：谁再添一个「此刻的事实」，就得同时出现在这四处。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)

        r = client.post(f"/api/projects/{pid}/citations/schedule")
        assert r.status_code == 200
        _assert_same_facts(client, pid, r.json())

        r = client.post(
            f"/api/projects/{pid}/citations/confirm",
            json={"binding": r.json()["binding"], "cite_style": "bracket"},
        )
        assert r.status_code == 200
        _assert_same_facts(client, pid, r.json())

        r = client.post(f"/api/projects/{pid}/citations/skip")
        assert r.status_code == 200
        _assert_same_facts(client, pid, r.json())


def test_generate_rejected_before_outline_confirmed(tmp_db):
    """没点「确认大纲」（人工确认点①）就不能生成正文。

    生成总闸原先只查「outline_json 存在吗」，而「存在」既包含已确认、也包含刚生成
    还没点确认 —— 于是不点确认也能出正文，人工闸门废掉一个。补的是状态白名单。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        outline = _generate_outline(client, pid)
        assert client.get(f"/api/projects/{pid}").json()["status"] == "outline_pending"

        r = client.post(f"/api/projects/{pid}/generate")
        assert r.status_code == 400
        assert "确认大纲" in r.json()["detail"]
        assert client.get(f"/api/projects/{pid}").json()["status"] == "outline_pending", \
            "被拒绝的生成不许动状态"

        # 确认之后放行 —— 守卫不能变成一把锁死不放的锁
        client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        assert client.post(f"/api/projects/{pid}/generate").status_code == 200


def test_binding_change_after_failed_run_is_caught(tmp_db, monkeypatch):
    """首轮中途失败也留下了编号基线 —— 于是「改了绑定再续写」照样拦得住。

    基线是随**首节**落库的那份 references_json。没有它的话，「跑挂 → 改引用设置 →
    继续生成」这条路会把两套编号缝进同一篇稿子：已落盘的那节用旧编号，新写的用新
    编号，而两边都像是正常正文。
    """
    from app.agents import generate_agent
    from app.main import app
    from app.routers.projects import _collect_ordered_sections

    real = generate_agent.generate_section
    calls = {"n": 0}

    async def flaky(**kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("模拟第 2 节失败")
        return await real(**kwargs)

    monkeypatch.setattr(generate_agent, "generate_section", flaky)

    with TestClient(app) as client:
        pid = _doc_project(client)
        outline = client.get(f"/api/projects/{pid}").json()["outline_json"]
        assert len(_collect_ordered_sections(outline)) >= 2, "本用例需要大纲至少两节"

        binding = client.post(f"/api/projects/{pid}/citations/schedule").json()["binding"]
        assert client.post(
            f"/api/projects/{pid}/citations/confirm",
            json={"binding": binding, "cite_style": "bracket"},
        ).status_code == 200
        assert client.post(f"/api/projects/{pid}/generate").status_code == 200

        import time

        state = {}
        for _ in range(200):
            state = client.get(f"/api/projects/{pid}/generate/progress").json()
            if state["status"] != "running":
                break
            time.sleep(0.02)
        assert state["status"] == "error", state
        proj = client.get(f"/api/projects/{pid}").json()
        assert len(proj["sections_json"]) == 1, "第 1 节已经落库"
        assert proj["references_json"], "基线随首节一起落库 —— 后面能拦住错配全靠它"

        # 改绑定（编号整体变了）再续写：必须被拦
        assert client.post(
            f"/api/projects/{pid}/citations/confirm", json={"binding": {}}
        ).status_code == 200
        r = client.post(f"/api/projects/{pid}/generate")
        assert r.status_code == 400
        assert "引用编号" in r.json()["detail"]

        # 改回原样（重排调度）→ 编号一致，续写重新放行
        assert client.post(f"/api/projects/{pid}/citations/schedule").status_code == 200
        assert client.post(f"/api/projects/{pid}/generate").status_code == 200


def test_orphan_sections_from_old_outline_are_dropped(tmp_db):
    """改过标题再重新生成，旧大纲留下的章节不许混进新正文。

    旧章节既不属于当前大纲（标题已不在里面），又会在结尾被原样写回 sections_json，
    于是导出的是一份「旧章节 + 新章节」并存的稿子 —— 两套都像正常正文，一起印出来。
    清理必须发生在续写判断之前。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        assert client.post(f"/api/projects/{pid}/generate").status_code == 200

        import time

        for _ in range(200):
            if client.get(f"/api/projects/{pid}/generate/progress").json()["status"] != "running":
                break
            time.sleep(0.02)
        old = client.get(f"/api/projects/{pid}").json()["sections_json"]
        assert len(old) >= 2

        # 把大纲里每一节都改个名字再确认（绑定会因标题全变而作废）。
        # 改名要按 _collect_ordered_sections 的规则来：有子节时，成文单元是**子节**。
        outline = client.get(f"/api/projects/{pid}").json()["outline_json"]
        for ch in outline.get("chapters") or []:
            for sec in ch.get("sections") or []:
                if sec.get("subsections"):
                    for sub in sec["subsections"]:
                        sub["title"] = "重写·" + sub["title"]
                else:
                    sec["title"] = "重写·" + sec["title"]
        assert client.post(
            f"/api/projects/{pid}/outline/confirm", json={"outline": outline}
        ).status_code == 200
        assert client.post(f"/api/projects/{pid}/generate").status_code == 200
        for _ in range(200):
            state = client.get(f"/api/projects/{pid}/generate/progress").json()
            if state["status"] != "running":
                break
            time.sleep(0.02)

        new = client.get(f"/api/projects/{pid}").json()["sections_json"]
        titles = [s["section_title"] for s in new]
        assert len(titles) == len(old), f"不许新旧两套并存：{titles}"
        assert all(t.startswith("重写·") for t in titles), titles


def test_generate_without_binding_is_allowed_for_any_type(tmp_db):
    """无引用绑定不再是生成的拦路虎（七类都能跳过引用调度）。

    这条用例替代了原先的 test_generate_requires_binding：那时「无绑定」被当作
    「还没做引用调度」的同义词，如今它是一个合法的终态（零引用论文）。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        outline = _generate_outline(client, pid)
        client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})

        # 未上传文献、未引用调度，直接生成 —— 现在应当放行
        r = client.post(f"/api/projects/{pid}/generate")
        assert r.status_code == 200, r.text

        import time

        data = {}
        for _ in range(200):
            data = client.get(f"/api/projects/{pid}/generate/progress").json()
            if data["status"] != "running":
                break
            time.sleep(0.02)
        assert data["status"] == "done", data
        assert data["references"] == []


# ---------------------------------------------------------------
# 文献综述的主题聚类：能看、能改、能确认的中间产物
# ---------------------------------------------------------------
def _review_project(client) -> str:
    """建一个「文献综述 + 已上传并解析一篇文献」的项目。"""
    pid = client.post("/api/projects", json={}).json()["id"]
    client.post(f"/api/projects/{pid}/topic", json={
        "paper_type": "文献综述", "target_words": 1000, "topic": "t",
    })
    pdf = _make_pdf(["Doc: theme", "Author: Li, 2022", "Body."])
    _upload_documents(client, pid, files=[("files", ("a.pdf", pdf, "application/pdf"))])
    return pid


def _doc_ids(client, pid) -> list[str]:
    return [d["id"] for d in client.get(f"/api/projects/{pid}/documents").json()]


def test_clusters_require_literature_first_type(tmp_db):
    """只有文献综述需要主题聚类：其余类型的结构不由文献决定。

    对它们开放这个入口会产出一份谁也不读的聚类，而用户会以为大纲按它组织过。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _design_project(client, "定量/实证研究")
        r = client.post(f"/api/projects/{pid}/clusters/generate")
        assert r.status_code == 400
        assert "聚类" in r.json()["detail"]
        # 确认端点也要挡：前端可能留着一个过期的项目视图
        r = client.post(f"/api/projects/{pid}/clusters/confirm", json={"clusters": []})
        assert r.status_code == 400


def test_clusters_require_documents(tmp_db):
    """零文献时聚类无从谈起 —— 与「文献综述先传文献再生成大纲」是同一道闸。"""
    from app.main import app

    with TestClient(app) as client:
        pid = _design_project(client, "文献综述")
        r = client.post(f"/api/projects/{pid}/clusters/generate")
        assert r.status_code == 400
        assert "文献" in r.json()["detail"]


def test_clusters_require_llm(tmp_db):
    """未配 LLM 时明确报错，而不是跑一圈回来给一份空聚类。"""
    from app.main import app

    with TestClient(app) as client:
        pid = _review_project(client)
        r = client.post(f"/api/projects/{pid}/clusters/generate")
        assert r.status_code == 400
        assert "LLM" in r.json()["detail"]


def test_clusters_generate_rejected_while_task_running(tmp_db, monkeypatch):
    """聚类跑着的时候不许再点一次。

    它与大纲、解析共用 task_state_json，并跑会让进度文案互相覆盖；更糟的是大纲会
    读到一份写到一半的聚类。
    """
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = _review_project(client)
        _enable_llm(monkeypatch, {})

        class _FakeTask:
            def done(self) -> bool:
                return False

        key = f"clusters:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/clusters/generate")
            assert r.status_code == 409
            assert "聚类" in r.json()["detail"]
        finally:
            projects_router.tasks._TASKS.pop(key, None)


# ---------------------------------------------------------------
# 引用改动：精确诊断（refs_change）与两个相称的补救动作
# ---------------------------------------------------------------
def _wait_generation(client, pid) -> dict:
    """等正文生成跑完，返回终态进度。"""
    import time

    state: dict = {}
    for _ in range(300):
        state = client.get(f"/api/projects/{pid}/generate/progress").json()
        if state["status"] != "running":
            break
        time.sleep(0.02)
    assert state.get("status") in ("done", "error"), state
    return state


def _generated(client, pid, **settings) -> dict:
    """把项目推到「引用已确认 + 正文已生成」，返回落库后的项目。"""
    body = {"binding": client.post(f"/api/projects/{pid}/citations/schedule").json()["binding"],
            "cite_style": "bracket", "citation_format": "gb7714"}
    body.update(settings)
    assert client.post(f"/api/projects/{pid}/citations/confirm", json=body).status_code == 200
    assert client.post(f"/api/projects/{pid}/generate").status_code == 200
    assert _wait_generation(client, pid)["status"] == "done"
    return client.get(f"/api/projects/{pid}").json()


def _change_of(client, pid, **settings) -> dict:
    """改一次引用设置，从写入接口的回执里读它报出的 refs_change。"""
    body = {"binding": client.get(f"/api/projects/{pid}").json()["citation_binding_json"],
            "cite_style": "bracket", "citation_format": "gb7714"}
    body.update(settings)
    r = client.post(f"/api/projects/{pid}/citations/confirm", json=body)
    assert r.status_code == 200, r.text
    return r.json()["refs_change"]


def test_refs_change_distinguishes_render_from_numbering(tmp_db):
    """改了什么就说什么 —— 三类改动给出三类事实，各自对应一个相称的出路。

    此前只有一句「正文与当前引用设置不一致」，出路只有一条（回选题重做一遍）。判据
    (_refs_key) 一直分辨得出这两种：**编号与指向变没变**。分不开的代价是让用户为改一个
    下拉框赔上大纲、引用绑定、设计、材料计划。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        _add_document(client, pid)  # 两篇文献，等下才能「换归属而不换成员」
        proj = _generated(client, pid)
        assert proj["refs_change"]["kind"] == "none", "刚生成完，什么都对得上"

        # 一、只改参考文献列表格式 → 只有渲染变了
        change = _change_of(client, pid, citation_format="apa")
        assert change["kind"] == "render"
        assert change["style_changed"] is False, "角标样式没动，动的是列表格式"
        assert change["added_count"] == 0 and change["removed_count"] == 0
        assert "参考文献列表格式" in change["summary"]

        # 二、只改角标样式 → 同样是「只有渲染变了」，但有一处重排修不掉的残留要说清
        change = _change_of(client, pid, citation_format="apa", cite_style="superscript")
        assert change["kind"] == "render"
        assert change["style_changed"] is True
        assert "角标" in change["summary"]

        # 三、绑定里少了一篇 → 成员变了：编号整体前移，正文角标会指错文献。
        #     （删文献那条路整份绑定会作废，报出来的是「全少」；由
        #      test_generate_refused_after_bound_document_deleted 覆盖。）
        docs = client.get(f"/api/projects/{pid}/documents").json()
        assert len(docs) == 2
        keys = list(proj["citation_binding_json"])
        kept = [it for it in proj["citation_binding_json"][keys[0]]
                if it["doc_id"] == docs[0]["id"]]
        assert kept, "第 1 节原本就绑着第 1 篇（均匀打散按章节顺序铺开）"
        trimmed = {k: (kept if k == keys[0] else []) for k in keys}
        change = _change_of(client, pid, binding=trimmed)
        assert change["kind"] == "numbering"
        assert change["removed_count"] == 1 and change["added_count"] == 0
        assert "少了 1 篇" in change["summary"]

        # 四、还是那几篇、只把归属换了 → 成员一个不多不少，编号却整体挪位。
        #     这是按下「执行引用调度」之后最常见的形态，必须单独有一句，不能说成「变了 0 篇」。
        #     归属项**原样搬**（不改标题、不改页码），换的只有它待在哪一节。
        orig = proj["citation_binding_json"]
        rotated = {k: [] for k in keys}
        for i in range(len(docs)):
            rotated[keys[i]] = list(orig[keys[(i + 1) % len(docs)]])
        change = _change_of(client, pid, binding=rotated)
        assert change["kind"] == "numbering"
        assert change["added_count"] == 0 and change["removed_count"] == 0
        assert "章节归属" in change["summary"] or "编号顺序" in change["summary"]


def test_rerender_references_is_allowed_only_when_only_rendering_changed(tmp_db):
    """「按当前设置重排文末列表」只对**只有排版变了**这一种情形开放。

    准入门槛就是安全性本身：编号变过时重排，等于亲手做出「正文角标指着 A、文末列表
    第 3 条写着 B」这种静默错配 —— 那正是生成入口那条拒绝守着的底线。所以重排这条路
    必须先判再写，不能从按钮这边绕过去。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        proj = _generated(client, pid)

        # 已经一致 → 无需重排（不写、不改 updated_at）
        r = client.post(f"/api/projects/{pid}/references/rerender")
        assert r.status_code == 400
        assert "无需重排" in r.json()["detail"]

        # 只改列表格式：重排 → 立刻一致，且正文**逐字节不变**
        _change_of(client, pid, citation_format="apa")
        body_before = client.get(f"/api/projects/{pid}").json()
        stale_formatted = body_before["references_json"][0]["formatted"]
        r = client.post(f"/api/projects/{pid}/references/rerender")
        assert r.status_code == 200, r.text
        assert r.json()["citations_stale"] is False
        assert r.json()["refs_change"]["kind"] == "none"

        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["sections_json"] == body_before["sections_json"], \
            "重排只动文末列表，正文一个字都不该变"
        assert proj["references_json"] != body_before["references_json"], \
            "真的换成了新格式，不是原样写回"
        assert proj["references_json"][0]["formatted"] != stale_formatted

        # 动了编号 → 拒绝，并指向唯一正确的出路
        assert client.post(
            f"/api/projects/{pid}/citations/confirm", json={"binding": {}}
        ).status_code == 200
        r = client.post(f"/api/projects/{pid}/references/rerender")
        assert r.status_code == 400
        assert "必须重写正文" in r.json()["detail"]
        assert client.get(f"/api/projects/{pid}").json()["citations_stale"] is True, \
            "被拒的重排不许留下任何痕迹"


def test_reference_runs_are_served_on_both_bodies_and_pair_with_the_string(tmp_db):
    """文末列表的**片段形态**（给 docx 排斜体）两条下发路径都要有，且与字符串那半逐条对得上。

    `referenceRuns` 是 `GET /projects/{id}`（详情页 / 启动恢复）与
    `GET /projects/{id}/generate/progress`（生成完那一下）各自**现算**的派生字段。两处
    任一漏挂，那条路上的 docx 就静默没有斜体，而刷新一下再导出又有了 —— 最难查的那种
    半生效。这里钉的不是"哪个字斜体"（那是 `test_citation_format.py` 那组用例的活），
    而是三件结构性事实：两条路都带它、逐条 `"".join(文本) == formatted`、片段只有
    `{text, italic}` 两个键。最后再用一条手填条目验一次真的会斜。
    """
    from app.main import app

    with TestClient(app) as client:
        # 还没有文末列表时也要有 `referenceRuns` 这个键，且**恒是数组**。
        # 注意它与 `references_json` 不同形：后者是库里那一列，草稿项目上就是 NULL；
        # `referenceRuns` 是现算的，算不出来的情形一律给 `[]`。前端按
        # `Array.isArray(x) && x.length > 0` 判，两种都给得动，但这里把差异钉住 ——
        # 哪天有人为了"同形"把它改成 None，前端那条兜底判据就得跟着改。
        pid = client.post("/api/projects", json={}).json()["id"]
        empty = client.get(f"/api/projects/{pid}").json()
        assert empty["references_json"] is None, "还没有正文，快照那一列就是 NULL"
        assert empty["referenceRuns"] == [], "现算字段恒定是数组，不是 None"

        pid = _doc_project(client)
        doc_id = client.get(f"/api/projects/{pid}/documents").json()[0]["id"]
        # 手填一条**虚构**的期刊条目（题目/年份/刊名全空时测不到斜体这条派生链）。
        # page_range 是 PATCH 那边的键名，ref 里换叫 pages（见 _plan_citations）。
        r = client.patch(f"/api/projects/{pid}/documents/{doc_id}", json={
            "authors": "张三", "year": "2023", "source": "教育研究",
            "volume": "32", "issue": "1", "page_range": "56-75", "source_type": "J",
        })
        assert r.status_code == 200, r.text

        proj = _generated(client, pid, citation_format="apa")

        # 一、详情页那条路：与 references_json 逐条配对
        assert proj["references_json"], "生成完必须落下文末列表"
        assert len(proj["referenceRuns"]) == len(proj["references_json"]), \
            "两条一一对应，不许少一条（少一条就是某条文献静默丢掉斜体）"
        for ref, runs in zip(proj["references_json"], proj["referenceRuns"]):
            assert runs, "每一条至少有一个片段"
            assert {k for r_ in runs for k in r_} == {"text", "italic"}, \
                "片段只有这两个键：多一个键前端读不懂，少一个键渲染不出来"
            joined = "".join(r_["text"] for r_ in runs)
            assert joined == ref["formatted"], \
                "片段文本的拼接必须**就是** formatted（同一个渲染器的结构化形式）"

        # 二、进度那条路：与 references 逐条配对（references 就是 references_json 整条）
        prog = client.get(f"/api/projects/{pid}/generate/progress").json()
        assert prog["status"] == "done"
        assert len(prog["referenceRuns"]) == len(prog["references"])
        for ref, runs in zip(prog["references"], prog["referenceRuns"]):
            assert "".join(r_["text"] for r_ in runs) == ref["formatted"]
        # 两条路各算各的，输入相同 → 输出必须相同（现算字段没有第二份真相）
        assert prog["referenceRuns"] == proj["referenceRuns"]

        # 三、真的会斜：APA 7 §9.34 的斜体范围是刊名 + 卷号，**含中间那个逗号**；
        #     期号与页码正体。整条都斜、或只斜刊名不带卷号，都在这里被点名。
        apa_runs = proj["referenceRuns"][0]
        assert [r_["text"] for r_ in apa_runs if r_["italic"]] == ["教育研究", ", 32"], \
            "斜体范围不是「刊名 + 卷号（含逗号）」"
        assert not [r_ for r_ in apa_runs if r_["italic"] and r_["text"] == ""], \
            "空的斜体片段是多余的，前端会多发一个空 run"

        # 四、切回 GB/T 7714 → 一律正体（该标准用类型方括号区分文献类型，不用斜体）。
        #     走真路由（顺带把 citations_stale 立起来），不是直接改库。
        _change_of(client, pid, citation_format="gb7714")
        gb_runs = client.get(f"/api/projects/{pid}").json()["referenceRuns"][0]
        gb_text = "".join(r_["text"] for r_ in gb_runs)
        assert "[J]" in gb_text, "先确认真换成了 GB/T 那条分支，否则「不斜体」可能是没生效"
        assert not any(r_["italic"] for r_ in gb_runs), "GB/T 7714 一律正体"


def test_reset_sections_discards_body_only(tmp_db, monkeypatch):
    """作废正文只作废正文：大纲、绑定、设计、材料计划、文献全部保留。

    这条替代了「回选题重做一遍」那条路 —— 它把四份下游产物一起覆盖式作废，与「改了个
    引用设置」完全不相称。同时补上一个此前不可达的诉求：想重写一版正文。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _design_project(client)  # 定量/实证研究：有设计步
        client.post(f"/api/projects/{pid}/design",
                    json={"fields": {"数据源与样本": "312 份问卷"}})
        outline = _generate_outline(client, pid)
        client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        section_title = outline["chapters"][0]["sections"][0]["title"]
        mid = client.post(f"/api/projects/{pid}/materials/text",
                          json={"text": "样本 312 份"}).json()[0]["id"]
        _enable_llm(monkeypatch, {
            "notes": "数据放正文",
            "placements": [{"section_title": section_title, "material_ids": [mid],
                            "usage": "用样本量说明现状", "key_points": ["312 份"]}],
        })
        assert client.post(f"/api/projects/{pid}/materials/analyze").status_code == 200
        assert _wait_task(client, pid)["status"] == "done"
        _disable_llm(monkeypatch)
        _add_document(client, pid)

        before = _generated(client, pid)
        assert before["status"] == "completed"
        assert before["materials_plan_json"] and before["design_json"] and before["sections_json"]
        docs_before = client.get(f"/api/projects/{pid}/documents").json()

        r = client.post(f"/api/projects/{pid}/sections/reset")
        assert r.status_code == 200, r.text
        assert r.json()["citations_stale"] is False
        assert r.json()["refs_change"]["kind"] == "none"

        proj = client.get(f"/api/projects/{pid}").json()
        assert proj["status"] == "citation_confirmed", "已过引用确认点、还没有正文"
        assert proj["sections_json"] is None
        assert proj["references_json"] is None, "基线随正文一起没了（否则下次续写会拿它比对）"
        assert proj["generation_state_json"] is None
        for key in ("outline_json", "citation_binding_json", "materials_plan_json",
                    "design_json", "topic", "paper_type"):
            assert proj[key] == before[key], f"作废正文不该动 {key}"
        assert client.get(f"/api/projects/{pid}/documents").json() == docs_before
        assert proj["unbound_documents"] == 0, "绑定还在，库里每篇仍被覆盖"

        # 随后能直接重写正文：清空正文正好落进「整篇重写」那条早退分支
        assert client.post(f"/api/projects/{pid}/generate").status_code == 200
        assert _wait_generation(client, pid)["status"] == "done"


def test_reset_sections_guards(tmp_db):
    """空正文时 400（没有可作废的东西），生成中 409（两套忙闲登记都要查）。"""
    from app.main import app
    from app.routers import projects as projects_router

    with TestClient(app) as client:
        pid = _doc_project(client)
        r = client.post(f"/api/projects/{pid}/sections/reset")
        assert r.status_code == 400
        assert "没有已生成的正文" in r.json()["detail"]

        assert client.post(f"/api/projects/{pid}/citations/schedule").status_code == 200
        assert client.post(f"/api/projects/{pid}/citations/confirm",
                           json={"binding": {}}).status_code == 200
        assert client.post(f"/api/projects/{pid}/generate").status_code == 200
        _wait_generation(client, pid)

        class _FakeTask:
            def done(self) -> bool:
                return False

        projects_router._GEN_TASKS[pid] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/sections/reset")
            assert r.status_code == 409
            assert "生成正文" in r.json()["detail"]
        finally:
            projects_router._GEN_TASKS.pop(pid, None)

        assert client.get(f"/api/projects/{pid}").json()["sections_json"], \
            "被拒的作废不许留下任何痕迹"


def test_schedule_reports_method_and_falls_back_offline(tmp_db, monkeypatch):
    """调度如实回报绑定是怎么来的：模型判的，还是确定性兜底按上传顺序铺的。

    未配置模型时（默认离线）必须仍然给出完整绑定 —— 那是「不配置模型也能跑完整条流程」
    的保障；模型可用时按相关性归属，并把模型给的理由带进绑定（界面上唯一能说明「为什么
    绑在这里」的信息）。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        body = client.post(f"/api/projects/{pid}/citations/schedule").json()
        assert body["method"] == "uniform"
        keys = list(body["binding"])
        assert all(body["binding"][k] for k in keys), "兜底也保证每节都有引用"

        docs = _add_document(client, pid, name="h.pdf", text="Second doc.")
        assert len(docs) == 2
        _enable_llm(monkeypatch, {"assignments": [
            {"doc_id": docs[0]["id"], "section": keys[0], "reason": "讲的是背景"},
            {"doc_id": docs[1]["id"], "section": keys[2], "reason": "讲的是对策"},
        ]})
        body = client.post(f"/api/projects/{pid}/citations/schedule").json()

        assert body["method"] == "relevance"
        assert [it["doc_id"] for it in body["binding"][keys[0]]] == [docs[0]["id"]]
        assert body["binding"][keys[0]][0]["reason"] == "讲的是背景"
        assert body["unbound_documents"] == 0, "模型没归到的那篇由兜底补上（无遗漏）"
        assert all(body["binding"][k] for k in keys)


def test_schedule_falls_back_when_model_judgement_is_unusable(tmp_db, monkeypatch):
    """模型给了判断、但一条都不可用（编造的 id / 章节）时，退回确定性兜底。

    这是最要紧的一条降级路径：半可信的判断比没有判断更危险 —— 界面上「已按相关性调度」
    与真按相关性调度长得一模一样。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        _enable_llm(monkeypatch, {"assignments": [
            {"doc_id": "编造的", "section": "不存在的章节", "reason": "幻觉"},
        ]})
        body = client.post(f"/api/projects/{pid}/citations/schedule").json()
        assert body["method"] == "uniform"
        assert body["unbound_documents"] == 0
        assert all(body["binding"][k] for k in body["binding"])


def test_clusters_generate_then_confirm(tmp_db, monkeypatch):
    """生成 → 落库但未确认 → 作者改名确认 → 确认时间落库。

    「生成」与「确认」必须是两个时刻：聚类决定章节结构，模型给的初稿不能自动生效。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _review_project(client)
        doc_id = _doc_ids(client, pid)[0]
        doc_title = client.get(f"/api/projects/{pid}/documents").json()[0]["title"]
        _enable_llm(monkeypatch, {"notes": "整体两极", "clusters": [
            {"title": "模型给的主题", "doc_ids": [doc_id], "summary": "s",
             "key_points": ["p"]},
            {"title": "编造的主题", "doc_ids": ["ghost"], "key_points": []},
        ]})

        r = client.post(f"/api/projects/{pid}/clusters/generate")
        assert r.status_code == 200 and r.json()["status"] == "running"
        task = _wait_task(client, pid)
        assert task["status"] == "done", task
        assert task["kind"] == "clusters"

        stored = client.get(f"/api/projects/{pid}").json()["clusters_json"]
        assert stored["confirmed_at"] is None, "生成不等于确认"
        # 编造 id 的簇在生成侧就被丢了，不会走到确认面板上
        assert [c["title"] for c in stored["clusters"]] == ["模型给的主题"]
        assert stored["clusters"][0]["doc_titles"] == [doc_title]
        assert stored["notes"] == "整体两极"

        r = client.post(f"/api/projects/{pid}/clusters/confirm", json={
            "clusters": [{"title": "作者改过的主题", "doc_ids": [doc_id],
                          "summary": "s2", "key_points": ["p2"]}],
            "notes": "作者的话",
        })
        assert r.status_code == 200, r.text
        res = r.json()
        assert res["ok"] is True
        assert res["cluster_count"] == 1
        assert res["outline_stale"] is False, "还没有大纲，谈不上失效"
        assert res["theme_sections"] == 3, "骨架里主题脉络章有几节要如实告诉前端"

        stored = client.get(f"/api/projects/{pid}").json()["clusters_json"]
        assert stored["clusters"][0]["title"] == "作者改过的主题"
        assert stored["notes"] == "作者的话"
        assert stored["confirmed_at"], "确认时间必须落库，界面据此区分两类状态"


def test_clusters_confirm_reports_stale_outline_without_touching_it(tmp_db):
    """已有大纲时只回一句 outline_stale，**不动大纲**。

    与「设计未填告警放行」同一条原则：护栏该拦退化，不该拦用户已被告知后果的选择。
    重新生成大纲要花几十秒，是否值得由用户判断。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _review_project(client)
        doc_id = _doc_ids(client, pid)[0]
        outline = _generate_outline(client, pid)

        r = client.post(f"/api/projects/{pid}/clusters/confirm", json={
            "clusters": [{"title": "主题", "doc_ids": [doc_id]}],
        })
        assert r.status_code == 200, r.text
        assert r.json()["outline_stale"] is True
        assert client.get(f"/api/projects/{pid}").json()["outline_json"] == outline


def test_clusters_confirm_drops_clusters_whose_documents_are_gone(tmp_db):
    """确认时再白名单一遍 doc_ids：聚类生成之后作者可能删过文献。

    不筛的后果不是报错，而是静默：大纲会照着一个已经不存在的主题去组织章节。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _review_project(client)
        pdf = _make_pdf(["Doc: another", "Author: Wang, 2021", "Body."])
        _upload_documents(client, pid, files=[("files", ("b.pdf", pdf, "application/pdf"))])
        first, second = _doc_ids(client, pid)[:2]

        # 只引用已删文献的簇被丢弃，其余照常保存
        client.delete(f"/api/projects/{pid}/documents/{first}")
        r = client.post(f"/api/projects/{pid}/clusters/confirm", json={"clusters": [
            {"title": "已失效的主题", "doc_ids": [first]},
            {"title": "仍然有效的主题", "doc_ids": [second]},
        ]})
        assert r.status_code == 200, r.text
        assert r.json()["dropped"] == 1
        assert r.json()["cluster_count"] == 1
        stored = client.get(f"/api/projects/{pid}").json()["clusters_json"]
        assert [c["title"] for c in stored["clusters"]] == ["仍然有效的主题"]

        # 一个都不剩时拒绝，而不是写一份空聚类装作确认成功
        r = client.post(f"/api/projects/{pid}/clusters/confirm", json={"clusters": [
            {"title": "已失效的主题", "doc_ids": [first]},
        ]})
        assert r.status_code == 400
        assert "重新生成" in r.json()["detail"]


def test_clusters_confirm_rejects_empty_clusters(tmp_db):
    """空列表不是「确认了零个主题」，而是没什么可确认的。"""
    from app.main import app

    with TestClient(app) as client:
        pid = _review_project(client)
        r = client.post(f"/api/projects/{pid}/clusters/confirm", json={"clusters": []})
        assert r.status_code == 400
        assert client.get(f"/api/projects/{pid}").json()["clusters_json"] is None


def test_document_change_invalidates_clusters(tmp_db, monkeypatch):
    """文献集合一变，旧聚类就作废 —— 新增与删除都算。

    聚类是「对这批文献的归纳」：漏了新增的、或还引用着已删的，它就不再完整。
    与材料计划失效同理：宁可让用户重跑一次，也不要留一份看着完整的旧产物。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _review_project(client)
        doc_id = _doc_ids(client, pid)[0]
        _enable_llm(monkeypatch, {"clusters": [{"title": "主题", "doc_ids": [doc_id]}]})

        client.post(f"/api/projects/{pid}/clusters/generate")
        assert _wait_task(client, pid)["status"] == "done"
        client.post(f"/api/projects/{pid}/clusters/confirm",
                    json={"clusters": [{"title": "主题", "doc_ids": [doc_id]}]})
        assert client.get(f"/api/projects/{pid}").json()["clusters_json"]["confirmed_at"]

        # 新增一篇：旧聚类没覆盖它
        pdf = _make_pdf(["Doc: more", "Author: Zhao, 2020", "Body."])
        _upload_documents(client, pid, files=[("files", ("b.pdf", pdf, "application/pdf"))])
        assert client.get(f"/api/projects/{pid}").json()["clusters_json"] is None

        # 再确认一次，然后删掉被引用的那篇
        client.post(f"/api/projects/{pid}/clusters/confirm",
                    json={"clusters": [{"title": "主题", "doc_ids": [doc_id]}]})
        r = client.delete(f"/api/projects/{pid}/documents/{doc_id}")
        assert r.json()["clusters_invalidated"] is True
        assert client.get(f"/api/projects/{pid}").json()["clusters_json"] is None


def test_confirmed_clusters_reach_the_outline_prompt(tmp_db, monkeypatch):
    """确认过的聚类要真的进大纲提示词，且只进第一遍。

    这条用例串的是产物的一生：生成 → 确认 → 大纲。前面那些用例各自只钉住其中一段，
    「确认了却没进提示词」这种断链只有端到端才照得出来。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _review_project(client)
        doc_id = _doc_ids(client, pid)[0]
        calls = _capture_llm(monkeypatch, {"clusters": [
            {"title": "促进与抑制效应并存", "doc_ids": [doc_id], "key_points": ["p"]},
        ]})

        client.post(f"/api/projects/{pid}/clusters/generate")
        assert _wait_task(client, pid)["status"] == "done"
        client.post(f"/api/projects/{pid}/clusters/confirm",
                    json={"clusters": [{"title": "促进与抑制效应并存",
                                        "doc_ids": [doc_id]}]})

        _generate_outline(client, pid)

        # 第一次是聚类自己那遍（它发出去的是文献清单，收到的才是簇标题），
        # 其后两遍是大纲的结构与字数。
        assert "主题分析专家" in calls[0]
        assert len(calls) >= 3, f"大纲应当跑两遍，实际收到 {len(calls)} 次调用"
        assert "作者已确认的主题聚类" in calls[1]
        assert "促进与抑制效应并存" in calls[1]
        assert "促进与抑制效应并存" not in calls[2]


def test_mark_exported_writes_the_terminal_status(tmp_db):
    """导出回执把状态推到 exported —— 这个终态此前**只有读者、没有作者**。

    db.ProjectStatus.EXPORTED 定义在枚举里、_CITATION_EDITABLE / _GENERATABLE 里都
    列着、前端还给它在步骤条上锚了一格，但全仓没有一个地方写它：状态机上一个永远
    到不了的终态，在界面上就是一格永远点不亮的格子。

    同时钉住幂等：换个格式再导一次是常态，第二次若报错，用户只会以为第一次没导出去。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        p = _generated(client, pid)
        assert p["status"] == "completed"

        r = client.post(f"/api/projects/{pid}/export")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "exported", "回执要能当场告诉前端新的状态"
        assert client.get(f"/api/projects/{pid}").json()["status"] == "exported"

        r = client.post(f"/api/projects/{pid}/export")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "exported"

        # 终态不是死路：exported 本来就在 _CITATION_EDITABLE 里，改引用设置只改设置、
        # 不把状态打回上游（_status_after_change），所以它仍然停在 exported。
        assert client.post(f"/api/projects/{pid}/citations/confirm",
                           json={"binding": {}}).status_code == 200
        assert client.get(f"/api/projects/{pid}").json()["status"] == "exported"


def test_confirm_outline_does_not_regress_done_status(tmp_db):
    """completed / exported 项目回大纲步重看大纲，状态不许被打回 outline_confirmed。

    这是 D1 那条缺陷：confirm_outline 曾无条件把 status 写成 outline_confirmed，
    于是正文已经生成完（甚至已导出）的项目只要回大纲步点一次「确认大纲」，导出
    入口就会被一句与事实相反的「正文还没有生成完成」挡住 —— 一份明明在库里的
    成稿，看起来却像还没生成。修复后它与引用三个入口共用同一条「终态不回退」的
    规则（_status_after_change）：已完成的项目只改大纲与作废下游、状态停在原处。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        p = _generated(client, pid)
        assert p["status"] == "completed"

        outline = client.get(f"/api/projects/{pid}").json()["outline_json"]
        r = client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        assert r.status_code == 200, r.text
        assert client.get(f"/api/projects/{pid}").json()["status"] == "completed"

        client.post(f"/api/projects/{pid}/export")
        assert client.get(f"/api/projects/{pid}").json()["status"] == "exported"
        r = client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        assert r.status_code == 200, r.text
        assert client.get(f"/api/projects/{pid}").json()["status"] == "exported"


def test_upload_all_duplicate_does_not_regress_done_status(tmp_db):
    """completed / exported 项目上传的全是重复文献时，状态不许被打回「文献注入」。

    这是 D3 那条缺陷：_run_parse 曾在 saved == 0（一篇都没入库，全重复/全失败）时
    照样写 status=RESOURCES_LOADING，把已成稿的项目推回「文献注入」，导出随即被
    「正文还没有生成完成」这句与事实相反的假理由挡住。文献集合既然没变，库里的
    聚类与正文都还覆盖得住，什么都不能写 —— 尤其不能动 status。任务仍落 done，
    saved=0 由 extra 报给前端。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        _generated(client, pid)
        assert client.get(f"/api/projects/{pid}").json()["status"] == "completed"

        # 同一篇文献换文件名重传：内容相同 → 判重跳过，saved == 0
        pdf = _make_pdf(["Doc content."])
        task, _ = _upload_documents(
            client, pid, files=[("files", ("d-renamed.pdf", pdf, "application/pdf"))])
        assert task["status"] == "done", task
        assert task["saved"] == 0
        assert client.get(f"/api/projects/{pid}").json()["status"] == "completed"

        client.post(f"/api/projects/{pid}/export")
        assert client.get(f"/api/projects/{pid}").json()["status"] == "exported"
        task, _ = _upload_documents(
            client, pid, files=[("files", ("d-again.pdf", pdf, "application/pdf"))])
        assert task["saved"] == 0
        assert client.get(f"/api/projects/{pid}").json()["status"] == "exported"


def test_mark_exported_guards(tmp_db):
    """404 / 400 / 两道忙闲各一条，且被拒的请求不留痕、守卫不变成锁。"""
    from app import db
    from app.main import app
    from app.routers import projects as projects_router

    class _FakeTask:
        def done(self) -> bool:
            return False

    with TestClient(app) as client:
        assert client.post("/api/projects/nosuchproject/export").status_code == 404

        pid = _doc_project(client)  # 大纲已确认、文献也解析过了，但正文还没生成
        before = client.get(f"/api/projects/{pid}").json()["status"]
        r = client.post(f"/api/projects/{pid}/export")
        assert r.status_code == 400
        assert "正文还没有生成完成" in r.json()["detail"]
        assert client.get(f"/api/projects/{pid}").json()["status"] == before, \
            "被拒的这一次不许改状态"

        # 「completed 但没有正文」也要拒：库里造得出这种行（作废过正文又手工改回状态、
        # 或 reset_stale_generating 的中间态），而「已导出」在这时是一句假话。
        db.update_project(pid, status=db.ProjectStatus.COMPLETED)
        assert client.post(f"/api/projects/{pid}/export").status_code == 400

        # 两道闸都要查：_busy_task 那一侧（大纲在跑）与 _gen_running 那一侧（正文生成）
        db.update_project(pid, status=db.ProjectStatus.COMPLETED,
                          sections_json=[{"section_title": "绪论", "content": "正文"}])
        key = f"outline:{pid}"
        projects_router.tasks._TASKS[key] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/export")
            assert r.status_code == 409
            assert "大纲正在生成中" in r.json()["detail"]
        finally:
            projects_router.tasks._TASKS.pop(key, None)

        projects_router._GEN_TASKS[pid] = _FakeTask()
        try:
            r = client.post(f"/api/projects/{pid}/export")
            assert r.status_code == 409
            assert "生成正文" in r.json()["detail"]
        finally:
            projects_router._GEN_TASKS.pop(pid, None)

        assert client.get(f"/api/projects/{pid}").json()["status"] == "completed", \
            "被拒的三次不许留下任何痕迹"
        # 守卫不能变成锁：两道闸都空出来之后，同一个动作必须能正常走完
        assert client.post(f"/api/projects/{pid}/export").status_code == 200
        assert client.get(f"/api/projects/{pid}").json()["status"] == "exported"


# ---------------------------------------------------------------
# 畸形绑定不能让项目此后每次读取都 500
# ---------------------------------------------------------------
#: 读不懂的六种形状。前四种来自「值不是条目数组」，后两种来自「条目不是对象」
#: 或 key 不是字符串 —— CitationConfirm.binding 只由 pydantic 保证最外层是 dict，
#: 内层结构完全自由，这几种都进得来。
_MALFORMED_BINDINGS = [
    {"1.1 研究背景": None},
    {"1.1 研究背景": 5},
    {"1.1 研究背景": "不是数组"},
    {"1.1 研究背景": [None]},
    {"1.1 研究背景": [1, "x", []]},
    {"1.1 研究背景": [{"doc_id": None}, {"doc_id": ""}]},
]


@pytest.mark.parametrize("binding", _MALFORMED_BINDINGS)
def test_malformed_binding_does_not_poison_every_later_read(tmp_db, binding):
    """畸形绑定落库之后，**每一次**读取都必须照常返回。

    这条与 A15 在台账里的定性一致：绑定是先落库、后算派生事实的，所以一处疏忽的
    代价不是一次 500，而是这个项目此后每次 GET 详情都 500 —— 用户连界面都打不开，
    也就没有任何办法把它改回来。原先 `_plan_citations` 直接在条目上 .get。

    判据按「安全的退化方向」：读不懂的条目一律跳过（少算一篇只会让文末列表少一条、
    界面请用户重排一次，而重排本就会用一份合法绑定覆盖掉它）。所以这里既断言不 500，
    也断言派生字段照常算出来。
    """
    from app import db as db_module
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        # 让 _refs_diff 不再短路：有正文 + 有基线，才会走到 _plan_citations
        db_module.update_project(
            pid,
            sections_json=[{"title": "1.1 研究背景", "content": "正文", "word_budget": 500}],
            references_json=[],
        )
        # 直接落库，模拟「这份绑定已经在库里」（写入路径见下面那条用例）
        db_module.update_project(pid, citation_binding_json=binding)

        r = client.get(f"/api/projects/{pid}")
        assert r.status_code == 200, r.text
        body = r.json()
        # 三个派生事实一个不少 —— 只是按「读不懂的当作没绑」算
        assert body["unbound_documents"] == 1
        assert body["citations_stale"] is False
        assert body["refs_change"]["kind"] == "none"

        # 写入路径同样不许 500，且落库后读取仍然正常
        r = client.post(f"/api/projects/{pid}/citations/confirm", json={"binding": binding})
        assert r.status_code == 200, r.text
        assert client.get(f"/api/projects/{pid}").status_code == 200


def test_readable_entries_survive_alongside_the_unreadable(tmp_db):
    """上面那组「跳过读不懂的」不能顺手把读得懂的也跳过。

    形状防御最容易犯的错是收得太紧：整份绑定被当成畸形，于是文末列表凭空变空、
    界面请用户重排 —— 而这份绑定其实是好的。这里直接盯住编号分配那一步。
    """
    import app.routers.projects as projects_router
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        doc = client.get(f"/api/projects/{pid}/documents").json()[0]

    sections = [{"title": "1.1 研究背景"}, {"title": "1.2 研究问题"}]
    doc_map = {doc["id"]: doc}
    binding = {
        "1.1 研究背景": [
            {"doc_id": doc["id"], "doc_title": doc["title"], "page": 1},
            None,          # 混进一条读不懂的，不许连累旁边那条
        ],
        "1.2 研究问题": 5,  # 这一章的值整个读不懂：跳过它，别抛
    }

    num_by_doc, ref_list = projects_router._plan_citations(sections, binding, doc_map)

    assert [r["doc_id"] for r in ref_list] == [doc["id"]]
    assert [r["num"] for r in ref_list] == [1]
    assert num_by_doc == {doc["id"]: 1}


# ---------------------------------------------------------------
# 「进来了，但有几页没提取到文本」必须说出来
# ---------------------------------------------------------------
def test_partial_page_failure_is_reported(tmp_db, monkeypatch):
    """「解析成功」不许盖住「缺了几页」。

    逐页提取失败原先被 pdf_parser 静默吞掉，而调用方只检查「所有页都空」—— 于是
    「10 页里 3 页解析失败」在两边看来都与完全成功一模一样。文献确实在库里，可它的
    页码索引在缺的那几页上是空的，而正文的角标与页码就取自这份索引；等引用调度那步
    才发现缺页，原因已经查不回来了。
    """
    import app.routers.projects as projects_router
    from app.main import app
    from app.routers import projects_documents as documents_router

    def fake_extract(_data):
        return [
            {"page": 1, "text": "第一页", "failed": False},
            {"page": 2, "text": "", "failed": True},
            {"page": 3, "text": "第三页", "failed": False},
        ]

    monkeypatch.setattr(documents_router, "extract_pdf_text", fake_extract)

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        pdf = _make_pdf(["x"])
        task, docs = _upload_documents(
            client, pid, files=[("files", ("d.pdf", pdf, "application/pdf"))]
        )

        assert task["status"] == "done", task
        assert task["saved"] == 1
        assert task["failed"] == []
        assert task["skipped"] == []
        # 计数进任务文案（界面直接显示这一行），点名到页进 extra（界面拼进提示条）
        assert task["partial"] == [
            {"filename": "d.pdf", "reason": "第 2 页文本提取失败"}
        ]
        assert "1 篇有页面未提取到" in task["message"]
        # 文件本身照常入库；缺的那页在页码索引里就是空片段，不编一个假的补上
        assert docs[0]["pages"] == [
            {"page": 1, "snippet": "第一页"},
            {"page": 2, "snippet": ""},
            {"page": 3, "snippet": "第三页"},
        ]


def test_clean_parse_reports_no_partial(tmp_db):
    """完全成功时不许出现这条提示 —— 假警告会让人学会忽略真警告。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        task, _ = _upload_documents(
            client, pid,
            files=[("files", ("d.pdf", _make_pdf(["Doc content."]), "application/pdf"))],
        )
        assert task["partial"] == []
        assert "未提取到" not in task["message"]


def test_all_pages_failing_says_so_instead_of_blaming_the_scan(tmp_db, monkeypatch):
    """每一页都提取失败 ≠ 扫描版。

    两种成因都落到「整篇没文本」这一个分支里，原先是同一句「未提取到文本（可能为
    扫描版）」。可它们是两件事：扫描版是文件没有文本层，逐页失败是解析器的问题 ——
    用同一句话盖住后者，用户会去重新找一份 PDF，而问题不在文件。
    """
    import app.routers.projects as projects_router
    from app.main import app
    from app.routers import projects_documents as documents_router

    monkeypatch.setattr(
        documents_router, "extract_pdf_text",
        lambda _d: [{"page": 1, "text": "", "failed": True},
                    {"page": 2, "text": "", "failed": True}],
    )

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        task, docs = _upload_documents(
            client, pid, files=[("files", ("d.pdf", _make_pdf(["x"]), "application/pdf"))]
        )
        assert docs == []
        assert [f["reason"] for f in task["failed"]] == ["每一页的文本提取都失败"]


def test_blank_scan_still_says_scanned(tmp_db, monkeypatch):
    """一页都没文本、也没有一页报错 ⇒ 扫描版（既有文案，不许被上面那条改掉）。"""
    import app.routers.projects as projects_router
    from app.main import app
    from app.routers import projects_documents as documents_router

    monkeypatch.setattr(
        documents_router, "extract_pdf_text",
        lambda _d: [{"page": 1, "text": "", "failed": False}],
    )

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        client.post(f"/api/projects/{pid}/topic", json={
            "paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t",
        })
        task, _ = _upload_documents(
            client, pid, files=[("files", ("d.pdf", _make_pdf(["x"]), "application/pdf"))]
        )
        assert [f["reason"] for f in task["failed"]] == ["未提取到文本（可能为扫描版）"]


# ---------------------------------------------------------------
# 写作语言（中文 / 英文）
# ---------------------------------------------------------------
_TOPIC_BODY = {"paper_type": "课程论文/小论文", "target_words": 1000, "topic": "t"}


def _set_topic(client, pid, **overrides):
    """提交选题。语言是这里的一个普通字段（**只在产出物上生效**，见决策 3）。"""
    return client.post(f"/api/projects/{pid}/topic", json={**_TOPIC_BODY, **overrides})


def test_meta_ships_the_language_and_format_tables(tmp_db):
    """七个新键必须随 /api/meta 下发。

    其中 `citation_formats_by_lang` 是「选了英文就不给 GB/T 7714」这条要求的**唯一
    数据源**：前端只渲染它，不另立一份格式清单 —— 抄一份过去，界面上会多出一个后端
    会 400 拒掉的选项，而它看起来完全正常。`citation_format_labels` 是它的配对：
    前者决定「能选哪些」，后者决定「选中后显示成什么」。
    """
    from app.citation_format import SUPPORTED_FORMATS, format_note
    from app.main import app

    meta = TestClient(app).get("/api/meta").json()
    assert meta["writing_langs"] == ["zh", "en"]
    assert meta["writing_lang_labels"] == {"zh": "中文", "en": "英文"}
    assert meta["citation_formats_by_lang"] == {
        "zh": ["gb7714", "apa", "mla"], "en": ["apa", "mla"],
    }
    assert meta["default_citation_format_by_lang"] == {"zh": "gb7714", "en": "apa"}
    assert meta["words_unit_by_lang"] == {"zh": "字", "en": "words"}
    assert meta["citation_format_labels"] == {
        "gb7714": "GB/T 7714", "apa": "APA（第 7 版）", "mla": "MLA（第 9 版）",
    }

    notes = meta["citation_format_notes_by_lang"]
    # 两种语言的**键集相同**、都覆盖全部格式 —— 这是本轮有意拓宽的形状：英文下
    # `gb7714` 是"该语言选不到"的那一项，也要取得到说明，因为选题步那句「GB/T 7714
    # 是中文期刊的著录标准，英文论文不用它」就是它（原先是前端手写的一句，后端改表
    # 之后那句话就变成假话，而它看起来完全正常）。
    assert set(notes["en"]) == set(SUPPORTED_FORMATS)
    assert set(notes["zh"]) == set(SUPPORTED_FORMATS)
    assert notes["en"]["apa"] == "" and notes["en"]["mla"] == "", "英文下两种都常用"
    assert "中文期刊" in notes["en"]["gb7714"], "被排除掉的格式要能解释自己为什么不在"
    assert notes["zh"]["gb7714"] == "", "中文的主选格式不用配提示"
    assert "中文期刊极少使用" in notes["zh"]["apa"]
    assert "中文期刊极少使用" in notes["zh"]["mla"]
    # 说明本身只有一份（citation_format.format_note），不是前端另写一句
    assert notes["en"]["gb7714"] == format_note("en", "gb7714") != ""


def test_format_tables_agree_with_the_backend_whitelist(tmp_db):
    """两张表必须自洽，否则界面上会出现「点了就 400」的选项。

    · 各语言可选格式的并集 = `SUPPORTED_FORMATS`（后端认识的集合）。多一项就是
      界面给了个必然被拒的选项；少一项则是某个能用的格式在界面上根本选不到。
    · 每种语言的默认格式必须落在它自己的清单里 —— 否则「格式在当前语言下不合法时
      回落」会落进另一个不合法值，`effective_format` 的收敛就成了空转。
    """
    from app.citation_format import SUPPORTED_FORMATS
    from app.main import app

    meta = TestClient(app).get("/api/meta").json()
    by_lang = meta["citation_formats_by_lang"]
    assert {f for fs in by_lang.values() for f in fs} == set(SUPPORTED_FORMATS)
    for lang, fmt in meta["default_citation_format_by_lang"].items():
        assert fmt in by_lang[lang], f"{lang} 的默认格式不在自己的清单里：{fmt}"

    # 标签表要**不多不少**盖住后端认识的格式：少一个键，那个格式的选项会显示成裸的
    # 标识（`gb7714`）；多一个键则是某个已经下线的格式还留着一句显示文字。
    assert set(meta["citation_format_labels"]) == set(SUPPORTED_FORMATS)
    assert "（默认）" not in "".join(meta["citation_format_labels"].values()), (
        "「默认」随语言变，由前端按 default_citation_format_by_lang 现判，"
        "写进标签就会在另一种语言下印着一个错的默认标记"
    )


def test_new_project_and_untouched_topic_default_to_chinese(tmp_db):
    """不传语种就是中文（默认值在 pydantic 模型与 DB 列上各一份，两处同值）。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        assert client.get(f"/api/projects/{pid}").json()["writing_lang"] == "zh"
        r = _set_topic(client, pid)      # 请求体里连这个键都没有
        assert r.status_code == 200, r.text
        assert r.json()["writing_lang"] == "zh"


def test_unknown_language_converges_to_chinese(tmp_db):
    """认不出的语种收敛到中文 —— 既不是 400，也不是原样存进去。

    这个字段可能来自一份旧版的前端镜像或手改的请求体。存进去的话，库里就有了一个
    谁都不认识的语种：读侧每次都回落（行为上还对），但界面那个下拉框选不中任何一项。
    收敛必须发生在**写库之前**，库里只留规范形。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        r = _set_topic(client, pid, writing_lang="fr")
        assert r.status_code == 200, r.text
        assert r.json()["writing_lang"] == "zh"
        assert client.get(f"/api/projects/{pid}").json()["writing_lang"] == "zh"


def test_min_target_words_message_uses_the_language_unit(tmp_db):
    """下限那句文案要按语言说「字」还是「words」。

    反过来说也一样错：英文项目说「不能少于 1000 字」，用户会照着一个错的单位填数。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]

        r = _set_topic(client, pid, target_words=999, writing_lang="en")
        assert r.status_code == 400
        assert "words" in r.json()["detail"], r.json()["detail"]

        r = _set_topic(client, pid, target_words=999, writing_lang="zh")
        assert r.status_code == 400
        assert "words" not in r.json()["detail"], "中文项目不该说 words"
        assert "字" in r.json()["detail"]


def test_set_topic_stores_the_language(tmp_db):
    """选了英文要真的存下来（读取侧只此一处：projects._lang_of）。"""
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        r = _set_topic(client, pid, writing_lang="en")
        assert r.status_code == 200, r.text
        assert r.json()["writing_lang"] == "en"
        assert client.get(f"/api/projects/{pid}").json()["writing_lang"] == "en"


def test_language_change_cascades_the_citation_format(tmp_db):
    """zh→en 且当前是 gb7714 时，同一笔里把格式改成该语言的默认（APA）。

    **不是 400**：用户没做错事，是他上一次的选择在新语言下不再合法。也不留一个
    「库里存着 en + gb7714」的组合给后面每个渲染入口去兜底 —— 那正是
    `effective_format` 存在的理由，但脏数据本该在写入点就被挡住。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        assert _set_topic(client, pid, writing_lang="zh").json()["citation_format"] == "gb7714"

        r = _set_topic(client, pid, writing_lang="en")
        assert r.status_code == 200, r.text
        assert r.json()["citation_format"] == "apa"
        assert client.get(f"/api/projects/{pid}").json()["citation_format"] == "apa"


def test_language_switch_back_does_not_revert_the_format(tmp_db):
    """en→zh **不**自动改回 gb7714。

    APA / MLA 在中文写作下是允许的（决策 2：三项都在，只是配一句提示），擅自改掉
    等于替用户做了选择 —— 而他可能正是在给一份投英文刊的稿子选格式。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = client.post("/api/projects", json={}).json()["id"]
        _set_topic(client, pid, writing_lang="en")           # 顺带把格式带成 apa
        r = _set_topic(client, pid, writing_lang="zh")
        assert r.json()["citation_format"] == "apa", "不得替用户改回 gb7714"
        assert r.json()["writing_lang"] == "zh"


def test_language_cascade_fixes_a_dirty_format_without_invalidating_downstream(tmp_db):
    """判据取**结果语言**而不是「语言变了没」：手改过的库也会被顺手修正。

    同时钉住「修正格式不作废下游」：格式只影响文末列表的渲染文本，已经有正文的项目
    改这一项，代价是一次零成本重排（`citations_stale` 会判成 render），不该被当成
    「改了选题」那样整份推倒。
    """
    from app import db
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        _set_topic(client, pid, writing_lang="en")
        db.update_project(pid, citation_format="gb7714", status="citation_confirmed")

        r = _set_topic(client, pid, writing_lang="en")
        assert r.status_code == 200, r.text
        assert r.json()["citation_format"] == "apa"
        assert r.json()["status"] == "citation_confirmed", "没改选题就不许把状态打回"


def test_set_topic_language_change_invalidates_downstream(tmp_db):
    """改语言与改选题同款：产出物整份作废（含 `clusters_json`）。

    库里那份中文大纲与正文留着就是错的，而界面上章节标题一个都没变、看不出异常。
    聚类也要一起作废 —— 它产出的主题名与概述会成为文献综述的**节标题与节内容**，
    是产物文字。

    **设计表单刻意不在这一批里**（与改类型时不同）：它的取值来自作者自己手填或从
    自有材料里提炼，是**作者的输入**而不是产出物；清掉等于把他写的东西删了。类型
    变了才清，因为那时字段名本身都换了。
    """
    from app import db
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        # 直接落库造出「下游都已生成完」的现场：这里测的是作废，不是那几条链路的产物
        db.update_project(
            pid,
            citation_binding_json={"绪论": [1]},
            sections_json=[{"section_title": "绪论", "content": "旧正文"}],
            references_json=[{"title": "某文献"}],
            materials_plan_json={"plan": [{"section": "绪论"}]},
            generation_state_json={"status": "done", "done": 1, "total": 1},
            clusters_json={"clusters": [{"title": "主题一"}]},
            design_json={"fields": {"研究方法": "半结构化访谈"}},
        )

        r = _set_topic(client, pid, writing_lang="en")
        assert r.status_code == 200, r.text
        after = r.json()
        assert after["writing_lang"] == "en"
        assert after["status"] == "topic_set"
        for col in ("outline_json", "citation_binding_json", "materials_plan_json",
                    "sections_json", "references_json", "generation_state_json",
                    "clusters_json"):
            assert after[col] is None, f"{col} 应随语言改动一起作废"
        assert after["design_json"] == {"fields": {"研究方法": "半结构化访谈"}}, \
            "作者的输入不该被顺手删掉"


def test_set_topic_unchanged_language_keeps_downstream(tmp_db):
    """语言原样重提不得作废任何东西。

    与「原样重提选题」同一条短路：用户点「确认选题」很可能只是路过，而作废是不可逆的
    —— 平白丢掉一份好大纲和已生成的正文，代价远大于省下这一次判断。
    """
    from app import db
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        _set_topic(client, pid, writing_lang="en")
        # 上一步是一次真的语言改动，大纲已被作废 —— 重走一遍，本轮要测的是「原样重提」
        outline = _generate_outline(client, pid)
        client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})
        db.update_project(
            pid,
            sections_json=[{"section_title": "绪论", "content": "正文"}],
            clusters_json={"clusters": [{"title": "主题一"}]},
        )

        r = _set_topic(client, pid, writing_lang="en")
        assert r.status_code == 200, r.text
        after = r.json()
        assert after["sections_json"], "原样重提不能作废正文"
        assert after["clusters_json"], "原样重提不能作废聚类"
        assert after["outline_json"], "原样重提不能作废大纲"


def test_confirm_citations_rejects_unknown_style_and_format(tmp_db):
    """两个白名单：拼错的角标样式 / 拼错的格式名都要拦在写库之前。

    为什么要有这两道：`_num_prefix` 对认不出的角标样式**一律当方括号渲染**，于是库里
    存着「上标」与存着「拼错的样式名」长得一模一样 —— 用户看到一份方括号正文，却不知
    道自己选的上标没生效。格式名同源：越界的值会被 `effective_format` 静默换成默认值。

    文案里**显示名与标识都要有**：界面上的用户见过的是「[1] 方括号角标」，直接调接口
    的人手里只有 `bracket` —— 少印哪个，另一个读者就得猜。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        before = client.get(f"/api/projects/{pid}").json()["status"]

        r = client.post(f"/api/projects/{pid}/citations/confirm", json={
            "binding": {}, "cite_style": "上标", "citation_format": "apa"})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "上标" in detail, "要原样回述用户填的那个值"
        assert "superscript" in detail, "要说清接口层该填什么"
        assert "方括号角标" in detail, "也要说清界面上那两项叫什么"

        r = client.post(f"/api/projects/{pid}/citations/confirm", json={
            "binding": {}, "cite_style": "bracket", "citation_format": "gb"})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "gb7714" in detail, "要说清合法取值"
        assert "GB/T 7714" in detail, "合法取值要带上下拉框里的名字"

        # 被拒的两次都没留痕
        assert client.get(f"/api/projects/{pid}").json()["status"] == before


def test_confirm_citations_rejects_gb7714_for_english_writing(tmp_db):
    """英文论文不支持 GB/T 7714 —— 后端侧兜底（界面已经不给这个选项了）。

    判据取自 `citation_formats_for`，不写死 `en != gb7714`：那张表才是「哪种语言能用
    哪种格式」的唯一来源，将来给它增删格式时，这里与下拉框一起跟着变。

    **前端收敛替代不了这一道**：`/citations/confirm` 是外部可调用的接口，界面上的收敛
    只保护界面；而且「闸门要在写状态的那一侧」是本项目已有的教训。
    """
    from app.main import app

    with TestClient(app) as client:
        pid = _doc_project(client)
        _set_topic(client, pid, writing_lang="en")     # 改语言会作废大纲，得重走一遍
        outline = _generate_outline(client, pid)
        client.post(f"/api/projects/{pid}/outline/confirm", json={"outline": outline})

        r = client.post(f"/api/projects/{pid}/citations/confirm", json={
            "binding": {}, "cite_style": "bracket", "citation_format": "gb7714"})
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "英文" in detail and "gb7714" in detail
        assert "apa" in detail, "要说清这个语言下能选什么"
        # 「可选」那串给的是**该语言下**的清单（GB/T 不在其中），而且带显示名 ——
        # 拒绝语若照抄全局 SUPPORTED_FORMATS，会把刚被拒的那个格式又列成可选。
        assert "GB/T 7714" not in detail.split("可选：")[1]
        assert "APA（第 7 版）" in detail.split("可选：")[1]

        # 该语言下合法的两项照常放行（守卫不能变成一把锁死不放的锁）
        for fmt in ("apa", "mla"):
            r = client.post(f"/api/projects/{pid}/citations/confirm", json={
                "binding": {}, "cite_style": "bracket", "citation_format": fmt})
            assert r.status_code == 200, r.text
