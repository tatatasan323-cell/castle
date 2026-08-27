"""経営者の問いに答える計算機。

> **今月、いくら儲かって終わるのか。それは順調なのか。**

締めを待たずにこれを出すのが、城の存在理由。月次が締まるのは翌月10日ごろで、
それまで社長は手探りで走っている。**その待ち時間を消す。**

日次の営業利益を、既存3サイロの掛け算で組み立てる。新しいサイロは要らない。

    売上      販売管理（日次）           そのまま
    売上原価  会計（月次の原価率）       日次売上 × 原価率
    人件費    勤怠（日次人時）× 会計（月次の人時単価）
    その他販管費  会計（月次）           固定費なので営業日数で日割り

着地見込みに**予測モデルは使わない**。説明できる比例配分だけ。
「前年と同じペースで残りも進んだら、こうなる」と言えることが要件。
"""

import datetime

DAILY_KINDS = ("売上", "労働時間")
MONTHLY_KIND = "部門損益"


def _month_of(day):
    return day[:7]


def load_monthly(conn):
    """会計の月次。{部門: {月: {sales, cost, labor, sga}}}"""
    rows = conn.execute(
        """SELECT subject, occurred_at,
                  json_extract(body,'$.sales') s, json_extract(body,'$.cost')  c,
                  json_extract(body,'$.labor') l, json_extract(body,'$.sga')   g
             FROM records WHERE kind=?""", (MONTHLY_KIND,)).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["subject"], {})[_month_of(r["occurred_at"])] = {
            "sales": r["s"] or 0, "cost": r["c"] or 0, "labor": r["l"] or 0, "sga": r["g"] or 0}
    return out


def load_daily(conn):
    """販売管理と勤怠の日次。売上は営業部門だけ、人時は全部門にある。"""
    sales, hours = {}, {}
    for r in conn.execute(
            "SELECT kind, occurred_at d, subject s, amount a, hours h FROM records WHERE kind IN (?,?)",
            DAILY_KINDS):
        if r["kind"] == "売上" and r["a"] is not None:
            sales.setdefault(r["s"], {})[r["d"]] = float(r["a"])
        elif r["kind"] == "労働時間" and r["h"] is not None:
            hours.setdefault(r["s"], {})[r["d"]] = float(r["h"])
    return sales, hours


def settled_value(by_month, month, key):
    """その月が締まっていれば実績。まだなら直近の確定月を当てる（＝推定）。"""
    if month in by_month:
        return by_month[month].get(key), False
    older = [m for m in by_month if m < month]
    return (by_month[max(older)].get(key), True) if older else (None, True)


def business_weekdays(dates):
    """データのある曜日を営業曜日とみなす。暦の前提をコードに埋め込まない。"""
    seen = {datetime.date.fromisoformat(d).weekday() for d in dates}
    return seen or {0, 1, 2, 3, 4}


