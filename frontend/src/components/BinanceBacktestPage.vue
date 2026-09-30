<template>
  <section class="binance-backtest-page page-content" aria-labelledby="binance-backtest-page-title">
    <header class="binance-backtest-page-head">
      <div class="binance-backtest-heading">
        <span class="binance-backtest-brand-mark" aria-hidden="true"><History :size="24" /></span>
        <div>
          <span class="binance-backtest-kicker">Binance futures · local replay</span>
          <h1 id="binance-backtest-page-title">回测复盘</h1>
          <p>{{ strategySettings.strategyEngine === 'MODEL' ? `${backtestTimeframe} 时间片回放选定时序模型直接生成的交易计划，结果只读，不会创建或修改真实订单。` : `${backtestTimeframe} 时间片回放当前价格行为纪律策略，结果只读，不会创建或修改真实订单。` }}</p>
        </div>
      </div>
      <div class="binance-backtest-head-actions">
        <span class="binance-backtest-network"><i></i>主网</span>
        <div class="binance-backtest-head-action-group">
          <el-button plain @click="$emit('open-model-research')">模型研究</el-button>
          <el-button plain :icon="ArrowLeft" title="返回 Binance 市场监控" aria-label="返回 Binance 市场监控" @click="$emit('back-to-binance')">返回 Binance</el-button>
        </div>
      </div>
    </header>

    <div class="binance-backtest-workspace">
      <section class="binance-backtest-panel binance-backtest-config-panel" aria-labelledby="backtest-window-title">
        <div class="binance-backtest-panel-head">
          <div>
            <span class="binance-backtest-panel-kicker">Replay window</span>
            <h2 id="backtest-window-title">设置回测区间</h2>
          </div>
          <span class="binance-backtest-timeframe-badge">{{ backtestTimeframe }}</span>
        </div>

        <div class="binance-backtest-mode-label"><span>区间方式</span><small>两种方式都只读取本地历史库</small></div>
        <el-radio-group v-model="backtestMode" class="binance-backtest-mode-switch" aria-label="回测区间方式">
          <el-radio-button value="RANDOM">随机 N 天</el-radio-button>
          <el-radio-button value="RANGE">指定时间区间</el-radio-button>
        </el-radio-group>

        <div v-if="backtestMode === 'RANDOM'" class="binance-backtest-option-block">
          <label class="binance-backtest-field-label" for="backtest-random-days">随机天数</label>
          <div class="binance-backtest-days-control">
            <el-input-number id="backtest-random-days" v-model="randomDays" :min="RANDOM_DAYS_MIN" :max="RANDOM_DAYS_MAX" :precision="0" :step="1" controls-position="right" />
            <strong>天</strong>
          </div>
          <small class="binance-backtest-field-help">后端会在满足宏观周期预热的可用窗口内随机选择起点，实际起止时间以回测结果为准。</small>
        </div>

        <div v-else class="binance-backtest-option-block binance-backtest-range-fields">
          <div class="binance-backtest-date-field">
            <label class="binance-backtest-field-label" for="backtest-start-time">开始时间</label>
            <el-date-picker
              id="backtest-start-time"
              v-model="backtestForm.startTime"
              type="datetime"
              format="YYYY-MM-DD HH:mm"
              :clearable="false"
              :editable="false"
              :teleported="true"
              placeholder="选择开始时间"
            />
          </div>
          <div class="binance-backtest-date-field">
            <label class="binance-backtest-field-label" for="backtest-end-time">结束时间</label>
            <el-date-picker
              id="backtest-end-time"
              v-model="backtestForm.endTime"
              type="datetime"
              format="YYYY-MM-DD HH:mm"
              :clearable="false"
              :editable="false"
              :teleported="true"
              placeholder="选择结束时间"
            />
          </div>
          <p v-if="rangeValidationMessage" class="binance-backtest-validation" role="alert">{{ rangeValidationMessage }}</p>
          <p v-else class="binance-backtest-field-help">按浏览器本地时间选择，提交时转换为 UTC；时间必须落在 {{ backtestTimeframe }} 边界。</p>
        </div>

        <div class="binance-backtest-window-summary">
          <span>本次请求</span>
          <strong>{{ requestedWindowLabel }}</strong>
          <small>{{ backtestMode === 'RANDOM' ? `随机 ${randomDays} 天 · 可用范围 ${formatBacktestDateTime(EARLIEST_START_TIME)} 至 ${formatBacktestDateTime(FIXED_HISTORY_END_TIME)}` : `指定区间 · ${rangeDurationLabel}` }}</small>
        </div>
        <div v-if="strategySettings.strategyEngine !== 'MODEL'" class="binance-backtest-model-mode" :class="{ 'is-active': modelEvaluationMode === 'COMPARE' }">
          <div><span>策略评估</span><small>模型对照只使用未参与训练的留出期</small></div>
          <el-radio-group v-model="backtestParameters.modelEvaluationMode" class="binance-backtest-mode-switch" aria-label="策略评估模式">
            <el-radio-button value="OFF">纪律基线</el-radio-button>
            <el-radio-button value="COMPARE" :disabled="!modelEvaluationAvailable">模型对照</el-radio-button>
          </el-radio-group>
          <p v-if="modelEvaluationMode === 'COMPARE'" :class="['binance-backtest-model-note', { 'is-error': !modelEvaluationAvailable }]">
            <strong>{{ modelEvaluationAvailable ? `留出期 ${modelEvaluationRangeLabel}` : '模型留出期不可用' }}</strong>
            <span>{{ modelEvaluationMessage }}</span>
          </p>
          <p v-else-if="backtestModelStatusError" class="binance-backtest-model-note is-error"><span>模型状态读取失败：{{ backtestModelStatusError }}</span></p>
        </div>
        <el-button type="primary" class="binance-backtest-start-button" :icon="History" :loading="backtestLoading || backtestRunning" :disabled="!canStartBacktest" @click="startCurrentStrategyBacktest">开始回测</el-button>
        <p class="binance-backtest-safety-note">回测使用当前账户保存的策略设置快照；不会读取实时账户余额或提交交易所订单。历史回放不读取 1m，无法确认同根 K 线先后时按方向不利路径处理。</p>
      </section>

      <div class="binance-backtest-side-stack">
        <section class="binance-backtest-panel binance-backtest-history-panel" aria-labelledby="backtest-history-title">
          <div class="binance-backtest-panel-head">
            <div>
              <span class="binance-backtest-panel-kicker">History corpus</span>
              <h2 id="backtest-history-title">历史数据准备</h2>
            </div>
            <span class="binance-backtest-history-percent">{{ backtestHistoryCompletenessLabel }}</span>
          </div>
          <div class="binance-backtest-history-meter" :class="{ 'is-loading': backtestHistoryStatusLoading, 'is-stale': backtestHistoryStatusError }" role="status" aria-live="polite" :aria-label="backtestHistoryCompletenessTitle" :title="backtestHistoryCompletenessTitle">
            <div class="binance-backtest-history-meter-head"><span>固定历史库完整度</span><strong>{{ backtestHistoryCompletenessLabel }}</strong></div>
            <el-progress :percentage="backtestHistoryCompletenessPercent" :show-text="false" :stroke-width="7" />
          </div>
          <div class="binance-backtest-history-range">
            <span>可用历史范围</span>
            <strong>{{ formatBacktestDateTime(FIXED_HISTORY_START_TIME) }} 至 {{ formatBacktestDateTime(FIXED_HISTORY_END_TIME) }}</strong>
          <small>{{ strategySettings.strategyEngine === 'MODEL' ? '直接计划模型和训练历史固定使用 4h / 1h / 15m / 5m，并以 5m 为时间锚点和执行粒度；历史回放不读取 1m，最小同根K线按最坏路径处理。' : (strategySettings.strategyMode === 'SHORT_TERM' ? '短线执行需要 4h / 1h / 15m / 5m；历史回放不读取 1m，最小同根K线按最坏路径处理。' : '中线宏观分析使用 4h / 1h / 15m，固定历史准备同时补齐 5m；历史回放不读取 1m。') }}</small>
          </div>
          <div v-if="historyFillJob && historyFillRunning" class="binance-backtest-history-job" role="status" aria-live="polite">
            <div><span>历史补全</span><strong>{{ historyFillProgress.phase || 'QUEUED' }}</strong><small>{{ historyFillProgress.message || '正在准备历史数据。' }}</small></div>
            <div><span>进度</span><strong>{{ historyFillProgress.completed || 0 }} / {{ historyFillProgress.total || '--' }}</strong><small>{{ historyFillProgress.currentSymbol || '等待数据' }}</small></div>
            <el-progress :percentage="historyFillProgressPercent" :show-text="false" :stroke-width="6" />
          </div>
          <div v-if="historyFillJob && !historyFillRunning && historyFillJob.status !== 'COMPLETED'" class="binance-backtest-history-error" role="alert">{{ historyFillJob.error || historyFillJob.progress?.message || '历史补全未完成' }}</div>
          <div class="binance-backtest-history-actions">
            <small>{{ backtestHistoryStatusError ? `读取失败：${backtestHistoryStatusError}；保留最近成功数据` : '缺口只会请求尚未保存的区间。' }}</small>
            <el-button plain :icon="RefreshCw" :loading="historyFillLoading || historyFillRunning" :disabled="historyFillRunning || backtestRunning || backtestLoading" @click="startBacktestHistoryFill">尝试补全</el-button>
          </div>
        </section>

        <section class="binance-backtest-panel binance-backtest-parameters-panel" aria-labelledby="backtest-parameters-title">
          <div class="binance-backtest-panel-head">
            <div>
              <span class="binance-backtest-panel-kicker">Replay parameters</span>
              <h2 id="backtest-parameters-title">回测参数配置</h2>
            </div>
            <el-button link type="info" :icon="RefreshCw" :disabled="backtestRunning || backtestLoading" @click="resetBacktestParameters">恢复默认</el-button>
          </div>
          <p class="binance-backtest-parameter-note">本次回测会使用这里的参数快照；登录后默认值与 Binance 设置同步，修改只影响本次回测。</p>

          <div class="binance-backtest-parameter-group">
            <div class="binance-backtest-parameter-group-head"><span>执行参数</span><small>控制资金、扫描范围与杠杆</small></div>
            <div class="binance-backtest-parameter-grid">
              <div class="binance-backtest-parameter-subgroup-heading">资金与仓位</div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-initial-balance">初始资金</label>
                <div class="binance-backtest-input-with-suffix"><el-input-number id="backtest-initial-balance" v-model="backtestParameters.initialBalance" :min="0.000001" :max="1000000000" :precision="6" :step="1" controls-position="right" /><span>USDT</span></div>
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-position-margin-fraction">单仓位保证金比例</label>
                <div class="binance-backtest-input-with-suffix"><el-input-number id="backtest-position-margin-fraction" v-model="backtestParameters.positionMarginFraction" :min="0.000001" :max="1" :precision="4" :step="0.01" controls-position="right" /><span>（0.25 = 25%）</span></div>
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-max-managed-positions">管理仓位上限</label>
                <el-input-number id="backtest-max-managed-positions" v-model="backtestParameters.maxManagedPositions" :min="1" :max="20" :precision="0" :step="1" controls-position="right" />
              </div>
              <div class="binance-backtest-parameter-subgroup-heading">扫描与杠杆</div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-scan-limit">扫描合约数量</label>
                <el-input-number id="backtest-scan-limit" v-model="backtestParameters.scanLimit" :min="1" :max="1000" :precision="0" :step="1" controls-position="right" />
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-max-leverage">最高杠杆</label>
                <div class="binance-backtest-input-with-suffix"><el-input-number id="backtest-max-leverage" v-model="backtestParameters.maxLeverage" :min="1" :max="20" :precision="0" :step="1" controls-position="right" /><span>x（最高 20x）</span></div>
              </div>
              <div class="binance-backtest-parameter-field binance-backtest-parameter-field-wide">
                <label>杠杆设立规则</label>
                <el-radio-group v-model="backtestParameters.leverageMode" class="binance-backtest-leverage-switch" aria-label="杠杆设立规则">
                  <el-radio-button value="RISK_BUDGET">按风险预算</el-radio-button>
                  <el-radio-button value="FIXED">固定杠杆</el-radio-button>
                </el-radio-group>
                <small>按风险预算会根据结构止损距离向下取整；固定杠杆仍会按风险预算缩小保证金。</small>
              </div>
              <div v-if="backtestParameters.leverageMode === 'FIXED'" class="binance-backtest-parameter-field">
                <label for="backtest-fixed-leverage">固定杠杆</label>
                <div class="binance-backtest-input-with-suffix"><el-input-number id="backtest-fixed-leverage" v-model="backtestParameters.leverage" :min="1" :max="20" :precision="0" :step="1" controls-position="right" /><span>x</span></div>
              </div>
            </div>
          </div>

              <div class="binance-backtest-parameter-group">
                <div class="binance-backtest-parameter-group-head"><span>{{ directModelSelected ? '模型与风控' : '策略参数' }}</span><small>{{ directModelSelected ? '模型点位由选定检查点直接生成；此处只保留执行安全与账户风控。' : '与策略设置页相同的当前默认值' }}</small></div>
                <div class="binance-backtest-parameter-grid">
                  <div class="binance-backtest-parameter-subgroup-heading">{{ directModelSelected ? '模型路径' : '策略路径' }}</div>
                  <div class="binance-backtest-parameter-field binance-backtest-parameter-field-wide">
                    <label>策略引擎</label>
                    <el-radio-group v-model="strategySettings.strategyEngine" class="binance-backtest-leverage-switch" aria-label="回测策略引擎">
                      <el-radio-button value="CLASSIC">经典策略</el-radio-button>
                      <el-radio-button value="MODEL">时序模型</el-radio-button>
                    </el-radio-group>
                    <small class="binance-backtest-field-help">模型直接从 4h / 1h / 15m / 5m 已收盘序列生成完整计划；仅保留执行安全校验。模型不可用时返回 WAIT，不回退经典策略。</small>
                  </div>
                  <div v-if="strategySettings.strategyEngine === 'MODEL'" class="binance-backtest-parameter-field binance-backtest-parameter-field-wide">
                    <label>时序模型分支</label>
                    <el-radio-group v-model="strategySettings.modelBranch" class="binance-backtest-leverage-switch" aria-label="回测时序模型分支">
                      <el-radio-button value="BEST">验证最佳</el-radio-button>
                      <el-radio-button value="STABLE">稳定末期</el-radio-button>
                    </el-radio-group>
                    <small class="binance-backtest-field-help">BEST 使用验证期表现最佳 checkpoint；STABLE 使用训练末十轮中最接近稳定中位表现的 checkpoint。</small>
                  </div>
                  <template v-if="strategySettings.strategyEngine !== 'MODEL'">
                  <div class="binance-backtest-parameter-field binance-backtest-parameter-field-wide">
                    <label>策略模式</label>
                <el-radio-group v-model="strategySettings.strategyMode" class="binance-backtest-leverage-switch" aria-label="回测策略模式">
                  <el-radio-button value="MIDLINE">中线模式</el-radio-button>
                  <el-radio-button value="SHORT_TERM">短线模式</el-radio-button>
                </el-radio-group>
                    <small>{{ strategySettings.strategyEngine === 'MODEL' ? '时序模型在中线/短线模式都使用 4h / 1h / 15m / 5m，5m 是时间锚点与执行粒度；下方经典策略参数不参与模型计划。' : '中线使用 4h → 1h → 15m；短线保留 4h 隐藏方向否决，由 1h → 15m → 5m 执行，仍须完整通过价格行为纪律。' }}</small>
              </div>
               <div class="binance-backtest-parameter-field binance-backtest-parameter-field-wide">
                 <label>点位策略路线</label>
                <el-radio-group v-model="strategySettings.levelStrategy" class="binance-backtest-leverage-switch" aria-label="回测点位策略路线">
                  <el-radio-button value="STRUCTURE_EXTREME">结构极值</el-radio-button>
                  <el-radio-button value="CONFIRMED_PLATFORM">确认平台/区域</el-radio-button>
                 </el-radio-group>
               </div>
                  </template>
               <div class="binance-backtest-parameter-subgroup-heading">账户风控</div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-max-account-loss-ratio">账户最大亏损比</label>
                <div class="binance-backtest-input-with-suffix"><el-input-number id="backtest-max-account-loss-ratio" v-model="strategySettings.maxAccountLossRatio" :min="0.01" :max="100" :precision="2" :step="0.5" controls-position="right" /><span>%</span></div>
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-max-portfolio-risk-ratio">组合结构风险上限</label>
                <div class="binance-backtest-input-with-suffix"><el-input-number id="backtest-max-portfolio-risk-ratio" v-model="strategySettings.maxPortfolioRiskRatio" :min="0.01" :max="100" :precision="2" :step="0.5" controls-position="right" /><span>%</span></div>
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-max-same-side-positions">同向持仓上限</label>
                <el-input-number id="backtest-max-same-side-positions" v-model="strategySettings.maxSameSidePositions" :min="1" :max="20" :precision="0" :step="1" controls-position="right" />
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-daily-loss-limit-ratio">单日风险上限</label>
                <div class="binance-backtest-input-with-suffix"><el-input-number id="backtest-daily-loss-limit-ratio" v-model="strategySettings.dailyLossLimitRatio" :min="0.01" :max="100" :precision="2" :step="0.5" controls-position="right" /><span>%</span></div>
              </div>
                  <template v-if="strategySettings.strategyEngine !== 'MODEL'">
              <div class="binance-backtest-parameter-subgroup-heading">环境与目标空间</div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-range-edge-fraction">区间边缘比例</label>
                <el-input-number id="backtest-range-edge-fraction" v-model="strategySettings.rangeEdgeFraction" :min="0.05" :max="0.49" :precision="2" :step="0.01" controls-position="right" />
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-range-minimum-target-r">区间最小目标 R</label>
                <div class="binance-backtest-input-with-suffix"><el-input-number id="backtest-range-minimum-target-r" v-model="strategySettings.rangeMinimumTargetR" :min="0.5" :max="10" :precision="2" :step="0.25" controls-position="right" /><span>R</span></div>
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-trend-minimum-target-r">趋势最小目标 R</label>
                <div class="binance-backtest-input-with-suffix"><el-input-number id="backtest-trend-minimum-target-r" v-model="strategySettings.trendMinimumTargetR" :min="0.5" :max="10" :precision="2" :step="0.25" controls-position="right" /><span>R</span></div>
              </div>
              <div class="binance-backtest-parameter-subgroup-heading">入场确认与失败退出</div>
              <div class="binance-backtest-parameter-field binance-backtest-parameter-field-wide">
                <label>入场确认方式</label>
                <el-radio-group v-model="strategySettings.entryConfirmationMode" class="binance-backtest-leverage-switch" aria-label="回测入场确认方式">
                  <el-radio-button value="RETEST_REQUIRED">等待 5m 回测</el-radio-button>
                  <el-radio-button value="TRIGGER_ONLY">仅触发</el-radio-button>
                </el-radio-group>
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-entry-confirmation-expiry-bars">确认有效 K 线数</label>
                <el-input-number id="backtest-entry-confirmation-expiry-bars" v-model="strategySettings.entryConfirmationExpiryBars" :min="1" :max="8" :precision="0" :step="1" controls-position="right" />
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-entry-failure-exit-bars">早期失败复核 K 线数</label>
                <el-input-number id="backtest-entry-failure-exit-bars" v-model="strategySettings.entryFailureExitBars" :min="1" :max="8" :precision="0" :step="1" controls-position="right" />
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-entry-failure-body-atr-multiplier">强反向实体 ATR 倍数</label>
                <el-input-number id="backtest-entry-failure-body-atr-multiplier" v-model="strategySettings.entryFailureBodyAtrMultiplier" :min="0.1" :max="5" :precision="2" :step="0.1" controls-position="right" />
              </div>
              <div class="binance-backtest-parameter-subgroup-heading">止损管理</div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-moving-stop-activation-r">移动止损启动 R</label>
                <div class="binance-backtest-input-with-suffix"><el-input-number id="backtest-moving-stop-activation-r" v-model="strategySettings.movingStopActivationR" :min="0.01" :max="20" :precision="2" :step="0.1" controls-position="right" /><span>R</span></div>
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-trailing-atr-multiplier">极点 ATR 移动止损倍数</label>
                <el-input-number id="backtest-trailing-atr-multiplier" v-model="strategySettings.trailingAtrMultiplier" :min="0.01" :max="20" :precision="2" :step="0.1" controls-position="right" />
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-structure-stop-atr-multiplier">结构止损 ATR 缓冲</label>
                <el-input-number id="backtest-structure-stop-atr-multiplier" v-model="strategySettings.structureStopAtrMultiplier" :min="0" :max="10" :precision="2" :step="0.01" controls-position="right" />
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-trigger-zone-stop-buffer-atr-multiplier">触发区防假突破缓冲</label>
                <el-input-number id="backtest-trigger-zone-stop-buffer-atr-multiplier" v-model="strategySettings.triggerZoneStopBufferAtrMultiplier" :min="0.1" :max="10" :precision="2" :step="0.1" controls-position="right" />
                <small>止损与触发区防守侧至少间隔 max(ATR × 倍数，触发区高度 × 25%)；默认 0.5 ATR。</small>
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-breakeven-buffer-atr-multiplier">保本缓冲 ATR 倍数</label>
                <el-input-number id="backtest-breakeven-buffer-atr-multiplier" v-model="strategySettings.breakevenBufferAtrMultiplier" :min="0" :max="10" :precision="2" :step="0.01" controls-position="right" />
              </div>
              <div class="binance-backtest-parameter-subgroup-heading">止盈与目标分配</div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-near-term-minimum-target-r">近端止盈最低 R</label>
                <div class="binance-backtest-input-with-suffix"><el-input-number id="backtest-near-term-minimum-target-r" v-model="strategySettings.nearTermMinimumTargetR" :min="0.05" :max="5" :precision="2" :step="0.05" controls-position="right" /><span>R</span></div>
                <small>近端结构磁力位距离触发价至少达到此 R；默认 0.5R。</small>
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-protective-take-profit-ratio">近端保护止盈累计比例</label>
                <div class="binance-backtest-input-with-suffix"><el-input-number id="backtest-protective-take-profit-ratio" v-model="strategySettings.protectiveTakeProfitRatio" :min="1" :max="98" :precision="1" :step="1" controls-position="right" /><span>%</span></div>
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-first-take-profit-ratio">第一止盈累计比例</label>
                <div class="binance-backtest-input-with-suffix"><el-input-number id="backtest-first-take-profit-ratio" v-model="strategySettings.firstTakeProfitRatio" :min="1" :max="99" :precision="1" :step="1" controls-position="right" /><span>%</span></div>
              </div>
              <div class="binance-backtest-parameter-field">
                <label for="backtest-second-take-profit-ratio">第二止盈累计比例</label>
                <div class="binance-backtest-input-with-suffix"><el-input-number id="backtest-second-take-profit-ratio" v-model="strategySettings.secondTakeProfitRatio" :min="1" :max="99" :precision="1" :step="1" controls-position="right" /><span>%</span></div>
              </div>
                  </template>
            </div>
          </div>
          <p v-if="parameterValidationMessage" class="binance-backtest-validation" role="alert">{{ parameterValidationMessage }}</p>
        </section>
      </div>
    </div>

    <section v-if="backtestJob" class="binance-backtest-panel binance-backtest-result-panel" aria-labelledby="backtest-result-title">
      <div class="binance-backtest-panel-head binance-backtest-result-head">
        <div>
          <span class="binance-backtest-panel-kicker">Replay result</span>
          <h2 id="backtest-result-title">回测结果</h2>
          <small v-if="backtestJob.backtestOptions || backtestResult?.backtestOptions">{{ backtestModeLabel(backtestJob.backtestOptions || backtestResult.backtestOptions) }} · 时间粒度 {{ backtestResult?.timeframe || (strategySettings.strategyEngine === 'MODEL' || backtestJob.backtestOptions?.strategyMode === 'SHORT_TERM' ? '5m' : '15m') }}</small>
        </div>
        <span :class="['binance-backtest-state', `is-${String(backtestJob.status || 'QUEUED').toLowerCase()}`]">{{ backtestStatusLabel }}</span>
      </div>

      <div v-if="backtestRunning || backtestLoading" class="binance-backtest-progress" role="status" aria-live="polite">
        <div class="binance-backtest-progress-head">
          <div><span>当前阶段</span><strong>{{ backtestProgress.phase || 'QUEUED' }}</strong><small>{{ backtestProgress.message || '正在准备历史数据。' }}</small></div>
          <div><span>进度</span><strong>{{ backtestProgress.completed || 0 }} / {{ backtestProgress.total || '--' }}</strong><small>{{ backtestProgress.currentSymbol || (backtestProgress.historyCached ? '已复用历史缓存' : '等待数据') }}</small></div>
        </div>
        <el-progress :percentage="backtestProgressPercent" :show-text="false" :stroke-width="8" />
        <p v-if="backtestJob.connectionError" class="binance-backtest-connection-warning">连接提示：{{ backtestJob.connectionError }}；将继续读取任务状态</p>
      </div>

      <div v-if="backtestResult" class="binance-backtest-result">
        <div v-if="backtestResult.isPartial" class="binance-backtest-live-note" role="status" aria-live="polite">
          <span>回测结果持续更新中</span>
          <strong>已遍历 {{ backtestResult.processedSlices || 0 }} / {{ backtestResult.totalSlices || backtestProgress.total || '--' }} 个 {{ backtestResult.timeframe || (strategySettings.strategyEngine === 'MODEL' ? '5m' : '15m') }} 时间片</strong>
          <small v-if="backtestResult.asOfTime">已更新至 {{ formatBacktestDateTime(backtestResult.asOfTime) }}；下方已显示当前已完成的时间片</small>
        </div>
        <div class="binance-backtest-summary">
          <div class="binance-backtest-period"><span>实际回测区间</span><strong>{{ formatBacktestDateTime(backtestResult.startTime) }}</strong><small>至 {{ formatBacktestDateTime(backtestResult.endTime) }} · {{ backtestResult.marketCount || '--' }} 个合约</small><small v-if="backtestResult.strategySettings?.strategyEngine === 'MODEL'">时序模型 · {{ backtestResult.timeframe || '5m' }} · 直接生成计划</small><small v-else-if="backtestResult.strategySettings?.levelStrategy">{{ backtestResult.strategySettings?.strategyMode === 'SHORT_TERM' ? '短线模式' : '中线模式' }} · {{ backtestResult.timeframe || '15m' }} · 点位路线 · {{ strategyLevelLabel(backtestResult.strategySettings.levelStrategy) }}</small></div>
          <div><span>初始资金</span><strong>{{ formatCrypto(backtestResult.initialBalance) }} USDT</strong><small>{{ backtestResult.isPartial ? '当前可用余额' : '可用余额' }} {{ formatCrypto(backtestResult.finalAvailableBalance) }}</small></div>
          <div><span>{{ backtestResult.isPartial ? '当前权益' : '最终权益' }}</span><strong>{{ formatCrypto(backtestResult.finalEquity) }} USDT</strong><small>未平仓 {{ backtestResult.summary?.openPositions || 0 }} 个</small></div>
          <div><span>总盈亏</span><strong :class="pnlDisplayClass(backtestResult.totalPnl)">{{ formatSignedNumber(backtestResult.totalPnl) }} USDT</strong><small :class="pnlDisplayClass(backtestResult.totalPnlPercent)">{{ formatSignedPercent(backtestResult.totalPnlPercent) }}</small></div>
        </div>

        <div class="binance-backtest-stats">
          <span>开仓 {{ backtestResult.summary?.openedCount || 0 }}</span>
          <span>止损 {{ backtestResult.summary?.stoppedCount || 0 }}</span>
          <span>近端保护 {{ backtestResult.summary?.protectiveTargetCount || 0 }}</span>
          <span>第一止盈 {{ backtestResult.summary?.firstTargetCount || 0 }}</span>
          <span>第二止盈 {{ backtestResult.summary?.secondTargetCount || 0 }}</span>
          <span>1m 顺序核对 {{ backtestResult.summary?.oneMinuteChecks || 0 }}（已停用）</span>
          <span>同根K最坏路径 {{ backtestResult.summary?.conservativeIntrabarPaths || 0 }}</span>
          <span v-if="backtestResult.summary?.oneMinuteFallbacks">1m 保守回退 {{ backtestResult.summary.oneMinuteFallbacks }}</span>
        </div>

        <section v-if="modelEvaluationResult && !backtestResult.isPartial" class="binance-backtest-model-evaluation" aria-labelledby="backtest-model-evaluation-title">
          <header class="binance-backtest-model-evaluation-head">
            <div>
              <span>策略级模型评估</span>
              <h3 id="backtest-model-evaluation-title">纪律基线与模型过滤对照</h3>
              <small>{{ modelEvaluationResult.model?.modelVersion || '本地模型' }} · {{ formatBacktestDateTime(modelEvaluationResult.model?.testStartTime) }} 至 {{ formatBacktestDateTime(modelEvaluationResult.model?.testEndTime) }}</small>
            </div>
            <strong>{{ modelEvaluationResult.model?.architecture || '条件计划模型' }}</strong>
          </header>

          <div class="binance-backtest-evaluation-table" role="table" aria-label="纪律基线与模型过滤回测指标对照">
            <div class="binance-backtest-evaluation-row is-head" role="row">
              <span role="columnheader">回放指标</span>
              <span role="columnheader">纪律基线</span>
              <span role="columnheader">模型过滤</span>
              <span role="columnheader">模型相对基线</span>
            </div>
            <div v-for="metric in MODEL_EVALUATION_METRICS" :key="metric.key" class="binance-backtest-evaluation-row" role="row">
              <span role="cell">{{ metric.label }}</span>
              <strong role="cell" :class="pnlDisplayClass(modelEvaluationResult.baseline?.[metric.key])">{{ formatModelEvaluationMetric(modelEvaluationResult.baseline?.[metric.key], metric) }}</strong>
              <strong role="cell" :class="pnlDisplayClass(modelEvaluationResult.modelFiltered?.[metric.key])">{{ formatModelEvaluationMetric(modelEvaluationResult.modelFiltered?.[metric.key], metric) }}</strong>
              <em role="cell" :class="modelEvaluationDeltaClass(modelEvaluationDeltaValue(modelEvaluationResult, metric.key), metric)">{{ formatModelEvaluationDelta(modelEvaluationDeltaValue(modelEvaluationResult, metric.key), metric) }}</em>
            </div>
          </div>
          <p class="binance-backtest-evaluation-footnote">第一止盈先达率只比较第一止盈与最终有效结构/移动止损，不把近端保护止盈计为胜；已实现盈利率则按整笔交易最终盈亏统计。未平仓头寸仍会进入最终权益和最大回撤。</p>

          <div class="binance-backtest-model-audit">
            <div><span>纪律候选</span><strong>{{ modelEvaluationResult.selection?.disciplineEligibleCount || 0 }}</strong></div>
            <div><span>模型已评估</span><strong>{{ modelEvaluationResult.selection?.modelEvaluatedCount || 0 }}</strong></div>
            <div><span>模型保留</span><strong>{{ modelEvaluationResult.selection?.modelAcceptedCount || 0 }}</strong></div>
            <div><span>模型剔除</span><strong>{{ modelEvaluationResult.selection?.modelRejectedCount || 0 }}</strong></div>
          </div>
          <div v-if="modelFilterRejectedReasons.length" class="binance-backtest-model-rejections" aria-label="模型筛选剔除原因">
            <span v-for="([reason, count]) in modelFilterRejectedReasons" :key="reason">{{ modelFilterReasonLabel(reason) }} {{ count }}</span>
          </div>
          <p v-if="modelEvaluationResult.model?.selectionRule" class="binance-backtest-evaluation-footnote">{{ modelEvaluationResult.model.selectionRule }}</p>
        </section>

        <div class="binance-backtest-record-note" v-if="backtestResult.recordFile"><span>本地复盘记录</span><code>{{ backtestResult.recordFile }}</code></div>

        <div class="binance-backtest-timeline" aria-label="回测时间线">
          <article v-for="row in backtestResult.timeline || []" :key="row.time" class="binance-backtest-row">
            <header class="binance-backtest-row-head">
              <div class="binance-backtest-row-title">
                <strong>{{ formatBacktestDateTime(row.time) }}</strong>
                <span>{{ backtestVisibleActions(row).length ? `${backtestVisibleActions(row).length} 项操作` : '无操作' }}<template v-if="backtestRowPositions(row).length"> · {{ backtestRowPositions(row).length }} 个持仓</template></span>
              </div>
              <button v-if="backtestHasDetails(row)" type="button" class="binance-backtest-row-toggle" :aria-expanded="isBacktestRowExpanded(row)" @click="toggleBacktestRow(row)">{{ isBacktestRowExpanded(row) ? '收起详情' : '展开详情' }}</button>
            </header>
            <div class="binance-backtest-row-metrics">
              <div><span>持仓数量</span><strong>{{ row.positionCount ?? row.positions?.length ?? 0 }}</strong></div>
              <div><span>可用余额</span><strong>{{ formatCrypto(row.availableBalance) }}</strong></div>
              <div><span>占用保证金</span><strong>{{ formatCrypto(row.usedMargin) }}</strong></div>
              <div><span>当前权益</span><strong>{{ formatCrypto(row.equity) }}</strong></div>
              <div><span>总盈亏</span><strong :class="pnlDisplayClass(row.totalPnl)">{{ formatSignedNumber(row.totalPnl) }}</strong></div>
            </div>
            <div v-if="backtestRowPositions(row).length" class="binance-backtest-position-tags" aria-label="当前持仓">
              <span v-for="position in backtestRowPositions(row)" :key="`${position.symbol}-${position.side}`" class="binance-backtest-position-tag" :title="`${position.symbol} · ${position.side === 'LONG' ? '做多' : '做空'}`"><strong>{{ position.symbol }}</strong><em :class="pnlDisplayClass(position.pnlPercent)">{{ formatBacktestPositionPnl(position) }}</em></span>
            </div>
            <div v-if="isBacktestRowExpanded(row) && backtestHasDetails(row)" class="binance-backtest-row-details">
              <div v-if="backtestVisibleActions(row).length" class="binance-backtest-actions">
                <div v-for="(action, index) in backtestVisibleActions(row)" :key="`${action.type}-${action.symbol}-${index}`" :class="['binance-backtest-action', `is-${String(action.type || '').toLowerCase()}`]">
                  <strong>{{ backtestActionLabel(action.type) }}</strong>
                  <em v-if="action.side" :class="directionClass(action.side)">{{ action.side === 'LONG' ? '做多' : '做空' }}</em>
                  <span v-if="action.symbol">{{ action.symbol }}</span>
                  <b v-if="action.price !== null && action.price !== undefined">{{ formatPrice(action.price) }}</b>
                  <small v-if="action.quantity !== null && action.quantity !== undefined">数量 {{ formatCrypto(action.quantity) }}</small>
                  <small v-if="action.pnl !== null && action.pnl !== undefined" :class="pnlDisplayClass(action.pnl)">盈亏 {{ formatSignedNumber(action.pnl) }}</small>
                  <p v-if="action.note">{{ action.note }}</p>
                </div>
              </div>
              <div v-if="backtestRowPositions(row).length" class="binance-backtest-holdings">
                <div v-for="position in backtestRowPositions(row)" :key="`${position.symbol}-${position.side}`" class="binance-backtest-holding">
                  <div><strong>{{ position.symbol }}</strong><em :class="directionClass(position.side)">{{ position.side === 'LONG' ? '做多' : '做空' }}</em><small>{{ position.leverage }}x · 保证金 {{ formatCrypto(position.margin) }}</small></div>
                  <div><span>数量 / 成本</span><strong>{{ formatCrypto(position.quantity) }} @ {{ formatPrice(position.entryPrice) }}</strong></div>
                  <div><span>最新价</span><strong>{{ formatPrice(position.currentPrice) }}</strong></div>
                  <div><span>持仓盈亏</span><strong :class="pnlDisplayClass(position.totalPnl)">{{ formatSignedNumber(position.totalPnl) }}</strong></div>
                  <div><span>{{ backtestStopLabel(position.activeStopSource) }}</span><strong>{{ formatPrice(position.activeStop) }}</strong><small>{{ backtestStageLabel(position.stopManagementStage) }}</small></div>
                </div>
              </div>
            </div>
          </article>
        </div>
      </div>

      <div v-else-if="backtestHistoryIncomplete || backtestRateLimited" class="binance-backtest-history-incomplete" role="status">
          <div><span>固定历史库</span><strong>{{ formatBacktestDateTime(FIXED_HISTORY_START_TIME) }} 至 {{ formatBacktestDateTime(FIXED_HISTORY_END_TIME) }}</strong><small>{{ backtestRateLimited ? '交易所已限制请求，本次回测未进入回放；已保存的历史数据可稍后继续补全。' : `待补区间 ${backtestHistoryPreparationStatus?.missingRangeCount ?? backtestProgress.historyMissingRanges ?? 0} 个 · ${backtestHistoryPreparationStatus?.historyRange?.universeCount || '--'} 个合约 · 周期 4h / 1h / 15m / 5m` }}</small></div>
        <el-button type="primary" :icon="RefreshCw" :loading="historyFillLoading || historyFillRunning" :disabled="historyFillRunning" @click="startBacktestHistoryFill">尝试补全</el-button>
      </div>

      <div v-else-if="!backtestRunning && !backtestLoading" class="binance-backtest-empty is-error"><Activity :size="19" /><span>{{ backtestJob.error || '回测结果不可用' }}</span></div>
    </section>

    <section v-else class="binance-backtest-panel binance-backtest-empty-state" aria-label="等待回测">
      <History :size="26" />
      <div><strong>选择区间后开始回测</strong><span>结果会在本页持续更新，并可展开每个执行时间片查看扫描、开仓、止盈和止损管理细节。</span></div>
    </section>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import ElMessage from 'element-plus/es/components/message/index.mjs'
