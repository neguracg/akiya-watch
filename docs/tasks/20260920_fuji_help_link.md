# 指示書: 「富士山」列の見方＋fujimapマップへのリンクを一覧に載せる（2026-09-20）

## 目的
昨日追加した「富士山」列（キャンプ場土地タブ）について、腱さんが一覧の中で
「この数字は何で、どう読むのか」を確認でき、fujimap のマップ本体と詳しい説明へ飛べるようにする。

## カイシュウ5問（着手前・答え済み）
1. **同じ知識の置き場**: 富士山可視度の「意味・読み方」の正本は fujimap の `docs/score.html`（2026-09-20 作成済み）。
   akiya-watch 側は**要約と読み方だけ**を持ち、詳細はリンクで渡す（同じ説明を2箇所に書かない）。
   表示ラベルの規則（0／1値／レンジ）は既に `fujiview.summarize()` が正本。ここに説明文を足すのも `fujiview.py`。
2. **同種の既存機能**: 既存の「参考情報（バッジの見方）」details ブロック（`build_html_report` 内・`H.append` で組む）。
   タブ別の出し分けは既存の `body[data-tab=camp]` CSS フックを使う（JS 分岐を新設しない）。
3. **引っ越しではない**（新しい説明ブロックの追加）。
4. **過去の類似改修**: `docs/WORKLOG.md` 2026-09-19〜20 の「fujimap連携A〜E」。
   **重要**: そのとき `watch.py` が1000行ラチェット（既存超過ファイルは行数増加を拒否）に当たり、
   ワーカーが新規コードを既存行へ詰め込んで可読性を落とした（残リスクとして報告済み）。
   **今回は同じ逃げ方をしない**。新しい行は `fujiview.py`（超過していない新モジュール）に置き、
   `watch.py` 側は既存行の文字列を書き換える／`H.append(...)` 1行を足すだけに収める。
   それでもラチェットに弾かれる場合は**詰め込まず、その旨を報告して止まる**。
5. **所有**: 富士山可視度に関する説明文・リンク先URLは `fujiview.py` が単独所有（`# @owns` に追記）。

## 実装

### A. `fujiview.py`（既存・所有者）
1. モジュール定数を1つ追加:
   ```python
   # マップ本体と詳しい説明（fujimap docs/index.html・score.html）の公開先URL。
   # 置き場が未決のため既定は None（未設定）。決まったらこの1行だけ書き換える。
   # None のときは一覧にはリンクを出さず「PCのfujimapで見る」案内だけを出す。
   DOC_BASE_URL = None
   ```
2. `help_html() -> str` を追加。**HTMLエスケープ済みの完成した文字列**を返す（呼び出し側は素通しで `H.append`）。
   内容（この粒度・この順で。文章は下の文言をそのまま使ってよい）:
   - 見出し行: `富士山（キャンプ場土地タブの列）`
   - 1行説明: 「富士山の**どこまで**見えるかを0〜100で表した数字。100＝麓の方まで見える、30＝頭だけ、0＝山頂も地形に隠れる。」
   - 表示の4通り: `0`＝その大字はどこも見えない／`78`＝ばらつきが小さくだいたい78／`10〜30`＝下位10%〜上位90%のレンジ／`—`＝住所から大字を特定できなかった
   - 「セルにマウスを乗せる（スマホは長押し）と 大字名・最小/中央/最大・メッシュ数が出る。**一番良い地点は「最大」を見る**。」
   - 「並べ替え・絞り込みはレンジの**右側の数字**で効く（一部でも良く見える大字を残すため）。」
   - 注意1行: 「**地形だけ**の判定。樹木・建物は見ていないので、木を伐れば見える場所も『見えない』と出る。」
   - 注意1行: 「大字単位なので、広い大字ほどレンジは広い。レンジが同じでも中央値は違う（例: 函南町の平井と丹那はどちらも `0〜82` だが中央値は72と33）。」
   - 末尾: `DOC_BASE_URL` があれば
     `<a href="{DOC_BASE_URL}/index.html" target="_blank" rel="noopener">富士山が見える場所マップ（地図）</a>` と
     `<a href="{DOC_BASE_URL}/score.html" target="_blank" rel="noopener">点数の考え方（詳しい説明）</a>` の2本。
     None なら `PCの C:\Claude\31_fujimap\docs\index.html ／ score.html（fujimapフォルダ）` という案内テキスト。
   - ラッパは既存 details と同じ見た目に揃える: `<details class='...'><summary>富士山の見え度合いとは（0〜100）</summary>…</details>`
     （既存の「参考情報（バッジの見方）」details のクラス名・マークアップを実物で確認して合わせる）。
   - **camp タブ以外では出さない**: ラッパに `class='fujihelp'` を付け、CSS で既定 `display:none`、
     `body[data-tab=camp] .fujihelp{display:block}` にする（CSS は `watch.py` の既存 CSS 文字列へ2行。
     JS 側の分岐は作らない）。
