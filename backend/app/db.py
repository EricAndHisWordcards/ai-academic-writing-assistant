"""数据模型：论文项目、大纲树、文献卡片、引用绑定。

使用 SQLite 持久化，JSON 序列化复杂结构（大纲树、绑定关系）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app import metadata, paper_types
from app.citation_format import format_reference
from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# 项目状态机
# ---------------------------------------------------------------
class ProjectStatus:
    """项目状态枚举。"""

    DRAFT = "draft"                              # 草稿
    TOPIC_SET = "topic_set"                      # 选题完成
    OUTLINE_PENDING = "outline_pending"          # 大纲待确认
    OUTLINE_CONFIRMED = "outline_confirmed"      # 大纲已确认
    RESOURCES_LOADING = "resources_loading"      # 资源注入中
    CITATION_PENDING = "citation_pending"        # 引用待确认
    CITATION_CONFIRMED = "citation_confirmed"    # 引用已确认
    GENERATING = "generating"                    # 生成中
    COMPLETED = "completed"                      # 生成完成
    EXPORTED = "exported"                        # 已导出

    ALL = [
        DRAFT, TOPIC_SET, OUTLINE_PENDING, OUTLINE_CONFIRMED,
        RESOURCES_LOADING, CITATION_PENDING, CITATION_CONFIRMED,
        GENERATING, COMPLETED, EXPORTED,
    ]


# 论文类型清单已收敛到 app/paper_types.py（单一事实来源），此处不再重复定义。


# ---------------------------------------------------------------
# 数据库连接
# ---------------------------------------------------------------
def _db_path() -> Path:
    p = Path(settings.database_path)
    if not p.is_absolute():
        if getattr(sys, "frozen", False):
            # 打包态（PyInstaller）：__file__ 指向**临时解压目录** _MEIPASS，
            # 那里的 data/app.db 会随进程退出一起被清掉，而且 onefile 每次启动
            # 解压出的目录名都不同 —— 用户双击打开看到的是**一个新的空库**，
            # 关掉再开，之前写的论文全没了。这是静默的数据丢失，比报错更难发现。
            # 落回 ~/.academic_writer/：与 local_config 的 config.json 同一个
            # 用户数据目录，稳定、可写、且不随应用目录迁移而丢。
            p = Path.home() / ".academic_writer" / p.name
        else:
            # 源码态：相对 backend/ 放，与历来行为一致
            p = Path(__file__).resolve().parent.parent / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """打开一个连接，**退出时提交并关闭**。

    为什么必须显式关闭：`sqlite3.Connection.__exit__` 只做 commit / rollback，
    **不关闭连接** —— 于是 `with get_conn() as conn:` 退出后连接仍然活着，只能等
    CPython 的循环 GC 顺手回收。两个真实代价：Windows 上句柄没释放期间那个 .db
    文件删不掉（`[WinError 32]`，测试夹具因此每跑一轮就在 TEMP 里留下一批回收不掉的
    临时库）；服务端则是每处理一个请求多留一个连接，直到某次 GC 才成批回收。

    用 contextmanager 把「提交」与「关闭」绑在一起：事务语义与 `with conn:` 逐字一致
    （正常退出提交、抛异常回滚），只是多了一次关闭。全部 20 余处调用点本来就是
    `with get_conn() as conn:` 形态，没有一处把连接带出这个块，所以是等价替换。
    """
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL：读不阻塞写。生成循环在持续写 generation_state_json / task_state_json，
    # 前端同一时间还在轮询进度——默认 journal 模式下这两类操作互相抢写锁，WAL 让
    # 轮询的读不再被生成的写挡住。journal_mode 持久化在库文件上，逐连接重复
    # 执行只是幂等确认，无额外代价。
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """初始化数据库表结构。"""
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                title TEXT,
                paper_type TEXT,
                target_words INTEGER,
                topic TEXT,
                -- writing_ideas：作者自己写的写作思路 / 论证思路（纯文本，非 JSON）。
                -- 选题是唯一承载「作者意图」的地方，而标题装不下「打算怎么论证」；
                -- 它进大纲第一遍与每一节正文的提示词。
                writing_ideas TEXT,
                status TEXT DEFAULT 'draft',
                outline_json TEXT,
                citation_binding_json TEXT,
                sections_json TEXT,
                cite_style TEXT DEFAULT 'bracket',
                citation_format TEXT DEFAULT 'gb7714',
                -- writing_lang：产出物的语言（zh / en），只管论文本身写成什么语言，
                -- **不管界面文案**（见 app/writing_lang.py 的模块说明与那条界）。存量
                -- 项目全部落在这个 DEFAULT 上，于是中文路径逐字节不变。
                writing_lang TEXT DEFAULT 'zh',
                generation_state_json TEXT,
                references_json TEXT,
                task_state_json TEXT,
                materials_plan_json TEXT,
                design_json TEXT,
                clusters_json TEXT,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                project_id TEXT,
                filename TEXT,
                title TEXT,
                -- title_en：题名的英译（题名本身是英文时为空）。只为 APA / MLA 而存在：
                -- 非英语文献在这两种格式下要印成「原文题名 [English translation].」
                -- （APA 7 §9.38）。抽取时就落库、**不看项目当前语言** —— 否则用户
                -- 改一次语言就得把全部文献重传一遍。
                --
                -- 代价是存量文献这一列为空（没法凭空翻译），那种条目在 APA/MLA 下
                -- 退回只印原文题名，不做回填迁移。
                title_en TEXT,
                authors TEXT,
                year TEXT,
                source TEXT,
                -- 期刊著录项（GB/T 7714-2015 顺序编码制）。四项都是纯文本、体量极小，
                -- 所以允许进 list_document_meta 那个轻量查询 —— 与 pages_json 不同。
                --
                -- 列名必须是 page_range，**不能**叫 pages：get_document 与
                -- list_documents 都会执行 d["pages"] = json.loads(d.pop("pages_json"))，
                -- 而生成循环正是走 list_documents 建 doc_map —— 叫 pages 会被逐页
                -- 文本静默覆盖，要到 _plan_citations 才发现卷期页全空了。
                volume TEXT,
                issue TEXT,
                page_range TEXT,
                -- source_type：文献类型标识（J/M/D/C/N/R/S/P/G/Z，**单字母**）。
                -- 渲染时按白名单校验、越界回落 J —— 它会直接进正文文本，而值来自
                -- 模型输出，不白名单就等于让模型往正文里写任意字符。
                -- 不带 /OL 这类电子双标识：那要跟 URL 与引用日期配对，本轮不做。
                source_type TEXT,
                -- 非期刊类型的著录项（GB/T 7714-2015 里 [M]/[D]/[C]/[N] 各需一项）：
                -- place = 出版地；edition = 版本项（专著用，如「第3版」）；
                -- publish_date = 报纸出版日期（**日**级精度，如 2023-05-04 —— year
                -- 那一列被提示词要求只填 4 位年份，装不下「日」）。
                --
                -- 这三列不是「有则更好」的补充，而是**非期刊模板的触发条件**：
                -- citation_gb_types._TRIGGERS 只读这三列，任一非空才走各自的标准形态；
                -- 全空（**全部存量条目**）则逐字节落回改动前的兜底形态 —— 那就是
                -- 「这一轮不动存量项目」的全部依据。所以加这三列的真正代价是
                -- 「四份手写字段清单必须同步改」，漏一处的症状见 _SNAPSHOT_META_FIELDS。
                --
                -- 出版者 / 学位授予单位 / 论文集名 / 报纸名**刻意不另开列**：source
                -- 那一列装的就是它们（提示词写的是「期刊/会议/来源」，前端标签是
                -- 「来源（刊名 / 出版社 / 学位授予单位 / 论文集名 / 报纸名）」）。另开一列的后果是模型抽到的
                -- 出版社落在 source 里、新列恒空，模板上线了等于没上线。
                place TEXT,
                edition TEXT,
                publish_date TEXT,
                pages_json TEXT,
                summary TEXT,
                -- content_hash：文献内容指纹（见 document_fingerprint），上传去重唯一依据。
                -- 存量行没有它，由 _migrate_document_hashes 在启动时从 pages_json 反算补齐。
                content_hash TEXT,
                created_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            -- 作者自有的研究材料（核心思路 / 数据 / 成果）。
            -- 刻意与 documents 分表：documents 会被引用调度与参考文献列表消费，
            -- 混入作者材料会让它们出现在参考文献里，那是学术事故。kind 区分
            -- 「上传文件」与「直接粘贴的文本」，label 是展示名（文件即原文件名）。
            CREATE TABLE IF NOT EXISTS materials (
                id TEXT PRIMARY KEY,
                project_id TEXT,
                label TEXT,
                kind TEXT,
                text TEXT,
                char_count INTEGER,
                created_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            """
        )
        # 迁移：为旧库补充后加的列
        cols = [r[1] for r in conn.execute("PRAGMA table_info(projects)").fetchall()]
        migrations = {
            "cite_style": "TEXT DEFAULT 'bracket'",
            "citation_format": "TEXT DEFAULT 'gb7714'",
            # generation_state_json：分段生成的实时进度（后台任务 + 轮询）
            # references_json：文末参考文献列表，落盘后刷新页面不再丢失
            "generation_state_json": "TEXT",
            "references_json": "TEXT",
            # task_state_json：通用后台任务进度（大纲生成 / 文献解析），
            # 形状见 tasks.py 与 routers/projects._run_outline
            "task_state_json": "TEXT",
            # materials_plan_json：LLM 对作者自有研究材料的分析结果
            # （每份材料该放进哪一节、如何融入），供分段生成时按节取用
            "materials_plan_json": "TEXT",
            # design_json：作者已给出的研究设计 / 技术方案字段
            # （{"fields": {字段名: 值}, "extracted_at": ISO}）。实证研究不在大纲前
            # 拿到作者自己的方法、假设与结果，第四章「实证分析」就只能靠编。
            "design_json": "TEXT",
            # clusters_json：文献综述的主题聚类产物（作者可改、可确认），
            # 形状见 agents/cluster_agent.py。按**文献 id** 归类而不是按章节标题，
            # 于是它不受大纲改标题的影响，也不会反过来作废引用绑定。
            "clusters_json": "TEXT",
            # writing_ideas：作者写下的写作思路 / 论证思路（见 CREATE TABLE 处的注释）。
            # 纯 TEXT，**不加进 _row_to_project 的 _json 名单** —— 那份名单是要 json.loads
            # 的，把一段中文放进去会当场抛 JSONDecodeError。
            "writing_ideas": "TEXT",
            # writing_lang：产出物的语言（见 CREATE TABLE 处注释）
            "writing_lang": "TEXT DEFAULT 'zh'",
        }
        for col, decl in migrations.items():
            if col not in cols:
                conn.execute(f"ALTER TABLE projects ADD COLUMN {col} {decl}")

        # documents 表也要补列，判据同上（PRAGMA 里找列名），不用
        # try/except ALTER 兜底 —— 那颗 except 会把「列名拼错」一并吞掉，
        # 于是新库永远缺这一列而没人发现。
        doc_cols = [r[1] for r in conn.execute("PRAGMA table_info(documents)").fetchall()]
        doc_migrations = {
            "content_hash": "TEXT",
            # 期刊著录项，见 CREATE TABLE 处注释（含「列名必须是 page_range」那条）
            "volume": "TEXT",
            "issue": "TEXT",
            "page_range": "TEXT",
            "source_type": "TEXT",
            # 题名的英译，见 CREATE TABLE 处注释（为 APA/MLA 的非英语文献著录而存）
            "title_en": "TEXT",
            # 非期刊类型的著录项，见 CREATE TABLE 处注释（含「出版者不另开列」那条）。
            # **这一处是唯一一个测试永远看不见的改动点**：夹具每次都建新库、走
            # CREATE TABLE，所以漏了这里测试照旧全绿，而真实用户的旧库在下次上传
            # 文献时撞 `no such column` 直接 500。
            "place": "TEXT",
            "edition": "TEXT",
            "publish_date": "TEXT",
        }
        for col, decl in doc_migrations.items():
            if col not in doc_cols:
                conn.execute(f"ALTER TABLE documents ADD COLUMN {col} {decl}")

    # 补列之后再迁移类型名：迁移只碰 paper_type 一列，与补列无依赖关系，
    # 但放在这里能保证它在 startup() 里先于 reset_stale_* 完成。
    _migrate_paper_types()
    # 元数据占位词要**先**清 documents，再重算快照 —— 快照重算优先取库里的字段，
    # 顺序反了就会拿着还没清干净的旧值重新烘一遍。
    _migrate_document_meta()
    # 指纹补算与上面几条无依赖关系（它只看 documents 自己的 pages_json），
    # 但必须在任何一次上传之前跑完 —— 否则存量文献在这一列上仍是空的，
    # 用户重传一遍老文献时不会被认出来。
    _migrate_document_hashes()
    _migrate_reference_snapshots()
    # 标题迁移与上面几条无依赖关系（它只看 projects 自己的 title / topic 两列），
    # 放在最后只是为了让「清存量」这几条排在一起。
    _migrate_project_titles()


