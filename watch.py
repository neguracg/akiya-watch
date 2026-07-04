#!/usr/bin/env python3
"""akiya-watch: 空き家・売地の差分監視スクリプト

仕様は CLAUDE.md / urls.yaml に従う。URLは urls.yaml が唯一のマスタ。
フィルタ閾値・キーワードは urls.yaml の filters: ブロックを唯一の定義元とし、
このコードには直接書かない。
"""

import argparse
import hashlib
import json
import logging
import logging.handlers
import random
import re
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.robotparser
from datetime import date, datetime
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

# Windows コンソール(cp932)で em-dash 等を含むログが UnicodeEncodeError を起こすのを防止。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

# 安全装置の定数
FETCH_TOTAL_TIMEOUT = 25     # 1リクエストの総時間上限（秒）。requestsのtimeoutは細切れ送信で無限化するため
SITE_TIME_BUDGET = 180       # 1サイトあたりの最大処理時間（秒）。超えたらページャ追従を打ち切り
RUN_WALLCLOCK_LIMIT = 1800   # 実行全体の上限（秒・30分）。超えたら残サイトを打ち切って報告

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data" / "snapshots"
ARCHIVE_DIR = BASE_DIR / "data" / "archive"
REPORTS_DIR = BASE_DIR / "reports"
LOGS_DIR = BASE_DIR / "logs"

for d in (DATA_DIR, ARCHIVE_DIR, REPORTS_DIR, LOGS_DIR):
    d.mkdir(parents=True, exist_ok=True)

DISAPPEAR_WINDOW_DAYS = 7   # 消滅掲載の保持日数（8日目以降は非掲載）
REPORT_RETENTION_DAYS = 14  # 日付別htmlの保持日数（15日以上前は削除）

handler = logging.handlers.RotatingFileHandler(
    LOGS_DIR / "watch.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[handler, logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.9",
}

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "ref", "from", "yclid",
}

# 物件っぽさの判定に使う
PRICE_HINT_RE = re.compile(r"万円|億|㎡|m2|m²|平米|坪")
DETAIL_RE = re.compile(r"/(detail|bukken|tochi|property|land|chukos?|estate)/", re.I)

TSUBO_TO_SQM = 3.30578

# ---- 価格正規化 ----
# 億・万を拾って万円整数に。応談/未定など取れなければ None。
_OKU_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*億")
_MAN_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*万")


def parse_price_man(text: str):
    """「980万円」「1,280万円」「1億2000万円」等を万円整数に。取れなければ None。"""
    total = 0.0
    found = False
    m_oku = _OKU_RE.search(text)
    if m_oku:
        total += float(m_oku.group(1).replace(",", "")) * 10000
        found = True
    m_man = _MAN_RE.search(text)
    if m_man:
        total += float(m_man.group(1).replace(",", ""))
        found = True
    if not found or total <= 0:
        return None
    return int(round(total))


# ---- 面積正規化 ----
_AREA_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(㎡|m²|m2|平米|平方メートル|坪)")


def _to_sqm(val: str, unit: str) -> float:
    v = float(val.replace(",", ""))
    if unit == "坪":
        return v * TSUBO_TO_SQM
    return v


def parse_area_sqm(text: str):
    """面積を㎡に正規化。

    戻り値: (area_sqm or None, estimated: bool)
    土地面積と建物面積が併記なら「土地」ラベル側を優先。
    判別不能で複数値あれば最大値＋推定フラグ。取れなければ (None, False)。
    """
    # 土地ラベル優先
    for label in ("土地面積", "土地"):
        idx = text.find(label)
        if idx != -1:
            window = text[idx: idx + 40]
            m = _AREA_RE.search(window)
            if m:
                return round(_to_sqm(m.group(1), m.group(2)), 1), False
    matches = _AREA_RE.findall(text)
    vals = [_to_sqm(v, u) for v, u in matches]
    vals = [v for v in vals if v > 0]
    if not vals:
        return None, False
    if len(vals) == 1:
        return round(vals[0], 1), False
    # 判別不能・複数 → 最大値＋推定フラグ
    return round(max(vals), 1), True


def normalize_url(href: str, base: str) -> str:
    abs_url = urllib.parse.urljoin(base, href)
    parsed = urllib.parse.urlparse(abs_url)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    filtered = {k: v for k, v in qs.items() if k not in TRACKING_PARAMS}
    new_query = urllib.parse.urlencode(filtered, doseq=True)
    return urllib.parse.urlunparse(parsed._replace(query=new_query, fragment=""))


def robots_allowed(url: str, session: requests.Session) -> bool:
    parsed = urllib.parse.urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        resp = session.get(robots_url, timeout=10, headers=HEADERS)
        if resp.status_code >= 400:
            return True
        rp.parse(resp.text.splitlines())
    except Exception:
        return True
    return rp.can_fetch(HEADERS["User-Agent"], url)


_SITE_DEADLINE = [0.0]  # run() が各サイト処理前に time.time()+SITE_TIME_BUDGET を設定


def _site_time_left() -> bool:
    """このサイトの処理時間予算が残っているか。"""
    return _SITE_DEADLINE[0] == 0.0 or time.time() < _SITE_DEADLINE[0]


def fetch(url: str, session: requests.Session) -> tuple[int, str]:
    """HTTP GET。総経過時間を FETCH_TOTAL_TIMEOUT 秒で必ず打ち切る（使い捨てスレッド＋join方式）。

    requests の timeout=(接続,読取) は「1回のソケット読み取り」単位にしか効かず、データを
    細切れに送り続ける相手(slow-drip)だと1リクエストの総時間が無限に延びる（過去に54分〜
    4時間ハング）。そこで取得を使い捨てデーモンスレッドで行い、メインは join(timeout) で必ず
    制限時間内に戻る。締切超過時はそのスレッドを放置（daemon＝プロセス終了時に消える）し、
    本流は status 0 で先へ進む。fetch ごとに新スレッドなので、プール枯渇による再ハングは無い。
    （注: stream＋iter_content 方式は http.client の read(amt) が amt バイト揃うまでブロック
      するため slow-drip でメインが固まり不可。本方式で回避。）
    """
    box = {}

    def _do():
        try:
            r = session.get(url, headers=HEADERS, timeout=(10, 15))  # 接続10s / 各読取15s
            # charset 未指定で requests が ISO-8859-1 を既定にした場合、UTF-8等へ補正
            # （空き家バンクしずおか等は実体UTF-8だが ISO-8859-1 と誤申告）。
            enc = (r.encoding or "").lower()
            if enc == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"
            elif enc in ("shift_jis", "shift-jis", "sjis", "x-sjis"):
                # ㎡・①・髙 等は CP932(Windows拡張)。strict shift_jis だと化けるため
                # 上位互換の cp932 で復号する（家っち snjhkk 等）。
                r.encoding = "cp932"
            box["v"] = (r.status_code, r.text)
        except Exception as e:
            box["v"] = (0, str(e))

    th = threading.Thread(target=_do, daemon=True)
    th.start()
    th.join(FETCH_TOTAL_TIMEOUT)
    if th.is_alive():
        log.warning(f"fetch 総時間切れ {FETCH_TOTAL_TIMEOUT}s で打ち切り（放置スレッドは無害）: {url}")
        return 0, "total-timeout"
    return box.get("v", (0, "no-result"))


def block_text_for(a) -> str:
    """物件カード相当の親ブロックのテキスト（最大~400字）を返す。"""
    block = a.get_text(" ", strip=True)
    node = a
    for _ in range(6):
        parent = node.parent
        if parent is None:
            break
        block = parent.get_text(" ", strip=True)
        node = parent
        if len(block) > 400:
            break
    return block


def extract_properties(html: str, base_url: str, filter_keywords: list, filters: dict) -> list:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith("#") or href.startswith("javascript"):
            continue
        text = a.get_text(" ", strip=True)
        norm = normalize_url(href, base_url)

        block = block_text_for(a)
        is_property = bool(PRICE_HINT_RE.search(text) or DETAIL_RE.search(norm) or PRICE_HINT_RE.search(block))
        if not is_property:
            continue

        if filter_keywords:
            combined = text + " " + norm + " " + block
            if not any(kw in combined for kw in filter_keywords):
                continue

        # 価格・面積: <a> テキスト優先、なければ親ブロック
        price = parse_price_man(text)
        if price is None:
            price = parse_price_man(block)
        area, area_est = parse_area_sqm(text)
        if area is None:
            area, area_est = parse_area_sqm(block)

        results.append(_make_record(norm, text, price, area, area_est, block, filters))

    # dedup by key
    seen = set()
    deduped = []
    for r in results:
        if r["key"] not in seen:
            seen.add(r["key"])
            deduped.append(r)
    return deduped


# ---------------------------------------------------------------------------
# サイト別アダプタ（site adapter 方式）
#   site_id → 専用パーサ のレジストリ。登録が無いサイトは extract_properties に
#   フォールバックする。今回は suumo 系のみ実装。
# ---------------------------------------------------------------------------

def _in_range(price, area, filters) -> bool:
    """価格・面積が両方取得でき、かつ閾値内か。"""
    return (price is not None and area is not None
            and price <= filters["price_max_man"]
            and area >= filters["area_min_sqm"])


def tsubo_unit_man(price_man, area_sqm):
    """坪単価（万円/坪）= 価格 ÷ (面積㎡ ÷ 3.30578)。小数1桁。取れなければ None。"""
    if price_man is None or area_sqm is None or area_sqm <= 0:
        return None
    return round(price_man / (area_sqm / TSUBO_TO_SQM), 1)


_CHIMOKU_TOKENS = ("宅地", "畑", "田", "山林", "雑種地", "原野", "牧場", "保安林")
_CHIMOKU_LABEL_RE = re.compile(r"地目[\s:：]*([^\s/／、,，]{1,6})")


def extract_chimoku(text: str) -> str:
    """地目（宅地/畑/山林/雑種地 等）。ラベル「地目」優先、無ければ単独語。無ければ —。"""
    m = _CHIMOKU_LABEL_RE.search(text)
    if m:
        val = m.group(1)
        for t in _CHIMOKU_TOKENS:
            if t in val:
                return t
        return val
    for t in _CHIMOKU_TOKENS:
        if t in text:
            return t
    return "—"


def extract_toshikeikaku(text: str) -> str:
    """都市計画区分。市街化調整区域 / 市街化区域 を判定。無ければ —。"""
    if "市街化調整区域" in text or "調整区域" in text:
        return "市街化調整区域"
    if "市街化区域" in text:
        return "市街化区域"
    return "—"


# 前半7つ=住宅探索(home)の対象市町。以降=キャンプ場土地(camp)タブの1h圏 Tier1+Tier2、
# 末尾3つ=Tier3（1h超だが広い山物件の在庫が豊富な伊豆最南部。現地訪問前提で監視だけ広げる）。
_MACHI_NAMES = ("函南町", "伊豆の国市", "三島市", "沼津市", "清水町", "長泉町", "裾野市",
                "伊豆市", "熱海市", "御殿場市", "小山町", "伊東市", "西伊豆町",
                "湯河原町", "箱根町", "富士市",
                "下田市", "東伊豆町", "南伊豆町")


def extract_machi(text: str) -> str:
    """所在地テキストから対象6市町を判定。無ければ空文字。"""
    for m in _MACHI_NAMES:
        if m in text:
            return m
    return ""


def extract_setsudo(text: str):
    """接道に関する生テキスト断片を返す（幅員 or 接道 周辺）。無ければ None。"""
    m = _ROAD_WIDTH_RE.search(text)
    if m:
        return f"幅員{m.group(1)}m"
    i = text.find("接道")
    if i != -1:
        return text[i:i + 14].strip()
    return None


_ROAD_WIDTH_RE = re.compile(r"(?:幅員|前面道路)[^0-9]{0,8}([\d]+(?:\.\d+)?)\s*m", re.I)
_FRONTAGE_RE = re.compile(r"間口[^0-9]{0,6}([\d]+(?:\.\d+)?)\s*m", re.I)


def _road_width(text):
    m = _ROAD_WIDTH_RE.search(text)
    return float(m.group(1)) if m else None


def _frontage(text):
    m = _FRONTAGE_RE.search(text)
    return float(m.group(1)) if m else None


_ZOKUJIN_TOKENS = ("農家住宅", "分家住宅", "農家", "分家")
_FURUYA_TOKENS = ("古家", "古屋", "古家付", "上物あり", "現況古家", "建物あり", "要解体")


def detect_zokujinsei(text: str) -> bool:
    """属人性（農家住宅/分家住宅 等）の疑いを検知。"""
    return any(t in text for t in _ZOKUJIN_TOKENS)


def classify_shubetsu(text: str, default_type: str):
    """物件種別を 更地/古家付き土地/中古戸建/空き家 に分類。戻り値 (種別, 判定根拠)。

    default_type は adapter が URL/カテゴリから渡すヒント。本文で上書き判定する。
    """
    if "空き家" in text or "空家" in text:
        return "空き家", "掲載に空き家表記"
    # 土地系（更地/古家付き）
    if default_type in ("更地", "古家付き土地") or ("土地" in text and "戸建" not in text):
        if any(t in text for t in _FURUYA_TOKENS):
            return "古家付き土地", "土地＋古家/上物の表記"
        return "更地", "土地カテゴリ（建物表記なし）"
    # 戸建系
    if default_type == "中古戸建" or any(t in text for t in ("中古", "戸建", "一戸建", "住宅")):
        return "中古戸建", "中古戸建/住宅カテゴリ"
    return default_type, "既定（URL種別）"


_HOUSE_SHUBETSU = ("空き家", "古家付き土地", "中古戸建")


def building_assessment(text: str, toshikeikaku: str, zokujin: bool, shubetsu: str):
    """建築可否ヒューリスティック（参考値）。戻り値 (mark, reason)。理由は必須。

    種別で観点を分ける:
      家付き系（空き家/古家付き土地/中古戸建）= 既存建物の **再建築** 可否
      更地系（更地/農地 等）                 = **新規建築** 可否
    いずれも法的確定ではなく掲載情報からの推定。最終判断は役場確認が前提。
    属人性の疑いがあれば注意喚起を理由に付す（除外はしない）。
    """
    is_house = shubetsu in _HOUSE_SHUBETSU
    kind = "再建築" if is_house else "新規建築"
    zk = "／属人的許可の疑い→用途変更許可が必要・第三者建替え不可の恐れ" if zokujin else ""

    # --- 強い否定シグナル（種別共通）---
    if "再建築不可" in text or "建築不可" in text:
        return "×", f"掲載に再建築不可の表記（{kind}不可）" + zk
    if "接道なし" in text or "未接道" in text or "無道路" in text:
        return "×", f"接道なしの疑い（{kind}不可の恐れ）" + zk

    width = _road_width(text)
    frontage = _frontage(text)
    in_market = (toshikeikaku == "市街化区域") or ("市街化区域" in text)
    is_chousei = (toshikeikaku == "市街化調整区域") or ("調整区域" in text)
    road_ok = width is not None and width >= 4 and (frontage is None or frontage >= 2)

    if is_house:
        # --- 既存建物の再建築可否 ---
        if "既存不適格" in text:
            return "△", "既存不適格＝現行法では同規模再建築できない恐れ・要役場確認" + zk
        if is_chousei:
            return "△", "調整区域の建替えは既存宅地要件等の確認が必要＝本命候補" + zk
        if road_ok and in_market:
            return "○", f"前面道路幅員{width:g}m・市街化区域で建基法道路に接道、再建築可と推定" + zk
        if width is not None and width < 4:
            return "△", f"前面道路幅員{width:g}m(<4m)。再建築はセットバック前提・要確認" + zk
        if zokujin:
            return "△", "属人的許可（農家/分家）の疑い→第三者の再建築不可の恐れ"
        return "不明", "接道・区分情報なし。建物ありだが現行法での再建築可否は役場で要確認"

    # --- 更地: 新規建築可否 ---
    if is_chousei:
        return "△", "市街化調整区域は原則新築不可・許可要件次第＝本命候補" + zk
    if road_ok and in_market:
        return "○", f"前面道路幅員{width:g}m・市街化区域で建基法道路に接道、新築可と推定" + zk
    if width is not None and width < 4:
        return "△", f"前面道路幅員{width:g}m(<4m)。セットバックで新築可の可能性・要確認" + zk
    if in_market:
        return "△", "市街化区域だが接道幅員が不明。新築可否は接道要確認" + zk
    if zokujin:
        return "△", "属人的許可の疑い→用途変更許可が必要" + zk
    return "不明", "接道・区分情報なし、詳細/役場で要確認"


def ceiling_for(shubetsu: str, filters: dict) -> int:
    """種別別の価格上限（万円）。filters.price_ceiling_by_type 優先、無ければ price_max_man。"""
    by_type = filters.get("price_ceiling_by_type") or {}
    return by_type.get(shubetsu, filters.get("price_max_man", 1000))


