"""申し送りから知識へ ── 流れるものを、残るものに上げる。

申し送りは「今回の事情」で、流れていく。知識は「この会社で繰り返し効くこと」で、残す。
その境目を決められるのは人だけである。**機械が「これは本質だ」と判定してはいけない。**

だから機械の仕事はここまでにする ──
①同じことが繰り返し起きているのを **名指しする**
②その本文を持ったまま、知識を書く画面へ **飛ばす**
③上げ終えたものに **印を残す**（消さない。経緯がいちばんの学びになる）

探しに行かせない、という点は知識の泉と同じ。
「そろそろ知識にしませんか」と、画面の側から言う。
"""

import datetime
import json

NOTE_KIND = "申し送り"


def _rows(conn):
    return conn.execute(
        """SELECT id, occurred_at, subject, body FROM records
            WHERE kind=? ORDER BY occurred_at DESC, id DESC""", (NOTE_KIND,)).fetchall()


def repeats(conn, days=90, threshold=3):
    """同じ部門・同じ区分が続いている塊を返す。

    起点は壁の時計ではなく、**いちばん新しい申し送りの日**。
    データが止まっている期間があっても、名指しが消えない。
    すでに知識へ上げたものは数えない ── 上げたのに催促され続けるのは、ただの雑音。
    """
    rows = _rows(conn)
    if not rows:
        return []
    newest = max(r["occurred_at"] for r in rows)
    since = (datetime.date.fromisoformat(newest) - datetime.timedelta(days=days)).isoformat()

    groups = {}
    for row in rows:
        if row["occurred_at"] < since:
            continue
        body = json.loads(row["body"])
        if body.get("promoted_to"):
            continue
        key = (row["subject"], body.get("category") or "（区分なし）")
        bucket = groups.setdefault(key, {"subject": key[0], "category": key[1],
                                         "ids": [], "texts": [], "latest": row["occurred_at"]})
        bucket["ids"].append(row["id"])
        bucket["texts"].append(body.get("text") or "")

    out = [dict(g, count=len(g["ids"])) for g in groups.values() if len(g["ids"]) >= threshold]
    return sorted(out, key=lambda g: (-g["count"], g["subject"]))


def draft(conn, note_id):
    """その申し送りを、知識の下書きにする。本質欄に本文をそのまま入れる。

    **要約しない。** 何が本質かを決めるのは書く人であって、ここではない。
    長すぎれば知識の側が「一行で書けないなら2件に分けてください」と止める。
    """
    row = conn.execute(
        "SELECT subject, body FROM records WHERE id=? AND kind=?", (note_id, NOTE_KIND)).fetchone()
    if row is None:
        return None
    body = json.loads(row["body"])
    return {"subject": row["subject"], "essence": (body.get("text") or "").strip(),
            "from_note": note_id}


def group_of(conn, note_id):
    """その申し送りと同じ塊（同じ部門・同じ区分）の id を返す。

    1件だけに印を付けると、残りが名指しされ続ける。上げたのは塊のほうである。
    """
    row = conn.execute(
        "SELECT subject, body FROM records WHERE id=? AND kind=?", (note_id, NOTE_KIND)).fetchone()
    if row is None:
        return []
    category = (json.loads(row["body"]).get("category") or "（区分なし）")
    for group in repeats(conn, threshold=1):
        if group["subject"] == row["subject"] and group["category"] == category:
            return group["ids"]
    return [note_id]


def mark_promoted(conn, note_ids, knowledge_id):
    """上げた申し送りに印を残す。申し送り自体は消さない。"""
    done = 0
    for note_id in note_ids:
        row = conn.execute(
            "SELECT body FROM records WHERE id=? AND kind=?", (note_id, NOTE_KIND)).fetchone()
        if row is None:
            continue
        body = json.loads(row["body"])
        body["promoted_to"] = knowledge_id
        conn.execute("UPDATE records SET body=?, updated_at=? WHERE id=?",
                     (json.dumps(body, ensure_ascii=False),
                      datetime.datetime.now().isoformat(timespec="seconds"), note_id))
        done += 1
    return done
