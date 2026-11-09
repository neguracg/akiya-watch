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
    ("ワンルーム", 1), ("1R", 1), ("１ＬＤＫ", 1), ("2LDK+3S", 2),
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
