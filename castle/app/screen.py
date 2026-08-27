"""経営者の一枚を描く。

この画面の順序は、そのまま重要度。いちばん上に**今月いくらで終わるか**を置く。
分析指標（人時生産性）は下に格下げする ── 社長の問いはそこではない。
"""


def money(value, sign=False):
    """億と万で出す。経営者が読む単位に合わせる。"""
    if value is None:
        return "—"
    head = "+" if sign and value > 0 else ""
    if abs(value) >= 1e8:
        return "%s%.2f億円" % (head, value / 1e8)
    return "%s%s万円" % (head, format(value / 1e4, ",.0f"))


def pct(value, good_high=True, unit="%"):
    if value is None:
        return '<span class="flat">—</span>'
    cls = "flat" if abs(value) < 0.5 else ("up" if (value > 0) == good_high else "down")
    return '<span class="%s">%+.1f%s</span>' % (cls, value, unit)


def _change(now, base):
    return None if not base or now is None else (now / base - 1) * 100


def landing(m):
    """この画面でいちばん大きいもの。今月いくらで終わるか、それだけ。"""
    total_days = max(m["actual_days"] + m["remaining_days"], 1)
    done = 100 * m["actual_days"] / total_days
    against = [
        "<div><span>予算 %s</span>%s</div>" % (money(m["budget"]), pct(m["vs_budget"])),
        "<div><span>前年同月 %s</span>%s</div>" % (money(m["last_year_op"]), pct(m["vs_last_year"])),
        "<div><span>すでに確定している分</span><b>%s</b></div>" % money(m["actual_op"]),
    ]
    return (
        '<div class="label">%s年%d月　着地見込み ── 営業利益</div>'
        '<div class="figure"><b>%s</b>'
        '<span class="unit">売上 %s ／ 粗利率 %.1f%%</span></div>'
        '<div class="against">%s</div>'
        '<div class="progress"><div class="track">'
        '<i class="done" style="width:%.1f%%"></i><i class="todo" style="width:%.1f%%"></i></div>'
        '<div class="legend">実績 %d営業日（%s まで）＋ 見込み %d営業日 ＝ 月間 %d営業日</div>'
        "</div>"
        % (m["month"][:4], int(m["month"][5:]), money(m["forecast_op"]),
           money(m["forecast_sales"]), m["margin"] * 100, "".join(against),
           done, 100 - done, m["actual_days"], m["last_actual_date"],
           m["remaining_days"], total_days))


def breakdown(m):
    """売上が伸びていても利益は落ちる。だから、どこが動いたかを並べる。"""
    ly_margin = (m["last_year_gross"] / m["last_year_sales"]) if m["last_year_sales"] else 0.0
    items = [
        ("売上（着地）", money(m["forecast_sales"]),
         "前年 %s に対して %s" % (money(m["last_year_sales"]),
                              pct(_change(m["forecast_sales"], m["last_year_sales"])))),
        ("粗利（着地）", money(m["forecast_gross"]),
         "予算 %s に対して %s" % (money(m["gross_budget"]),
                              pct(_change(m["forecast_gross"], m["gross_budget"])))),
        ("粗利率", "%.1f%%" % (m["margin"] * 100),
         "前年 %.1f%% に対して %s" % (ly_margin * 100,
                                  pct((m["margin"] - ly_margin) * 100, unit="pt"))),
        ("人件費（着地）", money(m["forecast_labor"]),
         "前年 %s に対して %s" % (money(m["last_year_labor"]),
                              pct(_change(m["forecast_labor"], m["last_year_labor"]), good_high=False))),
        ("その他固定費（着地）", money(m["forecast_fixed"]), "月額を営業日数で日割り"),
    ]
    return "".join('<div><div class="k">%s</div><div class="v">%s</div><div class="n">%s</div></div>' % i
                   for i in items)


