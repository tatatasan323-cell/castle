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


def load_free(conn, kind):
    """部門に紐づかない記録（試算表・残高）を {年月: {科目: 金額}} で返す。"""
    out = {}
    for row in conn.execute(
            "SELECT occurred_at, subject, amount FROM records WHERE kind=? AND amount IS NOT NULL",
            (kind,)):
        out.setdefault(row["occurred_at"][:7], {})[row["subject"]] = float(row["amount"])
    return out


def load_purchase(conn):
    """仕入は日次・部門別に入っているが、金繰りで見るのは全社の合計。"""
    out = {}
    for row in conn.execute(
            "SELECT occurred_at, amount FROM records WHERE kind='仕入' AND amount IS NOT NULL"):
        out[row["occurred_at"]] = out.get(row["occurred_at"], 0.0) + float(row["amount"])
    return out


def load_stock(conn):
    """週次の在庫。部門別に入っているが、金繰りで見るのは全社の合計。"""
    out = {}
    for row in conn.execute(
            "SELECT occurred_at, amount FROM records WHERE kind='在庫' AND amount IS NOT NULL"):
        out[row["occurred_at"]] = out.get(row["occurred_at"], 0.0) + float(row["amount"])
    return out


def daily_stock(conn, cfg, days=None, window=None):
    """日次の在庫。**週次までが実データ、そのあいだは置いた値。**

    置き方は着地見込みと同じ考え方 ── 予測モデルは使わない。
    直近の実データから **在庫日数（在庫 ÷ 日商原価）** を取り、その日数を、
    その日の日商原価に当てる。

    **金額を日割りしない。率（日数）を運ぶ。**
    月次の原価率を日次売上に当てるのと、まったく同じ形である。
    仕入を足して原価を引く積み上げにしないのは、廃棄も棚卸差異も拾えず、
    週をまたぐほど実データから離れていくため。在庫日数なら、実データに繋ぎ直せる。
    """
    if days is None:
        days = build(conn, cfg)["days"]
    stock = load_stock(conn)
    rows = sorted(days, key=lambda d: d["date"])
    # 窓は週の営業日数。ずれると曜日の谷が窓に出入りして、在庫が動いたように見える。
    if window is None:
        window = len(business_weekdays([r["date"] for r in rows])) or 6

    costs, out = [], {}
    held = None                      # 直近の実データから取った在庫日数
    for row in rows:
        costs.append(row["sales"] - row["gross"])
        if len(costs) < window:
            continue                       # 窓が満ちるまでは置かない
        daily_cost = sum(costs[-window:]) / window
        if row["date"] in stock:
            amount = stock[row["date"]]
            held = (amount / daily_cost) if daily_cost else held
            out[row["date"]] = {"amount": amount, "settled": True,
                                "days_of_stock": held, "daily_cost": daily_cost}
        elif held is not None:
            out[row["date"]] = {"amount": held * daily_cost, "settled": False,
                                "days_of_stock": held, "daily_cost": daily_cost}
    return out


def _calendar_days(month):
    year, mon = int(month[:4]), int(month[5:7])
    first = datetime.date(year, mon, 1)
    nxt = datetime.date(year + (mon == 12), (mon % 12) + 1, 1)
    return (nxt - first).days


