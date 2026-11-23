"""
EducationAgent 在线教育辅导系统 — FastAPI 入口

启动时打印小熊饼干图案。
所有核心组件在 lifespan 中初始化，通过环境变量配置。
"""
import asyncio
import base64
import json
import logging
import mimetypes
import os
import pathlib
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional


_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response, UploadFile, File, Form, Query, Request as HTTPRequest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

load_dotenv()

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ── 聊天超时预算（单一基准）──────────────────────────────────────────────────
# 以应用侧预算为基准，边缘代理和客户端依次留出余量：
#   应用 90s < 边缘 100s < 客户端 105s
# 应用先自行收口并返回 503，避免页面已超时而后台仍在生成。
CHAT_TIMEOUT_S = max(1, int(os.getenv("ECHOMIND_CHAT_TIMEOUT_S", "90")))
CHAT_EDGE_TIMEOUT_S = CHAT_TIMEOUT_S + 10
CHAT_CLIENT_TIMEOUT_MS = (CHAT_TIMEOUT_S + 15) * 1000

# 作业图片：保留原图供学生/家长人工核验，超过保留期的文件在启动时清理。
UPLOAD_DIR = pathlib.Path(os.getenv("ECHOMIND_UPLOAD_DIR", "").strip() or str(pathlib.Path(_ROOT) / "data" / "uploads"))
MAX_IMAGE_BYTES = int(os.getenv("ECHOMIND_MAX_IMAGE_MB", "8")) * 1024 * 1024
UPLOAD_RETENTION_DAYS = max(1, int(os.getenv("ECHOMIND_UPLOAD_RETENTION_DAYS", "7")))
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
LEARNING_TIMEOUT_S = max(5, int(os.getenv("ECHOMIND_LEARNING_TIMEOUT_S", "30")))

STAGE_LABELS = {
    "identify": "识别问题",
    "retrieve": "检索资料",
    "generate": "生成讲解",
}

BANNER = r"""
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
   ╔══════════════════════╗
   ║   EducationAgent     ║
   ║   在线教育辅导系统    ║
   ╚══════════════════════╝
    ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ  ʕ•ᴥ•ʔ
"""

# ── 全局组件（lifespan 中初始化）─────────────────────────────────────────────
_orchestrator = None
_memory       = None
_tool_manager = None
_monitor      = None
_evaluator    = None
_eval_orchestrator = None
_eval_tool_manager = None
_skill_manager = None
_profile_updater = None
_mcp_client = None
_llm_client = None
_learning_store = None
from evaluation.jobs import EvaluationJobs
_eval_jobs = EvaluationJobs()

def _anthropic_cfg() -> Dict[str, Any]:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("未设置 ANTHROPIC_API_KEY")
    cfg: Dict[str, Any] = {
        "api_key":  key,
        "model":    os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022").strip(),
    }
    base_url = os.getenv("ANTHROPIC_BASE_URL", "").strip()
    if base_url:
        cfg["base_url"] = base_url
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _eval_orchestrator, _eval_tool_manager, _orchestrator, _memory, _tool_manager, _monitor, _evaluator, _skill_manager, _profile_updater, _mcp_client
    global _llm_client, _learning_store

    from anthropic import AsyncAnthropic
    from core.access import identity
    from core.learning_store import LearningStore
    identity("")  # 启动时验证生产鉴权配置
    print(BANNER, flush=True)

    from agents.agent_orchestrator import AgentOrchestrator, Request, build_shared_rag_tools
    from core.intent_recognizer import IntentRecognizer
    from evaluation.evaluator import EndToEndEvaluator
    from tooling.knowledge_base import KnowledgeBase
    from tooling.tool_manager import MCPToolManager, Tool
    from memory.conversation_memory import MemoryManager
    from monitor.performance_monitor import PerformanceMonitor
    from core.skill_loader import SkillManager

    cfg = _anthropic_cfg()
    logger.info(f"模型: {cfg['model']}  base_url: {cfg.get('base_url', '(官方)')}")

    # 作业 OCR / 学习闭环共用的模型客户端。
    llm_kwargs: Dict[str, Any] = {"api_key": cfg["api_key"]}
    if cfg.get("base_url"):
        llm_kwargs["base_url"] = cfg["base_url"]
    _llm_client = AsyncAnthropic(**llm_kwargs)

    # 作业原图保留目录；启动时清理超过保留期的文件。
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - UPLOAD_RETENTION_DAYS * 86400
    removed = 0
    for item in UPLOAD_DIR.glob("*"):
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("已清理 %d 张超过 %d 天保留期的作业图片", removed, UPLOAD_RETENTION_DAYS)

    # 意图识别器（Orchestrator 内部也会创建，这里单独暴露给 Evaluator）
    recognizer = IntentRecognizer(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    # Skills：启动时从目录加载业务能力说明，并在 Agent 调用 LLM 时动态注入。
    skills_dir = os.getenv("ECHOMIND_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills"))
    _skill_manager = SkillManager(
        root_dir=skills_dir,
        max_prompt_chars=int(os.getenv("ECHOMIND_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    _skill_manager.load()

    # Agent 编排器
    _orchestrator = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=_skill_manager,
    )

    # 记忆管理器（Redis 工作记忆 + ChromaDB 情景记忆/用户画像）
    _memory = MemoryManager(
        redis_url=os.getenv("REDIS_URL", "redis://redis:6379/0"),
        chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/app/data/chroma"),
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )
    _learning_store = LearningStore(_memory.redis)

    # MCP 工具管理器 + RAG 知识库（基于 ChromaDB 的真实检索）
    _tool_manager = MCPToolManager(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )
    kb = KnowledgeBase(
        chroma_host=os.getenv("CHROMA_HOST", "chromadb"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/app/data/chroma"),
    )
    logger.info(f"知识库已加载: {await kb.doc_count_async()} 个文档片段")

    def knowledge_fallback(params: Dict[str, Any], context: Optional[Dict[str, Any]], error: str):
        query = params.get("query", "")
        return [{
            "title": "知识库降级结果",
            "content": f"知识库暂时不可用，未能完成对“{query}”的语义检索。请稍后重试。",
            "score": 0.0,
            "fallback": True,
            "error": error,
        }]

    _tool_manager.register(Tool(
        name="knowledge_search",
        description="检索教材、练习题、答案解析、考试政策和课程介绍（基于 ChromaDB 向量检索，自动过滤过期知识）",
        handler=kb.search_handler,
        schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
                "resource_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(RESOURCE_TYPES)},
                    "maxItems": 5,
                    "uniqueItems": True,
                },
                "subject": {"type": "string", "minLength": 1, "maxLength": 80},
                "grade": {"type": "string", "minLength": 1, "maxLength": 80},
                "chapter": {"type": "string", "minLength": 1, "maxLength": 120},
                "knowledge_point": {"type": "string", "minLength": 1, "maxLength": 120},
            },
            "required": ["query"],
        },
        cache_ttl=300.0,
        supports_rerank=True,
        fallback=knowledge_fallback,
    ))
    from tooling.mcp_client import StandardMCPClient, load_servers
    from tooling.mcp_server import configure
    configure(kb, _tool_manager)
    mcp_config = pathlib.Path(os.getenv("MCP_SERVERS_CONFIG", str(pathlib.Path(_ROOT) / "config/mcp_servers.json")))
    enabled = os.getenv("MCP_EXTERNAL_ENABLED", "true").lower() in {"1", "true", "yes"}
    _mcp_client = StandardMCPClient(_tool_manager, load_servers(mcp_config) if enabled else [], _refresh_agent_tools)
    _refresh_agent_tools()
    await _mcp_client.start()

    # 性能监控（可选启动 Prometheus）
    prom_port = int(os.getenv("PROMETHEUS_PORT", "0")) or None
    _monitor = PerformanceMonitor(
        orchestrator=_orchestrator,
        tool_manager=_tool_manager,
        interval_s=float(os.getenv("MONITOR_INTERVAL", "10")),
        webhook_url=os.getenv("ALERT_WEBHOOK_URL") or None,
        prometheus_port=prom_port,
    )
    await _monitor.start()

    # 评测与线上使用独立路由状态、工具缓存和熔断器。
    _eval_tool_manager = _tool_manager.fork()
    _eval_orchestrator = AgentOrchestrator(api_key=cfg["api_key"], base_url=cfg.get("base_url"),
        model=cfg["model"], skill_manager=_skill_manager)
    _refresh_agent_tools()
    _evaluator = EndToEndEvaluator(
        orchestrator=_eval_orchestrator,
        recognizer=recognizer,
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        baseline_path=os.getenv("EVAL_BASELINE_PATH", "/app/data/eval/baseline.json"),
    )

    from core.concurrency import ProfileUpdater
    _profile_updater = ProfileUpdater(_memory, workers=int(os.getenv("PROFILE_UPDATE_WORKERS", "4")))
    _profile_updater.start()
    logger.info("EducationAgent 已就绪")
    async with standard_mcp_server.session_manager.run():
        try:
            yield
        finally:
            await _eval_jobs.close()
            await _profile_updater.close()
            await _mcp_client.close()
            await _monitor.stop()
            if _llm_client is not None:
                try:
                    await _llm_client.close()
                except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                    pass
            if _memory is not None:
                await _memory.close()
            configure(None)
            logger.info("EducationAgent 已关闭")


