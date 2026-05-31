"""
向量化引擎：Gemini Embedding + 余弦相似度
"""
import math
import struct
import httpx
from config import GEMINI_API_KEY, EMBEDDING_MODEL


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
    """调用 Gemini API 获取 embedding"""
    if not GEMINI_API_KEY:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL}:embedContent?key={GEMINI_API_KEY}"
    body = {"content": {"parts": [{"text": text[:2000]}]}}  # 截断过长文本
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            return resp.json()["embedding"]["values"]
    except Exception as e:
        print(f"[Embedding] error: {e}")
        return None
