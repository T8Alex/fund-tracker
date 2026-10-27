# fund-tracker

一个本地运行的公募基金跟踪工具。它只抓公开数据，不连接支付宝、不登录账户、不执行任何交易。

> 股票模块是独立的 A 股追踪工具，配置、数据库、报告、飞书机器人和定时任务都与基金分开。使用说明见 [`stock_tracker/README.md`](stock_tracker/README.md)。

## 它每天做什么

每个交易日晚上自动跑一次：

1. 抓取跟踪列表里每只基金的最新净值、基金经理、规模、持仓比例、同类排名、费率。
2. 抓取最新季报/中报的前十大股票、前五大债券，以及最新年度报告/定期报告和重要公告。
3. 抓取宏观和外部环境：中国/美国国债收益率、美元/人民币、黄金、原油、全球股指、A 股指数 K 线、财经新闻标题。
4. 计算近 1 周/1 月/3 月/6 月/1 年/3 年收益、最大回撤、波动率、夏普比率，以及 MA20/MA60/RSI 等趋势指标。
5. 计算“证据分”：质量 55% + 趋势 15% + 位置 15% + 宏观 10% + 风险 5%。
6. 和上一次快照对比：评分变化、基金经理是否换人、规模是否异常。
7. 生成 Markdown 日报 + CSV，并推送 macOS 通知（可选飞书群机器人）。
8. 触发预警：回撤超阈值、规模低于 2 亿、经理变更、净值未更新、单日异常波动、重要公告等。
9. 每满 2 个交易日自动生成一份“推荐名单 + 买卖参考区间”的推荐报告。
10. 每周五生成一份周度总结推荐；每月最后一个工作日生成月度总结推荐。
11. 把每次 2 日推荐的动作写入本机预测日志，在 2/5/20 个交易日后回填结果，生成“学习与复盘”和“三原则卡”。
12. 每月最后一个工作日 20:30 生成基金 + 股票合并的“模型表现复盘”，只列规则候选，不自动改权重或阈值。
13. 用本机 Qwen 把新闻标题/摘要整理成“事实、偏多/偏空、风险、后续观察”；周报用本机 DeepSeek 做一次周度复盘。模型只在需要时启动，调用完释放，不常驻。

## 它不做什么

- 不登录支付宝、不自动读取你的持仓；你手动提供的持仓只在本机计算。
- 不预测涨跌，不自动下单。
- 不保证收益。评分只是“跟踪排序工具”，不是买入信号。
- 不做投资建议。最终决策需要你自己做。
- 本地模型只用于本项目的新闻/公告/文章摘要和研究分析，不接通用聊天，不用于无关任务。

## 快速开始

```bash
cd fund-tracker
python3 -m pip install -r requirements.txt

# 1. 初始化
python3 -m fund_tracker init

# 2. 检查数据接口
python3 -m fund_tracker check

# 3. 添加基金（代码或名称关键词）
python3 -m fund_tracker add 000032
python3 -m fund_tracker add 易方达信用债

# 4. 看跟踪列表
python3 -m fund_tracker list

# 5. 跑一次，生成日报
python3 -m fund_tracker run

# 6. 打开报告
open reports/latest.md

# 7. 立即生成一份 2 日推荐（到期才会自动生成）
python3 -m fund_tracker review --force
open reports/review-latest.md

# 8. 手动补一份周度 / 月度总结
python3 -m fund_tracker weekly --date 2026-10-23
python3 -m fund_tracker monthly --date 2026-10-30
open reports/weekly-latest.md
open reports/monthly-latest.md
```

首次运行会抓取全部历史净值，稍慢；之后有缓存，通常几十秒。

## 从排行榜筛选候选基金

```bash
# 短债：按 3 年收益排序，筛规模 2–200 亿、成立 3 年以上
python3 -m fund_tracker discover --type zq --keyword 短债 --keyword 中短债 --details

# 纯债
python3 -m fund_tracker discover --type zq --keyword 纯债 --keyword 信用债 --keyword 利率债 --details

# 二级债 / 固收+
python3 -m fund_tracker discover --type zq --keyword 二级 --keyword 增强 --details

# 红利 / 低波
python3 -m fund_tracker discover --type zs --keyword 红利 --keyword 低波 --details

# 宽基
python3 -m fund_tracker discover --type zs --keyword 沪深300 --keyword 中证500 --keyword 中证A500 --details
```