def _refresh_agent_tools():
    from agents.tools import build_shared_rag_tools
    if _orchestrator is not None and _tool_manager is not None:
        tools = build_shared_rag_tools(_tool_manager)
        if _mcp_client is not None:
            tools.update(_mcp_client.agent_tools())
        _orchestrator.set_shared_tools(tools)
    if _eval_orchestrator is not None:
        from dataclasses import replace
        for name in list(_eval_tool_manager._tools):
            if name not in _tool_manager._tools:
                _eval_tool_manager.unregister(name)
        for name, tool in _tool_manager._tools.items():
            if name not in _eval_tool_manager._tools or _eval_tool_manager._tools[name].handler is not tool.handler or _eval_tool_manager._tools[name].schema != tool.schema:
                _eval_tool_manager.register(replace(tool))
        eval_tools = build_shared_rag_tools(_eval_tool_manager)
        if _mcp_client is not None:
            eval_tools.update(_mcp_client.agent_tools(_eval_tool_manager))
        _eval_orchestrator.set_shared_tools({name: spec for name, spec in eval_tools.items() if spec.retry_safe})


from tooling.mcp_server import http_app as standard_mcp_app, server as standard_mcp_server


# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="EducationAgent 在线教育辅导",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def access_control(request: HTTPRequest, call_next):
    from core.access import identity
    from fastapi.responses import JSONResponse
    try:
        account = identity(request.headers.get("authorization", ""))
    except (ValueError, TypeError):
        return JSONResponse({"detail": "服务鉴权配置无效"}, status_code=503)
    request.state.account = account
    if account is not None and request.url.path != "/health" and request.method != "OPTIONS":
        if not account:
            return JSONResponse({"detail": "需要有效 Bearer Token"}, status_code=401)
        reader = (
            request.url.path in {"/chat", "/chat/stream", "/search", "/knowledge/stats", "/skills", "/mcp", "/mcp/"}
            or request.url.path.startswith(("/conversations", "/learning/", "/ocr"))
        )
        if not reader and account["role"] != "admin":
            return JSONResponse({"detail": "该接口需要管理员权限"}, status_code=403)
    is_chat = request.url.path == "/chat" and request.method == "POST"
    started = time.monotonic()
    if is_chat and _monitor is not None and "requests_total" in _monitor._prom:
        _monitor._prom["requests_total"].inc()
    response = await call_next(request)
    if is_chat:
        elapsed = (time.monotonic() - started) * 1000
        response.headers["Server-Timing"] = f"chat;dur={elapsed:.1f}"
        if _monitor is not None and "chat_latency_ms" in _monitor._prom:
            _monitor._prom["chat_latency_ms"].labels(status=str(response.status_code)).observe(elapsed)
    return response


# ── 请求/响应模型 ─────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message:     str = Field(min_length=1, max_length=12000)
    client_request_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    user_id:     str = "anonymous"
    conv_id:     Optional[str] = None


