# 指示書 W3: robots.txt 判定をRFC 9309準拠にし、KSI官公庁オークションを有効化する

発注: 2026-09-05 オーケストレータ／消込表 #472。W2 の保留事項の解消。
前提資料: `watch.py` の `robots_allowed()`・`fetch()`・`SITE_ADAPTERS` の `kankocho_ksi`・`parse_kankocho_ksi`／`urls.yaml` の `sources_extra` にある `kankocho_ksi`／`docs/SPEC.md` §5・§9・§10／`tests/test_watch_units.py`。

## 背景（W2の実測）
`https://kankocho.jp/robots.txt` は `Disallow: /search/` と `Allow: /search/real-estate/$` を持つ。RFC 9309（および Google の実装）では
**最長一致の規則が勝ち、`$` は「ここでURL終端」**を意味する。つまりサイト運営者は「`/search/real-estate/`（クエリ無しの一覧トップ）だけは
クロールしてよい」と明示している。ところが本プロジェクトの `robots_allowed()` は Python 標準 `urllib.robotparser` を使っており、
①先頭一致・記述順で判定（最長一致ではない）②`$`・`*` を解釈しない、ため、この明示許可を読めず False を返す。
**サイトの意思に反して禁止側に倒れている**（迂回ではなく、運営者が許可した範囲を正しく読む修正）。

## 着手前4問
1. 同じ知識を持つ場所: robots判定は `robots_allowed()` の1箇所のみ（`grep -n robots watch.py` で確認して報告）。
2. 同種の既存機能: なし。ただし判定結果は**全140サイトの fetch 可否**に効く共有基盤なので、下記Cの回帰差分を必須とする。
3. 引っ越しか: いいえ。
4. 過去の類似: `docs/BUGLOG.md`・`docs/WORKLOG.md`・`git log --grep=robots` を検索して報告（あれば判断理由まで読む）。

## 実装
A. `robots_allowed(url, session)` を RFC 9309 準拠の自前実装に置き換える（外部ライブラリ禁止・標準ライブラリのみ）:
   - robots.txt を取得（既存のキャッシュ/取得経路があれば流用。取得失敗（非200・例外）は**従来どおりの扱い**を維持＝現状が「許可」なら許可）
   - グループ選択: 自分の User-agent（既存の UA 文字列の product token）に一致するグループがあればそれ、無ければ `*`。大文字小文字無視
   - 規則: `Allow`/`Disallow` の path pattern。`*`＝任意文字列、末尾 `$`＝終端。パーセントエンコード差は正規化して比較
   - 判定対象は **path＋query**（`urllib.parse.urlsplit` の path と `?query`）。**最長一致（パターン長）が勝ち、同長なら Allow 優先**。空の Disallow は全許可
   - 単体テスト（`tests/test_watch_units.py` に追加）: ①`Disallow:/search/`＋`Allow:/search/real-estate/$` で `/search/real-estate/`＝許可・`/search/real-estate/?page=1`＝**不許可**・`/search/other`＝不許可 ②`*` ワイルドカード ③グループ選択（自UA vs `*`）④空Disallow＝全許可 ⑤取得失敗時の扱いが従来と同じ
B. `kankocho_ksi` を `sources_extra` から `sites`（`tab: camp`・channel ⑥）へ移す。**URLは robots が許可する `https://kankocho.jp/search/real-estate/`（クエリ無し・1ページ目のみ）に変更**し、`parse_kankocho_ksi` はページャ追従・`pageSize` 付与をしない（2ページ目以降は Disallow のため）。note に「robots: /search/real-estate/$ のみ許可＝1頁目のみ。2026-09-05 実測 HTTP 200・N件・圏内M件」。`Invoke-WebRequest` で再実測すること。
C. **回帰差分（必須）**: 全140サイト＋sources_extra の各 URL について、旧実装（robotparser）と新実装の判定を並べて出力するスクリプトを一時的に書き（`_pipeline/` 配下・コミットしない）、**判定が変わったURLを全部列挙して報告**。変わったものは1件ずつ robots.txt の該当行を引用して「新実装が正しい」ことを示す。説明できない差分が出たら実装を直す（説明できないまま進めない）。
D. `py watch.py --only=kankocho_ksi --dry-run` 完走、抽出例3件（所在地・価格・面積）と machi 判定をログで示す。pytest 全件 green。ast OK。`--rebuild` でJSエラー0・`git status --short reports SOURCES.md` 空。
E. 文書: `docs/SPEC.md` §5（robots判定の記述を「RFC 9309準拠・最長一致・`$`対応」に）・§9（kankocho_ksi 行）・§11（sources_extra から外す）。`docs/DECISIONS.md` §2 に1行（robots はRFC準拠で読む＝運営者の明示許可を取りこぼさない／迂回はしない）。`docs/WORKLOG.md` 1行。`docs/BUGLOG.md` 1行（症状: robots の明示Allowを読めず取得可能サイトを保留していた／原因: robotparser の先頭一致・`$`非対応／commit: (push後に追記)／@jissou ヨコテン: 回帰差分で全140サイト確認・変化N件／グローバル: 済(kankyo-policy NOTES に「urllib.robotparser はRFC 9309非準拠」を1行追記)）。
   kankyo-policy NOTES への追記: `C:\Users\mntns_05egufd\.claude\skills\kankyo-policy\NOTES.md` 末尾に `- 2026-09-05 | 事象 | 案:` の1行（追記のみ・他行を触らない）。

## 規律
- 作業単位（A→B→E）ごとにコミット（`git add <明示ファイル>`・`-A`禁止・pushしない）。`docs/WORKLOG.md` は追記のみ。
- 再委任禁止。完了報告: 回帰差分の一覧（変化件数と各件の根拠）・kankocho_ksi の実測・テスト結果・残リスク。

## F. 追加スコープ: `--only` 指定時は本番成果物を書かない（2026-09-05 オーケストレータが実害を確認）
**事象**: `py watch.py --only=ez_fujisan --dry-run` を実行しただけで `reports/index.html`・日付別html/csv・`SOURCES.md` が**その1サイト分のデータだけ**で上書きされ、古い日付別レポートが `prune` で削除された（コミット前に気づき `git checkout` で復元。W2も同じ罠を踏んで都度復元していた）。`--dry-run` はスナップショットを保存しないだけで、レポート出力は本番経路のまま。
**やること**: `--only` が指定された実行では、`--rebuild` と同じ出口（`reports/_preview.html` のみ・警告バナー付き・`prune` しない・`SOURCES.md` を書かない）にする。`emit_reports(..., preview=True)` が既にあるので、`--only` の有無で `preview` を決めるだけで済むはず（実装前に `emit_reports` の呼び出し元を全部 grep して、他の経路を壊さないことを確認）。`--dry-run` 単独（全サイト）は従来どおり本番成果物を書いてよいか？→ **書かない側に倒す**（`--dry-run` も preview 出口にする。本番成果物を書くのは「引数なしの通常実行」だけ、と SPEC §8 に明記）。
**検証**: `--only=ez_fujisan --dry-run` 実行後に `git status --short reports SOURCES.md` が空、`reports/_preview.html` にバナーがある。通常実行の出口は変えない（`emit_reports` の呼び出しを読んで説明）。
**文書**: SPEC §8 の表を更新、DECISIONS §3 に1行、BUGLOG 1行（症状/原因: 部分実行とレポート出力の分離が無かった/commit/@sekkei ヨコテン: emit_reports 呼び出し元の全数棚卸し N件 グローバル: 不要(このアプリ固有の出口設計)）。
