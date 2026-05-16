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

    for a in data["assets"]:
        raw   = a["quantity"] * a["currentPrice"]
        mkt   = a["market"]
        mult  = fx["HKD"] if mkt == "hk" else fx["USD"] if mkt in ("us", "crypto") else 1.0
        value = raw * mult
        total_cny += value

        loc = a.get("location", "")
        location_value[loc] = location_value.get(loc, 0.0) + value
        location_assets.setdefault(loc, []).append((a.get("addedAt") or 0, value))

        atype = a["assetType"]
        if atype == "cash":
            cash_cny += value
            continue

        beta = a.get("betaMultiplier")
        if beta is None:
            beta = type_beta.get(atype, 0.0)
        if beta is None:
            beta = 0.0
        nasdaq_exp += value * float(beta)

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

    # 历史总盈亏（外部校准 + 校准后的市值变化）
    # 取每个账户在校准时刻最近的 accountSnapshot 作为基准，与当前账户市值做差
    snaps = data.get("accountSnapshots", [])
    return_details = []
    total_return   = 0.0
    last_cal_ts    = 0
    for b in data.get("accountReturnBaselines", []):
        loc     = b.get("location", "")
        ext     = float(b.get("external_total_return_cny") or 0.0)
        cal_ts  = b.get("calibration_timestamp") or 0

        # 找该 location 校准时刻附近最近的 snapshot（<=cal_ts，取最大的）
        cal_value = None
        best_ts   = -1
        for s in snaps:
            if s.get("location") != loc:
                continue
            ts = s.get("timestamp", 0)
            if ts <= cal_ts and ts > best_ts:
                best_ts   = ts
                cal_value = s.get("value_cny")

        # 只统计校准时已存在的资产，避免转入新资产被算成盈亏
        cur_value = sum(v for added, v in location_assets.get(loc, []) if added <= cal_ts)
        delta     = (cur_value - cal_value) if cal_value is not None else 0.0
        amount    = ext + delta

        return_details.append({
            "location": loc, "external": ext, "delta": delta, "amount": amount,
        })
        total_return += amount
        if cal_ts > last_cal_ts:
            last_cal_ts = cal_ts

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
        "return_details": return_details,
        "total_return":   total_return,
        "last_cal_ts":    last_cal_ts,
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

# ── 主函数 ────────────────────────────────────────────────────────────────────
def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = find_latest_json(BACKUP_DIR)

    print(f"\n正在加载: {path.name}")
    print("正在获取市场数据...", end="", flush=True)
    data = load_data(path)
    fx, mkt, from_cache = get_market_info()
    p    = calc_portfolio(data, fx)
    cache_hint = "（缓存）" if from_cache else "（实时）"
    print(f" 完成 {cache_hint}\n")

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
        # 手动值优先，覆盖自动计算
        p["total_return"] = manual_ret
    if p["return_details"] or manual_ret is not None:
        print("\n【实时总盈亏】")
        if manual_ret is not None:
            print(f"  合计:        {fmt_pct_w(manual_ret)}  (手动)")
        else:
            for r in sorted(p["return_details"], key=lambda x: -x["amount"]):
                print(f"  {r['location']:<10}  {fmt_pct_w(r['amount']):>8}  "
                      f"(校准 {fmt_pct_w(r['external'])}  + 后续 {fmt_pct_w(r['delta'])})")
            print(f"  {'合计':<10}  {fmt_pct_w(p['total_return']):>8}  (估算)")
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

    # ── 模块4：月供压力 ──
    print("\n【贷款月供压力】")
    for ld in p["loan_details"]:
        print(f"  {ld['name']:<16} 余额 {w(ld['remaining'])}  月供 {ld['monthly']:,.0f} 元")
    print(f"  {'合计':<16} 余额 {w(p['total_debt'])}  月供 {p['total_monthly']:,.0f} 元")
    months = p["months_of_cash"]
    print(f"  现金可撑:     {months:.0f} 个月（约 {months / 12:.1f} 年）")

    # ── 模块5：仓位模拟 ──
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
