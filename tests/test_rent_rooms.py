# -*- coding: utf-8 -*-
"""賃貸タブの「広い家探し」判定（消込表#954・変更分類B）の固定。

適合 = 家賃≤上限 かつ 間取りの部屋数≥rooms_min。値はコードに書かず urls.yaml の
filters.rent を実際に読んで使う（run()/rebuild() と同じ {**filters, **filters.rent} マージ）。
間取りが読めない物件は、価格nullと同じ流儀で「数値不明」。home/camp は rooms_min を持たず
従来の判定のまま変わらない。
"""
from pathlib import Path

import pytest
import watch
import yaml
from madori import rooms_of

_FILTERS = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "urls.yaml").read_text(encoding="utf-8"))["filters"]
RENT = {**_FILTERS, **(_FILTERS.get("rent") or {})}
CAMP = {**_FILTERS, **(_FILTERS.get("camp") or {})}
HOME = dict(_FILTERS)


def _rent(price, madori, filters=RENT):
    return watch._make_record(
        "https://example.com/r", "テスト賃貸", price, None, False, "賃貸 テスト", filters,
        location="三島市", shubetsu_override="賃貸戸建", madori=madori)["verdict"]


@pytest.mark.parametrize("text,expected", [
    ("4LDK", 4), ("4SLDK", 4), ("8SLDK", 8), ("5DK", 5), ("3LDK", 3), ("10LDK", 10),
    ("ワンルーム", 1), ("1R", 1), ("１ＬＤＫ", 1), ("2LDK+3S", 2), ("1LK", 1), ("4LK", 4),
    ("3DK･3K･2LDK･1LDK", 3), ("1DK･1LDK･4DK", 4),
    (None, None), ("", None), ("その他", None), ("44.5～50.8㎡", None),
])
def test_rooms_of(text, expected):
    assert rooms_of(text) == expected


def test_yaml_defines_rooms_min_only_for_rent():
    assert RENT["rooms_min"] > 0  # 閾値の値そのものは urls.yaml が定義元（ここでは固定しない）
    assert "rooms_min" not in HOME and "rooms_min" not in CAMP


def test_rent_4ldk_within_budget_fits():
    assert _rent(18, "4LDK") == "適合"
    assert _rent(15, "8SLDK") == "適合"
    assert _rent(12, "3DK･2LDK･4LDK") == "適合"   # 列挙の最大が条件を満たす


def test_rent_3ldk_does_not_fit():
    assert _rent(15, "3LDK") == "不適合"
    assert _rent(5, "ワンルーム") == "不適合"


def test_rent_over_price_ceiling_does_not_fit():
    assert _rent(20, "4LDK") == "適合"            # 境界は含む
    assert _rent(21, "4LDK") == "不適合"


def test_rent_madori_missing_is_unknown_not_unfit():
    assert _rent(15, None) == "数値不明"
    assert _rent(15, "") == "数値不明"
    assert _rent(15, "その他") == "数値不明"
    assert _rent(None, "4LDK") == "数値不明"       # 価格nullも従来どおり数値不明


def test_rent_without_rooms_min_ignores_madori():
    no_rooms = {k: v for k, v in RENT.items() if k != "rooms_min"}
    assert _rent(10, "1K", no_rooms) == "適合"
    assert _rent(10, None, no_rooms) == "適合"


def test_home_and_camp_verdict_unaffected_by_madori():
    for filters, price, area in ((CAMP, 2000, 1500), (HOME, 1000, 400)):
        rec = watch._make_record("https://example.com/s", "売地", price, area, False,
                                 "売地", filters, location="三島市")
        assert rec["verdict"] == "適合"
        rec2 = watch._make_record("https://example.com/s", "売地", price, area, False,
                                  "売地", filters, location="三島市", madori="1K")
        assert rec2["verdict"] == "適合"


# ---- 表示側（JS）の部屋数絞り込み: madori.ROOMS_JS が rooms_of と同じ読み方をすること ----
# 賃貸タブは SUUMO(4K以上URL)以外（いい部屋ネット・CHINTAI・LIFULL函南）が全間取りの一覧のため、
# 画面で rooms_min 未満の行を外す（間取り不明は残す）。JS は Python を呼べないので規則の写しを持つ。
import json
import shutil
import subprocess

from madori import ROOMS_JS

_NODE = shutil.which("node")
_TABLE = ["4LDK", "4SLDK", "8SLDK", "5DK", "3LDK", "10LDK", "ワンルーム", "1R", "１ＬＤＫ", "2LDK+3S",
          "3DK･3K･2LDK･1LDK", "1DK･1LDK･4DK", "4k", "4K", "５ＤＫ", "ﾜﾝﾙｰﾑ", "3LDK+S(納戸)", "2SLDK",
          "ワンルーム 1R 2K", "1LK", "S", "LDK", "K", "R", "DK 4", "4 LDK", "44.5～50.8㎡", "その他", "", None]


def _node_json(script):
    out = subprocess.run([_NODE, "-e", script], capture_output=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(_NODE is None, reason="node が無いので JS 側の一致試験は省略（rebuild後の目視で代替）")
def test_rooms_js_matches_python_on_table_and_fuzz():
    import random
    rnd = random.Random(954)
    alphabet = list("0123456789１２３４５ＬＤＫＳＲSLDKR ・･+～㎡.-ワンルーム")
    fuzz = ["".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 9))) for _ in range(300)]
    cases = _TABLE + fuzz
    script = (ROOMS_JS + "\nconst cases=" + json.dumps(cases, ensure_ascii=False)
              + ";\nconsole.log(JSON.stringify(cases.map(roomsOf)));")
    assert _node_json(script) == [rooms_of(c) for c in cases]


@pytest.mark.skipif(_NODE is None, reason="node が無いので JS 側の一致試験は省略")
def test_pass_rooms_only_on_rent_tab_and_keeps_unknown():
    script = (
        "const CONFIG={rentRoomsMin:%d};let S={tab:'rent'};\n" % RENT["rooms_min"] + ROOMS_JS + """
const rows=['4LDK','8SLDK','5DK','3LDK','2DK','ワンルーム','','その他',null];
const rent=rows.map(m=>passRooms({madori:m}));
S={tab:'ie'};const ie=rows.map(m=>passRooms({madori:m}));
S={tab:'rent'};CONFIG.rentRoomsMin=null;const off=rows.map(m=>passRooms({madori:m}));
console.log(JSON.stringify({rent,ie,off}));""")
    got = _node_json(script)
    assert got["rent"] == [True, True, True, False, False, False, True, True, True]  # 不明は残す・3部屋以下は出さない
    assert got["ie"] == [True] * 9      # 賃貸タブ以外では絞らない
    assert got["off"] == [True] * 9     # rooms_min 未指定なら絞らない


def test_report_html_carries_rooms_min_and_filter_hook():
    """CONFIG へ urls.yaml の rooms_min が渡り、passFilters が passRooms を呼ぶこと（JSに4を直書きしない）。"""
    watch._MACHI_NAMES = watch._build_machi_names(_FILTERS)
    html = watch.build_html_report([], dict(_FILTERS), [], False)
    assert '"rentRoomsMin": %d' % RENT["rooms_min"] in html
    assert "!passRooms(d)" in html and "function roomsOf(" in html
    # _FILTER_JS の末尾は同期で初回 render() を呼ぶ（保存タブが賃貸なら passFilters→roomsOf が走る）。
    # ROOMS_RE は const（TDZ）なので _FILTER_JS より前に定義されていないと一覧ごと壊れる（2026-11-10 実測）。
    assert html.index("const ROOMS_RE") < html.index("function passFilters(")
