# 指示書 W4: 箱根外輪山の西側・北西側斜面（御殿場・裾野・小山）の監視を網羅化する

発注: 2026-09-05 オーケストレータ／消込表 #479。
根拠資料（必ず先に全文読む）: `docs/research/20260905_hakone_west_oaza_and_sites.md`（大字の実測とサイト候補。**URLはここから一字一句コピー**）、
`docs/tasks/20260905_camp_expand_w1.md`〜`w3.md`（直前3工程の型）、`docs/SPEC.md` §3.5/§4/§9、`docs/DECISIONS.md` §0（P2: 個人の事情は設定で表現し機能を分岐しない）、
`urls.yaml`（`takken_*`・`jmty_land_camp`・`filters:`）、`watch.py` の `_make_record`（`interest = [kw for kw in filters.get("interest_keywords"...)]` 付近）・`parse_takken`・`parse_jmty`・`SITE_ADAPTERS`・JS `SITE_PREFIXES`・参考情報列（`interests` バッジ）。

## 着手前4問
1. 同じ知識を持つ場所: 「本命/注意キーワード→バッジ」の判定は `_make_record` の1箇所（`interest`/`caution`）。市町判定は `extract_machi`。**新設する「地区グループ」も同じ場所で判定し、別ルートを作らない**。
2. 同種の既存機能: `interest_keywords`（文字列一致→バッジ）。今回はその**グループ版**（複数の大字を1つのラベルで表示・市町で絞る）を足す。サイト追加は `takken_*`（parse_takken流用）・`jmty_land_camp`（parse_jmty流用）・W2の新規パーサ群の型。
3. 引っ越しか: いいえ（追加のみ）。
4. 過去の類似: W1〜W3（WORKLOG 2026-09-05）。BUGLOG 2026-09-05「sup分断」「robots」「--only出口」＝新規パーサでは `_numeric_cell_text` を使い、robots判定に任せ、試走は `--only --dry-run`（preview出口）で行う。

## A. 「地区グループ」バッジ（設定で表現・コード分岐なし）
`urls.yaml` の `filters:` に **`interest_groups`** を新設（基本＝更地/家付きタブに効く。`camp` は基本を継承。`rent` は継承しない＝現行の rent 上書き方式に合わせる）:
```yaml
  # 地区グループ: 「市町 + 大字」で一致したら参考情報列にグループ名のバッジ（本命=緑）を付ける。
  # 大字だけの一致にしない（「神山」は箱根町にも、「東山」は熱海市にもある）。
  # 出典・実測: docs/research/20260905_hakone_west_oaza_and_sites.md Part1（郵便番号一覧94大字×地理院標高APIで斜面向きを実測）
  interest_groups:
    外輪山西麓:   # 箱根外輪山の西側・北西側斜面。一次圏8＋二次圏で西向き/別荘地帯のもの
      御殿場市: [深沢, 東田中, 二子, 東山, 二の岡, 神山, 沼田, 萩蕪, 大坂]
      裾野市: [茶畑, 深良, 久根]
      小山町: [竹之下, 新柴, 桑木, 中島]
```
- 判定: `machi == 市町` かつ `location`（無ければ本文）に大字を含む → `interest` にグループ名（例「外輪山西麓」）を1回だけ追加（大字が複数一致しても1バッジ）。既存の `interest_keywords` 判定は変えない。
- 画面: 既存の本命バッジ（緑・`bi`）として出る。凡例の参考情報説明に「地区グループ（例: 外輪山西麓）」を1語足す程度で、**説明文に腱さんの戦略を書かない**（P2）。参考情報列のフィルタでグループ名で絞れること（既存のバッジ絞り込みがあればそれに乗る）。
- スナップショットには `interest` として保存されるので `--rebuild` でも復元される（確認）。
- 単体テスト: 「御殿場市深沢」→付く／「箱根町神山」→付かない／「熱海市東山」→付かない／大字2つ一致でもバッジ1つ。

