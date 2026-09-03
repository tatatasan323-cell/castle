"""経営者の一枚を描く。

この画面の順序は、そのまま重要度。いちばん上に**今月いくらで終わるか**を置く。
分析指標（人時生産性）は下に格下げする ── 社長の問いはそこではない。
"""

from html import escape


MINUS = chr(0x2212)


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


# 用語の一行説明。**毎日見る人には邪魔、はじめての人には要る。**
# だから常時は出さず、印にしまう。JavaScriptは使わない（:hover と :focus だけ）。
TERMS = {
    "粗利率": "売上に対する粗利の割合。値引きが増えるか、相場が上がると落ちます。"
            "金額が伸びていても率が落ちていれば、売り方が変わっています。",
    "人時生産性": "粗利 ÷ 実労働時間。1時間働いていくら粗利を生んだか。"
                "売上ベースだと、薄利多売の部門が実力以上に見えます。",
    "労働分配率": "粗利に対する人件費の割合。稼いだ粗利のうち、どれだけを人に配ったか。"
                "高すぎれば利益が残らず、低すぎれば人が続きません。",
    "在庫日数": "在庫 ÷ 1日あたりの売上原価。いまの在庫が何日分かを表します。"
             "短いほど、同じ商売でも寝ている金が少なくて済みます。",
    "段階利益": "売上から当期純利益まで、利益が一段ずつ削られていく並びのこと。"
             "どの段で消えたかが分かると、打つ手が決まります。",
    "運転資本": "売掛金 ＋ 棚卸資産 − 買掛金。商売を回すために立て替えている金です。"
             "増えるとその分だけ現金が減ります。",
    "現金が戻ってくるまで": "仕入れた物が現金になって戻るまでの日数（CCC）。"
                    "売掛の回収 ＋ 在庫の滞留 − 仕入の支払猶予 で出します。",
    "期首": "その月の始まりの在庫。前月末に締めた在庫がそのまま繰り越されます。",
}


def hint(term, align="center"):
    """用語の隣に置く印。**ホバーだけに閉じ込めない** ── 触る画面には hover が無いので、
    tabindex を付けて focus でも開くようにする。

    align="right" は、画面の右端に置く印用。中央から開くと画面の外へ出る
    ── 2026-09-02、実測で3箇所がはみ出していた（右端 1045px ／ 画面幅 951px）。
    """
    text = TERMS.get(term)
    if not text:
        return ""
    return ('<span class="hint%s" tabindex="0" role="note" aria-label="%sとは">'
            'ⓘ<span class="tip"><b>%s</b><br>%s</span></span>'
            % (" tip-right" if align == "right" else "",
               escape(term), escape(term), escape(text)))


def freshness(items, today):
    """データの届き具合。**自動にするほど「今日も動いたはず」と思い込む。**

    静かに古いデータで画面が出るのが、いちばん悪い。だから届いていないことを
    画面の側から言う ── 気づくのを人の注意力に頼らない。
    遅れが無いときは1行に畳む。毎回大きく出す警告は、そのうち読まれなくなる。
    """
    if not items:
        return ""
    late = [f for f in items if f["late"]]
    rows = []
    for f in sorted(items, key=lambda x: (not x["late"], x["silo"], x["kind"])):
        rows.append('<tr class="%s"><td>%s</td><td>%s</td><td>%s</td>'
                    '<td class="num">%s</td><td class="num">%s</td><td>%s</td></tr>'
                    % ("late" if f["late"] else "", escape(f["silo"]), escape(f["kind"]),
                       f["cycle"], f["last"],
                       "%d日前" % f["behind"] if f["last"] != "—" else "—",
                       ("<b class=\"bad\">遅れ</b>（%s）" % f["expect"]) if f["late"]
                       else "届いています"))
    head = ('<b class="bad">%d本のデータが届いていません</b>' % len(late) if late
            else "予定どおり届いています")
    return ('<details class="freshness"%s><summary>データの届き具合 ── %s'
            '<span class="muted">（%s 時点）</span></summary>'
            '<table><thead><tr><th>サイロ</th><th>種類</th><th>周期</th>'
            '<th>最終データ日</th><th>経過</th><th>状態</th></tr></thead>'
            "<tbody>%s</tbody></table>"
            '<p class="legend">置き場に届いたものを見張りが自動で取り込みます。'
            '受入仕様にない名前のファイルは<b>推測で取り込まず、保留へ退けます</b>'
            ' ── 種別を当てにいくと、間違った種別のまま記録に入るためです。</p>'
            "</details>"
            % (" open" if late else "", head, today, "".join(rows)))


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