3. `summarize()` のdocstring相互参照に score.html の存在を1行足す（説明の正本がどこかを迷わせない）。

### B. `watch.py`
- 「参考情報（バッジの見方）」details を組んでいる直後に `H.append(fujiview.help_html())` を1行追加。
- CSS に `.fujihelp{display:none}` と `body[data-tab=camp] .fujihelp{display:block}` を追加（既存CSS文字列内）。
- `legendRow` の camp 用 span（現在「富士山: 大字ごとの可視度0〜100（数字2つはレンジ・地形のみの機械判定）」）の
  **既存行を書き換えて**、末尾に「見方↑」等の短い案内を足す（details へ誘導。行数は増やさない）。
  リンクにしなくてよい（details は同じページ内にあるため文言だけで足りる）。

### C. 文書（B変更＝同じコミットで）
- `docs/SPEC.md` §6: 参考情報セクションに「富士山の見え度合いとは」details（campタブのみ）が増えたことを1〜2行。
  説明の正本が fujimap `docs/score.html` であることも明記。
- `docs/README.md` の文書の地図に1行（富士山可視度の説明の正本＝fujimap側）。
- `docs/WORKLOG.md` に1行。
- `data/fujimap/README.md` に「説明の正本は fujimap `docs/score.html`」を1行追記。

### D. テスト
- `tests/test_fujiview.py` に3ケース追加:
  - `help_html()` が `DOC_BASE_URL=None` のとき `<a href` を含まず、案内テキストを含む
  - `DOC_BASE_URL` を monkeypatch で設定したとき `index.html`・`score.html` への `<a href` を2本含む
  - `help_html()` が `fujihelp` クラスを含む（campタブ限定の出し分けフックが消えていないことの回帰）
- 既存53〜55件と合わせて全緑。

## 完了条件
- `python -m pytest tests -q` 全緑（件数を報告）。`watch.py`・`fujiview.py` の `ast.parse` OK。
- `py watch.py --rebuild` 完走、`reports/_preview.html` で:
  - キャンプ場土地タブに details「富士山の見え度合いとは（0〜100）」が出る
  - 更地・家付き土地・賃貸タブでは出ない（DOM/CSSで実測）
  - コンソールエラー0
  - 1280px と 375px の両方で確認（375pxで details を開いて文字が溢れないこと）
- **本番 `reports/index.html` 等を書き換えないこと**（`git status` で確認して報告）。
- 作業単位ごとにコミット（自分が触ったファイルだけ明示add・`git add -A` 禁止）。push はしない。
- `spawn_task` を呼ばない。別件は完了報告に書く。
- 完了報告に: pytest件数、スクリーンショット確認結果、ラチェットに当たったか（当たった場合どう回避したか）、
  カイシュウ5問: 済、変更分類 B、横展開表（fujiview/watch.py CSS/legendRow/SPEC/README/WORKLOG/data README/テスト）、残リスク1〜3行。
