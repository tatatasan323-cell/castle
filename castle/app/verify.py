#!/usr/bin/env python3
"""判定者。ループを回す前にこれを書く ── ループが収束するかは、ここが機械かどうかで決まる。

  python castle/app/verify.py

instance/data.db を作り直し、CSVを全部取り込み、ダッシュボードを生成して、
「機械が正誤を言えること」だけを検査する。人の判断が要ることは検査しない（できない）。

終了コード 0=全部通った / 1=落ちたものがある。
"""

import datetime
import json
import re
import os
import pathlib
import subprocess
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import config as config_mod
import db

PY = sys.executable
APP = db.ROOT / "castle" / "app"

RESULTS = []


def yen_(v):
    return format(int(v), ",") + "円"


def check(name, ok, detail="", why=""):
    """name＝何を見たか、detail＝測った値（いつでも出す）、why＝落ちた時だけ言うこと。

    落ちた時の説明を通った時にも出すと、通っているのに事故に見える
    （「OK 足し算が合っている ── 37.7 + 10.3 − 31.5 ≠ 16.6」）。
    **判定ログもまた、人が読む成果物。**
    """
    text = detail if ok else "  ".join(x for x in (detail, why) if x)
    RESULTS.append((ok, name, text))
    print("  %s %s%s" % ("OK  " if ok else "NG  ", name, ("  ── " + text) if text else ""))
    return ok


def run_shell(command):
    proc = subprocess.run(command, shell=True, capture_output=True)
    return proc.returncode, (proc.stdout or b'').decode('utf-8', 'replace')


