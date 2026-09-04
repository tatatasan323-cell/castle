"""出口③：経営者の一枚。**今月いくらで終わるのか**を、締めを待たずに出す。

  python castle/app/build_dashboard.py            → instance/out/dashboard.html
                                                     instance/out/summary.json（判定用）

この画面は判断しない。数字と、その内訳と、確認すべきことまでを並べる。
「どこが落ちたか」は割り算で出る。「なぜ落ちたか」は現場にしかないので書かない。

3本目のサイロ（会計）が入ったので、指標は粗利ベース。
月次でしか締まらない原価は、**率に直してから日次売上に当てる**。金額を日割りしない
── 日ごとの原価は、どのシステムも持っていないから。
"""

import argparse
import datetime
import json
import pathlib
import string
import sys
from html import escape

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import config as config_mod
import db
import intake
import actions
import knowledge
import pnl
import screen

TEMPLATE = db.ROOT / "castle" / "templates" / "dashboard.html"
# 見た目は1枚にまとめてビルド時に差し込む。実行時の外部ファイル読み込みはゼロのまま。
THEME = db.ROOT / "castle" / "templates" / "theme.css"
# 全社の節と推移の節は別ファイル。部門を絞ったら、節ごと出さない／別の版を出す。
# 条件分岐をmarkupに書かず、見せるものが違うなら別のテンプレートにする。
PART = db.ROOT / "castle" / "templates"
# 凡例の日数と、実際の窓を1か所から出す。別々に書くと必ず食い違う。

# サーバ配信のときだけ出す。単体ファイルとして配ったときにリンク切れを作らないため。
# ナビは screen.nav に集約した。同じ並びを書き分けると、片方だけ欠ける。


# ---------------------------------------------------------------- 集計

def margin_trend(rows):
    """確定した直近2回の棚卸から、粗利率の動き。

    月次の会計から取っていた頃は、当月内で粗利率が1ミリも動かなかった。
    棚卸が週次で来るので、**週ごとに動く** ── 「粗利率が続けて下がっている」が言える。
    """
    if not rows or len(rows) < 2:
        return None
    (before, cost_before), (now, cost_now) = rows[-2], rows[-1]
    return before, 1 - cost_before, now, 1 - cost_now


def load_daily(conn, targets, weekly, monthly):
    """日付×部門で売上・労働時間・粗利を揃える。骨が同じなので結合はこれだけで済む。"""
    rows = conn.execute(
        """SELECT occurred_at AS d, subject AS s,
                  SUM(CASE WHEN kind='売上'     THEN amount END) AS sales,
                  SUM(CASE WHEN kind='労働時間' THEN hours  END) AS hours
             FROM records WHERE kind IN ('売上','労働時間')
            GROUP BY occurred_at, subject"""
    ).fetchall()

    data, half, estimated, rate_months = {}, 0, set(), {}
    for row in rows:
        if row["sales"] is None or row["hours"] is None or row["hours"] <= 0:
            if row["s"] in targets:
                half += 1
            continue
        # 原価率は棚卸から（pnl に1本化）。同じ元から取らないと、画面と階段がずれる。
        cost_rate, is_estimate, source = pnl.rate_at(weekly, monthly, row["s"], row["d"])
        if cost_rate is None:
            continue
        rate = 1 - cost_rate
        if is_estimate:
            estimated.add(row["d"])
        rate_months[(row["s"], row["d"])] = source
        data.setdefault(row["s"], {})[row["d"]] = (float(row["sales"]), float(row["hours"]), row["sales"] * rate)
    return data, half, estimated, rate_months


def shift(day, days):
    return (datetime.date.fromisoformat(day) - datetime.timedelta(days=days)).isoformat()


def agg(series, dates):
    sales = hours = gross = 0.0
    hit = 0
    for day in dates:
        if day in series:
            sales += series[day][0]
            hours += series[day][1]
            gross += series[day][2]
            hit += 1
    return {
        "sales": sales, "hours": hours, "gross": gross, "days": hit,
        "pph": gross / hours if hours else None,
        "sales_pph": sales / hours if hours else None,
        "margin": gross / sales if sales else None,
    }