def voyage(m, width=940, height=250, pad=36):
    """航海図。累計の線が予算の水平線より上にいるか ── 順調かはそれで分かる。"""
    cum, last_year, budget = m["cumulative"], m["last_year_cumulative"], m["budget"]
    count = len(cum)
    pool = list(cum) + [v for v in last_year if v is not None] + ([budget] if budget else []) + [0.0]
    low, high = min(pool), max(pool)
    span = (high - low) or abs(high) or 1.0
    low, high = low - span * 0.12, high + span * 0.14
    right = width - 96

    def x_of(i):
        return pad + (right - pad) * (i / max(count - 1, 1))

    def y_of(v):
        return height - pad - (height - pad - 18) * ((v - low) / (high - low))

    def points(values, start=0):
        return " ".join("%.1f,%.1f" % (x_of(start + i), y_of(v))
                        for i, v in enumerate(values) if v is not None)

    cut = max(m["actual_days"] - 1, 0)
    out = ['<svg viewBox="0 0 %d %d" role="img" aria-label="今月の累計営業利益">' % (width, height)]
    if low < 0 < high:
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--line)" stroke-width="1"/>'
                   % (pad, y_of(0), right, y_of(0)))
    if budget:
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--muted)" stroke-width="1.2" '
                   'stroke-dasharray="5 4"/>' % (pad, y_of(budget), right, y_of(budget)))
        out.append('<text x="%d" y="%.1f" fill="var(--muted)" font-size="12">予算 %s</text>'
                   % (right + 6, y_of(budget) + 4, money(budget)))
    out.append('<polyline fill="none" stroke="var(--prior)" stroke-width="2" points="%s"/>'
               % points(last_year))
    out.append('<polyline fill="none" stroke="var(--bar)" stroke-width="3" stroke-linejoin="round" '
               'points="%s"/>' % points(cum[:cut + 1]))
    out.append('<polyline fill="none" stroke="var(--bar)" stroke-width="2.5" stroke-dasharray="6 5" '
               'points="%s"/>' % points(cum[cut:], start=cut))
    out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="var(--bar)"/>' % (x_of(cut), y_of(cum[cut])))
    out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%d" stroke="var(--line)" stroke-width="1"/>'
               % (x_of(cut), y_of(cum[cut]) + 7, x_of(cut), height - pad + 4))
    out.append('<text x="%.1f" y="%d" fill="var(--muted)" font-size="11" text-anchor="middle">'
               "実績はここまで</text>" % (x_of(cut), height - pad + 17))
    out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="none" stroke="var(--bar)" stroke-width="2"/>'
               % (x_of(count - 1), y_of(cum[-1])))
    out.append('<text x="%d" y="%.1f" fill="var(--ink)" font-size="13" font-weight="700">%s</text>'
               % (right + 6, y_of(cum[-1]) + 4, money(cum[-1])))
    out.append('<text x="%d" y="%d" fill="var(--muted)" font-size="11">%s</text>'
               % (pad, height - pad + 17, m["dates"][0]))
    out.append('<text x="%d" y="%d" fill="var(--muted)" font-size="11" text-anchor="end">%s</text>'
               % (right, height - pad + 17, m["dates"][-1]))
    return "".join(out) + "</svg>"


def movement(departments):
    """悪い順に並べる。経営者が最初に見たいのは、落ちているほうだから。"""
    order = sorted(departments.items(),
                   key=lambda kv: (kv[1]["vs_budget"] is None, kv[1]["vs_budget"] or 0))
    mark = 78.0                     # 予算の位置（％）。バーがここに届けば達成
    rows = []
    for i, (name, v) in enumerate(order):
        move = v["vs_last_year"] or 0
        arrow = ('<span class="up">▲</span>' if move > 1 else
                 '<span class="down">▼</span>' if move < -1 else
                 '<span class="flat">→</span>')
        worst = ' class="worst"' if i == 0 and (v["vs_budget"] or 0) < -5 else ""
        rows.append(
            "<tr%s><td class=\"name\">%s %s</td><td><b>%s</b></td>"
            '<td class="barcell"><div class="bar"><span class="fill" style="width:%.1f%%"></span>'
            '<i class="target" style="left:%.1f%%"></i></div></td>'
            "<td>%s</td><td>%s</td><td>%.1f%%</td></tr>"
            % (worst, arrow, name, money(v["forecast_gross"]),
               min(mark * v["forecast_gross"] / v["budget"], 100.0) if v["budget"] else 0.0, mark,
               pct(v["vs_budget"]), pct(v["vs_last_year"]), v["margin"] * 100))
    return ('<table><thead><tr><th class="name">部門</th><th>着地見込み粗利</th>'
            '<th class="barcell"></th><th>予算比</th><th>前年比</th><th>粗利率</th>'
            "</tr></thead><tbody>%s</tbody></table>" % "".join(rows))


