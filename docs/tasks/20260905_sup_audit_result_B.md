# 監査結果: ワーカーB（13アダプタ）— `<sup>`分断バグ横展開監査

指示書: `docs/tasks/20260905_sup_audit.md`。監査のみ（watch.py/urls.yaml/data/reports/tests は無編集）。

## 実測方法の注記（着手前に統一）
- 「fetchは1回」を厳守するため、(a) pwsh IWR の StatusCode 相当と (b) watch.fetch のHTML取得は
  **同一の1リクエスト**で兼ねた（`w.robots_allowed(url, s)` → `w.fetch(url, s)` をpythonで実行し、
  返り値のHTTPステータスをIWR相当・watch.fetch双方の列に記載）。UAは watch.py の HEADERS と同一
  （`requests.Session` 経由で同じ `w.fetch`/`w.robots_allowed` を直接呼んでいるため必然的に一致）。
  Windows子プロセス出力は `PYTHONIOENCODING=utf-8` を明示。
- 取得HTMLは `scratchpad\sup_audit\B\<sid>.html` に保存。以後はこのファイルのみを読む（再取得なし）。
- 実データ検証は、現行アダプタの内部関数（`w._xxx_cards`等）を保存HTMLに対して**そのまま**呼び出す
  方式（再実装しない）。

---

### parse_izuhighland（代表: izuhighland_yochi／URL: https://www.izuhighland.jp/ドックランとキャンプ場用地一覧）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed。同一1リクエストで兼用）
- 静的判定: X（面積・価格とも抽出処理自体が無い。`_make_record(url, slug[:60], None, None, False, ...)` L1871-1872 で両方 `None` 固定。コメントL1850「価格・面積は取得不可（None）」と明記されたリンク監視型アダプタ）
- 価格経路: X（同上）
- 面積要素の innerHTML 例: （該当要素なし。詳細リンクのURLスラッグのみを監視）
- 分断タグ: なし（サイト全体で `<sup` 出現数=0。実測200・730,662B取得）
- 実データ検証: filter_keywords無指定で7件ヒット（全国物件含む）。全件 price=None・area=None（設計どおり）
- 判定: 非該当（Xそもそも抽出せず）

---

### parse_tokaiyajima（代表: tokaiyajima_izu／URL: https://tokaiyajima.com/bukken/os2）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: 面積=B相当（`_first_sqm`/`parse_area_sqm`を一切使用しない独自関数 `_tokaiyajima_area` L1730。坪単価からの価格逆算(L1733-1737)またはタイトル中"XX坪"表記(L1738-1740)のみで、㎡/m2表示を全く参照しない設計）
- 価格経路: B相当（L1757 `parse_price_man(mp.group(1))`。`card_text`自体はL1751 `get_text(" ", strip=True)`＝区切りありだが、渡されるのは`_TOKAIYAJIMA_PRICE_RE`(L1725: `r"価格\s*([\d,]+\s*万円)"`)でマッチ済みの捕捉群のみで、この正規表現自体が`\s*`許容済み）
- 面積要素の innerHTML 例: `<li class="price-box">坪単価<span>0.7613万円</span></li>`（数字と単位は同一span内の単一テキストノード）
- 分断タグ: なし（price-box内のspanにネストタグ0件・全10カードで確認。サイト全体`<sup`出現数=0）
- 実データ検証: 全10件中 price None=1件（"商談中"＝文字どおり金額非公開、正規表現が数字自体を検出できないだけでバグでない）／area None=5件（坪単価・タイトル坪数のいずれも記載が無い物件＝サイトが㎡表示を持たない設計。分断タグはゼロなので分断由来ではない）
- 判定: 非該当

---

### parse_resort_bukken（代表: resortbukken_izu／URL: https://resort-bukken.com/izu）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: 面積=X（L1814 `_make_record(url, card_text[:60], price, None, False, ...)` — area引数`None`固定。抽出処理自体が無い）
- 価格経路: A（L1812 `price = parse_price_man(card_text)`。card_textはL1807 `card.get_text(" ", strip=True)`＝区切りありのフル文字列をそのまま直接`parse_price_man`へ渡している＝バグ候補パターン）
- 面積要素の innerHTML 例: `<a href=".../detail/4803">…伊東市 富戸 ホテル 2LDK <span class="price">4,500万円</span>…</a>`（価格の数字と「万円」は同一span内の単一テキストノード）
- 分断タグ: なし（価格spanは全サンプルで数字+単位が単一テキストノード。detail anchor 24件中サンプル6件を生HTMLで直接確認、いずれも分断なし）
- 実データ検証: detail anchor 24件中サンプル6件（4,500万円→4500／51万円→51／95万円→95／60万円→60／3,650万円→3650／250万円→250）全て一致、None・誤差なし
- 判定: 非該当（コードパターンはA該当だが、実サイトの価格spanが分断されていないため発現せず）

