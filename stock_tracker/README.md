# stock-tracker

股票追踪是 `fund-tracker` 旁边的独立 A 股模块。它只抓公开行情、财务和公告，不连接券商、不登录账户、不执行任何交易。

它的报告节奏、四组结论和手机可读格式与基金模块一致，但**配置、数据库、报告、缓存、飞书 webhook、定时任务全部独立**。股票推送只走“股票追踪”机器人，不会复用基金机器人的 webhook。

## 先看清楚

- 股票波动比基金大得多；单票会遇到停牌、涨跌停、公告跳空、业绩变脸。
- 单票计划仓位建议不超过股票资金的 10%–15%，除非你明确愿意承担更大波动。
- 证据分不是胜率。80+ 只代表质量、趋势、位置、宏观、风险五项更一致，仍可能亏损。
- 参考买入/卖出区间是机械规则，不是目标价预测。
- 本工具不能查看账户、不能代下单、不能保证收益，也不是持牌投顾。
- **不预置股票池**：不会默认替你选股；必须先 `add` 你的代码/名称和目标权重。

## 快速开始

```bash
cd fund-tracker
python3 -m pip install -r requirements.txt

# 1. 初始化独立配置、数据库和报告目录
python3 -m stock_tracker init

# 2. 添加股票（代码或名称关键词）
python3 -m stock_tracker add 600519 --category 白酒 --role 核心 --weight 0.10
python3 -m stock_tracker add 600036 --category 银行 --role 防御 --weight 0.10

# 3. 检查单只股票的数据、评分和公告
python3 -m stock_tracker check 600519 --full

# 4. 看跟踪列表
python3 -m stock_tracker list

# 5. 跑一次（抓行情、生成日报；到期自动生成 2 日/周/月报告）
python3 -m stock_tracker run

# 6. 打开报告
open stock-reports/latest.md

# 7. 立即生成一份 2 日推荐（到期才会自动生成）
python3 -m stock_tracker review --force
open stock-reports/review-latest.md

# 8. 手动补周度 / 月度总结
python3 -m stock_tracker weekly --date 2026-10-30
python3 -m stock_tracker monthly --date 2026-10-30
```

`--weight 0.10` 表示目标权重 10%；写 `10` 也会被当作 10%。不写 `--weight` 时只进观察池，报告里通常只会出现“暂不动作”。

## 实际持仓（可选，但强烈建议）

没有持仓文件时，报告按“观察池/目标权重”判断，不会假装你已经有仓位；“继续持有”“可以出货”通常为空。

复制示例为真实持仓文件，只保存在本机：

```bash
cp stock-data/holdings.example.json stock-data/holdings.json
```

格式：

```json
{
  "as_of": "2026-10-23",
  "cash": 50000,
  "items": [
    {"code": "600519", "shares": 100, "cost_price": 1500.0},
    {"code": "600036", "shares": 1000, "cost_price": 35.0}
  ]
}
```

- `shares` + `cost_price` 用来算市值、盈亏和实际权重。
- 现金会算进总资产和现金权重；目标权重按“股票总资产（含现金）”计算。如果你只想按已投资市值算，把 `cash` 设为 0。
- 持仓文件只在本机读取；不要提交到公开仓库，也不要发给别人。

## 四组结论

每份 2 日推荐、周度总结、月度总结都会给出：

1. **继续持有**：已到目标权重，评分和证据不弱；维持，不追涨。
2. **可以跟进持有**：未建满或未持有，价格在参考区间，评分/证据达标；分 2–3 批，单票先建 1/3。
3. **可以调整**：实际权重超过目标、证据偏弱、需要复核公告；先减到目标或暂停加仓，不是一次清仓。
4. **可以出货**：目标权重为 0、重大预警、明显止盈区或评分跌破清仓复核线；分批减，不一次砸。
5. **暂不动作（观察）**：高分但价格没回调、数据不足、或没有实际仓位；不追高。

“可以出货”只在有实际持仓时出现；没有持仓时不会让你卖不存在的股票。

## 参考区间怎么来的

- 买入区间：近一年价格区间的 20%–40% 分位。
- 卖出区间：近一年价格区间的 80%–100% 分位。
- 价格低于买入区间时，报告会写“先等企稳，不接飞刀”，不会因为跌得多就自动买入。
- 价格高于买入区间时，报告会写“等回调，不追高”，不会追单日大涨。

这些都是机械参考，不是目标价。

## 报告节奏

- 每个交易日：日报（市场、宏观、观察池、四组预览、预警、公告）。
- 每 2 个交易日：2 日推荐（四组结论、证据面板、参考区间、执行清单）。
- 每周五：周度总结。
- 每月最后一个工作日：月度总结。
- 若周度/月度与 2 日推荐同日，默认都生成并都推送；可用配置关闭。

“2 个交易日”按真实行情日期计算，周末和长假顺延；报告里的“预计下一次日期”只跳过周末，遇到长假以实际交易日为准。

## 学习闭环与三原则卡

每次生成 2 日推荐时，股票动作、评分、证据分和当时价格会写入本机 `predictions` 表；之后按 2/5/20 个交易日回填，并出现在推荐/周报/月报的“学习与复盘”里。

