"""記録層。骨(castle/)とデータ(instance/)の分離をここで担保する。

コードは castle/ にしかなく、データベース・設定・取り込み対象は instance/ にしかない。
--instance で別の場所を指せるので、骨だけを持ち出しても動く。
"""

import pathlib
import sqlite3

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "castle" / "schema.sql"
DEFAULT_INSTANCE = ROOT / "instance"


def instance_dir(override=None):
    path = pathlib.Path(override).resolve() if override else DEFAULT_INSTANCE
    if not path.exists():
        raise SystemExit("instance が見つかりません: %s" % path)
    return path


def connect(instance):
    """data.db を開く。無ければ schema.sql から作る。"""
    conn = sqlite3.connect(instance / "data.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn
