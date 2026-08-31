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
NEW_WINDOW_DAYS = 7         # 新着扱いの保持日数（first_seenからこの日数以内を新着とする）
REPORT_RETENTION_DAYS = 14  # 日付別htmlの保持日数（15日以上前は削除）

# --rebuild が生成する確認用ページにだけ出す警告バナー文言。build_html_report に
# preview_note として渡された時だけ描画される（通常実行では一切表示されない）。
PREVIEW_BANNER_TEXT = (
    "⚠ これは表示確認用のプレビューです。物件データは前回クロール時点のもので、"
    "種別・間取り・敷金礼金・建築可否は簡略表示になっています。公開レポートではありません。"
)

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


# ---- 賃貸・月額家賃正規化（rentタブ専用。売買用 parse_price_man は int 丸めのため使わない）----
_YEN_RE = re.compile(r"([\d,]+)\s*円")


def _first_yen(text: str):
    """テキストから最初の「N円」を整数円で返す（管理費等の抽出に使用）。取れなければ None。"""
    if not text:
        return None
    m = _YEN_RE.search(text)
    if not m:
        return None
    try:
        v = int(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return v if v > 0 else None


def parse_rent_man(text: str):
    """賃貸の月額家賃を「万円」float で返す（例 "4.9万円"→4.9, "6.25 万円"→6.25）。

    取れなければ None。売買用 parse_price_man は int 丸めのため賃貸では使わない。
    「万」表記が無い場合は「55,000円」等の円表記を円→万円換算する。ただし管理費等の
    小額を誤って拾わないよう、10,000円以上のときだけ採用する。
    """
    if not text:
        return None
    m = _MAN_RE.search(text)
    if m:
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            v = None
        if v is not None and v > 0:
            return round(v, 2)
    yen = _first_yen(text)
    if yen is not None and yen >= 10000:
        return round(yen / 10000, 2)
    return None


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


# 住宅探索(home/camp/rentタブ共通)の対象市町。前半7つ=住宅探索(home)の対象市町。
# 以降=キャンプ場土地(camp)タブの1h圏 Tier1+Tier2、末尾5つ=Tier3（1h超だが広い山物件の
# 在庫が豊富な伊豆最南部+賀茂郡。現地訪問前提で監視だけ広げる）。河津町/松崎町は
# suumo_camp_kamogun（賀茂郡ページ）の machi 判定用に追加。
_MACHI_NAMES_SHIZUOKA = ("函南町", "伊豆の国市", "三島市", "沼津市", "清水町", "長泉町", "裾野市",
                "伊豆市", "熱海市", "御殿場市", "小山町", "伊東市", "西伊豆町",
                "湯河原町", "箱根町", "富士市",
                "下田市", "東伊豆町", "南伊豆町", "河津町", "松崎町")
# 賃貸タブ(rent)も静岡県東部が対象のため、_MACHI_NAMES は静岡側と等価。
_MACHI_NAMES = _MACHI_NAMES_SHIZUOKA


def extract_machi(text: str) -> str:
    """所在地テキストから対象市町（静岡県）を判定。無ければ空文字。"""
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
                 location="", default_type="更地", shubetsu_override=None,
                 madori=None, chikunen=None, kanrihi_yen=None,
                 shikikin=None, reikin=None) -> dict:
    """共通テーブルへの正規化レコードを作る。flag_text はフラグ・属性判定に使う範囲のテキスト。

    サーバ側ではハード除外しない（C方針）。判定は数値のみ:
      数値不明 = 価格が null（面積下限>0のタブでは面積nullも数値不明）
      適合     = 価格≤種別別上限（price_ceiling_by_type） かつ
                （面積下限≤0なら面積は問わない。>0なら面積≥下限も必要）
      不適合   = 上記以外
    NGエリア・キーワード・再建築不可は除外せず「フラグ」として保持し、絞り込みは
    クライアント側トグルで行う。

    賃貸タブ用の引数（既定None＝従来の売買動作を変えない）:
      shubetsu_override … 指定時は classify_shubetsu をバイパスしてこの値を種別に使う
                          （"賃貸戸建"/"賃貸アパート"/"賃貸マンション"/"賃貸その他"）。
      madori/chikunen/kanrihi_yen … 間取り/築年テキスト/管理費(円)。賃貸adapterのみ設定。
      shikikin/reikin … 敷金/礼金の表記そのまま（例 "2ヵ月"/"6.7万円"/"無"）。無理に
                        数値化しない。取得元に値が無ければ None（"不明"と"無"は区別する）。
    """
    interest = [kw for kw in filters.get("interest_keywords", []) if kw in flag_text]
    caution = [kw for kw in filters.get("caution_keywords", []) if kw in flag_text]
    ng_hay = (location or "") + " " + flag_text
    ng_areas = [a for a in filters.get("exclude_areas", []) if a in ng_hay]

    is_rent = shubetsu_override is not None
    if is_rent:
        shubetsu, shubetsu_reason = shubetsu_override, "賃貸カテゴリ（掲載種別）"
    else:
        shubetsu, shubetsu_reason = classify_shubetsu(flag_text, default_type)
    ceiling = ceiling_for(shubetsu, filters)
    area_min = filters.get("area_min_sqm", 330)
    # 面積下限が0以下（賃貸タブ等）のときは面積を判定条件から外し、価格のみで適合判定する。
    # area_min>0（既存の売買タブ=330/1000）の挙動はここで一切変えない。
    area_required = area_min > 0

    if price is None or (area_required and area is None):
        verdict = "数値不明"
    elif price <= ceiling and (not area_required or area >= area_min):
        verdict = "適合"
    else:
        verdict = "不適合"

    zokujin = detect_zokujinsei(flag_text)
    toshikeikaku = extract_toshikeikaku(flag_text)
    chimoku = extract_chimoku(flag_text)
    if is_rent:
        # 賃貸は「再建築/新築可否」の観点が無意味（借家であり土地取得ではないため）。
        rb_mark, rb_reason = "—", "賃貸のため対象外"
    else:
        rb_mark, rb_reason = building_assessment(flag_text, toshikeikaku, zokujin, shubetsu)

    return {
        "url": url,
        "text": text[:120],
        "key": url + "|" + text[:60],
        "price_man": price,
        "area_sqm": area,
        "area_estimated": area_est,
        "tsubo_man": None if is_rent else tsubo_unit_man(price, area),
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
        "madori": madori,
        "chikunen": chikunen,
        "kanrihi": kanrihi_yen,
        "shikikin": shikikin,
        "reikin": reikin,
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


# ---------------------------------------------------------------------------
# 山林売買.net アダプタ（全国山林専門。sanrin.net トップ tab=camp）
#   カード = div.floatleft / div.floatright のうち forest_detail へのリンクを持つもの
#   （トップページに約9件）。各カードは <dl><dt>所在地:</dt><dd>…</dd>…</dl> 形式で
#   所在地/地目/面積(ha主体)/価格を保持。ラベルは全角スペース混在のため NFKC 正規化＋
#   空白/コロン除去してキー化（"地　目:"→"地目"）。詳細URL=a[href*='forest_detail']。単一ページ。
# ---------------------------------------------------------------------------

def _sanrin_net_card_specs(card):
    dl = card.find("dl")
    if not dl:
        return {}
    keys = [re.sub(r"[\s:：]+", "", unicodedata.normalize("NFKC", dt.get_text(strip=True)))
            for dt in dl.find_all("dt")]
    vals = [dd.get_text(strip=True) for dd in dl.find_all("dd")]
    return dict(zip(keys, vals))


def parse_sanrin_net(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "div.floatleft, div.floatright"):
        raise BotBlocked(f"山林売買.net ソフトブロック（{len(first_html)}B）: {base_url}")
    out = []
    for card in soup.select("div.floatleft, div.floatright"):
        a = card.find("a", href=re.compile(r"forest_detail"))
        if not a:
            continue
        url = normalize_url(a["href"], base_url)
        specs = _sanrin_net_card_specs(card)
        location = specs.get("所在地", "").strip()
        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue
        area = _yamaichiba_sqm(specs.get("面積", ""))
        price = parse_price_man(specs.get("価格", ""))
        card_text = card.get_text(" ", strip=True)
        out.append(_make_record(url, location or card_text[:60], price, area, False,
                                card_text, filters, location=location, default_type="更地"))
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[sanrin_net] cards={len(dedup)} (1ページ・全国在庫から所在地一致のみ)")
    return dedup


# ---------------------------------------------------------------------------
# ふるさと情報館 アダプタ（田舎暮らし専門・全国。furusato-net.co.jp/result tab=camp）
#   カード = ul.comBukkenList > li（<a href="…/bukken/{id}">がliを丸ごと包む）。各カードは
#   <p class="title"><span class="sub">ラベル</span><span class="rSpan…">値</span></p> の
#   並びで 所在地/価格/土地面積/延床面積 を保持（sub→rSpan zip）。成約済等で価格が
#   「ーーー万円」の場合は parse_price_man が None を返す想定（数値不明扱い）。単一ページ。
# ---------------------------------------------------------------------------

def _furusato_card_specs(li):
    specs = {}
    for p in li.select("p.title"):
        sub = p.find("span", class_="sub")
        val = p.find("span", class_="rSpan")
        if sub and val:
            specs[sub.get_text(strip=True)] = val.get_text(" ", strip=True)
    return specs


def parse_furusato(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "ul.comBukkenList"):
        raise BotBlocked(f"ふるさと情報館 ソフトブロック（{len(first_html)}B）: {base_url}")
    out = []
    for li in soup.select("ul.comBukkenList > li"):
        a = li.find("a", href=re.compile(r"/bukken/"))
        if not a:
            continue
        url = normalize_url(a["href"], base_url)
        specs = _furusato_card_specs(li)
        location = specs.get("所在地", "").strip()
        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue
        price = parse_price_man(specs.get("価格", ""))
        area = _first_sqm(specs.get("土地面積", ""))
        card_text = li.get_text(" ", strip=True)
        out.append(_make_record(url, location or card_text[:60], price, area, False,
                                card_text, filters, location=location, default_type="更地"))
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[furusato] cards={len(dedup)} (1ページ)")
    return dedup


# ---------------------------------------------------------------------------
# 森林.net アダプタ（森林マッチング・東海地方。shin-rin.net/list/tokai tab=camp）
#   カード = li.post_list（<a>がliを丸ごと包む）。div.new_tit="所在地：…"、div.kind_area
#   （複数）の先頭が "面積：…㎡ 約…坪"、次が "ステータス：…"。価格は掲載が無く常に None。
#   詳細URL = a[href*='/archives/']。単一ページ（東海地方のみ・数件規模。新着監視用）。
# ---------------------------------------------------------------------------

def _shinrin_cards(soup, base_url, filter_keywords, filters):
    out = []
    for li in soup.select("li.post_list"):
        a = li.find("a", href=re.compile(r"/archives/\d"))
        if not a:
            continue
        url = normalize_url(a["href"], base_url)
        loc_el = li.select_one(".new_tit")
        location = ""
        if loc_el:
            location = re.sub(r"^所在地[:：]\s*", "", loc_el.get_text(" ", strip=True)).strip()
        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue
        area = None
        for ka in li.select(".kind_area"):
            tx = ka.get_text(" ", strip=True)
            if "面積" in tx:
                area = _first_sqm(tx)
                break
        title_el = li.select_one(".bold")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        card_text = li.get_text(" ", strip=True)
        out.append(_make_record(url, title[:60] or location or card_text[:60], None, area, False,
                                card_text, filters, location=location, default_type="更地"))
    return out


def parse_shinrin(first_html, base_url, filter_keywords, filters, session):
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "li.post_list"):
        raise BotBlocked(f"森林.net ソフトブロック（{len(first_html)}B）: {base_url}")
    out = _shinrin_cards(soup, base_url, filter_keywords, filters)
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[shinrin] cards={len(dedup)} (1ページ・東海のみ)")
    return dedup


# ---------------------------------------------------------------------------
# 賃貸タブ 共通ヘルパ（tab=rent。静岡県東部（函南町の友人拠点=丹那から通える範囲）の
# 激安賃貸監視向け・月額家賃で判定）
# ---------------------------------------------------------------------------

def _suumo_shikirei_val(text: str):
    """SUUMO賃貸の敷金/礼金セルの値。"-"・空文字は「不明」(None)。"無"と混同しない
    （空欄と"無"表記は意味が違う。CLAUDE.md/指示書のとおり "-" は None 扱い）。"""
    t = (text or "").strip()
    return t if t and t != "-" else None


def _rent_type_from_label(label: str) -> str:
    """建物種別ラベルを 賃貸戸建/賃貸アパート/賃貸マンション/駐車場/賃貸その他 に正規化。

    駐車場は住居ではないが月額数千円のため、混ぜると「安い順」の上位を占めて
    住む物件が埋もれる。独立した種別にして画面側で既定非表示にする。
    """
    label = label or ""
    if "駐車場" in label:
        return "駐車場"
    if "戸建" in label:
        return "賃貸戸建"
    if "マンション" in label:
        return "賃貸マンション"
    if "アパート" in label:
        return "賃貸アパート"
    return "賃貸その他"


# ---------------------------------------------------------------------------
# SUUMO賃貸 アダプタ（suumo.jp/chintai/{都道府県}/sc_*/ tab=rent・最重要チャネル。
#   現在は静岡県東部が対象。id prefix "suumo_rent_" で県を問わず共用）
#   建物カード = div.cassetteitem。建物名 = .cassetteitem_content-title、所在地 =
#   .cassetteitem_detail-col1、築年/階建 = .cassetteitem_detail-col3、建物種別ラベル =
#   .cassetteitem_content-label（"賃貸アパート"/"賃貸一戸建て"/"賃貸マンション"）。
#   部屋行 = table.cassetteitem_other tbody tr（1建物に複数部屋のことがある。部屋ごとに
#   1レコード）。各行: .cassetteitem_price--rent=家賃 / --administration=管理費 /
#   --deposit=敷金 / --gratuity=礼金（"-"は不明としてNone。"無"表記とは区別する）/
#   .cassetteitem_madori=間取り / .cassetteitem_menseki=専有面積。詳細URLは行内の
#   a.cassetteitem_other-linktext[href]（"詳細を見る"）。実測で部屋ごとに異なる実URL
#   （/chintai/jnc_.../）を持つことを確認済み（javascript:void(0) ではない）。
#   ページャ = 既存 parse_suumo と同じ div.pagination_set-nav の「次へ」。
# ---------------------------------------------------------------------------

def _extract_suumo_rent_cards(soup, base_url, filter_keywords, filters) -> list:
    out = []
    for card in soup.select("div.cassetteitem"):
        label_el = card.select_one(".cassetteitem_content-label")
        label = label_el.get_text(strip=True) if label_el else ""
        dtype = _rent_type_from_label(label)
        name_el = card.select_one(".cassetteitem_content-title")
        bname = name_el.get_text(strip=True) if name_el else ""
        loc_el = card.select_one(".cassetteitem_detail-col1")
        location = loc_el.get_text(strip=True) if loc_el else ""
        age_el = card.select_one(".cassetteitem_detail-col3")
        chikunen = age_el.get_text(" ", strip=True) if age_el else None

        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue

        for row in card.select("table.cassetteitem_other tbody tr"):
            link = row.select_one("a.cassetteitem_other-linktext[href]")
            if not link:
                continue
            url = normalize_url(link["href"], base_url)
            rent_el = row.select_one(".cassetteitem_price--rent")
            kanri_el = row.select_one(".cassetteitem_price--administration")
            price = parse_rent_man(rent_el.get_text(strip=True)) if rent_el else None
            kanrihi = _first_yen(kanri_el.get_text(strip=True)) if kanri_el else None
            dep_el = row.select_one(".cassetteitem_price--deposit")
            gra_el = row.select_one(".cassetteitem_price--gratuity")
            shikikin = _suumo_shikirei_val(dep_el.get_text(strip=True)) if dep_el else None
            reikin = _suumo_shikirei_val(gra_el.get_text(strip=True)) if gra_el else None
            madori_el = row.select_one(".cassetteitem_madori")
            menseki_el = row.select_one(".cassetteitem_menseki")
            madori = madori_el.get_text(strip=True) if madori_el else None
            area = _first_sqm(menseki_el.get_text(strip=True)) if menseki_el else None
            row_text = row.get_text(" ", strip=True)
            card_text = bname + " " + location + " " + row_text
            out.append(_make_record(url, bname or location, price, area, False,
                                    card_text, filters, location=location,
                                    shubetsu_override=dtype, madori=madori,
                                    chikunen=chikunen, kanrihi_yen=kanrihi,
                                    shikikin=shikikin, reikin=reikin))
    return out


def parse_suumo_rent(first_html, base_url, filter_keywords, filters, session) -> list:
    """SUUMO賃貸アダプタ。bot対策検出＋ページャ追従は既存parse_suumoと同じ流儀。

    賃貸ページの正常判定カードは div.cassetteitem（売買の div.property_unit とは別クラス）
    のため、汎用の _page_blocked() を使う（_suumo_looks_blocked は売買専用のため使わない）。
    """
    html = first_html
    soup = BeautifulSoup(html, "html.parser")
    for attempt in range(2):
        if not _page_blocked(html, soup, "div.cassetteitem"):
            break
        wait = 8 + attempt * 8
        log.warning(f"[suumo_rent] bot対策ページ検出（{len(html)}B）。{wait}秒待って再取得 {attempt + 1}/2: {base_url}")
        time.sleep(wait)
        code, html = fetch(base_url, session)
        soup = BeautifulSoup(html, "html.parser")
    if _page_blocked(html, soup, "div.cassetteitem"):
        raise BotBlocked(f"SUUMO賃貸 bot対策ページが継続: {base_url}")

    all_props = []
    page_url = base_url
    page = 1
    seen_urls = {base_url}
    seen_hashes = {page_hash(html)}
    while True:
        soup = BeautifulSoup(html, "html.parser")
        all_props.extend(_extract_suumo_rent_cards(soup, page_url, filter_keywords, filters))
        nxt = _suumo_next_url(soup, page_url)
        if not nxt or page >= SUUMO_MAX_PAGES or not _site_time_left():
            if not _site_time_left():
                log.warning(f"[suumo_rent] サイト時間予算超過でページ追従打ち切り page={page}")
            break
        if nxt in seen_urls:
            log.warning(f"[suumo_rent] 次ページURLが既出（ループ）→打ち切り: {nxt}")
            break
        time.sleep(random.uniform(2, 5))
        code, html = fetch(nxt, session)
        if code != 200:
            log.warning(f"[suumo_rent] page {page + 1} HTTP {code} - ページ追従を打ち切り（URLは変更しない）")
            break
        h = page_hash(html)
        if h in seen_hashes:
            log.warning(f"[suumo_rent] 同一内容ページ（ループ）→打ち切り page={page + 1}")
            break
        seen_urls.add(nxt)
        seen_hashes.add(h)
        page_url = nxt
        page += 1

    seen = set()
    out = []
    for r in all_props:
        if r["key"] not in seen:
            seen.add(r["key"])
            out.append(r)
    log.info(f"[suumo_rent] pages={page} cards={len(out)}")
    return out


# ---------------------------------------------------------------------------
# アットホーム空き家バンク 賃貸 アダプタ（akiya-athome.jp/rent/{都道府県コード}/ tab=rent。
#   現在は22=静岡県が対象）
#   カード = section.propety（サイト側の実際のクラス名。"property" のtypoではない）。
#   属性は dl(dt→dd) の並び: 賃料(管理費等)/間取/面積/敷金／保証金/礼金/物件種目/築年月/
#   所在地/交通。詳細URL = a[href*='.akiya-athome.jp/bukken/detail/rent/']（自治体サブ
#   ドメインの絶対URL、そのまま使える）。カテゴリ = .objectCategory（"貸戸建て"等）。
#   単一ページ（件数は都道府県により変動。urls.yaml の note 参照）。
# ---------------------------------------------------------------------------

def _athome_rent_specs(card) -> dict:
    specs = {}
    for dl in card.find_all("dl"):
        dt = dl.find("dt")
        dd = dl.find("dd")
        if dt and dd:
            specs[dt.get_text(strip=True)] = dd.get_text(strip=True)
    return specs


