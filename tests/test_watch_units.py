# -*- coding: utf-8 -*-
"""キャンプ場土地バッチ5〜10(W2・消込表#472)で追加したアダプタの単体テスト。

実行方法: プロジェクトルート(.venv)で `python -m pytest tests/ -v`
（pytestはwatch.py本体の実行時依存(requests/beautifulsoup4/pyyaml)には含めない。
テスト実行時だけ `pip install pytest` すればよい。requirements.txtは変更しない。）

指示書 docs/tasks/20260905_camp_expand_w2.md の要求どおり、最低限
「ha→㎡・円→万円・スマイミー売買価格」の3ケースに加え、実装中の自己テストで
見つけて同一コミットで直した差異（<sup>タグのテキスト分断・全角括弧のNFKC折り畳み・
tier_hintのmachi優先度・KSIのエスケープ済みJSON抽出）の再発防止ケースも含む。
"""
import unicodedata
from pathlib import Path

import pytest
import watch
import yaml
from bs4 import BeautifulSoup


@pytest.fixture(autouse=True, scope="session")
def _configure_machi_names():
    """watch._MACHI_NAMES は本来 run()/rebuild() が urls.yaml から組み立てる
    （モジュールimport直後は_MACHI_BASE7の7市町だけ）。extract_machi()に依存する
    テスト（忍野村・山中湖村・富士宮市等）が本番と同じ判定になるよう、実際の
    urls.yaml を読んでセッション開始時に一度だけ組み立てる。"""
    config_path = Path(__file__).resolve().parent.parent / "urls.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    watch._MACHI_NAMES = watch._build_machi_names(config["filters"])
    yield


BASE_FILTERS = {
    "price_max_man": 3000,
    "area_min_sqm": 1000,
    "price_ceiling_by_type": {"更地": 3000, "古家付き土地": 3000, "中古戸建": 3000, "空き家": 3000},
    "exclude_areas": [],
    "caution_keywords": [],
    "interest_keywords": [],
}


# ---------------------------------------------------------------------------
# 1. ha → ㎡（フォレステ・山いちば系で共用の _yamaichiba_sqm）
# ---------------------------------------------------------------------------

def test_ha_to_sqm_basic():
    assert watch._yamaichiba_sqm("0.74ha (2,239坪)") == 7400.0


def test_ha_to_sqm_large_value_with_comma():
    assert watch._yamaichiba_sqm("公簿面積：6.42ha (19,430坪)") == 64200.0


def test_ha_to_sqm_falls_back_to_sqm_when_no_ha():
    # ha表記が無ければ㎡/坪へフォールバックする（_first_sqm経由）。
    assert watch._yamaichiba_sqm("233.0㎡") == 233.0


# ---------------------------------------------------------------------------
# 2. 円 → 万円（フォレステ _foreste_price_man）
#    実装中の自己テストで「価格：」(コロンあり)固定にした初版が「価格 」(コロンなし)
#    表記のカードで価格未抽出(None)になる差異を発見し、コロンを任意化して修正した。
#    再発防止のため両表記を回帰テストとして残す。
# ---------------------------------------------------------------------------

def test_foreste_yen_to_man_with_colon_and_fullwidth_spaces():
    assert watch._foreste_price_man("価　　格： 550,000 円\n公簿面積：1.32ha(4,019坪)") == 55


def test_foreste_yen_to_man_without_colon():
    # 2026-09-05: コロン無し表記(価格 900,000 円)で0件→90万円に修正した回帰ケース。
    assert watch._foreste_price_man("価格 900,000 円\n公簿面積：6.42ha (19,430坪)") == 90


def test_foreste_yen_to_man_rounds_to_nearest_man():
    assert watch._foreste_price_man("価格：537,500円") == 54  # 53.75→54(四捨五入)


def test_foreste_yen_to_man_missing_price_returns_none():
    assert watch._foreste_price_man("価格：応相談") is None


# ---------------------------------------------------------------------------
# 3. スマイミー売買価格（parse_sumaimy_land。賃貸のparse_rent_manではなく
#    parse_price_man(総額・整数)を使うことをカードHTML経由で確認する）
# ---------------------------------------------------------------------------