def _make_record(url, text, price, area, area_est, flag_text, filters,
                 location="", default_type="更地") -> dict:
    """共通テーブルへの正規化レコードを作る。flag_text はフラグ・属性判定に使う範囲のテキスト。

    サーバ側ではハード除外しない（C方針）。判定は数値のみ:
      数値不明 = 価格・面積のどちらかが null
      適合     = 面積≥下限 かつ 価格≤種別別上限（price_ceiling_by_type）
      不適合   = 上記以外
    NGエリア・キーワード・再建築不可は除外せず「フラグ」として保持し、絞り込みは
    クライアント側トグルで行う。
    """
    interest = [kw for kw in filters.get("interest_keywords", []) if kw in flag_text]
    caution = [kw for kw in filters.get("caution_keywords", []) if kw in flag_text]
    ng_hay = (location or "") + " " + flag_text
    ng_areas = [a for a in filters.get("exclude_areas", []) if a in ng_hay]

    shubetsu, shubetsu_reason = classify_shubetsu(flag_text, default_type)
    ceiling = ceiling_for(shubetsu, filters)
    area_min = filters.get("area_min_sqm", 330)

    if price is None or area is None:
        verdict = "数値不明"
    elif price <= ceiling and area >= area_min:
        verdict = "適合"
    else:
        verdict = "不適合"

    zokujin = detect_zokujinsei(flag_text)
    toshikeikaku = extract_toshikeikaku(flag_text)
    chimoku = extract_chimoku(flag_text)
    rb_mark, rb_reason = building_assessment(flag_text, toshikeikaku, zokujin, shubetsu)

    return {
        "url": url,
        "text": text[:120],
        "key": url + "|" + text[:60],
        "price_man": price,
        "area_sqm": area,
        "area_estimated": area_est,
        "tsubo_man": tsubo_unit_man(price, area),
        "shubetsu": shubetsu,
        "shubetsu_reason": shubetsu_reason,
        "ceiling_man": ceiling,
        "chimoku": chimoku,
        "toshikeikaku": toshikeikaku,
        "setsudo": extract_setsudo(flag_text),
        "rebuild_mark": rb_mark,
        "rebuild_reason": rb_reason,
        "zokujinsei": zokujin,
        "verdict": verdict,
        "interest": interest,
        "caution": caution,
        "ng_areas": ng_areas,
        "location": location,
        "machi": extract_machi(location or flag_text),
        "first_seen": None,
        "last_seen": None,
    }


_SQM_ONLY_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:㎡|m²|m2)")
_TSUBO_ONLY_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*坪")


def _suumo_land_sqm(dd_text: str):
    """SUUMO 土地面積セル（例「224m2（67.75坪）（登記）」）から㎡値を取る。

    ㎡表記を最優先（坪は同一面積の併記なので推定にしない）。取れなければ None。
    """
    m = _SQM_ONLY_RE.search(dd_text)
    if m:
        return round(float(m.group(1).replace(",", "")), 1)
    m = _TSUBO_ONLY_RE.search(dd_text)
    if m:
        return round(float(m.group(1).replace(",", "")) * TSUBO_TO_SQM, 1)
    return None


SUUMO_MAX_PAGES = 20  # 1サイトあたりのページ追従上限


def _extract_suumo_cards(soup, base_url: str, filter_keywords: list, filters: dict) -> list:
    """1ページ分の `div.property_unit` をカード単位で抽出する（dedup なし）。

    カード内の dt/dd（dottable）から価格・土地面積・所在地を取り、詳細URLは
    `h2.property_unit-title > a`（nc_ で始まる物件詳細ページ）から取る。ナビ・
    ヘッダ・フッタ・ページャはカード外なので構造的に除外される。
    """
    out = []
    for card in soup.select("div.property_unit"):
        title_a = card.select_one("h2.property_unit-title a[href]")
        if not title_a:
            continue
        url = normalize_url(title_a["href"], base_url)
        text = title_a.get_text(" ", strip=True)
        card_text = card.get_text(" ", strip=True)

        # カード内の dt→dd マップ（カード境界の内側のみ）
        fields = {}
        for dl in card.select("div.dottable-line dl"):
            dt = dl.find("dt")
            dd = dl.find("dd")
            if dt and dd:
                k = dt.get_text(strip=True)
                v = dd.get_text(strip=True)  # 区切りなし＝「224m2」を分離させない
                fields.setdefault(k, v)

        # 県・郡ページの市町絞り込み（suumo_suntogun / suumo_tagatagun）
        if filter_keywords:
            hay = text + " " + fields.get("所在地", "") + " " + card_text
            if not any(kw in hay for kw in filter_keywords):
                continue

        # 価格: 「価格」を含み「単価」を含まないラベル（販売価格 等。坪単価は除外）
        price = None
        for k, v in fields.items():
            if "価格" in k and "単価" not in k:
                price = parse_price_man(v)
                if price is not None:
                    break

        # 面積: 土地面積を最優先。SUUMOは「224m2（67.75坪）」と同一面積を2単位で
        # 併記するため、土地面積セルは㎡値を直接採用し推定フラグは立てない。
        area, area_est = (None, False)
        if "土地面積" in fields:
            area = _suumo_land_sqm(fields["土地面積"])
        if area is None:
            for k, v in fields.items():
                if "面積" in k:
                    area, area_est = parse_area_sqm(v)
                    if area is not None:
                        break

        # 種別ヒント: /chukoikkodate/ は中古戸建、/tochi/ は土地（更地/古家付き）
        dtype = "中古戸建" if "/chukoikkodate/" in base_url else "更地"
        rec = _make_record(url, text, price, area, area_est, card_text, filters,
                           location=fields.get("所在地", ""), default_type=dtype)
        out.append(rec)
    return out


def _suumo_next_url(soup, base_url: str):
    """ページャの「次へ」リンク（?page=N）を絶対URLで返す。無ければ None。"""
    for a in soup.select("div.pagination_set-nav a[href]"):
        if a.get_text(strip=True) == "次へ":
            return urllib.parse.urljoin(base_url, a["href"])
    return None


class BotBlocked(Exception):
    """サイトの bot 対策ページ（インタースティシャル）を検出したときに送出。"""


def _suumo_looks_blocked(html: str, soup) -> bool:
    """SUUMO の bot対策ページ（カードも結果件数表示も無い極小ページ）か判定。

    正常な一覧は物件0件でも数万バイト＋検索フォーム＋件数表示を持つ。bot対策の
    インタースティシャルは数KBで property_unit も pagination も無い。
    """
    if soup.select_one("div.property_unit"):
        return False
    if len(html) >= 12000:
        return False  # 大きいページは正常（真の0件 or 構造変化）として扱う
    has_pager = bool(soup.select_one("div.pagination_set-nav"))
    has_hit = "件" in soup.get_text()
    return not (has_pager or has_hit)


def parse_suumo(first_html: str, base_url: str, filter_keywords: list,
                filters: dict, session: requests.Session) -> list:
    """SUUMO 土地一覧アダプタ（ページャ追従＋bot対策リトライつき）。

    1ページ目は呼び出し側が取得済みの first_html を使い、以降は「次へ」リンクを
    最大 SUUMO_MAX_PAGES ページまで辿る。ページ取得間に 2〜5 秒スリープを入れる。
    1ページ目が bot対策ページのときは間隔を空けて最大2回リトライ。なお解消しなければ
    BotBlocked を送出（呼び出し側で前回スナップショットを保持し「要確認」扱いにする）。
    """
    # --- 1ページ目の bot対策検出＋リトライ（バックオフ）---
    html = first_html
    soup = BeautifulSoup(html, "html.parser")
    for attempt in range(2):
        if not _suumo_looks_blocked(html, soup):
            break
        wait = 8 + attempt * 8
        log.warning(f"[suumo] bot対策ページ検出（{len(html)}B）。{wait}秒待って再取得 {attempt + 1}/2: {base_url}")
        time.sleep(wait)
        code, html = fetch(base_url, session)
        soup = BeautifulSoup(html, "html.parser")
    if _suumo_looks_blocked(html, soup):
        raise BotBlocked(f"SUUMO bot対策ページが継続: {base_url}")

    all_props = []
    page_url = base_url
    page = 1
    seen_urls = {base_url}
    seen_hashes = {page_hash(html)}
    while True:
        soup = BeautifulSoup(html, "html.parser")
        all_props.extend(_extract_suumo_cards(soup, page_url, filter_keywords, filters))
        nxt = _suumo_next_url(soup, page_url)
        if not nxt or page >= SUUMO_MAX_PAGES or not _site_time_left():
            if not _site_time_left():
                log.warning(f"[suumo] サイト時間予算超過でページ追従打ち切り page={page}")
            break
        if nxt in seen_urls:  # 同一URLループ検知
            log.warning(f"[suumo] 次ページURLが既出（ループ）→打ち切り: {nxt}")
            break
        time.sleep(random.uniform(2, 5))
        code, html = fetch(nxt, session)
        if code != 200:
            log.warning(f"[suumo] page {page + 1} HTTP {code} - ページ追従を打ち切り（URLは変更しない）")
            break
        h = page_hash(html)
        if h in seen_hashes:  # 同一内容ループ検知
            log.warning(f"[suumo] 同一内容ページ（ループ）→打ち切り page={page + 1}")
            break
        seen_urls.add(nxt)
        seen_hashes.add(h)
        page_url = nxt
        page += 1

    # dedup by key（ページ跨ぎの重複を除去）
    seen = set()
    out = []
    for r in all_props:
        if r["key"] not in seen:
            seen.add(r["key"])
            out.append(r)
    log.info(f"[suumo] pages={page} cards={len(out)}")
    return out


# ---------------------------------------------------------------------------
# takken アダプタ（空き家バンクしずおか）
#   一覧 = li.item-block（1ページ10件）。価格 .price / 面積 .area（建物・土地併記、
#   土地優先）/ 所在地 .title / 詳細URL は /物件/{id}/... リンク。ページャは
#   a.page-number の onclick $('#list_update').load('.../page/N') を辿る AJAX 型。
#   サーバが Content-Type を ISO-8859-1 と誤申告するが fetch() 側で UTF-8 補正済み。
# ---------------------------------------------------------------------------

TAKKEN_MAX_PAGES = 20  # 1サイトあたりのページ追従上限


def _extract_takken_cards(soup, base_url: str, filters: dict) -> list:
    out = []
    for card in soup.find_all("li", class_="item-block"):
        # 詳細URL: /物件/ を含むリンクを優先
        url = ""
        for a in card.find_all("a", href=True):
            if "物件" in urllib.parse.unquote(a["href"]):
                url = normalize_url(a["href"], base_url)
                break
        if not url:
            a = card.find("a", href=True)
            url = normalize_url(a["href"], base_url) if a else base_url

        price_el = card.select_one(".price")
        area_el = card.select_one(".area")
        title_el = card.select_one(".title")
        cat_el = card.select_one(".cat")

        price = parse_price_man(price_el.get_text(strip=True)) if price_el else None
        area, area_est = (None, False)
        if area_el:
            # 「建物面積… 土地面積…」併記。parse_area_sqm が土地ラベルを優先する。
            area, area_est = parse_area_sqm(area_el.get_text(strip=True))
        location = title_el.get_text(" ", strip=True) if title_el else ""
        cat = cat_el.get_text(strip=True) if cat_el else ""
        card_text = card.get_text(" ", strip=True)
        text = (cat + " " + location).strip() or location or card_text[:60]

        # 種別ヒント: .cat（売土地/中古売住宅/新築売住宅 等）から
        if "土地" in cat:
            dtype = "更地"
        elif "空き家" in cat or "空家" in cat:
            dtype = "空き家"
        else:
            dtype = "中古戸建"  # 売住宅/新築/中古 等
        rec = _make_record(url, text, price, area, area_est, card_text, filters,
                           location=location, default_type=dtype)
        out.append(rec)
    return out


def _takken_loadbase(soup):
    """ページャ onclick の .load('URL') から /page/N を除いた基底パスを取る。無ければ None。"""
    for a in soup.select("a.page-number"):
        m = re.search(r"\.load\('([^']+)'\)", a.get("onclick", "") or "")
        if m:
            load = urllib.parse.unquote(m.group(1))
            return re.sub(r"/page/\d+/?$", "", load)
    return None


def _takken_total_pages(soup) -> int:
    el = soup.select_one("span.pageAll")
    if el:
        m = re.search(r"(\d+)", el.get_text())
        if m:
            return int(m.group(1))
    return 1


def parse_takken(first_html: str, base_url: str, filter_keywords: list,
                 filters: dict, session: requests.Session) -> list:
    """takken（空き家バンクしずおか）一覧アダプタ。ページャ（AJAX .load）追従つき。"""
    soup = BeautifulSoup(first_html, "html.parser")
    all_props = _extract_takken_cards(soup, base_url, filters)
    total = _takken_total_pages(soup)
    loadbase = _takken_loadbase(soup)
    page = 1
    while loadbase and page < min(total, TAKKEN_MAX_PAGES):
        if not _site_time_left():
            log.warning(f"[takken] サイト時間予算超過でページ追従打ち切り page={page}")
            break
        page += 1
        time.sleep(random.uniform(2, 5))
        nxt = urllib.parse.urljoin(base_url, loadbase + f"/page/{page}")
        code, html = fetch(nxt, session)
        if code != 200:
            log.warning(f"[takken] page {page} HTTP {code} - ページ追従を打ち切り（URLは変更しない）")
            break
        all_props.extend(
            _extract_takken_cards(BeautifulSoup(html, "html.parser"), base_url, filters))

    seen = set()
    out = []
    for r in all_props:
        if r["key"] not in seen:
            seen.add(r["key"])
            out.append(r)
    log.info(f"[takken] pages={page} cards={len(out)}")
    return out


# ---------------------------------------------------------------------------
# 共通ヘルパ（athome / LIFULL アダプタ用）
# ---------------------------------------------------------------------------

def _first_sqm(text: str):
    """テキストから最初の面積値を㎡に正規化（m²/㎡/m2 優先、無ければ坪換算）。"""
    if not text:
        return None
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*(?:㎡|m²|m2)", text)
    if m:
        return round(float(m.group(1).replace(",", "")), 1)
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*坪", text)
    if m:
        return round(float(m.group(1).replace(",", "")) * TSUBO_TO_SQM, 1)
    return None


def _page_blocked(html: str, soup, card_selector: str) -> bool:
    """カードが1枚も無く、かつ極小ページ＝bot対策/ソフトブロックと判定。

    大きいページでカードが無い場合は真の0件・構造変化として扱い False。
    """
    if soup.select_one(card_selector):
        return False
    return len(html) < 12000


# ---------------------------------------------------------------------------
# athome アダプタ（土地 /tochi/・中古戸建 /kodate/chuko/。SSRで静的取得可）
#   カード = div.card-box。属性 = .property-detail-table__block(<strong>ラベル</strong>
#   <span>値</span>)。価格 = .property-price。詳細URL = /tochi|kodate/{id}/。
#   bot対策の極小ページは検出→リトライ→継続なら BotBlocked。単一ページ抽出。
# ---------------------------------------------------------------------------

def _extract_athome_cards(soup, base_url, filter_keywords, filters):
    out = []
    dtype = "中古戸建" if "/kodate/" in base_url else "更地"
    for card in soup.select("div.card-box"):
        pe = card.select_one(".property-price") or card.select_one("[class*=price]")
        price = parse_price_man(pe.get_text(strip=True)) if pe else None
        blocks = {}
        for blk in card.select(".property-detail-table__block"):
            st = blk.find("strong")
            sp = blk.find("span")
            if st and sp:
                blocks.setdefault(st.get_text(strip=True), sp.get_text(" ", strip=True))
        location = blocks.get("所在地", "")
        area = _first_sqm(blocks.get("土地面積", ""))
        if area is None:
            for k, v in blocks.items():
                if "面積" in k:
                    area = _first_sqm(v)
                    if area is not None:
                        break
        url = ""
        for a in card.find_all("a", href=True):
            if re.match(r"/(tochi|kodate)/\d", a["href"]):
                url = normalize_url(a["href"], base_url)
                break
        if not url:
            continue
        card_text = card.get_text(" ", strip=True)
        if filter_keywords and not any(kw in (location + " " + card_text) for kw in filter_keywords):
            continue
        out.append(_make_record(url, location or card_text[:60], price, area, False,
                                card_text, filters, location=location, default_type=dtype))
    return out


def parse_athome(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    for attempt in range(2):
        if not _page_blocked(first_html, soup, "div.card-box"):
            break
        wait = 8 + attempt * 8
        log.warning(f"[athome] bot対策ページ検出（{len(first_html)}B）。{wait}秒待って再取得 {attempt + 1}/2: {base_url}")
        time.sleep(wait)
        code, first_html = fetch(base_url, session)
        soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "div.card-box"):
        raise BotBlocked(f"athome bot対策ページが継続: {base_url}")
    out = _extract_athome_cards(soup, base_url, filter_keywords, filters)
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[athome] cards={len(dedup)} (1ページ)")
    return dedup


# ---------------------------------------------------------------------------
# LIFULL HOME'S アダプタ（土地 /tochi/・中古戸建 /kodate/chuko/）
#   カード = div.mod-mergeBuilding--sale。価格/土地面積は spec テーブルの th↔td 対応
#   （中古戸建は「土地面積」ラベルで土地優先）。所在地 = .bukkenName。
#   詳細URL = /tochi|kodate/b-{id}/。202対策は run() 側（スリープ延長＋再試行）。単一ページ。
# ---------------------------------------------------------------------------