import { Activity, ArrowLeft, History, RefreshCw } from 'lucide-vue-next'
import { fetchBinanceFuturesBacktestHistoryFill, fetchBinanceFuturesBacktestHistoryStatus, fetchBinanceFuturesModelStatus, fetchBinanceFuturesStrategyBacktest, fetchBinanceStrategySettings, startBinanceFuturesBacktestHistoryFill, startBinanceFuturesStrategyBacktest } from '../api'

defineEmits(['back-to-binance', 'open-model-research'])
const props = defineProps({
  currentUser: {
    type: Object,
    default: null,
  },
})

const NETWORK = 'mainnet'
const MODEL_SELECTION_STORAGE_KEY = 'binance.model.selection'
const MIDLINE_BACKTEST_INTERVAL_MS = 15 * 60 * 1000
const SHORT_TERM_BACKTEST_INTERVAL_MS = 5 * 60 * 1000
const BACKTEST_DAY_MS = 24 * 60 * 60 * 1000
const FIXED_HISTORY_START_TIME = Date.UTC(2026, 4, 31, 16)
const FIXED_HISTORY_END_TIME = Date.UTC(2026, 8, 1, 0)
const EARLIEST_START_TIME = Date.UTC(2026, 5, 14, 0)
const DEFAULT_BACKTEST_START_TIME = Date.UTC(2026, 7, 2, 0)
const RANDOM_DAYS_MIN = 1
const RANDOM_DAYS_MAX = 30
const BACKTEST_JOB_STORAGE_KEY = 'binance-backtest-job-id'
const MODEL_EVALUATION_METRICS = Object.freeze([
  { key: 'netPnl', label: '净盈亏', kind: 'amount', improvement: 'higher' },
  { key: 'totalReturnPercent', label: '总回报率', kind: 'percent', improvement: 'higher' },
  { key: 'maximumDrawdown', label: '最大回撤', kind: 'amount', improvement: 'lower' },
  { key: 'maximumDrawdownPercent', label: '最大回撤率', kind: 'percent', improvement: 'lower' },
  { key: 'profitFactor', label: '盈利因子', kind: 'ratio', improvement: 'higher' },
  { key: 'firstTargetBeforeStopRatePercent', label: '第一止盈先达率', kind: 'percent', improvement: 'higher' },
  { key: 'winRatePercent', label: '已实现盈利交易率', kind: 'percent', improvement: 'higher' },
  { key: 'averageWinLossRatio', label: '平均盈亏比', kind: 'ratio', improvement: 'higher' },
  { key: 'expectancyPerClosedTrade', label: '单笔已平仓期望', kind: 'amount', improvement: 'higher' },
  { key: 'realizedPnlPerHoldingHour', label: '每持有小时已实现收益', kind: 'amountRate', improvement: 'higher' },
  { key: 'meanHoldingHours', label: '平均持有时间', kind: 'hours', improvement: 'neutral' },
  { key: 'closedTradeCount', label: '已平仓交易', kind: 'count', improvement: 'neutral' },
])

