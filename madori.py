"""間取り表記 → 部屋数の解釈。

# @owns 間取りテキスト（"4LDK"/"8SLDK"/"ワンルーム"/"3DK･2LDK" 等）から部屋数を読む規則

賃貸タブの適合判定（watch.py の _make_record。`filters.rent.rooms_min`）が呼ぶだけで、
読み方の規則はここが唯一の所有者。閾値（何部屋以上か）は urls.yaml が唯一の定義元で、
このモジュールは数値を持たない。
"""
import re
import unicodedata

# 先頭の数字＝部屋数。S(納戸)・L・D・K は部屋数に数えない（4SLDK→4、8SLDK→8）。
# 「2LDK+3S」のS単独は拾わない（K/DK/LDK/SK等の語尾が付く数字だけが部屋数）。
_ROOMS_RE = re.compile(r"(\d+)\s*(?:SLDK|LDK|SDK|DK|SK|K|R)")
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