PALETTE = ["#5ee0f0", "#e8b866", "#5fd6a4", "#ff7a6b", "#a98cf0", "#7fb4ff", "#f08fc0"]


def series_chart(dates, series, unit, chart_id, width=940, height=260, pad=38):
    """部門を日次で並べて比べる図。

    **月次に丸めない。** 月次は会計の都合であって、経営の都合ではない。
    日次のまま7営業日の移動平均をかければ、3ヶ月でも傾向は出る。

    凡例にマウスを乗せるとその部門だけが残る ── JavaScriptは使わず、
    生成したCSSの :hover と兄弟セレクタだけで実現する。
    """
    pool = [v for _, values in series for v in values if v is not None]
    if not pool:
        return '<div class="none">描ける値がありません。</div>'
    low, high = min(pool), max(pool)
    span = (high - low) or abs(high) or 1.0
    low, high = low - span * 0.14, high + span * 0.14
    right = width - 118
    count = len(dates)

    def x_of(i):
        return pad + (right - pad) * (i / max(count - 1, 1))

    def y_of(v):
        return height - pad - (height - pad - 16) * ((v - low) / (high - low))

    rules, legend, lines = [], [], []
    for index, (name, values) in enumerate(series):
        color = PALETTE[index % len(PALETTE)]
        points = " ".join("%.1f,%.1f" % (x_of(i), y_of(v))
                          for i, v in enumerate(values) if v is not None)
        lines.append('<polyline class="s s%d" fill="none" stroke="%s" stroke-width="1.8" '
                     'stroke-linejoin="round" points="%s"/>' % (index, color, points))
        last = next((v for v in reversed(values) if v is not None), None)
        if last is not None:
            lines.append('<circle class="s s%d" cx="%.1f" cy="%.1f" r="2.6" fill="%s"/>'
                         % (index, x_of(count - 1), y_of(last), color))
        legend.append('<span class="key k%d"><i style="background:%s"></i>%s</span>' % (index, color, name))
        rules.append("#%s .k%d:hover ~ svg .s{opacity:.12}"
                     "#%s .k%d:hover ~ svg .s%d{opacity:1;stroke-width:3}"
                     % (chart_id, index, chart_id, index, index))

    grid = []
    for step in (0.0, 0.5, 1.0):
        value = low + (high - low) * step
        grid.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--line)" stroke-width="1"/>'
                    % (pad, y_of(value), right, y_of(value)))
        grid.append('<text x="%d" y="%.1f" fill="var(--muted)" font-size="10.5">%s</text>'
                    % (right + 8, y_of(value) + 3.5, unit(value)))

    months = []
    for i, day in enumerate(dates):
        if i and day[5:7] != dates[i - 1][5:7]:
            months.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" stroke="var(--line)" '
                          'stroke-width="1" stroke-dasharray="2 4"/>' % (x_of(i), pad - 10, x_of(i), height - pad))
            months.append('<text x="%.1f" y="%d" fill="var(--muted)" font-size="10.5" '
                          'text-anchor="middle">%s月</text>' % (x_of(i), pad - 16, day[5:7].lstrip("0")))

    return ('<div class="chart" id="%s"><style>%s</style>'
            '<div class="keys series">%s</div>'
            '<svg viewBox="0 0 %d %d" role="img" aria-label="部門別の推移">%s%s%s'
            '<text x="%d" y="%d" fill="var(--muted)" font-size="10.5">%s</text>'
            '<text x="%d" y="%d" fill="var(--muted)" font-size="10.5" text-anchor="end">%s</text>'
            "</svg></div>"
            % (chart_id, "".join(rules), "".join(legend), width, height,
               "".join(grid), "".join(months), "".join(lines),
               pad, height - pad + 17, dates[0], right, height - pad + 17, dates[-1]))