const STRATEGY_SETTINGS_DEFAULTS = Object.freeze({
  strategyEngine: 'CLASSIC',
  modelBranch: 'BEST',
  modelRunId: null,
  strategyMode: 'MIDLINE',
  levelStrategy: 'STRUCTURE_EXTREME',
  maxAccountLossRatio: 2,
  maxPortfolioRiskRatio: 4,
  maxSameSidePositions: 2,
  dailyLossLimitRatio: 4,
  rangeEdgeFraction: 0.25,
  rangeMinimumTargetR: 1,
  trendMinimumTargetR: 1.5,
  entryConfirmationMode: 'TRIGGER_ONLY',
  entryConfirmationExpiryBars: 3,
  entryFailureExitBars: 3,
  entryFailureBodyAtrMultiplier: 0.5,
  trailingAtrMultiplier: 2.5,
  structureStopAtrMultiplier: 0.28,
  triggerZoneStopBufferAtrMultiplier: 0.5,
  breakevenBufferAtrMultiplier: 0.05,
  movingStopActivationR: 1,
  nearTermMinimumTargetR: 0.5,
  protectiveTakeProfitRatio: 25,
  firstTakeProfitRatio: 50,
  secondTakeProfitRatio: 75,
})
const EXECUTION_SETTINGS_DEFAULTS = Object.freeze({
  initialBalance: 5,
  positionMarginFraction: 0.25,
  maxManagedPositions: 4,
  scanLimit: 100,
  maxLeverage: 20,
  leverageMode: 'RISK_BUDGET',
  leverage: 1,
  modelEvaluationMode: 'OFF',
})

const backtestMode = ref('RANGE')
const randomDays = ref(7)
const backtestForm = ref({
  startTime: new Date(DEFAULT_BACKTEST_START_TIME),
  endTime: new Date(FIXED_HISTORY_END_TIME),
})
const backtestParameters = ref({ ...EXECUTION_SETTINGS_DEFAULTS })
const strategySettings = ref({ ...STRATEGY_SETTINGS_DEFAULTS })
const loadedStrategySettings = ref({ ...STRATEGY_SETTINGS_DEFAULTS })
const strategySettingsLoading = ref(false)
const backtestJob = ref(null)
const backtestLoading = ref(false)
const backtestExpandedRows = ref({})
const historyFillJob = ref(null)
const historyFillLoading = ref(false)
const backtestHistoryStatus = ref(null)
const backtestHistoryStatusLoading = ref(false)
const backtestHistoryStatusError = ref('')
const backtestModelStatus = ref(null)
const backtestModelStatusLoading = ref(false)
const backtestModelStatusError = ref('')
const selectedModel = ref(null)
let backtestPollTimer = null
let backtestRequestId = 0
let historyFillPollTimer = null
let historyFillRequestId = 0
let backtestHistoryStatusPollTimer = null
let backtestHistoryStatusRequestId = 0