class ChatResponse(BaseModel):
    conv_id:     str
    request_id:  str = ""
    response:    str
    intent:      str
    intent_group: str = "other"
    agent_type:  str
    agent_types: List[str] = Field(default_factory=list)
    primary_agent: str = ""
    supporting_agents: List[str] = Field(default_factory=list)
    tools_used: List[str] = Field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0
    escalated:   bool
    latency_ms:  float
    knowledge_used: bool = False
    entities: Dict[str, List[str]] = Field(default_factory=dict)
    intent_confidence: float = 0.0
    intent_source_scores: Dict[str, float] = Field(default_factory=dict)
    intent_embedding_source: str = "local-ngram"
    status: Literal["success", "partial", "failed"] = "success"
    retryable: bool = False
    error_code: Optional[str] = None


class ToolTraceResponse(BaseModel):
    request_id: str
    found: bool
    trace: Dict[str, Any] = Field(default_factory=dict)


class RecentToolTracesResponse(BaseModel):
    items: List[Dict[str, Any]] = Field(default_factory=list)


class ConversationRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=60)


class MistakeRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    answer: str = Field(default="", max_length=4000)
    analysis: str = Field(default="", max_length=2000)
    topic: str = Field(default="", max_length=40)


class AttemptRequest(BaseModel):
    topic: str = Field(default="", max_length=40)
    correct: bool = False


class LearningQuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    topic: str = Field(default="", max_length=40)
    context: str = Field(default="", max_length=4000)


class PracticeRequest(BaseModel):
    topic: str = Field(default="", max_length=40)
    question: str = Field(default="", max_length=4000)


# ── 路由 ──────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return {
        "status": "ok",
        "chat_timeout_ms": CHAT_CLIENT_TIMEOUT_MS,
        "chat_edge_timeout_s": CHAT_EDGE_TIMEOUT_S,
        "agents": {} if os.getenv("ECHOMIND_API_TOKENS") else _orchestrator.get_stats(),
    }