def _lifull_card_specs(card):
    # 価格を含み、かつ th↔td が1対1に揃ったテーブルのみ採用する。
    # 中古戸建(kodate)のカードは 価格テーブルが先頭に画像/要約セルを持ち td数が th数と
    # 食い違う（例 th4・td10）ため、位置揃えの zip が破綻して価格が「掲載画像N枚」に
    # なる。揃ったテーブル(土地=9/9, 中古戸建の整列テーブル=4/4)を選べば両方で正しく取れる。
    for t in card.find_all("table"):
        ths = [x.get_text(strip=True) for x in t.find_all("th")]
        if "価格" not in ths:
            continue
        tds = [x.get_text(" ", strip=True) for x in t.find_all("td")]
        if len(tds) == len(ths):
            return dict(zip(ths, tds))
    return {}


def _extract_lifull_cards(soup, base_url, filter_keywords, filters):
    out = []
    dtype = "中古戸建" if "/kodate/" in base_url else "更地"
    for card in soup.select("div.mod-mergeBuilding--sale"):
        specs = _lifull_card_specs(card)
        price = parse_price_man(specs.get("価格", ""))
        if price is None:
            pl = card.select_one(".priceLabel")   # 整列テーブルが無い場合の価格フォールバック
            if pl:
                price = parse_price_man(pl.get_text(" ", strip=True))
        # 中古戸建は土地面積優先、無ければ建物面積で代替。
        area = _first_sqm(specs.get("土地面積", "")) or _first_sqm(specs.get("建物面積", ""))
        nm = card.select_one(".bukkenName")
        location = nm.get_text(" ", strip=True) if nm else ""
        url = ""
        for a in card.find_all("a", href=True):
            if re.search(r"/(tochi|kodate)/b-\d", a["href"]):
                url = normalize_url(a["href"], base_url)
                break
        if not url:
            continue
        card_text = card.get_text(" ", strip=True)
        if filter_keywords and not any(kw in (location + " " + card_text) for kw in filter_keywords):
            continue
        out.append(_make_record(url, location or card_text[:60], price, area, False,
                                card_text, filters, location=location, default_type=dtype))
    return out


def parse_lifull(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "div.mod-mergeBuilding--sale"):
        raise BotBlocked(f"LIFULL ソフトブロック（{len(first_html)}B）: {base_url}")
    out = _extract_lifull_cards(soup, base_url, filter_keywords, filters)
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[lifull] cards={len(dedup)} (1ページ)")
    return dedup


# ---------------------------------------------------------------------------
# 家いちば アダプタ（個人直・持て余し物件。ieichiba.com）
#   カード = a.property__list-item（カード自体が <a>、href=/project/{id} ＝詳細URL）。
#   価格 = .property__list-item-price。所在地 = .property__list-item-address（末尾に価格が
#   付くので除去）。※一覧カードに土地面積が無い（詳細ページのみ）→ area=None。
#   説明文が豊富なので 種別/再建築/プラス・マイナスフラグは card_text から判定できる。単一ページ。
# ---------------------------------------------------------------------------

def _extract_ieichiba_cards(soup, base_url, filter_keywords, filters):
    out = []
    for card in soup.select("a.property__list-item"):
        url = normalize_url(card.get("href", ""), base_url)
        if not url:
            continue
        pe = card.select_one(".property__list-item-price")
        price = parse_price_man(pe.get_text(strip=True)) if pe else None
        ae = card.select_one(".property__list-item-address")
        location = ""
        if ae:
            location = re.sub(r"\s*[\d,]+\s*万円.*$", "", ae.get_text(" ", strip=True)).strip()
        card_text = card.get_text(" ", strip=True)
        # 所在地(住所)で判定する。説明文には近隣市町名が出るため card_text 一致だと誤検出する。
        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue
        # 一覧に面積が無いため area=None（詳細ページ取得は将来）。種別は本文から判定。
        out.append(_make_record(url, location or card_text[:60], price, None, False,
                                card_text, filters, location=location, default_type="更地"))
    return out


def parse_ieichiba(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "a.property__list-item"):
        raise BotBlocked(f"家いちば ソフトブロック（{len(first_html)}B）: {base_url}")
    out = _extract_ieichiba_cards(soup, base_url, filter_keywords, filters)
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[ieichiba] cards={len(dedup)} (1ページ・面積は一覧に無し)")
    return dedup


# ---------------------------------------------------------------------------
# 真野開発 アダプタ（地場業者自社HP。manokaihatsu.com）
#   カード = li.estate-item（.item-price を持つもの＝物件カード、ナビ項目を排除）。
#   価格 = .item-price（テキスト "500 万円"）。
#   面積/所在地 = table.item-table の th↔td zip（"土地面積"/"所在地"）。
#   詳細URL = a[href*='estate/post'] 。単一ページ。
# ---------------------------------------------------------------------------

def _mano_card_specs(card):
    tbl = card.select_one("table.item-table")
    if not tbl:
        return {}
    ths = [th.get_text(strip=True) for th in tbl.find_all("th")]
    tds = [td.get_text(" ", strip=True) for td in tbl.find_all("td")]
    return dict(zip(ths, tds))


def parse_mano(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "li.estate-item"):
        raise BotBlocked(f"真野開発 ソフトブロック（{len(first_html)}B）: {base_url}")
    out = []
    for card in soup.select("li.estate-item"):
        pe = card.select_one(".item-price")
        if not pe:
            continue  # ナビ項目（物件でない li.estate-item）をスキップ
        price = parse_price_man(pe.get_text(" ", strip=True))
        specs = _mano_card_specs(card)
        location = specs.get("所在地", "").strip()
        area = _first_sqm(specs.get("土地面積", ""))
        a = card.find("a", href=re.compile(r"/estate/post"))
        url = normalize_url(a["href"], base_url) if a else ""
        if not url:
            continue
        card_text = card.get_text(" ", strip=True)
        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue
        out.append(_make_record(url, location or card_text[:60], price, area, False,
                                card_text, filters, location=location, default_type="更地"))
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[mano] cards={len(dedup)} (1ページ)")
    return dedup


# ---------------------------------------------------------------------------
# 不動産創研 アダプタ（地場業者自社HP。fudosansoken.jp）
#   カード = div.article-object（全物件一覧 /sp-allbukken/）。
#   価格 = .cell3 span.price.num（数字のみ）。
#   面積 = .cell5（br区切り 3行目が土地面積 or 建物面積）。
#   所在地 = .cell1（span.bold=路線名 のあとのテキストノード=住所）。
#   種別 = .cell6（"売地"/"中古戸建"等）。
#   詳細URL = a[href*='/detail-']（相対→絶対）。単一ページ。
# ---------------------------------------------------------------------------

def _fudosoken_location(cell1):
    bold = cell1.select_one("span.bold")
    if bold:
        bold.decompose()
    return cell1.get_text(" ", strip=True)


def _fudosoken_area(cell5):
    txt = cell5.get_text("\n", strip=True)
    for line in reversed(txt.split("\n")):
        v = _first_sqm(line)
        if v:
            return v
    return None


def parse_fudosoken(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "div.article-object"):
        raise BotBlocked(f"不動産創研 ソフトブロック（{len(first_html)}B）: {base_url}")
    out = []
    for card in soup.select("div.article-object"):
        c3 = card.select_one(".cell3")
        price_num = card.select_one("span.price.num")
        price = parse_price_man((price_num.get_text(strip=True) + "万円") if price_num else "") if c3 else None
        c5 = card.select_one(".cell5")
        area = _fudosoken_area(c5) if c5 else None
        c1 = card.select_one(".cell1")
        location = _fudosoken_location(c1) if c1 else ""
        a = card.find("a", href=re.compile(r"/detail-"))
        if not a:
            continue
        url = normalize_url(a["href"], base_url)
        card_text = card.get_text(" ", strip=True)
        shubetsu_hint = card.select_one(".cell6")
        flag_text = (shubetsu_hint.get_text(" ", strip=True) if shubetsu_hint else "") + " " + card_text
        if filter_keywords and not any(kw in (location + " " + card_text) for kw in filter_keywords):
            continue
        dtype = "中古戸建" if "/kodate/" in url else "更地"
        out.append(_make_record(url, location or card_text[:60], price, area, False,
                                flag_text, filters, location=location, default_type=dtype))
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[fudosoken] cards={len(dedup)} (1ページ)")
    return dedup


# ---------------------------------------------------------------------------
# 伊豆総合企画 アダプタ（地場業者自社HP。izu-s-k.fudohsan.jp）
#   カード = div.list_simple_box（10件/ページ、ページャ追従）。
#   価格 = dl.list_price dd:first → "38万円"。
#   面積 = .dpoint2 → "199m²"。
#   所在地 = .list_detail テキストの "所在地 {X} 交通" 区間。
#   詳細URL = a[href*='post_type=fudo']。
#   ページャ = a[href*='paged='] の次ページリンクを追従（2ページ目以降も同カード構造）。
# ---------------------------------------------------------------------------

def _izu_sougou_cards(soup, base_url, filter_keywords, filters):
    out = []
    for card in soup.select("div.list_simple_box"):
        dp2 = card.select_one(".dpoint2")
        area = _first_sqm(dp2.get_text(strip=True)) if dp2 else None
        lp = card.select_one("dl.list_price")
        price = None
        if lp:
            dd = lp.find("dd")
            if dd:
                price = parse_price_man(dd.get_text(strip=True))
        det = card.select_one(".list_detail")
        location = ""
        if det:
            txt = det.get_text(" ", strip=True)
            m = re.search(r"所在地\s+(.+?)(?:\s+交通|\s+面積:|$)", txt)
            if m:
                location = m.group(1).strip()
        a = card.find("a", href=re.compile(r"post_type=fudo"))
        if not a:
            continue
        url = normalize_url(a["href"], base_url)
        card_text = card.get_text(" ", strip=True)
        if filter_keywords and not any(kw in (location + " " + card_text) for kw in filter_keywords):
            continue
        out.append(_make_record(url, location or card_text[:60], price, area, False,
                                card_text, filters, location=location, default_type="更地"))
    return out


def parse_izu_sougou(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "div.list_simple_box"):
        raise BotBlocked(f"伊豆総合企画 ソフトブロック（{len(first_html)}B）: {base_url}")
    out = _izu_sougou_cards(soup, base_url, filter_keywords, filters)
    # ページャ追従（最大5ページ、同一コンテンツハッシュでループ検出）
    seen_hashes = {page_hash(first_html)}
    for plink in soup.select("a[href*='paged=']"):
        href = plink.get("href", "")
        if not re.search(r"paged=[2-9]", href):
            continue
        next_url = normalize_url(href, base_url)
        if not _site_time_left():
            break
        time.sleep(random.uniform(4, 8))
        code, nhtml = fetch(next_url, session)
        if code != 200 or page_hash(nhtml) in seen_hashes:
            break
        seen_hashes.add(page_hash(nhtml))
        nsoup = BeautifulSoup(nhtml, "html.parser")
        out.extend(_izu_sougou_cards(nsoup, base_url, filter_keywords, filters))
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[izu_sougou] cards={len(dedup)} ({len(seen_hashes)}ページ)")
    return dedup


# ---------------------------------------------------------------------------
# 新日本住建販売「家っち」 アダプタ（地場業者自社HP。snjhkk.com・Shift_JIS）
#   市町別の土地一覧 /list/1-4/0-{コード}/（HTTPヘッダが Shift_JIS 申告のため
#   requests が正しくデコード＝fetch側の追加対応は不要）。
#   カード = div.list_row_border。価格 = span.list_kakaku（"値下がり"等を含まない）。
#   所在地/土地面積 = div.list_row_right 内の th↔td テーブル（"所在地"/"土地面積"）。
#   詳細URL = a[href*='/s_r_']（相対 ../../../s_r_XXXXX/index.html → 絶対化）。単一ページ。
# ---------------------------------------------------------------------------

def parse_snjhkk(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "div.list_row_border"):
        raise BotBlocked(f"家っち ソフトブロック（{len(first_html)}B）: {base_url}")
    out = []
    for card in soup.select("div.list_row_border"):
        rt = card.select_one("div.list_row_right")
        if not rt:
            continue
        ths = [th.get_text(strip=True) for th in rt.find_all("th")]
        tds = [td.get_text(" ", strip=True) for td in rt.find_all("td")]
        specs = dict(zip(ths, tds))
        pe = card.select_one("span.list_kakaku")
        price = parse_price_man(pe.get_text(" ", strip=True)) if pe else parse_price_man(specs.get("価格", ""))
        location = specs.get("所在地", "").strip()
        area = _first_sqm(specs.get("土地面積", ""))
        a = card.find("a", href=re.compile(r"/s_r_\d"))
        url = normalize_url(a["href"], base_url) if a else ""
        if not url:
            continue
        card_text = card.get_text(" ", strip=True)
        # 所在地(住所)で7市町判定。各URLは市町別だが念のため住所一致で絞る。
        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue
        out.append(_make_record(url, location or card_text[:60], price, area, False,
                                card_text, filters, location=location, default_type="更地"))
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[snjhkk] cards={len(dedup)} (1ページ)")
    return dedup


# ---------------------------------------------------------------------------
# U2JAPAN 三島店 アダプタ（地場業者自社HP・仲介専門。u2japan-mishima-k.com/land/）
#   カード = li.result-list__panel。所在地 = p.result-list__address（"三島市 青木"）。
#   価格 = p.result-list__price（"1,200 万円"）。面積 = ㎡を含む p.result-list__text。
#   詳細URL = a[href*='/bkndetail/']（相対 /bkndetail/{id}/room{id}/ → 絶対化）。
#   本文に「上物あり」等が出るので 種別/フラグは card_text から判定可。?pg=2.. ページャ追従。
# ---------------------------------------------------------------------------

def _u2_cards(soup, base_url, filter_keywords, filters):
    out = []
    for card in soup.select("li.result-list__panel"):
        ad = card.select_one("p.result-list__address")
        location = ad.get_text(" ", strip=True) if ad else ""
        pe = card.select_one("p.result-list__price") or card.select_one("span.price")
        price = parse_price_man(pe.get_text(" ", strip=True)) if pe else None
        area = None
        for p in card.select("p.result-list__text"):
            tx = p.get_text(" ", strip=True)
            if "㎡" in tx:
                area = _first_sqm(tx)
                break
        a = card.find("a", href=re.compile(r"/bkndetail/"))
        url = normalize_url(a["href"], base_url) if a else ""
        if not url:
            continue
        card_text = card.get_text(" ", strip=True)
        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue
        out.append(_make_record(url, location or card_text[:60], price, area, False,
                                card_text, filters, location=location, default_type="更地"))
    return out


def parse_u2(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "li.result-list__panel"):
        raise BotBlocked(f"U2JAPAN ソフトブロック（{len(first_html)}B）: {base_url}")
    out = _u2_cards(soup, base_url, filter_keywords, filters)
    seen_hashes = {page_hash(first_html)}
    sep = "&" if "?" in base_url else "?"
    # ?pg=2.. を順に追従（最大8ページ、同一ハッシュ/カード0で打ち切り）
    for pg in range(2, 9):
        if not _site_time_left():
            break
        next_url = f"{base_url}{sep}pg={pg}"
        time.sleep(random.uniform(4, 8))
        code, nhtml = fetch(next_url, session)
        if code != 200 or page_hash(nhtml) in seen_hashes:
            break
        seen_hashes.add(page_hash(nhtml))
        nsoup = BeautifulSoup(nhtml, "html.parser")
        if not nsoup.select("li.result-list__panel"):
            break
        out.extend(_u2_cards(nsoup, base_url, filter_keywords, filters))
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[u2] cards={len(dedup)} ({len(seen_hashes)}ページ)")
    return dedup


# ---------------------------------------------------------------------------
# 山いちば アダプタ（山林売買専門。yamaichiba.com/category/sanrin-shizuoka/ tab=camp）
#   カード = article.list-article。タイトル(h2 a) = "山林物件318　静岡県周智郡森町"。
#   【済】= 売却済み → 除外。面積は "公簿面積 6.51ha（約19,700坪）" の ha 表記が主。
#   価格は一覧に無い → キーワード一致した販売中物件のみ詳細ページから取得（少数想定）。
# ---------------------------------------------------------------------------

_HA_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*(?:ha|ヘクタール)", re.I)


def _yamaichiba_sqm(text):
    """ha 優先で㎡に正規化（山林は ha 表記が主）。無ければ ㎡/坪。"""
    if not text:
        return None
    m = _HA_RE.search(text)
    if m:
        return round(float(m.group(1).replace(",", "")) * 10000, 1)
    return _first_sqm(text)


