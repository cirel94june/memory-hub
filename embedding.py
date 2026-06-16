"""
向量化引擎：硅基流动 API embedding
模型：BAAI/bge-large-zh-v1.5（中文优化，1024维）
"""
import math
import os
import struct

import httpx

EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "https://api.siliconflow.cn/v1")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5")


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
    if not text or not text.strip():
        return None
    if not EMBEDDING_API_KEY:
        print("[Embedding] no API key configured")
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
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
        print(f"[Embedding] error: {e}")
        return None