def _stack(labels, gap=15.0, low=14.0, high=None):
    """右端に並べる文字を、重ならない高さへ押し分ける。

    **値が近いほど文字は重なる。** 予算と着地と前期が数%違いなら、
    線は3本きれいに見えていても、文字は folded になって読めない ──
    数字が正しくても、読めなければ画面としては嘘をついている。

    引き出し線は付けない。押し分ける幅を1行ぶんに抑え、線と文字の縦の対応を保つ。
    """
    order = sorted(range(len(labels)), key=lambda i: labels[i][0])
    ys = [labels[i][0] for i in order]
    for k in range(1, len(ys)):
        ys[k] = max(ys[k], ys[k - 1] + gap)
    if high is not None and ys and ys[-1] > high:      # 下がはみ出たら上へ押し戻す
        shift = ys[-1] - high
        ys = [y - shift for y in ys]
    for k in range(len(ys) - 2, -1, -1):
        ys[k] = min(ys[k], ys[k + 1] - gap)
    ys = [max(y, low) for y in ys]
    out = [None] * len(labels)
    for slot, i in enumerate(order):
        out[i] = ys[slot]
    return out


def year_view(year, width=940):
    """年間の着地。**会社の損益は月ごとに確定し、それが年間へ積み上がる。**

    日次の推移を延々と並べても経営の視点にはならない。要るのは2つだけ。
      上 … 月ごとにいくらで着地したか（確定／当月／予測を塗り分ける）
      下 … その積み上げが年間でどこへ行くか（前期と年間予算に対して）

    **直近の月が予測でも構わない。** 締めを待たずに年間が見えることが、この画面の意味。
    ただし予測は予測と分かる形でしか描かない ── 実績と同じ塗りにしたら、それは嘘になる。
    """
    if not year:
        return ""
    rows = year["this_year"]["months"]
    prior = {m["month"][5:7]: m for m in year["last_year"]["months"]}
    budget = year["budget"]
    n = len(rows)
    pad, right, top = 44, width - 108, 20

    def x_of(i):
        return pad + (right - pad) * ((i + 0.5) / n)

    # ── 上：月ごとの営業利益
    h1 = 196
    per_month = year.get("monthly_budget") or {}
    vals = ([m["op"] for m in rows] + list(per_month.values()) + [0.0])
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or abs(hi) or 1.0
    lo, hi = lo - span * 0.10, hi + span * 0.16

    def y1(v):
        return h1 - 26 - (h1 - 26 - top) * ((v - lo) / (hi - lo))

    bw = (right - pad) / n * 0.56
    out = ['<svg viewBox="0 0 %d %d" role="img" aria-label="月ごとの営業利益">' % (width, h1)]
    zero = y1(0.0)
    out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--line-2)" stroke-width="1"/>'
               % (pad, zero, right, zero))
    if per_month:
        # 予算の線は月ごとに段になる ── 会社は年間予算を12等分しないから。
        steps = []
        for i, m in enumerate(rows):
            v = per_month.get(m["month"])
            if v is None:
                continue
            steps.append("%.1f,%.1f %.1f,%.1f"
                         % (x_of(i) - bw / 2 - 3, y1(v), x_of(i) + bw / 2 + 3, y1(v)))
        out.append('<polyline fill="none" stroke="var(--warn)" stroke-width="1.4" '
                   'stroke-dasharray="5 4" points="%s"/>' % " ".join(steps))
        out.append('<text x="%d" y="%.1f" fill="var(--warn)" font-size="11">月の予算</text>'
                   % (right + 6, y1(per_month[rows[-1]["month"]]) + 4))
    for i, m in enumerate(rows):
        v, cx = m["op"], x_of(i)
        y, hgt = min(y1(v), zero), abs(y1(v) - zero)
        color = "var(--bad)" if v < 0 else "var(--bar)"
        if m["state"] == "確定":
            style = 'fill="%s" fill-opacity=".82"' % color
        elif m["state"] == "当月":
            style = ('fill="%s" fill-opacity=".38" stroke="%s" stroke-width="1.6"' % (color, color))
        else:
            style = ('fill="%s" fill-opacity=".10" stroke="%s" stroke-width="1.4" '
                     'stroke-dasharray="4 3"' % (color, color))
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2" %s/>'
                   % (cx - bw / 2, y, bw, max(hgt, 1.0), style))
        # 前期の同じ月。棒の上に横棒で置く ── 伸びたか縮んだかが、目盛りを読まずに分かる。
        back = prior.get(m["month"][5:7])
        if back:
            out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="var(--prior)" '
                       'stroke-width="2.4"/>'
                       % (cx - bw / 2 - 2, y1(back["op"]), cx + bw / 2 + 2, y1(back["op"])))
        out.append('<text x="%.1f" y="%d" fill="var(--muted)" font-size="11" text-anchor="middle">'
                   "%d月</text>" % (cx, h1 - 8, int(m["month"][5:7])))
    out.append("</svg>")

    # ── 下：累計の着地
    h2 = 210
    cum, cum_prior, acc, acc_p = [], [], 0.0, 0.0
    for m in rows:
        acc += m["op"]
        cum.append(acc)
    for m in year["last_year"]["months"]:
        acc_p += m["op"]
        cum_prior.append(acc_p)
    pool = cum + cum_prior + ([budget] if budget else []) + [0.0]
    lo2, hi2 = min(pool), max(pool)
    span2 = (hi2 - lo2) or abs(hi2) or 1.0
    lo2, hi2 = lo2 - span2 * 0.10, hi2 + span2 * 0.14

    def y2(v):
        return h2 - 26 - (h2 - 26 - top) * ((v - lo2) / (hi2 - lo2))

    cut = max(len([m for m in rows if m["state"] == "確定"]) - 1, 0)

    def line(values, start=0):
        return " ".join("%.1f,%.1f" % (x_of(start + i), y2(v)) for i, v in enumerate(values))

    out.append('<svg viewBox="0 0 %d %d" role="img" aria-label="年間の着地">' % (width, h2))
    if lo2 < 0 < hi2:
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--line)" stroke-width="1"/>'
                   % (pad, y2(0.0), right, y2(0.0)))
    if budget:
        out.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--warn)" stroke-width="1.2" '
                   'stroke-dasharray="5 4"/>' % (pad, y2(budget), right, y2(budget)))
    out.append('<polyline fill="none" stroke="var(--prior)" stroke-width="2" points="%s"/>'
               % line(cum_prior))
    out.append('<polyline fill="none" stroke="var(--bar)" stroke-width="3" stroke-linejoin="round" '
               'points="%s"/>' % line(cum[:cut + 1]))
    out.append('<polyline fill="none" stroke="var(--bar)" stroke-width="2.5" stroke-dasharray="6 5" '
               'points="%s"/>' % line(cum[cut:], start=cut))
    out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="var(--bar)"/>' % (x_of(cut), y2(cum[cut])))
    out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%d" stroke="var(--line-2)" stroke-width="1"/>'
               % (x_of(cut), y2(cum[cut]) + 7, x_of(cut), h2 - 24))
    out.append('<text x="%.1f" y="%d" fill="var(--muted)" font-size="11" text-anchor="middle">'
               "確定はここまで</text>" % (x_of(cut), h2 - 8))
    out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="none" stroke="var(--bar)" stroke-width="2"/>'
               % (x_of(n - 1), y2(cum[-1])))

    tags = [(y2(cum[-1]), "着地 " + money(cum[-1]), "var(--ink)", 13, 700),
            (y2(cum_prior[-1]), "前期 " + money(cum_prior[-1]), "var(--prior)", 11, 400)]
    if budget:
        tags.append((y2(budget), "予算 " + money(budget), "var(--warn)", 11, 400))
    for (_, text, color, size, weight), y in zip(tags, _stack(tags, 15.0, 14.0, h2 - 30)):
        out.append('<text x="%d" y="%.1f" fill="%s" font-size="%d" font-weight="%d">%s</text>'
                   % (right + 6, y + 4, color, size, weight, text))
    out.append("</svg>")
    return "".join(out)