def weekly(series):
    """月曜起点で束ねる。5営業日以上ある週だけを「完全な週」として扱う。"""
    weeks = {}
    for day, (sales, hours, gross) in series.items():
        date = datetime.date.fromisoformat(day)
        monday = (date - datetime.timedelta(days=date.weekday())).isoformat()
        bucket = weeks.setdefault(monday, [0.0, 0.0, 0.0, 0])
        bucket[0] += sales
        bucket[1] += hours
        bucket[2] += gross
        bucket[3] += 1
    return {m: v for m, v in weeks.items() if v[3] >= 5}


def change(current, base):
    return None if not base or current is None else (current / base - 1) * 100


def analyse(series, target, dates, metric):
    """1部門ぶんの判断材料。ここで出すのは数字だけで、良し悪しは判定しない。"""
    window = metric["window_days"]
    cur = agg(series, dates[-window:])
    prev = agg(series, dates[-2 * window : -window])
    yoy = agg(series, [shift(d, metric["yoy_offset_days"]) for d in dates[-window:]])

    weeks = weekly(series)
    recent = [m for m in sorted(weeks) if m >= shift(dates[-1], 100)]
    steps = []
    for a, b in zip(recent[-4:], recent[-3:]):
        pa = weeks[a][2] / weeks[a][1] if weeks[a][1] else None
        pb = weeks[b][2] / weeks[b][1] if weeks[b][1] else None
        steps.append(change(pb, pa))

    return {
        "pph": cur["pph"], "sales_pph": cur["sales_pph"], "margin": cur["margin"],
        "sales": cur["sales"], "hours": cur["hours"], "gross": cur["gross"], "days": cur["days"],
        "target": target,
        "vs_target": change(cur["pph"], target),
        "vs_prev": change(cur["pph"], prev["pph"]),
        "vs_yoy": change(cur["pph"], yoy["pph"]),
        "sales_move": change(cur["sales"], prev["sales"]),
        "hours_move": change(cur["hours"], prev["hours"]),
        "margin_move": None if cur["margin"] is None or prev["margin"] is None
                       else (cur["margin"] - prev["margin"]) * 100,
        # 3週続けて意味のある幅で下がっていれば「傾向」。ノイズ幅の上下は傾向と呼ばない。
        "trend": len(steps) == 3 and all(s is not None and s <= -metric["trend_step_ratio"] * 100 for s in steps),
        "weeks": [(m, weeks[m][2] / weeks[m][1]) for m in recent],
    }


# ---------------------------------------------------------------- 描画

def yen(value):
    return "—" if value is None else "{:,.0f}".format(value)




# 同じ書き方が2箇所にあると、片方だけ直したときに画面の中で塗り分けが食い違う
# ── 2026-09-05、赤を2段階にしたのに週次カードだけ濃いままだった。screen に寄せる。
pct = screen.pct


