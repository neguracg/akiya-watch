"""akiya-watch 状態保存API (state-api)。

静的サイト(GitHub Pages)側のユーザー状態(★お気に入り・非表示・検索条件等)を、
localStorageに加えてサーバ側(PostgreSQL)にも永続化するための最小API。
動機はiOS SafariのITPによるlocalStorage揮発(7日未訪問で削除)対策・端末間同期・
利用者ごとの分離。設計の正本は
~/.claude/skills/vps-deploy/references/hybrid-state-api.md (ケースC)。

エンドポイントは {BASE_PATH}/healthz と {BASE_PATH}/state/{key} (GET/PUT) の3つのみ。
キーは環境変数 STATE_KEYS のallowlistによる事前登録制で、未知キーは常に404にする
(任意キーを自動作成すると、タイポで空状態が静かに増殖する事故につながるため)。

このAPIは認証を持たない設計(キー=識別子。詳細はREADME参照)。その代わり、
共有PostgreSQL(kintai/akkyの本番と同居)を無認証リクエストだけで巻き添えに
できないよう、(1)DB同時接続数のセマフォ (2)送信元IP単位のレート制限
(GET/healthz/PUT全て) の二段構えで防御する(2026-07-26追加)。
"""

import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("akiya_state")

# ---------------------------------------------------------------------------
# 設定 (環境変数)
# ---------------------------------------------------------------------------

# BASE_PATH未設定時は空文字("")、つまりプレフィックス無しでルーティングする
# (ローカル動作確認用。本番は.envでBASE_PATH=/akiya-stateを設定する)。
BASE_PATH = os.environ.get("BASE_PATH", "").rstrip("/")
APP_NAME = os.environ.get("APP_NAME", "akiya-watch")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# 前後の空白は.envのコピペ等で紛れ込みやすいため常に正規化する(strip)。
# ここでstripしておく理由: StarletteはCORSMiddlewareを最初のASGI呼び出し
# (=lifespan開始と同時)でミドルウェアスタックへ固定してしまうため、
# lifespan本体の中でCORS_ORIGINを書き換えても、既に構築済みのCORSMiddlewareには
# 反映されない。stripは「ミドルウェア構築より前」であるモジュール読み込み時に
# 済ませておく必要がある。
# `*`・末尾スラッシュ・(strip後もなお残る)埋め込み空白の妥当性検証は
# _check_cors_origin() が行い、lifespan起動時に呼び出してRuntimeErrorにする
# (検証自体を起動シーケンスの一部にすることで、CORSMiddlewareを実際に使う前に
# 誤設定を止める意図。値そのものは上記の理由でここより後ろでは直せない)。
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "").strip()