---

### parse_sanrin_net（代表: sanrin_net_zenkoku／URL: https://www.sanrin.net/）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: 面積=B（L1909 `area = _yamaichiba_sqm(specs.get("面積", ""))`。specsの値はL1891 `dd.get_text(strip=True)`＝区切りなし＝安全パターン）
- 価格経路: B（L1910 `price = parse_price_man(specs.get("価格", ""))`。同じspecs＝区切りなし）
- 面積要素の innerHTML 例: `<dl><dt>所在地:</dt><dd>三重県志摩市志摩町御座小浦</dd><dt>地　目:</dt><dd>山林</dd><dt>面　積:</dt><dd>0.0457ha</dd><dt>価　格:</dt><dd>50万円</dd></dl>`
- 分断タグ: なし（dd内は全て単一テキストノード）
- 実データ検証: forest_detail 9件中 area None=0・price None=0（全件 ha→㎡換算 成功、例: 0.0457ha→457.0㎡）
- 判定: 非該当

---

### parse_furusato（代表: furusato_shizuoka／URL: https://furusato-net.co.jp/result?bpref=6335）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: 面積=A（L1956 `area = _first_sqm(specs.get("土地面積", ""))`。specsの値はL1937 `val.get_text(" ", strip=True)`＝区切りあり）
- 価格経路: A（L1955 `price = parse_price_man(specs.get("価格", ""))`。同specs＝区切りあり）
- 面積要素の innerHTML 例: `<p class="title"><span class="sub">土地面積</span><span class="rSpan">281㎡</span></p>`
  価格側: `<p class="title"><span class="sub">価格</span><span class="rSpan color">880万円</span></p>`（インデント空白のみ、数字と単位は同一span内の単一テキストノード）
- 分断タグ: なし（rSpan内は全サンプルで単一テキストノード。全5件を生HTMLで直接確認）
- 実データ検証: 全5件中 area None=0。price None=1件（"ーーー万円"＝成約済み等を示す意図的表記。コード注釈L1928「価格が「ーーー万円」の場合はparse_price_manがNoneを返す想定」と一致・バグでない）
- 判定: 非該当（コードパターンはA該当だが、実サイトのrSpanが分断されていないため発現せず）

---

### parse_shinrin（代表: shinrin_tokai／URL: https://www.shin-rin.net/list/tokai）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: 面積=A（L1993 `area = _first_sqm(tx)`。txはL1991 `ka.get_text(" ", strip=True)`＝区切りあり）
- 価格経路: X（L1998 `_make_record(url, ..., None, area, ...)` — price引数`None`固定。掲載自体が無く常にNone、とコメントL1972にも明記）
- 面積要素の innerHTML 例: `<div class="kind_area">面積：254,814㎡ 　約77,081坪<br/></div>`
- 分断タグ: なし（.kind_area内は全3件で単一テキストノード。`<br/>`はあるが数字/単位間ではなく末尾）
- 実データ検証: 全3件中 area None=0（254,814㎡・69,437㎡・1,780㎡いずれも正しく抽出）
- 判定: 非該当（面積=コードパターンA該当だが分断なし／価格=X意図的）

---

### parse_akiya_athome_rent（代表: akiya_athome_rent_shizuoka／URL: https://www.akiya-athome.jp/rent/22/）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: 面積=B（L2295 `area = _first_sqm(v)`。vはspecs値=L2251 `dd.get_text(strip=True)`＝区切りなし）
- 価格経路: B（L2285 `price = parse_rent_man(v)`。同specs＝区切りなし）
- 面積要素の innerHTML 例: `<dl><dt>面積</dt><dd>88.6㎡</dd></dl>` ／ `<dl><dt>面積</dt><dd>面積不明</dd></dl>`
- 分断タグ: なし（dd内は全て単一テキストノード）
- 実データ検証: 3件中 area None=1件（`<dd>面積不明</dd>`＝サイトが明示的に「不明」と記載。バグでない）／price None=0
- 判定: 非該当