def momentum(weeks, span=3):
    """勢い。直近3週の平均と、その前3週の平均を比べる（%）。

    1週だけの上下はノイズなので傾きに使わない。「3週まとめて上か下か」で見る。
    """
    vals = [v for _, v in weeks if v is not None]
    if len(vals) < 2:
        return 0.0
    k = min(span, len(vals) // 2)
    recent = sum(vals[-k:]) / k
    before = sum(vals[-2 * k:-k]) / k
    return (recent / before - 1) * 100 if before else 0.0


# 良し悪しは2軸でしか決まらない ── 水準（目標に届いているか）と 向き（上がっているか）。
# 1軸だけで判定すると、「ほぼ目標線上で横ばい」と「大きく未達で落ち続けている」が
# 同じ言葉になる。そうなった瞬間、本当に手を打つべき部門が埋もれる。
#
#          上向き   横ばい   下向き
#   達成 |  順調     順調     失速
#   僅差 |  順調     失速     失速
#   未達 |  挽回中   要対処   要対処
VERDICTS = ("要対処", "失速", "挽回中", "順調")
VERDICT_WHY = {
    "要対処": "目標に届かず、戻る気配がない",
    "失速": "水準は保っているが、下を向いている",
    "挽回中": "目標未達だが、上を向いている",
    "順調": "目標を満たし、落ちていない",
}
NEAR_MISS = -5.0   # ここまでは「僅差」。誤差と本当の未達を混ぜない
FLAT = 1.0         # 3週平均どうしの比較なので、この幅の上下は向きと呼ばない


def verdict(vs_target, mom):
    level = "達成" if (vs_target or 0) >= 0 else ("僅差" if (vs_target or 0) >= NEAR_MISS else "未達")
    move = "上" if (mom or 0) >= FLAT else ("下" if (mom or 0) <= -FLAT else "平")
    if level == "未達":
        return "挽回中" if move == "上" else "要対処"
    if move == "下" or (level == "僅差" and move == "平"):
        return "失速"
    return "順調"


def sparkline(weeks, prior, target, width=280, height=94):
    """目盛りのある折れ線。

    スケールの無い線は「なんとなく上がっている」しか言えない。
    縦は上下端の実数、横は最初と最後の週、目標線には目標値を必ず添える。
    """
    left, right, top, bottom = 40, 6, 12, 66
    values = [v for _, v in weeks] + [v for v in prior if v is not None] + [target]
    lo, hi = min(values), max(values)
    span = (hi - lo) or (hi or 1)
    lo, hi = lo - span * 0.18, hi + span * 0.18

    def y_of(value):
        return bottom - (bottom - top) * ((value - lo) / (hi - lo))

    def points(series):
        out = []
        for i, value in enumerate(series):
            if value is None:
                continue
            x = left + (width - left - right) * (i / max(len(series) - 1, 1))
            out.append("%.1f,%.1f" % (x, y_of(value)))
        return " ".join(out)

    def md(monday):
        return "%d/%d" % (int(monday[5:7]), int(monday[8:10]))

    ty = y_of(target)
    parts = [
        '<svg viewBox="0 0 %d %d" role="img" aria-label="週次推移">' % (width, height),
        # 縦の目盛り。上下端の実数を置く
        '<text class="ax" x="%d" y="%.1f" text-anchor="end">%s</text>' % (left - 6, top + 3, yen(hi)),
        '<text class="ax" x="%d" y="%.1f" text-anchor="end">%s</text>' % (left - 6, bottom + 3, yen(lo)),
        # 目標線と、その値
        '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--warn)" stroke-width="1.2" '
        'stroke-dasharray="4 3" opacity=".8"/>' % (left, ty, width - right, ty),
        '<text class="axt" x="%d" y="%.1f">目標 %s</text>'
        % (left + 2, ty - 4 if ty > top + 14 else ty + 11, yen(target)),
        # 前年（奥に退く線）→ 当年（手前の光る線）の順で重ねる
        '<polyline class="prior" fill="none" stroke="var(--prior)" stroke-width="1.6" points="%s"/>' % points(prior),
        '<polyline fill="none" stroke="var(--accent)" stroke-width="2.4" stroke-linejoin="round" '
        'stroke-linecap="round" points="%s"/>' % points([v for _, v in weeks]),
        # 横の目盛り
        '<text class="axx" x="%d" y="%d">%s</text>' % (left, height - 6, md(weeks[0][0])),
        '<text class="axx" x="%d" y="%d" text-anchor="end">%s</text>'
        % (width - right, height - 6, md(weeks[-1][0])),
        "</svg>",
    ]
    return "".join(parts)


def build_ranking(results):
    scale = max(max(r["pph"], r["target"]) for r in results.values()) * 1.08
    sales_order = {n: i for i, (n, _) in enumerate(sorted(results.items(), key=lambda kv: -kv[1]["sales_pph"]), 1)}
    rows = []
    for rank, (name, r) in enumerate(sorted(results.items(), key=lambda kv: -kv[1]["pph"]), 1):
        gap = sales_order[name] - rank
        move = '<span class="up">▲%d</span>' % gap if gap > 0 else ('<span class="down">▼%d</span>' % -gap if gap < 0 else "—")
        rows.append(
            '<tr><td class="name">%s</td><td><b>%s</b></td>'
            '<td class="barcell"><div class="bar"><span class="fill" style="width:%.1f%%"></span>'
            '<i class="target" style="left:%.1f%%"></i></div></td>'
            "<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (escape(name), yen(r["pph"]), 100 * r["pph"] / scale, 100 * r["target"] / scale,
               pct(r["vs_target"]), "%.1f%%" % (r["margin"] * 100), yen(r["sales_pph"]), move)
        )
    return (
        '<table><thead><tr><th class="name">部門</th><th>粗利/人時</th><th class="barcell"></th>'
        "<th>目標比</th><th>粗利率</th><th>売上/人時</th><th>売上基準からの順位変動</th>"
        "</tr></thead><tbody>%s</tbody></table>" % "".join(rows)
    )




