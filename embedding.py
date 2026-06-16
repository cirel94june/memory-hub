"""
向量化引擎：本地 ONNX embedding（fastembed）
模型：从 config.EMBEDDING_MODEL 读取，默认 BAAI/bge-small-zh-v1.5（中文优化，512维）
"""
import math
import struct

from fastembed import TextEmbedding
from config import EMBEDDING_MODEL

_model = None


def _get_model():
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=EMBEDDING_MODEL)
    return _model


def pack_embedding(values: list[float]) -> bytes:
    return struct.pack(f'{len(values)}f', *values)


def unpack_embedding(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f'{n}f', blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def get_embedding(text: str) -> list[float] | None:
    """获取文本 embedding（本地 ONNX 推理，无网络依赖）"""
    if not text or not text.strip():
        return None
    try:
        model = _get_model()
        embeddings = list(model.embed([text[:2000]]))
        return embeddings[0].tolist()
    except Exception as e:
        print(f"[Embedding] error: {e}")
        return None