`--details` 会逐只抓详情并打分，20 只大约需要 1–2 分钟。确认后把代码加入跟踪：

```bash
python3 -m fund_tracker add 000032 --category 纯债 --role 核心 --weight 0.2 --note "底仓候选"
```

也可以直接：

```bash
python3 -m fund_tracker discover --type zq --keyword 短债 --details --add-top 3
```

## 每日报告怎么读

报告在 `reports/YYYY-MM-DD.md`，同时有一份 `reports/latest.md`。

重点看七块：

1. **宏观与外部环境**：中美利率、汇率、黄金、原油、全球股指、A 股 K 线、新闻标题和“环境→资产”的机械映射。
2. **今日重点（证据分）**：质量、趋势、位置、宏观、风险五项是否同向；证据分不是上涨概率。
3. **组合概览与实盘**：平均分、预警数量、净值未更新数量；接入 `holdings.json` 后显示实际市值、实际权重、持有收益、当日收益和与目标权重的差额。
4. **分类明细**：每只基金的收益、回撤、夏普、同类排名、规模、经理年限、评分。
5. **今日预警**：回撤超阈值、经理变更、规模骤降、净值未更新、重要公告。
6. **持仓与公告**：最新季报/中报的前十大股票、前五大债券，以及年度报告/定期报告链接。
7. **变化跟踪**：评分和上次比升了还是降了。

评分构成：

- 质量分 75%：收益 25、回撤 25、夏普 15、经理 15、规模 10、费率 10（不同类别权重略有调整）。
- 匹配分 25%：按你配置的最大回撤容忍度（默认 15%）给分。
- 历史不足 3 年扣 5 分，不足 1 年扣 20 分。

评分高不代表明天会涨，只代表“历史风险收益特征 + 当前数据质量”相对更好。

## 推荐节奏：2 日 / 周度 / 月度

每天跑日报；推荐分三层：

- **每 2 个交易日**：详细推荐，给每只基金的动作和参考区间。
- **每周五**：周度总结，回看本周 5 个交易日，按“继续持有 / 可以跟进持有 / 可以调整 / 可以出货”四组给结论。
- **每月最后一个工作日**：月度总结，回看本月，按同样的四组给结论。如果月末是周六周日，就提前到最后一个工作日。

### 每 2 个交易日

每天跑日报，但**只有满 2 个交易日**才生成一份“推荐名单 + 买卖参考区间”。默认自动触发，也可以手动跑：

```bash
# 到期才生成
python3 -m fund_tracker review

# 立即强制生成一份
python3 -m fund_tracker review --force

# 指定日期
python3 -m fund_tracker review --date 2026-10-27
```

报告在 `reports/review-YYYY-MM-DD.md`，同时有 `reports/review-latest.md`（文件名沿用 `review`，内容现在是 2 日推荐）。内容包括：

1. 2 日市场：沪深300、中证500、创业板、红利指数的 2 日涨跌。
2. 宏观与外部环境：中美利率、汇率、黄金、原油、全球股指、K 线、新闻标题。
3. 组合 2 日加权收益、最强/最弱基金。
4. 证据面板：每只基金的证据分、五项子分和结论。
5. 推荐名单：每只基金的动作（分批买入/持有、持有、观察、减仓/复核、剔除）。
6. 参考买入区间、参考卖出区间、理由。
7. 持仓穿透、年度报告/定期报告和重要公告。
8. 四组结论：继续持有、可以跟进持有、可以调整、可以出货；另列“暂不动作（观察）”。
9. 执行清单和下一次推荐日期。
10. 三原则卡（行动 / 坚持 / 眼光）和学习与复盘。

### 每周五：周度总结

默认在周五晚间自动生成，报告写入 `reports/weekly-YYYY-MM-DD.md` 和 `reports/weekly-latest.md`。它按本周 5 个交易日计算，给出四组结论。手动补生成：

```bash
python3 -m fund_tracker weekly --date 2026-10-23
python3 -m fund_tracker weekly --date 2026-10-23 --force  # 重生成
```

### 每月最后一个工作日：月度总结

默认在每月最后一个工作日自动生成，报告写入 `reports/monthly-YYYY-MM.md` 和 `reports/monthly-latest.md`。它按本月至今的净值计算，给出四组结论。手动补生成：

