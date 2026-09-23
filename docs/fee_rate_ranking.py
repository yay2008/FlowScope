# -*- coding: utf-8 -*-
"""期货手续费率换算与排序 (投资者手续费率查询 -> 单手成本排名).

费率表把两种口径混在一起, 直接比数字会得出错误结论:

- **按金额**: 成交额的万分之几 (如股指 2.306e-05)
- **按手数**: 元/手 (如国债 3.01)

股指“按手数”只有 0.01 元, 看着最便宜, 真正的大头在按金额那一列。所以本脚本
用 TqSdk 主连的合约乘数与最新价把两种口径统一成“单手成本(元)”:

    单手成本 = 按金额费率 x (乘数 x 最新价) + 按手数

费率表还自带了投保基金(亿分之六), 已含在“按金额”列里, 不需要另外加。于是可以
按“按金额”是否大于 6e-8 判断该品种是比例收费还是固定收费。

输出两份 CSV(默认写到本目录):

- ``手续费最低品种排名_<交易日>.csv``   按“隔夜往返 = 开仓 + 平昨”升序
- ``手续费排名_平今免费_<交易日>.csv``  假设平今免费, 按“开仓单边”升序

注意: 这张表里 82/82 个品种的“平仓”与“开仓”完全对称, 隔夜往返恒等于 2x开仓,
所以“平今免费”只改变成本数值、不改变排序顺序。

用法::

    python docs/fee_rate_ranking.py <投资者手续费率查询.xlsx> [输出目录]

凭据取自 .env 的 ``TQ_USER``/``TQ_PASS``(与 ``ingest.py`` 一致), 文件不存在时
回退到同名环境变量。
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import re

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

EX_CN2CODE = {
    "中金所": "CFFEX", "郑商所": "CZCE", "大商所": "DCE",
    "广期所": "GFEX", "能源中心": "INE", "上期所": "SHFE",
}
FUND_RATE = 6e-08    # 投保基金: 亿分之六, 每个品种的“按金额”列都含这一项
RATED_EPS = 1.5e-07  # 按金额超过该值即视为比例收费品种
FREE_TODAY = 0.02    # 平今成本低于该值(只剩投保基金)视为“今平免费”
SHEET = "Sheet1"
FIRST_ROW, LAST_ROW = 4, 242   # 表头在第 3 行, 第 243 行是“共计”合计行

# 报告用列: (表头, 取值键, 单元格格式)
PRICE_COLS = [("名义金额", "名义金额", "%11.0f"), ("开仓", "开仓元", "%9.3f"),
              ("隔夜往返", "隔夜往返元", "%9.3f"), ("日内往返", "日内往返元", "%9.3f")]
FREE_COLS = [("名义金额", "名义金额", "%11.0f"), ("开仓", "开仓元", "%9.3f"),
             ("今平实际", "平今元", "%9.3f"), ("今平免费", "平今免费", "%9s")]


def num(x):
    """Excel 单元格 -> float; 空值与 NaN 一律归 0。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if v != v else v


def base_code(code):
    """月份合约归一到品种: rb2609 -> rb, AP611 -> AP; 月均价合约 l_f 原样保留。"""
    return re.sub(r"\d+$", "", str(code).strip())


def fetch_specs():
    """主连 -> {(交易所, 品种): {乘数, 最新价, 名称}}。"""
    from tqsdk import TqApi, TqAuth

    user, password = os.getenv("TQ_USER"), os.getenv("TQ_PASS")
    if not user or not password:
        raise RuntimeError("未设置 TQ_USER/TQ_PASS")

    logging.getLogger("tqsdk").setLevel(logging.WARNING)
    api = TqApi(auth=TqAuth(user, password))
    try:
        syms = api.query_quotes(ins_class="CONT", expired=False)
        quotes = api.get_quote_list(syms)
        api.wait_update()
        api.wait_update()
        specs = {}
        for sym, q in zip(syms, quotes):
            exchange, product = sym.split("@", 1)[1].split(".", 1)
            specs[(exchange.upper(), product.lower())] = {
                "multiple": num(q.volume_multiple),
                "price": num(q.last_price),
                "name": q.instrument_name,
            }
        return specs
    finally:
        api.close()


