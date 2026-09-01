"""入口①の自動化 ── 置き場を見張って、規約どおりのものだけを取り込む。

  python castle/app/intake.py            置き場を一巡して取り込み、画面まで作り直す
  python castle/app/intake.py --dry-run  取り込まずに、何をするつもりかだけ言う
  python castle/app/intake.py --freshness  どのサイロがどこまで届いているかを見る

ベンダーが所定の名前で所定の場所に吐く ── そこまでは交渉ごとで、機械の仕事ではない。
機械の仕事は、**置かれたものを人の判断なしに取り込み、置かれなかったことに気づく**こと。

設計の勘どころは3つ。

**推測しない。** 受入仕様（config.json の sources）に書いた名前だけを取り込む。
名前から種別を当てにいくと、間違った種別のまま骨に入る。骨は共通なので入ってしまい、
画面に出てから気づくことになる。規約に無いものは保留へ退けて、人に渡す。

**同じものを二度取り込まない。判定は名前ではなく中身。** 中身のSHA-256で見る。
名前で見ると、ベンダーが同名で訂正版を送り直したときに取りこぼす ── 現場では普通に起きる。

**1本の不良で全部を止めない。** 壊れた1本だけ保留へ退け、他は進める。
止めてしまうと、その日は画面が丸ごと古いままになる。

そして自動にするほど「今日も動いたはず」と思い込む。だから freshness() を画面に出す。
**静かに古いデータで画面が出るのが、いちばん悪い。**
"""

import argparse
import datetime
import fnmatch
import hashlib
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import config as config_mod
import db
import import_csv

TAKEN_DIR = "取込済"
HELD_DIR = "保留"


def specs(cfg):
    return (cfg.sources or {}).get("受入") or []


def spec_of(cfg, name):
    """受入仕様のどの行に当たるか。当たらなければ None（＝推測しない）。"""
    for spec in specs(cfg):
        if fnmatch.fnmatch(name, spec["pattern"]):
            return spec
    return None


def classify(cfg, name):
    spec = spec_of(cfg, name)
    return spec["kind"] if spec else None


def fingerprint(path):
    """中身の指紋。名前ではなくこれで「同じもの」を判定する。"""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def seen_before(conn, digest):
    row = conn.execute(
        "SELECT id FROM import_log WHERE fingerprint=? AND undone_at IS NULL LIMIT 1",
        (digest,)).fetchone()
    return row["id"] if row else None


def _park(path, folder, stamp):
    """置き場から退ける。取り込んだものは月ごとに、保留はそのまま人が見る場所へ。"""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / path.name
    if target.exists():                       # 同名が既にあれば時刻を足す（消さない）
        target = folder / ("%s_%s%s" % (path.stem, stamp, path.suffix))
    shutil.move(str(path), str(target))
    return target


def run(conn, cfg, incoming, now=None, dry_run=False):
    """置き場を一巡する。**取り込めたものだけを退避し、残りは人に渡す。**"""
    incoming = pathlib.Path(incoming)
    now = now or datetime.datetime.now().isoformat(timespec="seconds")
    stamp = now.replace("-", "").replace(":", "").replace("T", "")[:14]
    result = {"taken": [], "same": [], "held": [], "unknown": [], "dry_run": dry_run}

    for path in sorted(p for p in incoming.glob("*") if p.is_file()):
        spec = spec_of(cfg, path.name)
        if spec is None:
            # 黙って捨てない。**捨てると、届いていたのに使われていないことに誰も気づけない。**
            result["unknown"].append(path.name)
            if not dry_run:
                _park(path, incoming / HELD_DIR, stamp)
            continue

        digest = fingerprint(path)
        before = seen_before(conn, digest)
        if before is not None:
            result["same"].append((path.name, before))
            if not dry_run:
                _park(path, incoming / TAKEN_DIR / now[:7], stamp)
            continue

        if dry_run:
            result["taken"].append((path.name, spec["kind"], 0))
            continue

        cursor = conn.execute(
            "INSERT INTO import_log(ran_at, source, kind, encoding, rows_ok, rows_skip,"
            " note, fingerprint) VALUES(?,?,?,?,0,0,?,?)",
            (now, path.name, spec["kind"], "?", "見張りが取り込み", digest))
        batch_id = cursor.lastrowid
        try:
            got = import_csv.import_file(conn, cfg, path, spec["kind"], batch_id)
        except (Exception, SystemExit) as failure:         # noqa: BLE001
            # SystemExit も拾う。取り込み側は「列が無い」を SystemExit で投げるが、
            # それは Exception を継承しないので、素通りして見張りごと落ちる
            # ── 2026-09-01、壊れた1本を実際に投げてみて見つけた。
            # 1本の不良で全部を止めない。**その日の画面が丸ごと古くなるほうが害が大きい。**
            conn.execute("DELETE FROM records WHERE batch_id=?", (batch_id,))
            conn.execute("DELETE FROM import_log WHERE id=?", (batch_id,))
            conn.commit()
            result["held"].append((path.name, "%s: %s" % (type(failure).__name__, failure)))
            _park(path, incoming / HELD_DIR, stamp)
            continue

        skipped = sum(sum(v.values()) for v in got["skipped"].values())
        if got["ok"] == 0:
            # 1行も入らなかったものを「取り込んだ」と数えない ── 形は合っていても中身が違う。
            conn.execute("DELETE FROM import_log WHERE id=?", (batch_id,))
            conn.commit()
            result["held"].append((path.name, "1行も取り込めませんでした（スキップ %d件）" % skipped))
            _park(path, incoming / HELD_DIR, stamp)
            continue

        conn.execute("UPDATE import_log SET encoding=?, rows_ok=?, rows_skip=? WHERE id=?",
                     (got["encoding"], got["ok"], skipped, batch_id))
        conn.commit()
        result["taken"].append((path.name, spec["kind"], got["ok"]))
        _park(path, incoming / TAKEN_DIR / now[:7], stamp)

    return result