const asNumber = (value) => {
  const number = Number(value)
  return Number.isFinite(number) ? number : 0
}

const marketErrorMessage = (error) => error?.response?.data?.message || error?.message || '回测服务暂时不可用'
const finiteNumber = (value) => Number.isFinite(Number(value))
const normalizeStrategySettings = (value) => {
  const source = value && typeof value === 'object' ? value : {}
  const strategyEngine = String(source.strategyEngine || '').trim().toUpperCase()
  const modelBranch = String(source.modelBranch || '').trim().toUpperCase()
  const modelRunId = String(source.modelRunId || '').trim()
  const strategyMode = String(source.strategyMode || '').trim().toUpperCase()
  const levelStrategy = String(source.levelStrategy || '').trim().toUpperCase()
  const entryConfirmationMode = String(source.entryConfirmationMode || '').trim().toUpperCase()
  return {
    strategyEngine: ['CLASSIC', 'MODEL'].includes(strategyEngine) ? strategyEngine : STRATEGY_SETTINGS_DEFAULTS.strategyEngine,
    modelBranch: ['BEST', 'STABLE'].includes(modelBranch) ? modelBranch : STRATEGY_SETTINGS_DEFAULTS.modelBranch,
    modelRunId: modelRunId || null,
    strategyMode: ['MIDLINE', 'SHORT_TERM'].includes(strategyMode) ? strategyMode : STRATEGY_SETTINGS_DEFAULTS.strategyMode,
    levelStrategy: ['STRUCTURE_EXTREME', 'CONFIRMED_PLATFORM'].includes(levelStrategy) ? levelStrategy : STRATEGY_SETTINGS_DEFAULTS.levelStrategy,
    maxAccountLossRatio: finiteNumber(source.maxAccountLossRatio) ? Number(source.maxAccountLossRatio) : STRATEGY_SETTINGS_DEFAULTS.maxAccountLossRatio,
    maxPortfolioRiskRatio: finiteNumber(source.maxPortfolioRiskRatio) ? Number(source.maxPortfolioRiskRatio) : STRATEGY_SETTINGS_DEFAULTS.maxPortfolioRiskRatio,
    maxSameSidePositions: finiteNumber(source.maxSameSidePositions) ? Number(source.maxSameSidePositions) : STRATEGY_SETTINGS_DEFAULTS.maxSameSidePositions,
    dailyLossLimitRatio: finiteNumber(source.dailyLossLimitRatio) ? Number(source.dailyLossLimitRatio) : STRATEGY_SETTINGS_DEFAULTS.dailyLossLimitRatio,
    rangeEdgeFraction: finiteNumber(source.rangeEdgeFraction) ? Number(source.rangeEdgeFraction) : STRATEGY_SETTINGS_DEFAULTS.rangeEdgeFraction,
    rangeMinimumTargetR: finiteNumber(source.rangeMinimumTargetR) ? Number(source.rangeMinimumTargetR) : STRATEGY_SETTINGS_DEFAULTS.rangeMinimumTargetR,
    trendMinimumTargetR: finiteNumber(source.trendMinimumTargetR) ? Number(source.trendMinimumTargetR) : STRATEGY_SETTINGS_DEFAULTS.trendMinimumTargetR,
    entryConfirmationMode: ['RETEST_REQUIRED', 'TRIGGER_ONLY'].includes(entryConfirmationMode) ? entryConfirmationMode : STRATEGY_SETTINGS_DEFAULTS.entryConfirmationMode,
    entryConfirmationExpiryBars: finiteNumber(source.entryConfirmationExpiryBars) ? Number(source.entryConfirmationExpiryBars) : STRATEGY_SETTINGS_DEFAULTS.entryConfirmationExpiryBars,
    entryFailureExitBars: finiteNumber(source.entryFailureExitBars) ? Number(source.entryFailureExitBars) : STRATEGY_SETTINGS_DEFAULTS.entryFailureExitBars,
    entryFailureBodyAtrMultiplier: finiteNumber(source.entryFailureBodyAtrMultiplier) ? Number(source.entryFailureBodyAtrMultiplier) : STRATEGY_SETTINGS_DEFAULTS.entryFailureBodyAtrMultiplier,
    trailingAtrMultiplier: finiteNumber(source.trailingAtrMultiplier) ? Number(source.trailingAtrMultiplier) : STRATEGY_SETTINGS_DEFAULTS.trailingAtrMultiplier,
    structureStopAtrMultiplier: finiteNumber(source.structureStopAtrMultiplier) ? Number(source.structureStopAtrMultiplier) : STRATEGY_SETTINGS_DEFAULTS.structureStopAtrMultiplier,
    triggerZoneStopBufferAtrMultiplier: finiteNumber(source.triggerZoneStopBufferAtrMultiplier) ? Number(source.triggerZoneStopBufferAtrMultiplier) : STRATEGY_SETTINGS_DEFAULTS.triggerZoneStopBufferAtrMultiplier,
    breakevenBufferAtrMultiplier: finiteNumber(source.breakevenBufferAtrMultiplier) ? Number(source.breakevenBufferAtrMultiplier) : STRATEGY_SETTINGS_DEFAULTS.breakevenBufferAtrMultiplier,
    movingStopActivationR: finiteNumber(source.movingStopActivationR) ? Number(source.movingStopActivationR) : STRATEGY_SETTINGS_DEFAULTS.movingStopActivationR,
    nearTermMinimumTargetR: finiteNumber(source.nearTermMinimumTargetR) ? Number(source.nearTermMinimumTargetR) : STRATEGY_SETTINGS_DEFAULTS.nearTermMinimumTargetR,
    protectiveTakeProfitRatio: finiteNumber(source.protectiveTakeProfitRatio) ? Number(source.protectiveTakeProfitRatio) : STRATEGY_SETTINGS_DEFAULTS.protectiveTakeProfitRatio,
    firstTakeProfitRatio: finiteNumber(source.firstTakeProfitRatio) ? Number(source.firstTakeProfitRatio) : STRATEGY_SETTINGS_DEFAULTS.firstTakeProfitRatio,
    secondTakeProfitRatio: finiteNumber(source.secondTakeProfitRatio) ? Number(source.secondTakeProfitRatio) : STRATEGY_SETTINGS_DEFAULTS.secondTakeProfitRatio,
  }
}
const strategySettingsPayload = () => normalizeStrategySettings(strategySettings.value)
const applySelectedModel = (selection) => {
  if (!selection?.runId) return
  selectedModel.value = selection.model && typeof selection.model === 'object'
    ? selection.model
    : { runId: String(selection.runId), branch: selection.branch }
  strategySettings.value = {
    ...strategySettings.value,
    strategyEngine: 'MODEL',
    strategyMode: 'MIDLINE',
    modelRunId: String(selection.runId),
    modelBranch: ['BEST', 'STABLE'].includes(String(selection.branch || '').toUpperCase())
      ? String(selection.branch).toUpperCase()
      : strategySettings.value.modelBranch,
  }
  ElMessage.success(`已选择 ${selection.branch === 'STABLE' ? '稳定末期' : '验证最佳'} 模型用于本次回测`)
}
const applyStoredModelSelection = () => {
  if (typeof window === 'undefined') return
  try {
    const saved = JSON.parse(window.localStorage.getItem(MODEL_SELECTION_STORAGE_KEY) || 'null')
    const branch = String(saved?.branch || '').trim().toUpperCase()
    const runId = String(saved?.runId || '').trim()
    if (runId && ['BEST', 'STABLE'].includes(branch)) {
      selectedModel.value = { runId, branch }
      strategySettings.value = { ...strategySettings.value, strategyEngine: 'MODEL', strategyMode: 'MIDLINE', modelRunId: runId, modelBranch: branch }
    }
  } catch {}
}
const handleModelSelectionEvent = (event) => applySelectedModel(event?.detail)
const backtestTimeframe = computed(() => strategySettings.value.strategyEngine === 'MODEL' || strategySettings.value.strategyMode === 'SHORT_TERM' ? '5m' : '15m')
const backtestIntervalMs = computed(() => backtestTimeframe.value === '5m' ? SHORT_TERM_BACKTEST_INTERVAL_MS : MIDLINE_BACKTEST_INTERVAL_MS)
const directModelSelected = computed(() => String(strategySettings.value.strategyEngine || '').toUpperCase() === 'MODEL')
const backtestExecutionPayload = () => ({
  initialBalance: Number(backtestParameters.value.initialBalance),
  positionMarginFraction: Number(backtestParameters.value.positionMarginFraction),
  maxManagedPositions: Number(backtestParameters.value.maxManagedPositions),
  scanLimit: Number(backtestParameters.value.scanLimit),
  maxLeverage: Number(backtestParameters.value.maxLeverage),
  leverageMode: String(backtestParameters.value.leverageMode || 'RISK_BUDGET').toUpperCase(),
  leverage: Number(backtestParameters.value.leverage),
  // Direct-plan MODEL replay is its own route; never send the legacy
  // baseline/filter comparison for this engine. The backend repeats this
  // normalization for stale clients.
  modelEvaluationMode: directModelSelected.value ? 'OFF' : String(backtestParameters.value.modelEvaluationMode || 'OFF').toUpperCase(),
  modelRunId: strategySettings.value.modelRunId,
  strategyMode: directModelSelected.value ? 'MIDLINE' : strategySettings.value.strategyMode,
})
const modelEvaluationMode = computed(() => directModelSelected.value ? 'OFF' : String(backtestParameters.value.modelEvaluationMode || 'OFF').toUpperCase())
const modelEvaluationTestRange = computed(() => {
  const selectedRunId = String(strategySettings.value.modelRunId || '').trim()
  const selectedRange = selectedModel.value?.runId === selectedRunId
    ? selectedModel.value?.dataset?.split?.test
    : null
  const test = selectedRange || backtestModelStatus.value?.latestModel?.dataset?.split?.test
  const startTime = asNumber(test?.startTime)
  const endTime = asNumber(test?.endTime)
  return startTime > 0 && endTime > startTime ? { startTime, endTime } : null
})
const modelEvaluationAvailable = computed(() => Boolean(
  ((selectedModel.value?.runId === String(strategySettings.value.modelRunId || '').trim()
    ? selectedModel.value?.artifactAvailable
    : (backtestModelStatus.value?.available || backtestModelStatus.value?.researchArtifactAvailable)))
  && modelEvaluationTestRange.value,
))
const modelEvaluationRangeLabel = computed(() => {
  const range = modelEvaluationTestRange.value
  return range ? `${formatBacktestDateTime(range.startTime)} 至 ${formatBacktestDateTime(range.endTime)}` : '--'
})
const modelEvaluationMessage = computed(() => {
  if (modelEvaluationMode.value !== 'COMPARE') return ''
  if (!modelEvaluationAvailable.value) return backtestModelStatusError.value || backtestModelStatus.value?.availabilityReason || '本地模型或其留出测试区间不可用。'
  return '模型对照仅使用模型留出的 15m 中线测试期；同一留出期先回放纪律基线，再回放时序模型直接生成的完整计划；两边共用止盈、移动止损和无 1m 的保守同根路径。'
})
const applyModelEvaluationWindow = () => {
  const range = modelEvaluationTestRange.value
  if (!range) return
  backtestMode.value = 'RANGE'
  strategySettings.value = { ...strategySettings.value, strategyMode: 'MIDLINE' }
  backtestForm.value = {
    startTime: new Date(range.startTime),
    endTime: new Date(range.endTime),
  }
}
const resetBacktestParameters = () => {
  backtestParameters.value = { ...EXECUTION_SETTINGS_DEFAULTS }
  selectedModel.value = null
  strategySettings.value = { ...loadedStrategySettings.value }
}
const formatBacktestDateTime = (value) => value ? new Date(Number(value)).toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '--'
const formatBacktestDateTimeUtc = (value) => value ? new Date(Number(value)).toISOString().slice(0, 16).replace('T', ' ') : '--'
const priceDecimalPlaces = (value) => {
  const number = Math.abs(asNumber(value))
  if (!number || number >= 1) return 4
  return Math.max(0, 4 - Math.floor(Math.log10(number)) - 1)
}
const formatPrice = (value) => {
  const number = asNumber(value)
  if (!number) return '--'
  return number.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: priceDecimalPlaces(number) })
}
const formatCrypto = (value) => asNumber(value).toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 8 })
const formatSignedNumber = (value) => {
  const number = asNumber(value)
  return `${number > 0 ? '+' : ''}${number.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 8 })}`
}
const formatSignedPercent = (value) => {
  const number = asNumber(value)
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`
}
const hasMetricValue = (value) => value !== null && value !== undefined && Number.isFinite(Number(value))
const formatModelEvaluationMetric = (value, metric) => {
  if (!hasMetricValue(value)) return '--'
  const number = Number(value)
  if (metric?.kind === 'amount') return `${formatSignedNumber(number)} USDT`
  if (metric?.kind === 'amountRate') return `${formatSignedNumber(number)} USDT/h`
  if (metric?.kind === 'percent') return `${number.toFixed(2)}%`
  if (metric?.kind === 'hours') return `${number.toFixed(2)} h`
  if (metric?.kind === 'ratio') return number.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 4 })
  return Math.round(number).toLocaleString('en-US')
}
const formatModelEvaluationDelta = (value, metric) => {
  if (!hasMetricValue(value)) return '--'
  const number = Number(value)
  if (metric?.kind === 'amount') return `${formatSignedNumber(number)} USDT`
  if (metric?.kind === 'amountRate') return `${formatSignedNumber(number)} USDT/h`
  if (metric?.kind === 'percent') return formatSignedPercent(number)
  if (metric?.kind === 'hours') return `${number > 0 ? '+' : ''}${number.toFixed(2)} h`
  if (metric?.kind === 'ratio') return `${number > 0 ? '+' : ''}${number.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 4 })}`
  return `${number > 0 ? '+' : ''}${Math.round(number).toLocaleString('en-US')}`
}
const pnlClass = (value) => {
  const number = asNumber(value)
  return number > 0 ? 'binance-rise' : number < 0 ? 'binance-fall' : 'binance-neutral'
}
const pnlDisplayClass = (value) => ['binance-change-value', pnlClass(value)]
const modelEvaluationDeltaClass = (value, metric) => {
  if (!hasMetricValue(value) || metric?.improvement === 'neutral') return ['binance-change-value', 'binance-neutral']
  return ['binance-change-value', pnlClass(metric?.improvement === 'lower' ? -Number(value) : Number(value))]
}
const modelEvaluationDeltaValue = (evaluation, key) => {
  const reported = evaluation?.delta?.[key]
  if (hasMetricValue(reported)) return Number(reported)
  const baseline = evaluation?.baseline?.[key]
  const filtered = evaluation?.modelFiltered?.[key]
  return hasMetricValue(baseline) && hasMetricValue(filtered) ? Number(filtered) - Number(baseline) : null
}
const directionClass = (value) => String(value || '').toUpperCase() === 'SHORT' ? 'binance-direction-short' : 'binance-direction-long'
const strategyLevelLabel = (value) => ({ STRUCTURE_EXTREME: '结构极值', CONFIRMED_PLATFORM: '确认平台/区域' }[value] || '结构极值')
const backtestModeLabel = (options) => {
  const source = options && typeof options === 'object' ? options : {}
  if (String(source.mode || '').toUpperCase() === 'RANGE') return `指定区间 · ${formatBacktestDateTimeUtc(source.startTime)} 至 ${formatBacktestDateTimeUtc(source.endTime)}`
  return `随机 ${asNumber(source.days) || randomDays.value} 天`
}

const rangeStartTime = computed(() => asNumber(backtestForm.value.startTime))
const rangeEndTime = computed(() => asNumber(backtestForm.value.endTime))
const rangeDurationLabel = computed(() => {
  const duration = rangeEndTime.value - rangeStartTime.value
  if (duration <= 0) return '待设置'
  const days = Math.floor(duration / BACKTEST_DAY_MS)
  const hours = Math.floor((duration % BACKTEST_DAY_MS) / (60 * 60 * 1000))
  return `${days} 天${hours ? ` ${hours} 小时` : ''}`
})
const rangeValidationMessage = computed(() => {
  const start = rangeStartTime.value
  const end = rangeEndTime.value
  if (!start || !end) return '请选择开始时间和结束时间。'
  if (start % backtestIntervalMs.value || end % backtestIntervalMs.value) return `开始时间和结束时间必须按 ${backtestTimeframe.value} 边界对齐。`
  if (start < EARLIEST_START_TIME) return `开始时间不能早于 ${formatBacktestDateTime(EARLIEST_START_TIME)}，需要给 4h 周期留出预热K线。`
  if (end > FIXED_HISTORY_END_TIME) return `结束时间不能晚于 ${formatBacktestDateTime(FIXED_HISTORY_END_TIME)}。`
  if (end <= start) return '结束时间必须晚于开始时间。'
  if (end - start > 30 * BACKTEST_DAY_MS) return '指定区间不能超过 30 天。'
  return ''
})
const parameterValidationMessage = computed(() => {
  const execution = backtestParameters.value
  const initialBalance = Number(execution.initialBalance)
  const positionMarginFraction = Number(execution.positionMarginFraction)
  const maxManagedPositions = Number(execution.maxManagedPositions)
  const scanLimit = Number(execution.scanLimit)
  const maxLeverage = Number(execution.maxLeverage)
  const leverageMode = String(execution.leverageMode || '').toUpperCase()
  const leverage = Number(execution.leverage)
  // The computed mode is OFF for direct MODEL replay even if a stale CLASSIC
  // comparison value is still present in the reactive form for one tick.
  const modelMode = modelEvaluationMode.value
  if (!Number.isFinite(initialBalance) || initialBalance <= 0) return '初始资金必须大于 0。'
  if (!Number.isFinite(positionMarginFraction) || positionMarginFraction <= 0 || positionMarginFraction > 1) return '单仓位保证金比例必须在 0 到 1 之间。'
  if (!Number.isInteger(maxManagedPositions) || maxManagedPositions < 1 || maxManagedPositions > 20) return '管理仓位上限必须是 1 到 20 的整数。'
  if (!Number.isInteger(scanLimit) || scanLimit < 1 || scanLimit > 1000) return '扫描合约数量必须是 1 到 1000 的整数。'
  if (!Number.isInteger(maxLeverage) || maxLeverage < 1 || maxLeverage > 20) return '最高杠杆必须是 1 到 20 的整数。'
  if (!['RISK_BUDGET', 'FIXED'].includes(leverageMode)) return '请选择有效的杠杆设立规则。'
  if (leverageMode === 'FIXED' && (!Number.isInteger(leverage) || leverage < 1 || leverage > 20)) return '固定杠杆必须是 1 到 20 的整数。'
  if (leverageMode === 'FIXED' && leverage > maxLeverage) return '固定杠杆不能高于最高杠杆。'
  if (!['OFF', 'COMPARE'].includes(modelMode)) return '请选择有效的模型评估模式。'
  if (modelMode === 'COMPARE' && !modelEvaluationAvailable.value) return modelEvaluationMessage.value

  const settings = strategySettings.value
  const directModel = String(settings.strategyEngine || '').toUpperCase() === 'MODEL'
  const strategyMode = String(settings.strategyMode || '').toUpperCase()
  const maxAccountLossRatio = Number(settings.maxAccountLossRatio)
  const maxPortfolioRiskRatio = Number(settings.maxPortfolioRiskRatio)
  const maxSameSidePositions = Number(settings.maxSameSidePositions)
  const dailyLossLimitRatio = Number(settings.dailyLossLimitRatio)
  const rangeEdgeFraction = Number(settings.rangeEdgeFraction)
  const rangeMinimumTargetR = Number(settings.rangeMinimumTargetR)
  const trendMinimumTargetR = Number(settings.trendMinimumTargetR)
  const entryConfirmationMode = String(settings.entryConfirmationMode || '').toUpperCase()
  const entryConfirmationExpiryBars = Number(settings.entryConfirmationExpiryBars)
  const entryFailureExitBars = Number(settings.entryFailureExitBars)
  const entryFailureBodyAtrMultiplier = Number(settings.entryFailureBodyAtrMultiplier)
  const trailingAtrMultiplier = Number(settings.trailingAtrMultiplier)
  const structureStopAtrMultiplier = Number(settings.structureStopAtrMultiplier)
  const triggerZoneStopBufferAtrMultiplier = Number(settings.triggerZoneStopBufferAtrMultiplier)
  const breakevenBufferAtrMultiplier = Number(settings.breakevenBufferAtrMultiplier)
  const movingStopActivationR = Number(settings.movingStopActivationR)
  const nearTermMinimumTargetR = Number(settings.nearTermMinimumTargetR)
  const protectiveTakeProfitRatio = Number(settings.protectiveTakeProfitRatio)
  const firstTakeProfitRatio = Number(settings.firstTakeProfitRatio)
  const secondTakeProfitRatio = Number(settings.secondTakeProfitRatio)
  if (!directModel && !['MIDLINE', 'SHORT_TERM'].includes(strategyMode)) return '请选择有效的策略模式。'
  if (!directModel && modelMode === 'COMPARE' && strategyMode !== 'MIDLINE') return '模型对照仅使用中线模式留出测试期；模型执行粒度固定为5m。'
  if (!directModel && modelMode === 'COMPARE' && (!modelEvaluationTestRange.value || rangeStartTime.value !== modelEvaluationTestRange.value.startTime || rangeEndTime.value !== modelEvaluationTestRange.value.endTime)) return '模型对照必须使用模型留出的测试区间。'
  if (!directModel && !['STRUCTURE_EXTREME', 'CONFIRMED_PLATFORM'].includes(String(settings.levelStrategy || '').toUpperCase())) return '请选择有效的点位策略路线。'
  if (!Number.isFinite(maxAccountLossRatio) || maxAccountLossRatio <= 0 || maxAccountLossRatio > 100) return '账户最大亏损比必须在 0% 到 100% 之间。'
  if (!Number.isFinite(maxPortfolioRiskRatio) || maxPortfolioRiskRatio <= 0 || maxPortfolioRiskRatio > 100) return '组合结构风险上限必须在 0% 到 100% 之间。'
  if (!Number.isInteger(maxSameSidePositions) || maxSameSidePositions < 1 || maxSameSidePositions > 20) return '同向持仓上限必须是 1 到 20 的整数。'
  if (!Number.isFinite(dailyLossLimitRatio) || dailyLossLimitRatio <= 0 || dailyLossLimitRatio > 100) return '单日风险上限必须在 0% 到 100% 之间。'
  if (!directModel) {
    if (!Number.isFinite(rangeEdgeFraction) || rangeEdgeFraction < 0.05 || rangeEdgeFraction >= 0.5) return '区间边缘比例必须在 0.05 到 0.5 之间。'
    if (!Number.isFinite(rangeMinimumTargetR) || rangeMinimumTargetR < 0.5 || rangeMinimumTargetR > 10) return '区间最小目标 R 必须在 0.5 到 10 之间。'
    if (!Number.isFinite(trendMinimumTargetR) || trendMinimumTargetR < 0.5 || trendMinimumTargetR > 10) return '趋势最小目标 R 必须在 0.5 到 10 之间。'
    if (!['RETEST_REQUIRED', 'TRIGGER_ONLY'].includes(entryConfirmationMode)) return '请选择有效的入场确认方式。'
    if (!Number.isInteger(entryConfirmationExpiryBars) || entryConfirmationExpiryBars < 1 || entryConfirmationExpiryBars > 8) return '确认有效 K 线数必须是 1 到 8 的整数。'
    if (!Number.isInteger(entryFailureExitBars) || entryFailureExitBars < 1 || entryFailureExitBars > 8) return '早期失败复核 K 线数必须是 1 到 8 的整数。'
    if (!Number.isFinite(entryFailureBodyAtrMultiplier) || entryFailureBodyAtrMultiplier < 0.1 || entryFailureBodyAtrMultiplier > 5) return '强反向实体 ATR 倍数必须在 0.1 到 5 之间。'
    if (!Number.isFinite(trailingAtrMultiplier) || trailingAtrMultiplier <= 0 || trailingAtrMultiplier > 20) return '极点 ATR 移动止损倍数必须在 0 到 20 之间。'
    if (!Number.isFinite(structureStopAtrMultiplier) || structureStopAtrMultiplier < 0 || structureStopAtrMultiplier > 10 || !Number.isFinite(breakevenBufferAtrMultiplier) || breakevenBufferAtrMultiplier < 0 || breakevenBufferAtrMultiplier > 10) return '结构止损与保本缓冲的 ATR 倍数必须在 0 到 10 之间。'
    if (!Number.isFinite(triggerZoneStopBufferAtrMultiplier) || triggerZoneStopBufferAtrMultiplier < 0.1 || triggerZoneStopBufferAtrMultiplier > 10) return '触发区防假突破缓冲必须在 0.1 到 10 之间。'
    if (!Number.isFinite(movingStopActivationR) || movingStopActivationR <= 0 || movingStopActivationR > 20) return '移动止损启动 R 倍数必须在 0 到 20 之间。'
    if (!Number.isFinite(nearTermMinimumTargetR) || nearTermMinimumTargetR < 0.05 || nearTermMinimumTargetR > 5) return '近端止盈最低 R 必须在 0.05 到 5 之间。'
    if (!Number.isFinite(protectiveTakeProfitRatio) || !Number.isFinite(firstTakeProfitRatio) || !Number.isFinite(secondTakeProfitRatio) || protectiveTakeProfitRatio < 1 || protectiveTakeProfitRatio >= firstTakeProfitRatio || firstTakeProfitRatio >= secondTakeProfitRatio || secondTakeProfitRatio >= 100) return '三档止盈必须满足近端保护 < 第一档 < 第二档，且第二档小于 100%。'
  }
  return ''
})
const requestedWindowLabel = computed(() => backtestMode.value === 'RANDOM'
  ? `随机选择 ${randomDays.value} 天`
  : (rangeValidationMessage.value ? '区间待修正' : `${formatBacktestDateTime(rangeStartTime.value)} 至 ${formatBacktestDateTime(rangeEndTime.value)}`))
const canStartBacktest = computed(() => {
  if (strategySettingsLoading.value || backtestLoading.value || ['QUEUED', 'RUNNING'].includes(backtestJob.value?.status) || historyFillLoading.value || ['QUEUED', 'RUNNING'].includes(historyFillJob.value?.status)) return false
  if (parameterValidationMessage.value) return false
  if (backtestMode.value === 'RANDOM') return Number.isInteger(Number(randomDays.value)) && Number(randomDays.value) >= RANDOM_DAYS_MIN && Number(randomDays.value) <= RANDOM_DAYS_MAX
  return !rangeValidationMessage.value
})

const backtestRunning = computed(() => ['QUEUED', 'RUNNING'].includes(backtestJob.value?.status))
const historyFillRunning = computed(() => ['QUEUED', 'RUNNING'].includes(historyFillJob.value?.status))
const backtestResult = computed(() => {
  const job = backtestJob.value
  if (!job) return null
  return job.status === 'COMPLETED' ? job.result || job.replayResult || null : job.replayResult || null
})
const modelEvaluationResult = computed(() => {
  const evaluation = backtestResult.value?.modelEvaluation
  return evaluation && typeof evaluation === 'object' && String(evaluation.mode || '').toUpperCase() === 'COMPARE' ? evaluation : null
})
const modelFilterRejectedReasons = computed(() => Object.entries(modelEvaluationResult.value?.selection?.rejectedByReason || {})
  .filter(([, count]) => asNumber(count) > 0)
  .sort(([, left], [, right]) => asNumber(right) - asNumber(left)))
const modelFilterReasonLabel = (reason) => ({
  prediction_unavailable: '模型预测不可用',
  plan_direction_invalid: '计划方向无效',
  below_validation_threshold: '未达到验证阈值',
  direction_mismatch: '模型方向不一致',
  probability_invalid: '预测概率无效',
}[String(reason || '').toLowerCase()] || String(reason || '未知原因'))
const backtestHistoryIncomplete = computed(() => backtestJob.value?.status === 'HISTORY_INCOMPLETE')
const backtestRateLimited = computed(() => backtestJob.value?.status === 'RATE_LIMITED')
const backtestHistoryPreparationStatus = computed(() => (backtestHistoryIncomplete.value || backtestRateLimited.value) ? backtestJob.value?.result || null : null)
const backtestProgress = computed(() => backtestJob.value?.progress || { phase: 'QUEUED', message: '', completed: 0, total: 0, currentSymbol: null, historyCached: false, historyReady: false, historyMissingRanges: 0, downloadedBars: 0 })
const backtestProgressPercent = computed(() => {
  const total = asNumber(backtestProgress.value.total)
  return total ? Math.min(100, Math.round(asNumber(backtestProgress.value.completed) / total * 100)) : 0
})
const historyFillProgress = computed(() => historyFillJob.value?.progress || { phase: 'QUEUED', message: '', completed: 0, total: 0, currentSymbol: null })
const historyFillProgressPercent = computed(() => {
  const total = asNumber(historyFillProgress.value.total)
  return total ? Math.min(100, Math.round(asNumber(historyFillProgress.value.completed) / total * 100)) : 0
})
const backtestHistoryCompletenessPercent = computed(() => Math.max(0, Math.min(100, asNumber(backtestHistoryStatus.value?.percent))))
const formatBacktestHistoryCompletenessPercent = (value) => {
  const percent = Math.max(0, Math.min(100, asNumber(value)))
  return `${percent.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: percent > 0 && percent < 10 ? 1 : 0 })}%`
}
const backtestHistoryCompletenessLabel = computed(() => {
  const status = backtestHistoryStatus.value
  if (!status) return backtestHistoryStatusLoading.value ? '读取中' : '--'
  return `${formatBacktestHistoryCompletenessPercent(status.percent)} · ${status.completeSeries || 0}/${status.requiredSeries || 0}`
})
const backtestHistoryCompletenessTitle = computed(() => {
  const status = backtestHistoryStatus.value
  if (!status) return backtestHistoryStatusError.value || '正在读取本地历史行情完整度'
  const intervalLines = (status.intervals || []).map((item) => `${item.interval}: 完整 ${item.complete || 0}/${item.total || 0}，覆盖 ${item.coverageComplete || 0}，K线 ${item.dataComplete || 0}`)
  const lines = [`${formatBacktestDateTime(FIXED_HISTORY_START_TIME)} 至 ${formatBacktestDateTime(FIXED_HISTORY_END_TIME)}，本地数据库严格校验`, `完整 ${status.completeSeries || 0}/${status.requiredSeries || 0} 个序列`, ...intervalLines]
  if (status.excludedCount) lines.push(`已排除 ${status.excludedCount} 个在区间开始后上线、无法覆盖整月数据的合约`)
  if (backtestHistoryStatusError.value) lines.push(`读取提示：${backtestHistoryStatusError.value}；已保留最近成功数据`)
  return lines.join('\n')
})
const backtestStatusLabel = computed(() => ({ QUEUED: '排队中', RUNNING: '运行中', COMPLETED: '已完成', HISTORY_INCOMPLETE: '待补历史', RATE_LIMITED: '已限频', FAILED: '失败' }[backtestJob.value?.status] || '等待开始'))
const backtestActionLabel = (type) => ({ OPEN: '开仓', STOP: '止损平仓', TAKE_PROFIT: '分批止盈', STOP_STAGE: '止损管理', SCAN_MATCH: '发现计划', CONFIRM: '回测确认', ENTRY_BLOCKED: '风险阻断', FAILURE_WARNING: '失败预警', FAILURE_EXIT: '失败退出', MINUTE_FALLBACK: '保守同根路径' }[String(type || '').toUpperCase()] || '操作')
const backtestRowKey = (row) => String(row?.time ?? '')
const backtestVisibleActions = (row) => (Array.isArray(row?.actions) ? row.actions : []).filter((action) => String(action?.type || '').toUpperCase() !== 'CANCEL')
const backtestRowPositions = (row) => (Array.isArray(row?.positions) ? row.positions : [])
const backtestHasDetails = (row) => backtestVisibleActions(row).length > 0 || backtestRowPositions(row).length > 0
const formatBacktestPositionPnl = (position) => {
  if (position?.pnlPercent !== null && position?.pnlPercent !== undefined) return formatSignedPercent(position.pnlPercent)
  const margin = asNumber(position?.initialMargin || position?.margin)
  return margin > 0 ? formatSignedPercent(asNumber(position?.totalPnl) / margin * 100) : '--'
}
const isBacktestRowExpanded = (row) => Boolean(backtestExpandedRows.value[backtestRowKey(row)])
const toggleBacktestRow = (row) => {
  const key = backtestRowKey(row)
  if (!key) return
  backtestExpandedRows.value = { ...backtestExpandedRows.value, [key]: !isBacktestRowExpanded(row) }
}
const backtestStopLabel = (source) => ({ STRUCTURE: '结构止损', MOVING: '移动止损', POSITION_RISK: '持仓风险止损' }[String(source || '').toUpperCase()] || '当前止损')
const backtestStageLabel = (stage) => ({ INITIAL: '初始防守', BREAKEVEN: '保本保护', STRUCTURE_TRAILING: '结构跟踪', ATR_TRAILING: 'ATR 跟踪' }[String(stage || '').toUpperCase()] || '管理中')

const saveJobId = (job) => {
  if (!job?.id) return
  try { sessionStorage.setItem(BACKTEST_JOB_STORAGE_KEY, String(job.id)) } catch { /* browser storage may be unavailable */ }
}
const readJobId = () => {
  try { return sessionStorage.getItem(BACKTEST_JOB_STORAGE_KEY) || '' } catch { return '' }
}
const applyBacktestJob = (job) => {
  if (!job) return
  backtestJob.value = { ...job, connectionError: '' }
  saveJobId(job)
}
const applyHistoryFillJob = (job) => {
  if (!job) return
  historyFillJob.value = { ...job, connectionError: '' }
}

const clearBacktestTimer = () => {
  if (backtestPollTimer && typeof window !== 'undefined') window.clearTimeout(backtestPollTimer)
  backtestPollTimer = null
}
const stopBacktestPolling = () => {
  backtestRequestId += 1
  clearBacktestTimer()
}
const clearHistoryFillTimer = () => {
  if (historyFillPollTimer && typeof window !== 'undefined') window.clearTimeout(historyFillPollTimer)
  historyFillPollTimer = null
}
const stopHistoryFillPolling = () => {
  historyFillRequestId += 1
  historyFillLoading.value = false
  clearHistoryFillTimer()
}
const stopBacktestHistoryStatusPolling = () => {
  backtestHistoryStatusRequestId += 1
  backtestHistoryStatusLoading.value = false
  if (backtestHistoryStatusPollTimer && typeof window !== 'undefined') window.clearInterval(backtestHistoryStatusPollTimer)
  backtestHistoryStatusPollTimer = null
}

const loadBacktestModelStatus = async () => {
  if (backtestModelStatusLoading.value) return
  backtestModelStatusLoading.value = true
  try {
    const status = await fetchBinanceFuturesModelStatus({ network: NETWORK })
    if (status) {
      backtestModelStatus.value = status
      backtestModelStatusError.value = ''
      if (modelEvaluationMode.value === 'COMPARE') applyModelEvaluationWindow()
    }
  } catch (error) {
    backtestModelStatusError.value = marketErrorMessage(error)
  } finally {
    backtestModelStatusLoading.value = false
  }
}

const loadBacktestHistoryStatus = async () => {
  if (backtestHistoryStatusLoading.value) return
  const requestId = ++backtestHistoryStatusRequestId
  backtestHistoryStatusLoading.value = true
  try {
    const status = await fetchBinanceFuturesBacktestHistoryStatus({ network: NETWORK })
    if (requestId !== backtestHistoryStatusRequestId || !status) return
    backtestHistoryStatus.value = status
    backtestHistoryStatusError.value = ''
  } catch (error) {
    if (requestId === backtestHistoryStatusRequestId) backtestHistoryStatusError.value = marketErrorMessage(error)
  } finally {
    if (requestId === backtestHistoryStatusRequestId) backtestHistoryStatusLoading.value = false
  }
}
const startBacktestHistoryStatusPolling = () => {
  if (typeof window === 'undefined' || backtestHistoryStatusPollTimer) return
  void loadBacktestHistoryStatus()
  backtestHistoryStatusPollTimer = window.setInterval(() => { void loadBacktestHistoryStatus() }, 3_000)
}

const pollCurrentStrategyBacktest = async (jobId, requestId) => {
  try {
    const job = await fetchBinanceFuturesStrategyBacktest(jobId)
    if (requestId !== backtestRequestId || !job) return
    applyBacktestJob(job)
    backtestLoading.value = false
    if (job.status === 'COMPLETED') {
      clearBacktestTimer()
      void loadBacktestHistoryStatus()
      ElMessage.success('当前策略回测完成')
      return
    }
    if (job.status === 'FAILED') {
      clearBacktestTimer()
      ElMessage.error(job.error || '当前策略回测失败')
      return
    }
    if (job.status === 'HISTORY_INCOMPLETE') {
      clearBacktestTimer()
      void loadBacktestHistoryStatus()
      ElMessage.info('固定历史库尚未补齐，已保存本轮数据')
      return
    }
    if (job.status === 'RATE_LIMITED') {
      clearBacktestTimer()
      void loadBacktestHistoryStatus()
      ElMessage.warning('交易所已限制历史请求，本次回测已中断')
      return
    }
    backtestPollTimer = window.setTimeout(() => pollCurrentStrategyBacktest(jobId, requestId), 900)
  } catch (error) {
    if (requestId !== backtestRequestId) return
    if (backtestJob.value?.id === jobId) backtestJob.value = { ...backtestJob.value, connectionError: marketErrorMessage(error) }
    backtestPollTimer = window.setTimeout(() => pollCurrentStrategyBacktest(jobId, requestId), 1500)
  }
}

const requestOptions = () => {
  const options = {
    mode: backtestMode.value === 'RANDOM' ? 'RANDOM' : 'RANGE',
    ...backtestExecutionPayload(),
  }
  if (backtestMode.value === 'RANDOM') {
    options.days = Number(randomDays.value)
  } else {
    options.startTime = rangeStartTime.value
    options.endTime = rangeEndTime.value
  }
  return options
}

const startCurrentStrategyBacktest = async () => {
  if (!canStartBacktest.value) return
  stopBacktestPolling()
  void loadBacktestHistoryStatus()
  const requestId = ++backtestRequestId
  backtestLoading.value = true
  backtestJob.value = null
  backtestExpandedRows.value = {}
  try {
    const job = await startBinanceFuturesStrategyBacktest({
      network: NETWORK,
      backtestOptions: requestOptions(),
      strategySettings: strategySettingsPayload(),
    })
    if (!job?.id) throw new Error('回测任务未创建')
    if (requestId !== backtestRequestId) return
    applyBacktestJob(job)
    await pollCurrentStrategyBacktest(job.id, requestId)
  } catch (error) {
    if (requestId !== backtestRequestId) return
    backtestLoading.value = false
    backtestJob.value = { status: 'FAILED', error: marketErrorMessage(error), progress: {} }
    ElMessage.error(marketErrorMessage(error))
  }
}

const pollBacktestHistoryFill = async (jobId, requestId) => {
  try {
    const job = await fetchBinanceFuturesBacktestHistoryFill(jobId)
    if (requestId !== historyFillRequestId || !job) return
    applyHistoryFillJob(job)
    historyFillLoading.value = false
    if (job.status === 'COMPLETED') {
      clearHistoryFillTimer()
      void loadBacktestHistoryStatus()
      ElMessage.success('固定历史行情已补全')
      return
    }
    if (job.status === 'RATE_LIMITED') {
      clearHistoryFillTimer()
      void loadBacktestHistoryStatus()
      ElMessage.warning('交易所已限制历史请求，本次补全已停止，可稍后再次尝试')
      return
    }
    if (job.status === 'HISTORY_INCOMPLETE') {
      clearHistoryFillTimer()
      void loadBacktestHistoryStatus()
      ElMessage.info('本轮补全结束，仍有缺口；已保存成功数据')
      return
    }
    if (job.status === 'FAILED') {
      clearHistoryFillTimer()
      ElMessage.error(job.error || '历史补全失败')
      return
    }
    historyFillPollTimer = window.setTimeout(() => pollBacktestHistoryFill(jobId, requestId), 900)
  } catch (error) {
    if (requestId !== historyFillRequestId) return
    if (historyFillJob.value?.id === jobId) historyFillJob.value = { ...historyFillJob.value, connectionError: marketErrorMessage(error) }
    historyFillPollTimer = window.setTimeout(() => pollBacktestHistoryFill(jobId, requestId), 1500)
  }
}

const startBacktestHistoryFill = async () => {
  if (historyFillRunning.value || historyFillLoading.value || backtestRunning.value || backtestLoading.value) return
  stopHistoryFillPolling()
  void loadBacktestHistoryStatus()
  const requestId = ++historyFillRequestId
  historyFillLoading.value = true
  historyFillJob.value = null
  try {
    const job = await startBinanceFuturesBacktestHistoryFill({ network: NETWORK })
    if (!job?.id) throw new Error('历史补全任务未创建')
    if (requestId !== historyFillRequestId) return
    applyHistoryFillJob(job)
    await pollBacktestHistoryFill(job.id, requestId)
  } catch (error) {
    if (requestId !== historyFillRequestId) return
    historyFillLoading.value = false
    historyFillJob.value = { status: 'FAILED', error: marketErrorMessage(error), progress: {} }
    ElMessage.error(marketErrorMessage(error))
  }
}

const loadBacktestStrategySettings = async () => {
  if (!props.currentUser || strategySettingsLoading.value) return
  strategySettingsLoading.value = true
  try {
    const normalized = normalizeStrategySettings(await fetchBinanceStrategySettings())
    loadedStrategySettings.value = normalized
    strategySettings.value = { ...normalized }
  } catch {
    // The backtest page remains usable for anonymous/public replays; the
    // built-in defaults are already the same as the strategy settings page.
  } finally {
    strategySettingsLoading.value = false
    applyStoredModelSelection()
  }
}

watch(() => props.currentUser?.id, (userId) => {
  // Clear the previous account's values before an asynchronous snapshot load.
  loadedStrategySettings.value = { ...STRATEGY_SETTINGS_DEFAULTS }
  strategySettings.value = { ...STRATEGY_SETTINGS_DEFAULTS }
  if (!userId) {
    // Do not carry a previous account's strategy snapshot into a guest or a
    // newly selected account.  The page should immediately fall back to the
    // same built-in defaults used by the backend until the new snapshot loads.
    return
  }
  void loadBacktestStrategySettings()
})

watch(modelEvaluationMode, (mode) => {
  if (mode !== 'COMPARE') return
  if (modelEvaluationTestRange.value) applyModelEvaluationWindow()
  else void loadBacktestModelStatus()
})

watch(directModelSelected, (selected) => {
  if (selected && backtestParameters.value.modelEvaluationMode !== 'OFF') {
    backtestParameters.value = { ...backtestParameters.value, modelEvaluationMode: 'OFF' }
  }
})

const restoreBacktestJob = async () => {
  const jobId = readJobId()
  if (!jobId) return
  try {
    const job = await fetchBinanceFuturesStrategyBacktest(jobId)
    if (!job) return
    applyBacktestJob(job)
    if (['QUEUED', 'RUNNING'].includes(job.status)) {
      const requestId = ++backtestRequestId
      backtestLoading.value = true
      await pollCurrentStrategyBacktest(job.id, requestId)
    }
  } catch (error) {
    backtestJob.value = { status: 'FAILED', error: marketErrorMessage(error), progress: {} }
  }
}

onMounted(() => {
  if (typeof window !== 'undefined') window.addEventListener('binance-model-selected', handleModelSelectionEvent)
  applyStoredModelSelection()
  startBacktestHistoryStatusPolling()
  void loadBacktestModelStatus()
  void loadBacktestStrategySettings()
  void restoreBacktestJob()
})

onUnmounted(() => {
  if (typeof window !== 'undefined') window.removeEventListener('binance-model-selected', handleModelSelectionEvent)
  stopBacktestPolling()
  stopHistoryFillPolling()
  stopBacktestHistoryStatusPolling()
})
</script>

<style scoped>
.binance-backtest-page {
  --binance-gold: #f0b90b;
  --binance-up: #0f9f73;
  --binance-down: #d85b68;
  --binance-rise: #d85b68;
  --binance-fall: #0f9f73;
  --binance-direction-long: #d85b68;
  --binance-direction-short: #0f9f73;
  --binance-change-font: var(--font-tab);
  display: grid;
  gap: 14px;
  width: 100%;
  max-width: 100%;
  overflow-x: clip;
  min-width: 0;
}
.binance-backtest-page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; min-width: 0; padding: 4px 2px 5px; }
.binance-backtest-head-action-group { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
.binance-backtest-heading { display: flex; align-items: flex-start; gap: 12px; min-width: 0; }
.binance-backtest-brand-mark { display: inline-grid; width: 42px; height: 42px; flex: 0 0 auto; place-items: center; border: 1px solid color-mix(in srgb, var(--binance-gold) 55%, var(--border-line)); background: color-mix(in srgb, var(--binance-gold) 12%, var(--panel-solid)); color: var(--binance-gold); }
.binance-backtest-kicker, .binance-backtest-panel-kicker { display: block; color: var(--text-faint); font-family: var(--font-tab); font-size: 10px; font-weight: 800; letter-spacing: .04em; text-transform: uppercase; }
.binance-backtest-heading h1 { margin: 2px 0 4px; color: var(--text-primary); font-size: 29px; font-weight: 800; line-height: 1.12; }
.binance-backtest-heading p { margin: 0; color: var(--text-muted); font-size: 12px; line-height: 1.5; }
.binance-backtest-head-actions { display: flex; align-items: center; gap: 10px; flex: 0 0 auto; }
.binance-backtest-network { display: inline-flex; min-height: 27px; align-items: center; gap: 6px; padding: 0 9px; border: 1px solid color-mix(in srgb, var(--binance-up) 32%, var(--border-line)); color: var(--binance-up); font-family: var(--font-tab); font-size: 10px; font-weight: 900; white-space: nowrap; }
.binance-backtest-network i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.binance-backtest-workspace { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(360px, .95fr); align-items: start; gap: 14px; min-width: 0; }
.binance-backtest-side-stack { display: grid; align-content: start; gap: 14px; min-width: 0; }
.binance-backtest-panel { min-width: 0; padding: 18px; border: 1px solid var(--border-line); background: var(--panel-solid); }
.binance-backtest-panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; min-width: 0; }
.binance-backtest-panel-head h2 { margin: 3px 0 0; color: var(--text-primary); font-size: 18px; font-weight: 800; line-height: 1.25; }
.binance-backtest-timeframe-badge, .binance-backtest-history-percent { display: inline-flex; min-height: 25px; align-items: center; padding: 0 8px; border: 1px solid color-mix(in srgb, var(--binance-gold) 42%, var(--border-line)); color: var(--binance-gold); font-family: var(--font-tab); font-size: 10px; font-weight: 900; white-space: nowrap; }
.binance-backtest-mode-label { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-top: 20px; }
.binance-backtest-mode-label span, .binance-backtest-field-label { color: var(--text-primary); font-size: 12px; font-weight: 800; }
.binance-backtest-mode-label small, .binance-backtest-field-help, .binance-backtest-safety-note { color: var(--text-faint); font-size: 11px; line-height: 1.45; }
.binance-backtest-mode-switch { display: flex; width: 100%; margin-top: 8px; }
.binance-backtest-mode-switch :deep(.el-radio-button) { flex: 1 1 0; }
.binance-backtest-mode-switch :deep(.el-radio-button__inner) { width: 100%; padding: 9px 12px; }
.binance-backtest-option-block { display: grid; gap: 8px; margin-top: 18px; padding: 14px; border: 1px solid var(--border-subtle); background: var(--panel-muted); }
.binance-backtest-days-control { display: flex; align-items: center; gap: 8px; }
.binance-backtest-days-control :deep(.el-input-number) { width: 150px; }
.binance-backtest-days-control strong { color: var(--text-primary); font-size: 13px; }
.binance-backtest-field-help { margin: 0; }
.binance-backtest-range-fields { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.binance-backtest-date-field { display: grid; gap: 7px; min-width: 0; }
.binance-backtest-date-field :deep(.el-date-editor) { width: 100%; }
.binance-backtest-validation { grid-column: 1 / -1; margin: 0; color: var(--binance-down); font-size: 11px; line-height: 1.45; }
.binance-backtest-window-summary { display: grid; gap: 4px; margin-top: 16px; padding: 12px 13px; border-left: 3px solid var(--binance-gold); background: color-mix(in srgb, var(--binance-gold) 7%, var(--panel-solid)); }
.binance-backtest-window-summary span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-backtest-window-summary strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-num); font-size: 15px; font-weight: 900; }
.binance-backtest-window-summary small { color: var(--text-faint); font-size: 10px; line-height: 1.4; }
.binance-backtest-start-button { width: 100%; margin-top: 14px; }
.binance-backtest-safety-note { margin: 10px 0 0; }
.binance-backtest-history-panel { padding: 12px; }
.binance-backtest-history-meter { display: grid; gap: 6px; margin-top: 9px; padding: 8px 9px; border: 1px solid var(--border-subtle); background: var(--panel-muted); }
.binance-backtest-history-meter-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.binance-backtest-history-meter-head span { color: var(--text-muted); font-size: 11px; font-weight: 700; }
.binance-backtest-history-meter-head strong { color: var(--text-primary); font-family: var(--font-num); font-size: 12px; font-weight: 900; }
.binance-backtest-history-meter :deep(.el-progress-bar__outer) { background: var(--border-subtle); }
.binance-backtest-history-meter :deep(.el-progress-bar__inner) { background: var(--binance-gold); transition: width 220ms ease; }
.binance-backtest-history-meter.is-loading :deep(.el-progress-bar__inner) { opacity: .68; }
.binance-backtest-history-meter.is-stale { border-color: color-mix(in srgb, var(--binance-gold) 45%, var(--border-line)); }
.binance-backtest-history-range { display: grid; gap: 3px; margin-top: 8px; }
.binance-backtest-history-range span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-backtest-history-range strong { color: var(--text-primary); font-family: var(--font-num); font-size: 13px; font-weight: 900; overflow-wrap: anywhere; }
.binance-backtest-history-range small { color: var(--text-faint); font-size: 10px; line-height: 1.45; }
.binance-backtest-history-job { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 7px 12px; margin-top: 8px; padding: 8px 9px; border: 1px solid color-mix(in srgb, var(--binance-gold) 38%, var(--border-line)); background: color-mix(in srgb, var(--binance-gold) 5%, var(--panel-muted)); }
.binance-backtest-history-job > div { display: grid; gap: 3px; min-width: 0; }
.binance-backtest-history-job > div:nth-child(2) { justify-items: end; text-align: right; }
.binance-backtest-history-job span, .binance-backtest-history-job small { color: var(--text-muted); font-size: 10px; }
.binance-backtest-history-job strong { color: var(--text-primary); font-family: var(--font-tab); font-size: 12px; font-weight: 900; }
.binance-backtest-history-job small { color: var(--text-faint); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.binance-backtest-history-job :deep(.el-progress) { grid-column: 1 / -1; }
.binance-backtest-history-job :deep(.el-progress-bar__inner) { background: var(--binance-gold); }
.binance-backtest-history-error { margin-top: 10px; color: var(--binance-down); font-size: 11px; line-height: 1.45; }
.binance-backtest-history-actions { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 8px; }
.binance-backtest-history-actions small { color: var(--text-faint); font-size: 10px; line-height: 1.4; }
.binance-backtest-parameters-panel { padding: 15px; }
.binance-backtest-parameters-panel > .binance-backtest-panel-head { align-items: center; }
.binance-backtest-parameters-panel > .binance-backtest-panel-head :deep(.el-button) { padding: 0; font-size: 11px; }
.binance-backtest-parameter-note { margin: 8px 0 0; color: var(--text-faint); font-size: 10px; line-height: 1.45; }
.binance-backtest-parameter-group { margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border-subtle); }
.binance-backtest-parameter-group:first-of-type { margin-top: 12px; padding-top: 0; border-top: 0; }
.binance-backtest-parameter-group-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-bottom: 9px; }
.binance-backtest-parameter-group-head span { color: var(--text-primary); font-size: 11px; font-weight: 900; }
.binance-backtest-parameter-group-head small { color: var(--text-faint); font-size: 10px; }
.binance-backtest-parameter-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 11px 12px; }
.binance-backtest-parameter-subgroup-heading { grid-column: 1 / -1; padding-top: 4px; border-top: 1px solid var(--border-subtle); color: var(--text-primary); font-size: 10px; font-weight: 900; line-height: 1.3; }
.binance-backtest-parameter-subgroup-heading:first-child { padding-top: 0; border-top: 0; }
.binance-backtest-parameter-field { display: grid; align-content: start; gap: 5px; min-width: 0; }
.binance-backtest-parameter-field-wide { grid-column: 1 / -1; }
.binance-backtest-parameter-field > label { color: var(--text-primary); font-size: 11px; font-weight: 800; line-height: 1.3; }
.binance-backtest-parameter-field :deep(.el-input-number) { width: 100%; }
.binance-backtest-input-with-suffix { display: flex; align-items: center; gap: 6px; min-width: 0; }
.binance-backtest-input-with-suffix :deep(.el-input-number) { flex: 1 1 auto; min-width: 0; }
.binance-backtest-input-with-suffix > span { flex: 0 0 auto; color: var(--text-faint); font-family: var(--font-num); font-size: 10px; white-space: nowrap; }
.binance-backtest-parameter-field > small { color: var(--text-faint); font-size: 10px; line-height: 1.4; }
.binance-backtest-leverage-switch { display: flex; width: 100%; }
.binance-backtest-leverage-switch :deep(.el-radio-button) { flex: 1 1 0; }
.binance-backtest-leverage-switch :deep(.el-radio-button__inner) { width: 100%; padding: 7px 8px; font-size: 11px; }
.binance-backtest-parameters-panel > .binance-backtest-validation { margin-top: 11px; }
.binance-backtest-result-panel { padding-bottom: 12px; }
.binance-backtest-result-head { align-items: center; }
.binance-backtest-result-head h2 { display: inline-block; margin-right: 9px; }
.binance-backtest-result-head small { color: var(--text-faint); font-size: 10px; }
.binance-backtest-state { display: inline-flex; min-height: 26px; align-items: center; padding: 0 9px; border: 1px solid var(--border-line); color: var(--text-muted); font-family: var(--font-tab); font-size: 11px; font-weight: 900; white-space: nowrap; }
.binance-backtest-state.is-running, .binance-backtest-state.is-queued { border-color: color-mix(in srgb, var(--binance-gold) 55%, var(--border-line)); color: var(--binance-gold); }
.binance-backtest-state.is-completed { border-color: color-mix(in srgb, var(--binance-up) 45%, var(--border-line)); color: var(--binance-up); }
.binance-backtest-state.is-history_incomplete, .binance-backtest-state.is-rate_limited { border-color: color-mix(in srgb, var(--binance-gold) 55%, var(--border-line)); color: var(--binance-gold); }
.binance-backtest-state.is-failed { border-color: color-mix(in srgb, var(--binance-down) 45%, var(--border-line)); color: var(--binance-down); }
.binance-backtest-progress { margin-top: 16px; padding: 14px; border: 1px solid var(--border-line); background: var(--panel-muted); }
.binance-backtest-progress-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; margin-bottom: 12px; }
.binance-backtest-progress-head > div { display: grid; gap: 4px; min-width: 0; }
.binance-backtest-progress-head > div:last-child { justify-items: end; text-align: right; }
.binance-backtest-progress-head span, .binance-backtest-progress-head small { color: var(--text-muted); font-size: 11px; font-weight: 700; }
.binance-backtest-progress-head small { overflow: hidden; color: var(--text-faint); line-height: 1.35; text-overflow: ellipsis; white-space: nowrap; }
.binance-backtest-progress-head strong { overflow: hidden; color: var(--text-primary); font-family: var(--font-tab); font-size: 15px; font-weight: 900; text-overflow: ellipsis; white-space: nowrap; }
.binance-backtest-progress :deep(.el-progress-bar__outer) { background: var(--border-subtle); }
.binance-backtest-progress :deep(.el-progress-bar__inner) { background: var(--binance-gold); }
.binance-backtest-live-note { display: grid; gap: 4px; margin-top: 16px; padding: 10px 12px; border: 1px solid color-mix(in srgb, var(--binance-gold) 48%, var(--border-line)); background: color-mix(in srgb, var(--binance-gold) 8%, var(--panel-muted)); }
.binance-backtest-live-note span { color: var(--binance-gold); font-size: 10px; font-weight: 900; }
.binance-backtest-live-note strong { color: var(--text-primary); font-family: var(--font-tab); font-size: 12px; font-weight: 900; overflow-wrap: anywhere; }
.binance-backtest-live-note small, .binance-backtest-connection-warning { color: var(--text-muted); font-size: 10px; line-height: 1.4; overflow-wrap: anywhere; }
.binance-backtest-connection-warning { margin: 10px 0 0; color: var(--binance-gold); font-weight: 800; }
.binance-backtest-summary { display: grid; grid-template-columns: minmax(210px, 1.45fr) repeat(3, minmax(0, 1fr)); margin-top: 16px; border: 1px solid var(--border-line); }
.binance-backtest-summary > div { display: grid; gap: 5px; min-width: 0; padding: 13px; border-right: 1px solid var(--border-line); }
.binance-backtest-summary > div:last-child { border-right: 0; }
.binance-backtest-summary span, .binance-backtest-row-metrics span, .binance-backtest-holding span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-backtest-summary strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-num); font-size: 16px; font-weight: 900; line-height: 1.3; }
.binance-backtest-summary small { color: var(--text-faint); font-size: 10px; font-weight: 700; line-height: 1.4; overflow-wrap: anywhere; }
.binance-backtest-stats { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; padding: 12px 0; border-bottom: 1px solid var(--border-subtle); }
.binance-backtest-stats span { display: inline-flex; min-height: 25px; align-items: center; padding: 0 8px; border: 1px solid var(--border-line); color: var(--text-muted); font-family: var(--font-tab); font-size: 10px; font-weight: 900; white-space: nowrap; }
.binance-backtest-model-evaluation { display: grid; gap: 11px; margin-top: 17px; padding-top: 15px; border-top: 1px solid var(--border-line); }
.binance-backtest-model-evaluation-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; }
.binance-backtest-model-evaluation-head > div { display: grid; gap: 3px; min-width: 0; }
.binance-backtest-model-evaluation-head span { color: var(--binance-gold); font-family: var(--font-tab); font-size: 10px; font-weight: 900; text-transform: uppercase; }
.binance-backtest-model-evaluation-head h3 { margin: 0; color: var(--text-primary); font-size: 15px; font-weight: 900; line-height: 1.25; }
.binance-backtest-model-evaluation-head small { color: var(--text-faint); font-family: var(--font-num); font-size: 10px; line-height: 1.4; overflow-wrap: anywhere; }
.binance-backtest-model-evaluation-head > strong { flex: 0 0 auto; max-width: 40%; color: var(--text-muted); font-family: var(--font-tab); font-size: 10px; font-weight: 900; line-height: 1.35; text-align: right; overflow-wrap: anywhere; }
.binance-backtest-evaluation-table { display: grid; border-top: 1px solid var(--border-line); border-bottom: 1px solid var(--border-line); }
.binance-backtest-evaluation-row { display: grid; grid-template-columns: minmax(124px, 1.25fr) repeat(3, minmax(0, 1fr)); gap: 8px 14px; align-items: center; min-width: 0; padding: 9px 10px; border-top: 1px solid var(--border-subtle); }
.binance-backtest-evaluation-row:first-child { border-top: 0; }
.binance-backtest-evaluation-row > * { min-width: 0; overflow-wrap: anywhere; }
.binance-backtest-evaluation-row > span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-backtest-evaluation-row > strong, .binance-backtest-evaluation-row > em { color: var(--text-primary); font-family: var(--font-num); font-size: 11px; font-style: normal; font-weight: 900; line-height: 1.35; text-align: right; }
.binance-backtest-evaluation-row.is-head { padding-top: 7px; padding-bottom: 7px; border-top: 0; background: var(--panel-muted); }
.binance-backtest-evaluation-row.is-head > span { color: var(--text-faint); font-family: var(--font-tab); font-size: 9px; font-weight: 900; text-align: right; }
.binance-backtest-evaluation-row.is-head > span:first-child { text-align: left; }
.binance-backtest-evaluation-footnote { margin: 0; color: var(--text-faint); font-size: 10px; font-weight: 700; line-height: 1.5; overflow-wrap: anywhere; }
.binance-backtest-model-audit { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); border: 1px solid var(--border-line); }
.binance-backtest-model-audit > div { display: grid; gap: 3px; min-width: 0; padding: 9px 10px; border-right: 1px solid var(--border-line); }
.binance-backtest-model-audit > div:last-child { border-right: 0; }
.binance-backtest-model-audit span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-backtest-model-audit strong { color: var(--text-primary); font-family: var(--font-num); font-size: 14px; font-weight: 900; }
.binance-backtest-model-rejections { display: flex; flex-wrap: wrap; gap: 6px; }
.binance-backtest-model-rejections span { display: inline-flex; align-items: center; min-height: 24px; padding: 0 7px; border: 1px solid var(--border-line); background: var(--panel-muted); color: var(--text-muted); font-family: var(--font-tab); font-size: 10px; font-weight: 800; line-height: 1.25; overflow-wrap: anywhere; }
.binance-backtest-record-note { display: flex; align-items: baseline; flex-wrap: wrap; gap: 7px; padding: 10px 0 2px; color: var(--text-faint); font-size: 10px; }
.binance-backtest-record-note code { color: var(--text-muted); font-family: var(--font-num); overflow-wrap: anywhere; }
.binance-backtest-timeline { display: grid; margin-top: 14px; border-top: 1px solid var(--border-line); }
.binance-backtest-row { min-width: 0; padding: 13px 0; border-bottom: 1px solid var(--border-line); content-visibility: auto; contain-intrinsic-size: auto 150px; }
.binance-backtest-row-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-width: 0; }
.binance-backtest-row-title { display: flex; flex: 1 1 auto; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; min-width: 0; }
.binance-backtest-row-head strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-tab); font-size: 12px; font-weight: 900; }
.binance-backtest-row-head span { color: var(--text-faint); font-size: 10px; font-weight: 800; overflow-wrap: anywhere; }
.binance-backtest-row-toggle { flex: 0 0 auto; min-height: 27px; padding: 0 9px; border: 1px solid var(--border-line); border-radius: 6px; background: var(--panel-muted); color: var(--text-muted); cursor: pointer; font-family: var(--font-tab); font-size: 10px; font-weight: 900; }
.binance-backtest-row-toggle:hover { border-color: var(--binance-gold); color: var(--text-primary); }
.binance-backtest-row-toggle:focus-visible { outline: 2px solid var(--binance-gold); outline-offset: 2px; }
.binance-backtest-row-metrics { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-top: 10px; }
.binance-backtest-row-metrics > div { display: grid; gap: 3px; min-width: 0; }
.binance-backtest-row-metrics strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-num); font-size: 12px; font-weight: 900; line-height: 1.3; }
.binance-backtest-position-tags { display: flex; flex-wrap: wrap; gap: 5px; min-width: 0; margin-top: 10px; }
.binance-backtest-position-tag { display: inline-flex; min-width: 0; max-width: 100%; align-items: center; gap: 6px; padding: 4px 7px; border: 1px solid var(--border-line); background: var(--panel-muted); color: var(--text-muted); font-family: var(--font-tab); font-size: 10px; font-weight: 900; line-height: 1.2; }
.binance-backtest-position-tag strong { min-width: 0; overflow-wrap: anywhere; color: var(--text-primary); }
.binance-backtest-position-tag em { flex: 0 0 auto; font-family: var(--font-num); font-size: 10px; font-style: normal; }
.binance-backtest-row-details { display: grid; gap: 10px; margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border-subtle); }
.binance-backtest-actions { display: grid; gap: 6px; }
.binance-backtest-action { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 8px; padding: 8px 10px; border-left: 3px solid var(--border-line); background: var(--panel-muted); }
.binance-backtest-action.is-open { border-left-color: var(--binance-direction-long); }
.binance-backtest-action.is-stop { border-left-color: var(--binance-down); }
.binance-backtest-action.is-take_profit { border-left-color: var(--binance-up); }
.binance-backtest-action.is-stop_stage, .binance-backtest-action.is-minute_fallback { border-left-color: var(--binance-gold); }
.binance-backtest-action strong { color: var(--text-primary); font-family: var(--font-tab); font-size: 10px; font-weight: 900; }
.binance-backtest-action em { font-size: 10px; font-style: normal; font-weight: 900; }
.binance-backtest-action span, .binance-backtest-action b, .binance-backtest-action small { color: var(--text-muted); font-family: var(--font-num); font-size: 10px; font-weight: 800; }
.binance-backtest-action b { color: var(--text-primary); }
.binance-backtest-action p { flex: 1 0 100%; margin: 0; color: var(--text-faint); font-size: 10px; font-weight: 700; line-height: 1.4; overflow-wrap: anywhere; }
.binance-backtest-holdings { display: grid; gap: 6px; margin-top: 10px; }
.binance-backtest-holding { display: grid; grid-template-columns: minmax(132px, 1.15fr) repeat(4, minmax(0, 1fr)); gap: 8px 12px; padding: 10px; border: 1px solid var(--border-line); background: var(--panel-solid); }
.binance-backtest-holding > div { display: grid; align-content: start; gap: 3px; min-width: 0; }
.binance-backtest-holding > div:first-child { grid-template-columns: auto auto; align-items: baseline; column-gap: 6px; }
.binance-backtest-holding > div:first-child small { grid-column: 1 / -1; }
.binance-backtest-holding strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-num); font-size: 11px; font-weight: 900; line-height: 1.35; }
.binance-backtest-holding em { font-size: 10px; font-style: normal; font-weight: 900; }
.binance-backtest-holding small { color: var(--text-faint); font-size: 9px; font-weight: 700; line-height: 1.3; overflow-wrap: anywhere; }
.binance-backtest-empty-state { display: flex; align-items: center; gap: 13px; min-height: 110px; color: var(--binance-gold); }
.binance-backtest-empty-state div { display: grid; gap: 4px; }
.binance-backtest-empty-state strong { color: var(--text-primary); font-size: 14px; }
.binance-backtest-empty-state span { color: var(--text-muted); font-size: 11px; line-height: 1.45; }
.binance-backtest-history-incomplete { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-top: 16px; padding: 14px; border: 1px solid color-mix(in srgb, var(--binance-gold) 44%, var(--border-line)); background: var(--panel-muted); }
.binance-backtest-history-incomplete > div { display: grid; gap: 4px; min-width: 0; }
.binance-backtest-history-incomplete span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-backtest-history-incomplete strong { color: var(--text-primary); font-family: var(--font-num); font-size: 14px; font-weight: 900; }
.binance-backtest-history-incomplete small { color: var(--text-faint); font-size: 10px; font-weight: 700; line-height: 1.4; overflow-wrap: anywhere; }
.binance-backtest-empty { display: flex; align-items: center; justify-content: center; gap: 8px; min-height: 100px; color: var(--binance-down); font-size: 12px; }
.binance-backtest-page :deep(.el-button), .binance-backtest-page :deep(.el-input__inner), .binance-backtest-page :deep(.el-radio-button__inner), .binance-backtest-page :deep(.el-date-editor) { font-family: var(--font-base) !important; }
.binance-backtest-page :deep(.el-button) { font-size: 13px; font-weight: 600; }
.binance-backtest-page :deep(.el-input__inner) { font-size: 13px; }
.binance-backtest-page :deep(.el-radio-button__inner) { font-size: 13px; font-weight: 500; }
.binance-backtest-page :deep(.el-date-editor .el-input__wrapper) { width: 100%; }
.binance-backtest-page :deep(.el-date-editor .el-input__inner) { min-width: 0; }
.binance-change-value { font-family: var(--binance-change-font) !important; font-variant-numeric: tabular-nums lining-nums; font-weight: 600 !important; letter-spacing: .01em; }
.binance-rise { color: var(--binance-rise) !important; }
.binance-fall { color: var(--binance-fall) !important; }
.binance-neutral { color: var(--text-muted) !important; }
.binance-direction-long { color: var(--binance-direction-long) !important; }
.binance-direction-short { color: var(--binance-direction-short) !important; }

@media (max-width: 900px) {
  .binance-backtest-workspace { grid-template-columns: 1fr; }
}
@media (max-width: 640px) {
  .binance-backtest-page-head { align-items: stretch; flex-direction: column; }
  .binance-backtest-head-actions { width: 100%; min-width: 0; justify-content: space-between; }
  .binance-backtest-head-actions .el-button { min-width: 0; }
  .binance-backtest-heading h1 { font-size: 25px; }
  .binance-backtest-panel { padding: 14px; }
  .binance-backtest-parameters-panel { padding: 14px; }
  .binance-backtest-parameter-grid { grid-template-columns: 1fr; }
  .binance-backtest-parameter-field-wide { grid-column: auto; }
  .binance-backtest-range-fields { grid-template-columns: 1fr; }
  .binance-backtest-validation { grid-column: auto; }
  .binance-backtest-history-actions { align-items: flex-start; flex-direction: column; }
  .binance-backtest-history-actions .el-button { width: 100%; }
  .binance-backtest-result-head { align-items: flex-start; flex-direction: column; }
  .binance-backtest-progress-head { grid-template-columns: 1fr; gap: 12px; }
  .binance-backtest-progress-head > div:last-child { justify-items: start; text-align: left; }
  .binance-backtest-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .binance-backtest-summary > div:nth-child(2) { border-right: 0; }
  .binance-backtest-summary > div:nth-child(-n + 2) { border-bottom: 1px solid var(--border-line); }
  .binance-backtest-model-evaluation-head { align-items: flex-start; flex-direction: column; }
  .binance-backtest-model-evaluation-head > strong { max-width: none; text-align: left; }
  .binance-backtest-model-audit { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .binance-backtest-model-audit > div:nth-child(2) { border-right: 0; }
  .binance-backtest-model-audit > div:nth-child(-n + 2) { border-bottom: 1px solid var(--border-line); }
  .binance-backtest-row-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .binance-backtest-holding { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 430px) {
  .binance-backtest-heading { gap: 9px; }
  .binance-backtest-brand-mark { width: 36px; height: 36px; }
  .binance-backtest-heading p { font-size: 11px; }
  .binance-backtest-mode-label { align-items: flex-start; flex-direction: column; gap: 3px; }
  .binance-backtest-summary { grid-template-columns: 1fr; }
  .binance-backtest-summary > div { border-right: 0; border-bottom: 1px solid var(--border-line); }
  .binance-backtest-summary > div:last-child { border-bottom: 0; }
  .binance-backtest-evaluation-row { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px 12px; padding: 10px 0; }
  .binance-backtest-evaluation-row.is-head { display: none; }
  .binance-backtest-evaluation-row > span:first-child { grid-column: 1 / -1; color: var(--text-primary); font-size: 11px; }
  .binance-backtest-evaluation-row > strong, .binance-backtest-evaluation-row > em { display: grid; gap: 2px; text-align: left; }
  .binance-backtest-evaluation-row > strong::before, .binance-backtest-evaluation-row > em::before { color: var(--text-faint); font-family: var(--font-base); font-size: 9px; font-style: normal; font-weight: 800; }
  .binance-backtest-evaluation-row > strong:nth-child(2)::before { content: '纪律基线'; }
  .binance-backtest-evaluation-row > strong:nth-child(3)::before { content: '模型过滤'; }
  .binance-backtest-evaluation-row > em::before { content: '模型相对基线'; }
  .binance-backtest-row-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .binance-backtest-holding { grid-template-columns: 1fr; }
  .binance-backtest-history-job { grid-template-columns: 1fr; }
  .binance-backtest-history-job > div:nth-child(2) { justify-items: start; text-align: left; }
}
</style>
