# 指示書: キャンプ場土地タブに「富士山可視度」列を追加（fujimap 連携）2026-09-19

## 目的
兄弟プロジェクト fujimap（C:\Claude\31_fujimap）が、国土地理院DEMから「富士山がどのくらい見えるか」を
200mメッシュで計算し、**大字（町丁・字等）単位に集計した表** `docs/oaza_scores.json` を出す（本日並行で実装中）。
akiya-watch の**キャンプ場土地タブだけ**に、各物件の大字の可視度（0〜100。レンジ表示あり）を列として出す。
更地／家付き土地／賃貸タブには出さない（腱さん指示）。

## カイシュウ5問（着手前・答え済み）
1. 同じ知識の置き場: 「大字名がテキスト中に単独の地名として出現するか」は `watch._oaza_hit()`（interest_groups用）が持つ。
   今回の照合も同じ規則が要るので、**`_oaza_hit` を新モジュール `fujiview.py` の `oaza_hit()` へ移し、watch.py は
   `from fujiview import oaza_hit as _oaza_hit` で従来名を維持**（既存テスト8件はそのまま通す）。可視度の「表示ラベルの
   組み立て」は JS の cellHtmlInner 1箇所、CSV は write_csv_report 1箇所（値は同じ埋め込みフィールドから）。
2. 同種の既存機能: 列追加の前例＝坪単価(range列)・建築可否。埋め込み(build_html_report data)・snapOf(★スナップショット)・
   colsFor・cellHtmlInner・モバイルCSS(data-k)・CSV の6点セットを揃える（1つ落とすとその画面だけ欠ける）。
3. 引っ越しではない。
4. 過去の類似改修: WORKLOG 2026-09-05 W4（interest_groups＝市町+大字の一致でバッジ。`_oaza_hit` の「神山/神山平」問題）。
   今回の照合はその教訓（直後が漢字なら別地名）を継承する。
5. 所有: 富士山可視度の読込・照合は `fujiview.py` が単独所有（`# @owns 富士山可視度（fujimap oaza_scores.json の読込と大字照合）`）。
   watch.py は呼ぶだけ。

## 作業場所
- `C:\Claude\30_akiya-watch`（main。pull 済み。未追跡の `AGENTS.md` は触らない）。作業単位ごとにコミット、push はしない。
- `git add -A` 禁止（自分が触ったファイルだけ明示）。spawn_task を呼ばない（別件は完了報告に書く）。
- 本番レポート（reports/index.html 等）は書き換えない。確認は `py watch.py --rebuild` → `reports/_preview.html`。

## 入力データの形（fujimap 側の出力仕様。実ファイルはオーケストレータが後で `data/fujimap/oaza_scores.json` へ置く）
```json
{"generated":"2026-09-19T..","scale":"富士山可視度 0-100（...）","bounds":[[..],[..]],"mesh_interval_m":200,
 "source":{"viewshed":"...","boundary":"..."},
 "rows":[{"pref":"静岡県","city":"函南町","oaza":"平井","lat":35.1,"lon":138.98,"n":123,"min":0,"p10":5,"p50":40,"p90":80,"max":95,"method":"cells"}]}
```
- `city` は郡名なし（「函南町」「静岡市葵区」）＝ akiya-watch の `machi` と同じ表記。`oaza` は「大字」接頭・「丁目」を除いた正規化名。
- `n=0, method="point"` は小さい町丁目（代表点1セルの値）。
- 意味: 0=見えない、100=麓近くまで見通せる（地形のみ。樹木・建物は非考慮）。`p10〜p90` がレンジ、`max` が大字内の最良地点。

## 実装
### A. `fujiview.py`（新規・リポジトリ直下）
- `oaza_hit(oaza, text) -> bool`: watch.py の `_oaza_hit` を移設（docstringごと）。
- `load_table(path=None)`: 既定 `data/fujimap/oaza_scores.json`。無ければ空の索引（起動時に1行だけ標準出力に注意を出す）。
  索引は `{city: {oaza: row}}`。モジュール内キャッシュ（1回だけ読む）。
- `lookup(location: str, machi: str) -> dict | None`:
  1. machi が空／索引に無い → None。
  2. text = NFKC 正規化した location から空白を除去。machi が text に含まれれば **machi の直後から**の残り文字列、
     無ければ text 全体を残り文字列とする。残り文字列の先頭の「大字」を除く。
  3. 候補（その市町の oaza 一覧）のうち **残り文字列の先頭に一致する最長のもの**を採用。無ければ、残り文字列中に
     `oaza_hit()` で単独地名として出現する最長のものを採用。それも無ければ None。
  4. 戻り値 `{"lo":p10,"hi":p90,"med":p50,"max":max,"n":n,"oaza":oaza,"method":method}`。
### B. `watch.py`
- `_oaza_hit` の定義を削除し `from fujiview import oaza_hit as _oaza_hit`（テストは `watch._oaza_hit` を参照し続けられる）。
- `build_html_report` の data 埋め込みに追加（全レコードで計算してよい。表示だけ camp タブ）:
  `fuji`(=hi・数値。range絞り込み/並べ替え用)・`fuji_lo`・`fuji_med`・`fuji_max`・`fuji_n`・`fuji_oaza`（None のときは全部 None/""）。
