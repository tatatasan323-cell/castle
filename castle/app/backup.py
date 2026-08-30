"""バックアップと復旧。

  python castle/app/backup.py                  控えを取る（既定）
  python castle/app/backup.py --list
  python castle/app/backup.py --restore <ファイル> --yes
  python castle/app/backup.py --prune 14 --yes

**ファイルコピーで済ませない。** SQLiteはWALで動いているので、コミット済みでもまだ
data.db 本体に書かれていない行がある。`data.db` を横からコピーすると、そこを取りこぼす。
標準の `Connection.backup()` を使えば、サーバが動いたままでも整合した控えが取れる。

**検証していない控えは、控えではない。** 取った直後に開き直して、
整合性検査とレコード件数を確かめてから「取れた」と言う。

**復旧も取り消せるようにする。** 戻す直前に、いまのDBを自動で退避する。
間違った控えを戻しても、そこから戻れる。
削除（--prune）と復旧（--restore）は `--yes` が無ければ実行しない ── 人が押す側の操作。
"""

import argparse
import datetime
import pathlib
import shutil
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import config as config_mod
import db

DIRNAME = "backups"
PREFIX = "data-"


def folder(instance):
    path = pathlib.Path(instance) / DIRNAME
    path.mkdir(exist_ok=True)
    return path


def listing(instance):
    return sorted(folder(instance).glob(PREFIX + "*.db"))


def _stamp(tag=""):
    now = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return "%s%s%s.db" % (PREFIX, now, ("-" + tag) if tag else "")


def create(instance, tag=""):
    """控えを取り、その場で開き直して確かめる。戻り値に検証結果を含める。"""
    instance = pathlib.Path(instance)
    source = sqlite3.connect(instance / "data.db")
    expected = source.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    path = folder(instance) / _stamp(tag)
    target = sqlite3.connect(path)
    with target:
        source.backup(target)
    target.close()
    source.close()

    audit = sqlite3.connect(path)
    integrity = audit.execute("PRAGMA integrity_check").fetchone()[0]
    records = audit.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    audit.close()

    return {"path": path, "integrity": integrity, "records": records,
            "expected": expected, "size": path.stat().st_size,
            "ok": integrity == "ok" and records == expected}


def mirror_targets(cfg):
    """複製先。設定に書かれていなければ空 ── 黙って1箇所だけに置かない。"""
    raw = (getattr(cfg, "backup", None) or {}).get("mirror") or []
    return [pathlib.Path(x) for x in ([raw] if isinstance(raw, str) else raw)]


def mirror(result, targets):
    """控えを別のディスクへ写し、写した先でも開き直して確かめる。

    同じディスクの上にしか控えが無いなら、それは控えではない ──
    ディスクが飛べば本体と一緒に飛ぶ。だから写す。
    そして**写した先で開き直す**。コピーできたことと、読めることは別。
    """
    out = []
    for target in targets:
        row = {"target": target, "ok": False, "reason": ""}
        try:
            target.mkdir(parents=True, exist_ok=True)
            copy = target / result["path"].name
            shutil.copy2(result["path"], copy)
            audit = sqlite3.connect(copy)
            integrity = audit.execute("PRAGMA integrity_check").fetchone()[0]
            records = audit.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            audit.close()
            row.update(ok=(integrity == "ok" and records == result["expected"]),
                       path=copy, records=records,
                       reason="" if integrity == "ok" else integrity)
            if row["ok"] is False and not row["reason"]:
                row["reason"] = "件数が合わない（%d ≠ %d）" % (records, result["expected"])
        except OSError as exc:
            row["reason"] = "%s: %s" % (type(exc).__name__, exc)
        out.append(row)
    return out


