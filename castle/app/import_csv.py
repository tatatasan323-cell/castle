"""入口①：既にあるもの（ベンダーのCSV出力）を骨に載せる。

  python castle/app/import_csv.py --kind 売上     instance/incoming/A社*.csv
  python castle/app/import_csv.py --kind 労働時間 instance/incoming/B社*.csv
  python castle/app/import_csv.py --list
  python castle/app/import_csv.py --undo 3

取り込みは取り消せる操作なので自動でよい。ただし取り消せることを実際に成立させるため、
バッチIDを記録し --undo を用意する。取り消せない操作をこのツールは持たない。
"""

import argparse
import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import config as config_mod
import db
from normalize import parse_date, parse_number, parse_period, pick, read_table

# 種類ごとに違うのは「どの列を値として見るか」だけ。骨は共通なので分岐はここに閉じる。
# fields は {列の項目名: (肉に入れるキー, 変換)}。
# 会計の「売上高」は列としては売上と同じ項目だが、肉には sales として入れる ──
# amount のまま入れると、原価のレコードが売上のレコードに化ける。
# monthly=True は「行に日付が無く、見出し前の行に年月がある」形（会計の月次表）。
KINDS = {
    "売上": {"fields": {"amount": ("amount", int)}, "by": "A社販売管理"},
    "労働時間": {"fields": {"hours": ("hours", lambda v: round(v, 2))}, "by": "B社勤怠"},
    # 会計の部門別損益。売上原価だけでなく、人件費とその他販管費まで1行で受ける。
    # 「売上原価」ではなく「部門損益」と呼ぶ ── 中身がそうだから。
    "部門損益": {"fields": {"amount": ("sales", int), "cost": ("cost", int),
                          "labor": ("labor", int), "sga": ("sga", int)},
                "by": "C社会計", "monthly": True},
    # 仕入は売上と同じ販売管理から出る。日次・部門別。
    "仕入": {"fields": {"buy": ("amount", int)}, "by": "A社販売管理"},
    # ここから下は部門に紐づかない ── 営業外・特別・税金・残高は全社のもの。
    # 部門の対応表を引かず、科目をそのまま主語にする（free_subject）。
    "試算表": {"fields": {"amount": ("amount", int)},
             "by": "C社会計", "monthly": True, "free_subject": True},
    "残高": {"fields": {"amount": ("amount", int)},
            "by": "C社会計", "monthly": True, "free_subject": True},
}


def import_file(conn, cfg, path, kind, batch_id):
    spec = KINDS[kind]
    table = read_table(path)
    columns, rows = table["columns"], table["rows"]
    present = set(columns.values())
    units = table["unit_of_field"]

    free = spec.get("free_subject", False)
    needed = list(spec["fields"]) + ([] if spec.get("monthly") else ["date"])
    if free:
        needed.append("account")
    if [f for f in needed if f not in present] or not (free or ({"dept", "code"} & present)):
        raise SystemExit(
            "%s: 必要な列が見つかりません（要る: %s ／ 見つかった: %s）"
            % (path.name, "、".join(needed), "、".join(sorted(present)) or "なし")
        )

    period = None
    if spec.get("monthly"):
        for line in table["preamble"]:
            period = parse_period(line)
            if period:
                break
        if period is None:
            raise SystemExit(
                "%s: 年月が分かりません。行に日付の列が無く、見出し前の行にも年月が書かれていません。" % path.name
            )

    now = datetime.datetime.now().isoformat(timespec="seconds")
    skipped = {}
    ok = 0
    dates = []

    for row in rows:
        cell = pick(columns, row)
        occurred_at = (period + "-01") if period else parse_date(cell.get("date", ""))

        if free:
            subject = (cell.get("account") or "").strip()
            if not subject:
                skipped.setdefault("科目が空", {}).setdefault("（空欄）", 0)
                skipped["科目が空"]["（空欄）"] += 1
                continue
        else:
            dept = cfg.resolve(cell.get("code", ""), cell.get("dept", ""))
            if dept is None:
                label = (cell.get("dept") or cell.get("code") or "（空欄）").strip()
                skipped.setdefault("部門が対応表にない", {}).setdefault(label, 0)
                skipped["部門が対応表にない"][label] += 1
                continue
            subject = dept["name"]
        if occurred_at is None:
            skipped.setdefault("日付が読めない", {}).setdefault(cell.get("date", "").strip() or "（空欄）", 0)
            skipped["日付が読めない"][cell.get("date", "").strip() or "（空欄）"] += 1
            continue

        values, unreadable = {}, None
        for field, (key, cast) in spec["fields"].items():
            raw = parse_number(cell.get(field, ""))
            if raw is None or raw < 0:
                unreadable = cell.get(field, "").strip() or "（空欄）"
                break
            values[key] = cast(raw * units.get(field, 1))
        if unreadable is not None:
            skipped.setdefault("数値が読めない", {}).setdefault(unreadable, 0)
            skipped["数値が読めない"][unreadable] += 1
            continue

        body = dict(values)
        body.update({
            "_key": "%s|%s|%s" % (kind, occurred_at, subject),
            "_batch": batch_id,
            "source": path.name,
        })
        if period:
            body["period"] = period
        conn.execute(
            """INSERT INTO records(kind, occurred_at, subject, status, created_by, updated_at, body)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(source_key) DO UPDATE SET
                 body=excluded.body, updated_at=excluded.updated_at, created_by=excluded.created_by""",
            (kind, occurred_at, subject, "confirmed", "import/" + spec["by"], now, json.dumps(body, ensure_ascii=False)),
        )
        ok += 1
        dates.append(occurred_at)

    return {"encoding": table["encoding"], "ok": ok, "skipped": skipped, "dates": dates}