@app.get("/skills", tags=["Skills"])
async def skills_summary():
    """查看当前已加载的 Skills，便于确认热加载结果和排查解析错误。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    return _skill_manager.summary()


@app.post("/skills/reload", tags=["Skills"])
async def reload_skills():
    """运行时重新扫描 Skill 目录，不需要重启服务。"""
    if _skill_manager is None:
        raise HTTPException(503, "Skills 未初始化")
    _skill_manager.reload()
    if _orchestrator is not None:
        _orchestrator.set_skill_manager(_skill_manager)
    return _skill_manager.summary()


async def _process_chat(req: ChatRequest, http_request: HTTPRequest, on_stage=None) -> ChatResponse:
    """
    主对话流程（/chat 与 /chat/stream 共用）：
      记忆读取 → 意图识别 → Agent 路由 → 执行 → 记忆写入
    """
    if _orchestrator is None or _memory is None:
        raise HTTPException(503, "服务未就绪")

    account = getattr(http_request.state, "account", None)
    if account:
        req.user_id = account["user_id"]
    from agents.agent_orchestrator import Request as OrcReq

    client_id = req.client_request_id or str(uuid.uuid4())
    conv_id = req.conv_id or str(uuid.uuid5(uuid.NAMESPACE_URL, req.user_id + ":" + client_id))
    started = time.monotonic()

    from core.concurrency import ConversationBusyError, LeaseLostError
    try:
        async with asyncio.timeout(CHAT_TIMEOUT_S), _memory.conversation(req.user_id, conv_id):
            receipt = await _memory.get_turn_receipt(req.user_id, conv_id, client_id)
            if receipt:
                if receipt["message"] != req.message:
                    raise HTTPException(409, "同一 client_request_id 不能用于不同消息")
                previous = ChatResponse(**receipt["response"])
                if previous.status != "failed" or not previous.retryable:
                    return previous
            # 1. 读取记忆上下文
            mem_ctx = await _memory.get_context(req.user_id, conv_id, query=req.message)

            # 2. 构建编排请求（含对话历史，用于意图识别上下文）
            history = [
                {"role": m.role.value, "content": m.content}
                for m in mem_ctx.recent_messages[-5:]
            ] if mem_ctx.recent_messages else None

            intent_result = await _orchestrator.recognize_intent(req.message, history=history)
            full_context = mem_ctx.to_prompt_text()

            orch_req = OrcReq(
                message=req.message,
                user_id=req.user_id,
                conv_id=conv_id,
                context=full_context,
                history=history,
                entities=intent_result.entities,
                intent=intent_result.intent,
                intent_group=intent_result.intent_group,
                urgency=intent_result.urgency,
                intent_confidence=intent_result.confidence,
                conversation_state=getattr(mem_ctx, "state", {}),
                on_stage=on_stage,
                deadline=started + CHAT_TIMEOUT_S,
            )

            async def before_unsafe_tool(name):
                # 在产生副作用之前持久化；超时/崩溃后同一 ID 不会重放未知结果。
                marker = ChatResponse(conv_id=conv_id, request_id=orch_req.request_id,
                    response="该请求涉及可能修改数据的工具，请先核验执行结果再发起新的请求。",
                    intent=intent_result.intent.value, agent_type="unknown", escalated=False,
                    latency_ms=(time.monotonic() - started) * 1000,
                    status="failed", retryable=False, error_code="tool_outcome_unknown")
                await _memory.commit_turn(req.user_id, conv_id, client_id, req.message, marker.model_dump())
            orch_req.before_unsafe_tool = before_unsafe_tool

            # 3. 执行
            result = await _orchestrator.run(orch_req)

            response = ChatResponse(
                conv_id=conv_id,
                request_id=result.request_id,
                response=result.response,
                intent=result.intent.value if result.intent else "other",
                intent_group=intent_result.intent_group,
                agent_type=result.agent_type.value,
                agent_types=[agent_type.value for agent_type in result.agent_types],
                primary_agent=result.primary_agent.value if result.primary_agent else result.agent_type.value,
                supporting_agents=[agent_type.value for agent_type in result.supporting_agents],
                tools_used=result.tools_used,
                routing_reason=result.routing_reason,
                routing_confidence=result.routing_confidence,
                escalated=result.escalated,
                latency_ms=round((time.monotonic() - started) * 1000, 1),
                knowledge_used=any(t.get("tool_name") == "search_knowledge_base" and t.get("result_success") is True for t in result.tool_traces),
                entities=orch_req.entities,
                intent_confidence=round(intent_result.confidence, 4),
                intent_source_scores=intent_result.source_scores,
                intent_embedding_source=getattr(intent_result, "embedding_source", "local-ngram"),
                status=result.status,
                retryable=result.retryable,
                error_code=result.error_code,
            )
            await _memory.commit_turn(req.user_id, conv_id, client_id, req.message, response.model_dump(),
                                      _orchestrator.get_tool_trace(result.request_id), state=orch_req.conversation_state)
            if response.status != "failed" and _profile_updater is not None:
                _profile_updater.submit(req.user_id, conv_id)
            return response
    except TimeoutError as ex:
        raise HTTPException(504, f"本轮处理超过 {CHAT_TIMEOUT_S} 秒，请使用相同 client_request_id 重试") from ex
    except (ConversationBusyError, LeaseLostError) as ex:
        raise HTTPException(503, str(ex)) from ex


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, http_request: HTTPRequest):
    """非流式对话，保留给脚本和兼容客户端。"""
    reply = await _process_chat(req, http_request)
    if reply.status == "failed":
        from fastapi.responses import JSONResponse
        return JSONResponse(reply.model_dump(), status_code=503)
    return reply


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chunk_text(text: str, size: int = 48):
    """把完整回答切成小块，前端可边接收边渲染。"""
    for start in range(0, len(text), size):
        yield text[start:start + size]


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, http_request: HTTPRequest):
    """
    流式对话（SSE）：
      stage  识别问题 / 检索资料 / 生成讲解
      delta  回答文本分片
      result 完整响应元数据
      error  错误信息
    客户端断开时取消后台任务，避免继续消耗模型额度。
    """
    if _orchestrator is None or _memory is None:
        raise HTTPException(503, "服务未就绪")

    queue: asyncio.Queue = asyncio.Queue()

    async def on_stage(stage: str, detail: Dict[str, Any]):
        await queue.put(("stage", {"stage": stage, "label": STAGE_LABELS.get(stage, stage),
                                   "detail": detail}))

    async def runner():
        try:
            response = await _process_chat(req, http_request, on_stage=on_stage)
            await queue.put(("result", response))
        except HTTPException as ex:
            await queue.put(("error", {"status": ex.status_code, "message": ex.detail}))
        except asyncio.CancelledError:
            raise
        except Exception as ex:  # noqa: BLE001 - 通过 SSE 返回错误，避免连接静默中断
            logger.exception("流式对话失败")
            await queue.put(("error", {"status": 500, "message": str(ex)}))

    task = asyncio.create_task(runner())

    async def event_stream():
        last_ping = time.monotonic()
        try:
            yield _sse("stage", {"stage": "identify", "label": STAGE_LABELS["identify"], "detail": {}})
            while True:
                if await http_request.is_disconnected():
                    task.cancel()
                    return
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    if time.monotonic() - last_ping >= 15:
                        last_ping = time.monotonic()
                        yield ": ping\n\n"
                    continue
                last_ping = time.monotonic()
                if kind == "stage":
                    yield _sse("stage", payload)
                elif kind == "error":
                    yield _sse("error", payload)
                    return
                else:
                    response: ChatResponse = payload
                    yield _sse("stage", {"stage": "generate", "label": STAGE_LABELS["generate"], "detail": {}})
                    for chunk in _chunk_text(response.response or ""):
                        yield _sse("delta", {"text": chunk})
                        await asyncio.sleep(0.012)
                    yield _sse("result", response.model_dump())
                    return
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── 会话管理 ──────────────────────────────────────────────────────────────────
def _resolve_user(http_request: HTTPRequest, user_id: Optional[str]) -> str:
    account = getattr(http_request.state, "account", None)
    if account:
        return account["user_id"]
    return (user_id or "anonymous").strip() or "anonymous"


@app.get("/conversations")
async def list_conversations(
    http_request: HTTPRequest,
    user_id: str = Query(default="anonymous", max_length=128),
    q: str = Query(default="", max_length=120),
    limit: int = Query(default=50, ge=1, le=100),
):
    """会话列表：按更新时间倒序，支持标题/摘要搜索。"""
    if _memory is None:
        raise HTTPException(503, "服务未就绪")
    items = await _memory.list_conversations(_resolve_user(http_request, user_id), query=q, limit=limit)
    return {"items": items, "total": len(items)}


@app.get("/conversations/{conv_id}")
async def get_conversation(
    conv_id: str,
    http_request: HTTPRequest,
    user_id: str = Query(default="anonymous", max_length=128),
):
    """读取指定会话的消息，用于刷新页面后恢复或继续学习。"""
    if _memory is None:
        raise HTTPException(503, "服务未就绪")
    return await _memory.get_conversation(_resolve_user(http_request, user_id), conv_id)


@app.patch("/conversations/{conv_id}")
async def rename_conversation(
    conv_id: str,
    body: ConversationRenameRequest,
    http_request: HTTPRequest,
    user_id: str = Query(default="anonymous", max_length=128),
):
    if _memory is None:
        raise HTTPException(503, "服务未就绪")
    try:
        renamed = await _memory.rename_conversation(_resolve_user(http_request, user_id), conv_id, body.title)
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex
    if not renamed:
        raise HTTPException(404, "会话不存在或已过期")
    return {"conv_id": conv_id, "title": body.title.strip()[:60]}


@app.delete("/conversations/{conv_id}")
async def delete_conversation(
    conv_id: str,
    http_request: HTTPRequest,
    user_id: str = Query(default="anonymous", max_length=128),
):
    if _memory is None:
        raise HTTPException(503, "服务未就绪")
    await _memory.delete_conversation(_resolve_user(http_request, user_id), conv_id)
    return {"conv_id": conv_id, "deleted": True}


# ── 作业图片 OCR ──────────────────────────────────────────────────────────────
OCR_PROMPT = """你是作业题目识别助手。请只提取图片中的题目内容：
1. 保留题号、选项、已知条件和单位；
2. 数学公式使用 LaTeX（行内 $...$，独立公式 $$...$$）；
3. 不要解题，不要添加解释或评价；
4. 如果图片不是作业题目，简短说明图片内容。"""


@app.post("/ocr")
async def ocr_homework(file: UploadFile = File(...)):
    """识别作业照片中的文字与公式，并保留原图供人工核验。"""
    if _llm_client is None:
        raise HTTPException(503, "模型服务未就绪")
    media_type = (file.content_type or "").lower()
    if media_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(415, "仅支持 JPG、PNG、WEBP、GIF 图片")
    data = await file.read()
    if not data:
        raise HTTPException(400, "图片为空")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, f"图片不能超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MB")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    image_id = uuid.uuid4().hex
    suffix = mimetypes.guess_extension(media_type) or ".png"
    path = UPLOAD_DIR / f"{image_id}{suffix}"
    path.write_bytes(data)

    result: Dict[str, Any] = {
        "image_id": image_id,
        "image_url": f"/ocr/{image_id}",
        "original_name": file.filename or "",
        "media_type": media_type,
        "text": "",
        "warning": "",
    }
    try:
        from core.llm_utils import extract_text_content
        resp = await asyncio.wait_for(_llm_client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022").strip(),
            max_tokens=1500,
            temperature=0.0,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(data).decode(),
                    }},
                    {"type": "text", "text": OCR_PROMPT},
                ],
            }],
        ), timeout=LEARNING_TIMEOUT_S)
        result["text"] = extract_text_content(resp.content).strip()
    except Exception as ex:  # noqa: BLE001 - 原图已保留，允许学生手动输入题目
        logger.warning("作业图片识别失败: %s", ex)
        result["warning"] = "自动识别失败，原图已保留，请手动补充题目文字。"
    return result


@app.get("/ocr/{image_id}")
async def get_ocr_image(image_id: str):
    """回看原始作业图片，供学生、家长或老师人工核验。"""
    safe_id = "".join(ch for ch in image_id if ch.isalnum())
    if not safe_id or safe_id != image_id:
        raise HTTPException(400, "图片标识无效")
    for suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        path = UPLOAD_DIR / f"{safe_id}{suffix}"
        if path.exists():
            return FileResponse(path)
    raise HTTPException(404, "图片不存在或已超过保留期")


# ── 学习闭环 ──────────────────────────────────────────────────────────────────
async def _learning_llm(prompt: str, max_tokens: int = 900) -> str:
    if _llm_client is None:
        raise HTTPException(503, "模型服务未就绪")
    from core.llm_utils import extract_text_content
    try:
        resp = await asyncio.wait_for(_llm_client.messages.create(
            model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022").strip(),
            max_tokens=max_tokens,
            temperature=0.3,
            system="你是在线教育辅导老师，面向中小学生和高中生。讲解准确、循序渐进而非直接给答案，数学公式使用 LaTeX。",
            messages=[{"role": "user", "content": prompt}],
        ), timeout=LEARNING_TIMEOUT_S)
    except asyncio.TimeoutError as ex:
        raise HTTPException(504, "学习辅导生成超时，请稍后重试") from ex
    text = extract_text_content(resp.content).strip()
    if not text:
        raise HTTPException(502, "模型未返回有效内容")
    return text


@app.post("/learning/hint")
async def learning_hint(body: LearningQuestionRequest):
    """只给提示，不公布完整答案，鼓励学生自己往下想。"""
    prompt = (
        f"题目或问题：{body.question}\n\n"
        "请给出一个简短的思考提示（1-3 句话），点出关键概念或第一步，但不要给出最终答案，也不要展开完整步骤。"
    )
    return {"hint": await _learning_llm(prompt, max_tokens=300)}


@app.post("/learning/steps")
async def learning_steps(body: LearningQuestionRequest):
    """分步讲解，便于学生按步骤理解而不是只看结论。"""
    prompt = (
        f"题目或问题：{body.question}\n\n"
        "请按步骤讲解：先说明解题思路，再逐步推导，每一步交代依据，最后总结可迁移的方法。"
        "使用 Markdown 列表组织步骤，公式使用 LaTeX。"
    )
    return {"steps": await _learning_llm(prompt, max_tokens=1200)}


@app.post("/learning/practice")
async def learning_practice(body: PracticeRequest):
    """生成同类型练习题，附答案与解析，由前端先隐藏答案。"""
    prompt = (
        f"知识点：{body.topic or '未指定'}\n"
        f"参考题目：{body.question or '未提供'}\n\n"
        "请生成一道同类型、难度相近但情境不同的练习题。只输出 JSON，格式："
        '{"question": "题目", "answer": "答案", "explanation": "简要解析", "topic": "知识点"}。'
        "数学公式使用 LaTeX，不要在 JSON 外添加文字。"
    )
    raw = await _learning_llm(prompt, max_tokens=800)
    data = _extract_json_object(raw) or {"question": raw, "answer": "", "explanation": "", "topic": body.topic}
    data.setdefault("topic", body.topic or "未分类")
    return data


@app.post("/learning/mistakes")
async def add_mistake(body: MistakeRequest, http_request: HTTPRequest,
                      user_id: str = Query(default="anonymous", max_length=128)):
    if _learning_store is None:
        raise HTTPException(503, "服务未就绪")
    item = await _learning_store.add_mistake(
        _resolve_user(http_request, user_id), body.question, body.answer, body.analysis, body.topic,
    )
    return {"item": item}


@app.get("/learning/mistakes")
async def list_mistakes(http_request: HTTPRequest,
                        user_id: str = Query(default="anonymous", max_length=128),
                        limit: int = Query(default=50, ge=1, le=200)):
    if _learning_store is None:
        raise HTTPException(503, "服务未就绪")
    items = await _learning_store.list_mistakes(_resolve_user(http_request, user_id), limit=limit)
    return {"items": items, "total": len(items)}


@app.delete("/learning/mistakes/{mistake_id}")
async def delete_mistake(mistake_id: str, http_request: HTTPRequest,
                         user_id: str = Query(default="anonymous", max_length=128)):
    if _learning_store is None:
        raise HTTPException(503, "服务未就绪")
    removed = await _learning_store.delete_mistake(_resolve_user(http_request, user_id), mistake_id)
    if not removed:
        raise HTTPException(404, "错题不存在")
    return {"id": mistake_id, "deleted": True}


@app.post("/learning/attempt")
async def record_attempt(body: AttemptRequest, http_request: HTTPRequest,
                         user_id: str = Query(default="anonymous", max_length=128)):
    """记录一次练习结果，实时更新知识点掌握度。"""
    if _learning_store is None:
        raise HTTPException(503, "服务未就绪")
    return await _learning_store.record_attempt(
        _resolve_user(http_request, user_id), body.topic, bool(body.correct),
    )


@app.get("/learning/mastery")
async def learning_mastery(http_request: HTTPRequest,
                           user_id: str = Query(default="anonymous", max_length=128)):
    if _learning_store is None:
        raise HTTPException(503, "服务未就绪")
    return await _learning_store.summary(_resolve_user(http_request, user_id))


def _extract_json_object(raw: str) -> Optional[Dict[str, Any]]:
    start, end = raw.find("{"), raw.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    try:
        value = json.loads(raw[start:end])
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


@app.get("/monitor")
async def monitor_summary():
    """实时监控摘要：Agent 成功率、工具统计、告警、优化建议。"""
    if _monitor is None:
        raise HTTPException(503, "服务未就绪")
    return _monitor.summary()


@app.get("/trace/tool/{request_id}", response_model=ToolTraceResponse)
async def get_tool_trace(request_id: str):
    """查看某次请求的工具调用明细。"""
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    trace = await _memory.get_trace(request_id) if _memory else None
    trace = trace or _orchestrator.get_tool_trace(request_id)
    return ToolTraceResponse(
        request_id=request_id,
        found=trace is not None,
        trace=trace or {},
    )


@app.get("/trace/tools", response_model=RecentToolTracesResponse)
async def list_recent_tool_traces(limit: int = 20):
    """查看最近 N 次请求的工具调用明细。"""
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return RecentToolTracesResponse(items=await _memory.recent_traces(limit) if _memory else _orchestrator.get_recent_tool_traces(limit=limit))


@app.get("/metrics")
async def prometheus_metrics():
    """Prometheus 指标入口。"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/search")