def _migrate_paper_types() -> int:
    """把改名前的论文类型就地更新为现名，返回受影响行数。

    只按**明确写死的键**逐条 `WHERE paper_type = ?`，不枚举、不通配、不按模式匹配：
    待改的行集是确定的。跑第二次匹配 0 行，幂等由 WHERE 子句本身保证，不需要标记位。

    为什么必须迁移而不是放任：`config_for` 对未知类型会静默退化成课程论文，
    于是老项目的工序、设计字段与生成闸门全部悄悄变样，用户只会觉得「这项目坏了」。
    """
    total = 0
    for old, new in paper_types.PAPER_TYPE_RENAMES.items():
        if old == new:
            continue
        with get_conn() as conn:
            cur = conn.execute(
                "UPDATE projects SET paper_type = ?, updated_at = ? WHERE paper_type = ?",
                (new, _now(), old),
            )
            total += cur.rowcount or 0
    if total:
        # 改的是用户数据，桌面版没有控制台，必须留下痕迹
        logger.info("已迁移 %d 个项目的论文类型名到当前清单", total)
    return total


def _migrate_document_meta() -> int:
    """把 documents 表里 authors / year / source 的占位词就地置空，返回受影响行数。

    为什么必须迁移而不是只在写入侧拦住：占位词是**已经落库的存量数据**。
    前端文献列表（App.jsx 的 ResourceStep）直接渲染库里这三个字段，Python 侧再
    怎么清洗也修不到那条路；拟大纲时它们还会被拼成元数据串喂给模型。

    判定复用 metadata.clean_meta_value（与写入侧、显示侧同一份清单），但**只**在
    归一化结果为空时才写库 —— 即只清占位词，绝不顺手改动任何真实值（连真实值的
    首尾空白都不动）。幂等由 WHERE 子句本身保证：置空后不再匹配，跑第二次 0 行。

    列名取自模块常量 META_FIELDS，不是用户输入；SQLite 也不支持把列名参数化。
    """
    total = 0
    with get_conn() as conn:
        for field in metadata.META_FIELDS:
            rows = conn.execute(
                f"SELECT DISTINCT {field} FROM documents "
                f"WHERE {field} IS NOT NULL AND {field} != ''"
            ).fetchall()
            for (raw,) in rows:
                if metadata.clean_meta_value(raw):
                    continue  # 真实值，原样保留
                cur = conn.execute(
                    f"UPDATE documents SET {field} = '' WHERE {field} = ?", (raw,)
                )
                total += cur.rowcount or 0
    if total:
        # 改的是用户数据，桌面版没有控制台，必须留下痕迹
        logger.info(
            "已清除 %d 处文献元数据占位词（未提及/未知/Not specified 等）", total
        )
    return total


