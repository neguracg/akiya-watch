# -*- coding: utf-8 -*-
"""urls.yaml（監視対象マスタ）の構造固定（消込表#954。賃貸URLの置換・激安専用サイトの sources_extra 移動後）。

URLそのものの正しさは試験できない（実測と腱さんの承認で決める。CLAUDE.md「URL変更の唯一の手順」）。
ここで固定するのは、読み込みが壊れていないこと・id重複なし・移動したサイトが sites に戻っていない
こと・賃貸SUUMOが「4K以上」条件付きURLの形を保っていること。
"""
from pathlib import Path

import yaml

CFG = yaml.safe_load((Path(__file__).resolve().parent.parent / "urls.yaml").read_text(encoding="utf-8"))
SITES = CFG["sites"]
BY_ID = {s["id"]: s for s in SITES}

# 2026-11-09 #954 で賃貸タブを広い家探しへ作り替えた際に sites から sources_extra へ移したID
_MOVED_OUT = ["vhouse_numazu", "vhouse_izunokuni", "sjkk_kenei", "jmty_rent_shizuoka",
              "akiya_athome_rent_shizuoka"] + [
    "sumaimy_rent_" + t for t in ("kannami", "izunokuni", "mishima", "numazu", "atami",
                                  "nagaizumi", "shimizu", "susono", "izu")]


def test_site_ids_unique_and_required_keys():
    ids = [s["id"] for s in SITES]
    assert len(ids) == len(set(ids))
    for s in SITES:
        for k in ("id", "name", "url", "channel", "kind"):
            assert s.get(k), (s.get("id"), k)


def test_sources_extra_have_required_keys():
    for e in CFG["sources_extra"]:
        for k in ("channel", "name", "kind", "url", "status"):
            assert e.get(k), (e.get("name"), k)


def test_moved_out_sites_are_not_in_sites_but_kept_in_sources_extra():
    for sid in _MOVED_OUT:
        assert sid not in BY_ID, sid
    extra_names = " ".join(e["name"] for e in CFG["sources_extra"])
    for sid in _MOVED_OUT:
        assert sid in extra_names, sid     # name に「旧sites <id>」を残して追跡できる


def test_suumo_rent_urls_are_4k_or_more_conditional_search():
    rent = [s for s in SITES if s["id"].startswith("suumo_rent_")]
    assert len(rent) == 8
    for s in rent:
        u = s["url"]
        assert "/jj/chintai/ichiran/FR301FC001/" in u, s["id"]
        assert all(f"md={n}" in u for n in (11, 12, 13, 14)), s["id"]   # 4K/4DK/4LDK/5K以上
        assert "pc=50" in u, s["id"]                                     # 1頁に全件が収まる
        assert s.get("tab") == "rent"


def test_ito_not_added():
    """2026-11-09 腱さん承認: 伊東市は賃貸・中古戸建とも監視に入れない。"""
    for s in SITES:
        assert "_ito" not in s["id"] or not s["id"].startswith(("suumo_rent_", "lifull_rent_", "suumo_chuko_")), s["id"]


def test_chuko_atami_izu_added_like_existing_chuko():
    for sid in ("suumo_chuko_atami", "suumo_chuko_izu"):
        s = BY_ID[sid]
        ref = BY_ID["suumo_chuko_mishima"]
        assert s["url"].startswith("https://suumo.jp/chukoikkodate/shizuoka/sc_")
        assert (s["channel"], s["kind"], s.get("tab")) == (ref["channel"], ref["kind"], ref.get("tab"))