def _check_cors_origin(value: str) -> None:
    """CORS_ORIGIN の値を検証する。不正なら RuntimeError を送出する。

    誤設定は「ブラウザ側でCORSプリフライトが黙って失敗するだけ」で、サーバは
    200を返し続けるため気づかれずに同期だけが死ぬ(ログにも異常が出ない)。
    起動時(lifespan)に検出して起動そのものを止め、フェイルファストにする。

    value は呼び出し側(lifespan)がモジュール読み込み時に既に.strip()済みの
    CORS_ORIGINを渡す前提。したがってここでの空白チェックは先頭・末尾ではなく
    埋め込み(内部)空白の検出になる。
    空文字(CORS_ORIGIN未設定)はCORSを全許可しない、という既存の意図した挙動
    として許可する(エラーにしない)。

    2026-07-26: 当初は `*`・末尾スラッシュ・埋め込み空白の3パターンしか弾いて
    おらず、以下4パターンが素通りして起動し、その後ACAOヘッダを一切返さない
    (=この検証がそもそも防ぐはずだった「無言死」そのもの)ことが実測で判明した:
      - `https://a.com,https://b.com` (隣のSTATE_KEYSがカンマ区切り仕様なので
        書式を引きずる典型的タイポ。カンマ区切り複数オリジンには非対応)
      - `neguracg.github.io` (スキーム欠落)
      - `HTTPS://NEGURACG.GITHUB.IO` (ブラウザが送るOriginヘッダは小文字だが
        Starletteの比較は大文字小文字を区別する完全一致)
      - `https://neguracg.github.io:443` (ブラウザはOriginヘッダ送信時に
        既定ポートを省略するため、明記すると一致しない)
    そのため urllib.parse.urlsplit による構造的な検証を追加した。
    """
    if not value:
        return
    if "*" in value:
        raise RuntimeError(
            "CORS_ORIGIN に '*' は指定できません(全オリジン許可は禁止)。"
            "単一のオリジンを明示的に指定してください。"
        )
    if "," in value:
        raise RuntimeError(
            f"CORS_ORIGIN にカンマが含まれています: {value!r}。"
            "このAPIは単一オリジンのみ対応です(複数オリジンのカンマ区切りは"
            "非対応)。STATE_KEYS(カンマ区切り)の書式と混同していませんか?"
        )
    if any(ch.isspace() for ch in value):
        raise RuntimeError(
            f"CORS_ORIGIN に空白が含まれています: {value!r}。"
        )
    if value != value.lower():
        raise RuntimeError(
            f"CORS_ORIGIN は小文字で指定してください: {value!r}。"
            "ブラウザが送るOriginヘッダは常に小文字ですが、Starlette側の比較は"
            "大文字小文字を区別する完全一致のため、大文字が混じっていると"
            "一致せずCORSがサイレントに失敗します。"
        )

    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        raise RuntimeError(
            f"CORS_ORIGIN にはスキーム(http:// または https://)が必要です: "
            f"{value!r}。"
        )
    if not parts.netloc:
        raise RuntimeError(f"CORS_ORIGIN にホスト部がありません: {value!r}。")
    if value.endswith("/"):
        raise RuntimeError(
            f"CORS_ORIGIN の末尾にスラッシュがあります: {value!r}。"
            "ブラウザのOriginヘッダは末尾スラッシュを含まないため一致せず、"
            "CORSがサイレントに失敗します。末尾のスラッシュを外してください。"
        )
    if parts.path or parts.query or parts.fragment:
        raise RuntimeError(
            f"CORS_ORIGIN には scheme://host[:port] 形式のオリジンのみ指定でき"
            f"ます(path/query/fragmentは不可): {value!r}。"
        )
    if (parts.scheme == "https" and parts.netloc.endswith(":443")) or (
        parts.scheme == "http" and parts.netloc.endswith(":80")
    ):
        raise RuntimeError(
            f"CORS_ORIGIN に既定ポートを明記しないでください: {value!r}。"
            "ブラウザはOriginヘッダ送信時に既定ポート(https→443/http→80)を"
            "省略するため、明記すると一致せずCORSが常に失敗します。"
        )


_STATE_KEYS_RAW = os.environ.get("STATE_KEYS", "")
# allowlistの「定義」側の空白除去は正規化してよいが、"受け取ったキー"の側は
# 正規化・部分一致を一切行わない(完全一致のみ)。両者を混同しないこと。
ALLOWED_KEYS: frozenset = frozenset(
    k.strip() for k in _STATE_KEYS_RAW.split(",") if k.strip()
)

try:
    MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", "262144"))
except ValueError:
    MAX_BODY_BYTES = 262144

# --- レート制限 (送信元IP単位・プロセス内メモリ・スライディングウィンドウ) ---
# 2026-07-26: 以前は「保存先キー」単位だった(PUTのみ・10回/分)。第三者が
# 特定キー(例: ken)へ空PUTを送り続けるだけで本人を恒久的に429で締め出せる
# 欠陥があったため、送信元IP単位に変更。併せてGET(healthz含む)にも適用する
# (無認証で叩けるGETがDB接続を無制限に消費できた問題(下記DB_CONNECTION_LIMIT
# 参照)の抑止策を兼ねる)。IP単位化で1ユーザーあたりの実質的な余裕が増えるため
# 上限も緩めた。
try:
    PUT_RATE_LIMIT_MAX = int(os.environ.get("PUT_RATE_LIMIT_MAX", "60"))