def document_fingerprint(pages: list[dict]) -> str:
    """一篇文献的内容指纹（sha256），上传去重的**唯一**判据。

    刻意打在**落库的形态**上（page + snippet），而不是原始 PDF 字节：原始字节在上传
    结束时就丢了（见 routers/projects.upload_documents —— 读完就只在内存里，从不落盘），
    pages_json 是这篇文献唯一留存下来的内容。于是这个指纹对**存量行也是可反算的**，
    启动时的 _migrate_document_hashes 才能把老文献补上：用户重传一篇改动之前就传过的
    文献，同样会被认出来。若打在原始字节上，老文献这一列只能永远是 NULL。

    代价是判据弱于字节级哈希：snippet 是每页前 300 字（_run_parse 截断的），两篇不同
    文献要撞上，得页数相同、且每一页的前 300 字都逐字相同。实际语料里这等于同一份文件；
    万一撞上，后果是**多跳过一篇**并如实报出文件名与「与《X》内容相同」，用户看得见 ——
    比反过来（同一篇入库两次、正文里两个角标指同一篇）好收拾。
    """
    raw = "\x1f".join(
        f"{p.get('page')}:{p.get('snippet') or ''}" for p in (pages or [])
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _migrate_document_hashes() -> int:
    """为存量文献补算 content_hash，返回被写入的行数。

    幂等由 WHERE 子句保证：补过之后不再匹配。pages 为空的行走不到这里（解析阶段就
    因为「未提取到文本」被拒了），真出现也不补 —— 把空指纹写进去会让所有空文献互撞。

    每次启动都要扫一遍 documents 的这一列。这是**一次性**扫描（补完即 0 行），但
    WHERE 里带 content_hash 时 SQLite 仍要把每行的这一列读出来判一遍 —— 代价与库大小
    同阶。桌面版单机、文献以十计，可以接受；真到以万计时该加索引，不是改判据。
    """
    total = 0
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, pages_json FROM documents "
            "WHERE content_hash IS NULL OR content_hash = ''"
        ).fetchall()
        for row in rows:
            try:
                pages = json.loads(row["pages_json"] or "[]")
            except (TypeError, ValueError):
                continue  # 坏数据不该让启动失败
            if not isinstance(pages, list) or not pages:
                continue
            conn.execute(
                "UPDATE documents SET content_hash = ? WHERE id = ?",
                (document_fingerprint(pages), row["id"]),
            )
            total += 1
    if total:
        # 改的是用户数据，桌面版没有控制台，必须留下痕迹
        logger.info("已为 %d 篇存量文献补算内容指纹（上传去重用）", total)
    return total


