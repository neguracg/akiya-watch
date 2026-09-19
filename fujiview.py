"""fujimap 連携: 大字ごとの富士山可視度データの読込・住所照合。

# @owns 富士山可視度（fujimap oaza_scores.json の読込と大字照合）

兄弟プロジェクト fujimap（C:\\Claude\\31_fujimap）が国土地理院DEMの視線判定を200mメッシュで
計算し、大字（町丁・字等）単位に集計した `data/fujimap/oaza_scores.json` を読み、akiya-watch の
物件レコード（所在地・市町）から該当する大字の可視度レンジを引く。watch.py はこのモジュールを
呼ぶだけで、読込・照合ロジック自体はここが唯一の所有者（指示書 docs/tasks/20260919_fujimap_link.md）。

入力データの形（fujimap 側の出力仕様。詳細は指示書・data/fujimap/README.md 参照）:
    {"rows": [{"pref":"静岡県","city":"函南町","oaza":"平井","n":123,
               "min":0,"p10":5,"p50":40,"p90":80,"max":95,"method":"cells"}, ...]}
`city` は郡名なし（akiya-watch の `machi` と同じ表記）。`oaza` は「大字」接頭・「丁目」を除いた
正規化名。`n=0, method="point"` は代表点1セルのみの小さい町丁目。
意味: 0=見えない、100=麓近くまで見通せる（地形のみ。樹木・建物は非考慮）。
"""
import json
import re
import unicodedata
from pathlib import Path

DEFAULT_TABLE_PATH = Path(__file__).resolve().parent / "data" / "fujimap" / "oaza_scores.json"

_KANJI_RE = re.compile(r"[一-鿿々]")  # CJK統合漢字（々含む）

_index_cache = None  # {city: {oaza: row}}。load_table() が実ファイルを読んで埋めるキャッシュ


def oaza_hit(oaza: str, text: str) -> bool:
    """大字名がtext中に単独の地名として出現するかを判定する（interest_groups・fujiview.lookup用）。

    地番は「大字名+数字」で続くのが通常のため、一致直後が別の漢字だと「地名の続き」
    （＝実は別の大字）である疑いが強い。例: 御殿場市の対象大字「神山」は、一致直後に
    「平」が続く「神山平」（対象外の別の大字。2026-09-05実測で本番データに出現し発見）
    まで拾ってしまう。直後が「字」（大字+字+小字の継続。例:「深沢字二子」）のときだけは
    正しい継続として許容し、それ以外の漢字が続く場合は不一致として扱う。

    2026-09-19: watch.py の _make_record（interest_groups判定）に加え、本モジュールの
    lookup()（富士山可視度の大字照合）からも同じ規則で呼ばれる（watch.py からは
    `from fujiview import oaza_hit as _oaza_hit` で従来名のまま参照される）。
    """
    start = 0
    while True:
        idx = text.find(oaza, start)
        if idx == -1:
            return False
        tail = text[idx + len(oaza): idx + len(oaza) + 1]
        if not tail or tail == "字" or not _KANJI_RE.match(tail):
            return True
        start = idx + 1  # この出現は別地名の一部。次の出現を探す


def _prefix_hit(oaza: str, residual: str) -> bool:
    """residual の先頭が oaza と一致するか（先頭一致には「直後が別漢字なら不一致」の
    ガードを掛けない）。

    oaza_hit() のガードは interest_groups のように候補が「対象の大字だけ」（不完全な集合）
    のときに要る（「神山」しか候補に無いと「神山平」を神山と誤認する）。ここで使う候補は
    fujimap の e-Stat 小地域表＝その市町の大字の**完全な一覧**なので、より長い大字名が
    実在すれば必ず候補にあり、lookup() の「最長一致」がそれを選ぶ（神山/神山平は両方が
    行として存在する。2026-09-19 実データで確認）。逆にガードを掛けると
    「奈古谷小松ケ原」（大字 奈古谷 の中の字名）のような正当な細分が全部落ちる
    （2026-09-19 実測: 未照合270件のうち61件がこの型）。
    """
    return bool(oaza) and residual.startswith(oaza)


def load_table(path=None):
    """oaza_scores.json を読み {city: {oaza: row}} の索引にして返す。

    path省略時は DEFAULT_TABLE_PATH をモジュール内キャッシュ付きで読む（プロセス内で
    実ファイルアクセスは1回だけ。2回目以降はキャッシュを返す）。path明示時は常にその
    ファイルで読み直し、以降のキャッシュもそれに置き換える（テストが fixture を注入する
    ための経路。他の呼び出し元は path を渡さない＝lookup()が使うキャッシュも自動更新される）。
    ファイルが無ければ空索引を返し、標準出力に1行だけ注意を出す（起動のたびに毎回ではなく、
    実際に読み込みを試みた＝キャッシュ未生成の時だけ）。
    """
    global _index_cache
    if path is None:
        if _index_cache is not None:
            return _index_cache
        path = DEFAULT_TABLE_PATH
    p = Path(path)
    if not p.exists():
        # cp932環境（Windowsコンソールのデフォルト）でも落ちないよう、この1行は
        # cp932にない記号（em dash等）を使わない（watch.py側のUTF-8 reconfigureが
        # 先に効いていない単体import・pytest実行時でも安全に出せるようにするため）。
        print(f"[fujiview] 富士山可視度データが見つかりません: {p}"
              "（富士山列は空欄のまま表示します。data/fujimap/README.md 参照）")
        _index_cache = {}
        return _index_cache
    raw = json.loads(p.read_text(encoding="utf-8"))
    idx = {}
    for row in raw.get("rows", []):
        city, oaza = row.get("city"), row.get("oaza")
        if not city or not oaza:
            continue
        idx.setdefault(city, {})[oaza] = row
    _index_cache = idx
    return _index_cache


