# -*- coding: utf-8 -*-
"""IT初心者向け 物件さがしツール かんたんマニュアル（日本語PDF）"""
import io, os
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, PageBreak, KeepTogether)

pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
JP = "HeiseiKakuGo-W5"

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "物件さがしツール_かんたん説明.pdf")

NAVY = colors.HexColor("#1f3864")
BLUE = colors.HexColor("#2e75b6")
LIGHT = colors.HexColor("#eaf1f8")
GRAY = colors.HexColor("#595959")
GREEN = colors.HexColor("#2a8a4a")
ORANGE = colors.HexColor("#c07030")
PURPLE = colors.HexColor("#7b52ab")

S = {
    "title": ParagraphStyle("title", fontName=JP, fontSize=22, leading=30,
                            textColor=NAVY, spaceAfter=4),
    "sub": ParagraphStyle("sub", fontName=JP, fontSize=11, leading=17,
                          textColor=GRAY, spaceAfter=14),
    "h": ParagraphStyle("h", fontName=JP, fontSize=15, leading=22, textColor=colors.white,
                        backColor=BLUE, borderPadding=(6, 8, 6, 8), spaceBefore=16, spaceAfter=10),
    "body": ParagraphStyle("body", fontName=JP, fontSize=11.5, leading=19, spaceAfter=8),
    "big": ParagraphStyle("big", fontName=JP, fontSize=13, leading=21, spaceAfter=8),
    "note": ParagraphStyle("note", fontName=JP, fontSize=10, leading=16,
                           textColor=GRAY, spaceAfter=6),
    "cell": ParagraphStyle("cell", fontName=JP, fontSize=10.5, leading=16),
    "cellb": ParagraphStyle("cellb", fontName=JP, fontSize=11, leading=16, textColor=NAVY),
    "step": ParagraphStyle("step", fontName=JP, fontSize=12, leading=19),
}


def box(text, style, bg, border):
    t = Table([[Paragraph(text, style)]], colWidths=[165 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("BOX", (0, 0), (-1, -1), 1.2, border),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
    ]))
    return t


def table(rows, widths, header_bg=BLUE):
    data = []
    for i, r in enumerate(rows):
        st = S["cellb"] if i == 0 else S["cell"]
        if i == 0:
            st = ParagraphStyle("hdr", parent=st, textColor=colors.white)
        data.append([Paragraph(c, st) for c in r])
    t = Table(data, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
        ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#b8c8dc")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f8fc")]),
    ]))
    return t


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont(JP, 8.5)
    canvas.setFillColor(GRAY)
    canvas.drawString(20 * mm, 12 * mm, "物件さがしツール かんたん説明")
    canvas.drawRightString(190 * mm, 12 * mm, "%d" % doc.page)
    canvas.setStrokeColor(colors.HexColor("#c8d4e4"))
    canvas.line(20 * mm, 15 * mm, 190 * mm, 15 * mm)
    canvas.restoreState()


F = []

# ============ 1ページ目 ============
F.append(Paragraph("物件さがしツール", S["title"]))
F.append(Paragraph("かんたん説明（これだけ読めば使えます）", S["sub"]))

F.append(box(
    "静岡県の東部（函南・三島・沼津のあたり）で売っている土地・家、"
    "貸している部屋の情報を、<b>毎朝ぜんぶ自動で集めてくる</b>ページです。<br/><br/>"
    "全部で <b>101か所</b>のサイトを見に行っています。"
    "自分でひとつずつサイトを開いて探す必要はありません。",
    S["big"], LIGHT, BLUE))

F.append(Spacer(1, 14))
F.append(Paragraph("使い方は 4つだけ", S["h"]))

steps = [
    ("1", "リンクを開く", "送られてきたリンクをタップするだけです。<br/>"
     "<font color='#2a8a4a'><b>スマホの「ホーム画面に追加」をしておくと、次から1タップで開けます。</b></font>"),
    ("2", "上のボタンで切りかえる", "「更地」「家付き土地」「キャンプ場土地」「賃貸」の4つのボタンがあります。<br/>"
     "部屋を借りたいなら <b>「賃貸」</b> を押してください。"),
    ("3", "気になったら ☆ を押す", "行の左はしにある ☆ を押すと <b>★</b> に変わって、"
     "下の「お気に入り」にたまっていきます。"),
    ("4", "いらないものは「非表示」", "右はしの「非表示」を押すと、その物件は一覧から消えます。<br/>"
     "何度も同じ物件を見なくてすみます。"),
]
rows = []
for n, t, d in steps:
    rows.append([
        Paragraph("<font color='#ffffff'><b>%s</b></font>" % n,
                  ParagraphStyle("num", fontName=JP, fontSize=17, leading=22, alignment=1)),
        Paragraph("<b>%s</b><br/><font size=10.5>%s</font>" % (t, d), S["cell"]),
    ])
t = Table(rows, colWidths=[16 * mm, 149 * mm])
t.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (0, -1), BLUE),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("GRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#b8c8dc")),
    ("LEFTPADDING", (1, 0), (1, -1), 9),
    ("TOPPADDING", (0, 0), (-1, -1), 9),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
]))
F.append(t)