# 快照重算时与文献库对齐的元数据字段：(快照里的键名, documents 表里的列名)。
# 两处键名不同，且只有 pages 这一处不同 —— 库里叫 page_range（避开逐页文本那个
# pages），而 citation_format 读的是 ref["pages"]（_apa / _mla 也一样）。
_SNAPSHOT_META_FIELDS = (
    ("authors", "authors"),
    ("year", "year"),
    ("source", "source"),
    ("volume", "volume"),
    ("issue", "issue"),
    ("pages", "page_range"),
    ("source_type", "source_type"),
    # 题名的英译：快照里的键名与列名同形。只有 APA / MLA 读它，但两边都要有 ——
    # 少一处就变成「这一轮写出的文末列表」与「重算出来的」不是同一个字符串。
    ("title_en", "title_en"),
    # 非期刊类型的著录三项：键名与列名同形，只有 GB/T 那条分支读它们。理由同 title_en。
    ("place", "place"),
    ("edition", "edition"),
    ("publish_date", "publish_date"),
)


def _snapshot_field(ref: dict, doc: dict | None, field: str) -> str:
    """快照重算时取一个元数据字段：**文献还在就用库里的，文献已删才退回快照自带的**。

    判据必须是「行在不在」（`doc is not None`），不能是「字段值真不真」。
    先前这里写的是 `doc.get(field) or ref.get(field)`，`or` 让空串也退回快照里的
    旧值 —— 于是用户「把抽错的作者清空」这个动作永远落不了地（下次启动被压回去），
    而填一个错的反而能生效。空串是**有效值**，只有行不存在才是「取不到」。
    """
    raw = doc.get(field) if doc is not None else ref.get(field)
    return metadata.clean_meta_value(raw)


