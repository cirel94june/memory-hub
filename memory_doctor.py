"""
记忆医生（Memory Doctor）——记忆系统的自查自愈
daemon 每 12h 跑一次体检；有把握的自动修，没把握的写进体检报告。

体检项目：
1. 身份冲突：记忆被打了"宠物"类标签但内容说的是人/AI（对照 identity_registry）→ 自动修
2. 矛盾扫描：同房间中等相似度（0.60-0.75，低于去重线）的记忆对，
   用小模型判断是否矛盾/张冠李戴，可疑的对照原文保险箱验证 → 写报告，不自动动
3. 门卫统计：最近被写入门卫拦下的内容 → 写报告供人工复核

报告存 data/doctor_report.json；
- 前端/API：GET /api/doctor/report，POST /api/doctor/run
- TG：AI 用 [体检报告:] 能力标签把报告念给用户
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import github_store as store
from embedding import cosine_similarity, unpack_embedding

log = logging.getLogger("memory_doctor")

REPORT_PATH = Path(__file__).parent / "data" / "doctor_report.json"

# 只体检"事实类"房间；梦境/日记/社交梗本来就允许荒诞和重复
FACT_ROOMS = {"living_room", "relationships", "career", "health",
              "preferences", "work_tasks", "learning", "psychology"}

MAX_LLM_PAIRS = 8  # 每次体检最多让小模型判断的记忆对数（控制成本）


async def run_checkup() -> dict:
    """完整体检。返回并保存报告。"""
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "auto_fixed": [],
        "issues": [],
        "stats": {},
    }

    all_mems = store.get_all_memories()
    active = [m for m in all_mems.values() if m.get("status") == "active"]
    report["stats"]["active_memories"] = len(active)

    # ── 1. 身份冲突：人/AI 被打成宠物 ──
    try:
        import identity_registry
        reg = identity_registry.get_registry()
        person_names = set()
        user = reg.get("user", {})
        person_names.add(user.get("canonical", ""))
        person_names.update(user.get("aliases", []))
        for p in reg.get("people", []):
            person_names.add(p.get("canonical", ""))
            person_names.update(p.get("aliases", []))
        for nicks in reg.get("ai_nicknames", {}).values():
            person_names.update(nicks)
        person_names.discard("")

        for m in active:
            domain_raw = m.get("domain") or "[]"
            try:
                domains = json.loads(domain_raw) if isinstance(domain_raw, str) else list(domain_raw)
            except Exception:
                continue
            if not any("宠物" in str(d) for d in domains):
                continue
            content = str(m.get("content", ""))
            hit = next((n for n in person_names if n and n in content), "")
            if not hit:
                continue
            new_domains = [d for d in domains if "宠物" not in str(d)]
            m["domain"] = json.dumps(new_domains, ensure_ascii=False)
            comments = m.get("comments") if isinstance(m.get("comments"), list) else []
            comments.append({
                "date": report["generated_at"],
                "author": "memory_doctor",
                "kind": "auto_fix",
                "content": f"体检自动修复：内容提到「{hit}」是人/AI，移除宠物类标签",
            })
            m["comments"] = comments
            store.set_memory(m)
            report["auto_fixed"].append({
                "id": m.get("id"), "type": "pet_mislabel",
                "detail": f"「{hit}」被打了宠物标签，已移除",
            })
    except Exception as e:
        log.warning(f"doctor identity check failed: {e}")

    # ── 2. 矛盾/归属混乱扫描（小模型 + 原文对照） ──
    try:
        candidates = []
        fact_mems = [m for m in active if m.get("room") in FACT_ROOMS and m.get("embedding")]
        for i, a in enumerate(fact_mems):
            va = unpack_embedding(a["embedding"])
            for b in fact_mems[i + 1:]:
                if a.get("room") != b.get("room"):
                    continue
                sim = cosine_similarity(va, unpack_embedding(b["embedding"]))
                if 0.60 <= sim < 0.75:
                    candidates.append((sim, a, b))
        candidates.sort(key=lambda x: -x[0])

        from gateway import _call_llm
        import identity_registry as _ir
        glossary = _ir.glossary_text()

        for sim, a, b in candidates[:MAX_LLM_PAIRS]:
            prompt = (
                f"{glossary}\n\n"
                "判断下面两条记忆是否互相矛盾，或者存在张冠李戴（把A的特征/物品/行为写到了B身上）。\n"
                f"记忆1：{a['content'][:300]}\n"
                f"记忆2：{b['content'][:300]}\n"
                '只输出 JSON：{"conflict": true/false, "reason": "一句话说明"}'
            )
            result = await _call_llm(prompt)
            try:
                result = result.strip()
                if result.startswith("```"):
                    result = result.split("\n", 1)[-1].rsplit("```", 1)[0]
                verdict = json.loads(result)
            except Exception:
                continue
            if not verdict.get("conflict"):
                continue

            # 对照原文保险箱找证据
            evidence = ""
            try:
                import raw_vault
                key_terms = [w for w in str(verdict.get("reason", ""))[:20].split() if len(w) >= 2]
                if key_terms:
                    hits = raw_vault.search(key_terms[0], limit=2)
                    if hits:
                        h = hits[0]
                        evidence = f"原话（{h['created_at'][:10]}）：用户说「{h['user_text'][:80]}」"
            except Exception:
                pass

            report["issues"].append({
                "type": "contradiction",
                "memory_ids": [a.get("id"), b.get("id")],
                "similarity": round(sim, 2),
                "detail": verdict.get("reason", ""),
                "memory_1": a["content"][:120],
                "memory_2": b["content"][:120],
                "evidence": evidence,
                "suggestion": "在 TG 里告诉 bot 哪条是对的（比如「真丝裤衩是狗蛋的，帮我修掉错的那条」），或到前端记忆库手动改",
            })
    except Exception as e:
        log.warning(f"doctor contradiction scan failed: {e}")

    # ── 3. 门卫统计 ──
    try:
        import write_gate
        blocked = write_gate.recent_blocked(limit=10)
        report["stats"]["gate_recent_blocked"] = len(blocked)
        if blocked:
            report["stats"]["gate_samples"] = [
                {"reason": x.get("reason"), "content": x.get("content", "")[:60]} for x in blocked[-3:]
            ]
    except Exception:
        pass

    # ── 4. 原文保险箱状态 ──
    try:
        import raw_vault
        report["stats"]["raw_vault"] = raw_vault.stats()
    except Exception:
        pass

    if report["auto_fixed"]:
        await store.push_dirty()

    _save_report(report)
    log.info(f"Doctor checkup: {len(report['auto_fixed'])} auto-fixed, {len(report['issues'])} issues")
    return report


def _save_report(report: dict):
    try:
        REPORT_PATH.parent.mkdir(exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"doctor report save failed: {e}")


def read_report() -> dict:
    if not REPORT_PATH.exists():
        return {}
    try:
        return json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def report_text() -> str:
    """给 TG bot 念的人话版报告。"""
    r = read_report()
    if not r:
        return "还没有体检报告——后台每 12 小时自动体检一次，也可以让主人在前端手动触发。"
    lines = [f"记忆体检报告（{r.get('generated_at', '')[:16].replace('T', ' ')}）："]
    stats = r.get("stats", {})
    if stats.get("active_memories") is not None:
        lines.append(f"· 活跃记忆 {stats['active_memories']} 条")
    fixed = r.get("auto_fixed", [])
    if fixed:
        lines.append(f"· 自动修复 {len(fixed)} 处：" + "；".join(f["detail"] for f in fixed[:3]))
    else:
        lines.append("· 没有需要自动修复的问题")
    issues = r.get("issues", [])
    if issues:
        lines.append(f"· 发现 {len(issues)} 处存疑，需要主人确认：")
        for i, iss in enumerate(issues[:3], 1):
            lines.append(f"  {i}. {iss.get('detail', '')}（{iss.get('memory_1', '')[:40]}… vs {iss.get('memory_2', '')[:40]}…）")
        lines.append("直接告诉我哪条是对的，我来修。")
    else:
        lines.append("· 没有发现矛盾或张冠李戴")
    gate = stats.get("gate_recent_blocked")
    if gate:
        lines.append(f"· 写入门卫最近拦下 {gate} 条低价值碎片")
    return "\n".join(lines)
