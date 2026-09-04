# 指示書 W2: キャンプ場土地の新規サイト（新規パーサ5本）

発注: 2026-09-05 オーケストレータ／消込表 #472「沼津IC圏の監視拡大」。**W1（`docs/tasks/20260905_camp_expand_w1.md`）完了後に着手**。
前提資料: W1と同じ＋`docs/SPEC.md` §9「アダプタの型」・既存の類似パーサ（`parse_sumaimy_rent`・`parse_resort_estate`・`parse_sanrinbank`・`parse_yamaichiba`）。
URLは `docs/research/20260905_camp_sites_candidates.md` から**一字一句コピー**。追加前に各URLを `Invoke-WebRequest` で再実測し `note:` に実測を書く。

## 着手前4問
1. 同じ知識: 価格/面積の正規化は `parse_price_man`/`parse_area_sqm`/`_to_sqm`、レコード化は `_make_record` のみ（各パーサで独自に計算しない）。市町判定は `extract_machi`（W1で拡張済み）。
2. 同種の既存機能: スマイミー売土地＝`parse_sumaimy_rent` と同一基盤（賃貸→売買の差は価格の単位と敷礼列の有無）。フォレステ＝`parse_yamaichiba`/`parse_sanrin_net` の ha→㎡・円→万円の型。KSI＝`parse_sanrinbank`（全国一覧から `@camp_tiers` で所在地一致抽出）の型。
3. 引っ越しか: いいえ。
4. 過去の類似: 2026-07-03/04/12 のcampアダプタ追加（WORKLOG）。BotBlocked/時間予算/スリープの作法は既存パーサに倣う。

## 追加するサイト（優先順。上から順に実装し、1サイトごとにコミット）
1. **熱海不動産イーズ**（1パーサ `parse_ez` で2エントリ・channel ③-B・tab camp・`@camp_tiers`）
   - `ez_fujisan` … `https://www.e-z.co.jp/search/list-spec.php?s=203&i=1`（特徴検索「富士山眺望」。name に「富士山眺望」を含める。**「すべて表示」リンクがあれば追従**）
   - `ez_land` … `https://www.e-z.co.jp/search/list-estate.php`（土地一覧）
   - 期待値: 調査で「伊豆の国市奈古谷 1,032㎡ 100万円」「函南町丹那 370万円」等が実在。抽出結果にこれらが出ることを完了条件にする
2. **スマイミー静岡 売土地**（`parse_sumaimy_land`。既存 `parse_sumaimy_rent` を継承/共通化し、価格は `parse_price_man`）
   - URL形式: `https://www.shizuoka.fudohsan.jp/一覧/買う-土地/地域/<市町>`（調査で 御殿場市/田方郡函南町/伊豆市/富士宮市 が実測200）。
     まず実測済み4市町を追加し、続けて 裾野市/伊豆の国市/駿東郡小山町/熱海市/富士市 を **(a)実測して200のものだけ**追加（404は追加せず報告）。id `sumaimy_land_<ローマ字>`・channel ①・tab camp。市町別URLなので filter_keywords 不要。ページャは賃貸版と同じ（最大3頁）
3. **しずなび 土地**（`parse_sest`。`https://buy.s-est.co.jp/area/<slug>/land/`。実測済み slug: fujinomiyashi/susonoshi/gotenbashi/tagatagun/izushi。続けて izunokunishi/fujishi/shuntogun-oyamacho を(a)実測して200のみ追加。id `sest_<slug>`・channel ①・tab camp）
4. **KSI官公庁オークション 不動産**（`parse_kankocho_ksi`。`https://kankocho.jp/search/real-estate/?page=1&pageSize=50`。id `kankocho_ksi`・channel ⑥・tab camp・`@camp_tiers`。name は「KSI官公庁オークション 不動産（公売・現状渡し）」。価格は円→万円。**既存 sources_extra の `kankocho-athome.jp`（アットホーム版・JS）とは別運営**と note に明記）
5. **フォレステ（山いちばグループ）**（`parse_foreste`。`https://foreste.yamaichiba.com/`。id `foreste_zenkoku`・channel ⑦・tab camp・`@camp_tiers`。面積 ha→㎡、価格 円→万円。現在は圏内在庫0＝新着待ち。抽出0件でも「パーサが全国在庫N件を読めている」ことをログで示す）

### ストレッチ（(a)(b) を通し、静的に一覧が読める時だけ追加。無理なら sources_extra に実測付きで記録）
- 朝霧高原「富士山麓不動産情報」 `http://www3.tokai.or.jp/tosei/betusou.html`（httpのみ。`requests` で本文を取り、一覧の所在を特定。富士山ビューの本命エリア）
- 東急リゾート 山中湖・河口湖 土地絞込 `https://www.tokyu-resort.co.jp/search/result/?SHUBETSU_ID[3]=1&HPSRC_AREA_ID[37]=1&area_top_flg=1`（未実測。土地のみの件数と価格帯を確認）

## 各パーサの必須事項（既存に倣う）
- シグネチャ `parse_x(first_html, base_url, filter_keywords, filters, session)`、戻りは `_make_record` 由来のリスト
- robots は `fetch` 側が見る。ページャ追従は `SITE_TIME_BUDGET` と同一内容/カード0で打ち切り、2〜8秒スリープ
- 極小ページ/bot対策の疑いは `BotBlocked`（前回スナップショット保持）
- `SITE_ADAPTERS` への登録は**前方一致の順序**に注意（`sumaimy_land_` は `sumaimy_` より前）
- 各サイトで **価格・面積・所在地の抽出例を3件**ログに出す（単位換算の誤りを目で確認できるように）
- JS `SITE_PREFIXES` に短縮名を追加し衝突検査（W1と同じ方法）

## 検証（完了条件）
- 各サイト `py watch.py --only=<id> --dry-run` 完走。件数・抽出例3件・`machi` 判定例を報告
- 単体テスト（既存のテスト方式があればそれに追加、無ければ `tests/` に最小のpytest）: ha→㎡・円→万円・スマイミー売買価格の3ケース
- `py watch.py --rebuild` でJSエラー0・`git status --short reports SOURCES.md` 空
- `docs/SPEC.md` §9 のアダプタ表に行を追加、§3 のサイト数更新。`docs/WORKLOG.md` 1行。sources_extra に落としたものは urls.yaml に実測付きで記録

## 規律
- 1サイト=1コミット。pushしない。URLは調査結果からコピー。推測で書かない。再委任禁止。
- 完了報告: サイトごとの (id/URL/HTTP/件数/抽出例3件)・見送ったものと理由・残リスク。