def _migrate_reference_snapshots() -> int:
    """重算存量项目的 references_json，返回被改写的项目数。

    为什么光清 documents 不够：references_json 是**生成时烘死的快照** —— 其中每条
    文献的 formatted 字符串，在生成那一刻就用当时的元数据拼好了。实测有个项目里
    5 条已经印成「[1] 未提及. 题名[J]. 来源, 未提及.」，不清快照的话用户不重新生成
    就永远看到脏列表。这里按每个项目自己的 citation_format / cite_style /
    writing_lang 重算一遍，把 formatted 就地刷新。

    纯确定性重算：零 LLM 调用，不碰引用绑定与正文，只重写这一个派生字段。
    逐字相同则不写库 —— 于是「幂等」是重算结果的属性，而不是靠 WHERE 匹配的巧合。

    读的字段与 projects._plan_citations 逐项对齐（authors / year / source / volume /
    issue / page_range / source_type / title_en / place / edition / publish_date
    全部**只取库里的值**）：不对齐的话，这里重算出的 formatted 与「这一轮会写出的
    文末列表」就不是同一个字符串，citations_stale 会常年亮着，而用户按了重排也修不好。
    这份清单与 `_SNAPSHOT_META_FIELDS`、`list_document_meta` 的 SELECT、
    `_plan_citations` 的 ref dict 是**同一件事的四份手写表述** —— 加字段时四处都要改。

    **刻意不碰 doc_title**：题名的权威是引用绑定（schedule_agent._make_binding 把当时
    的题名冻了进去，_plan_citations 也是绑定优先于 documents.title）。这个迁移不读绑定，
    所以不去猜题名 —— 改过题名的项目会如实落进「文末列表待重排」那个提示，由用户按
    一次重排，而不是被这里静默换成另一份。

    刻意**不更新 updated_at**：这不是内容改动，而是派生字段的清理；动它会让这些
    项目在列表里凭空跳到最前面，用户会以为项目被改过。
    """
    changed = 0
    with get_conn() as conn:
        projects = conn.execute(
            "SELECT id, citation_format, cite_style, writing_lang, references_json "
            "FROM projects "
            "WHERE references_json IS NOT NULL AND references_json != ''"
        ).fetchall()
        for proj in projects:
            try:
                refs = json.loads(proj["references_json"])
            except (TypeError, ValueError):
                continue  # 坏数据不该让启动失败
            if not isinstance(refs, list) or not refs:
                continue
            # dict(...)：row_factory 是 sqlite3.Row，它不支持 .get()，而下面要按可缺
            # 字段取这几个值（文献被删、或旧快照里压根没这个键）。
            docs = {
                d["id"]: dict(d)
                for d in conn.execute(
                    "SELECT id, authors, year, source, volume, issue, page_range, "
                    "source_type, title_en, place, edition, publish_date "
                    "FROM documents WHERE project_id = ?",
                    (proj["id"],),
                ).fetchall()
            }
            rebuilt: list[Any] = []
            for ref in refs:
                if not isinstance(ref, dict):
                    rebuilt.append(ref)
                    continue
                # 不用 `or {}` 兜底：`docs.get(...)` 直接返回 None 才是「文献已被删」，
                # 而 {} 会让「行在但字段空」与「行不存在」变成同一件事。区别见 _snapshot_field。
                doc = docs.get(ref.get("doc_id"))
                merged = {**ref}
                for ref_key, doc_field in _SNAPSHOT_META_FIELDS:
                    # 文献还在就用库里（已被上一个迁移清洗过的）字段；文献已被删则
                    # 退回快照自带的值再清洗一遍 —— 两条路都过同一个判定。
                    value = _snapshot_field(ref, doc, doc_field)
                    # **有值、或快照里本来就有这个键**才写。少了后半句，每条老快照都会
                    # 凭空多出四个空键而被判成「变了」——「已经是干净的条目一字都不能改」
                    # 这条保证随即失效，且「逐字相同就不写库」的幂等性也一起没了。
                    if value or ref_key in ref:
                        merged[ref_key] = value
                # 语言与格式的解析只在 citation_format.effective_format 里做一次，
                # 这里与运行期走同一份映射：各写一份的话，启动重算与「这一轮会写出的
                # 文末列表」就可能不是同一个字符串，citations_stale 会常年亮着。
                # 所以**不给格式兜底默认值**（此处原先写的是 `proj["citation_format"]
                # or "gb7714"`）—— 那就是同一件事的第二处判据，而且方向是错的（它会
                # 把英文项目兜成 GB/T）。format_reference 内部必然过一次
                # effective_format，NULL 与脏值在那一处收敛。
                merged["formatted"] = format_reference(
                    merged,
                    proj["citation_format"],
                    proj["cite_style"] or "bracket",
                    proj["writing_lang"],
                )
                rebuilt.append(merged)
            if rebuilt == refs:
                continue
            conn.execute(
                "UPDATE projects SET references_json = ? WHERE id = ?",
                (json.dumps(rebuilt, ensure_ascii=False), proj["id"]),
            )
            changed += 1
    if changed:
        logger.info("已重算 %d 个项目的参考文献快照（清除了其中的元数据占位词）", changed)
    return changed


