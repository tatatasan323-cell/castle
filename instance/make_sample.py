"""架空食品商事のサンプルCSVを作る。実在の企業・実在のデータは一切含まない。

instance/ 側に置いてある理由：これは「骨」ではなく「肉」だから。
castle/ には汎用の骨しか置かない。

わざと揃えていないもの（現実がそうだから）：
  - 文字コード      販売管理=CP932 ／ 勤怠=UTF-8(BOM付き)
  - 日付の書式      2026/06/01 ／ 2026-06-01
  - 部門名          「加工食品部」／「加工食品」、「菓子・飲料部」／「菓子飲料」
  - 半角カナ・空白  「物流ｾﾝﾀｰ」「業務用　食材」
  - 並び順          日付順 ／ 組織順
  - 余計な行        見出し前のタイトル行、末尾の合計行
  - 単位            会計だけ千円単位（1000倍しないと桁が3つ足りない）
  - 日付の在り処    会計は行に日付が無い。ヘッダのタイトル行にしか年月が書いていない
  - 部門の書き方    会計は「1010 加工食品」とコードと名前が1セルに同居
どのベンダーも悪くない。誰も「間」を担当していないだけ。

  python instance/make_sample.py
"""

import datetime
import pathlib
import random

OUT = pathlib.Path(__file__).resolve().parent / "incoming"

START = datetime.date(2026, 6, 1)
END = datetime.date(2026, 8, 12)          # 販売管理・勤怠が出せている最終日（取り込みは必ず遅れる）
MONTH_END = datetime.date(2026, 8, 31)    # 当月の末日。着地見込みはここまでを埋める
YOY_OFFSET = 364               # 52週。曜日が揃うので前年同期の比較が素直になる

# コード, 販売管理側の部門名, 勤怠側の組織名, 日次売上の基準, 目標人時生産性, 定常水準, 人時単価
SALES_DEPTS = [
    ("1010", "加工食品部",   "加工食品",     38_000_000, 68000, 1.04, 2600),
    ("1020", "低温食品部",   "低温食品",     26_000_000, 58000, 0.99, 2600),
    ("1030", "農産部",       "農産",         14_000_000, 46000, 1.01, 2600),
    ("1040", "水産部",       "水産",         12_000_000, 52000, 0.96, 2650),
    ("1050", "畜産部",       "畜産",         18_000_000, 64000, 1.05, 2650),
    ("1060", "菓子・飲料部", "菓子飲料",     16_000_000, 54000, 1.00, 2550),
    ("1070", "業務用食材部", "業務用　食材", 11_000_000, 41000, 0.94, 2500),
]

# 売上を持たない部門。勤怠システムは当然これらの人時も持っている。
# コード, 勤怠側の組織名, 日次人時, 人時単価, 月次その他販管費
INDIRECT = [("9010", "物流ｾﾝﾀｰ", 620.0, 1900, 45_000_000),
            ("9020", "品質保証",  72.0, 2800,  5_000_000),
            ("9030", "管理",     128.0, 3000, 18_000_000)]

# 営業部門のその他販管費は売上に比例させる（配送・販促・通信など）
SGA_RATE = 0.022

# 食品卸の原価率。業務用は手間がかかる分だけ粗利率が高く、加工食品は薄い。
# 売上ベースの人時生産性と粗利ベースで順位が入れ替わるのは、この差のため。
COST_RATE = {"1010": 0.920, "1020": 0.885, "1030": 0.865,
             "1040": 0.835, "1050": 0.870, "1060": 0.890, "1070": 0.800}
# 農産部だけ、相場高で仕入値が上がったぶんを売価に転嫁しきれていない
NOSAN_COST = {"2026-06": 0.865, "2026-07": 0.878, "2026-08": 0.888}
# 会計は月次で締める。当月はまだ確定していないので出力されない。
UNSETTLED = "2026-08"

WEEKDAY = [1.10, 1.06, 1.00, 1.02, 1.09, 0.70]      # 月〜土。日曜は休み
# 前年は当月末ぶんまで作るので、9月にわずかにはみ出す（2025-09-01）。その分も持たせる。
SEASON = {
    "1040": {6: 0.98, 7: 1.08, 8: 1.14, 9: 1.05},
    "1060": {6: 0.97, 7: 1.12, 8: 1.18, 9: 1.04},
    "1050": {6: 1.00, 7: 1.06, 8: 1.10, 9: 1.04},
    "1030": {6: 1.02, 7: 1.00, 8: 0.96, 9: 1.02},
}
SEASON_DEFAULT = {6: 1.00, 7: 1.02, 8: 1.04, 9: 1.02}