def _extract_athome_rent_cards(soup, base_url, filter_keywords, filters) -> list:
    out = []
    for card in soup.select("section.propety"):
        a = card.select_one("a[href*='.akiya-athome.jp/bukken/detail/rent/']")
        if not a or not a.get("href"):
            continue
        url = normalize_url(a["href"], base_url)
        specs = _athome_rent_specs(card)

        location = ""
        for k, v in specs.items():
            if "所在地" in k:
                location = v.strip()
                break
        if not location:
            gov = card.select_one(".governmentName")
            location = gov.get_text(strip=True) if gov else ""

        cat_el = card.select_one(".objectCategory")
        cat_text = cat_el.get_text(strip=True) if cat_el else ""
        buttype = ""
        for k, v in specs.items():
            if "物件種目" in k:
                buttype = v
                break
        dtype = _rent_type_from_label(buttype or cat_text)

        price = None
        for k, v in specs.items():
            if "賃料" in k:
                price = parse_rent_man(v)
                break
        madori = None
        for k, v in specs.items():
            if k.strip() == "間取":
                madori = v.strip()
                break
        area = None
        for k, v in specs.items():
            if "面積" in k:
                area = _first_sqm(v)
                break
        chikunen = None
        for k, v in specs.items():
            if "築年" in k:
                chikunen = v.strip()
                break

        card_text = card.get_text(" ", strip=True)
        if filter_keywords and not any(kw in (location + " " + card_text) for kw in filter_keywords):
            continue
        title_el = card.select_one(".propetyTitle")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        out.append(_make_record(url, title or location or card_text[:60], price, area, False,
                                card_text, filters, location=location,
                                shubetsu_override=dtype, madori=madori, chikunen=chikunen))
    return out


def parse_akiya_athome_rent(first_html, base_url, filter_keywords, filters, session) -> list:
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "section.propety"):
        raise BotBlocked(f"アットホーム空き家バンク賃貸 ソフトブロック（{len(first_html)}B）: {base_url}")
    out = _extract_athome_rent_cards(soup, base_url, filter_keywords, filters)
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[akiya_athome_rent] cards={len(dedup)} (1ページ)")
    return dedup


# ---------------------------------------------------------------------------
# CHINTAI アダプタ（chintai.net/{都道府県slug}/area/{code}/list/ tab=rent。
#   slugは先方サイトの表記そのまま（例: 静岡は"sizuoka"）
#   建物カード = section.cassette_item。建物名/種別 = .cassette_ttl h2（種別は
#   span.icn_typeB のみ。"新築"等の別バッジ span.icn_newBuilding と混同しないこと）。
#   所在地/築年 = div.bukken_information table の th(住所/築年)→td。
#   部屋 = div.cassette_detail table 内の tbody（1建物に複数部屋のことがある。tbody ごとに
#   hidden input(input.chinRyo=家賃円 / input.madori / input.senMenseki=㎡ / input.bkName)
#   を持ち、tbody[data-detailurl] に実詳細URLが直接入っている（実測で確認済み・
#   javascript:void(0) 表示は「追加」ボタン等の別リンクで、行本体のURLは実URL）。
#   管理費は hidden input が無いため td.price の表示テキストから抽出。
#   ページャ = base+"/pageN/"（"次へ"相当）。大規模市だと数百件規模になるため
#   time budget で自然に打ち切る。
# ---------------------------------------------------------------------------

def _chintai_building_specs(card):
    """建物共通の所在地/築年を bukken_information テーブルから取得。"""
    location, chikunen = "", None
    tbl = card.select_one("div.bukken_information table")
    if not tbl:
        return location, chikunen
    for tr in tbl.find_all("tr"):
        ths = tr.find_all("th")
        tds = tr.find_all("td")
        for th, td in zip(ths, tds):
            label = th.get_text(strip=True)
            if label == "住所":
                mp = td.find("p", class_="map")
                if mp:
                    mp.decompose()
                location = td.get_text(strip=True)
            elif label == "築年":
                chikunen = td.get_text(strip=True)
    return location, chikunen


def _extract_chintai_cards(soup, base_url, filter_keywords, filters) -> list:
    out = []
    for card in soup.select("section.cassette_item"):
        h2 = card.select_one(".cassette_ttl h2")
        cat_el = h2.select_one("span.icn_typeB") if h2 else None
        cat_text = cat_el.get_text(strip=True) if cat_el else ""
        dtype = _rent_type_from_label(cat_text)
        location, chikunen = _chintai_building_specs(card)

        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue

        for tb in card.select("div.cassette_detail table tbody"):
            ci = tb.select_one("input.chinRyo")
            mi = tb.select_one("input.madori")
            si = tb.select_one("input.senMenseki")
            bn = tb.select_one("input.bkName")
            detail = tb.get("data-detailurl") or ""
            if not detail:
                a = tb.select_one("td.detail a[href^='/detail/']")
                detail = a["href"] if a else ""
            if not detail:
                continue  # 実URLが取れないカードはスキップ
            url = normalize_url(detail, base_url)
            price = None
            if ci and ci.get("value"):
                price = parse_rent_man(ci["value"] + "円")
            madori = mi["value"].strip() if mi and mi.get("value") else None
            area = None
            if si and si.get("value"):
                try:
                    area = round(float(si["value"]), 1)
                except ValueError:
                    area = None
            bkname = bn["value"].strip() if bn and bn.get("value") else ""
            price_td = tb.select_one("td.price")
            kanrihi = _first_yen(price_td.get_text(" ", strip=True)) if price_td else None
            room_text = tb.get_text(" ", strip=True)
            title = bkname or (h2.get_text(" ", strip=True) if h2 else location)
            out.append(_make_record(url, title[:60] or location, price, area, False,
                                    room_text, filters, location=location,
                                    shubetsu_override=dtype, madori=madori,
                                    chikunen=chikunen, kanrihi_yen=kanrihi))
    return out


CHINTAI_MAX_PAGES = 30  # 大規模市(数百件規模)を想定した上限。実際は SITE_TIME_BUDGET で自然に打ち切られる想定。


def parse_chintai_net(first_html, base_url, filter_keywords, filters, session) -> list:
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "section.cassette_item"):
        raise BotBlocked(f"CHINTAI ソフトブロック（{len(first_html)}B）: {base_url}")
    out = _extract_chintai_cards(soup, base_url, filter_keywords, filters)
    seen_hashes = {page_hash(first_html)}
    base = base_url.rstrip("/")
    pages = 1
    for pg in range(2, CHINTAI_MAX_PAGES + 1):
        if not _site_time_left():
            log.warning(f"[chintai_net] サイト時間予算超過でページ追従打ち切り page={pg - 1}")
            break
        time.sleep(random.uniform(3, 6))
        code, nhtml = fetch(f"{base}/page{pg}/", session)
        if code != 200 or page_hash(nhtml) in seen_hashes:
            break
        nsoup = BeautifulSoup(nhtml, "html.parser")
        if not nsoup.select("section.cassette_item"):
            break
        seen_hashes.add(page_hash(nhtml))
        pages = pg
        out.extend(_extract_chintai_cards(nsoup, base_url, filter_keywords, filters))
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[chintai_net] cards={len(dedup)} ({pages}ページ)")
    return dedup


# ---------------------------------------------------------------------------
# いい部屋ネット アダプタ（eheya.net/{都道府県slug}/area/{code}/search/ tab=rent。
#   現在は shizuoka が対象）
#   一覧コンテナ = div.logs_bukken_list。建物 = 直下の div[class*=styles_buildingCassette
#   Wrapper]（CSS Modulesのハッシュ付きクラス。class属性は複数トークンを持つ場合があり
#   （例 "logs_dev_imp styles_buildingCassetteWrapper__xxxx"）先頭一致(^=)だと拾い漏れる
#   ため、部分一致(*=)を使うこと（実測で20件中5件が拾い漏れると確認済み）。
#   建物名/種別 = .styles_cassetteTitle*のh2（末尾のspan[class*=styles_buildingKindPicto]
#   が種別バッジ）。所在地/築年 = data-testid="InfoContent_address"/"InfoContent_ageAndStory"
#   （これは安定属性でハッシュではないので優先使用）。
#   部屋 = building内の [data-testid="BuildingCassette_propertyCassette"]（安定属性）。
#   家賃 = [class*=styles_rentPrice]+[class*=styles_rentUnit]。管理費 =
#   data-testid="BuildingPropertyCassette_managementFee"。間取り/面積 =
#   data-testid="BuildingPropertyCassette_roomDetail"（"2LDK / 59.55m2"形式）。
#   詳細URL = a[href^='/detail/']。ページャ = リンクテキスト"次へ"を辿る（クラス名に
#   依存しない）。ハッシュクラス変更で0件になった場合は BotBlocked にせず通常0件として
#   ログに残す（サイト固有仕様どおり）。
# ---------------------------------------------------------------------------

def _eheya_room_madori_area(detail_text: str):
    """detail_text は "2LDK/59.55m2" 形式（get_text(strip=True)がコメントノード区切りの
    " / " を "/" に潰すため、区切りは半角スラッシュのみ・空白は入らない）。
    面積は _first_sqm がこの詰まった形のまま正しく拾えるためテキストはそのまま渡す。"""
    madori = None
    if detail_text:
        parts = detail_text.split("/")
        madori = parts[0].strip() if parts and parts[0].strip() else None
    area = _first_sqm(detail_text)
    return madori, area


def _extract_eheya_cards(soup, base_url, filter_keywords, filters) -> list:
    out = []
    blist = soup.select_one("div.logs_bukken_list")
    if not blist:
        return out
    for building in blist.select("div[class*=styles_buildingCassetteWrapper]"):
        addr_el = building.select_one("[data-testid=InfoContent_address]")
        location = addr_el.get_text(strip=True) if addr_el else ""
        age_el = building.select_one("[data-testid=InfoContent_ageAndStory]")
        # " " 区切りで取得（SUUMO側のchikunen表記"築19年 2階建"と見た目を揃える。
        # 無指定だとコメントノード区切りの " / " が "/" に潰れて"築2年/2階建"になる）。
        chikunen = age_el.get_text(" ", strip=True) if age_el else None

        title_link = building.select_one("a[class*=styles_cassetteTitle]")
        h2 = title_link.find("h2") if title_link else building.find("h2")
        full_title = h2.get_text(strip=True) if h2 else ""
        badge_el = h2.select_one("span[class*=styles_buildingKindPicto]") if h2 else None
        badge_text = badge_el.get_text(strip=True) if badge_el else ""
        bname = full_title
        if badge_text and full_title.endswith(badge_text):
            bname = full_title[:-len(badge_text)].strip()
        dtype = _rent_type_from_label(badge_text)

        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue

        for room in building.select("[data-testid=BuildingCassette_propertyCassette]"):
            link = room.select_one("a[href^='/detail/']")
            if not link or not link.get("href"):
                continue
            url = normalize_url(link["href"], base_url)
            rp = room.select_one("[class*=styles_rentPrice]")
            ru = room.select_one("[class*=styles_rentUnit]")
            rent_text = (rp.get_text(strip=True) if rp else "") + (ru.get_text(strip=True) if ru else "")
            price = parse_rent_man(rent_text) if rent_text else None
            mgmt_el = room.select_one("[data-testid=BuildingPropertyCassette_managementFee]")
            kanrihi = _first_yen(mgmt_el.get_text(" ", strip=True)) if mgmt_el else None
            detail_el = room.select_one("[data-testid=BuildingPropertyCassette_roomDetail]")
            detail_text = detail_el.get_text(strip=True) if detail_el else ""
            madori, area = _eheya_room_madori_area(detail_text)
            room_text = room.get_text(" ", strip=True)
            card_text = bname + " " + location + " " + room_text
            out.append(_make_record(url, bname or location, price, area, False,
                                    card_text, filters, location=location,
                                    shubetsu_override=dtype, madori=madori, chikunen=chikunen,
                                    kanrihi_yen=kanrihi))
    return out


EHEYA_MAX_PAGES = 20


def _eheya_next_url(soup, base_url):
    """"次へ" リンクを絶対URLで返す（CSS Modulesのハッシュクラスに依存せずリンクテキストで判定）。"""
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True) == "次へ" and "page=" in a["href"]:
            return urllib.parse.urljoin(base_url, a["href"])
    return None


def parse_eheya(first_html, base_url, filter_keywords, filters, session) -> list:
    soup = BeautifulSoup(first_html, "html.parser")
    out = _extract_eheya_cards(soup, base_url, filter_keywords, filters)
    seen_urls = {base_url}
    seen_hashes = {page_hash(first_html)}
    page_url, html, page = base_url, first_html, 1
    while True:
        nxt = _eheya_next_url(BeautifulSoup(html, "html.parser"), page_url)
        if not nxt or page >= EHEYA_MAX_PAGES or not _site_time_left():
            if not _site_time_left():
                log.warning(f"[eheya] サイト時間予算超過でページ追従打ち切り page={page}")
            break
        if nxt in seen_urls:
            break
        time.sleep(random.uniform(3, 6))
        code, html = fetch(nxt, session)
        if code != 200:
            break
        h = page_hash(html)
        if h in seen_hashes:
            break
        seen_urls.add(nxt)
        seen_hashes.add(h)
        page_url = nxt
        page += 1
        out.extend(_extract_eheya_cards(BeautifulSoup(html, "html.parser"), page_url, filter_keywords, filters))

    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    if not dedup:
        log.warning(f"[eheya] cards=0 — CSS Modulesハッシュクラス変更等で構造が崩れた可能性あり"
                    f"（BotBlocked にはせず通常0件として扱う）: {base_url}")
    else:
        log.info(f"[eheya] cards={len(dedup)} ({page}ページ)")
    return dedup


# ---------------------------------------------------------------------------
# スマイミー静岡 賃貸 アダプタ（shizuoka.fudohsan.jp/一覧/借りる/地域/{市町}/ tab=rent。
#   takken（空き家バンクしずおか＝akiya-bank.shizuoka.fudohsan.jp）と同じ静岡県宅建協会
#   システムで、DOM構造・ページャ機構とも同一（実測で確認済み）。
#   カード = li.item-block（1ページ10件）。詳細URL = カード内 a[href] のうち
#   unquote後に"物件"を含むもの（takkenと同じ判定）。
#   カード内 div.item-block_header.pc に:
#     span.cat        = 種別ラベル（"貸戸建住宅"/"貸マンション"/"貸アパート"/
#                        "貸店舗（建物一括/一部）"/"貸駐車場" 等）
#     p.title          = 所在地（住所そのもの。建物名は含まない）
#     p.price          = .num01=金額 / .num02="万円" / .num03=管理費("管理費無"等)
#     p.area           = .num01=間取り数("2"や"ワンルーム") / .num02=間取り種別("LDK"等。
#                        ワンルームの場合は無し) / .num03=面積テキスト（"専有面積49.68m²"
#                        等。駐車場は空）
#   カード内 div.specTable table（th→td）に "敷金等"="敷金2ヵ月/ 保証金無/ 礼金無" や
#   "築年月/所在階/階数"="1992年8月/ 2階/ 2階建" 等。specTable はカードの直接の子孫要素
#   （カードとtableの対応は1カード=1tableで一意。実測で紐付けの曖昧さなしを確認済み）。
#   ページャ = takkenと同じ a.page-number の onclick $('#list_update').load('.../page/N')
#   AJAX式。指示により2〜3ページに追従を制限（SUMAIMY_MAX_PAGES）。
#   種別分類は既存 _rent_type_from_label を流用（戸建/マンション/アパート以外は賃貸その他
#   ＝貸店舗/貸駐車場等はここに入り、filters.rent の caution_keywords で注意フラグが立つ）。
# ---------------------------------------------------------------------------

SUMAIMY_MAX_PAGES = 3  # 指示どおり2〜3ページに制限（ページ内訳はtakkenと同じAJAX pager）

_SUMAIMY_SHIKI_RE = re.compile(r"敷金\s*([^/]+?)\s*/")
_SUMAIMY_REI_RE = re.compile(r"礼金\s*([^/]+?)(?:/|$)")


def _sumaimy_shikirei(text: str):
    """「敷金2ヵ月/ 保証金無/ 礼金無」等から (敷金, 礼金) を抽出。表記ゆれのまま保持し、
    無理に数値化しない。"敷金等"セル自体が無ければ (None, None)。"""
    if not text:
        return None, None
    shiki = None
    rei = None
    m = _SUMAIMY_SHIKI_RE.search(text)
    if m:
        shiki = m.group(1).strip().replace(" ", "") or None
    m2 = _SUMAIMY_REI_RE.search(text)
    if m2:
        rei = m2.group(1).strip().replace(" ", "") or None
    return shiki, rei


def _sumaimy_madori(area_p) -> str:
    """p.area の .num01(数/ワンルーム)+.num02(LDK等) を連結（"2"+"LDK"→"2LDK"）。
    .num02はワンルーム表記では存在しないため位置ではなくクラスで個別に取る。"""
    if area_p is None:
        return None
    n1 = area_p.select_one(".num01")
    n2 = area_p.select_one(".num02")
    madori = (n1.get_text(strip=True) if n1 else "") + (n2.get_text(strip=True) if n2 else "")
    return madori or None


def _sumaimy_specfields(card) -> dict:
    """カード内 div.specTable table の th→td マップ（敷金等/築年月等）。"""
    fields = {}
    table = card.select_one("div.specTable table")
    if table:
        for tr in table.select("tr"):
            th = tr.find("th")
            td = tr.find("td")
            if th and td:
                fields.setdefault(th.get_text(strip=True), td.get_text(" ", strip=True))
    return fields


def _extract_sumaimy_rent_cards(soup, base_url, filter_keywords, filters) -> list:
    out = []
    for card in soup.select("li.item-block"):
        url = ""
        for a in card.find_all("a", href=True):
            if "物件" in urllib.parse.unquote(a["href"]):
                url = normalize_url(a["href"], base_url)
                break
        if not url:
            continue

        hdr = card.select_one("div.item-block_header.pc") or card
        cat_el = hdr.select_one("span.cat")
        cat = cat_el.get_text(strip=True) if cat_el else ""
        dtype = _rent_type_from_label(cat)

        title_el = hdr.select_one("p.title")
        location = title_el.get_text(" ", strip=True) if title_el else ""

        price_p = hdr.select_one("p.price")
        price, kanrihi = None, None
        if price_p:
            n1 = price_p.select_one(".num01")
            n2 = price_p.select_one(".num02")
            n3 = price_p.select_one(".num03")
            price_text = (n1.get_text(strip=True) if n1 else "") + (n2.get_text(strip=True) if n2 else "")
            price = parse_rent_man(price_text)
            kanrihi = _first_yen(n3.get_text(strip=True)) if n3 else None

        area_p = hdr.select_one("p.area")
        madori = _sumaimy_madori(area_p)
        area = None
        if area_p:
            n3 = area_p.select_one(".num03")
            area = _first_sqm(n3.get_text(" ", strip=True)) if n3 else None

        fields = _sumaimy_specfields(card)
        shiki_key = next((k for k in fields if "敷金" in k), None)
        shikikin, reikin = _sumaimy_shikirei(fields.get(shiki_key, "")) if shiki_key else (None, None)

        chikunen = None
        for k, v in fields.items():
            if k.startswith("築年月"):
                chikunen = v.split("/")[0].strip() or None
                break

        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue

        card_text = card.get_text(" ", strip=True)
        text = (cat + " " + location).strip() or location
        out.append(_make_record(url, text, price, area, False, card_text, filters,
                                location=location, shubetsu_override=dtype,
                                madori=madori, chikunen=chikunen, kanrihi_yen=kanrihi,
                                shikikin=shikikin, reikin=reikin))
    return out


def parse_sumaimy_rent(first_html, base_url, filter_keywords, filters, session) -> list:
    """スマイミー静岡 賃貸アダプタ。ページャ（takkenと同じAJAX .load）に最大3ページ追従。"""
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "li.item-block"):
        raise BotBlocked(f"スマイミー静岡 ソフトブロック（{len(first_html)}B）: {base_url}")

    all_props = _extract_sumaimy_rent_cards(soup, base_url, filter_keywords, filters)
    total = _takken_total_pages(soup)
    loadbase = _takken_loadbase(soup)
    page = 1
    while loadbase and page < min(total, SUMAIMY_MAX_PAGES):
        if not _site_time_left():
            log.warning(f"[sumaimy_rent] サイト時間予算超過でページ追従打ち切り page={page}")
            break
        page += 1
        time.sleep(random.uniform(2, 5))
        nxt = urllib.parse.urljoin(base_url, loadbase + f"/page/{page}")
        code, html = fetch(nxt, session)
        if code != 200:
            log.warning(f"[sumaimy_rent] page {page} HTTP {code} - ページ追従を打ち切り（URLは変更しない）")
            break
        all_props.extend(_extract_sumaimy_rent_cards(
            BeautifulSoup(html, "html.parser"), base_url, filter_keywords, filters))

    seen = set()
    out = []
    for r in all_props:
        if r["key"] not in seen:
            seen.add(r["key"])
            out.append(r)
    log.info(f"[sumaimy_rent] pages={page} cards={len(out)}")
    return out


