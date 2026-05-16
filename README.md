# 持仓风险评估工具 (risk_assessment)

读取 [AssetTracker](https://github.com/zhanggy000/Nq100) app 导出的 JSON 备份，输出**持仓风险报告**：杠杆、估值、压力测试、仓位模拟。

适合**用了杠杆 / 借款投资**、需要量化"如果市场跌 X% 我的净资产剩多少"的长期投资者参考。

---

## 功能

- **持仓快照**：总资产 / 总负债 / 净资产 / 有效纳指暴露 / 杠杆倍数 / 现金储备
- **实时总盈亏**：按账户拆分（基准 + App 已记录），算法对齐 [Nq100 dashboard.historicalTotalInvestmentReturnCny](https://github.com/zhanggy000/Nq100/blob/master/return_calculation_rules.md)
- **市场估值**：QQQ 市盈率 TTM、Forward PE（手动）、距 52 周高点
- **综合风险等级**🟢低 / 🟡中 / 🟠高 / 🔴极高
- **涨跌场景压力测试**：±5/10/15/20/25/30/40/50%，每档显示净资产 + 实时总盈亏
- **贷款月供压力**：自动按摊还表算出当前实际剩余本金（不用对账单原值）
- **仓位模拟**：减仓/加仓对比，多档位横向展示下行风险 vs 上行收益
- **HTML 报告**：浏览器打开，**仓位模拟可交互**（输入框改金额，回车刷新表格）
- **数据缓存**：市场数据 2h 缓存，重复运行秒开
- **手动覆盖**：Forward PE 与实时总盈亏支持手动输入（存 `.config.json`，长期保留）

---

## 安装

```bash
pip install yfinance
```

只需要 yfinance 一个外部依赖。

---

## 使用方法

```bash
# 终端报告（交互式仓位模拟，输入 q 退出）
python3 risk_assessment.py

# HTML 报告（自动用浏览器打开，仓位模拟在浏览器里实时调整）
python3 risk_assessment.py --html

# 指定 JSON 路径（默认会自动找 AssetTracker 备份目录里最新的 export_*.json）
python3 risk_assessment.py /path/to/export.json
python3 risk_assessment.py --html /path/to/export.json
```

### 第一次运行后会提示输入

- **Forward PE**：yfinance 不提供 ETF 的 forwardPE，从 [multpl.com/nasdaq-pe-ratio](https://www.multpl.com/nasdaq-pe-ratio) 手抄
- **实时总盈亏**：如果你想要 app 里显示的精确值（FX 差异可能让脚本估算偏 0.x 万），从 app 抄过来；输入 `auto` 恢复自动估算

输入一次后存到 `.config.json`，之后每次运行自动用。

---

## 路径配置

脚本默认从这里读取最新的 JSON：

```
/Users/zhang/Library/Mobile Documents/com~apple~CloudDocs/Downloads/asset tracker/数据备份/
```

如果你的路径不同，编辑 [risk_assessment.py](risk_assessment.py) 顶部 `BACKUP_DIR`。

---

## 输出文件

- `report_YYYYMMDD_HHMM.html` — HTML 报告（运行 `--html` 时生成）
- `.market_cache.json` — 市场数据缓存（2 小时有效，已加入 `.gitignore`）
- `.config.json` — 手动覆盖值（已加入 `.gitignore`）

---

## 算法说明

**实时总盈亏（核心）**

对每个持股账户：

```
账户总盈利 = baseline_return_cny + App 已记录账户收益
App 已记录账户收益 = 最后一条快照市值 - 第一条快照市值 + 转账调整
转账调整 = ∑(转出金额) - ∑(转入金额)   // 仅 type='transfer'，buy/sell 不参与
```

合计为各账户之和。算法完全对齐 AssetTracker app 的 `historicalTotalInvestmentReturnCny` 显示值。

**贷款剩余本金**

按等额本息摊还表，根据 `lastAppliedMonth` 自动累计计算到今天的实际余额（不依赖 JSON 里存的旧 `principal`）。

**压力测试**

```
新净资产 = 当前净资产 + 有效纳指暴露 × 涨跌%
新实时盈亏 = 当前实时盈亏 + (新净资产 - 当前净资产)
```

**仓位模拟**（仅交易股票/现金的内部转换，不假设还贷）

减仓/加仓改变 `有效纳指暴露`，净资产保持不变（股票 ↔ 现金）。