def load_notes(conn, since):
    """入口②から入った申し送り。ダッシュボードが答えない「なぜ」の側。"""
    return conn.execute(
        """SELECT occurred_at, subject, created_by,
                  json_extract(body,'$.text')     AS text,
                  json_extract(body,'$.category') AS category
             FROM records WHERE kind='申し送り' AND occurred_at >= ?
            ORDER BY occurred_at DESC, id DESC""", (since,)).fetchall()


def note_html(rows):
    """人が書いたものをHTMLに置く唯一の場所。ここでエスケープを外すと自分で穴を開ける。"""
    return "".join(
        '<div class="note"><div class="meta">%s ／ <b>%s</b> ／ %s ／ %s</div>%s</div>'
        % (escape(r["occurred_at"]), escape(r["subject"]), escape(r["category"] or ""),
           escape(r["created_by"]), escape(r["text"] or ""))
        for r in rows)


def knowledge_html(rows):
    """知識も、人が書いたもの。HTMLに置く場所は1箇所に閉じ、必ずエスケープする。"""
    out = []
    for row in rows:
        body = knowledge.body_of(row)
        out.append(
            '<div class="know"><div class="meta">%s ／ %s ／ %s</div>'
            '<div class="ess">%s</div><div class="sub">なぜ：%s</div>'
            '<div class="sub">使い方：%s</div></div>'
            % (escape(body.get("type", "")), escape(row["subject"] or ""), escape(row["created_by"] or ""),
               escape(body.get("essence", "")), escape(body.get("why", "")), escape(body.get("how", ""))))
    return "".join(out)


def build_trends(entries, series_by_name, metric):
    """カードを作る。良いほうから並べない ── 手を打つべき部門を先に見せる。"""
    cards = []
    for name, r in entries:
        weeks_prior = weekly(series_by_name[name])
        prior = []
        for monday, _ in r["weeks"]:
            bucket = weeks_prior.get(shift(monday, metric["yoy_offset_days"]))
            prior.append(bucket[2] / bucket[1] if bucket and bucket[1] else None)
        mom = momentum(r["weeks"])
        cards.append({
            "name": name, "pph": r["pph"], "target": r["target"],
            "vs_target": r["vs_target"] or 0.0, "momentum": mom,
            "verdict": verdict(r["vs_target"], mom),
            "svg": sparkline(r["weeks"], prior, r["target"]),
        })

    rank = {v: i for i, v in enumerate(VERDICTS)}
    head, rest = cards[:1], cards[1:]
    rest.sort(key=lambda c: (rank[c["verdict"]], c["vs_target"]))
    cards = head + rest

    html = "".join(
        '<div class="spark c%d"><div class="t"><b>%s</b>'
        '<span class="chip c%d" title="%s">%s</span></div>'
        '<div class="now">%s <em>円/人時</em></div>%s'
        '<div class="f"><span>目標比 %s</span><span>直近3週 %s</span></div></div>'
        % (rank[c["verdict"]] + 1, escape(c["name"]), rank[c["verdict"]] + 1,
           VERDICT_WHY[c["verdict"]], c["verdict"], yen(c["pph"]), c["svg"],
           pct(c["vs_target"]), pct(c["momentum"]))
        for c in cards)
    return html, [{k: v for k, v in c.items() if k != "svg"} for c in cards]


# ---------------------------------------------------------------- 組み立て

def main():
    parser = argparse.ArgumentParser(description="経営ステータスの一枚を書き出す")
    parser.add_argument("--instance")
    args = parser.parse_args()
    build(db.instance_dir(args.instance), verbose=True)


LIMITED = ('<div class="notice">この画面は <b>%s</b> のぶんだけです。'
           '全社の数字（着地見込み・利益の階段・金の巡り）は、'
           '<b>全社の鍵でなければ開きません。</b></div>')


def _stock_view(book):
    """在庫の要点。判定と、記事との突き合わせが読む。"""
    daily = book.get("stock") or {}
    if not daily:
        return None
    days = sorted(daily)
    settled = max((d for d in days if daily[d]["settled"]), default=None)
    if settled is None:
        return None
    return {"settled_at": settled, "settled_amount": daily[settled]["amount"],
            "settled_days": daily[settled]["days_of_stock"],
            "latest_at": days[-1], "latest_amount": daily[days[-1]]["amount"],
            "latest_days": daily[days[-1]]["days_of_stock"],
            "guessed_days": sum(1 for d in days if d > settled)}