# 历史遗留的项目标题占位词。新建项目现在写空标题（标题由「选题」跟随而来，
# 见 routers/projects.set_topic），所以这个集合只为清存量数据而存在 —— 它**不是**
# 运行时的兜底清单，别在别处引用它做判断。
PLACEHOLDER_TITLES = ("未命名论文", "论文")


def _migrate_project_titles() -> int:
    """把存量项目的占位标题换成选题（没有选题可补的置 NULL），返回受影响行数。

    为什么必须迁移而不是只在写入侧拦住：title 列从建表起就在、update_project 的
    白名单里也一直有它，但**唯一的写入点是前端新建项目时写死的 '未命名论文'**，
    此后没有任何改名入口。库里那些项目的 title 全是这个常量，只在写入侧改成
    「跟随选题」的话，存量项目会永远显示「未命名论文」—— 用户看到的是「我明明
    填过研究核心方向，它还是未命名」。

    只认**明确写死**的空值与占位词，不枚举、不通配、不按模式匹配：真实标题一律
    原样保留（连它的首尾空白都不动）。两条语句的先后不能反：第二条收拾的是第一条
    没接住的（没有选题可补的）那些行，反过来会把刚填好的标题再清掉。

    幂等由「只在目标值与现值真的不同时才写」（differs）保证，不靠 WHERE 碰巧不匹配：
    否则「标题恰好就是占位词、而选题也叫这个名字」那一行会被每次启动都匹配一次、
    白打一条日志。空标题（只有空白）也**不**归一成 NULL —— 它在每个读取点都等价于
    NULL（_display_title、跟随判定都取 falsy），写它只会制造一次「改了但没改」。

    刻意**不更新 updated_at**：这不是内容改动，而是把一个从未被用户写过的值补上；
    动它会让这批项目按 updated_at DESC（list_projects 的排序）整体跳到列表最前面。
    """
    marks = ", ".join("?" * len(PLACEHOLDER_TITLES))
    # 「这个名字不是用户起的」：没写过、写了个空白，或就是那个写死的常量
    unnamed = f"(title IS NULL OR TRIM(title) = '' OR TRIM(title) IN ({marks}))"
    # 只认占位词。空白标题不在内：它没东西可清，动它只会多写一次库
    placeholder = f"(TRIM(title) IN ({marks}))"
    differs = "(title IS NULL OR title != TRIM(COALESCE(topic, '')))"

    changed = 0
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE projects SET title = TRIM(topic) "
            f"WHERE {unnamed} AND TRIM(COALESCE(topic, '')) != '' AND {differs}",
            list(PLACEHOLDER_TITLES),
        )
        changed += cur.rowcount or 0
        cur = conn.execute(
            f"UPDATE projects SET title = NULL WHERE {placeholder} AND {differs}",
            list(PLACEHOLDER_TITLES),
        )
        changed += cur.rowcount or 0
    if changed:
        # 改的是用户数据，桌面版没有控制台，必须留下痕迹
        logger.info("已把 %d 个项目的占位标题换成选题（没有选题的置空）", changed)
    return changed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_local() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------
# 项目 CRUD
# ---------------------------------------------------------------
def create_project(project_id: str, title: str | None = None) -> dict:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, title, ProjectStatus.DRAFT, _now(), _now()),
        )
    return get_project(project_id)