```bash
python3 -m fund_tracker monthly --date 2026-10-30
python3 -m fund_tracker monthly --date 2026-10-30 --force  # 重生成
```

### 每月最后一个工作日 20:30：模型表现复盘

这是基金和股票共用的**模型复盘**，不生成买卖建议，也不改目标权重。它读取两边的本地预测日志，按 2/5/20 日周期统计样本量、方向命中率、平均涨跌、平均最大不利，并给出四组表现和证据分分层：

```bash
# 手动生成（默认今天）
python3 -m fund_tracker.model_review

# 指定月份的最后一天，便于补生成
python3 -m fund_tracker.model_review --date 2026-10-30

# 同月已存在时强制覆盖
python3 -m fund_tracker.model_review --date 2026-10-30 --force

# 只在“本月最后一个工作日”运行，其他日期直接跳过
python3 -m fund_tracker.model_review --if-due
```

报告写入 `reports/model-review-YYYY-MM.md` 和 `reports/model-review-latest.md`。规则：

- 单个周期方向样本少于 **20 条**时，只记录、不调参；报告会明确写“样本不足”。
- 达到门槛也只列“规则候选”，必须经过至少 6 个月滚动回测和人工确认，才考虑改评分/证据权重/阈值。
- 不自动改目标权重，不自动下单，不把命中率包装成胜率。
- 2/5/20 日窗口高度重叠，不是独立实验；基金按累计净值、股票按收盘价回填。

配置在 `model-review-config.json`（模板见 `model-review-config.example.json`）。默认只发 macOS 通知；如果希望单独推送，建议新建一个“模型复盘”飞书群机器人，把 webhook 和密钥填到：

```json
{
  "notify": {
    "macos": true,
    "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/...",
    "webhook_secret": "..."
  }
}
```

不建议复用基金或股票机器人：这是跨模块的第三类报告，单独一个群更容易分流、静音和回看。没有填 webhook 时，报告照常在本机生成，不会外发。

定时任务 `com.t8alex.modelreview` 已安装在每个工作日 20:30 触发，脚本内部用 `--if-due` 判断是否为当月最后一个工作日；日志在 `~/.local/share/model-review/cron.log`。

```bash
# 查看状态
launchctl print gui/$(id -u)/com.t8alex.modelreview

# 卸载（需要时）
launchctl bootout gui/$(id -u)/com.t8alex.modelreview
```

### 四组结论怎么读

- **继续持有**：实际仓位已在目标附近，维持不动，不追涨。
- **可以跟进持有**：还没建满或还没建仓，才按参考区间分 2–3 批；已建满就只持有。
- **可以调整**：实际仓位高于目标，先再平衡、暂停加仓，或减 1/3；先核对公告。
- **可以出货**：目标权重为 0、评分/证据低、重大预警或进入止盈区；分批减，不一次清仓。
- **暂不动作（观察）**：不占仓、不追高；高分观察类只有出现回调、触发“小仓试探”时，才转入“可以跟进持有”。

> 如果 `data/holdings.json` 存在，这四组按“实际仓位 vs 目标仓位”判断；没有持仓文件时，才退回“按目标权重”的研究口径。四组不是收益承诺。

### 接入实际持仓（可选）

把实际持仓写入 `data/holdings.json`，报告会显示真实市值、实际权重、持有收益、当日收益和与目标权重的差额：

```json
{
  "as_of": "2026-10-23",
  "total_value": 82902.63,
  "total_pnl": -8510.86,
  "daily_pnl": 998.02,
  "cash": 0,
  "items": [
    {
      "code": "016967",
      "name": "南方旺元60天滚动持有中短债E",
      "market_value": 32723.60,
      "pnl": 762.14,
      "daily_pct": 0.01
    }
  ]
}
```

- `market_value` 是持仓市值，`pnl` 是持有收益；`total_value`/`total_pnl` 可省略，程序会按明细相加。
- 不需要股数/份额；如果你有份额和成本，也可以后续扩展。
- 文件只在本机读取，不上传、不打印到报告；不要把真实持仓发到公开群。
- 当前按你填写的市值/盈亏展示，不会用净值自动推算“实时盈亏”；更新持仓时请同时更新 `as_of` 和各项金额。
- 没有该文件时，报告明确标注“按目标权重/观察池判断”，不会假装已有仓位。

