#!/usr/bin/env python3
"""判定者。ループを回す前にこれを書く ── ループが収束するかは、ここが機械かどうかで決まる。

  python castle/app/verify.py

instance/data.db を作り直し、CSVを全部取り込み、ダッシュボードを生成して、
「機械が正誤を言えること」だけを検査する。人の判断が要ることは検査しない（できない）。

終了コード 0=全部通った / 1=落ちたものがある。
"""

import json
import re
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


def check(name, ok, detail=""):
    RESULTS.append((ok, name, detail))
    print("  %s %s%s" % ("OK  " if ok else "NG  ", name, ("  ── " + detail) if detail else ""))
    return ok


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

    # サイロが増えたぶんだけ具体的に指す。A社*.csv では売上と仕入を取り違える。
    steps = [
        ("売上", sorted(incoming.glob("A社*売上*.csv"))),
        ("仕入", sorted(incoming.glob("A社*仕入*.csv"))),
        ("労働時間", sorted(incoming.glob("B社*.csv"))),
        ("部門損益", sorted(incoming.glob("C社*部門別損益*.csv"))),
        ("試算表", sorted(incoming.glob("C社*試算表*.csv"))),
        ("残高", sorted(incoming.glob("C社*月末残高*.csv"))),
    ]
    for kind, files in steps:
        if not files:
            check("%s のCSVが存在する" % kind, False, "instance/incoming に見つからない")
            continue
        code, out = run(APP / "import_csv.py", "--kind", kind, *[str(f) for f in files])
        check("%s を取り込める（%d本）" % (kind, len(files)), code == 0, "" if code == 0 else out.strip().splitlines()[-1][:120])

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
            biscuit = cookies.get("castle_key")
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
    check("推移の切り替えが3枚ある（全社／部門別の額／部門別の率）",
          top.count('class="panel"') == 3, "CSSタブ")
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
    for word in ("推定", "傾向", "単発", "内訳"):
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
              "%s ≠ %s" % (rows[0].get("records") if rows else "—", made["expected"]))

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
          "目標比マイナスでも順調と出る（%s）のに、凡例が「目標以上」と書いている" % "、".join(loose))

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
          "売掛%.1f + 棚卸%.1f − 買掛%.1f ≠ %.1f"
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
    rng = book.get("buy_chart_range") or []
    check("仕入の推移が年をまたいでいない", len(rng) == 2 and rng[0][:4] == rng[1][:4],
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
          "低温食品部は1件しかないのに挙がっている")

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
          "まだ %d件 残っている" % (left[0]["count"] if left else 0))

    conn.execute("DELETE FROM records WHERE id > ?", (mark,))
    conn.commit()
    check("検査で入れた昇格の材料を片付けた",
          conn.execute("SELECT COUNT(*) FROM records WHERE id > ?", (mark,)).fetchone()[0] == 0)

    print("")
    print("【21】貸借と損益が繋がっているか")
    # 表示が壊れていないことと、数字が互いに整合していることは別。
    # 残高を流れと別々に作れば、CCCも運転資本も「それらしいだけの数字」になる。
    bal = pnl.load_free(conn, "残高")
    buys = pnl.load_purchase(conn)
    # この節より上で book は summary.json に差し替わっている。ここで組み直す。
    ledger = pnl.build(conn, cfg)
    by_day = {d["date"]: d for d in ledger["days"]}
    names = cfg.accounting["balance"]

    months = sorted(bal)
    checked = 0
    for n in range(1, len(months)):
        before, now = months[n - 1], months[n]
        if before[:4] != now[:4] or int(now[5:7]) != int(before[5:7]) + 1:
            continue
        days = sorted(d for d in by_day if d[:7] == now)
        if not days:
            continue
        # 在庫の積み上げ：前月末 ＋ 仕入 − 売上原価
        rolled = bal[before][names["棚卸資産"]]
        for day in days:
            rolled += buys.get(day, 0.0) - (by_day[day]["sales"] - by_day[day]["gross"])
        real = bal[now][names["棚卸資産"]]
        gap = abs(rolled - real) / real * 100 if real else 100.0
        checked += 1
        check("%s の棚卸資産が、仕入と売上原価の流れと繋がっている" % now, gap <= 1.5,
              "積み上げ %.2f億 ／ 会計 %.2f億 ／ ずれ %.1f%%"
              % (rolled / 1e8, real / 1e8, gap))
    check("繋がりを確かめられる月がある", checked >= 2, "%d月ぶん" % checked)

    # 売掛金：前月末 ＋ 売上 − 回収。回収は持っていないので、回転日数が暴れないことだけ見る
    ratios = []
    for month_key in months:
        days = [d for d in by_day if d[:7] == month_key]
        if not days:
            continue
        sales = sum(by_day[d]["sales"] for d in days)
        if sales:
            ratios.append(bal[month_key][names["売掛金"]] / sales)
    check("売掛金と売上の比が、月ごとに暴れていない",
          bool(ratios) and (max(ratios) - min(ratios)) / (sum(ratios) / len(ratios)) < 0.30,
          "月商比 %s" % "／".join("%.2f" % r for r in ratios))

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
        return _run(instance)
    finally:
        store.unlink(missing_ok=True)
        if backup is not None:
            store.write_bytes(backup)


if __name__ == "__main__":
    sys.exit(main())
