"""巡回の取得ペース制御（ドメイン別の最小間隔・回数制限HTTP 202の待機と再試行・一覧のページ継続）。

# @owns 巡回中の「どのサイトをいつ叩くか」（ドメイン別の最小間隔・202待機・再試行・ページ継続の待ち行列）

背景（2026-11-10 #954）: LIFULL(homes.co.jp)は IP 単位の累積で 6〜7 リクエスト目から HTTP 202
（中身の無い応答＝回数制限）を返し、5分待つと数本通る。以前は一覧の1ページ目しか取らず、202 が
来たサイトは「要確認」で終わっていた。相手の制限を尊重したまま全ページ取るため、**IP分散など
制限の回避はせず**、1回の実行の中で ①間隔を空ける ②202 なら待ってから再試行 ③待っている間は
他ドメインの取得を進める、を行う。

仕組み: run() は `Scheduler` を for で回す。Scheduler は「準備できているサイト」を先頭から選んで
返し、準備できているものが無ければ最短の解除まで待つ。LIFULL の一覧は「1ページ = 1回の取得 =
1タスク」で、次ページがあるとアダプタが `step()` 経由で `Continue` を送出し、run() が
`Scheduler.again()` で待ち行列の先頭へ戻す（次の取得は最小間隔の後。それまで他ドメインが進む）。
202 は `Scheduler.rate_limited()` が待ち行列の先頭へ戻し、そのドメインを冷却(COOLDOWN)する。
再試行は上限つきで、使い切ったら今までどおり「HTTP 202 要確認」として報告し、前回の取得結果は
残す（スナップショットは 200 で取り切れた時にしか保存されない。部分結果は保存に乗せない）。

巡回時間の見積もり（2026-11-10。Scheduler を偽の時計・偽サーバ〔窓300秒にK本通るトークン桶〕で回した
シミュレーション。他ドメイン133サイト×14秒≒31分を土台に、LIFULL を一覧の各ページ1本として混ぜた）:
  必要本数 中古17ページ＋土地7×2ページ＋賃貸3＋akiyabank1 ≒ 36本 → K=6で32分／K=4で50分
  土地が各4ページ（≒50本）→ K=6で42分／K=4で74分。上限ページ数まで取る最悪級（≒150本）は K=6で136分。
  実測の窓（6〜7本目で202・5分待つと数本）はK≒6。RUN_WALLCLOCK_LIMIT を 80分、daily.yml の
  timeout-minutes を 100分にしたのは、K=4・50本の74分でも上限内に収めるため（超過分は打ち切られる）。

watch.py は1000行ラチェットのため、この規則はここに集約し、watch.py は呼ぶだけにする。
"""
import logging
import random
import re
import time
import urllib.parse

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 値はすべてここ（巡回の取得ペースに関する定数の唯一の置き場）
# ---------------------------------------------------------------------------
POLICIES = {
    "homes.co.jp": {
        "interval": (12, 18),   # 同ドメインへの取得の最小間隔（秒の範囲・乱数）。家庭回線で12秒間隔の全ページ取得に成功した実測値
        "cooldown": 300,        # HTTP 202 を受けたらそのドメインを休ませる秒数（5分待つと数本通る実測）
        "retries": 3,           # 同じ取得の再試行回数（初回+3回で諦める）
        "give_up_streak": 6,    # 200 を挟まず 202 が続いた回数がこれを超えたら、以降は待たずに諦める（締め出し時に実行が伸び切らない保険）
        "max_pages": 10,        # 売買一覧のページ追従の上限（沼津=5ページ。30件/ページ）
        "max_pages_rent": 4,    # 賃貸一覧の上限（函南は全間取りの一覧で、広い家に無関係な部屋が大半＝取りすぎない）
    },
}


def policy_for(url: str):
    """URL のドメインに対応する (ドメイン名, 方針dict)。対象外は (None, None)。"""
    netloc = urllib.parse.urlsplit(url).netloc.lower()
    for dom, pol in POLICIES.items():
        if netloc == dom or netloc.endswith("." + dom):
            return dom, pol
    return None, None


# ---------------------------------------------------------------------------
# ページ継続（アダプタ <-> run() の合図）
# ---------------------------------------------------------------------------
class Continue(Exception):
    """一覧に次ページがある。run() が待ち行列へ戻し、次の取得は最小間隔の後に行う。"""


_RESUME = {}   # base_url -> {"url": 次に取るページURL, "page": その番号, "cards": 取得済み物件, "seen": 既出URL}


def fetch_url(base_url: str) -> str:
    """run() が次に取るURL。途中のページがあればそのURL、無ければ一覧の1ページ目（base_url）。"""
    st = _RESUME.get(base_url)
    return st["url"] if st else base_url


def next_page_url(soup, page_url: str):
    """一覧の「次へ」の実リンク。`<link rel="next">`を第1選択、無ければ「次へ」のアンカー（?page=N）。
    別ドメイン・別パスへは追わない（広告等の誤追従を避ける）。無ければ None。"""
    link = soup.find("link", rel="next")
    cand = link.get("href") if link else None
    if not cand:
        for a in soup.find_all("a", href=True):
            if "次へ" in a.get_text(strip=True) and re.search(r"[?&]page=\d+", a["href"]):
                cand = a["href"]
                break
    if not cand:
        return None
    nxt = urllib.parse.urljoin(page_url, cand)
    a, b = urllib.parse.urlsplit(nxt), urllib.parse.urlsplit(page_url)
    return nxt if (a.netloc, a.path) == (b.netloc, b.path) else None