async def search(
    query: str = Query(min_length=1, max_length=2000),
    top_k: int = Query(default=5, ge=1, le=20),
    resource_types: Optional[str] = Query(default=None, description="逗号分隔：textbook,exercise,solution,policy,course"),
    subject: Optional[str] = Query(default=None, max_length=80),
    grade: Optional[str] = Query(default=None, max_length=80),
    chapter: Optional[str] = Query(default=None, max_length=120),
    knowledge_point: Optional[str] = Query(default=None, max_length=120),
):
    """
    演示检索优化链路：查询改写 → 并行召回 → 重排 → Top-K。
    可按知识类型与章节路径过滤；过期知识不会返回。
    """
    if _tool_manager is None:
        raise HTTPException(503, "服务未就绪")
    extra: Dict[str, Any] = {}
    if resource_types:
        types = [item.strip() for item in resource_types.split(",") if item.strip()]
        invalid = [item for item in types if item not in RESOURCE_TYPES]
        if invalid:
            raise HTTPException(400, f"未知知识类型: {', '.join(invalid)}")
        extra["resource_types"] = types
    for key, value in (("subject", subject), ("grade", grade),
                       ("chapter", chapter), ("knowledge_point", knowledge_point)):
        if value:
            extra[key] = value
    result = await _tool_manager.search_with_rewrite(
        "knowledge_search", query, top_k=top_k, extra_params=extra or None,
    )
    return {"query": query, "results": result.data, "reranked": result.reranked}