def business_days(first, last):
    span = (last - first).days + 1
    return [first + datetime.timedelta(days=i) for i in range(span)
            if (first + datetime.timedelta(days=i)).weekday() < 6]


def sales_event(code, day):
    """農産部：7月中旬から相場高で数量が落ちていく（＝売上側の要因）。"""
    if code == "1030":
        weeks = (day - datetime.date(2026, 7, 13)).days // 7
        return [1.0, 0.97, 0.94, 0.90, 0.87][min(weeks + 1, 4)] if weeks >= -1 else 1.0
    if code == "1010" and day >= datetime.date(2026, 7, 20):
        return 1.03
    return 1.0


def hours_event(code, day):
    """業務用食材部：直近1週だけ、盆前の休業先増でルート効率が落ちた（＝人時側の要因）。"""
    if code == "1070" and day >= datetime.date(2026, 8, 6):
        return 1.15
    return 1.0


def build(pairs, current_year):
    """pairs は (モデル用の日, 記録する日)。前年は当月末まで揃える ──
    **前年が月末まで無いと、今年の着地見込みが立たない。**"""
    rng = random.Random(20260813 if current_year else 20250813)
    sales_rows, hours_rows = [], []
    year_scale = 1.0 if current_year else 0.975
    hour_scale = 1.0 if current_year else 1.01

    for day, stamp in pairs:
        wd_s = WEEKDAY[day.weekday()]
        wd_h = 1 + (wd_s - 1) * 0.65          # 人員は売上ほど曜日に追随しない

        for code, name, org, base, target, level, _wage in SALES_DEPTS:
            season = SEASON.get(code, SEASON_DEFAULT)[stamp.month]
            sales = base * wd_s * season * level * year_scale * rng.gauss(1, 0.035)
            hours = (base / target) * wd_h * (1 + (season - 1) * 0.7) * hour_scale * rng.gauss(1, 0.025)
            if current_year:
                sales *= sales_event(code, day)
                hours *= hours_event(code, day)

            sales = max(int(sales), 0)
            hours = round(max(hours, 1.0), 1)
            sales_rows.append((code, name, stamp, sales, max(int(sales / rng.uniform(150000, 195000)), 1)))
            hours_rows.append((org, stamp, hours, round(hours * rng.uniform(0.04, 0.08), 1)))

        for code, org, base_hours, _wage, _sga in INDIRECT:
            hours = round(base_hours * wd_h * rng.gauss(1, 0.02), 1)
            hours_rows.append((org, stamp, hours, round(hours * rng.uniform(0.05, 0.10), 1)))

    return sales_rows, hours_rows


def cost_rate(code, month):
    if code == "1030":
        return NOSAN_COST.get(month, COST_RATE[code])
    return round(COST_RATE[code] + random.Random(month + code).gauss(0, 0.004), 4)


def write_accounting(rows, hours_rows, current_year):
    """会計は月次・千円単位・年月はタイトル行だけ。確定した月しか出さない。

    **全部門ぶん出す**（売上を持たない間接部門も、人件費と経費は発生する）。
    人件費を部門別に出すのは、多くの会計システムの部門別損益がそうしているから。
    """
    org_of = {code: org for code, _n, org, _b, _t, _lv, _w in SALES_DEPTS}
    org_of.update({code: org for code, org, _h, _w, _s in INDIRECT})
    wage_of = {code: w for code, _n, _o, _b, _t, _lv, w in SALES_DEPTS}
    wage_of.update({code: w for code, _o, _h, w, _s in INDIRECT})

    totals, hours = {}, {}
    for code, name, day, sales, _slips in rows:
        totals[(day.strftime("%Y-%m"), code)] = totals.get((day.strftime("%Y-%m"), code), 0) + sales
    by_org = {}
    for org, day, hrs, _ot in hours_rows:
        by_org[(day.strftime("%Y-%m"), org)] = by_org.get((day.strftime("%Y-%m"), org), 0) + hrs

    written = 0
    for month in sorted({m for m, _ in totals}):
        if month == UNSETTLED:
            continue
        if len({d for _c, _n, d, _s, _x in rows if d.strftime("%Y-%m") == month}) < 15:
            continue                       # 端の月は日数が足りない。会計は締めない
        lines = ["会計システム　部門別損益計算書　%s年%d月度　出力:%s"
                 % (month[:4], int(month[5:]), month + "-10 締"),
                 "部門,売上高(千円),売上原価(千円),人件費(千円),その他販管費(千円)"]
        sums = [0, 0, 0, 0]

        def row(code, label, sales, cost, sga_fixed=None):
            hrs = by_org.get((month, org_of[code]), 0)
            labor = int(hrs * wage_of[code])
            sga = int(sga_fixed if sga_fixed is not None else sales * SGA_RATE)
            for i, v in enumerate((sales, cost, labor, sga)):
                sums[i] += v
            lines.append("%s %s,%d,%d,%d,%d" % (code, label, round(sales / 1000), round(cost / 1000),
                                                round(labor / 1000), round(sga / 1000)))

        for code, name, _org, _base, _t, _lv, _w in SALES_DEPTS:
            sales = totals.get((month, code), 0)
            row(code, name.replace("部", ""), sales, int(sales * cost_rate(code, month)))
        for code, org, _h, _w, monthly_sga in INDIRECT:
            row(code, org, 0, 0, sga_fixed=monthly_sga)

        lines.append("合計,%d,%d,%d,%d" % tuple(round(v / 1000) for v in sums))
        path = OUT / ("C社会計_部門別損益_%s.csv" % month.replace("-", ""))
        path.write_bytes(("\r\n".join(lines) + "\r\n").encode("cp932"))
        written += 1
    return written


