# 指示書 W1: キャンプ場土地の監視エリア拡大（設定・エリア定義・表示側）

発注: 2026-09-05 オーケストレータ／消込表 #472「沼津IC圏の監視拡大」
前提資料（必ず先に読む）: `CLAUDE.md`・`docs/SPEC.md` §1/§3/§4/§6/§9・`docs/DECISIONS.md` §0/§1・
`docs/research/20260905_camp_sites_candidates.md`（調査結果。URLと所要時間の出典・実測値はここ）・
`urls.yaml` の `filters:` と `tab: camp` の全エントリ・`watch.py` の `_MACHI_NAMES_SHIZUOKA`/`extract_machi`/`load_config`相当/
`inTabBucket`(JS)/`SITE_PREFIXES`(JS)/`RUN_WALLCLOCK_LIMIT`。

## 着手前4問（kaishu-policy。ワーカーはこの答えを前提に実装する）
1. **同じ知識を持つ場所**: 「キャンプ場土地の対象市町」は現在 ①各campサイトの `filter_keywords`（19市町のコピーが複数エントリに散在）②`watch.py` の `_MACHI_NAMES_SHIZUOKA`（市町判定・JSの市町フィルタ選択肢）の2箇所＋コピー。**本改修で `urls.yaml` の `filters.camp.tiers` に一本化**し、①は `"@camp_tiers"` 参照、②は起動時に `filters` から組み立てる。
2. **同種の既存機能**: `jmty_land_camp`＝「同一URLをcamp圏キーワードで再抽出」の型（家いちば静岡エリアで再利用）。`resort_estate_shizuoka`/`resortbukken_izu`/`parse_suumo`＝URL追加だけで別エリアが開くアダプタ（今回の主力）。
3. **引っ越しか**: いいえ（追加のみ。既存の更地/家付き/賃貸タブの挙動は変えない）。
4. **過去の類似改修**: 2026-07-12 バッチ4（SUUMO camp圏8エントリ追加・`_MACHI_NAMES` 拡張。`docs/WORKLOG.md`）／2026-08-31 サイト名短縮表の衝突（`docs/BUGLOG.md`）／`docs/DECISIONS.md` §1「Tier方式」。

## 実装内容
### A. urls.yaml（URLは調査結果ファイルから**一字一句コピー**。記憶で書かない）
A1. `filters.camp` に `tiers:` を追加（正本。コメントに「沼津IC起点・NAVITIME深夜実測・役所までの時間。出典 docs/research/…」）:
```yaml
    tiers:
      tier1:   # ≤60分
        [沼津市, 三島市, 函南町, 長泉町, 清水町, 裾野市, 御殿場市, 小山町, 伊豆の国市, 伊豆市, 熱海市,
         富士市, 富士宮市, 箱根町, 湯河原町, 真鶴町, 静岡市清水区, 静岡市葵区, 静岡市駿河区, 焼津市,
         小田原市, 南足柄市, 秦野市, 山北町, 松田町, 開成町, 大井町, 中井町,
         山中湖村, 忍野村, 富士吉田市, 富士河口湖町, 鳴沢村, 西桂町]
      tier2:   # 60〜90分
        [藤枝市, 伊東市, 河津町, 身延町, 南部町, 富士川町, 下田市, 東伊豆町, 西伊豆町, 松崎町]
      tobichi: # 90分超だが指定で残す（ネームバリュー）
        [南伊豆町]
```
   （伊東市は実測68分でtier2。下田は深夜85分だが県資料は整備前150分＝tier2に置きつつ「実勢は2時間超もありうる」をコメントに残す）
A2. **SUUMO 土地 13エントリ**を `tab: camp`・`channel: "①"` で追加（id は `suumo_camp_<slug>`。URLは調査結果 1-4 の表から）。
   `filter_keywords` は郡単位で圏外の町を含むものだけ: 南都留郡→`[富士河口湖, 鳴沢, 忍野, 山中湖, 西桂]`、南巨摩郡→`[身延, 南部町, 富士川町]`。
   足柄上郡・足柄下郡は全町が圏内なので `"@camp_tiers"`。市単位URLは省略可（既存campのSUUMOに合わせる）。
A3. 既存パーサ流用の新エントリ（すべて `tab: camp`）:
   - `resort_estate_yamanashi` … `https://resort-estate.com/select/mode:yamanashi`（channel ⑦・`filter_keywords: "@camp_tiers"`）
   - `resortbukken_fuji` … `https://resort-bukken.com/fuji`、`resortbukken_kawaguchiko` … `https://resort-bukken.com/kawaguchiko`（同上）
   - `ieichiba_shizuoka_camp` … `https://www.ieichiba.com/area/shizuoka`（既存 `ieichiba_shizuoka` と同一URL。channel ④・`"@camp_tiers"`。noteに「jmty_land_camp と同じ再抽出方式」）
   追加前に各URLを `Invoke-WebRequest -UseBasicParsing` で再実測し、`note:` に「2026-09-05 実測 HTTP 200・N件」を書く（(b)は調査結果ファイルで済んでいる旨も書く）。