---

### parse_chintai_net（代表: chintai_net_izunokuni／URL: https://www.chintai.net/sizuoka/area/22225/list/）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: 面積=B（L2393-2397 `area = round(float(si["value"]), 1)` — hidden inputの`value`属性を直接読む。get_text自体を使わない。`_first_sqm`/`parse_area_sqm`不使用）
- 価格経路: B（L2389-2390 `price = parse_rent_man(ci["value"] + "円")` 同様に属性値を直読）
- 面積要素の innerHTML 例: `<input class="senMenseki" type="hidden" value="59"/>`
- 分断タグ: なし（そもそも`get_text`を経由しないため`<sup>`等の子タグ分断の影響を受けない構造）
- 実データ検証: 20部屋行中 area None=0・price None=0
- 判定: 非該当（そもそも該当機構の対象外の抽出経路）

---

### parse_eheya（代表: eheya_mishima／URL: https://www.eheya.net/shizuoka/area/22206/search/）
- IWR StatusCode: 403 ／ watch.fetch: 403（robots: allowed。html_len=919B＝ボット対策の拒否ページ）
- 静的判定: 面積=B（L2513 `detail_text = detail_el.get_text(strip=True)`＝区切りなし → L2471 `_first_sqm(detail_text)`。コード注釈L2464-2466で開発者が「コメントノード区切りの' / 'が'/'に潰れる」問題を既に認識した上で意図的に区切りなしを選択したと明記）
- 価格経路: B（L2508 `rent_text = (rp.get_text(strip=True) if rp else "") + (ru.get_text(strip=True) if ru else "")`＝両要素とも区切りなし取得後に単純連結。連結境界にも区切り文字を挟まない）
- 面積要素の innerHTML 例: **実測不可（403のため取得できず）**
- 分断タグ: **実測不可（403）**。今回のfetch（1回のみ・再試行なし）でHTTP 403・919Bのボット対策ページが返った。robots_allowedはTrue（fetch自体は許可）。本番SOURCES.md（2026-09-05 00:20更新、直近のActions実行結果）でも eheya_mishima・eheya_kannami が「要確認(HTTP 403)」と一致しており、data/snapshots にも eheya 関連のスナップショットが1件も存在しない（＝本番でも過去に一度もこのサイトの取得に成功していない）ことを確認済み（読み取りのみ）。既知の持続的ブロックであり今回の監査に起因する一時的事象ではない。
- 実データ検証: 実測不可（403）。ただしコード上は`_numeric_cell_text`と同型の区切りなしパターンであり、`<sup>`等の分断があっても`_first_sqm`は影響を受けない設計（開発者コメントに明記済み）。
- 判定: 非該当（推定・コード上B。分断タグの実物確認は403のため不能だが、区切りなし抽出のため該当機構の対象外）

---

### parse_jmty（代表: jmty_land／URL: https://jmty.jp/shizuoka/est-land）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: 面積=A（L3305 `area = _first_sqm(card_text)`。card_textはL3304 `title + " " + location + " " + card.get_text(" ", strip=True)`＝区切りありの結合）
- 価格経路: B（L3291 `price = parse_rent_man(price_text) if is_rent else parse_price_man(price_text)`。price_textはL3290 `price_el.get_text(strip=True)`＝card_textとは別要素で区切りなし取得）
- 面積要素の innerHTML 例: `地積：畑7筆合計　7,944㎡(公簿面積)`（カード本文中のプレーンテキスト、タグ分断なし）
- 分断タグ: なし（サイト全体`<sup`出現数=0。面積記載のあるサンプルカードでいずれも数字・単位は連続テキスト）
- 実データ検証: 詳細リンクあり18件中 area None=4件。**4件全てを個別に生HTMLで確認**したところ、いずれもカード本文中に㎡/m2/m²/坪/平米/面積のキーワードが一切含まれず（一覧の説明文が「...」で途中省略されており、そもそも面積情報を掲載していない物件）。分断による誤検出ではなく、サイト側が一覧に面積を出していないケース。price None=0（is_rent=False固定のためparse_price_man使用、jmty_landの代表URLでは常にFalse）
- 判定: 非該当

---