def write_sales(path, rows, issued):
    rows = sorted(rows, key=lambda r: (r[2], r[0]))
    lines = ["■売上日報　出力日:%s" % issued.strftime("%Y/%m/%d")]
    lines.append("部門コード,部門名,計上日,売上金額（税抜）,伝票枚数")
    for code, name, day, sales, slips in rows:
        lines.append("%s,%s,%s,%d,%d" % (code, name, day.strftime("%Y/%m/%d"), sales, slips))
    lines.append(",合計,,%d," % sum(r[3] for r in rows))     # 集計行。部門マスタに無いのでスキップされる
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("cp932"))
    return len(rows)


def write_hours(path, rows):
    rows = sorted(rows, key=lambda r: (r[0], r[1]))
    lines = ["組織,日付,実労働時間,残業時間"]
    for org, day, hours, overtime in rows:
        lines.append("%s,%s,%.1f,%.1f" % (org, day.strftime("%Y-%m-%d"), hours, overtime))
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8-sig"))
    return len(rows)


# ── 会計サイロの残り半分 ──────────────────────────────────────
# 部門別損益だけでは営業利益までしか出ない。経営者が見るのはその先。
# 営業外・特別・税金は部門に紐づかないので、部門列を持たない縦持ちで受ける。

CRLF = chr(13) + chr(10)

# 月次の営業外・特別損益（千円）。実在の企業の数字ではない
NONOP = {
    "受取利息": 300, "仕入割引": 12000, "雑収入": 3000,
    "支払利息": 2500, "為替差損": 800, "減価償却費": 22000,
}
# 特別損益は毎月は出ない。出る月だけ入れる ── 階段が動くのを見せるため
SPECIAL = {
    "2026-06": {"固定資産売却益": 25000},
    "2026-07": {"減損損失": 40000},
    "2025-06": {"固定資産売却益": 8000},
}
# 月末残高（千円）。月商およそ38億の会社として置いた
BALANCE = {
    "現金及び預金": 1_480_000, "売掛金": 4_050_000, "棚卸資産": 1_310_000,
    "買掛金": 3_020_000, "未払金": 480_000, "短期借入金": 2_000_000,
}
OPENING = {                            # 期首（最初の月の前月末）。ここから流れで積む
    "現金及び預金": 1_620_000_000, "売掛金": 3_900_000_000, "棚卸資産": 1_240_000_000,
    "買掛金": 2_950_000_000, "未払金": 470_000_000, "短期借入金": 2_260_000_000,
}
REPAY = 20_000_000                     # 毎月の約定返済

# 回収率（前月の売上のうち、当月に入ってくる割合）。
# ここが1.0を割ると売掛が積み上がり、利益が出ていても現金が細る ──
# 「黒字なのに金が無い」の、いちばんありふれた原因。
COLLECT = {"2025-07": 0.998, "2025-08": 0.997, "2026-06": 0.985, "2026-07": 0.955}
COLLECT_DEFAULT = 0.998



