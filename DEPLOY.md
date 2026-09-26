# 部署方案

本文档说明 AI 学术写作辅助系统的部署方式，覆盖本地开发、Docker 一键部署、生产部署三种场景。

## 一、部署架构

```
用户浏览器
    │
    ▼
Nginx（前端静态文件 + 反向代理 /api）
    │
    ▼ /api/*
FastAPI 后端（uvicorn 单进程，原因见 4.3）
    │
    ├── SQLite 数据库（持久化卷）
    └── LLM（DeepSeek / 通义 / Moonshot 等 OpenAI 兼容接口）
```

- **前端**：React 构建产物（静态文件），由 Nginx 托管。
- **后端**：FastAPI + uvicorn，处理 Agent 工作流与业务逻辑。
- **数据**：SQLite 文件，通过 Docker volume 持久化。
- **LLM**：通过环境变量配置外部 API。

---

## 二、方式一：本地开发（已在使用）

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

## 三、方式二：Docker 一键部署

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

## 四、方式三：生产部署要点

### 4.1 环境变量清单

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `LLM_API_KEY` | 是 | LLM 服务商 API Key |
| `LLM_BASE_URL` | 否 | OpenAI 兼容 base_url（DeepSeek 填 `https://api.deepseek.com/v1`） |
| `LLM_MODEL` | 否 | 模型名（默认 `deepseek-chat`） |
| `LLM_TEMPERATURE` | 否 | 采样温度（默认 0.3） |
| `DATABASE_PATH` | 否 | 数据库路径（Docker 内默认 `/app/data/app.db`） |
| `DATABASE_WAL` | 否 | SQLite WAL，**默认 `true`**。置 `false` **只影响新建的库**（已是 WAL 的库不会被降级，见 4.3）；生产不建议关闭，也不要写进 `.env` |

### 4.2 安全建议

1. **不要将 `.env` 提交到版本库**，使用服务器环境变量或密钥管理服务（如 KMS、Vault）。
2. **API Key 轮换**：Key 泄露后立即在服务商后台重置。
3. **HTTPS**：生产环境需在 Nginx 前加 TLS（如 Let's Encrypt / 云负载均衡）。
4. **限流与鉴权**：当前 MVP 无用户系统，公网部署需加认证层（后续迭代）。

### 4.3 性能与约束

- **必须单进程**：启动 uvicorn 时**不要**加 `--workers`。运行中的生成任务登记在进程内
  字典里（`tasks._TASKS` / `projects._GEN_TASKS`），"同一项目同一时刻只有一个生成在跑"
  的 409 互斥完全依赖它——多 worker 后每个 worker 各持一份登记表，互斥整体失效，
  同一项目会被两个 worker 同时生成：双倍 token 消耗、状态互相覆盖。当前单机规模下
  单进程 + 异步 IO 足够；真扛不住时的正确路径是把任务登记表外置（如 Redis），而不是加 worker。
- **数据库 WAL**：`db.get_conn()` 里 `PRAGMA journal_mode = WAL`（读不阻塞写），由环境变量
  `DATABASE_WAL` 控制、**默认开** —— 前端进度轮询不会被生成写入挡住。唯一的例外是测试：
  `tests/conftest.py` 的 `tmp_db` 夹具会在 `init_db()` **之前**把它压成 `false`，因为 WAL 下
  每个连接干净关闭都要建/拆一次 `-wal` / `-shm`，237 个建库用例会把全量 pytest 从约 5 分钟
  拖到约 24 分钟。**不要在生产关闭它，也不要把它写进 `.env`**（写了也只影响新建的库，容易
  误导排障）。
- **上传体积**：批量 PDF 上传按"整批之和"计体积，`frontend/nginx.conf` 已设
  `client_max_body_size 200m`（默认 1m 会导致上传 413）；单篇更大时相应调大。
- **长文生成超时**：LLM 分段生成耗时较长，Nginx `proxy_read_timeout` 已设为 300s；若生成更长的论文需进一步调大。
- **数据库**：SQLite 适合单机 MVP；多用户高并发时迁移到 PostgreSQL（届时同步外置任务登记表，后续迭代）。
- **LLM 并发**：注意服务商 API 的 RPM/TPM 限流。

### 4.4 监控与日志

- uvicorn 访问日志已开启，可通过 `docker compose logs` 查看。
- 建议接入日志采集（如 Loki / ELK）与健康检查告警。
- 健康检查端点：`GET /api/health`。

---

## 五、常见问题（FAQ）

**Q：上传 PDF 报错？**
A：新版 pypdf 需文件流，已在 `pdf_parser.py` 用 `io.BytesIO` 处理；扫描版 PDF（无文本层）会提示"未提取到文本"，需用户自行处理（系统不提供 OCR）。

**Q：上传自己的研究数据（Word / Excel）报错？**
A：文献只接受 PDF；**.docx / .xlsx 属于「作者自有材料」**，请在上传区下方的研究材料板块提交。材料的文本提取是纯本地操作（docx / xlsx 直接读 OOXML，不引入额外依赖），毫秒级返回，不走后台任务。旧版 `.doc` 与扫描版 PDF 不支持，请另存为新格式后重传。

**Q：生成内容是占位文本？**
A：说明未配置 LLM。检查 `LLM_API_KEY` 是否已设置并重启后端，`GET /api/meta` 的 `llm_configured` 应为 `true`。

**Q：如何切换 LLM 服务商？**
A：修改 `LLM_BASE_URL` 与 `LLM_MODEL` 即可，任何 OpenAI 兼容接口均支持。

**Q：数据如何备份？**
A：备份 SQLite 文件（本地 `backend/data/app.db`，Docker 为 `academic_data` volume）。
