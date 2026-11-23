"""本地 ONNX Embedding：BAAI/bge-small-zh-v1.5（中文教育语料友好）。

不依赖 sentence-transformers/torch：用 onnxruntime + tokenizers 直接推理，
输出 512 维、均值池化并 L2 归一化，供 ChromaDB 做余弦检索。
"""
from __future__ import annotations

import logging
import os
import urllib.request
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)

EMBEDDING_MODEL_ID = "bge-small-zh-v1.5"
EMBEDDING_DIMENSION = 512
MAX_LENGTH = 512
BATCH_SIZE = 16

# Xenova 仓库提供可直接推理的 ONNX 版本，避免构建期依赖 torch 转换。
_MODEL_REPO = "Xenova/bge-small-zh-v1.5"
_MODEL_FILES = {
    "model.onnx": "onnx/model.onnx",
    "tokenizer.json": "tokenizer.json",
    "config.json": "config.json",
}
_DEFAULT_ENDPOINTS = ("https://hf-mirror.com", "https://huggingface.co")


def resolve_model_dir() -> Path:
    """返回模型目录；相对路径按当前工作目录解析，容器内即 /app/data/models/..."""
    raw = os.getenv("ECHOMIND_EMBEDDING_DIR", f"data/models/{EMBEDDING_MODEL_ID}")
    path = Path(raw)
    return path if path.is_absolute() else Path.cwd() / path


def _missing_files(model_dir: Path) -> List[str]:
    return [
        local
        for local in _MODEL_FILES
        if not (model_dir / local).is_file() or (model_dir / local).stat().st_size == 0
    ]


def ensure_model_files(model_dir: Path, download: bool = True) -> None:
    """确保 model.onnx / tokenizer.json / config.json 存在，缺失时按镜像站顺序下载。"""
    missing = _missing_files(model_dir)
    if not missing:
        return
    if not download:
        raise FileNotFoundError(
            f"Embedding 模型文件缺失: {model_dir}，请设置 ECHOMIND_EMBEDDING_DIR "
            "或允许 ECHOMIND_EMBEDDING_AUTO_DOWNLOAD=true"
        )

    model_dir.mkdir(parents=True, exist_ok=True)
    endpoints = list(
        dict.fromkeys(
            filter(
                None,
                [
                    os.getenv("ECHOMIND_HF_ENDPOINT"),
                    *_DEFAULT_ENDPOINTS,
                ],
            )
        )
    )
    for local in missing:
        remote = _MODEL_FILES[local]
        last_error: Optional[Exception] = None
        for endpoint in endpoints:
            url = f"{endpoint.rstrip('/')}/{_MODEL_REPO}/resolve/main/{remote}"
            temp = model_dir / f"{local}.part"
            try:
                logger.info("下载 embedding 模型文件: %s", url)
                with urllib.request.urlopen(url, timeout=180) as response, open(temp, "wb") as target:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                os.replace(temp, model_dir / local)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - 网络异常统一换下一个源
                last_error = exc
                if temp.exists():
                    temp.unlink()
        if last_error is not None:
            raise RuntimeError(f"下载 embedding 模型失败: {local}: {last_error}") from last_error


class OnnxEmbeddingFunction:
    """ChromaDB 兼容的 embedding 函数，接口为 __call__(texts) -> List[List[float]]。"""

    def __init__(self, model_dir: Path):
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._session = ort.InferenceSession(
            str(model_dir / "model.onnx"),
            providers=["CPUExecutionProvider"],
        )
        self._input_names = {item.name for item in self._session.get_inputs()}
        self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=MAX_LENGTH)
        self._tokenizer.no_padding()

    def name(self) -> str:
        return EMBEDDING_MODEL_ID

    def __call__(self, input: Sequence[str]) -> List[List[float]]:  # noqa: A002 - 对齐 Chroma 协议
        texts = [str(item) for item in input]
        vectors: List[List[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start:start + BATCH_SIZE]
            encodings = self._tokenizer.encode_batch(batch)
            max_len = max(len(encoding.ids) for encoding in encodings)
            ids = np.zeros((len(batch), max_len), dtype=np.int64)
            mask = np.zeros((len(batch), max_len), dtype=np.int64)
            for row, encoding in enumerate(encodings):
                length = len(encoding.ids)
                ids[row, :length] = encoding.ids
                mask[row, :length] = 1
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._input_names:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self._session.run(None, feeds)[0]
            vectors.extend(self._pool(hidden).tolist())
        return vectors

    @staticmethod
    def _pool(hidden: np.ndarray) -> np.ndarray:
        """BGE 系列使用 CLS 池化（首 token），再做 L2 归一化。"""
        pooled = hidden[:, 0]
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        return pooled / np.clip(norms, 1e-12, None)


def build_embedding_function() -> OnnxEmbeddingFunction:
    """构建本地中文 embedding 函数；模型缺失时按配置下载或直接报错。"""
    model = os.getenv("ECHOMIND_EMBEDDING_MODEL", EMBEDDING_MODEL_ID).strip()
    if model != EMBEDDING_MODEL_ID:
        raise ValueError(f"暂只支持 {EMBEDDING_MODEL_ID}，当前配置: {model}")
    auto_download = os.getenv("ECHOMIND_EMBEDDING_AUTO_DOWNLOAD", "true").lower() in {
        "1", "true", "yes"
    }
    model_dir = resolve_model_dir()
    ensure_model_files(model_dir, download=auto_download)
    logger.info("加载本地 embedding 模型: %s", model_dir)
    return OnnxEmbeddingFunction(model_dir)
