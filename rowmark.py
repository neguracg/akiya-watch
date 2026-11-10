"""レポート画面の「行の赤枠マーク」（見た目CSS＋クリック処理JS）。

# @owns 一覧表の行に付ける赤枠マーク（端末ローカルの一時印）の見た目と操作

「詳細」で別タブへ飛んで戻った時に、どの物件だったか分からなくなる問題の対策（2026-11-10）。
★・非表示（localStorage＋VPS状態API）とは別物で、sessionStorage にだけ持つ。VPS へは同期しない。
watch.py は CSS / JS を連結し、rowHtml が markAttr(bdk) を呼び、既存の document click から
rowMarkClick(e) を呼ぶだけ（watch.py は1000行ラチェットのため、規則はここに集約する）。
"""

# 枠は inset の box-shadow で描く（border だと border-collapse:collapse の表で行高・列幅が動く／
# outline は tr に効かない Safari があるため）。上下は全セル、左右は先頭/末尾セルだけ。
# 太さは --mk（PC 3px・モバイル 2px。モバイルは td の padding が 2px しかなく文字に被らせない）。
CSS = (
    "tr.marked{--mk:3px;--mkc:#e53935;}"
    "tr.marked td{box-shadow:inset 0 var(--mk) 0 var(--mkc),inset 0 calc(-1*var(--mk)) 0 var(--mkc);}"
    "tr.marked td:first-child{box-shadow:inset 0 var(--mk) 0 var(--mkc),inset 0 calc(-1*var(--mk)) 0 var(--mkc),"
    "inset var(--mk) 0 0 var(--mkc);}"
    "tr.marked td:last-child{box-shadow:inset 0 var(--mk) 0 var(--mkc),inset 0 calc(-1*var(--mk)) 0 var(--mkc),"
    "inset calc(-1*var(--mk)) 0 0 var(--mkc);}"
    "@media(max-width:700px){tr.marked{--mk:2px;}}"
)

# マークは bdk（rowHtml の d.dk||g.dk）単位＝同じ物件が新着/お気に入り/物件ブラウザの複数表に
# 出ていれば全部に付く。render() で表が作り直されても、markAttr が MARKED から class を付け直す。
# markAttr の onclick="" は iOS Safari 対策（非インタラクティブ要素へのクリックは、onclick 属性が
# 無いと document への委譲まで届かないことがある。cursor:pointer は「押せる行」に見えるため付けない）。
# 詳細リンクは常に「付ける」（トグルしない）。リンク遷移は妨げない（preventDefault しない）。
# 中クリック/Ctrl+クリックで開く操作は click が来ない（中クリックは auxclick）ため、詳細リンクだけ拾う。
JS = r"""
const MARK_KEY='akiyaMarked';
const MARKED=new Set((()=>{try{const a=JSON.parse(sessionStorage.getItem(MARK_KEY)||'[]');return Array.isArray(a)?a:[];}catch(e){return [];}})());
function saveMarked(){try{sessionStorage.setItem(MARK_KEY,JSON.stringify([...MARKED]));}catch(e){}}
function markAttr(bdk){return ' data-bdk="'+esc(bdk)+'" onclick=""'+(MARKED.has(bdk)?' class=marked':'');}
function setMark(bdk,on){
  if(on===MARKED.has(bdk))return;
  if(on)MARKED.add(bdk);else MARKED.delete(bdk);
  saveMarked();
  document.querySelectorAll('tr[data-bdk]').forEach(tr=>{if(tr.dataset.bdk===bdk)tr.classList.toggle('marked',on);});
}
function rowMarkClick(e){
  const t=e.target; if(!t||!t.closest)return;
  const tr=t.closest('tr[data-bdk]'); if(!tr)return;
  const bdk=tr.dataset.bdk;
  if(t.closest('td.detailcell a')){setMark(bdk,true);return;}
  if(t.closest('a,button,input,select,textarea,label,.help'))return;
  if(String(window.getSelection())!=='')return;
  setMark(bdk,!MARKED.has(bdk));
}
document.addEventListener('auxclick',e=>{if(e.button===1&&e.target.closest&&e.target.closest('td.detailcell a'))rowMarkClick(e);});
"""