def get_project(project_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    if row is None:
        return None
    return _row_to_project(row)


def list_projects() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM projects ORDER BY updated_at DESC"
        ).fetchall()
    return [_row_to_project(r) for r in rows]


def _row_to_project(row: sqlite3.Row) -> dict:
    d = dict(row)
    for key in (
        "outline_json",
        "citation_binding_json",
        "sections_json",
        "generation_state_json",
        "references_json",
        "task_state_json",
        "materials_plan_json",
        "design_json",
        "clusters_json",
    ):
        val = d.get(key)
        d[key] = json.loads(val) if val else None
    return d


def update_project(project_id: str, **fields: Any) -> dict:
    allowed = {
        "title", "paper_type", "target_words", "topic", "writing_ideas", "status",
        "outline_json", "citation_binding_json", "sections_json",
        "cite_style", "citation_format", "writing_lang",
        "generation_state_json", "references_json", "task_state_json",
        "materials_plan_json", "design_json", "clusters_json",
    }
    sets = []
    values: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k.endswith("_json") and v is not None:
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k} = ?")
        values.append(v)
    sets.append("updated_at = ?")
    values.append(_now())
    values.append(project_id)

    with get_conn() as conn:
        conn.execute(
            f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", values
        )
    return get_project(project_id)


def reset_stale_generating() -> int:
    """清理僵死的「生成中」状态，返回清理条数。

    后台生成任务只活在进程内，服务重启即丢失；若不清理，这些项目会永远停在
    「生成中」并让前端一直转圈。已逐节落盘的 sections_json 保留，用户可重新发起。
    """
    count = 0
    for p in list_projects():
        if p.get("status") != ProjectStatus.GENERATING:
            continue
        state = dict(p.get("generation_state_json") or {})
        state["status"] = "interrupted"
        state["error"] = "生成任务因服务重启而中断，可重新发起生成"
        update_project(
            p["id"],
            status=ProjectStatus.CITATION_CONFIRMED,
            generation_state_json=state,
        )
        count += 1
    return count


def reset_stale_tasks() -> int:
    """把因服务重启而僵死的通用任务（大纲生成 / 文献解析）标记为 interrupted。

    与 reset_stale_generating 同理：后台任务只活在进程内，重启即丢。不清理的话
    前端会照着 task_state_json.status == "running" 一直转圈。已落盘的成果
    （文献卡片、大纲）原样保留，用户可重新发起。
    """
    count = 0
    for p in list_projects():
        state = p.get("task_state_json") or {}
        if state.get("status") != "running":
            continue
        state = dict(state)
        state["status"] = "interrupted"
        state["error"] = "任务因服务重启而中断，可重新发起"
        update_project(p["id"], task_state_json=state)
        count += 1
    return count


def delete_project(project_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


# ---------------------------------------------------------------
# 文献 CRUD
# ---------------------------------------------------------------
# 文献元数据里**允许被编辑**的列。这是白名单而不是「除 id 之外都能改」：documents 行里
# 还有 content_hash（判重判据）、pages_json（引用定位的原文）、project_id（归属）、
# filename（来自文件名，判重的展示名之一）。让编辑接口按 keys 直接拼 UPDATE，等于把
# 前三个暴露成可写 —— 改掉 content_hash 就能绕过判重，改掉 pages_json 就让引用定位
# 指到不存在的位置。
DOC_META_COLUMNS = (
    "title", "title_en", "authors", "year", "source",
    "volume", "issue", "page_range", "source_type",
    "place", "edition", "publish_date",
)


def add_document(project_id: str, doc: dict) -> dict:
    doc_id = doc["id"]
    # 指纹在**写入这一个点上**算，不接受调用方传进来：去重判据一旦有第二处推导，
    # 「上传时比对的那份」和「落库的那份」就可能不是同一个字符串，判重会静默失效。
    pages = doc.get("pages") or []
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO documents (id, project_id, filename, title, title_en, "
            "authors, year, source, volume, issue, page_range, source_type, "
            "place, edition, publish_date, "
            "pages_json, summary, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id, project_id, doc.get("filename"), doc.get("title"),
                doc.get("title_en"),
                doc.get("authors"), doc.get("year"), doc.get("source"),
                doc.get("volume"), doc.get("issue"), doc.get("page_range"),
                doc.get("source_type"),
                doc.get("place"), doc.get("edition"), doc.get("publish_date"),
                json.dumps(pages, ensure_ascii=False),
                doc.get("summary"),
                document_fingerprint(pages) if pages else None,
                _now(),
            ),
        )
    return get_document(doc_id)


