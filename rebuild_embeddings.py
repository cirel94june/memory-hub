"""
重建所有记忆的向量（更换 embedding 模型后使用）

用法：
  cd /opt/memory-hub
  set -a && source .env && set +a
  .venv/bin/python rebuild_embeddings.py

流程：
  1. 删除旧的 memories_vec 表（384维）
  2. 重建新维度的 memories_vec 表（1024维）
  3. 清空 vec_id_map
  4. 批量重新生成所有 active 记忆的 embedding
  5. 写入新向量
"""
import asyncio
import sqlite3
import struct
import sys
import time

# 加载配置
import config
from config import EMBEDDING_DIM

DB_PATH = config.DATA_DIR / "memories.db"

print(f"=== Embedding 重建工具 ===")
print(f"数据库: {DB_PATH}")
print(f"模型:   {config.EMBEDDING_MODEL}")
print(f"维度:   {EMBEDDING_DIM}")
print(f"API:    {config.EMBEDDING_BASE_URL}")
print(f"Key:    {'已设置' if config.EMBEDDING_API_KEY else '❌ 未设置!'}")
print()

if not config.EMBEDDING_API_KEY:
    print("错误: EMBEDDING_API_KEY 未设置，请检查 .env")
    sys.exit(1)


async def main():
    import sqlite_vec

    conn = sqlite3.connect(str(DB_PATH))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.row_factory = sqlite3.Row

    # 1. 统计
    total = conn.execute("SELECT COUNT(*) FROM memories WHERE status = 'active'").fetchone()[0]
    print(f"Active 记忆总数: {total}")

    if total == 0:
        print("没有需要处理的记忆")
        return

    # 2. 删除旧向量表
    print("\n[1/4] 删除旧向量表...")
    conn.execute("DROP TABLE IF EXISTS memories_vec")
    conn.execute("DELETE FROM vec_id_map")
    conn.commit()
    print("  ✓ 旧表已删除")

    # 3. 创建新维度的向量表
    print(f"\n[2/4] 创建新向量表 (embedding float[{EMBEDDING_DIM}])...")
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS memories_vec "
        f"USING vec0(embedding float[{EMBEDDING_DIM}])"
    )
    conn.commit()
    print("  ✓ 新表已创建")

    # 4. 读取所有 active 记忆
    print(f"\n[3/4] 读取记忆内容...")
    rows = conn.execute(
        "SELECT id, content FROM memories WHERE status = 'active' ORDER BY created_at"
    ).fetchall()
    print(f"  ✓ 读取了 {len(rows)} 条记忆")

    # 5. 批量生成 embedding
    print(f"\n[4/4] 批量生成 embedding...")
    from embedding import get_embedding_batch, pack_embedding

    BATCH = 32
    success = 0
    failed = 0
    t0 = time.time()

    for start in range(0, len(rows), BATCH):
        batch_rows = rows[start:start + BATCH]
        texts = [r["content"] for r in batch_rows]
        ids = [r["id"] for r in batch_rows]

        embeddings = await get_embedding_batch(texts, batch_size=BATCH)

        for mem_id, vec in zip(ids, embeddings):
            if vec is None:
                failed += 1
                continue

            blob = pack_embedding(vec)

            # 写入 memories 表
            conn.execute("UPDATE memories SET embedding = ? WHERE id = ?", (blob, mem_id))

            # 写入 vec_id_map + memories_vec
            cur = conn.execute("INSERT INTO vec_id_map (memory_id) VALUES (?)", (mem_id,))
            vec_rowid = cur.lastrowid
            conn.execute(
                "INSERT INTO memories_vec (rowid, embedding) VALUES (?, ?)",
                (vec_rowid, blob),
            )
            success += 1

        conn.commit()

        elapsed = time.time() - t0
        done = start + len(batch_rows)
        rate = done / elapsed if elapsed > 0 else 0
        eta = (len(rows) - done) / rate if rate > 0 else 0
        print(f"  进度: {done}/{len(rows)} ({success}✓ {failed}✗) "
              f"  速度: {rate:.1f}/s  ETA: {eta:.0f}s")

        # 硅基流动免费层限速：避免太快
        if start + BATCH < len(rows):
            await asyncio.sleep(0.5)

    elapsed = time.time() - t0
    print(f"\n=== 完成 ===")
    print(f"  成功: {success}")
    print(f"  失败: {failed}")
    print(f"  耗时: {elapsed:.1f}s")
    print(f"  维度: {EMBEDDING_DIM}")

    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