def scale_of(conn, cfg, book):
    """記事が引用する「規模」の数字を、成果物の側で持つ。

    **記事に書き写した数字は、書いた瞬間から古くなる。** ファイル数も行数も
    取り込み件数も、作業のたびに動く。人が覚えて書き直すのは必ず漏れるので、
    ここで数えて summary.json に載せ、記事の照合（crosscheck_article.py）に使う。
    """
    root = db.ROOT
    skip = {"__pycache__", ".git", "out", "backups", "incoming", "図解"}
    files = [f for f in root.rglob("*")
             if f.is_file() and f.suffix in (".py", ".html", ".css", ".json", ".md", ".sql", ".bat")
             and not (set(f.parts) & skip)]
    lines = sum(len(f.read_text(encoding="utf-8", errors="replace").splitlines()) for f in files)

    taken = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(rows_ok),0) AS rows FROM import_log"
        " WHERE undone_at IS NULL").fetchone()
    records = conn.execute(
        "SELECT COUNT(*) FROM records WHERE created_by LIKE 'import/%'").fetchone()[0]

    # 間接部門が「その他販管費」に占める割合。売上を持たない部門を外すと消える額。
    monthly = pnl.load_monthly(conn)
    direct = {d["name"] for d in cfg.measured()}
    last = max((m for rows in monthly.values() for m in rows), default=None)
    total = indirect = 0.0
    for name, rows in monthly.items():
        v = rows.get(last)
        if not v:
            continue
        total += v["sga"]
        if name not in direct:
            indirect += v["sga"]

    # 積み上げた値が、次の棚卸の実データにどれだけ着地したか（いちばん外した週）。
    # 判定【21】と同じ測り方 ── 棚卸の前日の推定値を、その棚卸の実数と比べる。
    daily = pnl.daily_stock(conn, cfg)
    counted = pnl.load_stock(conn)
    worst = 0.0
    stamps = sorted(counted)
    for before, now in zip(stamps, stamps[1:]):
        if (datetime.date.fromisoformat(now)
                - datetime.date.fromisoformat(before)).days > 60:
            continue
        prev_day = max((d for d in daily if d < now), default=None)
        if prev_day is None:
            continue
        worst = max(worst, abs(daily[prev_day]["amount"] - counted[now]) / counted[now] * 100)

    return {"files": len(files), "lines": lines,
            "imports": taken["n"], "imported_rows": records,
            "indirect_sga_share": (indirect / total * 100) if total else 0.0,
            "stock_drift": worst}


