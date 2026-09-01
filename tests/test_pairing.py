"""缘分榜（pairing）：本地互动排名、分数上限、提示词事实块与卡片渲染。

棱镜姻缘和人格画像的区别全靠这一层撑着：配对结果先在本地数出来，再当既成事实
交给模型。所以"双向往来排在单向前面""单向不许超过 74 分""榜首名字会进提示词"
这几条必须有回归测试兜着。
"""

from __future__ import annotations

from astrbot_plugin_persona_prism.prism import cards, dialogue, pairing
from astrbot_plugin_persona_prism.prism.models import Pairing, Portrait, Section, Tag

TA = "1001"
BASE = 1700000000


def _link(uid, name, mine=0, theirs=0, nearby=0):
    return dialogue.SocialLink(user_id=uid, name=name, mine=mine, theirs=theirs, nearby=nearby)


def _row(mid, uid, text, offset=0, **extra):
    row = {
        "message_id": mid,
        "user_id": uid,
        "user_name": {TA: "狐狸", "1002": "鹿鹿", "1003": "熊熊"}.get(uid, "U" + uid),
        "text": text,
        "ts": BASE + offset,
    }
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# SocialLink 权重
# ---------------------------------------------------------------------------


class TestSocialLink:
    def test_mutual_needs_both_directions(self):
        assert _link("1002", "鹿鹿", mine=2, theirs=1).mutual is True
        assert _link("1002", "鹿鹿", mine=5).mutual is False

    def test_mutual_link_outweighs_louder_one_way(self):
        mutual = _link("1002", "鹿鹿", mine=3, theirs=3)
        one_way = _link("1003", "熊熊", mine=5)
        assert mutual.weight > one_way.weight

    def test_nearby_only_carries_little_weight(self):
        assert _link("1002", "鹿鹿", nearby=4).weight < _link("1003", "熊熊", mine=1).weight


# ---------------------------------------------------------------------------
# 排名
# ---------------------------------------------------------------------------


class TestRankPairings:
    def test_empty_input_gives_empty_board(self):
        assert pairing.rank_pairings([]) == []

    def test_links_without_any_contact_are_dropped(self):
        assert pairing.rank_pairings([_link("1002", "鹿鹿")]) == []

    def test_nameless_links_are_dropped(self):
        assert pairing.rank_pairings([_link("1002", "", mine=3)]) == []

    def test_mutual_partner_takes_the_crown(self):
        board = pairing.rank_pairings(
            [_link("1003", "熊熊", mine=6), _link("1002", "鹿鹿", mine=4, theirs=4)],
        )
        assert board[0].name == "鹿鹿"
        assert board[0].mutual is True

    def test_scores_are_descending(self):
        board = pairing.rank_pairings(
            [
                _link("1002", "鹿鹿", mine=5, theirs=5),
                _link("1003", "熊熊", mine=2, theirs=1),
                _link("1004", "兔兔", nearby=2),
            ],
        )
        scores = [item.score for item in board]
        assert scores == sorted(scores, reverse=True)

    def test_one_way_is_capped(self):
        board = pairing.rank_pairings([_link("1003", "熊熊", mine=40)])
        assert board[0].score <= pairing.ONE_WAY_CAP

    def test_nearby_only_is_capped_lower(self):
        board = pairing.rank_pairings([_link("1004", "兔兔", nearby=30)])
        assert board[0].score <= pairing.NEARBY_CAP

    def test_top_score_stays_in_range(self):
        board = pairing.rank_pairings([_link("1002", "鹿鹿", mine=30, theirs=30)])
        assert pairing.TOP_FLOOR <= board[0].score <= 99

    def test_thin_evidence_still_gets_a_readable_score(self):
        board = pairing.rank_pairings([_link("1002", "鹿鹿", mine=1, theirs=1)])
        assert board[0].score >= 12

    def test_limit_truncates_the_board(self):
        links = [_link(str(1002 + i), "人" + str(i), mine=5 - i, theirs=1) for i in range(4)]
        assert len(pairing.rank_pairings(links, limit=2)) == 2

    def test_limit_zero_still_keeps_the_champion(self):
        board = pairing.rank_pairings([_link("1002", "鹿鹿", mine=2, theirs=2)], limit=0)
        assert len(board) == 1

    def test_excluded_ids_are_skipped(self):
        board = pairing.rank_pairings(
            [_link("1002", "鹿鹿", mine=4, theirs=4), _link("1003", "熊熊", mine=2, theirs=2)],
            exclude_ids=["1002"],
        )
        assert [item.name for item in board] == ["熊熊"]

    def test_notes_are_player_facing(self):
        board = pairing.rank_pairings(
            [
                _link("1002", "鹿鹿", mine=3, theirs=2),
                _link("1003", "熊熊", mine=2),
                _link("1004", "兔兔", theirs=2),
                _link("1005", "猫猫", nearby=3),
            ],
        )
        notes = {item.name: item.note for item in board}
        assert "5" in notes["鹿鹿"]
        assert notes["熊熊"].startswith("TA 主动")
        assert "对方" in notes["兔兔"]
        assert "同场" in notes["猫猫"]

    def test_ties_break_by_name_so_results_are_stable(self):
        links = [_link("1003", "熊熊", mine=2, theirs=2), _link("1002", "鹿鹿", mine=2, theirs=2)]
        first = [item.name for item in pairing.rank_pairings(links)]
        second = [item.name for item in pairing.rank_pairings(list(reversed(links)))]
        assert first == second