RESOURCE_TYPES = ("textbook", "exercise", "solution", "policy", "course")


class DocInput(BaseModel):
    """单篇文档输入，包含章节路径与来源/有效期等完整元数据。"""
    title:   str
    content: str
    document_id: Optional[str] = None
    resource_type: Optional[Literal["textbook", "exercise", "solution", "policy", "course"]] = "textbook"
    subject: Optional[str] = None
    grade: Optional[str] = None
    chapter: Optional[str] = None
    knowledge_point: Optional[str] = None
    subsection: Optional[str] = None
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    publisher: Optional[str] = None
    published_at: Optional[str] = None
    effective_at: Optional[str] = None
    expires_at: Optional[str] = None
    region: Optional[str] = None
    textbook_version: Optional[str] = None
    version: Optional[str] = None


class BatchDocInput(BaseModel):
    """批量文档导入请求体。"""
    documents: List[DocInput]


class EvalIntentInput(BaseModel):
    """意图识别评测用例。"""
    message: str
    expected_intent: str
    context: Optional[Dict[str, Any]] = None


EvalAgentName = Literal["general", "tutor", "study_coach", "exam_advisor", "escalation"]


class EvalDialogInput(BaseModel):
    """对话质量评测用例。question 单轮，turns 多轮。"""
    question: Optional[str] = None
    reference_answer: Optional[str] = Field(default=None, max_length=12000)
    evidence: Optional[str] = Field(default=None, max_length=20000)
    turns: Optional[List[str]] = Field(default=None, max_length=50)
    user_id: Optional[str] = None
    conv_id: Optional[str] = None
    expected_agent: Optional[EvalAgentName] = None
    expected_supporting_agents: List[EvalAgentName] = Field(default_factory=list, max_length=5)
    expected_intent: Optional[str] = None
    required_tools: List[str] = Field(default_factory=list, max_length=20)
    expected_entities: Dict[str, List[str]] = Field(default_factory=dict)


class EvalRunInput(BaseModel):
    """评测请求。为空时使用内置默认用例。"""
    intent_cases: Optional[List[EvalIntentInput]] = Field(default=None, max_length=200)
    dialog_cases: Optional[List[EvalDialogInput]] = Field(default=None, max_length=100)