_SUMAIMY_LAND_CARD_HTML = """
<li class="item-block">
  <div class="item-block_header pc">
    <div class="item-block_header_title">
      <span class="cat">売土地</span>
      <p class="title"><a href="/%E7%89%A9%E4%BB%B6/176170" target="_blank">御殿場市萩原1287-3</a></p>
    </div>
    <div class="item-block_header_info">
      <p class="price">
        <span class="num01">1,927</span><span class="num02">万円</span>
        <span class="num03">&nbsp;</span>
      </p>
      <p class="area">
        <br>
        <span class="num03">（実測）土地面積243.19m&sup2;</span>
      </p>
    </div>
  </div>
</li>
"""


def test_sumaimy_land_extracts_sale_price_as_total_man_not_rent():
    soup = BeautifulSoup(_SUMAIMY_LAND_CARD_HTML, "html.parser")
    out = watch._extract_sumaimy_land_cards(soup, "https://www.shizuoka.fudohsan.jp/", [], BASE_FILTERS)
    assert len(out) == 1
    rec = out[0]
    # 総額1,927万円がint(1927)で入る（parse_rent_manなら"1927.0"のfloatや
    # 円→万円の再換算が誤って走る恐れがある。売買はparse_price_manで直接万円扱い）。
    assert rec["price_man"] == 1927
    assert isinstance(rec["price_man"], int)
    assert rec["area_sqm"] == 243.2
    assert rec["location"] == "御殿場市萩原1287-3"
    assert rec["url"].endswith("/%E7%89%A9%E4%BB%B6/176170")


def test_sumaimy_land_has_no_rent_only_fields():
    soup = BeautifulSoup(_SUMAIMY_LAND_CARD_HTML, "html.parser")
    out = watch._extract_sumaimy_land_cards(soup, "https://www.shizuoka.fudohsan.jp/", [], BASE_FILTERS)
    rec = out[0]
    # 売買カードには敷金/礼金/間取りが無い（賃貸専用フィールド。Noneのままでよい）。
    assert rec["shikikin"] is None
    assert rec["reikin"] is None
    assert rec["madori"] is None


# ---------------------------------------------------------------------------
# 4. <sup>タグを含む面積セルのテキスト結合（e-z / しずなび / 東急リゾートで共通の
#    根本原因）。get_text(" ", strip=True)だと"m<sup>2</sup>"が"m 2"に分断されて
#    _first_sqmの"m2"パターンに一致しなくなる回帰を防ぐ。
# ---------------------------------------------------------------------------

def test_numeric_cell_text_helper_avoids_sup_tag_split():
    # e-z/しずなび/東急リゾートの3アダプタで見つけた同一原因の差異を機械化した
    # 共通部品(_numeric_cell_text)の直接テスト。今後の新規アダプタもこれを使えば
    # 同じ差異を再発させない。
    html = '<dd>495m<sup>2</sup> （149.74坪）</dd>'
    dd = BeautifulSoup(html, "html.parser").find("dd")
    assert watch._first_sqm(watch._numeric_cell_text(dd)) == 495.0
    # 修正前の抽出方法（" "セパレータ）は一致しない＝この回帰を防ぐためのテスト
    assert dd.get_text(" ", strip=True) == "495m 2 （149.74坪）"


def test_numeric_cell_text_helper_handles_none():
    assert watch._numeric_cell_text(None) == ""