def year_verdict(year):
    """年間の着地を、言葉で言い切る。**読み手に判定させない。**

    ただし、**言い切れる強さは、確定した月がどれだけあるかで決まる。**
    12ヶ月のうち5ヶ月しか締まっていないのに「届く」と言えば、それは予測を実績と偽ること。
    だから「残りが何%落ちたら届かないか」を添える ── これなら、外れても嘘にならない。
    """
    if not year:
        return ""
    now, back = year["this_year"], year["last_year"]
    budget = year["budget"]
    vs_ly = (now["op"] / back["op"] - 1) * 100 if back["op"] else 0.0
    settled = [m for m in now["months"] if m["state"] == "確定"]
    ahead = [m for m in now["months"] if m["state"] != "確定"]
    bits = ['<p class="verdict">']
    if budget:
        gap = now["op"] - budget
        rate = gap / budget * 100
        pool = sum(m["op"] for m in ahead)
        room = (gap / pool * 100) if pool > 0 else None
        if rate >= 3:
            word, tone = "届く見通し", "ok"
        elif rate >= -3:
            word, tone = "予算とほぼ同じ線", "warn"
        else:
            word, tone = "届かない見通し", "bad"
        bits.append('<b class="%s">年間の着地 %s ── %s</b>（年間予算 %sに対して %s）'
                    % (tone, money(now["op"]), word, money(budget), pct(rate)))
        if room is not None:
            bits.append("<br>確定しているのは%dヶ月ぶん。残り%dヶ月が見込みより<b>%.1f%%</b>下ぶれすると、"
                        "予算に届きません。" % (len(settled), len(ahead), abs(room)))
    else:
        bits.append('<b>年間の着地 %s</b>' % money(now["op"]))
    bits.append("<br>前期 %sに対して %s。%d月まで確定、%d月は当月の見込み、残り%dヶ月は"
                "前期の同じ月に当期のここまでの伸び（売上・原価・人件費・経費それぞれ）を"
                "当てた予測です。"
                % (money(back["op"]), pct(vs_ly), int(settled[-1]["month"][5:7]),
                   int(now["months"][len(settled)]["month"][5:7]), len(ahead) - 1))
    worst = min(now["months"], key=lambda m: m["op"])
    if worst["op"] < 0:
        bits.append("<br>%d月は営業赤字の%s（%s）。ここが年間の足を引いています。"
                    % (int(worst["month"][5:7]),
                       "実績" if worst["state"] == "確定" else "見込み", money(worst["op"])))
    bits.append("</p>")
    return "".join(bits)


