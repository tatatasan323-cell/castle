"""知識の泉 ── 数字の泉と並ぶ、もう一本の泉。

数字は「何が起きたか」を出す。ここに貯めるのは「**この会社で、繰り返し効くこと**」。
社内の判断、うまくいかなかった改善、現場の小さなコツ、新人が知らない前提。

**ただのメモ帳にしないための仕組みが本体。** 社内wikiが必ず死ぬ理由は3つあり、
それぞれに対して構造で答える。

  1. 書式が自由すぎて後から引けない
     → **1件1事実**。本質は一行（120字）しか書けない。
        一行で書けないなら、まだ2つ以上が混ざっている。分けて2件にする。
        「なぜ」と「どう使うか」を必須にする ── 理由の無い知識は捨ててよいか判断できず、
        使い方の無い知識はただの感想になる。

  2. 古い知識が現役の顔をして残る
     → **上書き原則**。覆った知識は superseded にして現役から外す。並存させない。
        並存は「忘れる」より危険な記憶違いを生む。ただし**消さない** ──
        なぜ覆ったかが、いちばん学びになる。
     → **棚卸し**。一定期間だれも見直していないものを名指しで出す。

  3. 書いても読まれない
     → **探しに行かせない**。数字の泉が落ち込みを検知した瞬間、
        その部門の知識をダッシュボードに並べる。必要な瞬間にだけ出てくる。

新しい理屈ではない。個人の記憶運用で効くと分かっている規範 ──
1件1事実／上書き原則／想起キーを研ぐ／定期棚卸し ── を、そのまま社内の仕組みに移しただけ。
"""

import datetime
import json
import re
import unicodedata

TYPES = ("判断", "失敗", "コツ", "前提")
KIND = "知識"
COMPANY_WIDE = "全社"
LIMITS = {"essence": 120, "why": 200, "how": 200}
STALE_DAYS = 180


# ---------------------------------------------------------------- 表記ゆれの吸収

def fold(text):
    """検索側と対象側の両方を、同じ畳み方に通す。

    NFKC が全角英数→半角と半角カナ→全角カナを引き受けるので、
    残るのは「ひらがな→カタカナ」と「小文字化」だけ。
    """
    value = unicodedata.normalize("NFKC", text or "")
    value = "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in value)
    return re.sub(r"\s+", " ", value.lower()).strip()


# 拗音（3字）から先に当てる。順番を間違えると sha が s/h/a に割れて壊れる。
_ROMAJI = [
    {"kya": "キャ", "kyu": "キュ", "kyo": "キョ", "sha": "シャ", "shu": "シュ", "sho": "ショ",
     "cha": "チャ", "chu": "チュ", "cho": "チョ", "nya": "ニャ", "nyu": "ニュ", "nyo": "ニョ",
     "hya": "ヒャ", "hyu": "ヒュ", "hyo": "ヒョ", "mya": "ミャ", "myu": "ミュ", "myo": "ミョ",
     "rya": "リャ", "ryu": "リュ", "ryo": "リョ", "gya": "ギャ", "gyu": "ギュ", "gyo": "ギョ",
     "bya": "ビャ", "byu": "ビュ", "byo": "ビョ", "pya": "ピャ", "pyu": "ピュ", "pyo": "ピョ",
     "shi": "シ", "chi": "チ", "tsu": "ツ"},
    {"ka": "カ", "ki": "キ", "ku": "ク", "ke": "ケ", "ko": "コ",
     "sa": "サ", "si": "シ", "su": "ス", "se": "セ", "so": "ソ",
     "ta": "タ", "ti": "チ", "tu": "ツ", "te": "テ", "to": "ト",
     "na": "ナ", "ni": "ニ", "nu": "ヌ", "ne": "ネ", "no": "ノ",
     "ha": "ハ", "hi": "ヒ", "hu": "フ", "fu": "フ", "he": "ヘ", "ho": "ホ",
     "ma": "マ", "mi": "ミ", "mu": "ム", "me": "メ", "mo": "モ",
     "ya": "ヤ", "yu": "ユ", "yo": "ヨ",
     "ra": "ラ", "ri": "リ", "ru": "ル", "re": "レ", "ro": "ロ",
     "wa": "ワ", "wo": "ヲ", "nn": "ン",
     "ga": "ガ", "gi": "ギ", "gu": "グ", "ge": "ゲ", "go": "ゴ",
     "za": "ザ", "ji": "ジ", "zi": "ジ", "zu": "ズ", "ze": "ゼ", "zo": "ゾ",
     "da": "ダ", "de": "デ", "do": "ド",
     "ba": "バ", "bi": "ビ", "bu": "ブ", "be": "ベ", "bo": "ボ",
     "pa": "パ", "pi": "ピ", "pu": "プ", "pe": "ペ", "po": "ポ",
     "ja": "ジャ", "ju": "ジュ", "jo": "ジョ", "va": "バ", "vi": "ビ", "vu": "ヴ"},
    {"a": "ア", "i": "イ", "u": "ウ", "e": "エ", "o": "オ", "n": "ン", "-": "ー"},
]