# 営業外も毎月同じではない。仕入割引は仕入高に連動し、支払利息は借入残に連動する。
# 全月同額にすると画面に「前年比 +0.0%」が並び、壊れているように見える。
NONOP_DRIFT = {
    "2025-06": {"仕入割引": 0.93, "支払利息": 0.86}, "2025-07": {"仕入割引": 0.95, "支払利息": 0.88},
    "2025-08": {"仕入割引": 0.96, "支払利息": 0.90}, "2026-06": {"仕入割引": 1.02, "支払利息": 1.00},
    "2026-07": {"仕入割引": 0.98, "支払利息": 1.04},
}


def write_trial(months):
    """合計残高試算表。縦持ち・千円・部門列なし。"""
    written = 0
    for month in months:
        lines = ["会計システム　合計残高試算表　%s年%d月度　出力:%s"
                 % (month[:4], int(month[5:]), month + "-10 締"),
                 "科目,金額(千円)"]
        drift = NONOP_DRIFT.get(month, {})
        for name, value in NONOP.items():
            lines.append("%s,%d" % (name, round(value * drift.get(name, 1.0))))
        for name, value in SPECIAL.get(month, {}).items():
            lines.append("%s,%d" % (name, value))
        path = OUT / ("C社会計_試算表_%s.csv" % month.replace("-", ""))
        path.write_bytes((CRLF.join(lines) + CRLF).encode("cp932"))
        written += 1
    return written


def buy_of(code, day, sales):
    """その日の仕入。売上原価に、日ごとの発注の波を掛けたもの。

    **式はここ1箇所に置く。** 仕入日報と月末残高の両方が同じ式を使うので、
    片方だけ直すと貸借と損益が繋がらなくなる。
    """
    return int(sales * cost_rate(code, day.strftime("%Y-%m")) * (0.94 + 0.12 * ((day.day % 7) / 6)))


def write_balance(months, rows, hours_rows):
    """月末残高。**流れから積み上げて作る。**

    残高を係数で置くと、貸借と損益が繋がらない ── CCCも運転資本も
    「それらしいだけの数字」になる。だから、実際の売上・仕入・原価から積む。

      売掛金  = 前月末 ＋ 当月売上 − 当月回収（回収は前月売上ぶん）
      買掛金  = 前月末 ＋ 当月仕入 − 当月支払（支払は前月仕入ぶん）
      棚卸資産 = 前月末 ＋ 当月仕入 − 当月売上原価 ＋ 棚卸差異
      現預金  = 前月末 ＋ 回収 − 支払 − 人件費 − 販管費 − 営業外収支 − 借入返済

    棚卸差異は±0.4%まで。実地棚卸は必ず少しズレる ── ゼロにすると嘘になる。
    """
    org_of = {code: org for code, _n, org, _b, _t, _lv, _w in SALES_DEPTS}
    org_of.update({code: org for code, org, _h, _w, _s in INDIRECT})
    wage_of = {code: w for code, _n, _o, _b, _t, _lv, w in SALES_DEPTS}
    wage_of.update({code: w for code, _o, _h, w, _s in INDIRECT})

    sales_m, buy_m, cost_m = {}, {}, {}
    for code, _name, day, sales, _slips in rows:
        key = day.strftime("%Y-%m")
        sales_m[key] = sales_m.get(key, 0) + sales
        buy_m[key] = buy_m.get(key, 0) + buy_of(code, day, sales)
        cost_m[key] = cost_m.get(key, 0) + int(sales * cost_rate(code, key))

    hours_m = {}
    for org, day, hrs, _ot in hours_rows:
        hours_m.setdefault(day.strftime("%Y-%m"), {}).setdefault(org, 0.0)
        hours_m[day.strftime("%Y-%m")][org] += hrs

    def labor_of(month):
        total = 0
        for code in list(org_of):
            total += int(hours_m.get(month, {}).get(org_of[code], 0.0) * wage_of[code])
        return total

    def sga_of(month):
        return (int(sales_m.get(month, 0) * SGA_RATE)
                + sum(monthly for _c, _o, _h, _w, monthly in INDIRECT))

    ordered = sorted(months)
    every = sorted(sales_m)
    state = dict(OPENING)
    rnd = random.Random(20260630)
    written = 0
    for month in every:
        before = every[every.index(month) - 1] if every.index(month) else None
        collect = (sales_m.get(before, sales_m[month])
                   * COLLECT.get(month, COLLECT_DEFAULT))   # 回収は1か月遅れ
        pay = buy_m.get(before, buy_m[month])                      # 支払も1か月遅れ
        drift = 1.0 + rnd.uniform(-0.004, 0.004)                   # 棚卸差異

        state["売掛金"] += sales_m[month] - collect
        state["買掛金"] += buy_m[month] - pay
        state["棚卸資産"] = (state["棚卸資産"] + buy_m[month] - cost_m[month]) * drift
        state["未払金"] = sga_of(month) * 3.0                       # 経費のおよそ3か月ぶん
        state["短期借入金"] -= REPAY
        nonop = sum(NONOP_DRIFT.get(month, {}).get(k, 1.0) * v * 1000
                    for k, v in NONOP.items() if k in ("受取利息", "仕入割引"))
        outgo = sum(NONOP_DRIFT.get(month, {}).get(k, 1.0) * v * 1000
                    for k, v in NONOP.items() if k in ("支払利息", "為替差損"))
        state["現金及び預金"] += (collect - pay - labor_of(month) - sga_of(month)
                            + nonop - outgo - REPAY)

        if month not in ordered:
            continue
        lines = ["会計システム　月末残高一覧　%s年%d月度" % (month[:4], int(month[5:])),
                 "科目,残高(千円)"]
        for name in BALANCE:
            lines.append("%s,%d" % (name, round(state[name] / 1000)))
        path = OUT / ("C社会計_月末残高_%s.csv" % month.replace("-", ""))
        path.write_bytes((CRLF.join(lines) + CRLF).encode("cp932"))
        written += 1
    return written