def parse_yamaichiba(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "article.list-article"):
        raise BotBlocked(f"山いちば ソフトブロック（{len(first_html)}B）: {base_url}")
    out = []
    detail_fetched = 0
    for card in soup.select("article.list-article"):
        h = card.find(["h2", "h3"])
        title = h.get_text(" ", strip=True) if h else ""
        if "物件" not in title:
            continue
        if "【済】" in title:
            continue  # 売却済み
        a = card.find("a", href=True)
        url = normalize_url(a["href"], base_url) if a else ""
        if not url:
            continue
        # 所在地 = タイトルから物件番号を除いた部分（"静岡県…" だが "静岡市…" 形式もある）
        location = re.sub(r"^山林物件\s*\d+\s*", "", title).replace("【済】", "").strip()
        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue
        card_text = card.get_text(" ", strip=True)
        area = _yamaichiba_sqm(card_text)
        price = None
        flag_text = card_text
        # 販売中×対象エリアのみ詳細ページで価格・面積を補完（レア新着想定・最大6件）
        if detail_fetched < 6 and _site_time_left():
            time.sleep(random.uniform(3, 6))
            code, dhtml = fetch(url, session)
            detail_fetched += 1
            if code == 200:
                dsoup = BeautifulSoup(dhtml, "html.parser")
                body = dsoup.select_one(".entry-content") or dsoup.find("article") or dsoup
                dtext = body.get_text(" ", strip=True)
                idx = dtext.find("価格")
                if idx != -1:
                    price = parse_price_man(dtext[idx: idx + 40])
                if price is None:
                    price = parse_price_man(dtext)
                if area is None:
                    area = _yamaichiba_sqm(dtext)
                flag_text = dtext[:2000]
        out.append(_make_record(url, title[:60], price, area, False,
                                flag_text, filters, location=location, default_type="更地"))
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[yamaichiba] cards={len(dedup)} (販売中のみ・詳細取得{detail_fetched}件)")
    return dedup


# ---------------------------------------------------------------------------
# 山林バンク アダプタ（山林売買専門。sanrinbank.jp トップ＝今月の全国在庫 tab=camp）
#   カード = ul.advise-list li（"物件No"入りのみ）。ラベル "所在地/地   目/面   積/価   格"
#   を正規表現で抽出。全角数字・全角スペース混在のため NFKC 正規化してから解析。
#   面積は坪表記 → ㎡換算。価格は "3万7000円"/"130万円（応相談）" 等の揺れに対応。
# ---------------------------------------------------------------------------

def _sanrinbank_price(s):
    """NFKC済み文字列から価格(万円)。"3万7000円"=3.7万 → 4万に丸め。取れなければ None。"""
    m = re.search(r"([\d,]+)\s*万\s*([\d,]+)?", s)
    if not m:
        return None
    man = float(m.group(1).replace(",", ""))
    if m.group(2):
        man += float(m.group(2).replace(",", "")) / 10000
    return max(1, int(round(man)))


def parse_sanrinbank(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "ul.advise-list li"):
        raise BotBlocked(f"山林バンク ソフトブロック（{len(first_html)}B）: {base_url}")
    out = []
    for li in soup.select("ul.advise-list li"):
        raw = li.get_text(" ", strip=True)
        if "物件No" not in raw:
            continue
        text = unicodedata.normalize("NFKC", raw)
        mloc = re.search(r"所在地\s*(.+?)\s*地\s*目", text)
        location = mloc.group(1).strip() if mloc else ""
        # 全国在庫なので所在地キーワード一致のみ採用（"全国各地"の案内行もここで落ちる）
        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue
        marea = re.search(r"面\s*積\s*([\d,.]+)\s*坪", text)
        area = round(float(marea.group(1).replace(",", "")) * TSUBO_TO_SQM, 1) if marea else None
        mp = re.search(r"価\s*格\s*(\S{1,24})", text)
        price = _sanrinbank_price(mp.group(1)) if mp else None
        a = li.find("a", href=True)
        url = normalize_url(a["href"], base_url) if a else base_url
        out.append(_make_record(url, location or text[:60], price, area, False,
                                text, filters, location=location, default_type="更地"))
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[sanrinbank] cards={len(dedup)} (全国在庫から所在地一致のみ)")
    return dedup


# ---------------------------------------------------------------------------
# 日本マウント アダプタ（田舎暮らし・リゾート専門。resort-estate.com tab=camp）
#   カード = div.bukken-items 直下の <a href=/detail/{id}>（22件/頁）。
#   価格 = .price。所在地 = カード本文の "静岡県…（「別荘地名」）"。面積は一覧に無し。
#   ページャ = base_url + "/page:N"（N=2..、カード0/同一ハッシュで打ち切り）。
# ---------------------------------------------------------------------------

def _resort_estate_cards(soup, base_url, filter_keywords, filters):
    out = []
    for card in soup.select("div.bukken-items > a[href]"):
        href = card.get("href", "")
        if "/detail/" not in href:
            continue
        url = normalize_url(href, base_url)
        card_text = card.get_text(" ", strip=True)
        mloc = re.search(r"静岡県\S*(?:「[^」]*」)?", card_text)
        location = mloc.group(0) if mloc else ""
        pe = card.select_one(".price")
        price = parse_price_man(pe.get_text(" ", strip=True)) if pe else parse_price_man(card_text)
        # 所在地優先で市町判定（説明文の近隣地名での誤検出を避ける）。所在地が
        # 取れないカードのみ本文で判定する。
        hay = location if location else card_text
        if filter_keywords and not any(kw in hay for kw in filter_keywords):
            continue
        dtype = "中古戸建" if re.search(r"\d\s*[SLDK]{1,4}\b|\dLDK", card_text) else "更地"
        out.append(_make_record(url, location or card_text[:60], price, None, False,
                                card_text, filters, location=location, default_type=dtype))
    return out


def parse_resort_estate(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "div.bukken-items"):
        raise BotBlocked(f"日本マウント ソフトブロック（{len(first_html)}B）: {base_url}")
    out = _resort_estate_cards(soup, base_url, filter_keywords, filters)
    seen_hashes = {page_hash(first_html)}
    base = base_url.rstrip("/")
    for pg in range(2, 13):
        if not _site_time_left():
            break
        time.sleep(random.uniform(4, 8))
        code, nhtml = fetch(f"{base}/page:{pg}", session)
        if code != 200 or page_hash(nhtml) in seen_hashes:
            break
        seen_hashes.add(page_hash(nhtml))
        nsoup = BeautifulSoup(nhtml, "html.parser")
        if not nsoup.select("div.bukken-items > a[href]"):
            break
        out.extend(_resort_estate_cards(nsoup, base_url, filter_keywords, filters))
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[resort_estate] cards={len(dedup)} ({len(seen_hashes)}ページ)")
    return dedup


# ---------------------------------------------------------------------------
# 東海ヤジマ アダプタ（伊豆最南部の地場業者。tokaiyajima.com/bukken/os2 tab=camp）
#   カード = article.hentry。本文に "価格 XX万円 坪単価 Y万円 所在地:静岡県…市… 交通:…"
#   が一続きのテキストで出る。詳細URL = a[href*='/fudo/']。
#   ?bukken=os2&paged=N.. ページャ追従（実測は下田市中心・東伊豆町/南伊豆町=Tier3含む）。
# ---------------------------------------------------------------------------

_TOKAIYAJIMA_LOC_RE = re.compile(r"所在地:(.*?)(?:\s*交通:|$)")
_TOKAIYAJIMA_PRICE_RE = re.compile(r"価格\s*([\d,]+\s*万円)")
_TOKAIYAJIMA_TANKA_RE = re.compile(r"坪単価\s*([\d,]+(?:\.\d+)?)\s*万円")
_TOKAIYAJIMA_TSUBO_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*坪(?!単価)")


def _tokaiyajima_area(text, price):
    """面積(㎡)。坪単価が併記されていれば「価格÷坪単価」の逆算を優先（誤差最小）。
    無ければタイトルの "XX坪" 表記を㎡換算。取れなければ None。"""
    mt = _TOKAIYAJIMA_TANKA_RE.search(text)
    if mt and price:
        tanka = float(mt.group(1).replace(",", ""))
        if tanka > 0:
            return round(price / tanka * TSUBO_TO_SQM, 1)
    mt2 = _TOKAIYAJIMA_TSUBO_RE.search(text)
    if mt2:
        return round(float(mt2.group(1).replace(",", "")) * TSUBO_TO_SQM, 1)
    return None


def _tokaiyajima_cards(soup, base_url, filter_keywords, filters):
    out = []
    for card in soup.select("article.hentry"):
        a = card.find("a", href=re.compile(r"/fudo/"))
        url = normalize_url(a["href"], base_url) if a else ""
        if not url:
            continue
        card_text = card.get_text(" ", strip=True)
        mloc = _TOKAIYAJIMA_LOC_RE.search(card_text)
        location = mloc.group(1).strip() if mloc else ""
        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue
        mp = _TOKAIYAJIMA_PRICE_RE.search(card_text)
        price = parse_price_man(mp.group(1)) if mp else None
        area = _tokaiyajima_area(card_text, price)
        dtype = "中古戸建" if "戸建" in card_text[:60] else "更地"
        title = card_text.split("【", 1)[0].strip()
        out.append(_make_record(url, title[:60] or card_text[:60], price, area, False,
                                card_text, filters, location=location, default_type=dtype))
    return out


def parse_tokaiyajima(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "article.hentry"):
        raise BotBlocked(f"東海ヤジマ ソフトブロック（{len(first_html)}B）: {base_url}")
    out = _tokaiyajima_cards(soup, base_url, filter_keywords, filters)
    seen_hashes = {page_hash(first_html)}
    for pg in range(2, 13):
        if not _site_time_left():
            break
        time.sleep(random.uniform(4, 8))
        code, nhtml = fetch(f"https://tokaiyajima.com/?bukken=os2&paged={pg}&so=kak&ord=&s=", session)
        if code != 200 or page_hash(nhtml) in seen_hashes:
            break
        seen_hashes.add(page_hash(nhtml))
        nsoup = BeautifulSoup(nhtml, "html.parser")
        if not nsoup.select("article.hentry"):
            break
        out.extend(_tokaiyajima_cards(nsoup, base_url, filter_keywords, filters))
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[tokaiyajima] cards={len(dedup)} ({len(seen_hashes)}ページ)")
    return dedup


# ---------------------------------------------------------------------------
# 田舎暮らし物件.com（いなかも家探し） アダプタ（複数業者アグリゲータ。
#   resort-bukken.com/izu tab=camp。resort-estate.com(日本マウント)と同系列テンプレート。
#   カード = div.bukken-items > a[href*='/detail/']。本文冒頭が "☆ 市町名 大字 種別 価格"
#   の形（"静岡県"接頭辞なし）。所在地はカード先頭2トークンから抽出。/izu/page:N.. ページャ。
# ---------------------------------------------------------------------------

def _resort_bukken_cards(soup, base_url, filter_keywords, filters):
    out = []
    for card in soup.select("div.bukken-items > a[href]"):
        href = card.get("href", "")
        if "/detail/" not in href:
            continue
        url = normalize_url(href, base_url)
        card_text = card.get_text(" ", strip=True).lstrip("☆").strip()
        tokens = card_text.split(" ")
        location = " ".join(tokens[:2]) if len(tokens) >= 2 else card_text[:20]
        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue
        price = parse_price_man(card_text)
        dtype = "中古戸建" if any(t in card_text for t in ("中古別荘", "中古住宅", "戸建て")) else "更地"
        out.append(_make_record(url, card_text[:60], price, None, False,
                                card_text, filters, location=location, default_type=dtype))
    return out


def parse_resort_bukken(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "div.bukken-items"):
        raise BotBlocked(f"いなかも家探し ソフトブロック（{len(first_html)}B）: {base_url}")
    out = _resort_bukken_cards(soup, base_url, filter_keywords, filters)
    seen_hashes = {page_hash(first_html)}
    base = base_url.rstrip("/")
    for pg in range(2, 13):
        if not _site_time_left():
            break
        time.sleep(random.uniform(4, 8))
        code, nhtml = fetch(f"{base}/page:{pg}", session)
        if code != 200 or page_hash(nhtml) in seen_hashes:
            break
        seen_hashes.add(page_hash(nhtml))
        nsoup = BeautifulSoup(nhtml, "html.parser")
        if not nsoup.select("div.bukken-items > a[href]"):
            break
        out.extend(_resort_bukken_cards(nsoup, base_url, filter_keywords, filters))
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[resort_bukken] cards={len(dedup)} ({len(seen_hashes)}ページ)")
    return dedup


# ---------------------------------------------------------------------------
# 天城オートキャンプ アダプタ（キャンプ場用地譲渡。izuhighland.jp tab=camp）
#   Wix で本文は遅延描画だが、物件詳細リンク（"〜用地詳細"/"〜不動産"）は HTML 内に
#   存在する → リンク一覧を監視するライト方式。価格・面積は取得不可（None）。
#   現在は全国物件のみ＝1h圏キーワードに一致せず0件。伊豆物件の新着待ち。
# ---------------------------------------------------------------------------

def parse_izuhighland(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    out = []
    seen_urls = set()
    for a in soup.find_all("a", href=True):
        href = urllib.parse.unquote(a["href"]).rstrip("/")
        if not re.search(r"(用地詳細|不動産)$", href):
            continue
        url = normalize_url(a["href"], base_url)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        # リンクテキストは「詳細」等の定型のため、所在地はURLスラッグ（日本語）から取る
        slug = href.rsplit("/", 1)[-1]
        location = re.sub(r"(ドックラン|および|キャンプ場|用地|詳細|一覧|スキー)", "", slug).strip() or slug
        if filter_keywords and not any(kw in slug for kw in filter_keywords):
            continue
        out.append(_make_record(url, slug[:60], None, None, False, slug, filters,
                                location=location, default_type="更地"))
    log.info(f"[izuhighland] cards={len(out)} (リンク監視型・全{len(seen_urls)}物件中キーワード一致のみ)")
    return out


# (述語, パーサ) の順に評価。最初に一致したものを使う。
# アダプタは (first_html, base_url, filter_keywords, filters, session) を取り、
# 正規化レコードのリストを返す（ページャ追従はアダプタ内で行う）。
SITE_ADAPTERS = [
    (lambda sid: sid.startswith("suumo_"), parse_suumo),
    (lambda sid: sid.startswith("takken_"), parse_takken),
    # athome は現在持続的に bot対策でブロック中のため adapter 対象から外し、urls.yaml で
    # sources_extra(フェーズ2) へ退避済み（リトライストーム回避）。parse_athome は将来用に残置。
    (lambda sid: sid.startswith("lifull_") and sid != "lifull_akiyabank", parse_lifull),
    (lambda sid: sid.startswith("ieichiba"), parse_ieichiba),
    (lambda sid: sid.startswith("mano_"), parse_mano),
    (lambda sid: sid.startswith("fudosoken_"), parse_fudosoken),
    (lambda sid: sid.startswith("izu_sougou_"), parse_izu_sougou),
    (lambda sid: sid.startswith("snjhkk_"), parse_snjhkk),
    (lambda sid: sid.startswith("u2_"), parse_u2),
    (lambda sid: sid.startswith("yamaichiba_"), parse_yamaichiba),
    (lambda sid: sid.startswith("sanrinbank_"), parse_sanrinbank),
    (lambda sid: sid.startswith("resort_estate_"), parse_resort_estate),
    (lambda sid: sid.startswith("izuhighland_"), parse_izuhighland),
    (lambda sid: sid.startswith("tokaiyajima_"), parse_tokaiyajima),
    (lambda sid: sid.startswith("resortbukken_"), parse_resort_bukken),
]


def get_adapter(site_id):
    for pred, fn in SITE_ADAPTERS:
        if pred(site_id):
            return fn
    return None


def page_hash(html: str) -> str:
    return hashlib.sha256(html.encode()).hexdigest()[:16]


def load_snapshot(site_id: str) -> dict:
    path = DATA_DIR / f"{site_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_snapshot(site_id: str, data: dict) -> None:
    path = DATA_DIR / f"{site_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_archive(site_id: str) -> dict:
    path = ARCHIVE_DIR / f"{site_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_archive(site_id: str, data: dict) -> None:
    path = ARCHIVE_DIR / f"{site_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _days_between(a_iso: str, b_iso: str) -> int:
    try:
        return (date.fromisoformat(a_iso) - date.fromisoformat(b_iso)).days
    except Exception:
        return 0


def municipality_hint(name: str) -> str:
    """サイト名から対象市町名（整合チェック用）を粗く抽出。"""
    for town in ("函南町", "伊豆の国市", "三島市", "沼津市", "清水町", "長泉町",
                 "田方郡", "駿東郡"):
        if town in name:
            return town
    return ""


def _intra_domain_order(items):
    """同一ドメイン内で種別(URLパス先頭: tochi/kodate 等)を round-robin に交互配置する。
    homes.co.jp は累積リクエスト数で 202(レート制限)になり、後半のサイトが弾かれる。
    土地(tochi)と中古戸建(kodate)を交互にすると「制限前の良い枠」が両種別へ分かれ、
    主要な町は土地・中古戸建の双方を取得できる（中古戸建が常に最後＝0件になるのを防ぐ）。"""
    subs, order = {}, []
    for s in items:
        path = urllib.parse.urlsplit(s.get("url", "")).path.strip("/").split("/")
        seg = path[0] if path and path[0] else ""
        if seg not in subs:
            subs[seg] = []
            order.append(seg)
        subs[seg].append(s)
    queues = [subs[k] for k in order]
    idx = [0] * len(queues)
    out = []
    while len(out) < len(items):
        for qi, q in enumerate(queues):
            if idx[qi] < len(q):
                out.append(q[idx[qi]])
                idx[qi] += 1
    return out


def _disperse_by_domain(sites):
    """各ドメインを巡回全体へ均等配置して並べ替える（連続を避ける）。
    LIFULL(homes.co.jp)の14サイトが連続して叩かれ 202(レート制限)になるのを緩和する。
    各サイトに「グループ内位置の分数 = (group内index+0.5)/group件数」を割り当て、その昇順に
    並べると、件数の多いドメインも巡回全体へ均等にばらける（末尾への偏りが出ない）。
    さらにドメイン内では種別(土地/中古戸建)を交互配置する。
    URLは一切変えない。巡回順のみ変更（順序は結果の正しさに影響しない）。"""
    groups = {}
    for s in sites:
        dom = urllib.parse.urlsplit(s.get("url", "")).netloc
        groups.setdefault(dom, []).append(s)
    keyed = []
    for n, (dom, items) in enumerate(groups.items()):
        items = _intra_domain_order(items)
        for i, s in enumerate(items):
            # 第2キー(n)は同分数時の安定なドメイン分散用。
            keyed.append((((i + 0.5) / len(items)), n, s))
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in keyed]