def romaji_to_kana(text):
    out, i = [], 0
    while i < len(text):
        # 促音：同じ子音が続いたら ッ
        if (i + 1 < len(text) and text[i] == text[i + 1]
                and text[i].isalpha() and text[i] not in "aiueon"):
            out.append("ッ")
            i += 1
            continue
        for size, table in ((3, _ROMAJI[0]), (2, _ROMAJI[1]), (1, _ROMAJI[2])):
            piece = text[i:i + size]
            if piece in table:
                out.append(table[piece])
                i += size
                break
        else:
            return None          # 読めない綴りが混ざったら、ローマ字ではないと判断する
    return "".join(out)


def expand(query, readings=None):
    """入力を、当てにいく形に広げる。英字だけならカナ読みも並べる。"""
    base = fold(query)
    if not base:
        return []
    out = {base}
    if re.fullmatch(r"[a-z0-9 -]+", base):
        kana = romaji_to_kana(base.replace(" ", ""))
        if kana:
            out.add(fold(kana))
    # 英略語のカナ読みは自動生成しない（読みが多義になる）。config で個別に持つ。
    for word, reading in (readings or {}).items():
        if fold(word) and fold(word) in base:
            out.add(fold(reading))
    return sorted(out)


# ---------------------------------------------------------------- 記録

def body_of(row):
    return json.loads(row["body"])


_body = body_of


def _blob(row):
    b = _body(row)
    return fold(" ".join([
        b.get("essence", ""), b.get("why", ""), b.get("how", ""),
        b.get("type", ""), row["subject"] or "", row["created_by"] or "",
        " ".join(b.get("tags", [])),
    ]))