def build(instance, verbose=False, nav=False, scope=None):
    """scope に部門名の一覧を渡すと、その部門ぶんだけを描く。

    None は全社。**部門を絞ったときは、全社の集計を出さない** ──
    部門別の欄だけを見せて全社の合計を残すと、引き算で全社が復元できてしまう。
    """
    conn = db.connect(instance)
    cfg = config_mod.load(instance)
    metric = cfg.metric

    weekly = pnl.weekly_cost_rates(conn)
    monthly = pnl.load_monthly(conn)
    if not weekly and not monthly:
        raise SystemExit("棚卸も会計も入っていません。原価率が出せません。")

    data, half, estimated, rate_months = load_daily(
        conn, {d["name"] for d in cfg.measured()}, weekly, monthly)
    measured = [d for d in cfg.measured() if d["name"] in data]
    if not measured:
        raise SystemExit("売上・労働時間・原価の3つが揃っている部門がありません。")
    if scope is not None:
        allowed = set(scope)
        measured = [d for d in measured if d["name"] in allowed]
        if not measured:
            raise SystemExit("この鍵で見てよい部門が、データの中にありません。")

    dates = sorted({d for dept in measured for d in data[dept["name"]]})
    window = metric["window_days"]
    if len(dates) < window * 2:
        raise SystemExit("営業日が %d日しかありません。前週比を出すには %d日必要です。" % (len(dates), window * 2))

    results = {d["name"]: analyse(data[d["name"]], d["target"], dates, metric) for d in measured}

    # 全社は部門の平均ではなく、合計 ÷ 合計。規模の違う部門を平らに扱わないため。
    total = {}
    for day in dates:
        parts = [data[d["name"]].get(day) for d in measured]
        parts = [p for p in parts if p]
        if parts and sum(p[1] for p in parts):
            total[day] = tuple(sum(p[i] for p in parts) for i in range(3))
    whole_target = (sum(results[d["name"]]["hours"] * d["target"] for d in measured)
                    / sum(results[d["name"]]["hours"] for d in measured))
    whole = analyse(total, whole_target, dates, metric)
    whole_label = "全社（対象%d部門）" % len(measured)

    series_by_name = {d["name"]: data[d["name"]] for d in measured}
    series_by_name[whole_label] = total
    trend_entries = [(whole_label, whole)] + sorted(results.items(), key=lambda kv: -kv[1]["pph"])

    trends_html, trend_cards = build_trends(trend_entries, series_by_name, metric)
    est_in_window = sorted(d for d in dates[-window:] if d in estimated)
    trends = {name: margin_trend(weekly.get(name)) for name in results}

    # ── 経営者の問いに答える計算。ここがこの画面の主役になる ──
    book = pnl.build(conn, cfg, metric["yoy_offset_days"])
    year = pnl.build_year(conn, cfg, metric["yoy_offset_days"])
    if scope is not None:
        # 計算は全社ぶん回るが、**画面に渡すのは見てよい部門だけ**にする。
        # ここを絞り忘れると、全社の集計を伏せても部門別の欄から復元できてしまう。
        book = dict(book, departments={k: v for k, v in book["departments"].items()
                                       if k in set(scope)})
    month = book["month"]
    if month is None:
        raise SystemExit("当月のデータがありません。")

    if month["estimated"]:
        notice = ('<div class="notice"><b>当月の原価率と人時単価は、直近の確定月のものです。</b>'
                  "会計が当月を締めるのは翌月10日ごろ。売上と労働時間は実績です。"
                  "<b>その締めを待たずに着地を出すのが、この画面の役目です。</b></div>")
    else:
        notice = ""

    excluded = [d for d in cfg.departments if not d.get("productivity")]
    limits = [
        "<li><b>着地見込みは予測モデルではない。</b>「前年同月の残り日数ぶんに、今年の進み具合（%.3f倍）を掛ける」"
        "という比例配分だけ。人件費は直近の日次平均、固定費は月額の日割り。"
        "<b>だから外れ方も説明できる</b> ── 前年と違う動きをした月は、そのぶんずれる。</li>" % month["pace"],
        "<li><b>当月の原価率と人時単価は推定。</b>%s</li>" % metric["note"],
        "<li><b>%s は対象外。</b>売上を持たないため、この割り算が成立しない。人時は全社の分母に入っていない。</li>"
        % "・".join(d["name"] for d in excluded),
        "<li><b>目標値は設定値であって、実績から導いたものではない。</b>config.json に手で置いてある。目標そのものの妥当性はこの画面では検証できない。</li>",
        "<li><b>粗利率の変化は、月が締まった瞬間に段差として現れる。</b>"
        "当月は直近の確定月の率を当てているので、週単位では粗利率は動かない。"
        "動きを見たいときは、要確認欄の「確定した粗利率の推移」を見る。</li>",
        "<li><b>「なぜ」は書いていない。</b>落ちた部門と、売上・粗利率・人時のどこが動いたかまでは数字で出る。理由は現場にしかない。</li>",
        "<li><b>取り込んだ時点のスナップショット。</b>常時更新ではない。再取り込みと再生成が要る。</li>",
    ]
    if half:
        limits.append("<li>売上と労働時間の片方しか無い日付×部門が %d件あり、除外した。</li>" % half)

    notes = load_notes(conn, shift(dates[-1], 30))
    if scope is not None:
        notes = [r for r in notes if r["subject"] in set(scope)]
    notes_by_dept = {}
    for row in notes:
        notes_by_dept.setdefault(row["subject"], []).append(row)

    # 知識は探しに行かせない。落ち込んだ部門の隣に、その部門の知識を出す。
    know_by_dept = {}
    for dept in measured:
        found = knowledge.related(conn, dept["name"])
        if found:
            know_by_dept[dept["name"]] = found

    # 検知の隣に、その場の事情（申し送り）と、この会社の蓄積（知識）を添える
    attached = {}
    for dept in measured:
        name = dept["name"]
        blocks = []
        if notes_by_dept.get(name):
            blocks.append('<div class="attached"><div class="cap">現場からの申し送り</div>%s</div>'
                          % note_html(notes_by_dept[name]))
        if know_by_dept.get(name):
            blocks.append('<div class="attached"><div class="cap">この部門の知識（知識の泉より）</div>%s</div>'
                          % knowledge_html(know_by_dept[name]))
        if blocks:
            attached[name] = "".join(blocks)

    records = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    batches = conn.execute("SELECT COUNT(*) FROM import_log WHERE undone_at IS NULL").fetchone()[0]
    current = [d for d in dates if d > shift(dates[-1], 180)]
    stats = ("骨に載っているレコード %s件／取り込みバッチ %d／サイロ3本（販売管理・勤怠・会計）／"
             "対象部門 %d（除外 %d）／営業日 %d日（%s 〜 %s）＋前年同期 %d日"
             % (yen(records), batches, len(measured), len(excluded), len(current), current[0], current[-1],
                len(dates) - len(current)))

    # 部門別の推移は**月次**で並べる。日次を2ヶ月半だらだら並べても、経営の視点にはならない
    # ── 会社の損益は月ごとに確定するので、部門の良し悪しも月の単位で判断する。
    # 12ヶ月あれば季節が見える。3ヶ月の日次では、季節と曜日の区別すらつかない。
    monthly = pnl.load_monthly(conn)
    chart_months = sorted({m for rows in monthly.values() for m in rows})[-13:]
    buy_by_day = dict(book["cash"]["purchase"]["series"]) if book["cash"] else {}
    stock_by_day = book.get("stock") or {}
    amount_series, rate_series = [], []
    for dept in sorted(measured, key=lambda d: -results[d["name"]]["gross"]):
        name = dept["name"]
        rows = monthly.get(name, {})
        gross, rate = [], []
        for key in chart_months:
            v = rows.get(key)
            gross.append(None if v is None else v["sales"] - v["cost"])
            rate.append(None if not v or not v["sales"]
                        else (v["sales"] - v["cost"]) / v["sales"] * 100)
        amount_series.append((name, gross))
        rate_series.append((name, rate))

    dept_charts = {
        "dept_amount": screen.series_chart(chart_months, amount_series,
                                           lambda v: "%.1f億" % (v / 1e8), "c-amount"),
        "dept_rate": screen.series_chart(chart_months, rate_series,
                                         lambda v: "%.1f%%" % v, "c-rate"),
        "chart_months": len(chart_months),
    }
    # 在庫は**当月だけ**を描く（期首＝前月末の1点だけ添える）。
    # 2ヶ月を1枚に並べても、どちらの月の話をしているのか分からなくなる。
    _board = actions.board(conn, cfg, scope=scope)
    stock_view = pnl.stock_month(conn, cfg)
    stock_days = (stock_view or {}).get("days") or []

    if scope is None:
        trend_block = string.Template(
            (PART / "_trend.html").read_text(encoding="utf-8")).substitute(
            voyage=screen.voyage(month),
            actual_days=month["actual_days"], remaining_days=month["remaining_days"],
            year_view=screen.year_view(year), year_verdict=screen.year_verdict(year),
            **dept_charts)
        company_block = string.Template(
            (PART / "_company.html").read_text(encoding="utf-8")).substitute(
            freshness=screen.freshness(
                intake.freshness(conn, cfg),
                (cfg.sources or {}).get("基準日")
                or datetime.date.today().isoformat()),
            trend_block=trend_block,
            hint_ladder=screen.hint("段階利益"),
            hint_cash=screen.hint("運転資本"),
            hint_stock=screen.hint("期首"),
            closing=screen.closing(pnl.gap(conn, cfg, metric["yoy_offset_days"]), _board),
            landing=screen.landing(month),
            breakdown=screen.breakdown(month),
            ladder=screen.ladder(book),
            cash=screen.cash(book),
            purchase=screen.purchase(book) + screen.stock(book),
            # 仕入の線は外した。金額は上のカードに出ているうえ、
            # 在庫を積み上げで置くようにしたので、仕入の線は在庫の線の微分になる。
            stockchart=screen.stock_month(pnl.stock_month(conn, cfg)),
            stock_month=(pnl.stock_month(conn, cfg) or {}).get("month", ""),
        )
        howto_block = (PART / "_howto.html").read_text(encoding="utf-8")
        health_block = ('<section><h2>正常に回っているか%s</h2>%s</section>'
                        % (screen.hint("人時生産性"),
                           screen.health(month, whole["pph"], whole_target,
                                         whole["vs_target"], build_ranking(results))))
    else:
        company_block = ""
        trend_block = string.Template(
            (PART / "_trend_dept.html").read_text(encoding="utf-8")).substitute(**dept_charts)
        howto_block = (PART / "_howto_dept.html").read_text(encoding="utf-8")
        health_block = ""

    html = string.Template(TEMPLATE.read_text(encoding="utf-8")).substitute(
        theme=THEME.read_text(encoding="utf-8"),
        company=cfg.company,
        generated=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        stamp=datetime.datetime.now().strftime("%Y%m%d-%H%M"),
        last_actual=month["last_actual_date"],
        actual_days=month["actual_days"], remaining_days=month["remaining_days"],
        nav=('<nav>%s</nav>' % screen.nav("/", logout=True)) if nav else "",
        notice=(LIMITED % "・".join(sorted(scope)) + notice) if scope is not None else notice,
        company_block=company_block,
        trend_block=trend_block,
        movement=screen.movement(book["departments"]),
        health_block=health_block,
        howto_block=howto_block,
        alerts=screen.alerts(book["departments"],
                             {n: r["trend"] for n, r in results.items()}, trends, attached,
                             metric["alert_drop_ratio"] * 100),
        trends=trends_html,
        notes=(note_html(notes) if notes else
               '<div class="none">まだありません。<a href="/note">申し送りを書く</a>'
               '（<code>python castle/app/serve.py</code> で開きます）</div>'),
        limits="".join(limits), stats=stats)

    out = instance / "out"
    out.mkdir(exist_ok=True)
    (out / "dashboard.html").write_text(html, encoding="utf-8")

    # 判定者が読む用。画面のHTMLを機械が読むのは脆いので、数字は別に出す。
    (out / "summary.json").write_text(json.dumps({
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "stamp": datetime.datetime.now().strftime("%Y%m%d-%H%M"),
        "scale": scale_of(conn, cfg, book),
        "metric": metric["formula"],
        "window": [dates[-window], dates[-1]],
        "estimated_days": len(est_in_window),
        "whole": {"gross_pph": whole["pph"], "sales_pph": whole["sales_pph"],
                  "margin": whole["margin"], "target": whole_target, "vs_target": whole["vs_target"]},
        "departments": {
            name: {"gross_pph": r["pph"], "sales_pph": r["sales_pph"], "margin": r["margin"],
                   "target": r["target"], "vs_target": r["vs_target"], "vs_prev": r["vs_prev"],
                   "vs_yoy": r["vs_yoy"], "trend": r["trend"]}
            for name, r in results.items()},
        "trend_cards": trend_cards,
        "stock_chart_range": [stock_days[0], stock_days[-1]] if stock_days else None,
        # 3つ目の問い。記事の照合も図解も、ここから数字を取る。
        "gap": pnl.gap(conn, cfg, metric["yoy_offset_days"]),
        "actions": ({"planned": _board["planned"], "landed": _board["landed"],
                     "counted": len(_board["counted"]),
                     "done": _board["by_state"]["効いた"],
                     "overdue": len(_board["overdue"])}),
        # 年間の着地。記事の照合も、ここから数字を取る（記事に期待値を書き写さない）。
        "year": ({"label": year["this_year"]["label"], "budget": year["budget"],
                  "sales": year["this_year"]["sales"], "op": year["this_year"]["op"],
                  "last_sales": year["last_year"]["sales"], "last_op": year["last_year"]["op"],
                  "months": year["this_year"]["months"]} if year else None),
        "stock_view": _stock_view(book),
        "ladder": book["ladder"]["steps"],
        "depreciation": book["ladder"]["depreciation"],
        "cash": ({k: v for k, v in book["cash"].items() if k != "purchase"}
                 | {"purchase": {k: v for k, v in book["cash"]["purchase"].items()
                                 if k != "series"}}) if book["cash"] else None,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not verbose:
        return out / "dashboard.html"
    print("書き出しました: %s" % (out / "dashboard.html"))
    print("全社 %s 円/人時（粗利率 %.1f%% ／ 目標比 %+.1f%%）／ 要確認 %d部門／推定日 %d"
          % (yen(whole["pph"]), whole["margin"] * 100, whole["vs_target"],
             sum(1 for r in results.values()
                 if (r["vs_prev"] or 0) <= -metric["alert_drop_ratio"] * 100
                 or (r["vs_target"] or 0) <= -metric["alert_drop_ratio"] * 100),
             len(est_in_window)))


if __name__ == "__main__":
    main()