def build_ladder(cfg, month, trial):
    """営業利益の先。段ごとに符号を持たせ、小計は積み上げと一致させる。

    **営業外と特別で扱いを変えている。**
    営業外（利息・仕入割引など）は毎月出るものなので、当月が未確定なら直近確定月の額を当てる。
    特別損益は非経常 ── 前月に減損があったからといって今月も出るわけがない。
    だから当月に確定していなければ **ゼロのまま置く**。推定してよいものと、してはいけないものがある。
    """
    acct = cfg.accounting or {}
    settled = sorted(m for m in trial if m < month["month"])
    recurring_src = month["month"] if month["month"] in trial else (settled[-1] if settled else None)

    def bucket(name, source):
        if source is None:
            return 0.0
        return sum(trial.get(source, {}).get(n, 0.0) for n in acct.get(name, []))

    nonop_in = bucket("営業外収益", recurring_src)
    nonop_out = bucket("営業外費用", recurring_src)
    extra_src = month["month"] if month["month"] in trial else None
    extra_in = bucket("特別利益", extra_src)
    extra_out = bucket("特別損失", extra_src)

    def ladder_of(sales, gross, labor, sga, op, nonop_in, nonop_out, extra_in, extra_out):
        ordinary = op + nonop_in - nonop_out
        pretax = ordinary + extra_in - extra_out
        tax = max(0.0, pretax) * rate
        return [
            ("売上高", sales, 1, ""),
            ("売上原価", sales - gross, -1, "月次の原価率を日次売上に当てたもの"),
            ("売上総利益", gross, 0, ""),
            ("人件費", labor, -1, "勤怠の人時 × 会計の人時単価"),
            ("その他販管費", sga, -1, "月額を営業日数で割ったもの"),
            ("営業利益", op, 0, ""),
            ("営業外収益", nonop_in, 1, "受取利息・仕入割引など"),
            ("営業外費用", nonop_out, -1, "支払利息など"),
            ("経常利益", ordinary, 0, ""),
            ("特別利益", extra_in, 1, ""),
            ("特別損失", extra_out, -1, ""),
            ("税引前当期純利益", pretax, 0, ""),
            ("法人税等", tax, -1, "実効税率 %.0f%% での概算" % (rate * 100)),
            ("当期純利益", pretax - tax, 0, ""),
        ]

    rate = acct.get("effective_tax_rate", 0.30)
    now = ladder_of(month["forecast_sales"], month["forecast_gross"],
                    month["forecast_labor"], month["forecast_fixed"], month["forecast_op"],
                    nonop_in, nonop_out, extra_in, extra_out)

    # 前年同月の同じ階段。段ごとに比べられないと「順調かどうか」は言えない。
    ly_month = (datetime.date.fromisoformat(month["dates"][0])
                - datetime.timedelta(days=364)).strftime("%Y-%m")
    ly_sga = month["last_year_gross"] - month["last_year_labor"] - month["last_year_op"]
    prior = ladder_of(month["last_year_sales"], month["last_year_gross"],
                      month["last_year_labor"], ly_sga, month["last_year_op"],
                      bucket("営業外収益", ly_month if ly_month in trial else None),
                      bucket("営業外費用", ly_month if ly_month in trial else None),
                      bucket("特別利益", ly_month if ly_month in trial else None),
                      bucket("特別損失", ly_month if ly_month in trial else None))

    steps = []
    for (label, value, sign, note), (_l, was, _s, _n) in zip(now, prior):
        steps.append({"label": label, "amount": value, "sign": sign, "note": note,
                      "last_year": was,
                      "vs_ly": ((value / was - 1) * 100) if was else None})
    return {
        "steps": steps,
        "nonop_from": recurring_src,
        "nonop_estimated": recurring_src != month["month"],
        "extra_settled": extra_src is not None,
        "last_year_month": ly_month,
        "tax_rate": rate,
        "depreciation": bucket("減価償却費", recurring_src),
        "net": steps[-1]["amount"],
    }