def write_purchase(path, rows, issued):
    """仕入日報。売上日報と同じ形（同じ販売管理システムから出るので）。

    仕入は売上に先行して動く ── 明日売る物を今日買う。だから1営業日ずらしてある。
    """
    lines = ["■仕入日報　出力日:%s" % issued.strftime("%Y/%m/%d"),
             "部門コード,部門名,計上日,仕入金額（税抜）,伝票枚数"]
    total = 0
    for code, name, day, sales, _slips in sorted(rows, key=lambda r: (r[2], r[0])):
        buy = buy_of(code, day, sales)
        total += buy
        lines.append("%s,%s,%s,%d,%d" % (code, name, day.strftime("%Y/%m/%d"), buy, max(1, buy // 900_000)))
    lines.append(",合計,,%d," % total)
    path.write_bytes((CRLF.join(lines) + CRLF).encode("cp932"))
    return len(rows)


def main():
    OUT.mkdir(exist_ok=True)
    shift = datetime.timedelta(days=YOY_OFFSET)
    current = [(d, d) for d in business_days(START, END)]
    # 前年は当月末までぶんを用意する（今年の残り営業日の見込みを立てる材料になる）
    prior = [(d, d - shift) for d in business_days(START, MONTH_END)]

    for current_year, pairs in ((True, current), (False, prior)):
        suffix = "2026" if current_year else "2025"
        issued = END + datetime.timedelta(days=1 if current_year else 1 - YOY_OFFSET)
        sales_rows, hours_rows = build(pairs, current_year)
        n1 = write_sales(OUT / ("A社販売管理_売上日報_%s.csv" % suffix), sales_rows, issued)
        n2 = write_hours(OUT / ("B社勤怠_勤務実績_%s.csv" % suffix), hours_rows)
        n3 = write_accounting(sales_rows, hours_rows, current_year)
        n4 = write_purchase(OUT / ("A社販売管理_仕入日報_%s.csv" % suffix), sales_rows, issued)
        months = sorted({d.strftime("%Y-%m") for _c, _n, d, _s, _x in sales_rows}
                        - {UNSETTLED}
                        - {m for m in {d.strftime("%Y-%m") for _c, _n, d, _s, _x in sales_rows}
                           if len({d for _c, _n, d, _s, _x in sales_rows if d.strftime("%Y-%m") == m}) < 15})
        n5 = write_trial(months)
        n6 = write_balance(months, sales_rows, hours_rows)
        print("%s: 売上 %d行 ／ 勤怠 %d行 ／ 部門損益 %dヶ月 ／ 仕入 %d行 ／ 試算表 %dヶ月 ／ 月末残高 %dヶ月"
              % (suffix, n1, n2, n3, n4, n5, n6))

    print("当年 %d営業日（%s 〜 %s）／ 前年は当月末ぶんまで %d営業日" 
          % (len(current), current[0][1], current[-1][1], len(prior)))


if __name__ == "__main__":
    main()