- 买入/持有类看是否上涨，减仓/出货类看是否下跌；观察/复核不计入方向命中率，单独看是否避免追高。
- 同时记录平均涨跌和平均最大不利，避免只看命中率、忽略过程回撤。
- 样本不足 20 条只记录，不自动改评分、目标权重或下单；2/5/20 日窗口重叠，不是独立实验。
- 三原则卡：行动（马斯克式最小动作）、坚持（黄仁勋式长期迭代/证伪条件）、眼光（巴菲特式能力圈/安全边际）。

A 版不调用 API。B 版已接入本机模型：日报用 Qwen 做新闻/文章摘要，周报用 DeepSeek 做周度复盘。没有固定专家源，所以只做媒体事实摘要和 AI 推断，不冒名专家；模型只允许用于本项目的新闻/公告/文章分析，冷启动后调用完会请求本地代理卸载，不常驻。模型不可用时报告照常生成。

## 飞书“股票追踪”机器人

必须使用单独的机器人，不要填基金机器人的 webhook。

1. 在飞书群里添加自定义机器人，名称设为 **股票追踪**。
2. 复制 webhook（有签名密钥就一起复制）。
3. 保存到独立配置：

```bash
python3 -m stock_tracker set-webhook 'https://open.feishu.cn/open-apis/bot/v2/hook/...' --secret '...'
```

4. 先发测试卡：

```bash
python3 -m stock_tracker test-notify --webhook-only
```

5. 确认手机收到后，再跑正式报告：

```bash
python3 -m stock_tracker run
```

完整报告会转成飞书 schema 2.0 卡片；表格会拆成逐条列表，长报告拆成多段卡片。卡片失败时自动回退文本完整版。

## 定时任务

```bash
# 安装：每个工作日 19:00 自动跑
python3 -m stock_tracker schedule --install

# 查看状态
python3 -m stock_tracker schedule --status

# 卸载
python3 -m stock_tracker schedule --uninstall
```

- label：`com.t8alex.stocktracker`
- 包装脚本：`~/.local/share/stock-tracker/run_daily.sh`
- 日志：`stock-data/cron.log`、`stock-data/launchd.err.log`

股票任务（19:00）和基金任务（20:00）分开，互不影响；另有跨基金的“模型表现复盘”任务在每个工作日 20:30 触发、只在当月最后一个工作日生成，读取基金和股票两边的预测日志，报告在 `reports/model-review-YYYY-MM.md`。

## 配置

`stock-config.json` 与基金 `config.json` 完全独立。常用项：

| 键 | 默认 | 说明 |
|---|---|---|
| `data_dir` | `stock-data` | 数据库和缓存 |
| `report_dir` | `stock-reports` | 报告目录 |
| `holdings_file` | `stock-data/holdings.json` | 实际持仓 |
| `notify.webhook_url` | 空 | 股票追踪机器人 |
| `notify.full_report` | `all` | 日报/2日/周/月都推完整版 |
| `review.interval_trading_days` | `2` | 推荐间隔 |
| `summary.weekly_enabled` | `true` | 周五总结 |
| `summary.monthly_enabled` | `true` | 月末总结 |
| `alerts.daily_move_pct` | `5.0` | 单日异动 |
| `alerts.dd_1y_pct` | `20.0` | 近一年回撤预警 |
| `alerts.announcement_days` | `45` | 公告预警回看天数 |
| `ai.enabled` | `true` | 本地模型新闻摘要开关 |
| `ai.daily` / `ai.weekly` | 本机 Qwen / DeepSeek | 每日摘要与周度复盘模型 |
| `ai.release_after_call` | `true` | 冷启动模型后立即卸载 |
| `schedule.hour/minute` | `19:00` | 定时任务 |

## 数据源

- 行情：腾讯行情为主，东财行情为备。
- 日 K：新浪财经为主，东财/腾讯为备。
- 财务：东财 F10 主要财务指标。
- 公告：东财公告接口。
- 公司行业：东财 F10 公司概况。
- 宏观：复用基金模块的公开宏观数据。

接口失败时会用缓存；没有缓存时跳过该股票，并在报告里标注。行情/财务/公告都可能延迟，下单前以券商实时行情为准。

## 目录结构

```
fund-tracker/
├── stock-config.json
├── stock_tracker/
│   ├── cli.py          # 命令行
│   ├── config.py       # 独立配置
│   ├── fetch.py        # 行情/K线/财务/公告
│   ├── metrics.py      # 收益/回撤/波动/技术面/评分
│   ├── evidence.py     # 证据分
│   ├── store.py        # SQLite
│   ├── holdings.py     # 实际持仓
│   └── report.py       # 日报/2日/周/月
├── fund_tracker/learning.py  # 共用预测日志与 2/5/20 日回填
├── fund_tracker/notify.py    # 复用；调用时只传 stock-config.json 的 notify 配置
├── stock-data/
│   ├── stock_tracker.db
│   ├── holdings.json
│   └── cache/
└── stock-reports/
```

## 风险提示

股票是单票风险，不是基金。任何评分、证据分、参考区间都只是历史数据和机械规则的整理，不构成投资建议，也不保证收益。请根据自己的资金期限、风险承受能力和实际持仓做决策。
