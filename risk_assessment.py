#!/usr/bin/env python3
"""持仓风险评估工具 - 读取 AssetTracker 导出的 JSON，输出完整风险报告"""

import json
import sys
import time
from pathlib import Path
from datetime import datetime, date

try:
    import yfinance as yf
except ImportError:
    print("请先安装依赖: pip install yfinance")
    sys.exit(1)

# ── 配置 ─────────────────────────────────────────────────────────────────────
BACKUP_DIR  = Path(
    "/Users/zhang/Library/Mobile Documents/com~apple~CloudDocs"
    "/Downloads/asset tracker/数据备份"
)
CACHE_FILE  = Path(__file__).parent / ".market_cache.json"
CONFIG_FILE = Path(__file__).parent / ".config.json"
CACHE_TTL   = 2 * 3600  # 2小时，单位秒

UP_PCTS   = [5, 10, 15, 20, 25, 35, 40, 50]
DOWN_PCTS = [5, 10, 15, 20, 25, 30, 40, 50]

# ── 数据加载 ──────────────────────────────────────────────────────────────────
def find_latest_json(backup_dir: Path) -> Path:
    jsons = sorted(backup_dir.glob("export_*.json"), reverse=True)
    if not jsons:
        raise FileNotFoundError(f"在 {backup_dir} 找不到导出文件")
    return jsons[0]

