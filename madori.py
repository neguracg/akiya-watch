"""間取り表記 → 部屋数の解釈。

# @owns 間取りテキスト（"4LDK"/"8SLDK"/"ワンルーム"/"3DK･2LDK" 等）から部屋数を読む規則

賃貸タブの適合判定（watch.py の _make_record。`filters.rent.rooms_min`）が呼ぶだけで、
読み方の規則はここが唯一の所有者。閾値（何部屋以上か）は urls.yaml が唯一の定義元で、
このモジュールは数値を持たない。

レポート画面（JS）は Python の rooms_of を呼べないため、同じ規則の写し ROOMS_JS を持つ
（賃貸タブで部屋数が rooms_min 未満の行を一覧から外す表示専用。2026-11-09 #954）。
規則を変えるときは rooms_of と ROOMS_JS を必ず同時に直す。一致は tests/test_rent_rooms.py が
同じ入力表で固定する（node があるとき）。
"""
import re
import unicodedata

# 先頭の数字＝部屋数。S(納戸)・L・D・K は部屋数に数えない（4SLDK→4、8SLDK→8）。
# 「2LDK+3S」のS単独は拾わない（K/DK/LDK/SK等の語尾が付く数字だけが部屋数）。
_ROOMS_RE = re.compile(r"(\d+)\s*(?:SLDK|LDK|SDK|DK|LK|SK|K|R)")
_ONE_ROOM = "ワンルーム"


def rooms_of(madori):
    """間取りテキストの部屋数（int）。読めなければ None（＝数値不明）。

    複数の間取りが列挙された表記（県営住宅の「1DK･1LDK･2DK…」等）は最大値を返す
    （1棟の中に条件を満たす間取りが1つでもあれば候補として残すため）。
    ワンルーム/1R は 1。
    """
    if not madori:
        return None
    s = unicodedata.normalize("NFKC", str(madori)).upper()
    found = [int(n) for n in _ROOMS_RE.findall(s)]
    if _ONE_ROOM in s:
        found.append(1)
    return max(found) if found else None


# rooms_of の JS 写し。対応: _ROOMS_RE=ROOMS_RE（NFKC正規化後に照合するため \d は ASCII で足りる）／
# found の最大値＝best／ワンルーム→1／読めなければ null。passRooms は賃貸タブ（S.tab==='rent'）で
# CONFIG.rentRoomsMin（urls.yaml filters.rent.rooms_min）が正のときだけ効き、部屋数が読めて下限未満の
# 行だけを外す（間取り不明は残す＝Python 側の「数値不明」と同じ流儀）。watch.py の passFilters が呼ぶ。
ROOMS_JS = r"""
const ROOMS_RE=/(\d+)\s*(?:SLDK|LDK|SDK|DK|LK|SK|K|R)/g;
function roomsOf(m){
  if(!m)return null;
  const s=String(m).normalize('NFKC').toUpperCase();
  let best=null;
  for(const x of s.matchAll(ROOMS_RE)){const n=parseInt(x[1],10);if(best===null||n>best)best=n;}
  if(s.includes('ワンルーム')&&(best===null||best<1))best=1;
  return best;
}
function passRooms(d){
  const mn=CONFIG.rentRoomsMin;
  if(S.tab!=='rent'||!(mn>0))return true;
  const r=roomsOf(d.madori);
  return r===null||r>=mn;
}
"""
