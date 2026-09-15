"""raw_vault search() 隔离策略 + 模糊搜索测试。"""
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone


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


class TestIsolation:
    def test_private_only_visible_to_own_ai(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("开心", ai_id="cloudy")
            chat_types = {h["chat_type"] for h in hits}
            private_hits = [h for h in hits if h["chat_type"] == "private"]
            assert all(h["ai_id"] == "cloudy" for h in private_hits)
            assert any(h["chat_type"] == "private" for h in hits)

    def test_private_not_visible_to_other_ai(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("开心", ai_id="lucien")
            private_hits = [h for h in hits if h["chat_type"] == "private"]
            assert all(h["ai_id"] == "lucien" for h in private_hits)
            assert not any(h["user_text"] == "我今天很开心" for h in hits)

    def test_small_group_visible_to_all_ai(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits_cloudy = raw_vault.search("小群", ai_id="cloudy")
            hits_lucien = raw_vault.search("小群", ai_id="lucien")
            hits_jasper = raw_vault.search("小群", ai_id="jasper")
            for hits in [hits_cloudy, hits_lucien, hits_jasper]:
                pg_hits = [h for h in hits if h["chat_type"] == "private_group"]
                assert len(pg_hits) >= 1

    def test_public_group_visible_to_all_ai(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("大群", ai_id="jasper")
            assert any(h["chat_type"] == "public_group" for h in hits)

    def test_no_ai_id_only_groups(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("开心", ai_id="")
            assert not any(h["chat_type"] == "private" for h in hits)
            chat_types = {h["chat_type"] for h in hits}
            assert chat_types <= {"public_group", "group", "supergroup", "private_group"}

    def test_private_group_in_no_ai_id_mode(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("小群", ai_id="")
            assert any(h["chat_type"] == "private_group" for h in hits)


class TestWordSplitAndSynonyms:
    def test_single_word_synonym_expand(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("母亲", ai_id="")
            assert any("妈妈" in h["user_text"] for h in hits)

    def test_multi_word_any_match(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("开心 妈妈", ai_id="")
            assert len(hits) >= 2

    def test_multi_word_ranking(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            hits = raw_vault.search("妈妈 生日", ai_id="")
            if len(hits) >= 2:
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
            assert len(hits_ai) >= 1


class TestStats:
    def test_stats_with_ai_id_includes_private_and_groups(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            s = raw_vault.stats(ai_id="cloudy")
            assert s["count"] >= 4  # 1 private + 1 small + 1 public + 1 group

    def test_stats_with_ai_id_excludes_other_private(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            s_cloudy = raw_vault.stats(ai_id="cloudy")
            s_all = raw_vault.stats()
            assert s_cloudy["count"] < s_all["count"]

    def test_stats_public_only_includes_private_group(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            s = raw_vault.stats(public_only=True)
            assert s["count"] >= 4  # 2 private_group + 1 public_group + 1 supergroup + 1 group

    def test_stats_no_filter_counts_all(self, tmp_db):
        with patch("raw_vault.DB_PATH", tmp_db):
            import raw_vault
            s = raw_vault.stats()
            assert s["count"] == 7


class TestSplitWords:
    def test_split_words(self):
        from raw_vault import _split_words
        assert _split_words("开心 妈妈") == ["开心", "妈妈"]
        assert _split_words("单词") == ["单词"]
        assert _split_words("  多  空格  ") == ["多", "空格"]
        assert _split_words("") == []
        assert _split_words("   ") == []
