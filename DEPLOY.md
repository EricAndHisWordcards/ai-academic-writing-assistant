# 部署方案

本文档说明 AI 学术写作辅助系统的部署方式，覆盖**桌面版（解压即用）、本地开发、Docker 一键部署、生产部署**四种场景。

> **只是想用起来？** 看 [方式一：桌面版](#二方式一桌面版解压即用零安装)——下载一个 zip、解压、双击，不用装 Python 也不用装 Node。后面三种是给要在服务器上跑、或要改代码的人看的。

> **⚠️ 章节沿革（2026-09-27，v1.31）**：本轮新增「方式一：桌面版」，其余方式整体后移一位。
> **别处文档里若引用本文件 §4.x 或 §三，那是重编号之前的编号**，对照如下：
>
> | 旧 | 新 | 章节 |
> | --- | --- | --- |
> | §二 | **§三** | 本地开发 |
> | §三 | **§四** | Docker 一键部署 |
> | §四 / §4.1–4.4 | **§五 / §5.1–5.4** | 生产部署要点 |
> | §五 | **§六** | 常见问题 |
>
> **上游引用已同步**：`ARCHITECTURE.md` 两处、`DECISIONS.md` D-29 的「此前 / 现在」两处，均已改为 §5.3。
> 唯独 `DECISIONS.md` 里标注「改写前出处」的那几处（`DEPLOY.md L14 / L112`、`§三`）按该文件自身声明的
> 规矩**刻意保留旧编号**——那个字段是历史快照，不是可点的指针，跟着改反而会伪造历史。

## 一、部署架构

### 服务器部署（方式三 / 方式四走这条）

```
用户浏览器
    │
    ▼
Nginx（前端静态文件 + 反向代理 /api）
    │
    ▼ /api/*
FastAPI 后端（uvicorn 单进程，原因见 5.3）
    │
    ├── SQLite 数据库（持久化卷）
    └── LLM（DeepSeek / 通义 / Moonshot 等 OpenAI 兼容接口）
```

- **前端**：React 构建产物（静态文件），由 Nginx 托管。
- **后端**：FastAPI + uvicorn，处理 Agent 工作流与业务逻辑。
- **数据**：SQLite 文件，通过 Docker volume 持久化。
- **LLM**：通过环境变量配置外部 API。

### 桌面版（方式一 / 方式二走这条）

没有 Nginx、没有 Docker，只有一个进程在管两件事：

```
AI学术写作辅助系统.exe（Electron 外壳）
    │  启动时 spawn 子进程
    ▼
resources/backend/academic_backend.exe（PyInstaller onefile：Python 运行时 + FastAPI + 全部 Agent）
    │
    ├── SQLite 数据库 → ~/.academic_writer/app.db
    ├── LLM 配置     → ~/.academic_writer/config.json（界面里填，不用 .env）
    └── LLM（同上）
    ▲
    └── 界面：Electron 内嵌 Chromium 加载 resources/app/dist（前端构建产物）
```

两点与服务器部署**实质不同**，排障时最容易搞错：

- **后端只监听 `127.0.0.1:8000`**，只有本机能访问；不像服务器那样经 Nginx 对外。想验证它活着：浏览器打开 `http://127.0.0.1:8000/api/health`，应返回 `{"status":"ok"}`。
- **数据不在程序目录里**，而在用户主目录的 `~/.academic_writer/`（Windows 即 `C:\Users\<你>\.academic_writer\`）。**删掉解压出来的程序目录不会丢数据；删掉这个目录才会。**

---

## 二、方式一：桌面版（解压即用，零安装）

面向**只想用、不想装环境**的人（这也是本项目的主要用户：高校学生与科研人员）。不需要 Python、不需要 Node、不需要 Docker。

### 步骤

1. 打开 [Releases 页面](https://github.com/EricAndHisWordcards/ai-academic-writing-assistant/releases)，下载最新那份 `AI-Academic-Writing-Assistant-<版本>-win.zip`。
2. **解压整个 zip** 到任意目录（比如桌面新建一个文件夹）。
3. 双击 `AI学术写作辅助系统.exe`。

就这样。首屏可以直接在界面里填 LLM 的 Key / Base URL / 模型名，存进 `~/.academic_writer/config.json`；**不填也能打开**，只是生成时用的会是演示模式。

### 校验下载是否完整（可选）

```powershell
# Windows PowerShell —— 与 Releases 页面标注的 SHA256 对比
Get-FileHash .\AI-Academic-Writing-Assistant-v1.34.0-win.zip -Algorithm SHA256
```

### ⚠️ 两个必须知道的限制

1. **不要只把 `AI学术写作辅助系统.exe` 单独拷出来运行，也不要单独发给别人。**
   它启动时要读**同目录下**的 `resources/`、若干 `.dll` 与 `.pak`（Electron 的运行时依赖）——那些文件不在 exe 里。单独拷/单独传的结果是**打不开**。要传就传整个 zip，或解压后的整个文件夹。
2. **只有 Windows x64 构建。** macOS / Linux 目前需要自己打包（见下）。

### 自己从源码打包

见 [源码打包指南.md](./源码打包指南.md)。里面有坑清单（输出目录必须随版本递增、Electron builder 的文件锁、`_MEI*` 残留等）与验收步骤。

### 排障

| 现象 | 原因 |
| --- | --- |
| 双击没反应 / 闪一下就退 | 多半是只拷了 exe，缺同目录的 `resources/` 与 `.dll`。解压整个 zip 再试 |
| 界面能开，但生成时提示未配置 LLM | 在界面里填 Key；或检查 `~/.academic_writer/config.json` |
| 关掉再打开，之前写的论文不见了 | 检查 `~/.academic_writer/app.db` 是否还在（打包态的库在这里，不在程序目录） |
| 想清空所有数据重来 | 删掉 `~/.academic_writer/` 整个目录（**不可逆**，先备份） |

---

## 三、方式二：本地开发（已在使用）

### 后端

```bash
cd backend
python -m venv venv
# Windows: venv\Scripts\activate   Linux/macOS: source venv/bin/activate
pip install -r requirements.txt

# 配置 LLM（可选，不配置则演示模式）
cp .env.example .env   # 编辑填入 LLM_API_KEY 等

uvicorn app.main:app --reload --port 8000
```

### 前端

```bash
cd frontend
npm install
npm run dev   # http://localhost:5173，自动代理 /api 到 8000
```

---

## 四、方式三：Docker 一键部署

### 前置条件

- 安装 Docker 与 Docker Compose。

### 步骤

1. 在项目根目录创建 `.env` 文件（用于 docker-compose 读取 LLM 配置）：

```bash
cp backend/.env.example .env
# 编辑根目录 .env，填入 LLM_API_KEY 等
```

2. 构建并启动：

```bash
docker compose up -d --build
```

3. 访问：`http://localhost`（前端）；API 文档 `http://localhost/api/docs`。

4. 查看日志 / 停止：

```bash
docker compose logs -f backend
docker compose down
```

### 说明

- 后端不对外暴露端口，仅通过前端 Nginx 反向代理访问（更安全）。
- 数据库持久化在 `academic_data` volume，重启不丢失。
- 批量 PDF 上传按"整批之和"计体积，Nginx 已设 `client_max_body_size 200m`（默认 1m 会导致上传 413）。
- 若需单独调试后端，可在 `docker-compose.yml` 中取消 `backend.ports` 注释。

---

## 五、方式四：生产部署要点

### 5.1 环境变量清单

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `LLM_API_KEY` | 是 | LLM 服务商 API Key |
| `LLM_BASE_URL` | 否 | OpenAI 兼容 base_url（DeepSeek 填 `https://api.deepseek.com/v1`） |
| `LLM_MODEL` | 否 | 模型名（默认 `deepseek-chat`） |
| `LLM_TEMPERATURE` | 否 | 采样温度（默认 0.3） |
| `DATABASE_PATH` | 否 | 数据库路径（Docker 内默认 `/app/data/app.db`） |
| `DATABASE_WAL` | 否 | SQLite WAL，**默认 `true`**。置 `false` **只影响新建的库**（已是 WAL 的库不会被降级，见 5.3）；生产不建议关闭，也不要写进 `.env` |

### 5.2 安全建议

1. **不要将 `.env` 提交到版本库**，使用服务器环境变量或密钥管理服务（如 KMS、Vault）。
2. **API Key 轮换**：Key 泄露后立即在服务商后台重置。
3. **HTTPS**：生产环境需在 Nginx 前加 TLS（如 Let's Encrypt / 云负载均衡）。
4. **限流与鉴权**：当前 MVP 无用户系统，公网部署需加认证层（后续迭代）。

### 5.3 性能与约束

- **必须单进程**：启动 uvicorn 时**不要**加 `--workers`。运行中的生成任务登记在进程内
  字典里（`tasks._TASKS` / `projects._GEN_TASKS`），"同一项目同一时刻只有一个生成在跑"
  的 409 互斥完全依赖它——多 worker 后每个 worker 各持一份登记表，互斥整体失效，
  同一项目会被两个 worker 同时生成：双倍 token 消耗、状态互相覆盖。当前单机规模下
  单进程 + 异步 IO 足够；真扛不住时的正确路径是把任务登记表外置（如 Redis），而不是加 worker。
- **数据库 WAL**：`db.get_conn()` 里 `PRAGMA journal_mode = WAL`（读不阻塞写），由环境变量
  `DATABASE_WAL` 控制、**默认开** —— 前端进度轮询不会被生成写入挡住。唯一的例外是测试：
  `tests/conftest.py` 的 `tmp_db` 夹具会在 `init_db()` **之前**把它压成 `false`，因为 WAL 下
  每个连接干净关闭都要建/拆一次 `-wal` / `-shm`，237 个建库用例会把全量 pytest 从约 6 分钟
  拖到约 24 分钟（Windows 本机安静实测 374s；并行做别的事时会显著变慢，别拿那种数字当基线）。**不要在生产关闭它，也不要把它写进 `.env`**（写了也只影响新建的库，容易
  误导排障）。
- **上传体积**：批量 PDF 上传按"整批之和"计体积，`frontend/nginx.conf` 已设
  `client_max_body_size 200m`（默认 1m 会导致上传 413）；单篇更大时相应调大。
- **长文生成超时**：LLM 分段生成耗时较长，Nginx `proxy_read_timeout` 已设为 300s；若生成更长的论文需进一步调大。
- **数据库**：SQLite 适合单机 MVP；多用户高并发时迁移到 PostgreSQL（届时同步外置任务登记表，后续迭代）。
- **LLM 并发**：注意服务商 API 的 RPM/TPM 限流。

### 5.4 监控与日志

- uvicorn 访问日志已开启，可通过 `docker compose logs` 查看。
- 建议接入日志采集（如 Loki / ELK）与健康检查告警。
- 健康检查端点：`GET /api/health`。

---

## 六、常见问题（FAQ）

**Q：上传 PDF 报错？**
A：新版 pypdf 需文件流，已在 `pdf_parser.py` 用 `io.BytesIO` 处理；扫描版 PDF（无文本层）会提示"未提取到文本"，需用户自行处理（系统不提供 OCR）。

**Q：上传自己的研究数据（Word / Excel）报错？**
A：文献只接受 PDF；**.docx / .xlsx 属于「作者自有材料」**，请在上传区下方的研究材料板块提交。材料的文本提取是纯本地操作（docx / xlsx 直接读 OOXML，不引入额外依赖），毫秒级返回，不走后台任务。旧版 `.doc` 与扫描版 PDF 不支持，请另存为新格式后重传。

**Q：生成内容是占位文本？**
A：说明未配置 LLM。检查 `LLM_API_KEY` 是否已设置并重启后端，`GET /api/meta` 的 `llm_configured` 应为 `true`。

**Q：如何切换 LLM 服务商？**
A：修改 `LLM_BASE_URL` 与 `LLM_MODEL` 即可，任何 OpenAI 兼容接口均支持。

**Q：数据如何备份？**
A：**先认准自己在用哪一种部署方式——三种形态的库不在同一个地方**（这是最容易备份错的地方，照着另一种的路径去找会找不到文件）：

| 形态 | 数据库文件 | 备注 |
| --- | --- | --- |
| **桌面版（exe）** | `~/.academic_writer/app.db` | Windows 即 `C:\Users\<你>\.academic_writer\app.db`。**不在程序目录里**，删掉解压出来的文件夹不影响它 |
| 源码运行 | `backend/data/app.db` | 相对 `backend/` |
| Docker | `academic_data` volume | 用 `docker compose cp` 或 `docker run --rm -v` 导出 |

**整个目录一起备更省事**：桌面版直接备 `~/.academic_writer/`（里面除 `app.db` 还有 `config.json`，即你填的 LLM 配置）。备份前建议先关掉应用——SQLite 运行中可能还有未落盘的 WAL 内容（同目录的 `-wal` / `-shm` 两个文件要一起备，否则可能丢最后一段写入）。

**Q：桌面版双击没反应 / 打开就退？**
A：多半是**只拷了 exe**。它启动时要读同目录下的 `resources/`、若干 `.dll` 与 `.pak`——那些不在 exe 里。解压**整个 zip**再运行；给别人传也要传整个 zip 或整个文件夹。详见 [方式一](#二方式一桌面版解压即用零安装) 的限制说明。

**Q：桌面版和源码运行，数据是同一份吗？**
A：**不是。** 桌面版的库在 `~/.academic_writer/app.db`，源码运行的在 `backend/data/app.db`（见上表）。在源码模式下建的项目，打开桌面版看不到——两者互不相通。