def moving_average(values, window=7):
    """7営業日の移動平均。日次のギザギザを均して、傾きだけを残す。"""
    out = []
    for i in range(len(values)):
        chunk = [v for v in values[max(0, i - window + 1):i + 1] if v is not None]
        out.append(sum(chunk) / len(chunk) if chunk else None)
    return out


def alerts(departments, trends, margin_trends, attached, threshold=5.0):
    """いま手を打つこと。

    **主語は粗利の予算比**。人時生産性ではない ── 社長が動く理由になるのは、
    「予算に届かない」であって「効率が悪い」ではないから。
    分析指標は下の「正常に回っているか」に置いてある。
    """
    flagged = [(name, v) for name, v in departments.items()
               if (v["vs_budget"] is not None and v["vs_budget"] <= -threshold)
               or (v["vs_last_year"] is not None and v["vs_last_year"] <= -threshold)]
    if not flagged:
        return ('<div class="none">予算比・前年比とも、閾値（%.0f%%）を超えて落ちている部門はありません。</div>'
                % threshold)

    out = []
    for name, v in sorted(flagged, key=lambda kv: kv[1]["vs_budget"] or 0):
        is_trend = trends.get(name, False)
        tag, ask = (("傾向", "3週以上続けて下がっている。数量が戻る見込みか、売価・人時の張り方を変えるかを部門長と確認。")
                    if is_trend else
                    ("単発", "続けての低下ではない。特売・出荷ずれ・得意先の休業など単発の要因かを日次で確認。"))
        moves = {"売上": abs(v["sales_vs_ly"] or 0), "人時": abs(v["hours_vs_ly"] or 0)}
        cause = max(moves, key=moves.get)

        monthly = ""
        trend = margin_trends.get(name)
        if trend:
            a, va, b, vb = trend
            monthly = ("<br>確定した粗利率の推移：%s %.1f%% → %s %.1f%%（%s）"
                       % (a, va * 100, b, vb * 100, pct((vb - va) * 100, unit="pt")))

        out.append(
            '<div class="alert%s"><div class="head">%s<span class="tag">%s</span></div>'
            '<div class="body">粗利の着地見込み <b>%s</b>'
            "（予算 %s に対して %s ／ 前年同月 %s）。粗利率 %.1f%%。<br>"
            "当月ここまでの前年同期比：売上 %s ／ 人時 %s → <b>%s側</b>の動きが大きい。%s</div>"
            '<div class="ask">確認：%s</div>%s</div>'
            % ("" if is_trend else " mild", name, tag,
               money(v["forecast_gross"]), money(v["budget"]),
               pct(v["vs_budget"]), pct(v["vs_last_year"]), v["margin"] * 100,
               pct(v["sales_vs_ly"]), pct(v["hours_vs_ly"], good_high=False),
               cause, monthly, ask, attached.get(name, "")))
    return "".join(out)


def health(m, whole_pph, whole_target, vs_target, ranking_html):
    """分析指標はここ。経営者の主指標ではないが、回っているかは示す。"""
    labor_rate = (m["forecast_labor"] / m["forecast_gross"]) if m["forecast_gross"] else 0.0
    cards = [
        ("粗利（着地）", money(m["forecast_gross"]),
         "予算 %s に対して %s" % (money(m["gross_budget"]),
                              pct(_change(m["forecast_gross"], m["gross_budget"])))),
        ("粗利率", "%.1f%%" % (m["margin"] * 100), "売上に対する粗利"),
        ("労働分配率", "%.1f%%" % (labor_rate * 100), "粗利に対する人件費"),
        ("人時生産性（全社）", "%s 円/人時" % format(whole_pph, ",.0f"),
         "加重目標 %s に対して %s" % (format(whole_target, ",.0f"), pct(vs_target))),
    ]
    top = "".join('<div><div class="k">%s</div><div class="v">%s</div><div class="n">%s</div></div>' % c
                  for c in cards)
    return '<div class="breakdown">%s</div>%s' % (top, ranking_html)
