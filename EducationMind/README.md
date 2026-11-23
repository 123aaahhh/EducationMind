# EducationAgent（EchoMind）

面向 K12 与高等教育的多 Agent 在线辅导系统。后端基于 FastAPI，前端基于 Vue 3，支持流式讲解、作业图片识别、学习闭环、混合检索知识库、对话记忆、在线评测与监控。

> 当前版本：2.0.0

---

## 目录

- [核心能力](#核心能力)
- [系统架构](#系统架构)
- [请求处理流程](#请求处理流程)
- [技术栈](#技术栈)
- [快速开始](#快速开始)
- [端口说明](#端口说明)
- [配置说明](#配置说明)
- [Agent 与路由](#agent-与路由)
- [RAG 知识库](#rag-知识库)
- [API 一览](#api-一览)
- [评测与监控](#评测与监控)
- [测试](#测试)
- [项目结构](#项目结构)
- [部署与数据](#部署与数据)
- [上线前置条件](#上线前置条件)
- [常见问题](#常见问题)

---

## 核心能力

- **多 Agent 协作**：TutorAgent 负责知识点讲解，StudyCoachAgent 负责学习计划，ExamAdvisorAgent 负责考试咨询；跨领域问题会拆成主 Agent + 辅助 Agent 并行处理，再统一合成。
- **流式讲解**：`/chat/stream` 通过 SSE 推送「识别问题 → 检索资料 → 生成讲解」阶段进度，支持中途取消。
- **富内容渲染**：前端支持 Markdown、KaTeX 数学公式、代码高亮，并使用 DOMPurify 做安全过滤。
- **作业图片识别**：上传作业图片后由多模态模型识别文字与 LaTeX 公式，原图保留 7 天供人工核验。
- **学习闭环**：提示 → 分步讲解 → 同类练习 → 错题记录 → 掌握度，答错自动进入错题本。
- **会话管理**：会话列表、重命名、搜索、继续学习、删除，刷新页面可恢复对话。
- **混合检索 RAG**：查询改写 + 向量检索 + BM25 + RRF 融合 + LLM 重排，按知识类型分集合存储，自动过滤过期知识。
- **可解释路由**：每次响应返回 `intent`、`primary_agent`、`supporting_agents`、`routing_reason` 和打分，便于定位与评测。
- **在线评测**：意图评测 + 对话质量评测，LLM-as-Judge 与确定性检查结合，支持回归基线和 `suite_id` 版本隔离。
- **可观测性**：Prometheus 指标、健康检查、Agent 降权/恢复、工具调用轨迹。
- **MCP 扩展**：内置标准 MCP 客户端与服务端，可接入外部数据源。

---

## 系统架构

```
┌──────────────────────────────┐
│  Vue 3 前端                   │
│  Vite dev :5173 / Nginx :5174 │
└──────────────┬───────────────┘
               │ HTTP / SSE
┌──────────────▼───────────────┐
│  Nginx 网关 :8088             │
└──────────────┬───────────────┘
               │
┌──────────────▼─────────────────────────────────────────────┐
│  FastAPI 应用 :8000                                         │
│                                                            │
│  API 层：鉴权 / 幂等 / 会话 / 学习闭环 / OCR / 知识库 / 评测  │
│      │                                                     │
│  AgentOrchestrator                                         │
│      ├─ IntentRecognizer（LLM + 向量 + 规则三路融合）        │
│      ├─ 路由与多 Agent 协作判定                             │
│      ├─ Tutor / StudyCoach / ExamAdvisor / General / ...    │
│      ├─ ResponseComposer（并行结果统一改写）                │
│      └─ ToolManager                                        │
│            ├─ 混合 RAG（向量 + BM25 + RRF + LLM 重排）       │
│            ├─ MCP 外部工具                                  │
│            └─ 作业/学习相关工具                              │
└──────┬────────────────┬──────────────────┬─────────────────┘
       │                │                  │
┌──────▼──────┐  ┌──────▼──────┐  ┌────────▼────────┐
│ Redis :16379│  │ Chroma :8001│  │ Prometheus :9090│
│ 会话/记忆   │  │ 向量与知识库 │  │ 指标与告警       │
└─────────────┘  └─────────────┘  └─────────────────┘
```

可选的 Java 后端（`docker-compose.web.yml`）运行在 `:8080`，前端可通过 `/api/java` 访问；Python 后端仍是默认主链路。

---

## 请求处理流程

1. **鉴权与幂等**：校验 API Token（生产环境），按 `client_request_id` 保证重试幂等。
2. **读取上下文**：从 Redis 读取最近对话，必要时做记忆压缩与用户画像注入。
3. **意图识别**：LLM 分类、向量相似度、规则匹配三路融合，输出意图、置信度与分组。
4. **Agent 路由**：意图先验 + 领域关键词 + 实体打分选出主 Agent；协作规则决定是否需要辅助 Agent；首分句优先纠正主从顺序。
5. **并行执行**：主 Agent 与辅助 Agent 并行运行，共享同一轮聊天预算；预算不足时跳过整轮降级和模型汇总。
6. **工具调用**：按需检索知识库、调用 MCP、识别作业图片，工具结果写入轨迹。
7. **结果合成**：`ResponseComposer` 合并主/辅内容并去重；模型不可用时退化为确定性拼接。
8. **持久化与观测**：写回会话记忆、更新掌握度、记录延迟与工具轨迹、暴露 Prometheus 指标。

---

## 技术栈

| 层级 | 选型 |
|---|---|
| 后端 | Python 3.11 / 3.12、FastAPI 0.115、Uvicorn、Pydantic 2 |
| LLM | Anthropic SDK，兼容 DeepSeek 的 Anthropic 入口 |
| 对话存储 | Redis 7 |
| 向量存储 | ChromaDB 0.5.23 |
| RAG Embedding | 本地 ONNX `BAAI/bge-small-zh-v1.5`（512 维，CLS 池化） |
| 词法检索 | 自实现 BM25（字符二元组 + 英文/公式分词） |
| 前端 | Vue 3.5、Vite 5、Marked、KaTeX、Highlight.js、DOMPurify |
| 监控 | prometheus-client、Prometheus |
| 扩展 | 标准 Model Context Protocol（MCP） |
| 部署 | Docker Compose、Nginx |

---

## 快速开始

### 1. 准备环境

- Docker Desktop（建议分配 8 GB 以上内存）
- 可选：Python 3.11+（本地调试）、Node.js 18+（前端开发）

### 2. 创建配置

```bash
cp .env.example .env
```

至少填写模型 Key 和入口地址：

```dotenv
ANTHROPIC_API_KEY=你的Key
ANTHROPIC_MODEL=deepseek-v4-pro
ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic

APP_ENV=development
ECHOMIND_ENV=development
```

生产环境还需要配置 `ECHOMIND_API_TOKENS`，详见[部署与数据](#部署与数据)。

### 3. 启动后端与依赖

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health
```

启动后：

- 健康检查：http://127.0.0.1:8000/health
- Swagger UI：http://127.0.0.1:8000/docs
- 经网关访问：http://127.0.0.1:8088/docs

首次启动会自动下载 RAG Embedding 模型（约 95 MB）到 `data/models/bge-small-zh-v1.5`。离线环境可预置模型文件并设置 `ECHOMIND_EMBEDDING_AUTO_DOWNLOAD=false`。

### 4. 启动前端

```bash
cd ../Frontend
npm install
npm run dev
```

打开 http://127.0.0.1:5173。Vite 已配置代理：`/api/python` → `:8000`，`/api/java` → `:8080`。

生产构建：

```bash
npm run build
```

### 5. 发一条测试消息

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message":"什么是二次函数的顶点公式？","user_id":"demo","conv_id":"demo-1"}'
```

---

## 端口说明

| 端口 | 服务 | 说明 |
|---|---|---|
| 8000 | FastAPI | 主后端，Swagger 在 `/docs` |
| 8088 | Nginx 网关 | 代理 Python 后端，SSE 长连接统一超时 |
| 5173 | Vite dev server | 前端开发 |
| 5174 | 前端容器 | `docker-compose.web.yml` 中的 Nginx |
| 8080 | Java 后端 | 可选，前端 `/api/java` |
| 8001 | ChromaDB | 向量数据库 |
| 16379 | Redis | 会话、记忆、幂等键 |
| 9090 | Prometheus | 指标采集 |

---

## 配置说明

完整配置见 [.env.example](.env.example)。常用项：

| 配置组 | 关键变量 | 说明 |
|---|---|---|
| 模型 | `ANTHROPIC_API_KEY`、`ANTHROPIC_MODEL`、`ANTHROPIC_BASE_URL` | Anthropic 兼容入口 |
| 运行 | `APP_ENV`、`ECHOMIND_ENV`、`LOG_LEVEL` | 生产环境会收紧鉴权与本地降级 |
| Redis | `REDIS_URL`、`REDIS_PASSWORD` | 会话、记忆、限流 |
| Chroma | `CHROMA_HOST`、`CHROMA_PORT`、`CHROMA_PERSIST_DIRECTORY` | 生产连接失败会中止启动 |
| 超时 | `ECHOMIND_CHAT_TIMEOUT_S`、`ECHOMIND_LLM_TIMEOUT_S`、`ECHOMIND_COMPOSER_TIMEOUT_S` | 应用基准 90s，边缘 100s，客户端 105s |
| RAG | `ECHOMIND_EMBEDDING_MODEL`、`ECHOMIND_EMBEDDING_DIR`、`ECHOMIND_AUTO_REINDEX` | 中文 Embedding 与旧集合迁移 |
| 上传 | `ECHOMIND_MAX_IMAGE_MB`、`ECHOMIND_UPLOAD_RETENTION_DAYS` | 作业图片大小与保留期 |
| 意图向量 | `INTENT_EMBEDDING_PROVIDER`、`SILICONFLOW_API_KEY` | 有 Key 用在线向量，否则本地 n-gram |
| MCP | `MCP_EXTERNAL_ENABLED`、`MCP_SERVERS_CONFIG` | 外部工具接入 |
| 鉴权 | `ECHOMIND_API_TOKENS` | JSON 格式，至少 24 位随机 Token |

**超时链**：应用侧 `90s` 是唯一基准，边缘 Nginx 固定为 `100s`，前端客户端为 `105s`。修改基准时三处需要同步，避免页面先超时而后台仍在生成。

修改配置后：

```bash
# 只改 .env
docker compose up -d --force-recreate echomind

# 改了代码或依赖
docker compose up -d --build echomind
```

---

## Agent 与路由

| Agent | 职责 |
|---|---|
| `tutor` | 知识点讲解、作业引导、分步解题 |
| `study_coach` | 学习计划、复习安排、课程推荐 |
| `exam_advisor` | 考试政策、报名时间、成绩查询说明 |
| `general` | 学习咨询接待、信息澄清、兜底 |
| `escalation` | 转人工与交接 |

路由规则：

1. **打分**：意图先验（主意图 0.85）+ 领域关键词（0.15/0.18/0.20）+ 实体加分。
2. **主从**：最高分为主 Agent；协作规则独立判断需要并行的辅助 Agent。
3. **首分句优先**：按「，；。顺便/另外/同时」切句，第一个命中领域词的分句决定主 Agent，另一个领域降为辅助。
4. **可解释**：`routing_reason` 输出 `primary`、`supporting`、各 Agent 分数和 `first_clause`。
5. **兜底**：主 Agent 失败时降级到 `GeneralAgent`；剩余预算不足 15s 时不再整轮重试。

示例响应字段：

```json
{
  "intent": "knowledge_query",
  "primary_agent": "tutor",
  "supporting_agents": ["study_coach"],
  "routing_reason": "intent=knowledge_query, primary=tutor, supporting=study_coach, scores=[...], first_clause=tutor"
}
```

---

## RAG 知识库

### 检索链路

```
用户查询
  ├─ 查询改写（LLM 生成 3 个子查询，保留原查询）
  ├─ 每个子查询并行混合召回
  │    ├─ Chroma 向量召回（cosine）
  │    └─ BM25 词法召回
  │    └─ RRF 融合（k=60）→ TopK
  ├─ 跨子查询按内容哈希去重
  ├─ LLM 重排（输出相关性索引序列）
  └─ 返回最终 TopK
```

### 存储设计

- 按知识类型分 5 个集合：教材、练习、答案解析、考试政策、课程介绍。
- 集合名带 `_bge_zh` 后缀，与旧 384 维集合隔离，避免维度混写。
- 每个片段保留「学科 → 年级 → 章节 → 知识点 → 子节」层级路径，以及来源、版本、有效期等元数据。
- 片段约 500 字，公式、表格、代码单独切分；过期内容在查询时自动过滤。
- 旧集合在首次启动时自动重嵌入到新集合，按片段数判断是否完成。

### 配置

```dotenv
ECHOMIND_EMBEDDING_MODEL=bge-small-zh-v1.5
ECHOMIND_EMBEDDING_DIR=./data/models/bge-small-zh-v1.5
ECHOMIND_EMBEDDING_AUTO_DOWNLOAD=true
ECHOMIND_AUTO_REINDEX=true
```

### 当前边界

- 采用单层分块，没有父子索引；长文档跨片段时可能缺少前后文。
- BM25 索引在进程内构建，多 worker 会各存一份；十万级以上建议外置词法检索。
- LLM 重排失败时退化为召回顺序，响应中 `reranked=false`。

---

## API 一览

| 分类 | 接口 |
|---|---|
| 健康与监控 | `GET /health`、`GET /monitor`、`GET /metrics` |
| 对话 | `POST /chat`、`POST /chat/stream` |
| 会话 | `GET /conversations`、`GET/PATCH/DELETE /conversations/{conv_id}` |
| 学习闭环 | `/learning/hint`、`/learning/steps`、`/learning/practice`、`/learning/mistakes`、`/learning/attempt`、`/learning/mastery` |
| 作业 OCR | `POST /ocr`、`GET /ocr/{image_id}` |
| 知识库 | `POST /search`、`POST /knowledge/add`、`POST /knowledge/upload`、`GET /knowledge/stats` |
| 评测 | `POST /eval/run`、`GET /eval/jobs/{job_id}` |
| 轨迹 | `GET /trace/tool/{request_id}`、`GET /trace/tools` |
| Skills | `GET /skills`、`POST /skills/reload` |
| 集成 | `GET /integrations/embedding`、`GET /integrations/mcp`、`POST /integrations/mcp/call` |
| MCP Server | `/mcp`（标准 MCP 接入点，支持流式传输） |

完整参数与在线调试见 http://127.0.0.1:8000/docs。

---

## 评测与监控

### 评测

`POST /eval/run` 提交任务后立即返回 `202 + job_id`，后台串行执行；`GET /eval/jobs/{job_id}` 查询进度与结果。

- **意图评测**：准确率、Macro-F1、每类 P/R/F1。
- **对话评测**：真实调用编排器，按相关性、准确性、完整性、有用性四维打分。
- **确定性检查**：`expected_agent`、`expected_supporting_agents`、`expected_intent`、`required_tools`。
- **通过条件**：执行成功 + 确定性检查通过 + `overall >= 0.75`。
- **回归基线**：指标下降超过 5% 记为回归；只有配置完全一致（`suite_id`）才允许比较。
- **任务队列在进程内**：服务重启后历史任务不再可查，生产环境建议外置任务存储。

### 监控

- `GET /metrics`：Prometheus 指标
- `GET /monitor`：Agent 健康状态与降权情况
- `GET /trace/tool/{request_id}`：单次请求的工具调用轨迹
- `GET /trace/tools`：最近工具调用记录

---

## 测试

### 后端

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

说明：

- `requirements-dev.txt` 包含 `pytest`、`pytest-asyncio`、`fakeredis`。
- 未安装 `fakeredis` 时，异步用例会在收集阶段报错。
- 部分集成用例需要本机已启动 Redis / ChromaDB。

### 前端

```bash
cd ../Frontend
npm test
```

前端使用 Node.js 内置测试运行器，覆盖请求状态与评测任务轮询。

---

## 项目结构

```text
EducationMind/
├── agents/            # 编排器、Agent 实现、工具定义
├── api/               # FastAPI 路由与生命周期
├── core/              # 意图识别、学习闭环、技能加载
├── memory/            # Redis/Chroma 记忆与用户画像
├── tooling/           # RAG、Embedding、MCP、工具管理
├── evaluation/        # 评测、Judge、few-shot、任务队列
├── monitor/           # 指标、健康检查、告警
├── skills/            # 业务规则（education、general_customer_service）
├── config/            # Nginx、Prometheus、MCP 配置
├── tests/             # 后端测试
├── data/              # 运行时数据（gitignored）
├── Dockerfile
├── docker-compose.yml
├── docker-compose.web.yml
├── requirements.txt
└── requirements-dev.txt
```

前端：

```text
Frontend/
├── src/
│   ├── App.vue
│   ├── components/    # MarkdownView、RequestInspector、AppIcon
│   └── lib/           # backends、requestState、markdown、evaluationJobs
├── public/runtime-config.js
├── tests/
└── docker/
```

---

## 部署与数据

### 鉴权

生产环境必须设置 `ECHOMIND_ENV=production` 并配置 `ECHOMIND_API_TOKENS`：

```dotenv
ECHOMIND_API_TOKENS={"至少24位随机Token":{"user_id":"u1","role":"user"},"管理员Token至少24位":{"user_id":"admin","role":"admin"}}
```

请求头使用 `Authorization: Bearer <token>`。未配置 Token 时仅允许本地开发访问。

### 数据位置

| 数据 | 位置 |
|---|---|
| Redis | Docker volume `redis-data` |
| ChromaDB | Docker volume `chromadb-data` |
| 作业原图 | `data/uploads`（默认保留 7 天） |
| 知识库模型 | `data/models/bge-small-zh-v1.5` |
| 日志 | `logs/` |

### 生产注意事项

- 生产环境 ChromaDB 连接失败会中止启动，不会静默使用本地目录。
- 示例知识只在 development 且空库时导入，生产空库不会自动写入演示数据。
- 备份时需要同时覆盖 Redis 与 ChromaDB volume；只备份其一会导致会话或向量缺失。
- 对外部署时把域名加入 `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS`。

---

## 上线前置条件

面向真实未成年人用户上线前，必须完成以下能力，当前项目尚未全部实现：

1. 监护人同意与年龄分级
2. 数据最小化采集（仅匿名 ID、年级、学科、学习目标）
3. 明确的数据保留期限与自动清理
4. 账号删除与数据导出（覆盖 Redis、向量库、评测与备份）
5. 全链路敏感信息脱敏（模型输入、存储、日志、学习轨迹）
6. 双向内容安全审核与不可用时的保守降级
7. 心理危机/自伤倾向识别、人工转接与监护人通知机制

在上述能力完成前，不得面向真实未成年人开放。

---

## 常见问题

**前端请求 `/api/python` 报 ECONNREFUSED**

后端未启动或端口未就绪。先确认 `curl http://127.0.0.1:8000/health`，再重启 Vite。

**Docker 启动失败，提示无法连接 Linux 引擎**

启动 Docker Desktop 并确认 WSL2 正常；`docker info` 能返回 ServerVersion 后再执行 compose。

**聊天请求 504 或页面提示超过预算**

先看 `/health` 中 `chat_timeout_ms` 与各 Agent 的 `recent_avg_ms`，再检查应用 90s、边缘 100s、客户端 105s 是否一致。多 Agent 请求耗时接近预算时，优先排查 RAG 与模型调用延迟。

**首次启动卡在下载 Embedding 模型**

确认 `hf-mirror.com` 可访问；离线环境把 `model.onnx`、`tokenizer.json`、`config.json` 放入 `ECHOMIND_EMBEDDING_DIR`，并设置 `ECHOMIND_EMBEDDING_AUTO_DOWNLOAD=false`。

**切换 Embedding 模型后检索为空**

新模型使用 `_bge_zh` 集合，旧集合需要重嵌入。确认 `ECHOMIND_AUTO_REINDEX=true`，并查看启动日志中的「重嵌入旧集合」记录；若新集合已写入但维度不符，删除对应 `_bge_zh` 集合后重启。

**评测任务重启后查不到**

评测队列在进程内，重启即丢失。需要持久化时把任务与结果迁移到 Redis 或数据库。

---

## 相关文档

- 在线接口文档：http://127.0.0.1:8000/docs
- 配置模板：[.env.example](.env.example)
- 教育业务规则：[skills/education/SKILL.md](skills/education/SKILL.md)
- 接待与分流规则：[skills/general_customer_service/SKILL.md](skills/general_customer_service/SKILL.md)