**区间的含义：**

- 短债/纯债：当前净值附近到小幅回调，属于“分批建仓参考”，不是预测。债基不要频繁做波段。
- 二级债：当前净值回调 1%–3% 的区间。
- 红利/宽基：近 1 年价格区间的 25%–50% 分位作为买入参考，90%–100% 分位作为止盈参考。
- 观察类基金不给买入区间，避免把“观察”误当成“建议买”。

**重要：**这些区间是机械规则，不是市场预测。基金经理变更、规模骤降、回撤超阈值时，优先按预警处理，不要机械补仓。接入 `holdings.json` 后，动作是相对“实际持仓”的：超配按“再平衡”，低配按“分批买入”，目标为 0 的持仓按“剔除”；没有持仓文件时才按“目标权重”判断。

## 学习闭环与三原则卡

### A 版：零 API 的预测日志（当前已启用）

每次生成 2 日推荐时，程序把每只基金/股票的动作、评分、证据分、当时净值/价格写入本机数据库的 `predictions` 表。之后在 2/5/20 个交易日后按最新净值/价格回填：

- **方向命中**：买入/持有类看是否上涨，减仓/出货类看是否下跌；观察/复核不计入方向命中率，单独统计“是否避免追高”。
- **平均涨跌**和**平均最大不利**：不仅看方向，还看持有过程有多疼。
- **最近到期、目前最好/最差**：周报和月报会直接展示，并给出“我错在哪”的复盘顺序：逻辑错、执行错，还是短期波动。
- **样本不足 20 条只记录、不调权重**；即使样本够了，也不自动改目标权重或下单，仍需人工确认和滚动验证。

学习记录只在本地，不联网、不调用 LLM。2/5/20 日窗口高度重叠，样本不是独立实验，不能把命中率当成胜率承诺。股票按行情收盘价、基金按累计净值回填；遇到分红/拆股，短期结果可能有偏差，只作方向复盘。

### 三原则卡

每份 2 日推荐、周度总结和月度总结会附一张三原则卡：

- **行动（马斯克式）**：现在具体做什么、参考区间、首笔仓位；先做最小可行动作。
- **坚持（黄仁勋式）**：当初的逻辑是什么、什么情况证明逻辑错了；不是无条件死扛。
- **眼光（巴菲特式）**：质量、估值、位置和能力圈；看不懂、证据不足就不做。

卡片只是检查表，不产生新的买卖信号。

### B 版：本地模型新闻/文章摘要（已接入）

B 版使用本机模型，不购买云 API、不把数据发到外部：

- **每日新闻/文章摘要**：`Qwen3.8-27B`（`127.0.0.1:11436`）读取本项目抓到的新闻标题、摘要和宏观数据，提炼事实、偏多/偏空线索、风险和后续观察。
- **周度深度复盘**：`DeepSeek-R1-32B`（`127.0.0.1:11434`）读取本周已生成的每日摘要、组合/观察池表现和新闻材料，做“被验证/被挑战/下周观察”的复盘。
- **来源边界**：没有配置固定专家源，所以不做“专家原话”摘要；只做媒体/机构事实摘要和 AI 推断。报告里会明确标注“AI 推断”，不冒名专家，不编造来源。
- **使用边界**：本地模型只允许用于本项目的新闻/公告/文章摘要、事实提炼和研究分析；程序没有通用聊天入口，也不会拿它做与项目无关的事。
- **内存释放**：调用前先检查模型是否已加载。若是本次冷启动的模型，调用结束会请求本地代理的 `/unload` 立即卸载；如果模型本来就被其他任务占用，则不强行卸载，交给代理的空闲计时器（默认 300 秒）处理。程序不做保活。
- **失败降级**：模型不可用或返回格式不对时，报告照常生成，只显示“AI 摘要未生成”，不会阻塞基金/股票数据、预警和推荐。
- **不是信号**：AI 摘要只作为带来源的研究材料，不自动改目标权重、不自动下单；置信度是模型对材料一致性的自评，不是胜率。

配置在 `config.json` / `stock-config.json` 的 `ai` 段。当前 `ai.daily.model=qwen3.8:27b`、`ai.weekly.model=deepseek-r1:32b`。

## 设置每天自动跑（macOS）

