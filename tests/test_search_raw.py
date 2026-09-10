"""Tests for search_raw: synonym expansion + speaker_filter + privacy."""
import sqlite3
import pytest
from unittest.mock import patch
from datetime import datetime, timezone
from pathlib import Path

import raw_vault
import synonym_pairs


# ── synonym_pairs unit tests ──────────────────────────────────────

class TestSynonymPairs:
    def test_expand_with_synonym(self):
        result = synonym_pairs.expand("妈妈")
        assert "母亲" in result
        assert "妈妈" in result

    def test_expand_eating(self):
        result = synonym_pairs.expand("吃饭")
        assert "聚餐" in result

    def test_expand_no_synonym(self):
        result = synonym_pairs.expand("臭味大排挡")
        assert result == ["臭味大排挡"]

    def test_expand_empty(self):
        assert synonym_pairs.expand("") == []
        assert synonym_pairs.expand("  ") == []

    def test_expand_returns_sorted(self):
        result = synonym_pairs.expand("妈妈")
        assert result == sorted(result)

    def test_all_groups_bidirectional(self):
        for group in synonym_pairs._GROUPS:
            for word in group:
                assert synonym_pairs.expand(word) == sorted(group)


# ── raw_vault.search tests ────────────────────────────────────────

@pytest.fixture()
def test_db(tmp_path):
    db_path = tmp_path / "raw_events.db"
    with patch.object(raw_vault, "DB_PATH", db_path):
        raw_vault._init_db()
        conn = sqlite3.connect(db_path)
        rows = [
            ("cloudy", "telegram", "g1", "public_group",
             "我妈妈今天做了红烧肉", "听起来很好吃！",
             "2026-09-01T12:00:00+00:00"),
            ("cloudy", "telegram", "g1", "public_group",
             "我母亲说明天来看我", "真好呀",
             "2026-09-02T12:00:00+00:00"),
            ("lucien", "telegram", "g2", "public_group",
             "昨天去聚餐了", "在哪吃的？",
             "2026-09-03T12:00:00+00:00"),
            ("cloudy", "telegram", "dm1", "private",
             "臭味大排挡真好吃", "哈哈确实",
             "2026-09-04T12:00:00+00:00"),
            ("lucien", "telegram", "dm2", "private",
             "我们去看病吧", "好的我陪你",
             "2026-09-05T12:00:00+00:00"),
            ("jasper", "telegram", "g1", "public_group",
             "今天好开心啊", "是什么让你开心？",
             "2026-09-06T12:00:00+00:00"),
        ]
        conn.executemany(
            "INSERT INTO raw_events (ai_id, platform, chat_id, chat_type, "
            "user_text, ai_text, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()
        yield db_path


class TestSearchSynonymExpansion:
    def test_mama_finds_muqin(self, test_db):
        with patch.object(raw_vault, "DB_PATH", test_db):
            hits = raw_vault.search("妈妈", ai_id="cloudy")
            texts = [h["user_text"] for h in hits]
            assert any("母亲" in t for t in texts)
            assert any("妈妈" in t for t in texts)

    def test_chifan_finds_jucan(self, test_db):
        with patch.object(raw_vault, "DB_PATH", test_db):
            hits = raw_vault.search("吃饭")
            texts = [h["user_text"] for h in hits]
            assert any("聚餐" in t for t in texts)

    def test_no_synonym_literal_match(self, test_db):
        with patch.object(raw_vault, "DB_PATH", test_db):
            hits = raw_vault.search("臭味大排挡", ai_id="cloudy")
            assert len(hits) == 1
            assert "臭味大排挡" in hits[0]["user_text"]

    def test_no_match_returns_empty(self, test_db):
        with patch.object(raw_vault, "DB_PATH", test_db):
            hits = raw_vault.search("完全不存在的词")
            assert hits == []


class TestSearchSpeakerFilter:
    def test_default_searches_all(self, test_db):
        with patch.object(raw_vault, "DB_PATH", test_db):
            hits = raw_vault.search("好吃", ai_id="cloudy")
            texts_user = [h["user_text"] for h in hits]
            texts_ai = [h["ai_text"] for h in hits]
            all_text = " ".join(texts_user + texts_ai)
            assert "好吃" in all_text

    def test_speaker_user_only(self, test_db):
        with patch.object(raw_vault, "DB_PATH", test_db):
            hits = raw_vault.search("好吃", ai_id="cloudy",
                                    speaker_filter="user")
            for h in hits:
                assert "好吃" in h["user_text"]

    def test_speaker_ai_only(self, test_db):
        with patch.object(raw_vault, "DB_PATH", test_db):
            hits = raw_vault.search("好吃", ai_id="cloudy",
                                    speaker_filter="ai")
            for h in hits:
                assert "好吃" in h["ai_text"]


class TestSearchPrivacy:
    def test_no_ai_id_excludes_private(self, test_db):
        """Without ai_id, private DMs must not be returned."""
        with patch.object(raw_vault, "DB_PATH", test_db):
            hits = raw_vault.search("臭味大排挡")
            assert len(hits) == 0

    def test_ai_id_includes_own_private(self, test_db):
        """With ai_id=cloudy, cloudy's private DMs are included."""
        with patch.object(raw_vault, "DB_PATH", test_db):
            hits = raw_vault.search("臭味大排挡", ai_id="cloudy")
            assert len(hits) == 1

    def test_ai_id_does_not_see_other_private(self, test_db):
        """cloudy cannot see lucien's private DMs."""
        with patch.object(raw_vault, "DB_PATH", test_db):
            hits = raw_vault.search("看病", ai_id="cloudy")
            assert len(hits) == 0

    def test_no_ai_id_sees_public_group(self, test_db):
        """Without ai_id, public_group conversations are visible."""
        with patch.object(raw_vault, "DB_PATH", test_db):
            hits = raw_vault.search("开心")
            assert len(hits) >= 1
            assert all(h["chat_type"] != "private" for h in hits)

    def test_happy_synonym_across_ais(self, test_db):
        """搜 '高兴' (synonym of 开心) without ai_id finds jasper's public msg."""
        with patch.object(raw_vault, "DB_PATH", test_db):
            hits = raw_vault.search("高兴")
            texts = [h["user_text"] for h in hits]
            assert any("开心" in t for t in texts)