# ---------------------------------------------------------------------------
# ジモティー アダプタ（jmty.jp/shizuoka/est-hou・est-land 共通構造。個人掲示板・channel④）
#   カード = li.p-articles-list-item（"is-highlighted u-color-background-highlight" 等の
#   追加クラスが付く場合があるが、CSSクラスセレクタは部分一致（複数クラスの1つでも可）
#   なので単純に "li.p-articles-list-item" で両方拾える（複合セレクタにする必要はない）。
#   詳細URL = カード内の a[href*='/article-']（ジモティー自身の投稿のみ）。"alliance-"で
#   始まる提携サイト転載カードは対象外＝a[href*='/article-']が無いので自然にスキップされる。
#   ?from=pr 等のクエリは normalize_url が TRACKING_PARAMS で自動除去するため重複しない。
#   価格 = .p-item-most-important（例 "3.48万円"）。est-hou(賃貸)は月額家賃なので
#   parse_rent_man、est-land(土地)は総額なので parse_price_man。判定は base_url に
#   "est-hou" を含むかどうかで行う。
#   所在地 = .p-item-secondary-important（都道府県="静岡"）+ 先頭の
#   .p-item-supplementary-info（市町名/駅名/カテゴリ。2つ目以降の.p-item-supplementary-info
#   はキーワードタグ("初期"等)なので使わない）。
#   種別(est-houのみ) = 所在地テキストに含まれる「戸建/マンション/アパート」を
#   _rent_type_from_label で 賃貸戸建/賃貸マンション/賃貸アパート/賃貸その他 に正規化。
#   面積 = カード全文から _first_sqm（取れなければ None）。新着順ページを
#   <a rel="next" href="/shizuoka/est-hou/p-2"> 形式のページャで最大3ページまで追従
#   （全静岡県で数百〜数万件規模のため指示により2〜3ページに制限。JMTY_MAX_PAGES）。
# ---------------------------------------------------------------------------

JMTY_MAX_PAGES = 3  # 指示どおり2〜3ページに制限（全県だと数百〜数万件規模のため）


def _jmty_next_url(soup, base_url):
    """<a rel="next" href="..."> を絶対URLで返す（無ければ None）。"""
    a = soup.select_one("a[rel='next']")
    if a and a.get("href"):
        return normalize_url(a["href"], base_url)
    return None


def _extract_jmty_cards(soup, base_url, filter_keywords, filters) -> list:
    is_rent = "est-hou" in base_url
    out = []
    for card in soup.select("li.p-articles-list-item"):
        a = card.select_one("a[href*='/article-']")
        if not a or not a.get("href"):
            continue  # 提携サイト転載カード(alliance-)等は対象外
        url = normalize_url(a["href"], base_url)

        price_el = card.select_one(".p-item-most-important")
        price_text = price_el.get_text(strip=True) if price_el else ""
        price = parse_rent_man(price_text) if is_rent else parse_price_man(price_text)

        pref_el = card.select_one(".p-item-secondary-important")
        pref = pref_el.get_text(strip=True) if pref_el else ""
        loc_el = card.select_one(".p-item-supplementary-info")
        loc_text = loc_el.get_text(" ", strip=True) if loc_el else ""
        location = (pref + " " + loc_text).strip()

        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue

        title_el = card.select_one(".p-item-title")
        title = title_el.get_text(strip=True) if title_el else ""
        card_text = title + " " + location + " " + card.get_text(" ", strip=True)
        area = _first_sqm(card_text)

        dtype = _rent_type_from_label(loc_text) if is_rent else None
        out.append(_make_record(url, title or location, price, area, False,
                                card_text, filters, location=location,
                                default_type="更地", shubetsu_override=dtype))
    return out


def parse_jmty(first_html, base_url, filter_keywords, filters, session) -> list:
    """ジモティー アダプタ。<a rel=next> ページャに最大 JMTY_MAX_PAGES ページ追従。

    既存の他adapterと同じ作法: _site_time_left() チェック、time.sleep(4〜8秒)、
    同一ハッシュ/カード0件で打ち切り、URLキーでdedup。
    """
    html = first_html
    soup = BeautifulSoup(html, "html.parser")
    if _page_blocked(html, soup, "li.p-articles-list-item"):
        raise BotBlocked(f"ジモティー ソフトブロック（{len(html)}B）: {base_url}")

    all_out = _extract_jmty_cards(soup, base_url, filter_keywords, filters)
    page_url = base_url
    page = 1
    seen_urls = {base_url}
    seen_hashes = {page_hash(html)}
    while True:
        nxt = _jmty_next_url(soup, page_url)
        if not nxt or page >= JMTY_MAX_PAGES or not _site_time_left():
            if not _site_time_left():
                log.warning(f"[jmty] サイト時間予算超過でページ追従打ち切り page={page}")
            break
        if nxt in seen_urls:
            log.warning(f"[jmty] 次ページURLが既出（ループ）→打ち切り: {nxt}")
            break
        time.sleep(random.uniform(4, 8))
        code, html = fetch(nxt, session)
        if code != 200:
            log.warning(f"[jmty] page {page + 1} HTTP {code} - ページ追従を打ち切り（URLは変更しない）")
            break
        h = page_hash(html)
        if h in seen_hashes:
            log.warning(f"[jmty] 同一内容ページ（ループ）→打ち切り page={page + 1}")
            break
        soup = BeautifulSoup(html, "html.parser")
        cards = _extract_jmty_cards(soup, nxt, filter_keywords, filters)
        if not cards:
            log.warning(f"[jmty] page {page + 1} カード0件 → 打ち切り")
            break
        all_out.extend(cards)
        seen_urls.add(nxt)
        seen_hashes.add(h)
        page_url = nxt
        page += 1

    seen, dedup = set(), []
    for r in all_out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    kind = "賃貸" if "est-hou" in base_url else "土地"
    log.info(f"[jmty] cards={len(dedup)} pages={page} ({kind})")
    return dedup


# ---------------------------------------------------------------------------
# LIFULL HOME'S 賃貸 アダプタ（homes.co.jp/chintai/... tab=rent。売買用 parse_lifull とは
#   別関数・別のDOM構造。homes.co.jp は累積202レート制限の実績があるため urls.yaml 側で
#   3URLに限定し、ページャは追わない（1ページのみ・レート制限配慮。回避策は取らない）。
#   建物カード = div[class*=mergeBuilding]（複数部屋が同一建物にマージされた表示）。
#   建物名/種別 = .bukkenName / .icon-bukkenType .bType（建物見出し内）。建物spec表
#   （所在地/交通/築年数・階数）= .moduleInner.prg-building 内 table の th/td。
#   部屋行 = 建物カード内 tr.prg-room[data-href]（実データ行）。data-href の無い
#   tr.prg-room は仲介業者コメント行（class memberDataRow）であり物件データではないため
#   除外する（実測で必須。無視すると空セルの偽レコードが混ざる）。各行:
#   td.price（例 "7.1万円/7,700円<br>無/1ヶ月/-/-" = 賃料/管理費等の後に
#   敷金/礼金/保証/敷引・償却が続く。一覧ヘッダの表記どおり）・td.layout（間取り<br>
#   専有面積）。詳細URL = tr[data-href] 属性値。
#   なお同ページに「PR」広告カード（div.moduleInner.prg-kksSictClickInfo、adlads等の
#   広告トラッキング属性付き）が数件混在するが対象外とした。実測(函南町・2件)では
#   1件は建物カード内の部屋と完全重複（data-bid一致・URLスキームのみ異なる）だったが、
#   もう1件（レオパレスサン平井）は30建物カードのどこにも現れない、PR経由でのみ見える
#   固有物件だった。二重計上の回避を優先しPRカードは全件対象外としたため、この種の
#   PR限定物件は本アダプタでは拾えない（既知の残課題。将来PR経由の固有物件を拾いたく
#   なったら、建物カード側のdata-bid集合との突き合わせで重複判定してから採用する）。
# ---------------------------------------------------------------------------

_LIFULL_RENT_PRICE_RE = re.compile(
    r"^([\d,]+(?:\.\d+)?)\s*万円\s*/\s*(\S+)\s+(\S*)/(\S*)/(\S*)/(\S*)$"
)


def _lifull_rent_price_fields(text: str):
    """HOME'S賃貸 部屋行の td.price テキスト（例 "7.1 万円 /7,700円 無/1ヶ月/-/-"）を
    (rent_man, kanrihi_yen, shikikin, reikin) に分解。"-" は不明(None)。"無"は契約上の
    表記としてそのまま保持（0円と混同しない）。パターン不一致時は家賃のみ
    parse_rent_man でフォールバック抽出。"""
    m = _LIFULL_RENT_PRICE_RE.match(text or "")
    if not m:
        return parse_rent_man(text), None, None, None
    rent = parse_rent_man(m.group(1) + "万円")
    kanrihi = _first_yen(m.group(2))
    shikikin = m.group(3) if m.group(3) and m.group(3) != "-" else None
    reikin = m.group(4) if m.group(4) and m.group(4) != "-" else None
    return rent, kanrihi, shikikin, reikin


def _lifull_rent_madori_area(text: str):
    """td.layout テキスト（例 "2LDK 62.81m²"）を (madori, area_sqm) に分解。"""
    area = _first_sqm(text)
    madori = re.sub(r"[\d,.]+\s*(?:㎡|m²|m2).*$", "", text or "").strip() or None
    return madori, area


def _lifull_rent_building_specs(building_header) -> dict:
    """建物カード内 .moduleInner.prg-building の spec table（所在地/交通/築年数・階数）
    を th→td dict に。"""
    specs = {}
    tbl = building_header.select_one("table")
    if not tbl:
        return specs
    for tr in tbl.select("tr"):
        th, td = tr.find("th"), tr.find("td")
        if th and td:
            specs[th.get_text(strip=True)] = td.get_text(" ", strip=True)
    return specs


def _extract_lifull_rent_cards(soup, base_url, filter_keywords, filters) -> list:
    out = []
    for card in soup.select("div[class*=mergeBuilding]"):
        header = card.select_one(".moduleInner.prg-building")
        if not header:
            continue
        label_el = header.select_one(".icon-bukkenType .bType")
        dtype = _rent_type_from_label(label_el.get_text(strip=True) if label_el else "")
        name_el = header.select_one(".bukkenName")
        bname = name_el.get_text(strip=True) if name_el else ""
        specs = _lifull_rent_building_specs(header)
        location = specs.get("所在地", "").strip()
        chikunen = specs.get("築年数/階数")

        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue

        for row in card.select("tr.prg-room[data-href]"):
            url = normalize_url(row["data-href"], base_url)
            price_td = row.select_one("td.price")
            layout_td = row.select_one("td.layout")
            price, kanrihi, shikikin, reikin = _lifull_rent_price_fields(
                price_td.get_text(" ", strip=True) if price_td else "")
            madori, area = _lifull_rent_madori_area(
                layout_td.get_text(" ", strip=True) if layout_td else "")
            row_text = row.get_text(" ", strip=True)
            card_text = bname + " " + location + " " + row_text
            out.append(_make_record(url, bname or location, price, area, False,
                                    card_text, filters, location=location,
                                    shubetsu_override=dtype, madori=madori,
                                    chikunen=chikunen, kanrihi_yen=kanrihi,
                                    shikikin=shikikin, reikin=reikin))
    return out


def parse_lifull_rent(first_html, base_url, filter_keywords, filters, session):
    """HOME'S賃貸アダプタ。202レート制限の実績があるため urls.yaml 側で3URLに限定済み。
    ページャは追わない（1ページのみ・レート制限配慮）。売買用 parse_lifull とは別関数・
    別DOM構造（registry では lifull_rent_ を lifull_ より必ず先に置くこと）。
    """
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "div[class*=mergeBuilding]"):
        raise BotBlocked(f"HOME'S賃貸 ソフトブロック（{len(first_html)}B）: {base_url}")
    out = _extract_lifull_rent_cards(soup, base_url, filter_keywords, filters)
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[lifull_rent] cards={len(dedup)} (1ページ・ページャ追従なし)")
    return dedup


# ---------------------------------------------------------------------------
# 静岡県営住宅 アダプタ（sjkk.or.jp/kenei/list.php。tab=rent。抽選制・所得制限あり、
#   月1〜2万円台も出る最安チャネル。カード = div.search_result_box。dl(dt→dd)に
#   所在地/家賃(円)/間取り(専有面積)/竣工年度 が直接載っており、詳細ページ
#   （syosai.php）の取得は不要（実測で確認済み・サーバ負荷配慮に合致）。
#   家賃は「13,900～64,100円」のような幅表記のため下限を採用（安い物件を見つけるのが
#   目的のため）。一覧は POST専用フォーム（form name=main, action=list.php）による
#   ページング（次へ＝hidden PGを進めてsubmit）・絞り込みで、GETのクエリパラメータは
#   一切効かない（実測: ?PG=1 / ?f1=熱海市 / ?display_num=50 のいずれも既定と同一内容
#   を返すことを確認済み）。POSTフォーム追従は実装せず、既定（無フィルタ・1ページ目・
#   20件)のみをGET取得し、ページャは追わない（サーバ負荷配慮。全団地の走査はしない）。
#   なお既定順は熱海市→伊東市→駿東郡→沼津市→三島市→裾野市→御殿場市という地理順で、
#   1ページ目20件だけで対象の東部団地の大半をカバーできる（実測18/20。残る2件は
#   伊東市で対象キーワード外のため意図的に非該当）。
#   間取り欄は複数の部屋タイプ（例 3DK･3K･2LDK･1LDK）を1本の面積レンジ(44.5～50.8㎡)に
#   まとめた表記で、最安家賃の部屋タイプと一対一に対応しないため area は None のままとし
#   （無理に数値化しない）、レンジ文字列は madori にそのまま残す。
# ---------------------------------------------------------------------------

_SJKK_RENT_LOW_RE = re.compile(r"([\d,]+)")


def _sjkk_rent_man(text: str):
    """県営住宅の家賃(円)欄（例 "13,900～64,100円"）の下限を万円floatで返す。
    幅表記の下限を採用（安い物件を見つけるのが目的のため）。取れなければNone。"""
    if not text:
        return None
    m = _SJKK_RENT_LOW_RE.search(text)
    if not m:
        return None
    try:
        yen = int(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return round(yen / 10000, 2) if yen > 0 else None


def _sjkk_cards(soup, base_url, filter_keywords, filters) -> list:
    out = []
    for card in soup.select("div.search_result_box"):
        a = card.select_one("a[href*='syosai.php']")
        if not a or not a.get("href"):
            continue
        url = normalize_url(a["href"], base_url)
        name_el = card.select_one(".apartment_name")
        name = name_el.get_text(strip=True) if name_el else ""
        specs = {}
        for dl in card.select("dl"):
            dt, dd = dl.find("dt"), dl.find("dd")
            if dt and dd:
                specs[dt.get_text(strip=True)] = dd.get_text(" ", strip=True)
        location, rent_text, madori, chikunen = "", "", None, None
        for k, v in specs.items():
            if "所在地" in k:
                location = v
            elif "家賃" in k:
                rent_text = v
            elif "間取り" in k:
                madori = v
            elif "竣工" in k:
                chikunen = v
        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue
        price = _sjkk_rent_man(rent_text)
        card_text = name + " " + location + " " + (madori or "")
        out.append(_make_record(url, name or location, price, None, False,
                                card_text, filters, location=location,
                                shubetsu_override="賃貸その他", madori=madori,
                                chikunen=chikunen))
    return out


def parse_sjkk(first_html, base_url, filter_keywords, filters, session):
    """静岡県営住宅アダプタ。list.php はPOST専用フォームのためページング・絞り込みの
    GETクエリは効かない（実測確認済み）。既定の1ページ目のみ取得しページャは追わない。
    """
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "div.search_result_box"):
        raise BotBlocked(f"静岡県営住宅 ソフトブロック（{len(first_html)}B）: {base_url}")
    out = _sjkk_cards(soup, base_url, filter_keywords, filters)
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[sjkk] cards={len(dedup)} (1ページのみ・ページャ追従なし)")
    return dedup


# ---------------------------------------------------------------------------
# ビレッジハウス アダプタ（villagehouse.jp。旧雇用促進住宅。tab=rent。東部の在庫は
#   沼津2棟・伊豆の国1棟と少数だが敷金礼金ゼロの独自在庫。家賃は静的HTMLに出ない
#   （JS描画）ためリンク監視型で実装（価格None・面積None）。物件が増えたときに気づける
#   ことが目的。建物カード = li.container-search-cards-community。建物名 =
#   .container-search-cards-community-title。所在地（番地まで）=
#   .container-search-cards-community-area（実測で駅・交通情報とは別要素に分離されて
#   おり、番地までの住所のみをクリーンに取得できる）。詳細URL = a[href] のうち
#   /chintai/tokai/shizuoka/{市}/{団地}-{id}/ にマッチするもの（# を含むアンカー
#   （例 "#photos"）は正規表現の $ 終端一致により自然に除外される）。
#   敷金/礼金はサイト全体の謳い文句（title="…✔️敷金なし✔️"・meta description=
#   "礼金なし✔️仲介手数料無料の安いアパート…"）どおり無で確定。個別一覧に敷礼の表示は
#   無いが、事実として敷金礼金ゼロが同社の売りであるため shikikin/reikin="無" を
#   ハードコードする（指示書で事前承認済み）。空室状況（.container-search-cards-
#   community-status＝"空室あり"/"空室なし"/"N 残り部屋"）はカード内に存在するが、
#   専用の表示列が無く madori/chikunen 等に載せると項目名と意味が食い違うため、
#   レコードのタイトル/キーには含めずflag_text（キーワード判定用のみ）に留める
#   （タイトルに含めると状態変化のたびにkeyが変わり「消滅→新規」を誤って繰り返すため）。
#   単一ページ（市町別URLのためページャなし）。
# ---------------------------------------------------------------------------

_VHOUSE_LINK_RE = re.compile(r"/chintai/tokai/shizuoka/[^/]+/[^/#]+-\d+/?$")


def _extract_vhouse_cards(soup, base_url, filter_keywords, filters) -> list:
    out = []
    seen_urls = set()
    for card in soup.select("li.container-search-cards-community"):
        a = card.find("a", href=_VHOUSE_LINK_RE)
        if not a or "#" in a.get("href", ""):
            continue
        url = normalize_url(a["href"], base_url)
        if url in seen_urls:
            continue
        seen_urls.add(url)
        name_el = card.select_one(".container-search-cards-community-title")
        bname = name_el.get_text(strip=True) if name_el else ""
        addr_el = card.select_one(".container-search-cards-community-area")
        location = addr_el.get_text(strip=True) if addr_el else ""
        status_el = card.select_one(".container-search-cards-community-status")
        status = status_el.get_text(strip=True) if status_el else ""
        if filter_keywords and not any(kw in location for kw in filter_keywords):
            continue
        card_text = bname + " " + location + " " + status + " 敷金なし 礼金なし"
        out.append(_make_record(url, bname or location, None, None, False,
                                card_text, filters, location=location,
                                shubetsu_override="賃貸アパート",
                                shikikin="無", reikin="無"))
    return out


def parse_vhouse(first_html, base_url, filter_keywords, filters, session):
    """ビレッジハウスアダプタ。JS描画で家賃が静的HTMLに出ないためリンク監視型
    （価格None）。単一ページ（市町別URLのためページャなし）。
    """
    soup = BeautifulSoup(first_html, "html.parser")
    if _page_blocked(first_html, soup, "li.container-search-cards-community"):
        raise BotBlocked(f"ビレッジハウス ソフトブロック（{len(first_html)}B）: {base_url}")
    out = _extract_vhouse_cards(soup, base_url, filter_keywords, filters)
    seen, dedup = set(), []
    for r in out:
        if r["key"] not in seen:
            seen.add(r["key"])
            dedup.append(r)
    log.info(f"[vhouse] cards={len(dedup)} (リンク監視型・価格None・1ページ)")
    return dedup