```bash
# 安装：每个工作日 20:00 自动运行（基金）
python3 -m fund_tracker schedule --install

# 查看状态
python3 -m fund_tracker schedule --status

# 卸载
python3 -m fund_tracker schedule --uninstall
```

定时任务写入 `~/Library/LaunchAgents/com.t8alex.fundtracker.plist`，运行日志在 `data/cron.log`；`data/launchd.err.log` / `data/launchd.out.log` 记录 launchd 启动层面的错误。macOS 的 launchd 对中文路径支持不稳定，安装时会在 `~/.local/share/fund-tracker/` 生成一个 ASCII 路径的包装脚本；这是正常现象，不要删除。基金默认每个工作日 20:00；股票是独立任务，每个工作日 19:00；模型复盘是第三个独立任务，每个工作日 20:30 触发、只在当月最后一个工作日实际生成，三者互不影响。

修改运行时间：编辑 `config.json` 的 `schedule.hour` / `schedule.minute`，然后重新 `--install`。

## 推送到飞书（可选）

1. 在飞书里建一个群（自定义机器人只能加在群聊里，单聊不行）。
2. 打开群右上角 `⋯` → **设置** → **群机器人** → **添加机器人** → **自定义机器人**。
3. 起名“基金跟踪”。安全设置建议选 **自定义关键词**，关键词填 `基金`：程序发的标题都含“基金”，能通过校验。
   - 也可以选 **签名校验**，复制密钥，填到 `notify.webhook_secret`。
   - 不建议选 **IP 白名单**，家庭宽带 IP 会变。
4. 复制 Webhook 地址（`https://open.feishu.cn/open-apis/bot/v2/hook/...`），填到 `config.json`：

```json
"notify": {
  "macos": true,
  "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/...",
  "webhook_secret": ""
}
```

5. 测试：

```bash
python3 -m fund_tracker test-notify --webhook-only
```

看到“飞书机器人: 成功”并且群里收到消息，就配置好了。

`notify.full_report` 控制完整版推送：`"review"` 推 2 日推荐和周五/月末总结的完整版；`"all"` 日报也推完整版；`"none"` 只推摘要。当前 `config.json` 设为 `"all"`。

测试完整版排版：

```bash
python3 -m fund_tracker test-notify --webhook-only --full
```

飞书卡片会把 Markdown 表格自动转成逐条列表，避免手机端表格过宽；本地 `reports/*.md` 仍保留表格。完整报告会拆成多个卡片段落发送，避免单个组件过大被截断。

Webhook 相当于密码，不要发到公开仓库或公开群里。国际版 Lark 的地址是 `open.larksuite.com`，同样支持。

官方文档：<https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot>

## 配置

`config.json`：

| 字段 | 说明 |
|---|---|
| `holdings_file` | 实际持仓文件，默认 `data/holdings.json`；不存在则按目标权重 |
| `risk_free_rate` | 夏普比率用的无风险利率，默认 1.5% |
| `max_drawdown_tolerance` | 你的最大回撤容忍度，默认 15% |
| `stale_days` | 净值超过几天没更新就预警，默认 5 天（覆盖周末/假期） |
| `alerts.dd_1y_pct` | 近 1 年回撤预警线，默认 8% |
| `alerts.dd_3y_pct` | 近 3 年回撤预警线，默认 15% |
| `alerts.size_min_yi` | 规模下限，默认 2 亿 |
| `alerts.size_max_yi` | 规模上限，默认 200 亿 |
| `alerts.manager_change` | 基金经理变更是否预警 |
| `review.enabled` | 是否启用推荐报告 |
| `review.auto` | 每日任务到期时是否自动生成推荐 |
| `review.interval_trading_days` | 每几个交易日生成一次推荐，默认 2 |
| `summary.enabled` | 是否启用周度/月度总结 |
| `summary.weekly_enabled` | 是否在周五生成周度总结 |
| `summary.monthly_enabled` | 是否在每月最后一个工作日生成月度总结 |
| `extras_cache_hours` | 持仓/公告缓存小时数，默认 24 |
| `macro.enabled` | 是否抓宏观、K 线、新闻并写入报告 |
| `macro.news_enabled` | 是否抓新闻标题 |
| `macro.news_limit` | 报告里最多列几条新闻，默认 8 |
| `ai.enabled` | 是否启用本地模型新闻/文章摘要；关闭后不调用模型 |
| `ai.daily` | 每日摘要模型端点、模型名、超时和输出上限 |
| `ai.weekly.enabled` | 是否启用本地 DeepSeek 周度复盘；关闭则周报退回每日摘要 |
| `ai.release_after_call` | 冷启动模型后是否立即请求本地代理卸载，默认 `true` |
| `ai.max_news` / `ai.intro_chars` | 每次送入模型的最大新闻条数和摘要截断长度 |
| `ai.max_input_chars` | 输入材料的保守字符上限，超长会先缩短摘要/减少新闻 |
| `notify.macos` | 是否弹 macOS 通知 |
| `notify.webhook_url` | 可选，POST 到自定义 webhook；飞书机器人填这里 |
| `notify.webhook_secret` | 可选，飞书机器人开启“签名校验”时填密钥 |
| `notify.full_report` | `review`（推推荐和周五/月末总结完整版）、`all`（日报也推完整版，当前配置）、`none`（只推摘要） |

