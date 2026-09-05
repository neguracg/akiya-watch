# `<sup>`分断バグ横展開監査 結果（W=A）

指示書: `docs/tasks/20260905_sup_audit.md`。対象13アダプタ: parse_suumo, parse_takken,
parse_sumaimy_rent, parse_lifull, parse_ieichiba, parse_mano, parse_fudosoken,
parse_izu_sougou, parse_snjhkk, parse_u2, parse_yamaichiba, parse_sanrinbank,
parse_resort_estate。

監査環境: watch.py 5938行時点（`git diff --stat 02c2bc1 HEAD -- watch.py urls.yaml` で無差分を
確認済み＝監査中に他ワーカー/Actionsが行った並行コミットの影響なし）。取得HTML・検証スクリプトは
`C:\Users\MNTNS_~1\AppData\Local\Temp\claude\C--Claude-30-akiya-watch\7c45d4ef-96c7-4fc1-913f-b27981e265ec\scratchpad\sup_audit\A\`。
User-Agent はwatch.py HEADERS と同一（`Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36
(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36`）。各サイトfetchは1回のみ（IWR1回＋
python watch.fetch 1回＝2リクエスト、パラレル/リトライ無し）。

---

### parse_suumo（代表: suumo_izunokuni／URL: https://suumo.jp/tochi/shizuoka/sc_izunokuni/）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: B（`_extract_suumo_cards` L801-807: `v = dd.get_text(strip=True)` ※区切りなし
  （コメントL806「区切りなし＝『224m2』を分離させない」で明示済み）→ `fields["土地面積"]` →
  `_suumo_land_sqm(dd_text)` L766-777 が正規表現で直接抽出）
- 価格経路: B（同じ `fields` dict。L817-821 `price = parse_price_man(v)`。v は同じ
  `get_text(strip=True)`＝区切りなし）
- 面積要素の innerHTML 例:
  - `<dd>172.11m<sup>2</sup>・194.35m<sup>2</sup>（登記）</dd>`
  - `<dd>201.83m<sup>2</sup>（登記）</dd>`
  - `<dd>311m<sup>2</sup>（94.07坪）（登記）</dd>`
- 分断タグ: **あり（sup）**。ただしコードが区切りなし get_text のため実害なし。
- 実データ検証: 全20件中 area_sqm None 0件／price_man None 0件。サンプル3件:
  (1600万,172.1㎡), (1400万,201.8㎡), (150万,311.0㎡)。get_text(strip=True) により
  "172.11m<sup>2</sup>" は "172.11m2" として連結され `_suumo_land_sqm` の `m2` パターンに
  正しく一致（1件目は同一セルに2値併記「172.11m2・194.35m2」だが `_SQM_ONLY_RE.search`
  は最初の一致=172.11を採用。仕様上の挙動でsup分断とは無関係）。
- 判定: **非該当**（`<sup>`は使われているが、区切りなしget_textのため分断されず安全）

### parse_takken（代表: takken_mishima／URL: https://akiya-bank.shizuoka.fudohsan.jp/一覧/買う-定住タイプ/地域/三島市/）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: B（`_extract_takken_cards` L965-967: `area_el.get_text(strip=True)` ※区切りなし
  → `parse_area_sqm`）
- 価格経路: B（L963: `price_el.get_text(strip=True)` ※区切りなし → `parse_price_man`。
  price_el は `.price`（`<p>`全体）だが子要素の連結も区切りなしのため安全）
- 面積要素の innerHTML 例:
  `<p class="area"><span class="num01">3</span><span class="num02">SLDK</span><br/>
  <span class="num03">建物面積87.35m²/ （公簿）土地面積121.15m²</span></p>`
  （`m²`はUnicode文字1文字としてテキストノードに直接書かれており、`<sup>`等での分断なし）
- 分断タグ: **なし**（`m²`は単一Unicode文字。suumoと異なりsupタグを使わない実装）
- 実データ検証: 全10件中 area_sqm None 0件／price_man None 0件。サンプル3件:
  (3380万,121.2㎡), (780万,847.2㎡), (4280万,156.0㎡)。
- 判定: **非該当**（分断タグ自体が存在しないため該当なし）

### parse_sumaimy_rent（代表: sumaimy_rent_mishima／URL: https://www.shizuoka.fudohsan.jp/一覧/借りる/地域/三島市/）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: A（`_extract_sumaimy_rent_cards` L2677-2678: `n3.get_text(" ", strip=True)`
  ※区切りあり → `_first_sqm`。ただし `.num03` は子要素を持たない葉ノードのため区切り文字は
  実際には挿入されない＝機械的にはA、実害の有無は実データで確認要）
- 価格経路: B（L2669: `n1.get_text(strip=True) + n2.get_text(strip=True)` を`+`で連結。
  各要素は区切りなしで個別取得後に結合するため安全。`parse_rent_man`使用＝賃貸タブなので
  `parse_price_man`ではない）
- 面積要素の innerHTML 例（`p.area .num03`）:
  - `<span class="num03"></span>`（面積情報なし物件）
  - `<span class="num03">専有面積66m²</span>`
  - `<span class="num03">（公簿）土地面積495m²</span>`
  - `<span class="num03">建物(専有)面積127.51m²</span>`
- 分断タグ: **なし**（`m²`はUnicode文字1つで`.num03`直下にテキストノードのみ。子要素なし
  ＝区切り文字が挿入される箇所自体が存在しない）
- 実データ検証: 全10件中 area_sqm None **2件**（うち2件とも `.num03` が空文字列＝該当
  物件に面積情報が無い駐車場等。区切りあり/なしで比較しても両方 None で **差なし**）。
  面積が入っている8件は区切りあり/なしで値が完全一致（例: 66.0/66.0, 495.0/495.0,
  29.8/29.8, 127.5/127.5）。price_man None 0件。
- 判定: **非該当**（コード形状はA相当だが対象要素が子要素を持たないため分断が発生せず、
  現行値と修正後値に差がない。Noneの2件は面積情報自体が原データに無いだけ）

### parse_lifull（代表: lifull_mishima／URL: https://www.homes.co.jp/tochi/shizuoka/mishima-city/list/）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: A（`_lifull_card_specs` L1158: `tds = [x.get_text(" ", strip=True) ...]`
  ※区切りあり → `specs["土地面積"]`/`specs["建物面積"]` → `_first_sqm` L1175）
  ※本指示書の書式例と同一行番号（本アダプタが実例のたたき台になっている）
- 価格経路: A（L1169: `specs.get("価格", "")` は同じ tds 由来。フォールバック L1173:
  `pl.get_text(" ", strip=True)`（`.priceLabel`）も区切りあり）
- 面積要素の innerHTML 例（`td`要素そのもの、子タグ無し）:
  - `<td>\n  ...空白...  136.52m²  </td>`（children tags: []）
  - `<td>\n  ...空白...  173.21m²〜173.32m²  </td>`
  - 価格td: `<td colspan="3"><span class="priceLabel"><span class="num">3,220万円</span>
    </span></td>`（children: [span] のみ、num spanの中に金額+単位が1つのテキストノード）
- 分断タグ: **なし**（ページ全体を`<sup`/`<sub`でgrep→0件。`㎡`は常にUnicode文字1つとして
  td/span直下のテキストノードに書かれ、区切り文字が入る子要素構造自体が存在しない）
- 実データ検証: 全30件中 area_sqm None **27件**／price_man None 27件（同じ27件）。
  区切りあり(現行)/区切りなし(修正後)を比較しても **完全に同値**（例: 136.52m²→136.5/136.5,
  173.21m²〜173.32m²→173.2/173.2）＝分断由来の差は無い。
  **原因は別バグ**: `_lifull_card_specs`が「価格テーブルのth数=td数」の時だけ採用する
  実装のため、価格テーブルに画像/お気に入り/詳細列が追加でtd化されている30件中27件
  （th9/td10等）で specs={} となり area/price 双方 None になっている（該当th/td一致=3件
  のみ）。コード中のコメント（L1151-1153）は「中古戸建(kodate)のみで起きる」と書いて
  いるが、実測では**土地(tochi)の三島市ページでも同じth/td不一致が90%発生**しており、
  コメントの前提が古い可能性がある。**本監査のsup分断とは別原因のため対象外**（報告のみ・
  修正はしない。次のいずれかの改修候補: ①th/td不一致時のフォールバック処理の追加 ②実際の
  カード構造の再調査）。
- 判定: **非該当**（sup分断は無し。ただし上記の別バグでNone率が高いことを別途報告）

### parse_ieichiba（代表: ieichiba_shizuoka／URL: https://www.ieichiba.com/area/shizuoka）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: **X**（`_extract_ieichiba_cards` L1231-1233: 一覧に土地面積が無いため
  `area=None`をハードコードして`_make_record`に渡している。`_first_sqm`/`parse_area_sqm`
  自体を呼んでいない＝面積抽出の対象外）
- 価格経路: B（L1221-1222: `pe.get_text(strip=True)` ※区切りなし → `parse_price_man`）
- 面積要素の innerHTML 例: 該当なし（一覧に面積列が無い。urls.yaml 側の既存noteと一致）
- 価格要素の innerHTML 例: `<div class="property__list-item-price" data-v-...>1,500万円</div>`
  （子タグなし・単一テキストノード）
- 分断タグ: なし（価格要素に子タグ無し。面積は非対象）
- 実データ検証: 対象7市町キーワードで10件中1件が絞り込み通過。price_man None 0件。
  area_sqm は仕様どおり常にNone（1件中1件）。
- 判定: **非該当**（面積は元々未抽出=X。価格は分断タグ自体が存在しない）

### parse_mano（代表: mano_shizuoka／URL: https://manokaihatsu.com/estate/）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: A（`_mano_card_specs` L1264: `tds = [td.get_text(" ", strip=True) ...]`
  ※区切りあり → `specs["土地面積"]` → `_first_sqm` L1280）
- 価格経路: A（L1277: `pe.get_text(" ", strip=True)` ※区切りあり → `parse_price_man`）
- 面積要素の innerHTML 例: `<td>\n  990.69㎡（299.68坪）㎡  </td>`（`㎡`はUnicode文字1つ、
  子タグ無し。末尾に`㎡`が重複表示される元データの癖があるが本監査とは無関係）
- 価格要素の innerHTML 例: `<p class="item-price">8,300<span class="is-unit">万円</span></p>`
  （**数値と単位が`<span class="is-unit">`で子要素分断されている**）
- 分断タグ: 面積側は**なし**（㎡がUnicode文字直書き）。価格側は**あり（span.is-unit）**
  だが `parse_price_man`の`_MAN_RE`は数値と「万」の間に`\s*`を許容する正規表現のため、
  区切り文字が入っても実害なし。ページ全体を`<sup`/`<sub`でgrep→0件（該当タグはspanのみ）。
- 実データ検証: 対象7市町キーワードで11件中4件が絞り込み通過。area_sqm None 0件／
  price_man None 0件。価格の区切りあり(現行)/なし(修正後)を全11件で比較→**完全一致**
  （例: "8,300 万円"→8300/8300）。サンプル3件: (1000万,166.8㎡), (1100万,339.3㎡),
  (3480万,190.0㎡)。
- 判定: **非該当**（面積は分断タグ無し。価格はspan分断ありだが正規表現の`\s*`許容により
  無害）

### parse_fudosoken（代表: fudosoken_shizuoka／URL: https://www.fudosansoken.jp/sp-allbukken/）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: A（`_fudosoken_area` L1317: `cell5.get_text("\n", strip=True)` ※区切り
  （"\n"）あり→行ごとに`_first_sqm` L1319 で判定。ただし区切り文字="\n"は数値と単位の
  分断用ではなく、**建物面積/間取り/土地面積の3行を区別するための意図的な設計**（`<br>`
  区切りの3行から逆順に最初に数値が取れた行=最終行=土地面積を採用する仕組み）
- 価格経路: B（L1333: `price_num.get_text(strip=True) + "万円"`。Python文字列連結で
  get_text自体は区切りなし。price_numは子タグ無しの葉要素）
- 面積要素の innerHTML 例（`.cell5`）:
  - `<td class="cell5">-<br/>-<br/>339.00㎡</td>`
  - `<td class="cell5">2LDK<br/>52.99㎡<br/>292.00㎡</td>`（建物面積52.99㎡と土地面積
    292.00㎡が併記、`<br>`のみで区切られ`<sup>`等の数値内分断タグは無し）
- 分断タグ: **なし**（ページ全体を`<sup`でgrep→0件。`㎡`はUnicode文字1つ。`<br>`による
  複数行の区切りはあるが、これは数値と単位の間の分断ではない）
- 実データ検証: 対象7市町キーワードで23件中22件が絞り込み通過。area_sqm None 0件／
  price_man None 0件。サンプル3件: (90万,339.0㎡), (110万,374.0㎡), (113万,292.0㎡)。
  **注意（誤って"修正"しないための記録）**: 本監査の比較手法どおり「区切り無し
  `get_text(strip=True)`」をセル全体に対して試すと、複数行セル（建物面積+土地面積併記）で
  行が連結されてしまい **誤った値になる**（例2: "2LDK52.99㎡292.00㎡" → `_first_sqm`は
  最初の一致=52.99を返し、正しい土地面積292.00を取り損なう。現行実装のcur=292.0が正解、
  "修正後"のfixed=53.0の方が誤り）。つまり本セルの区切り文字は「sup分断の原因」ではなく
  「複数行の意味区別」に使われている必須の仕組みであり、機械的に区切りを外す修正は
  **絶対に適用してはならない**（横展開の対象外・修正フェーズへの申し送り事項）。
- 判定: **非該当**（分断タグ無し。かつ区切り文字自体が別の正当な用途で必須）

### parse_izu_sougou（代表: izu_sougou_shizuoka／URL: https://izu-s-k.fudohsan.jp/menu/?bukken=jsearch&shu=1）
- IWR StatusCode: **実施せず**（robots制限のため未実施） ／ watch.fetch: **実施せず**
- robots判定: **`robots_allowed()` = False**（`izu-s-k.fudohsan.jp/robots.txt` を実測）。
  ```
  User-agent: *
  Disallow: /*?bukken=
  ```
  対象URLは `?bukken=jsearch&shu=1` を含むためこのワイルドカード規則に一致し禁止と
  判定される。urls.yaml L342 の既存note「robots.txt: 全3社とも物件ページへの取得禁止
  なし（WP管理画面のみDisallow）」は `/*?bukken=` ワイルドカード規則を見落としており
  **実態と食い違っている**（urls.yamlは編集不可のため報告のみ。noteの訂正は修正フェーズ
  または別途ユーザー判断へ）。
- 静的判定: B（`_izu_sougou_cards` L1373: `dp2.get_text(strip=True)` ※区切りなし →
  `_first_sqm`）
- 価格経路: B（L1379: `dd.get_text(strip=True)` ※区切りなし → `parse_price_man`）
- 面積要素の innerHTML 例: **実測不可**（robots制限のためHTML取得せず）
- 分断タグ: **実測不可（robots制限）**。ただしコード判定がB（区切りなしget_text）のため、
  仮に`<sup>`が使われていても分断されず安全な構造。
- 実データ検証: **実測不可**（robots制限のためfetchしていない）
- 判定: **実測不可（robots制限）**。コード判定はB＝分断の影響を受けない構造であり、
  修正フェーズでの対応は不要と推定される（未実測のため断定はしない）。

### parse_snjhkk（代表: snjhkk_mishima／URL: https://www.snjhkk.com/list/1-4/0-241/）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed。Shift_JIS→fetch()内でcp932
  補正済み）
- 静的判定: A（L1448: `tds = [td.get_text(" ", strip=True) ...]` ※区切りあり →
  `specs["土地面積"]` → `_first_sqm` L1453）
- 価格経路: A相当（L1450-1451: `pe.get_text(" ", strip=True)`（`span.list_kakaku`）優先、
  無ければ同じ tds 由来の `specs.get("価格")`）
- 面積要素の innerHTML 例: `<td>127.43㎡</td>`（`㎡`はUnicode文字1つ、子タグ無し）
- 価格要素の innerHTML 例: `<span class="list_kakaku">3,680万円</span>`（子タグ無し）
- 分断タグ: カード内には**なし**。ページ全体では`<sup>`が2箇所あるが、いずれも
  検索フォームの面積絞り込みプルダウン脇の単位表示「m<sup>2</sup>」（`<select>`直後の
  ラベル）であり、**物件カード（div.list_row_border）の外側**＝抽出対象外。
- 実データ検証: 三島市キーワードで該当1件（このURLは元々三島市専用ページのため
  filter_keywords一致は自明）。area_sqm None 0件／price_man None 0件。値:
  (3680万,127.4㎡)。区切りあり(現行)/なし(修正後)を比較→完全一致（面積127.4/127.4、
  価格3680/3680）。※本ページのカード数が1件のみ（urls.yaml note記載の「約3件」より
  少ない。在庫減少と推測・本監査の対象外）。
- 判定: **非該当**（カード内に分断タグ無し。ページ内のsupは検索フォームのみで無関係）

### parse_u2（代表: u2_land／URL: https://www.u2japan-mishima-k.com/land/）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: A（`_u2_cards` L1490-1492: `p.get_text(" ", strip=True)` ※区切りあり →
  `"㎡" in tx` 判定 → `_first_sqm`）
- 価格経路: A（L1487: `pe.get_text(" ", strip=True)` ※区切りあり → `parse_price_man`）
- 面積要素の innerHTML 例: `<p class="result-list__text"> - / 182.00㎡ / - </p>`
  （`㎡`はUnicode文字1つ、子タグ無し）
- 価格要素の innerHTML 例: `<p class="result-list__price"><span class="price">2,280</span>
  万円</p>`（数値がspan、続く「万円」はpタグ直下の兄弟テキスト。区切り文字が入っても
  `_MAN_RE`の`\s*`許容で無害）
- 分断タグ: **なし**（ページ全体を`<sup`/`<sub`でgrep→0件）
- 実データ検証: 対象7市町キーワードで30件中20件が絞り込み通過。area_sqm None 0件／
  price_man None 0件。区切りあり(現行)/なし(修正後)を全カードで比較→**完全一致**。
  サンプル3件: (2280万,182.0㎡), (3900万,846.6㎡), (185万,312.0㎡)。
- 判定: **非該当**（分断タグ自体が存在しない）

### parse_yamaichiba（代表: yamaichiba_shizuoka／URL: https://yamaichiba.com/category/sanrin-shizuoka/）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: **C**（面積: L1577-1578 `card_text = card.get_text(" ", strip=True)`
  ※カード全文（区切りあり）→ `_yamaichiba_sqm(card_text)` L1546-1553。ha優先で
  `_HA_RE`、無ければ `_first_sqm`。カード全文結合＝坪/㎡が別経路でも拾えるC類型）
- 価格経路: 一覧には価格が無く、対象エリア一致×販売中の物件のみ詳細ページを
  最大6件フェッチして取得（L1582-1597）。詳細ページも
  `dtext = body.get_text(" ", strip=True)`（区切りあり）→`parse_price_man(dtext[idx:idx+40])`
  で**同機構のA相当**だが、本監査は「1サイトfetch1回」の予算上**詳細ページは取得していない**
  （下記の実データ検証は一覧ページのみ・price常にNone＝本監査の制約によるものでバグではない）。
- 面積要素の innerHTML 例: `<p>価格 2,679,833 円　 公簿面積 2.85ha（約8...`（haはASCII2文字、
  タグでの分断なし）
- 分断タグ: **なし**（ページ全体を`<sup`/`<sub`でgrep→0件。全26記事中、"物件"を含み
  【済】=売却済みでないもの＝販売中は**1件のみ**、静岡県内在庫がほぼ売り切れという
  urls.yaml note「現在静岡在庫0」とは裏腹に熱海市の1件が新着していた＝運用上の気づき
  だが本監査の対象外）。
- 実データ検証: 販売中1件（静岡県熱海市伊豆山、tier1該当）。area_sqm: 現行28500.0／
  修正後(区切りなしget_text)28500.0＝**完全一致**（"2.85ha"のhaはタグ分断が無いため
  区切り有無で差が出ない）。price_manは本監査では詳細ページ未取得のため常にNone
  （検証対象外・バグではない）。
- 判定: **非該当**（面積: 分断タグ無し。価格: 実測不可＝詳細ページ未取得。コード形状は
  同型のA相当だが、ha表記・円表記ともにタグ分断が起きる書き方をこのサイトはしていない
  と推定。確度を上げるには詳細ページ1件の追加取得が必要＝修正フェーズでの再確認を推奨）

### parse_sanrinbank（代表: sanrinbank_zenkoku／URL: https://sanrinbank.jp/）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: 独自機構（`_first_sqm`/`parse_area_sqm`は不使用）。面積: L1633 NFKC正規化した
  `li.get_text(" ", strip=True)` ※区切りあり → 専用正規表現 `面\s*積\s*([\d,.]+)\s*坪`
  L1642（**坪のみ対応・㎡は非対応**）。コード形状としてはA相当（区切りあり）だが、
  正規表現自体が数値と単位の間に`\s*`を許容する作りのため、区切り文字が入っても
  マッチが壊れない。
- 価格経路: 同様に独自関数 `_sanrinbank_price`（L1616-1624）。`価\s*格\s*(\S{1,24})`
  L1644 で価格文字列を切り出し。`\S{1,24}`（非空白文字）は区切り文字が挿入されると
  そこで途切れる可能性があり、機構としてはA寄りだが、実データでは「価格」の直後に
  子タグが無いため影響なし（下記実測）。
- 面積要素の innerHTML 例: `<dd><span>面   積  </span>5800坪　実測</dd>`／
  `<dd><span>面   積  </span>1911㎡　　</dd>`（後者は㎡表記のため現行正規表現が
  そもそも非対応でNone。**sup分断とは無関係の別要因**＝坪専用パターンの仕様上の限界。
  修正フェーズの対象外として報告のみ）
- 分断タグ: **なし**（ページ全体を`<sup`/`<sub`でgrep→0件）
- 実データ検証: 全国在庫7件中、静岡近辺(camp_tiers)キーワード一致は**0件**（実際の
  `parse_sanrinbank`をこのHTMLで実行→cards=0。urls.yaml note「現在静岡在庫0」と一致・
  在庫入れ替わりなし）。キーワード一致に関係なく全7件で機構検証: 区切りあり(現行)/
  なし(修正後)を比較→面積・価格とも**全件完全一致**（例: 5800坪→19173.5/19173.5、
  220万→220/220。㎡表記の1件は現行/修正後とも同じくNone＝坪専用正規表現の仕様限界で
  分断とは無関係）。
- 判定: **非該当**（分断タグ無し。㎡非対応は別問題として報告のみ）

### parse_resort_estate（代表: resort_estate_shizuoka／URL: https://resort-estate.com/select/mode:shizuoka）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: **X**（`_resort_estate_cards` L1684: `_make_record(..., None, False, ...)`＝
  面積は常にハードコードNone。urls.yaml note「面積は一覧に無し」と一致。`_first_sqm`/
  `parse_area_sqm`自体を呼んでいない）
- 価格経路: A（L1677: `pe.get_text(" ", strip=True)`（`.price`）※区切りあり →
  `parse_price_man`）
- 価格要素の innerHTML 例: `<span class="price">480万円</span>`／`<span class="price">
  900万円</span>`／`<span class="price">商談中</span>`（子タグ無し、単一テキストノード）
- 分断タグ: **なし**（ページ全体を`<sup`/`<sub`でgrep→0件）
- 実データ検証: camp_tiersキーワードで22件中19件が絞り込み通過。area_sqm None
  19件/19件（仕様どおり常にNone）。price_man None 1件（「商談中」という非数値テキスト＝
  正しい仕様上の挙動、区切りあり/なし双方でNoneで一致）。それ以外18件は区切りあり(現行)/
  なし(修正後)で価格が完全一致（例: "480万円"→480/480）。サンプル3件: (900万,None),
  (None,None・商談中), (1470万,None)。
- 判定: **非該当**（面積は元々未抽出=X。価格は分断タグ自体が存在しない）

---

## まとめ表（W=A・13アダプタ）

| アダプタ | 代表sid | IWR | fetch | 静的判定 | 分断タグ | 判定 | 一言理由 |
|---|---|---|---|---|---|---|---|
| parse_suumo | suumo_izunokuni | 200 | 200 | B | **あり**(sup) | 非該当 | 区切りなしget_text(L806)で分断されず安全 |
| parse_takken | takken_mishima | 200 | 200 | B | なし | 非該当 | m²がUnicode文字直書き・子タグ無し |
| parse_sumaimy_rent | sumaimy_rent_mishima | 200 | 200 | A(面積) | なし | 非該当 | .num03が子要素を持たない葉ノード |
| parse_lifull | lifull_mishima | 200 | 200 | A(両方) | なし(0件) | 非該当※ | supは無いが**別バグ**でarea/price27/30件None（th/td不一致・報告のみ） |
| parse_ieichiba | ieichiba_shizuoka | 200 | 200 | X(面積)/B(価格) | なし | 非該当 | 面積は元々一覧に無く未抽出 |
| parse_mano | mano_shizuoka | 200 | 200 | A(両方) | 価格のみ**あり**(span) | 非該当 | \s*許容の正規表現で無害・全11件cur=fixed一致 |
| parse_fudosoken | fudosoken_shizuoka | 200 | 200 | A(面積) | なし | 非該当※ | "\n"は複数行区別が目的で必須。**誤って一律修正すると壊れる**（要申し送り） |
| parse_izu_sougou | izu_sougou_shizuoka | 未実施 | 未実施 | B | 実測不可 | **実測不可(robots)** | robots.txt `Disallow: /*?bukken=` に一致（urls.yaml note要修正） |
| parse_snjhkk | snjhkk_mishima | 200 | 200 | A(両方) | カード内なし(ページ内supは検索フォームのみ) | 非該当 | 唯一の該当カードでcur=fixed一致 |
| parse_u2 | u2_land | 200 | 200 | A(両方) | なし | 非該当 | ㎡がUnicode文字直書き・全20件cur=fixed一致 |
| parse_yamaichiba | yamaichiba_shizuoka | 200 | 200 | C(面積)/A相当(価格) | なし | 非該当 | ha表記に分断タグ無し。価格は詳細ページ未取得で実測不可 |
| parse_sanrinbank | sanrinbank_zenkoku | 200 | 200 | 独自(坪限定) | なし | 非該当 | \s*許容の正規表現。㎡非対応は別問題（報告のみ） |
| parse_resort_estate | resort_estate_shizuoka | 200 | 200 | X(面積)/A(価格) | なし | 非該当 | 面積は元々未抽出。価格は分断タグ無し |

**該当（バグ確認）: 0件 ／ 非該当: 12件 ／ 実測不可: 1件（izu_sougou・robots制限）**

### 修正フェーズへの申し送り（重要）
1. **parse_fudosoken の `_fudosoken_area`（cell5.get_text("\n", strip=True)）を安易に
   `_numeric_cell_text` へ置換しないこと**。この"\n"区切りは建物面積/間取り/土地面積の
   3行を区別するために必須で、区切りを外すと複数値セルで誤った値（建物面積など）を
   拾ってしまう（本結果ファイルの実測例参照）。
2. **parse_lifull で area_sqm/price_man が27/30件Noneになる別バグを発見**（`_lifull_card_specs`
   のth数=td数一致要求が、画像/お気に入り/詳細列を持つ価格テーブルで破綻）。sup分断とは
   無関係の独立した問題。本監査の対象外につき修正はしていない。**別途チケット化を推奨**。
3. **parse_izu_sougou は robots.txt (`Disallow: /*?bukken=`) により実測不可**。urls.yaml
   L342の既存note「WP管理画面のみDisallow」は実態と食い違っている（URLは変更していない・
   note文言の訂正はユーザー判断）。
4. parse_sanrinbank の面積正規表現は坪のみ対応・㎡非対応（別問題、報告のみ）。