except ValueError:
    PUT_RATE_LIMIT_MAX = 60
try:
    GET_RATE_LIMIT_MAX = int(os.environ.get("GET_RATE_LIMIT_MAX", "60"))
except ValueError:
    GET_RATE_LIMIT_MAX = 60
RATE_LIMIT_WINDOW_SECONDS = 60.0

# --- DB同時接続数の上限 (セマフォ) ---
# 2026-07-26: 認証もレート制限も無かった時代のGET/healthzは、リクエストごとに
# 新規DB接続を張る一方、エンドポイントが同期関数(def)のためStarletteの
# スレッドプール(既定40)上で並列実行され、無認証の外部リクエストだけで
# 最大40本のPostgreSQL接続を占有できた。このPostgreSQLはkintai/akky(共に
# 本番稼働中)と共有しており、max_connections既定100に対し40は致命的。
# このプロセスが同時に張れる接続数をここで頭打ちにし、超過分は待たせず503にする。
try:
    DB_CONNECTION_LIMIT = int(os.environ.get("DB_CONNECTION_LIMIT", "5"))
except ValueError:
    DB_CONNECTION_LIMIT = 5
DB_SEMAPHORE_TIMEOUT_SECONDS = 5.0


def _check_positive_int(
    name: str, value: int, *, min_value: int, max_value: int | None = None
) -> None:
    """数値系の環境変数設定(name)がレンジ内かを検証する。範囲外ならRuntimeError。

    DB_CONNECTION_LIMIT/PUT_RATE_LIMIT_MAX/GET_RATE_LIMIT_MAX/MAX_BODY_BYTES
    共通のレンジチェック。文字列としてint変換できない値は既存通りモジュール
    読み込み時にデフォルト値へフォールバックする(上のtry/except参照。ここでは
    「パースはできたが値としておかしい」レンジ外だけを扱う)。

    2026-07-26: 実測でDB_CONNECTION_LIMIT=0は全リクエスト恒久503(healthzは
    「db down」と偽装)、=-1は後述の理由でクラッシュループ、=500は上限チェック
    自体を無意味化することが判明したため追加。

    呼び出しはlifespan内で行うこと(_check_cors_origin同様、起動時に検出して
    フェイルファストにする意図)。**この関数自体はここでは呼び出さない**:
    例えばDB_CONNECTION_LIMITをこの直後(モジュール読み込み時)に
    `threading.Semaphore(DB_CONNECTION_LIMIT)` のような「値を実際に使う」形で
    消費すると、負値の場合はSemaphoreの生成自体がValueErrorでモジュール
    importを失敗させてしまい、Dockerのrestart:unless-stoppedがクラッシュ
    ループするだけで、意図した「RuntimeErrorであることを明示して起動失敗」に
    ならない(_db_semaphoreの生成をlifespan側に遅延させている理由も同じ)。
    """
    if value < min_value or (max_value is not None and value > max_value):
        range_desc = (
            f"{min_value}以上{max_value}以下"
            if max_value is not None
            else f"{min_value}以上"
        )
        raise RuntimeError(
            f"{name} の値が不正です: {value!r}。{range_desc}を指定してください。"
        )


TABLE_SQL = """
CREATE TABLE IF NOT EXISTS user_state (
  app        TEXT        NOT NULL,
  user_key   TEXT        NOT NULL,
  state      JSONB       NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (app, user_key)
)
"""