def add(conn, cfg, payload):
    """1件足す。整理されていないものは、ここで止める。"""
    name = (payload.get("subject") or "").strip()
    if name == COMPANY_WIDE:
        subject = COMPANY_WIDE
    else:
        dept = cfg.resolve(name=name)
        if dept is None:
            return {"ok": False, "message": "部門「%s」は対応表にありません。" % (name or "（未選択）")}
        subject = dept["name"]

    kind_of = (payload.get("type") or "").strip()
    if kind_of not in TYPES:
        return {"ok": False, "message": "種類は %s のいずれかです。" % "／".join(TYPES)}

    fields = {}
    labels = {"essence": "本質", "why": "なぜそうなのか", "how": "どう使うか"}
    for key, limit in LIMITS.items():
        value = (payload.get(key) or "").strip()
        if not value:
            return {"ok": False, "message": "「%s」が空です。" % labels[key]}
        if len(value) > limit:
            hint = ("　一行で書けないものは、まだ2つ以上が混ざっています。分けて2件にしてください。"
                    if key == "essence" else "")
            return {"ok": False,
                    "message": "「%s」は%d文字までです（いまは%d文字）。%s" % (labels[key], limit, len(value), hint)}
        fields[key] = value

    author = (payload.get("author") or "").strip()
    if not 1 <= len(author) <= 30:
        return {"ok": False, "message": "記録者は1〜30文字で入れてください。"}

    supersedes = payload.get("supersedes")
    supersedes = int(supersedes) if str(supersedes or "").strip().isdigit() else None
    if supersedes:
        target = conn.execute(
            "SELECT id, status FROM records WHERE id=? AND kind=?", (supersedes, KIND)).fetchone()
        if target is None:
            return {"ok": False, "message": "覆す相手の知識 #%d が見つかりません。" % supersedes}
        if target["status"] != "active":
            return {"ok": False, "message": "知識 #%d は既に現役から外れています。" % supersedes}

    today = datetime.date.today().isoformat()
    body = dict(fields)
    body.update({
        "type": kind_of,
        "tags": [t.strip() for t in (payload.get("tags") or "").replace("　", " ").split() if t.strip()],
        "reviewed_at": today,
        "source": "form",
    })
    if supersedes:
        body["supersedes"] = supersedes
    if str(payload.get("from_note") or "").strip().isdigit():
        body["from_note"] = int(payload["from_note"])

    cursor = conn.execute(
        """INSERT INTO records(kind, occurred_at, subject, status, created_by, updated_at, body)
           VALUES(?,?,?,?,?,?,?)""",
        (KIND, today, subject, "active", author,
         datetime.datetime.now().isoformat(timespec="seconds"), json.dumps(body, ensure_ascii=False)))
    new_id = cursor.lastrowid

    message = "知識 #%d を記録しました。" % new_id
    if supersedes:
        # 上書き原則：現役から外すが、消さない。なぜ覆ったかが残る。
        old = conn.execute("SELECT body FROM records WHERE id=?", (supersedes,)).fetchone()
        old_body = json.loads(old["body"])
        old_body["_superseded_by"] = new_id
        conn.execute("UPDATE records SET status='superseded', body=?, updated_at=? WHERE id=?",
                     (json.dumps(old_body, ensure_ascii=False),
                      datetime.datetime.now().isoformat(timespec="seconds"), supersedes))
        message += "（#%d を覆しました。古い方は履歴として残ります）" % supersedes

    return {"ok": True, "id": new_id, "message": message}


# ---------------------------------------------------------------- 引き出す

def _rows(conn, where="", args=()):
    return conn.execute(
        "SELECT * FROM records WHERE kind=? %s ORDER BY id DESC" % where, (KIND, *args)).fetchall()


def active(conn, subject=None, type_of=None):
    where, args = "AND status='active'", []
    if subject:
        where += " AND subject=?"
        args.append(subject)
    if type_of:
        where += " AND json_extract(body,'$.type')=?"
        args.append(type_of)
    return _rows(conn, where, tuple(args))


def superseded(conn):
    return _rows(conn, "AND status='superseded'")


def search(conn, query, readings=None):
    """表記ゆれを吸収して当てる。ひら／カタ／全半角／大小／ローマ字／略語読み。"""
    queries = expand(query, readings)
    if not queries:
        return active(conn)
    hits = []
    for row in active(conn):
        blob = _blob(row)
        if any(q in blob for q in queries):
            hits.append(row)
    return hits


def related(conn, subject, limit=3):
    """その部門の現役の知識。落ち込みの隣に出すためのもの。"""
    return active(conn, subject=subject)[:limit] + active(conn, subject=COMPANY_WIDE)[:1]


def stale(conn, days=STALE_DAYS):
    """一定期間だれも見直していない知識。古いまま現役の顔をさせない。"""
    edge = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    return [r for r in active(conn) if _body(r).get("reviewed_at", "") <= edge]


def review(conn, entry_id, by):
    """「まだ有効」を押す。棚卸しの起点を今日に戻す。"""
    row = conn.execute("SELECT body FROM records WHERE id=? AND kind=? AND status='active'",
                       (entry_id, KIND)).fetchone()
    if row is None:
        return False
    body = json.loads(row["body"])
    body["reviewed_at"] = datetime.date.today().isoformat()
    body["reviewed_by"] = by
    conn.execute("UPDATE records SET body=?, updated_at=? WHERE id=?",
                 (json.dumps(body, ensure_ascii=False),
                  datetime.datetime.now().isoformat(timespec="seconds"), entry_id))
    return True