def exit_code(result):
    """保留があれば0以外。**タスクスケジューラが「失敗」と分かる形にする。**"""
    return 1 if (result["held"] or result["unknown"]) else 0


def summary(result):
    """1行で言う。**何も来ていなくても「0本」と言う** ── 沈黙を成功と読ませない。"""
    rows = sum(count for _n, _k, count in result["taken"])
    text = "取り込み %d本（%d件）／ 同じ中身のため見送り %d本" % (
        len(result["taken"]), rows, len(result["same"]))
    if result["held"] or result["unknown"]:
        text += " ／ **保留 %d本**" % (len(result["held"]) + len(result["unknown"]))
    return text


# ── 届いているか ────────────────────────────────────────────

def _month_floor(month, back):
    year, mon = int(month[:4]), int(month[5:7])
    total = year * 12 + (mon - 1) - back
    return "%04d-%02d" % (total // 12, total % 12 + 1)


def freshness(conn, cfg, today=None):
    """サイロごとに、どこまで届いているか。**遅れていたら名指しする。**

    日次・週次は日数で、月次は月で測る。会計は翌月10日ごろに締まるので、
    日数で測ると締め直前が必ず遅れ扱いになり、毎月かならず赤が出る
    ── 毎回出る警告は読まれなくなる。
    """
    source = cfg.sources or {}
    today = today or source.get("基準日") or datetime.date.today().isoformat()
    day = datetime.date.fromisoformat(today)
    close = int(source.get("会計の締め日", 10)) + int(source.get("締め後の猶予日数", 5))

    last_of = {}
    for row in conn.execute(
            "SELECT kind, MAX(occurred_at) AS m FROM records"
            " WHERE created_by LIKE 'import/%' GROUP BY kind"):
        last_of[row["kind"]] = row["m"]

    out = []
    for spec in specs(cfg):
        last = last_of.get(spec["kind"])
        item = {"silo": spec["silo"], "kind": spec["kind"], "cycle": spec["cycle"],
                "last": last or "—", "behind": 0, "late": last is None, "expect": ""}
        if last:
            if spec["cycle"] == "月次":
                # 締め日＋猶予を過ぎていれば前月ぶんが、まだなら前々月ぶんが来ていればよい
                want = _month_floor(today, 1 if day.day >= close else 2)
                item["expect"] = "%s ぶんまで" % want
                item["late"] = last[:7] < want
                item["behind"] = (day - datetime.date.fromisoformat(last)).days
            else:
                allow = int(spec.get("allow_days", 4))
                item["expect"] = "%d日以内" % allow
                item["behind"] = (day - datetime.date.fromisoformat(last)).days
                item["late"] = item["behind"] > allow
        out.append(item)
    return out


def main():
    parser = argparse.ArgumentParser(description="置き場を見張って、規約どおりのCSVを取り込む")
    parser.add_argument("--instance", help="データ側のディレクトリ（既定: 城/instance）")
    parser.add_argument("--dry-run", action="store_true", help="取り込まず、何をするかだけ言う")
    parser.add_argument("--freshness", action="store_true", help="届き具合だけ見る")
    parser.add_argument("--no-build", action="store_true", help="画面を作り直さない")
    args = parser.parse_args()

    instance = db.instance_dir(args.instance)
    cfg = config_mod.load(instance)
    conn = db.connect(instance)

    if args.freshness:
        for f in freshness(conn, cfg):
            mark = "遅れ" if f["late"] else "正常"
            print("  %-4s %-12s %-8s 最終 %s（%d日前 ／ %s）"
                  % (mark, f["silo"], f["kind"], f["last"], f["behind"], f["expect"]))
        return 0

    incoming = instance / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    result = run(conn, cfg, incoming, dry_run=args.dry_run)
    print(("[下見] " if args.dry_run else "") + summary(result))
    for name, kind, count in result["taken"]:
        print("  取り込み  %-40s %-8s %d件" % (name, kind, count))
    for name, before in result["same"]:
        print("  見送り    %-40s 同じ中身をバッチ #%d で取り込み済み" % (name, before))
    for name, reason in result["held"]:
        print("  保留      %-40s %s" % (name, reason))
    for name in result["unknown"]:
        print("  保留      %-40s 受入仕様にない名前です" % name)
    if result["held"] or result["unknown"]:
        print("\n保留したものは %s に置いてあります。人が見てください。" % (incoming / HELD_DIR))

    late = [f for f in freshness(conn, cfg) if f["late"]]
    if late:
        print("\n届いていないものがあります:")
        for f in late:
            print("  %s ／ %s ── 最終 %s（%d日前。%s のはず）"
                  % (f["silo"], f["kind"], f["last"], f["behind"], f["expect"]))

    if result["taken"] and not args.dry_run and not args.no_build:
        import build_dashboard
        build_dashboard.build(instance)
        print("\n画面を作り直しました。")

    return exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
