# BUGLOG

## 分析済み（2026-07-28 レビュー）

- 2026-07-26 | 状態同期(watch.py `_FILTER_JS`)がnull値の意味論不備で★/非表示/検索条件を消失させる複合バグ（exareas null化で全タブ0件表示に固定/空レコードのpullで★・非表示が全消去/pull失敗後もpushしサーバの正データを貧弱なローカルで上書き/デバウンス中の離脱で変更消失） | 原因: 「サーバに未設定のキー=null」をそのまま送受信・適用しており、「サーバに無いキー＝ローカルを維持」という意味論が実装されていなかった | commit: fc3d656 | #data-loss #state-sync @sekkei → 対策: なし（単発）
- 2026-07-26 | state-api: CORS_ORIGIN起動時検証が不十分で、カンマ区切り複数指定/スキーム欠落/大文字混在/既定ポート明記(:443)の4パターンが検証を素通りして起動し、その後ACAOヘッダを一切返さず同期が無言死する(敵対的レビューで実測) | 原因: 検証が`*`/末尾スラッシュ/埋め込み空白の3パターンの文字列チェックのみで、urllib.parse.urlsplitによる構造的検証が無かった | commit: fc3d656 | #config-validation #silent-failure @jissou → 対策: なし（次週: 設定値の起動時検証パターン候補・error-policy受け皿見込み）
- 2026-07-26 | state-api: DB_CONNECTION_LIMIT/PUT_RATE_LIMIT_MAX/GET_RATE_LIMIT_MAX/MAX_BODY_BYTESに起動時のレンジ検証が無く、DB_CONNECTION_LIMIT=0で全リクエスト恒久503(healthzは「db down」と偽装)・=-1でthreading.Semaphore生成がValueErrorとなりモジュールimport自体が失敗してrestart:unless-stoppedがクラッシュループ・=500で上限対策自体が無意味化(敵対的レビューで実測) | 原因: 環境変数をint変換するのみでレンジチェックが無く、さらにDB_CONNECTION_LIMITをモジュール読み込み時に直接threading.Semaphoreへ渡していたため異常値がフェイルファストなRuntimeErrorではなくimport時例外になっていた | commit: fc3d656 | #config-validation #crash-loop @jissou → 対策: なし（次週: 設定値の起動時検証パターン候補・error-policy受け皿見込み）
- 2026-07-26 | `--rebuild`(クロールなし再生成)の初版が本番レポートを劣化させて上書きした。賃貸の種別が全件「賃貸その他」・間取り0件・敷金0件・建築可否△が193→1件になった状態でreports/index.htmlを書き換え、公開しかけた（実測比較で発覚しgit checkoutで復元） | 原因: スナップショットが判定結果を保存しておらず再現できないのに、出力先を本番成果物と同じにしていた。「劣化しうる再生成」と「本番成果物」を分離する設計になっていなかった | commit: 7fd9235 | #data-quality #destructive-default @sekkei → 対策: なし（単発）
