# -*- coding: utf-8 -*-
# @owns watch.classify_shubetsu（種別判定）の単体試験
"""消込表 #954 調査中に見つけた不具合の再発防止試験。

SUUMO 中古戸建カードの本文は「土地面積」を含み「戸建」を含まないため、adapter が
default_type="中古戸建"（URL が /chukoikkodate/）を渡していても、本文の「土地」だけで
「更地」に落ちていた（公開レポートで中古戸建378件中350件が更地）。
家系ヒント（中古戸建/空き家）の時は本文の「土地」では土地系に入らず、古家表記がある時だけ入る。
default_type が汎用（更地）の時の本文判定（地場業者の混在一覧）は従来どおり。
"""
import pytest
import watch

# 実物（2026-11-10 SUUMO 伊豆の国市 中古一戸建一覧のカード本文）。「戸建」を含まず「土地面積」を含む。
SUUMO_CHUKO_CARD = (
    "チェック 購入サポート情報 ☆420㎡強のゆとりの敷地に、広いお庭。家庭菜園や趣味の幅が広がる生活 "
    "お気に入りに追加する 資料請求する 物件名 伊豆の国市吉田中古住宅 販売価格 1750万円 所在地 静岡県伊豆の国市吉田 "
    "沿線・駅 伊豆箱根鉄道駿豆線「大仁」徒歩14分 土地面積 422.42m 2 （127.78坪）（登記） 間取り 7DK+S（納戸） "
    "建物面積 154.65m 2 （46.78坪）（登記） 築年月 1970年10月"
)
# 家の語が「土地面積」以外に無い最小形（中古戸建ヒントだけが頼りの本文）
HOUSE_HINT_ONLY = "販売価格 1750万円 所在地 静岡県沼津市 土地面積 200m2 間取り 4LDK 築年月 1990年"
LAND_LIST_CARD = "売土地 静岡県伊豆の国市 販売価格 800万円 土地面積 330m2 建築条件なし"
FURUYA_LAND_CARD = "売土地 静岡県伊豆の国市 古家付き 販売価格 800万円 土地面積 330m2"


def test_suumo_chuko_card_stays_chuko_kodate_with_house_hint():
    assert watch.classify_shubetsu(SUUMO_CHUKO_CARD, "中古戸建")[0] == "中古戸建"


def test_house_hint_is_not_overridden_by_tochi_menseki_alone():
    assert watch.classify_shubetsu(HOUSE_HINT_ONLY, "中古戸建")[0] == "中古戸建"


def test_land_list_body_is_sarachi_with_default_hint():
    assert watch.classify_shubetsu(LAND_LIST_CARD, "更地")[0] == "更地"


def test_land_body_without_hint_info_is_sarachi_for_generic_default():
    # 地場業者の混在一覧など default_type が汎用（更地）の時、本文に「土地」のみ・建物表記なしは更地のまま
    assert watch.classify_shubetsu("静岡県沼津市 土地 500万円", "更地")[0] == "更地"


def test_furuya_token_gives_furuya_tsuki_tochi_for_generic_default():
    assert watch.classify_shubetsu(FURUYA_LAND_CARD, "更地")[0] == "古家付き土地"


def test_furuya_token_still_wins_over_house_hint():
    # 家系ヒントでも、土地表記＋古家の明示証拠があれば古家付き土地（従来どおり）
    assert watch.classify_shubetsu(FURUYA_LAND_CARD, "中古戸建")[0] == "古家付き土地"


@pytest.mark.parametrize("hint", ["中古戸建", "更地", "空き家", "古家付き土地"])
def test_akiya_wording_wins_over_every_hint(hint):
    assert watch.classify_shubetsu("空き家バンク登録物件 土地面積 300m2", hint)[0] == "空き家"


def test_akiya_hint_without_wording_is_not_dropped_to_sarachi():
    # 空き家ヒント（takkenの空き家カテゴリ）で本文に「土地」だけがある場合も更地へ落とさない
    assert watch.classify_shubetsu("静岡県沼津市 土地面積 300m2", "空き家")[0] == "空き家"


def test_sarachi_hint_with_house_words_stays_sarachi():
    # 従来挙動の固定: 更地ヒントは本文の「住宅」等では中古戸建に化けない（地場業者の土地を守る）
    assert watch.classify_shubetsu("売土地 住宅地 土地面積 300m2", "更地")[0] == "更地"