def shutil_rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def run(*args):
    proc = subprocess.run([PY, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _run(instance):
    cfg = config_mod.load(instance)
    incoming = instance / "incoming"

    print("\n【1】作り直し（再現性も同時に見る）")
    try:
        for suffix in ("", "-wal", "-shm"):
            (instance / ("data.db" + suffix)).unlink(missing_ok=True)
    except PermissionError:
        # serve.py が動いているとDBを掴んでいて消せない。実運用でも起きるので、原因を名指しする。
        raise SystemExit(
            "data.db を作り直せません。serve.py が動いていませんか？\n"
            "  サーバを止めてから、もう一度 verify.py を実行してください。")

    # **作り直しも、本番と同じ自動の道を通す。** 種別を判定者が指定して取り込むと、
    # 「人が選べば動く」ことしか確かめられない。見張りが選べることを、ここで見る。
    # 見張りは取り込んだファイルを退避するので、CSVは毎回作り直してから渡す。
    code, out = run(instance / "make_sample.py")
    check("サイロのCSVを作り直せる", code == 0,
          "" if code == 0 else out.strip().splitlines()[-1][:120])
    for folder in ("取込済", "保留"):
        shutil_rmtree(incoming / folder)

    code, out = run(APP / "intake.py", "--no-build")
    check("見張りが置き場を一巡して取り込む", code == 0,
          out.strip().splitlines()[0][:150] if out.strip() else "")
    check("置き場が空になる（取り込んだものは退避される）",
          not list(incoming.glob("*.csv")),
          "残り %d本" % len(list(incoming.glob("*.csv"))))
    check("退避先に全部そろっている",
          len(list((incoming / "取込済").rglob("*.csv"))) >= 50,
          "%d本" % len(list((incoming / "取込済").rglob("*.csv"))))
    check("保留は出ていない", not list((incoming / "保留").glob("*")),
          "保留 %s" % [p.name for p in (incoming / "保留").glob("*")][:3])

    kinds = {r[0] for r in sqlite3.connect(instance / "data.db").execute(
        "SELECT DISTINCT kind FROM records")}
    for kind in ("売上", "仕入", "在庫", "労働時間", "部門損益", "試算表", "残高"):
        check("%s が骨に入っている" % kind, kind in kinds)

    # 申し送りと知識はCSVから来ない。だがデモの一部なので、作り直しに含める。
    # 「数字と現場の一行が同じ画面に並ぶ」が城の目的地であり、
    # それが無い状態の画面は、デモとして成立しない。
    code, out = run(instance / "seed_demo.py")
    check("デモの種（申し送り・知識）が入る", code == 0,
          "" if code == 0 else out.strip().splitlines()[-1][:120])

    conn = sqlite3.connect(instance / "data.db")
    conn.row_factory = sqlite3.Row
    measured = [d["name"] for d in cfg.measured()]

    print("\n【2】記録の形")
    rows = conn.execute(
        "SELECT subject, occurred_at, json_extract(body,'$.cost') c, json_extract(body,'$.sales') s "
        "FROM records WHERE kind='部門損益'").fetchall()
    months = sorted({r["occurred_at"] for r in rows})
    check("部門損益が 10部門 × 月数 で入っている", bool(rows) and len(rows) == len(cfg.departments) * len(months),
          "%d件（部門%d × 月%d）" % (len(rows), len({r["subject"] for r in rows}), len(months)))
    check("当月（未確定）は入っていない", bool(months) and "2026-08-01" not in months, "確定月: %s" % ", ".join(m[:7] for m in months))

    print("\n【3】単位（千円のまま入れると1000分の1になる）")
    # 間接部門は売上ゼロが正しいので、範囲を見るのは営業部門だけ
    bad_unit = [r for r in rows if r["subject"] in measured
                and not (100_000_000 <= (r["s"] or 0) <= 2_000_000_000)]
    check("月次売上が 1.0億〜20億円の範囲", bool(rows) and not bad_unit,
          "外れ %d件 例:%s %s" % (len(bad_unit), bad_unit[0]["subject"], f"{bad_unit[0]['s']:,}") if bad_unit else "")

    print("\n【4】原価率")
    rates = {}
    for r in rows:
        if r["subject"] not in measured:
            continue
        rate = (r["c"] or 0) / (r["s"] or 1)
        rates.setdefault(r["subject"], {})[r["occurred_at"][:7]] = rate
    flat = [v for m in rates.values() for v in m.values()]
    check("全件が 0.70〜0.98 に収まる", flat and all(0.70 <= v <= 0.98 for v in flat),
          "最小 %.3f / 最大 %.3f" % (min(flat), max(flat)) if flat else "0件")
    nosan = rates.get("農産部", {})
    check("農産部の原価率が月を追って上がっている", len(nosan) >= 2 and nosan.get("2026-06", 9) < nosan.get("2026-07", 0),
          " → ".join("%s %.1f%%" % (m, v * 100) for m, v in sorted(nosan.items()) if m.startswith("2026")))

    print("\n【5】粗利が出せる")
    code, out = run(APP / "build_dashboard.py")
    check("ダッシュボードが生成できる", code == 0, "" if code == 0 else out.strip().splitlines()[-1][:160])

    summary_path = instance / "out" / "summary.json"
    data = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else None
    if not check("集計結果が summary.json に出ている", data is not None, "判定できる形で出力が要る"):
        return report()

    gross = {d: v["gross_pph"] for d, v in data["departments"].items()}
    sales = {d: v["sales_pph"] for d, v in data["departments"].items()}
    check("粗利/人時が全部門で正", all(v > 0 for v in gross.values()))
    check("粗利/人時 < 売上/人時", all(gross[d] < sales[d] for d in gross))
    order_g = [d for d, _ in sorted(gross.items(), key=lambda kv: -kv[1])]
    order_s = [d for d, _ in sorted(sales.items(), key=lambda kv: -kv[1])]
    check("粗利ベースと売上ベースで順位が変わる", order_g != order_s,
          "粗利1位=%s ／ 売上1位=%s" % (order_g[0], order_s[0]))

    print("\n【6】画面")
    html = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")
    import re
    check("未置換のテンプレート変数が無い", not re.search(r"\$\{?[a-z_]+\}?", html))
    check("空の折れ線が無い", 'points=""' not in html and html.count("<polyline") >= 8,
          "polyline %d本" % html.count("<polyline"))
    check("未確定月の扱いが画面に書いてある", "推定" in html, "「推定」の語で確認")
    # 同じ月の原価率を当てている2週を比べて「粗利率 ±0」と書くと、
    # 「粗利率は問題ない」と読ませてしまう。算術は正しいが、表示として誤り。
    check("動きようがない粗利率を ±0 と表示していない", "0.0pt" not in html)

    print("\n【7】入口②（フォーム）── これから発生するもの")
    try:
        import serve
    except Exception as exc:
        check("serve.py が読める", False, "%s: %s" % (type(exc).__name__, exc))
        return report()
    check("serve.py が読める", True)

    mark = conn.execute("SELECT COALESCE(MAX(id),0) FROM records").fetchone()[0]
    before = conn.execute("SELECT COUNT(*) FROM records WHERE kind='申し送り'").fetchone()[0]
    good = serve.add_note(conn, cfg, {"subject": "農産部", "occurred_at": "2026-08-12",
                                      "category": "相場・仕入価格", "author": "青果課 佐藤",
                                      "text": "北海道産の相場が高止まり。数量を絞って粗利を守っている。"})
    conn.commit()
    check("正しい申し送りを保存できる", good[0], good[1])
    after = conn.execute("SELECT COUNT(*) FROM records WHERE kind='申し送り'").fetchone()[0]
    check("骨に1件だけ増える", after == before + 1, "%d件 → %d件" % (before, after))

    ng_cases = [
        ("部門が対応表にない", {"subject": "存在しない部", "occurred_at": "2026-08-12",
                              "category": "相場・仕入価格", "author": "誰か", "text": "テスト"}),
        ("本文が空", {"subject": "農産部", "occurred_at": "2026-08-12",
                     "category": "相場・仕入価格", "author": "誰か", "text": "   "}),
        ("日付が読めない", {"subject": "農産部", "occurred_at": "きのう",
                          "category": "相場・仕入価格", "author": "誰か", "text": "テスト"}),
        ("区分が一覧にない", {"subject": "農産部", "occurred_at": "2026-08-12",
                            "category": "なんとなく", "author": "誰か", "text": "テスト"}),
    ]
    rejected = all(not serve.add_note(conn, cfg, payload)[0] for _, payload in ng_cases)
    check("不正な入力を4種類とも拒否する", rejected, "、".join(n for n, _ in ng_cases))
    conn.commit()

    # フォームがあるということは、入力が画面に出るということ。ここを抜かすと自分で穴を開ける。
    serve.add_note(conn, cfg, {"subject": "水産部", "occurred_at": "2026-08-12",
                               "category": "その他", "author": "<script>alert(1)</script>",
                               "text": "<img src=x onerror=alert(2)> 危険な入力のテスト"})
    conn.commit()
    code, out = run(APP / "build_dashboard.py")
    check("申し送り入りでダッシュボードが生成できる", code == 0, "" if code == 0 else out.strip().splitlines()[-1][:160])
    html2 = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")
    # 見るのは「タグとして生きているか」。エスケープ後も onerror という文字列は残るが、それは無害。
    # 消えていないこと（&lt;script&gt; がある）も一緒に見る ── 黙って捨てる実装を通さないため。
    check("入力がタグとして生きていない",
          "<script" not in html2 and "<img" not in html2, "生の <script / <img がゼロ")
    check("入力が捨てられずエスケープされている",
          "&lt;script&gt;" in html2 and "&lt;img" in html2)
    check("申し送りが画面に出ている", "北海道産の相場が高止まり" in html2)

    form_html = serve.render_note_page(instance, conn, cfg)
    last = conn.execute("SELECT MAX(occurred_at) FROM records WHERE kind='売上'").fetchone()[0]
    # 既定日が「今日」だと、取り込みの遅れぶんだけ画面の外を指す。
    check("対象日の既定が、データのある最終日になっている", ('value="%s"' % last) in form_html, last)

    print("\n【8】薄いサーバ")
    import threading, urllib.error, urllib.parse, urllib.request
    srv = serve.make_server(instance, 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_address[1]
    try:
        for path, label in (("/", "ダッシュボード"), ("/note", "申し送りフォーム")):
            with urllib.request.urlopen(base + path, timeout=10) as res:
                check("GET %s が返る（%s）" % (path, label), res.status == 200, "HTTP %d" % res.status)

        n0 = conn.execute("SELECT COUNT(*) FROM records WHERE kind='申し送り'").fetchone()[0]
        form = urllib.parse.urlencode({"subject": "業務用食材部", "occurred_at": "2026-08-12",
                                       "category": "得意先の事情", "author": "業務用課 鈴木",
                                       "text": "盆前で休業の得意先が増え、1件あたりの配送効率が落ちている。"},
                                      encoding="utf-8").encode()
        req = urllib.request.Request(base + "/note", data=form, method="POST")
        with urllib.request.urlopen(req, timeout=10) as res:
            check("POST /note が受理される", res.status in (200, 303), "HTTP %d" % res.status)
        n1 = conn.execute("SELECT COUNT(*) FROM records WHERE kind='申し送り'").fetchone()[0]
        check("POST で骨に1件増える", n1 == n0 + 1, "%d件 → %d件" % (n0, n1))
    finally:
        srv.shutdown()
        srv.server_close()

    print("\n【9】認証と操作者の記録")
    try:
        import users
    except Exception as exc:
        check("users.py が読める", False, "%s: %s" % (type(exc).__name__, exc))
        return report()
    check("users.py が読める", True)

    store = instance / "users.json"
    backup = store.read_bytes() if store.exists() else None
    store.unlink(missing_ok=True)
    try:
        token = users.issue(instance, "検査用 太郎")
        check("トークンを発行できる", isinstance(token, str) and len(token) >= 32, "%d文字" % len(token))
        raw = store.read_text(encoding="utf-8")
        # 保管はハッシュだけ。ファイルが流出しても、それだけでは入れない。
        check("users.json に平文トークンが入っていない", token not in raw)
        check("正しいトークンで本人が引ける", users.resolve(instance, token) == "検査用 太郎")
        check("誤ったトークンは通らない",
              users.resolve(instance, "x" * len(token)) is None and users.resolve(instance, "") is None)

        srv2 = serve.make_server(instance, 0)
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        base2 = "http://127.0.0.1:%d" % srv2.server_address[1]
        try:
            import http.cookiejar
            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

            with opener.open(base2 + "/note", timeout=10) as res:
                landed = res.read().decode("utf-8", "replace")
            check("未認証で /note を開くとログイン画面になる",
                  "アクセスキー" in landed and "<textarea" not in landed)

            bad = urllib.parse.urlencode({"token": "y" * 40}).encode()
            try:
                with opener.open(urllib.request.Request(base2 + "/login", data=bad), timeout=10) as res:
                    body = res.read().decode("utf-8", "replace")
                    status = res.status
            except urllib.error.HTTPError as err:
                body, status = err.read().decode("utf-8", "replace"), err.code
            check("誤ったキーではログインできない", status == 401 and "アクセスキー" in body, "HTTP %d" % status)

            ok_form = urllib.parse.urlencode({"token": token}).encode()
            with opener.open(urllib.request.Request(base2 + "/login", data=ok_form), timeout=10) as res:
                check("正しいキーでログインできる", res.status == 200, "HTTP %d" % res.status)
            cookies = {c.name: c for c in jar}
            biscuit = cookies.get("castle_session")
            check("クッキーに鍵の文字列そのものが載っていない",
                  biscuit is not None and token not in biscuit.value,
                  "載っている" if biscuit and token in biscuit.value else "署名付きのセッション")
            check("クッキーの値がASCIIである（日本語の名前でも壊れない）",
                  biscuit is not None and biscuit.value.isascii())
            check("Cookie に HttpOnly と SameSite が付いている",
                  biscuit is not None
                  and biscuit.has_nonstandard_attr("HttpOnly")
                  and (biscuit.get_nonstandard_attr("SameSite") or "").lower() == "strict")

            n0 = conn.execute("SELECT COUNT(*) FROM records WHERE kind='申し送り'").fetchone()[0]
            post = urllib.parse.urlencode({"subject": "水産部", "occurred_at": "2026-08-12",
                                           "category": "天候・入荷", "author": "なりすまし 花子",
                                           "text": "認証後は created_by が自己申告ではなくなるかの確認。"},
                                          encoding="utf-8").encode()
            with opener.open(urllib.request.Request(base2 + "/note", data=post), timeout=10) as res:
                check("認証済みなら申し送りを書ける", res.status == 200, "HTTP %d" % res.status)
            n1 = conn.execute("SELECT COUNT(*) FROM records WHERE kind='申し送り'").fetchone()[0]
            who = conn.execute(
                "SELECT created_by FROM records WHERE kind='申し送り' ORDER BY id DESC LIMIT 1").fetchone()
            check("記録が1件増える", n1 == n0 + 1, "%d件 → %d件" % (n0, n1))
            # 認証を入れる意味はここ。名乗りではなく、鍵の持ち主が記録される。
            check("操作者が自己申告ではなく登録名になる",
                  who and who[0] == "検査用 太郎", "created_by = %s" % (who[0] if who else "—"))

            users.revoke(instance, "検査用 太郎")
            with opener.open(base2 + "/note", timeout=10) as res:
                after_revoke = res.read().decode("utf-8", "replace")
            check("失効させたキーは通らなくなる", "アクセスキー" in after_revoke)
        finally:
            srv2.shutdown()
            srv2.server_close()

        store.unlink(missing_ok=True)
        refused = serve.guard_exposure(instance, "0.0.0.0")
        check("利用者0人のまま外向きに開こうとしたら止める", refused is not None, str(refused or "")[:80])
        check("127.0.0.1 なら利用者0人でも動く", serve.guard_exposure(instance, "127.0.0.1") is None)
        users.issue(instance, "門番テスト")
        # 鍵が1本あるだけで外に出さない。打ち間違いやコピペで社内網に出るのを止める。
        check("利用者がいても --expose なしでは外に開かない",
              serve.guard_exposure(instance, "0.0.0.0") is not None)
        check("--expose を明示すれば開ける",
              serve.guard_exposure(instance, "0.0.0.0", True) is None)
    finally:
        store.unlink(missing_ok=True)
        if backup is not None:
            store.write_bytes(backup)

    print("\n【10】バックアップと復旧")
    try:
        import backup
    except Exception as exc:
        check("backup.py が読める", False, "%s: %s" % (type(exc).__name__, exc))
        return report()
    check("backup.py が読める", True)
    kept_before = set(backup.listing(instance))

    # 委細：WALでは、コミット済みでもまだ data.db 本体に書かれていない行がある。
    # data.db をファイルコピーしただけのバックアップは、この行を取りこぼす。
    live = sqlite3.connect(instance / "data.db")
    live.execute(
        "INSERT INTO records(kind,occurred_at,subject,status,created_by,updated_at,body)"
        " VALUES('検査用','2026-08-12','水産部','confirmed','検査',datetime('now'),'{}')")
    live.commit()
    expected = live.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    made = backup.create(instance)
    check("バックアップが作れる", made["path"].exists(), made["path"].name)
    check("整合性検査が通る", made["integrity"] == "ok", made["integrity"])
    check("WALに未反映の行も取りこぼさない", made["records"] == expected,
          "原本 %d件 / 控え %d件" % (expected, made["records"]))
    live.close()

    refused = backup.restore(instance, made["path"], yes=False)
    check("--yes なしでは復旧しない", refused["done"] is False, refused["message"][:60])

    # 復旧が正しいかを見るために、原本をわざと壊す
    live = sqlite3.connect(instance / "data.db")
    live.execute("DELETE FROM records WHERE kind='検査用'")
    live.commit()
    broken = live.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    live.close()
    check("壊した状態を作れた", broken == expected - 1, "%d件" % broken)

    before_files = set(backup.listing(instance))
    done = backup.restore(instance, made["path"], yes=True)
    check("復旧できる", done["done"], done["message"][:70])
    live = sqlite3.connect(instance / "data.db")
    check("中身が戻る", live.execute("SELECT COUNT(*) FROM records").fetchone()[0] == expected)
    live.close()
    # 復旧そのものを取り消せるようにする。これが無いと、間違った控えを戻した時点で終わる。
    check("復旧の直前に、いまのDBが自動で退避される",
          len(set(backup.listing(instance)) - before_files) == 1)

    kept = backup.prune(instance, keep=1, yes=False)
    check("--yes なしでは古い控えを消さない", kept["deleted"] == 0 and len(backup.listing(instance)) >= 2,
          kept["message"][:60])

    print("\n【13】経営者の一枚 ── 儲かっているか、着地はどうか")
    try:
        import pnl
    except Exception as exc:
        check("pnl.py が読める", False, "%s: %s" % (type(exc).__name__, exc))
        return report()
    check("pnl.py が読める", True)

    monthly = conn.execute(
        "SELECT subject, occurred_at, json_extract(body,'$.labor') l, json_extract(body,'$.sga') s "
        "FROM records WHERE kind='部門損益'").fetchall()
    check("部門損益が全10部門ぶん入っている（間接部門も）",
          bool(monthly) and len({r["subject"] for r in monthly}) == len(cfg.departments),
          "%d部門 × %d月" % (len({r["subject"] for r in monthly}), len({r["occurred_at"] for r in monthly})))
    check("間接部門にも人件費が載っている",
          bool(monthly) and all((r["l"] or 0) > 0 for r in monthly if r["subject"] == "物流センター"))

    book = pnl.build(conn, cfg)
    days = book["days"]
    check("日次の営業利益が組み立つ", bool(days), "%d日ぶん" % len(days))
    sample = days[-1] if days else {}
    # 人件費は勤怠（日次人時）× 会計（月次単価）で日次化する。ここが2サイロの掛け算。
    check("人件費が日次に落ちている", bool(days) and sample["labor"] > 0,
          "最終日 %s: 人件費 %s円" % (sample.get("date"), format(sample.get("labor", 0), ",.0f")))
    check("営業利益 = 売上 − 原価 − 人件費 − その他販管費",
          bool(days) and abs(sample["op"] - (sample["sales"] - sample["cost"] - sample["labor"] - sample["sga"])) < 1)

    month = book["month"]
    check("当月の実績累計が出る", month["actual_days"] > 0, "%d営業日ぶん" % month["actual_days"])
    check("残営業日を数えている", month["remaining_days"] >= 0, "残り %d日" % month["remaining_days"])
    # 着地は「実績＋見込み」。残りがあるなら実績を下回らない。
    check("着地見込みが実績累計以上", month["forecast_op"] >= month["actual_op"],
          "実績 %s → 着地 %s" % (format(month["actual_op"], ",.0f"), format(month["forecast_op"], ",.0f")))
    check("予算比が出る", month["vs_budget"] is not None,
          "%+.1f%%" % month["vs_budget"] if month["vs_budget"] is not None else "")
    check("前年同月比が出る", month["vs_last_year"] is not None,
          "%+.1f%%" % month["vs_last_year"] if month["vs_last_year"] is not None else "")
    check("累計の時系列がある（航海図の材料）",
          len(month["cumulative"]) == month["actual_days"] + month["remaining_days"],
          "%d点" % len(month["cumulative"]))
    check("前年同月の累計も並ぶ", any(v is not None for v in month["last_year_cumulative"]))

    code, out = run(APP / "build_dashboard.py")
    check("経営者の一枚が生成できる", code == 0, "" if code == 0 else out.strip().splitlines()[-1][:160])
    top = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")
    # 画面のいちばん上が着地見込みでないと、経営者の一枚にならない。
    # 文字数で測ると見た目を足すたびに壊れる。**順序**で測る。
    body_top = top[top.index("<body"):]
    check("画面の先頭が着地見込みになっている",
          body_top.index("着地見込み") < body_top.index("推移")
          and body_top.index("着地見込み") < body_top.index("何が伸びて"),
          "landing → 推移 → 部門別 の順")
    # どこまでが事実で、どこからが予想か。これが分からない着地見込みは危ない。
    block = top[top.index('class="landing"'):top.index("</section>", top.index('class="landing"'))]
    check("実績と見込みの日数が、着地の真下で分かれている",
          all(w in block for w in ("実績", "見込み", "営業日")),
          "landing 内で確認")
    check("航海図（累計の折れ線）がある", 'class="voyage' in top)
    # 外部ライブラリを入れないのと同じ理由でJavaScriptも入れない。
    # タブ切り替えもホバー強調も、CSSの :checked と :hover だけで作る。
    check("JavaScriptが1行も無い", "<script" not in top and "onclick" not in top)
    # この城の目的地 ── 経営陣も現場も、同じ画面を見て同じ判断ができる状態。
    check("数字と現場の一行が同じ画面に並んでいる",
          "数量減は意図的" in html and html.count('class="attached"') > 0,
          "申し送り %s ／ 知識の添付 %d箇所"
          % ("あり" if "数量減は意図的" in html else "無し", html.count('class="attached"')))
    check("推移の切り替えが4枚ある（今月の着地／年間の着地／部門別の額／部門別の率）",
          top.count('class="panel"') == 4, "CSSタブ %d枚" % top.count('class="panel"'))
    # タブは「切り替わって初めてタブ」。入口・見出し・中身の3つが揃っているかを見る。
    for tab in ("total", "year", "amount", "rate"):
        check("%s のタブが切り替わる" % tab,
              all(x in top for x in ('id="ch-%s"' % tab, 'label[for="ch-%s"]' % tab.replace("x", "x"),
                                     'id="p-%s"' % tab))
              or ('id="ch-%s"' % tab in top and 'id="p-%s"' % tab in top
                  and '#ch-%s:checked~#p-%s' % (tab, tab) in board),
              "入口・切替CSS・中身")
    check("部門別の線が部門数ぶん引かれている",
          top.count('class="s s') >= len(measured) * 2, "%d本" % top.count('class="s s'))
    check("人時生産性は下に格下げされている",
          "人時生産性" in top and top.index("着地見込み") < top.index("人時生産性"))

    print("\n【12】知識の泉 ── 本質を、引ける形で貯める")
    try:
        import knowledge
    except Exception as exc:
        check("knowledge.py が読める", False, "%s: %s" % (type(exc).__name__, exc))
        return report()
    check("knowledge.py が読める", True)

    base = {"subject": "農産部", "type": "判断", "author": "農産部長 田中",
            "essence": "相場高のときは数量を追わず、赤字取引を止めて粗利を守る",
            "why": "農産は数量が減っても荷扱いの手間が減らない。数量を追うと人時だけ増えて粗利が沈む",
            "how": "原価率が前月比1pt以上悪化したら、まず赤字明細を洗って止める"}
    first = knowledge.add(conn, cfg, dict(base))
    conn.commit()
    check("知識を保存できる", first["ok"], first["message"][:50])

    # 一行で書けないものは、まだ2つ以上が混ざっている。ここで止めるのが整理整頓の本体。
    rejects = [
        ("本質が長すぎる", dict(base, essence="あ" * 121)),
        ("本質が空", dict(base, essence="  ")),
        ("なぜが無い", dict(base, why="")),
        ("どう使うかが無い", dict(base, how="")),
        ("種類が4つ以外", dict(base, type="雑感")),
        ("部門が対応表にない", dict(base, subject="存在しない部")),
    ]
    bad = [name for name, payload in rejects if knowledge.add(conn, cfg, dict(payload))["ok"]]
    check("整理されていない入力を6種類とも拒否する", not bad, "通ってしまった: " + "、".join(bad) if bad else "")
    conn.commit()

    knowledge.add(conn, cfg, dict(base, subject="水産部", type="コツ",
                                  essence="クラウド発注は前日17時までに締める",
                                  why="17時を過ぎると翌朝の積み込みに間に合わない",
                                  how="16時半にリマインドを出す"))
    conn.commit()
    for query, label in [("くらうど", "ひらがな"), ("kuraudo", "ローマ字"),
                         ("ｸﾗｳﾄﾞ", "半角カナ"), ("ＣＬＯＵＤ", "全角英字")]:
        hits = knowledge.search(conn, query, cfg.search_readings)
        check("検索が%sで引ける（%s）" % (label, query), len(hits) >= 1, "%d件" % len(hits))

    # 上書き原則：旧決定と新決定を並存させない。並存は「忘れる」より危険な記憶違いを生む。
    #
    # 覆す相手は、検査が自分で作ったものにする。active()[0] を掴むと
    # デモの種を覆してしまい、判定が公開物を壊す側に回る。
    OLD_ESSENCE = "相場高でも数量を落とさず棚を守る（検査用の旧決定）"
    knowledge.add(conn, cfg, dict(base, essence=OLD_ESSENCE,
                                  why="棚を失うと相場が戻っても取り返せないと考えていた",
                                  how="相場に関わらず定番の発注量を維持する"))
    conn.commit()
    old_id = conn.execute("SELECT MAX(id) FROM records WHERE kind='知識'").fetchone()[0]
    knowledge.add(conn, cfg, dict(base, essence="相場高でも定番だけは数量を維持する",
                                  why="定番を切らすと棚を失い、相場が戻っても戻らない",
                                  how="定番リストの品目は赤字でも止めない", supersedes=old_id))
    conn.commit()
    live_ids = [r["id"] for r in knowledge.active(conn, subject="農産部")]
    check("覆した古い知識は現役から外れる", old_id not in live_ids, "現役 %d件" % len(live_ids))
    gone = conn.execute("SELECT status FROM records WHERE id=?", (old_id,)).fetchone()
    # 消さないのが要点。なぜ覆ったかが、いちばん学びになる。
    check("覆っても消さずに残す", gone is not None and gone[0] == "superseded", gone[0] if gone else "消えた")

    stale = knowledge.stale(conn, days=0)
    check("見直されていない知識を棚卸しに出せる", len(stale) >= 1, "%d件" % len(stale))

    code, out = run(APP / "build_dashboard.py")
    check("知識入りでダッシュボードが生成できる", code == 0, "" if code == 0 else out.strip().splitlines()[-1][:160])
    html3 = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")
    # 探しに行かせない。落ち込んだ瞬間に、その部門の知識が同じ画面に出る。
    check("要確認の隣に、その部門の知識が出る", "相場高でも定番だけは数量を維持する" in html3)
    check("覆った古い知識は画面に出ない", OLD_ESSENCE not in html3)

    page = serve.render_knowledge_page(conn, cfg, "検査用 太郎", {"q": "くらうど"})
    check("知識の画面が検索結果を出せる", "クラウド発注は前日17時までに締める" in page)
    check("記録者は名乗らせない（鍵で確認済みと表示）", "アクセスキーで確認済み" in page)

    # 全部が今日の記録では棚卸しは出ない（それが正しい）。実際に古い1件を作って確かめる。
    fresh = serve.render_knowledge_page(conn, cfg, None)
    check("見直したてなら棚卸しの呼びかけは出ない", "見直されていません" not in fresh)
    aged = knowledge.active(conn, subject="水産部")[0]
    aged_body = knowledge.body_of(aged)
    aged_body["reviewed_at"] = "2024-01-01"
    conn.execute("UPDATE records SET body=? WHERE id=?",
                 (json.dumps(aged_body, ensure_ascii=False), aged["id"]))
    conn.commit()
    old_page = serve.render_knowledge_page(conn, cfg, None)
    check("古い知識が出たら名指しで棚卸しを呼びかける", "見直されていません" in old_page,
          "%d日以上" % knowledge.STALE_DAYS)
    check("その知識に要見直しの印が付く", "要見直し" in old_page)
    knowledge.review(conn, aged["id"], "検査")
    conn.commit()
    check("「まだ有効」を押すと棚卸しから外れる",
          "見直されていません" not in serve.render_knowledge_page(conn, cfg, None))

    knowledge.add(conn, cfg, dict(base, subject="畜産部", type="失敗",
                                  essence="<script>alert(3)</script> 危険な入力"))
    conn.commit()
    run(APP / "build_dashboard.py")
    html4 = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")
    check("知識の入力もタグとして生きていない", "<script" not in html4)

    print("\n【11】利用ガイド（READMEではなく、画面の中にあること）")
    guide_tpl = db.ROOT / "castle" / "templates" / "guide.html"
    check("guide.html がある", guide_tpl.exists())

    # 配布されるのは dashboard.html 単体。サーバが無くても読み方が読めないと意味がない。
    standalone = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")
    check("ダッシュボード単体にも読み方が入っている", "この画面の読み方" in standalone)
    for word in ("推定", "傾向", "単発", "内訳", "年間", "予測", "下ぶれ"):
        check("読み方が「%s」に触れている" % word, word in standalone)

    users.issue(instance, "ガイド検査")
    srv3 = serve.make_server(instance, 0)
    threading.Thread(target=srv3.serve_forever, daemon=True).start()
    base3 = "http://127.0.0.1:%d" % srv3.server_address[1]
    try:
        with urllib.request.urlopen(base3 + "/guide", timeout=10) as res:
            guide = res.read().decode("utf-8", "replace")
            status = res.status
        # 鍵を持っていない人が「鍵の貰い方」を読めないと詰む。ガイドだけは認証の外に置く。
        check("鍵が無くても /guide が読める", status == 200 and "アクセスキー" not in guide[:400],
              "HTTP %d" % status)
        for word in ("取り込", "アクセスキー", "控え"):
            check("ガイドが「%s」に触れている" % word, word in guide)
        with urllib.request.urlopen(base3 + "/", timeout=10) as res:
            check("ログイン画面から使い方へ行ける", '/guide' in res.read().decode("utf-8", "replace"))
    finally:
        srv3.shutdown()
        srv3.server_close()
    (instance / "users.json").unlink(missing_ok=True)

    removed = conn.execute(
        "DELETE FROM records WHERE id > ? AND kind IN ('申し送り','知識')", (mark,)).rowcount
    conn.execute("DELETE FROM records WHERE kind='検査用'")
    conn.commit()
    # 判定者が出した控えは判定者が片付ける（実運用の控えには手を触れない）
    # 同じディスクの上にしか控えが無いなら、それは控えではない。
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        away = pathlib.Path(tmp) / "mirror"
        made = backup.create(instance, tag="mirror")
        rows = backup.mirror(made, [away])
        check("控えを別の場所へ写せる", rows and rows[0]["ok"],
              rows[0]["reason"] if rows else "写せなかった")
        check("写した先で開き直して件数が一致する",
              rows and rows[0].get("records") == made["expected"],
              "写した先 %s件 ／ 原本 %s件"
              % (rows[0].get("records") if rows else "—", made["expected"]))

        # 写せなかったときに黙って成功と言わないか（存在しないドライブを指す）
        bad = backup.mirror(made, [pathlib.Path("Z:/castle-nowhere")])
        check("写せなかったら失敗と言う", bad and not bad[0]["ok"] and bad[0]["reason"],
              bad[0]["reason"] if bad else "判定できず")

    check("複製先が未設定なら、そうと言う",
          "mirror" in (APP / "backup.py").read_text(encoding="utf-8")
          and "複製先が設定されていません" in (APP / "backup.py").read_text(encoding="utf-8"))
    junk = [f for f in backup.listing(instance) if f not in kept_before]
    for path in junk:
        path.unlink()
    check("検査で作った控えを片付けた", not (set(backup.listing(instance)) - kept_before),
          "%d件を削除" % len(junk))
    conn.commit()
    run(APP / "build_dashboard.py")
    check("検査で入れた申し送りを片付けた", 
          conn.execute("SELECT COUNT(*) FROM records WHERE id > ?", (mark,)).fetchone()[0] == 0,
          "%d件を削除" % removed)


    print("")
    print("【14】週次の推移 ── 良し悪しが言えているか")
    # この節が答えるべき問いは1つ ── 「この線は、良い推移なのか悪い推移なのか」。
    # 線が描けているかではなく、判定を言えているかを見る。
    board = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")
    cards = json.loads((instance / "out" / "summary.json").read_text(encoding="utf-8")).get("trend_cards")

    if not check("判定材料が summary.json に出ている", bool(cards), "trend_cards が要る"):
        return report()

    VERDICTS = ("要対処", "失速", "挽回中", "順調")

    def judge(vs_target, momentum):
        """判定者が持つ独立の定義。画面側の実装をコピーしない。"""
        if vs_target < -5.0:
            return "挽回中" if momentum >= 1.0 else "要対処"
        if momentum <= -1.0 or (vs_target < 0 and abs(momentum) < 1.0):
            return "失速"
        return "順調"

    check("カードが全社＋全部門ぶんある", len(cards) >= 8, "%d枚" % len(cards))
    check("すべてのカードに判定語がある",
          all(c.get("verdict") in VERDICTS for c in cards),
          "、".join(sorted({str(c.get("verdict")) for c in cards})))
    wrong = [c["name"] for c in cards if c["verdict"] != judge(c["vs_target"], c["momentum"])]
    check("判定が定義どおりに付いている", not wrong, "定義と違う: " + "、".join(wrong))
    check("判定が1種類に偏っていない", len({c["verdict"] for c in cards}) >= 2,
          "全部 %s では判定が効いていない" % cards[0]["verdict"])
    check("判定語が画面に出ている", all(v in board for v in {c["verdict"] for c in cards}))
    # 凡例の言葉と実装の食い違いは、判定が緑でも読み手を誤らせる。
    loose = [c["name"] for c in cards if c["verdict"] == "順調" and c["vs_target"] < 0]
    check("凡例の言葉が判定規則と食い違っていない",
          not (loose and "順調＝目標以上" in board),
          "凡例と食い違う判定 %d件" % len(loose),
          why="目標比マイナスでも順調と出る（%s）のに、凡例が「目標以上」と書いている"
              % "、".join(loose))

    rank = {v: n for n, v in enumerate(VERDICTS)}
    rest = cards[1:]
    check("全社が先頭にある", cards[0]["name"].startswith("全社"), cards[0]["name"])
    check("悪いほうが先に並んでいる",
          all(rank[a["verdict"]] <= rank[b["verdict"]] for a, b in zip(rest, rest[1:])),
          " → ".join(c["verdict"] for c in rest))

    # 人が読んで見つけた穴 ── 誤差圏の部門と、大きく未達の部門が同じ言葉になっていた。
    for a in cards:
        for b in cards:
            if a["vs_target"] < -10.0 and b["vs_target"] > -5.0 and a["verdict"] == b["verdict"]:
                check("誤差圏と大きな未達を同じ判定にしていない", False,
                      "%s(%+.1f%%) と %s(%+.1f%%) がどちらも %s"
                      % (a["name"], a["vs_target"], b["name"], b["vs_target"], a["verdict"]))
                break
        else:
            continue
        break
    else:
        check("誤差圏と大きな未達を同じ判定にしていない", True)

    check("目標線に数値が添えてある", board.count("目標 ") >= len(cards),
          "%d箇所" % board.count("目標 "))
    check("縦軸の上端と下端が読める", board.count('class="ax"') >= 2 * len(cards),
          "%d箇所" % board.count('class="ax"'))
    check("いつからいつまでかが読める", board.count('class="axx"') >= 2 * len(cards),
          "%d箇所" % board.count('class="axx"'))
    check("上に行くほど良い指標だと明記してある", "上に行くほど良い" in board)
    check("前年と目標が違う色で描かれている",
          "var(--prior)" in board and "var(--warn)" in board)

    print("")
    print("【15】利益の階段 ── 営業利益の先まで言えているか")
    board = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")
    book = json.loads((instance / "out" / "summary.json").read_text(encoding="utf-8"))
    ladder = book.get("ladder")

    if not check("階段が summary.json に出ている", bool(ladder), "ladder が要る"):
        return report()

    labels = [x["label"] for x in ladder]
    for want in ("売上総利益", "営業利益", "経常利益", "税引前当期純利益", "当期純利益"):
        check("%s がある" % want, want in labels)

    # 階段が算術的に閉じているか。表示だけそれらしく、足し算が合っていない画面は無数にある。
    running, broken = 0.0, []
    for step in ladder:
        if step["sign"] == 0:
            if abs(step["amount"] - running) > 1.0:
                broken.append("%s（表示 %.0f ／ 積み上げ %.0f）" % (step["label"], step["amount"], running))
        else:
            running += step["sign"] * step["amount"]
    check("階段の足し算が合っている", not broken, "、".join(broken))

    got = {x["label"]: x["amount"] for x in ladder}
    check("営業利益と経常利益が同じ額になっていない",
          abs(got["営業利益"] - got["経常利益"]) > 1.0,
          "同額なら営業外を拾えていない（段が飾りになる）")
    check("税引後が税引前より小さい", got["当期純利益"] < got["税引前当期純利益"],
          "%.0f → %.0f" % (got["税引前当期純利益"], got["当期純利益"]))
    check("法人税等が概算だと画面に書いてある", "実効税率" in board)
    check("階段が画面に出ている", all(w in board for w in ("経常利益", "当期純利益")))
    # ゼロに符号を付けると、計算した振りに見える。
    check("金額ゼロの段に符号を付けていない",
          "＋0万円" not in board and "−0万円" not in board)

    print("")
    print("【16】金は回るか ── 残高と仕入")
    cash = book.get("cash")
    if not check("残高が summary.json に出ている", bool(cash), "cash が要る"):
        return report()

    for want in ("現預金", "売掛金", "棚卸資産", "買掛金", "未払金", "借入金"):
        check("%s の残高がある" % want, isinstance(cash.get(want, {}).get("amount"), (int, float))
              and cash[want]["amount"] > 0, str(cash.get(want)))
        check("%s が画面に出ている" % want, want in board)

    ccc = cash.get("ccc", {})
    check("CCCの内訳が3本そろっている",
          all(isinstance(ccc.get(k), (int, float)) for k in ("receivable_days", "inventory_days", "payable_days")))
    check("CCCの足し算が合っている",
          abs((ccc.get("receivable_days", 0) + ccc.get("inventory_days", 0)
               - ccc.get("payable_days", 0)) - ccc.get("days", -999)) < 0.1,
          "売掛%.1f + 棚卸%.1f − 買掛%.1f → %.1f日"
          % (ccc.get("receivable_days", 0), ccc.get("inventory_days", 0),
             ccc.get("payable_days", 0), ccc.get("days", 0)))
    check("運転資本 = 売掛 + 棚卸 − 買掛 になっている",
          abs(cash.get("working_capital", 0)
              - (cash["売掛金"]["amount"] + cash["棚卸資産"]["amount"] - cash["買掛金"]["amount"])) < 1.0)

    buy = cash.get("purchase", {})
    check("仕入の実績と着地見込みが出ている",
          buy.get("actual", 0) > 0 and buy.get("forecast", 0) > buy.get("actual", 0),
          "実績 %.0f ／ 着地 %.0f" % (buy.get("actual", 0), buy.get("forecast", 0)))
    check("仕入が画面に出ている", "仕入" in board)
    check("残高が確定月のものだと画面に明記されている", "月末時点" in board)
    rng = book.get("stock_chart_range") or []
    check("在庫の推移が年をまたいでいない", len(rng) == 2 and rng[0][:4] == rng[1][:4],
          "→".join(rng))

    print("")
    print("【17】道具としての安全 ── 時報002の点検項目を、機械に見させる")
    # 「外部依存ゼロ」は方針ではなく、検査されて初めて事実になる。
    # 書いた本人が守るつもりでも、次に触る人（AIを含む）は知らない。
    import ast as _ast
    stdlib = set(sys.stdlib_module_names)
    local = {f.stem for f in APP.glob("*.py")}
    used, where = set(), {}
    for path in sorted(APP.glob("*.py")):
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, _ast.ImportFrom):
                names = [node.module] if (node.level == 0 and node.module) else []
            else:
                continue
            for name in names:
                head = name.split(".")[0]
                used.add(head)
                where.setdefault(head, path.name)
    outside = sorted(used - stdlib - local)
    check("外部ライブラリを1つも使っていない", not outside,
          "、".join("%s（%s）" % (m, where[m]) for m in outside))

    # 幅ゼロ・双方向制御・不可視の空白。目視は効かないので数えさせる。
    INVISIBLE = {0x00AD: "ソフトハイフン", 0x200B: "幅ゼロ空白", 0x200C: "幅ゼロ非結合",
                 0x200D: "幅ゼロ結合", 0x200E: "左横書き制御", 0x200F: "右横書き制御",
                 0x2060: "単語結合子", 0x2028: "行区切り", 0x2029: "段落区切り",
                 0x202A: "双方向制御", 0x202B: "双方向制御", 0x202C: "双方向制御",
                 0x202D: "双方向制御", 0x202E: "双方向上書き", 0xFEFF: "BOM"}
    found = []
    targets = list(APP.glob("*.py")) + list((APP.parent / "templates").glob("*"))
    targets.append(APP.parent / "schema.sql")
    for path in sorted(t for t in targets if t.is_file()):
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.split(chr(10)), 1):
            for ch in line:
                code = ord(ch)
                if code in INVISIBLE or (code < 32 and ch not in (chr(9), chr(13))):
                    found.append("%s:%d %s" % (path.name, line_no,
                                               INVISIBLE.get(code, "制御文字 U+%04X" % code)))
    check("見えない文字が1つも無い", not found, "、".join(found[:5]))

    # CDNは「入れていない」ではなく「入れられない」状態にしておく
    board = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")
    outside_refs = [m for m in ("src=" + chr(34) + "http", "@import") if m in board]
    check("外部から読み込むものが1つも無い", not outside_refs, "、".join(outside_refs))

    # 画面に出す値は、出どころを問わずエスケープする。
    # 「設定ファイルは本人が書くから安全」は、次に触る人には引き継がれない。
    import screen
    POISON = "農産部<script>alert(1)</script>"
    fake = {POISON: {"vs_budget": -9.0, "vs_last_year": -3.0, "forecast_gross": 1e7,
                     "budget": 2e7, "margin": 0.12}}
    leaks = []
    if "<script>" in screen.movement(fake):
        leaks.append("部門別の表")
    if "<script>" in screen.series_chart(["2026-08-01", "2026-08-02"],
                                         [(POISON, [1.0, 2.0])], lambda v: "%.0f" % v, "c-x"):
        leaks.append("グラフの凡例")
    check("部門名をそのままHTMLに出していない", not leaks, "素通り: " + "、".join(leaks))

    # 何度でも叩ける入口を残さない
    guard = (APP / "serve.py").read_text(encoding="utf-8")
    check("回数制限の仕組みが入っている", "too_many" in guard,
          "ログインの総当たりと投稿の連打を止める側が要る")
    check("制限を超えたら 429 を返す", "429" in guard)

    # ここまでは「そう書いてある」だけ。実際に叩いて止まるかを確かめる。
    import threading, urllib.error, urllib.parse, urllib.request
    # 判定者が対象の状態を握る。前の節のログイン試行が残っていると、
    # 何回目で止まるかが変わってしまう（users.json を退避するのと同じ理由）。
    serve._seen.clear()
    serve.LIMITS["login"] = (3, 600)      # 試験のあいだだけ狭める
    srv2 = serve.make_server(instance, 0)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    base2 = "http://127.0.0.1:%d" % srv2.server_address[1]
    codes = []
    try:
        body = urllib.parse.urlencode({"token": "まちがった鍵"}).encode()
        for _ in range(5):
            try:
                with urllib.request.urlopen(
                        urllib.request.Request(base2 + "/login", data=body, method="POST"),
                        timeout=10) as res:
                    codes.append(res.status)
            except urllib.error.HTTPError as err:
                codes.append(err.code)
    finally:
        srv2.shutdown()
        srv2.server_close()
        serve.LIMITS["login"] = (10, 600)
        serve._seen.clear()

    check("3回を超えた鍵の試行が実際に止まる（429が返る）",
          codes[:3] and all(c != 429 for c in codes[:3]) and 429 in codes[3:],
          "返ってきたHTTP: %s" % codes)

    print("")
    print("【18】公開用の一式 ── 出したものに、城の半分が欠けていないか")
    # ダッシュボードだけを公開すると、申し送りと知識の泉が
    # 「公開されたものからは存在しない」ことになる。記事はその2つを語っているのに。
    code, out = run(APP / "export_static.py")
    check("公開用の一式を書き出せる", code == 0, "" if code == 0 else out.strip().splitlines()[-1][:160])

    docs = APP.parent.parent / "docs"
    want = ("index.html", "note.html", "knowledge.html", "guide.html")
    missing = [n for n in want if not (docs / n).exists()]
    check("4画面そろっている", not missing, "足りない: " + "、".join(missing))
    if missing:
        return report()

    pages = {n: (docs / n).read_text(encoding="utf-8") for n in want}
    check("知識の泉に中身がある", "盆前の欠品対策" in pages["knowledge.html"])
    check("申し送りに中身がある", "数量減は意図的" in pages["note.html"])
    dead = {n: len(re.findall(chr(34) + "/[a-z]", p)) for n, p in pages.items()}
    check("押しても何も起きないリンクが残っていない", not any(dead.values()),
          "、".join("%s:%d" % kv for kv in dead.items() if kv[1]))
    for name in ("note.html", "knowledge.html"):
        check("%s の書き込み口が閉じてある" % name, " disabled" in pages[name])
        check("%s に書けない理由が書いてある" % name, "これは読むだけの見本です" in pages[name])
    broken = [n for n, p in pages.items() if p.count("<h1>") != p.count("</h1>")]
    check("HTMLの見出しが閉じている", not broken, "、".join(broken))

    print("")
    print("【19】認可 ── 見てよい範囲の外が、見えないか")
    # 全社の集計を伏せるだけでは足りない。部門別の欄が全部残っていれば、
    # 引き算で全社が復元できる。**データの側で絞る。**
    import http.cookiejar
    import threading, urllib.error, urllib.parse, urllib.request

    keep = (instance / "users.json").read_bytes() if (instance / "users.json").exists() else None
    try:
        users.save(instance, [])
        token = users.issue(instance, "検査用 農産部長", ["農産部"])
        check("範囲つきの鍵を発行できる", users.scope_of(instance, "検査用 農産部長") == ["農産部"],
              str(users.scope_of(instance, "検査用 農産部長")))
        wide = users.issue(instance, "検査用 役員")
        check("範囲を書かない鍵は全社（既存の鍵を壊さない）",
              users.scope_of(instance, "検査用 役員") is None)

        serve._seen.clear()
        srv = serve.make_server(instance, 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            def open_as(key):
                jar = http.cookiejar.CookieJar()
                op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
                op.open(urllib.request.Request(
                    base + "/login", data=urllib.parse.urlencode({"token": key}).encode()),
                    timeout=10).read()
                return op

            narrow, broad = open_as(token), open_as(wide)
            page = narrow.open(base + "/", timeout=10).read().decode("utf-8")
            body = page[page.index("<body"):]
            # 語ではなく構造で見る。断り書きが節の名前を挙げるので、語では判定できない。
            for label, marker in (("着地見込み", 'class="landing"'),
                                  ("利益の階段", 'table class="ladder"'),
                                  ("金の巡り", 'class="cashgrid"'),
                                  ("航海図", 'id="ch-total"')):
                check("部門の鍵で「%s」の節が出ない" % label, marker not in body)
            outside = [n for n in ("加工食品部", "畜産部", "水産部", "低温食品部") if n in body]
            check("部門の鍵で範囲外の部門が出ない", not outside, "出ている: " + "、".join(outside))
            check("部門の鍵でも自部門は見える", "農産部" in body)

            full = broad.open(base + "/", timeout=10).read().decode("utf-8")
            check("全社の鍵ではこれまで通り全部見える",
                  all(w in full for w in ('table class="ladder"', 'class="cashgrid"',
                                          'id="ch-total"', "加工食品部")))

            # 読めない部門に書けてしまわないか
            form = urllib.parse.urlencode(
                {"subject": "水産部", "occurred_at": "2026-08-12", "category": "得意先の事情",
                 "author": "x", "text": "範囲外への書き込みが通ってはいけない。"}).encode()
            status = None
            try:
                status = narrow.open(urllib.request.Request(base + "/note", data=form), timeout=10).status
            except urllib.error.HTTPError as err:
                status = err.code
            check("範囲外の部門には書けない（403）", status == 403, "HTTP %s" % status)

            note = narrow.open(base + "/note", timeout=10).read().decode("utf-8")
            check("範囲外の申し送りが一覧に出ない", "盆前で休業の得意先" not in note)
            know = narrow.open(base + "/knowledge", timeout=10).read().decode("utf-8")
            check("範囲外の知識が一覧に出ない", "盆前の欠品対策" not in know)
            check("全社の知識は部門の鍵でも読める", "会計の締めは翌月10日ごろ" in know)
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        (instance / "users.json").unlink(missing_ok=True)
        if keep is not None:
            (instance / "users.json").write_bytes(keep)
        serve._seen.clear()

    print("")
    print("【20】申し送りから知識へ ── 流れるものを、残るものに上げる")
    # 何が「繰り返し効くこと」かは人にしか決められない。
    # 機械にできるのは、**繰り返していることを名指しして、上げる口を用意する**ところまで。
    import promote

    mark = conn.execute("SELECT MAX(id) FROM records").fetchone()[0]
    base_note = {"occurred_at": "2026-08-11", "category": "相場・仕入価格",
                 "author": "検査用", "text": "相場が高いので数量を絞っている。"}
    for i in range(3):
        ok, msg = serve.add_note(conn, cfg, dict(base_note, subject="水産部",
                                                 text="相場が高いので数量を絞っている。%d" % i))
        check("繰り返しの材料を入れられる（%d件目）" % (i + 1), ok, msg)
    conn.commit()

    found = promote.repeats(conn, days=90, threshold=3)
    hit = [r for r in found if r["subject"] == "水産部" and r["category"] == "相場・仕入価格"]
    check("同じ部門・同じ区分が続いていると名指しする", len(hit) == 1 and hit[0]["count"] >= 3,
          "%d件" % (hit[0]["count"] if hit else 0))
    check("閾値に届かないものは名指ししない",
          not [r for r in found if r["subject"] == "低温食品部"],
          "1件しかない部門は挙がっていない", why="低温食品部が挙がっている")

    seed = promote.draft(conn, hit[0]["ids"][0])
    check("申し送りの本文を持ったまま知識の入力へ渡せる",
          seed and seed["subject"] == "水産部" and "相場が高い" in seed["essence"],
          str(seed))

    made = knowledge.add(conn, cfg, dict(seed, type="判断",
                                         why="相場高で数量を追うと粗利率が先に落ちるため",
                                         how="相場が前年比+15%を超えたら赤字取引を切る",
                                         author="検査用"))
    check("その場で知識にできる", made["ok"], made["message"])
    conn.commit()
    promote.mark_promoted(conn, hit[0]["ids"], made["id"])
    conn.commit()

    body = json.loads(conn.execute("SELECT body FROM records WHERE id=?",
                                   (hit[0]["ids"][0],)).fetchone()[0])
    check("上げた申し送りに印が残る", body.get("promoted_to") == made["id"],
          str(body.get("promoted_to")))
    again = promote.repeats(conn, days=90, threshold=3)
    check("上げ終えたものは、もう名指ししない",
          not [r for r in again if r["subject"] == "水産部" and r["category"] == "相場・仕入価格"])

    html5 = None
    code, out = run(APP / "build_dashboard.py")
    check("昇格を入れてもダッシュボードが生成できる", code == 0,
          "" if code == 0 else out.strip().splitlines()[-1][:160])
    page = serve.render_knowledge_page(conn, cfg, None)
    check("繰り返している申し送りが、知識の泉の画面に出る", "繰り返し出ている申し送り" in page)

    # 画面から上げたときに、塊ぜんぶへ印が付くか（1件だけだと残りが催促され続ける）
    for i in range(3):
        serve.add_note(conn, cfg, dict(base_note, subject="畜産部",
                                       text="出荷が前倒しになっている。%d" % i))
    conn.commit()
    pack = [g for g in promote.repeats(conn, threshold=3) if g["subject"] == "畜産部"][0]
    lift = serve.render_knowledge_page(conn, cfg, None, {"from": str(pack["ids"][0])})
    check("上げる画面が、その申し送りを持って開く", "出荷が前倒しになっている" in lift)
    check("どの申し送りから来たかを画面が持っている",
          ('name="from_note" value="%d"' % pack["ids"][0]) in lift)

    payload = {"subject": "畜産部", "type": "コツ", "author": "検査用",
               "essence": "出荷の前倒しが続く時期は、翌週の人時を先に厚くする",
               "why": "前倒しが続くと荷受けが集中し、残業でしか吸収できなくなるため",
               "how": "2週続けて前倒しが出たら、翌週のシフトを1人ぶん増やす",
               "from_note": str(pack["ids"][0])}
    got = knowledge.add(conn, cfg, payload)
    promote.mark_promoted(conn, promote.group_of(conn, pack["ids"][0]), got["id"])
    conn.commit()
    left = [g for g in promote.repeats(conn, threshold=3) if g["subject"] == "畜産部"]
    check("塊ぜんぶが上げ済みになる（1件だけ残らない）", not left,
          "名指しに残った件数 %d" % (left[0]["count"] if left else 0))

    conn.execute("DELETE FROM records WHERE id > ?", (mark,))
    conn.commit()
    check("検査で入れた昇格の材料を片付けた",
          conn.execute("SELECT COUNT(*) FROM records WHERE id > ?", (mark,)).fetchone()[0] == 0)

    print("")
    print("【21】在庫 ── 週次で実、あいだは積み上げ")
    # 表示が壊れていないことと、数字が互いに整合していることは別。
    # 在庫を流れと別々に作れば、CCCも運転資本も「それらしいだけの数字」になる。
    ledger = pnl.build(conn, cfg)
    by_day = {d["date"]: d for d in ledger["days"]}
    buys = pnl.load_purchase(conn)
    stock = pnl.load_stock(conn)

    check("在庫が入っている", len(stock) >= 8, "%d時点" % len(stock))
    # 年をまたぐ隙間（前年データと当年データのあいだ）は数えない
    gaps = [g for g in
            ((datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days
             for a, b in zip(sorted(stock), sorted(stock)[1:])) if g < 60]
    # 期首から最初の週末までは7日に満たない。そこは正しい挙動なので数えない。
    check("在庫は週次で上がってくる（7日おき）",
          gaps and max(gaps) <= 7 and gaps.count(7) >= len(gaps) - 2,
          "間隔 %s日" % "／".join(str(g) for g in sorted(set(gaps))))
    check("会計の月末残高に棚卸資産を二重に持っていない",
          "棚卸資産" not in (cfg.accounting.get("balance") or {}),
          "在庫の出どころは棚卸だけ", why="残高側にも棚卸資産がある ── 出どころが2つになる")

    # 週次の実データが、仕入と売上原価の流れと繋がっているか
    stamps = sorted(stock)
    worst, worst_at = 0.0, ""
    for before, now in zip(stamps, stamps[1:]):
        if (datetime.date.fromisoformat(now) - datetime.date.fromisoformat(before)).days > 60:
            continue                       # 前年データと当年データのあいだは繋がらない
        days = [d for d in by_day if before < d <= now]
        rolled = stock[before] + sum(buys.get(d, 0.0) - (by_day[d]["sales"] - by_day[d]["gross"])
                                     for d in days)
        gap = abs(rolled - stock[now]) / stock[now] * 100 if stock[now] else 100.0
        if gap > worst:
            worst, worst_at = gap, now
    check("週次の在庫が、仕入と売上原価の流れと繋がっている", worst <= 2.0,
          "いちばんずれた週 %s で %.1f%%" % (worst_at, worst))

    # 日次の埋め方
    daily = pnl.daily_stock(conn, cfg)
    check("日次の在庫が出ている", len(daily) >= 60, "%d日" % len(daily))
    fixed = [d for d, v in daily.items() if v["settled"]]
    # 日次の在庫が届いている範囲に入っている棚卸だけを数える（前年ぶんは範囲外）
    covered = [d for d in stock if min(daily) <= d <= max(daily)]
    check("確定した日と、置いた日が区別されている",
          len(fixed) == len(covered) and len(fixed) < len(daily),
          "確定 %d日 ／ 範囲内の棚卸 %d回 ／ 全体 %d日" % (len(fixed), len(covered), len(daily)))
    check("確定した日は、実データそのままになっている",
          all(abs(daily[d]["amount"] - stock[d]) < 1.0 for d in fixed))

    # 置いた日は「在庫日数 × 日商原価」── 金額を日割りしていない
    guessed = [d for d in sorted(daily) if not daily[d]["settled"]]
    check("置いた日には、根拠（前の実データからの積み上げ）が付いている",
          all(daily[d].get("from") and daily[d].get("moved") is not None for d in guessed))

    # いちばん大事な性質 ── 置いた値が、次の実データにちゃんと着地するか。
    # 在庫日数を当てる置き方は平均1.44%外し、7月の週は+4.7%（5,900万円）外していた。
    landed = []
    for before, now in zip(stamps, stamps[1:]):
        if (datetime.date.fromisoformat(now) - datetime.date.fromisoformat(before)).days > 60:
            continue
        prev_day = max((d for d in daily if d < now), default=None)
        if prev_day is None or prev_day not in daily:
            continue
        landed.append(abs(daily[prev_day]["amount"] - stock[now]) / stock[now] * 100)
    # 線は 0.8%。実地棚卸は必ずズレるので、そこは誰にも当てられない
    # （このデモは ±0.4% の棚卸差異を意図的に入れてある）。
    # 在庫日数を当てる置き方は平均1.44%・最大4.7%で、この線を大きく超えていた。
    check("置いた値が、次の実データに着地する（0.8%以内）",
          landed and max(landed) < 0.8,
          "いちばん外した週で %.2f%%（棚卸差異ぶんは誰にも当てられない）"
          % (max(landed) if landed else 0))

    # 推定を、確定の顔をして混ぜない
    cash = ledger["cash"]
    check("CCCと運転資本は、確定した在庫で計算している",
          cash["stock_settled_at"] in stock
          and abs(cash["棚卸資産"]["amount"] - stock[cash["stock_settled_at"]]) < 1.0,
          "使った時点 %s" % cash.get("stock_settled_at"))

    board = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")
    check("どこまでが実で、どこからが置いた値かが画面に書いてある",
          "在庫日数" in board and "週次" in board)

    print("")
    print("【22】在庫の推定が、週のあいだで暴れないか")
    ledger2 = pnl.build(conn, cfg)
    # 在庫の推定は、週の中で大きく上下しない（動いているのは在庫ではなく割る側）
    stock_daily = pnl.daily_stock(conn, cfg, ledger2["days"])
    weeks = {}
    for day in sorted(stock_daily):
        if day < "2026-06-15":
            continue
        key = datetime.date.fromisoformat(day).isocalendar()[:2]
        weeks.setdefault(key, []).append(stock_daily[day]["amount"])
    def span(values):
        return (max(values) - min(values)) / (sum(values) / len(values)) * 100

    worst = max((span(v), k) for k, v in weeks.items() if len(v) >= 4)
    # 凡例に書いてあることと実装が違えば、数字が合っていても嘘になる。
    # 部門別の推移を月次にしたので、画面のどこにも「移動平均」「日次」は残っていないはず。
    board2 = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")
    ghost = [w for w in ("移動平均", "営業日）の移動") if w in board2]
    check("消した仕組みの説明が画面に残っていない", not ghost, "残り: %s" % ghost)
    import re as _re
    axis = _re.findall(r"(\d{4}-\d{2}) 〜 (\d{4}-\d{2})", board2)
    check("部門別の推移が月の単位で並んでいる", bool(axis)
          and (int(axis[0][1][:4]) - int(axis[0][0][:4])) * 12
              + int(axis[0][1][5:7]) - int(axis[0][0][5:7]) >= 11,
          "軸の範囲 %s" % (str(axis[0]) if axis else "無し",))

    check("在庫の推定が、週の中で暴れない", worst[0] < 4.0,
          "いちばん振れた週で %.1f%%（在庫ではなく、割る側の日商原価が動いている）" % worst[0])

    print("")
    print("【23】在庫は、粗利を出すための入力になっているか")
    # 在庫を数える理由は、売上原価が「期首＋仕入−期末」で出るから。
    # 在庫が粗利に効いていないなら、週次で数える意味がない。
    rates = pnl.weekly_cost_rates(conn, cfg)
    check("部門ごと・週ごとの原価率が出ている", len(rates) >= 7,
          "%d部門" % len(rates))
    weeks = max((len(v) for v in rates.values()), default=0)
    check("週の本数ぶんある", weeks >= 8, "%d週" % weeks)

    # 会計が締まっていない当月でも、棚卸があれば原価率は出る
    last_month = max(m for m in {r["occurred_at"][:7] for r in conn.execute(
        "SELECT occurred_at FROM records WHERE kind='売上'")})
    settled_months = {r[0][:7] for r in conn.execute(
        "SELECT occurred_at FROM records WHERE kind='部門損益'")}
    check("当月は会計が締まっていない", last_month not in settled_months, last_month)
    fresh = [d for v in rates.values() for d, _ in v if d[:7] == last_month]
    check("それでも当月の原価率が、棚卸から出ている", fresh,
          "当月の週次原価率 %d本" % len(fresh))

    # 棚卸から出した率と、会計が後から出す率が合っているか（貸借と損益の繋がり）
    monthly = pnl.load_monthly(conn)
    gaps = []
    for dept, rows in rates.items():
        for month in sorted(monthly.get(dept, {})):
            got = [r for d, r in rows if d[:7] == month]
            acc = monthly[dept][month]
            if not got or not acc["sales"]:
                continue
            gaps.append(abs(sum(got) / len(got) - acc["cost"] / acc["sales"]) * 100)
    check("棚卸から出した原価率が、会計の月次率と合う（1pt以内）",
          gaps and max(gaps) < 1.0, "いちばん離れた月で %.2fpt" % (max(gaps) if gaps else 99))

    # いちばん大事な判定 ── 在庫を動かしたら、粗利が動くか
    before = pnl.build(conn, cfg)["month"]["forecast_gross"]
    conn.execute("UPDATE records SET body=json_set(body,'$.amount', amount * 1.02) "
                 "WHERE kind='在庫' AND occurred_at >= ?", (max(pnl.load_stock(conn)),))
    after = pnl.build(conn, cfg)["month"]["forecast_gross"]
    conn.rollback()
    check("在庫を動かすと、粗利が動く（＝在庫が入力になっている）",
          abs(after - before) > 1.0,
          "動かなかった ── 在庫は粗利に効いていない" if abs(after - before) <= 1.0
          else "%s → %s" % (yen_(before), yen_(after)))

    print("")
    print("【24】実在の会社として成り立っているか")
    # 3ヶ月ぶんの飛び飛びのデータでは、年間の着地は原理的に出せない。
    months = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT substr(occurred_at,1,7) FROM records WHERE kind='売上'")})
    check("売上が16ヶ月以上ある", len(months) >= 16, "%dヶ月（%s〜%s）"
          % (len(months), months[0], months[-1]))

    def step(a, b):
        ya, ma = int(a[:4]), int(a[5:7])
        yb, mb = int(b[:4]), int(b[5:7])
        return (yb - ya) * 12 + (mb - ma)

    holes = [(a, b) for a, b in zip(months, months[1:]) if step(a, b) != 1]
    check("月が飛んでいない", not holes, "飛び: %s" % holes[:3])

    fiscal = (cfg.fiscal or {}).get("start_month")
    check("事業年度が決まっている（何月始まりか）", fiscal in range(1, 13), str(fiscal))

    year = pnl.build_year(conn, cfg)
    check("前期が通年（12ヶ月）そろっている", len(year["last_year"]["months"]) == 12,
          "%dヶ月" % len(year["last_year"]["months"]))
    kinds = {m["state"] for m in year["this_year"]["months"]}
    check("当期の月が3つの状態に分かれている（確定／当月／予測）",
          kinds == {"確定", "当月", "予測"}, str(sorted(kinds)))
    settled = [m for m in year["this_year"]["months"] if m["state"] == "確定"]
    check("締まった月が2つ以上ある", len(settled) >= 2, "%dヶ月" % len(settled))
    check("当月はちょうど1つ",
          len([m for m in year["this_year"]["months"] if m["state"] == "当月"]) == 1)

    # 年間の着地＝確定の積み上げ ＋ 当月の見込み ＋ 先の月の予測
    total = sum(m["op"] for m in year["this_year"]["months"])
    check("年間の着地が、月ごとの積み上げと一致する",
          abs(total - year["this_year"]["op"]) < 1.0,
          "積み上げ %.0f ／ 年間 %.0f" % (total, year["this_year"]["op"]))
    check("年間の着地が前期と比べられる", year["last_year"]["op"] != 0,
          "前期の営業利益 %s" % yen_(year["last_year"]["op"]))

    # 賞与は引当でならす。だから損益は跳ねない ── 跳ねたら年間の着地が読めない。
    # 「支給月に人件費が跳ねる」判定を最初に書いたが、それは会計の実務として誤り。
    # 資金のほうは跳ねる。**損益と資金は別に動く。** 両方を見る。
    labor = sorted(m["labor"] for m in year["last_year"]["months"])
    mid = labor[len(labor) // 2]
    check("賞与が引当でならされている（月次損益が跳ねない）", labor[-1] < mid * 1.25,
          "いちばん高い月 %s ／ 中央 %s" % (format(labor[-1], ","), format(mid, ",")))
    cash_by_month = {r[0]: r[1] for r in conn.execute(
        "SELECT substr(occurred_at,1,7), amount FROM records"
        " WHERE kind='残高' AND subject='現金及び預金' ORDER BY occurred_at")}
    cash_flow = {b: cash_by_month[b] - cash_by_month[a]
                 for a, b in zip(sorted(cash_by_month), sorted(cash_by_month)[1:])}
    bonus_m = [m for m in cash_flow if int(m[5:7]) in (6, 12)]
    other = [cash_flow[m] for m in cash_flow if m not in bonus_m]
    check("賞与の支給月は、資金のほうが落ち込む", bool(bonus_m)
          and max(cash_flow[m] for m in bonus_m) < sum(other) / len(other),
          "賞与月 %s ／ ほかの月の平均 %s"
          % (format(int(max(cash_flow[m] for m in bonus_m)), ","),
             format(int(sum(other) / len(other)), ",")))
    sales = {m["month"]: m["sales"] for m in year["last_year"]["months"]}
    top, bottom = max(sales, key=sales.get), min(sales, key=sales.get)
    check("年間で山と谷がある（季節がある）",
          sales[top] / sales[bottom] > 1.15,
          "山 %s / 谷 %s ＝ %.2f倍" % (top, bottom, sales[top] / sales[bottom]))

    print("【25】画面の文字が、重なって読めなくなっていないか")
    # 値が近いほど、右端に添えた文字は折り重なる（予算3.60億・着地3.68億・前期3.33億）。
    # 線は3本きれいに見えているので、**判定が全部緑でも気づかない。**
    # 2026-08-31、年間の着地の画面で実際に踏んだ。だから数える側へ移した。
    import re as _re3
    board3 = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")
    pairs, checked = [], 0

    # 言い切った言葉と、実際の差が合っているか。**誤差圏と大きな未達を同じ語で呼ばない。**
    # 節が増えたので、文書の先頭から探すと別の節の判定文を拾う。年間のタブの中を見る。
    panel = board3[board3.index('id="p-year"'):]
    verdict = _re3.search(r'<p class="verdict">(.*?)</p>', panel, _re3.S).group(1)
    gap_rate = (year["this_year"]["op"] / year["budget"] - 1) * 100
    said_ok = "届く見通し" in verdict
    said_ng = "届かない見通し" in verdict
    check("年間の判定語が、実際の差と食い違っていない",
          (said_ok and gap_rate >= 3) or (said_ng and gap_rate < -3)
          or (not said_ok and not said_ng and abs(gap_rate) < 3),
          "差 %+.1f%% ／ 語 %s" % (gap_rate, "届く" if said_ok else ("届かない" if said_ng else "ほぼ同じ")))

    # 「残りが何%下ぶれたら届かないか」は、外れても嘘にならない言い方。数字が合っているかを見る。
    hit = _re3.search(r"残り(\d+)ヶ月が見込みより<b>([\d.]+)%</b>下ぶれ", verdict)
    check("下ぶれの余地が、実際の積み上げと合っている", bool(hit))
    if hit:
        ahead = [m for m in year["this_year"]["months"] if m["state"] != "確定"]
        want = (year["this_year"]["op"] - year["budget"]) / sum(m["op"] for m in ahead) * 100
        check("下ぶれ%の計算が合っている", abs(float(hit.group(2)) - abs(want)) < 0.05
              and int(hit.group(1)) == len(ahead),
              "画面 %s%% ／ 計算 %.1f%%（残り%dヶ月）" % (hit.group(2), abs(want), len(ahead)))

    for svg in _re3.findall(r"<svg.*?</svg>", board3, _re3.S):
        texts = []
        for tag in _re3.findall(r"<text[^>]*>[^<]*</text>", svg):
            x = _re3.search(r'x="([-\d.]+)"', tag)
            y = _re3.search(r'y="([-\d.]+)"', tag)
            size = _re3.search(r'font-size="([\d.]+)"', tag)
            body = _re3.search(r">([^<]*)</text>", tag)
            if not (x and y and body and body.group(1).strip()):
                continue
            texts.append((float(x.group(1)), float(y.group(1)),
                          float(size.group(1)) if size else 12.0, body.group(1)))
        for i in range(len(texts)):
            for j in range(i + 1, len(texts)):
                a, b = texts[i], texts[j]
                if abs(a[0] - b[0]) > 10:            # 横に離れていれば重ならない
                    continue
                checked += 1
                if abs(a[1] - b[1]) < max(a[2], b[2]) * 0.85:
                    pairs.append((a[3], b[3], round(abs(a[1] - b[1]), 1)))
    # 記号は、それが指す数字と同じ枠に置く。離れた列に置くと隣の数字と結びつけて読まれる
    # ── 2026-09-01、▲が部門名の左にあり、すぐ右の予算比がマイナスという並びを画面で見つけた。
    cells = _re3.findall(
        r"<td><span[^>]*>([▲▼→])</span> <span[^>]*>([+-][\d.]+)%</span></td>", board3)
    check("表の記号が、同じ枠の数字と食い違っていない", bool(cells) and all(
        (a == "▲" and float(b) > 1) or (a == "▼" and float(b) < -1)
        or (a == "→" and abs(float(b)) <= 1) for a, b in cells),
        "%d箇所" % len(cells))
    check("記号が何を指すか画面に書いてある", "▲▼は<b>前年比</b>の向き" in board3)

    check("同じ位置に置いた文字が重なっていない", not pairs,
          "%d組を検査／重なり %s" % (checked, pairs[:3] if pairs else "無し"))
    check("重なりを検査できるだけの文字がある", checked >= 3, "%d組" % checked)

    print("")
    print("【26】取り込みの自動化 ── 誰も種別を選ばない")
    # 「CSVを落とす作業が消えた」と言うなら、種別を人が選ばずに取り込めなければならない。
    # **判定は自分の置き場を握る。** 本番の incoming を汚さない。
    import shutil

    import intake

    # **判定は本番のDBを触らない。** 取り込みは同じ自然キーを上書きするので、
    # 後片付けでバッチを消すと、元から入っていた本物の記録まで道連れになる
    # ── 2026-09-01、2026年の売上と仕入を実際に消してしまった。専用の一式を作る。
    before26 = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    sandbox = instance.parent / "検査用インスタンス"
    shutil.rmtree(sandbox, ignore_errors=True)
    (sandbox / "incoming").mkdir(parents=True)
    shutil.copy(instance / "config.json", sandbox / "config.json")
    lab = sandbox / "incoming"
    box = db.connect(sandbox)
    # 【1】で見張りが退避したので、置き場ではなく退避先から取る
    found = sorted((instance / "incoming" / "取込済").rglob("A社販売管理_売上日報_2026*.csv"))
    check("試すための元ファイルがある", bool(found), "%d本" % len(found))
    src = found[0]
    shutil.copy(src, lab / src.name)

    check("ファイル名から種別が決まる", intake.classify(cfg, src.name) == "売上",
          str(intake.classify(cfg, src.name)))
    # 規約に無い名前を推測で取り込むと、間違った種別のまま骨に入る。**推測しない。**
    for bad in ("売上.csv", "A社_なにか_2026.csv", "C社会計_請求書_202608.csv"):
        check("規約に無い「%s」を推測で取り込まない" % bad, intake.classify(cfg, bad) is None)

    got = intake.run(box, cfg, lab)
    box.commit()
    check("置き場を一巡して取り込める", len(got["taken"]) == 1 and not got["held"],
          "取り込み %d本 ／ 保留 %d本" % (len(got["taken"]), len(got["held"])))
    check("取り込んだファイルは置き場から消える（退避される）",
          not list(lab.glob("*.csv")) and list((lab / "取込済").rglob("*.csv")),
          "置き場の残り %d本" % len(list(lab.glob("*.csv"))))

    # 同じ中身をもう一度置いても、二度は数えない（ベンダーの再送は普通に起きる）
    shutil.copy(src, lab / src.name)
    again = intake.run(box, cfg, lab)
    box.commit()
    check("同じ中身を二度取り込まない", not again["taken"] and len(again["same"]) == 1,
          "取り込み %d本 ／ 同一 %d本" % (len(again["taken"]), len(again["same"])))

    # 中身が変わっていれば取り込む。**名前で判断すると、訂正版の再送を取りこぼす。**
    (lab / src.name).write_bytes(
        src.read_bytes() + "1010,加工食品部,2026/08/12,1,1\r\n".encode("cp932"))
    fixed = intake.run(box, cfg, lab)
    box.commit()
    check("同じ名前でも中身が変われば取り込む", len(fixed["taken"]) == 1,
          "取り込み %d本" % len(fixed["taken"]))

    # 1本が壊れていても、他は進める（1本の不良で全部止めない）
    (lab / "A社販売管理_売上日報_9999.csv").write_bytes("壊れた中身".encode("cp932"))
    shutil.copy(sorted((instance / "incoming" / "取込済").rglob(
        "A社販売管理_仕入日報_2026*.csv"))[0], lab)
    (lab / "得体の知れないもの.csv").write_bytes("なにか".encode("cp932"))
    mixed = intake.run(box, cfg, lab)
    box.commit()
    check("壊れた1本があっても、他は取り込む", len(mixed["taken"]) >= 1,
          "取り込み %d本 ／ 保留 %d本" % (len(mixed["taken"]), len(mixed["held"])))
    check("壊れたものは保留に退ける", any("9999" in n for n, _r in mixed["held"]),
          "保留: %s" % [n for n, _r in mixed["held"]])
    check("規約に無い名前も保留に退ける（黙って捨てない）",
          any("得体の知れないもの" in n for n in mixed["unknown"]), str(mixed["unknown"]))
    check("保留したものは、保留の置き場に残る",
          len(list((lab / "保留").glob("*.csv"))) == 2,
          "%d本" % len(list((lab / "保留").glob("*.csv"))))

    # 失敗したことが、呼んだ側に伝わるか（タスクスケジューラが気づけるか）
    check("保留があれば終了コードが0にならない", intake.exit_code(mixed) != 0,
          "終了コード %d" % intake.exit_code(mixed))
    none_at_all = {"taken": [], "same": [], "held": [], "unknown": []}
    check("全部うまくいけば終了コードは0",
          intake.exit_code(dict(none_at_all, taken=["x"])) == 0)
    check("何も来ていなくても、来たことにしない", "0本" in intake.summary(none_at_all),
          intake.summary(none_at_all))

    made = box.execute(
        "SELECT id FROM import_log WHERE note LIKE '%見張り%'").fetchall()
    check("台帳に、見張りが入れたものとして残る", len(made) >= 3, "%d件" % len(made))
    check("本番のDBは1件も動いていない",
          conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == before26,
          "%d件 → %d件"
          % (before26, conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]))
    box.close()
    shutil.rmtree(sandbox, ignore_errors=True)
    check("検査用の一式を片付けた", not sandbox.exists())


    # 決まった時刻に走らせる口。**動かして、ログが読めることまで見る。**
    # 2026-09-01、2つ踏んだ ── 日本語名の .bat が呼び出し側から見つからないこと、
    # ログに python（UTF-8）と echo（CP932）が混ざって、どちらでも読めなくなること。
    launcher = instance.parent / "intake.bat"
    check("決まった時刻に走らせる口がある", launcher.exists(), launcher.name)
    check("名前がASCIIである（呼び出し側の文字コードに左右されない）",
          launcher.name.isascii(), launcher.name)
    raw = launcher.read_bytes()
    # cmd はバッチファイルをOEMの文字コードで読む。UTF-8で保存すると日本語コメントが
    # 化け、その断片をコマンドとして実行しようとして落ちる
    # ── 2026-09-01、実際に踏んだ（chcp を先に書いても、ファイルの読み方には間に合わない）。
    try:
        raw.decode("cp932")
        cp932_ok = True
    except UnicodeDecodeError:
        cp932_ok = False
    check("バッチ本体がCP932で保存されている（cmdが読める形）", cp932_ok)
    text = raw.decode("cp932", "replace")
    check("多段の起動チェーンになっていない（pythonを直接呼ぶ）",
          "python " in text and not any(w in text for w in ("wscript", "cscript", ".vbs")))
    check("pythonの出力をUTF-8に固定している", "PYTHONIOENCODING" in text)
    # ログに日本語をechoすると、python（UTF-8）と echo（CP932）が混ざって読めなくなる
    echoed = [ln for ln in text.splitlines()
              if "echo" in ln and not ln.strip().startswith("rem")]
    check("ログへ書く行がASCIIだけになっている", all(ln.isascii() for ln in echoed),
          "／".join(ln.strip()[:40] for ln in echoed if not ln.isascii()) or "全部ASCII")
    check("終了コードを呼んだ側へ返している", "exit /b %CODE%" in text)
    check("終了コードがリダイレクトに食われない書き方になっている",
          '>> "instance' + chr(92) + 'intake.log" echo' in text)

    if os.name == "nt":
        log = instance / "intake.log"
        log.unlink(missing_ok=True)
        code, _out = run_shell(str(launcher))
        check("実際に走って、終了コードを返す", code == 0, "終了コード %d" % code)
        check("ログができる", log.exists())
        if log.exists():
            try:
                body = log.read_bytes().decode("utf-8")
                readable = True
            except UnicodeDecodeError:
                body, readable = "", False
            check("ログが1つの文字コードで読める（UTF-8）", readable)
            check("ログに終了コードが数字まで残る",
                  bool(_re3.search("exit=" + chr(92) + "d", body)),
                  body.strip().splitlines()[-1][:60] if body.strip() else "空")
            log.unlink(missing_ok=True)






    print("")
    print("【33】3つ目の問い ── 足りない分を、どう埋めるか")
    # 社長の3つ目の問いは「どこで消えたか」ではない。**「で、どうする」**である。
    #   いくら足りないか／何をどれだけ動かせば埋まるか／打ち手は仕込まれているか
    #   ／その見込みでいくら埋まるか
    # 診断で終わる画面は、読んだあとに何も起きない。
    import actions

    gap = pnl.gap(conn, cfg)
    check("いくら足りないかが出る", gap is not None)
    check("当月の不足が予算と着地の差になっている",
          abs(gap["month"]["short"] - (gap["month"]["budget"] - gap["month"]["forecast"])) < 1.0,
          "予算 %.0f − 着地 %.0f" % (gap["month"]["budget"], gap["month"]["forecast"]))
    check("年間の過不足も出る", "year" in gap and "short" in gap["year"],
          "年間 %.0f円" % gap["year"]["short"])

    # **何をどれだけ動かせば埋まるか。** 判断は人がするが、材料は機械が出す。
    levers = {l["name"]: l for l in gap["levers"]}
    check("レバーが4本そろっている",
          set(levers) == {"売上", "粗利率", "人件費", "その他販管費"}, str(sorted(levers)))
    check("レバーの効き目が正の額で出ている",
          all(l["amount"] > 0 for l in gap["levers"]),
          "／".join("%s %.0f" % (k, v["amount"]) for k, v in levers.items()))
    # 検算：粗利率を0.1pt上げれば、残り期間の売上 × 0.1% ぶん営業利益が増える
    check("粗利率のレバーが、残り期間の売上と一致している",
          abs(levers["粗利率"]["amount"] - gap["remaining"]["sales"] * 0.001) < 1.0,
          "%.0f ／ %.0f" % (levers["粗利率"]["amount"], gap["remaining"]["sales"] * 0.001))
    check("人件費のレバーが、残り期間の人件費と一致している",
          abs(levers["人件費"]["amount"] - gap["remaining"]["labor"] * 0.01) < 1.0)
    check("不足を埋めるのに必要な動かし幅が出ている",
          all("needed" in l for l in gap["levers"]),
          "／".join("%s %s" % (k, v.get("needed")) for k, v in levers.items()))

    # ── 打ち手 ──────────────────────────────────────────
    mark33 = conn.execute("SELECT COALESCE(MAX(id),0) FROM records").fetchone()[0]
    ok33, msg33 = actions.add(conn, cfg, {
        "subject": "農産部", "lever": "粗利率", "expect": 4_000_000,
        "due": "2026-10-31", "owner": "農産課 佐藤",
        "text": "赤字取引を上位20件まで洗い出し、値上げ交渉か取引停止を部門長と決める。"})
    conn.commit()
    check("打ち手を登録できる", ok33, msg33)

    bad33 = [
        ("部門が対応表にない", {"subject": "存在しない部", "lever": "売上", "expect": 1e6,
                            "due": "2026-10-31", "owner": "誰か", "text": "なにか"}),
        ("レバーが一覧にない", {"subject": "農産部", "lever": "気合", "expect": 1e6,
                           "due": "2026-10-31", "owner": "誰か", "text": "なにか"}),
        ("見込み額がゼロ", {"subject": "農産部", "lever": "売上", "expect": 0,
                        "due": "2026-10-31", "owner": "誰か", "text": "なにか"}),
        ("期限が読めない", {"subject": "農産部", "lever": "売上", "expect": 1e6,
                        "due": "そのうち", "owner": "誰か", "text": "なにか"}),
        ("何をするかが空", {"subject": "農産部", "lever": "売上", "expect": 1e6,
                        "due": "2026-10-31", "owner": "誰か", "text": "   "}),
    ]
    for label, payload in bad33:
        got33, why33 = actions.add(conn, cfg, payload)
        check("打ち手を拒否する（%s）" % label, not got33, why33)

    # **見込みだけ積んでも埋まらない。** ギャップとの引き算まで出す。
    board = actions.board(conn, cfg)
    check("打ち手の見込み合計が出る", board["planned"] >= 4_000_000,
          "%.0f円" % board["planned"])
    check("まだ埋まっていない額が出る", "uncovered" in board,
          "%.0f円" % board["uncovered"])
    check("まだ埋まっていない額が、不足 − 見込みになっている",
          abs(board["uncovered"] - max(gap["year"]["short"] - board["planned"], 0.0)) < 1.0,
          "不足 %.0f − 見込み %.0f" % (gap["year"]["short"], board["planned"]))

    # **打ったつもりを許さない。** 期限が過ぎて動いていない打ち手は名指しする。
    ok33b, _ = actions.add(conn, cfg, {
        "subject": "水産部", "lever": "人件費", "expect": 2_000_000,
        "due": "2026-08-01", "owner": "水産課 高橋",
        "text": "夜間のシフトを1名減らし、翌朝へ寄せる。"})
    conn.commit()
    board2 = actions.board(conn, cfg)
    # デモにも期限切れが1件あるので、絶対数ではなく増えたぶんで見る
    check("期限が過ぎた打ち手が名指しされる",
          len(board2["overdue"]) == len(board["overdue"]) + 1,
          "%d件 → %d件" % (len(board["overdue"]), len(board2["overdue"])))
    check("期限切れは見込みから外れている", board2["planned"] < board["planned"] + 2_000_000,
          "%.0f円" % board2["planned"])

    # 効かなかったと分かった打ち手も、見込みから外す
    # ORDER BY を書かないと、索引の都合で逆順に返る（2026-09-02、実際に逆で返った）
    made33 = [r["id"] for r in conn.execute(
        "SELECT id FROM records WHERE kind='打ち手' AND id > ? ORDER BY id", (mark33,))]
    actions.advance(conn, made33[0], "効かなかった")
    conn.commit()
    board3 = actions.board(conn, cfg)
    check("効かなかった打ち手は見込みから外れる", board3["planned"] < board2["planned"],
          "%.0f円 → %.0f円" % (board2["planned"], board3["planned"]))
    # 効いたものの効果は、もう実績の側に出ている。**見込みにも足すと二重になる。**
    actions.advance(conn, made33[1], "効いた", 3_000_000)
    conn.commit()
    board4 = actions.board(conn, cfg)
    check("効いた打ち手を見込みに二重に数えない",
          board4["planned"] == board3["planned"] and board4["landed"] >= 3_000_000,
          "見込み %.0f ／ 効いた実績 %.0f" % (board4["planned"], board4["landed"]))
    check("状態は一覧のものしか受け付けない",
          not actions.advance(conn, made33[0], "たぶん効いた")[0])

    # 画面
    import build_dashboard as _bd
    _bd.build(instance)
    board33 = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")
    check("3つ目の問いが「どう埋めるか」になっている", "どう埋めるか" in board33)
    check("レバーの効き目が画面に出ている", "粗利率を" in board33 and "上げる" in board33)
    check("打ち手が画面に出ている", "赤字取引を上位20件" in board33)
    check("期限切れが画面で名指しされている", "期限が過ぎ" in board33)
    # **消したのではない。** 金の巡りは残っている
    check("CCCは金の巡りの節に残っている", "現金が戻ってくるまで" in board33)

    for row_id in made33:
        conn.execute("DELETE FROM records WHERE id=?", (row_id,))
    conn.commit()
    check("検査で入れた打ち手を片付けた",
          conn.execute("SELECT COUNT(*) FROM records WHERE kind='打ち手' AND id > ?",
                       (mark33,)).fetchone()[0] == 0)

    print("")
    print("【32】常時見えるのは数字。解説は、要るときだけ出す")
    # **毎日見る人には、解説は邪魔になる。** はじめての人には要る。
    # 消さずに畳む ── 限界を書いていない可視化は、いずれ誤読されるので。
    import re as _re4
    board32 = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")

    def _chars(cls, text):
        return sum(len(_re4.sub("<[^>]+>", "", h))
                   for h in _re4.findall(r'<p class="%s">(.*?)</p>' % cls, text, _re4.S))

    def _visible(text):
        """畳んである中（details の中）を除いた、常時見えている解説の量。"""
        outside = _re4.sub(r"<details.*?</details>", "", text, flags=_re4.S)
        return _chars("lead", outside) + _chars("legend", outside)

    check("常時見えている解説が300文字以内", _visible(board32) <= 300,
          "%d文字" % _visible(board32))
    # **消したのではなく、畳んだ。** 中身が残っていることを見る。
    check("限界の記述は消えていない（畳んだだけ）", "この画面が答えないこと" in board32)
    check("在庫の読み方も残っている", "期首 ＋ 仕入 − 売上原価" in board32)
    check("開く口がある", board32.count("<summary") >= 4,
          "%d箇所" % board32.count("<summary"))

    # 判定語は畳まない。**経営者が読む本体を隠したら、画面の意味が無い。**
    plain = _re4.sub(r"<details.*?</details>", "", board32, flags=_re4.S)
    check("年間の判定語は常時見えている", "予算とほぼ同じ線" in plain or "届く見通し" in plain
          or "届かない見通し" in plain)
    check("要確認の所見は常時見えている", "いま手を打つこと" in plain)

    # 触る画面には hover が無い。**ホバーだけに閉じ込めない。**
    tips = _re4.findall(r'<span class="hint"([^>]*)>', board32)
    check("補足の印が置いてある", len(tips) >= 4, "%d箇所" % len(tips))
    check("補足はキーボードでも開ける（tabindex がある）",
          all("tabindex" in t for t in tips), "%d箇所" % len(tips))
    css32 = board32[board32.index("<style>"):board32.index("</style>")]
    check("ホバーだけでなく focus でも出る",
          ".hint:focus" in css32 and ".hint:hover" in css32)
    # 吹き出しの中身が空なら、印は「押しても何も出ないもの」になる
    bodies = _re4.findall(r'<span class="tip">(.*?)</span>', board32, _re4.S)
    check("吹き出しに中身がある", bodies and all(len(_re4.sub("<[^>]+>", "", b)) > 20
                                        for b in bodies), "%d箇所" % len(bodies))
    # 右端の印は左へ開かないと画面から出る（2026-09-02、実測で3箇所がはみ出した）
    check("右端用の変種が用意されている", ".hint.tip-right .tip" in css32)
    check("右端用の変種が実際に使われている", "hint tip-right" in board32)
    # 同じ用語の印を、同じ節で繰り返さない（カード8枚に同じ印を出していた）
    labels = _re4.findall(r'aria-label="([^"]+)とは"', board32)
    dupes = [x for x in set(labels) if labels.count(x) > 2]
    check("同じ用語の印を繰り返していない", not dupes,
          "／".join("%s %d回" % (d, labels.count(d)) for d in dupes) or "重複なし")


    # **見出しは大中小の順に大きい。** 節が3つのときは目立たない寸法でよかったが、
    # 11節になると「どこが節の切れ目か」が目で分からなくなる。
    # 2026-09-05、h2（節）12.5px が h3（節の中）13.5〜15px より小さく、階層が逆だった。
    import re as _re5

    def _size(rule):
        # 規則の始まりだけを見る。コメントの中の "h3" を拾わないよう、行頭に錨を打つ
        # ── 2026-09-05、自分で書いたコメントの h3 を拾って別の値を返していた。
        m = _re5.search(r"^" + rule + r"[^{]*\{[^}]*font-size:([\d.]+)px",
                        css32, _re5.S | _re5.M)
        return float(m.group(1)) if m else None

    size1, size2, size3 = _size(r"\bh1"), _size(r"\bh2"), _size(r"\bh3")
    check("見出しの大きさが階層どおり（h1 > h2 > h3）",
          None not in (size1, size2, size3) and size1 > size2 > size3,
          "h1 %s / h2 %s / h3 %s" % (size1, size2, size3))
    inner = _size(r"\.panel-box h3")
    check("節の中の見出しが、節の見出しを超えない",
          inner is None or (size2 is not None and inner <= size2),
          "節 %s / 節の中 %s" % (size2, inner))
    # 節が増えるほど、切れ目が見えることが効く
    check("節の見出しに区切りが付いている", "border-bottom" in
          (_re5.search(r"\bh2[^{]*\{[^}]*\}", css32, _re5.S) or _re5.Match).group(0)
          if _re5.search(r"\bh2[^{]*\{[^}]*\}", css32, _re5.S) else False)


    # **記事が「大事な3つ」と言うなら、画面もそう見えていなければならない。**
    # 11節が同じ重さで並んでいると、どれが本題か読み手に伝わらない。
    heads = _re5.findall(r"<h2>(.*?)</h2>", board32, _re5.S)
    plain_heads = [_re5.sub("<[^>]+>", "", h).strip() for h in heads]
    check("節の見出しが並んでいる", len(heads) >= 8, "%d節" % len(heads))
    numbered = [h for h in heads if 'class="q"' in h]
    check("大事な3つに番号が振ってある", len(numbered) == 3, "%d件" % len(numbered))
    check("番号が 1・2・3 の順に並んでいる",
          [_re5.search(r'class="q">(\d)', h).group(1) for h in numbered] == ["1", "2", "3"])

    # 経営者が最初に見るのは着地。**締めを待たずに見えることが、この画面の値打ち。**
    order = {h: i for i, h in enumerate(plain_heads)}
    landing_at = next((i for h, i in order.items() if "着地の見通し" in h), None)
    first_q = next((i for h, i in order.items() if h.startswith("1")), None)
    check("着地の見通しが、3つの問いより前にある",
          landing_at is not None and first_q is not None and landing_at < first_q,
          "着地 %s / 1つ目 %s" % (landing_at, first_q))
    check("3つの問いが続けて並んでいる",
          first_q is not None
          and [i for h, i in order.items() if h[:1] in "123"] == [first_q, first_q + 1, first_q + 2],
          str([h[:20] for h in plain_heads[first_q:first_q + 3]]) if first_q is not None else "")


    # **全部が赤いと、どれも赤くない。** 誤差圏まで同じ濃さで塗ると、強調が効かなくなる
    # ── 2026-09-05、画面の赤が50箇所あり、-0.7% と -24.6% が同じ見た目だった。
    loud = [float(v.rstrip("%")) for v in
            _re5.findall(r'class="down">([+-][\d.]+)%<', board32)]
    soft = _re5.findall(r'class="down soft">', board32)
    check("小さなズレを強い赤で塗っていない",
          all(abs(v) >= screen.LOUD for v in loud),
          "強く塗った最小 %.1f%%（線は %.0f%%）" % (min(map(abs, loud)) if loud else 0, screen.LOUD))
    check("弱い赤も使われている（2段階になっている）", len(soft) >= 5, "%d箇所" % len(soft))
    check("強い赤が多すぎない", len(loud) <= 40, "%d箇所" % len(loud))

    check("JavaScriptは1行も無いまま", "<script" not in board32)

    print("")
    print("【31】Googleで入る ── 送り出して、戻りを検算する")
    # **部品があることと、動くことは別。** 実際にHTTPで往復させて、入れることまで見る。
    # 実テナントは無いので、判定の中に発行者を立てる（Google本番との疎通は別途）。
    import http.server as _hs
    import socketserver as _ss
    import threading as _th

    import oidc

    idp2 = oidc.TestIssuer(issuer="https://accounts.example.test",
                           audience="castle.apps.example.test")
    issued = {}

    class _Idp(_hs.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, body, code=200):
            raw = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self._json(idp2.jwks())

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))
            code = form.get("code", [""])[0]
            if code not in issued:
                return self._json({"error": "invalid_grant"}, 400)
            self._json({"id_token": issued[code], "token_type": "Bearer"})

    idp_server = _ss.TCPServer(("127.0.0.1", 0), _Idp)
    idp_port = idp_server.server_address[1]
    _th.Thread(target=idp_server.serve_forever, daemon=True).start()

    conf = instance / "config.json"
    keep_conf = conf.read_bytes()
    keep_users = (instance / "users.json").read_bytes() if (instance / "users.json").exists() else None
    secret_file = instance / "google_client_secret.txt"
    try:
        raw_cfg = json.loads(keep_conf.decode("utf-8"))
        raw_cfg["auth"] = dict(raw_cfg.get("auth", {}), **{
            "client_id": "castle.apps.example.test",
            "domain": "example-foods.co.jp",
            "issuer": idp2.issuer,
            "auth_endpoint": "http://127.0.0.1:%d/authorize" % idp_port,
            "token_endpoint": "http://127.0.0.1:%d/token" % idp_port,
            "jwks_uri": "http://127.0.0.1:%d/certs" % idp_port,
        })
        conf.write_text(json.dumps(raw_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        secret_file.write_text("test-secret", encoding="utf-8")
        (instance / "users.json").unlink(missing_ok=True)
        users.enroll(instance, "shacho@example-foods.co.jp", "社長")

        cfg31 = config_mod.load(instance)
        check("設定が埋まればGoogleの口が開く", oidc.configured(cfg31))
        check("ログイン画面にGoogleの口が出る",
              "/login/google" in serve.render_login_page(cfg31))

        srv31 = serve.make_server(instance, 0)
        _th.Thread(target=srv31.serve_forever, daemon=True).start()
        base31 = "http://127.0.0.1:%d" % srv31.server_address[1]
        try:
            import http.cookiejar
            jar31 = http.cookiejar.CookieJar()

            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None

            opener31 = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar31), _NoRedirect)
            try:
                opener31.open(base31 + "/login/google", timeout=10)
                where, code31 = "", 200
            except urllib.error.HTTPError as hop:
                where, code31 = hop.headers.get("Location", ""), hop.status
            check("Googleへ送り出す（302）", code31 == 302, "HTTP %d" % code31)
            sent = urllib.parse.parse_qs(urllib.parse.urlparse(where).query)
            for key in ("state", "nonce", "code_challenge", "client_id", "redirect_uri"):
                check("送り出しに %s が付いている" % key, key in sent, where[:60])
            check("PKCEの方式がS256", sent.get("code_challenge_method") == ["S256"])
            check("控えのクッキーが渡されている",
                  any(c.name == "castle_flow" for c in jar31))

            state31, nonce31 = sent["state"][0], sent["nonce"][0]
            issued["ok-code"] = idp2.token(email="shacho@example-foods.co.jp",
                                           hd="example-foods.co.jp", nonce=nonce31,
                                           lifetime=10 ** 9)
            issued["outsider"] = idp2.token(email="arubaito@example-foods.co.jp",
                                            hd="example-foods.co.jp", nonce=nonce31,
                                            lifetime=10 ** 9)

            def come_back(code, state):
                url = "%s/login/google/callback?%s" % (
                    base31, urllib.parse.urlencode({"code": code, "state": state}))
                try:
                    with opener31.open(url, timeout=15) as res:
                        return res.status
                except urllib.error.HTTPError as bad:
                    return bad.status

            # **戻りの控えが合わなければ弾く。** ここを飛ばすと、別のサイトから
            # 投げ込まれた戻りをそのまま信じてしまう。
            check("控えの合わない戻りは弾く", come_back("ok-code", "でたらめ") == 401)
            check("名簿に無い人は、署名が正しくても入れない",
                  come_back("outsider", state31) == 403)
            check("正しく戻れば入れる", come_back("ok-code", state31) == 200)
            check("入ったあとはセッションが配られる",
                  any(c.name == "castle_session" for c in jar31))
        finally:
            srv31.shutdown()
    finally:
        idp_server.shutdown()
        conf.write_bytes(keep_conf)
        secret_file.unlink(missing_ok=True)
        (instance / "users.json").unlink(missing_ok=True)
        if keep_users is not None:
            (instance / "users.json").write_bytes(keep_users)
    check("検査で触った設定と名簿を元に戻した",
          conf.read_bytes() == keep_conf and not secret_file.exists())

    print("")
    print("【30】在庫は月ごとに見る ── 期首から始まり、月の中で経過する")
    # **2ヶ月を1枚に並べる意味がない。** 在庫は期首（前月末）から始まって月の中で動き、
    # 月末に締まる。7月と8月を並べても、どちらの月の話をしているのか分からなくなる。
    #   8月の期首在庫 ＝ 7月末の期末在庫
    #   そこから日々  ＝ 期首 ＋ 仕入 − 売上原価
    #   棚卸の日には、実測に戻す
    view = pnl.stock_month(conn, cfg)
    check("在庫の画面が月の単位で組める", view is not None)
    check("描いているのは当月だけ", all(d.startswith(view["month"]) for d in view["days"]),
          "%s ／ %s 〜 %s" % (view["month"], view["days"][0], view["days"][-1]))

    # 期首は前月末。**当月の中には無い日付**なので、月をまたぐのはこの1点だけ。
    check("期首（前月末）が起点として置いてある", view["opening"] is not None,
          str(view["opening"] and view["opening"]["date"]))
    check("期首の日付が前月の末日である",
          view["opening"]["date"][:7] < view["month"], view["opening"]["date"])
    daily = pnl.daily_stock(conn, cfg)
    check("期首の値が、前月末の在庫と一致する",
          abs(view["opening"]["amount"] - daily[view["opening"]["date"]]["amount"]) < 1.0,
          "%.0f ／ %.0f" % (view["opening"]["amount"], daily[view["opening"]["date"]]["amount"]))

    settled = [p for p in view["points"] if p["settled"]]
    check("棚卸の日が確定として区別されている", len(settled) >= 2, "%d日" % len(settled))
    check("確定の日は、実測そのものが入っている",
          all(abs(p["amount"] - daily[p["date"]]["amount"]) < 1.0 for p in settled))

    # **確定と確定のあいだが、積み上げになっているかを検算する。**
    # 在庫日数のような比率で置くと、割る側が動いたぶん在庫が動いて見えて、波打つ。
    gaps = []
    for a, b in zip(view["points"], view["points"][1:]):
        want = a["amount"] + (b["moved_in"] or 0) - (b["moved_out"] or 0)
        if not b["settled"]:
            gaps.append(abs(want - b["amount"]))
    check("確定のあいだは、期首＋仕入−原価の積み上げになっている",
          not gaps or max(gaps) < 1.0, "最大のずれ %.1f円" % (max(gaps) if gaps else 0))

    amounts = [p["amount"] for p in view["points"]]
    swing = [abs(b - a) / a * 100 for a, b in zip(amounts, amounts[1:]) if a]
    check("日ごとに波打っていない（隣り合う日の変化が1%未満）",
          swing and max(swing) < 1.0, "最大 %.2f%%" % (max(swing) if swing else 0))

    # 描いていない先があることを画面に書く。**書かない可視化は、いずれ誤読される。**
    board30 = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")

    check("在庫の画面が当月だけを描いていると分かる", "当月の在庫" in board30)
    check("この先を描いていない理由が書いてある", "仕入の予定" in board30)
    # 期首は月日だけ描くので、フルの日付で数えると0件で通ってしまう。図の実物を数える。
    chart = board30[board30.index("当月の在庫"):]
    chart = chart[:chart.index("</svg>") + 6]
    check("期首の目印が置いてある",
          "期首" in chart and view["opening"]["date"][5:] in chart,
          "期首=%s" % view["opening"]["date"][5:])
    marks = _re3.findall(r">(\d{2})</text>", chart)
    inside = {q["date"][8:] for q in view["points"]}
    stray = [m for m in marks if m not in inside and m != view["opening"]["date"][8:]]
    check("当月と期首より前の日付が混ざっていない", not stray, "余計な日付 %s" % stray[:4])
    check("棚卸の日が図の中で名指しされている", chart.count("日 実測") >= 2,
          "%d箇所" % chart.count("日 実測"))

    # **傾きの理由を、傾きの真下に置く。**
    # 在庫の線だけでは「仕入れすぎ」と「売れなかった」を言い分けられない。
    # 2026-09-02、画面を見て「この山は何か」と問われた ── 問われた時点で、画面が
    # 仕事をしていない。日ごとの出入り（仕入 − 原価）を線の下に添える。
    check("日ごとの出入りが図に出ている", chart.count('class="flow"') >= len(view["points"]) - 1,
          "%d本" % chart.count('class="flow"'))
    # 出入りの符号と、線の傾きが一致していなければ、説明になっていない
    mismatched = []
    for a, b in zip(view["points"], view["points"][1:]):
        if b["settled"]:
            continue
        slope = b["amount"] - a["amount"]
        flow = b["moved_in"] - b["moved_out"]
        if (slope > 0) != (flow > 0) and abs(slope) > 1.0:
            mismatched.append(b["date"])
    check("出入りの向きと、在庫の傾きが一致している", not mismatched, str(mismatched[:3]))
    check("いちばん積み上がった日が名指しできる", "積み上がった" in board30 or "いちばん" in board30)


    print("")
    print("【28】名簿 ── 誰かは外が決め、何が見えるかは城が決める")
    # Google Workspace のアカウントで本人確認をする。城はアカウントを持たない。
    # **城が持つのは「このメールアドレスは、どの部門まで見てよいか」の対応表だけ。**
    # 入退社は情シスがGoogle側で済ませ、城は触らない ── そこが目的。
    roster = instance / "users.json"
    keep_roster = roster.read_bytes() if roster.exists() else None
    try:
        users.enroll(instance, "buchou@example-foods.co.jp", "農産部長", ["農産部"])
        users.enroll(instance, "shacho@example-foods.co.jp", "社長")
        check("メールアドレスで名簿に載せられる", len(users.load(instance)) == 2,
              "%d人" % len(users.load(instance)))
        check("名簿はメールアドレスで引ける",
              users.identify(instance, "buchou@example-foods.co.jp") is not None)
        check("大文字小文字は同じ人として扱う",
              users.identify(instance, "BUCHOU@Example-Foods.co.jp") is not None)
        check("範囲が名簿から引ける",
              users.scope_of(instance, "buchou@example-foods.co.jp") == ["農産部"],
              str(users.scope_of(instance, "buchou@example-foods.co.jp")))
        check("範囲を書かなければ全社",
              users.scope_of(instance, "shacho@example-foods.co.jp") is None)

        # **ここが要。** Googleで認証が通っても、名簿に無ければ城には入れない。
        # ドメインの中の全員が入れてしまうと、認可が消える。
        check("同じ会社のドメインでも、名簿に無ければ入れない",
              users.identify(instance, "arubaito@example-foods.co.jp") is None)
        check("外していないつもりの人も、外れていれば入れない",
              users.revoke(instance, "buchou@example-foods.co.jp")
              and users.identify(instance, "buchou@example-foods.co.jp") is None)
    finally:
        roster.unlink(missing_ok=True)
        if keep_roster is not None:
            roster.write_bytes(keep_roster)
    check("検査で触った名簿を元に戻した",
          (roster.read_bytes() if roster.exists() else None) == keep_roster)

    print("")
    print("【29】本人確認を外に出す ── 署名を確かめる")
    # **通ることは何の証明にもならない。** 通る側1つに対して、弾く側を6つ確かめる。
    # 実テナントは無いので、判定の中に発行者を立てて署名する
    # （Google本番との疎通は、この判定では確かめていない。導入時に確かめること）。
    import oidc

    idp = oidc.TestIssuer(issuer="https://accounts.example.test",
                          audience="castle-demo.apps.example.test")
    good = idp.token(email="shacho@example-foods.co.jp", hd="example-foods.co.jp",
                     nonce="n-0001")
    want = {"issuer": idp.issuer, "audience": idp.audience,
            "domain": "example-foods.co.jp", "nonce": "n-0001"}

    claims = oidc.verify(good, idp.jwks(), want, now=idp.now)
    check("正しく署名されたトークンは通る", claims and claims["email"] == "shacho@example-foods.co.jp",
          str(claims and claims.get("email")))

    reasons = []

    def rejected(label, token, **over):
        why = None
        try:
            oidc.verify(token, idp.jwks(), dict(want, **over), now=idp.now)
        except oidc.Rejected as stop:
            why = str(stop)
        if why:
            reasons.append(why)
        check(label, why is not None, why or "**通ってしまった**")

    # 署名を1文字だけ変える。**中身は正しいまま**なので、署名を見ていなければ通ってしまう。
    head, body, sig = good.split(".")
    swapped = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    rejected("署名を1文字変えたら弾く", "%s.%s.%s" % (head, body, swapped))
    rejected("中身を書き換えたら弾く（別人になりすます）",
             idp.tampered(good, {"email": "shacho@example-foods.co.jp"},
                          {"email": "arubaito@example-foods.co.jp"}))
    rejected("期限が切れていたら弾く",
             idp.token(email="shacho@example-foods.co.jp", hd="example-foods.co.jp",
                       nonce="n-0001", lifetime=-60))
    rejected("発行者が違えば弾く", good, issuer="https://accounts.google.com")
    rejected("宛先（このアプリ向けでない）なら弾く", good, audience="別のアプリ")
    rejected("投げ返しの合言葉が合わなければ弾く", good, nonce="n-9999")
    rejected("会社のドメインの外なら弾く",
             idp.token(email="dareka@gmail.com", hd="", nonce="n-0001"))
    # 鍵の取り違え。JWKSに複数の鍵があるとき、kid を見ずに総当たりすると
    # 「どれかで通れば通る」になる。**発行者が指した鍵で検証する。**
    rejected("別の鍵で署名されていたら弾く", idp.token(
        email="shacho@example-foods.co.jp", hd="example-foods.co.jp",
        nonce="n-0001", other_key=True))

    # 理由を残さない拒否は、原因が追えない。**理由が全部違うことまで見る**
    # ── 同じ文言を使い回していると、どこで弾いたか分からなくなる。
    check("弾いた理由がすべて残っている", len(reasons) == 8, "%d件" % len(reasons))
    check("理由が日本語で書かれている",
          all(any("぀" <= c <= "ヿ" or "一" <= c <= "鿿" for c in r)
              for r in reasons),
          "／".join(r[:18] for r in reasons[:3]))
    check("弾いた理由が種類ごとに違う", len(set(reasons)) >= 6,
          "%d通り／%d件" % (len(set(reasons)), len(reasons)))
    # 名簿と繋がっているか ── 署名が正しくても、名簿に無ければ入れない
    keep2 = roster.read_bytes() if roster.exists() else None
    try:
        roster.unlink(missing_ok=True)
        users.enroll(instance, "shacho@example-foods.co.jp", "社長")
        who = oidc.sign_in(instance, good, idp.jwks(), want, now=idp.now)
        check("署名が正しく、名簿にもいれば入れる", who and who["email"] == "shacho@example-foods.co.jp",
              str(who))
        outsider = idp.token(email="arubaito@example-foods.co.jp",
                             hd="example-foods.co.jp", nonce="n-0001")
        check("署名が正しくても、名簿に無ければ入れない",
              oidc.sign_in(instance, outsider, idp.jwks(), want, now=idp.now) is None)
    finally:
        roster.unlink(missing_ok=True)
        if keep2 is not None:
            roster.write_bytes(keep2)

    print("")
    print("【27】届いていないことに、気づけるか")
    # **静かに古いデータで画面が出るのが、いちばん悪い。**
    # 自動にするほど「今日も動いたはず」と思い込むので、鮮度は画面に出す。
    fresh = intake.freshness(conn, cfg)
    check("サイロごとの最終データ日が出る", len(fresh) >= 3,
          "／".join("%s %s" % (f["silo"], f["last"]) for f in fresh))
    check("周期が書いてある", all(f["cycle"] for f in fresh),
          "／".join("%s=%s" % (f["silo"], f["cycle"]) for f in fresh))
    check("いまは全部そろっている（遅れなし）", not [f for f in fresh if f["late"]],
          "遅れ: %s" % [f["silo"] for f in fresh if f["late"]])
    # **通ることは何の証明にもならない。** 遅れを作って、警告が出ることを見る。
    late = intake.freshness(conn, cfg, today="2027-06-30")
    check("日が経てば、遅れとして名指しされる",
          len(late) == len([f for f in late if f["late"]]),
          "遅れ %d／%d" % (len([f for f in late if f["late"]]), len(late)))
    check("遅れの日数が数えてある", all(f["behind"] > 0 for f in late if f["late"]),
          "／".join("%s %d日" % (f["silo"], f["behind"]) for f in late if f["late"]))

    board26 = (instance / "out" / "dashboard.html").read_text(encoding="utf-8")
    check("鮮度が画面に出ている", "データの届き具合" in board26)
    for f in fresh:
        check("画面に「%s」の届き具合が出ている" % f["silo"],
              f["silo"] in board26 and f["last"] in board26)


    return report()











def report():
    ng = [r for r in RESULTS if not r[0]]
    print("\n" + "=" * 60)
    print("判定 %d件中 %d件が通過、%d件が未達" % (len(RESULTS), len(RESULTS) - len(ng), len(ng)))
    for _, name, detail in ng:
        print("   未達: %s%s" % (name, ("  ── " + detail) if detail else ""))
    print("=" * 60 + "\n")
    return 1 if ng else 0


def main():
    """認証の有無で結果が変わらないよう、判定者が users.json の状態を握る。

    実運用の鍵は退避して、検査が終わったら必ず戻す。
    判定対象の状態を判定者が握っていないと、同じコードでも結果が変わる。
    """
    instance = db.instance_dir()
    store = instance / "users.json"
    backup = store.read_bytes() if store.exists() else None
    store.unlink(missing_ok=True)
    try:
        code = _run(instance)
    finally:
        store.unlink(missing_ok=True)
        if backup is not None:
            store.write_bytes(backup)

    # **判定は自分が汚した成果物を片づける。**
    # 検査中にサーバを叩くと、その時点のデータ（検査用の申し送りを含む）で
    # 画面が組み直され、ディスクに焼き付く。DBから消しても画面には残る。
    # 2026-09-01、作成者「検査用」の申し送りが出荷物に混ざっているのを画面で見つけた。
    # 消すのも組み直すのも、検査で使ったのとは別の接続で行う
    # ── 同じ接続だと、検査中の未確定の書き込みが見えたまま組み直してしまう。
    clean = db.connect(instance)
    try:
        clean.execute("DELETE FROM records WHERE kind='申し送り' AND body LIKE '%検査用%'")
        clean.commit()
    finally:
        clean.close()
    import build_dashboard
    html = build_dashboard.build(instance).read_text(encoding="utf-8")
    dirt = [w for w in ("検査用", "くらうど") if w in html]
    if dirt:
        print("成果物に検査の跡が残りました: %s" % dirt)
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
