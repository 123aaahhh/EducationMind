"""
RAG 知识库 —— 基于 ChromaDB 的真实检索实现。

设计要点：
  1. 章节路径：每个片段记录 学科 → 年级 → 章节 → 知识点 → 子节，并写入片段正文前缀，
     使向量化后的检索更稳定。
  2. 结构化切分：数学公式、表格、代码独立成块；表格重复表头；代码按函数/类聚合。
  3. 完整元数据：学科、年级、来源、有效期、教材版本等随片段一起存储。
  4. 分类集合：教材、练习题、答案解析、考试政策、课程介绍使用独立 collection，
     避免全部混在一个检索空间。
  5. 易变知识：由带来源与有效期的文档维护，过期后不再参与检索。

ChromaDB 在这里的角色：
  - memory/ 中用于存储对话记忆（情景记忆 + 用户画像）
  - 这里用于存储知识库文档（RAG 检索）
  两者是不同的 collection，互不干扰。
"""
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

import chromadb

from tooling.embeddings import EMBEDDING_MODEL_ID, build_embedding_function

logger = logging.getLogger(__name__)

# 五类知识分别落在独立集合；textbook 沿用旧集合名，保证已有数据可直接读取。
DEFAULT_RESOURCE_TYPE = "textbook"
RESOURCE_TYPE_COLLECTIONS: Dict[str, str] = {
    "textbook": "knowledge_base",
    "exercise": "kb_exercise",
    "solution": "kb_solution",
    "policy": "kb_policy",
    "course": "kb_course",
}
# 中文 embedding 维度（512）与旧默认模型（384）不兼容，使用独立集合名避免混写。
_EMBEDDING_COLLECTION_SUFFIX = "_bge_zh"
_RESOURCE_TYPE_ALIASES = {
    "textbook": "textbook", "教材": "textbook", "教材知识": "textbook",
    "exercise": "exercise", "练习题": "exercise", "习题": "exercise", "题目": "exercise",
    "solution": "solution", "答案": "solution", "答案解析": "solution", "解析": "solution",
    "policy": "policy", "考试政策": "policy", "政策": "policy",
    "course": "course", "课程": "course", "课程介绍": "course",
}

# 未设置有效期时视为长期有效；2100-01-01 之后视为无失效。
_MAX_TS = 4102444800

# 代码块中的顶层定义，用于把长代码按函数/类拆开。
_CODE_DEF = re.compile(
    r"^(?:async\s+def\s|def\s|class\s|function\s|func\s|fn\s|interface\s|struct\s|impl\s|"
    r"export\s|public\s|private\s|protected\s|static\s)"
)

# 混合检索：BM25 使用中文字符二元组 + 英文/代码词，避免引入额外分词依赖。
_RRF_K = 60
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_LEX_TOKEN_RE = re.compile(r"\\[a-zA-Z]+|[a-zA-Z_][a-zA-Z0-9_.]*|\d+(?:\.\d+)?")


def _bm25_tokens(text: str) -> List[str]:
    """中文按字符二元组切分，英文/数字/LaTeX 命令按词切分。"""
    text = (text or "").lower()
    tokens = [match.group(0) for match in _LEX_TOKEN_RE.finditer(text)]
    for sequence in _CJK_RE.findall(text):
        if len(sequence) == 1:
            tokens.append(sequence)
        else:
            tokens.extend(sequence[i:i + 2] for i in range(len(sequence) - 1))
    return tokens


class _BM25Index:
    """轻量 Okapi BM25，用于和向量检索做 RRF 融合。"""

    def __init__(self, documents: List[List[str]], k1: float = 1.5, b: float = 0.75):
        self._k1, self._b = k1, b
        self._doc_len = [len(tokens) for tokens in documents]
        self._avgdl = sum(self._doc_len) / len(documents) if documents else 0.0
        self._term_freq: List[Dict[str, int]] = []
        doc_freq: Dict[str, int] = {}
        for tokens in documents:
            counts: Dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            self._term_freq.append(counts)
            for token in counts:
                doc_freq[token] = doc_freq.get(token, 0) + 1
        total = len(documents)
        self._idf = {
            token: math.log(1 + (total - freq + 0.5) / (freq + 0.5))
            for token, freq in doc_freq.items()
        }

    def scores(self, query_tokens: List[str]) -> List[float]:
        scores = [0.0] * len(self._term_freq)
        if not self._avgdl:
            return scores
        for token in set(query_tokens):
            idf = self._idf.get(token)
            if idf is None:
                continue
            for index, counts in enumerate(self._term_freq):
                freq = counts.get(token, 0)
                if not freq:
                    continue
                denominator = freq + self._k1 * (
                    1 - self._b + self._b * self._doc_len[index] / self._avgdl
                )
                scores[index] += idf * freq * (self._k1 + 1) / denominator
        return scores