class TestPairingsFromMentions:
    def test_empty_partners_give_empty_board(self):
        assert pairing.pairings_from_mentions([]) == []

    def test_zero_counts_are_dropped(self):
        assert pairing.pairings_from_mentions([("鹿鹿", 0)]) == []

    def test_fallback_board_is_never_marked_mutual(self):
        board = pairing.pairings_from_mentions([("鹿鹿", 6), ("熊熊", 2)])
        assert [item.name for item in board] == ["鹿鹿", "熊熊"]
        assert all(item.mutual is False for item in board)
        assert board[0].score <= pairing.ONE_WAY_CAP

    def test_fallback_respects_limit(self):
        board = pairing.pairings_from_mentions([("A", 5), ("B", 4), ("C", 3)], limit=2)
        assert len(board) == 2


# ---------------------------------------------------------------------------
# 提示词事实块
# ---------------------------------------------------------------------------


class TestFactsBlock:
    def test_empty_board_writes_nothing(self):
        assert pairing.facts_block([]) == ""

    def test_block_pins_names_scores_and_champion(self):
        board = [
            Pairing(name="鹿鹿", score=88, note="一来一回搭过 5 次话", mutual=True),
            Pairing(name="熊熊", score=61, note="TA 主动找过 2 次"),
        ]
        text = pairing.facts_block(board)
        assert "鹿鹿" in text and "88" in text
        assert "双向往来" in text and "暂时单向" in text
        assert "headline" in text
        assert "不得改动" in text

    def test_top_name_helper(self):
        assert pairing.top_name([]) == ""
        assert pairing.top_name([Pairing(name="鹿鹿", score=70)]) == "鹿鹿"


# ---------------------------------------------------------------------------
# social_signals 产出的往来记录
# ---------------------------------------------------------------------------


class TestSocialSignalLinks:
    def test_reply_and_at_count_as_outgoing(self):
        rows = [
            _row("1", "1002", "在吗", 0),
            _row("2", TA, "在", 10, reply_to="1"),
            _row("3", TA, "叫你呢", 20, at_ids="1002"),
        ]
        sig = dialogue.social_signals(rows, TA)
        link = {item.user_id: item for item in sig.links}["1002"]
        assert link.mine == 2
        assert link.theirs == 0
        assert link.name == "鹿鹿"

    def test_being_replied_counts_as_incoming(self):
        rows = [_row("1", TA, "有人吗", 0), _row("2", "1002", "我在", 10, reply_to="1")]
        sig = dialogue.social_signals(rows, TA)
        assert sig.links[0].theirs == 1
        assert sig.links[0].mine == 0

    def test_target_never_links_to_itself(self):
        rows = [_row("1", TA, "自言自语", 0), _row("2", TA, "接着说", 10, reply_to="1")]
        sig = dialogue.social_signals(rows, TA)
        assert all(item.user_id != TA for item in sig.links)

    def test_nearby_is_recorded_when_no_reply_exists(self):
        rows = [_row("1", TA, "今天好累", 0), _row("2", "1002", "我也", 30)]
        sig = dialogue.social_signals(rows, TA)
        link = sig.links[0]
        assert link.nearby >= 1
        assert link.mine == 0 and link.theirs == 0

    def test_far_apart_messages_are_not_nearby(self):
        rows = [_row("1", TA, "早", 0), _row("2", "1002", "晚", 6 * 60 * 60)]
        sig = dialogue.social_signals(rows, TA)
        assert sig.links == []

    def test_links_are_sorted_by_weight(self):
        rows = [
            _row("1", "1002", "喂", 0),
            _row("2", TA, "嗯", 10, reply_to="1"),
            _row("3", "1002", "哦", 20, reply_to="2"),
            _row("4", "1003", "路过", 30),
        ]
        sig = dialogue.social_signals(rows, TA)
        assert sig.links[0].user_id == "1002"

    def test_board_can_be_built_straight_from_signals(self):
        rows = [
            _row("1", "1002", "喂", 0),
            _row("2", TA, "嗯", 10, reply_to="1"),
            _row("3", "1002", "哦", 20, reply_to="2"),
        ]
        board = pairing.rank_pairings(dialogue.social_signals(rows, TA).links)
        assert board and board[0].name == "鹿鹿"