# ---------------------------------------------------------------------------
# DBアクセス (psycopg2)
#
# 低頻度アクセス(数人・デバウンス済みPUT)の小規模用途のため、コネクション
# プールは持たずリクエストごとに接続・切断する(プールのアイドル切れ・
# サイジング検討を避けるための意図的な単純化)。ただし無制限に接続を張ると
# 共有PostgreSQL(kintai/akkyの本番と同居)を巻き添えにするため、同時接続数は
# _db_connection() のセマフォで頭打ちにする(DB_CONNECTION_LIMIT本。上のコメント
# 参照)。
# DB接続不可時は例外をそのまま伝播させる。起動時(_init_db)ならアプリ起動
# 自体が失敗し、Dockerのrestart:unless-stoppedによる再試行に委ねる設計
# (アプリ側にリトライ/バックオフは実装しない)。SQLiteへのフォールバックは
# 行わない(本番と挙動が変わるため意図的に持たない)。
# ---------------------------------------------------------------------------

# DB_CONNECTION_LIMITの値検証(レンジチェック)はlifespan内の_check_positive_int
# で行う。このモジュール読み込み時点ではまだ未検証のため、ここで直接
# threading.Semaphore(DB_CONNECTION_LIMIT) を生成してはいけない
# (例えば負値だとSemaphoreの生成自体がValueErrorでモジュールimportを失敗させ、
# Dockerのrestart:unless-stoppedがクラッシュループするだけになり、意図した
# 「lifespan内でRuntimeErrorを出してフェイルファスト」にならない。2026-07-26に
# 実測で確認)。そのため実体の生成はlifespan側の検証成功後に遅延させ、
# ここではNoneのプレースホルダのみを置く(ASGIのlifespan.startupが完了する
# までStarletteはHTTPリクエストをルーティングしないため、リクエストハンドラ
# から_db_connection()経由でこれを参照する時点では必ず生成済みになっている)。
_db_semaphore: "threading.Semaphore | None" = None


class _DatabaseBusyError(Exception):
    """DB同時接続数の上限に達し、待たせず503を返すためのマーカー例外。"""


def _connect():
    return psycopg2.connect(DATABASE_URL, connect_timeout=5)


@contextmanager
def _db_connection():
    """DB接続を1本取得するコンテキストマネージャ(同時接続数をセマフォで頭打ちにする)。

    DB_SEMAPHORE_TIMEOUT_SECONDS 待っても空きが出ない場合は、接続を試みずに
    _DatabaseBusyError を送出する(スレッドプールのワーカーを専有し続けさせない
    ため。無期限に待つと結局スレッドプールの方が先に枯渇し、503にすらならず
    タイムアウトするだけになる)。
    接続確立そのものの失敗(DB down等)は素通しする(呼び出し元の既存の
    例外処理に委ねる。_init_db/_db_checkの挙動は変えない)。
    """
    if not _db_semaphore.acquire(timeout=DB_SEMAPHORE_TIMEOUT_SECONDS):
        raise _DatabaseBusyError("db connection limit reached")
    try:
        conn = _connect()
        try:
            yield conn
        finally:
            conn.close()
    finally:
        _db_semaphore.release()


def _init_db() -> None:
    # アプリ起動シーケンス内で1回だけ呼ばれる(並行アクセスが無い)ため、
    # セマフォを介さず直接接続する。
    conn = _connect()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(TABLE_SQL)
    finally:
        conn.close()


def _db_check() -> bool:
    """healthz用のDB疎通確認。行データには一切触れない(SELECT 1相当)。

    同時接続数の上限(_DatabaseBusyError)に達している場合も「今は確認できない」
    という意味でFalse(down扱い)にする。healthzのレスポンス形は常に
    {"ok": bool, "db": "up"|"down"} で安定させ、理由によって形を変えない。
    """
    try:
        with _db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception:
        return False


def _fetch_state(key: str):
    """(state, updated_at) を返す。レコードが無ければ None。

    DB接続数の上限に達した場合は _DatabaseBusyError を送出する
    (put_state/get_stateには伝播させ、専用の例外ハンドラが503に変換する)。
    """
    with _db_connection() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT state, updated_at FROM user_state "
                    "WHERE app = %s AND user_key = %s",
                    (APP_NAME, key),
                )
                row = cur.fetchone()
    if row is None:
        return None
    return row[0], row[1]