def test_ez_area_extraction_with_sup_tag():
    html = """
    <div class="articleList">
      <h3><a href="../sale/x/x-1.html">テスト物件</a></h3>
      <ul class="ulprice"><li class="listate">土地</li><li class="liprice">80<span>万円</span></li></ul>
      <dl class="item h6item">
        <dt>交通：</dt><dd>熱海</dd>
        <dt>土地：</dt><dd>495m<sup>2</sup> （149.74坪）</dd>
        <dt>所在：</dt><dd>伊豆の国市奈古谷字中峠</dd>
        <dt class="nobdr">温泉：</dt><dd class="nobdr">不可</dd>
      </dl>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    out = watch._ez_cards(soup, "https://www.e-z.co.jp/search/list-spec.php", [], BASE_FILTERS)
    assert len(out) == 1
    assert out[0]["area_sqm"] == 495.0
    assert out[0]["price_man"] == 80


# ---------------------------------------------------------------------------
# 5. 朝霧高原(asagiri): 全角括弧のNFKC半角化・「その他の地域」の所在地上書き・
#    価格に小数が出るケースの3差異（実装中に発見・同一コミットで修正）の回帰テスト。
# ---------------------------------------------------------------------------

def test_asagiri_district_location_uses_full_precision():
    full_text = unicodedata.normalize(
        "NFKC",
        "朝霧地区 （富士宮市猪之頭）　～説明文～ "
        "物件種別 外観 面積（坪） 価格（万円） 備考 "
        "売　土　地 土地　８７８．１５ □ １０００ ・建築可",
    )
    out = watch._asagiri_cards(full_text, "http://www3.tokai.or.jp/tosei/betusou.html", [], BASE_FILTERS)
    assert len(out) == 1
    # 地区名だけでなく所在地(猪之頭)まで含めて解決できること（全角括弧がNFKCで
    # 半角化されるため、半角括弧で検索する実装になっている）。
    assert out[0]["location"] == "静岡県富士宮市猪之頭"
    assert out[0]["price_man"] == 1000
    assert out[0]["area_sqm"] == round(878.15 * watch.TSUBO_TO_SQM, 1)


def test_asagiri_other_area_section_overrides_location_from_remark():
    full_text = unicodedata.normalize(
        "NFKC",
        "その他の地域 ～上記四地区以外（管理外）の別荘情報です。 "
        "物件種別 外観 面積（坪） 価格（万円） 備考 "
        "売　別　荘 土地　　４６．０２ 建物　　１７．０３ １５０ "
        "富士宮市猪之頭（国立音大別荘地） ・木造２階建３DK（昭和４９年１１月築） ・再建築可",
    )
    out = watch._asagiri_cards(full_text, "http://www3.tokai.or.jp/tosei/betusou.html", [], BASE_FILTERS)
    assert len(out) == 1
    rec = out[0]
    # 地区ヘッダに所在地が無い「その他の地域」は、価格直後・備考より前に出る
    # 所在地テキストを個別に拾えること（remarkだけを見ると検出できなかった差異）。
    assert rec["location"] == "静岡県富士宮市猪之頭(国立音大別荘地)"
    assert rec["price_man"] == 150
    assert rec["shubetsu"] == "中古戸建"  # 種別"別荘"→中古戸建


def test_asagiri_handles_decimal_price():
    full_text = unicodedata.normalize(
        "NFKC",
        "白糸地区 （富士宮市内野）　～説明文～ "
        "物件種別 外観 面積（坪） 価格（万円） 備考 "
        "売　土　地 土地　１３４．３１ ９９．８ ・建築可、建２０・容４０（８０）",
    )
    # 価格が"99.8"のような小数でもintキャストで例外にならず、四捨五入で
    # 万円整数になること（実装中にValueErrorで落ちた差異の回帰テスト）。
    out = watch._asagiri_cards(full_text, "http://www3.tokai.or.jp/tosei/betusou.html", [], BASE_FILTERS)
    assert len(out) == 1
    assert out[0]["price_man"] == 100  # 99.8→100(四捨五入)


# ---------------------------------------------------------------------------
# 6. 東急リゾート(tokyu_resort): tier_hint(エリア略称)をlocationへ無条件連結すると
#    物件名自体が正確な市町名を含む場合でも誤ったtier_hintにmachiが引っ張られる
#    差異（実測で「忍野村内野」がエリア列="山中湖"に分類される例を確認）の回帰テスト。
# ---------------------------------------------------------------------------

_TOKYU_CARD_HTML = """
<div class="tb-list">
  <a href="/yamanakako_kawaguchiko/detail/43409">
    <table>
      <tr class="tr-header">
        <td>&nbsp;</td>
        <th colspan="9"><p class="link-detail02">物件詳細・写真をもっと見る</p>忍野村内野</th>
      </tr>
      <tr><td colspan="10" class="catch">忍野村にある土地。</td></tr>
      <tr>
        <td colspan="2" rowspan="3"></td>
        <td>山中湖</td>
        <td class="price01">3,400<span class="price02">万円</span></td>
        <td>9,160.00m<sup>2</sup></td>
        <td>-</td>
      </tr>
    </table>
  </a>