# ---------------------------------------------------------------------------
# 模型层与卡片
# ---------------------------------------------------------------------------


class TestPortraitCarriesPairings:
    def test_round_trip_keeps_the_board(self):
        portrait = Portrait(
            kind="match",
            headline="和鹿鹿最聊得来",
            pairings=[Pairing(name="鹿鹿", user_id="1002", score=88, note="搭过 5 次话", mutual=True)],
        )
        again = Portrait.from_dict(portrait.to_dict())
        assert again.pairings[0].name == "鹿鹿"
        assert again.pairings[0].score == 88
        assert again.pairings[0].mutual is True

    def test_plain_text_mentions_the_board(self):
        portrait = Portrait(
            kind="match",
            headline="和鹿鹿最聊得来",
            tags=[Tag("互相捧场")],
            pairings=[Pairing(name="鹿鹿", score=88)],
        )
        assert "鹿鹿" in portrait.to_plain_text("群友姻缘")

    def test_missing_pairings_default_to_empty(self):
        assert Portrait.from_dict({"kind": "match", "headline": "x"}).pairings == []


def _match_ctx():
    return cards.CardContext(
        title="群友姻缘",
        kind_label="群友姻缘",
        target_name="小明",
        target_id="10001",
        group_name="测试群",
        sample_size=120,
    )


class TestMatchCard:
    def _portrait(self):
        return Portrait(
            kind="match",
            headline="和鹿鹿互相捧场的固定搭子",
            tags=[Tag("互相捧场", "positive")],
            sections=[Section("最佳搭子", "两个人经常一起接梗。")],
            pairings=[
                Pairing(name="鹿鹿", user_id="1002", score=88, note="一来一回搭过 5 次话", mutual=True),
                Pairing(name="熊熊", user_id="1003", score=57, note="TA 主动找过 2 次"),
            ],
            structured=True,
        )

    def test_card_renders_the_pair_panel(self):
        html = cards.build_card_html(self._portrait(), _match_ctx())
        assert 'class="panel panel-pair"' in html
        assert "鹿鹿" in html
        assert "88%" in html

    def test_runner_up_is_listed(self):
        html = cards.build_card_html(self._portrait(), _match_ctx())
        assert "熊熊" in html and "pr-row" in html

    def test_panel_titles_are_match_flavoured(self):
        html = cards.build_card_html(self._portrait(), _match_ctx())
        assert "缘分解读" in html

    def test_pair_styles_ship_with_the_card(self):
        # 这段样式曾经被塞进 Markdown 卡专用的 CSS 里，结构化卡片拿不到，版式整个塌掉。
        html = cards.build_card_html(self._portrait(), _match_ctx())
        assert ".pair-top" in html and ".pr-row" in html

    def test_portrait_card_has_no_pair_panel(self):
        plain = Portrait(kind="portrait", headline="夜猫子", sections=[Section("印象", "话多。")], structured=True)
        html = cards.build_card_html(plain, _match_ctx())
        assert 'class="panel panel-pair"' not in html


class TestMarkdownMatchCard:
    def test_top_html_is_placed_above_the_body(self):
        portrait = Portrait(
            kind="legacy_match",
            pairings=[Pairing(name="鹿鹿", user_id="1002", score=81, mutual=True)],
        )
        top = cards.pairings_panel_html(portrait, _match_ctx())
        html = cards.build_markdown_card_html("## 红娘报告\n\n有点缘分。", _match_ctx(), top_html=top)
        assert html.index('class="panel panel-pair"') < html.index('<div class="md-body">')
        assert "鹿鹿" in html

    def test_empty_board_adds_nothing(self):
        portrait = Portrait(kind="legacy_match")
        assert cards.pairings_panel_html(portrait, _match_ctx()) == ""
