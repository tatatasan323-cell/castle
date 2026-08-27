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
import knowledge
import pnl
import screen

TEMPLATE = db.ROOT / "castle" / "templates" / "dashboard.html"
# 見た目は1枚にまとめてビルド時に差し込む。実行時の外部ファイル読み込みはゼロのまま。
THEME = db.ROOT / "castle" / "templates" / "theme.css"

# サーバ配信のときだけ出す。単体ファイルとして配ったときにリンク切れを作らないため。
NAV = ('<nav><b>経営ステータス</b>'
       '<a href="/note">申し送りを書く</a>'
       '<a href="/knowledge">知識の泉</a>'
       '<a href="/guide">使い方</a>'
       '<a href="/logout">閉じる</a></nav>')


# ---------------------------------------------------------------- 集計

def load_margins(conn):
    """部門×月の粗利率。会計の売上高で会計の原価を割る（サイロ内で閉じた比にする）。"""
    rows = conn.execute(
        """SELECT subject AS s, occurred_at AS d,
                  json_extract(body,'$.cost')  AS cost,
                  json_extract(body,'$.sales') AS sales
             FROM records WHERE kind='部門損益'"""
    ).fetchall()
    margins = {}
    for row in rows:
        if row["sales"]:
            margins.setdefault(row["s"], {})[row["d"][:7]] = 1 - row["cost"] / row["sales"]
    return margins


def margin_trend(by_month):
    """確定した直近2ヶ月の粗利率。当月内では粗利率が動かないので、動きはここでしか見えない。"""
    months = sorted(by_month)
    if len(months) < 2:
        return None
    return months[-2], by_month[months[-2]], months[-1], by_month[months[-1]]


def margin_for(by_month, day):
    """その月が締まっていれば実績、まだなら直近の確定月の率（＝推定）。

    どの月の率を使ったかも返す。ここが同じ月なら粗利率は動きようがないので、
    「粗利率 ±0」を変化として表示してはいけない。
    """
    month = day[:7]
    if month in by_month:
        return by_month[month], False, month
    settled = [m for m in by_month if m < month]
    return (by_month[max(settled)], True, max(settled)) if settled else (None, True, None)


def load_daily(conn, targets, margins):
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
        rate, is_estimate, source = margin_for(margins.get(row["s"], {}), row["d"])
        if rate is None:
            continue
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




def pct(value, good_high=True, unit="%"):
    if value is None:
        return '<span class="flat">—</span>'
    cls = "flat" if abs(value) < 0.5 else ("up" if (value > 0) == good_high else "down")
    return '<span class="%s">%+.1f%s</span>' % (cls, value, unit)


def sparkline(weeks, prior, target, width=260, height=70, pad=8):
    values = [v for _, v in weeks] + [v for v in prior if v is not None] + [target]
    lo, hi = min(values), max(values)
    span = (hi - lo) or (hi or 1)
    lo, hi = lo - span * 0.15, hi + span * 0.15

    def points(series):
        out = []
        for i, value in enumerate(series):
            if value is None:
                continue
            x = pad + (width - 2 * pad) * (i / max(len(series) - 1, 1))
            y = height - pad - (height - 2 * pad) * ((value - lo) / (hi - lo))
            out.append("%.1f,%.1f" % (x, y))
        return " ".join(out)

    ty = height - pad - (height - 2 * pad) * ((target - lo) / (hi - lo))
    return (
        '<svg viewBox="0 0 %d %d" role="img" aria-label="週次推移">'
        '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--muted)" stroke-width="1" stroke-dasharray="3 3" opacity=".7"/>'
        '<polyline fill="none" stroke="var(--prior)" stroke-width="1.6" points="%s"/>'
        '<polyline fill="none" stroke="var(--bar)" stroke-width="2.2" stroke-linejoin="round" points="%s"/>'
        "</svg>"
    ) % (width, height, pad, ty, width - pad, ty, points(prior), points([v for _, v in weeks]))


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
            % (name, yen(r["pph"]), 100 * r["pph"] / scale, 100 * r["target"] / scale,
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
    cards = []
    for name, r in entries:
        weeks_prior = weekly(series_by_name[name])
        prior = []
        for monday, _ in r["weeks"]:
            bucket = weeks_prior.get(shift(monday, metric["yoy_offset_days"]))
            prior.append(bucket[2] / bucket[1] if bucket and bucket[1] else None)
        cards.append(
            '<div class="spark"><div class="t"><b>%s</b><span>%s 円/人時</span></div>%s</div>'
            % (name, yen(r["pph"]), sparkline(r["weeks"], prior, r["target"]))
        )
    return "".join(cards)


# ---------------------------------------------------------------- 組み立て

def main():
    parser = argparse.ArgumentParser(description="経営ステータスの一枚を書き出す")
    parser.add_argument("--instance")
    args = parser.parse_args()
    build(db.instance_dir(args.instance), verbose=True)


def build(instance, verbose=False, nav=False):
    conn = db.connect(instance)
    cfg = config_mod.load(instance)
    metric = cfg.metric

    margins = load_margins(conn)
    if not margins:
        raise SystemExit("会計（部門損益）が入っていません。着地見込みは出せません。")

    data, half, estimated, rate_months = load_daily(conn, {d["name"] for d in cfg.measured()}, margins)
    measured = [d for d in cfg.measured() if d["name"] in data]
    if not measured:
        raise SystemExit("売上・労働時間・原価の3つが揃っている部門がありません。")

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

    est_in_window = sorted(d for d in dates[-window:] if d in estimated)
    trends = {name: margin_trend(margins.get(name, {})) for name in results}

    # ── 経営者の問いに答える計算。ここがこの画面の主役になる ──
    book = pnl.build(conn, cfg, metric["yoy_offset_days"])
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

    # 部門別の推移は**日次のまま**並べる。月次に丸めるのは会計の都合であって、経営の都合ではない。
    chart_days = [d for d in dates if d >= shift(dates[-1], 120)]
    amount_series, rate_series = [], []
    for dept in sorted(measured, key=lambda d: -results[d["name"]]["gross"]):
        name = dept["name"]
        daily = data[name]
        gross = [daily.get(d, (0.0, 0.0, None))[2] for d in chart_days]
        rate = [None if daily.get(d) is None or not daily[d][0]
                else daily[d][2] / daily[d][0] * 100 for d in chart_days]
        amount_series.append((name, screen.moving_average(gross)))
        rate_series.append((name, rate))

    html = string.Template(TEMPLATE.read_text(encoding="utf-8")).substitute(
        theme=THEME.read_text(encoding="utf-8"),
        company=cfg.company,
        generated=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        last_actual=month["last_actual_date"],
        actual_days=month["actual_days"], remaining_days=month["remaining_days"],
        nav=(NAV if nav else ""),
        notice=notice,
        landing=screen.landing(month),
        breakdown=screen.breakdown(month),
        voyage=screen.voyage(month),
        dept_amount=screen.series_chart(
            chart_days, amount_series,
            lambda v: "%.0f万" % (v / 1e4), "c-amount"),
        dept_rate=screen.series_chart(
            chart_days, rate_series,
            lambda v: "%.1f%%" % v, "c-rate"),
        movement=screen.movement(book["departments"]),
        health=screen.health(month, whole["pph"], whole_target, whole["vs_target"],
                             build_ranking(results)),
        alerts=screen.alerts(book["departments"],
                             {n: r["trend"] for n, r in results.items()}, trends, attached,
                             metric["alert_drop_ratio"] * 100),
        trends=build_trends(trend_entries, series_by_name, metric),
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
