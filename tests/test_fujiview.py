# -*- coding: utf-8 -*-
"""fujiview.py（富士山可視度の読込・大字照合）の単体テスト。

指示書 docs/tasks/20260919_fujimap_link.md D の要求ケースに対応。
fixture: tests/fixtures/oaza_scores_sample.json（函南町: 平井/丹那/畑毛、
御殿場市: 神山/神山平、伊豆市: 冷川）。

実行方法: プロジェクトルート(.venv)で `python -m pytest tests/test_fujiview.py -v`
"""
from pathlib import Path

import pytest

import fujiview

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "oaza_scores_sample.json"
MISSING_PATH = Path(__file__).resolve().parent / "fixtures" / "does_not_exist_oaza_scores.json"


@pytest.fixture(autouse=True)
def _reset_and_load_fixture():
    """各テストの前に fujiview の索引キャッシュをリセットし、サンプルfixtureを
    読み直す（load_table(path=...)はキャッシュを常に上書きする実装のため、
    前のテストの状態が残らない）。"""
    fujiview._index_cache = None
    fujiview.load_table(path=FIXTURE_PATH)
    yield
    fujiview._index_cache = None


# ---------------------------------------------------------------------------
# lookup(): 住所文字列から大字を特定する（3ステップ・神山/神山平ガード）
# ---------------------------------------------------------------------------

def test_lookup_full_address_with_gun_prefix():
    # 「静岡県田方郡函南町平井1689-55」→ machi直後の残り文字列先頭が候補「平井」に一致
    hit = fujiview.lookup("静岡県田方郡函南町平井1689-55", "函南町")
    assert hit is not None
    assert hit["oaza"] == "平井"
    assert hit["lo"] == 5 and hit["hi"] == 80 and hit["med"] == 40 and hit["max"] == 95
    assert hit["n"] == 123


def test_lookup_strips_whitespace_between_machi_and_oaza():
    # 「函南町 丹那」→ NFKC正規化後に空白除去してから残り文字列を作る
    hit = fujiview.lookup("函南町 丹那", "函南町")
    assert hit is not None
    assert hit["oaza"] == "丹那"


def test_lookup_strips_leading_ooaza_prefix():
    # 「伊豆市大字冷川」→ 残り文字列の先頭の「大字」を除いてから候補と照合
    hit = fujiview.lookup("伊豆市大字冷川", "伊豆市")
    assert hit is not None
    assert hit["oaza"] == "冷川"


def test_lookup_prefers_longest_prefix_match_kamiyama_vs_kamiyamadaira():
    # 候補に「神山」「神山平」の両方がある場合、残り文字列の先頭一致は最長の
    # 「神山平」を採用する（既存interest_groupsの神山/神山平問題と同じ大字境界）。
    hit = fujiview.lookup("御殿場市神山平２丁目2-2", "御殿場市")
    assert hit is not None
    assert hit["oaza"] == "神山平"


def test_lookup_returns_none_when_only_kamiyama_candidate_and_text_is_kamiyamadaira():
    # 対象大字が「神山」だけの索引だと、「神山平」の一部として誤って「神山」に
    # 一致してはいけない（直後が別の漢字「平」＝地名の続きの疑い）。
    fujiview._index_cache["御殿場市"] = {
        "神山": fujiview._index_cache["御殿場市"]["神山"],
    }
    hit = fujiview.lookup("御殿場市神山平２丁目2-2", "御殿場市")
    assert hit is None


def test_lookup_returns_none_when_machi_is_empty():
    hit = fujiview.lookup("函南町平井1689-55", "")
    assert hit is None


def test_lookup_returns_none_when_machi_not_in_index():
    # 索引には函南町/御殿場市/伊豆市しかない
    hit = fujiview.lookup("沼津市大手町1-1", "沼津市")
    assert hit is None


def test_lookup_returns_none_when_table_file_missing():
    fujiview._index_cache = None
    idx = fujiview.load_table(path=MISSING_PATH)
    assert idx == {}
    hit = fujiview.lookup("函南町平井1689-55", "函南町")
    assert hit is None


# ---------------------------------------------------------------------------
# summarize(): 表示ラベルの3規則（JS版cellHtmlInnerのcase 'fuji'と同じ規則）
# ---------------------------------------------------------------------------

def test_summarize_max_zero_returns_zero_label():
    hit = fujiview.lookup("函南町畑毛", "函南町")
    assert hit is not None and hit["max"] == 0
    label, mx = fujiview.summarize(hit)
    assert (label, mx) == ("0", 0)


def test_summarize_narrow_range_returns_median_only():
    # 丹那: p10=60,p90=65 → hi-lo=5 (境界値・5以下は中央値1つ)
    hit = fujiview.lookup("函南町丹那", "函南町")
    assert hit is not None and hit["hi"] - hit["lo"] == 5
    label, mx = fujiview.summarize(hit)
    assert (label, mx) == ("63", 70)


def test_summarize_wide_range_returns_lo_to_hi_label():
    # 平井: p10=5,p90=80 → hi-lo=75 (5より大きいのでレンジ表示)
    hit = fujiview.lookup("函南町平井1689-55", "函南町")
    assert hit is not None and hit["hi"] - hit["lo"] > 5
    label, mx = fujiview.summarize(hit)
    assert (label, mx) == ("5〜80", 95)