A4. 既存campエントリの `filter_keywords` で、旧Tier一覧（函南〜南伊豆の19市町）を丸写ししているものは `"@camp_tiers"` に置き換える（意図的に狭いものは残し、理由をnoteに）。

### B. watch.py
B1. `filter_keywords: "@camp_tiers"` を展開する処理（tier1+tier2+tobichi の市町名。**町名の短縮形も一緒に入れる**: 「富士河口湖町」→「富士河口湖」等、既存の短縮慣習に合わせる。展開は設定読込直後の1箇所で行う）。
B2. `_MACHI_NAMES` を `urls.yaml` から組み立てる: **先頭は従来の7市町（順序を変えない＝更地/家付き/賃貸の判定を変えない）**、続けて `filters.camp.tiers` の全市町。`CONFIG.machi`（JSの市町フィルタ選択肢）も同じ配列。「静岡市清水区」と「清水町」、「南部町」と本文中の「南部」の誤マッチに注意（**必ず正式名で照合**）。
B3. JS `inTabBucket`: `tab==='camp'` のとき、`d.tab==='camp'` に加えて **`d.tab==='home' かつ 面積 ≥ filters.camp.area_min_sqm（1,000㎡）のレコードも含める**（沼津・三島・函南等のSUUMO/LIFULL既存監視の大面積土地をキャンプ場土地タブでも見せる。再クロールせずに網羅する）。`CONFIG` に `filters.camp.area_min_sqm` が無ければ埋め込む。★/非表示/除外エリア/NGログ/消滅の各集計は現行のタブ判定関数を通しているはずなので、同じ関数を直すだけで揃うことを確認する（別の判定を書かない）。
B4. `RUN_WALLCLOCK_LIMIT` 1800→**3300**（55分）、`.github/workflows/daily.yml` の `timeout-minutes` 40→**70**。根拠: 直近6回のActionsが全て約30分30秒で終了＝上限到達の疑い。**直近runのログで「実行ウォールクロック上限」警告の有無と打ち切りサイト数を実測し、報告に書く**（`gh run view <id> --log`）。
B5. JS `SITE_PREFIXES` に新サイトの短縮名を追加（`日本マウント`・`いなかも`・`家いちば` は既存があれば流用）。追加後、urls.yaml 全 `name:` で短縮名の衝突が無いことを既存の方法で機械確認（BUGLOG 2026-08-31 の型）。

### C. 検証（完了条件）
- `py watch.py --only=suumo_camp_fujinomiya --dry-run` と `--only=resort_estate_yamanashi --dry-run`、`--only=ieichiba_shizuoka_camp --dry-run` が完走し、抽出件数と `machi` の判定例（富士宮市/富士河口湖町 等）をログで示す
- `py watch.py --rebuild` → `reports/_preview.html` の埋め込みDATAで、campタブ表示件数が B3 によって増えること（増分件数）、更地/家付き/賃貸の件数が**変わらない**ことを数値で示す
- `python -c "import ast;ast.parse(open('watch.py',encoding='utf-8').read())"` OK、生成HTMLのJSがブラウザでエラー0（`--rebuild` 結果を開いて確認）
- `git status --short reports SOURCES.md` が空（本番成果物に触れていない）

### D. 文書（同じコミットで）
- `docs/SPEC.md`: §3.5「監視エリア（キャンプ場土地）」を新設（tier表＝調査結果 1-2 を市町・分・Tierの3列に圧縮、注意書き「役所までの深夜実測」、出典パス）。§3 のサイト数表を更新。§4 に EARS 1文「更地/家付き由来のレコードでも面積が1,000㎡以上のとき、システムはキャンプ場土地タブにも表示すること」。§6 に同旨1行。§8 に上限55分。
- `docs/DECISIONS.md` §1 末尾に 2026-09-05 の行を3つ（tiers正本化／home大面積のcamp合流／ウォールクロック55分）。§4 に「伊東・伊豆高原の別荘地系6社＝tier2かつ管理別荘地で見送り」「0円系3サイト＝zero.estate未満で却下」を追記。
- `docs/WORKLOG.md` に1行。
- `docs/BUGLOG.md`: 上限到達で打ち切りが**実際に起きていた**場合のみ1行（症状=毎回30分で打ち切られ末尾サイトが更新されない／原因／commit／ヨコテン/グローバル欄）。

## 規律
- 作業単位（A→B→D）ごとにコミット。**pushはしない**（オーケストレータが行う。ハッシュを文書に書くのはpush後）。
- 既存の更地/家付き/賃貸タブの判定・表示を変えない。URLは調査結果からコピー。分からないことは推測せず報告に「未確認」と書く。
- 再委任禁止。完了報告: 追加エントリ一覧（id/URL/実測HTTP/件数）・B3の増分・B4の実測・衝突検査結果・残リスク。