def load_schedule(path):
    """读费率表 -> 每个品种一行。

    同一品种可能有多行(投机/套保、模板/公司标准、品种行/月份合约行), 按
    “模板 > 公司标准”、“投机 > 套保”、“品种行 > 月份行”的优先级择一。
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[SHEET]
    picked, date = {}, "unknown"
    for row in ws.iter_rows(min_row=FIRST_ROW, max_row=LAST_ROW, values_only=True):
        if row[0] is None:
            continue
        raw_date = str(row[0]).strip()
        if date == "unknown" and re.fullmatch(r"\d{8}", raw_date):
            date = "%s-%s-%s" % (raw_date[:4], raw_date[4:6], raw_date[6:])

        code = str(row[2]).strip()
        rec = {
            "ex_cn": str(row[1]).strip(), "code": code, "base": base_code(code),
            "name": str(row[3]).strip(), "scope": str(row[4]).strip(),
            "flag": str(row[9]).strip(),
            "oa": num(row[10]), "ol": num(row[11]),   # 开仓   按金额 / 按手数
            "ca": num(row[12]), "cl": num(row[13]),   # 平昨(平仓)
            "ta": num(row[14]), "tl": num(row[15]),   # 平今
        }
        rank = (rec["scope"] == "模板", rec["flag"] == "投机", code == rec["base"])
        cur = picked.get(rec["base"].lower())
        if cur is None or rank > cur[0]:
            picked[rec["base"].lower()] = (rank, rec)
    return [rec for _, rec in picked.values()], date


def build(records, specs):
    """费率 x 合约规格 -> 每个品种的单手成本(元)。"""
    rows, missing = [], []
    for rec in records:
        spec = specs.get((EX_CN2CODE.get(rec["ex_cn"], "").upper(), rec["base"].lower()))
        if not spec or not spec["multiple"] or not spec["price"]:
            missing.append(rec)
            continue
        notional = spec["multiple"] * spec["price"]
        opened = rec["oa"] * notional + rec["ol"]
        closed = rec["ca"] * notional + rec["cl"]
        today = rec["ta"] * notional + rec["tl"]
        rows.append({
            "交易所": rec["ex_cn"], "产品代码": rec["code"], "产品名称": rec["name"],
            "范围": rec["scope"], "投机套保": rec["flag"], "乘数": spec["multiple"],
            "价格": spec["price"], "名义金额": notional,
            "开仓元": opened, "平昨元": closed, "平今元": today,
            "隔夜往返元": opened + closed, "日内往返元": opened + today,
            "平今免费": "是" if today <= FREE_TODAY else "否",
            "收费方式": "比例" if rec["oa"] > RATED_EPS else "固定",
        })
    return rows, missing


def write_csv(path, rows, cols):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _head_spec(spec):
    """数值格式 (%11.0f) -> 同宽字符串格式 (%11s), 让表头复用同一套列宽。"""
    return re.sub(r"%(\d+)(?:\.\d+)?[fds]", r"%\1s", spec)


def table(rows, title, note, cols, n=10):
    """cols: (表头, 取值键, 单元格格式) 三元组列表。"""
    fmt = "%-3s %-6s %-7s %-16s " + " ".join(c[2] for c in cols)
    head = "%-3s %-6s %-7s %-16s " + " ".join(_head_spec(c[2]) for c in cols)
    lines = ["", "### %s  (%s)" % (title, note),
             head % (("#", "交易所", "代码", "名称") + tuple(c[0] for c in cols)),
             "-" * 88]
    for i, r in enumerate(rows[:n], 1):
        lines.append(fmt % ((i, r["交易所"], r["产品代码"], r["产品名称"])
                            + tuple(r[c[1]] for c in cols)))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="期货手续费率换算与排序")
    ap.add_argument("xlsx", help="投资者手续费率查询 xlsx")
    ap.add_argument("outdir", nargs="?", default=os.path.dirname(os.path.abspath(__file__)),
                    help="CSV 输出目录, 默认本目录")
    args = ap.parse_args()

    records, date = load_schedule(args.xlsx)
    specs = fetch_specs()
    rows, missing = build(records, specs)

    overnight = sorted(rows, key=lambda r: r["隔夜往返元"])
    dayfree = sorted(rows, key=lambda r: r["开仓元"])
    asym = [r for r in rows if abs(r["开仓元"] - r["平昨元"]) > 1e-9]
    free_today = [r for r in dayfree if r["平今免费"] == "是"]

    a = os.path.join(args.outdir, "手续费最低品种排名_%s.csv" % date)
    b = os.path.join(args.outdir, "手续费排名_平今免费_%s.csv" % date)
    write_csv(a, overnight, ["交易所", "产品代码", "产品名称", "范围", "投机套保", "乘数",
                             "价格", "名义金额", "开仓元", "平昨元", "平今元",
                             "隔夜往返元", "日内往返元", "收费方式"])
    write_csv(b, dayfree, ["交易所", "产品代码", "产品名称", "乘数", "价格", "名义金额",
                           "开仓元", "平昨元", "平今元", "隔夜往返元", "日内往返元",
                           "平今免费", "收费方式"])

    print("=" * 78)
    print("期货手续费单手成本排名 —— 费率日期 %s, 价格为 TqSdk 主连实时价" % date)
    print("=" * 78)
    print("口径: 单手成本 = 按金额费率 x (乘数 x 最新价) + 按手数")
    print("匹配 %d 个品种, 无主连/退市 %d 个: %s"
          % (len(rows), len(missing), ", ".join(r["code"] for r in missing) or "无"))
    print("开仓与平昨不对称的品种: %s" % (asym or "无(隔夜往返恒等于 2x开仓)"))

    print(table(overnight, "手续费最低 10 个品种", "按隔夜往返 = 开仓 + 平昨", PRICE_COLS))
    print(table(dayfree, "平今免费假设下的排序", "按开仓单边, 顺序与上表一致", FREE_COLS))
    print("\n现实中今平本来就免费/近乎免费的品种 %d 个: %s"
          % (len(free_today), ", ".join(r["产品代码"] for r in free_today)))
    print("\n已写出:\n  %s\n  %s" % (a, b))


if __name__ == "__main__":
    main()