def lookup(location: str, machi: str) -> dict | None:
    """住所文字列(location)と市町(machi)から、大字単位の富士山可視度を引く。

    1. machiが空、または索引にその市町が無ければNone。
    2. locationをNFKC正規化して空白を除去したものをtextとする。machiがtextに含まれれば
       machiの直後からを残り文字列、含まれなければtext全体を残り文字列とする。
       残り文字列の先頭の「大字」は除く。
    3. その市町のoaza一覧のうち残り文字列の先頭に一致する最長のものを採用
       （ガード無し・最長一致。_prefix_hit）。無ければ、残り文字列中に oaza_hit() で
       単独の地名として出現する最長のものを採用。それも無ければ細分名の束ね（_group_lookup）。
    4. 戻り値 {"lo":p10,"hi":p90,"med":p50,"max":max,"n":n,"oaza":採用した大字名,"method":method}。
    """
    idx = load_table()
    candidates = idx.get(machi) if machi else None
    if not candidates:
        return None

    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", location or ""))
    pos = text.find(machi)
    residual = text[pos + len(machi):] if pos != -1 else text
    if residual.startswith("大字"):
        residual = residual[2:]

    best = None
    for oaza in candidates:
        if _prefix_hit(oaza, residual) and (best is None or len(oaza) > len(best)):
            best = oaza
    if best is None:
        for oaza in candidates:
            if oaza_hit(oaza, residual) and (best is None or len(oaza) > len(best)):
                best = oaza
    if best is None:
        return _group_lookup(candidates, residual)

    row = candidates[best]
    return {
        "lo": row.get("p10"),
        "hi": row.get("p90"),
        "med": row.get("p50"),
        "max": row.get("max"),
        "n": row.get("n"),
        "oaza": best,
        "method": row.get("method"),
    }


_HEAD_CUT_RE = re.compile(r"[0-9０-９\-－‐ー、,，字（(「]")


def _group_lookup(candidates: dict, residual: str) -> dict | None:
    """大字そのものの行が無く、細分名（「中之郷かぎあな」「仁科大浜」「井ノ口宮上」）
    しか表に無いときの救済。住所の残り文字列の先頭（地番・「字」・区切りの手前まで）を
    大字名とみなし、それで始まる細分の行を**全部まとめて**1つのレンジにする。

    e-Stat の小地域は大字を丸ごと持たず細分だけで持つ市町がある（2026-09-19 実測:
    富士市152行中「中之郷」単独無し・「中之郷○○」8行）。細分の和集合＝大字なので、
    レンジは min/max の外側、p10/p90 は各細分の最小/最大を取る（広めに出る側に倒す。
    「候補を消さない」方針）。中央値は細分の p50 のセル数加重平均（近似）。

    先頭は縮めない（縮めると「富士ヶ嶺…」が「富士見○○」を束ねる誤爆になる。2026-09-19
    実データで発生）。2文字未満も束ねない。
    """
    prefix = _HEAD_CUT_RE.split(residual, 1)[0]
    if len(prefix) < 2:
        return None
    rows = [r for name, r in candidates.items() if name.startswith(prefix)]
    if not rows:
        return None
    n_total = sum(r.get("n") or 0 for r in rows)
    if n_total > 0:
        med = round(sum((r.get("p50") or 0) * (r.get("n") or 0) for r in rows) / n_total)
    else:
        med = round(sum(r.get("p50") or 0 for r in rows) / len(rows))
    return {
        "lo": min(r.get("p10") for r in rows),
        "hi": max(r.get("p90") for r in rows),
        "med": med,
        "max": max(r.get("max") for r in rows),
        "n": n_total,
        "oaza": f"{prefix}〜({len(rows)}小地域)",
        "method": "group",
    }


def summarize(hit: dict):
    """lookup()の非None戻り値から (表示ラベル, 最大値) を作る（CSV用）。

    HTML側はJS（watch.py の _FILTER_JS 内 cellHtmlInner の case 'fuji'）が同じ規則を
    実装しており、同じ知識を2箇所に持たないよう規則の定義自体はここが正本（両側の
    コードコメントで相互参照する）。
    規則: 最大値0→"0"（見えない）／p90-p10<=5→中央値1つだけ／それ以外→"下限〜上限"。
    """
    if hit["max"] == 0:
        return "0", hit["max"]
    if hit["hi"] - hit["lo"] <= 5:
        return str(hit["med"]), hit["max"]
    return f"{hit['lo']}〜{hit['hi']}", hit["max"]


# ---- watch.py からの呼び出し口（1行で済ませ、None処理もここに閉じ込める）----

def embed_fields(location: str, machi: str) -> dict:
    """build_html_report のJSON埋め込み用。fuji/fuji_lo/fuji_med/fuji_max/fuji_n/fuji_oaza
    の6キーを返す（ヒット無しは全部None）。watch.py側のJS（snapOf/cellHtmlInnerのcase
    'fuji'）はこの6キー名と対応する。"""
    hit = lookup(location, machi)
    if hit is None:
        return {"fuji": None, "fuji_lo": None, "fuji_med": None,
                "fuji_max": None, "fuji_n": None, "fuji_oaza": None}
    return {"fuji": hit["hi"], "fuji_lo": hit["lo"], "fuji_med": hit["med"],
            "fuji_max": hit["max"], "fuji_n": hit["n"], "fuji_oaza": hit["oaza"]}


def csv_fields(location: str, machi: str):
    """write_csv_report用。(表示ラベル, 最大値) を返す（ヒット無しは ("","") ＝空欄）。"""
    hit = lookup(location, machi)
    return ("", "") if hit is None else summarize(hit)
