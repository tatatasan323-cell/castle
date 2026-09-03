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

import actions                       # noqa: E402
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

    # 同じ部門・同じ区分が続く形。これが「繰り返し出ている申し送り」として名指しされ、
    # 知識へ上げる口が画面に出る ── 今回の事情ではなく、繰り返し効くことなら。
    {"subject": "農産部", "occurred_at": "2026-07-14", "category": "相場・仕入価格",
     "author": "農産課 佐藤",
     "text": "葉物の相場が高い。数量を追うと粗利率が落ちるため、上位得意先に絞って出している。"},
    # 数字が言えるのは「売掛が膨らんだ」まで。なぜ膨らんだかは、ここにしかない。
    {"subject": "加工食品部", "occurred_at": "2026-07-22", "category": "得意先の事情",
     "author": "加工課 山本",
     "text": "大口2社の支払サイトが翌月末から翌々月10日に延びた。7月から回収が1か月ずれている。"},

    {"subject": "農産部", "occurred_at": "2026-06-23", "category": "相場・仕入価格",
     "author": "農産課 佐藤",
     "text": "相場高で仕入を絞った。売価転嫁が追いつかず、数量より率を優先した。"},
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

# 打ち手。**診断で終わらせない。** 足りない分に対して、誰が何をいつまでにやるか。
# 見込み額は人が入れる ── 「これをやれば1,200万円」は現場と部門長にしか言えない。
ACTIONS = [
    {"subject": "農産部", "lever": "粗利率", "expect": 12_000_000, "due": "2026-10-31",
     "owner": "農産課 佐藤",
     "text": "赤字取引を上位20件まで洗い出し、値上げ交渉か取引停止を部門長と決める。"},
    {"subject": "低温食品部", "lever": "その他販管費", "expect": 6_000_000, "due": "2026-09-30",
     "owner": "低温課 田中",
     "text": "冷凍庫の改修が終わり次第、外部倉庫の利用をやめる。"},
    {"subject": "業務用食材部", "lever": "売上", "expect": 9_000_000, "due": "2026-11-30",
     "owner": "業務用課 鈴木",
     "text": "盆で止まった得意先20社へ、9月第1週に定番の再提案を回る。"},
    {"subject": "加工食品部", "lever": "人件費", "expect": 4_000_000, "due": "2026-08-20",
     "owner": "加工課 山本",
     "text": "ピッキングの応援体制を見直し、残業を週5時間減らす。"},
    # 期限が過ぎて動いていないもの。**実在の会社の台帳には必ずこれがある。**
    # 見込みからは外れ、画面では赤で名指しされる ── 打ったつもりを残さないため。
    {"subject": "水産部", "lever": "売上", "expect": 7_000_000, "due": "2026-07-31",
     "owner": "水産課 高橋",
     "text": "量販店向けの解凍品を、7月中に3社へ試験導入する。"},
    # 効いたもの。**打ちっぱなしにせず、いくら効いたかまで戻す。**
    {"subject": "畜産部", "lever": "粗利率", "expect": 8_000_000, "due": "2026-07-15",
     "owner": "畜産課 中村",
     "text": "低回転の輸入品を絞り、国産の構成比を上げる。",
     "state": "効いた", "actual": 9_200_000},
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

        moves = 0
        for payload in ACTIONS:
            ok, message = actions.add(conn, cfg,
                                      {k: v for k, v in payload.items()
                                       if k not in ("state", "actual")})
            if not ok:
                raise SystemExit("打ち手が入りませんでした: %s" % message)
            if payload.get("state"):
                last = conn.execute(
                    "SELECT MAX(id) AS id FROM records WHERE kind='打ち手'").fetchone()["id"]
                actions.advance(conn, last, payload["state"], payload.get("actual"))
            moves += 1

        conn.commit()
        print("デモの種を入れました: 申し送り %d件 ／ 知識 %d件 ／ 打ち手 %d件"
              % (notes, known, moves))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
