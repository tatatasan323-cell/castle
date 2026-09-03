"""打ち手 ── 診断のあとに、処方と追跡を置く。

社長の3つ目の問いは「どこで消えたか」ではない。**「で、どうする」**である。
どれだけ足りないか（[[pnl.gap]]）まで出しても、そこで止まれば読んだあとに何も起きない。

だからここで持つのは3つ。

  何をするか   部門・レバー・具体的にやること・担当・期限
  いくら見込むか  その打ち手で営業利益がいくら動くと見ているか
  どうなったか   仕込み中 → 実行中 → 効果測定中 → 効いた／効かなかった

**見込み額は人が入れる。機械は推定しない。**「これをやれば1,200万円」は現場と
部門長にしか言えない。機械にできるのは「粗利率を0.1pt動かせば2,877万円」という
目盛りを出すところまで（[[pnl.gap]] のレバー）。判断の材料は機械、判断は人。

**期限を過ぎて動いていない打ち手は名指しする。** 打ち手の台帳がいちばん腐るのは、
期限の切れた行が黙って残ることで、それは「打ったつもり」を作る。見込みからも外す
── 効くと決まっていないものを足して着地を語れば、それは粉飾と同じ形になる。
"""

import datetime
import json

LEVERS = ("売上", "粗利率", "人件費", "その他販管費")

# 状態。**効いた／効かなかったまで持つ。** 打ちっぱなしにしない。
STATES = ("仕込み中", "実行中", "効果測定中", "効いた", "効かなかった")

# 見込みに数える状態。**「効いた」は数えない。**
# 効いたものの効果は、もう実績（確定した月）の側に出ている。
# それを見込みにも足すと二重に数えることになり、着地が水増しされる。
# 効かなかったものと、期限切れも数えない。
COUNTED = ("仕込み中", "実行中", "効果測定中")

MAX_TEXT = 200


def add(conn, cfg, payload):
    """打ち手を1件記録する。**入口で弾く。** 骨は共通なので、緩めると何でも入る。"""
    subject = (payload.get("subject") or "").strip()
    if subject not in {d["name"] for d in cfg.departments}:
        return False, "部門が対応表にありません: %s" % (subject or "（空欄）")

    lever = (payload.get("lever") or "").strip()
    if lever not in LEVERS:
        return False, "レバーは %s のどれかです: %s" % ("／".join(LEVERS), lever or "（空欄）")

    try:
        expect = float(payload.get("expect") or 0)
    except (TypeError, ValueError):
        return False, "見込み額が数字として読めません: %s" % payload.get("expect")
    if expect <= 0:
        return False, "見込み額は0より大きい額が要ります（いくら動くと見ているか）"

    due = (payload.get("due") or "").strip()
    try:
        datetime.date.fromisoformat(due)
    except ValueError:
        return False, "期限が読めません（YYYY-MM-DD）: %s" % (due or "（空欄）")

    text = (payload.get("text") or "").strip()
    if not text:
        return False, "何をするかが空です。**打ち手は動詞で書きます。**"
    if len(text) > MAX_TEXT:
        return False, "長すぎます（%d字まで）。1つの打ち手に絞ってください。" % MAX_TEXT

    owner = (payload.get("owner") or "").strip() or "（担当未定）"
    body = {"lever": lever, "expect": expect, "due": due, "owner": owner,
            "text": text, "state": "仕込み中", "actual": None}
    now = datetime.datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO records(kind, occurred_at, subject, status, created_by, updated_at, body)"
        " VALUES('打ち手',?,?,'confirmed',?,?,?)",
        (due, subject, payload.get("created_by") or owner, now,
         json.dumps(body, ensure_ascii=False)))
    return True, "%s／%s の打ち手を記録しました（見込み %s円・期限 %s）" % (
        subject, lever, format(int(expect), ","), due)


def advance(conn, row_id, state, actual=None):
    """状態を進める。**効いた額を後から入れられる**ようにしておく。"""
    if state not in STATES:
        return False, "状態は %s のどれかです: %s" % ("／".join(STATES), state)
    row = conn.execute("SELECT body FROM records WHERE id=? AND kind='打ち手'",
                       (row_id,)).fetchone()
    if row is None:
        return False, "その打ち手はありません: %s" % row_id
    body = json.loads(row["body"])
    body["state"] = state
    if actual is not None:
        body["actual"] = float(actual)
    conn.execute("UPDATE records SET body=?, updated_at=? WHERE id=?",
                 (json.dumps(body, ensure_ascii=False),
                  datetime.datetime.now().isoformat(timespec="seconds"), row_id))
    return True, "%s にしました。" % state


def load(conn, cfg, scope=None, today=None):
    today = today or (cfg.sources or {}).get("基準日") or datetime.date.today().isoformat()
    rows = []
    for row in conn.execute(
            "SELECT id, subject, body FROM records WHERE kind='打ち手' ORDER BY occurred_at"):
        if scope is not None and row["subject"] not in scope:
            continue
        body = json.loads(row["body"])
        done = body["state"] in ("効いた", "効かなかった")
        rows.append(dict(body, id=row["id"], subject=row["subject"],
                         overdue=(not done) and body["due"] < today))
    return rows


def board(conn, cfg, scope=None, today=None):
    """打ち手ぜんぶを、ギャップと突き合わせた形にする。

    **見込みに数えるのは、期限内で、効かなかったと分かっていないものだけ。**
    期限の切れた行を足して着地を語れば、それは粉飾と同じ形になる。
    """
    import pnl

    rows = load(conn, cfg, scope=scope, today=today)
    counted = [r for r in rows if r["state"] in COUNTED and not r["overdue"]]
    planned = sum(r["expect"] for r in counted)
    gap = pnl.gap(conn, cfg)
    short = gap["year"]["short"] if gap else 0.0
    return {
        "rows": rows,
        "counted": counted,
        "overdue": [r for r in rows if r["overdue"]],
        "planned": planned,
        "landed": sum((r["actual"] or 0.0) for r in rows if r["state"] == "効いた"),
        "short": short,
        "uncovered": max(short - planned, 0.0),
        "cushion": planned - short,     # 打ち手が効いたときの、予算に対する余裕
        "by_state": {s: len([r for r in rows if r["state"] == s]) for s in STATES},
    }