def _upsert_state(key: str, state: dict) -> datetime:
    """UPSERT (Last Write Wins)。保存後のupdated_atを返す。

    DB接続数の上限に達した場合は _DatabaseBusyError を送出する(_fetch_state同様)。
    """
    with _db_connection() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO user_state (app, user_key, state, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (app, user_key)
                    DO UPDATE SET state = EXCLUDED.state, updated_at = now()
                    RETURNING updated_at
                    """,
                    (APP_NAME, key, psycopg2.extras.Json(state)),
                )
                row = cur.fetchone()
    return row[0]


def _iso(dt: datetime) -> str:
    """TIMESTAMPTZをISO8601(UTC・+00:00表記)文字列に変換する。
    セッションのタイムゾーン設定に依らず出力を安定させるため、明示的にUTC変換する。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# レート制限 (送信元IP単位・プロセス内メモリ・GET/PUT/healthz全てが対象)
#
# 2026-07-26: 以前は「保存先キー」単位・PUTのみだった。第三者が特定キーへ
# 空PUTを送り続けるだけで本人を恒久的に429で締め出せる欠陥があったため、
# 送信元IPをキーにする方式へ変更し、GET(healthz含む)にも適用範囲を広げた
# (無認証GETがDB接続を無制限に消費できた問題の抑止も兼ねる。本命の対策は
# 上のDB_CONNECTION_LIMITセマフォで、レート制限はその手前の第一防波堤)。
#
# 注意: allowlistの3キーと違い、送信元IPは任意の文字列になり得る
# (X-Forwarded-Forはクライアントが自由に送れるヘッダで偽装可能。_client_ip参照)。
# そのため以前のコメントが前提していた「キー数は実在するキー分にしか増えない」
# という制約はもう成立しない。追跡するキー数を max_tracked_keys で頭打ちにし、
# 超過時は最も古く見たキーを削除する(挿入順FIFO。厳密なLRUではないが、
# 無限にメモリが増え続ける事態だけは防ぐ)。
# ---------------------------------------------------------------------------


class _RateLimiter:
    def __init__(
        self, max_calls: int, window_seconds: float, max_tracked_keys: int = 10000
    ) -> None:
        self._max_calls = max_calls
        self._window_seconds = window_seconds
        self._max_tracked_keys = max_tracked_keys
        self._hits: dict = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window_seconds
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self._max_calls:
                return False
            hits.append(now)
            if len(self._hits) > self._max_tracked_keys:
                oldest_key = next(iter(self._hits))
                if oldest_key != key:
                    del self._hits[oldest_key]
            return True


def _client_ip(request: Request) -> str:
    """レート制限のキーに使う送信元IPを返す。

    本番はCloudflare Tunnel経由のため、X-Forwarded-Forの先頭要素
    (オリジナルクライアントIP)を使う。ヘッダが無ければTCP接続元
    (request.client.host)にフォールバックする。

    注意: X-Forwarded-Forはクライアントが自由に送信できるヘッダであり偽装可能。
    ここでの用途はあくまで「濫用の抑止」であって認可・認証ではない
    (信頼できるプロキシからの値かどうかの検証は行っていない。Cloudflare
    Tunnel配下という運用上の前提に依存した簡易対策)。
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    if request.client:
        return request.client.host
    return "unknown"


_put_rate_limiter = _RateLimiter(PUT_RATE_LIMIT_MAX, RATE_LIMIT_WINDOW_SECONDS)
_get_rate_limiter = _RateLimiter(GET_RATE_LIMIT_MAX, RATE_LIMIT_WINDOW_SECONDS)


# ---------------------------------------------------------------------------
# ボディサイズ上限 (ASGIミドルウェア)
#
# Content-Lengthが正直に上限超過を申告していれば読み込み前に即413。
# ヘッダが無い/嘘の場合でも、実際に受信したバイト数の累積で上限を強制する。
#
# 上限以内と判定できるまでは後段のASGIアプリ(FastAPI)を一切呼び出さず、
# このミドルウェア自身がボディを読み切ってから、読み込み済みチャンクを
# "再生"するreceiveで後段へ渡す(ASGIのreceiveは一度読んだら戻せないため)。
# そのため後段でストリーミング応答を先行送信する設計には対応しないが、
# このアプリのハンドラは全てJSON即時応答のみなので問題ない。
#
# CORSMiddlewareより内側(先にadd_middlewareする側)に置くことで、
# ここから直接送る413応答にもCORSヘッダが付く
# (Starletteはadd_middlewareを後から呼んだ方を外側に積む)。
# ---------------------------------------------------------------------------


def _get_header(scope: dict, name: bytes):
    for k, v in scope.get("headers") or []:
        if k == name:
            return v
    return None


async def _send_json(send, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = _get_header(scope, b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_bytes:
                    await _send_json(send, 413, {"detail": "payload too large"})
                    return
            except ValueError:
                pass  # 不正な値は実読み込み側のチェックに委ねる

        chunks: list = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # http.disconnect等はそのままアプリに渡し、以降の処理に委ねる。
                async def _pass_through():
                    return message

                await self.app(scope, _pass_through, send)
                return
            body = message.get("body") or b""
            total += len(body)
            if total > self.max_bytes:
                await _send_json(send, 413, {"detail": "payload too large"})
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        sent = 0

        async def _replay_receive():
            nonlocal sent
            if sent < len(chunks):
                chunk = chunks[sent]
                sent += 1
                return {
                    "type": "http.request",
                    "body": chunk,
                    "more_body": sent < len(chunks),
                }
            return await receive()

        await self.app(scope, _replay_receive, send)


class SafeErrorMiddleware:
    """未捕捉例外に対する最終防波堤(ASGIミドルウェア)。

    重要: FastAPI/Starletteの `@app.exception_handler(Exception)` (または `500`)
    はStarlette側で特別扱いされ、CORSMiddlewareの**外側**にある ServerErrorMiddleware
    の handler として登録される(ExceptionMiddleware内のハンドラ表には入らない)。
    そのため通常のExceptionハンドラ登録では、未捕捉例外の500応答にCORSヘッダが付かず、
    ブラウザ側からは実際のエラーではなく「CORSエラー」に見えてしまう
    (実際にTestClientで検証して確認した挙動)。

    このミドルウェアをCORSMiddlewareの内側に置くことで、ここで捕まえた例外は
    CORS適用後のsendで安全な500 JSONを返せる。スタックトレース・DSN等は
    レスポンスに一切出さず、ログにのみ記録する(docker logsで追える)。
    HTTPException/RequestValidationErrorはExceptionMiddlewareの標準ハンドラが
    先にマッチして正常応答になるため、ここに来るのは真に未捕捉の例外のみ。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def _tracking_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, _tracking_send)
        except Exception as exc:
            logger.exception("unhandled error: %s", exc)
            if response_started:
                # 応答を送信し始めた後の例外は安全に上書きできないため、
                # ここでは再送を試みず再送出する(最終的にASGIサーバのログに残る)。
                raise
            await _send_json(send, 500, {"detail": "internal server error"})