@app.post("/knowledge/add", tags=["知识库"])
async def add_knowledge(body: BatchDocInput):
    """
    批量导入文档到知识库。

    文档按结构切片（公式、表格、代码独立成块，普通正文约 500 字），
    并按 resource_type 写入独立集合：textbook / exercise / solution / policy / course。
    每个片段保存 学科 → 年级 → 章节 → 知识点 → 子节 路径与来源、有效期等元数据。
    已过期的片段不会再参与检索。

    示例请求体：
    ```json
    {
      "documents": [
        {
          "title": "二次函数顶点公式",
          "content": "二次函数 f(x)=ax²+bx+c 的顶点坐标为 (-b/2a, (4ac-b²)/4a)，对称轴为 x=-b/2a。",
          "resource_type": "textbook",
          "subject": "高中数学", "grade": "高一", "chapter": "函数",
          "knowledge_point": "二次函数", "subsection": "顶点公式",
          "source_name": "人教版高中数学必修一", "publisher": "人民教育出版社",
          "textbook_version": "人教版2019", "region": "全国",
          "effective_at": "2025-09-01", "expires_at": "2030-08-31",
          "source_url": "https://example.edu.cn/math/quadratic"
        },
        {
          "title": "2027 年高考报名时间",
          "content": "报名时间以省教育考试院公告为准。",
          "resource_type": "policy",
          "subject": "高考", "grade": "高三", "chapter": "考试政策",
          "knowledge_point": "报名时间",
          "source_name": "某省教育考试院", "publisher": "某省教育考试院",
          "published_at": "2026-10-01", "effective_at": "2026-10-01",
          "expires_at": "2027-03-01", "region": "某省",
          "source_url": "https://example.edu.cn/policy/2027"
        }
      ]
    }
    ```
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    try:
        count = await kb.add_documents_async([d.model_dump(exclude_none=True) for d in body.documents])
    except ValueError as ex:
        raise HTTPException(400, str(ex)) from ex
    finally:
        _tool_manager.invalidate_cache("knowledge_search")
        if _eval_tool_manager is not None:
            _eval_tool_manager.invalidate_cache("knowledge_search")
    total = await kb.doc_count_async()
    return {"message": f"成功导入 {count} 个文档片段", "added_chunks": count, "total_chunks": total}


@app.post("/knowledge/upload", tags=["知识库"])
async def upload_knowledge(file: UploadFile = File(...), metadata: Optional[str] = Form(default=None)):
    """
    上传文件导入知识库。

    支持格式：
    - `.txt` / `.md`：整个文件作为一篇文档，文件名作为标题
    - `.json`：JSON 数组格式 `[{"title": "...", "content": "...", ...}, ...]`

    表单字段 `metadata` 可传 JSON 对象，作为 txt/md 文档的默认元数据
    （学科、年级、章节、资源类型、来源、有效期等）。文件大小限制：10MB。
    """
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__

    filename = file.filename or "unknown"
    if pathlib.Path(filename).suffix.lower() not in {".txt", ".md", ".json"}:
        raise HTTPException(400, "仅支持 .txt、.md、.json 文件")
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(413, "文件大小超过 10MB 限制")

    text = content.decode("utf-8", errors="ignore")

    defaults: Dict[str, Any] = {}
    if metadata:
        try:
            parsed = json.loads(metadata)
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"metadata 不是合法 JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise HTTPException(400, "metadata 必须是 JSON 对象")
        defaults = parsed

    if filename.lower().endswith(".json"):
        try:
            docs = json.loads(text)
            if not isinstance(docs, list):
                raise HTTPException(400, "JSON 文件应为数组格式: [{title, content}, ...]")
        except json.JSONDecodeError as e:
            raise HTTPException(400, f"JSON 解析失败: {e}")
    else:
        # txt / md：整个文件作为一篇文档
        title = filename.rsplit(".", 1)[0] if "." in filename else filename
        docs = [{"title": title, "content": text}]

    try:
        validated = [
            DocInput.model_validate({**defaults, **doc}).model_dump(exclude_none=True)
            for doc in docs
        ]
        count = await kb.add_documents_async(validated)
    except ValueError as ex:
        raise HTTPException(400, "文档格式不正确：需要非空 title、content 和可选元数据") from ex
    finally:
        _tool_manager.invalidate_cache("knowledge_search")
        if _eval_tool_manager is not None:
            _eval_tool_manager.invalidate_cache("knowledge_search")
    total = await kb.doc_count_async()
    return {
        "message": f"文件 {filename} 导入成功",
        "added_chunks": count,
        "total_chunks": total,
    }


@app.get("/knowledge/stats", tags=["知识库"])
async def knowledge_stats():
    """查看知识库统计信息：片段总数与各知识类型分布。"""
    tool = _tool_manager._tools.get("knowledge_search") if _tool_manager else None
    if tool is None:
        raise HTTPException(503, "知识库未初始化")
    kb = tool.handler.__self__
    return {
        "total_chunks": await kb.doc_count_async(),
        "by_resource_type": await asyncio.to_thread(kb.doc_count_by_type),
    }


@app.post("/eval/run", status_code=202)
async def run_eval(body: Optional[EvalRunInput] = None):
    if _evaluator is None:
        raise HTTPException(503, "服务未就绪")
    snapshot = body.model_copy(deep=True) if body else None
    try:
        return _eval_jobs.submit(lambda progress: _execute_eval(snapshot, progress))
    except RuntimeError as ex:
        raise HTTPException(429, str(ex)) from ex


@app.get("/eval/jobs/{job_id}")
async def get_eval_job(job_id: str):
    job = _eval_jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "评测任务不存在或已过期；服务重启后需要重新提交")
    return job


async def _execute_eval(body: Optional[EvalRunInput] = None, progress=None):
    """运行内置评测用例，返回评测报告。"""
    if _evaluator is None:
        raise HTTPException(503, "服务未就绪")
    from evaluation.evaluator import DEFAULT_DIALOG_CASES, DEFAULT_INTENT_CASES, IntentTestCase

    if body and body.intent_cases is not None:
        intent_cases = [
            IntentTestCase(
                message=c.message,
                expected_intent=c.expected_intent,
                context=c.context,
            )
            for c in body.intent_cases
        ]
    else:
        intent_cases = DEFAULT_INTENT_CASES

    if body and body.dialog_cases is not None:
        dialog_cases = [
            c.model_dump(exclude_none=True)
            for c in body.dialog_cases
        ]
    else:
        dialog_cases = DEFAULT_DIALOG_CASES

    report = await _evaluator.run(
        intent_cases=intent_cases,
        dialog_cases=dialog_cases,
        progress=progress,
    )
    return {
        "pass_rate":       report.pass_rate,
        "scope":           "orchestrator",
        "status":          report.status,
        "suite_id":        report.suite_id,
        "regression_note": report.regression_note,
        "valid_total":     report.valid_total,
        "evaluation_errors": report.evaluation_errors,
        "total":           report.total,
        "passed":          report.passed,
        "avg_scores":      report.avg_scores,
        "regressions":     report.regressions,
        "recommendations": report.recommendations,
        "results": [
            {
                "test_id": r.test_id,
                "passed": r.passed,
                "scores": r.scores,
                "detail": r.detail,
                "metadata": r.metadata,
            }
            for r in report.results
        ],
    }


class MCPCallInput(BaseModel):
    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)


@app.get("/integrations/embedding", tags=["Embedding"])
async def embedding_status():
    if _orchestrator is None:
        raise HTTPException(503, "服务未就绪")
    return _orchestrator._intent_recognizer._embedding.status()


@app.get("/integrations/mcp", tags=["MCP"])
async def mcp_status():
    """查看外部标准 MCP 连接状态和已发现的白名单工具。"""
    if _mcp_client is None:
        raise HTTPException(503, "MCP 尚未初始化")
    return _mcp_client.summary()


@app.post("/integrations/mcp/call", tags=["MCP"])
async def mcp_call(body: MCPCallInput):
    """调试已配置的外部 MCP 工具，无需先调用 LLM。"""
    if _mcp_client is None or _tool_manager is None:
        raise HTTPException(503, "MCP 尚未初始化")
    if not _mcp_client.owns_tool(body.name):
        raise HTTPException(404, "MCP 工具未连接或未在配置白名单中")
    result = await _tool_manager.call(body.name, body.arguments)
    return {"success": result.success, "data": result.data, "error": result.error,
            "cached": result.cached, "latency_ms": round(result.latency_ms, 1)}


app.mount("/mcp", standard_mcp_app)


# ── 交互式 CLI ────────────────────────────────────────────────────────────────
async def _cli():
    print(BANNER)
    print("EducationAgent CLI — 输入 quit 退出\n")

    from agents.agent_orchestrator import AgentOrchestrator, Request
    from memory.conversation_memory import MemoryManager, MsgRole
    from core.skill_loader import SkillManager

    cfg = _anthropic_cfg()
    skill_manager = SkillManager(
        root_dir=os.getenv("ECHOMIND_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills")),
        max_prompt_chars=int(os.getenv("ECHOMIND_SKILLS_MAX_PROMPT_CHARS", "5000")),
    )
    skill_manager.load()
    orch = AgentOrchestrator(
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
        skill_manager=skill_manager,
    )
    mem  = MemoryManager(
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        chroma_host=os.getenv("CHROMA_HOST", "localhost"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
        chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/tmp/chroma"),
        api_key=cfg["api_key"],
        base_url=cfg.get("base_url"),
        model=cfg["model"],
    )

    from tooling.tool_manager import MCPToolManager, Tool
    from tooling.knowledge_base import KnowledgeBase
    from tooling.mcp_client import StandardMCPClient, load_servers
    from agents.tools import build_shared_rag_tools
    manager = MCPToolManager(**cfg)
    kb = KnowledgeBase(chroma_host=os.getenv("CHROMA_HOST", "localhost"),
                       chroma_port=int(os.getenv("CHROMA_PORT", "8000")),
                       chroma_path=os.getenv("CHROMA_PERSIST_DIRECTORY", "/tmp/chroma"))
    manager.register(Tool(name="knowledge_search", description="本地知识库检索",
                          handler=kb.search_handler, schema={"type":"object", "properties":{
                              "query":{"type":"string"}, "top_k":{"type":"integer", "minimum":1}},
                              "required":["query"]}, cache_ttl=300, supports_rerank=True))
    bridge = None
    def refresh():
        tools = build_shared_rag_tools(manager)
        if bridge is not None:
            tools.update(bridge.agent_tools())
        orch.set_shared_tools(tools)
    path = pathlib.Path(os.getenv("MCP_SERVERS_CONFIG", str(pathlib.Path(_ROOT) / "config/mcp_servers.json")))
    enabled = os.getenv("MCP_EXTERNAL_ENABLED", "true").lower() in {"1", "true", "yes"}
    bridge = StandardMCPClient(manager, load_servers(path) if enabled else [], refresh)
    refresh()
    await bridge.start()

    user_id, conv_id = "cli_user", str(uuid.uuid4())

    try:
        while True:
            try:
                msg = (await asyncio.to_thread(input, "你: ")).strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见 ʕ•ᴥ•ʔ")
                break
            if not msg or msg.lower() in ("quit", "exit", "退出"):
                print("再见 ʕ•ᴥ•ʔ")
                break

            ctx = await mem.get_context(user_id, conv_id, query=msg)
            history = [
                {"role": m.role.value, "content": m.content}
                for m in ctx.recent_messages[-5:]
            ] if ctx.recent_messages else None
            req = Request(message=msg, user_id=user_id, conv_id=conv_id, context=ctx.to_prompt_text(), history=history)
            result = await orch.run(req)

            await mem.add_message(user_id, conv_id, MsgRole.USER, msg)
            await mem.add_message(user_id, conv_id, MsgRole.ASSISTANT, result.response)

            print(f"\nEducationAgent [{result.agent_type.value}]: {result.response}\n")
    finally:
        await bridge.close()
        await manager._client.close()
        await mem.close()


if __name__ == "__main__":
    if "--cli" in sys.argv:
        asyncio.run(_cli())
    else:
        uvicorn.run(
            "api.main:app",
            host=os.getenv("API_HOST", "0.0.0.0"),
            port=int(os.getenv("API_PORT", "8000")),
            reload=os.getenv("APP_ENV") == "development",
        )