F.append(Spacer(1, 12))
F.append(box(
    "<b>★や「非表示」は、あなたのぶんだけ保存されます。</b><br/>"
    "他の人には見えませんし、他の人の★があなたの画面に出ることもありません。<br/>"
    "スマホとパソコンの両方で同じリンクを開けば、同じ★が出ます。",
    S["body"], colors.HexColor("#eef7f0"), GREEN))

F.append(PageBreak())

# ============ 2ページ目 ============
F.append(Paragraph("4つのボタン（上のほうにあります）", S["h"]))
F.append(table([
    ["ボタン", "何が出るか"],
    ["更地", "建物がない土地。家を建てる用"],
    ["家付き土地", "家が建っている土地。中古の家・空き家"],
    ["キャンプ場土地", "山や林などの広い土地"],
    ["<b>賃貸</b>", "<b>借りる部屋・家。家賃と敷金・礼金が出ます</b>"],
], [40 * mm, 125 * mm]))

F.append(Spacer(1, 16))
F.append(Paragraph("画面のみかた", S["h"]))
F.append(table([
    ["ことば", "意味"],
    ["新着", "ここ7日間に新しく出てきた物件。新しい順にならびます"],
    ["検索条件", "家賃や広さで、しぼりこめます"],
    ["敷/礼", "敷金と礼金。「無」はいりません、という意味です"],
    ["色のついた数字", "<font color='#2a8a4a'><b>緑は安い・広い（おすすめ）</b></font>、"
     "赤は高い・せまい、の目印です"],
    ["非表示にした物件", "非表示にしたものは、ここにたまります。「戻す」で元にもどせます"],
], [40 * mm, 125 * mm]))

F.append(Spacer(1, 16))
F.append(box(
    "<b>まちがえて消してしまっても大丈夫です。</b><br/>"
    "「非表示にした物件」のところにある「戻す」を押せば、元にもどります。",
    S["body"], colors.HexColor("#fdf4ec"), ORANGE))

F.append(PageBreak())

# ============ 3ページ目 ============
F.append(Paragraph("どんなサイトを見に行っているの？", S["h"]))
F.append(Paragraph(
    "有名なサイトから、地元の小さな不動産屋さんまで、<b>101か所</b>を毎朝じゅんばんに見ています。",
    S["body"]))
F.append(Spacer(1, 6))

F.append(table([
    ["しゅるい", "見に行っている先（一部）"],
    ["<b>大手のサイト</b>", "SUUMO（スーモ）、LIFULL HOME'S（ホームズ）、<br/>"
     "アットホーム、CHINTAI、いい部屋ネット"],
    ["<b>空き家バンク</b>", "静岡県の空き家バンク、三島市・長泉町など<br/>市や町がやっているもの"],
    ["<b>公営の住宅</b>", "静岡県営住宅（1万円台からあります。※抽選です）"],
    ["<b>地元の不動産屋さん</b>", "真野開発、不動産創研、伊豆総合企画、家っち、<br/>U2JAPAN、東海ヤジマ など"],
    ["<b>個人が出しているもの</b>", "ジモティー、家いちば<br/>（不動産屋を通さない掘り出し物が出ます）"],
    ["<b>山や林の専門サイト</b>", "山いちば、山林バンク、森林.net、<br/>ふるさと情報館 など"],
    ["<b>安く借りられるところ</b>", "ビレッジハウス（敷金・礼金０円）"],
    ["<b>競売・公売</b>", "裁判所の競売、官公庁オークション、<br/>県や国が売る土地"],
], [42 * mm, 123 * mm]))

F.append(Spacer(1, 12))
F.append(box(
    "毎朝あたらしい情報にいれかわります。<br/>"
    "<b>「新着」のところだけ見れば、その日に出てきた物件がわかります。</b>",
    S["big"], LIGHT, BLUE))
F.append(PageBreak())

F.append(Spacer(1, 14))
# 表が途中で切れないよう見出しごとひとかたまりにする
F.append(KeepTogether([
    Paragraph("こまったとき", S["h"]),
    table([
        ["こんなとき", "どうする"],
        ["物件が1件も出てこない", "「検索条件」でしぼりこみすぎているかもしれません。<br/>"
         "「条件をリセット」を押してみてください"],
        ["★が消えてしまった", "しばらく開いていないと消えることがあります。<br/>"
         "けんさんに言えば元にもどせる場合があります"],
        ["よくわからない", "さわっても壊れません。<br/>いろいろ押してみて大丈夫です"],
    ], [45 * mm, 120 * mm]),
    Spacer(1, 12),
    Paragraph(
        "※ このページの情報は各サイトから自動で集めたものです。"
        "実際に借りる・買うときは、かならず元のサイトや不動産屋さんで確認してください。",
        S["note"]),
]))

doc = SimpleDocTemplate(
    OUT, pagesize=A4,
    leftMargin=22 * mm, rightMargin=23 * mm,
    topMargin=20 * mm, bottomMargin=22 * mm,
    title="物件さがしツール かんたん説明", author="negura",
)
doc.build(F, onFirstPage=footer, onLaterPages=footer)
print("OK:", OUT, os.path.getsize(OUT), "bytes")