### parse_lifull_rent（代表: lifull_rent_mishima／URL: https://www.homes.co.jp/chintai/shizuoka/mishima-city/list/）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed。202レート制限は今回発生せず・1回のみで取得成功）
- 静的判定: 面積=A（L3456 `layout_td.get_text(" ", strip=True)` → L3413 `_lifull_rent_madori_area`内 `_first_sqm(text)`）
- 価格経路: C（L3454 `price_td.get_text(" ", strip=True)` → `_lifull_rent_price_fields`。正規表現`_LIFULL_RENT_PRICE_RE`(L3391-3393)が不一致の場合のみ`parse_rent_man(text)`へフォールバック＝区切りテキストを関数へ直接渡す経路が併存）
- 面積要素の innerHTML 例: `<td class="layout">2LDK 59.43m²</td>`（分断なし・単一テキストノード）
- 価格要素の innerHTML 例: `<td class="price"><span id="label-...-part2"><span class="priceLabel"><span class="num">8.3</span>万円</span>/5,500円</span><br/>無/1ヶ月/-/-</td>`（**数字8.3と「万円」が実際にspanタグで分断されている**）
- 分断タグ: 面積側=なし（td.layout 80件全てネストタグ0件）／価格側=**あり**（td.price 80件**全件**が`<span class="num">N</span>万円`型で分断。ただし`_LIFULL_RENT_PRICE_RE`の`([\d,]+(?:\.\d+)?)\s*万円`部分が`\s*`を明示的に許容する設計になっており、分断由来の"8.3 万円"（空白混入）でも正しく一致する）
- 実データ検証: 30建物・80部屋行を現行アダプタで実行 → area None=0／price None=0（例: "8.3 万円 /5,500円 無/1ヶ月/-/-" → price=8.3, kanrihi=5500, shikikin="無", reikin="1ヶ月" 全て正常抽出）。参考: 本番reports/20260905.csvのLIFULL賃貸3市町（伊豆の国市/田方郡函南町/三島市）合計200件でもarea_sqm空欄=0件と一致。
- 判定: 非該当（価格側は実際に分断が存在するが、正規表現側が`\s*`許容済みで既に対応済み。面積側はそもそも分断なし）

---

### parse_sjkk（代表: sjkk_kenei／URL: https://www.sjkk.or.jp/kenei/list.php）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: 面積=X（L3550 `_make_record(url, ..., None, False, ...)` — area引数`None`固定。コード注釈L3499-3501「間取り欄は複数の部屋タイプを1本の面積レンジにまとめた表記で対応不能のため area は None のままとし」と意図的設計を明記）
- 価格経路: 対象外関数（`parse_price_man`/`parse_rent_man`のいずれでもない専用関数`_sjkk_rent_man` L3507。範囲表記"13,900～64,100円"の下限を`_SJKK_RENT_LOW_RE=re.compile(r"([\d,]+)")`で抽出。本監査の3対象関数（`_first_sqm`/`parse_area_sqm`/`parse_price_man`）のいずれも使用しないため厳密には対象外だが、価格経路として参考記録する）
- 面積要素の innerHTML 例: （area抽出処理なし）／価格: `<dl><dt>家賃(円)</dt><dd>13,900～64,100円</dd></dl>`（区切りありget_text L3535だが単一テキストノードで分断なし）
- 分断タグ: なし（家賃ddは全20件で単一テキストノード確認）
- 実データ検証: 20件中 price None=0（下限抽出は全件成功）
- 判定: 非該当（面積=X意図的設計／価格=本監査の対象関数を使用していないため対象外。実測上も問題なし）

---

### parse_vhouse（代表: vhouse_numazu／URL: https://www.villagehouse.jp/chintai/tokai/shizuoka/numazu-shi-222038/）
- IWR StatusCode: 200 ／ watch.fetch: 200（robots: allowed）
- 静的判定: 面積=X・価格=X（L3618 `_make_record(url, ..., None, None, False, ...)` — 両方`None`固定。コード注釈L3576-3577「家賃は静的HTMLに出ない（JS描画）ためリンク監視型で実装（価格None・面積None）」と意図的設計を明記）
- 面積要素の innerHTML 例: （抽出処理なし。JS描画のため静的HTMLに家賃・面積情報自体が存在しない）
- 分断タグ: なし（該当要素が存在しないため判定対象外）
- 実データ検証: li.container-search-cards-community 2件（コード注釈の「沼津2棟」と一致）。全件 price=None・area=None（設計どおり）
- 判定: 非該当

---

## 総括表（ワーカーB担当13アダプタ）

| アダプタ | 代表sid | IWR | fetch | 静的判定 | 分断タグ | 判定 | 一言理由 |
|---|---|---|---|---|---|---|---|
| parse_izuhighland | izuhighland_yochi | 200 | 200 | 面積X／価格X | なし | 非該当 | リンク監視型・そもそも数値抽出なし |
| parse_tokaiyajima | tokaiyajima_izu | 200 | 200 | 面積B相当／価格B相当 | なし | 非該当 | 面積は坪単価逆算のみでm2/㎡不使用、価格は`\s*`許容正規表現経由 |
| parse_resort_bukken | resortbukken_izu | 200 | 200 | 面積X／価格A | なし | 非該当 | 価格spanは常に数字+単位が単一テキストノード |
| parse_sanrin_net | sanrin_net_zenkoku | 200 | 200 | 面積B／価格B | なし | 非該当 | dd.get_text(strip=True)＝区切りなしの安全パターン |
| parse_furusato | furusato_shizuoka | 200 | 200 | 面積A／価格A | なし | 非該当 | rSpanは常に単一テキストノード、None1件は「ーーー万円」の意図的表記 |
| parse_shinrin | shinrin_tokai | 200 | 200 | 面積A／価格X | なし | 非該当 | .kind_areaは常に単一テキストノード |
| parse_akiya_athome_rent | akiya_athome_rent_shizuoka | 200 | 200 | 面積B／価格B | なし | 非該当 | dd区切りなし。None1件は「面積不明」の明示表記 |
| parse_chintai_net | chintai_net_izunokuni | 200 | 200 | 面積B／価格B | なし(該当外) | 非該当 | hidden input のvalue属性を直読・get_text不使用 |
| parse_eheya | eheya_mishima | 403 | 403 | 面積B／価格B | 実測不可(403) | 非該当(推定) | 区切りなしget_text設計（コード注釈で意図明記）。本番も持続的403で未取得 |
| parse_jmty | jmty_land | 200 | 200 | 面積A／価格B | なし | 非該当 | None4件は面積記載自体が無い物件（分断由来ではない） |
| parse_lifull_rent | lifull_rent_mishima | 200 | 200 | 面積A／価格C | 価格側**あり**（面積側なし） | 非該当 | 価格の分断は実在するが正規表現が`\s*`許容済みで対応済み |
| parse_sjkk | sjkk_kenei | 200 | 200 | 面積X／価格=対象外関数 | なし | 非該当 | 面積は意図的None設計。価格は本監査対象の3関数を未使用 |
| parse_vhouse | vhouse_numazu | 200 | 200 | 面積X／価格X | なし(該当要素なし) | 非該当 | JS描画のため静的HTMLに情報自体が存在しない設計 |

**結論: ワーカーB担当13アダプタ中、「該当（壊れている）」は0件。** 全13アダプタで`<sup>`分断バグ（またはそれに類する子タグ分断）による実害は確認されなかった。内訳:
- 6アダプタ（sanrin_net/akiya_athome_rent/chintai_net/eheya/tokaiyajima価格）は区切りなし取得または属性直読で、そもそも分断の影響を受けない設計。
- 5アダプタ（resort_bukken価格/furusato/shinrin/jmty面積/lifull_rent面積）はコード上「A」（区切りあり→直接渡し）パターンに該当するが、実サイトのHTML構造上、数字と単位が分断されていないため発現していない。
- 1アダプタ（lifull_rent価格）は実際に分断が存在する（`<span class="num">8.3</span>万円`）が、`_LIFULL_RENT_PRICE_RE`の正規表現が`\s*`を明示的に許容する設計で既に対応済みと確認。
- 3アダプタ（izuhighland/vhouse/sjkk面積、resort_bukken/shinrin/sjkk価格の一部）はそもそも当該値を抽出しない意図的設計（X）。
- 1アダプタ（eheya）は今回・本番とも persistent 403 で実HTML確認は不能。コード（`_numeric_cell_text`と同型の区切りなし設計、開発者コメントで意図明記）から判断して安全と推定。

担当13件中12件でHTML実測に成功（残1件=eheyaは403）。urls.yamlのURLは一切変更していない。