# ---------------------------------------------------------------------------
# FastAPI本体
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _db_semaphore
    # CORS_ORIGIN検証を最初に行う(DB接続を試みる前に誤設定で止める)。
    _check_cors_origin(CORS_ORIGIN)
    # 数値系設定のレンジ検証も同様にDB接続を試みる前に行う(フェイルファスト)。
    _check_positive_int(
        "DB_CONNECTION_LIMIT", DB_CONNECTION_LIMIT, min_value=1, max_value=20
    )
    _check_positive_int("PUT_RATE_LIMIT_MAX", PUT_RATE_LIMIT_MAX, min_value=1)
    _check_positive_int("GET_RATE_LIMIT_MAX", GET_RATE_LIMIT_MAX, min_value=1)
    _check_positive_int("MAX_BODY_BYTES", MAX_BODY_BYTES, min_value=1024)
    if not ALLOWED_KEYS:
        logger.warning("STATE_KEYS が未設定/空です。全キーが404になります。")
    # 検証成功後にのみ実体を生成する(理由は_db_semaphore定義箇所のコメント参照)。
    _db_semaphore = threading.Semaphore(DB_CONNECTION_LIMIT)
    _init_db()
    logger.info(
        "state-api starting: app_name=%s base_path=%s allowed_keys=%d "
        "db_connection_limit=%d put_rate_limit=%d/min get_rate_limit=%d/min",
        APP_NAME,
        BASE_PATH or "(none)",
        len(ALLOWED_KEYS),
        DB_CONNECTION_LIMIT,
        PUT_RATE_LIMIT_MAX,
        GET_RATE_LIMIT_MAX,
    )
    yield


