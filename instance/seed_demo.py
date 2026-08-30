"""デモ用の申し送りと知識を入れる。

申し送りと知識は、CSVから来ない ── これから現場が書くものだから。
だから make_sample.py（サイロの出力を作る側）ではなく、こちらに置く。

**入れ方は本番と同じ道を通す。** add_note / knowledge.add を呼ぶので、
検証も同じものが効く。デモ専用の抜け道を作らない。

数字だけの画面と、現場の一行が並んでいること ── それがこの城の目的地なので、
デモがその状態でなければ、デモとして成立しない。
"""

import pathlib
import sqlite3
import sys

APP = pathlib.Path(__file__).resolve().parent.parent / "castle" / "app"
sys.path.insert(0, str(APP))

import config as config_mod          # noqa: E402
import db                            # noqa: E402
import knowledge                     # noqa: E402
import serve                         # noqa: E402

NOTES = [
    {"subject": "農産部", "occurred_at": "2026-08-10", "category": "相場・仕入価格",
     "author": "農産課 佐藤",
     "text": "数量減は意図的。相場高が続くため、赤字取引を止めて粗利を守る方針で動いている。"
             "8月下旬に戻る見込み。"},
    {"subject": "業務用食材部", "occurred_at": "2026-08-12", "category": "得意先の事情",
     "author": "業務用課 鈴木",
     "text": "盆前で休業の得意先が増え、1件あたりの配送効率が落ちている。"},
    {"subject": "低温食品部", "occurred_at": "2026-08-07", "category": "物流・配送",
     "author": "低温課 田中",
     "text": "冷凍庫の一部が改修中で、外部倉庫を一時利用している。8月いっぱい続く見込み。"},
]

KNOWLEDGE = [
    {"subject": "業務用食材部", "type": "失敗", "author": "業務用課 鈴木",
     "essence": "盆前の欠品対策で在庫を積んだ年は、明けに廃棄が出て粗利がかえって沈んだ",
     "why": "盆明けの立ち上がりは需要が読みにくく、積んだ分がそのまま残る。"
            "欠品を恐れる気持ちのほうが、廃棄の記憶より強く働く。",
     "how": "盆前の発注は前年実績の1.0倍を上限にする。欠品は許容し、廃棄を避ける側に倒す。"},
    {"subject": "農産部", "type": "判断", "author": "農産課 佐藤",
     "essence": "相場高のときは数量を追わない。赤字取引を止めて粗利率を守るほうが着地は良い",
     "why": "数量を維持しようとすると売価転嫁が遅れ、粗利率が先に落ちる。"
            "戻ったときに元の率へ戻すほうが難しい。",
     "how": "相場が前年比+15%を超えたら、赤字取引の一覧を出して部門長と切る順番を決める。"},
    {"subject": "全社", "type": "前提", "author": "経理課 井上",
     "essence": "会計の締めは翌月10日ごろ。当月の原価率と人時単価は直近確定月のものを当てている",
     "why": "日ごとの原価を持っているシステムが無い。持っていない数字は作らない方針のため。",
     "how": "当月の粗利まわりの数字を外部に出すときは、推定であることを必ず添える。"},
]


def main():
    instance = db.instance_dir()
    cfg = config_mod.load(instance)
    conn = db.connect(instance)
    try:
        notes = 0
        for payload in NOTES:
            ok, message = serve.add_note(conn, cfg, payload)
            if not ok:
                raise SystemExit("申し送りが入りませんでした: %s" % message)
            notes += 1

        known = 0
        for payload in KNOWLEDGE:
            result = knowledge.add(conn, cfg, payload)
            if not result["ok"]:
                raise SystemExit("知識が入りませんでした: %s" % result["message"])
            known += 1

        conn.commit()
        print("デモの種を入れました: 申し送り %d件 ／ 知識 %d件" % (notes, known))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