- JS `snapOf` の keys に上記6キーを追加（★スナップショットで消えないように）。
- JS `colsFor(tab)`: `tab==='camp'` のときだけ `{k:'fuji',l:'富士山',f:'range'}` を坪単価の直後に挿入（他タブは無変更）。
- JS `cellHtmlInner` に `case 'fuji'`: 
  - 値なし → `—`。`fuji_max===0` → `0`。`hi-lo<=5` → `med` の1値。それ以外 → `lo〜hi`。
  - `title` に「大字:○○ 最小/中央/最大=a/b/c（200mメッシュ n 個）」。
  - ヒートマップ `hFuji(v)`（v=`fuji`）: ≥80 濃緑(#1a7d36 白字)／≥60 緑(#66bb6a)／≥40 黄(#ffe082)／≥20 橙(#ffb74d)／それ未満 赤(#ef9a9a)／null 無色。
- 凡例（`legendRow` 内の「参考情報:」行の近く）に camp タブのときだけ「富士山: 大字ごとの可視度0〜100（数字2つはレンジ・地形のみの機械判定）」を1行足す。
- モバイル（700px以下）: `fuji` 列は**残す**（キャンプ場土地の核心指標）。375px で実測して崩れるなら data-k=fuji を非表示リストへ入れ、報告にその旨を書く。
- CSV `write_csv_report`: 「富士山可視度(p10-p90)」「富士山可視度(最大)」の2列を末尾に追加（None は空欄）。
  値は build_html_report と同じ `fujiview.lookup()` の結果から作る（同じ知識を2箇所に持たないよう、
  `fujiview.py` に `summarize(hit) -> (label, max)` のような**1つの整形関数**を置き、HTML側は JS なので JS 版と同じ規則を
  コメントで相互参照する。Python側の label 規則は JS と同じ: max==0→"0"／hi-lo<=5→med／それ以外→"lo〜hi"）。
### C. データ置き場
- `.gitignore` に `!data/fujimap/` を追加（`data/*` が無視されるため）。
- `data/fujimap/README.md`: 出所（31_fujimap `docs/oaza_scores.json`・生成日は JSON の `generated`）、更新手順
  （fujimap で `py -3 analyze/viewshed.py` → `py -3 analyze/oaza_scores.py` → このフォルダへコピー → コミット）、出典表記
  （国土地理院 標高タイル／e-Stat 国勢調査2020 小地域境界）。
- 実ファイルがまだ無ければ、`C:\Claude\31_fujimap\docs\oaza_scores.json` が存在するか確認し、あればコピーして使う。
  無ければテストは fixture だけで進め、`--rebuild` は「列が出て値は — 」の状態で確認する（報告にどちらだったか書く）。
### D. テスト `tests/test_fujiview.py`
- fixture: `tests/fixtures/oaza_scores_sample.json`（函南町: 平井/丹那/畑毛、御殿場市: 神山/神山平、伊豆市: 冷川 程度）。
- ケース: 「静岡県田方郡函南町平井1689-55」→平井／「函南町 丹那」→丹那／「伊豆市大字冷川」→冷川／
  「御殿場市神山平」（候補に神山・神山平）→神山平（最長）／「御殿場市神山平」（候補に神山のみ）→None（直後が漢字）／
  machi 空→None／索引に無い市町→None／ファイル無し→None。`summarize` の3規則も。
- 既存 `python -m pytest tests -q` 全緑。
### E. 文書（B変更＝同じコミットで）
- `docs/SPEC.md` §1 の camp 行「可視判定はシステム化対象外・目視確認」→ fujimap 連携の1文に更新、§5 の埋め込み
  フィールド（fuji 系）、§6 の列定義（camp は「富士山」列あり＝3パターン目）と凡例、ヒートマップ表に富士山の閾値。
- `docs/DECISIONS.md` 末尾に1行（2026-09-19・富士山可視度を大字単位で fujimap から取り込む・なぜ大字単位か＝住所が
  大字までしか取れない・点ではなくレンジで出す）。
- `docs/README.md` の文書の地図に `data/fujimap/README.md` を1行。
- `docs/WORKLOG.md` に1行（作業単位ごと）。
## 完了条件
- pytest 全緑（件数を報告）。`py -c "import ast;ast.parse(open('watch.py',encoding='utf-8').read())"` OK。
- `py watch.py --rebuild` が完走し `reports/_preview.html` の camp タブに「富士山」列が出る。ブラウザ（Claude Browser の
  preview か `py -m http.server`）で 1280px と 375px のスクリーンショット、コンソールエラー0 を確認して報告に貼る。
- 実データがあった場合: camp タブ対象レコードのうち `fuji` が付いた件数／全件（照合率）と、照合できなかった location の例を
  10件報告する（次の改善材料）。
- 完了報告に「カイシュウ5問: 済」「変更分類: B（SPEC同コミット更新）」、横展開表（6点セットの各箇所のコミット）、
  残リスク1〜3行。
