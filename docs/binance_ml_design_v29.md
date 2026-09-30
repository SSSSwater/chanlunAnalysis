# 合约多周期预测与计划模型设计（v29）

## 资料依据

- PatchTST（Nie et al., 2023）提出 patch 化输入和 channel-independent 编码，降低长窗口注意力成本并保留局部形态：<https://arxiv.org/abs/2211.14730>
- TimesFM（Das et al., 2024）采用 patched decoder，并支持不同历史长度、预测长度和时间粒度：<https://arxiv.org/abs/2310.10688>
- Chronos（Ansari et al., 2024）把数值缩放/量化后做概率预测，说明金融场景应输出分布而非单点：<https://arxiv.org/abs/2403.07815>
- Moirai（Woo et al., 2024）强调跨频率、任意变量数量和分布差异的统一建模：<https://arxiv.org/abs/2402.02592>
- TimeMixer（Wang et al., 2024）用细粒度/粗粒度分解和多尺度混合提取短期季节性与长期趋势：<https://arxiv.org/abs/2405.14616>
- iTransformer（Liu et al., 2024）指出长回看窗口下应显式建模变量/尺度之间的相关性，而不是把所有时间点当作同质 token：<https://arxiv.org/abs/2310.06625>
- Decision Transformer（Chen et al., 2021）说明可将状态、目标回报和动作作为序列条件生成，但不能脱离数据分布盲目外推：<https://arxiv.org/abs/2106.01345>
- IQL（Kostrikov et al., 2022）和 CQL（Kumar et al., 2020）说明离线动作选择要依赖历史动作支持和保守价值估计，避免 OOD 动作的虚高评分：<https://arxiv.org/abs/2110.06169>、<https://arxiv.org/abs/2006.04779>
- Conformal prediction（Angelopoulos & Bates, 2021）提供与模型无关的预测区间校准方式，适合把不确定性纳入计划准入：<https://arxiv.org/abs/2107.07511>

## 当前落地结构

1. 四个尺度（4h/1h/15m/5m）分别经过共享输入投影和 patch 分支；粗粒度分支捕获趋势，5m 分支保留入场微结构，再与跨尺度 Transformer 表征融合。
2. 预测头输出 4/12/24 根 5m 的 q10/q50/q90 未来收益区间和方向概率。分位数采用单调参数化，避免 q10 > q50 或 q50 > q90 的不可能输出。
3. 计划头以“市场状态 + 预测分布”作为输入，直接产生方向、触发区、止损、两个目标、止盈分配、移动止损阶段和时间成本参数。
4. 动作条件 critic 继续估计具体计划的保守收益分位数、回撤、成交概率、达标概率和持仓时长；训练回放保留完整动作几何，不在预处理阶段按方向符号删样本。
5. 市场扫描先对所有候选合约完成同一套推理，再以校准分数、下行风险、时间效率和执行状态做横截面排序，只返回前 3 个可执行模型计划；WAIT 不占用返回名额。

## 计划质量评分

计划排名的主信号是保守收益下分位数，减去回撤、不确定性和持仓成本，并加入成交/达标概率。模型输出的质量字段至少包括：

- `expectedR`、`lowerTailR`、`drawdownP90`
- `fillProbability`、`targetProbability`
- `expectedDurationBars`、`timeEfficiency`
- `selectionScore`、`waitScore`、`uncertainty`

这套分数用于全市场排序和展示，不把经典策略条件重新混入 MODEL 路线。

## 执行语义

训练回放不因 LONG/SHORT 与触发价符号不一致而提前丢弃动作；触发价已经被最近的 5m K 线穿过时，只在最终执行边界把整套几何平移到下一安全侧。这样保留学习覆盖率，同时不提交已经失效的旧触发单。