def month_calendar(month, weekdays):
    first = datetime.date.fromisoformat(month + "-01")
    last = (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
    return [(first + datetime.timedelta(days=i)).isoformat()
            for i in range((last - first).days + 1)
            if (first + datetime.timedelta(days=i)).weekday() in weekdays]


def build(conn, cfg, yoy_offset=364):
    monthly = load_monthly(conn)
    sales, hours = load_daily(conn)
    if not sales:
        return {"days": [], "month": None, "departments": {}}

    selling = [d["name"] for d in cfg.measured() if d["name"] in sales]
    all_days = sorted({d for by in sales.values() for d in by})

    # 固定費の日割りの分母は「その月の営業日数」。**データのある日数ではない** ──
    # 当月は途中までしかデータが無いので、そこで割ると固定費が数倍に膨らむ。
    weekdays = business_weekdays(all_days)
    days_in_month, dept_hours = {}, {}
    for month in {_month_of(d) for d in all_days}:
        days_in_month[month] = len(month_calendar(month, weekdays))
    for dept, by in hours.items():
        for day, h in by.items():
            dept_hours.setdefault(dept, {})[_month_of(day)] = dept_hours.setdefault(dept, {}).get(_month_of(day), 0) + h

    def cost_rate(dept, month):
        row, est = settled_value(monthly.get(dept, {}), month, "cost")
        base, _ = settled_value(monthly.get(dept, {}), month, "sales")
        return ((row / base) if row is not None and base else None), est

    def wage(dept, month):
        labor, est = settled_value(monthly.get(dept, {}), month, "labor")
        source = month if month in monthly.get(dept, {}) else max(
            [m for m in monthly.get(dept, {}) if m < month], default=None)
        total = (dept_hours.get(dept, {}) or {}).get(source)
        return ((labor / total) if labor is not None and total else None), est

    def daily_sga(dept, month):
        value, est = settled_value(monthly.get(dept, {}), month, "sga")
        divisor = days_in_month.get(month) or 1
        return ((value / divisor) if value is not None else 0.0), est

    # ------------------------------------------------------------ 日次のP&L
    days = []
    for day in all_days:
        month = _month_of(day)
        estimated = False
        s = c = l = g = 0.0
        for dept in selling:
            amount = sales.get(dept, {}).get(day, 0.0)
            s += amount
            rate, est = cost_rate(dept, month)
            estimated |= est
            c += amount * (rate or 0)
        for dept in list(hours):
            per_hour, est = wage(dept, month)
            estimated |= est
            l += hours[dept].get(day, 0.0) * (per_hour or 0)
        for dept in list(monthly) or []:
            per_day, est = daily_sga(dept, month)
            estimated |= est
            g += per_day
        days.append({"date": day, "sales": s, "cost": c, "labor": l, "sga": g,
                     "gross": s - c, "op": s - c - l - g, "estimated": estimated})

    by_date = {d["date"]: d for d in days}

    # ------------------------------------------------------------ 当月と、その着地
    this_month = _month_of(all_days[-1])
    calendar = month_calendar(this_month, weekdays)
    actual = [d for d in calendar if d in by_date]
    remaining = [d for d in calendar if d > all_days[-1]]

    def total(field, dates):
        return sum(by_date[d][field] for d in dates if d in by_date)

    actual_op = total("op", actual)
    actual_sales = total("sales", actual)
    actual_gross = total("gross", actual)

    # 前年同月（364日ずらし＝曜日が揃う）
    def shifted(dates):
        return [(datetime.date.fromisoformat(d) - datetime.timedelta(days=yoy_offset)).isoformat() for d in dates]

    ly_actual = shifted(actual)
    ly_remaining = shifted(remaining)
    ly_actual_sales = total("sales", ly_actual)
    ly_remaining_sales = total("sales", ly_remaining)

    # 見込み：売上は「前年と同じペースなら」。人件費は直近平均。固定費は日割り。
    pace = (actual_sales / ly_actual_sales) if ly_actual_sales else 1.0
    forecast_sales = ly_remaining_sales * pace
    margin = (actual_gross / actual_sales) if actual_sales else 0.0
    forecast_gross = forecast_sales * margin
    recent = actual[-7:] or actual
    daily_labor = (total("labor", recent) / len(recent)) if recent else 0.0
    daily_fixed = (total("sga", recent) / len(recent)) if recent else 0.0
    forecast_op = actual_op + forecast_gross - (daily_labor + daily_fixed) * len(remaining)

    # 航海図：累計の時系列（実績はそのまま、残りは見込みを日割りで積む）
    cumulative, running = [], 0.0
    for day in actual:
        running += by_date[day]["op"]
        cumulative.append(running)
    step = ((forecast_op - actual_op) / len(remaining)) if remaining else 0.0
    for _ in remaining:
        running += step
        cumulative.append(running)

    ly_cumulative, running = [], 0.0
    for day in shifted(calendar):
        if day in by_date:
            running += by_date[day]["op"]
            ly_cumulative.append(running)
        else:
            ly_cumulative.append(None)

    budget = (cfg.budget or {}).get("monthly_operating_profit")
    ly_op = total("op", shifted(calendar))

    month_block = {
        "month": this_month,
        "actual_days": len(actual), "remaining_days": len(remaining),
        "last_actual_date": all_days[-1],
        "actual_op": actual_op, "forecast_op": forecast_op,
        "actual_sales": actual_sales, "forecast_sales": actual_sales + forecast_sales,
        "actual_gross": actual_gross, "forecast_gross": actual_gross + forecast_gross,
        "margin": margin, "pace": pace,
        "forecast_labor": total("labor", actual) + daily_labor * len(remaining),
        "forecast_fixed": total("sga", actual) + daily_fixed * len(remaining),
        "last_year_sales": total("sales", shifted(calendar)),
        "last_year_gross": total("gross", shifted(calendar)),
        "last_year_labor": total("labor", shifted(calendar)),
        "budget": budget,
        "vs_budget": ((forecast_op / budget - 1) * 100) if budget else None,
        "gross_budget": sum(((cfg.budget or {}).get("department_gross") or {}).values()) or None,
        "last_year_op": ly_op,
        "vs_last_year": ((forecast_op / ly_op - 1) * 100) if ly_op else None,
        "cumulative": cumulative, "last_year_cumulative": ly_cumulative,
        "dates": calendar,
        "estimated": any(by_date[d]["estimated"] for d in actual),
    }

    # ------------------------------------------------------------ 部門別：何が伸びて、何が落ちているか
    dept_budget = ((cfg.budget or {}).get("department_gross") or {})
    departments = {}
    for dept in selling:
        rate, _ = cost_rate(dept, this_month)
        def gross_of(dates):
            return sum(sales.get(dept, {}).get(d, 0.0) * (1 - (rate or 0)) for d in dates)
        actual_g = gross_of(actual)
        ly_g_actual = sum(sales.get(dept, {}).get(d, 0.0) for d in ly_actual)
        ly_g_rest = sum(sales.get(dept, {}).get(d, 0.0) for d in ly_remaining)
        dept_pace = (sum(sales.get(dept, {}).get(d, 0.0) for d in actual) / ly_g_actual) if ly_g_actual else 1.0
        forecast_g = actual_g + ly_g_rest * dept_pace * (1 - (rate or 0))
        ly_full = sum(sales.get(dept, {}).get(d, 0.0) for d in shifted(calendar))
        ly_rate, _ = cost_rate(dept, _month_of(shifted(calendar)[0]))
        ly_gross = ly_full * (1 - (ly_rate if ly_rate is not None else (rate or 0)))
        # 当月ここまでの、前年同期との比較（警告の内訳に使う）
        sales_now = sum(sales.get(dept, {}).get(d, 0.0) for d in actual)
        hours_now = sum(hours.get(dept, {}).get(d, 0.0) for d in actual)
        hours_ly = sum(hours.get(dept, {}).get(d, 0.0) for d in ly_actual)
        target = dept_budget.get(dept)
        departments[dept] = {
            "sales_vs_ly": ((sales_now / ly_g_actual - 1) * 100) if ly_g_actual else None,
            "hours_vs_ly": ((hours_now / hours_ly - 1) * 100) if hours_ly else None,
            "actual_gross": actual_g, "forecast_gross": forecast_g,
            "budget": target,
            "vs_budget": ((forecast_g / target - 1) * 100) if target else None,
            "last_year_gross": ly_gross,
            "vs_last_year": ((forecast_g / ly_gross - 1) * 100) if ly_gross else None,
            "margin": 1 - (rate or 0),
        }

    # 部門別の月次推移。傾向は単月では言えないので、確定した月を並べて渡す。
    series_months = sorted({m for by in monthly.values() for m in by})
    by_dept = {}
    for dept in selling:
        points = []
        for month in series_months:
            row = monthly.get(dept, {}).get(month)
            if row and row.get("sales"):
                gross = row["sales"] - row["cost"]
                points.append((month, gross, gross / row["sales"]))
            else:
                points.append((month, None, None))
        by_dept[dept] = points

    return {"days": days, "month": month_block, "departments": departments,
            "series_months": series_months, "monthly_by_dept": by_dept}