def closing(gap, board):
    """足りない分を、どう埋めるか。**社長の3つ目の問いはこれ。**

    診断で終わる画面は、読んだあとに何も起きない。だから
    「いくら足りないか」「何をどれだけ動かせば埋まるか」「打ち手は仕込まれているか」
    「その見込みでいくら埋まるか」まで、1つの節に並べる。

    **期限が過ぎて動いていない打ち手は、赤で名指しして見込みから外す。**
    それを足して着地を語れば、粉飾と同じ形になる。
    """
    if not gap:
        return ""
    short, planned = gap["year"]["short"], board["planned"]
    if short > 0:
        head = ('年間予算まで <b class="bad">あと %s</b>' % money(short))
        rest = ("打ち手で <b>%s</b> ぶん見込んでいます。<b class=\"%s\">残り %s</b>"
                % (money(planned), "bad" if board["uncovered"] > 0 else "ok",
                   money(board["uncovered"]) if board["uncovered"] > 0 else "足ります"))
    else:
        head = ('年間予算に対して <b class="ok">%s の余裕</b>' % money(-short))
        rest = ("薄い余裕です。打ち手が効けば <b>%s</b> まで厚くなります。"
                % money(-short + planned))

    cards = [
        '<div><div class="k">年間予算まで</div><div class="v %s">%s</div>'
        '<div class="n">着地 %s ／ 予算 %s</div></div>'
        % ("bad" if short > 0 else "ok",
           money(abs(short)) + ("" if short > 0 else " 余裕"),
           money(gap["year"]["forecast"]), money(gap["year"]["budget"])),
        '<div><div class="k">打ち手の見込み</div><div class="v">%s</div>'
        '<div class="n">%d件（これから効くもの。当月の不足は %s）</div></div>'
        % (money(planned), len(board["counted"]), money(gap["month"]["short"])),
        '<div><div class="k">効いた実績</div><div class="v ok">%s</div>'
        '<div class="n">%d件。<b>もう数字に出ているので、見込みには足しません</b></div></div>'
        % (money(board["landed"]), board["by_state"]["効いた"]),
    ]

    # レバー。**判断の材料は機械が出し、判断は人がする。**
    rows = []
    for lever in gap["levers"]:
        rows.append('<tr><td class="name">%s を %s</td><td class="v">%s</td>'
                    "<td>%s</td></tr>"
                    % (escape(lever["name"]), escape(lever["step"]),
                       money(lever["amount"]), escape(lever["note"])))
    levers = ('<table><thead><tr><th class="name">動かすもの</th>'
              "<th>営業利益への効き目</th><th>中身</th></tr></thead>"
              "<tbody>%s</tbody></table>"
              '<p class="legend">残り%dヶ月ぶんの効き目です。'
              "<b>どれが現実的かを決めるのは人</b>で、機械はここまでしか言えません。</p>"
              % ("".join(rows), gap["remaining"]["months"]))

    # 打ち手の一覧。悪い順（期限切れが上）
    order = sorted(board["rows"], key=lambda r: (not r["overdue"], r["due"]))
    moves = []
    for r in order:
        klass = ' class="worst"' if r["overdue"] else ""
        state = ('<b class="bad">期限が過ぎています</b>' if r["overdue"]
                 else escape(r["state"]))
        moves.append(
            "<tr%s><td class=\"name\">%s</td><td>%s</td><td class=\"v\">%s</td>"
            "<td>%s</td><td>%s</td><td class=\"note\">%s</td></tr>"
            % (klass, escape(r["subject"]), escape(r["lever"]), money(r["expect"]),
               escape(r["due"]), state, escape(r["text"])))
    table = ('<table><thead><tr><th class="name">部門</th><th>動かすもの</th>'
             "<th>見込み</th><th>期限</th><th>状態</th><th>何をするか</th>"
             "</tr></thead><tbody>%s</tbody></table>" % "".join(moves)) if moves else (
        '<div class="none">打ち手がまだ1件も登録されていません。'
        "<b>足りない分は、誰かが動かなければ埋まりません。</b></div>")

    late = ""
    if board["overdue"]:
        late = ('<p class="verdict"><b class="bad">期限が過ぎて動いていない打ち手が %d件</b>'
                "あります（見込みからは外してあります）。"
                "<b>打ったつもりのまま残るのが、この台帳のいちばんの腐り方です。</b></p>"
                % len(board["overdue"]))

    return ('<p class="verdict">%s。%s</p><div class="breakdown">%s</div>%s%s%s'
            % (head, rest, "".join(cards), levers, late, table))


