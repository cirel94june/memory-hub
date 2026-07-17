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

# AI 的展示别名 → canonical（与 dream._ALIAS_GLOSSARY / identity_registry 保持一致）
# 用于内容级"张冠李戴"检查：别称出现在错误的人名括号里
_AI_DISPLAY_ALIASES = {
    "claude": {"小克", "cloudy", "夜鹭", "大蟑螂"},
    "lucien": {"lucien", "狐狸", "老狐狸"},
    "jasper": {"jasper", "狗蛋", "鹦鹉", "谷歌大少爷"},
}


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

    # 记忆池仪表：各状态数量 + 数据库体积，让"池子越来越大"看得见
    try:
        status_counts: dict = {}
        for m in all_mems.values():
            s = m.get("status") or "unknown"
            status_counts[s] = status_counts.get(s, 0) + 1
        report["stats"]["pool"] = status_counts
        db_dir = Path(__file__).parent / "data"
        sizes = {}
        for f in ("memories.db", "memory_hub.db", "raw_events.db"):
            p = db_dir / f
            if p.exists():
                sizes[f] = f"{p.stat().st_size / 1024 / 1024:.1f}MB"
        report["stats"]["db_sizes"] = sizes
    except Exception:
        pass

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

    # ── 2.5 待办复审：让"未解决"流动起来，不再是写死的钉子 ──
    # 挂了 14 天以上的待办，用小模型对照当前画像判断是否还成立；
    # 明确过时的自动摘牌（记忆保留，只是不再天天置顶推给 AI）。
    try:
        from datetime import timedelta
        from gateway import _call_llm as _llm
        import current_status
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        stale_unresolved = [
            m for m in active
            if m.get("resolved") is False
            and (m.get("updated_at") or m.get("created_at") or "") < cutoff
        ]
        status_text = "\n".join(
            f"{v.get('text', '')}" for v in
            (current_status.get_status().get("sections", {}) or {}).values() if v.get("text")
        )[:600]
        for m in stale_unresolved[:6]:
            prompt = (
                f"用户当前状态画像：\n{status_text or '（暂无画像）'}\n\n"
                f"下面这条记忆被标记为'未解决待办'，已挂了 14 天以上：\n{m['content'][:300]}\n\n"
                "判断它现在是否仍然是一个**进行中的、需要每天提醒的待办**。\n"
                "注意：情绪描述、状态描述、已经过时的旧情况都不算待办。\n"
                '只输出 JSON：{"still_open": true/false, "reason": "一句话"}'
            )
            result = await _llm(prompt)
            try:
                result = result.strip()
                if result.startswith("```"):
                    result = result.split("\n", 1)[-1].rsplit("```", 1)[0]
                verdict = json.loads(result)
            except Exception:
                continue
            if verdict.get("still_open"):
                continue
            m["resolved"] = True
            comments = m.get("comments") if isinstance(m.get("comments"), list) else []
            comments.append({
                "date": report["generated_at"],
                "author": "memory_doctor",
                "kind": "auto_fix",
                "content": f"体检自动摘牌：不再是进行中的待办（{str(verdict.get('reason', ''))[:80]}）",
            })
            m["comments"] = comments
            store.set_memory(m)
            report["auto_fixed"].append({
                "id": m.get("id"), "type": "stale_todo",
                "detail": f"过期待办已摘牌：{m['content'][:50]}…",
            })
    except Exception as e:
        log.warning(f"doctor unresolved review failed: {e}")

    # ── 2.5 身份别名张冠李戴（内容级）──
    # 实例：「Lucien（夜鹭/狐狸）」——夜鹭是小克的别称，被塞进了 Lucien 的括号。
    # 结构性检查看不到这种错误，必须扫正文。报告不自动修。
    try:
        import re
        alias_owner = {}
        for canon, aliases in _AI_DISPLAY_ALIASES.items():
            for a in aliases:
                alias_owner[a] = canon
        pat = re.compile(r"([A-Za-z一-鿿]{2,10})[（(]([^）)]{1,50})[）)]")
        found = 0
        for mem in active:
            content = mem.get("content") or ""
            for name, inside in pat.findall(content):
                owner = alias_owner.get(name.lower()) or alias_owner.get(name)
                if not owner:
                    continue
                for alias, a_owner in alias_owner.items():
                    if a_owner != owner and alias in inside:
                        report["issues"].append({
                            "type": "identity_alias_confusion",
                            "memory_id": mem["id"],
                            "detail": f"「{name}（…{alias}…）」——{alias} 是 {a_owner} 的别称，不是 {owner} 的",
                            "content_head": content[:80],
                        })
                        found += 1
                        break
                if found >= 20:
                    break
            if found >= 20:
                break
        report["stats"]["identity_alias_confusion"] = found
    except Exception as e:
        log.warning(f"doctor identity alias check failed: {e}")

    # ── 2.6 身份类房间串味（事件/技术支持/玩梗被当成稳定偏好/核心人设）──
    # living_room 是 type=always 的核心身份房间，每次唤醒必读，串味进这里
    # 比进 preferences 更严重（实例：「享受被骗」玩梗被客厅刷新升格成人设双胞胎）
    _IDENTITY_ROOMS = {"preferences", "living_room"}
    try:
        misfiled = []
        for mem in active:
            if mem.get("room") not in _IDENTITY_ROOMS:
                continue
            content = mem.get("content") or ""
            prov = mem.get("provenance_type") or ""
            if content.startswith("[互动]") or prov == "roleplay_meme":
                misfiled.append({
                    "type": "identity_room_misfile",
                    "memory_id": mem["id"],
                    "room": mem.get("room"),
                    "detail": "疑似事件/互动/玩梗被归入身份类房间（这里只该放长期稳定的事实）",
                    "content_head": content[:80],
                })
            elif (mem.get("room") == "living_room"
                  and prov == "ai_summary"
                  and float(mem.get("importance", 0.5) or 0.5) >= 0.75):
                # AI 再总结写进核心身份且高 importance：需要人工确认没把梗升格
                misfiled.append({
                    "type": "identity_room_misfile",
                    "memory_id": mem["id"],
                    "room": "living_room",
                    "detail": "AI 摘要以高 importance 写入核心身份房间，请确认不是玩梗升格",
                    "content_head": content[:80],
                })
        report["issues"].extend(misfiled[:15])
        report["stats"]["identity_room_misfile"] = len(misfiled)
    except Exception as e:
        log.warning(f"doctor identity room misfile check failed: {e}")

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
    pool = stats.get("pool") or {}
    if pool:
        parts = "、".join(f"{k} {v} 条" for k, v in pool.items())
        sizes = stats.get("db_sizes") or {}
        size_part = f"（记忆库 {sizes.get('memories.db', '?')}）" if sizes else ""
        lines.append(f"· 记忆池：{parts}{size_part}")
    return "\n".join(lines)