def run(dry_run: bool = False, only: str = "") -> int:
    config = yaml.safe_load((BASE_DIR / "urls.yaml").read_text(encoding="utf-8"))
    sites = config["sites"]
    filters = config["filters"]
    # tab: camp のサイトは filters.camp で閾値を上書きした判定を使う（定義元は urls.yaml）
    camp_filters = {**filters, **(filters.get("camp") or {})}
    if only:
        sites = [s for s in sites if only in s["id"]]
        log.info(f"--only='{only}' で {len(sites)} サイトに絞り込み")
    # 同一ドメイン連続を避ける（LIFULL 202レート制限の緩和）。
    sites = _disperse_by_domain(sites)
    session = requests.Session()

    results = []
    disappeared = []   # (site_name, archived_item, days_since_removed) 消滅(7日以内)
    today = date.today().isoformat()
    fail_count = 0
    run_start = time.time()

    for i, site in enumerate(sites):
        sid = site["id"]
        name = site["name"]
        url = site["url"]
        yaml_status = site.get("status", "")
        filter_kws = site.get("filter_keywords", [])
        site_tab = site.get("tab", "home")
        site_filters = camp_filters if site_tab == "camp" else filters
        # 実行全体のウォールクロック上限。超えたら残サイトを打ち切ってレポートへ。
        if time.time() - run_start > RUN_WALLCLOCK_LIMIT:
            log.warning(f"実行ウォールクロック上限 {RUN_WALLCLOCK_LIMIT}s 超過。残 {len(sites) - i} サイトを打ち切り")
            break
        _SITE_DEADLINE[0] = time.time() + SITE_TIME_BUDGET  # このサイトの時間予算
        log.info(f"[{sid}] fetch start: {url}")

        row = {
            "id": sid, "name": name, "url": url, "yaml_status": yaml_status,
            "http": None, "raw": 0, "price_cnt": 0, "area_cnt": 0,
            "fit_cnt": 0, "ng_cnt": 0, "added_cnt": 0, "note": "", "phase2": False,
            "props": [], "fits": [], "ng_items": [], "added_items": [],
            "promote": False, "mode": "",
        }

        if not robots_allowed(url, session):
            log.warning(f"[{sid}] robots制限")
            row["http"] = "robots制限"
            row["note"] = "robots制限により除外"
            row["phase2"] = False
            results.append(row)
            fail_count += 1
            if i < len(sites) - 1:
                time.sleep(random.uniform(2, 5))
            continue

        status_code, html = fetch(url, session)
        row["http"] = status_code if status_code != 0 else "ERROR"

        if status_code == 0:
            log.error(f"[{sid}] fetch error: {html[:120]}")
            row["note"] = f"接続エラー: {html[:80]}"
            row["phase2"] = True
            fail_count += 1
        elif status_code != 200:
            log.warning(f"[{sid}] HTTP {status_code}")
            row["note"] = f"HTTP {status_code} — 要確認（URLは変更しない）"
            row["phase2"] = True
            fail_count += 1
        else:
            adapter = get_adapter(sid)
            if adapter:
                try:
                    props = adapter(html, url, filter_kws, site_filters, session)
                    row["mode"] = "adapter"
                except BotBlocked as e:
                    # bot対策ページ＝0件で上書きしない。前回スナップショットを保持し
                    # 「要確認」扱い（差分・消滅判定もスキップ＝誤った全消滅を防ぐ）。
                    log.warning(f"[{sid}] BotBlocked: {e}")
                    row["mode"] = "blocked"
                    row["note"] = "bot対策ページ検出 — 前回データ保持・要確認（フェーズ2候補）"
                    row["phase2"] = True
                    fail_count += 1
                    results.append(row)
                    if i < len(sites) - 1:
                        time.sleep(random.uniform(2, 5))
                    continue
            else:
                # アダプタ未実装サイトは構造化抽出せずハッシュ監視（変更検知）に回す。
                # 物件テーブルの品質を adapter 済みサイトに揃えるため（C方針）。
                props = []
                row["mode"] = "hash"
            for p in props:
                p["tab"] = site_tab
            snapshot = load_snapshot(sid)
            row["raw"] = len(props)
            row["price_cnt"] = sum(1 for p in props if p["price_man"] is not None)
            row["area_cnt"] = sum(1 for p in props if p["area_sqm"] is not None)

            # 二層差分: first_seen/last_seen と added/removed
            prev_keys = snapshot.get("keys", {})
            current_keys = {}
            added_items = []
            for p in props:
                k = p["key"]
                pv = prev_keys.get(k)
                fs = pv.get("first_seen") if isinstance(pv, dict) else None
                p["first_seen"] = fs or today
                p["last_seen"] = today
                if not fs:
                    added_items.append(p)
                current_keys[k] = {
                    "first_seen": p["first_seen"], "last_seen": today,
                    "location": p["location"], "price_man": p["price_man"],
                    "area_sqm": p["area_sqm"], "url": p["url"], "text": p["text"],
                }

            # removed → archive 退避（消滅検出日を記録）。再出現したら archive から除去。
            archive = load_archive(sid)
            for k, pv in prev_keys.items():
                if k not in current_keys and k not in archive and isinstance(pv, dict):
                    archive[k] = {
                        "first_seen": pv.get("first_seen", today),
                        "last_seen": pv.get("last_seen", today),
                        "location": pv.get("location", ""), "price_man": pv.get("price_man"),
                        "area_sqm": pv.get("area_sqm"), "url": pv.get("url", ""),
                        "text": pv.get("text", ""), "removed_on": today, "site_name": name,
                    }
            for k in list(archive):
                if k in current_keys:
                    del archive[k]

            fits = [p for p in props if p["verdict"] == "適合"]
            ng_items = [p for p in props if p.get("ng_areas")]
            row["fit_cnt"] = len(fits)
            row["ng_cnt"] = len(ng_items)
            row["added_cnt"] = len(added_items)
            row["props"] = props
            row["fits"] = fits
            row["ng_items"] = ng_items
            row["added_items"] = added_items

            # 消滅(7日以内)を収集
            for k, a in archive.items():
                d = _days_between(today, a.get("removed_on", today))
                if 0 <= d <= DISAPPEAR_WINDOW_DAYS:
                    disappeared.append((a.get("site_name", name), a, d))

            if props:
                if not dry_run:
                    save_snapshot(sid, {
                        "keys": current_keys,
                        "hash": page_hash(html),
                        "fetched_at": datetime.now().isoformat(),
                    })
                    save_archive(sid, archive)
                log.info(
                    f"[{sid}] raw={len(props)} price={row['price_cnt']} "
                    f"area={row['area_cnt']} fit={row['fit_cnt']} added={row['added_cnt']}"
                )
            else:
                # 0件 → ページ本文ハッシュ監視
                h = page_hash(html)
                if not dry_run:
                    save_snapshot(sid, {
                        "keys": {}, "hash": h,
                        "fetched_at": datetime.now().isoformat(),
                    })
                    save_archive(sid, archive)
                if row["mode"] == "hash":
                    row["note"] = "アダプタ未実装 — ハッシュ監視（変更検知）"
                else:
                    row["note"] = "抽出0件 — ハッシュ監視扱い"
                    row["phase2"] = True
                log.info(f"[{sid}] hash-only mode={row['mode']}")

            # ⑤ derived 昇格推奨判定: 200 かつ 対象市町と整合
            if yaml_status == "derived":
                town = municipality_hint(name)
                page_ok = (town and town in html) or row["raw"] > 0
                if page_ok:
                    row["promote"] = True

        results.append(row)

        if i < len(sites) - 1:
            # 次サイトが LIFULL(homes.co.jp) なら間隔を延長（202レート制限回避）。
            # ドメイン分散で連続は減るが、念のため homes.co.jp 直前は長めに空ける。
            nxt_url = sites[i + 1].get("url", "")
            if "homes.co.jp" in nxt_url:
                time.sleep(random.uniform(12, 20))
            else:
                time.sleep(random.uniform(2, 5))

    ymd = datetime.now().strftime("%Y%m%d")
    prune_old_reports()
    html_doc = build_html_report(results, filters, disappeared, dry_run)
    html_path = REPORTS_DIR / f"{ymd}.html"
    index_path = REPORTS_DIR / "index.html"
    csv_path = REPORTS_DIR / f"{ymd}.csv"
    html_path.write_text(html_doc, encoding="utf-8")
    index_path.write_text(html_doc, encoding="utf-8")  # 最新の複製＝既定表示
    write_csv_report(csv_path, results)
    sources_path = BASE_DIR / "SOURCES.md"
    write_sources_md(sources_path, config, results)
    log.info(f"report(html):  {html_path}")
    log.info(f"report(index): {index_path}")
    log.info(f"report(csv):   {csv_path}")
    log.info(f"sources(md):   {sources_path}")

    success = sum(1 for r in results if isinstance(r["http"], int) and r["http"] == 200)
    if fail_count == 0:
        return 0
    elif success > 0:
        return 1
    else:
        return 2


def _flag_text(p) -> str:
    """フラグ（関心/注意/NG/属人性/面積推定）を1つの文字列に。空なら空文字。"""
    tags = []
    if p.get("interest"):
        tags.append("関心:" + "/".join(p["interest"]))
    if p.get("caution"):
        tags.append("注意:" + "/".join(p["caution"]))
    if p.get("ng_areas"):
        tags.append("NG:" + "/".join(p["ng_areas"]))
    if p.get("zokujinsei"):
        tags.append("属人性")
    if p.get("area_estimated"):
        tags.append("面積推定")
    return " ".join(tags)


def _fmt_price(p):
    v = p["price_man"]
    return f"{v:,}万円" if v is not None else "—"


def _fmt_area(p):
    a = p["area_sqm"]
    if a is None:
        return "—"
    return f"{a:g}㎡{'(推定)' if p.get('area_estimated') else ''}"


def _fmt_tsubo(p):
    v = p.get("tsubo_man")
    return f"{v:g}" if v is not None else "—"


def _short_loc(p) -> str:
    """表示用に所在地を短縮（静岡県を除去、市町＋大字程度に丸め）。全文はCSVに保持。"""
    s = (p.get("location") or p.get("text") or "").replace("静岡県", "").strip()
    return s[:20] if s else "—"


def prune_old_reports() -> None:
    """15日以上前の日付別 html/csv を削除（index.html は対象外）。"""
    today = date.today()
    for f in REPORTS_DIR.glob("*"):
        m = re.fullmatch(r"(\d{8})\.(html|csv)", f.name)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if (today - d).days > REPORT_RETENTION_DAYS:
            try:
                f.unlink()
                log.info(f"prune old report: {f.name}")
            except OSError:
                pass


def _date_options(today_ymd: str) -> list:
    """過去14日ぶんの日付別html（存在するもの＋当日）を新しい順に返す。"""
    days = {today_ymd}
    for f in REPORTS_DIR.glob("*.html"):
        m = re.fullmatch(r"(\d{8})\.html", f.name)
        if m:
            days.add(m.group(1))
    return sorted(days, reverse=True)[:REPORT_RETENTION_DAYS]


def _rebuild_class(mark: str) -> str:
    return {"○": "rb-ok", "△": "rb-wn", "×": "rb-ng"}.get(mark, "rb-uk")


def _help(text) -> str:
    """? ヘルプアイコン（ホバー/クリックで吹き出し）。"""
    from html import escape
    return (f"<span class='help' onclick=\"this.classList.toggle('show')\">?"
            f"<span class='tip'>{escape(text)}</span></span>")


