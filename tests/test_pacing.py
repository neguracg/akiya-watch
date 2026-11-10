# -*- coding: utf-8 -*-
"""取得ペース（pacing.py）の固定。ネットワーク・実時間は使わない（時計とsleepを差し替える）。

# @owns LIFULL等の回数制限(HTTP 202)への待機・再試行・交互実行・ページ追従打ち切りの挙動

固定するもの: ①ページ送りの実リンク解釈と打ち切り（上限・ループ・別パス） ②待ち行列の順序
（同一ドメインの間隔の間は他ドメインが進む／202で冷却→再試行／上限超で諦め） ③robots.txtの
同一session内キャッシュ ④run() 全体を通した結線（偽のfetchで 202→待機→再試行→全ページ連結）。
"""
import time

import pytest
from bs4 import BeautifulSoup

import pacing
import watch


@pytest.fixture(autouse=True)
def _clean_resume():
    pacing._RESUME.clear()
    yield
    pacing._RESUME.clear()


# --------------------------------------------------------------------------- ページ送り
def _soup(html):
    return BeautifulSoup(html, "html.parser")


BASE = "https://www.homes.co.jp/kodate/chuko/shizuoka/numazu-city/list/"


def test_next_page_prefers_link_rel_next():
    s = _soup(f'<head><link rel="next" href="{BASE}?page=2"></head><a href="{BASE}?page=9">次へ</a>')
    assert pacing.next_page_url(s, BASE) == BASE + "?page=2"


def test_next_page_falls_back_to_anchor_and_resolves_relative():
    s = _soup('<a href="?page=2">2</a><a href="?page=3">次へ</a>')
    assert pacing.next_page_url(s, BASE) == BASE + "?page=3"


def test_next_page_none_on_last_page_and_ignores_other_paths():
    assert pacing.next_page_url(_soup(f'<link rel="prev" href="{BASE}?page=4">'), BASE + "?page=5") is None
    # 次へ風のリンクでも一覧の別パス・別ドメインへは追わない
    assert pacing.next_page_url(_soup('<link rel="next" href="https://www.homes.co.jp/other/?page=2">'), BASE) is None
    assert pacing.next_page_url(_soup('<link rel="next" href="https://evil.example/x/?page=2">'), BASE) is None
    assert pacing.next_page_url(_soup('<a href="/price-watch/">次へ</a>'), BASE) is None   # page= が無い


def _card(n):
    return {"key": f"k{n}"}


def _page(n, last=False):
    nxt = "" if last else f'<link rel="next" href="{BASE}?page={n + 1}">'
    return _soup(f"<head>{nxt}</head>")


def test_step_accumulates_pages_and_dedups_only_at_the_end():
    with pytest.raises(pacing.Continue):
        pacing.step(_page(1), BASE, [_card(1), _card(2)], "t")
    assert pacing.fetch_url(BASE) == BASE + "?page=2"
    with pytest.raises(pacing.Continue):
        pacing.step(_page(2), BASE, [_card(2), _card(3)], "t")     # ページ跨ぎの重複 k2
    out = pacing.step(_page(3, last=True), BASE, [_card(4)], "t")
    assert [r["key"] for r in out] == ["k1", "k2", "k3", "k4"]
    assert pacing.fetch_url(BASE) == BASE      # 終わったら状態は残らない（次の巡回は1ページ目から）


def test_step_never_returns_partial_results_while_pages_remain():
    """途中のページでは Continue を投げるだけで、部分結果を返さない（未取得ページの物件が「消滅」扱いになるのを防ぐ）。"""
    with pytest.raises(pacing.Continue):
        pacing.step(_page(1), BASE, [_card(1)], "t")
    assert BASE in pacing._RESUME


def test_step_stops_at_max_pages(monkeypatch):
    monkeypatch.setitem(pacing.POLICIES["homes.co.jp"], "max_pages", 3)
    for n in (1, 2):
        with pytest.raises(pacing.Continue):
            pacing.step(_page(n), BASE, [_card(n)], "t")
    out = pacing.step(_page(3), BASE, [_card(3)], "t")           # 3ページ目に次リンクがあっても打ち切る
    assert [r["key"] for r in out] == ["k1", "k2", "k3"]