def build_cash(cfg, month, balances, buys, by_date, ladder_block, yoy_offset, stock=None):
    """金は回るか。残高は確定した月末のものしか無い ── そこを画面でも隠さない。"""
    names = (cfg.accounting or {}).get("balance", {})
    settled = sorted(m for m in balances if m <= month["month"])
    if not settled:
        return None
    latest = settled[-1]
    prev = settled[-2] if len(settled) > 1 else None

    def at(source, key):
        return balances.get(source, {}).get(names.get(key, key), 0.0)

    stock = stock or {}

    def stock_at(month_key):
        """その月末に間に合っている、いちばん新しい実データ。**推定は混ぜない。**"""
        end = "%s-31" % month_key
        got = [d for d in stock if d <= end]
        return (max(got), stock[max(got)]) if got else (None, 0.0)

    out = {}
    for key in names:
        now = at(latest, key)
        before = at(prev, key) if prev else None
        out[key] = {"amount": now, "prev": before,
                    "change": (now - before) if before else None,
                    "vs_prev": ((now / before - 1) * 100) if before else None}

    stock_at_latest, stock_now = stock_at(latest)
    stock_at_prev, stock_before = stock_at(prev) if prev else (None, None)
    out["棚卸資産"] = {"amount": stock_now, "prev": stock_before,
                    "change": (stock_now - stock_before) if stock_before else None,
                    "vs_prev": ((stock_now / stock_before - 1) * 100) if stock_before else None}

    span = _calendar_days(latest)
    m_sales = sum(v["sales"] for d, v in by_date.items() if d[:7] == latest)
    m_gross = sum(v["gross"] for d, v in by_date.items() if d[:7] == latest)
    m_buy = sum(v for d, v in buys.items() if d[:7] == latest)

    def days_of(balance, flow):
        return (balance / (flow / span)) if flow else 0.0

    ccc = {
        "month": latest,
        "receivable_days": days_of(out["売掛金"]["amount"], m_sales),
        "inventory_days": days_of(out["棚卸資産"]["amount"], m_sales - m_gross),
        "payable_days": days_of(out["買掛金"]["amount"], m_buy),
    }
    ccc["days"] = ccc["receivable_days"] + ccc["inventory_days"] - ccc["payable_days"]

    working = out["売掛金"]["amount"] + out["棚卸資産"]["amount"] - out["買掛金"]["amount"]
    working_prev = None
    if prev and stock_before is not None:
        working_prev = at(prev, "売掛金") + stock_before - at(prev, "買掛金")

    shift = datetime.timedelta(days=yoy_offset)

    def ly(day):
        return (datetime.date.fromisoformat(day) - shift).isoformat()

    actual_days = [d for d in month["dates"] if d in by_date]
    rest_days = [d for d in month["dates"] if d > month["last_actual_date"]]
    buy_actual = sum(buys.get(d, 0.0) for d in actual_days)
    buy_rest_ly = sum(buys.get(ly(d), 0.0) for d in rest_days)
    buy_forecast = buy_actual + buy_rest_ly * month["pace"]
    buy_ly = sum(buys.get(ly(d), 0.0) for d in month["dates"])

    # 稼いだ利益がそのまま現金になるわけではない ──
    # 減価償却は現金が出ていかず、運転資本が膨らめばその分だけ現金は減る。
    wc_change = (working - working_prev) if working_prev is not None else 0.0
    # 「運転資本が増えた」だけでは打つ手が決まらない。売掛なら回収、在庫なら発注。
    parts = {}
    if prev:
        for key, sign in (("売掛金", 1), ("棚卸資産", 1), ("買掛金", -1)):
            if key == "棚卸資産":
                parts[key] = (stock_now - stock_before) if stock_before is not None else 0.0
            else:
                parts[key] = sign * (at(latest, key) - at(prev, key))
    cash_end = out["現預金"]["amount"] + ladder_block["net"] + ladder_block["depreciation"] - wc_change

    return dict(out, ccc=ccc, working_capital=working, working_capital_prev=working_prev,
                working_capital_change=wc_change, working_capital_parts=parts,
                cash_end_forecast=cash_end, as_of=latest,
                stock_settled_at=stock_at_latest,
                purchase={"actual": buy_actual, "forecast": buy_forecast, "last_year": buy_ly,
                          "vs_ly": ((buy_forecast / buy_ly - 1) * 100) if buy_ly else None,
                          "series": [(d, buys.get(d, 0.0)) for d in sorted(by_date)]})


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

    trial = load_free(conn, "試算表")
    balances = load_free(conn, "残高")
    buys = load_purchase(conn)
    ladder_block = build_ladder(cfg, month_block, trial)
    cash_block = build_cash(cfg, month_block, balances, buys, by_date, ladder_block,
                            yoy_offset, load_stock(conn))

    return {"days": days, "month": month_block, "ladder": ladder_block, "cash": cash_block,
            "stock": daily_stock(conn, cfg, days),
            "departments": departments,
            "series_months": series_months, "monthly_by_dept": by_dept}