def build_html_report(results: list, filters: dict, disappeared: list, dry_run: bool) -> str:
    from html import escape

    now = datetime.now()
    ts_label = now.strftime("%Y-%m-%d %H:%M")
    ymd = now.strftime("%Y%m%d")
    pmax_def = filters["price_max_man"]
    amin_def = filters["area_min_sqm"]

    ceil_by_type = filters.get("price_ceiling_by_type") or {}
    types = ["更地", "古家付き土地", "中古戸建", "空き家"]
    added_keys = {p["key"] for r in results for p in r["added_items"]}

    # 全物件を JSON 埋め込み用に整形（サーバ側ハード除外なし＝全件）
    data = []
    for r in results:
        for p in r["props"]:
            data.append({
                "site": r["name"],
                "tab": p.get("tab", "home"),
                "added": p["key"] in added_keys,
                "machi": p.get("machi", ""),
                "shubetsu": p.get("shubetsu", "更地"),
                "shubetsu_reason": p.get("shubetsu_reason", ""),
                "loc": p["location"] or p["text"],
                "price": p["price_man"],
                "area": p["area_sqm"],
                "tsubo": p.get("tsubo_man"),
                "chimoku": p.get("chimoku", "—"),
                "toshi": p.get("toshikeikaku", "—"),
                "setsudo": p.get("setsudo"),
                "rb": p.get("rebuild_mark", "不明"),
                "rbreason": p.get("rebuild_reason", ""),
                "flags": _flag_text(p),
                "cautions": p.get("caution", []),
                "interests": p.get("interest", []),
                "zokujin": bool(p.get("zokujinsei")),
                "first_seen": p.get("first_seen") or "",
                "url": p["url"],
                "dk": p["key"],  # バックエンドdedupキー（url+"|"+text[:60]）。非表示永続化に使用
                "ng": bool(p.get("ng_areas")),
                # 自由入力の除外語マッチ用の検索テキスト（所在地＋見出し＋フラグ＋属性）
                "hay": " ".join([
                    p.get("location", "") or "", p.get("text", "") or "", _flag_text(p),
                    p.get("chimoku", "") or "", p.get("toshikeikaku", "") or "",
                    p.get("rebuild_reason", "") or "", p.get("shubetsu", "") or "",
                ]),
            })
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    added = [(r["name"], p) for r in results for p in r["added_items"]]
    added.sort(key=lambda t: (t[1]["price_man"] if t[1]["price_man"] is not None else 1 << 30))
    ng_log = [(r["name"], p) for r in results for p in r["ng_items"]]

    css = _REPORT_CSS

    # 日付ドロップダウン（過去14日）
    opts = []
    for d in _date_options(ymd):
        sel = " selected" if d == ymd else ""
        label = f"{d[:4]}-{d[4:6]}-{d[6:]}" + ("（最新）" if d == ymd else "")
        opts.append(f"<option value='{d}.html'{sel}>{label}</option>")
    date_nav = ("<select onchange=\"location.href=this.value\">" + "".join(opts) + "</select>")

    disclaimer = ("<p class='note'>※ 建築可否の ○△×／不明 は掲載情報からの<b>推定</b>であり、"
                  "法的確定ではありません（更地＝新規建築、家付き＝再建築の可否を表示）。"
                  "最終判断には役場確認が必要です。"
                  "市街化調整区域（△）は除外ではなく本命候補シグナルです。</p>")

    camp_over = filters.get("camp") or {}
    config_js = json.dumps({
        "ceilings": {t: ceil_by_type.get(t, pmax_def) for t in types},
        "types": types,
        "machi": list(_MACHI_NAMES),
        "cautions": filters.get("caution_keywords", []),
        "exareas": filters.get("exclude_areas", []),
        "amin": amin_def,
        # キャンプ場土地タブ: 判定閾値(参考)。表示フィルタ既定は「絞らない」(null)
        "campPmax": camp_over.get("price_max_man"),
        "campAmin": camp_over.get("area_min_sqm"),
    }, ensure_ascii=False)

    H = ["<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width, initial-scale=1'>",
         "<meta name='robots' content='noindex'>",
         f"<title>不動産情報収集ツール {ts_label}</title><style>{css}</style></head><body>"]

    # ---- トップバー（タイトル / 日付）----
    H.append("<div class='topbar'>")
    H.append(f"<h1>不動産情報収集ツール <span class='muted'>{ts_label}</span></h1>")
    H.append("<div class='topctl'>日付 " + date_nav + "</div>")
    H.append("</div>")
    if dry_run:
        H.append("<p class='muted'>dry-run モード（スナップショット更新なし）</p>")

    # ---- タブ（更地 / 家付き土地 / キャンプ場土地）----
    H.append("<div class='tabs'>")
    H.append("<button class='tab-btn' data-tab='sarachi'>更地</button>")
    H.append("<button class='tab-btn' data-tab='ie'>家付き土地</button>")
    H.append("<button class='tab-btn' data-tab='camp'>キャンプ場土地</button>")
    H.append("</div>")

    # ---- パネル（検索条件）----
    amin_tsubo = f"{amin_def / 3.305785:.1f}"
    H.append("<button id='filterToggle'>検索条件 ▾</button>"
             " <span class='cnt' id='cntTop'>—</span>")
    H.append("<div class='panel' id='panel'>")
    # タイトル文字は出さず、操作ボタンのみ右上に置く
    H.append("<div class='panel-title'><span></span>"
             "<button id='resetBtn'>条件をリセット</button></div>")
    H.append("<div class='princ'>種別・地目・市町・建築可否・坪単価のしぼり込みと並べ替えは"
             "<b>各列の見出しをタップ</b>（PC・iPhone共通）。</div>")
    # (a) 価格フィルタ（上=下限／下=上限 の縦並び）
    H.append("<div class='filter-block prow'>"
             "<div class='fb-head'><span class='filter-label'>価格</span>"
             + _help("このタブの物件を価格でしぼり込みます。空欄にすると制限なし。") + "</div>"
             "<div class='fb-line'><span class='mm'>いくら以上</span>"
             "<input type='number' id='priceMinInput' value=''> 万円</div>"
             "<div class='fb-line'><span class='mm'>いくら以下</span>"
             "<input type='number' id='priceMaxInput'> 万円</div>"
             "</div>")
    # (b) 面積フィルタ（坪メイン入力＋㎡は読取専用表示。上=下限／下=上限）
    H.append(f"<div class='filter-block prow'>"
             f"<div class='fb-head'><span class='filter-label'>面積</span>"
             + _help("坪で入力します（㎡は自動換算の参考表示）。空欄にすると制限なし。") + "</div>"
             f"<div class='fb-line'><span class='mm'>これ以上</span>"
             f"<input type='number' id='aminTsuboInput' value='{amin_tsubo}'> 坪"
             f"<span class='sqm-note'>= <span id='aminSqmView'>{amin_def}</span> ㎡</span></div>"
             f"<div class='fb-line'><span class='mm'>これ以下</span>"
             f"<input type='number' id='amaxTsuboInput'> 坪"
             f"<span class='sqm-note'>= <span id='amaxSqmView'>—</span> ㎡</span></div>"
             f"</div>")
    # (c) 除外エリア
    H.append("<div class='prow'><b>除外エリア</b>"
             + _help("所在地にこの地名を含む物件を一覧から隠します。")
             + " <button id='areaBtn'>除外エリアを編集…</button></div>")
    H.append("<div class='prow'>表示 <span class='cnt' id='cnt'>—</span></div>")
    H.append("</div>")
    # 「除外エリア」ポップアップ
    H.append("<div id='areaPop'>"
             + "<div class='pr'><b>除外エリア</b>（所在地に含む地名で隠す）"
             + _help("所在地にこの地名を含む物件を一覧から隠します。") + "</div>"
             + "<div id='areaList'></div>"
             + "<div class='pr' style='border-top:1px solid #ddd;padding-top:5px'>追加: "
             + "<input id='areaInput' type='text' style='width:120px' placeholder='例: 別荘地名'>"
             + " <button id='areaAdd'>追加</button></div>"
             + "<div class='pr'><button id='areaClose'>閉じる</button></div></div>")

    # ---- 参考情報（バッジ凡例）— パネル直下に配置 ----
    H.append("<details class='refbox cond'>")
    H.append("<summary>参考情報（バッジの見方）</summary>")
    H.append("<div><span class='lbl lbl-plus'>プラス要素（好材料）</span> "
             + escape("、".join(filters.get("interest_keywords", [])) or "なし")
             + _help("所在地の後ろに、好材料は緑・注意点は赤の目印が付きます。除外はしません。") + "</div>")
    H.append("<div><span class='lbl lbl-minus'>マイナス要素（注意点）</span> "
             + escape("、".join(filters.get("caution_keywords", [])) or "なし")
             + _help("所在地の後ろに、好材料は緑・注意点は赤の目印が付きます。除外はしません。") + "</div>")
    H.append("<div class='note'>" + disclaimer.replace("<p class='note'>", "").replace("</p>", "") + "</div>")
    H.append("</details>")

    # ---- 物件ブラウザ（折り畳み・JS描画）----
    H.append("<h2 class='sec open' data-target='secMain'>物件ブラウザ（全件・クライアント側フィルタ）</h2>")
    H.append("<div id='secMain' class='secbody open'><table id='mainTbl'></table></div>")

    # ---- 新着（折り畳み・同フォーマット）----
    H.append(f"<h2 class='sec open new' data-target='secNew'>🆕 新着</h2>")
    H.append("<div id='secNew' class='secbody open'><table id='newTbl'></table></div>")

    # ---- お気に入り（折り畳み・JS描画）----
    H.append("<h2 class='sec fav' data-target='secFav'>⭐ お気に入り <span id='favCnt'></span></h2>")
    H.append("<div id='secFav' class='secbody'><table id='favTbl'></table></div>")

    # ---- NGエリア該当ログ（折り畳み・既定閉・静的）----
    H.append(f"<h2 class='sec excl' data-target='secNg'>NGエリア該当ログ（{len(ng_log)} 件）</h2>")
    H.append("<div id='secNg' class='secbody'>")
    if ng_log:
        H.append("<table><tr><th>サイト</th><th>種別</th><th>所在地</th><th>価格</th><th>土地面積</th>"
                 "<th>NGエリア</th><th>詳細</th></tr>")
        for sname, p in ng_log:
            H.append(
                f"<tr><td>{escape(sname)}</td><td>{escape(p.get('shubetsu','—'))}</td>"
                f"<td>{escape(_short_loc(p))}</td>"
                f"<td>{_fmt_price(p)}</td><td>{_fmt_area(p)}</td>"
                f"<td class='flag'>{escape('、'.join(p.get('ng_areas', [])))}</td>"
                f"<td><a href='{escape(p['url'])}' target='_blank'>詳細</a></td></tr>")
        H.append("</table>")
    else:
        H.append("<p class='muted'>NGエリア該当なし。</p>")
    H.append("</div>")

    # ---- 非表示にした物件（折り畳み・既定閉・JS描画）----
    H.append("<h2 class='sec' data-target='secHidden'>非表示にした物件 <span id='hiddenCnt'></span></h2>")
    H.append("<div id='secHidden' class='secbody'><table id='hiddenTbl'></table></div>")

    # ---- 消滅（折り畳み・既定閉・静的）----
    H.append(f"<h2 class='sec gone' data-target='secGone'>消滅（{len(disappeared)} 件・ページ削除から7日以内）</h2>")
    H.append("<div id='secGone' class='secbody'>")
    if disappeared:
        H.append("<table><tr><th>サイト</th><th>所在地</th><th>価格</th><th>面積</th>"
                 "<th>消滅検出日</th><th>経過</th><th>詳細</th></tr>")
        for sname, a, d in sorted(disappeared, key=lambda x: x[2]):
            loc = (a.get("location") or a.get("text") or "").replace("静岡県", "")[:20]
            price = f"{a['price_man']:,}万円" if a.get("price_man") is not None else "—"
            area = f"{a['area_sqm']:g}㎡" if a.get("area_sqm") is not None else "—"
            H.append(
                f"<tr><td>{escape(sname)}</td><td>{escape(loc or '—')}</td>"
                f"<td>{price}</td><td>{area}</td><td>{escape(a.get('removed_on', '—'))}</td>"
                f"<td>{d}日前</td>"
                f"<td><a href='{escape(a.get('url', ''))}' target='_blank'>詳細</a></td></tr>")
        H.append("</table>")
    else:
        H.append("<p class='muted'>消滅物件なし。</p>")
    H.append("</div>")

    # ---- サイト別サマリ（静的・最下部。毎回見る情報ではないメタ情報）----
    H.append("<h2 class='sec' data-target='secSummary'>サイト別サマリ（取得状況のメタ情報）</h2>")
    H.append("<div id='secSummary' class='secbody'>")
    H.append("<table><tr><th>ID</th><th>サイト名</th><th>HTTP</th><th>方式</th><th>抽出</th>"
             "<th>価格取得</th><th>面積取得</th><th>基準内(参考)</th><th>新着</th><th>NG該当</th>"
             "<th>status</th><th>備考</th></tr>")
    for r in results:
        H.append(
            f"<tr><td>{escape(r['id'])}</td><td>{escape(r['name'])}</td>"
            f"<td>{escape(str(r['http']))}</td><td>{escape(r.get('mode',''))}</td><td>{r['raw']}</td>"
            f"<td>{r['price_cnt']}</td><td>{r['area_cnt']}</td>"
            f"<td class='num-fit'>{r['fit_cnt']}</td>"
            f"<td class='num-new'>{r['added_cnt']}</td><td>{r['ng_cnt']}</td>"
            f"<td>{escape(r['yaml_status'])}</td><td>{escape(r['note'])}</td></tr>")
    H.append("</table></div>")

    H.append(disclaimer)

    # 列フィルタ用ポップアップ
    H.append("<div id='popup'></div>")

    # 非表示確認モーダル（JSで .open クラスを付与して表示）。
    # ※ スクリプトはトップレベルで getElementById('hideConfirm') 等を参照するため、
    #   モーダル DOM は必ず <script> より前に配置すること。
    H.append("<div id='hideModal'>"
             "<div id='hideModalBox'>"
             "<h3>この物件を非表示にしますか？</h3>"
             "<p class='hide-target' id='hideTarget'></p>"
             "<p>非表示にした物件は下の「非表示にした物件」からいつでも戻せます。</p>"
             "<div class='modal-btns'>"
             "<button id='hideCancel'>キャンセル</button>"
             "<button id='hideConfirm'>非表示にする</button>"
             "</div></div></div>")

    H.append("<script>")
    H.append("const DATA=" + data_json + ";")
    H.append("const CONFIG=" + config_js + ";")
    H.append(_FILTER_JS)
    H.append("</script>")
    H.append("</body></html>")
    return "\n".join(H)


_REPORT_CSS = (
    # background 明示: 未指定だとダークモード端末で透過→黒背景になり表が読めない
    "body{font-family:'Segoe UI','Meiryo',sans-serif;margin:0 16px 40px;color:#222;font-size:13px;"
    "background:#fff;}"
    ".topbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;"
    "position:sticky;top:0;background:#fff;border-bottom:1px solid #ddd;padding:6px 0;z-index:30;}"
    "h1{font-size:18px;margin:4px 0;}.topctl{font-size:13px;}.topctl>*{margin-left:8px;}"
    "h2{font-size:15px;margin:18px 0 0;border-left:5px solid #2b7;padding:4px 8px;}"
    "h2.sec{cursor:pointer;background:#f3f7f4;}h2.sec:hover{background:#e7f0ea;}"
    "h2.sec::before{content:'\\25b6 ';font-size:11px;color:#666;}h2.sec.open::before{content:'\\25bc ';}"
    "h2.static{cursor:default;}h2.new{border-left-color:#d00;}h2.excl{border-left-color:#c0392b;}"
    "h2.fav{border-left-color:#e0a800;}h2.gone{border-left-color:#888;}"
    ".secbody{display:none;}.secbody.open{display:block;}"
    "table{border-collapse:collapse;width:100%;margin-top:6px;}"
    "th,td{border:1px solid #ccc;padding:2px 5px;font-size:12px;text-align:left;white-space:nowrap;}"
    "th{background:#f0f3f5;}th.col{cursor:pointer;user-select:none;}th.col:hover{background:#e3eaf0;}"
    "th.filtered{background:#dcebff;}.ar{font-size:10px;color:#06c;}.fi{color:#c0392b;}"
    "tbody tr:nth-child(even){background:#fafbfc;}.hitrow td{box-shadow:inset 0 0 0 9999px rgba(46,158,79,.10);}"
    ".hitrow td:first-child{border-left:3px solid #2e9e4f;}"
    ".cond{background:#eef7f0;border:1px solid #bcdcc6;padding:8px 12px;border-radius:6px;}"
    ".cond div{margin:1px 0;}"
    ".cond .lbl{display:inline-block;min-width:9em;font-weight:bold;}"
    ".lbl-plus{color:#0a7d2c;}.lbl-minus{color:#c0392b;}"
    ".muted{color:#888;}a{color:#1565c0;}.flag{color:#b25b00;}.num-fit{color:#c0392b;}.num-new{color:#d00;}"
    ".rb-ok{color:#0a7d2c;font-weight:bold;}.rb-wn{color:#b8860b;font-weight:bold;}"
    ".rb-ng{color:#c0392b;font-weight:bold;}.rb-uk{color:#888;}"
    "select,input,button{font-size:13px;padding:2px;}.note{font-size:11px;color:#666;margin-top:14px;}"
    ".panel{background:#f7f9fb;border:1px solid #cdd7df;border-radius:0 6px 6px 6px;padding:8px 12px;margin-top:0;}"
    ".panel label{margin-right:8px;white-space:nowrap;}.panel input[type=number]{width:60px;}"
    ".cnt{font-weight:bold;font-size:15px;color:#06c;}"
    ".help{display:inline-block;width:15px;height:15px;line-height:15px;text-align:center;border-radius:50%;"
    "background:#9aa;color:#fff;font-size:11px;cursor:pointer;position:relative;margin-left:3px;font-weight:normal;}"
    ".help .tip{display:none;position:absolute;left:19px;top:-4px;width:230px;background:#333;color:#fff;"
    "padding:6px 9px;border-radius:5px;font-size:11px;font-weight:normal;z-index:40;white-space:normal;line-height:1.4;}"
    ".help:hover .tip,.help.show .tip{display:block;}"
    "#popup{display:none;position:absolute;z-index:50;background:#fff;border:1px solid #888;border-radius:5px;"
    "box-shadow:0 3px 10px rgba(0,0,0,.25);padding:8px;font-size:12px;min-width:150px;}"
    "#popup .pr{margin:3px 0;}#popup button{margin:2px 3px 0 0;}#popup label{display:block;}"
    ".legendrow td{background:#fff;border:none;font-size:11px;color:#555;padding-top:6px;white-space:normal;}"
    ".heatleg b{display:inline-block;padding:1px 6px;margin:0 1px;border-radius:3px;}"
    ".hidebtn{cursor:pointer;color:#c0392b;border:1px solid #e0b4b4;border-radius:3px;background:#fff;font-weight:bold;padding:1px 6px;}"
    ".restorebtn{cursor:pointer;color:#0a7d2c;border:1px solid #b4e0bf;border-radius:3px;background:#fff;}"
    ".princ{font-size:11px;color:#444;background:#fff;border:1px dashed #cfd8dc;padding:3px 7px;border-radius:4px;}"
    ".prow{margin-top:7px;}"
    "#areaPop .pr{margin:3px 0;}"
    ".bi,.bc,.bz{display:inline-block;font-size:10px;padding:0 4px;margin-left:3px;border-radius:3px;line-height:1.4;}"
    ".bi{background:#e3f3e6;color:#0a7d2c;}.bc{background:#fde3e3;color:#c0392b;}.bz{background:#f3e3fd;color:#7b2fb5;}"
    ".chip{display:inline-block;background:#eef;border:1px solid #ccd;border-radius:10px;padding:0 4px 0 7px;"
    "margin:2px 3px 0 0;font-size:11px;}.chip b{cursor:pointer;color:#c0392b;margin-left:4px;}"
    "#filterToggle{display:none;font-size:14px;padding:5px 12px;margin-top:8px;}"
    "#areaPop{display:none;position:absolute;z-index:50;background:#fff;border:1px solid #888;border-radius:5px;"
    "box-shadow:0 3px 10px rgba(0,0,0,.25);padding:8px;font-size:12px;min-width:180px;max-width:300px;}"
    "#areaPop label{display:inline-block;}#areaList .arow{margin:2px 0;}#areaList .delx{cursor:pointer;color:#c0392b;margin-left:6px;}"
    ".loccell{white-space:nowrap;}.infocell{white-space:normal;max-width:220px;}"
    ".secbody{overflow-x:auto;-webkit-overflow-scrolling:touch;}"
    # ---- タブ ----
    ".tabs{display:flex;margin:10px 0 0;border-bottom:3px solid #ddd;}"
    ".tab-btn{padding:9px 24px;font-size:14px;font-weight:bold;cursor:pointer;"
    "border:2px solid transparent;border-bottom:none;border-radius:6px 6px 0 0;"
    "background:#f7f7f7;margin-right:3px;transition:background .12s;}"
    ".tab-btn[data-tab=sarachi]{border-color:#2a8a4a;color:#2a8a4a;}"
    ".tab-btn[data-tab=sarachi]:not(.active):hover{background:#e8f5ee;}"
    ".tab-btn[data-tab=sarachi].active{background:#2a8a4a;color:#fff;}"
    ".tab-btn[data-tab=ie]{border-color:#c07030;color:#c07030;}"
    ".tab-btn[data-tab=ie]:not(.active):hover{background:#fdf0e8;}"
    ".tab-btn[data-tab=ie].active{background:#c07030;color:#fff;}"
    ".tab-btn[data-tab=camp]{border-color:#3a6ea5;color:#3a6ea5;}"
    ".tab-btn[data-tab=camp]:not(.active):hover{background:#eaf1f8;}"
    ".tab-btn[data-tab=camp].active{background:#3a6ea5;color:#fff;}"
    # ---- パネルタイトル & フィルタブロック ----
    ".panel-title{font-size:13px;margin-bottom:8px;"
    "display:flex;justify-content:space-between;align-items:center;}"
    ".panel-title button{font-size:11px;padding:2px 8px;}"
    ".filter-block{margin-top:8px;}"
    ".fb-head{margin-bottom:3px;}.fb-head .filter-label{font-weight:bold;}"
    ".fb-line{display:flex;align-items:center;gap:4px;margin:3px 0;font-size:12px;}"
    ".fb-line .mm{display:inline-block;min-width:5em;color:#555;}"
    ".fb-line input[type=number]{width:74px;}"
    ".sqm-note{color:#888;font-size:11px;margin-left:4px;}"
    # ---- 参考情報 (details) ----
    ".refbox{margin:8px 0;}.refbox summary{cursor:pointer;font-size:12px;color:#555;"
    "font-weight:bold;padding:4px 2px;list-style:none;}"
    ".refbox summary::before{content:'\\25b6  ';font-size:10px;}"
    ".refbox[open] summary::before{content:'\\25bc  ';}"
    ".refbox summary:hover{color:#333;}"
    ".refbox .cond{margin-top:4px;}"
    # ---- 非表示確認モーダル ----
    "#hideModal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:100;"
    "justify-content:center;align-items:center;}"
    "#hideModal.open{display:flex;}"
    "#hideModalBox{background:#fff;border-radius:8px;padding:20px 24px;max-width:340px;width:90%;"
    "box-shadow:0 4px 20px rgba(0,0,0,.3);}"
    "#hideModalBox h3{margin:0 0 8px;font-size:15px;color:#c0392b;}"
    "#hideModalBox p{font-size:12px;color:#555;margin:0 0 14px;line-height:1.5;}"
    "#hideModalBox p.hide-target{font-weight:bold;color:#222;background:#f3f4f6;"
    "border-radius:5px;padding:8px 10px;margin:0 0 12px;}"
    ".modal-btns{display:flex;gap:8px;justify-content:flex-end;}"
    ".modal-btns button{font-size:13px;padding:6px 16px;border-radius:4px;cursor:pointer;border:1px solid;}"
    "#hideConfirm{background:#c0392b;color:#fff;border-color:#c0392b;}"
    "#hideCancel{background:#fff;color:#444;border-color:#ccc;}"
    # ---- お気に入り★ボタン ----
    ".favbtn{cursor:pointer;border:none;background:none;font-size:16px;line-height:1;"
    "color:#ccc;padding:0 2px;}.favbtn.on{color:#f0a500;}"
    ".favcell{text-align:center;}"
    # ---- ヒートマップ凡例（改行・本文と同サイズ）----
    ".legendrow td{font-size:11px;}"
    ".legendrow .lgline{display:block;margin:2px 0;white-space:normal;}"
    ".legendrow .lgline b{display:inline-block;padding:1px 6px;margin:0 1px;border-radius:3px;font-weight:normal;}"
    # ---- モバイル対応 ----
    "@media(max-width:700px){"
    "body{margin:0 8px 40px;}.topbar{flex-direction:column;align-items:flex-start;}"
    ".topctl{margin-top:4px;}#filterToggle{display:inline-block;}"
    ".panel{display:none;}.panel.open{display:block;}"
    ".tab-btn{padding:7px 16px;font-size:13px;}"
    "h1{font-size:16px;}th,td{font-size:11px;padding:2px 4px;}.infocell{max-width:140px;}"
    ".fb-line input[type=number]{width:64px;}}"
)