</div>
"""


def test_tokyu_resort_prefers_precise_property_name_over_area_bucket():
    soup = BeautifulSoup(_TOKYU_CARD_HTML, "html.parser")
    out = watch._tokyu_resort_cards(soup, "https://www.tokyu-resort.co.jp/", [], BASE_FILTERS)
    assert len(out) == 1
    rec = out[0]
    # エリア列は"山中湖"(粗い区分・実際は誤り)だが、物件名"忍野村内野"に正しい
    # 市町名が含まれるため、locationはtier_hintを混ぜずprop_nameのみを使い、
    # machiは正しく"忍野村"に解決されること。
    assert rec["location"] == "忍野村内野"
    assert rec["machi"] == "忍野村"
    assert rec["price_man"] == 3400
    assert rec["area_sqm"] == 9160.0


def test_tokyu_resort_area_sup_tag_extracted_without_space_split():
    soup = BeautifulSoup(_TOKYU_CARD_HTML, "html.parser")
    out = watch._tokyu_resort_cards(soup, "https://www.tokyu-resort.co.jp/", [], BASE_FILTERS)
    # 修正前は<sup>分断によりarea_sqmが常にNoneだった（坪の併記が無く
    # e-z/しずなびのようなフォールバック救済も効かないケース）。
    assert out[0]["area_sqm"] is not None


# ---------------------------------------------------------------------------
# 7. KSI官公庁オークション(kankocho_ksi): エスケープ済みJSON(\" )からのレコード
#    境界検出・円→万円換算の回帰テスト（実装中に境界正規表現がエスケープ有無を
#    取り違えて0件になっていた差異）。
# ---------------------------------------------------------------------------

def _ksi_escaped_snippet(id_: int, title: str, estimate_fee: int, location_text: str, land_space: float) -> str:
    """本物のNext.js RSCペイロードを模した \" エスケープ済みJSON風スニペット。"""
    return (
        f'{{\\"id\\":{id_},\\"auctionId\\":1,\\"openWay\\":\\"BID\\",\\"printOrder\\":null,'
        f'\\"title\\":\\"{title}\\",\\"estimateFee\\":{estimate_fee},\\"deposit\\":10000,'
        f'\\"divisionName\\":\\"テスト県税事務所\\",\\"status\\":\\"DURING_BIDDING\\",'
        f'\\"category\\":\\"REAL_ESTATE\\",\\"price\\":null,'
        f'\\"estateProperty\\":{{\\"propertyId\\":{id_},'
        f'\\"locationText\\":\\"{location_text}\\",\\"landSpace\\":{land_space}}}}}'
    )


def test_kankocho_ksi_extracts_price_and_area_from_escaped_json():
    # 実物のRSCペイロードは日本語もエスケープせずUTF-8のまま埋め込まれる
    # （2026-09-05実測で確認済み。\uXXXX形式ではない）ため、そのままリテラルで書く。
    html = "<script>self.__next_f.push([1,\"2:[" + _ksi_escaped_snippet(
        70962, "【再値下げ】山梨県鳴沢村", 110000, "南都留郡鳴沢村10872番1", 233,
    ) + "]\"])</script>"
    recs = watch._ksi_records(html, "https://kankocho.jp/search/real-estate/")
    assert len(recs) == 1
    url, title, price, area, location = recs[0]
    assert url == "https://kankocho.jp/items/70962/"
    assert price == 11  # 110,000円 → 11万円
    assert area == 233.0
    assert location == "南都留郡鳴沢村10872番1"


def test_kankocho_ksi_record_boundary_regex_matches_unescaped_text():
    # 2026-09-05: レコード境界の正規表現がエスケープ有り(\"id\":)のまま書かれており、
    # html.replace('\\"','"')後の平文テキストに対して0件になっていた回帰の防止。
    unescaped_sample = '{"id":1,"auctionId":2,"title":"x"}'
    assert len(list(watch._KSI_RECORD_RE.finditer(unescaped_sample))) == 1


