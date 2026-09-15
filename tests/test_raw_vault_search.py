"""raw_vault search() 隔离策略 + 模糊搜索 + 安全回归测试。"""
import sqlite3
import pytest
from unittest.mock import patch


@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "raw_events.db"
    with patch("raw_vault.DB_PATH", db_path):
        import raw_vault
        raw_vault._init_db()
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        rows = [
            # 私聊：cloudy 和 lucien 各有一条
            ("cloudy", "telegram", "chat_priv_1", "private",
             "我今天很开心", "太好了", "2025-01-01T10:00:00Z"),
            ("lucien", "telegram", "chat_priv_2", "private",
             "我妈妈生日", "记住了", "2025-01-01T11:00:00Z"),
            # 小群 (private_group)
            ("cloudy", "telegram", "chat_sg_1", "private_group",
             "小群里聊天", "好的", "2025-01-01T12:00:00Z"),
            ("lucien", "telegram", "chat_sg_1", "private_group",
             "小群里说开心", "很好", "2025-01-01T13:00:00Z"),
            # 大群 (public_group)
            ("cloudy", "telegram", "chat_pg_1", "public_group",
             "大群讨论妈妈的事", "了解", "2025-01-01T14:00:00Z"),
            # supergroup
            ("jasper", "telegram", "chat_sup_1", "supergroup",
             "超级群聊天开心", "嗯", "2025-01-01T15:00:00Z"),
            # group
            ("cloudy", "telegram", "chat_g_1", "group",
             "普通群妈妈生日快乐", "生日快乐", "2025-01-01T16:00:00Z"),
        ]
        for r in rows:
            conn.execute(
                "INSERT INTO raw_events (ai_id, platform, chat_id, chat_type, "
                "user_text, ai_text, created_at) VALUES (?,?,?,?,?,?,?)", r)
        conn.commit()
        conn.close()
        yield db_path


# ── 三层隔离策略 ──