## B. サイト追加（URLは根拠資料からコピー。追加前に Invoke-WebRequest で再実測し note に書く）
| id | URL | tab | channel | parse | 備考 |
|---|---|---|---|---|---|
| `takken_gotemba` | 空き家バンクしずおか 御殿場市（資料 #1） | camp | ② | 既存 `parse_takken` | id前方一致 `takken_` で自動 |
| `takken_oyama` | 同 駿東郡小山町（#2） | camp | ② | 同 | |
| `takken_susono` | 同 裾野市（#3） | **home**（裾野は更地/家付きの7市町に含まれる） | ② | 同 | 既存 `takken_*` と同じ `kind` |
| `jmty_land_gotemba` | ジモティー 御殿場市で絞込（#10） | camp | ④ | 既存 `parse_jmty` | `filter_keywords: "@camp_tiers"` |
| `takane_gotemba_sanrin` | 高根不動産 事業用・山林農地 御殿場（#6） | camp | ③-B | **新規 `parse_takane`** | http のみ。**「成約済/売止/申込済」行は落とす**（価格欄が数値でない行を捨てる） |
| `takane_gotemba_tochi` | 高根不動産 売土地 御殿場（#7） | camp | ③-B | 同 | |
| `takane_oyama_tochi` | 高根不動産 売土地 小山町（#8） | camp | ③-B | 同 | |
| `juwa_gotemba_tochi` | 住和 売土地 御殿場（#4・`search279.html`） | camp | ③-B | **新規 `parse_juwa`** | ハブ `b_bukken.html` は使わない。運営者名は現物の本文で確認して name に書く（WebFetch の「株式会社十和」誤読の疑いあり） |
| `juwa_oyama_tochi` | 住和 売土地 小山町（#5・`1-0t.html`） | camp | ③-B | 同 | |
| `tomeichisan_susono` | 東名地産（裾野。#9） | home | ③-B | **新規 `parse_tomeichisan`** | 売買一覧の正しいURLをサイト自身のナビから採取して(a)(b)。`/osusume_b/` は404＝使わない |
| `housedo_gotemba` | ハウスドゥ.com 御殿場市（#11） | camp | ③-B | **新規 `parse_housedo`** | ページャ `/list/?page=N` 等はサイトから採取 |
ストレッチ（(a)(b)が通り静的に読めれば追加。無理なら sources_extra に実測付きで記録）: アットハウス「売土地」全件URL（資料 2-6）。
見送り（今回は追加しない・理由は資料）: goo住宅（SUUMO/LIFULLの再掲）、E-LIFE（御殿場404・面積なし）、第一開発（市町絞込不明）、競売公売.com（既存と重複）、オフィスはせがわ（価格面積なし）、御殿場地所（在庫1件）。

各パーサの必須事項は W2 指示書と同じ（`_make_record` 経由・`_numeric_cell_text`・BotBlocked・時間予算・スリープ・`SITE_ADAPTERS` の前方一致順序・抽出例3件をログ・`SITE_PREFIXES` 短縮名と衝突検査）。

## C. 検証（完了条件）
- 各サイト `py watch.py --only=<id> --dry-run`（preview出口）完走。件数・抽出例3件・`machi`・**「外輪山西麓」バッジが付いた件数**をログで示す（高根の 上小林4,444㎡/1,075万円・大堰1,958㎡/472万円、住和の 深沢340万円/253㎡ が出ること）
- pytest 全件 green（A のテスト含む）。ast OK。`--rebuild` で JS エラー0、参考情報列に「外輪山西麓」バッジが表示され、参考情報フィルタで絞れる。`git status --short reports SOURCES.md` 空
- 既存の更地/家付き/賃貸タブの件数が変わらない（`--rebuild` の DATA で前後比較。裾野の home 追加分＝takken_susono/東名地産 は更地/家付きに増える＝想定内として増分を報告）

## D. 文書（同じコミットで）
- `docs/SPEC.md`: §3 サイト数、§3.5 に「外輪山西麓の大字（地区グループ）」小節（一次圏8＋二次圏の表を圧縮して転記・出典パス・限界の1行）、§4 に `interest_groups` の定義と EARS 1文、§9 アダプタ表に4関数を追加
- `docs/DECISIONS.md` §1 に1行（2026-09-05 外輪山西麓を地区グループとして設定に持つ。P2に従いコード分岐なし。大字は実測で確定＝たたき台の10か所が誤りだった）
- `docs/WORKLOG.md` 1行。BUGLOG は新規バグが出た時のみ

## 規律
- 作業単位（A→B各サイト→D）ごとにコミット（`git add <明示ファイル>`・`-A`禁止・pushしない）。1単位終わるごとに必ずコミット（途中停止に備える）。
- 再委任禁止。試走で reports/SOURCES.md が変わらないこと（W3で出口分離済み。変わったら報告）。
- 完了報告: A のテスト結果／サイトごとの id・URL・HTTP・件数・抽出例3件・バッジ件数／見送り／残リスク。