# ---------------------------------------------------------------------------
# 8. robots.txt判定のRFC 9309準拠化（W3・消込表#472）。urllib.robotparserは
#    先頭一致・記述順で判定し`$`終端アンカー・`*`ワイルドカードを解釈しないため、
#    kankocho.jpの「Disallow: /search/」より後に書かれた明示許可
#    「Allow: /search/real-estate/$」を読み落として禁止側に誤って倒れていた
#    （2026-09-05実測。docs/BUGLOG.md）。RFC 9309（最長一致優先・同長ならAllow
#    優先・`$`は文字列終端・`*`は任意文字列0文字以上）準拠の自前実装への
#    置き換えの回帰テスト。
# ---------------------------------------------------------------------------

class _FakeRobotsResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class _FakeRobotsSession:
    """robots_allowed()用のダミーsession。requests.Sessionの
    .get(url, timeout=, headers=) と同じ引数だけを受ける最小スタブ
    （実ネットワークアクセスをしない）。"""

    def __init__(self, robots_text="", status_code=200, raise_exc=None):
        self._robots_text = robots_text
        self._status_code = status_code
        self._raise_exc = raise_exc

    def get(self, url, timeout=None, headers=None):
        if self._raise_exc:
            raise self._raise_exc
        return _FakeRobotsResp(self._status_code, self._robots_text)


def test_robots_allow_wins_only_at_exact_dollar_anchor():
    # kankocho.jp/robots.txt実物と同じ構成（W3背景）。
    robots_txt = "User-agent: *\nDisallow: /search/\nAllow: /search/real-estate/$\n"
    session = _FakeRobotsSession(robots_txt)
    assert watch.robots_allowed("https://kankocho.jp/search/real-estate/", session) is True
    # クエリが付くと$アンカーにマッチしなくなり、Disallow:/search/だけが一致→不許可
    assert watch.robots_allowed("https://kankocho.jp/search/real-estate/?page=1", session) is False
    # 別パスはAllowの対象外でDisallow:/search/のみ一致→不許可
    assert watch.robots_allowed("https://kankocho.jp/search/other", session) is False


def test_robots_wildcard_star_matches_any_middle_segment():
    robots_txt = "User-agent: *\nDisallow: /cat/*/private/\n"
    session = _FakeRobotsSession(robots_txt)
    assert watch.robots_allowed("https://example.com/cat/123/private/x", session) is False
    # "/private/"を含まないパスはこのDisallowに一致せず、他に一致規則も無いので許可
    assert watch.robots_allowed("https://example.com/cat/123/public/x", session) is True


def test_robots_group_selection_prefers_own_ua_over_wildcard():
    # 自分のUA文字列(watch.HEADERS)は"...Chrome/125.0.0.0 Safari/537.36"を含むため
    # "chrome"宛のグループが"*"より優先されること（urllib.robotparserと同じ
    # 部分一致でのグループ選択。ここは今回の修正で変えていない）。
    robots_txt = (
        "User-agent: chrome\nDisallow: /blocked/\n\n"
        "User-agent: *\nAllow: /\n"
    )
    session = _FakeRobotsSession(robots_txt)
    assert watch.robots_allowed("https://example.com/blocked/x", session) is False
    assert watch.robots_allowed("https://example.com/other", session) is True


def test_robots_empty_disallow_means_allow_all():
    robots_txt = "User-agent: *\nDisallow:\n"
    session = _FakeRobotsSession(robots_txt)
    assert watch.robots_allowed("https://example.com/anything/here", session) is True


def test_robots_fetch_failure_keeps_fail_open_behavior():
    # 従来(urllib.robotparser版)と同じく、非200・例外時は「許可」に倒す
    # （W3指示書: 取得失敗時の扱いは変えない）。
    session_404 = _FakeRobotsSession(status_code=404)
    assert watch.robots_allowed("https://example.com/x", session_404) is True
    session_error = _FakeRobotsSession(raise_exc=Exception("boom"))
    assert watch.robots_allowed("https://example.com/x", session_error) is True


def test_robots_percent_encoding_is_normalized_before_matching():
    # robots.txt側が%2D(ハイフンの%エンコード)で書かれていても、URL側の生の
    # "real-estate"と一致すること（パーセントエンコード差の正規化・追加ケース）。
    robots_txt = "User-agent: *\nDisallow: /search/\nAllow: /search/real%2Destate/$\n"
    session = _FakeRobotsSession(robots_txt)
    assert watch.robots_allowed("https://kankocho.jp/search/real-estate/", session) is True