def test_step_rent_uses_lower_limit():
    lim = pacing.POLICIES["homes.co.jp"]["max_pages_rent"]
    assert lim < pacing.POLICIES["homes.co.jp"]["max_pages"]
    for n in range(1, lim):
        with pytest.raises(pacing.Continue):
            pacing.step(_page(n), BASE, [_card(n)], "t", rent=True)
    assert len(pacing.step(_page(lim), BASE, [_card(lim)], "t", rent=True)) == lim


def test_step_stops_on_loop():
    s = _soup(f'<link rel="next" href="{BASE}">')               # 次ページが1ページ目自身
    assert [r["key"] for r in pacing.step(s, BASE, [_card(1)], "t")] == ["k1"]


def test_step_single_page_without_policy_domain_is_not_followed():
    other = "https://example.com/list/"
    s = _soup(f'<link rel="next" href="{other}?page=2">')
    assert len(pacing.step(s, other, [_card(1)], "t")) == 1


# --------------------------------------------------------------------------- 待ち行列
class Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def _sites(spec):
    """spec: 'L1 A L2 B' → id と URL（Lで始まる=LIFULL）。"""
    out = []
    for sid in spec.split():
        host = "www.homes.co.jp" if sid.startswith("L") else f"{sid.lower()}.example.com"
        out.append({"id": sid, "url": f"https://{host}/{sid}/"})
    return out


def _sched(spec, clk, deadline=None):
    return pacing.Scheduler(_sites(spec), deadline=deadline, clock=clk.now, sleep=clk.sleep,
                            rand=lambda lo, hi: lo)


def test_other_domains_run_while_lifull_waits_its_interval():
    clk = Clock()
    s = _sched("L1 L2 A B", clk)
    order = []
    for site in s:
        order.append(site["id"])
        clk.t += 1            # 1回の取得にかかる時間
    # L1の直後、L2は間隔(12秒)が来るまで準備未完了 → A,B が先に進む。それでもL2が来る前に空きは出ない
    assert order[:3] == ["L1", "A", "B"] and order[3] == "L2"
    # L1の取得後(1秒)に間隔12秒を置く → A,B(各1秒)の後、残りはL2だけ → 解除まで残り10秒を待つ
    assert clk.slept == [pytest.approx(10)]


def test_first_ready_site_in_queue_order_is_taken():
    clk = Clock()
    order = [x["id"] for x in _sched("A L1 B C", clk)]
    assert order == ["A", "L1", "B", "C"]
    assert clk.slept == []


def test_202_cools_down_and_retries_while_others_proceed():
    clk = Clock()
    s = _sched("L1 A B", clk)
    got = []
    first = True
    for site in s:
        got.append(site["id"])
        clk.t += 1
        if site["id"] == "L1" and first:
            first = False
            assert s.rate_limited(site) is True
    # L1(202) → 冷却中は A,B → 最後に L1 を再試行（待ちは冷却の残り）
    assert got == ["L1", "A", "B", "L1"]
    cd = pacing.POLICIES["homes.co.jp"]["cooldown"]
    assert len(clk.slept) == 1 and clk.slept[0] == pytest.approx(cd - 2)    # A,B で2秒進んだ分を差し引く


def test_202_gives_up_after_retries_and_clears_resume_state():
    clk = Clock()
    s = _sched("L1 A", clk)
    outcomes = []
    for site in s:
        if site["id"] == "L1":
            pacing._RESUME[site["url"]] = {"url": site["url"] + "?page=2"}   # 途中ページを取っている最中の202
            outcomes.append(s.rate_limited(site))
    pol = pacing.POLICIES["homes.co.jp"]
    assert outcomes == [True] * pol["retries"] + [False]       # 初回+retries回の再試行で諦める
    assert "https://www.homes.co.jp/L1/" not in pacing._RESUME


def test_non_policy_domain_202_is_not_retried():
    clk = Clock()
    s = _sched("A", clk)
    for site in s:
        assert s.rate_limited(site) is False


