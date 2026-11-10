# -*- coding: utf-8 -*-
"""行の赤枠マーク（rowmark.py）の組み込み固定。挙動そのものはヘッドレスブラウザで実測（WORKLOG参照）。

# @owns 行の赤枠マークが watch.py の出力へ配線されていること（構造試験）
"""
import watch
import yaml
from pathlib import Path

_FILTERS = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "urls.yaml").read_text(encoding="utf-8"))["filters"]


def _html():
    watch._MACHI_NAMES = watch._build_machi_names(_FILTERS)
    return watch.build_html_report([], dict(_FILTERS), [], False)


def test_rowmark_is_wired_into_report():
    html = _html()
    # MARKED は const（TDZ）。_FILTER_JS 末尾の初回 render() より前に定義されていないと一覧ごと壊れる
    assert html.index("const MARKED=") < html.index("function rowHtml(")
    assert "'akiyaMarked'" in html and "tr.marked td" in html
    # 描画側（tr に data-bdk と class）とクリック側（既存の document click から呼ぶ）の両方が繋がっている
    assert 'return "<tr"+markAttr(bdk)+">"' in html
    assert "rowMarkClick(e);" in html


def test_rowmark_does_not_touch_server_state():
    """★・非表示と違い端末ローカルの一時印＝VPS状態API・localStorage には載せない。"""
    import rowmark
    assert "sessionStorage" in rowmark.JS
    assert "localStorage" not in rowmark.JS and "syncPush" not in rowmark.JS and "fetch(" not in rowmark.JS
