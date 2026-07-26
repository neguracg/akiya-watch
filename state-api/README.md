# akiya-watch state-api

akiya-watch は GitHub Pages でホストする静的サイトで、これまでユーザーの★/非表示/検索条件を
ブラウザの localStorage だけに保存していた。しかし iOS Safari の ITP は7日間未訪問の
localStorage を自動削除するため、間が空くと状態が消える。このAPIは、その状態JSONを
PostgreSQL に永続化するだけの最小サーバであり、静的サイト本体はそのままGitHub Pagesに残す
「ハイブリッド増築」構成(vps-deploy スキルの hybrid-state-api.md ケースC)の実装。
利用者は `ken`/`yumiko`/`ayako` の3名で、キー単位で状態を分離する。

## 認証モデル(重要)

**このAPIは認証を持たない設計。キーを知っていれば誰でもその状態を読み書きできる。
だから機微データは絶対に置かない。**

`key`(`ken`/`yumiko`/`ayako`)は人が読める合言葉であり、実質的にパスワードの代わりを
兼ねている(この命名は意図的な決定であり、変更しない)。しかしトークン検証やパスワード
照合のような認可の仕組みは無く、キー文字列そのものが「知っていればアクセスできる」識別子
に過ぎない。CORS(下記)はブラウザの中での読み取りを制限するだけで、curlや別サーバからの
リクエストはそのまま素通りする。**CORSはアクセス制御ではない。**
保存する`state`は★/非表示/検索条件などのUI設定に限定し、個人情報・認証情報等の
機微データは一切含めないこと。

## エンドポイント

ベースURL: `https://staff.negura.website/akiya-state`(`BASE_PATH=/akiya-state`)

| メソッド | パス | 概要 |
|---|---|---|
| GET | `/healthz` | 認証不要。DB疎通確認。200 `{"ok":true,"db":"up"}` / 503 |
| GET | `/state/{key}` | 200 `{"state":{...}\|null,"updated_at":"..."\|null}` / 未知キーは404 |
| PUT | `/state/{key}` | body `{"state":{...}}`。UPSERT。200 `{"ok":true,"updated_at":"..."}` |

`key` は環境変数 `STATE_KEYS`(カンマ区切り)の allowlist に完全一致するものだけを受け付ける。
未登録キーは常に404(自動作成しない)。`state` の中身はサーバ側で解釈せずそのまま保存する。

`/docs` `/redoc` `/openapi.json` は無効化している(3エンドポイントのみの内輪向け最小APIで
Swagger UIを公開する意味が無く、外部CDNへの依存も避けたいため)。

## 防御(濫用対策であって認可ではない)

このAPIには認証が無いため(上の「認証モデル」参照)、以下は「データへのアクセスを制限する」
ものではなく、**共有インフラを無認証リクエストだけで巻き添えにしない**ための濫用対策に過ぎない。

- **DB同時接続数の上限**(既定5本、`DB_CONNECTION_LIMIT`)。このPostgreSQLはkintai/akky
  (共に本番稼働中)と共有しており、無認証のGETだけで接続を使い切らせるわけにいかない。
  上限に達したリクエストは待たせず503を返す。
- **レート制限は送信元IP単位**、GET(`/state/{key}` `/healthz`)・PUT両方に適用
  (既定いずれも60回/分。`GET_RATE_LIMIT_MAX` / `PUT_RATE_LIMIT_MAX`)。超過は429。
  IPはCloudflare Tunnel経由の`X-Forwarded-For`先頭要素(無ければTCP接続元)を使う。
  以前は「保存先キー」単位だったため、第三者が特定キーへ空PUTを送り続けるだけで
  本人を恒久的に締め出せる欠陥があった。ただし`X-Forwarded-For`はクライアントが
  自由に送信できるヘッダで偽装可能なため、これも認可ではなく濫用の抑止に過ぎない。
- ボディサイズ上限(既定256KB、`MAX_BODY_BYTES`)。Content-Lengthを騙っても実読み込みで打ち切る。
- CORSは `CORS_ORIGIN` で指定した単一オリジンからのブラウザ内読み取りのみ許可する
  (`*` にしない)。繰り返しになるが、これはブラウザの中だけの制限であり、curl等の
  非ブラウザからのアクセスを止めるものではない。

## ローカル実行

```
pip install -r requirements.txt
DATABASE_URL=postgresql://user:pass@localhost:5432/akiya_state \
STATE_KEYS=ken,yumiko,ayako \
CORS_ORIGIN=http://localhost:5500 \
uvicorn app.main:app --reload
```

`DATABASE_URL` が疎通できない場合、起動時の `CREATE TABLE IF NOT EXISTS` で必ず起動に失敗する
(SQLiteへのフォールバックは無い。本番と挙動を変えないための意図的な仕様)。

`CORS_ORIGIN` に `*`・末尾スラッシュ・空白混入がある場合も、起動時の検証(RuntimeError)で
同様に起動が止まる。誤設定は「ブラウザ側でCORSが黙って失敗するだけでサーバは200を返し続け、
気づかれずに同期だけが死ぬ」事故につながるため、あえてフェイルファストにしている。