_FILTER_JS = r"""
const TYPES=CONFIG.types, MACHI=CONFIG.machi;
const CHIMOKU_OPTS=[...new Set(DATA.map(d=>d.chimoku||'—'))].sort();
const HOUSE_TYPES=new Set(['空き家','古家付き土地','中古戸建']);
const COLS=[
 {k:'price',l:'価格'},
 {k:'area',l:'面積'},
 {k:'tsubo',l:'坪単価',f:'range'},
 {k:'shubetsu',l:'種別',f:'check',opts:TYPES},
 {k:'rb',l:'建築可否',f:'check',opts:['○','△','×','不明']},
 {k:'loc',l:'所在地'},
 {k:'machi',l:'市町',f:'check',opts:MACHI},
 {k:'chimoku',l:'地目',f:'check',opts:CHIMOKU_OPTS},
 {k:'first_seen',l:'検出日'},
 {k:'info',l:'参考情報',nostat:true}
];
// 先頭=お気に入り★、末尾=詳細リンク＋非表示ボタン の計3列を加える
const NCOL=COLS.length+3;
function esc(s){s=(s==null?'':String(s));return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function numOrNull(v){v=(''+(v==null?'':v)).trim();return v===''?null:parseFloat(v);}
function hPrice(v){if(v==null)return'';if(v<=300)return'background:#1a7d36;color:#fff';if(v<=600)return'background:#66bb6a';if(v<=1000)return'background:#ffe082';if(v<=2000)return'background:#ffb74d';return'background:#ef9a9a';}
function hArea(v){if(v==null)return'';if(v>=990)return'background:#1a7d36;color:#fff';if(v>=660)return'background:#66bb6a';if(v>=495)return'background:#ffe082';if(v>=330)return'background:#ffb74d';return'background:#ef9a9a';}
function hTsubo(v){if(v==null)return'';if(v<=2)return'background:#1a7d36;color:#fff';if(v<=5)return'background:#66bb6a';if(v<=10)return'background:#ffe082';if(v<=20)return'background:#ffb74d';return'background:#ef9a9a';}
function rbClass(m){return {'○':'rb-ok','△':'rb-wn','×':'rb-ng'}[m]||'rb-uk';}
function normLoc(s){return (s||'').replace('静岡県','').replace(/\s+/g,'');}

// ---- 状態 ----
// キャンプ場土地(camp)タブは既定で価格・面積を絞らない（手広く構えて目で選ぶ方針）
function defState(){return{tab:'sarachi',priceMin:0,priceMaxSarachi:1500,priceMaxIe:3000,priceMaxCamp:null,amin:CONFIG.amin,amax:null,aminCamp:null,amaxCamp:null,cf:{},sort:{k:null,d:1}};}
let S=defState();

// ---- localStorage (akiyawatch_ プレフィックス) ----
const LS_TAB='akiyawatch_tab', LS_PRICE='akiyawatch_price';
const LS_AREA_FILTER='akiyawatch_area', LS_HIDDEN='akiyawatch_hidden';
const LS_FAV='akiyawatch_fav', LS_EXAREAS='akiyawatch_exareas';
const LS_OLD_EXAREAS='akiya.exareas.v2';
function lsGet(k,def){try{const v=JSON.parse(localStorage.getItem(k));return v==null?def:v;}catch(e){return def;}}
function lsSave(k,v){try{localStorage.setItem(k,JSON.stringify(v));}catch(e){}}

// 除外エリア: 新キーになければ旧キー(akiya.exareas.v2)を移行
let EXAREAS=(()=>{
  const nv=localStorage.getItem(LS_EXAREAS);
  if(nv!=null){try{return JSON.parse(nv);}catch(e){}}
  const ov=localStorage.getItem(LS_OLD_EXAREAS);
  if(ov!=null){try{const v=JSON.parse(ov);lsSave(LS_EXAREAS,v);return v;}catch(e){}}
  return (CONFIG.exareas||[]).map(n=>({name:n,on:true}));
})();

// 非表示／お気に入りは Map<bdk, snapshot>。物件が今日のデータから消えても
// snapshot で表示し続け、物件自体が消滅するまでリストに残す。
// 旧形式(bdk文字列の配列)は snapshot=null として移行読込する。
function loadKV(key){
  const raw=lsGet(key,[]); const m=new Map();
  (raw||[]).forEach(e=>{
    if(typeof e==='string')m.set(e,null);
    else if(e&&e.bdk)m.set(e.bdk,e.snap||null);
  });
  return m;
}
function saveKV(key,map){lsSave(key,[...map.entries()].map(([bdk,snap])=>({bdk,snap})));}
let HIDDEN=loadKV(LS_HIDDEN);
let FAVOR=loadKV(LS_FAV);

function saveState(){
  lsSave(LS_TAB,S.tab);
  lsSave(LS_PRICE,{min:S.priceMin,maxSarachi:S.priceMaxSarachi,maxIe:S.priceMaxIe,maxCamp:S.priceMaxCamp});
  lsSave(LS_AREA_FILTER,{min:S.amin,max:S.amax,minCamp:S.aminCamp,maxCamp:S.amaxCamp});
}
function saveHidden(){saveKV(LS_HIDDEN,HIDDEN);}
function saveFav(){saveKV(LS_FAV,FAVOR);}
function saveAreas(){lsSave(LS_EXAREAS,EXAREAS);}

// bdk から表示用スナップショットを作る（レコードを浅くコピー）
function snapOf(rep){
  const keys=['url','dk','loc','price','area','tsubo','shubetsu','shubetsu_reason',
    'rb','rbreason','setsudo','machi','chimoku','first_seen','site','interests','cautions','zokujin','tab'];
  const o={}; keys.forEach(k=>o[k]=rep[k]); return o;
}
function bdkOf(g){return g.rep.dk||g.dk;}
// Map(bdk->snap) を描画用グループ配列に。現存グループがあれば最新を使い、無ければ snapshot を使う。
function groupsFromMap(map){
  const out=[];
  map.forEach((snap,bdk)=>{
    const live=GROUPS.find(g=>bdkOf(g)===bdk);
    if(live)out.push(live);
    else if(snap)out.push({dk:bdk,rep:snap,sites:[snap.site||'—'],added:false,gone:true});
  });
  return out;
}

function restoreState(){
  const tab=lsGet(LS_TAB,null);
  if(tab==='sarachi'||tab==='ie'||tab==='camp')S.tab=tab;
  const price=lsGet(LS_PRICE,null);
  if(price){if(price.min!=null)S.priceMin=price.min;if(price.maxSarachi!=null)S.priceMaxSarachi=price.maxSarachi;if(price.maxIe!=null)S.priceMaxIe=price.maxIe;if(price.maxCamp!==undefined)S.priceMaxCamp=price.maxCamp;}
  const area=lsGet(LS_AREA_FILTER,null);
  if(area){S.amin=(area.min!=null?area.min:S.amin);S.amax=(area.max!=null?area.max:S.amax);
    if(area.minCamp!==undefined)S.aminCamp=area.minCamp;if(area.maxCamp!==undefined)S.amaxCamp=area.maxCamp;}
}

// ---- グループ化（JS側dedup: normLoc+面積+価格）----
const GROUPS=[],KIDX={};
DATA.forEach((d,i)=>{
  d._dk = d.loc ? (normLoc(d.loc)+'|'+d.area+'|'+d.price) : ('u'+i);
  if(KIDX[d._dk]===undefined){KIDX[d._dk]=GROUPS.length;GROUPS.push({dk:d._dk,rep:d,sites:[d.site],added:!!d.added});}
  else{const g=GROUPS[KIDX[d._dk]];if(!g.sites.includes(d.site))g.sites.push(d.site);if(d.added)g.added=true;}
});

// ---- フィルタ ----
function passFilters(d){
  // キャンプ場土地(camp)は独立タブ: campレコードはcampタブのみ、homeレコードは更地/家付きのみ
  if(S.tab==='camp'){
    if(d.tab!=='camp')return false;
  }else{
    if(d.tab==='camp')return false;
    const isHouse=HOUSE_TYPES.has(d.shubetsu);
    if(S.tab==='sarachi'&&isHouse)return false;
    if(S.tab==='ie'&&!isHouse)return false;
  }
  const loc=d.loc||'';
  if(EXAREAS.some(a=>a.on&&a.name&&loc.includes(a.name)))return false;
  for(const k in S.cf){const cf=S.cf[k],v=d[k];
    if(cf.t==='range'){if(v==null)return false;if(cf.min!=null&&v<cf.min)return false;if(cf.max!=null&&v>cf.max)return false;}
    else if(cf.t==='check'){if(cf.set&&!cf.set.includes(String(v==null?'—':v)))return false;}
  }
  const pmax=S.tab==='sarachi'?S.priceMaxSarachi:(S.tab==='camp'?S.priceMaxCamp:S.priceMaxIe);
  const amin=S.tab==='camp'?S.aminCamp:S.amin;
  const amax=S.tab==='camp'?S.amaxCamp:S.amax;
  if(S.priceMin!=null&&S.priceMin>0&&(d.price==null||d.price<S.priceMin))return false;
  if(pmax!=null&&(d.price==null||d.price>pmax))return false;
  if(amin!=null&&(d.area==null||d.area<amin))return false;
  if(amax!=null&&(d.area==null||d.area>amax))return false;
  return true;
}

// ---- テーブル描画 ----
function buildHead(){
  let h='<thead><tr><th class=favcell title="お気に入り">★</th>';
  COLS.forEach(c=>{
    if(c.nostat){h+="<th>"+esc(c.l)+"</th>";return;}
    const active=S.cf[c.k]?' filtered':'';
    const ar=(S.sort.k===c.k)?(S.sort.d>0?'▲':'▼'):'';
    const fi=(c.f&&S.cf[c.k])?' <span class=fi>⚑</span>':'';
    h+="<th class='col"+active+"' data-k='"+c.k+"'>"+esc(c.l)+" <span class=ar>"+ar+"</span>"+fi+"</th>";
  });
  return h+'<th>詳細</th><th>非表示</th></tr></thead>';
}
function rowHtml(g,inHidden){
  const d=g.rep;
  const bdk=d.dk||g.dk;
  const price=(d.price==null)?'—':d.price.toLocaleString()+'万';
  const area=(d.area==null)?'—':d.area+'㎡';
  const tsubo=(d.tsubo==null)?'—':d.tsubo;
  const rbTitle=d.rbreason+(d.setsudo?(' / 接道:'+d.setsudo):'');
  let loc=esc(normLoc(d.loc).slice(0,22)||'—');
  if(g.sites.length>1){const o=g.sites.filter(x=>x!=d.site);loc+=" <span class=muted>他"+o.length+"件("+esc(o.join('/'))+")</span>";}
  let info='';
  (d.interests||[]).forEach(x=>info+="<span class=bi>"+esc(x)+"</span>");
  (d.cautions||[]).forEach(x=>info+="<span class=bc>"+esc(x)+"</span>");
  if(d.zokujin)info+="<span class=bz>属人性</span>";
  if(!info)info='<span class=muted>—</span>';
  const op=inHidden?("<button class=restorebtn data-bdk='"+esc(bdk)+"'>戻す</button>")
                   :("<button class=hidebtn data-bdk='"+esc(bdk)+"'>非表示</button>");
  const isFav=FAVOR.has(bdk);
  const fav="<td class=favcell><button class='favbtn"+(isFav?' on':'')+"' data-bdk='"+esc(bdk)+"' title='お気に入り'>"+(isFav?'★':'☆')+"</button></td>";
  return "<tr>"
    +fav
    +"<td style='"+hPrice(d.price)+"'>"+price+"</td>"
    +"<td style='"+hArea(d.area)+"'>"+area+"</td>"
    +"<td style='"+hTsubo(d.tsubo)+"'>"+tsubo+"</td>"
    +"<td title='"+esc(d.shubetsu_reason)+"'>"+esc(d.shubetsu)+"</td>"
    +"<td class='"+rbClass(d.rb)+"' title='"+esc(rbTitle)+"'>"+esc(d.rb)+"</td>"
    +"<td class='loccell'>"+loc+"</td>"
    +"<td>"+esc(d.machi||'—')+"</td>"
    +"<td>"+esc(d.chimoku)+"</td>"
    +"<td>"+esc(d.first_seen||'—')+"</td>"
    +"<td class='infocell'>"+info+"</td>"
    +"<td><a href='"+esc(d.url)+"' target=_blank>詳細</a></td>"
    +"<td>"+op+"</td></tr>";
}
function legendRow(){
  return "<tfoot><tr class=legendrow><td colspan="+NCOL+">"
    +"<span class=lgline>価格(安いほど濃い緑):<b style='background:#1a7d36;color:#fff'>≤300</b><b style='background:#66bb6a'>≤600</b><b style='background:#ffe082'>≤1000</b><b style='background:#ffb74d'>≤2000</b><b style='background:#ef9a9a'>&gt;2000</b> 万円</span>"
    +"<span class=lgline>面積(広いほど濃い緑):<b style='background:#1a7d36;color:#fff'>≥990</b><b style='background:#66bb6a'>≥660</b><b style='background:#ffe082'>≥495</b><b style='background:#ffb74d'>≥330</b> ㎡</span>"
    +"<span class=lgline>坪単価(安いほど濃い緑):<b style='background:#1a7d36;color:#fff'>≤2</b><b style='background:#66bb6a'>≤5</b><b style='background:#ffe082'>≤10</b><b style='background:#ffb74d'>≤20</b><b style='background:#ef9a9a'>&gt;20</b> 万円/坪</span>"
    +"<span class=lgline>参考情報: <span class=bi>緑=好材料</span> <span class=bc>赤=注意点</span> <span class=bz>属人性</span></span>"
    +"</td></tr></tfoot>";
}
function sortGroups(list){
  if(S.sort.k){const k=S.sort.k,dir=S.sort.d;
    list.sort((A,B)=>{let av=A.rep[k],bv=B.rep[k];if(av==null&&bv==null)return 0;if(av==null)return 1;if(bv==null)return -1;
      if(typeof av==='number'&&typeof bv==='number')return(av-bv)*dir;return String(av).localeCompare(String(bv),'ja')*dir;});
  } else list.sort((A,B)=>(((A.rep.price==null)?1e12:A.rep.price)-((B.rep.price==null)?1e12:B.rep.price)));
  return list;
}
function tbl(list,inHidden,showLegend){return buildHead()+'<tbody>'+(list.length?list.map(g=>rowHtml(g,inHidden)).join(''):"<tr><td colspan="+NCOL+" class=muted>該当なし</td></tr>")+'</tbody>'+(showLegend?legendRow():'');}
function render(){
  let vis=GROUPS.filter(g=>{const bdk=bdkOf(g);return !HIDDEN.has(bdk)&&passFilters(g.rep);});
  sortGroups(vis);
  // 物件ブラウザ（凡例つき）
  document.getElementById('mainTbl').innerHTML=tbl(vis,false,true);
  // 新着（凡例なし＝上と重複のため）
  const nv=vis.filter(g=>g.added);
  document.getElementById('newTbl').innerHTML=tbl(nv,false,false);
  // お気に入り（消滅まで残す。除外エリア等の絞り込みは無視して常に表示）
  const fav=sortGroups(groupsFromMap(FAVOR));
  document.getElementById('favTbl').innerHTML=tbl(fav,false,false);
  const favC=document.getElementById('favCnt');if(favC)favC.textContent='('+fav.length+'件)';
  // 非表示（消滅まで残す）
  const hid=sortGroups(groupsFromMap(HIDDEN));
  document.getElementById('hiddenTbl').innerHTML=tbl(hid,true,false);
  document.getElementById('hiddenCnt').textContent='('+hid.length+'件)';
  // 件数表示（内部用語"グループ"を使わない）
  const msg='該当 '+vis.length+'件（全'+GROUPS.length+'件中）・新着 '+nv.length+'件';
  document.getElementById('cnt').textContent=msg;
  const ct=document.getElementById('cntTop');if(ct)ct.textContent='該当 '+vis.length+'件・新着 '+nv.length+'件';
  updateTabUI();
}

// ---- タブ ----
function updateTabUI(){
  document.querySelectorAll('.tab-btn').forEach(btn=>{btn.classList.toggle('active',btn.dataset.tab===S.tab);});
}

// ---- 坪㎡ 双方向換算 ----
const TSUBO_PER_SQM=3.305785;
function sqmToTsubo(v){return v==null?'':(v/TSUBO_PER_SQM).toFixed(1);}
function tsuboToSqm(v){return v==null?'':(v*TSUBO_PER_SQM).toFixed(1);}

// ---- コントロールへの状態反映 ----
// 面積は坪を入力欄（編集可）、㎡は読取専用の参考表示（小数1桁）。内部Sはsqmで保持。
function applyStateToControls(){
  const pmin=document.getElementById('priceMinInput');
  const pmax=document.getElementById('priceMaxInput');
  if(pmin)pmin.value=(S.priceMin==null||S.priceMin===0)?'':S.priceMin;
  if(pmax)pmax.value=(S.tab==='sarachi'?S.priceMaxSarachi:(S.tab==='camp'?S.priceMaxCamp:S.priceMaxIe))||'';
  const camp=(S.tab==='camp');
  const amin=camp?S.aminCamp:S.amin, amax=camp?S.amaxCamp:S.amax;
  const aminTsubo=document.getElementById('aminTsuboInput');
  const amaxTsubo=document.getElementById('amaxTsuboInput');
  const aminSqmView=document.getElementById('aminSqmView');
  const amaxSqmView=document.getElementById('amaxSqmView');
  if(aminTsubo)aminTsubo.value=(amin==null?'':sqmToTsubo(amin));
  if(amaxTsubo)amaxTsubo.value=(amax==null?'':sqmToTsubo(amax));
  if(aminSqmView)aminSqmView.textContent=(amin==null?'—':(+amin).toFixed(1));
  if(amaxSqmView)amaxSqmView.textContent=(amax==null?'—':(+amax).toFixed(1));
}

// ---- 除外エリアリスト描画 ----
function renderAreaList(){
  const box=document.getElementById('areaList'); if(!box)return;
  box.innerHTML=EXAREAS.length?EXAREAS.map(a=>
    "<div class=arow><label><input type=checkbox class=areachk data-name='"+esc(a.name)+"' "+(a.on?'checked':'')+"> "+esc(a.name)+"</label><b class=delx data-name='"+esc(a.name)+"'>×</b></div>"
  ).join(''):"<div class=muted>（除外エリアなし）</div>";
}

// ---- 列ヘッダ ポップアップ ----
function closePopup(){document.getElementById('popup').style.display='none';}
function openPopup(th){
  const k=th.dataset.k, col=COLS.find(c=>c.k===k), pop=document.getElementById('popup');
  let h="<div class=pr><b>"+esc(col.l)+"</b></div>"
       +"<div class=pr><button data-act=sa>▲ 昇順</button><button data-act=sd>▼ 降順</button></div>";
  if(col.f==='range'){const cf=S.cf[k]||{};
    h+="<div class=pr>下限 <input id=fmin type=number style='width:80px' value='"+(cf.min==null?'':cf.min)+"'></div>";
    h+="<div class=pr>上限 <input id=fmax type=number style='width:80px' value='"+(cf.max==null?'':cf.max)+"'></div>";
    h+="<div class='pr muted' style='white-space:normal;max-width:180px'>※その場の範囲絞り込み。価格・面積パネルフィルタとは別に効きます。</div>";
  } else if(col.f==='check'){const cf=S.cf[k]; const set=(cf&&cf.set)?cf.set:col.opts.slice();
    h+=col.opts.map(o=>"<label><input type=checkbox class=fchk value='"+esc(o)+"' "+(set.includes(o)?'checked':'')+"> "+esc(o)+"</label>").join('');
  }
  h+="<div class=pr><button data-act=apply>適用</button><button data-act=clear>解除</button><button data-act=close>閉じる</button></div>";
  pop.innerHTML=h; pop.dataset.k=k; pop.dataset.f=col.f||'';
  const r=th.getBoundingClientRect();
  pop.style.left=(window.scrollX+r.left)+'px'; pop.style.top=(window.scrollY+r.bottom+2)+'px'; pop.style.display='block';
}
document.getElementById('popup').addEventListener('click',e=>{
  const act=e.target.dataset.act; if(!act)return; e.stopPropagation();
  const pop=document.getElementById('popup'), k=pop.dataset.k, f=pop.dataset.f;
  if(act==='sa')S.sort={k:k,d:1};
  else if(act==='sd')S.sort={k:k,d:-1};
  else if(act==='apply'){
    if(f==='range'){const mn=numOrNull(document.getElementById('fmin').value),mx=numOrNull(document.getElementById('fmax').value);
      if(mn==null&&mx==null)delete S.cf[k]; else S.cf[k]={t:'range',min:mn,max:mx};}
    else if(f==='check'){const set=[...document.querySelectorAll('#popup .fchk:checked')].map(c=>c.value);
      const all=COLS.find(c=>c.k===k).opts; if(set.length===all.length)delete S.cf[k]; else S.cf[k]={t:'check',set:set};}
    closePopup();
  } else if(act==='clear'){delete S.cf[k]; if(S.sort.k===k)S.sort={k:null,d:1}; closePopup();}
  else if(act==='close'){closePopup(); return;}
  render();
});

// ---- bdk から代表レコードを引く（現存→お気に入り→非表示の順）----
function repByBdk(bdk){
  const g=GROUPS.find(g=>bdkOf(g)===bdk);
  if(g)return g.rep;
  return FAVOR.get(bdk)||HIDDEN.get(bdk)||{dk:bdk,loc:'',price:null,area:null};
}

// ---- 非表示確認モーダル（対象の所在地を表示して押し間違いを防ぐ）----
let _pendingHide=null;
function openHideModal(rep){
  _pendingHide={bdk:(rep.dk),snap:snapOf(rep)};
  const loc=normLoc(rep.loc||'').slice(0,40)||'(所在地不明)';
  const price=(rep.price==null)?'価格不明':rep.price.toLocaleString()+'万円';
  const area=(rep.area==null)?'':(' / '+rep.area+'㎡');
  document.getElementById('hideTarget').textContent=loc+'（'+price+area+'）';
  document.getElementById('hideModal').classList.add('open');
}
function closeHideModal(){document.getElementById('hideModal').classList.remove('open');_pendingHide=null;}
document.getElementById('hideConfirm').addEventListener('click',()=>{
  if(_pendingHide){HIDDEN.set(_pendingHide.bdk,_pendingHide.snap);saveHidden();render();}
  closeHideModal();
});
document.getElementById('hideCancel').addEventListener('click',closeHideModal);
document.getElementById('hideModal').addEventListener('click',e=>{if(e.target===document.getElementById('hideModal'))closeHideModal();});

// ---- 初期化＆イベント ----
(function init(){
  restoreState();
  applyStateToControls();
  updateTabUI();

  document.querySelectorAll('.tab-btn').forEach(btn=>{
    btn.addEventListener('click',()=>{S.tab=btn.dataset.tab;saveState();applyStateToControls();render();});
  });

  document.getElementById('priceMinInput').addEventListener('input',e=>{S.priceMin=numOrNull(e.target.value);saveState();render();});
  document.getElementById('priceMaxInput').addEventListener('input',e=>{
    if(S.tab==='sarachi')S.priceMaxSarachi=numOrNull(e.target.value);
    else if(S.tab==='camp')S.priceMaxCamp=numOrNull(e.target.value);
    else S.priceMaxIe=numOrNull(e.target.value);
    saveState();render();});

  // 面積は坪で入力。㎡は読取専用の参考表示(span)を更新。内部Sはsqmで保持。
  // campタブは独立の下限/上限（既定=絞らない）を編集する。
  document.getElementById('aminTsuboInput').addEventListener('input',e=>{
    const t=numOrNull(e.target.value);const v2=(t==null?null:parseFloat(tsuboToSqm(t)));
    if(S.tab==='camp')S.aminCamp=v2; else S.amin=v2;
    const v=document.getElementById('aminSqmView');if(v)v.textContent=(v2==null?'—':(+v2).toFixed(1));
    saveState();render();});
  document.getElementById('amaxTsuboInput').addEventListener('input',e=>{
    const t=numOrNull(e.target.value);const v2=(t==null?null:parseFloat(tsuboToSqm(t)));
    if(S.tab==='camp')S.amaxCamp=v2; else S.amax=v2;
    const v=document.getElementById('amaxSqmView');if(v)v.textContent=(v2==null?'—':(+v2).toFixed(1));
    saveState();render();});

  document.getElementById('resetBtn').addEventListener('click',()=>{S=defState();saveState();applyStateToControls();render();});
  document.getElementById('filterToggle').addEventListener('click',()=>{document.getElementById('panel').classList.toggle('open');});

  const areaPop=document.getElementById('areaPop');
  document.getElementById('areaBtn').addEventListener('click',e=>{e.stopPropagation();
    if(areaPop.style.display==='block'){areaPop.style.display='none';return;}
    renderAreaList();const r=e.target.getBoundingClientRect();
    areaPop.style.left=(window.scrollX+Math.max(8,r.left-100))+'px';areaPop.style.top=(window.scrollY+r.bottom+2)+'px';areaPop.style.display='block';});
  document.getElementById('areaClose').addEventListener('click',e=>{e.stopPropagation();areaPop.style.display='none';});
  function addArea(){const v=(document.getElementById('areaInput').value||'').trim();
    if(v&&!EXAREAS.some(a=>a.name===v)){EXAREAS.push({name:v,on:true});saveAreas();document.getElementById('areaInput').value='';renderAreaList();render();}}
  document.getElementById('areaAdd').addEventListener('click',e=>{e.stopPropagation();addArea();});
  document.getElementById('areaInput').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();addArea();}});
  areaPop.addEventListener('click',e=>{e.stopPropagation();
    const del=e.target.closest('.delx'); if(del){EXAREAS=EXAREAS.filter(a=>a.name!==del.dataset.name);saveAreas();renderAreaList();render();return;}
    const chk=e.target.closest('.areachk'); if(chk){const a=EXAREAS.find(a=>a.name===chk.dataset.name);if(a){a.on=chk.checked;saveAreas();render();}}});

  document.addEventListener('click',e=>{
    const fb=e.target.closest('.favbtn'); if(fb){e.stopPropagation();const bdk=fb.dataset.bdk;
      if(FAVOR.has(bdk))FAVOR.delete(bdk); else FAVOR.set(bdk,snapOf(repByBdk(bdk)));
      saveFav();render();return;}
    const th=e.target.closest('th.col'); if(th){e.stopPropagation();openPopup(th);return;}
    const hb=e.target.closest('.hidebtn'); if(hb){e.stopPropagation();openHideModal(repByBdk(hb.dataset.bdk));return;}
    const rb=e.target.closest('.restorebtn'); if(rb){HIDDEN.delete(rb.dataset.bdk);saveHidden();render();return;}
    const sec=e.target.closest('h2.sec'); if(sec){const t=document.getElementById(sec.dataset.target);sec.classList.toggle('open');t.classList.toggle('open');return;}
    const pop=document.getElementById('popup'); if(pop.style.display==='block'&&!pop.contains(e.target))closePopup();
    const ap=document.getElementById('areaPop'); if(ap.style.display==='block'&&!ap.contains(e.target)&&e.target.id!=='areaBtn')ap.style.display='none';
  });

  render();
})();
"""