def test_domain_streak_gives_up_immediately_without_waiting(monkeypatch):
    """200 を挟まず202が続くと（締め出しの疑い）、後続サイトは待たずに諦める＝実行が伸び切らない。"""
    monkeypatch.setitem(pacing.POLICIES["homes.co.jp"], "give_up_streak", 2)
    clk = Clock()
    s = _sched("L1 L2 L3", clk)
    res = {}
    for site in s:
        res.setdefault(site["id"], []).append(s.rate_limited(site))
    assert res["L1"][0] is True and res["L1"][1] is True      # streak 1,2 まで冷却して再試行
    assert res["L1"][2] is False                              # streak 3 > 2 → 諦め
    assert res["L2"] == [False] and res["L3"] == [False]      # 以降は即・要確認へ
    assert sum(clk.slept) <= 2 * pacing.POLICIES["homes.co.jp"]["cooldown"] + 100


def test_success_resets_streak_and_attempts():
    clk = Clock()
    s = _sched("L1 L2", clk)
    n = 0
    for site in s:
        n += 1
        if n == 1:
            assert s.rate_limited(site) is True      # L1が202
        elif n == 2:
            pass                                      # 次の取得は成功（200）
    assert s.streak["homes.co.jp"] == 0 and "L1" not in s.attempts


def test_again_puts_continuation_at_front_and_interleaves():
    clk = Clock()
    s = _sched("L1 A B C", clk)
    got, cont = [], 0
    for site in s:
        got.append(site["id"])
        clk.t += 5
        if site["id"] == "L1" and cont < 2:
            cont += 1
            s.again(site)               # 一覧の次ページ
    # L1p1 → (間隔12秒は他サイトが進める) A,B,C の取得が間に入り、L1 は続きを取る
    assert got.count("L1") == 3 and got[0] == "L1"
    assert got.index("A") < got.index("L1", 1)       # 続きより先に他ドメインが進んだ


def test_deadline_stops_waiting_instead_of_sleeping_past_it():
    clk = Clock()
    s = _sched("L1", clk, deadline=clk.t + 100)
    got = []
    for site in s:
        got.append(site["id"])
        assert s.rate_limited(site) is True        # 冷却300秒 > 上限までの100秒
    assert got == ["L1"] and clk.slept == []


# --------------------------------------------------------------------------- robots.txt キャッシュ
class _Resp:
    def __init__(self, code, text):
        self.status_code, self.text = code, text


class _Sess:
    def __init__(self, text="User-agent: *\nDisallow: /private/\n", code=200):
        self.calls, self._text, self._code = 0, text, code

    def get(self, url, timeout=None, headers=None):
        self.calls += 1
        return _Resp(self._code, self._text)


def test_robots_txt_is_fetched_once_per_domain_per_session():
    s = _Sess()
    assert watch.robots_allowed("https://www.homes.co.jp/a/list/", s) is True
    assert watch.robots_allowed("https://www.homes.co.jp/b/list/", s) is True
    assert watch.robots_allowed("https://www.homes.co.jp/private/x", s) is False   # キャッシュしても判定は変わらない
    assert s.calls == 1
    assert watch.robots_allowed("https://www.homes.co.jp/a/", _Sess()) is True     # 別sessionは別キャッシュ
    s404 = _Sess(code=404)
    assert watch.robots_allowed("https://x.example/a", s404) is True
    assert watch.robots_allowed("https://x.example/b", s404) is True
    assert s404.calls == 1


# --------------------------------------------------------------------------- run() 全体の結線
def _lifull_html(ids, nxt=None):
    cards = "".join(
        f'<div class="mod-mergeBuilding--sale"><span class="bukkenName">沼津市大岡{i}</span>'
        f'<a href="/kodate/b-{i}/">詳細</a><table><tr><th>価格</th><th>土地面積</th></tr>'
        f'<tr><td>1,000万円</td><td>100.00m2</td></tr></table></div>' for i in ids)
    link = f'<link rel="next" href="{BASE}?page={nxt}">' if nxt else ""
    return f"<html><head>{link}</head><body>{cards}{'x' * 13000}</body></html>"