def movement(departments):
    """悪い順に並べる。経営者が最初に見たいのは、落ちているほうだから。

    **記号は、それが指す数字の隣に置く。** 前年比の向きを表す▲▼を部門名の左に置くと、
    すぐ右の予算比と結びつけて読まれる（▲なのに予算比マイナス、という見た目になる）。
    """
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
            "<tr%s><td class=\"name\">%s</td><td><b>%s</b></td>"
            '<td class="barcell"><div class="bar"><span class="fill" style="width:%.1f%%"></span>'
            '<i class="target" style="left:%.1f%%"></i></div></td>'
            "<td>%s</td><td>%s %s</td><td>%.1f%%</td></tr>"
            % (worst, escape(name), money(v["forecast_gross"]),
               min(mark * v["forecast_gross"] / v["budget"], 100.0) if v["budget"] else 0.0, mark,
               pct(v["vs_budget"]), arrow, pct(v["vs_last_year"]), v["margin"] * 100))
    return ('<table><thead><tr><th class="name">部門</th><th>着地見込み粗利</th>'
            '<th class="barcell">予算まで</th><th>予算比</th><th>前年比</th>'
            '<th>粗利率%s</th>' % hint("粗利率", align="right") +
            "</tr></thead><tbody>%s</tbody></table>"
            '<details class="why"><summary>この表の読み方</summary>'
            '<p class="legend">棒は、その部門の予算に対してどこまで来たか。'
            '縦の目印が予算の位置です。▲▼は<b>前年比</b>の向き（±1%%以内は→）。'
            "予算比の悪い順に並べています ── 経営者が最初に見たいのは落ちているほうなので。"
            "</p></details>"
            % "".join(rows))


PALETTE = ["#5ee0f0", "#e8b866", "#5fd6a4", "#ff7a6b", "#a98cf0", "#7fb4ff", "#f08fc0"]


