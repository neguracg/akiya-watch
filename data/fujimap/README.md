# 富士山可視度データ（fujimap連携）

`oaza_scores.json` は兄弟プロジェクト **fujimap**（`C:\Claude\31_fujimap`）が生成する、
大字（町丁・字等）単位の富士山可視度データ。akiya-watch の `fujiview.py` がこれを読み、
キャンプ場土地タブの「富士山」列に表示する（指示書 `docs/tasks/20260919_fujimap_link.md`）。

## 出所
- 生成元: `C:\Claude\31_fujimap\docs\oaza_scores.json`
- 生成日時: このフォルダの `oaza_scores.json` を開き、トップレベルの `"generated"` を見る
  （ISO日時。akiya-watch側では更新日を別管理しない＝データ自体が唯一の記録）。
- 中身: 国土地理院DEMベースの視線判定（200mメッシュ）を、e-Stat小地域境界で大字単位に
  集計した `{pref, city, oaza, lat, lon, n, min, p10, p50, p90, max, method}` の配列。
  `city` は郡名なし（akiya-watchの`machi`と同じ表記）、`oaza`は「大字」接頭・「丁目」を
  除いた正規化名。詳しい仕様は指示書、または fujimap 側の `analyze/oaza_scores.py` を参照。

## 更新手順（fujimap側でデータを作り直した後）
1. `C:\Claude\31_fujimap` で `py -3 analyze/viewshed.py`（DEMから200mメッシュの視線判定）
2. 続けて `py -3 analyze/oaza_scores.py`（メッシュを大字単位に集計し `docs/oaza_scores.json` を出力）
3. 生成された `C:\Claude\31_fujimap\docs\oaza_scores.json` をこのフォルダへ上書きコピー
   （akiya-watch側: `data/fujimap/oaza_scores.json`）
4. akiya-watch 側で `py watch.py --rebuild` → `reports/_preview.html` のキャンプ場土地タブで
   「富士山」列の値が更新されたことを確認してからコミットする

## 出典表記（利用規約）
- 標高: 国土地理院 標高タイル（DEM10B等）。出典: 国土地理院
- 行政界: 政府統計の総合窓口(e-Stat) 国勢調査2020 小地域境界データ

## 注意
- 可視度は**地形のみ**の機械判定（樹木・建物は非考慮）。0=見えない、100=麓近くまで見通せる。
- 大字単位の集計のため、同じ大字内でも実際の可視性は地点ごとにばらつく
  （`p10`〜`p90`のレンジ表示・`max`は大字内最良地点）。**最終確認は現地／目視**。
- このファイルが無い状態でも akiya-watch は正常動作する（`fujiview.py`が起動時に1行だけ
  注意を出し、「富士山」列は「—」のまま表示される＝任意機能）。
- 数字の意味・読み方・計算方法の説明文の正本は fujimap `docs/score.html`。akiya-watch側
  （キャンプ場土地タブの「富士山の見え度合いとは」details・`fujiview.help_html()`）は要約のみを
  持ち、詳細はリンク（`DOC_BASE_URL`未設定時はこのフォルダの案内テキスト）で渡す
  （同じ説明を2箇所に書かない。指示書 `docs/tasks/20260920_fuji_help_link.md`）。