def test_run_follows_pages_retries_202_and_interleaves(monkeypatch, tmp_path):
    """LIFULL 3ページ（2ページ目は最初202）＋他ドメイン1サイトを run() に通す。ネットワーク無し。
    期待: 3ページ分が1つのサイトとして連結される／202の後は冷却して同じページを再取得／
    LIFULLの待ち中に他ドメインが先に処理される／冷却は待った（sleepに300秒）。"""
    off = [0.0]
    real = time.time
    sleeps = []
    monkeypatch.setattr(time, "time", lambda: real() + off[0])
    monkeypatch.setattr(time, "sleep", lambda s: (sleeps.append(s), off.__setitem__(0, off[0] + s)))

    sites = [
        {"id": "lifull_chuko_t", "name": "LIFULL テスト", "url": BASE, "channel": 1, "kind": "x",
         "status": "verified", "filter_keywords": []},
        {"id": "other_site", "name": "他サイト", "url": "https://other.example.com/list/", "channel": 1,
         "kind": "x", "status": "verified"},
    ]
    cfg = {"sites": sites, "filters": {"price_max_man": 5000, "area_min_sqm": 0}, "sources_extra": []}
    monkeypatch.setattr(watch.yaml, "safe_load", lambda *_a, **_k: cfg)
    monkeypatch.setattr(watch, "robots_allowed", lambda url, session: True)
    monkeypatch.setattr(watch, "load_snapshot", lambda sid: {})
    monkeypatch.setattr(watch, "load_archive", lambda sid: {})
    monkeypatch.setattr(watch, "_build_machi_names", lambda f: [])
    monkeypatch.setattr(watch, "_expand_camp_tiers", lambda s, f: None)
    captured = {}
    monkeypatch.setattr(watch, "emit_reports", lambda results, *a, **k: captured.setdefault("results", results))

    pages = {BASE: [(200, _lifull_html([1, 2], 2))],
             BASE + "?page=2": [(202, ""), (200, _lifull_html([3, 4], 3))],
             BASE + "?page=3": [(200, _lifull_html([5]))],
             "https://other.example.com/list/": [(200, "<html>" + "y" * 13000 + "</html>")]}
    log = []

    def fake_fetch(url, session):
        log.append(url)
        return pages[url].pop(0)

    monkeypatch.setattr(watch, "fetch", fake_fetch)
    rc = watch.run(dry_run=True, only="")

    assert [u for u in log if "homes.co.jp" in u] == [
        BASE, BASE + "?page=2", BASE + "?page=2", BASE + "?page=3"]      # 202のページは同じURLを再取得
    assert log.index("https://other.example.com/list/") < log.index(BASE + "?page=2")  # LIFULL待ち中に他ドメインが先
    rows = {r["id"]: r for r in captured["results"]}
    assert rows["lifull_chuko_t"]["http"] == 200 and rows["lifull_chuko_t"]["raw"] == 5   # 3ページ分を連結
    assert len(captured["results"]) == 2                  # 継続・再試行の途中経過は結果行にならない
    assert any(x >= pacing.POLICIES["homes.co.jp"]["cooldown"] - 60 for x in sleeps)       # 冷却を待った
    assert rc in (0, 1)


def test_run_reports_202_as_needs_check_when_retries_are_used_up(monkeypatch):
    """202 が続いて諦めたサイトは「HTTP 202 要確認」の行になり、スナップショットは保存されない（前回データ保持）。"""
    off = [0.0]
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + off[0])
    monkeypatch.setattr(time, "sleep", lambda s: off.__setitem__(0, off[0] + s))
    sites = [{"id": "lifull_chuko_t", "name": "LIFULL テスト", "url": BASE, "channel": 1, "kind": "x",
              "status": "verified", "filter_keywords": []}]
    cfg = {"sites": sites, "filters": {"price_max_man": 5000}, "sources_extra": []}
    monkeypatch.setattr(watch.yaml, "safe_load", lambda *_a, **_k: cfg)
    monkeypatch.setattr(watch, "robots_allowed", lambda url, session: True)
    monkeypatch.setattr(watch, "_build_machi_names", lambda f: [])
    monkeypatch.setattr(watch, "_expand_camp_tiers", lambda s, f: None)
    saved = []
    monkeypatch.setattr(watch, "save_snapshot", lambda sid, d: saved.append(sid))
    captured = {}
    monkeypatch.setattr(watch, "emit_reports", lambda results, *a, **k: captured.setdefault("results", results))
    calls = []
    monkeypatch.setattr(watch, "fetch", lambda url, session: (calls.append(url), (202, ""))[1])

    rc = watch.run(dry_run=False, only="")
    assert len(calls) == 1 + pacing.POLICIES["homes.co.jp"]["retries"]
    (row,) = captured["results"]
    assert row["http"] == 202 and "要確認" in row["note"] and row["phase2"] is True
    assert saved == []
    assert rc == 2
