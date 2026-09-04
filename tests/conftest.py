"""pytest設定。プロジェクトルート（watch.pyの場所）をsys.pathへ追加するだけ。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
