# 指示書 W5: LIFULL 土地一覧の面積が 89.5% 欠落する件（消込表 #480）の修正

発注: 2026-09-05 オーケストレータ。**git worktree 上で作業**（並走する W4 が本体の watch.py を編集中。あなたは自分の worktree ルートに書く。`git rev-parse --show-toplevel` で確認）。
根拠: 消込表 #480（`python C:\Claude\72_DevSupport\cli.py show 480`）、`docs/tasks/20260905_sup_audit_result_A.md`、`watch.py` の `_lifull_card_specs`／`_extract_lifull_cards`／`parse_lifull`／`_lifull_rent_*`、`data/snapshots/lifull_*.json`、`docs/BUGLOG.md`（2026-06-22「LIFULL中古戸建の価格/面積抽出はth/td数が一致する整列テーブルのみ採用」の経緯＝**今回の原因を作った過去の修正**。`docs/DECISIONS.md` §2 も参照）。

## 症状（監査セッションの実測）
LIFULL 土地7サイト 210件中 188件（89.5%）で `area_sqm` が None。中古（`lifull_chuko_*`）・賃貸（`lifull_rent_*`）は0件で健全。価格は `.priceLabel` フォールバックがあるため0件。

## 原因（同・実HTMLで実測済み）
`_lifull_card_specs` が「th数 == td数 の整列テーブルだけ specs 採用」で、土地カードは画像・お気に入り・詳細列が td 化されて th9/td10 等の不一致が30件中27件。不一致だと specs={} → `土地面積` が空 → None。コードのコメント「中古戸建でのみ起きる」は前提が古い。

## 着手前4問
1. 同じ知識を持つ場所: LIFULL のカード→specs 変換は `_lifull_card_specs` の1箇所（rent は別関数か確認して報告）。面積の正規化は `_first_sqm`/`parse_area_sqm`。
2. 同種の既存機能: 価格側の `.priceLabel` 直接取得フォールバック＝**同じ型で面積も「th/td 不一致時は面積要素を直接取る」フォールバックを付ける**。
3. 引っ越しか: いいえ。
4. 過去の類似: 2026-06-22 の「整列テーブルのみ採用」修正（BUGLOG/WORKLOG/git log --grep=LIFULL）。**その時の判断理由（先頭の画像/要約セルで位置がずれ誤値になる）を殺さない**＝整列一致時は従来どおり、不一致時だけフォールバック。誤値（別の列の数字を面積と誤認）を出さないこと。

## 実装
- `_lifull_card_specs` を「①th/td 一致→従来 ②不一致→th のラベル文字列（`土地面積`/`建物面積`/`価格` 等）を基準に、**同じ行の隣接 td** か **ラベル付き要素（dt/dd・`[class*=area]` 等、実HTMLで確認した確実なもの）**から取る」に拡張。実HTML（`--only=lifull_mishima --dry-run` の fetch 結果、または `data/snapshots` の URL を `watch.fetch` で再取得）で**どの要素に面積が入っているかを先に実測**してから書く。推測で書かない。
- 誤値防止: 面積として採用する値は `㎡`/`m2`/`坪` の単位付き文字列からのみ（`_numeric_cell_text` 経由）。単位が無い数字は採用しない。
- ヨコテン: 同じ「整列一致のみ」判定を使う箇所を `grep -n "len(ths)\|th数\|整列" watch.py` 等で全数棚卸し（LIFULL 中古・賃貸・他アダプタ）。同型があれば同じフォールバックを入れるか、非該当の理由を報告。
- 単体テスト: `tests/test_watch_units.py` に、実HTMLから切り出した**th9/td10 の土地カード断片**（個人情報なし・所在地は市町まで）で `area_sqm` が取れるケース＋従来の整列一致ケースが壊れないケース。
- 検証: 7サイトすべて `py watch.py --only=lifull_<site> --dry-run`（preview出口）で `area` 取得率を前後比較（before 10.5% → after の実測値）。中古・賃貸の取得率が**下がらない**こと。誤値チェック: 取れた面積の分布（最小/最大/中央値）を出し、330㎡未満や1万㎡超が急増していないか確認。pytest 全件 green。
- 文書: `docs/BUGLOG.md` 1行（症状/原因/commit: (push後に追記)/@jissou ヨコテン:済N件/恒久化:テスト名 グローバル:不要 or 済の判定）。`docs/SPEC.md` §9 の LIFULL 行に「不一致時フォールバック」を1語。`docs/WORKLOG.md` 1行。消込表 #480 に note（実測の前後比較・commit）。

## 規律
- worktree 内で `git add <明示ファイル>` → コミット（pushしない。本体へのマージはオーケストレータ）。WORKLOG/BUGLOG は末尾追記のみ（マージ競合を最小に）。
- 再委任禁止。完了報告: 前後の取得率（7サイト表）・誤値チェックの分布・ヨコテン表・テスト結果・worktree のパスとブランチ名・残リスク。