# docs_url/redoc_url/openapi_url を全てNoneにし、/docs /redoc /openapi.json を
# 露出しない(外部CDN(cdn.jsdelivr.net)への依存も同時に消える)。このAPIは
# 3エンドポイントのみの内輪向け最小APIで、Swagger UIを公開する意味が無い。
app = FastAPI(
    lifespan=lifespan,
    title="akiya-watch state-api",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# 登録順が重要: 先に登録した方が内側に積まれる(Starletteの仕様。
# add_middlewareは呼ぶたびに内側のスタックの先頭に挿入されるため、
# 後から呼んだものほど外側になる)。
# 内側から: BodySizeLimitMiddleware -> SafeErrorMiddleware -> CORSMiddleware(最外)
# にすることで、413応答・未捕捉例外の500応答の両方にCORSヘッダが付く。
app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)
app.add_middleware(SafeErrorMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[CORS_ORIGIN] if CORS_ORIGIN else [],
    allow_methods=["GET", "PUT", "OPTIONS"],
    allow_headers=["Content-Type"],
    allow_credentials=False,
)


@app.exception_handler(_DatabaseBusyError)
async def _database_busy_handler(
    _request: Request, _exc: _DatabaseBusyError
) -> JSONResponse:
    """DB同時接続数の上限に達した時の応答。

    _DatabaseBusyErrorは通常のHTTPExceptionと同じくExceptionMiddleware
    (CORSMiddlewareの内側)で処理されるため、SafeErrorMiddleware(bare Exception
    用の最終防波堤)とは違ってこの応答にもCORSヘッダが正しく付く。
    """
    return JSONResponse(status_code=503, content={"detail": "database busy, try again"})


class StatePutBody(BaseModel):
    state: dict[str, Any]


@app.get(f"{BASE_PATH}/healthz")
def healthz(request: Request):
    if not _get_rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")
    if _db_check():
        return {"ok": True, "db": "up"}
    return JSONResponse(status_code=503, content={"ok": False, "db": "down"})


@app.get(f"{BASE_PATH}/state/{{key}}")
def get_state(key: str, request: Request):
    # レート制限をallowlistチェックより先に行う: 有効なキーへの正規アクセスだけ
    # でなく、無効なキーを大量に探索するようなアクセスもIP単位で頭打ちにする。
    if not _get_rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")
    if key not in ALLOWED_KEYS:
        raise HTTPException(status_code=404, detail="unknown key")
    row = _fetch_state(key)
    if row is None:
        return {"state": None, "updated_at": None}
    state, updated_at = row
    return {"state": state, "updated_at": _iso(updated_at)}


@app.put(f"{BASE_PATH}/state/{{key}}")
def put_state(key: str, body: StatePutBody, request: Request):
    if not _put_rate_limiter.allow(_client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests")
    if key not in ALLOWED_KEYS:
        raise HTTPException(status_code=404, detail="unknown key")
    updated_at = _upsert_state(key, body.state)
    return {"ok": True, "updated_at": _iso(updated_at)}