# (述語, パーサ) の順に評価。最初に一致したものを使う。
# アダプタは (first_html, base_url, filter_keywords, filters, session) を取り、
# 正規化レコードのリストを返す（ページャ追従はアダプタ内で行う）。
SITE_ADAPTERS = [
    # suumo_rent_ は suumo_ の特殊化なので必ず先に置く（後だと suumo_ に食われて土地用
    # パーサ(parse_suumo)が誤って呼ばれ、賃貸ページが0件になる）。
    (lambda sid: sid.startswith("suumo_rent_"), parse_suumo_rent),
    (lambda sid: sid.startswith("suumo_"), parse_suumo),
    (lambda sid: sid.startswith("takken_"), parse_takken),
    (lambda sid: sid.startswith("sumaimy_"), parse_sumaimy_rent),
    # lifull_rent_ は lifull_ の特殊化なので必ず先に置く（後だと lifull_ に食われて
    # 売買用パーサ(parse_lifull)が誤って呼ばれる。suumo_rent_/suumo_ と同じ理由）。
    (lambda sid: sid.startswith("lifull_rent_"), parse_lifull_rent),
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
    (lambda sid: sid.startswith("sanrin_net"), parse_sanrin_net),
    (lambda sid: sid.startswith("furusato_"), parse_furusato),
    (lambda sid: sid.startswith("shinrin_"), parse_shinrin),
    (lambda sid: sid.startswith("akiya_athome_rent_"), parse_akiya_athome_rent),
    (lambda sid: sid.startswith("chintai_net_"), parse_chintai_net),
    (lambda sid: sid.startswith("eheya_"), parse_eheya),
    (lambda sid: sid.startswith("jmty_"), parse_jmty),
    (lambda sid: sid.startswith("sjkk_"), parse_sjkk),
    (lambda sid: sid.startswith("vhouse_"), parse_vhouse),
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
    # tab: camp / rent のサイトはそれぞれ filters.camp / filters.rent で閾値を上書きした
    # 判定を使う（定義元は urls.yaml。ハードコード禁止）。
    camp_filters = {**filters, **(filters.get("camp") or {})}
    rent_filters = {**filters, **(filters.get("rent") or {})}
    if only:
        sites = [s for s in sites if only in s["id"]]
        log.info(f"--only='{only}' で {len(sites)} サイトに絞り込み")
    # 同一ドメイン連続を避ける（LIFULL 202レート制限の緩和）。
    sites = _disperse_by_domain(sites)
    session = requests.Session()

    results = []
    disappeared = []   # (site_name, archived_item, days_since_removed, site_tab) 消滅(7日以内)
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
        if site_tab == "camp":
            site_filters = camp_filters
        elif site_tab == "rent":
            site_filters = rent_filters
        else:
            site_filters = filters
        # 実行全体のウォールクロック上限。超えたら残サイトを打ち切ってレポートへ。
        if time.time() - run_start > RUN_WALLCLOCK_LIMIT:
            log.warning(f"実行ウォールクロック上限 {RUN_WALLCLOCK_LIMIT}s 超過。残 {len(sites) - i} サイトを打ち切り")
            break
        _SITE_DEADLINE[0] = time.time() + SITE_TIME_BUDGET  # このサイトの時間予算
        log.info(f"[{sid}] fetch start: {url}")

        row = {
            "id": sid, "name": name, "url": url, "yaml_status": yaml_status, "tab": site_tab,
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
                    # 2026-07-26以降: --rebuild が種別・間取り・敷金礼金・建築可否等を
                    # 正確に復元できるよう、判定済みの確定値も保存する（カード全文
                    # (flag_text)は保存しない＝判定結果だけで rebuild は再現できるため）。
                    # verdict は _make_record が必ず非None文字列で設定するため、
                    # --rebuild 側はこのキーの有無を「新形式スナップショットか」の
                    # 目印として使う（古いスナップショットとの後方互換の分岐）。
                    "shubetsu": p.get("shubetsu"), "shubetsu_reason": p.get("shubetsu_reason"),
                    "madori": p.get("madori"), "chikunen": p.get("chikunen"),
                    "kanrihi": p.get("kanrihi"), "shikikin": p.get("shikikin"),
                    "reikin": p.get("reikin"),
                    "rebuild_mark": p.get("rebuild_mark"), "rebuild_reason": p.get("rebuild_reason"),
                    "chimoku": p.get("chimoku"), "toshikeikaku": p.get("toshikeikaku"),
                    "setsudo": p.get("setsudo"), "tsubo_man": p.get("tsubo_man"),
                    "verdict": p.get("verdict"),
                    "interest": p.get("interest"), "caution": p.get("caution"),
                    "ng_areas": p.get("ng_areas"), "zokujinsei": p.get("zokujinsei"),
                    "machi": p.get("machi"), "tab": p.get("tab"),
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

            # 消滅(7日以内)を収集。site_tab はこの物件を出していたサイトの所属タブ
            # （home/camp/rent。archive自体には持たせず、実行時に現在のurls.yaml設定から都度紐付ける）
            for k, a in archive.items():
                d = _days_between(today, a.get("removed_on", today))
                if 0 <= d <= DISAPPEAR_WINDOW_DAYS:
                    disappeared.append((a.get("site_name", name), a, d, site_tab))

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

    emit_reports(results, config, filters, disappeared, dry_run)

    success = sum(1 for r in results if isinstance(r["http"], int) and r["http"] == 200)
    if fail_count == 0:
        return 0
    elif success > 0:
        return 1
    else:
        return 2


def emit_reports(results: list, config: dict, filters: dict, disappeared: list, dry_run: bool,
                 preview: bool = False) -> None:
    """レポート出力（html/index/csv/SOURCES.md）を書き出す共通処理。

    通常のクロール実行(run)から呼ばれる（preview=False、既定）。

    preview=True（--rebuild専用）のときは reports/_preview.html だけを書き、本番成果物
    （reports/index.html・日付別html・csv・SOURCES.md）は一切書き換えない・削除もしない
    （prune_old_reportsも呼ばない）。rebuildはクロールを飛ばした簡易データで作るため、
    本番を上書きしてはならない（実際に上書き事故が起き git checkout で復元した経緯がある
    ための安全策。preview_note付きのHTMLを本番同名パスへ絶対に書かないことがこの分岐の目的）。
    """
    if preview:
        html_doc = build_html_report(results, filters, disappeared, dry_run,
                                     preview_note=PREVIEW_BANNER_TEXT)
        preview_path = REPORTS_DIR / "_preview.html"
        preview_path.write_text(html_doc, encoding="utf-8")
        log.info(f"report(preview): {preview_path}")
        log.info("本番レポート（reports/index.html・日付別html・csv・SOURCES.md）は変更していません")
        return

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


def _rebuild_type_hint(url: str) -> str:
    """--rebuild専用: プロパティ自身のURLから種別ヒント(更地/中古戸建)を推定する。

    通常のアダプタはURL(土地/中古戸建で別URL、または個別カードのURL)から同じ判定をしている
    （例: parse_suumo/parse_lifull/parse_athomeはbase_urlの/chukoikkodate/や/kodate/、
    parse_fudosokenはカード自身のurlの/kodate/）。--rebuildはスナップショットに保存された
    このurlしか使えないため、その共通部分（URLパターン）だけを再現する。
    """
    if "chukoikkodate" in url or "/kodate/" in url:
        return "中古戸建"
    return "更地"


def _rebuild_site_props(site: dict, site_filters: dict) -> list:
    """--rebuild専用: 1サイト分のスナップショット(data/snapshots/{id}.json)から
    `_make_record` 相当のレコードを再構築する。HTTPは一切行わない（読むだけ）。

    2026-07-26以降に保存されたスナップショットには、判定済みの確定値（種別・間取り・
    敷金礼金・建築可否 等。run() の current_keys 構築部を参照）が入っており、それを
    そのまま復元に使うため通常クロールと同精度で再現できる。新形式の目印は "verdict"
    キーの有無（_make_record が必ず非None文字列で設定するため、これが無い＝旧形式）。

    それより前の（このフィールド追加前の）スナップショットには無いため、その場合のみ
    旧来の簡略フォールバック（location+text から `_make_record` で再判定）に落ちる
    （建築可否はほぼ「不明」、賃貸は種別が一律「賃貸その他」寄りになる等、精度が下がる。
    次回の通常クロールでそのサイトのスナップショットが新形式に更新されれば自動的に
    高精度側に切り替わる＝後方互換のためだけの分岐で、恒久的な二重実装ではない）。
    """
    site_tab = site.get("tab", "home")
    is_rent = site_tab == "rent"
    snapshot = load_snapshot(site["id"])
    keys = snapshot.get("keys", {}) or {}
    props = []
    for v in keys.values():
        if not isinstance(v, dict):
            continue
        url = v.get("url", "") or ""
        text = v.get("text", "") or ""
        location = v.get("location", "") or ""
        price = v.get("price_man")
        area = v.get("area_sqm")

        if "verdict" in v:
            # 新形式: crawl時点で確定した判定値をそのまま復元する（キー構成は
            # _make_record の戻り値と1:1で揃え、後段の共通処理を差し替えなしで使えるように）。
            shubetsu = v.get("shubetsu") or "更地"
            rec = {
                "url": url, "text": text[:120], "key": url + "|" + text[:60],
                "price_man": price, "area_sqm": area, "area_estimated": False,
                "tsubo_man": v.get("tsubo_man"),
                "shubetsu": shubetsu, "shubetsu_reason": v.get("shubetsu_reason") or "",
                "ceiling_man": ceiling_for(shubetsu, site_filters),
                "chimoku": v.get("chimoku") or "—", "toshikeikaku": v.get("toshikeikaku") or "—",
                "setsudo": v.get("setsudo"),
                "rebuild_mark": v.get("rebuild_mark") or "不明",
                "rebuild_reason": v.get("rebuild_reason") or "",
                "zokujinsei": bool(v.get("zokujinsei")),
                "verdict": v.get("verdict") or "数値不明",
                "interest": v.get("interest") or [], "caution": v.get("caution") or [],
                "ng_areas": v.get("ng_areas") or [],
                "location": location, "machi": v.get("machi") or "",
                "madori": v.get("madori"), "chikunen": v.get("chikunen"),
                "kanrihi": v.get("kanrihi"), "shikikin": v.get("shikikin"),
                "reikin": v.get("reikin"),
            }
        else:
            # 旧形式（判定済み値なし）: 従来どおりの簡略フォールバック。
            flag_text = (location + " " + text).strip()
            rec = _make_record(
                url, text, price, area, False, flag_text, site_filters,
                location=location,
                default_type=_rebuild_type_hint(url),
                shubetsu_override=("賃貸その他" if is_rent else None),
            )

        rec["first_seen"] = v.get("first_seen") or v.get("last_seen") or ""
        rec["last_seen"] = v.get("last_seen") or ""
        rec["tab"] = v.get("tab") or site_tab
        props.append(rec)
    return props


def _rebuild_row_for_site(site: dict, filters: dict, camp_filters: dict,
                          rent_filters: dict, today: str) -> tuple[dict, list]:
    """--rebuild専用: run()内のクロールループが作る `row` dict 相当を、
    HTTPを使わずスナップショット/アーカイブだけから組み立てる。
    戻り値: (row, disappeared_entries)。disappeared_entriesの形式はrun()のdisappearedと同じ
    (site_name, archived_item, days_since_removed, site_tab) タプルのリスト。
    """
    sid = site["id"]
    name = site["name"]
    site_tab = site.get("tab", "home")
    if site_tab == "camp":
        site_filters = camp_filters
    elif site_tab == "rent":
        site_filters = rent_filters
    else:
        site_filters = filters

    props = _rebuild_site_props(site, site_filters)
    fits = [p for p in props if p["verdict"] == "適合"]
    ng_items = [p for p in props if p.get("ng_areas")]

    row = {
        "id": sid, "name": name, "url": site.get("url", ""),
        "yaml_status": site.get("status", ""), "tab": site_tab,
        "http": "rebuild", "raw": len(props),
        "price_cnt": sum(1 for p in props if p["price_man"] is not None),
        "area_cnt": sum(1 for p in props if p["area_sqm"] is not None),
        "fit_cnt": len(fits), "ng_cnt": len(ng_items), "added_cnt": 0,
        "note": "rebuildモード（クロールなし・保存済みスナップショットから再生成。"
                "カード全文が無いため種別/建築可否等の判定は簡易）",
        "phase2": False, "props": props, "fits": fits, "ng_items": ng_items,
        "added_items": [], "promote": False, "mode": "rebuild",
    }

    disappeared_entries = []
    archive = load_archive(sid)
    for a in archive.values():
        d = _days_between(today, a.get("removed_on", today))
        if 0 <= d <= DISAPPEAR_WINDOW_DAYS:
            disappeared_entries.append((a.get("site_name", name), a, d, site_tab))
    return row, disappeared_entries


def rebuild() -> int:
    """--rebuild: クロールを一切行わず、保存済みスナップショット/アーカイブと
    urls.yaml の現在のfilters/サイト定義だけから確認用ページ reports/_preview.html を
    再生成する。HTTPリクエストは1本も出さない。スナップショットは読むだけで書き換えず、
    差分検出（first_seen更新・added/removed判定）も行わない
    （新着表示は保存済みのfirst_seenをそのまま使う＝run()と同じロジックのbuild_html_report
    に渡すため自動的にそうなる）。

    本番成果物（reports/index.html・日付別html・csv・SOURCES.md）は emit_reports に
    preview=True を渡すことで一切書き換えない（詳細は emit_reports のdocstring参照）。
    """
    log.info("rebuildモード: クロールせず保存済みデータから確認用ページを再生成")
    t0 = time.time()
    config = yaml.safe_load((BASE_DIR / "urls.yaml").read_text(encoding="utf-8"))
    sites = config["sites"]
    filters = config["filters"]
    camp_filters = {**filters, **(filters.get("camp") or {})}
    rent_filters = {**filters, **(filters.get("rent") or {})}
    today = date.today().isoformat()

    results = []
    disappeared = []
    for site in sites:
        row, disap = _rebuild_row_for_site(site, filters, camp_filters, rent_filters, today)
        results.append(row)
        disappeared.extend(disap)

    emit_reports(results, config, filters, disappeared, dry_run=False, preview=True)

    elapsed = time.time() - t0
    total_props = sum(r["raw"] for r in results)
    log.info(f"[rebuild] サイト数={len(results)} 総件数={total_props} 所要時間={elapsed:.1f}秒")
    log.info("[rebuild] 本番レポート(index.html/日付別html/csv/SOURCES.md)は変更していません")
    return 0


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


def build_html_report(results: list, filters: dict, disappeared: list, dry_run: bool,
                      preview_note: str = None) -> str:
    from html import escape

    now = datetime.now()
    ts_label = now.strftime("%Y-%m-%d %H:%M")
    ymd = now.strftime("%Y%m%d")
    today_iso = now.strftime("%Y-%m-%d")
    pmax_def = filters["price_max_man"]
    amin_def = filters["area_min_sqm"]

    ceil_by_type = filters.get("price_ceiling_by_type") or {}
    types = ["更地", "古家付き土地", "中古戸建", "空き家"]

    # 全物件を JSON 埋め込み用に整形（サーバ側ハード除外なし＝全件）
    data = []
    for r in results:
        for p in r["props"]:
            fs = p.get("first_seen") or ""
            days_ago = _days_between(today_iso, fs) if fs else None
            data.append({
                "site": r["name"],
                "tab": p.get("tab", "home"),
                "days_ago": days_ago,
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
                "first_seen": fs,
                "madori": p.get("madori") or "",
                "chikunen": p.get("chikunen") or "",
                "kanrihi": p.get("kanrihi"),
                "shikikin": p.get("shikikin"),
                "reikin": p.get("reikin"),
                "url": p["url"],
                "dk": p["key"],  # バックエンドdedupキー（url+"|"+text[:60]）。非表示永続化に使用
                "ng": bool(p.get("ng_areas")),
                "ng_areas": p.get("ng_areas", []),  # NGエリア該当ログ用（クライアント側でDATAから抽出）
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

    # NGエリア該当ログ・消滅・サイト別サマリは、mainTbl等と同じ流儀でJS側がタブ別に描画する
    # （サーバ側で全件を静的HTML化すると、賃貸タブを見ているのに更地の物件が混ざるため）。
    # NGエリア該当ログはDATA自体（ng===true）から引けるので専用JSONは持たない。
    summary_data = [{
        "id": r["id"], "name": r["name"], "http": str(r["http"]), "mode": r.get("mode", ""),
        "raw": r["raw"], "priceCnt": r["price_cnt"], "areaCnt": r["area_cnt"],
        "fitCnt": r["fit_cnt"], "addedCnt": r["added_cnt"], "ngCnt": r["ng_cnt"],
        "status": r["yaml_status"], "note": r["note"], "tab": r.get("tab", "home"),
    } for r in results]
    summary_json = json.dumps(summary_data, ensure_ascii=False).replace("</", "<\\/")

    disappeared_data = []
    for sname, a, d, site_tab_d in sorted(disappeared, key=lambda x: x[2]):
        loc = (a.get("location") or a.get("text") or "").replace("静岡県", "")[:20]
        price = f"{a['price_man']:,}万円" if a.get("price_man") is not None else "—"
        area = f"{a['area_sqm']:g}㎡" if a.get("area_sqm") is not None else "—"
        disappeared_data.append({
            "site": sname, "loc": loc or "—", "price": price, "area": area,
            "removedOn": a.get("removed_on", "—"), "days": d,
            "url": a.get("url", ""), "tab": site_tab_d,
        })
    disappeared_json = json.dumps(disappeared_data, ensure_ascii=False).replace("</", "<\\/")

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
                  "市街化調整区域（△）は除外ではなく本命候補シグナルです。"
                  "賃貸タブは月額家賃です（他タブの万円表示＝売買価格とは意味が違います）。</p>")

    camp_over = filters.get("camp") or {}
    rent_over = filters.get("rent") or {}
    config_js = json.dumps({
        "ceilings": {t: ceil_by_type.get(t, pmax_def) for t in types},
        "types": types,
        # machi=静岡（更地/家付き/キャンプ場土地/賃貸の全タブ共通）。
        "machi": list(_MACHI_NAMES_SHIZUOKA),
        "cautions": filters.get("caution_keywords", []),
        "exareas": filters.get("exclude_areas", []),
        "amin": amin_def,
        # キャンプ場土地タブ: 判定閾値(参考)。表示フィルタ既定は「絞らない」(null)
        "campPmax": camp_over.get("price_max_man"),
        "campAmin": camp_over.get("area_min_sqm"),
        # 賃貸タブ: 判定閾値(参考・月額家賃万円)。表示フィルタ既定は「絞らない」(null)
        "rentPmax": rent_over.get("price_max_man"),
        "rentAmin": rent_over.get("area_min_sqm"),
        # 参考情報（バッジの見方）の本命/注意キーワード説明文はタブごとに意味が違うため
        # base(更地/家付き/キャンプ場土地共通)とrent(賃貸)を両方渡し、JS側でタブ切替時に
        # 差し替える（filters.rent が賃貸タブのサーバ側判定に使われているのと対応させる）。
        "interestBase": filters.get("interest_keywords", []),
        "cautionBase": filters.get("caution_keywords", []),
        "interestRent": rent_over.get("interest_keywords", []),
        "cautionRent": rent_over.get("caution_keywords", []),
        "newWindowDays": NEW_WINDOW_DAYS,
    }, ensure_ascii=False)

    H = ["<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>",
         "<meta name='viewport' content='width=device-width, initial-scale=1'>",
         "<meta name='robots' content='noindex'>",
         f"<title>不動産情報収集ツール {ts_label}</title><style>{css}</style></head><body>"]

    # ---- トップバー（タイトル / 日付）----
    # 同期状態バッジ(#syncStatus): 既定 display:none。?u=キーが有効な時だけJSがinline-blockにする
    # （キー未使用のブラウザでは何も見えないままにする＝挙動を変えないための土台）。
    sync_badge = ("<span id='syncStatus' class='syncstatus' style='display:none' "
                  "onclick=\"this.classList.toggle('show')\">"
                  "<span id='syncStatusLabel'></span>"
                  "<span class='synctip' id='syncStatusTip'></span></span>")
    H.append("<div class='topbar'>")
    H.append(f"<h1>不動産情報収集ツール <span class='muted'>{ts_label}</span></h1>")
    H.append("<div class='topctl'>日付 " + date_nav + " " + sync_badge + "</div>")
    H.append("</div>")
    if preview_note:
        H.append(f"<div class='preview-banner'>{escape(preview_note)}</div>")
    if dry_run:
        H.append("<p class='muted'>dry-run モード（スナップショット更新なし）</p>")

    # ---- タブ（更地 / 家付き土地 / キャンプ場土地 / 賃貸）----
    H.append("<div class='tabs'>")
    H.append("<button class='tab-btn' data-tab='sarachi'>更地</button>")
    H.append("<button class='tab-btn' data-tab='ie'>家付き土地</button>")
    H.append("<button class='tab-btn' data-tab='camp'>キャンプ場土地</button>")
    H.append("<button class='tab-btn' data-tab='rent'>賃貸</button>")
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
             + "<div class='pr'><b>除外エリア<span id='areaPopTabLabel'></span></b>（所在地に含む地名で隠す）"
             + _help("所在地にこの地名を含む物件を一覧から隠します。除外エリアの内容はタブごとに別々に保存されます。") + "</div>"
             + "<div id='areaList'></div>"
             + "<div class='pr' style='border-top:1px solid #ddd;padding-top:5px'>追加: "
             + "<input id='areaInput' type='text' style='width:120px' placeholder='例: 別荘地名'>"
             + " <button id='areaAdd'>追加</button></div>"
             + "<div class='pr'><button id='areaClose'>閉じる</button></div></div>")

    # ---- 参考情報（バッジ凡例）— パネル直下に配置 ----
    H.append("<details class='refbox cond'>")
    H.append("<summary>参考情報（バッジの見方）</summary>")
    H.append("<div><span class='lbl lbl-plus'>プラス要素（好材料）</span> "
             + "<span id='interestKwText'>"
             + escape("、".join(filters.get("interest_keywords", [])) or "なし") + "</span>"
             + _help("所在地の後ろに、好材料は緑・注意点は赤の目印が付きます。除外はしません。"
                     "賃貸タブでは売買と意味が違うため、内容が入れ替わります。") + "</div>")
    H.append("<div><span class='lbl lbl-minus'>マイナス要素（注意点）</span> "
             + "<span id='cautionKwText'>"
             + escape("、".join(filters.get("caution_keywords", [])) or "なし") + "</span>"
             + _help("所在地の後ろに、好材料は緑・注意点は赤の目印が付きます。除外はしません。"
                     "賃貸タブでは売買と意味が違うため、内容が入れ替わります。") + "</div>")
    H.append("<div class='note'>" + disclaimer.replace("<p class='note'>", "").replace("</p>", "") + "</div>")
    H.append("</details>")

    # ---- モバイル専用: 列フィルタ/並べ替えチップ（モバイルでは列を間引いて表のまま表示する
    #   ため、間引いて隠した列にはth.colタップが届かない。その列への絞り込み/並べ替え導線として
    #   残す。中身はrender()内のrenderColChips()がタブ切替・フィルタ適用のたびに再生成する。
    #   PCでは.colchipsをdisplay:noneにして非表示＝デスクトップの見た目・挙動は変えない）----
    H.append("<div id='colChips' class='colchips'></div>")

    # ---- 物件ブラウザ（折り畳み・JS描画）----
    H.append("<h2 class='sec open' data-target='secMain'>物件ブラウザ（全件・クライアント側フィルタ）</h2>")
    H.append("<div id='secMain' class='secbody open'><table id='mainTbl' class='listtbl'></table></div>")

    # ---- 新着（折り畳み・同フォーマット。first_seenがNEW_WINDOW_DAYS日以内のものを
    #   新しい順（同日内は価格の安い順）に表示。メイン表の並べ替えとは独立）----
    H.append(f"<h2 class='sec open new' data-target='secNew'>🆕 新着（{NEW_WINDOW_DAYS}日以内・新しい順）</h2>")
    H.append("<div id='secNew' class='secbody open'><table id='newTbl' class='listtbl'></table></div>")

    # ---- お気に入り（折り畳み・JS描画）----
    H.append("<h2 class='sec fav' data-target='secFav'>⭐ お気に入り <span id='favCnt'></span></h2>")
    H.append("<div id='secFav' class='secbody'><table id='favTbl' class='listtbl'></table></div>")

    # ---- NGエリア該当ログ（折り畳み・既定閉・JS描画＝タブ別。DATAのng===trueから抽出）----
    H.append("<h2 class='sec excl' data-target='secNg'>NGエリア該当ログ <span id='ngCnt'></span></h2>")
    H.append("<div id='secNg' class='secbody'></div>")

    # ---- 非表示にした物件（折り畳み・既定閉・JS描画）----
    H.append("<h2 class='sec' data-target='secHidden'>非表示にした物件 <span id='hiddenCnt'></span></h2>")
    H.append("<div id='secHidden' class='secbody'><table id='hiddenTbl' class='listtbl'></table></div>")

    # ---- 消滅（折り畳み・既定閉・JS描画＝タブ別）----
    H.append("<h2 class='sec gone' data-target='secGone'>消滅 <span id='goneCnt'></span></h2>")
    H.append("<div id='secGone' class='secbody'></div>")

    # ---- サイト別サマリ（最下部・毎回見る情報ではないメタ情報。JS描画＝タブ別）----
    H.append("<h2 class='sec' data-target='secSummary'>サイト別サマリ（取得状況のメタ情報）</h2>")
    H.append("<div id='secSummary' class='secbody'></div>")

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
    H.append("const SUMMARY=" + summary_json + ";")
    H.append("const DISAPPEARED=" + disappeared_json + ";")
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
    ".preview-banner{background:#fff3cd;border:2px solid #e0a800;color:#7a4b00;font-weight:bold;"
    "padding:8px 14px;border-radius:6px;margin:8px 0;font-size:13px;}"
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
    ".sitecell{max-width:130px;overflow:hidden;text-overflow:ellipsis;}"
    # sitecell内の.sitemobile(モバイル向けさらなる短縮名)はPCでは常に非表示。
    # モバイルCSS側で.sitefullと入れ替える（@media側の詳細はそちらのコメント参照）。
    ".sitecell .sitemobile{display:none;}"
    ".secbody{overflow-x:auto;-webkit-overflow-scrolling:touch;}"
    ".tblwrap{overflow-x:auto;-webkit-overflow-scrolling:touch;}"
    # モバイル専用チップ列。PCでは常に非表示（@media側でモバイルのみdisplay:blockへ上書き）。
    ".colchips{display:none;}"
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
    ".tab-btn[data-tab=rent]{border-color:#7b52ab;color:#7b52ab;}"
    ".tab-btn[data-tab=rent]:not(.active):hover{background:#f2ecf8;}"
    ".tab-btn[data-tab=rent].active{background:#7b52ab;color:#fff;}"
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
    # ---- 同期状態バッジ（?u=キー使用時のみJSが表示。既定は非表示のまま）----
    ".syncstatus{display:inline-block;position:relative;cursor:pointer;font-size:11px;"
    "padding:2px 8px;border-radius:10px;background:#eef3f8;color:#456;white-space:nowrap;vertical-align:middle;}"
    ".syncstatus.st-syncing{background:#eef3f8;color:#456;}"
    ".syncstatus.st-synced{background:#e3f3e6;color:#0a7d2c;}"
    ".syncstatus.st-local{background:#fdf3d8;color:#8a6800;}"
    ".syncstatus.st-unregistered{background:#fde3e3;color:#c0392b;}"
    ".syncstatus.st-invalid{background:#fde3e3;color:#c0392b;}"
    ".synctip{display:none;position:absolute;right:0;top:22px;width:220px;background:#333;color:#fff;"
    "padding:6px 9px;border-radius:5px;font-size:11px;font-weight:normal;z-index:45;"
    "white-space:normal;line-height:1.4;text-align:left;}"
    ".syncstatus.show .synctip{display:block;}"
    # ---- モバイル対応 ----
    "@media(max-width:700px){"
    # overflow-xの安全網はbody ではなく html 側に付ける。bodyに付けると、実際のスクロールは
    # html/viewportへ伝播して問題なく動く一方、bodyの子孫にとっては「overflow:auto等を持つ
    # 祖先」に該当してしまい、.listtbl thead th のposition:stickyがviewportではなくbodyの
    # スクロールボックスに閉じ込められて完全に効かなくなる（実機で確認した既知の罠）。
    # html側なら、そのoverflow-xがそのままviewport自身の挙動になるためsticky計算に影響しない。
    "html{overflow-x:hidden;}"
    "body{margin:0 8px 40px;}.topbar{flex-direction:column;align-items:flex-start;}"
    ".topctl{margin-top:4px;}#filterToggle{display:inline-block;}"
    ".panel{display:none;}.panel.open{display:block;}"
    ".tabs{flex-wrap:wrap;}"
    ".tab-btn{padding:6px 14px;font-size:13px;min-height:44px;display:inline-flex;"
    "align-items:center;justify-content:center;}"
    "h1{font-size:16px;}th,td{font-size:10px;padding:2px 3px;}"
    ".fb-line{flex-wrap:wrap;}.fb-line input[type=number]{width:64px;}"
    "#popup,#areaPop{max-width:90vw;}"
    # ---- モバイル: 一覧表は表のまま・列を間引いて縦に詰める（カード化はしない。
    #   6000件規模を「ざっと眺める」用途では1画面の表示行数が最重要指標のため。
    #   対象は.listtblのみ=mainTbl/newTbl/favTbl/hiddenTbl。NGログ/消滅/サマリ等の
    #   付随テーブルはth/tdにdata-kを持たないためこのブロックの影響を受けず、
    #   従来どおりtblwrapの横スクロールのみで対応する）----
    # 列を間引く: th/tdに既に付与済みのdata-k（colsFor(tab)が唯一の列定義。CSS側で
    #   モバイル用分岐を作らない）をフックに、優先度の低い列をdisplay:noneにする。
    #   非rentタブは 価格/面積/建築可否/掲載サイト/所在地/検出日 が残り、rentタブは
    #   建築可否の代わりに間取り(madori。非表示指定に無いキーなので自動的に残る)が残る。
    ".listtbl [data-k=info],.listtbl [data-k=chimoku],.listtbl [data-k=machi],"
    ".listtbl [data-k=tsubo],.listtbl [data-k=shubetsu],.listtbl [data-k=chikunen],"
    ".listtbl [data-k=shikirei]{display:none;}"
    # 残す列だけになってもなお375pxには収まらない（実測。詳細は下の各ルールのコメント）
    # ため、th,td共通ルールより詳細度の高い.listtbl th/tdでさらに切り詰める。
    ".listtbl th,.listtbl td{padding:2px 2px;}"
    # 見出しは「建築可否」「掲載サイト」等いずれも短い固定の日本語ラベルで、値側より
    # 見出し側のほうが列幅のボトルネックになりやすい（実測で確認）ため、見出しだけ
    # 少し縮める（値側の10pxはPCと同じ。列見出しの可読性が主目的の値ではないので許容）。
    ".listtbl thead th{font-size:9px;}"
    # 所在地・掲載サイト・建築可否は、見出しラベルを含めてもなお375pxに収まらないため
    # 省略表示にする。th/td共通のdata-kをフックにして見出し・値の両方に効かせる
    # （tdだけ絞っても見出し側の文字数がボトルネックのままでは列幅が縮まないため）。
    # 幅は実データをcanvas計測して決めた: 所在地72pxは「郡+町」の最長組み合わせ
    # （例:賀茂郡東伊豆町/西伊豆町/南伊豆町。7文字）まで市町名が必ず見えるのに必要な幅。
    # 番地まで入れるのは不可能なので市町名までを合格ラインとする（腱さん指示どおり。
    # ごく一部、住所欄の先頭に物件番号等が付く一回限りの例外データは対象外）。
    ".listtbl [data-k=loc]{max-width:72px;overflow:hidden;text-overflow:ellipsis;}"
    ".listtbl [data-k=site]{max-width:60px;overflow:hidden;text-overflow:ellipsis;}"
    # 建築可否は値自体(○/△/×/不明)が最大2文字と短いため、見出しラベル(「建築可否」4文字)
    # だけを削ってでも幅を切り詰める（値の可読性は失われない。実測: 22pxで値は全件フル表示）。
    ".listtbl [data-k=rb]{max-width:22px;overflow:hidden;text-overflow:ellipsis;}"
    # 価格・面積は元々max-width制約が無く自然幅任せだった。最初「camp/rentは絞らない既定
    # なので高額物件が混じり得る」という理由で31px/36pxまで削ったところ、全DATAの実測で
    # price 37.9-58.3%・area 2.8-10.6%が切れる実害になった（「1,000万」のような普通に
    # 頻出する4桁価格まで切れていた＝腱さんが問題視していない列を壊してしまっていた）。
    # 全DATA(6344件)のwidth分布を実測し直し、価格は最大値(10,000万)・面積はp95(≈95%
    # タイル値。極端に広い山林等の希少な外れ値だけ許容)を基準に上限を引き直した。
    ".listtbl [data-k=price]{max-width:34px;overflow:hidden;text-overflow:ellipsis;}"
    ".listtbl [data-k=area]{max-width:34px;overflow:hidden;text-overflow:ellipsis;}"
    # 掲載サイト: PC用の.sitefull(正式名。例「SUUMO 土地」)ではなく、さらに縮めた
    # .sitemobile(例「SUUMO」。SITE_PREFIXESのshortを参照)を表示する。切れて判別不能に
    # なっていた実害の対策（腱さん実測で「SUUM…」「空き…」しか見えず、どのサイトの
    # 物件か分からないと指摘された）。
    ".listtbl [data-k=site] .sitefull{display:none;}"
    ".listtbl [data-k=site] .sitemobile{display:inline;}"
    # 所在地: 先頭の郵便番号(〒xxx-xxxx。付くサイトのみ)は限られた幅の中で市町名を
    # 押し出してしまう実害があったため隠す。付かないデータではlocpostが無いため無害。
    ".listtbl [data-k=loc] .locpost{display:none;}"
    # rentタブの間取り(madori)は建築可否(rb)の代わりに出る列。県営住宅等は
    # 「1DK･1LDK･2DK…(35.3～72.6㎡)」のように複数間取りを列挙し得るため見出しでは
    # なく値側が実測でボトルネックだった（375px実測で発覚）。他タブに無いキーなので
    # このルールは賃貸タブにしか効かない。下のarea非表示で浮いた幅もここへ回した。
    ".listtbl [data-k=madori]{max-width:61px;overflow:hidden;text-overflow:ellipsis;}"
    # rentタブの面積(area)は間取り側に「(43.9㎡)」の形で内包されており実質的に空欄
    # （—）になることが多い。data-k=areaは非rentタブと共通のキーなのでcolsFor側は
    # 分岐させず、body[data-tab]（updateTabUI()が render() のたびに反映）を起点にした
    # CSSだけでrentタブに限定して隠す。非rentタブのarea(面積)は必須列なので対象外。
    "body[data-tab=rent] .listtbl [data-k=area]{display:none;}"
    # 検出日:「2026-07-27（18日前）」は長すぎるため、モバイルは相対表示だけ残す
    # （cellHtmlInnerのfirst_seenケースで日付/相対を別spanにしてある。色分けの
    # style指定はtd自体に付くため、どのspanを隠しても維持される＝PC表示は不変）。
    # 括弧(.fsp)も表示の無駄なので隠す＝モバイルは「18日前」のみ残る。
    ".listtbl [data-k=first_seen] .fsdate{display:none;}"
    ".listtbl [data-k=first_seen] .fsp{display:none;}"
    # ボタン/★/詳細リンクはPC用サイズのままだと横幅を大きく食うため縮小する
    # （タップ精度より1画面の情報量を優先する、という今回の要求どおりのtrade-off）。
    ".listtbl .favbtn{font-size:14px;padding:0;}"
    ".listtbl .hidebtn,.listtbl .restorebtn{font-size:9px;padding:1px 1px;}"
    ".listtbl td.detailcell{padding:2px 1px;}"
    ".listtbl td.hidecell{padding:2px 1px;}"
    # 見出し行を固定（sticky）。position:stickyは「overflow:auto等を持つ祖先の内側では
    # 効かない」既知の罠があり、.secbodyがoverflow-x:autoを持つため元々は効かない。
    # ここでは列を間引いて横スクロール自体を無くした（=.secbody側にoverflow-xで
    # 隠すべきはみ出しがそもそも無い）ので、その祖先だけoverflow-xをvisibleに戻して
    # 罠を回避する（.tblwrap配下のNGログ等は別要素で、overflow-xはそちらに残るため
    # 横スクロールは維持される）。
    # thのtopは、既存の上部固定バー(.topbar。position:sticky;top:0;z-index:30)の
    # 実高さぶんオフセットする必要があるが、タイトル文字列や同期バッジ有無で高さが
    # 変わるため固定px値では合わなくなる。JS(syncTopbarHeight)が実測して
    # --topbarHに反映する。z-indexはtopbarの30未満にして必ずその下に潜らせる
    # （12_kintaiで本番差し戻しになった『バーと重なる/表の途中に浮く』の再発防止。
    # 自信の持てるモバイル限定にとどめ、デスクトップのsticky theadは今回入れない）。
    ".secbody{overflow-x:visible;}"
    ".listtbl thead th{position:sticky;top:var(--topbarH,88px);z-index:20;}"
    # ---- モバイル: 列フィルタ/並べ替えチップ（間引いて隠した列へのタップ代替導線）----
    # 折り返しはinline-block+margin(右下)で実現。flex/gapは使わない（指示による）。
    # box-sizing:border-boxで、min-height:36pxがそのままタップ域の実高さになるようにする
    # （content-box既定のままだとborder/paddingぶん36pxを超えて膨らみ、規定と数値がずれる）。
    ".colchips{display:block;margin:6px 0 10px;}"
    ".colchip{box-sizing:border-box;display:inline-block;margin:0 6px 6px 0;padding:0 12px;"
    "min-height:36px;line-height:36px;border:1px solid #999;border-radius:18px;"
    "background:#f5f5f5;font-family:inherit;font-size:12px;color:#222;cursor:pointer;user-select:none;}"
    # th.filtered(背景#dcebff)と同じ判定(S.cf[k])・同じ配色にして、絞り込み中の見た目を揃える。
    ".colchip.filtered{background:#dcebff;border-color:#06c;font-weight:bold;}"
    # チップは折り返し行内で位置が毎回ぶれる（タブごとに列数・並び順が変わる）ため、
    # thのように矩形右下にぶら下げず、画面中央にfixed表示する。thから開くPC側の位置決め
    # (openPopup内のelse分岐)はこの変更で一切触っていない。
    "#popup.mobilepos{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);"
    "max-height:80vh;overflow-y:auto;}"
    "}"
)


_FILTER_JS = r"""
const TYPES=CONFIG.types, MACHI=CONFIG.machi;
// 駐車場は住居ではないので既定非表示（種別フィルタで明示的にチェックすれば出る）
const RENT_TYPES=['賃貸戸建','賃貸アパート','賃貸マンション','賃貸その他','駐車場'];
const CHIMOKU_OPTS=[...new Set(DATA.map(d=>d.chimoku||'—'))].sort();
const HOUSE_TYPES=new Set(['空き家','古家付き土地','中古戸建']);
// ---- サイト名短縮（掲載サイト列専用）----
// d.site（例「SUUMO 賃貸 田方郡（函南町）」）は市町名まで含み長い。市町は別列にあるため
// 表示・フィルタでは既知の接頭辞（サービス名）で正規化する。「先頭の空白まで」等の機械的な
// トリムだと「静岡県 未利用県有地 売却・貸付（行政経営課）」と「静岡県 先着順 県有地売却
// 物件」のような別サイトが同じ短縮名に潰れて事故る（実データで確認済み）ため、実際の
// urls.yaml の name: を見て作った既知プレフィックス一覧のみ照合し、一致しなければ全文の
// まま返す（重複しない一回限りの長い名前はCSSの省略表示に任せる）。元データ(d.site)は
// 書き換えず、この関数は表示・フィルタ判定のときにだけ使う。
// full: フィルタ判定・siteOptsForTab()・PC表示に使う正式な正規化名（従来のSITE_PREFIXES
//   そのもの。1文字も変えていない＝フィルタの挙動は不変）。
// short: モバイル表示専用のさらなる短縮名（5〜6文字目安。タブで既に土地/中古戸建/賃貸/
//   市町が分かれているため種別・地域部分は落としてサービス名だけにする）。短縮表を
//   別配列にすると同じ知識が2箇所に分裂するため、1つの表にfull/short両方を持たせる。
const SITE_PREFIXES=[
  {full:'SUUMO 賃貸',short:'SUUMO'},
  {full:'SUUMO 土地',short:'SUUMO'},
  {full:'SUUMO 中古戸建',short:'SUUMO'},
  {full:'LIFULL 賃貸',short:'LIFULL'},
  {full:'LIFULL 中古戸建',short:'LIFULL'},
  {full:'LIFULL 空き家バンク',short:'LIFULL'},
  {full:'LIFULL 土地',short:'LIFULL'},
  {full:'空き家バンクしずおか',short:'空き家BK'},
  {full:'アットホーム空き家バンク',short:'アットホーム'},
  {full:'住むなら三島',short:'住むなら三島'},
  {full:'ジモティー',short:'ジモティー'},
  {full:'家いちば',short:'家いちば'},
  {full:'真野開発',short:'真野開発'},
  {full:'不動産創研',short:'不動産創研'},
  {full:'伊豆総合企画',short:'伊豆総合企画'},
  {full:'家っち(新日本住建販売)',short:'家っち'},
  {full:'U2JAPAN三島店',short:'U2JAPAN'},
  {full:'スマイミー静岡',short:'スマイミー'},
  {full:'山いちば',short:'山いちば'},
  {full:'山林バンク',short:'山林BK'},
  {full:'山林売買.net',short:'山林売買'},
  {full:'森林.net',short:'森林net'},
  {full:'日本マウント',short:'日本マウント'},
  {full:'天城オートキャンプ',short:'天城キャンプ'},
  {full:'東海ヤジマ',short:'東海ヤジマ'},
  {full:'田舎暮らし物件.com',short:'田舎暮らし'},
  {full:'ふるさと情報館',short:'ふるさと'},
  {full:'CHINTAI',short:'CHINTAI'},
  {full:'いい部屋ネット',short:'いい部屋'},
  {full:'静岡県営住宅',short:'県営住宅'},
  {full:'ビレッジハウス',short:'ビレッジ'},
];
function shortSite(name){
  name=name||'';
  for(const p of SITE_PREFIXES){if(name.startsWith(p.full))return p.full;}
  return name||'—';
}
// モバイル表示専用: shortSite()が返す正式な正規化名を、さらに画面幅向けに縮めた短縮名にする。
// フィルタ判定・siteOptsForTab()は従来どおりshortSite()（正式名）を使い続け、この関数は
// td内の表示（cellHtmlInnerのsiteケース）からだけ呼ぶ。正式名がSITE_PREFIXESに無い
// （＝一回限りの長い名前でshortSite自体が全文を返した）場合はそのままCSSの省略表示に任せる。
function mobileSite(name){
  const full=shortSite(name);
  const hit=SITE_PREFIXES.find(p=>p.full===full);
  return hit?hit.short:full;
}
// 列フィルタの値取得。'site'だけ短縮名で比較する（opts自体が短縮名の集合のため、生の
// d.siteのままだと絶対に一致しない）。他の列は従来どおり d[k] を直接見る（挙動を変えない）。
function filterValue(k,d){return k==='site'?shortSite(d.site):d[k];}
// 現在タブに出現するサイトの短縮名一覧（CHIMOKU_OPTSと同じ流儀。タブごとに中身が違うため
// colsFor(tab)から呼ぶ。inTabBucketは関数宣言でホイストされるため、テキスト上の定義位置が
// このあとでも呼び出し時点（render時）には問題なく参照できる）。
function siteOptsForTab(tab){
  return [...new Set(DATA.filter(d=>inTabBucket(d,tab)).map(d=>shortSite(d.site)))].sort();
}
// タブ別の列定義。rentタブは坪単価・建築可否・地目を出さず、間取り・築年を出す。
function colsFor(tab){
  if(tab==='rent'){
    return [
      {k:'price',l:'家賃(月)'},
      {k:'shikirei',l:'敷/礼'},
      {k:'madori',l:'間取り'},
      {k:'area',l:'面積'},
      {k:'shubetsu',l:'種別',f:'check',opts:RENT_TYPES},
      {k:'chikunen',l:'築年'},
      {k:'site',l:'掲載サイト',f:'check',opts:siteOptsForTab('rent')},
      {k:'loc',l:'所在地'},
      {k:'machi',l:'市町',f:'check',opts:MACHI},
      {k:'first_seen',l:'検出日'},
      {k:'info',l:'参考情報',nostat:true}
    ];
  }
  return [
    {k:'price',l:'価格'},
    {k:'area',l:'面積'},
    {k:'tsubo',l:'坪単価',f:'range'},
    {k:'shubetsu',l:'種別',f:'check',opts:TYPES},
    {k:'rb',l:'建築可否',f:'check',opts:['○','△','×','不明']},
    {k:'site',l:'掲載サイト',f:'check',opts:siteOptsForTab(tab)},
    {k:'loc',l:'所在地'},
    {k:'machi',l:'市町',f:'check',opts:MACHI},
    {k:'chimoku',l:'地目',f:'check',opts:CHIMOKU_OPTS},
    {k:'first_seen',l:'検出日'},
    {k:'info',l:'参考情報',nostat:true}
  ];
}
// 現在表示中タブの列定義（列ポップアップ等、レンダリング経路の外から参照する箇所用）
function currentCols(){return colsFor(S.tab);}
function esc(s){s=(s==null?'':String(s));return s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function numOrNull(v){v=(''+(v==null?'':v)).trim();return v===''?null:parseFloat(v);}
function hPrice(v){if(v==null)return'';if(v<=300)return'background:#1a7d36;color:#fff';if(v<=600)return'background:#66bb6a';if(v<=1000)return'background:#ffe082';if(v<=2000)return'background:#ffb74d';return'background:#ef9a9a';}
function hArea(v){if(v==null)return'';if(v>=990)return'background:#1a7d36;color:#fff';if(v>=660)return'background:#66bb6a';if(v>=495)return'background:#ffe082';if(v>=330)return'background:#ffb74d';return'background:#ef9a9a';}
function hTsubo(v){if(v==null)return'';if(v<=2)return'background:#1a7d36;color:#fff';if(v<=5)return'background:#66bb6a';if(v<=10)return'background:#ffe082';if(v<=20)return'background:#ffb74d';return'background:#ef9a9a';}
// 賃貸タブ専用ヒートマップ（家賃は万円/月・面積は建物専有面積なので売買の土地面積とは尺度が違う）
function hRent(v){if(v==null)return'';if(v<=3)return'background:#1a7d36;color:#fff';if(v<=4)return'background:#66bb6a';if(v<=5)return'background:#ffe082';if(v<=7)return'background:#ffb74d';return'background:#ef9a9a';}
function hAreaRent(v){if(v==null)return'';if(v>=90)return'background:#1a7d36;color:#fff';if(v>=60)return'background:#66bb6a';if(v>=40)return'background:#ffe082';if(v>=25)return'background:#ffb74d';return'background:#ef9a9a';}
// 検出日の鮮度ヒートマップ（新しいほど濃い緑。4週間超は無着色＝従来どおり）
function hFirstSeen(v){if(v==null||v<0)return'';if(v<=7)return'background:#1a7d36;color:#fff';if(v<=14)return'background:#66bb6a';if(v<=21)return'background:#ffe082';if(v<=28)return'background:#ffb74d';return'';}
function rbClass(m){return {'○':'rb-ok','△':'rb-wn','×':'rb-ng'}[m]||'rb-uk';}
function normLoc(s){return (s||'').replace('静岡県','').replace(/\s+/g,'');}

// ---- 状態 ----
// キャンプ場土地(camp)・賃貸(rent)タブは既定で価格・面積を絞らない（手広く構えて目で選ぶ方針）
function defState(){return{tab:'sarachi',priceMin:0,priceMaxSarachi:1500,priceMaxIe:3000,priceMaxCamp:null,priceMaxRent:null,amin:CONFIG.amin,amax:null,aminCamp:null,amaxCamp:null,aminRent:null,amaxRent:null,cf:{},sort:{k:null,d:1}};}
let S=defState();

// ---- localStorage (akiyawatch_ プレフィックス) ----
const LS_TAB='akiyawatch_tab', LS_PRICE='akiyawatch_price';
const LS_AREA_FILTER='akiyawatch_area', LS_HIDDEN='akiyawatch_hidden';
const LS_FAV='akiyawatch_fav', LS_EXAREAS='akiyawatch_exareas';
const LS_OLD_EXAREAS='akiya.exareas.v2';
function lsGet(k,def){try{const v=JSON.parse(localStorage.getItem(k));return v==null?def:v;}catch(e){return def;}}
// lsSaveRaw: localStorageへの書き込みのみ（同期はしない）。lsSave: 書き込み＋同期スケジュール。
// 呼び出し側（お気に入り/非表示/フィルタ等）は既存どおり lsSave を使えば自動で同期対象になる。
function lsSaveRaw(k,v){try{localStorage.setItem(k,JSON.stringify(v));}catch(e){}}
// DIRTY_KEYS: このタブでローカル変更があり、まだサーバへ確定していないlocalStorageキーの集合。
// キー単位で管理するのは、pull時に「サーバ優先で丸ごと上書き／丸ごとローカルpush」の二択だと、
// 触っていないキー（例: fav）まで巻き添えで失われるため。触ったキーだけローカル優先で残し、
// 触っていないキーはサーバ優先でマージする（syncPull参照）。
// PUT成功時だけ空に戻す。失敗時やpull完走前の編集時は残したままにし、
// 次の同期機会（次の操作 or ページ離脱）で再送する（無限リトライはしない）。
const DIRTY_KEYS=new Set();
function lsSave(k,v){lsSaveRaw(k,v);DIRTY_KEYS.add(k);scheduleSync();}

// ---- 状態同期（多端末共有。任意機能＝?u=キーが無い/不正なブラウザではUKEY=nullになり、
// 以降のfetchは一切発生しない。state-api側の実装は別担当のためここでは触らない）----
const SYNC_API='https://staff.negura.website/akiya-state';
const LS_UKEY='akiyawatch_ukey';
function isValidUkey(v){return typeof v==='string'&&/^[a-z0-9_-]{1,32}$/.test(v);}
// ?u= > localStorage記憶 > null の優先順。?u=off は記憶を消して明示的に同期解除する。
// UKEY_INVALID: ?u=は付いていたが形式不正だったことを覚えておき、後段でバッジに警告を出すためのフラグ
// （UKEY自体はnullのまま＝形式不正の値を記憶キーへフォールバックさせることはしない）。
let UKEY_INVALID=false;
function resolveUkey(){
  let params=[];
  try{params=new URLSearchParams(location.search).getAll('u');}catch(e){}
  if(params.length){
    // ?u=ken&u=off のように複数指定されていても、offが1つでも含まれていれば停止を優先する（大小文字無視）。
    if(params.some(v=>String(v).trim().toLowerCase()==='off')){
      try{localStorage.removeItem(LS_UKEY);}catch(e){}
      return null;
    }
    const uParam=params[0]; // offが無ければ従来どおり最初の指定を採用
    if(isValidUkey(uParam)){try{localStorage.setItem(LS_UKEY,uParam);}catch(e){} return uParam;}
    UKEY_INVALID=true; // 記憶キーへは意図的にフォールバックしない（不正値の温存を防ぐ）
    return null;
  }
  let stored=null;
  try{stored=localStorage.getItem(LS_UKEY);}catch(e){}
  return isValidUkey(stored)?stored:null;
}
// resolveUkey()がLS_UKEYを書き換える前の値を読んでおく（キー切替検知用）。
let _prevUkeyRaw=null;
try{_prevUkeyRaw=localStorage.getItem(LS_UKEY);}catch(e){}
const UKEY=resolveUkey();
// KEY_SWITCHED: 「前回このブラウザで使ったキーと違う」または「このブラウザで初めてキーを使う」。
// この場合はローカルの状態(tab/price/area/hidden/fav/exareas)をシードに使わない＝サーバ側だけを
// 適用する（サーバが空なら空のまま始める）。前の利用者のお気に入り等が別人のキーへ初回シードで
// アップロードされてしまう事故を防ぐため（実例: ?u=ayakoで開いたら前の利用者のローカル★が
// ayakoのサーバ状態へ初回シードされてしまった）。同じキーの再訪なら従来どおりローカルを維持する。
const KEY_SWITCHED=(UKEY!=null&&UKEY!==_prevUkeyRaw);
if(KEY_SWITCHED){
  [LS_TAB,LS_PRICE,LS_AREA_FILTER,LS_HIDDEN,LS_FAV,LS_EXAREAS,LS_OLD_EXAREAS].forEach(k=>{
    try{localStorage.removeItem(k);}catch(e){}
  });
}
// PULLED: このページ表示中にGETが200で完了し、サーバ状態の適用 or シードのどちらかが
// 確定したときだけtrue。falseの間はscheduleSync/syncPushNow/flushSyncNowが一切pushしない
// （貧弱なローカル状態でサーバの正データを上書きする事故を防ぐ）。
let PULLED=false;
if(UKEY_INVALID)setSyncStatus('invalid'); // ?u=形式不正を無言のままにしない
let _syncTimer=null;
// 連続保存（★連打等）で毎回PUTしないためのデバウンス。
// UKEYが無い、またはpull未完走(PULLED=false)なら即return＝fetch自体を作らない。
function scheduleSync(){
  if(!UKEY||!PULLED)return;
  if(_syncTimer)clearTimeout(_syncTimer);
  _syncTimer=setTimeout(()=>{_syncTimer=null;syncPushNow();},800);
}
// state-api契約の6キーを、今のlocalStorageの生の値からそのまま組み立てる。
// 未保存(=null)のキーはpayloadに含めない：「サーバに無いキー＝ローカルを維持」という
// syncPull側の意味論と対にするための取り決めで、ここでnullを送ってサーバ側の値を
// 消してしまわないようにする（syncPull側のフォールバック実装だけでは、他デバイスが
// 先にnullをPUTしてしまえば手遅れになるため、送信側でも防ぐ＝多層防御）。
function buildStatePayload(){
  const p={v:1};
  const tab=lsGet(LS_TAB,null); if(tab!=null)p.tab=tab;
  const price=lsGet(LS_PRICE,null); if(price!=null)p.price=price;
  const area=lsGet(LS_AREA_FILTER,null); if(area!=null)p.area=area;
  const hidden=lsGet(LS_HIDDEN,null); if(hidden!=null)p.hidden=hidden;
  const fav=lsGet(LS_FAV,null); if(fav!=null)p.fav=fav;
  const exareas=lsGet(LS_EXAREAS,null); if(exareas!=null)p.exareas=exareas;
  return p;
}
function fetchWithTimeout(url,opts,ms){
  const ctrl=new AbortController();
  const timer=setTimeout(()=>ctrl.abort(),ms||8000);
  const o=Object.assign({credentials:'omit'},opts,{signal:ctrl.signal});
  return fetch(url,o).finally(()=>clearTimeout(timer));
}
async function syncPushNow(){
  if(!UKEY||!PULLED)return;
  try{
    const res=await fetchWithTimeout(SYNC_API+'/state/'+encodeURIComponent(UKEY),{
      method:'PUT',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({state:buildStatePayload()})
    },8000);
    if(res.ok){DIRTY_KEYS.clear();setSyncStatus('synced');}
    else{
      // PUT失敗(429含む)は静かに握りつぶす。localStorageには既に保存済みなのでデータは失われないが、
      // DIRTY_KEYSは残したままにして次の同期機会(次の操作 or ページ離脱)で再送する。
      if(res.status===404)setSyncStatus('unregistered');else setSyncStatus('local');
    }
  }catch(e){setSyncStatus('local');}
}
// リモート優先で上書きした後、localStorageから状態一式を読み直して再描画する
// （restoreState/HIDDEN/FAVOR/EXAREASの再構築＋applyStateToControls/updateTabUI/renderの一式）。
// S=defState()を先に行うのは、restoreStateが「値が非nullの項目だけSを上書きする」設計のため。
// これをやらないと、サーバに無いキーの分だけ古いメモリ上のS（pull前の値）が残留し、
// 次のユーザー操作でのsaveStateでサーバへ書き戻ってしまう。
// ただしS.sort/S.cfはlocalStorageに永続化されない純UI状態(同期対象外)のため、
// defState()に巻き込まれて黙って消えないよう退避・復元する。
function reloadAllStateFromLocalStorage(){
  const prevSort=S.sort, prevCf=S.cf;
  S=defState();
  S.sort=prevSort; S.cf=prevCf;
  restoreState();
  HIDDEN=loadKV(LS_HIDDEN);
  FAVOR=loadKV(LS_FAV);
  EXAREAS=loadExareas();
  applyStateToControls();
  updateTabUI();
  render();
}
async function syncPull(){
  if(!UKEY)return;
  setSyncStatus('syncing');
  try{
    const res=await fetchWithTimeout(SYNC_API+'/state/'+encodeURIComponent(UKEY),{method:'GET'},8000);
    if(res.status===404){setSyncStatus('unregistered');return;} // 未知キー→同期を諦める(PULLEDは立てない=push禁止のまま)
    if(!res.ok){setSyncStatus('local');return;} // 5xx等→ローカル継続。PULLEDは立てない(=以後push禁止)。
    const data=await res.json();
    const st=(data&&data.state)?data.state:null;
    // 実質空（キーが無い、または全キーがnull）のレコードは「レコード未作成」と同じ扱いにする。
    // でないと、まっさらな端末が先にGETしただけで既存の★・非表示が全消去される。
    const hasContent=!!(st&&Object.keys(st).some(k=>k!=='v'&&st[k]!=null));
    // GET自体はここで確定＝以後pushしても安全（サーバの実データ or その不在を確認済み）。
    // 下のシード分岐が呼ぶsyncPushNow()自身がPULLEDガードを持つため、その前にtrueにする必要がある。
    PULLED=true;
    if(hasContent){
      // キー単位マージ: 「サーバに値がある(非null) かつ このタブでまだ触っていない(DIRTY_KEYSに無い)」
      // キーだけサーバ優先でlocalStorageへ書き戻す。触ったキーはローカルの値を維持する。
      // (旧実装はhasContent&&DIRTYで丸ごとサーバ優先をスキップしローカルを丸ごとpushしていたが、
      //  それだと取りこぼし1件を守るためにサーバ側の他キー(お気に入り等)を全消去してしまっていた)
      const serverKV=[[LS_TAB,st.tab],[LS_PRICE,st.price],[LS_AREA_FILTER,st.area],
        [LS_HIDDEN,st.hidden],[LS_FAV,st.fav],[LS_EXAREAS,st.exareas]];
      serverKV.forEach(([lsKey,val])=>{if(val!=null&&!DIRTY_KEYS.has(lsKey))lsSaveRaw(lsKey,val);});
      reloadAllStateFromLocalStorage();
      setSyncStatus('synced');
      // マージ後のlocalStorageは全キーが揃っている(サーバ由来+ローカル由来)ため、
      // ここでpushしてもサーバ側の他キーが消えることはない。触ったキーが無ければpush不要。
      if(DIRTY_KEYS.size>0)scheduleSync();
    }else{
      // サーバが実質空 → ローカルを正として押し返す（空レコードでの全消去を防ぐ）。
      await syncPushNow();
    }
  }catch(e){
    // ネットワークエラー/タイムアウト → 静かにlocalStorageのまま継続（エラーダイアログは出さない）。
    // PULLEDは立てない(=以後push禁止のまま。貧弱なローカル状態でサーバの正データを上書きしない)。
    setSyncStatus('local');
  }
}
// デバウンス中の離脱（日付ドロップダウンの即時遷移・タブを閉じる等）で保留中の変更が
// 消えるのを防ぐ。UKEY無し/pull未完走(PULLED=false)の端末には一切影響しない(fetchを作らない)。
function flushSyncNow(){
  if(!UKEY||!PULLED)return;
  if(DIRTY_KEYS.size===0)return; // 送るべき変更が無ければ何もしない
  if(_syncTimer){clearTimeout(_syncTimer);_syncTimer=null;}
  // sendBeaconはPUTを送れないため使わない。keepalive:trueのfetchでページ離脱後も送信を継続させる。
  try{
    fetch(SYNC_API+'/state/'+encodeURIComponent(UKEY),{
      method:'PUT',credentials:'omit',keepalive:true,
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({state:buildStatePayload()})
    }).then(res=>{if(res&&res.ok)DIRTY_KEYS.clear();}).catch(()=>{});
  }catch(e){}
}
// pagehideはwindowに発火するイベントで、documentのリスナには届かない(実測確認済み)。
// documentに登録すると常時発火せず、デバウンス中の離脱(日付ドロップダウンの即時遷移等)で
// 保留中のPUTが飛ばずに消える。visibilitychangeはdocumentで正しく発火するのでそのまま。
window.addEventListener('pagehide',flushSyncNow);
document.addEventListener('visibilitychange',()=>{if(document.hidden)flushSyncNow();});
// 沈黙故障に気づくための小さな表示。UKEYがnullの間はこの関数自体が呼ばれない
// （syncPull/syncPushNowが先頭でreturnするため）ので、既定の見た目は一切変わらない。
function setSyncStatus(kind){
  const el=document.getElementById('syncStatus'); if(!el)return;
  el.style.display='inline-block';
  el.classList.remove('st-syncing','st-synced','st-local','st-unregistered','st-invalid');
  el.classList.add('st-'+kind);
  const labels={syncing:'☁ 同期中…',synced:'☁ '+UKEY,local:'⚠ ローカルのみ',unregistered:'⚠ キー未登録',invalid:'⚠ キーが正しくありません'};
  const tips={
    syncing:'この端末の検索条件・お気に入り・非表示を他の端末と照合しています。',
    synced:'この端末の検索条件・お気に入り・非表示は「'+UKEY+'」という名前で他の端末と共有されています。',
    local:'他の端末と通信できていません。この端末には保存されているので操作は失われません。',
    unregistered:'「'+UKEY+'」はまだ使えない名前です。共有するには登録が必要です（この端末には保存されています）。',
    invalid:'URLの?u=の形式が正しくありません（半角英数字と-_のみ、32文字以内）。この端末はローカルのみで動作します。'
  };
  const lab=document.getElementById('syncStatusLabel'); if(lab)lab.textContent=labels[kind]||'';
  const tip=document.getElementById('syncStatusTip'); if(tip)tip.textContent=tips[kind]||'';
}

// 除外エリア: タブ(sarachi/ie/camp/rent)ごとに独立したリストを持つ。使い分けが人によって
// 違う「個人の事情」であってツールの機能ではないため、全タブ同じロジックを適用したうえで
// 中身の管理だけをタブ別にする（賃貸タブだけ除外を無効化する特別扱いは持たない）。
const EXAREA_TABS=['sarachi','ie','camp','rent'];
const TAB_LABELS={sarachi:'更地',ie:'家付き土地',camp:'キャンプ場土地',rent:'賃貸'};
// rentタブは既定=空（サーバ側filters.rent.exclude_areasが空なのに合わせる）。
// sarachi/ie/campはCONFIG.exareas（urls.yaml filters.exclude_areas）を初期値にする。
function defaultExareaList(tab){return tab==='rent'?[]:(CONFIG.exareas||[]).map(n=>({name:n,on:true}));}
function normalizeExareasByTab(obj){
  const out={};
  EXAREA_TABS.forEach(t=>{out[t]=(obj&&Array.isArray(obj[t]))?obj[t]:defaultExareaList(t);});
  return out;
}
// 除外エリア: 新キーになければ旧キー(akiya.exareas.v2)を移行。
// 旧形式は配列1本をタブ共通で使っていた（賃貸タブだけ適用除外という特別扱いがあったため、
// 実質sarachi/ie/campにだけ効いていた）。移行時もその等価な形にする: 配列をsarachi/ie/campの
// 初期値として引き継ぎ、rentは空配列にする（既存ユーザーの設定を壊さない）。
// パース結果が配列でもオブジェクトでもない場合（nullを含む。例えばサーバにexareasが無い状態を
// 旧コードがそのまま書き戻した"null"文字列など）は既定値へフォールバックする。フォールバックしないと
// EXAREASが壊れた形のままcurExareas().some()がTypeErrorになりrender()全体が止まり、
// 全タブが恒久的に0件表示になる（localStorageを手動で消すまで直らない）。
function loadExareas(){
  const nv=localStorage.getItem(LS_EXAREAS);
  if(nv!=null){
    try{
      const v=JSON.parse(nv);
      if(Array.isArray(v)){
        const migrated={sarachi:v,ie:v.map(a=>({...a})),camp:v.map(a=>({...a})),rent:[]};
        lsSaveRaw(LS_EXAREAS,migrated);
        return migrated;
      }
      if(v&&typeof v==='object')return normalizeExareasByTab(v);
    }catch(e){}
  }
  const ov=localStorage.getItem(LS_OLD_EXAREAS);
  if(ov!=null){
    try{
      const v=JSON.parse(ov);
      if(Array.isArray(v)){
        const migrated={sarachi:v,ie:v.map(a=>({...a})),camp:v.map(a=>({...a})),rent:[]};
        lsSaveRaw(LS_EXAREAS,migrated);
        return migrated;
      }
    }catch(e){}
  }
  // 既定値へフォールバックした=localStorageの値が無い/壊れていたということなので、
  // 既定値で上書きして掃除する（放置すると次回以降も同じ壊れた値を読み続ける）。
  const def=normalizeExareasByTab(null);
  lsSaveRaw(LS_EXAREAS,def);
  return def;
}
let EXAREAS=loadExareas();
// 現在のタブの除外エリア配列（無ければ空配列を用意して返す）。
function curExareas(){return EXAREAS[S.tab]||(EXAREAS[S.tab]=[]);}

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
  lsSave(LS_PRICE,{min:S.priceMin,maxSarachi:S.priceMaxSarachi,maxIe:S.priceMaxIe,maxCamp:S.priceMaxCamp,maxRent:S.priceMaxRent});
  lsSave(LS_AREA_FILTER,{min:S.amin,max:S.amax,minCamp:S.aminCamp,maxCamp:S.amaxCamp,minRent:S.aminRent,maxRent:S.amaxRent});
}
function saveHidden(){saveKV(LS_HIDDEN,HIDDEN);}
function saveFav(){saveKV(LS_FAV,FAVOR);}
function saveAreas(){lsSave(LS_EXAREAS,EXAREAS);}

// bdk から表示用スナップショットを作る（レコードを浅くコピー）
function snapOf(rep){
  const keys=['url','dk','loc','price','area','tsubo','shubetsu','shubetsu_reason',
    'rb','rbreason','setsudo','machi','chimoku','first_seen','site','interests','cautions','zokujin','tab',
    'madori','chikunen','kanrihi','shikikin','reikin'];
  const o={}; keys.forEach(k=>o[k]=rep[k]); return o;
}
function bdkOf(g){return g.rep.dk||g.dk;}
// Map(bdk->snap) を描画用グループ配列に。現存グループがあれば最新を使い、無ければ snapshot を使う。
function groupsFromMap(map){
  const out=[];
  map.forEach((snap,bdk)=>{
    const live=GROUPS.find(g=>bdkOf(g)===bdk);
    if(live)out.push(live);
    // snapshotのdays_agoは保存時点の値のまま古びるため持たず、新着扱いにはしない(daysAgo:null)。
    else if(snap)out.push({dk:bdk,rep:snap,sites:[snap.site||'—'],daysAgo:null,gone:true});
  });
  return out;
}

function restoreState(){
  const tab=lsGet(LS_TAB,null);
  if(tab==='sarachi'||tab==='ie'||tab==='camp'||tab==='rent')S.tab=tab;
  const price=lsGet(LS_PRICE,null);
  if(price){if(price.min!=null)S.priceMin=price.min;if(price.maxSarachi!=null)S.priceMaxSarachi=price.maxSarachi;if(price.maxIe!=null)S.priceMaxIe=price.maxIe;if(price.maxCamp!==undefined)S.priceMaxCamp=price.maxCamp;if(price.maxRent!==undefined)S.priceMaxRent=price.maxRent;}
  const area=lsGet(LS_AREA_FILTER,null);
  if(area){S.amin=(area.min!=null?area.min:S.amin);S.amax=(area.max!=null?area.max:S.amax);
    if(area.minCamp!==undefined)S.aminCamp=area.minCamp;if(area.maxCamp!==undefined)S.amaxCamp=area.maxCamp;
    if(area.minRent!==undefined)S.aminRent=area.minRent;if(area.maxRent!==undefined)S.amaxRent=area.maxRent;}
}

// ---- グループ化（JS側dedup: normLoc+面積+価格）----
// daysAgo = グループ内での最小のfirst_seen経過日数（重複掲載のどれかが直近ならグループごと新着扱い）。
const GROUPS=[],KIDX={};
DATA.forEach((d,i)=>{
  d._dk = d.loc ? (normLoc(d.loc)+'|'+d.area+'|'+d.price) : ('u'+i);
  if(KIDX[d._dk]===undefined){KIDX[d._dk]=GROUPS.length;GROUPS.push({dk:d._dk,rep:d,sites:[d.site],daysAgo:(d.days_ago==null?null:d.days_ago)});}
  else{const g=GROUPS[KIDX[d._dk]];if(!g.sites.includes(d.site))g.sites.push(d.site);
    if(d.days_ago!=null&&(g.daysAgo==null||d.days_ago<g.daysAgo))g.daysAgo=d.days_ago;}
});

// ---- フィルタ ----
// タブ(sarachi/ie/camp/rent)への所属判定。キャンプ場土地(camp)・賃貸(rent)は独立タブ:
// 各タブは自分のtabのレコードのみ、homeレコードは更地/家付きのみ。
// NGエリア該当ログ等、DATAをタブ別に絞り込む他の描画でも同じ判定を使い回す。
function inTabBucket(d,tab){
  if(tab==='camp')return d.tab==='camp';
  if(tab==='rent')return d.tab==='rent';
  if(d.tab==='camp'||d.tab==='rent')return false;
  const isHouse=HOUSE_TYPES.has(d.shubetsu);
  if(tab==='sarachi')return !isHouse;
  if(tab==='ie')return isHouse;
  return true;
}
// お気に入り/非表示のスナップショットはtabを持たない旧形式で保存されている場合があるため、
// その場合だけ互換維持として全タブに表示する（tabがあれば inTabBucket と同じ判定）。
function inTabBucketOrUntagged(d,tab){return d.tab==null?true:inTabBucket(d,tab);}
function passFilters(d){
  if(!inTabBucket(d,S.tab))return false;
  // 駐車場は住居ではなく月額数千円のため、混ぜると「安い順」の上位を占めて
  // 住む物件が埋もれる。既定で隠し、種別フィルタで明示的にチェックすれば出す。
  if(S.tab==='rent'&&d.shubetsu==='駐車場'){
    const cf=S.cf['shubetsu'];
    if(!(cf&&cf.set&&cf.set.includes('駐車場')))return false;
  }
  const loc=d.loc||'';
  // 除外エリアは全タブ共通のロジックで適用する（賃貸だけ特別扱いにしない＝個人の使い分けは
  // タブ別リストの中身で吸収する。どのエリアを入れるかは利用者がタブごとに選ぶ）。
  if(curExareas().some(a=>a.on&&a.name&&loc.includes(a.name)))return false;
  for(const k in S.cf){const cf=S.cf[k],v=filterValue(k,d);
    if(cf.t==='range'){if(v==null)return false;if(cf.min!=null&&v<cf.min)return false;if(cf.max!=null&&v>cf.max)return false;}
    else if(cf.t==='check'){if(cf.set&&!cf.set.includes(String(v==null?'—':v)))return false;}
  }
  const pmax=S.tab==='sarachi'?S.priceMaxSarachi:(S.tab==='camp'?S.priceMaxCamp:(S.tab==='rent'?S.priceMaxRent:S.priceMaxIe));
  const amin=S.tab==='camp'?S.aminCamp:(S.tab==='rent'?S.aminRent:S.amin);
  const amax=S.tab==='camp'?S.amaxCamp:(S.tab==='rent'?S.amaxRent:S.amax);
  if(S.priceMin!=null&&S.priceMin>0&&(d.price==null||d.price<S.priceMin))return false;
  if(pmax!=null&&(d.price==null||d.price>pmax))return false;
  if(amin!=null&&(d.area==null||d.area<amin))return false;
  if(amax!=null&&(d.area==null||d.area>amax))return false;
  return true;
}

// ---- テーブル描画（列は colsFor(tab) を見て組み立てる。タブごとに列構成が違う）----
function buildHead(cols){
  let h='<thead><tr><th class=favcell title="お気に入り">★</th>';
  cols.forEach(c=>{
    if(c.nostat){h+="<th data-k='"+c.k+"'>"+esc(c.l)+"</th>";return;}
    const active=S.cf[c.k]?' filtered':'';
    const ar=(S.sort.k===c.k)?(S.sort.d>0?'▲':'▼'):'';
    const fi=(c.f&&S.cf[c.k])?' <span class=fi>⚑</span>':'';
    h+="<th class='col"+active+"' data-k='"+c.k+"'>"+esc(c.l)+" <span class=ar>"+ar+"</span>"+fi+"</th>";
  });
  return h+'<th>詳細</th><th>非表示</th></tr></thead>';
}
// 列キー1つぶんの <td> の中身を組み立てる（実際にrowHtmlから呼ばれるのは下のcellHtml。
// 関数名がInnerなのは、cellHtml側でdata-k付与を薄くラップするため）。price/areaは
// 売買(home/camp)と賃貸(rent)でヒートマップ関数が違う点にだけ注意。
function cellHtmlInner(k,d,g){
  switch(k){
    case 'price':{
      const heat=(d.tab==='rent')?hRent(d.price):hPrice(d.price);
      const label=(d.price==null)?'—':d.price.toLocaleString()+'万';
      return "<td style='"+heat+"'>"+label+"</td>";
    }
    case 'area':{
      const heat=(d.tab==='rent')?hAreaRent(d.area):hArea(d.area);
      return "<td style='"+heat+"'>"+(d.area==null?'—':d.area+'㎡')+"</td>";
    }
    case 'tsubo':
      return "<td style='"+hTsubo(d.tsubo)+"'>"+(d.tsubo==null?'—':d.tsubo)+"</td>";
    case 'shubetsu':
      return "<td title='"+esc(d.shubetsu_reason)+"'>"+esc(d.shubetsu)+"</td>";
    case 'rb':{
      const rbTitle=d.rbreason+(d.setsudo?(' / 接道:'+d.setsudo):'');
      return "<td class='"+rbClass(d.rb)+"' title='"+esc(rbTitle)+"'>"+esc(d.rb)+"</td>";
    }
    case 'site':{
      const others=g.sites.length-1;
      const otherHtml=others>0?" <span class=muted>ほか"+others+"件</span>":"";
      // PC(.sitefull)は従来どおりshortSite()の正式名。モバイル(.sitemobile)はさらに縮めた
      // mobileSite()を出し、CSSで出し分ける（既定は.sitemobileを隠す＝PCは無変更）。
      const full="<span class=sitefull>"+esc(shortSite(d.site))+"</span>";
      const mob="<span class=sitemobile>"+esc(mobileSite(d.site))+"</span>";
      return "<td class='sitecell' title='"+esc(g.sites.join(' / '))+"'>"+full+mob+otherHtml+"</td>";
    }
    case 'loc':{
      const loc=esc(normLoc(d.loc).slice(0,22)||'—');
      // 先頭が郵便番号(〒xxx-xxxx)なら別spanで包む。モバイルCSSでそこだけ隠し、限られた
      // 幅を市町名以降の実際の住所に回す（付かないサイトのデータでも壊れないよう、
      // マッチしない場合はlocをそのまま返す）。
      const m=loc.match(/^(〒\d{3}-\d{4})([\s\S]*)$/);
      const label=m?("<span class=locpost>"+m[1]+"</span>"+m[2]):loc;
      return "<td class='loccell'>"+label+"</td>";
    }
    case 'machi':
      return "<td>"+esc(d.machi||'—')+"</td>";
    case 'chimoku':
      return "<td>"+esc(d.chimoku)+"</td>";
    case 'madori':
      return "<td>"+esc(d.madori||'—')+"</td>";
    case 'chikunen':
      return "<td>"+esc(d.chikunen||'—')+"</td>";
    case 'shikirei':{
      const sk=(d.shikikin==null?'—':d.shikikin), rk=(d.reikin==null?'—':d.reikin);
      const label=(d.shikikin==null&&d.reikin==null)?'—':(sk+' / '+rk);
      return "<td>"+esc(label)+"</td>";
    }
    case 'first_seen':{
      // 日付/相対の2つに分けてspanで包む。モバイルCSSが.fsdateだけdisplay:noneにして
      // 「18日前」の相対表示だけを残す（「2026-07-27（18日前）」は長すぎるため）。
      // 括弧（fsp）もモバイルでは表示自体を無駄にするので別spanにして隠す＝モバイルは
      // 「18日前」のみ、PCは従来どおり「2026-07-27（18日前）」のまま。
      // 色分け(hFirstSeen)のstyleはtd自体に付けるので、どのspanを隠しても維持される。
      const agoNum=(d.days_ago==null||d.days_ago<0)?'':(d.days_ago+'日前');
      const dateHtml="<span class=fsdate>"+esc(d.first_seen||'—')+"</span>";
      const agoHtml=agoNum?("<span class=fsago><span class=fsp>（</span>"+esc(agoNum)+"<span class=fsp>）</span></span>"):"";
      return "<td style='"+hFirstSeen(d.days_ago)+"'>"+dateHtml+agoHtml+"</td>";
    }
    case 'info':{
      let info='';
      (d.interests||[]).forEach(x=>info+="<span class=bi>"+esc(x)+"</span>");
      (d.cautions||[]).forEach(x=>info+="<span class=bc>"+esc(x)+"</span>");
      if(d.zokujin)info+="<span class=bz>属人性</span>";
      if(!info)info='<span class=muted>—</span>';
      return "<td class='infocell'>"+info+"</td>";
    }
    default:
      return "<td>—</td>";
  }
}
// cellHtmlInnerの結果にdata-k属性を付けて返す。data-k: 列キー。th側で既に使っている
// data-k付与と同じ流儀（モバイルの列間引きCSS([data-k=xxx]{display:none})のフックと、
// 価格/所在地の強調表示のCSSフックを兼ねる）。
function cellHtml(k,d,g){
  const html=cellHtmlInner(k,d,g);
  return html.replace('<td',"<td data-k='"+esc(k)+"'");
}
function rowHtml(g,inHidden,cols){
  const d=g.rep;
  const bdk=d.dk||g.dk;
  const op=inHidden?("<button class=restorebtn data-bdk='"+esc(bdk)+"'>戻す</button>")
                   :("<button class=hidebtn data-bdk='"+esc(bdk)+"'>非表示</button>");
  const isFav=FAVOR.has(bdk);
  const fav="<td class=favcell><button class='favbtn"+(isFav?' on':'')+"' data-bdk='"+esc(bdk)+"' title='お気に入り'>"+(isFav?'★':'☆')+"</button></td>";
  let cells='';
  cols.forEach(c=>{cells+=cellHtml(c.k,d,g);});
  return "<tr>"
    +fav
    +cells
    +"<td class='detailcell'><a href='"+esc(d.url)+"' target=_blank>詳細</a></td>"
    +"<td class='hidecell'>"+op+"</td></tr>";
}
function legendRow(ncol,tab){
  if(tab==='rent'){
    return "<tfoot><tr class=legendrow><td colspan="+ncol+">"
      +"<span class=lgline>家賃(安いほど濃い緑):<b style='background:#1a7d36;color:#fff'>≤3</b><b style='background:#66bb6a'>≤4</b><b style='background:#ffe082'>≤5</b><b style='background:#ffb74d'>≤7</b><b style='background:#ef9a9a'>&gt;7</b> 万円/月</span>"
      +"<span class=lgline>面積(広いほど濃い緑):<b style='background:#1a7d36;color:#fff'>≥90</b><b style='background:#66bb6a'>≥60</b><b style='background:#ffe082'>≥40</b><b style='background:#ffb74d'>≥25</b> ㎡</span>"
      +"<span class=lgline>検出日(新しいほど濃い緑):<b style='background:#1a7d36;color:#fff'>1週間以内</b><b style='background:#66bb6a'>2週間以内</b><b style='background:#ffe082'>3週間以内</b><b style='background:#ffb74d'>4週間以内</b><b>それ以前</b></span>"
      +"<span class=lgline>参考情報: <span class=bi>緑=好材料</span> <span class=bc>赤=注意点</span> <span class=bz>属人性</span></span>"
      +"</td></tr></tfoot>";
  }
  return "<tfoot><tr class=legendrow><td colspan="+ncol+">"
    +"<span class=lgline>価格(安いほど濃い緑):<b style='background:#1a7d36;color:#fff'>≤300</b><b style='background:#66bb6a'>≤600</b><b style='background:#ffe082'>≤1000</b><b style='background:#ffb74d'>≤2000</b><b style='background:#ef9a9a'>&gt;2000</b> 万円</span>"
    +"<span class=lgline>面積(広いほど濃い緑):<b style='background:#1a7d36;color:#fff'>≥990</b><b style='background:#66bb6a'>≥660</b><b style='background:#ffe082'>≥495</b><b style='background:#ffb74d'>≥330</b> ㎡</span>"
    +"<span class=lgline>坪単価(安いほど濃い緑):<b style='background:#1a7d36;color:#fff'>≤2</b><b style='background:#66bb6a'>≤5</b><b style='background:#ffe082'>≤10</b><b style='background:#ffb74d'>≤20</b><b style='background:#ef9a9a'>&gt;20</b> 万円/坪</span>"
    +"<span class=lgline>検出日(新しいほど濃い緑):<b style='background:#1a7d36;color:#fff'>1週間以内</b><b style='background:#66bb6a'>2週間以内</b><b style='background:#ffe082'>3週間以内</b><b style='background:#ffb74d'>4週間以内</b><b>それ以前</b></span>"
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
function tbl(list,inHidden,showLegend,cols,ncol,tab){return buildHead(cols)+'<tbody>'+(list.length?list.map(g=>rowHtml(g,inHidden,cols)).join(''):"<tr class='nocard'><td colspan="+ncol+" class=muted>該当なし</td></tr>")+'</tbody>'+(showLegend?legendRow(ncol,tab):'');}
// ---- モバイル専用: 列フィルタ/並べ替えチップ（#colChips）----
// buildHead(cols)のth生成と同じ判定(nostat除外・S.cf[k]で絞り込み中マーク・S.sort.kで矢印)を
// チップにも適用し、PC(th)とモバイル(チップ)で見た目の意味を揃える。クリック時はth.colと同じ
// openPopup(k,anchorEl)を呼ぶ（ポップアップの中身・位置決めロジックはopenPopup側の1箇所のみ）。
function renderColChips(cols){
  const box=document.getElementById('colChips'); if(!box)return;
  let h='';
  cols.forEach(c=>{
    if(c.nostat)return;  // buildHead側でth.colにならない列(参考情報)はチップも出さない
    const filtered=S.cf[c.k]?' filtered':'';
    const ar=(S.sort.k===c.k)?(S.sort.d>0?' ▲':' ▼'):'';
    h+="<button type=button class='colchip"+filtered+"' data-k='"+c.k+"'>"+esc(c.l)+ar+"</button>";
  });
  box.innerHTML=h;
}
function render(){
  const tab=S.tab, cols=colsFor(tab), ncol=cols.length+3;  // +3 = お気に入り★/詳細/非表示
  renderColChips(cols);
  let vis=GROUPS.filter(g=>{const bdk=bdkOf(g);return !HIDDEN.has(bdk)&&passFilters(g.rep);});
  sortGroups(vis);
  // 物件ブラウザ（凡例つき）
  document.getElementById('mainTbl').innerHTML=tbl(vis,false,true,cols,ncol,tab);
  // 新着（凡例なし＝上と重複のため）。直近CONFIG.newWindowDays日以内のみ、新しい順固定
  // （同日内は価格の安い順）。メイン表(vis)の並べ替え状態とは独立させる。
  const nv=vis.filter(g=>g.daysAgo!=null&&g.daysAgo<=CONFIG.newWindowDays);
  nv.sort((A,B)=>{if(A.daysAgo!==B.daysAgo)return A.daysAgo-B.daysAgo;
    const pa=(A.rep.price==null)?1e12:A.rep.price,pb=(B.rep.price==null)?1e12:B.rep.price;return pa-pb;});
  document.getElementById('newTbl').innerHTML=tbl(nv,false,false,cols,ncol,tab);
  // お気に入り（消滅まで残す。除外エリア等の絞り込みは無視して常に表示。ただしタブは絞る＝
  // 賃貸タブを見ているのに更地の★が出ないように。tab無し(旧形式)は互換維持で全タブ表示）
  const fav=sortGroups(groupsFromMap(FAVOR).filter(g=>inTabBucketOrUntagged(g.rep,tab)));
  document.getElementById('favTbl').innerHTML=tbl(fav,false,false,cols,ncol,tab);
  const favC=document.getElementById('favCnt');if(favC)favC.textContent='('+fav.length+'件)';
  // 非表示（消滅まで残す。同様にタブで絞る）
  const hid=sortGroups(groupsFromMap(HIDDEN).filter(g=>inTabBucketOrUntagged(g.rep,tab)));
  document.getElementById('hiddenTbl').innerHTML=tbl(hid,true,false,cols,ncol,tab);
  document.getElementById('hiddenCnt').textContent='('+hid.length+'件)';
  // NGエリア該当ログ・消滅・サイト別サマリもタブ別に絞ってJS描画（mainTbl等と同じ流儀）。
  renderNgLog();
  renderDisappeared();
  renderSummary();
  // 件数表示（内部用語"グループ"を使わない）
  const msg='該当 '+vis.length+'件（全'+GROUPS.length+'件中）・新着 '+nv.length+'件';
  document.getElementById('cnt').textContent=msg;
  const ct=document.getElementById('cntTop');if(ct)ct.textContent='該当 '+vis.length+'件・新着 '+nv.length+'件';
  updateTabUI();
}

// ---- NGエリア該当ログ（DATAのng===trueをタブ別に抽出。mainTbl等と同じくJS描画）----
function renderNgLog(){
  const box=document.getElementById('secNg'); if(!box)return;
  const tab=S.tab;
  const rows=DATA.filter(d=>d.ng&&inTabBucket(d,tab));
  const cnt=document.getElementById('ngCnt'); if(cnt)cnt.textContent='（'+rows.length+' 件）';
  if(!rows.length){box.innerHTML="<p class='muted'>NGエリア該当なし。</p>";return;}
  let h="<table><tr><th>サイト</th><th>種別</th><th>所在地</th><th>価格</th><th>土地面積</th>"
       +"<th>NGエリア</th><th>詳細</th></tr>";
  rows.forEach(d=>{
    const price=(d.price==null)?'—':d.price.toLocaleString()+'万円';
    const area=(d.area==null)?'—':d.area+'㎡';
    h+="<tr><td>"+esc(d.site)+"</td><td>"+esc(d.shubetsu||'—')+"</td>"
      +"<td>"+esc(normLoc(d.loc).slice(0,20)||'—')+"</td>"
      +"<td>"+price+"</td><td>"+area+"</td>"
      +"<td class='flag'>"+esc((d.ng_areas||[]).join('、'))+"</td>"
      +"<td><a href='"+esc(d.url)+"' target='_blank'>詳細</a></td></tr>";
  });
  box.innerHTML="<div class='tblwrap'>"+h+"</table></div>";
}

// ---- サイト所属タブ(home/camp/rent) と 画面タブ(sarachi/ie/camp/rent) の対応。
// homeサイトは更地/家付き土地の両方を出しうるため、両タブに属するものとして扱う
// （消滅・サイト別サマリはサイト単位の情報で、DATAのようなshubetsuによる細分ができないため）----
function siteBelongsToUiTab(siteTab,uiTab){
  if(uiTab==='camp')return siteTab==='camp';
  if(uiTab==='rent')return siteTab==='rent';
  return siteTab!=='camp'&&siteTab!=='rent';
}

// ---- 消滅（DISAPPEARED をサイトのtab属性でタブ別に描画）----
function renderDisappeared(){
  const box=document.getElementById('secGone'); if(!box)return;
  const tab=S.tab;
  const rows=DISAPPEARED.filter(d=>siteBelongsToUiTab(d.tab,tab));
  const cnt=document.getElementById('goneCnt'); if(cnt)cnt.textContent='（'+rows.length+' 件・ページ削除から7日以内）';
  if(!rows.length){box.innerHTML="<p class='muted'>消滅物件なし。</p>";return;}
  let h="<table><tr><th>サイト</th><th>所在地</th><th>価格</th><th>面積</th>"
       +"<th>消滅検出日</th><th>経過</th><th>詳細</th></tr>";
  rows.forEach(d=>{
    h+="<tr><td>"+esc(d.site)+"</td><td>"+esc(d.loc||'—')+"</td>"
      +"<td>"+esc(d.price)+"</td><td>"+esc(d.area)+"</td><td>"+esc(d.removedOn)+"</td>"
      +"<td>"+d.days+"日前</td>"
      +"<td><a href='"+esc(d.url)+"' target='_blank'>詳細</a></td></tr>";
  });
  box.innerHTML="<div class='tblwrap'>"+h+"</table></div>";
}

// ---- サイト別サマリ（SUMMARY をサイトのtab属性でタブ別に描画）----
function renderSummary(){
  const box=document.getElementById('secSummary'); if(!box)return;
  const tab=S.tab;
  const rows=SUMMARY.filter(r=>siteBelongsToUiTab(r.tab,tab));
  if(!rows.length){box.innerHTML="<p class='muted'>該当サイトなし。</p>";return;}
  let h="<table><tr><th>ID</th><th>サイト名</th><th>HTTP</th><th>方式</th><th>抽出</th>"
       +"<th>価格取得</th><th>面積取得</th><th>基準内(参考)</th><th>新着</th><th>NG該当</th>"
       +"<th>status</th><th>備考</th></tr>";
  rows.forEach(r=>{
    h+="<tr><td>"+esc(r.id)+"</td><td>"+esc(r.name)+"</td>"
      +"<td>"+esc(r.http)+"</td><td>"+esc(r.mode)+"</td><td>"+r.raw+"</td>"
      +"<td>"+r.priceCnt+"</td><td>"+r.areaCnt+"</td>"
      +"<td class='num-fit'>"+r.fitCnt+"</td>"
      +"<td class='num-new'>"+r.addedCnt+"</td><td>"+r.ngCnt+"</td>"
      +"<td>"+esc(r.status)+"</td><td>"+esc(r.note)+"</td></tr>";
  });
  box.innerHTML="<div class='tblwrap'>"+h+"</table></div>";
}

// ---- タブ ----
function updateTabUI(){
  document.querySelectorAll('.tab-btn').forEach(btn=>{btn.classList.toggle('active',btn.dataset.tab===S.tab);});
  // body[data-tab]: colsForは分岐させない方針を維持したまま、CSS側でタブ別の出し分け
  // （モバイルでrentタブだけ面積列を隠す等）をするためのフック。render()末尾から必ず呼ばれる
  // updateTabUI()に置くことで、タブ切替・フィルタ変更・★/非表示操作のどの再描画でも追随する。
  document.body.dataset.tab=S.tab;
  // 参考情報の本命/注意キーワード説明文はタブで意味が違うため賃貸タブでは filters.rent の値に差し替える
  const isRent=(S.tab==='rent');
  const iTxt=document.getElementById('interestKwText');
  const cTxt=document.getElementById('cautionKwText');
  if(iTxt)iTxt.textContent=((isRent?CONFIG.interestRent:CONFIG.interestBase)||[]).join('、')||'なし';
  if(cTxt)cTxt.textContent=((isRent?CONFIG.cautionRent:CONFIG.cautionBase)||[]).join('、')||'なし';
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
  if(pmax)pmax.value=(S.tab==='sarachi'?S.priceMaxSarachi:(S.tab==='camp'?S.priceMaxCamp:(S.tab==='rent'?S.priceMaxRent:S.priceMaxIe)))||'';
  const camp=(S.tab==='camp'), rent=(S.tab==='rent');
  const amin=camp?S.aminCamp:(rent?S.aminRent:S.amin), amax=camp?S.amaxCamp:(rent?S.amaxRent:S.amax);
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
  const list=curExareas();
  box.innerHTML=list.length?list.map(a=>
    "<div class=arow><label><input type=checkbox class=areachk data-name='"+esc(a.name)+"' "+(a.on?'checked':'')+"> "+esc(a.name)+"</label><b class=delx data-name='"+esc(a.name)+"'>×</b></div>"
  ).join(''):"<div class=muted>（除外エリアなし）</div>";
  // 今どのタブの除外リストを編集しているかを見出しに出す（全タブ共通の機能である旨も分かるように）
  const lbl=document.getElementById('areaPopTabLabel'); if(lbl)lbl.textContent='（'+(TAB_LABELS[S.tab]||S.tab)+'）';
}

// ---- 列ヘッダ ポップアップ ----
// openPopup(k,anchorEl): kは列キー、anchorElは位置決めの基準要素。
//   PC(th.colクリック)・モバイル(絞り込みチップタップ)の両方からこの1つの関数を呼ぶ
//   （ロジックを複製しない）。位置決めだけ経路で分岐する:
//   - PC(>700px): 従来どおりanchorEl(th)のgetBoundingClientRectを使う（この分岐の中身は
//     モバイル対応前と1文字も変えていない）。
//   - モバイル(<=700px): チップは折り返し行内で位置が毎回ぶれ、th同様の絶対配置だと
//     画面外にはみ出しうるため、#popup.mobilepos(CSS)で画面中央にfixed表示する。
function closePopup(){const pop=document.getElementById('popup');pop.style.display='none';pop.classList.remove('mobilepos');}
function openPopup(k,anchorEl){
  const col=currentCols().find(c=>c.k===k), pop=document.getElementById('popup');
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
  if(window.innerWidth<=700){
    pop.classList.add('mobilepos'); pop.style.left=''; pop.style.top='';
  } else {
    pop.classList.remove('mobilepos');
    const r=anchorEl.getBoundingClientRect();
    pop.style.left=(window.scrollX+r.left)+'px'; pop.style.top=(window.scrollY+r.bottom+2)+'px';
  }
  pop.style.display='block';
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
      const all=currentCols().find(c=>c.k===k).opts; if(set.length===all.length)delete S.cf[k]; else S.cf[k]={t:'check',set:set};}
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

// ---- モバイル: 見出し行sticky(B-3)用に.topbarの実高さを測って--topbarHへ反映する。
// タイトル文字列の長さや同期バッジ(#syncStatus)の表示有無で高さが変わるため、固定px値
// ではなく実測する（CSS側の.listtbl thead th{top:var(--topbarH,88px)}が参照する。
// このCSS変数を使うルールは@media(max-width:700px)の中にしか無いためPC表示には影響しない）。
function syncTopbarHeight(){
  const tb=document.querySelector('.topbar');
  if(!tb)return;
  document.documentElement.style.setProperty('--topbarH',tb.offsetHeight+'px');
}
syncTopbarHeight();
window.addEventListener('resize',syncTopbarHeight);
window.addEventListener('orientationchange',syncTopbarHeight);
// リサイズを伴わない高さ変化（同期バッジのラベル変化での折り返し等）にも追随させる。
if(window.ResizeObserver){
  new ResizeObserver(syncTopbarHeight).observe(document.querySelector('.topbar'));
}

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
    else if(S.tab==='rent')S.priceMaxRent=numOrNull(e.target.value);
    else S.priceMaxIe=numOrNull(e.target.value);
    saveState();render();});

  // 面積は坪で入力。㎡は読取専用の参考表示(span)を更新。内部Sはsqmで保持。
  // camp/rentタブは独立の下限/上限（既定=絞らない）を編集する。
  document.getElementById('aminTsuboInput').addEventListener('input',e=>{
    const t=numOrNull(e.target.value);const v2=(t==null?null:parseFloat(tsuboToSqm(t)));
    if(S.tab==='camp')S.aminCamp=v2; else if(S.tab==='rent')S.aminRent=v2; else S.amin=v2;
    const v=document.getElementById('aminSqmView');if(v)v.textContent=(v2==null?'—':(+v2).toFixed(1));
    saveState();render();});
  document.getElementById('amaxTsuboInput').addEventListener('input',e=>{
    const t=numOrNull(e.target.value);const v2=(t==null?null:parseFloat(tsuboToSqm(t)));
    if(S.tab==='camp')S.amaxCamp=v2; else if(S.tab==='rent')S.amaxRent=v2; else S.amax=v2;
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
    const list=curExareas();
    if(v&&!list.some(a=>a.name===v)){list.push({name:v,on:true});saveAreas();document.getElementById('areaInput').value='';renderAreaList();render();}}
  document.getElementById('areaAdd').addEventListener('click',e=>{e.stopPropagation();addArea();});
  document.getElementById('areaInput').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();addArea();}});
  areaPop.addEventListener('click',e=>{e.stopPropagation();
    const del=e.target.closest('.delx'); if(del){EXAREAS[S.tab]=curExareas().filter(a=>a.name!==del.dataset.name);saveAreas();renderAreaList();render();return;}
    const chk=e.target.closest('.areachk'); if(chk){const a=curExareas().find(a=>a.name===chk.dataset.name);if(a){a.on=chk.checked;saveAreas();render();}}});

  document.addEventListener('click',e=>{
    const fb=e.target.closest('.favbtn'); if(fb){e.stopPropagation();const bdk=fb.dataset.bdk;
      if(FAVOR.has(bdk))FAVOR.delete(bdk); else FAVOR.set(bdk,snapOf(repByBdk(bdk)));
      saveFav();render();return;}
    const th=e.target.closest('th.col'); if(th){e.stopPropagation();openPopup(th.dataset.k,th);return;}
    const chip=e.target.closest('.colchip'); if(chip){e.stopPropagation();openPopup(chip.dataset.k,chip);return;}
    const hb=e.target.closest('.hidebtn'); if(hb){e.stopPropagation();openHideModal(repByBdk(hb.dataset.bdk));return;}
    const rb=e.target.closest('.restorebtn'); if(rb){HIDDEN.delete(rb.dataset.bdk);saveHidden();render();return;}
    const sec=e.target.closest('h2.sec'); if(sec){const t=document.getElementById(sec.dataset.target);sec.classList.toggle('open');t.classList.toggle('open');return;}
    const pop=document.getElementById('popup'); if(pop.style.display==='block'&&!pop.contains(e.target))closePopup();
    const ap=document.getElementById('areaPop'); if(ap.style.display==='block'&&!ap.contains(e.target)&&e.target.id!=='areaBtn')ap.style.display='none';
  });

  render();
  // localStorageでの即時描画はここまでで完了。UKEYがある時だけ非同期でサーバ状態を取りに行く
  // （無ければsyncPullは先頭でreturnし、fetchは一切発生しない）。
  if(UKEY)syncPull();
})();
"""


def _site_status(row) -> str:
    h = row["http"]
    if row.get("mode") == "rebuild":
        return "rebuild再生成（HTTP未実行）"
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
            cnt = r["raw"] if r.get("mode") in ("adapter", "rebuild") else "—"
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
    parser.add_argument(
        "--rebuild", action="store_true",
        help="クロールせず、保存済みスナップショット(data/snapshots)から確認用ページ"
             "reports/_preview.html だけを数十秒で再生成する（HTTP通信なし）。"
             "本番レポート(index.html/日付別html/csv/SOURCES.md)は一切書き換えない。"
             "物件データは前回クロール時点のもの（本修正以降にクロール済みのサイトは"
             "種別/間取り/敷金礼金/建築可否も通常実行と同精度で復元。未クロールのサイトの"
             "みフォールバックの簡易表示）"
             "（--only/--dry-runを同時に指定しても無視される。単独で使うこと）。",
    )
    args = parser.parse_args()
    if args.rebuild:
        sys.exit(rebuild())
    sys.exit(run(dry_run=args.dry_run, only=args.only))