class KnowledgeBase:
    """
    基于 ChromaDB 的 RAG 知识库。

    使用本地 ONNX 模型 bge-small-zh-v1.5（512 维）生成向量，
    调用 add() 时自动生成向量，query() 时自动做语义匹配。
    不需要额外调用 Anthropic Embeddings API。
    """

    COLLECTION_NAME = RESOURCE_TYPE_COLLECTIONS[DEFAULT_RESOURCE_TYPE]

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
    ):
        # 优先连接独立 ChromaDB 服务；默认 embedding_function 在客户端生成向量。
        self._write_lock = threading.RLock()
        self._use_server = False
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            self._use_server = True
            logger.info(f"知识库 ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception as ex:
            if os.getenv("ECHOMIND_ENV", "development").lower() == "production":
                raise RuntimeError("生产环境知识库 ChromaDB 不可用，禁止切换到本地库") from ex
            logger.info(f"知识库 ChromaDB 服务不可用，使用本地模式: {chroma_path}")
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 不传 embedding_function 时使用 ChromaDB 客户端默认模型；
        # 这里显式使用本地中文模型，并写入版本化集合名，避免和旧 384 维向量混用。
        self._embedding = build_embedding_function()
        self._collection_names = {
            resource_type: f"{name}{_EMBEDDING_COLLECTION_SUFFIX}"
            for resource_type, name in RESOURCE_TYPE_COLLECTIONS.items()
        }
        self.COLLECTION_NAME = self._collection_names[DEFAULT_RESOURCE_TYPE]
        logger.info("知识库 embedding 模型: %s", EMBEDDING_MODEL_ID)
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME,
            metadata=self._collection_metadata("EducationAgent RAG 知识库"),
            embedding_function=self._embedding,
        )
        self._warn_legacy_distance(self._collection, self.COLLECTION_NAME)
        # 按知识类型懒加载集合；textbook 直接复用旧集合。
        self._collections: Dict[str, Any] = {DEFAULT_RESOURCE_TYPE: self._collection}

        # 旧集合是 384 维向量，无法原地升级；首次启动时重嵌入到新集合。
        self._migrate_legacy_collections()

        # BM25 索引随写入失效，首次检索时懒构建。
        self._bm25_lock = threading.RLock()
        self._bm25_index: Optional[_BM25Index] = None
        self._bm25_entries: List[Dict[str, Any]] = []
        self._bm25_dirty = True

        # 如果知识库为空，导入默认文档
        seed_demo = os.getenv("ECHOMIND_SEED_DEMO_DOCS", "true").lower() in {"1", "true", "yes"}
        if os.getenv("ECHOMIND_ENV", "development").lower() != "production" and seed_demo and self._collection.count() == 0:
            self._load_default_docs()

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, Any]]) -> int:
        """
        批量导入文档到知识库。

        documents 格式:
            [{
                "title": "...", "content": "...",
                "resource_type": "textbook|exercise|solution|policy|course",
                "subject": "高中数学", "grade": "高一", "chapter": "函数",
                "knowledge_point": "二次函数", "subsection": "顶点公式",
                "source_name": "...", "source_url": "...", "publisher": "...",
                "published_at": "2026-01-01", "effective_at": "2026-01-01",
                "expires_at": "2026-12-31", "region": "全国",
                "textbook_version": "人教版2019", "document_id": "...", "version": "..."
            }, ...]

        长文档会按结构切片（公式、表格、代码单独成块）；默认每片约 500 字。
        """
        # 相同文档身份（同一集合内）在同一批次只采用最后一个版本，避免重复 ID。
        prepared: Dict[tuple, tuple] = {}
        for doc in documents:
            title, content = doc.get("title", ""), doc.get("content", "")
            if not isinstance(title, str) or not isinstance(content, str):
                raise ValueError("文档 title 和 content 必须是字符串")
            if not title.strip() or not content.strip():
                raise ValueError("文档标题和内容不能为空")
            explicit_id = doc.get("document_id")
            if explicit_id is not None and (not isinstance(explicit_id, str) or not explicit_id.strip()):
                raise ValueError("document_id 必须是非空字符串")
            resource_type = self._normalize_resource_type(doc.get("resource_type"))
            # 教材沿用旧身份，保证升级后原有文档不会被重复写入。
            identity_parts = (
                ["explicit", explicit_id]
                if explicit_id
                else ["title", title] if resource_type == DEFAULT_RESOURCE_TYPE
                else ["title", resource_type, title]
            )
            identity = json.dumps(identity_parts, ensure_ascii=True)
            prepared[(resource_type, identity)] = (title, content, bool(explicit_id), doc)

        count = 0
        with self._write_lock:
            for (resource_type, identity), (title, content, explicit, doc) in prepared.items():
                collection = self._collection_for(resource_type)
                document_id = hashlib.sha256(identity.encode()).hexdigest()
                chunks = self._chunk_document(content)
                if not chunks:
                    continue
                version = str(doc.get("version") or "").strip() or hashlib.sha256(content.encode()).hexdigest()
                path = self._chapter_path(doc)
                stored = [self._decorate_chunk(chunk, path) for chunk in chunks]
                ids = [f"{document_id}:{i}" for i in range(len(chunks))]

                existing = collection.get(where={"document_id": document_id}, include=["metadatas"])
                old_ids = set(existing["ids"])
                # 仅迁移归属可验证的旧身份；旧显式 ID 与 title: 前缀冲突时保留旧记录供核验。
                parts = json.loads(identity)
                legacy_value = parts[1] if explicit else f"title:{title}"
                if not explicit or not str(parts[1]).startswith("title:"):
                    legacy_id = hashlib.sha256(legacy_value.encode()).hexdigest()
                    previous = collection.get(where={"document_id": legacy_id}, include=["metadatas"])
                    old_ids.update(i for i, meta in zip(previous["ids"], previous["metadatas"])
                                   if (meta or {}).get("title") == title)
                if not explicit and resource_type == DEFAULT_RESOURCE_TYPE:
                    # 兼容旧版只有 title/chunk_index 元数据的片段。
                    legacy = collection.get(where={"title": title}, include=["metadatas"])
                    old_ids.update(i for i, meta in zip(legacy["ids"], legacy["metadatas"])
                                   if not (meta or {}).get("document_id"))

                metadatas = [
                    self._build_metadata(doc, title, document_id, version, i, len(chunks), resource_type, path)
                    for i in range(len(chunks))
                ]
                collection.upsert(ids=ids, documents=stored, metadatas=metadatas)
                stale = old_ids - set(ids)
                if stale:
                    collection.delete(ids=list(stale))
                count += len(chunks)
        self._bm25_dirty = True
        logger.info("知识库写入 %s 个文档片段", count)
        return count

    async def add_documents_async(self, documents: List[Dict[str, Any]]) -> int:
        """异步导入文档；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.add_documents, documents)

    def search(
        self,
        query: str,
        top_k: int = 5,
        resource_types: Optional[Sequence[str]] = None,
        filters: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        混合检索：向量召回 + BM25 召回，使用 RRF 融合后返回最相关的文档片段。

        - resource_types：限定教材/练习题/答案解析/政策/课程；为空时检索全部类型。
        - filters：按 subject/grade/chapter/knowledge_point 等元数据过滤。
        - 已过期的知识（expires_at 早于当前时间）不会返回。
        """
        if type(top_k) is not int or not 1 <= top_k <= 20:
            raise ValueError("top_k 必须是 1–20 的整数")

        now = int(time.time())
        vector_items = self._vector_candidates(query, top_k, resource_types, filters, now)
        lexical_items = self._bm25_candidates(query, top_k, resource_types, filters, now)

        if lexical_items:
            top_items = self._fuse_results(vector_items, lexical_items, top_k)
        else:
            top_items = sorted(vector_items, key=lambda item: item["score"], reverse=True)[:top_k]
        for item in top_items:
            item.pop("_effective_ts", None)
            item.pop("_expires_ts", None)
        return top_items

    def _vector_candidates(
        self,
        query: str,
        top_k: int,
        resource_types: Optional[Sequence[str]],
        filters: Optional[Dict[str, str]],
        now: int,
    ) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for resource_type, collection in self._target_collections(resource_types):
            try:
                count = collection.count()
            except Exception as ex:  # noqa: BLE001 - 单个集合不可用不应中断整体检索
                logger.warning("知识库集合 %s 不可用: %s", resource_type, ex)
                continue
            if not count:
                continue
            results = collection.query(
                query_texts=[query],
                n_results=min(count, max(top_k * 3, 10)),
            )
            documents = (results.get("documents") or [[]])[0]
            metadatas = (results.get("metadatas") or [[]])[0]
            distances = (results.get("distances") or [[]])[0]
            for doc, meta, dist in zip(documents, metadatas, distances):
                meta = meta or {}
                item = self._result_item(
                    resource_type,
                    doc,
                    meta,
                    dist,
                    score=self._distance_to_score(collection, dist),
                )
                if self._is_valid(item, now) and self._matches(item, filters):
                    items.append(item)
        return items

    def _bm25_candidates(
        self,
        query: str,
        top_k: int,
        resource_types: Optional[Sequence[str]],
        filters: Optional[Dict[str, str]],
        now: int,
    ) -> List[Dict[str, Any]]:
        tokens = _bm25_tokens(query)
        if not tokens:
            return []
        entries, index = self._ensure_bm25_index()
        if index is None or not entries:
            return []

        scores = index.scores(tokens)
        best = max(scores, default=0.0)
        if best <= 0:
            return []

        allowed_types = None
        if resource_types:
            allowed_types = {self._normalize_resource_type(item) for item in resource_types}
        limit = max(top_k * 3, 10)
        items: List[Dict[str, Any]] = []
        for index_position in sorted(range(len(entries)), key=lambda i: scores[i], reverse=True):
            score = scores[index_position]
            if score <= 0:
                break
            entry = entries[index_position]
            if allowed_types and entry["resource_type"] not in allowed_types:
                continue
            item = self._result_item(
                entry["resource_type"],
                entry["content"],
                entry["metadata"],
                1.0 - score / best,
                score=score / best,
            )
            if not (self._is_valid(item, now) and self._matches(item, filters)):
                continue
            item["bm25_score"] = item["score"]
            items.append(item)
            if len(items) >= limit:
                break
        return items

    def _ensure_bm25_index(self) -> tuple[List[Dict[str, Any]], Optional[_BM25Index]]:
        lock = getattr(self, "_bm25_lock", None)
        if lock is None:
            lock = self._bm25_lock = threading.RLock()
        with lock:
            if not getattr(self, "_bm25_dirty", True):
                return self._bm25_entries, self._bm25_index

            entries: List[Dict[str, Any]] = []
            for resource_type, collection in self._target_collections(None, include_empty=True):
                try:
                    data = collection.get(include=["documents", "metadatas"])
                except Exception as ex:  # noqa: BLE001 - 假集合或单集合异常时跳过
                    logger.debug("BM25 索引跳过集合 %s: %s", resource_type, ex)
                    continue
                documents = data.get("documents") or []
                metadatas = data.get("metadatas") or []
                for content, meta in zip(documents, metadatas):
                    if not content:
                        continue
                    meta = meta or {}
                    try:
                        entry_type = self._normalize_resource_type(
                            meta.get("resource_type") or resource_type
                        )
                    except ValueError:
                        entry_type = resource_type
                    entries.append({
                        "resource_type": entry_type,
                        "content": content,
                        "metadata": meta,
                    })

            self._bm25_entries = entries
            self._bm25_index = (
                _BM25Index([_bm25_tokens(entry["content"]) for entry in entries]) if entries else None
            )
            self._bm25_dirty = False
            if entries:
                logger.info("BM25 索引已构建: %s 个片段", len(entries))
            return entries, self._bm25_index

    def _fuse_results(
        self,
        vector_items: List[Dict[str, Any]],
        lexical_items: List[Dict[str, Any]],
        top_k: int,
    ) -> List[Dict[str, Any]]:
        """RRF 融合：按名次累加，不依赖向量与 BM25 的原始分数量纲。"""
        fused: Dict[str, Dict[str, Any]] = {}
        for rank, item in enumerate(
            sorted(vector_items, key=lambda entry: entry["score"], reverse=True)
        ):
            key = self._fusion_key(item)
            entry = fused.setdefault(key, {
                **item,
                "vector_score": item["score"],
                "bm25_score": 0.0,
                "_rrf": 0.0,
            })
            entry["_rrf"] += 1.0 / (_RRF_K + rank + 1)

        for rank, item in enumerate(lexical_items):
            key = self._fusion_key(item)
            if key in fused:
                fused[key]["_rrf"] += 1.0 / (_RRF_K + rank + 1)
                fused[key]["bm25_score"] = max(
                    fused[key]["bm25_score"], item.get("bm25_score", item["score"])
                )
            else:
                fused[key] = {
                    **item,
                    "vector_score": 0.0,
                    "bm25_score": item.get("bm25_score", item["score"]),
                    "_rrf": 1.0 / (_RRF_K + rank + 1),
                }

        items = sorted(fused.values(), key=lambda entry: entry["_rrf"], reverse=True)[:top_k]
        for item in items:
            item["rrf_score"] = round(item.pop("_rrf"), 6)
            if not item.get("vector_score"):
                item["score"] = round(item.get("bm25_score", 0.0), 4)
        return items

    @staticmethod
    def _fusion_key(item: Dict[str, Any]) -> str:
        document_id = item.get("document_id") or ""
        if document_id:
            return f"{item.get('resource_type', '')}:{document_id}:{item.get('chunk', 0)}"
        digest = hashlib.sha1((item.get("content") or "").encode("utf-8")).hexdigest()
        return f"{item.get('resource_type', '')}:{item.get('title', '')}:{item.get('chunk', 0)}:{digest}"

    async def search_async(
        self,
        query: str,
        top_k: int = 5,
        resource_types: Optional[Sequence[str]] = None,
        filters: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """异步检索；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.search, query, top_k, resource_types, filters)

    def get_doc_count(self) -> int:
        return sum(collection.count() for collection in self._all_collections())

    @property
    def doc_count(self) -> int:
        return self.get_doc_count()

    async def doc_count_async(self) -> int:
        """异步获取文档片段数量。"""
        return await asyncio.to_thread(self.get_doc_count)

    def doc_count_by_type(self) -> Dict[str, int]:
        """按知识类型统计片段数量；未写入过的类型返回 0，不额外创建集合。"""
        collections = getattr(self, "_collections", None) or {DEFAULT_RESOURCE_TYPE: self._collection}
        counts: Dict[str, int] = {}
        for resource_type in RESOURCE_TYPE_COLLECTIONS:
            collection = collections.get(resource_type)
            if collection is None:
                counts[resource_type] = 0
                continue
            try:
                counts[resource_type] = collection.count()
            except Exception:  # noqa: BLE001 - 统计失败按 0 处理
                counts[resource_type] = 0
        return counts

    # ── MCP 工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        """
        作为 MCP 工具的 handler 注册。

        MCPToolManager.register(Tool(
            name="knowledge_search",
            handler=kb.search_handler,
            ...
        ))
        """
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        resource_types = params.get("resource_types")
        filters = {
            key: params[key]
            for key in ("subject", "grade", "chapter", "knowledge_point")
            if params.get(key)
        }
        return await self.search_async(
            query,
            top_k=top_k,
            resource_types=resource_types,
            filters=filters or None,
        )

    # ── 集合与元数据 ──────────────────────────────────────────────────────────

    @staticmethod
    def _collection_metadata(description: str) -> Dict[str, str]:
        """新建集合统一使用余弦距离，避免默认 L2 与相似度换算不一致。"""
        return {"description": description, "hnsw:space": "cosine"}

    @staticmethod
    def _collection_space(collection: Any) -> str:
        metadata = getattr(collection, "metadata", None) or {}
        return str(metadata.get("hnsw:space") or "l2").lower()

    def _warn_legacy_distance(self, collection: Any, name: str) -> None:
        """存量集合的 hnsw:space 不可原地修改，检测到非余弦时给出重建提示。"""
        space = self._collection_space(collection)
        if space == "cosine":
            return
        try:
            count = collection.count()
        except Exception:  # noqa: BLE001 - 假集合或客户端不可用时不阻塞启动
            count = 0
        if count:
            logger.warning(
                "知识库集合 %s 仍使用 %s 距离（hnsw:space 不可原地修改）；"
                "score 已按余弦换算，新建集合将使用 cosine，存量集合建议重建",
                name,
                space,
            )

    def _distance_to_score(self, collection: Any, distance: float) -> float:
        """把 Chroma 距离换算为 0~1 余弦相似度。

        默认 embedding 已归一化：L2 是平方欧氏距离，cos = 1 - d/2；
        cosine/ip 距离本身等于 1 - cos，直接取反。结果截断，避免浮点越界。
        """
        space = self._collection_space(collection)
        similarity = 1.0 - distance / 2.0 if space == "l2" else 1.0 - distance
        return max(0.0, min(1.0, similarity))

    def _migrate_legacy_collections(self) -> None:
        """把旧集合中的文档重嵌入到中文模型集合，按片段数判断是否已完成。"""
        if os.getenv("ECHOMIND_AUTO_REINDEX", "true").lower() not in {"1", "true", "yes"}:
            return
        for resource_type, legacy_name in RESOURCE_TYPE_COLLECTIONS.items():
            target = self._collection_for(resource_type)
            if getattr(target, "name", legacy_name) == legacy_name:
                continue
            try:
                legacy = self._client.get_collection(legacy_name)
            except Exception:  # noqa: BLE001 - 旧集合不存在属于正常情况
                continue
            try:
                legacy_count = legacy.count()
                if legacy_count == 0 or target.count() >= legacy_count:
                    continue
                payload = legacy.get(include=["documents", "metadatas"])
                ids = payload.get("ids") or []
                documents = payload.get("documents") or []
                metadatas = payload.get("metadatas") or []
                logger.info("重嵌入旧集合 %s -> %s，共 %s 个片段", legacy_name, target.name, len(ids))
                for start in range(0, len(ids), 100):
                    batch_ids = ids[start:start + 100]
                    if not batch_ids:
                        continue
                    target.upsert(
                        ids=batch_ids,
                        documents=documents[start:start + 100],
                        metadatas=metadatas[start:start + 100],
                    )
                logger.info("重嵌入完成: %s，片段数 %s", target.name, target.count())
            except Exception:  # noqa: BLE001 - 单个集合失败不阻塞服务启动
                logger.exception("重嵌入旧集合失败: %s", legacy_name)

    def _collection_for(self, resource_type: str) -> Any:
        """返回知识类型对应的集合；测试或本地简化场景回退到默认集合。"""
        resource_type = self._normalize_resource_type(resource_type)
        if resource_type == DEFAULT_RESOURCE_TYPE:
            return self._collection
        collections = getattr(self, "_collections", None)
        client = getattr(self, "_client", None)
        if collections is None or client is None:
            return self._collection
        if resource_type not in collections:
            names = getattr(self, "_collection_names", RESOURCE_TYPE_COLLECTIONS)
            name = names.get(resource_type) or RESOURCE_TYPE_COLLECTIONS[resource_type]
            embedding = getattr(self, "_embedding", None)
            options: Dict[str, Any] = {
                "name": name,
                "metadata": self._collection_metadata(f"EducationAgent RAG {resource_type} 知识库"),
            }
            if embedding is not None:
                options["embedding_function"] = embedding
            collections[resource_type] = client.get_or_create_collection(**options)
            self._warn_legacy_distance(collections[resource_type], name)
        return collections[resource_type]

    def _all_collections(self) -> List[Any]:
        collections = getattr(self, "_collections", None)
        if not collections:
            return [self._collection]
        return [collections[key] for key in RESOURCE_TYPE_COLLECTIONS if key in collections]

    def _target_collections(
        self,
        resource_types: Optional[Sequence[str]],
        include_empty: bool = False,
    ) -> List[tuple]:
        collections = getattr(self, "_collections", None)
        if not collections:
            return [(DEFAULT_RESOURCE_TYPE, self._collection)]
        if resource_types:
            types = list(dict.fromkeys(self._normalize_resource_type(t) for t in resource_types))
        elif include_empty:
            types = list(RESOURCE_TYPE_COLLECTIONS)
        else:
            types = list(dict.fromkeys(collections))
        return [(resource_type, self._collection_for(resource_type)) for resource_type in types]

    def _build_metadata(
        self,
        doc: Dict[str, Any],
        title: str,
        document_id: str,
        version: str,
        chunk_index: int,
        total_chunks: int,
        resource_type: str,
        chapter_path: str,
    ) -> Dict[str, Any]:
        """组装写入 ChromaDB 的完整元数据（值必须是 str/int/float/bool）。"""
        return {
            "title": title,
            "document_id": document_id,
            "version": version,
            "chunk_index": chunk_index,
            "total_chunks": total_chunks,
            "resource_type": resource_type,
            "subject": self._text(doc.get("subject")),
            "grade": self._text(doc.get("grade")),
            "chapter": self._text(doc.get("chapter")),
            "knowledge_point": self._text(doc.get("knowledge_point")),
            "subsection": self._text(doc.get("subsection")),
            "chapter_path": chapter_path,
            "source_name": self._text(doc.get("source_name")),
            "source_url": self._text(doc.get("source_url")),
            "publisher": self._text(doc.get("publisher")),
            "published_at": self._text(doc.get("published_at")),
            "effective_at": self._text(doc.get("effective_at")),
            "expires_at": self._text(doc.get("expires_at")),
            "effective_ts": self._parse_ts(doc.get("effective_at"), 0),
            "expires_ts": self._parse_ts(doc.get("expires_at"), _MAX_TS),
            "region": self._text(doc.get("region")),
            "textbook_version": self._text(doc.get("textbook_version")),
        }

    def _result_item(
        self,
        resource_type: str,
        document: str,
        meta: Dict[str, Any],
        distance: float,
        score: Optional[float] = None,
    ) -> Dict[str, Any]:
        similarity = score if score is not None else 1.0 - distance
        return {
            "title": meta.get("title", ""),
            "content": document,
            "score": round(similarity, 4),
            "chunk": meta.get("chunk_index", 0),
            "document_id": meta.get("document_id", ""),
            "version": meta.get("version", ""),
            "resource_type": meta.get("resource_type", resource_type),
            "subject": meta.get("subject", ""),
            "grade": meta.get("grade", ""),
            "chapter": meta.get("chapter", ""),
            "knowledge_point": meta.get("knowledge_point", ""),
            "subsection": meta.get("subsection", ""),
            "chapter_path": meta.get("chapter_path", ""),
            "source_name": meta.get("source_name", ""),
            "source_url": meta.get("source_url", ""),
            "publisher": meta.get("publisher", ""),
            "published_at": meta.get("published_at", ""),
            "effective_at": meta.get("effective_at", ""),
            "expires_at": meta.get("expires_at", ""),
            "region": meta.get("region", ""),
            "textbook_version": meta.get("textbook_version", ""),
            "total_chunks": meta.get("total_chunks", 0),
            "_effective_ts": meta.get("effective_ts", 0),
            "_expires_ts": meta.get("expires_ts", _MAX_TS),
        }

    @staticmethod
    def _is_valid(item: Dict[str, Any], now: int) -> bool:
        """有效期过滤：未设置有效期的旧片段仍然可用，已过期片段停止参与检索。"""
        return item.get("_effective_ts", 0) <= now <= item.get("_expires_ts", _MAX_TS)

    @staticmethod
    def _matches(item: Dict[str, Any], filters: Optional[Dict[str, str]]) -> bool:
        if not filters:
            return True
        for key, expected in filters.items():
            if expected and item.get(key) != expected:
                return False
        return True

    @staticmethod
    def _chapter_path(doc: Dict[str, Any]) -> str:
        """学科 → 年级 → 章节 → 知识点 → 子节。"""
        parts = [
            KnowledgeBase._text(doc.get(key))
            for key in ("subject", "grade", "chapter", "knowledge_point", "subsection")
        ]
        return " / ".join(part for part in parts if part)

    @staticmethod
    def _decorate_chunk(chunk: str, chapter_path: str) -> str:
        """把章节路径写入片段正文，让路径参与向量化和检索。"""
        chunk = chunk.strip()
        return f"{chapter_path}\n{chunk}" if chapter_path and chunk else chunk

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _parse_ts(value: Any, default: int) -> int:
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            raise ValueError(f"时间字段格式无效: {value}")
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if text.isdigit():
            return int(text)
        try:
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
        except ValueError as ex:
            raise ValueError(f"时间字段格式无效: {value}") from ex

    @staticmethod
    def _normalize_resource_type(value: Any) -> str:
        if value is None or not str(value).strip():
            return DEFAULT_RESOURCE_TYPE
        key = str(value).strip().lower()
        normalized = _RESOURCE_TYPE_ALIASES.get(key)
        if normalized is None:
            raise ValueError(
                "resource_type 必须是 textbook、exercise、solution、policy、course 之一"
            )
        return normalized

    # ── 结构化切分 ────────────────────────────────────────────────────────────

    def _chunk_document(self, text: str, chunk_size: int = 500) -> List[str]:
        """
        按文档结构切分：公式、表格、代码块独立成块，普通正文按句子聚合。
        """
        blocks = self._split_blocks(text)
        chunks: List[str] = []
        buffer = ""

        for kind, block in blocks:
            if kind == "text":
                for piece in self._chunk_text_block(block, chunk_size):
                    if not piece:
                        continue
                    if buffer and len(buffer) + len(piece) + 1 > chunk_size:
                        chunks.append(buffer)
                        buffer = piece
                    else:
                        buffer = f"{buffer}\n{piece}" if buffer else piece
            else:
                # 公式 / 表格 / 代码不与普通句子混切。
                if buffer:
                    chunks.append(buffer)
                    buffer = ""
                chunks.extend(self._split_structured_block(kind, block, chunk_size))

        if buffer:
            chunks.append(buffer)
        return chunks or ([text.strip()] if text.strip() else [])

    @staticmethod
    def _split_blocks(text: str) -> List[tuple]:
        """把 Markdown/文本拆成 text、code、math、table 四类块。"""
        lines = text.splitlines()
        blocks: List[tuple] = []
        buffer: List[str] = []

        def flush_text() -> None:
            if buffer:
                block = "\n".join(buffer)
                if block.strip():
                    blocks.append(("text", block))
                buffer.clear()

        index = 0
        total = len(lines)
        while index < total:
            line = lines[index]
            stripped = line.strip()

            if stripped.startswith("```"):
                flush_text()
                code = [line]
                index += 1
                while index < total:
                    code.append(lines[index])
                    if lines[index].strip().startswith("```"):
                        index += 1
                        break
                    index += 1
                blocks.append(("code", "\n".join(code)))
                continue

            if stripped.startswith("$$"):
                flush_text()
                math = [line]
                index += 1
                if stripped.count("$$") >= 2 and len(stripped) > 4:
                    blocks.append(("math", "\n".join(math)))
                    continue
                while index < total:
                    math.append(lines[index])
                    closed = "$$" in lines[index]
                    index += 1
                    if closed:
                        break
                blocks.append(("math", "\n".join(math)))
                continue

            if stripped.startswith("|"):
                flush_text()
                table = [line]
                index += 1
                while index < total and lines[index].strip().startswith("|"):
                    table.append(lines[index])
                    index += 1
                blocks.append(("table", "\n".join(table)))
                continue

            buffer.append(line)
            index += 1

        flush_text()
        return blocks

    def _chunk_text_block(self, text: str, chunk_size: int) -> List[str]:
        """普通正文：按空行分段、按句子聚合，行内公式不被拆断。"""
        if len(text) <= chunk_size:
            return [text.strip()] if text.strip() else []

        pieces: List[str] = []
        for paragraph in re.split(r"\n\s*\n", text):
            paragraph = paragraph.strip()
            if not paragraph:
                continue
            if len(paragraph) <= chunk_size:
                pieces.append(paragraph)
                continue
            for sentence in self._split_sentences(paragraph):
                while len(sentence) > chunk_size:
                    pieces.append(sentence[:chunk_size])
                    sentence = sentence[chunk_size:]
                if sentence:
                    pieces.append(sentence)

        chunks: List[str] = []
        current = ""
        for piece in pieces:
            if current and len(current) + len(piece) + 1 > chunk_size:
                chunks.append(current)
                current = piece
            else:
                current = f"{current}\n{piece}" if current else piece
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        """按句末标点切分，跳过 $...$、\\(...\\)、`...` 内部。"""
        sentences: List[str] = []
        buffer: List[str] = []
        in_math = False
        in_code = False
        index = 0
        while index < len(text):
            char = text[index]
            if text.startswith("$$", index):
                in_math = not in_math
                buffer.append("$$")
                index += 2
                continue
            if text.startswith("\\(", index) or text.startswith("\\[", index):
                in_math = True
                buffer.append(text[index:index + 2])
                index += 2
                continue
            if text.startswith("\\)", index) or text.startswith("\\]", index):
                in_math = False
                buffer.append(text[index:index + 2])
                index += 2
                continue
            if char == "$":
                in_math = not in_math
            elif char == "`":
                in_code = not in_code
            elif not in_math and not in_code and char in "。！？!?；;\n":
                buffer.append(char)
                sentence = "".join(buffer).strip()
                if sentence:
                    sentences.append(sentence)
                buffer = []
                index += 1
                continue
            buffer.append(char)
            index += 1
        tail = "".join(buffer).strip()
        if tail:
            sentences.append(tail)
        return sentences

    def _split_structured_block(self, kind: str, block: str, chunk_size: int) -> List[str]:
        if len(block) <= chunk_size:
            return [block]
        if kind == "table":
            return self._split_table(block, chunk_size)
        if kind == "code":
            return self._split_code(block, chunk_size)
        return self._split_math(block, chunk_size)

    @staticmethod
    def _split_math(block: str, chunk_size: int) -> List[str]:
        pieces = [piece.strip() for piece in re.split(r"\n\s*\n", block) if piece.strip()]
        chunks: List[str] = []
        for piece in pieces:
            chunks.extend(piece[i:i + chunk_size] for i in range(0, len(piece), chunk_size))
        return chunks or [block]

    @staticmethod
    def _split_code(block: str, chunk_size: int) -> List[str]:
        """按顶层函数/类聚合；超长函数再按长度兜底切分。"""
        lines = block.splitlines()
        groups: List[List[str]] = []
        current: List[str] = []
        for line in lines:
            if current and _CODE_DEF.match(line.strip()) and sum(len(item) + 1 for item in current) > chunk_size // 2:
                groups.append(current)
                current = [line]
            else:
                current.append(line)
        if current:
            groups.append(current)

        chunks: List[str] = []
        buffer = ""
        for group in groups:
            text = "\n".join(group)
            if len(text) > chunk_size:
                if buffer:
                    chunks.append(buffer)
                    buffer = ""
                chunks.extend(text[i:i + chunk_size] for i in range(0, len(text), chunk_size))
                continue
            if buffer and len(buffer) + len(text) + 1 > chunk_size:
                chunks.append(buffer)
                buffer = text
            else:
                buffer = f"{buffer}\n{text}" if buffer else text
        if buffer:
            chunks.append(buffer)
        return chunks or [block]

    @staticmethod
    def _split_table(block: str, chunk_size: int) -> List[str]:
        """表格超过长度时按行拆分，并在每片重复表头。"""
        lines = block.splitlines()
        header_lines = 1
        if len(lines) >= 2 and set(lines[1].strip()) <= set("|-: "):
            header_lines = 2
        header = "\n".join(lines[:header_lines])
        rows = lines[header_lines:]

        chunks: List[str] = []
        buffer: List[str] = [header]
        for row in rows:
            if len(row) > chunk_size:
                if len(buffer) > 1:
                    chunks.append("\n".join(buffer))
                buffer = [header]
                chunks.extend(row[i:i + chunk_size] for i in range(0, len(row), chunk_size))
                continue
            if len(buffer) > 1 and len("\n".join(buffer)) + len(row) + 1 > chunk_size:
                chunks.append("\n".join(buffer))
                buffer = [header]
            buffer.append(row)
        if len(buffer) > 1:
            chunks.append("\n".join(buffer))
        elif not chunks:
            chunks.append(header)
        return chunks

    def _chunk_text(self, text: str, chunk_size: int = 500) -> List[str]:
        """将长文本按 chunk_size 切片，保留语义完整性（按句号/换行切分）。"""
        if len(text) <= chunk_size:
            return [text] if text.strip() else []

        chunks = []
        current = ""
        # 按句子切分
        sentences = text.replace("\n", "。").split("。")
        sentences = [sent[i:i + chunk_size] for sent in sentences
                     for i in range(0, len(sent), chunk_size)]
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(current) + len(sent) + 1 > chunk_size:
                if current:
                    chunks.append(current)
                current = sent
            else:
                current = f"{current}。{sent}" if current else sent

        if current:
            chunks.append(current)

        return chunks

    def _load_default_docs(self) -> None:
        """导入默认知识库文档（教材/课程类，不含时效性考试信息）。"""
        # 易变知识（报名时间、查分时间等）不写死在代码中，
        # 应通过 /knowledge/add 导入 resource_type=policy 且带 source_url 与 expires_at 的文档。
        default_docs = [
            {
                "title": "牛顿第二定律",
                "content": (
                    "牛顿第二定律：物体的加速度与所受合外力成正比，与质量成反比，方向与合外力相同。"
                    "公式: F = ma 或 a = F/m。"
                    "其中 F 是合外力（牛顿 N），m 是质量（千克 kg），a 是加速度（米每二次方秒 m/s）。"
                    "注意：F 和 a 都是矢量，方向必须相同。"
                    "常见应用：小车斜面实验、自由落体（忽略空气阻力时 a=g9.8m/s）。"
                ),
                "subject": "高中物理", "grade": "高一", "chapter": "力与运动",
                "knowledge_point": "牛顿第二定律", "subsection": "公式与方向",
                "source_name": "人教版高中物理必修一", "publisher": "人民教育出版社",
                "textbook_version": "人教版2019", "region": "全国",
            },
            {
                "title": "三角函数和差化积公式",
                "content": (
                    "和差化积公式："
                    "sinα + sinβ = 2sin((α+β)/2)cos((α-β)/2)"
                    "sinα - sinβ = 2cos((α+β)/2)sin((α-β)/2)"
                    "cosα + cosβ = 2cos((α+β)/2)cos((α-β)/2)"
                    "cosα - cosβ = -2sin((α+β)/2)sin((α-β)/2)"
                    "适用场景：三角恒等变换、求三角函数值、证明三角恒等式。"
                ),
                "subject": "高中数学", "grade": "高一", "chapter": "三角函数",
                "knowledge_point": "和差化积", "subsection": "公式",
                "source_name": "人教版高中数学必修一", "publisher": "人民教育出版社",
                "textbook_version": "人教版2019", "region": "全国",
            },
            {
                "title": "Python 入门学习路线",
                "content": (
                    "零基础 Python 学习建议分为 4 个阶段："
                    "第一阶段（1-2 周）：基础语法变量、数据类型、条件判断、循环、函数。"
                    "第二阶段（2-3 周）：面向对象类、对象、继承、多态、魔术方法。"
                    "第三阶段（2-3 周）：常用库文件操作、异常处理、标准库、pip 包管理。"
                    "第四阶段（3-4 周）：实战项目Web 开发（Flask/Django）、数据分析（Pandas/NumPy）、爬虫。"
                    "推荐入门书：《Python编程：从入门到实践》。推荐在线平台：LeetCode 练题。"
                ),
                "resource_type": "course",
                "subject": "信息技术", "grade": "高中", "chapter": "Python 编程",
                "knowledge_point": "学习路线", "subsection": "阶段规划",
                "source_name": "EducationAgent 课程组", "publisher": "EducationAgent",
                "region": "全国",
            },
            {
                "title": "期末复习方法",
                "content": (
                    "高效期末复习 6 步法："
                    "1. 梳理知识框架用思维导图把整本书的章节结构画出来。"
                    "2. 标记重点难点老师强调的、历年考过的、自己不会的都是重点。"
                    "3. 过教材和笔记逐章过，不理解的做上标记集中攻克。"
                    "4. 刷题课后习题、历年真题、模拟题，限时完成。"
                    "5. 错题整理做错的题要搞懂为什么错，定期回顾。"
                    "6. 考前冲刺考前一周集中看错题本和知识框架，做轻量模拟保持手感。"
                    "注意：每天保证 6-8 小时睡眠，饮食规律，考前不熬夜。"
                ),
                "resource_type": "course",
                "subject": "学习方法", "grade": "通用", "chapter": "复习规划",
                "knowledge_point": "期末复习", "subsection": "六步法",
                "source_name": "EducationAgent 教研组", "publisher": "EducationAgent",
                "region": "全国",
            },
            {
                "title": "机器学习入门推荐",
                "content": (
                    "机器学习入门路线建议："
                    "数学基础：线性代数（矩阵运算、特征值）、概率论与数理统计（分布、贝叶斯、假设检验）、微积分（偏导、梯度）。"
                    "编程基础：Python + NumPy + Pandas + Matplotlib。"
                    "核心教材：《统计学习方法》（李航）、《机器学习》（周志华，西瓜书）、Coursera Andrew Ng 机器学习课程。"
                    "实践平台：Kaggle 竞赛、UCI 数据集。"
                    "方向选择：计算机视觉（CNN、YOLO）、自然语言处理（Transformer、BERT）、推荐系统。"
                    "注意：不要只看教程不动手，每学一个模型就要写代码跑一遍。"
                ),
                "resource_type": "course",
                "subject": "人工智能", "grade": "大学", "chapter": "机器学习",
                "knowledge_point": "入门路线", "subsection": "学习资源",
                "source_name": "EducationAgent 课程组", "publisher": "EducationAgent",
                "region": "全国",
            },
        ]
        self.add_documents(default_docs)
        logger.info("已导入默认知识库: %s 篇文档", len(default_docs))
