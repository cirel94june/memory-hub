"""
向量化引擎：硅基流动 API embedding
模型：BAAI/bge-large-zh-v1.5（中文优化，1024维）
"""
import math
import struct

import httpx

# 单一配置来源在 config.py，避免多处默认值漂移
from config import EMBEDDING_BASE_URL, EMBEDDING_API_KEY, EMBEDDING_MODEL


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


_embed_client: httpx.AsyncClient | None = None


def _get_embed_client() -> httpx.AsyncClient:
    global _embed_client
    if _embed_client is None or _embed_client.is_closed:
        _embed_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3, read=10, write=5, pool=3),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _embed_client


async def get_embedding(text: str) -> list[float] | None:
    if not text or not text.strip():
        return None
    if not EMBEDDING_API_KEY:
        return None
    try:
        client = _get_embed_client()
        resp = await client.post(
            f"{EMBEDDING_BASE_URL}/embeddings",
            headers={"Authorization": f"Bearer {EMBEDDING_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": EMBEDDING_MODEL, "input": text[:2000]},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"][0]["embedding"]
    except Exception as e:
        import logging
        logging.getLogger("embedding").warning(f"get_embedding failed: {e}")
        return None
