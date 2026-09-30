# 价格行为代码化契约

此文档是 `price-action-contract-v1` 的字段字典和灰度运行说明。它把四本资料中的概率性结构转换为可审计的事实、结构、计划和纪律状态；任何价格、目标或形态都不是收益保证。

交易纪律的权威来源和唯一例外定义见 [价格行为交易纪律来源与实现政策](trading-discipline-source-policy.md)。四份 PDF 优先于笔记、OpenSpec、代码注释和前端文案；A 股/美股差异只由市场执行适配器处理，不得改变价格行为纪律。

## 管线

`BarFacts -> ContextAndStructures -> TradePlan -> DisciplineState`

- `BarFacts` 只读取已完成 OHLCV，记录实体、上下影线、收盘位置、ATR/EMA、重叠、缺口、K 线标签、交易会话和数据质量。
- `ContextAndStructures` 输出确认摆动、腿、趋势/区间/过渡、结构生命周期、磁力位、量能/换手上下文和形态证据。
- `TradePlan` 输出条件触发、入场区间、结构失效、初始/动态止损、第一/扩展目标、取消条件、风险回报和硬阻断。
- `DisciplineState` 输出 `WAIT/ARMED/BLOCKED/ENTERED/MANAGING/REDUCE/EXIT`，始终标记 `automatedOrder=false`。
- `REDUCE` 只表示已有多头持仓的部分卖出：必须同时有确认的强反向冲击、结构突破/失败和后续跟随，或确认的指数防守环境；普通下行候选仍为 `HOLD`/防守观察，止损未触发时不生成卖出数量。

## 关键字段

| 路径 | 含义 |
| --- | --- |
| `priceAction.profileId/profileVersion` | 可复现的阈值配置版本 |
| `priceAction.barFacts[]` | 单根K线事实；`completed=false` 不得触发交易 |
| `priceAction.environment` | `BULL_TREND/BEAR_TREND/RANGE/TRANSITION/DATA_INSUFFICIENT`、score、confidence、evidence、conflicts、confirmationTime |
| `priceAction.structures[]` | `structureId` 与 observed/confirmed/triggered/failed/expired 时间 |
| `priceAction.setups[]` | 形态、证据 ID、缺失条件、替代情景、来源页码和状态转换 |
| `priceAction.magnetZones[]` | 支撑/阻力/区间边界/缺口/测量运动等目标候选，不保证到达 |
| `priceAction.tradePlans[]` | `planId`、触发/入场区间、结构止损、动态止损、目标、roomR、取消和阻断 |
| `trendEntryContext`（Binance 趋势计划） | 当前回撤/回测的新鲜性、EMA 距离、推进/高潮证据、末端延伸阻断和来源页码；不可用旧 EMA 接触替代 |
| `priceAction.volumeTurnover` | 相对成交量、同会话基线、成交额分位数、加速、量价一致性、换手来源/质量和异常标记 |
| `priceAction.sessionContext` | 日内会话、开盘区间、前三根、第一小时、昨日高低、午盘/11:30/收盘窗口 |
| `priceAction.discipline` | 当前纪律状态、硬门槛、事件、是否允许自动下单（恒为 false） |
| `priceAction.sourcePolicy` | 四份 PDF 权威来源、页码索引、政策版本和市场适配边界 |

持仓计划中的 `levels.reductionShares/reductionTriggerPrice/reductionReason` 和 `order.shares` 仅在上述减仓事件确认后出现。减仓比例是账户执行 profile 的可版本化参数（当前 `riskRules.reductionFraction=0.50`，风险超预算时优先按风险预算计算），不是四本书规定的固定百分比；A 股只能减少已有持仓，不能由此创建空头。
第一磁铁触发的 `REDUCE` 是分批止盈事件；强反向结构触发的 `REDUCE` 是风险暴露管理事件。两者都必须保留事件原因，不能与结构止损 `SELL` 混淆。

旧字段 `signals`、`futurePlans`、`levels` 保留兼容映射；新代码优先使用 `tradePlans`、`magnetZones` 和 `structures`。

## 硬门槛优先级

数据完整性、已收盘、结构止损、目标空间、现金/集中度/风险预算、100股整手、涨跌停、流动性、T+1 和交易时段高于任何形态评分。趋势续行还必须有当前已收盘的回撤/回测，且不能处于三推、高潮/通道过冲等末端延伸；这类状态阻断追随入场而不自动生成反向交易。量能和换手只调整证据质量，不能单独产生买卖信号。换手率缺少合法流通股本或供应商值时必须为 `UNKNOWN`。

动态止损状态按 `initial -> breakeven -> structure_trailing -> ATR_trailing` 单向推进。达到 `1R` 后先按持仓期间有利极点（多头最高价、空头最低价）计算 `2.5 ATR` 移动止损；若该候选仍在亏损侧，才使用保本保护兜底，若候选已经处于盈利侧则直接使用候选值，不应用保本阶段。每次移动记录旧价、新价、事件、时间和理由，不能因为临时重叠K线而提前收紧，也不能为了成交把结构止损移入结构内部。

## 日内纪律

分钟计划带 `mode=SCALP` 和 `sessionExpiry=SESSION_CLOSE`。盘前/隔夜量能不进入常规会话基线；缺少第一根或前三根K时开盘结构为 `DATA_INSUFFICIENT`。11:30、午盘重开和收盘前30-60分钟重新检查滑点、流动性和高周期磁力位；低周期反向信号只能 `SCALP_OR_NEEDS_REVIEW`，不能覆盖高周期强趋势。

## 书籍来源索引

- `日本蜡烛图交易技术分析.pdf`：K线事实、趋势线/通道、缺口、磁力位、反转和日内上下文，PDF 第15-338页。
- `Al Brooks 价格行为交易趋势篇.pdf`：信号/入场K、二次入场、趋势线、微型通道、尖峰-通道、趋势日和开盘起趋势，PDF 第70-405页。
- `Al Brooks 价格行为交易区间篇.pdf`：突破/回测/失败、磁力位、第一回撤、腿与两腿计数、楔形、区间/铁丝网/三角形和交易数学，PDF 第18-436页。
- `Al Brooks 价格行为交易反转篇.pdf`：高潮、楔形、扩张三角形、最终旗形、双顶底、失败、开盘反转、关键时刻和高周期管理，PDF 第37-420页。

页码是 PDF 页码索引，具体章节解释以 `notes/` 下四本详细笔记为准；阈值是可版本化的工程基线，不应声称为书中固定数值。

## 灰度与回滚

默认 `PRICE_ACTION_CONTRACT_MODE=shadow`，新契约可展示和记录但不改变现有模拟成交动作。通过回放比较旧 `priceAction` 与新 `tradePlans` 后，再按 profile 放开 `CONFIRMED` 条件计划。出现数据源单位异常、未来函数或计划字段不一致时设置 `PRICE_ACTION_CONTRACT_MODE=legacy`，旧字段读取路径继续工作，新增缓存列保留但不参与决策。