def restore(instance, path, yes=False):
    """控えから戻す。戻す前に、いまのDBを自動で退避する。"""
    instance = pathlib.Path(instance)
    path = pathlib.Path(path)
    if not path.exists():
        return {"done": False, "message": "控えが見つかりません: %s" % path}
    if not yes:
        return {"done": False,
                "message": "%s を data.db に戻します。取り消せない操作なので、"
                           "実行するには --yes を付けてください。" % path.name}

    safety = create(instance, tag="復旧前")
    source = sqlite3.connect(path)
    target = sqlite3.connect(instance / "data.db")
    with target:
        source.backup(target)
    records = target.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
    target.close()
    source.close()

    return {"done": True, "records": records, "integrity": integrity, "safety": safety["path"],
            "message": "%s から戻しました（%d件・整合性 %s）。直前のDBは %s に退避してあります。"
                       % (path.name, records, integrity, safety["path"].name)}


def prune(instance, keep=14, yes=False):
    """古い控えを消す。削除は人が押す側なので、--yes が無ければ数えるだけ。"""
    files = listing(instance)
    old = files[:-keep] if keep > 0 else files
    if not old:
        return {"deleted": 0, "message": "控え %d件。消す対象はありません（%d件を残す設定）。" % (len(files), keep)}
    if not yes:
        return {"deleted": 0,
                "message": "控え %d件のうち %d件が対象です（%s 〜 %s）。消すには --yes を付けてください。"
                           % (len(files), len(old), old[0].name, old[-1].name)}
    for path in old:
        path.unlink()
    return {"deleted": len(old), "message": "%d件を削除しました。残り %d件。" % (len(old), len(files) - len(old))}


def main():
    parser = argparse.ArgumentParser(description="城のバックアップと復旧")
    parser.add_argument("--instance")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--restore", metavar="ファイル")
    parser.add_argument("--prune", type=int, metavar="残す数")
    parser.add_argument("--yes", action="store_true", help="復旧・削除を実際に実行する")
    parser.add_argument("--mirror", action="append", metavar="複製先",
                        help="別ディスクへ写す場所。設定より優先する")
    args = parser.parse_args()
    instance = db.instance_dir(args.instance)

    if args.list:
        files = listing(instance)
        if not files:
            print("控えはまだありません。")
            return
        print("控え %d件（%s）" % (len(files), folder(instance)))
        for path in files:
            audit = sqlite3.connect(path)
            try:
                count = audit.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            except sqlite3.DatabaseError:
                count = -1
            audit.close()
            print("  %-34s %8.1f KB  %s"
                  % (path.name, path.stat().st_size / 1024,
                     "%d件" % count if count >= 0 else "※読めません"))
        return

    if args.restore:
        result = restore(instance, args.restore, args.yes)
        print(result["message"])
        return

    if args.prune is not None:
        print(prune(instance, args.prune, args.yes)["message"])
        return

    made = create(instance)
    print("控えを取りました: %s" % made["path"])
    print("  %.1f KB ／ %d件 ／ 整合性 %s" % (made["size"] / 1024, made["records"], made["integrity"]))
    if not made["ok"]:
        raise SystemExit("原本は %d件でした。一致していません。この控えは使わないでください。" % made["expected"])
    print("  原本と一致しました。控え %d件目。" % len(listing(instance)))

    # 同じディスクの上にしか控えが無いなら、それは控えではない。
    targets = [pathlib.Path(x) for x in (args.mirror or [])] or mirror_targets(
        config_mod.load(instance))
    if not targets:
        print("  ※ 複製先が設定されていません。この控えは原本と同じディスクの上にしかありません。")
        print("     instance/config.json の backup.mirror に別ディスクの場所を書いてください。")
        return
    failed = []
    for row in mirror(made, targets):
        if row["ok"]:
            print("  複製しました: %s（%d件・開き直して確認済み）" % (row["path"], row["records"]))
        else:
            failed.append("%s ── %s" % (row["target"], row["reason"]))
            print("  ※ 複製できませんでした: %s ── %s" % (row["target"], row["reason"]))
    if failed:
        raise SystemExit("複製に失敗しています。控えは1箇所にしかありません。")


if __name__ == "__main__":
    main()