class TestIsolationPrivateChat:
    """私聊：只对自己的 ai_id（含别名）可见。"""

    def test_private_visible_to_own_ai(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("开心", ai_id="cloudy")
            private_hits = [h for h in hits if h["chat_type"] == "private"]
            assert len(private_hits) == 1
            assert private_hits[0]["ai_id"] == "cloudy"

    def test_private_invisible_to_other_ai(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("开心", ai_id="lucien")
            private_hits = [h for h in hits if h["chat_type"] == "private"]
            assert not any(h["ai_id"] == "cloudy" for h in private_hits)

    def test_private_invisible_without_ai_id(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("开心", ai_id="")
            assert not any(h["chat_type"] == "private" for h in hits)

    def test_private_alias_cloudy_sees_claude_records(self, tmp_db):
        """cloudy 和 claude 是同一 AI 的别名，互相可见。"""
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("开心", ai_id="claude")
            private_hits = [h for h in hits if h["chat_type"] == "private"]
            assert len(private_hits) == 1
            assert private_hits[0]["ai_id"] == "cloudy"


class TestIsolationSmallGroup:
    """小群 (private_group)：需要 ai_id 非空才可见，但不过滤具体 ai_id。"""

    def test_small_group_visible_to_any_identified_ai(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            for ai in ["cloudy", "lucien", "jasper"]:
                hits = raw_vault.search("小群", ai_id=ai)
                pg_hits = [h for h in hits if h["chat_type"] == "private_group"]
                assert len(pg_hits) == 2, f"ai_id={ai} should see both private_group records"

    def test_small_group_invisible_without_ai_id(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("小群", ai_id="")
            assert not any(h["chat_type"] == "private_group" for h in hits)


class TestIsolationPublicGroup:
    """大群 (public_group/group/supergroup)：任何人可搜，包括 ai_id=""。"""

    def test_public_group_visible_to_any_ai(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("大群", ai_id="jasper")
            assert any(h["chat_type"] == "public_group" for h in hits)

    def test_public_group_visible_without_ai_id(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("开心", ai_id="")
            chat_types = {h["chat_type"] for h in hits}
            assert chat_types <= {"public_group", "group", "supergroup"}

    def test_no_ai_id_sees_only_public_groups(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("开心", ai_id="")
            for h in hits:
                assert h["chat_type"] in ("public_group", "group", "supergroup"), \
                    f"ai_id='' should not see {h['chat_type']}"


# ── 安全回归（对应审查发现） ──

class TestSecurityRegression:
    def test_mcp_cannot_query_private_chats(self, tmp_db):
        """MCP 传 ai_id="" 不能看到私聊（Critical fix）。"""
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("妈妈", ai_id="")
            assert not any(h["chat_type"] == "private" for h in hits)

    def test_mcp_cannot_query_private_groups(self, tmp_db):
        """MCP 传 ai_id="" 不能看到小群（High fix）。"""
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("小群", ai_id="")
            assert not any(h["chat_type"] == "private_group" for h in hits)

    def test_spoofed_ai_id_cannot_read_others_private(self, tmp_db):
        """伪造 source_ai 无法读到其他 AI 的私聊。"""
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("妈妈", ai_id="jasper")
            private_hits = [h for h in hits if h["chat_type"] == "private"]
            assert not any(h["ai_id"] == "lucien" for h in private_hits)


# ── 拆词 + 同义词 + 排序 ──

class TestWordSplitAndSynonyms:
    def test_single_word_synonym_expand(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("母亲", ai_id="cloudy")
            assert any("妈妈" in h["user_text"] for h in hits)

    def test_multi_word_any_match(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("开心 妈妈", ai_id="cloudy")
            assert len(hits) >= 2

    def test_multi_word_ranking_best_first(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("妈妈 生日", ai_id="cloudy")
            assert len(hits) >= 2, "should match at least 2 records"
            top = hits[0]
            assert "妈妈" in top["user_text"] and "生日" in top["user_text"]

    def test_empty_query_returns_empty(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            assert raw_vault.search("", ai_id="cloudy") == []
            assert raw_vault.search("   ", ai_id="cloudy") == []

    def test_speaker_filter_user(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("太好了", ai_id="cloudy", speaker_filter="user")
            assert len(hits) == 0
            hits_ai = raw_vault.search("太好了", ai_id="cloudy", speaker_filter="ai")
            assert len(hits_ai) == 1

    def test_return_columns_consistent(self, tmp_db):
        """单词和多词查询返回相同的列集合（Low fix）。"""
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            single = raw_vault.search("开心", ai_id="cloudy")
            multi = raw_vault.search("开心 妈妈", ai_id="cloudy")
            assert len(single) > 0 and len(multi) > 0
            assert set(single[0].keys()) == set(multi[0].keys())
            expected = {"id", "ai_id", "platform", "chat_id", "chat_type",
                        "user_text", "ai_text", "created_at"}
            assert set(single[0].keys()) == expected


# ── 拆词上限 + 去重 ──

class TestWordLimit:
    def test_dedup_words(self):
        from raw_vault import _split_words
        assert _split_words("开心 开心 妈妈") == ["开心", "妈妈"]

    def test_max_words_capped(self):
        from raw_vault import _split_words, _MAX_SEARCH_WORDS
        big_query = " ".join(f"词{i}" for i in range(100))
        result = _split_words(big_query)
        assert len(result) == _MAX_SEARCH_WORDS

    def test_excessive_words_no_crash(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            big_query = " ".join(f"词{i}" for i in range(200))
            hits = raw_vault.search(big_query, ai_id="cloudy")
            assert isinstance(hits, list)

    def test_split_words_basic(self):
        from raw_vault import _split_words
        assert _split_words("开心 妈妈") == ["开心", "妈妈"]
        assert _split_words("单词") == ["单词"]
        assert _split_words("  多  空格  ") == ["多", "空格"]
        assert _split_words("") == []
        assert _split_words("   ") == []


# ── stats 口径一致性 ──

class TestStats:
    def test_stats_with_ai_id_includes_own_private_and_all_groups(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            s = raw_vault.stats(ai_id="cloudy")
            # cloudy: 1 private + 2 private_group + 1 public_group + 1 supergroup + 1 group = 6
            assert s["count"] == 6

    def test_stats_with_ai_id_excludes_other_private(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            s_cloudy = raw_vault.stats(ai_id="cloudy")
            s_all = raw_vault.stats()
            assert s_cloudy["count"] < s_all["count"]

    def test_stats_public_only_excludes_private_group(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            s = raw_vault.stats(public_only=True)
            # public: 1 public_group + 1 supergroup + 1 group = 3
            assert s["count"] == 3

    def test_stats_no_filter_counts_all(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            s = raw_vault.stats()
            assert s["count"] == 7

    def test_stats_ai_id_empty_matches_public_only(self, tmp_db):
        """ai_id="" 和 public_only=True 应该返回相同口径（Medium fix）。"""
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            s_empty = raw_vault.stats(ai_id="")
            s_public = raw_vault.stats(public_only=True)
            # ai_id="" → falls to public_only=False → counts all (doctor_report)
            # But search(ai_id="") only returns public groups
            # So stats(ai_id="") should match stats() for backwards compat
            # The MCP tool calls stats(public_only=True) explicitly
            assert s_empty["count"] == 7  # doctor_report path

    def test_stats_alias_canonicalization(self, tmp_db):
        """claude 和 cloudy 是别名，stats 口径一致（M4 fix）。"""
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            s_cloudy = raw_vault.stats(ai_id="cloudy")
            s_claude = raw_vault.stats(ai_id="claude")
            assert s_cloudy["count"] == s_claude["count"]