def step(soup, base_url: str, cards: list, tag: str, rent: bool = False) -> list:
    """アダプタが1ページ分を抽出した後に呼ぶ。次ページがあれば Continue を送出（取得済みは
    保持）、最後のページなら全ページ分を重複除去して返す。部分結果は返さない。"""
    st = _RESUME.pop(base_url, None) or {"url": base_url, "page": 1, "cards": [], "seen": {base_url}}
    page, allc = st["page"], st["cards"] + cards
    pol = policy_for(base_url)[1] or {}
    limit = pol.get("max_pages_rent" if rent else "max_pages", 1)
    nxt = next_page_url(soup, st["url"])
    if nxt and nxt in st["seen"]:
        log.warning(f"[{tag}] 次ページURLが既出（ループ）→打ち切り: {nxt}")
        nxt = None
    elif nxt and page >= limit:
        log.warning(f"[{tag}] ページ追従の上限 {limit} ページに達したため打ち切り: {base_url}")
        nxt = None
    if nxt:
        st.update(url=nxt, page=page + 1, cards=allc)
        st["seen"].add(nxt)
        _RESUME[base_url] = st
        raise Continue(nxt)
    seen, out = set(), []
    for r in allc:
        if r["key"] not in seen:
            seen.add(r["key"])
            out.append(r)
    log.info(f"[{tag}] pages={page} cards={len(out)}")
    return out


# ---------------------------------------------------------------------------
# 待ち行列
# ---------------------------------------------------------------------------
class Scheduler:
    """巡回するサイトを「準備できているもの」から順に返す。対象ドメインは取得の後に最小間隔、
    202 の後は冷却の間 準備未完了になり、その間は他ドメインのサイトが先に処理される。"""

    def __init__(self, sites, deadline=None, clock=None, sleep=None, rand=None):
        self.queue = list(sites)
        self.deadline = deadline          # clock() で測った打ち切り時刻。待ちがこれを越えるなら止める
        # 既定は実時計。呼び出し時に引くので、試験が time.time / time.sleep を差し替えても効く
        self.clock, self.sleep, self.rand = clock or time.time, sleep or time.sleep, rand or random.uniform
        self.ready = {}                   # ドメイン -> 次に取れる時刻
        self.attempts = {}                # サイトid -> 今の取得で続けて 202 になった回数
        self.streak = {}                  # ドメイン -> 200 を挟まず続いた 202 の回数
        self._limited = False
        _RESUME.clear()

    def pending(self) -> int:
        """まだ処理していないサイト数（今返したサイトは含まない）。"""
        return len(self.queue)

    def _dom(self, site):
        return policy_for(site["url"])[0]

    def _pick(self):
        while True:
            now, soonest = self.clock(), None
            for k, s in enumerate(self.queue):
                d = self._dom(s)
                t = self.ready.get(d, 0) if d else 0
                if t <= now:
                    return self.queue.pop(k)
                soonest = t if soonest is None else min(soonest, t)
            if self.deadline is not None and soonest > self.deadline:
                return None
            log.info(f"取得待ち: 残りは全て間隔/冷却中。{soonest - now:.0f}秒待つ（残 {len(self.queue)} サイト）")
            self.sleep(soonest - now)

    def __iter__(self):
        while self.queue:
            site = self._pick()
            if site is None:
                log.warning(f"実行の時間上限までに取得できない残 {len(self.queue)} サイトを打ち切り")
                return
            self._limited = False
            yield site
            # ここへはループ本体（取得）が終わってから戻る。取得の後に最小間隔を置く。
            d = self._dom(site)
            if d:
                if not self._limited:
                    self.streak[d] = 0
                    self.attempts.pop(site["id"], None)
                lo, hi = POLICIES[d]["interval"]
                self.ready[d] = max(self.ready.get(d, 0), self.clock() + self.rand(lo, hi))

    def again(self, site):
        """一覧の次ページがある（Continue）。待ち行列の先頭へ戻す＝そのサイトを続きから優先して進める。"""
        self.queue.insert(0, site)

    def rate_limited(self, site) -> bool:
        """HTTP 202 を受けた。再試行できるなら冷却して待ち行列の先頭へ戻し True。
        対象外ドメイン・再試行の使い切り・締め出し疑い（連続202）は False（呼び出し側が要確認として報告）。"""
        d = self._dom(site)
        if not d:
            return False
        pol, sid = POLICIES[d], site["id"]
        self.attempts[sid] = n = self.attempts.get(sid, 0) + 1
        self.streak[d] = self.streak.get(d, 0) + 1
        self._limited = True
        if n > pol["retries"] or self.streak[d] > pol["give_up_streak"]:
            log.warning(f"[{sid}] HTTP 202 が続くため諦める（再試行{n - 1}回・ドメイン連続202={self.streak[d]}）")
            _RESUME.pop(site["url"], None)
            self.attempts.pop(sid, None)
            return False
        self.ready[d] = self.clock() + pol["cooldown"]
        log.warning(f"[{sid}] HTTP 202（回数制限）。{pol['cooldown']}秒空けて再試行 {n}/{pol['retries']}")
        self.queue.insert(0, site)
        return True