def _site_status(row) -> str:
    h = row["http"]
    if h == 200:
        return "稼働(adapter)" if row.get("mode") == "adapter" else "稼働(ハッシュ)"
    if h == "robots制限":
        return "対象外(robots)"
    return f"要確認(HTTP {h})"


def write_sources_md(path: Path, config: dict, results: list) -> None:
    """全体像マトリクス SOURCES.md を生成。sites(実行結果) + sources_extra(静的) をマージ。"""
    by_id = {r["id"]: r for r in results}
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    L = [f"# akiya-watch 監視ソース一覧（SOURCES.md）",
         "",
         f"最終更新: {ts}（watch.py 実行時に自動更新）",
         "",
         "| 区分 | ソース名 | 対象市町・種別 | URL | 状態 | 最終HTTP | 件数 |",
         "|---|---|---|---|---|---|---|"]
    for s in config.get("sites", []):
        r = by_id.get(s["id"])
        ch = s.get("channel", "?")
        kind = s.get("kind", "")
        if r:
            status = _site_status(r)
            http = r["http"]
            cnt = r["raw"] if r.get("mode") == "adapter" else "—"
        else:
            status, http, cnt = "未実行", "—", "—"
        L.append(f"| {ch} | {s['name']} | {kind} | {s['url']} | {status} | {http} | {cnt} |")
    for e in config.get("sources_extra", []):
        L.append(f"| {e.get('channel','?')} | {e.get('name','')} | {e.get('kind','')} | "
                 f"{e.get('url','')} | {e.get('status','')} | {e.get('http','—')} | {e.get('note','')} |")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def write_csv_report(path: Path, results: list) -> None:
    """Excel 用。UTF-8 with BOM（utf-8-sig）。所在地は全文保持。"""
    import csv

    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["サイト", "市町", "種別", "種別根拠", "所在地", "価格(万円)", "土地面積(㎡)",
                    "坪単価(万円/坪)", "種別上限(万円)", "地目", "都市計画", "接道",
                    "建築可否", "建築可否理由", "属人性", "検出日", "フラグ", "詳細URL", "判定"])
        for r in results:
            for p in r["props"]:
                w.writerow([
                    r["name"],
                    p.get("machi", ""),
                    p.get("shubetsu", ""),
                    p.get("shubetsu_reason", ""),
                    p["location"] or p["text"],
                    p["price_man"] if p["price_man"] is not None else "",
                    p["area_sqm"] if p["area_sqm"] is not None else "",
                    p.get("tsubo_man") if p.get("tsubo_man") is not None else "",
                    p.get("ceiling_man", ""),
                    p.get("chimoku", "—"),
                    p.get("toshikeikaku", "—"),
                    p.get("setsudo") or "",
                    p.get("rebuild_mark", "不明"),
                    p.get("rebuild_reason", ""),
                    "○" if p.get("zokujinsei") else "",
                    p.get("first_seen") or "",
                    _flag_text(p),
                    p["url"],
                    p["verdict"],
                ])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="akiya-watch")
    parser.add_argument("--dry-run", action="store_true", help="スナップショットを保存しない")
    parser.add_argument("--only", default="", help="site id に部分一致するサイトだけ巡回（例: suumo_）")
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run, only=args.only))