## 数据来源

- 净值、经理、规模、持仓、同类排名：天天基金公开页面 `fund.eastmoney.com`。
- 基金类型、管理费、托管费、申赎费、公告：天天基金基金档案 `fundf10.eastmoney.com`。
- 指数行情、全球股指、汇率、黄金、原油：新浪财经公开行情 `hq.sinajs.cn`。
- A 股 K 线历史：新浪财经历史行情接口。
- 中国国债收益率：中国债券信息网（中债国债收益率曲线）。
- 美国国债收益率：美国财政部 daily treasury yield curve CSV。
- 新闻标题：新浪财经滚动新闻、美联储、CNBC RSS；只做事件提示，不把标题当结论。

数据缓存 6 小时，基金档案缓存 7 天。`run --force` 可强制刷新。

## 常见问题

**净值没更新？**
基金净值通常交易日 20:00–23:00 更新。报告里会显示 `nav_date`，超过 5 天会预警。周末和节假日沿用上一交易日，属正常。

**排行榜里收益特别高的基金能买吗？**
不能只看收益。排行榜按 3 年收益排序，排在前面的往往是押注单一赛道或高波动品种。用 `--details` 看回撤、夏普、规模、经理年限后再判断。

**为什么评分和昨天差很多？**
可能因为净值更新、排名变化、规模披露变化，或基金经理变更。报告里的“变化跟踪”会列出来。单日评分波动不用紧张。

**接口失败怎么办？**
先跑 `python3 -m fund_tracker check`。如果是网络问题，程序会自动用缓存；没有缓存时会跳过该基金并在报告里标注。

**会不会泄露隐私？**
不会。所有数据保存在本机 `data/`；程序不登录支付宝/券商、不读取账号密码。你手动写入的 `data/holdings.json` 只在本机计算，不会上传，也不会在报告里回显密钥。

## 目录结构

```
fund-tracker/
├── config.json
├── model-review-config.json  # 每月模型复盘：数据库、报告目录、可选飞书机器人
├── requirements.txt
├── run_daily.sh
├── fund_tracker/
│   ├── cli.py          # 命令行入口
│   ├── config.py       # 配置
│   ├── fetch.py        # 数据抓取与缓存
│   ├── metrics.py      # 收益/回撤/夏普/K线指标/评分
│   ├── macro.py        # 宏观、利率、汇率、商品、新闻
│   ├── evidence.py     # 多因子证据分
│   ├── learning.py     # 预测日志、2/5/20 日回填、学习与复盘
│   ├── model_review.py # 基金 + 股票合并的每月模型表现复盘
│   ├── store.py        # SQLite 存储
│   ├── report.py       # 日报、N 日推荐、周度/月度总结生成
│   └── notify.py       # 通知
├── data/
│   ├── fund_tracker.db
│   └── cache/
├── reports/
└── tests/
```

## 风险提示

本工具只做数据整理和规则提醒，不构成投资建议。基金有风险，过往业绩不代表未来表现。评分、排名、回撤都是历史数据，不能预测未来。请根据自己的资金期限和风险承受能力做决策。

**关于“8 成把握”：** “证据分”不是胜率。没有任何公开模型能保证单只基金 1–3 年上涨概率超过 80%。80 分以上只代表质量、趋势、位置、宏观、风险五项更一致，仍可能亏损；本工具不会把证据分包装成收益承诺。