def report(path, kind, result, batch_id):
    print("\n%s" % path.name)
    print("  文字コード: %s ／ 種類: %s" % (result["encoding"], kind))
    span = "（%s 〜 %s）" % (min(result["dates"]), max(result["dates"])) if result["dates"] else ""
    print("  取り込み: %d件 %s" % (result["ok"], span))
    total_skip = sum(sum(v.values()) for v in result["skipped"].values())
    print("  スキップ: %d件" % total_skip)
    for reason, items in result["skipped"].items():
        for label, count in sorted(items.items(), key=lambda kv: -kv[1]):
            print("    - %s: %s（%d件）" % (reason, label, count))
    print("  バッチ #%d ／ 取り消し: --undo %d" % (batch_id, batch_id))


def cmd_list(conn):
    rows = conn.execute("SELECT * FROM import_log ORDER BY id").fetchall()
    if not rows:
        print("取り込み履歴はありません。")
        return
    print("ID  実行時刻             種類      件数  スキップ  元ファイル")
    for r in rows:
        mark = "  ← 取消済 %s" % r["undone_at"][:16] if r["undone_at"] else ""
        print("%-3d %-20s %-9s %5d %9d  %s%s" % (r["id"], r["ran_at"][:19], r["kind"], r["rows_ok"], r["rows_skip"], r["source"], mark))


def cmd_undo(conn, batch_id):
    row = conn.execute("SELECT * FROM import_log WHERE id=?", (batch_id,)).fetchone()
    if row is None:
        raise SystemExit("バッチ #%d はありません。" % batch_id)
    if row["undone_at"]:
        raise SystemExit("バッチ #%d は既に取り消し済みです。" % batch_id)
    deleted = conn.execute("DELETE FROM records WHERE batch_id=?", (batch_id,)).rowcount
    conn.execute(
        "UPDATE import_log SET undone_at=? WHERE id=?",
        (datetime.datetime.now().isoformat(timespec="seconds"), batch_id),
    )
    conn.commit()
    print("バッチ #%d（%s）を取り消しました。%d件を削除。" % (batch_id, row["source"], deleted))
    # 履歴は持たない。同じ自然キーを上書きしていた場合、前の値には戻らず消える。
    print("※ 上書きで取り込んだぶんも消えます。戻すには元のCSVを取り込み直してください。")


def main():
    parser = argparse.ArgumentParser(description="ベンダーのCSVを城の骨に取り込む")
    parser.add_argument("files", nargs="*", type=pathlib.Path)
    parser.add_argument("--kind", choices=sorted(KINDS))
    parser.add_argument("--instance", help="データ側のディレクトリ（既定: 城/instance）")
    parser.add_argument("--list", action="store_true", help="取り込み履歴を表示")
    parser.add_argument("--undo", type=int, metavar="ID", help="指定バッチの取り込みを取り消す")
    args = parser.parse_args()

    instance = db.instance_dir(args.instance)
    conn = db.connect(instance)
    cfg = config_mod.load(instance)

    if args.list:
        return cmd_list(conn)
    if args.undo is not None:
        return cmd_undo(conn, args.undo)
    if not args.files or not args.kind:
        raise SystemExit("--kind と対象ファイルを指定してください。")

    now = datetime.datetime.now().isoformat(timespec="seconds")
    for path in args.files:
        if not path.exists():
            raise SystemExit("ファイルがありません: %s" % path)
        cursor = conn.execute(
            "INSERT INTO import_log(ran_at, source, kind, encoding, rows_ok, rows_skip) VALUES(?,?,?,?,0,0)",
            (now, path.name, args.kind, "?"),
        )
        batch_id = cursor.lastrowid
        result = import_file(conn, cfg, path, args.kind, batch_id)
        conn.execute(
            "UPDATE import_log SET encoding=?, rows_ok=?, rows_skip=? WHERE id=?",
            (result["encoding"], result["ok"], sum(sum(v.values()) for v in result["skipped"].values()), batch_id),
        )
        conn.commit()
        report(path, args.kind, result, batch_id)

    total = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    print("\n骨に載っているレコード: %d件" % total)


if __name__ == "__main__":
    main()