def existing_document_title(project_id: str, content_hash: str) -> str | None:
    """项目内是否已有内容相同的文献，有则返回它的展示名（标题，缺则文件名）。

    **只在同一个项目内判重**：不同的两篇论文引用同一篇文献是正常的，跨项目去重会
    让第二个项目「传不上文献」而看不出原因。
    """
    if not content_hash:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT title, filename FROM documents "
            "WHERE project_id = ? AND content_hash = ? LIMIT 1",
            (project_id, content_hash),
        ).fetchone()
    if row is None:
        return None
    return row["title"] or row["filename"] or "(未命名)"


def get_document(doc_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM documents WHERE id = ?", (doc_id,)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["pages"] = json.loads(d.pop("pages_json") or "[]")
    return d


def list_documents(project_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["pages"] = json.loads(d.pop("pages_json") or "[]")
        out.append(d)
    return out


def list_document_meta(project_id: str) -> list[dict]:
    """只取「引用快照」真正读到的字段。

    id / filename / title / title_en / authors / year / source，外加七项著录字段
    （volume / issue / page_range / source_type / place / edition / publish_date）。

    与 list_documents 的差别不是省不省那点 SQL，而是**不碰 pages_json**：页码全文
    是每篇文献最重的一列，而 list_documents 会把它逐篇 json.loads 出来。生成前的一致性
    校验与「正文是否按旧编号写成」的判断只读上面这些字段（projects._refs_snapshot →
    _plan_citations / format_reference 从来看不到 pages），所以这两处走轻量查询 ——
    否则每打开一次项目、每点一次引用确认，都要把全部文献的正文解析一遍。

    那七项虽然也是「快照读到的字段」，却都是**后来才加进这条 SELECT 的**：漏加不会报错，
    只会让 _project_refs_snapshot 拿到的对应字段恒为空 —— 而那里正是文末列表的唯一来源，
    改 list_documents 完全替代不了它。漏掉 place/edition/publish_date 的症状尤其隐蔽：
    非期刊模板在**运行期**永远不触发，而启动迁移那一侧读得到（它走自己的 SELECT），
    于是同一份设置算出两串不一样的文本 —— 与 title_en 漏掉时是同一个症状。
    同理，往后新加的著录字段都要补进这条 SELECT。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, filename, title, title_en, authors, year, source, "
            "volume, issue, page_range, source_type, "
            "place, edition, publish_date FROM documents "
            "WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_document_ids(project_id: str) -> list[str]:
    """只取文献 id。

    与 list_document_meta 同一条理由，只是更窄：这是「这个项目里现在有哪些文献」这个
    问题的最小答案。调用方（projects._unbound_documents）要的是一份**集合**，用来和
    绑定里的 doc_id 求差 —— 为它多读十四个字段、再拼成一份没人看的 dict，没有意义。

    **刻意不排序**：库里其它读者都按 created_at 排，是因为那份列表要显示给人看；
    集合求差不在乎顺序。别把 ORDER BY 补回来。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM documents WHERE project_id = ?", (project_id,)
        ).fetchall()
    return [r["id"] for r in rows]


def update_document_meta(doc_id: str, fields: dict) -> dict | None:
    """按白名单改一篇文献的元数据列，返回更新后的整行（含 pages）。

    `fields` 里**只放调用方明确要写的列**（值为空串 = 清空该列，是有意义的写入）。
    键越界时抛 ValueError 而不是静默忽略：静默忽略会让「改了但没生效」这一类缺陷
    再出现一次，而这正是本轮要修的东西（改题名毫无反应）。
    """
    unknown = [k for k in fields if k not in DOC_META_COLUMNS]
    if unknown:
        raise ValueError(f"不可编辑的文献字段：{', '.join(sorted(unknown))}")
    if not fields:
        return get_document(doc_id)
    # 列名来自上面的白名单常量，值走占位符 —— 拼接的只有列名，不是值。
    sets = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE documents SET {sets} WHERE id = ?",
            (*fields.values(), doc_id),
        )
    return get_document(doc_id)


def delete_document(doc_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))


# ---------------------------------------------------------------
# 作者自有研究材料 CRUD
# ---------------------------------------------------------------
def add_material(project_id: str, material: dict) -> dict:
    mat_id = material["id"]
    text = material.get("text") or ""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO materials (id, project_id, label, kind, text, "
            "char_count, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                mat_id, project_id, material.get("label"),
                material.get("kind") or "file", text, len(text), _now(),
            ),
        )
    return get_material(mat_id)


def get_material(mat_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM materials WHERE id = ?", (mat_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def list_materials(project_id: str, with_text: bool = False) -> list[dict]:
    """列出项目的研究材料。

    默认**不带正文**：材料动辄十几万字，列表接口每轮询一次就回传全部正文毫无意义，
    前端列表只需要文件名与字数。需要正文的是生成环节，它直接走 get_material。
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM materials WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
    out = []
    for r in rows:
        m = dict(r)
        if not with_text:
            m.pop("text", None)
        out.append(m)
    return out


def delete_material(mat_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM materials WHERE id = ?", (mat_id,))
