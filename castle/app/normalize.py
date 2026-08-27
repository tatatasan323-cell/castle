"""汚いCSVを読むための共通処理。城のいちばん外側の壁。

現実のベンダー出力は、文字コードも列名も日付形式も部門名も揃っていない。
「入口で全部を同じ骨に変換する」のが城の役割なので、揃っていない前提でここに全部吸収する。
判定の骨は連載#12の売上集計ツール（HTML版）から移植した。
"""

import csv
import io
import re
import unicodedata

# ---------------------------------------------------------------- 文字コード

_CANDIDATES = ("cp932", "euc_jp", "utf_16_le", "iso2022_jp")


def _garbage_score(text):
    """復号の失敗しやすさを点数化する。小さいほど良い。

    U+FFFD の数だけで比べると、SJIS を UTF-16LE として読んだ場合のように
    「置換文字は出ないが中身は全部デタラメ」というケースを取り逃がす。
    区切り文字と改行があるか、という構造の当たり前を条件に足して塞ぐ。
    """
    score = text.count("�") * 100
    if "\n" not in text:
        score += 500
    if "," not in text and "\t" not in text:
        score += 500
    return score


def decode_bytes(data):
    """バイト列を文字列に直す。戻り値は (text, 判定した文字コード名)。

    BOM → 厳密UTF-8 → 候補総当り（最も壊れていないもの）の順。
    """
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", "replace"), "utf-8-sig"
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    best = None
    for enc in _CANDIDATES:
        text = data.decode(enc, "replace")
        score = _garbage_score(text)
        if best is None or score < best[2]:
            best = (text, enc, score)
    return best[0], best[1]


# ---------------------------------------------------------------- 表記の正規化


def norm(value):
    """全半角・大小文字・空白の揺れを潰す。半角カナもここで全角になる。"""
    text = unicodedata.normalize("NFKC", value or "").strip().lower()
    return text.replace(" ", "").replace("　", "")


_DATE = re.compile(r"(\d{4})\D{1,3}(\d{1,2})\D{1,3}(\d{1,2})")
_NUM = re.compile(r"[^0-9.\-]")


def parse_date(value):
    """2026/06/01 も 2026-6-1 も 2026年6月1日 も同じ YYYY-MM-DD にする。"""
    match = _DATE.search(unicodedata.normalize("NFKC", value or ""))
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return "%04d-%02d-%02d" % (year, month, day)


def parse_number(value):
    """カンマ・円記号・全角数字・空白を落として数値にする。読めなければ None。"""
    text = _NUM.sub("", unicodedata.normalize("NFKC", value or ""))
    if text in ("", "-", ".", "-."):
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------- 列名の対応付け

ALIAS = {
    "date": ["計上日", "営業日", "売上日", "勤務日", "年月日", "日付", "date"],
    "code": ["部門コード", "組織コード", "部署コード", "コード", "code"],
    "dept": ["部門名", "組織名", "部署名", "部門", "組織", "部署", "所属", "department", "dept"],
    "amount": ["売上金額", "純売上高", "売上高", "販売金額", "売上", "金額", "sales", "amount"],
    "hours": ["実労働時間", "総労働時間", "就業時間", "労働時間", "人時", "hours"],
    "cost": ["売上原価", "仕入原価", "原価", "cost"],
    "labor": ["人件費", "労務費", "給与手当", "labor"],
    "sga": ["その他販管費", "販売費及び一般管理費", "販売管理費", "販管費", "経費", "sga"],
}

# 会計は千円単位で出すことが多い。ここで直さないと桁が3つ足りないまま全部が進む。
# 「百万円」が「万円」を含むので、長いものから見る。
UNITS = [("百万円", 1_000_000), ("千円", 1_000), ("万円", 10_000)]

# 長いものから照合する。「部門コード」が「部門」に食われないようにするため。
_FLAT = sorted(
    ((norm(a), field) for field, aliases in ALIAS.items() for a in aliases),
    key=lambda pair: -len(pair[0]),
)


def field_of(header):
    """列見出しから項目名を引く。「売上金額（税抜）」のような装飾は部分一致で吸収する。"""
    key = norm(header)
    if not key:
        return None
    for alias, field in _FLAT:
        if alias in key:
            return field
    return None


def unit_of(header):
    """見出しに書いてある単位を倍率にする。「売上原価(千円)」→ 1000。"""
    key = norm(header)
    for label, multiplier in UNITS:
        if label in key:
            return multiplier
    return 1


_PERIOD = re.compile(r"(\d{4})\D{0,3}(\d{1,2})\s*月")


def parse_period(text):
    """「2026年7月度」から YYYY-MM を取る。会計の月次表は行に日付が無く、ここにしか書いていない。"""
    match = _PERIOD.search(unicodedata.normalize("NFKC", text or ""))
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    return "%04d-%02d" % (year, month) if 1 <= month <= 12 else None


# ---------------------------------------------------------------- 表として読む


def read_table(path):
    """CSV/TSVを読んで、揃えるのに必要なものを全部返す。

    見出しの前に「■売上日報 出力日:…」のような行が入っていることがあるので、
    先頭8行から「項目として認識できる列が2つ以上ある行」を探して見出しとする。
    その前の行（preamble）は捨てない ── 会計の月次表は、年月がそこにしか書いていない。
    """
    with open(path, "rb") as handle:
        text, encoding = decode_bytes(handle.read())

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    delimiter = "\t" if text.count("\t") > text.count(",") else ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    empty = {"columns": {}, "rows": [], "encoding": encoding, "preamble": [], "unit_of_field": {}}
    if not rows:
        return empty

    header_index = 0
    for i, row in enumerate(rows[:8]):
        if sum(1 for cell in row if field_of(cell)) >= 2:
            header_index = i
            break

    columns, unit_of_field = {}, {}
    for i, cell in enumerate(rows[header_index]):
        field = field_of(cell)
        if field and field not in columns.values():
            columns[i] = field
            unit_of_field[field] = unit_of(cell)

    return {
        "columns": columns,
        "rows": rows[header_index + 1 :],
        "encoding": encoding,
        "preamble": [" ".join(r) for r in rows[:header_index]],
        "unit_of_field": unit_of_field,
    }


def pick(columns, row):
    """行を {項目名: 値} に畳む。列数が足りない行でも落ちないようにする。"""
    out = {}
    for index, field in columns.items():
        out[field] = row[index] if index < len(row) else ""
    return out
