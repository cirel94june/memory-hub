"""
向量化引擎：支持多种 embedding 后端
优先级：EMBEDDING_BASE_URL(自定义) > HuggingFace Inference API > Gemini
"""
import os
import math
import struct
import httpx

from config import GEMINI_API_KEY, EMBEDDING_MODEL

EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
HF_TOKEN = os.getenv("HF_TOKEN", "")

# HuggingFace 免费 Inference API 的模型
HF_EMBEDDING_MODEL = os.getenv("HF_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")


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
    """获取文本 embedding，按优先级尝试多种后端"""
    text = text[:2000]

    # 1. 自定义 OpenAI 兼容 API（如果配了 EMBEDDING_BASE_URL）
    if EMBEDDING_BASE_URL:
        result = await _openai_embedding(text)
        if result:
            return result

    # 2. HuggingFace Inference API（免费，无需 token 也能用）
    result = await _hf_embedding(text)
    if result:
        return result

    # 3. Gemini（fallback）
    if GEMINI_API_KEY:
        result = await _gemini_embedding(text)
        if result:
            return result

    return None


async def _openai_embedding(text: str) -> list[float] | None:
    """OpenAI 兼容格式的 embedding API"""
    url = f"{EMBEDDING_BASE_URL}/embeddings"
    headers = {"Content-Type": "application/json"}
    if EMBEDDING_API_KEY:
        headers["Authorization"] = f"Bearer {EMBEDDING_API_KEY}"
    body = {"model": EMBEDDING_MODEL, "input": text}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data["data"][0]["embedding"]
    except Exception as e:
        print(f"[Embedding/OpenAI] error: {e}")
        return None


async def _hf_embedding(text: str) -> list[float] | None:
    """HuggingFace Inference API（免费）"""
    url = f"https://api-inference.huggingface.co/pipeline/feature-extraction/{HF_EMBEDDING_MODEL}"
    headers = {"Content-Type": "application/json"}
    if HF_TOKEN:
        headers["Authorization"] = f"Bearer {HF_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, json={"inputs": text}, headers=headers)
            resp.raise_for_status()
            result = resp.json()
            # HF returns [[float, ...]] for single input
            if isinstance(result, list) and len(result) > 0:
                if isinstance(result[0], list):
                    return result[0]
                return result
            return None
    except Exception as e:
        print(f"[Embedding/HF] error: {e}")
        return None


async def _gemini_embedding(text: str) -> list[float] | None:
    """Gemini Embedding API"""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL}:embedContent?key={GEMINI_API_KEY}"
    body = {"content": {"parts": [{"text": text}]}}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=body)
            resp.raise_for_status()
            return resp.json()["embedding"]["values"]
    except Exception as e:
        print(f"[Embedding/Gemini] error: {e}")
        return None