def series_chart(dates, series, unit, chart_id, width=940, height=260, pad=38):
    """複数の系列を並べて比べる図。x軸のラベルは渡されたものをそのまま使う。

    凡例にマウスを乗せるとその系列だけが残る ── JavaScriptは使わず、
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
        legend.append('<span class="key k%d"><i style="background:%s"></i>%s</span>'
                      % (index, color, escape(name)))
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

    # x軸のラベルは、渡された粒度で描き分ける。
    # 月次の系列に「月の区切り線」を引くと全点に立ち、目盛りの文字とぶつかる
    # ── 2026-08-31、部門別を月次にしたときに実際に起きた。
    monthly = len(dates[0]) == 7
    months = []
    if monthly:
        for i, key in enumerate(dates):
            months.append('<text x="%.1f" y="%d" fill="var(--muted)" font-size="10.5" '
                          'text-anchor="middle">%s月</text>'
                          % (x_of(i), height - pad + 17, key[5:7].lstrip("0")))
    else:
        for i, day in enumerate(dates):
            if i and day[5:7] != dates[i - 1][5:7]:
                months.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" stroke="var(--line)" '
                              'stroke-width="1" stroke-dasharray="2 4"/>'
                              % (x_of(i), pad - 10, x_of(i), height - pad))
                months.append('<text x="%.1f" y="%d" fill="var(--muted)" font-size="10.5" '
                              'text-anchor="middle">%s月</text>'
                              % (x_of(i), pad - 16, day[5:7].lstrip("0")))
    ends = ("" if monthly else
            ('<text x="%d" y="%d" fill="var(--muted)" font-size="10.5">%s</text>'
             '<text x="%d" y="%d" fill="var(--muted)" font-size="10.5" text-anchor="end">%s</text>'
             % (pad, height - pad + 17, dates[0], right, height - pad + 17, dates[-1])))
    span = ('<text x="%d" y="%d" fill="var(--muted)" font-size="10.5">%s 〜 %s</text>'
            % (pad, pad - 16, dates[0], dates[-1])) if monthly else ""

    return ('<div class="chart" id="%s"><style>%s</style>'
            '<div class="keys series">%s</div>'
            '<svg viewBox="0 0 %d %d" role="img" aria-label="部門別の推移">%s%s%s%s%s'
            "</svg></div>"
            % (chart_id, "".join(rules), "".join(legend), width, height,
               "".join(grid), "".join(months), "".join(lines), ends, span))


def ladder(book):
    """利益の階段。営業利益で止めない ── 銀行と話すのも、株主に説明するのも経常利益から先。

    段ごとに前年同月を並べる。「売上は伸びたが利益は落ちた」がどの段で起きたかは、
    段を並べて初めて言える。
    """
    block = book["ladder"]
    rows = []
    for index, step in enumerate(block["steps"]):
        # 起点（売上高）は加減項目ではないので符号を付けない
        subtotal = step["sign"] == 0 or index == 0

        def show(value):
            # ゼロに符号を付けない。「−0万円」は金額ではなく、計算した振りに見える。
            if not value:
                return "—"
            head = "" if subtotal else ("＋" if step["sign"] > 0 else "−")
            return head + money(value)

        cls = "sub" if step["sign"] == 0 else "adj"
        if step["label"] == "当期純利益":
            cls += " last"
        # 法人税は「減れば良い」ではない ── 利益が減れば税も減る。色を付けない。
        change = ('<span class="flat">%+.1f%%</span>' % step["vs_ly"]
                  if step["label"] == "法人税等" and step["vs_ly"] is not None
                  else pct(step["vs_ly"], good_high=(step["sign"] >= 0)))
        rows.append(
            '<tr class="%s"><td class="name">%s</td><td class="v">%s</td>'
            '<td class="v ly">%s</td><td class="c">%s</td><td class="note">%s</td></tr>'
            % (cls, step["label"], show(step["amount"]), show(step["last_year"]),
               change, step["note"]))

    caution = "営業外（利息・仕入割引など）は毎月出るものなので、%s の額を当てています。" % block["nonop_from"]
    if not block["nonop_estimated"]:
        caution = "営業外は当月の確定額です。"
    if not block["extra_settled"]:
        caution += "<b>特別損益は推定していません</b> ── 前月に減損があったからといって今月も出るわけがないからです。当月の確定分だけを載せ、無ければゼロのまま置きます。"

    return (
        '<table class="ladder"><thead><tr><th class="name">段</th>'
        '<th class="v">当月の着地見込み</th><th class="v">前年同月（%s）</th>'
        '<th class="c">前年比</th><th class="note">出どころ</th></tr></thead>'
        "<tbody>%s</tbody></table>"
        '<div class="legend">%s</div>' % (block["last_year_month"], "".join(rows), caution))


# 残高は「増えたら良い」が科目ごとに逆になる。現金は増えて良く、売掛は増えると良くない。
BALANCE_GOOD_UP = {"現預金": True, "売掛金": False, "棚卸資産": False,
                   "買掛金": True, "未払金": True, "借入金": False}
BALANCE_WHY = {
    "現預金": "手元の現金", "売掛金": "まだ回収していない売上", "棚卸資産": "在庫として寝ている金",
    "買掛金": "まだ払っていない仕入", "未払金": "まだ払っていない経費", "借入金": "返す約束のある金",
}


def cash(book):
    """金は回るか。利益が出ていても現金は減る ── その理由を1枚で言い切る。"""
    block = book["cash"]
    if not block:
        return '<div class="none">残高のデータがありません。</div>'

    cards = []
    for key in BALANCE_GOOD_UP:
        item = block[key]
        cards.append(
            '<div><div class="k">%s</div><div class="v">%s</div>'
            '<div class="n">%s ／ 前月比 %s</div></div>'
            % (key, money(item["amount"]), BALANCE_WHY[key],
               pct(item["vs_prev"], good_high=BALANCE_GOOD_UP[key])))

    ccc = block["ccc"]
    bridge = [
        ("%s 月末の現預金" % block["as_of"], block["現預金"]["amount"], 0),
        ("当期純利益の見込み", book["ladder"]["net"], 1),
        ("減価償却費（現金は出ていかない）", book["ladder"]["depreciation"], 1),
        ("運転資本の増加（売掛と在庫に化けた分）" + hint("運転資本"),
         block["working_capital_change"], -1),
    ]
    # 内訳を出す。売掛なら回収、在庫なら発注 ── 打つ手が違う。
    WHY = {"売掛金": "回収が遅れた分", "棚卸資産": "在庫に積んだ分", "買掛金": "支払を待ってもらった分"}
    detail = "".join(
        '<tr class="adj sub2"><td class="name">うち %s（%s）</td><td class="v">%s</td></tr>'
        % (key, WHY[key], ("＋" if value > 0 else "−") + money(abs(value)))
        for key, value in (block.get("working_capital_parts") or {}).items() if value)
    steps = ""
    for label, value, sign in bridge:
        steps += ('<tr class="%s"><td class="name">%s</td><td class="v">%s</td></tr>'
                  % ("adj" if sign else "sub", label,
                     (("＋" if sign > 0 else "−") if sign else "") + money(abs(value))))
        if label.startswith("運転資本"):
            steps += detail

    return (
        '<div class="breakdown">%s</div>'
        '<div class="cashgrid">'
        '<div class="panel-box"><h3>現金が戻ってくるまで %.0f日%s</h3>' 
        '<div class="legend">売掛の回収 %.1f日 ＋ 在庫の滞留 %.1f日 − 仕入の支払猶予 %.1f日。'
        '<b>短いほど、同じ商売でも手元に金が残ります。</b>（%s 月末の残高で計算）</div></div>'
        '<div class="panel-box"><h3>月末の現預金は %s の見込み</h3>'
        '<table class="ladder small"><tbody>%s'
        '<tr class="sub last"><td class="name">当月末の現預金（見込み）</td><td class="v">%s</td></tr>'
        "</tbody></table>"
        '<div class="legend"><b>利益が出ていても現金は増えません。</b>'
        '売掛金と在庫が膨らめば、その分だけ金は寝たままです。</div></div>'
        "</div>"
        % ("".join(cards), ccc["days"], hint("現金が戻ってくるまで"),
           ccc["receivable_days"], ccc["inventory_days"],
           ccc["payable_days"], ccc["month"], money(block["cash_end_forecast"]),
           steps, money(block["cash_end_forecast"])))


def stock(book):
    """在庫。**週次までが実データ、その先は在庫日数を当てた想定値。**

    どこまでが事実で、どこからが置いた値か ── そこを書かない画面は、
    いずれ「毎日数えている」と誤読される。
    """
    daily = book.get("stock") or {}
    if not daily:
        return ""
    days = sorted(daily)
    last_real = max((d for d in days if daily[d]["settled"]), default=None)
    if last_real is None:
        return ""
    now = daily[days[-1]]
    real = daily[last_real]
    guessed = [d for d in days if d > last_real]
    return (
        '<div class="breakdown">'
        '<div><div class="k">最後に数えた在庫</div><div class="v">%s</div>'
        '<div class="n">%s 時点（週次）／ 在庫日数 %.1f日%s</div></div>'
        '<div><div class="k">いまの在庫（想定）</div><div class="v">%s</div>'
        '<div class="n">%s ／ 数えた日から %d営業日ぶん、在庫日数を当てたもの</div></div>'
        '<div><div class="k">在庫日数</div><div class="v">%.1f日</div>'
        '<div class="n">在庫 ÷ 日商原価。短いほど、同じ商売でも金が寝ません</div></div>'
        "</div>"
        % (money(real["amount"]), last_real, real["days_of_stock"], hint("在庫日数"),
           money(now["amount"]), days[-1], len(guessed), now["days_of_stock"]))


def purchase(book):
    """仕入。売上の裏側で、いちばん先に動く数字。"""
    buy = book["cash"]["purchase"]
    return (
        '<div class="breakdown">'
        '<div><div class="k">今月ここまでの仕入</div><div class="v">%s</div>'
        '<div class="n">確定した営業日ぶん</div></div>'
        '<div><div class="k">当月の着地見込み</div><div class="v">%s</div>'
        '<div class="n">売上と同じ「前年と同じペースなら」で伸ばしたもの</div></div>'
        '<div><div class="k">前年同月</div><div class="v">%s</div>'
        '<div class="n">前年比 %s</div></div>'
        "</div>"
        % (money(buy["actual"]), money(buy["forecast"]), money(buy["last_year"]),
           pct(buy["vs_ly"], good_high=False)))


def stock_month(view, width=940, height=360, pad=44, flow=96):
    """当月の在庫。**期首から始まり、月の中で経過する。**

    2ヶ月を1枚に並べない ── 在庫は月ごとに締まるので、
    並べるとどちらの月の話をしているのか分からなくなる。

    期首（前月末）は左に1点だけ置いて、点線でつなぐ。**線は当月しか引かない。**
    最後の棚卸までは実線、その先は破線 ── 棚卸で裏が取れているかどうかが、
    確定と推定の分かれ目なので。
    """
    if not view or not view["points"]:
        return ""
    points, opening = view["points"], view["opening"]
    values = [p["amount"] for p in points] + ([opening["amount"]] if opening else [])
    low, high = min(values), max(values)
    span = (high - low) or abs(high) or 1.0
    low, high = low - span * 0.35, high + span * 0.30
    right, left = width - 104, pad + (52 if opening else 0)
    count = len(points)

    def x_of(i):
        return left + (right - left) * (i / max(count - 1, 1))

    top = height - pad - flow                     # 線の帯の下端（ここから下が出入り）

    def y_of(v):
        return top - (top - 26) * ((v - low) / (high - low))

    cut = max((i for i, p in enumerate(points) if p["settled"]), default=0)
    out = ['<svg viewBox="0 0 %d %d" role="img" aria-label="当月の在庫">' % (width, height)]

    # 期首。**当月の中には無い日付**なので、離して置き、点線でつなぐ。
    if opening:
        ox, oy = pad, y_of(opening["amount"])
        out.append('<circle cx="%d" cy="%.1f" r="4" fill="none" stroke="var(--prior)" '
                   'stroke-width="2"/>' % (ox, oy))
        out.append('<line x1="%d" y1="%.1f" x2="%.1f" y2="%.1f" stroke="var(--prior)" '
                   'stroke-width="1.4" stroke-dasharray="3 4"/>'
                   % (ox, oy, x_of(0), y_of(points[0]["amount"])))
        out.append('<text x="%d" y="%.1f" fill="var(--prior)" font-size="11" '
                   'text-anchor="middle">期首</text>' % (ox, oy - 12))
        out.append('<text x="%d" y="%d" fill="var(--prior)" font-size="10.5" '
                   'text-anchor="middle">%s</text>' % (ox, height - pad + 16, opening["date"][5:]))

    def line(items, start=0):
        return " ".join("%.1f,%.1f" % (x_of(start + i), y_of(p["amount"]))
                        for i, p in enumerate(items))

    out.append('<polyline fill="none" stroke="var(--bar)" stroke-width="3" '
               'stroke-linejoin="round" points="%s"/>' % line(points[:cut + 1]))
    if cut < count - 1:
        out.append('<polyline fill="none" stroke="var(--bar)" stroke-width="2.5" '
                   'stroke-dasharray="6 5" points="%s"/>' % line(points[cut:], start=cut))
    for i, p in enumerate(points):
        if p["settled"]:
            out.append('<circle cx="%.1f" cy="%.1f" r="5" fill="var(--bar)"/>'
                       % (x_of(i), y_of(p["amount"])))
            out.append('<text x="%.1f" y="%.1f" fill="var(--muted)" font-size="10.5" '
                       'text-anchor="middle">%s</text>'
                       % (x_of(i), y_of(p["amount"]) + 20, p["date"][8:] + "日 実測"))
    last = points[-1]
    out.append('<circle cx="%.1f" cy="%.1f" r="4" fill="none" stroke="var(--bar)" '
               'stroke-width="2"/>' % (x_of(count - 1), y_of(last["amount"])))

    tags = [(y_of(last["amount"]), "いま %.2f億円" % (last["amount"] / 1e8), "var(--ink)", 13, 700)]
    if opening:
        diff = last["amount"] - opening["amount"]
        tags.append((y_of(opening["amount"]), "期首 %.2f億円" % (opening["amount"] / 1e8),
                     "var(--prior)", 11, 400))
        tags.append((y_of(last["amount"]) + 16, "期首から %s%.0f万円"
                     % ("+" if diff >= 0 else MINUS, abs(diff) / 1e4),
                     "var(--muted)", 11, 400))
    for (_, text, color, size, weight), y in zip(tags, _stack(tags, 15.0, 16.0, height - 32)):
        out.append('<text x="%d" y="%.1f" fill="%s" font-size="%d" font-weight="%d">%s</text>'
                   % (right + 8, y + 4, color, size, weight, text))

    # ── 傾きの理由を、傾きの真下に置く ────────────────────
    #
    # 在庫の線だけでは「仕入れすぎ」と「売れなかった」を言い分けられない。
    # その日に **買った分と売れた分の差** を棒で置けば、山の理由が線の下で読める。
    # 2026-09-02、画面を見て「この山は何か」と問われた ── 問われた時点で、画面の負け。
    flows = [p["moved_in"] - p["moved_out"] for p in points]
    scale = max((abs(v) for v in flows), default=1.0) or 1.0
    zero = top + 30
    bw = max((right - left) / count * 0.42, 4.0)
    out.append('<line x1="%.1f" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--line-2)" '
               'stroke-width="1"/>' % (pad, zero, right, zero))
    out.append('<text x="%d" y="%.1f" fill="var(--muted)" font-size="10">仕入 − 原価</text>'
               % (right + 8, zero + 4))
    peak = max(range(count), key=lambda i: flows[i]) if count else 0
    for i, p in enumerate(points):
        v = flows[i]
        h = abs(v) / scale * (flow - 44)
        y = zero - h if v >= 0 else zero
        color = "var(--ok)" if v >= 0 else "var(--warn)"
        out.append('<rect class="flow" x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2" '
                   'fill="%s" fill-opacity="%s"/>'
                   % (x_of(i) - bw / 2, y, bw, max(h, 1.5), color,
                      ".85" if i == peak else ".45"))
    out.append('<text x="%.1f" y="%.1f" fill="var(--ok)" font-size="10.5" '
               'text-anchor="middle">いちばん積み上がった日 %s%.0f万円</text>'
               % (min(max(x_of(peak), 120), right - 90), zero - (abs(flows[peak]) / scale
                  * (flow - 44)) - 8,
                  "+" if flows[peak] >= 0 else MINUS, abs(flows[peak]) / 1e4))

    for i, p in enumerate(points):
        if i in (0, count - 1) or p["settled"]:
            out.append('<text x="%.1f" y="%d" fill="var(--muted)" font-size="10.5" '
                       'text-anchor="middle">%s</text>'
                       % (x_of(i), height - pad + 16, p["date"][8:]))
    out.append('<text x="%d" y="%d" fill="var(--muted)" font-size="11">%s月</text>'
               % (pad, 18, int(view["month"][5:7])))
    return "".join(out) + "</svg>"


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
            % ("" if is_trend else " mild", escape(name), tag,
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