def load_data(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["data"]

# ── 汇率 + 市场数据（带2小时缓存）────────────────────────────────────────────
def _fetch_live() -> dict:
    fx  = {"HKD": 0.924, "USD": 7.25}
    mkt = {"pe": None, "forward_pe": None, "from_high_pct": None}
    try:
        hkd = yf.Ticker("HKDCNY=X").fast_info["last_price"]
        usd = yf.Ticker("USDCNY=X").fast_info["last_price"]
        if hkd: fx["HKD"] = float(hkd)
        if usd: fx["USD"]  = float(usd)
    except Exception:
        pass
    try:
        info = yf.Ticker("QQQ").info
        pe   = info.get("trailingPE")
        fpe  = info.get("forwardPE")
        h52  = info.get("fiftyTwoWeekHigh")
        cur  = info.get("currentPrice") or info.get("regularMarketPrice")
        if pe:  mkt["pe"]         = float(pe)
        if fpe: mkt["forward_pe"] = float(fpe)
        if h52 and cur:
            mkt["from_high_pct"] = (float(cur) / float(h52) - 1) * 100
    except Exception:
        pass
    return {"fx": fx, "mkt": mkt, "ts": time.time()}

def get_market_info() -> tuple[dict, dict, bool]:
    """返回 (fx, mkt, from_cache)。缓存有效期 CACHE_TTL 秒。"""
    if CACHE_FILE.exists():
        try:
            cached = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if time.time() - cached["ts"] < CACHE_TTL:
                return cached["fx"], cached["mkt"], True
        except Exception:
            pass
    data = _fetch_live()
    try:
        CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return data["fx"], data["mkt"], False

# ── 手动 Forward PE（持久存储，不随市场缓存过期）────────────────────────────
def load_forward_pe() -> float | None:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return cfg.get("forward_pe")
    except Exception:
        return None

def save_forward_pe(value: float) -> None:
    _save_config_key("forward_pe", value)

def load_manual_return() -> float | None:
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return cfg.get("manual_total_return")
    except Exception:
        return None

def save_manual_return(value: float) -> None:
    _save_config_key("manual_total_return", value)

def _save_config_key(key: str, value) -> None:
    try:
        cfg = {}
        if CONFIG_FILE.exists():
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        cfg[key] = value
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

# ── 贷款余额计算 ─────────────────────────────────────────────────────────────
def calc_remaining_principal(loan: dict, today: date) -> float:
    """根据 lastAppliedMonth 动态算出当前实际剩余本金。

    JSON 里的 principal 是 lastAppliedMonth 当月的期初余额（还款前），
    对于每个已过还款日的月份累计摊还计算。
    """
    principal    = loan["principal"]
    annual_rate  = loan.get("annualRate")
    monthly_pmt  = loan.get("monthlyPayment")
    last_applied = loan.get("lastAppliedMonth")   # e.g. 202604
    payment_day  = loan.get("monthlyReductionDay") or 1
    mode         = loan.get("repaymentMode", "")

    # 只对等额还款且有利率信息的贷款动态计算
    if mode != "equalPayment" or not all([annual_rate, monthly_pmt, last_applied]):
        return principal

    monthly_rate = annual_rate / 100 / 12
    y = last_applied // 100
    m = last_applied % 100

    while True:
        # 判断 (y, m) 这个月的还款日是否已过
        if y < today.year or (y == today.year and m < today.month):
            passed = True                         # 完整过去的月份，肯定已还
        elif y == today.year and m == today.month:
            passed = today.day >= payment_day     # 当月：看今天是否 >= 还款日
        else:
            break                                 # 未来月份，停止

        if passed:
            interest   = principal * monthly_rate
            principal -= (monthly_pmt - interest)
            if principal <= 0:
                return 0.0

        # 移到下一个月
        m += 1
        if m > 12:
            m = 1
            y += 1

        if not passed:
            break

    return max(0.0, principal)

# ── 持仓计算 ──────────────────────────────────────────────────────────────────
def calc_portfolio(data: dict, fx: dict) -> dict:
    type_beta = {t["typeKey"]: t["betaMultiplier"] for t in data.get("assetTypes", [])}

    total_cny = cash_cny = nasdaq_exp = 0.0
    location_value: dict[str, float] = {}
    # location → [(addedAt, value_cny), ...] 用于校准后过滤新增资产
    location_assets: dict[str, list[tuple[int, float]]] = {}
    # 聚合后的持仓明细（用于调仓模拟）。key = (location, symbol, market, beta)
    stock_agg: dict[tuple, dict] = {}
    # 每个账户的总资产（含现金）
    location_total: dict[str, float] = {}

    for a in data["assets"]:
        raw   = a["quantity"] * a["currentPrice"]
        mkt   = a["market"]
        mult  = fx["HKD"] if mkt == "hk" else fx["USD"] if mkt in ("us", "crypto") else 1.0
        value = raw * mult
        total_cny += value

        loc = a.get("location", "") or "未指定账户"
        location_value[loc] = location_value.get(loc, 0.0) + value
        location_assets.setdefault(loc, []).append((a.get("addedAt") or 0, value))
        location_total[loc] = location_total.get(loc, 0.0) + value

        atype = a["assetType"]
        if atype == "cash":
            cash_cny += value
            continue

        beta = a.get("betaMultiplier")
        if beta is None:
            beta = type_beta.get(atype, 0.0)
        if beta is None:
            beta = 0.0
        beta = float(beta)
        nasdaq_exp += value * beta

        sym = (a.get("symbol") or "").strip()
        key = (loc, sym, mkt, beta)
        if key not in stock_agg:
            stock_agg[key] = {
                "location": loc,
                "name":   a.get("name") or sym or "?",
                "symbol": sym,
                "market": mkt,
                "beta":   beta,
                "value":  0.0,
            }
        stock_agg[key]["value"] += value

    # 按账户总资产降序、账户内按持仓降序
    assets_detail = sorted(
        stock_agg.values(),
        key=lambda x: (-location_total.get(x["location"], 0), x["location"], -x["value"]),
    )

    total_debt = 0.0
    loan_details = []
    for loan in data.get("investmentLoans", []):
        cur  = loan.get("currency", "cny").lower()
        mult = fx["USD"] if cur == "usd" else fx["HKD"] if cur == "hkd" else 1.0
        remaining = calc_remaining_principal(loan, date.today()) * mult
        total_debt += remaining
        pmt = loan.get("monthlyPayment") or loan.get("monthlyPrincipalReduction") or 0.0
        loan_details.append({
            "name":     loan.get("name", "贷款"),
            "remaining": remaining,
            "monthly":   pmt * mult,
        })

    total_monthly = sum(l["monthly"] for l in loan_details)
    net_equity    = total_cny - total_debt
    leverage      = nasdaq_exp / net_equity if net_equity > 0 else float("inf")

    # 实时总盈亏（口径同 Nq100 dashboard.historicalTotalInvestmentReturnCny）
    # 每个有股票资产的账户：
    #   account_return = baseline_return_cny + (last_snap - first_snap + 转账调整)
    # 转账调整 = 转出金额 - 转入金额（按当前汇率换算）
    baselines       = data.get("accountReturnBaselines", [])
    all_snaps       = data.get("accountSnapshots", [])
    all_flows       = data.get("cashFlows", [])
    base_by_loc     = {b["location"]: b for b in baselines}

    # 列出"持有股票的账户"（symbol 非空）
    stock_locs = sorted({
        a["location"].strip() for a in data["assets"]
        if a.get("symbol", "").strip() and a.get("location", "").strip()
    })

    def fx_mult(cur: str) -> float:
        c = (cur or "cny").lower()
        return fx["USD"] if c == "usd" else fx["HKD"] if c == "hkd" else 1.0

    def app_recorded_for(loc: str) -> float:
        rows = sorted(
            [s for s in all_snaps if s.get("location") == loc],
            key=lambda s: s["timestamp"],
        )
        if len(rows) < 2:
            return 0.0
        start_ts = rows[0]["timestamp"]
        end_ts   = rows[-1]["timestamp"] + 1
        adj = 0.0
        for c in all_flows:
            if c.get("type") != "transfer":
                continue
            ts = c.get("timestamp", 0)
            if ts < start_ts or ts >= end_ts:
                continue
            amt_cny = abs(c.get("amount", 0)) * fx_mult(c.get("currency", "cny"))
            if c.get("location") == loc:
                adj += amt_cny
            if c.get("to_location") == loc:
                adj -= amt_cny
        return rows[-1]["value_cny"] - rows[0]["value_cny"] + adj

    return_details = []
    total_return   = 0.0
    last_cal_ts    = 0
    for loc in stock_locs:
        b           = base_by_loc.get(loc)
        baseline    = float(b.get("baseline_return_cny") or 0.0) if b else 0.0
        recorded    = app_recorded_for(loc)
        amount      = baseline + recorded
        return_details.append({
            "location": loc, "baseline": baseline,
            "recorded": recorded, "amount": amount,
        })
        total_return += amount
        if b and b.get("calibration_timestamp", 0) > last_cal_ts:
            last_cal_ts = b["calibration_timestamp"]

    return {
        "total_cny":     total_cny,
        "total_debt":    total_debt,
        "net_equity":    net_equity,
        "nasdaq_exp":    nasdaq_exp,
        "leverage":      leverage,
        "cash_cny":      cash_cny,
        "loan_details":  loan_details,
        "total_monthly": total_monthly,
        "months_of_cash": cash_cny / total_monthly if total_monthly > 0 else float("inf"),
        "return_details":  return_details,
        "total_return":    total_return,
        "last_cal_ts":     last_cal_ts,
        "assets_detail":   assets_detail,
        "location_total":  location_total,
    }

# ── 风险评分 ──────────────────────────────────────────────────────────────────
RISK_LABELS = ["🟢 低", "🟡 中", "🟠 高", "🔴 极高"]

def overall_risk(p: dict, m: dict) -> tuple[int, list[str]]:
    score, reasons = 0, []

    lev = p["leverage"]
    if lev >= 2.0:
        score += 2
        reasons.append(f"杠杆倍数 {lev:.2f}x — 极高风险（建议 <1.5x）")
    elif lev >= 1.5:
        score += 1
        reasons.append(f"杠杆倍数 {lev:.2f}x — 中等风险（建议 <1.5x）")
    else:
        reasons.append(f"杠杆倍数 {lev:.2f}x — 在安全范围内")

    pe = m.get("pe")
    if pe:
        if pe >= 40:
            score += 1
            reasons.append(f"纳指 PE {pe:.1f} — 历史高位（历史均值约 25，>40 为高）")
        elif pe >= 32:
            reasons.append(f"纳指 PE {pe:.1f} — 偏高（历史均值约 25）")
        else:
            reasons.append(f"纳指 PE {pe:.1f} — 估值合理")

    fh = m.get("from_high_pct")
    if fh is not None:
        if fh > -5:
            score += 1
            reasons.append(f"距52周高点 {fh:.1f}% — 接近历史高位")
        elif fh > -15:
            reasons.append(f"距52周高点 {fh:.1f}% — 略有回落")
        else:
            reasons.append(f"距52周高点 {fh:.1f}% — 已有较大回调")

    return min(score, 3), reasons

# ── 格式化 ────────────────────────────────────────────────────────────────────
def w(v: float) -> str:
    return f"{v / 10000:.1f}万"

def fmt_pct(v: float) -> str:
    return f"{'+' if v >= 0 else ''}{v:.1f}%"

def fmt_pct_w(v: float) -> str:
    return f"{'+' if v >= 0 else ''}{v / 10000:.1f}万"

# ── 模块3：涨跌场景 ──────────────────────────────────────────────────────────
def print_stress_test(p: dict) -> None:
    eq, exp = p["net_equity"], p["nasdaq_exp"]
    base_ret = p["total_return"]
    print("\n【涨跌场景】")
    print(f"  {'':6}  {'净资产':>7}  {'净资产变化%':>9}  {'实时总盈亏':>10}")
    for up in UP_PCTS:
        new_eq = eq + exp * up / 100
        chg    = (new_eq / eq - 1) * 100
        total  = base_ret + (new_eq - eq)
        print(f"  涨{up:>2}%  {w(new_eq):>7}  {fmt_pct(chg):>9}  {fmt_pct_w(total):>10}")
    print(f"  ── 当前 ──  净资产 {w(eq)}  实时总盈亏 {fmt_pct_w(base_ret)}")
    for dn in DOWN_PCTS:
        new_eq = eq - exp * dn / 100
        chg    = (new_eq / eq - 1) * 100
        total  = base_ret + (new_eq - eq)
        print(f"  跌{dn:>2}%  {w(new_eq):>7}  {fmt_pct(chg):>9}  {fmt_pct_w(total):>10}")

# ── 模块5：仓位模拟 ──────────────────────────────────────────────────────────
def print_position_sim(p: dict, amounts_wan: list[float]) -> None:
    eq       = p["net_equity"]
    exp      = p["nasdaq_exp"]
    base_ret = p["total_return"]

    # 当前 列在最左，其余按调仓金额排序（负→正）
    sorted_amts = sorted(amounts_wan)
    all_amts    = [0.0] + sorted_amts  # 0 = 当前

    cw = 16  # 每列宽度（含 净资产+盈亏）

    def col_label(a: float) -> str:
        if a == 0.0:
            return "当前"
        return f"{'减仓' if a < 0 else '加仓'}{abs(a):.0f}万"

    def row(label: str, values: list[str]) -> None:
        print(f"  {label:<14}" + "".join(f"{v:>{cw}}" for v in values))

    def fmt_cell(new_eq: float) -> str:
        total = base_ret + (new_eq - eq)
        return f"{w(new_eq)} {fmt_pct_w(total)}"

    # 表头
    row("", [col_label(a) for a in all_amts])
    print("  " + "─" * (14 + cw * len(all_amts)))

    # 有效纳指暴露
    row("有效纳指暴露", [w(exp + a * 10000) for a in all_amts])

    # 杠杆倍数（减/加仓视为换仓不动净资产）
    row("杠杆倍数", [f"{(exp + a * 10000) / eq:.2f}x" for a in all_amts])

    # 跌幅行
    for dn in DOWN_PCTS:
        vals = [fmt_cell(eq + (exp + a * 10000) * (-dn / 100)) for a in all_amts]
        row(f"跌{dn:>2}%净资产", vals)

    # 涨幅行
    for up in UP_PCTS:
        vals = [fmt_cell(eq + (exp + a * 10000) * (up / 100)) for a in all_amts]
        row(f"涨{up:>2}%净资产", vals)

# ── HTML 报告生成 ─────────────────────────────────────────────────────────────
HTML_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Helvetica Neue',sans-serif;
     max-width:1100px;margin:24px auto;padding:0 16px;color:#1d1d1f;background:#f5f5f7;}
h1{font-size:22px;margin:0 0 6px}
h2{font-size:16px;margin:28px 0 10px;padding-left:8px;border-left:4px solid #0071e3}
.meta{color:#86868b;font-size:13px;margin-bottom:18px}
.card{background:#fff;border-radius:12px;padding:16px 20px;margin-bottom:16px;
      box-shadow:0 1px 3px rgba(0,0,0,.06)}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 10px;text-align:right;border-bottom:1px solid #f0f0f3}
th{background:#fafafc;font-weight:500;color:#515154}
td:first-child,th:first-child{text-align:left}
tr.current td{background:#fffceb;font-weight:600}
tr.alert td{background:#fff4f4;font-weight:600}
.pos{color:#0a8a3a}
.neg{color:#d70015}
.risk{display:inline-block;padding:3px 10px;border-radius:8px;font-weight:600;font-size:13px}
.r0{background:#d4edda;color:#0a8a3a}
.r1{background:#fff3cd;color:#806100}
.r2{background:#fee3c0;color:#a04500}
.r3{background:#f8d7da;color:#d70015}
.kv{display:grid;grid-template-columns:auto 1fr;column-gap:24px;row-gap:6px;font-size:13.5px}
.kv span:nth-child(odd){color:#86868b}
.kv span:nth-child(even){font-weight:500}
.subtle{color:#86868b;font-size:11px}
.tabs{display:flex;gap:4px;margin:6px 0 18px;border-bottom:1px solid #d2d2d7}
.tab-btn{padding:10px 18px;font-size:14px;background:none;border:none;cursor:pointer;
        color:#515154;border-bottom:2px solid transparent;margin-bottom:-1px;font-weight:500}
.tab-btn.active{color:#0071e3;border-bottom-color:#0071e3}
.tab-pane{display:none}
.tab-pane.active{display:block}
"""

def _cls(v: float) -> str:
    return "pos" if v > 0 else "neg" if v < 0 else ""

def _td_num(v: float, suffix: str = "万", sign: bool = True) -> str:
    s = ("+" if sign and v > 0 else "") + f"{v/10000:.1f}{suffix}"
    return f'<td class="{_cls(v)}">{s}</td>'

def generate_html(p: dict, fx: dict, mkt: dict, forward_pe, score: int,
                  reasons: list, sim_amounts: list[float]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = [f"<!DOCTYPE html><html><head><meta charset='utf-8'>"
           f"<title>持仓风险评估报告 {now}</title><style>{HTML_CSS}</style></head><body>"]
    out.append(f"<h1>持仓风险评估报告</h1>")
    out.append(f"<div class='meta'>{now}　·　USD {fx['USD']:.2f}　HKD {fx['HKD']:.4f}</div>")
    out.append("<div class='tabs'>"
               "<button class='tab-btn active' data-tab='1'>风险报告</button>"
               "<button class='tab-btn' data-tab='2'>调仓模拟</button>"
               "</div>")
    out.append("<div id='pane-1' class='tab-pane active'>")

    # 持仓快照
    out.append("<div class='card'><h2 style='margin-top:0'>持仓快照</h2><div class='kv'>")
    pairs = [
        ("总资产", f"{p['total_cny']/10000:.1f} 万"),
        ("总负债", f"{p['total_debt']/10000:.1f} 万"),
        ("净资产", f"{p['net_equity']/10000:.1f} 万"),
        ("有效纳指暴露", f"{p['nasdaq_exp']/10000:.1f} 万"),
        ("杠杆倍数", f"{p['leverage']:.2f}x"),
        ("现金储备", f"{p['cash_cny']/10000:.1f} 万"),
    ]
    for k, v in pairs:
        out.append(f"<span>{k}</span><span>{v}</span>")
    out.append("</div></div>")

    # 实时总盈亏
    if p["return_details"]:
        out.append("<div class='card'><h2 style='margin-top:0'>实时总盈亏</h2>")
        out.append("<table><tr><th>账户</th><th>合计</th><th>基准</th><th>App 已记录</th></tr>")
        for r in sorted(p["return_details"], key=lambda x: -x["amount"]):
            out.append(f"<tr><td>{r['location']}</td>")
            out.append(_td_num(r['amount']))
            out.append(_td_num(r['baseline']))
            out.append(_td_num(r['recorded']))
            out.append("</tr>")
        out.append(f"<tr class='current'><td>合计</td>{_td_num(p['total_return'])}<td colspan='2'></td></tr>")
        out.append("</table>")
        if p["last_cal_ts"]:
            cal_str = datetime.fromtimestamp(p["last_cal_ts"]/1000).strftime("%Y-%m-%d")
            out.append(f"<div class='subtle' style='margin-top:6px'>最近校准: {cal_str}</div>")
        out.append("</div>")

    # 市场估值 + 风险等级
    pe_s  = f"{mkt['pe']:.1f}"      if mkt["pe"]      else "—"
    fpe_s = f"{forward_pe:.1f}"     if forward_pe     else "—"
    fh_s  = f"{mkt['from_high_pct']:.1f}%" if mkt.get('from_high_pct') is not None else "—"
    labels = ["低", "中", "高", "极高"]
    out.append("<div class='card'><h2 style='margin-top:0'>市场估值 & 风险等级</h2>")
    out.append(f"<div class='kv'>"
               f"<span>市盈率 TTM</span><span>{pe_s}</span>"
               f"<span>预期市盈率</span><span>{fpe_s}</span>"
               f"<span>距52周高点</span><span>{fh_s}</span></div>")
    out.append(f"<div style='margin-top:14px'>综合风险等级 "
               f"<span class='risk r{score}'>{labels[score]}</span></div>")
    out.append("<ul style='margin:8px 0 0;padding-left:20px;color:#515154;font-size:13px'>")
    for r in reasons:
        out.append(f"<li>{r}</li>")
    out.append("</ul></div>")

    # 涨跌场景
    eq, exp, base_ret = p["net_equity"], p["nasdaq_exp"], p["total_return"]
    out.append("<div class='card'><h2 style='margin-top:0'>涨跌场景</h2>")
    out.append("<table><tr><th>场景</th><th>净资产</th><th>净资产变化%</th><th>实时总盈亏</th></tr>")
    for up in UP_PCTS:
        new_eq = eq + exp * up / 100
        chg    = (new_eq / eq - 1) * 100
        total  = base_ret + (new_eq - eq)
        out.append(f"<tr><td>涨{up}%</td><td>{new_eq/10000:.1f}万</td>"
                   f"<td class='pos'>+{chg:.1f}%</td>{_td_num(total)}</tr>")
    out.append(f"<tr class='current'><td>当前</td><td>{eq/10000:.1f}万</td>"
               f"<td>—</td>{_td_num(base_ret)}</tr>")
    for dn in DOWN_PCTS:
        new_eq = eq - exp * dn / 100
        chg    = (new_eq / eq - 1) * 100
        total  = base_ret + (new_eq - eq)
        cls    = "alert" if dn in (20, 30) else ""
        out.append(f"<tr class='{cls}'><td>跌{dn}%</td><td>{new_eq/10000:.1f}万</td>"
                   f"<td class='neg'>{chg:.1f}%</td>{_td_num(total)}</tr>")
    out.append("</table></div>")

    # 仓位模拟（拖动滑块）
    out.append("""
<div class='card'>
  <h2 style='margin-top:0'>仓位模拟</h2>
  <div style='display:flex;align-items:center;gap:16px;margin-bottom:14px;flex-wrap:wrap'>
    <div style='font-size:13px;color:#515154;min-width:120px'>调仓金额<br><span class='subtle'>非实际仓位<br>= β 加权后的有效纳指暴露</span></div>
    <input id='simSlider' type='range' min='-50' max='50' step='0.5' value='10'
           style='flex:1;min-width:280px;accent-color:#0071e3'/>
    <div id='simLabel' style='min-width:110px;font-size:15px;font-weight:600;
         color:#0071e3;font-variant-numeric:tabular-nums'></div>
  </div>
  <div style='display:flex;gap:8px;font-size:12px;color:#86868b;margin:-8px 0 14px'>
    <span>减仓50万</span><span style='flex:1'></span>
    <span>当前</span><span style='flex:1'></span>
    <span>加仓50万</span>
  </div>
  <div id='simTable'></div>
  <div class='subtle' style='margin-top:6px'>
    盈亏 = 当前实时盈亏 + 场景变化（暴露 × 涨跌%）
  </div>
</div>
""")

    # 月供压力（pane-1 最后一块）
    out.append("<div class='card'><h2 style='margin-top:0'>贷款月供压力</h2>")
    out.append("<table><tr><th>贷款</th><th>剩余本金</th><th>月供</th></tr>")
    for ld in p["loan_details"]:
        out.append(f"<tr><td>{ld['name']}</td>"
                   f"<td>{ld['remaining']/10000:.1f}万</td>"
                   f"<td>{ld['monthly']:,.0f} 元</td></tr>")
    out.append(f"<tr class='current'><td>合计</td>"
               f"<td>{p['total_debt']/10000:.1f}万</td>"
               f"<td>{p['total_monthly']:,.0f} 元</td></tr>")
    out.append("</table>")
    out.append(f"<div class='subtle' style='margin-top:8px'>"
               f"现金可撑 {p['months_of_cash']:.0f} 个月（约 {p['months_of_cash']/12:.1f} 年）</div></div>")

    # pane-1 结束，pane-2 开始：调仓模拟
    out.append("</div><div id='pane-2' class='tab-pane'>")
    out.append("""
<div class='card'>
  <h2 style='margin-top:0'>调仓模拟</h2>
  <div class='subtle' style='margin-bottom:10px'>
    每行输入调整金额（万元，正=加仓 / 负=减仓），从现金里扣减。
    可点击下方按钮新增标的。
  </div>
  <table id='rebalTable'>
    <thead><tr>
      <th>名称</th><th>市场</th><th>β</th>
      <th style='text-align:right'>当前 (万)</th>
      <th style='text-align:right'>调整 (万)</th>
      <th style='text-align:right'>调整后 (万)</th>
    </tr></thead>
    <tbody id='rebalBody'></tbody>
  </table>
  <button id='rebalAdd' style='margin-top:10px;padding:6px 14px;font-size:13px;
          border:1px solid #d2d2d7;border-radius:8px;background:#f5f5f7;cursor:pointer'>
    + 新增标的
  </button>
  <button id='rebalReset' style='margin-top:10px;margin-left:6px;padding:6px 14px;font-size:13px;
          border:1px solid #d2d2d7;border-radius:8px;background:#fff;cursor:pointer'>
    重置
  </button>
  <h3 style='margin:18px 0 8px;font-size:15px'>调仓后指标</h3>
  <div id='rebalKv' class='kv'></div>
  <h3 style='margin:18px 0 8px;font-size:15px'>调仓后压力测试</h3>
  <table id='rebalStress'></table>
</div>
</div>
""")

    # 嵌入数据 + JS
    data_js = json.dumps({
        "eq":       eq,
        "exp":      exp,
        "base_ret": base_ret,
        "up_pcts":  UP_PCTS,
        "down_pcts": DOWN_PCTS,
        "cash":     p["cash_cny"],
        "debt":     p["total_debt"],
        "total":    p["total_cny"],
        "assets":   p["assets_detail"],
        "loc_total": p["location_total"],
    }, ensure_ascii=False)
    out.append(f"<script>const D={data_js};\n")
    out.append(r"""
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    const t = btn.dataset.tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab===t));
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id==='pane-'+t));
  });
});
""")
    out.append(r"""
function fmtPL(v) {
  const sign = v >= 0 ? '+' : '';
  const cls  = v > 0 ? 'pos' : v < 0 ? 'neg' : '';
  return `<td class="${cls}">${sign}${(v/10000).toFixed(1)}万</td>`;
}
function fmtVal(v) {
  return `<td>${(v/10000).toFixed(1)}万</td>`;
}
function renderSim() {
  const a = parseFloat(document.getElementById('simSlider').value);
  const label = a === 0 ? '当前'
              : (a < 0 ? '减仓 ' : '加仓 ') + Math.abs(a).toFixed(1) + ' 万 (有效纳指暴露)';
  document.getElementById('simLabel').textContent = label;

  const newExp = D.exp + a*10000;
  const newLev = D.eq > 0 ? newExp / D.eq : 0;

  let html = `<div class='kv' style='margin-bottom:12px'>`
    + `<span>有效纳指暴露</span><span>${(newExp/10000).toFixed(1)}万`
    + (a!==0 ? ` <span class='subtle'>(当前 ${(D.exp/10000).toFixed(1)}万)</span>` : '')
    + `</span>`
    + `<span>杠杆倍数</span><span>${newLev.toFixed(2)}x`
    + (a!==0 ? ` <span class='subtle'>(当前 ${(D.exp/D.eq).toFixed(2)}x)</span>` : '')
    + `</span></div>`;

  function plCell(v) {
    const s = v>=0?'+':''; const cl = v>0?'pos':v<0?'neg':'';
    return `<td class='${cl}'>${s}${(v/10000).toFixed(1)}万</td>`;
  }
  html += '<table><tr><th>场景</th><th>净资产</th><th>净资产变化%</th><th>实时总盈亏</th></tr>';
  D.up_pcts.forEach(up => {
    const newEq = D.eq + newExp * up/100;
    const chg   = (newEq/D.eq - 1)*100;
    const pl    = D.base_ret + (newEq - D.eq);
    html += `<tr><td>涨${up}%</td><td>${(newEq/10000).toFixed(1)}万</td>`
         + `<td class='pos'>+${chg.toFixed(1)}%</td>${plCell(pl)}</tr>`;
  });
  const curPL = D.base_ret;
  html += `<tr class='current'><td>当前</td><td>${(D.eq/10000).toFixed(1)}万</td><td>—</td>${plCell(curPL)}</tr>`;
  D.down_pcts.forEach(dn => {
    const newEq = D.eq - newExp * dn/100;
    const chg   = (newEq/D.eq - 1)*100;
    const pl    = D.base_ret + (newEq - D.eq);
    const ac    = (dn===20||dn===30) ? 'alert' : '';
    html += `<tr class='${ac}'><td>跌${dn}%</td><td>${(newEq/10000).toFixed(1)}万</td>`
         + `<td class='neg'>${chg.toFixed(1)}%</td>${plCell(pl)}</tr>`;
  });
  html += '</table>';
  document.getElementById('simTable').innerHTML = html;
}
const slider = document.getElementById('simSlider');
slider.addEventListener('input', renderSim);
renderSim();

// ───── 调仓模拟 ─────
const MKT_LABEL = {cn:'A股', us:'美股', hk:'港股', crypto:'加密'};
// 深拷贝初始持仓，再加一个虚拟的新增数组
const rebalRows = D.assets.map(a => ({
  location:a.location, name:a.name, symbol:a.symbol, market:a.market,
  beta:a.beta, base:a.value, delta:0, isNew:false
}));
function fmtWan(v, sign){
  const s = (sign && v>0 ? '+' : '') + (v/10000).toFixed(2);
  return s;
}
function cls(v){ return v>0?'pos':v<0?'neg':''; }

function buildRebalTable(){
  const body = document.getElementById('rebalBody');
  body.innerHTML = '';

  // 按 location 分组（保持顺序：D.assets 已按账户总资产排序）
  const byLoc = new Map();
  rebalRows.forEach((r, i) => {
    if (!byLoc.has(r.location)) byLoc.set(r.location, []);
    byLoc.get(r.location).push(i);
  });

  byLoc.forEach((idxs, loc) => {
    const baseTotal = D.loc_total[loc] || 0;
    const head = document.createElement('tr');
    head.innerHTML = `
      <td colspan='3' style='background:#fafafc;font-weight:600;color:#1d1d1f;font-size:13px'>
        ${loc}
      </td>
      <td colspan='3' style='background:#fafafc;text-align:right;font-size:12px;color:#515154'>
        账户总资产 ${fmtWan(baseTotal,false)} 万
      </td>
    `;
    body.appendChild(head);

    idxs.forEach(i => {
      const r = rebalRows[i];
      const tr = document.createElement('tr');
      if (r.isNew) {
        tr.innerHTML = `
          <td style='padding-left:22px'><input data-i='${i}' data-f='name' value='${r.name}' style='width:90px'/></td>
          <td>
            <select data-i='${i}' data-f='market' style='font-size:13px'>
              <option value='cn' ${r.market==='cn'?'selected':''}>A股</option>
              <option value='us' ${r.market==='us'?'selected':''}>美股</option>
              <option value='hk' ${r.market==='hk'?'selected':''}>港股</option>
            </select>
          </td>
          <td><input data-i='${i}' data-f='beta' type='number' step='any' value='${r.beta}' style='width:55px'/></td>
          <td style='text-align:right'>—</td>
          <td style='text-align:right'>
            <input data-i='${i}' data-f='delta' type='number' step='any' value='${r.delta}' style='width:90px;text-align:right'/>
          </td>
          <td data-after='${i}' style='text-align:right'></td>
        `;
      } else {
        tr.innerHTML = `
          <td style='padding-left:22px'>${r.name}${r.symbol?` <span class='subtle'>${r.symbol}</span>`:''}</td>
          <td>${MKT_LABEL[r.market]||r.market}</td>
          <td>${r.beta.toFixed(2)}</td>
          <td style='text-align:right'>${fmtWan(r.base,false)}</td>
          <td style='text-align:right'>
            <input data-i='${i}' data-f='delta' type='number' step='any' value='${r.delta}' style='width:90px;text-align:right'/>
          </td>
          <td data-after='${i}' style='text-align:right'></td>
        `;
      }
      body.appendChild(tr);
    });
  });
  // 绑定输入：不重建表格，只增量更新
  body.querySelectorAll('input,select').forEach(el => {
    const evt = el.tagName === 'SELECT' ? 'change' : 'input';
    el.addEventListener(evt, e => {
      const i = +e.target.dataset.i, f = e.target.dataset.f;
      const v = e.target.value;
      if (f === 'delta' || f === 'beta') rebalRows[i][f] = parseFloat(v)||0;
      else rebalRows[i][f] = v;
      recomputeRebal();
    });
  });
  recomputeRebal();
}

function recomputeRebal(){
  // 更新每行"调整后"单元格
  rebalRows.forEach((r, i) => {
    const cell = document.querySelector(`[data-after='${i}']`);
    if (!cell) return;
    const newAfter = r.base + r.delta*10000;
    cell.className = newAfter < 0 ? 'neg' : '';
    cell.style.textAlign = 'right';
    cell.textContent = fmtWan(newAfter, false);
  });

  // 计算指标
  let newExp = 0, newCashDelta = 0, newStockValue = 0;
  rebalRows.forEach(r => {
    const v = r.base + r.delta*10000;
    newExp += v * r.beta;
    newCashDelta -= r.delta*10000;          // 加仓 → 现金减；减仓 → 现金加
    newStockValue += v;
  });
  const newCash  = D.cash + newCashDelta;
  const newTotal = newStockValue + newCash;
  const newEq    = newTotal - D.debt;
  const newLev   = newEq > 0 ? newExp / newEq : Infinity;
  const stockPct = newTotal > 0 ? (newStockValue / newTotal) * 100 : 0;

  const oldExp = D.exp, oldEq = D.eq, oldLev = oldEq>0?oldExp/oldEq:0;
  const oldCash = D.cash, oldTotal = D.total;

  function pair(label, oldV, newV, fmt){
    const dv = newV - oldV;
    const dStr = dv === 0 ? '' :
      ` <span class='${cls(dv)}' style='font-size:12px'>(${dv>0?'+':''}${fmt(dv)})</span>`;
    return `<span>${label}</span><span>${fmt(newV)}${dStr}</span>`;
  }
  const fW = v => (v/10000).toFixed(1)+'万';
  const fL = v => v.toFixed(2)+'x';
  const fP = v => v.toFixed(1)+'%';

  const cashLabel = newCash < 0 ? `现金 <span class='neg' style='font-size:12px'>(不足!)</span>` : '现金';
  document.getElementById('rebalKv').innerHTML =
    pair('总资产',       oldTotal, newTotal, fW) +
    pair(cashLabel,      oldCash,  newCash,  fW) +
    pair('有效纳指暴露', oldExp,   newExp,   fW) +
    pair('杠杆倍数',     oldLev,   newLev,   fL) +
    pair('净资产',       oldEq,    newEq,    fW) +
    `<span>股票占比</span><span>${fP(stockPct)}</span>`;

  // 压力测试：调仓后净资产 / 调仓后总盈亏 / 对比当前（净资产）
  function plCell(v) {
    const s = v>=0?'+':''; return `<td class='${cls(v)}'>${s}${(v/10000).toFixed(1)}万</td>`;
  }
  let h = '<tr><th>场景</th><th>调仓后净资产</th><th>调仓后总盈亏</th><th>对比当前</th></tr>';
  D.up_pcts.forEach(up => {
    const v   = newEq + newExp * up/100;
    const pl  = D.base_ret + (v - D.eq);
    const cmp = v - (oldEq + oldExp * up/100);
    h += `<tr><td>涨${up}%</td><td>${fW(v)}</td>${plCell(pl)}${plCell(cmp)}</tr>`;
  });
  const curPL = D.base_ret + (newEq - D.eq);
  h += `<tr class='current'><td>当前</td><td>${fW(newEq)}</td>${plCell(curPL)}<td>—</td></tr>`;
  D.down_pcts.forEach(dn => {
    const v   = newEq - newExp * dn/100;
    const pl  = D.base_ret + (v - D.eq);
    const cmp = v - (oldEq - oldExp * dn/100);
    const ac  = (dn===20||dn===30) ? 'alert' : '';
    h += `<tr class='${ac}'><td>跌${dn}%</td><td>${fW(v)}</td>${plCell(pl)}${plCell(cmp)}</tr>`;
  });
  document.getElementById('rebalStress').innerHTML = h;
}

document.getElementById('rebalAdd').addEventListener('click', () => {
  rebalRows.push({location:'新增持仓', name:'新标的', symbol:'', market:'us',
                  beta:1.0, base:0, delta:0, isNew:true});
  buildRebalTable();
});
document.getElementById('rebalReset').addEventListener('click', () => {
  rebalRows.length = 0;
  D.assets.forEach(a => rebalRows.push({
    location:a.location, name:a.name, symbol:a.symbol, market:a.market,
    beta:a.beta, base:a.value, delta:0, isNew:false
  }));
  buildRebalTable();
});
buildRebalTable();
""")
    out.append("</script>")
    out.append("</body></html>")
    return "".join(out)

# ── 主函数 ────────────────────────────────────────────────────────────────────
def main() -> None:
    args = [a for a in sys.argv[1:]]
    html_mode = "--html" in args
    if html_mode:
        args.remove("--html")
    path = Path(args[0]) if args else find_latest_json(BACKUP_DIR)
    sim_amounts = [-20, -10, -5, 5, 10, 20]   # HTML 默认值，浏览器中可改

    print(f"\n正在加载: {path.name}")
    print("正在获取市场数据...", end="", flush=True)
    data = load_data(path)
    fx, mkt, from_cache = get_market_info()
    p    = calc_portfolio(data, fx)
    cache_hint = "（缓存）" if from_cache else "（实时）"
    print(f" 完成 {cache_hint}\n")

    # HTML 模式：手动盈亏覆盖
    manual_ret_pre = load_manual_return()
    if manual_ret_pre is not None:
        p["total_return"] = manual_ret_pre
    forward_pe_pre = mkt.get("forward_pe") or load_forward_pe()

    if html_mode:
        score, reasons = overall_risk(p, mkt)
        html = generate_html(p, fx, mkt, forward_pe_pre, score, reasons, sim_amounts)
        out_path = Path(__file__).parent / f"report_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
        out_path.write_text(html, encoding="utf-8")
        print(f"HTML 报告已生成: {out_path}")
        import subprocess
        subprocess.run(["open", str(out_path)], check=False)
        return

    sep = "=" * 56
    print(sep)
    print(f"  持仓风险评估报告  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(sep)

    # ── 模块1：持仓快照 ──
    print("\n【持仓快照】")
    print(f"  总资产:       {w(p['total_cny'])}")
    print(f"  总负债:       {w(p['total_debt'])}")
    print(f"  净资产:       {w(p['net_equity'])}")
    print(f"  有效纳指暴露: {w(p['nasdaq_exp'])}")
    print(f"  杠杆倍数:     {p['leverage']:.2f}x")
    print(f"  现金储备:     {w(p['cash_cny'])}")
    print(f"  汇率参考:     USD {fx['USD']:.2f}  HKD {fx['HKD']:.4f}")

    # ── 模块1.5：实时总盈亏 ──
    manual_ret = load_manual_return()
    if manual_ret is not None:
        p["total_return"] = manual_ret
    if p["return_details"] or manual_ret is not None:
        print("\n【实时总盈亏】")
        if manual_ret is not None:
            print(f"  合计:        {fmt_pct_w(manual_ret)}  (手动)")
        else:
            for r in sorted(p["return_details"], key=lambda x: -x["amount"]):
                print(f"  {r['location']:<10}  {fmt_pct_w(r['amount']):>8}  "
                      f"(基准 {fmt_pct_w(r['baseline'])} + 已记录 {fmt_pct_w(r['recorded'])})")
            print(f"  {'─'*60}")
            print(f"  {'合计实时总盈亏':<10}     {fmt_pct_w(p['total_return']):>8}")
        if p["last_cal_ts"]:
            cal_str = datetime.fromtimestamp(p["last_cal_ts"] / 1000).strftime("%Y-%m-%d")
            print(f"  最近校准日期: {cal_str}")

    # ── 模块2：市场估值 + 风险等级 ──
    manual_fpe = load_forward_pe()
    forward_pe = mkt.get("forward_pe") or manual_fpe  # 自动优先，无则用手动

    print("\n【市场估值（QQQ）】")
    pe_str  = f"{mkt['pe']:.1f}" if mkt["pe"] else "获取失败"
    if forward_pe:
        fpe_label = "(手动)" if not mkt.get("forward_pe") else ""
        fpe_str = f"{forward_pe:.1f} {fpe_label}".strip()
    else:
        fpe_str = "未设置（见提示）"
    fh_str  = f"{mkt['from_high_pct']:.1f}%" if mkt["from_high_pct"] is not None else "获取失败"
    print(f"  市盈率 TTM:   {pe_str}")
    print(f"  预期市盈率:   {fpe_str}")
    print(f"  距52周高点:   {fh_str}")

    score, reasons = overall_risk(p, mkt)
    print(f"\n  综合风险等级: {RISK_LABELS[score]}")
    for r in reasons:
        print(f"    · {r}")

    # ── 模块3：涨跌场景 ──
    print_stress_test(p)

    # ── 模块4：仓位模拟 ──
    print("\n【仓位模拟】")
    print("  输入调仓金额（万元，正=加仓，负=减仓，空格分隔），输入 q 退出")
    print("  示例: -20 -10 -5 5 10 20")
    while True:
        try:
            raw = input("  （输入 q 退出）> ").strip()
        except EOFError:
            break
        if raw.lower() in ("q", "quit", "exit", "退出"):
            break
        if not raw:
            continue
        try:
            amounts = [float(x) for x in raw.split()]
            print()
            print_position_sim(p, amounts)
            print()
        except ValueError:
            print("  输入无效，请输入数字（例如 -10 5 20）")

    # ── 模块5：月供压力 ──
    print("\n【贷款月供压力】")
    for ld in p["loan_details"]:
        print(f"  {ld['name']:<16} 余额 {w(ld['remaining'])}  月供 {ld['monthly']:,.0f} 元")
    print(f"  {'合计':<16} 余额 {w(p['total_debt'])}  月供 {p['total_monthly']:,.0f} 元")
    months = p["months_of_cash"]
    print(f"  现金可撑:     {months:.0f} 个月（约 {months / 12:.1f} 年）")

    # ── 更新 Forward PE ──
    cur_fpe_hint = f"当前: {forward_pe:.1f}" if forward_pe else "当前: 未设置"
    print(f"\n【更新预期市盈率 Forward PE】（{cur_fpe_hint}，参考 multpl.com/nasdaq-pe-ratio）")
    try:
        raw = input("  输入新数值更新，直接回车跳过: ").strip()
        if raw:
            save_forward_pe(float(raw))
            print(f"  已保存 Forward PE: {float(raw):.1f}")
    except (ValueError, EOFError):
        pass

    # ── 更新手动总盈亏（覆盖估算值）──
    print(f"\n【更新实时总盈亏】（当前: {fmt_pct_w(p['total_return'])}）")
    print("  从你的 app 直接读取更准确，输入 CNY 金额覆盖估算（输入 auto 恢复自动）")
    try:
        raw = input("  输入新数值/auto，直接回车跳过: ").strip()
        if raw.lower() == "auto":
            _save_config_key("manual_total_return", None)
            print("  已恢复自动估算")
        elif raw:
            save_manual_return(float(raw))
            print(f"  已保存实时总盈亏: {fmt_pct_w(float(raw))}")
    except (ValueError, EOFError):
        pass

    print(f"\n{sep}\n")


if __name__ == "__main__":
    main()
