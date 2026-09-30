<template>
  <section ref="binancePageRef" class="binance-page page-content" aria-labelledby="binance-page-title">
    <header class="binance-page-head">
      <div class="binance-heading">
        <span class="binance-brand-mark" aria-hidden="true"><Bitcoin :size="24" /></span>
        <div>
          <h1 id="binance-page-title">Binance 量化交易</h1>
        </div>
      </div>
      <div class="binance-head-actions">
        <span class="binance-network-indicator is-mainnet"><i></i>主网</span>
        <el-button plain :icon="RefreshCw" :loading="activeMarketsLoading" title="刷新行情" aria-label="刷新行情" @click="refreshMarkets" />
      </div>
    </header>

    <div class="binance-content-grid">
      <section ref="chartStageRef" :class="['binance-panel', 'binance-chart-stage', { 'is-chart-collapsed': chartCollapsed }]" aria-label="市场行情图表">
        <div class="binance-panel-head binance-chart-stage-head">
          <div>
            <span class="binance-panel-kicker">Market monitor</span>
            <h2 id="binance-market-title">市场监控</h2>
          </div>
        </div>
        <div ref="chartPanelRef" id="binance-market-chart" class="binance-market-chart-panel">
          <div class="binance-chart-layout">
            <div class="binance-chart-main">
              <div class="binance-chart-shell">
                <div v-if="binanceConnectionNotice" class="binance-market-stale is-connection-warning" role="status" aria-live="polite">
                  连接提示：{{ binanceConnectionNotice }}；已保留最近成功数据
                </div>
                <div v-else-if="activeMarketsStale" class="binance-market-stale is-connection-warning" role="status" aria-live="polite">
                  行情暂时使用最近成功数据
                </div>
                <BinanceKlineChart
                  :klines="klines"
                  :market-key="chartMarketKey"
                  :market-data-ready-key="marketDataReadyKey"
                  :plan="chartPlan"
                  :current-price="chartCurrentPrice"
                  :interval="interval"
                  :loading="marketLoading"
                  :stale="marketStale"
                  :has-market="Boolean(selectedMarketSymbol)"
                />
              </div>
            </div>
            <div class="binance-chart-periods">
              <el-segmented v-model="interval" :options="intervalOptions" size="small" aria-label="K 线周期" @change="loadMarketKlines" />
            </div>
          </div>
        </div>
        <button
          type="button"
          class="binance-chart-collapse-toggle"
          :class="{ 'is-collapsed': chartCollapsed }"
          :aria-expanded="!chartCollapsed"
          aria-controls="binance-market-chart"
          :aria-label="chartCollapsed ? '展开固定 K 线图' : '收起固定 K 线图'"
          :title="chartCollapsed ? '展开固定 K 线图' : '收起固定 K 线图'"
          @click="toggleChartCollapsed"
        >
          <ChevronDown v-if="chartCollapsed" :size="16" aria-hidden="true" />
          <ChevronUp v-else :size="16" aria-hidden="true" />
        </button>
      </section>

      <section class="binance-panel binance-search-panel" aria-labelledby="binance-search-title">
        <div class="binance-panel-head binance-market-head">
          <div>
            <span class="binance-panel-kicker">Target scanner</span>
            <h2 id="binance-search-title">寻找目标</h2>
          </div>
          <div class="binance-market-actions">
            <div class="binance-market-target-row">
              <el-autocomplete
                v-model="marketSearch"
                class="binance-market-search"
                size="small"
                clearable
                :prefix-icon="Search"
                :fetch-suggestions="queryMarketSymbols"
                :trigger-on-focus="true"
                :highlight-first-item="true"
                placeholder="搜索交易对"
                aria-label="搜索交易对"
                @select="selectMarketSuggestion"
                @clear="clearSelectedMarket"
                @keyup.enter="selectTypedMarket"
              >
                <template #default="{ item }">
                  <div class="binance-market-suggestion">
                    <strong>{{ item.symbol }}</strong>
                    <span>{{ formatPrice(item.lastPrice) }} · <em :class="changeDisplayClass(item.priceChangePercent)">{{ formatSignedPercent(item.priceChangePercent) }}</em> · {{ formatCompactNumber(item.quoteVolume) }}</span>
                  </div>
                </template>
              </el-autocomplete>
              <el-button plain :icon="SearchCheck" :loading="targetAnalyzing" :disabled="!isMarketSearchSelection || activeAnalyzing" @click="analyzeMarketTarget">分析标的</el-button>
            </div>
            <div class="binance-market-scan-row">
              <label class="binance-futures-limit" :title="`当前市场共 ${futuresScanLimitMax} 个合约，按成交额从高到低扫描`"><span>寻找前</span><el-input-number v-model="activeScanLimit" :min="1" :max="futuresScanLimitMax" :precision="0" :step="1" :disabled="activeAnalysisRunning" controls-position="right" size="small" aria-label="寻找合约前多少个" /><span>个</span></label>
              <el-button class="binance-scan-btn" type="primary" :icon="ScanSearch" :loading="activeAnalyzing && activeAnalysisScope === 'scan'" :disabled="activeAnalysisRunning && activeAnalysisScope === 'target'" @click="analyzeActiveMarket">寻找目标</el-button>
            </div>
          </div>
        </div>
        <section v-if="activeAnalysisJob" class="binance-futures-analysis binance-market-analysis" aria-labelledby="futures-analysis-title">
          <div class="binance-panel-head">
            <div>
              <span class="binance-panel-kicker">Local price-action scan</span>
              <h3 id="futures-analysis-title">{{ activeAnalysisScope === 'target' ? '合约目标纪律分析' : '合约位置计划' }}</h3>
            </div>
          </div>
          <div v-if="activeAnalysisRunning" class="binance-futures-progress" role="status" aria-live="polite">
            <div class="binance-futures-progress-head">
              <div><span>当前分析</span><strong>{{ activeAnalysisProgress.currentSymbol || '正在准备合约候选' }}</strong></div>
              <div><span>完成进度</span><strong>{{ activeAnalysisProgress.completed || 0 }} / {{ activeAnalysisProgress.total || activeScanLimit }}</strong></div>
            </div>
            <el-progress :percentage="activeAnalysisPercent" :show-text="false" :stroke-width="8" />
          </div>
          <template v-if="activeAnalysisPlan?.symbol">
            <div class="binance-futures-result-tabs" role="tablist" aria-label="扫描结果计划">
              <span>{{ activeAnalysisScope === 'target' ? '指定目标结果' : (isModelAnalysis ? `模型推理结果 · 可执行 ${activeArmedPlanCount}` : `可执行结果 · 符合 ${activeArmedPlanCount} · 试错 ${activeTrialPlanCount}`) }}</span>
              <div>
                <button v-for="plan in activeAnalysisPlans" :key="plan.symbol" type="button" :class="['binance-futures-result-tab', { 'is-active': activeAnalysisPlan?.symbol === plan.symbol, 'is-armed': plan.status === 'ARMED', 'is-trial': plan.trialEligible } ]" :aria-pressed="activeAnalysisPlan?.symbol === plan.symbol" @click="selectAnalysisPlanWithMotion(plan)">
                  <span class="binance-futures-result-tab-symbol"><strong>{{ plan.symbol }}</strong><em :class="directionClass(plan.direction)">{{ directionLabel(plan.direction) }}</em></span>
                  <span class="binance-futures-result-tab-meta"><b>{{ plan.conditionMet ?? '--' }}/{{ plan.conditionTotal ?? '--' }}</b><small>{{ analysisPlanStatusLabel(plan.status, plan) }}</small></span>
                </button>
              </div>
            </div>
            <div ref="analysisDetailRef" class="binance-futures-analysis-detail">
            <div class="binance-futures-plan-summary">
              <div :class="['binance-futures-scan-contract', 'binance-chart-selection', activeAnalysisPlan.direction === 'LONG' ? 'is-long' : (activeAnalysisPlan.direction === 'SHORT' ? 'is-short' : 'is-wait')]" role="button" tabindex="0" :aria-label="`查看 ${activeAnalysisPlan.symbol} 的计划图表`" @click="selectAnalysisPlan(activeAnalysisPlan)" @keydown.enter="selectAnalysisPlan(activeAnalysisPlan)" @keydown.space.prevent="selectAnalysisPlan(activeAnalysisPlan)">
                <div class="binance-futures-scan-copy">
                  <span>扫描合约</span>
                  <div class="binance-futures-scan-symbol"><strong>{{ activeAnalysisPlan.symbol }}</strong><em :class="directionClass(activeAnalysisPlan.direction)"><template v-if="activeAnalysisPlan.direction === 'WAIT'">WAIT</template><template v-else>{{ activeAnalysisPlan.direction === 'LONG' ? 'LONG' : 'SHORT' }}</template></em></div>
                  <small><span v-if="activeAnalysisPlan.opportunityLabel">{{ activeAnalysisPlan.opportunityLabel }} · </span>{{ activeAnalysisPlan.strategyModeLabel || (activeAnalysisPlan.strategyMode === 'SHORT_TERM' ? '短线模式' : '中线模式') }} · {{ modeLabel(activeAnalysisPlan.marketMode) }} · {{ strategyLevelLabel(activeAnalysisPlan.levelStrategy) }} · 条件完整度 {{ activeAnalysisPlan.conditionCompleteness ?? activeAnalysisPlan.confidence ?? 0 }}%（{{ activeAnalysisPlan.conditionMet ?? '--' }}/{{ activeAnalysisPlan.conditionTotal ?? '--' }}） · 质量 {{ activeAnalysisPlan.quality ?? '--' }}</small>
                </div>
                <div class="binance-futures-risk-reward">
                  <span>风险回报</span>
                  <strong>{{ formatPlanRiskReward(activeAnalysisPlan) }}</strong>
                </div>
              </div>
              <el-button class="binance-futures-create-monitor" plain :icon="Plus" :disabled="!isPlanExecutable(activeAnalysisPlan)" @click="startAnalysisPlanExecution">创建监控</el-button>
            </div>
            <div class="binance-futures-level-grid">
              <div><span>{{ isModelAnalysis ? '模型入场点' : '触发价格' }}</span><strong>{{ formatPrice(activeAnalysisPlan.entry?.trigger) }}</strong><small class="binance-trigger-zone"><span v-if="isModelAnalysis">单一限价/触发价，不使用入场区间。</span><span v-else>触发区 {{ formatPrice(activeAnalysisPlan.entry?.zoneLow) }} - {{ formatPrice(activeAnalysisPlan.entry?.zoneHigh) }}</span><em>{{ activeAnalysisPlan.direction === 'LONG' ? '向上突破并收盘确认后进场。' : (activeAnalysisPlan.direction === 'SHORT' ? '向下突破并收盘确认后进场。' : (activeAnalysisPlan.modelReason || '模型当前选择等待，不生成入场点位。')) }}</em><em v-if="isModelAnalysis && activeAnalysisPlan.entryTiming?.state">当前价 {{ formatPrice(activeAnalysisPlan.lastPrice || activeAnalysisPlan.latestPrice || activeAnalysisPlan.markPrice) }} · {{ modelEntryStateLabel(activeAnalysisPlan.entryTiming.state) }}</em></small></div>
              <div class="binance-entry-timing-level" :class="{ 'is-ready': activeEntryTimingDisplay.ready, 'has-reference': activeEntryTimingDisplay.hasReference }"><span>{{ activeEntryTimingDisplay.title }}</span><strong :class="{ 'is-pending': !activeEntryTimingDisplay.ready }">{{ activeEntryTimingDisplay.value }}</strong></div>
              <div><span>结构止损</span><strong>{{ formatPrice(activeAnalysisPlan.stopLoss) }}</strong><small>{{ activeAnalysisPlan.levelBasis?.initialStop?.label || '放在信号与回撤结构另一侧' }}</small></div>
               <div><span>{{ planTarget(activeAnalysisPlan, 'PROTECTIVE_TARGET', 0)?.label || '近端保护目标' }}</span><strong>{{ planTarget(activeAnalysisPlan, 'PROTECTIVE_TARGET', -1)?.price ? formatPrice(planTarget(activeAnalysisPlan, 'PROTECTIVE_TARGET', -1)?.price) : '未设置' }}</strong><small>{{ formatTargetR(planTarget(activeAnalysisPlan, 'PROTECTIVE_TARGET', -1)?.rMultiple) }} · 累计 {{ formatTargetPercent(planTargetRatio(activeAnalysisPlan, 'protectiveTakeProfitRatio', activeAnalysisPlan.strategySettings?.protectiveTakeProfitRatio)) }}</small></div>
               <div><span>{{ planTarget(activeAnalysisPlan, 'FIRST_TARGET', 0)?.label || '第一目标' }}</span><strong>{{ formatPrice(planTarget(activeAnalysisPlan, 'FIRST_TARGET', 0)?.price) }}</strong><small>{{ formatTargetR(planTarget(activeAnalysisPlan, 'FIRST_TARGET', 0)?.rMultiple) }} · 累计 {{ formatTargetPercent(planTargetRatio(activeAnalysisPlan, 'firstTakeProfitRatio', activeAnalysisPlan.strategySettings?.firstTakeProfitRatio)) }}</small></div>
              <div v-if="planHasExtensionTarget(activeAnalysisPlan)"><span>{{ planTarget(activeAnalysisPlan, 'EXTENSION_TARGET', 1)?.label || '第二目标' }}</span><strong>{{ formatPrice(planTarget(activeAnalysisPlan, 'EXTENSION_TARGET', 1)?.price) }}</strong><small>{{ formatTargetR(planTarget(activeAnalysisPlan, 'EXTENSION_TARGET', 1)?.rMultiple) }}</small></div>
            </div>
            <section
              v-if="activeModelPrediction"
              class="binance-futures-model-output"
              aria-labelledby="binance-futures-model-output-title"
            >
              <div class="binance-futures-model-output-head">
                <div>
                  <span class="binance-futures-model-output-kicker">Model inference</span>
                  <strong id="binance-futures-model-output-title">时序模型预测</strong>
                </div>
                <span :class="['binance-futures-model-verdict', `is-${modelVerdictClass(activeModelPrediction.verdict)}`]">{{ modelVerdictLabel(activeModelPrediction.verdict) }}</span>
              </div>
              <div v-if="activeModelPrediction.direct" class="binance-futures-model-output-grid">
                <div class="binance-futures-model-output-group">
                  <strong>直接生成计划</strong>
                  <div><span>计划方向</span><b>{{ activeModelPrediction.direction || '--' }}</b></div>
                  <div><span>入场触发</span><b>{{ formatPrice(activeAnalysisPlan.entry?.trigger) }}</b></div>
                  <div><span>初始止损</span><b>{{ formatPrice(activeAnalysisPlan.stopLoss) }}</b></div>
                  <div><span>第一止盈</span><b>{{ formatPrice(activeAnalysisPlan.takeProfits?.[0]?.price) }}</b></div>
                  <div><span>第二止盈</span><b>{{ formatPrice(activeAnalysisPlan.takeProfits?.[1]?.price) }}</b></div>
                </div>
                <div class="binance-futures-model-output-group">
                  <strong>止盈比例与时间成本</strong>
                  <div><span>第一止盈累计比例</span><b>{{ formatTargetPercent(activeAnalysisPlan.takeProfits?.[0]?.cumulativeRatio ?? activeAnalysisPlan.takeProfits?.[0]?.ratio) }}</b></div>
                  <div><span>第二止盈累计比例</span><b>{{ formatTargetPercent(activeAnalysisPlan.takeProfits?.[1]?.cumulativeRatio ?? ((asNumber(activeAnalysisPlan.takeProfits?.[0]?.ratio) + asNumber(activeAnalysisPlan.takeProfits?.[1]?.ratio)) || activeAnalysisPlan.takeProfits?.[1]?.ratio)) }}</b></div>
                  <div><span>到第一止盈</span><b>{{ formatModelBars(activeAnalysisPlan.timeCost?.tp1Bars, modelExecutionIntervalFor(activeAnalysisPlan)) }}</b></div>
                  <div><span>到第二止盈</span><b>{{ formatModelBars(activeAnalysisPlan.timeCost?.tp2Bars, modelExecutionIntervalFor(activeAnalysisPlan)) }}</b></div>
                  <div><span>综合时间效率</span><b>{{ formatModelR(activeAnalysisPlan.timeCost?.efficiency) }}</b></div>
                  <div><span>综合持仓成本</span><b>{{ formatModelR(activeAnalysisPlan.timeCost?.holdingCost) }}</b></div>
                  <div><span>模型排序分数</span><b>{{ formatModelR(activeAnalysisPlan.modelStrategy?.selectionScore) }}</b></div>
                  <div><span>模型准入</span><b>{{ activeAnalysisPlan.status === 'ARMED' ? '符合' : (activeAnalysisPlan.trialEligible ? '试错' : '阻断') }}</b></div>
                </div>
                <div class="binance-futures-model-output-group">
                  <strong>未来走势分布</strong>
                  <div><span>4 根5m q10 / q50 / q90</span><b>{{ formatForecastTriplet(activeModelPrediction.forecast, 0) }}</b></div>
                  <div><span>12 根5m q10 / q50 / q90</span><b>{{ formatForecastTriplet(activeModelPrediction.forecast, 3) }}</b></div>
                  <div><span>24 根5m q10 / q50 / q90</span><b>{{ formatForecastTriplet(activeModelPrediction.forecast, 6) }}</b></div>
                  <div><span>方向概率</span><b>{{ formatForecastDirection(activeModelPrediction.forecastDirectionProbabilities) }}</b></div>
                  <div><span>触发订单语义</span><b>{{ activeAnalysisPlan.entry?.orderType || '--' }}</b></div>
                  <div><span>预测引导候选</span><b>{{ activeModelPrediction.forecastPlanFeatures?.directionalPersistence ?? '--' }}</b></div>
                </div>
                <div class="binance-futures-model-output-group">
                  <strong>移动止损阶段</strong>
                  <div><span>INITIAL</span><b>{{ formatModelDecimal(activeAnalysisPlan.trailingStop?.INITIAL?.activationR) }}R</b></div>
                  <div><span>BREAKEVEN</span><b>{{ formatModelDecimal(activeAnalysisPlan.trailingStop?.BREAKEVEN?.triggerR) }}R / {{ formatModelDecimal(activeAnalysisPlan.trailingStop?.BREAKEVEN?.bufferAtr) }} ATR</b></div>
                  <div><span>STRUCTURE</span><b>{{ activeAnalysisPlan.trailingStop?.STRUCTURE_TRAILING?.lookbackBars ?? '--' }} bars / {{ formatModelDecimal(activeAnalysisPlan.trailingStop?.STRUCTURE_TRAILING?.bufferAtr) }} ATR</b></div>
                  <div><span>ATR</span><b>{{ activeAnalysisPlan.trailingStop?.ATR_TRAILING?.period ?? '--' }} / {{ formatModelDecimal(activeAnalysisPlan.trailingStop?.ATR_TRAILING?.multiplier) }} ATR</b></div>
                  <div><span>最终退出</span><b>{{ formatModelBars(activeAnalysisPlan.timeCost?.exitBars, modelExecutionIntervalFor(activeAnalysisPlan)) }}</b></div>
                </div>
              </div>
              <div v-else class="binance-futures-model-output-grid">
                <div class="binance-futures-model-output-group">
                  <strong>模型信息</strong>
                  <div><span>模型分支</span><b>{{ activeModelPrediction.modelBranch || activeAnalysisPlan.modelBranch || '--' }}</b></div>
                  <div><span>训练轮次</span><b>{{ activeModelPrediction.selectedEpoch ? `第 ${activeModelPrediction.selectedEpoch} 轮` : '--' }}</b></div>
                  <div><span>运行编号</span><b class="is-breakable">{{ activeModelPrediction.modelRunId || '--' }}</b></div>
                  <div><span>预测方向</span><b>{{ activeModelPrediction.direction || '--' }}</b></div>
                  <div><span>方向置信度</span><b>{{ formatModelProbability(activeModelPrediction.directionConfidence) }}</b></div>
                  <div><span>验证门槛</span><b>{{ activeModelPrediction.qualityGateAccepted ? '已通过' : '未通过（显式使用）' }}</b></div>
                </div>
                <div class="binance-futures-model-output-group">
                  <strong>入场与目标概率</strong>
                  <div><span>入场成交概率</span><b>{{ formatModelProbability(activeModelDetails?.entryProbability) }}</b></div>
                  <div><span>目标先于止损</span><b>{{ formatModelProbability(activeModelDetails?.targetFirstProbability) }}</b></div>
                  <div><span>成交后目标概率</span><b>{{ formatModelProbability(activeModelDetails?.targetFirstAfterEntryProbability) }}</b></div>
                  <div><span>未达目标概率</span><b>{{ formatModelProbability(activeModelDetails?.notTargetAfterEntryProbability) }}</b></div>
                </div>
                <div class="binance-futures-model-output-group">
                  <strong>首个结果概率</strong>
                  <div><span>止损先达</span><b>{{ formatModelProbability(activeModelDetails?.stopFirstProbability) }}</b></div>
                  <div><span>到期未决</span><b>{{ formatModelProbability(activeModelDetails?.expireProbability) }}</b></div>
                  <div><span>目标先达</span><b>{{ formatModelProbability(activeModelDetails?.targetFirstAfterEntryProbability) }}</b></div>
                  <div><span>模型判定</span><b>{{ modelVerdictLabel(activeModelPrediction.verdict) }}</b></div>
                </div>
                <div class="binance-futures-model-output-group">
                  <strong>收益与持有预测</strong>
                  <div><span>预期收益</span><b>{{ formatModelR(activeModelDetails?.expectedR) }}</b></div>
                  <div><span>预期持有</span><b>{{ formatModelBars(activeModelDetails?.expectedDurationBars, modelExecutionIntervalFor(activeAnalysisPlan)) }}</b></div>
                  <div><span>时间效率</span><b>{{ formatModelR(activeModelDetails?.timeEfficiencyScore) }}</b></div>
                  <div><span>排序分数</span><b>{{ formatModelR(activeModelDetails?.rankingScore) }}</b></div>
                  <div><span>预期最大有利幅度</span><b>{{ formatModelR(activeModelDetails?.expectedMfeR) }}</b></div>
                  <div><span>预期最大不利幅度</span><b>{{ formatModelR(activeModelDetails?.expectedMaeR) }}</b></div>
                </div>
                <div class="binance-futures-model-output-group is-impact">
                  <strong>对计划的影响</strong>
                  <div><span>目标调整</span><b>{{ modelTargetAdjustmentLabel(activeAnalysisPlan.modelStrategy?.targetAdjustment) }}</b></div>
                  <div><span>止损策略</span><b>{{ activeAnalysisPlan.modelStrategy?.stopPolicy === 'CLASSIC_STRUCTURAL_STOP_UNCHANGED' ? '经典结构止损不变' : (activeAnalysisPlan.modelStrategy?.stopPolicy || '--') }}</b></div>
                  <div><span>显式分支使用</span><b>{{ activeModelPrediction.explicitBranchUse ? '是' : '否' }}</b></div>
                </div>
              </div>
            </section>
            <section class="binance-futures-evidence" aria-label="计划分析依据">
              <div class="binance-futures-evidence-head">
                <strong>分析依据</strong>
                <button type="button" class="binance-futures-evidence-toggle" :aria-expanded="String(!analysisEvidenceCollapsed)" aria-controls="binance-futures-evidence-content" :title="analysisEvidenceCollapsed ? '展开分析依据' : '收起分析依据'" @click="analysisEvidenceCollapsed = !analysisEvidenceCollapsed">
                  <ChevronDown v-if="analysisEvidenceCollapsed" :size="15" aria-hidden="true" />
                  <ChevronUp v-else :size="15" aria-hidden="true" />
                </button>
              </div>
              <div v-show="!analysisEvidenceCollapsed" id="binance-futures-evidence-content" class="binance-futures-evidence-body">
                <div class="binance-futures-timeframes">
                  <article v-for="timeframe in activeAnalysisTimeframes" :key="timeframe.id">
                    <div><span>{{ timeframe.id }}</span><strong :class="timeframeBiasDisplayClass(timeframe.bias)">{{ timeframeLabel(timeframe) }}</strong></div>
                    <p v-for="evidence in timeframe.evidence" :key="evidence">{{ evidence }}</p>
                  </article>
                </div>
                <div class="binance-futures-reason-grid">
                  <div><span>成立理由</span><p v-for="reason in activeAnalysisPlan.reasons" :key="reason">{{ reason }}</p><p v-if="!activeAnalysisPlan.reasons?.length">当前没有足够独立理由。</p></div>
                  <div><span>等待条件</span><p v-for="condition in activeAnalysisPlan.missingConditions" :key="condition">{{ condition }}</p><p v-if="!activeAnalysisPlan.missingConditions?.length">已满足当前扫描的条件，仍需等触发价确认。</p></div>
                  <div><span>取消条件</span><p v-for="condition in activeAnalysisPlan.cancellationConditions" :key="condition">{{ condition }}</p></div>
                </div>
              </div>
            </section>
            </div>
          </template>
          <div v-else-if="!activeAnalysisRunning" class="binance-futures-empty"><Activity :size="19" /><span>{{ activeAnalysisJob.error || '暂未形成可审计的条件计划' }}</span></div>
        </section>
        <div v-else class="binance-search-empty"><SearchCheck :size="18" /><span>选择交易对分析，或设置范围后点击“寻找目标”。</span></div>
      </section>

      <section class="binance-panel binance-smart-money-panel" aria-labelledby="binance-smart-money-title">
        <div class="binance-panel-head binance-copy-trading-head">
          <div>
            <span class="binance-panel-kicker">Smart money</span>
            <h2 id="binance-smart-money-title">聪明钱</h2>
          </div>
          <div class="binance-copy-trading-controls">
            <small class="binance-copy-trading-updated">{{ copyTradingUpdatedAt ? `更新于 ${formatTime(copyTradingUpdatedAt)}` : '等待数据' }}</small>
          </div>
        </div>
        <div v-if="copyTradingSubscriptions.length" class="binance-copy-trader-subscription-section" :aria-busy="copyTradingSubscriptionSelecting">
          <span class="binance-copy-trader-list-title">关注列表</span>
          <div class="binance-copy-trader-list binance-copy-trader-subscriptions">
            <button
              v-for="subscription in copyTradingSubscriptions"
              :key="`subscription-${subscription.topTraderId}`"
              type="button"
              :class="['binance-copy-trader-list-item', 'binance-copy-trader-subscription-item', subscription.topTraderId === copyTradingSettings.topTraderId ? 'is-active' : '']"
              :disabled="copyTradingSettingsSaving"
              @click="selectCopyTradingSubscriptionWithMotion(subscription)"
            >
              <img
                v-if="subscription.avatarUrl && !copyTradingSubscriptionAvatarFailed(subscription)"
                :src="subscription.avatarUrl"
                :alt="subscription.traderName || subscription.accountName || '聪明钱头像'"
                loading="lazy"
                decoding="async"
                referrerpolicy="no-referrer"
                @error="handleCopyTradingSubscriptionAvatarError(subscription)"
              />
              <span v-else class="binance-copy-trader-subscription-avatar-fallback" aria-hidden="true">{{ copyTradingSubscriptionAvatarInitial(subscription) }}</span>
              <span class="binance-copy-trader-subscription-content">
                <strong>{{ subscription.traderName || subscription.accountName || '未命名用户' }}</strong>
                <small>{{ subscription.accountName || 'Smart Money' }}</small>
                <span class="binance-copy-trader-subscription-stats">
                  <span>30天盈亏率 <b :class="changeDisplayClass(asNumber(subscription.roi) * 100)">{{ formatSignedPercent(asNumber(subscription.roi) * 100) }}</b></span>
                  <span>累计收益 <b :class="changeDisplayClass(subscription.pnl)">{{ formatSignedAssetAmount(subscription.pnl) }}</b></span>
                </span>
              </span>
            </button>
          </div>
          <div class="binance-copy-trading-multiplier">
            <span class="binance-copy-trading-multiplier-label">Multiply</span>
            <div class="binance-copy-trading-multiplier-control">
              <button
                type="button"
                class="binance-copy-trading-multiplier-step"
                title="倍率减一"
                aria-label="跟单倍率减一"
                :disabled="copyTradingSettingsLoading || copyTradingSettingsSaving || copyTradingSubscriptionSelecting || copyTradingMultiplier <= COPY_TRADING_MULTIPLIER_MIN"
                @click="adjustCopyTradingMultiplier(-1)"
              >
                <Minus :size="15" />
              </button>
              <input
                class="binance-copy-trading-multiplier-value"
                type="number"
                inputmode="numeric"
                :value="copyTradingMultiplier"
                :min="COPY_TRADING_MULTIPLIER_MIN"
                :max="COPY_TRADING_MULTIPLIER_MAX"
                step="1"
                aria-label="跟单倍率，范围 1 到 10"
                :disabled="copyTradingSettingsLoading || copyTradingSettingsSaving || copyTradingSubscriptionSelecting"
                @change="commitCopyTradingMultiplier"
                @keydown.enter.prevent="$event.target.blur()"
              />
              <span class="binance-copy-trading-multiplier-unit" aria-hidden="true">x</span>
              <button
                type="button"
                class="binance-copy-trading-multiplier-step"
                title="倍率加一"
                aria-label="跟单倍率加一"
                :disabled="copyTradingSettingsLoading || copyTradingSettingsSaving || copyTradingSubscriptionSelecting || copyTradingMultiplier >= COPY_TRADING_MULTIPLIER_MAX"
                @click="adjustCopyTradingMultiplier(1)"
              >
                <Plus :size="15" />
              </button>
            </div>
          </div>
        </div>
        <div ref="smartMoneyGridRef" class="binance-smart-money-grid">
      <section class="binance-copy-trading-panel" aria-labelledby="binance-copy-trading-title">
        <div class="binance-smart-money-subhead">
          <span class="binance-panel-kicker">Recent operations</span>
          <h3 id="binance-copy-trading-title">操作记录</h3>
        </div>
        <div v-if="copyTradingLoading && !copyTradingRecords.length" class="binance-simulated-empty"><Activity :size="19" /><span>正在读取公开操作记录...</span></div>
        <div v-else-if="copyTradingError && !copyTradingRecords.length" class="binance-simulated-empty is-error"><Activity :size="19" /><span>{{ copyTradingError }}</span></div>
        <div v-else-if="!copyTradingRecords.length" class="binance-simulated-empty"><WalletCards :size="19" /><span>暂无公开操作记录</span></div>
        <div v-else class="binance-copy-trading-list">
          <article v-for="record in copyTradingRecords" :key="copyTradingRecordKey(record)" class="binance-copy-trading-item">
            <div class="binance-copy-trading-main">
              <div class="binance-copy-trading-title-row">
                <button type="button" class="binance-smart-symbol-link" @click="selectSmartMoneyMarket(record.symbol)">{{ record.symbol || '--' }}</button>
                <span :class="copyTradingRecordActionClass(record)">{{ copyTradingRecordActionLabel(record) }}</span>
                <span class="binance-copy-trading-order-type">{{ record.type || 'TRADE' }}</span>
              </div>
              <div class="binance-copy-trading-meta">
                <span>操作 {{ formatCopyTradingTime(record) }}</span>
                <span>成交价值 {{ copyTradingTradeValue(record) == null ? '--' : formatAssetAmount(copyTradingTradeValue(record)) }}</span>
                <span>保证金占比 {{ formatCopyTradingRatio(record) }}</span>
                <span>推荐成交价值 {{ formatAssetAmountOrPlaceholder(copyTradingRecommendedValue(record)) }}</span>
                <span>成交价 {{ formatPrecisePrice(record.avgPrice || record.price) }}</span>
                <span>数量 {{ formatCrypto(record.executedQty || record.qty || record.origQty) }}</span>
                <span v-if="record.totalPnl != null || record.realizedProfit != null" :class="changeDisplayClass(record.totalPnl ?? record.realizedProfit)">盈亏 {{ formatSignedAssetAmount(record.totalPnl ?? record.realizedProfit) }}</span>
              </div>
            </div>
            <el-button type="primary" plain size="small" :icon="Link2" @click="openCopyTradeDialog(record)">跟单</el-button>
          </article>
        </div>
        <p v-if="copyTradingError && copyTradingRecords.length" class="binance-copy-trading-stale">{{ copyTradingError }}，当前显示最近成功数据</p>
      </section>

      <section class="binance-smart-positions-panel" aria-labelledby="binance-smart-positions-title">
        <div class="binance-smart-money-subhead">
          <div>
            <span class="binance-panel-kicker">Open positions</span>
            <h3 id="binance-smart-positions-title">仓位</h3>
          </div>
          <small class="binance-copy-trading-updated">{{ copyTradingPositions.length }} 个</small>
        </div>
        <div v-if="copyTradingLoading && !copyTradingPositions.length" class="binance-simulated-empty"><Activity :size="19" /><span>正在读取聪明钱仓位...</span></div>
        <div v-else-if="copyTradingError && !copyTradingPositions.length" class="binance-simulated-empty is-error"><Activity :size="19" /><span>{{ copyTradingError }}</span></div>
        <div v-else-if="!copyTradingPositions.length" class="binance-simulated-empty"><WalletCards :size="19" /><span>暂无公开仓位</span></div>
        <div v-else class="binance-smart-positions-workspace">
          <div class="binance-smart-position-tabs" role="tablist" aria-label="聪明钱仓位">
            <button
              v-for="position in copyTradingPositionsDisplay"
              :key="`smart-position-tab-${copyTradingPositionTabKey(position)}`"
              type="button"
              role="tab"
              :aria-selected="selectedCopyTradingPosition && copyTradingPositionTabKey(selectedCopyTradingPosition) === copyTradingPositionTabKey(position)"
              :class="['binance-smart-position-tab', selectedCopyTradingPosition && copyTradingPositionTabKey(selectedCopyTradingPosition) === copyTradingPositionTabKey(position) ? 'is-active' : '', { 'is-copying': isCopyTradingPositionFollowing(position), 'is-same-car': isCopyTradingPositionSameCar(position), 'is-value-alert': copyTradingPositionValueAlert(position) }]"
              :title="copyTradingPositionValueAlert(position) ? `推荐仓位价值与我的仓位价值相差 ${formatAssetAmountTwoDecimals(copyTradingPositionValueDeviation(position))} USDT` : undefined"
              @click="selectCopyTradingPositionWithMotion(position)"
            >
              <span class="binance-smart-position-tab-head">
                <strong>{{ copyTradingPositionSymbol(position) }}</strong>
                <em v-if="copyTradingOwnPosition(position)" :class="isCopyTradingPositionFollowing(position) ? 'is-following' : 'is-same-car'">{{ isCopyTradingPositionFollowing(position) ? '跟单中' : '同车' }}</em>
                <em v-if="copyTradingPositionValueAlert(position)" class="is-alert">价值偏差</em>
              </span>
              <span class="binance-smart-position-tab-pnl">
                <span><small>对方</small><strong :class="changeDisplayClass(copyTradingPositionPnl(position))">{{ formatSignedPercent(copyTradingPositionPnlPercent(position)) }}</strong></span>
                <span><small>我的</small><strong v-if="copyTradingOwnPosition(position)" :class="changeDisplayClass(copyTradingOwnPnl(copyTradingOwnPosition(position)))">{{ formatSignedPercent(copyTradingOwnPnlPercent(copyTradingOwnPosition(position))) }}</strong><strong v-else class="binance-neutral">--</strong></span>
              </span>
            </button>
          </div>
          <article v-for="position in selectedCopyTradingPosition ? [selectedCopyTradingPosition] : []" :key="copyTradingPositionKey(position)" ref="smartPositionDetailRef" class="binance-futures-position-item binance-smart-position-item is-selected" :class="{ 'is-copying': isCopyTradingPositionFollowing(position), 'is-same-car': isCopyTradingPositionSameCar(position) }">
            <div class="binance-smart-position-head">
              <div class="binance-futures-position-name">
                <span :class="['binance-futures-direction', copyTradingPositionSide(position) === 'SHORT' ? 'is-short' : 'is-long']">{{ copyTradingPositionSide(position) === 'SHORT' ? 'SHORT' : 'LONG' }}</span>
                <button type="button" class="binance-smart-symbol-link" @click="selectSmartMoneyMarket(copyTradingPositionSymbol(position))">{{ copyTradingPositionSymbol(position) }}</button>
                <span v-if="copyTradingOwnPosition(position)" :class="['binance-smart-copying-state', isCopyTradingPositionFollowing(position) ? 'is-following' : 'is-same-car']">{{ isCopyTradingPositionFollowing(position) ? '跟单中' : '同车' }}</span>
              </div>
            </div>
            <div class="binance-smart-position-detail">
              <div class="binance-smart-position-main">
                <div class="binance-smart-position-grid">
              <div class="binance-smart-grid-spacer" aria-hidden="true"></div>
              <div :class="['binance-smart-grid-title', smartMoneyPnlTone(copyTradingPositionPnl(position))]">聪明钱</div>
              <div :class="['binance-smart-grid-title', 'is-own', smartMoneyPnlTone(copyTradingOwnPosition(position) ? copyTradingOwnPnl(copyTradingOwnPosition(position)) : null)]">我的</div>

              <span class="binance-smart-grid-label">盈亏</span>
              <div :class="['binance-smart-grid-pnl', smartMoneyPnlTone(copyTradingPositionPnl(position))]">
                <strong :class="changeDisplayClass(copyTradingPositionPnl(position))"><AnimatedNumber :value="copyTradingPositionPnlPercent(position)" :formatter="formatSignedPercent" /></strong>
                <small :class="changeDisplayClass(copyTradingPositionPnl(position))"><AnimatedNumber :value="copyTradingPositionPnl(position)" :formatter="formatSignedAssetAmount" /></small>
              </div>
              <div :class="['binance-smart-grid-pnl', 'is-own', smartMoneyPnlTone(copyTradingOwnPosition(position) ? copyTradingOwnPnl(copyTradingOwnPosition(position)) : null)]">
                <template v-if="copyTradingOwnPosition(position)">
                  <strong :class="changeDisplayClass(copyTradingOwnPnl(copyTradingOwnPosition(position)))"><AnimatedNumber :value="copyTradingOwnPnlPercent(copyTradingOwnPosition(position))" :formatter="formatSignedPercent" /></strong>
                  <small :class="changeDisplayClass(copyTradingOwnPnl(copyTradingOwnPosition(position)))"><AnimatedNumber :value="copyTradingOwnPnl(copyTradingOwnPosition(position))" :formatter="formatSignedAssetAmount" /></small>
                </template>
                <strong v-else class="binance-neutral">--</strong>
              </div>

              <span class="binance-smart-grid-label">杠杆</span>
              <div class="binance-smart-grid-value"><strong>{{ copyTradingPositionLeverage(position) }}x</strong></div>
              <div class="binance-smart-grid-value is-own"><strong>{{ copyTradingOwnPosition(position) ? copyTradingOwnLeverage(copyTradingOwnPosition(position)) : '--' }}</strong></div>

              <span class="binance-smart-grid-label">开仓均价</span>
              <div class="binance-smart-grid-value"><strong>{{ formatPrecisePrice(copyTradingPositionEntry(position)) }}</strong></div>
              <div class="binance-smart-grid-value is-own"><strong :class="copyTradingOwnEntryClass(position)">{{ copyTradingOwnPosition(position) ? formatPrecisePrice(copyTradingOwnPosition(position).entryPrice) : '--' }}</strong></div>

              <span class="binance-smart-grid-label">仓位价值</span>
              <div :class="['binance-smart-grid-value', { 'is-value-alert': copyTradingPositionValueAlert(position) }]" :title="copyTradingPositionValueAlert(position) ? `推荐仓位价值与我的仓位价值相差 ${formatAssetAmountTwoDecimals(copyTradingPositionValueDeviation(position))} USDT` : undefined"><strong>{{ formatRecommendedPositionValue(copyTradingRecommendedPositionValue(position)) }}</strong></div>
              <div :class="['binance-smart-grid-value', 'is-own', { 'is-value-alert': copyTradingPositionValueAlert(position) }]" :title="copyTradingPositionValueAlert(position) ? `推荐仓位价值与我的仓位价值相差 ${formatAssetAmountTwoDecimals(copyTradingPositionValueDeviation(position))} USDT` : undefined"><strong>{{ copyTradingOwnPosition(position) ? formatAssetAmountTwoDecimalsOrPlaceholder(copyTradingOwnPositionValue(copyTradingOwnPosition(position))) : '--' }}</strong></div>

              <span class="binance-smart-grid-label">持仓数量</span>
              <div class="binance-smart-grid-value"><strong>{{ formatCrypto(copyTradingPositionQuantity(position)) }}</strong></div>
              <div class="binance-smart-grid-value is-own"><strong>{{ copyTradingOwnPosition(position) ? formatCrypto(copyTradingPositionQuantity(copyTradingOwnPosition(position))) : '--' }}</strong></div>

                </div>
                <div class="binance-smart-position-mark">标记价 <strong>{{ formatPrecisePrice(copyTradingPositionMark(position)) }}</strong></div>
              </div>
              <aside class="binance-smart-position-side">
                <div class="binance-smart-position-deviation">
                  <span>仓位价值偏差</span>
                  <template v-if="copyTradingPositionDeviationSummary(position)?.ratio != null">
                    <strong :class="copyTradingPositionDeviationSummary(position).alert ? 'is-alert' : ''">当前 {{ formatAssetAmountTwoDecimals(copyTradingPositionDeviationSummary(position).actual) }} / 推荐 {{ formatAssetAmountTwoDecimals(copyTradingPositionDeviationSummary(position).recommended) }}</strong>
                    <small :class="copyTradingPositionDeviationSummary(position).alert ? 'is-alert' : ''">差值 {{ formatSignedAssetAmountTwoDecimals(copyTradingPositionDeviationSummary(position).difference) }} USDT，当前为推荐的 {{ formatRatioCompletionPercent(copyTradingPositionDeviationSummary(position).ratio) }}</small>
                  </template>
                  <small v-else-if="copyTradingFollowForPosition(position)">等待跟随监控数据</small>
                  <small v-else>开启跟随监控后显示偏差</small>
                </div>
                <div class="binance-smart-position-actions">
                  <el-button
                    v-if="copyTradingOwnPosition(position)"
                    plain
                    size="small"
                    :type="copyTradingFollowForPosition(position) ? 'success' : 'warning'"
                    :loading="copyTradingFollowSavingKey === copyTradingPositionTabKey(position)"
                    @click="toggleCopyTradingFollow(position)"
                  >{{ copyTradingFollowForPosition(position) ? '取消跟随' : '跟随监控' }}</el-button>
                  <el-button type="primary" plain size="small" :icon="Link2" @click="openCopyTradeDialogForPosition(position)">跟单</el-button>
                </div>
              </aside>
            </div>
          </article>
        </div>
      </section>
        </div>
      </section>

      <section class="binance-panel binance-simulated-panel" aria-labelledby="binance-simulated-title">
          <div class="binance-panel-head">
            <div>
              <span class="binance-panel-kicker">Position monitoring</span>
              <h2 id="binance-simulated-title">持仓计划监控</h2>
            </div>
          </div>
          <div v-if="!currentUser" class="binance-auth-gate binance-simulated-auth-gate">
            <div class="binance-auth-icon"><LockKeyhole :size="20" /></div>
            <strong>登录后管理持仓计划监控</strong>
            <p>监控参数和动态点位仅保存在当前账户下。</p>
            <el-button type="primary" :icon="LogIn" @click="$emit('login-request')">登录或注册</el-button>
          </div>
          <div v-else-if="simulatedPositionsLoading && !simulatedPositions.length" class="binance-simulated-empty"><Activity :size="19" /><span>正在读取持仓计划监控...</span></div>
          <div v-else-if="simulatedPositionsError && !simulatedPositions.length" class="binance-simulated-empty is-error"><Activity :size="19" /><span>{{ simulatedPositionsError }}</span></div>
          <div v-else-if="!simulatedPositions.length" class="binance-simulated-empty"><WalletCards :size="19" /><span>还没有持仓计划监控，请在条件计划中点击“创建监控”。</span></div>
          <div v-else class="binance-simulated-workspace">
            <div class="binance-execution-tabs" role="tablist" aria-label="持仓计划监控">
              <button
                v-for="position in simulatedPositions"
                :key="`execution-tab-${position.id}`"
                type="button"
                role="tab"
                :aria-selected="selectedExecutionPosition?.id === position.id"
                :class="['binance-execution-tab', `is-${monitoringStageClass(position)}`, selectedExecutionPosition?.id === position.id ? 'is-active' : '']"
                @click="selectExecutionPosition(position)"
              >
                <span class="binance-execution-tab-head">
                  <span class="binance-execution-tab-symbol">
                    <strong>{{ position.symbol }}</strong>
                  </span>
                  <em :class="directionClass(position.side)" :title="position.side === 'LONG' ? '做多方向' : '做空方向'">{{ position.side === 'LONG' ? 'LONG' : 'SHORT' }}</em>
                </span>
                <span class="binance-execution-tab-summary">
                  <span class="binance-execution-tab-pnl"><strong :class="executionTabPnl(position) === null ? 'binance-neutral' : executionPnlClass(position)">{{ formatExecutionTabPnlPercent(position) }}</strong><small :class="executionTabPnl(position) === null ? 'binance-neutral' : executionPnlClass(position)">{{ formatExecutionTabPnl(position) }}</small></span>
                  <span :class="['binance-simulated-status', 'binance-execution-tab-status', `is-${monitoringStageClass(position)}`]">{{ monitoringStageLabel(position) }}</span>
                </span>
              </button>
            </div>
            <article v-if="selectedExecutionPosition" class="binance-simulated-item binance-chart-selection" role="button" tabindex="0" :aria-label="`查看 ${selectedExecutionPosition.symbol} 的持仓监控计划图表`" @click="selectExecutionPosition(selectedExecutionPosition)" @keydown.enter="selectExecutionPosition(selectedExecutionPosition)" @keydown.space.prevent="selectExecutionPosition(selectedExecutionPosition)">
              <div class="binance-simulated-item-head">
                <div class="binance-simulated-symbol">
                  <div class="binance-simulated-symbol-main">
                    <strong>{{ selectedExecutionPosition.symbol }}</strong>
                    <span :class="['binance-simulated-side', selectedExecutionPosition.side === 'LONG' ? 'is-long' : 'is-short']">{{ selectedExecutionPosition.side === 'LONG' ? 'LONG' : 'SHORT' }}</span>
                  </div>
                </div>
                <div class="binance-simulated-item-actions">
                  <span :class="['binance-simulated-status', `is-${monitoringStageClass(selectedExecutionPosition)}`]">{{ monitoringStageLabel(selectedExecutionPosition) }}</span>
                  <el-button
                    class="binance-monitor-action"
                    plain
                    size="small"
                    :icon="ShieldCheck"
                    :disabled="!connected || !hasConfirmedLivePosition(selectedExecutionPosition) || !selectedActualFuturesPosition || selectedExecutionPosition.executionStatus === 'STOPPED'"
                    :title="hasConfirmedLivePosition(selectedExecutionPosition) && selectedActualFuturesPosition ? '将当前计划点位应用到实际合约持仓' : '等待账户确认对应真实合约持仓后可应用'"
                    @click.stop="applySelectedPlanToActualPosition"
                  >手动应用点位</el-button>
                  <el-button
                    v-if="selectedExecutionPosition.executionStatus === 'STOPPED'"
                    class="binance-monitor-action"
                    plain
                    size="small"
                    :icon="RefreshCw"
                    :disabled="!canRestoreStoppedMonitor(selectedExecutionPosition)"
                    :title="canRestoreStoppedMonitor(selectedExecutionPosition) ? '确认同向真实持仓仍在后恢复监控' : '需要可靠账户快照确认同一合约和方向的真实持仓仍在'"
                    @click.stop="restoreStoppedPosition(selectedExecutionPosition)"
                  >恢复监控</el-button>
                  <el-button class="binance-monitor-action" plain :icon="Pencil" title="调整监控参数" aria-label="调整监控参数" @click.stop="openPositionDialog(selectedExecutionPosition)" />
                  <el-button class="binance-monitor-action" plain :icon="Trash2" title="删除持仓计划监控" aria-label="删除持仓计划监控" @click.stop="removePosition(selectedExecutionPosition)" />
                </div>
              </div>
              <div v-if="selectedExecutionPosition.marketError" class="binance-simulated-market-error"><Activity :size="15" /><span>{{ selectedExecutionPosition.marketError }}</span></div>
              <template v-else-if="selectedExecutionPosition.plan">
                <div class="binance-simulated-metrics">
                  <template v-if="hasDisplayedPositionMetrics(selectedExecutionPosition)">
                    <div class="binance-simulated-pnl-metric" :aria-label="selectedExecutionPosition.executionStatus === 'STOPPED' ? '止损收益' : '浮动盈亏'">
                      <div class="binance-simulated-pnl-value"><span :class="['binance-pnl-percent', executionPnlClass(selectedExecutionPosition)]"><AnimatedNumber :value="executionUnrealizedPnlPercentFor(selectedExecutionPosition)" :formatter="formatSignedPercent" /></span><span :class="['binance-pnl-amount', executionPnlClass(selectedExecutionPosition)]"><AnimatedNumber :value="executionUnrealizedPnlFor(selectedExecutionPosition)" :formatter="formatSignedMonitoringPnl" /></span></div>
                    </div>
                    <div class="binance-simulated-price-pair"><span>最新/标记</span><strong>{{ formatPrecisePrice(executionLatestPriceFor(selectedExecutionPosition)) }} / {{ formatPrecisePrice(executionMarkPriceFor(selectedExecutionPosition)) }}</strong></div>
                    <div><span>持仓数量</span><strong>{{ formatCrypto(executionQuantityFor(selectedExecutionPosition)) }}</strong></div>
                    <div><span>持仓成本</span><strong>{{ formatPrecisePrice(executionEntryPriceFor(selectedExecutionPosition)) }}</strong></div>
                  </template>
                  <template v-else-if="isWaitingForLivePosition(selectedExecutionPosition)">
                    <div><span>计划数量</span><strong>{{ formatCrypto(selectedExecutionPosition.quantity) }}</strong></div>
                    <div><span>挂单价</span><strong>{{ formatPrecisePrice(selectedExecutionPosition.costPrice) }}</strong></div>
                    <div><span>目前最新价</span><strong>{{ formatPrice(waitingEntryLatestPrice(selectedExecutionPosition)) }}</strong></div>
                    <div class="binance-waiting-entry-gap"><span>距挂单价</span><strong class="binance-change-value">{{ formatWaitingEntryGap(selectedExecutionPosition) }} <small>{{ formatWaitingEntryGapPercent(selectedExecutionPosition) }}</small></strong></div>
                  </template>
                  <template v-else>
                    <div><span>计划数量</span><strong>{{ formatCrypto(selectedExecutionPosition.quantity) }}</strong></div>
                    <div><span>计划成本</span><strong>{{ formatPrecisePrice(selectedExecutionPosition.costPrice) }}</strong></div>
                    <div><span>真实持仓</span><strong>{{ monitoringWaitingLabel(selectedExecutionPosition) }}</strong></div>
                  </template>
                </div>
                <div class="binance-simulated-plan-bar">
                  <span v-if="isPendingEntry(selectedExecutionPosition)">订单状态 <strong>等待限价单成交</strong></span>
                  <span v-else-if="selectedExecutionPosition.executionStatus === 'STOPPED'">结果状态 <strong>止损收益已冻结</strong></span>
                  <span v-else-if="hasConfirmedLivePosition(selectedExecutionPosition)">策略管理 <strong>{{ selectedExecutionPosition.plan.statusLabel }}</strong></span>
                  <span v-else>账户状态 <strong>{{ monitoringWaitingLabel(selectedExecutionPosition) }}</strong></span>
                  <span>监控时长 <strong>{{ formatMonitoringDuration(selectedExecutionPosition) }}</strong></span>
                  <span v-if="hasConfirmedLivePosition(selectedExecutionPosition)">R 倍数 <strong>{{ selectedExecutionPosition.plan.rMultiple ?? '--' }}R</strong></span>
                  <span v-if="selectedExecutionPosition.plan.stale" class="binance-simulated-stale">使用缓存行情</span>
                </div>
                <div v-if="selectedExecutionPosition.plan.realProtectionSync" class="binance-real-protection-sync" :class="{ 'is-error': selectedExecutionPosition.plan.realProtectionSync.status === 'ERROR', 'is-waiting': ['WAITING', 'SKIPPED'].includes(selectedExecutionPosition.plan.realProtectionSync.status) }">
                  <ShieldCheck :size="14" />
                  <span>真实保护单：{{ selectedExecutionPosition.plan.realProtectionSync.reason }}</span>
                </div>
                <div class="binance-simulated-levels">
                  <div v-if="currentExecutionStop" :class="['binance-active-stop-level', 'binance-current-stop', currentExecutionStop.activeClass]">
                    <span>{{ currentExecutionStop.label }} <em>当前使用</em></span>
                    <strong>{{ formatPrice(currentExecutionStop.price) }}</strong>
                    <small :class="changeDisplayClass(currentExecutionStop.pnl)">{{ currentExecutionStop.description }}</small>
                  </div>
                  <div v-for="stop in remainingExecutionStops" :key="stop.key" class="binance-active-stop-level">
                    <span>{{ stop.label }}</span>
                    <strong>{{ formatPrice(stop.price) }}</strong>
                    <small :class="changeDisplayClass(stop.pnl)">{{ stop.description }}</small>
                  </div>
                </div>
                <div v-if="executionTpTargets(selectedExecutionPosition).length" class="binance-simulated-tp-row" :style="{ gridTemplateColumns: `repeat(${executionTpTargets(selectedExecutionPosition).length}, minmax(0, 1fr))` }">
                  <div v-for="target in executionTpTargets(selectedExecutionPosition)" :key="target.key">
                    <span>{{ target.label }}</span>
                    <strong>{{ formatPrice(target.price) }}</strong>
                    <small class="binance-level-result"><span>{{ target.ratio }}/</span><strong :class="changeDisplayClass(target.pnl)">{{ formatSignedMonitoringPnl(target.pnl) }}</strong></small>
                  </div>
                </div>
                <div class="binance-simulated-timeframes">
                  <template v-for="timeframe in positionTimeframes" :key="timeframe">
                    <span v-if="selectedExecutionPosition.plan.timeframes?.[timeframe]" :class="timeframeBiasClass(selectedExecutionPosition.plan.timeframes?.[timeframe])">{{ timeframe }} {{ timeframeBiasLabel(selectedExecutionPosition.plan.timeframes?.[timeframe], selectedExecutionPosition.side) }}</span>
                  </template>
                </div>
                <div v-if="selectedExecutionPosition.plan.managementNote" class="binance-simulated-management-note" :class="{ 'is-warning': selectedExecutionPosition.plan.biasConflict }">
                  <span>纪律管理</span>
                  <p>{{ selectedExecutionPosition.plan.managementNote }}</p>
                </div>
                <div v-if="executionPlanSupportingReasons(selectedExecutionPosition.plan).length" class="binance-simulated-reasons">
                  <p v-for="reason in executionPlanSupportingReasons(selectedExecutionPosition.plan)" :key="reason.text" :class="{ 'is-muted': reason.muted }">{{ reason.text }}</p>
                </div>
              </template>
              <div v-else class="binance-simulated-empty compact"><span>等待主周期行情后生成动态计划。</span></div>
               <div class="binance-simulated-item-foot">
                 <span>{{ selectedExecutionPosition.note || '未填写备注' }}</span>
                 <div class="binance-simulated-item-foot-actions">
                   <span>更新于 {{ formatDateTime(selectedExecutionPosition.updatedAt) }}</span>
                 </div>
               </div>
            </article>
          </div>
      </section>

    <aside
      :class="['binance-account-drawer', accountDrawerOpen ? 'is-open' : 'is-collapsed']"
      :style="accountDrawerStyle"
      aria-label="账户资产"
    >
      <button
          type="button"
          class="binance-account-drawer-handle"
          :class="{ 'is-dragging': accountDrawerDragging, 'is-hidden': accountDrawerOpen }"
          :aria-label="`展开账户资产，未结算盈亏 ${accountDrawerUnrealizedPnl == null ? '暂不可用' : formatSignedAssetAmount(accountDrawerUnrealizedPnl)}`"
          @pointerdown="startAccountDrawerDrag"
          @pointermove="moveAccountDrawerDrag"
          @pointerup="stopAccountDrawerDrag"
          @pointercancel="stopAccountDrawerDrag"
          @click="openAccountDrawer"
        >
          <i class="binance-account-drawer-status-dot" :class="connected ? 'is-connected' : ''" aria-hidden="true"></i>
          <span
            class="binance-account-drawer-pnl"
            :class="accountUnrealizedTone"
            aria-hidden="true"
          ><AnimatedNumber :class="['binance-drawer-pnl-number', accountUnrealizedTone]" :value="accountDrawerUnrealizedPnl" :formatter="formatSignedAccountSummaryAmount" /></span>
      </button>
        <section :class="['binance-panel', 'binance-account-panel', { 'is-hidden': !accountDrawerOpen }]">
          <div class="binance-panel-head">
            <div>
              <span class="binance-panel-kicker">Account snapshot</span>
              <h2>账户资产</h2>
            </div>
            <el-button link :icon="X" title="收起账户资产" aria-label="收起账户资产" @click="closeAccountDrawer" />
          </div>
          <div v-if="account" class="binance-balance-list">
            <div class="binance-account-status-rows" role="status" aria-live="polite">
              <div class="binance-account-status-line">
                <span :class="['binance-connection-state', connected ? 'is-connected' : '']"><i></i>{{ connected ? '在线' : (credentialsProfile.configured ? '自动连接中' : '未配置') }}</span>
                <span :class="['binance-permission-state', (account.futures?.canTrade ?? account.canTrade) ? 'is-on' : 'is-off']">交易权限 {{ (account.futures?.canTrade ?? account.canTrade) ? '已开启' : '未开启' }}</span>
              </div>
              <div class="binance-account-status-line">
                <span :class="['binance-connection-state', accountSnapshotStale ? 'is-stale' : 'is-connected']"><i></i>{{ accountSnapshotStale ? '使用缓存' : '数据正常' }}</span>
                <small>数据 {{ formatTime(accountSnapshotUpdatedAt) }} · 检查 {{ formatTime(accountSnapshotCheckedAt) }}</small>
                <small v-if="accountSnapshotError" class="binance-account-sync-error">{{ accountSnapshotError }}</small>
              </div>
            </div>
            <template v-if="account.futures?.available !== false">
              <div class="binance-futures-account-summary">
                <div class="binance-balance-summary" :style="{ '--balance-fill': `${accountBalanceRatio}%` }">
                  <span>Balance</span>
                  <strong><AnimatedNumber class="binance-account-balance-number" :value="account.futures?.availableBalance" :formatter="formatAccountSummaryAmount" /><i>/</i><AnimatedNumber class="binance-account-balance-number" :value="account.futures?.totalMarginBalance" :formatter="formatAccountSummaryAmount" /></strong>
                </div>
                <div class="binance-unrealized-summary"><span>PnL</span><strong><AnimatedNumber :class="['binance-account-pnl-number', accountUnrealizedTone]" :value="account.futures?.totalUnrealizedProfit" :formatter="formatSignedAccountSummaryAmount" /></strong></div>
              </div>
              <div v-if="futuresPositions.length" class="binance-futures-position-list">
                <article v-for="position in futuresPositions" :key="`${position.symbol}-${position.positionSide}`" :class="['binance-futures-position-item', { 'is-expanded': isFuturesPositionExpanded(position), 'is-value-alert': futuresPositionHasValueAlert(position) }]">
                  <button
                    type="button"
                    :class="['binance-futures-position-head', asNumber(position.unrealizedProfit) < 0 ? 'is-loss' : 'is-profit']"
                    :aria-expanded="isFuturesPositionExpanded(position)"
                    @click="toggleFuturesPositionExpanded(position)"
                  >
                    <div class="binance-futures-position-name">
                      <span :class="['binance-futures-direction', position.side === 'LONG' ? 'is-long' : 'is-short']">{{ position.side === 'LONG' ? 'LONG' : 'SHORT' }}</span>
                      <strong>{{ position.symbol }}</strong>
                      <span class="binance-futures-leverage">{{ position.leverage || '--' }}x</span>
                    </div>
                    <div class="binance-futures-position-pnl">
                      <strong :class="changeDisplayClass(position.unrealizedProfit)"><AnimatedNumber :value="futuresPositionRoePercent(position)" :formatter="formatSignedPercent" /></strong>
                      <small :class="changeDisplayClass(position.unrealizedProfit)"><AnimatedNumber :value="position.unrealizedProfit" :formatter="formatSignedAssetAmount" /></small>
                    </div>
                  </button>
                  <Transition name="binance-futures-position-expand">
                    <div v-if="isFuturesPositionExpanded(position)" class="binance-futures-position-details">
                      <div class="binance-futures-position-details-inner">
                        <BinancePositionPnlChart :points="futuresPositionPnlHistoryFor(position)" :smoothing="futuresPositionSmoothing" />
                        <div class="binance-futures-position-metrics">
                          <div><span>持仓数量</span><strong>{{ formatCrypto(position.quantity) }}</strong></div>
                          <div><span>名义价值</span><strong>{{ formatAssetAmount(position.notional) }}</strong></div>
                          <div><span>最新/标记</span><strong>{{ formatPrecisePrice(futuresLatestPriceFor(position)) }} / {{ formatPrecisePrice(futuresMarkPriceFor(position)) }}</strong></div>
                          <div><span>开仓均价</span><strong>{{ formatPrecisePrice(position.entryPrice) }}</strong></div>
                          <div><span>已实现盈亏（本轮，扣手续费）</span><strong :class="position.realizedPnl == null ? 'binance-neutral' : changeDisplayClass(position.realizedPnl)">{{ position.realizedPnl == null ? '--' : formatSignedAssetAmount(position.realizedPnl) }}</strong></div>
                        </div>
                        <div class="binance-futures-position-bottom">
                          <div class="binance-futures-stop-tp">
                            <div class="binance-futures-stop-row"><span>止损</span><strong>{{ asNumber(position.positionStopLoss ?? position.stopLoss) > 0 ? formatPrecisePrice(position.positionStopLoss ?? position.stopLoss) : '--' }}</strong><small v-if="positionStopLossPnl(position) !== null" :class="changeDisplayClass(positionStopLossPnl(position))">{{ formatSignedAssetAmount(positionStopLossPnl(position)) }}</small></div>
                            <div
                              v-for="(level, levelIndex) in partialTakeProfitLevelsFor(position)"
                              :key="`${position.symbol}-${position.positionSide}-target-${levelIndex}-${level.price}`"
                              :class="['binance-futures-stop-row', 'binance-futures-target-level', { 'is-reached': partialTakeProfitReached(position, levelIndex) }]"
                            >
                              <span>止盈</span>
                              <strong>{{ formatPrecisePrice(level.price) }}</strong>
                              <small>{{ partialTakeProfitRatioCompact(position, levelIndex) }}</small>
                              <small :class="changeDisplayClass(positionTakeProfitPnl(position, level))">{{ formatSignedAssetAmount(positionTakeProfitPnl(position, level)) }}</small>
                            </div>
                          </div>
                          <div v-if="smartMoneyFollowsForAccountPosition(position).length" class="binance-smart-follow-account-info">
                            <div v-for="follow in smartMoneyFollowsForAccountPosition(position)" :key="`follow-${follow.topTraderId}-${follow.symbol}-${follow.positionSide}`" class="binance-smart-follow-account-source">
                              <div>
                                <span>聪明钱跟随</span>
                                <strong>{{ smartMoneyFollowSourceName(follow) }}</strong>
                                <el-button text size="small" type="danger" :icon="X" :loading="copyTradingFollowSavingKey === smartMoneyFollowKey(follow)" @click="cancelSmartMoneyFollow(follow)">取消跟随</el-button>
                              </div>
                              <template v-if="smartMoneyPositionForAccountFollow(position, follow)">
                                <small :class="changeDisplayClass(copyTradingPositionPnlPercent(smartMoneyPositionForAccountFollow(position, follow)))">对方盈亏率 {{ formatSignedPercent(copyTradingPositionPnlPercent(smartMoneyPositionForAccountFollow(position, follow))) }}</small>
                                <small
                                  :class="['binance-smart-follow-account-value', { 'is-value-alert': smartMoneyFollowPositionValueAlert(position, follow) }]"
                                  :title="smartMoneyFollowPositionValueAlert(position, follow) ? `推荐仓位价值与当前仓位价值相差 ${formatAssetAmountTwoDecimals(smartMoneyFollowPositionValueDeviation(position, follow))} USDT` : undefined"
                                >
                                  <span>当前/推荐仓位价值 {{ formatAssetAmountTwoDecimalsOrPlaceholder(copyTradingOwnPositionValue(position)) }} / {{ formatRecommendedPositionValue(smartMoneyFollowRecommendedPositionValue(position, follow)) }}</span>
                                  <em
                                    v-if="smartMoneyFollowPositionValueDeviationPercent(position, follow) !== null"
                                    :class="changeDisplayClass(smartMoneyFollowPositionValueDifference(position, follow))"
                                  >{{ formatSignedAssetAmountTwoDecimals(smartMoneyFollowPositionValueDifference(position, follow)) }}（{{ formatRatioCompletionPercent(smartMoneyFollowPositionValueDeviationPercent(position, follow)) }}）</em>
                                </small>
                              </template>
                              <small v-else-if="smartMoneyFollowSnapshotError(follow)" class="is-warning">对方仓位读取失败</small>
                              <small v-else class="is-warning">对方已无该持仓</small>
                            </div>
                          </div>
                          <div class="binance-futures-position-foot-actions">
                            <el-button
                              v-if="realPositionMonitorFor(position)"
                              plain
                              size="small"
                              type="success"
                              :icon="ShieldCheck"
                              disabled
                            >监控中</el-button>
                            <el-button
                              v-else
                              plain
                              size="small"
                              type="primary"
                              :icon="Plus"
                              :loading="monitorPreviewLoadingKey === futuresPositionMonitorKey(position)"
                              @click="openLivePositionMonitorPreview(position)"
                            >添加监控</el-button>
                            <el-button plain size="small" :icon="Pencil" @click="openFuturesProtectionDialog(position)">保护设置</el-button>
                            <el-button plain size="small" type="danger" :icon="X" @click="openMarketCloseDialog(position)">市价平仓</el-button>
                          </div>
                        </div>
                      </div>
                    </div>
                  </Transition>
                </article>
              </div>
              <div v-if="futuresPositions.length" class="binance-futures-position-smoothing">
                <span>盈亏走势平滑</span>
                <el-slider v-model="futuresPositionSmoothing" :min="0" :max="1" :step="0.05" :show-tooltip="false" @change="saveFuturesPositionSmoothing" />
                <strong>{{ Math.round(futuresPositionSmoothing * 100) }}%</strong>
              </div>
              <p v-else class="binance-inline-empty">暂无合约持仓</p>
            </template>
            <div v-else class="binance-simulated-market-error"><Activity :size="15" /><span>{{ account.futures?.error || '当前 API 没有合约账户读取权限' }}</span></div>
          </div>
          <div v-else class="binance-inline-empty"><WalletCards :size="18" /><span>{{ currentUser ? (credentialsProfile.configured ? (accountSnapshotError ? `账户数据读取失败：${accountSnapshotError}` : '后台正在读取账户资产...') : '请在账户 API 设置中保存配置，后台会自动读取资产') : '登录后自动读取账户资产' }}</span></div>
        </section>
    </aside>
    </div>

    <el-dialog v-model="accountSettingsVisible" title="Binance 设置" width="580px" class="binance-position-dialog binance-account-settings-dialog responsive-dialog" :style="{ '--responsive-dialog-width': '580px', maxWidth: 'calc(100vw - 24px)', margin: '12px auto' }" append-to-body align-center>
      <div v-if="!currentUser" class="binance-auth-gate">
        <div class="binance-auth-icon"><LockKeyhole :size="20" /></div>
        <strong>登录后配置 Binance 设置</strong>
        <p>登录账户后可管理账户 API 与策略默认参数。</p>
        <el-button type="primary" :icon="LogIn" @click="$emit('login-request')">登录或注册</el-button>
      </div>
      <template v-else>
        <el-tabs v-model="accountSettingsTab" class="binance-account-settings-tabs">
          <el-tab-pane label="连接设置" name="connection">
            <div class="binance-connection-settings-sections">
              <section class="binance-connection-settings-section">
                <div class="binance-connection-settings-section-head"><strong>账户 API</strong></div>
                <div class="binance-account-settings-status">
                  <span :class="['binance-connection-state', connected ? 'is-connected' : '']"><i></i>{{ connected ? '后台连接正常' : (credentialsProfile.configured ? '后台正在重试连接' : '尚未配置') }}</span>
                </div>
                <div v-if="credentialsProfile.configured" class="binance-stored-credentials">
                  <div><span>已保存 API Key</span><strong>{{ credentialsProfile.apiKeyMasked }}</strong></div>
                  <div><el-button link type="danger" :icon="Trash2" @click="removeSavedCredentials">删除</el-button></div>
                </div>
                <p v-if="credentialsProfile.requiresReconfiguration" class="binance-settings-warning">当前保存的连接配置已停用，请重新保存 Binance 主网 API 配置。</p>
                <el-form v-if="!credentialsProfile.configured" label-position="top" class="binance-credential-form" @submit.prevent="connectAccount">
                  <el-form-item label="API Key">
                    <el-input v-model="apiKey" autocomplete="off" placeholder="粘贴 API Key" />
                  </el-form-item>
                  <el-form-item label="Secret Key">
                    <el-input v-model="apiSecret" type="password" show-password autocomplete="new-password" placeholder="粘贴 Secret Key" />
                  </el-form-item>
                </el-form>
              </section>
              <section class="binance-connection-settings-section">
                <div class="binance-connection-settings-section-head">
                  <div class="binance-smart-money-auth-head-copy"><strong>聪明钱登录态</strong><small>{{ copyTradingSettings.smartMoneyAuthConfigured ? '已保存加密登录态' : '尚未配置' }}</small></div>
                  <el-button plain size="small" :icon="ScanSearch" :loading="smartMoneyAuthCapturing" :disabled="copyTradingSettingsLoading || copyTradingSettingsSaving" @click="captureSmartMoneyAuth">打开 Binance 并获取登录态</el-button>
                </div>
                <div class="binance-smart-money-auth-grid">
                  <el-form-item label="Cookie（必填）">
                    <el-input v-model="smartMoneyAuthForm.cookie" autocomplete="off" placeholder="粘贴请求 Header 中的 Cookie 值" />
                  </el-form-item>
                  <el-form-item label="csrftoken（必填）">
                    <el-input v-model="smartMoneyAuthForm.csrfToken" type="password" show-password autocomplete="off" placeholder="粘贴请求 Header 中的 csrftoken" />
                  </el-form-item>
                </div>
              </section>
              <section class="binance-connection-settings-section">
                <div class="binance-connection-settings-section-head"><strong>连接方式</strong></div>
                <section class="binance-connection-panel" aria-live="polite">
                  <div class="binance-connection-mode-head">
                    <strong>后台连接方式</strong>
                    <span :class="['binance-connection-mode-badge', connectionSettings.connectionMode === 'WEBSOCKET' ? 'is-websocket' : 'is-rest']">
                      {{ connectionSettings.connectionMode === 'WEBSOCKET' ? 'WebSocket' : 'REST' }}
                    </span>
                  </div>
                  <el-radio-group v-model="connectionSettings.connectionMode" class="binance-connection-mode-switch" aria-label="后台连接方式" :disabled="connectionSettingsLoading || connectionSettingsSaving">
                    <el-radio-button value="REST">REST</el-radio-button>
                    <el-radio-button value="WEBSOCKET">WebSocket</el-radio-button>
                  </el-radio-group>
                  <div class="binance-connection-lines">
                    <article :class="['binance-connection-line-card', connectionLineClass(connectionStatus?.account)]">
                      <div>
                        <span>账户资产线路</span>
                        <strong>{{ connectionLineLabel(connectionStatus?.account) }}</strong>
                      </div>
                      <small>{{ connectionLineDetail(connectionStatus?.account, '账户资产') }}</small>
                    </article>
                    <article :class="['binance-connection-line-card', connectionLineClass(connectionStatus?.market)]">
                      <div>
                        <span>订阅行情线路</span>
                        <strong>{{ connectionLineLabel(connectionStatus?.market) }}</strong>
                      </div>
                      <small>{{ connectionLineDetail(connectionStatus?.market, '监控与图表行情') }}</small>
                    </article>
                  </div>
                </section>
              </section>
              <section class="binance-connection-settings-section">
                <div class="binance-connection-settings-section-head"><strong>WebSocket 测试</strong></div>
                <section class="binance-websocket-test-panel" aria-live="polite">
                  <div class="binance-websocket-test-actions">
                    <el-button type="primary" :icon="Activity" :loading="websocketTestLoading" :disabled="websocketAccountTestLoading" @click="runWebsocketTest">测试行情订阅</el-button>
                    <el-button plain :icon="WalletCards" :loading="websocketAccountTestLoading" :disabled="websocketTestLoading" @click="runWebsocketAccountTest">测试账户持仓</el-button>
                  </div>
                  <div v-if="websocketTestKind === 'market' && websocketTestResult" :class="['binance-websocket-test-result', websocketTestResult.ok ? 'is-success' : 'is-error']">
                    <strong>{{ websocketTestResult.ok ? '行情 WebSocket 已连通并完成订阅确认' : '行情连接未通过' }}</strong>
                    <small v-if="websocketTestResult.ok">握手 {{ websocketTestResult.connectedMs }}ms · 订阅确认 {{ websocketTestResult.firstMessageMs }}ms · {{ websocketTestResult.proxyUsed ? '经后台当前代理' : '直连' }}</small>
                    <small v-else>{{ websocketTestResult.message }}</small>
                  </div>
                  <div v-else-if="websocketTestKind === 'account' && websocketAccountTestResult" :class="['binance-websocket-test-result', websocketAccountTestResult.ok ? 'is-success' : 'is-error']">
                    <strong>{{ websocketAccountTestResult.ok ? '账户持仓 WebSocket 查询成功' : '账户持仓查询未通过' }}</strong>
                    <small v-if="websocketAccountTestResult.ok">响应 {{ websocketAccountTestResult.responseMs }}ms · account.status · 返回 {{ websocketAccountTestResult.rawPositionCount }} 条仓位记录 · 当前非零持仓 {{ websocketAccountTestResult.positionCount }} 个 · {{ websocketAccountTestResult.proxyUsed ? '经后台当前代理' : '直连' }}</small>
                    <small v-else>{{ websocketAccountTestResult.message }}</small>
                    <template v-if="websocketAccountTestResult.ok">
                      <div class="binance-websocket-account-summary">
                        <div><span>交易权限</span><strong>{{ websocketAccountTestResult.account?.canTrade ? '已开启' : '未开启' }}</strong></div>
                        <div><span>钱包余额</span><strong>{{ formatAssetAmount(websocketAccountTestResult.account?.totalWalletBalance) }} USDT</strong></div>
                        <div><span>可用余额</span><strong>{{ formatAssetAmount(websocketAccountTestResult.account?.availableBalance) }} USDT</strong></div>
                        <div><span>未实现盈亏</span><strong :class="changeDisplayClass(websocketAccountTestResult.account?.totalUnrealizedProfit)">{{ formatSignedAssetAmount(websocketAccountTestResult.account?.totalUnrealizedProfit) }} USDT</strong></div>
                        <div><span>资产 / 非零持仓</span><strong>{{ websocketAccountTestResult.assetCount }} / {{ websocketAccountTestResult.positionCount }}</strong></div>
                      </div>
                      <div v-if="websocketAccountTestResult.positions?.length" class="binance-websocket-account-positions">
                        <div v-for="position in websocketAccountTestResult.positions" :key="`${position.symbol}-${position.positionSide}`" class="binance-websocket-account-position">
                          <div><strong>{{ position.symbol }}</strong><em :class="directionClass(position.side)">{{ position.side === 'LONG' ? '做多' : '做空' }}</em><small>{{ position.positionSide }} · {{ position.leverage || '--' }}x</small></div>
                          <div><span>数量 / 开仓价</span><strong>{{ formatCrypto(position.quantity) }} / {{ formatPrecisePrice(position.entryPrice) }}</strong></div>
                          <div><span>标记价 / 未实现盈亏</span><strong>{{ formatPrecisePrice(position.markPrice) }} / <em :class="changeDisplayClass(position.unrealizedProfit)">{{ formatSignedAssetAmount(position.unrealizedProfit) }}</em></strong></div>
                          <div><span>强平价 / 仓位价值</span><strong>{{ formatPrecisePrice(position.liquidationPrice) }} / {{ position.notional == null ? '--' : formatAssetAmount(position.notional) }}</strong></div>
                        </div>
                      </div>
                      <small v-else class="binance-websocket-account-empty">当前没有非零合约持仓；交易所仍返回了 {{ websocketAccountTestResult.rawPositionCount }} 条仓位记录。</small>
                    </template>
                  </div>
                </section>
              </section>
            </div>
          </el-tab-pane>
          <el-tab-pane label="邮件通知" name="notifications">
            <el-form v-loading="notificationSettingsLoading" label-position="top" class="binance-credential-form" @submit.prevent="saveNotificationSettings">
              <section class="binance-connection-settings-section">
                <div class="binance-connection-settings-section-head"><strong>提醒邮箱</strong><small>{{ notificationSettings.email ? '已启用 A 股与 Binance 共用提醒' : '尚未配置' }}</small></div>
                <el-form-item label="邮箱地址">
                  <el-input v-model="notificationSettings.email" type="email" autocomplete="email" placeholder="留空则关闭邮件提醒" />
                </el-form-item>
                <p class="binance-secret-note"><ShieldCheck :size="15" />此邮箱为当前账户的共享通知设置，A 股与 Binance 均会使用。</p>
              </section>
            </el-form>
          </el-tab-pane>
          <el-tab-pane label="策略设置" name="strategy">
            <el-form v-loading="strategySettingsLoading" :disabled="strategySettingsLoading || strategySettingsSaving" label-position="top" class="binance-strategy-settings-form" @submit.prevent="saveStrategySettings">
              <div class="binance-strategy-settings-sections">
                <section class="binance-strategy-settings-section">
                  <div class="binance-strategy-settings-section-head"><strong>策略路径</strong><small>选择分析路线与点位来源</small></div>
                  <div class="binance-strategy-settings-grid">
                <el-form-item label="策略引擎" class="binance-strategy-route-field">
                  <el-radio-group v-model="strategySettings.strategyEngine" class="binance-level-strategy-switch" aria-label="策略引擎">
                    <el-radio-button value="CLASSIC">经典策略</el-radio-button>
                    <el-radio-button value="MODEL">时序模型</el-radio-button>
                  </el-radio-group>
                  <small v-if="strategySettings.strategyEngine === 'MODEL'" class="binance-strategy-setting-help">时序模型直接从 4h / 1h / 15m / 5m 已收盘序列生成方向、入场、止损、目标和移动止损参数；所选检查点的持仓档案决定回放、ATR、移动止损与持仓管理周期，5m 仅作输入和入场时机。经典策略的模式、点位路线、确认、止损和止盈参数不参与本次模型计划。</small>
                  <small v-else class="binance-strategy-setting-help">经典策略完全按结构纪律生成；时序模型直接从 4h / 1h / 15m / 5m 已收盘序列生成方向、入场、止损、目标和移动止损参数。模型不可用时返回 WAIT，不回退经典策略。</small>
                </el-form-item>
                <el-form-item label="时序模型分支" class="binance-strategy-route-field" v-if="strategySettings.strategyEngine === 'MODEL'">
                  <el-radio-group v-model="strategySettings.modelBranch" class="binance-level-strategy-switch" aria-label="时序模型分支">
                    <el-radio-button value="BEST">验证最佳</el-radio-button>
                    <el-radio-button value="STABLE">稳定末期</el-radio-button>
                  </el-radio-group>
                  <small class="binance-strategy-setting-help">验证最佳使用时间外筛选表现最好的 epoch；稳定末期使用训练末十轮中最接近稳定中位表现的 epoch。</small>
                </el-form-item>
                <el-form-item label="使用训练模型" class="binance-strategy-route-field" v-if="strategySettings.strategyEngine === 'MODEL'">
                  <el-select :model-value="selectedModelSelectionKey" class="binance-position-field" clearable placeholder="跟随最新完成训练" @change="applyModelSelectionKey">
                    <el-option
                      v-for="model in selectableModelOptions"
                      :key="modelSelectionKey(model)"
                      :label="modelOptionLabel(model)"
                      :value="modelSelectionKey(model)"
                    />
                  </el-select>
                  <div class="binance-selected-model-summary" aria-live="polite">
                    <div class="binance-selected-model-summary-head">
                      <span>当前推理检查点</span>
                      <em>{{ selectedModel ? (strategySettings.modelRunId ? '已固定选择' : '跟随最新完成训练') : '尚无可加载检查点' }}</em>
                    </div>
                    <template v-if="selectedModel">
                      <div class="binance-selected-model-summary-grid">
                        <div><span>模型版本</span><strong class="is-breakable" :title="selectedModel.modelVersion || ''">{{ modelVersionDisplay(selectedModel.modelVersion) }}</strong></div>
                        <div><span>训练任务 / 分支</span><strong class="is-breakable">{{ selectedModel.runId || '--' }} · {{ selectedModel.branch || '--' }}</strong></div>
                        <div><span>选定轮次</span><strong>第 {{ selectedModel.selectedEpoch || '--' }} 轮</strong></div>
                        <div><span>持仓档案</span><strong>{{ modelHoldingProfileLabel(selectedModel.holdingProfile || selectedModel.config?.holdingProfile) }} · {{ selectedModel.executionInterval || selectedModel.config?.executionInterval || '--' }} 管理</strong></div>
                        <div><span>测试集覆盖率</span><strong>{{ formatModelPercent(selectedModel.test?.selectedCoverage) }}</strong></div>
                        <div><span>测试集胜率</span><strong>{{ formatModelPercent(selectedModel.test?.selectedWinRate) }}</strong></div>
                        <div><span>测试实现 R</span><strong>{{ formatModelR(selectedModel.test?.selectedRealizedR) }}</strong></div>
                        <div><span>测试实现效用</span><strong>{{ formatModelR(selectedModel.test?.selectedRealizedUtility) }}</strong></div>
                        <div><span>WAIT 比例</span><strong>{{ formatModelPercent(selectedModel.test?.waitRate) }}</strong></div>
                        <div><span>平均持仓</span><strong>{{ formatModelBars(selectedModel.test?.selectedMeanDurationBars, modelExecutionIntervalFor(selectedModel)) }}</strong></div>
                      </div>
                    </template>
                    <small v-else>训练完成并生成可加载的 BEST/STABLE 检查点后，这里会显示其版本和测试集性能。</small>
                  </div>
                  <small class="binance-strategy-setting-help">不选择时跟随最新完成任务；选择后实时行情、目标寻找和点位更新会固定使用该训练任务的 {{ strategySettings.modelBranch }} 分支。</small>
                </el-form-item>
                <el-form-item v-if="strategySettings.strategyEngine !== 'MODEL'" label="策略模式" class="binance-strategy-route-field">
                  <el-radio-group v-model="strategySettings.strategyMode" class="binance-level-strategy-switch" aria-label="策略模式">
                    <el-radio-button value="MIDLINE">中线模式</el-radio-button>
                    <el-radio-button value="SHORT_TERM">短线模式</el-radio-button>
                  </el-radio-group>
                  <small class="binance-strategy-setting-help">中线：4h → 1h → 15m 主分析，5m 仅优化入场；短线：4h 仍作隐藏方向否决，1h 做环境、15m 看位置与结构、5m 执行触发。两种模式都只用已收盘 K 线，不因周期变小放宽纪律。</small>
                </el-form-item>
                <el-form-item v-if="strategySettings.strategyEngine !== 'MODEL'" label="点位策略路线" class="binance-strategy-route-field">
                  <el-radio-group v-model="strategySettings.levelStrategy" class="binance-level-strategy-switch" aria-label="点位策略路线">
                    <el-radio-button value="STRUCTURE_EXTREME">结构极值</el-radio-button>
                    <el-radio-button value="CONFIRMED_PLATFORM">确认平台/区域</el-radio-button>
                  </el-radio-group>
                  <small class="binance-strategy-setting-help">结构极值：初始止损取近 8 根 15m K 的结构极值。确认平台/区域：仅使用近 24 根已收盘 15m K 中至少 3 次测试、且价格差不超过 0.35 ATR 的平台；多头止损在支撑平台下沿外，第一目标取阻力平台下沿，空头完全镜像。未形成平台时自动回退到结构极值。这些阈值是工程判定，不是书中的固定参数。</small>
                </el-form-item>
                  </div>
                </section>
                <section class="binance-strategy-settings-section">
                  <div class="binance-strategy-settings-section-head"><strong>账户风控</strong><small>限制单笔、组合与当日风险</small></div>
                  <div class="binance-strategy-settings-grid">
                <el-form-item label="账户最大亏损比">
                  <el-input-number v-model="strategySettings.maxAccountLossRatio" class="binance-position-field" :min="0.01" :max="100" :precision="2" :step="0.5" controls-position="right" />
                  <small class="binance-strategy-setting-help">风险预算 = 账户权益 × 该比例 ÷ 100；用于约束持仓风险止损对应的账户最大可承受亏损。</small>
                </el-form-item>
                <el-form-item label="组合结构风险上限">
                  <el-input-number v-model="strategySettings.maxPortfolioRiskRatio" class="binance-position-field" :min="0.01" :max="100" :precision="2" :step="0.5" controls-position="right" />
                  <small class="binance-strategy-setting-help">新计划与已有仓位到各自结构止损的剩余风险之和超过此比例时，不再开新仓。</small>
                </el-form-item>
                <el-form-item label="同向持仓上限">
                  <el-input-number v-model="strategySettings.maxSameSidePositions" class="binance-position-field" :min="1" :max="20" :precision="0" :step="1" controls-position="right" />
                  <small class="binance-strategy-setting-help">限制同一方向同时承担的结构风险，避免在同一行情判断上重复加码。</small>
                </el-form-item>
                <el-form-item label="单日风险上限">
                  <el-input-number v-model="strategySettings.dailyLossLimitRatio" class="binance-position-field" :min="0.01" :max="100" :precision="2" :step="0.5" controls-position="right" />
                  <small class="binance-strategy-setting-help">当日已实现亏损加上在途结构风险超过此比例时，当日停止新开仓。</small>
                </el-form-item>
                  </div>
                </section>
                <section v-if="strategySettings.strategyEngine !== 'MODEL'" class="binance-strategy-settings-section">
                  <div class="binance-strategy-settings-section-head"><strong>环境与目标空间</strong><small>定义区间位置与最低目标空间</small></div>
                  <div class="binance-strategy-settings-grid">
                <el-form-item label="区间边缘比例">
                  <el-input-number v-model="strategySettings.rangeEdgeFraction" class="binance-position-field" :min="0.05" :max="0.49" :precision="2" :step="0.01" controls-position="right" />
                  <small class="binance-strategy-setting-help">只把区间下方或上方这部分定义为可反向交易的边缘，中部不追单。</small>
                </el-form-item>
                <el-form-item label="区间最小目标 R">
                  <el-input-number v-model="strategySettings.rangeMinimumTargetR" class="binance-position-field" :min="0.5" :max="10" :precision="2" :step="0.25" controls-position="right" />
                  <small class="binance-strategy-setting-help">区间边缘计划需要保留至少此倍数的第一目标空间，否则只观察。</small>
                </el-form-item>
                <el-form-item label="趋势最小目标 R">
                  <el-input-number v-model="strategySettings.trendMinimumTargetR" class="binance-position-field" :min="0.5" :max="10" :precision="2" :step="0.25" controls-position="right" />
                  <small class="binance-strategy-setting-help">趋势回调计划需要保留至少此倍数的第一结构目标空间；该门槛只筛掉空间不足的追价，不改变结构止损。</small>
                </el-form-item>
                  </div>
                </section>
                <section v-if="strategySettings.strategyEngine !== 'MODEL'" class="binance-strategy-settings-section">
                  <div class="binance-strategy-settings-section-head"><strong>入场确认与失败退出</strong><small>控制触发确认和早期反向失败复核</small></div>
                  <div class="binance-strategy-settings-grid">
                <el-form-item label="入场确认方式" class="binance-strategy-route-field">
                  <el-radio-group v-model="strategySettings.entryConfirmationMode" class="binance-level-strategy-switch" aria-label="入场确认方式">
                    <el-radio-button value="RETEST_REQUIRED">等待 5m 回测</el-radio-button>
                    <el-radio-button value="TRIGGER_ONLY">仅触发</el-radio-button>
                  </el-radio-group>
                    <small class="binance-strategy-setting-help">中线默认按 4h/1h/15m 的完整结构执行；短线由 5m 执行，但仍须通过 4h/1h/15m 的方向、位置、结构止损与目标空间。等待 5m 回测是更严格的可选确认，不会改变主周期条件。</small>
                </el-form-item>
                <el-form-item label="确认有效 K 线数">
                  <el-input-number v-model="strategySettings.entryConfirmationExpiryBars" class="binance-position-field" :min="1" :max="8" :precision="0" :step="1" controls-position="right" />
                  <small class="binance-strategy-setting-help">宏观计划在这个 15m K 线窗口内未得到 5m 确认即取消。</small>
                </el-form-item>
                <el-form-item label="早期失败复核 K 线数">
                  <el-input-number v-model="strategySettings.entryFailureExitBars" class="binance-position-field" :min="1" :max="8" :precision="0" :step="1" controls-position="right" />
                  <small class="binance-strategy-setting-help">开仓后仅在这个早期窗口复核强反向失败，普通重叠 K 不退出。</small>
                </el-form-item>
                <el-form-item label="强反向实体 ATR 倍数">
                  <el-input-number v-model="strategySettings.entryFailureBodyAtrMultiplier" class="binance-position-field" :min="0.1" :max="5" :precision="2" :step="0.1" controls-position="right" />
                  <small class="binance-strategy-setting-help">反向实体必须达到此 ATR 倍数且穿回触发区，再等待后续跟随确认失败。</small>
                </el-form-item>
                  </div>
                </section>
                <section v-if="strategySettings.strategyEngine !== 'MODEL'" class="binance-strategy-settings-section">
                  <div class="binance-strategy-settings-section-head"><strong>止损管理</strong><small>设置移动止损的启动、结构与波动缓冲</small></div>
                  <div class="binance-strategy-settings-grid">
                <el-form-item label="移动止损启动 R 倍数">
                  <el-input-number v-model="strategySettings.movingStopActivationR" class="binance-position-field" :min="0.01" :max="20" :precision="2" :step="0.1" controls-position="right" />
                  <small class="binance-strategy-setting-help">R = |入场价 - 初始止损|。持仓有利极值相对入场的收益 ÷ R 达到该值后，才启动移动止损。</small>
                </el-form-item>
                <el-form-item label="极点 ATR 移动止损倍数">
                  <el-input-number v-model="strategySettings.trailingAtrMultiplier" class="binance-position-field" :min="0.01" :max="20" :precision="2" :step="0.1" controls-position="right" />
                  <small class="binance-strategy-setting-help">多头候选 = 持仓期间最高价 - ATR × 倍数；空头候选 = 持仓期间最低价 + ATR × 倍数。使用持仓期 K 线极值，不依赖单次当前价。</small>
                </el-form-item>
                <el-form-item label="结构止损 ATR 缓冲">
                  <el-input-number v-model="strategySettings.structureStopAtrMultiplier" class="binance-position-field" :min="0" :max="10" :precision="2" :step="0.01" controls-position="right" />
                  <small class="binance-strategy-setting-help">多头止损 = 结构下界 - ATR × 倍数；空头止损 = 结构上界 + ATR × 倍数，并始终保持在完整触发区外。</small>
                </el-form-item>
                <el-form-item label="触发区防假突破缓冲">
                  <el-input-number v-model="strategySettings.triggerZoneStopBufferAtrMultiplier" class="binance-position-field" :min="0.1" :max="10" :precision="2" :step="0.1" controls-position="right" />
                  <small class="binance-strategy-setting-help">止损与触发区防守侧至少间隔 max(ATR × 倍数，触发区高度 × 25%)；默认 0.5 ATR。回撤/回测结构极值更远时仍取更远者；空间不足会筛掉计划，不会把止损收紧。</small>
                </el-form-item>
                <el-form-item label="保本缓冲 ATR 倍数">
                  <el-input-number v-model="strategySettings.breakevenBufferAtrMultiplier" class="binance-position-field" :min="0" :max="10" :precision="2" :step="0.01" controls-position="right" />
                  <small class="binance-strategy-setting-help">多头保本线 = 入场价 + ATR × 倍数；空头反向。仅在已启动且极点移动止损仍在亏损侧时作为兜底。</small>
                </el-form-item>
                  </div>
                </section>
                <section v-if="strategySettings.strategyEngine !== 'MODEL'" class="binance-strategy-settings-section">
                  <div class="binance-strategy-settings-section-head"><strong>止盈与目标分配</strong><small>设置近端、第一档和第二档累计比例</small></div>
                  <div class="binance-strategy-settings-grid">
                <el-form-item label="近端止盈最低 R">
                  <el-input-number v-model="strategySettings.nearTermMinimumTargetR" class="binance-position-field" :min="0.05" :max="5" :precision="2" :step="0.05" controls-position="right" />
                  <small class="binance-strategy-setting-help">近端结构磁力位距离触发价至少达到此 R；默认 0.5R。该值只筛选近端止盈，不改变第一止盈和移动止损。</small>
                </el-form-item>
                <el-form-item label="近端保护止盈累计比例">
                  <el-input-number v-model="strategySettings.protectiveTakeProfitRatio" class="binance-position-field" :min="1" :max="98" :precision="1" :step="1" controls-position="right" />
                  <small class="binance-strategy-setting-help">有近端保护目标时，首档按该累计比例分配数量；必须小于第一止盈累计比例。近端目标来自当前路线最近 20 根 5m/15m 的已确认平台或局部结构，不是固定价格。</small>
                </el-form-item>
                <el-form-item label="第一止盈累计比例">
                  <el-input-number v-model="strategySettings.firstTakeProfitRatio" class="binance-position-field" :min="1" :max="99" :precision="1" :step="1" controls-position="right" />
                  <small class="binance-strategy-setting-help">到达第一止盈时累计平仓该比例；若已有近端止盈，第一止盈单只提交两者的数量差，点位来自本次计划结构。</small>
                </el-form-item>
                <el-form-item label="第二止盈累计比例">
                  <el-input-number v-model="strategySettings.secondTakeProfitRatio" class="binance-position-field" :min="1" :max="99" :precision="1" :step="1" controls-position="right" />
                  <small class="binance-strategy-setting-help">第二档本次数量 = 总仓位 ×（第二档累计比例 - 第一档累计比例）÷ 100；仅在本次计划确认扩展目标时下第二档。没有第二确认平台时，不下第二档，第一档后的余仓 = 总仓位 ×（100 - 第一档累计比例）÷ 100，交由移动止损管理。</small>
                </el-form-item>
                  </div>
                </section>
              </div>
              <p v-if="strategySettings.strategyEngine !== 'MODEL'" class="binance-strategy-settings-note">保存后，下一次“寻找目标”和新启动回测使用当前路线与默认值；已开始执行的计划保持创建时的策略快照。第二档本次止盈 {{ formatTargetPercent(strategySecondTakeProfitLegRatio) }}，剩余 {{ formatTargetPercent(strategyRemainingTakeProfitRatio) }} 由移动止损管理。</p>
              <p v-else class="binance-strategy-settings-note">保存后，下一次“寻找目标”和新启动回测使用选定的训练模型；模型计划的环境、入场、点位、移动止损和止盈分配由模型输出，账户风控仍按上方设置执行。</p>
            </el-form>
          </el-tab-pane>
        </el-tabs>
      </template>
      <template #footer>
        <el-button @click="accountSettingsVisible = false">关闭</el-button>
        <template v-if="currentUser && accountSettingsTab === 'connection'">
          <el-button v-if="!credentialsProfile.configured" type="primary" :icon="Link2" :loading="connectionLoading" @click="connectAccount">保存并连接</el-button>
          <el-button type="primary" plain :loading="copyTradingSettingsSaving" :disabled="copyTradingSettingsLoading" @click="saveSmartMoneyAuthSettings">保存聪明钱登录态</el-button>
          <el-button type="primary" plain :loading="connectionSettingsSaving" :disabled="connectionSettingsLoading" @click="saveConnectionSettings">保存连接方式</el-button>
        </template>
        <template v-else-if="currentUser && accountSettingsTab === 'notifications'">
          <el-button plain :loading="notificationTestSending" :disabled="!notificationSettings.email" @click="sendNotificationTestEmail">发送模板测试邮件</el-button>
          <el-button type="primary" :loading="notificationSettingsSaving" :disabled="notificationSettingsLoading" @click="saveNotificationSettings">保存邮件设置</el-button>
        </template>
        <el-button v-else-if="currentUser && accountSettingsTab === 'strategy'" type="primary" :loading="strategySettingsSaving" :disabled="strategySettingsLoading" @click="saveStrategySettings">保存策略设置</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="livePositionMonitorPreviewVisible"
      title="添加真实持仓监控"
      width="580px"
      class="binance-position-dialog binance-live-monitor-preview-dialog"
      :style="{ maxWidth: 'calc(100vw - 24px)', margin: '12px auto' }"
      append-to-body
      align-center
    >
      <template v-if="livePositionMonitorPreview">
        <section class="binance-live-monitor-preview-head">
          <div>
            <strong>{{ livePositionMonitorPreview.position.symbol }}</strong>
            <span :class="directionClass(livePositionMonitorPreview.position.side)">{{ livePositionMonitorPreview.position.side === 'LONG' ? '做多' : '做空' }}</span>
          </div>
          <small>该计划用于当前真实仓位保护与动态止损，不构成新的入场建议。</small>
        </section>
        <p v-if="livePositionMonitorPreview.position.accountSnapshotStale" class="binance-live-monitor-preview-stale">账户数据正在使用最后有效快照。可先创建监控；真实止盈止损将在后台可靠刷新账户后自动提交。</p>
        <section class="binance-live-monitor-preview-grid" aria-label="真实持仓信息">
          <div><span>开仓均价</span><strong>{{ formatPrecisePrice(livePositionMonitorPreview.position.costPrice) }}</strong></div>
          <div><span>持仓数量</span><strong>{{ formatCrypto(livePositionMonitorPreview.position.quantity) }}</strong></div>
          <div><span>杠杆 / 名义金额</span><strong>{{ livePositionMonitorPreview.position.leverage }}x / {{ formatAssetAmount(livePositionMonitorPreview.position.notional) }}</strong></div>
          <div><span>保证金占比</span><strong>{{ livePositionMonitorPreview.position.marginRatio == null ? '--' : formatRatioPercent(livePositionMonitorPreview.position.marginRatio) }}</strong></div>
          <div><span>标记价</span><strong>{{ formatPrecisePrice(livePositionMonitorPreview.position.markPrice) }}</strong></div>
          <div><span>持仓风险预算</span><strong>{{ livePositionMonitorPreview.position.positionRiskBudget == null ? '--' : formatAssetAmount(livePositionMonitorPreview.position.positionRiskBudget) }}</strong></div>
        </section>
        <section class="binance-live-monitor-preview-levels" aria-label="监控计划点位">
          <div><span>结构止损</span><strong>{{ formatPrecisePrice(livePositionMonitorPreview.plan.stopLoss) }}</strong></div>
          <div><span>持仓风险止损</span><strong>{{ formatPrecisePrice(livePositionMonitorPreview.position.positionRiskStop) }}</strong></div>
          <div>
            <span>移动止损</span>
            <strong>{{ livePositionMonitorPreview.plan.movingStopActive ? formatPrecisePrice(livePositionMonitorPreview.plan.movingStop) : '未启用' }}</strong>
            <small v-if="livePositionMonitorPreview.plan.movingStopActive">已达到 {{ livePositionMonitorPreview.plan.movingStopActivationR }}R 启用条件</small>
            <small v-else>达到 {{ livePositionMonitorPreview.plan.movingStopActivationR }}R 后启用</small>
          </div>
          <div>
            <span>当前有效止损</span>
            <strong>{{ formatPrecisePrice(livePositionMonitorPreview.plan.activeStop) }}</strong>
            <small>{{ livePositionMonitorPreview.plan.activeStopSource === 'MOVING' ? '当前使用移动止损' : livePositionMonitorPreview.plan.activeStopSource === 'POSITION_RISK' ? '当前使用持仓风险止损' : '当前使用结构止损' }}</small>
          </div>
          <div><span>第一止盈</span><strong>{{ formatPrecisePrice(planTarget(livePositionMonitorPreview.plan, 'FIRST_TARGET', 0)?.price) }}</strong></div>
          <div><span>第二止盈</span><strong>{{ formatPrecisePrice(planTarget(livePositionMonitorPreview.plan, 'EXTENSION_TARGET', 1)?.price) }}</strong></div>
        </section>
        <section class="binance-live-monitor-preview-reasons">
          <strong>计划说明</strong>
          <p>{{ livePositionMonitorPreview.plan.monitoringPlanReason }}</p>
          <ul v-if="livePositionMonitorPreview.plan.reasons?.length">
            <li v-for="reason in livePositionMonitorPreview.plan.reasons.slice(0, 4)" :key="reason">{{ reason }}</li>
          </ul>
        </section>
        <p class="binance-live-monitor-preview-confirmation">确认后将创建真实持仓监控，仅同步必要的止盈、止损和移动止损，不要求满足新的入场计划条件。</p>
      </template>
      <template #footer>
        <el-button :disabled="livePositionMonitorSaving" @click="livePositionMonitorPreviewVisible = false">取消</el-button>
        <el-button type="primary" :loading="livePositionMonitorSaving" :disabled="!livePositionMonitorPreview" @click="confirmLivePositionMonitor">确认添加监控</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="copyTradeDialogVisible" title="提交跟单订单" width="600px" class="binance-position-dialog binance-copy-trade-dialog" :style="{ maxWidth: 'calc(100vw - 24px)', margin: '12px auto' }" append-to-body align-center>
      <section v-if="copyTradeRecord" class="binance-copy-trade-context">
        <div><strong>{{ copyTradeRecord.symbol }}</strong><span :class="directionClass(copyTradeTradingDirection)">{{ copyTradeTradingDirection === 'LONG' ? '做多' : '做空' }}</span></div>
        <small>来源操作：{{ formatCopyTradingTime(copyTradeRecord) }} · 参考成交价 {{ formatPrecisePrice(copyTradeRecord.avgPrice || copyTradeRecord.price) }}</small>
      </section>
      <el-form label-position="top" class="binance-position-form" @submit.prevent="submitCopyTrade">
        <div class="binance-position-form-grid">
          <el-form-item class="binance-copy-trade-order-type" label="入场方式">
            <el-segmented v-model="copyTradeForm.orderType" :options="copyOrderTypeOptions" block />
          </el-form-item>
          <el-form-item label="跟单方向">
            <el-select v-model="copyTradeForm.direction" class="binance-position-field" disabled>
              <el-option label="做多" value="LONG" />
              <el-option label="做空" value="SHORT" />
            </el-select>
          </el-form-item>
          <el-form-item label="持仓数量">
            <el-input-number v-model="copyTradeForm.quantity" class="binance-position-field" :min="quantityMinFor(copyTradeForm.symbol, copyTradeForm.orderType) || quantityStepFor(copyTradeForm.symbol, copyTradeForm.orderType)" :precision="quantityPrecisionForStep(quantityStepFor(copyTradeForm.symbol, copyTradeForm.orderType))" :step="quantityStepFor(copyTradeForm.symbol, copyTradeForm.orderType)" controls-position="right" />
            <small class="binance-quantity-rule-note">交易所最小单位 {{ formatCrypto(quantityStepFor(copyTradeForm.symbol, copyTradeForm.orderType)) }} · 最小数量 {{ quantityMinFor(copyTradeForm.symbol, copyTradeForm.orderType) > 0 ? formatCrypto(quantityMinFor(copyTradeForm.symbol, copyTradeForm.orderType)) : '--' }}</small>
          </el-form-item>
          <el-form-item label="杠杆">
            <el-input-number v-model="copyTradeForm.leverage" class="binance-position-field" :min="1" :max="20" :precision="0" :step="1" controls-position="right" />
          </el-form-item>
          <el-form-item :label="copyTradeForm.orderType === 'LIMIT' ? '限价入场价' : '参考成本价'">
            <el-input-number v-model="copyTradeForm.costPrice" class="binance-position-field" :min="0.000000000001" :precision="priceDecimalPlaces(copyTradeForm.costPrice)" :step="priceInputStep(copyTradeForm.costPrice)" controls-position="right" />
          </el-form-item>
          <el-form-item label="备注">
            <el-input v-model="copyTradeForm.note" type="textarea" :rows="2" maxlength="500" show-word-limit placeholder="记录跟单依据" />
          </el-form-item>
        </div>
        <p class="binance-copy-trade-warning">提交后向 Binance 提交真实{{ copyTradeForm.orderType === 'MARKET' ? '市价' : '限价' }}入场单；跟单不会自动创建本地计划、止盈止损或移动止损点位。</p>
      </el-form>
      <template #footer>
        <el-button @click="copyTradeDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="copyTradeSaving" @click="submitCopyTrade">提交真实跟单</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="positionDialogVisible" :title="editingPositionId ? '调整监控参数' : '创建持仓计划监控'" width="560px" class="binance-position-dialog binance-create-monitor-dialog" :style="{ maxWidth: 'calc(100vw - 24px)', margin: '12px auto' }" append-to-body align-center>
      <el-form label-position="top" class="binance-position-form" @submit.prevent="savePosition">
        <div class="binance-position-form-grid">
          <el-form-item label="交易对">
            <el-input v-model="positionForm.symbol" class="binance-position-field" placeholder="例如 BTCUSDT" :disabled="Boolean(editingPositionId)" @input="positionForm.symbol = String(positionForm.symbol || '').toUpperCase()" />
          </el-form-item>
          <el-form-item label="方向">
            <el-select v-model="positionForm.side" class="binance-position-field" placeholder="选择方向" :disabled="Boolean(editingPositionId)">
              <el-option label="做多" value="LONG" />
              <el-option label="做空" value="SHORT" />
            </el-select>
          </el-form-item>
          <el-form-item class="binance-position-quantity-mode" label="数量方式">
            <div class="binance-position-quantity-mode-control"><el-segmented v-model="positionForm.quantityMode" :options="quantityModeOptions" block /></div>
          </el-form-item>
          <el-form-item label="持仓数量">
            <el-input-number v-model="positionForm.quantity" class="binance-position-field" :min="quantityMinFor(positionForm.symbol, 'LIMIT') || quantityStepFor(positionForm.symbol, 'LIMIT')" :precision="quantityPrecisionForStep(quantityStepFor(positionForm.symbol, 'LIMIT'))" :step="quantityStepFor(positionForm.symbol, 'LIMIT')" controls-position="right" :disabled="positionForm.quantityMode === 'MARGIN_RATIO'" />
            <small class="binance-quantity-rule-note">交易所最小单位 {{ formatCrypto(quantityStepFor(positionForm.symbol, 'LIMIT')) }} · 最小数量 {{ quantityMinFor(positionForm.symbol, 'LIMIT') > 0 ? formatCrypto(quantityMinFor(positionForm.symbol, 'LIMIT')) : '--' }}</small>
          </el-form-item>
          <el-form-item v-if="positionForm.quantityMode === 'MARGIN_RATIO'" label="保证金使用比例">
            <div class="binance-margin-ratio-presets" role="group" aria-label="保证金比例快捷值">
              <button
                v-for="preset in [25, 33, 50, 75, 100]"
                :key="preset"
                type="button"
                class="binance-margin-ratio-preset"
                :class="{ 'is-selected': asNumber(positionForm.marginRatio) === preset }"
                :aria-pressed="asNumber(positionForm.marginRatio) === preset"
                @click="positionForm.marginRatio = preset"
              >{{ preset }}%</button>
            </div>
            <el-slider v-model="positionForm.marginRatio" :min="1" :max="100" :step="1" show-input />
          </el-form-item>
          <el-form-item label="持仓成本">
            <div v-if="executionPlan && entryPricePresets.length" class="binance-entry-price-presets" role="group" aria-label="挂单价预选">
              <button
                v-for="preset in entryPricePresets"
                :key="preset.key"
                type="button"
                class="binance-entry-price-preset"
                :class="{ 'is-selected': asNumber(positionForm.costPrice) === preset.price }"
                :aria-pressed="asNumber(positionForm.costPrice) === preset.price"
                @click="selectEntryPricePreset(preset)"
              >
                <span>{{ preset.label }}</span>
                <strong>{{ formatPrecisePrice(preset.price) }}</strong>
              </button>
            </div>
            <el-input-number v-model="positionForm.costPrice" class="binance-position-field" :min="0.000000000001" :precision="priceDecimalPlaces(positionForm.costPrice)" :step="priceInputStep(positionForm.costPrice)" controls-position="right" />
          </el-form-item>
          <el-form-item label="杠杆">
            <el-input-number v-model="positionForm.leverage" class="binance-position-field" :min="1" :max="20" :precision="1" :step="1" controls-position="right" />
          </el-form-item>
          <div class="binance-position-loss-inline" aria-live="polite">
            <div class="binance-position-loss-inline-label">
              <span>止损风险</span>
            </div>
            <div v-if="positionLossAdvice.available" class="binance-position-loss-inline-values">
              <div class="binance-position-loss-main">
                <strong>预计亏损 {{ formatAssetAmount(positionLossAdvice.estimatedLoss) }}</strong>
                <small>保证金亏损比 {{ formatRatioPercent(positionLossAdvice.accountLossRatio) }}</small>
              </div>
              <small class="binance-position-loss-secondary">名义金额 {{ formatAssetAmount(positionLossAdvice.notional) }} · 数量 {{ formatCrypto(positionLossAdvice.quantity) }} · {{ positionLossAdvice.leverage }}x</small>
            </div>
            <small v-else class="binance-position-loss-inline-unavailable">{{ positionLossAdvice.reason }}</small>
          </div>
          <el-form-item v-if="executionPlan && asNumber(planTarget(executionPlan, 'PROTECTIVE_TARGET', -1)?.price) > 0" label="近端保护止盈累计比例">
            <el-input-number v-model="positionForm.protectiveTakeProfitRatio" class="binance-position-field" :min="1" :max="98" :precision="1" :step="1" controls-position="right" />
          </el-form-item>
          <div v-if="executionPlan" class="binance-tp-ratio-row">
            <el-form-item label="第一止盈累计比例">
              <el-input-number v-model="positionForm.firstTakeProfitRatio" class="binance-position-field" :min="1" :max="99" :precision="1" :step="1" controls-position="right" />
            </el-form-item>
            <el-form-item label="第二止盈累计比例">
              <el-input-number v-model="positionForm.secondTakeProfitRatio" class="binance-position-field" :min="1" :max="99" :precision="1" :step="1" controls-position="right" />
            </el-form-item>
          </div>
        </div>
        <section class="binance-position-risk-advice" :class="{ 'is-expanded': positionRiskAdviceExpanded }" aria-label="合约风险建议">
          <button
            type="button"
            class="binance-position-risk-toggle"
            :aria-expanded="positionRiskAdviceExpanded"
            aria-controls="binance-position-risk-advice-content"
            :title="positionRiskAdviceExpanded ? '收起风险约束建议' : '展开风险约束建议'"
            @click="positionRiskAdviceExpanded = !positionRiskAdviceExpanded"
          >
            <span class="binance-position-risk-toggle-copy"><strong>风险约束建议</strong><small>{{ positionRiskAdvice.isMicroAccount ? '小额账户适配' : '标准账户' }}</small></span>
            <span class="binance-position-risk-toggle-state">{{ positionRiskAdviceExpanded ? '收起' : '展开' }}</span>
            <ChevronDown :size="15" :class="['binance-position-risk-toggle-icon', { 'is-expanded': positionRiskAdviceExpanded }]" aria-hidden="true" />
          </button>
          <div v-if="positionRiskAdviceExpanded" id="binance-position-risk-advice-content" class="binance-position-risk-content">
            <small class="binance-position-risk-settings">当前采用：账户最大亏损比 {{ formatRatioPercent(positionRiskAdvice.riskFraction || configuredRiskFraction) }} · 保证金上限 {{ formatRatioPercent(positionRiskAdvice.maxMarginFraction || MAX_MARGIN_FRACTION) }} · 预估滑点 0.1%</small>
            <template v-if="positionRiskAdvice.available">
              <div class="binance-position-risk-grid">
                <div><span>可用余额</span><strong>{{ formatAssetAmount(positionRiskAdvice.availableBalance) }}</strong></div>
                <div><span>保证金余额</span><strong>{{ formatAssetAmount(positionRiskAdvice.marginBalance) }}</strong></div>
                <div><span>风控基数</span><strong>{{ formatAssetAmount(positionRiskAdvice.accountBase) }}</strong><small>取两者较低值</small></div>
                <div><span>单计划风险预算</span><strong>{{ formatAssetAmount(positionRiskAdvice.riskBudget) }}</strong><small>止损最大预估损失</small></div>
                <div><span>单计划风险比例</span><strong>{{ formatRatioPercent(positionRiskAdvice.riskFraction) }}</strong><small>{{ positionRiskAdvice.isMicroAccount ? '小额账户适配' : '标准账户基线' }}</small></div>
                <div><span>保证金使用上限</span><strong>{{ formatRatioPercent(positionRiskAdvice.maxMarginFraction) }}</strong><small>不是名义价值比例</small></div>
                <div><span>风险建议最高可开杠杆</span><strong>{{ positionRiskAdvice.maxLeverage }}x</strong><small>交易所品种上限另行约束</small></div>
                <div><span>当前杠杆最大仓位比例</span><strong>{{ formatRatioPercent(positionRiskAdvice.maxPositionRatio) }}</strong><small>按风控基数计算保证金占比</small></div>
                <div v-if="positionRiskAdvice.observedLeverage"><span>同交易对现有杠杆</span><strong>{{ positionRiskAdvice.observedLeverage }}x</strong><small>用于抬高建议下限，不超过交易所上限</small></div>
                <div><span>建议最大持仓数量</span><strong>{{ formatCrypto(positionRiskAdvice.maxQuantity) }}</strong><small>成本 {{ formatPrecisePrice(positionRiskAdvice.costPrice) }}</small></div>
                <div><span>结构止损距离</span><strong>{{ formatRatioPercent(positionRiskAdvice.stopDistanceRate) }}</strong><small>止损 {{ formatPrecisePrice(positionRiskAdvice.stopLoss) }}</small></div>
              </div>
              <div v-if="positionForm.quantityMode === 'MARGIN_RATIO' && normalizedQuantityFromMarginRatio > 0" class="binance-position-ratio-preview">
                <span>比例换算预览</span><strong>{{ formatCrypto(normalizedQuantityFromMarginRatio) }} 数量</strong><small>保证金 {{ formatAssetAmount(marginAmountFromRatio) }} · 名义价值 {{ formatAssetAmount(notionalFromRatio) }}</small>
              </div>
              <p v-if="positionRiskAdvice.quantityOverLimit" class="binance-position-risk-warning">持仓数量超过风险建议上限 {{ formatCrypto(positionRiskAdvice.maxQuantity) }}；当前数量 {{ formatCrypto(positionForm.quantity) }} 仅作风险提示，仍可保存。</p>
            </template>
            <p v-else class="binance-position-risk-unavailable">{{ positionRiskAdvice.reason }}</p>
          </div>
        </section>
        <section class="binance-position-loss-advice" aria-label="止损损失估算">
          <div class="binance-position-loss-head">
            <div class="binance-position-loss-main">
              <span>到达止损的预估损失</span>
              <strong>{{ positionLossAdvice.available ? formatAssetAmount(positionLossAdvice.estimatedLoss) : '暂无法计算' }}</strong>
              <small v-if="positionLossAdvice.available">保证金亏损比 {{ formatRatioPercent(positionLossAdvice.accountLossRatio) }}</small>
            </div>
          </div>
          <template v-if="positionLossAdvice.available">
            <div class="binance-position-loss-grid">
              <div><span>计算数量</span><strong>{{ formatCrypto(positionLossAdvice.quantity) }}</strong></div>
              <div><span>成本 → 止损</span><strong>{{ formatPrecisePrice(positionLossAdvice.costPrice) }} → {{ formatPrecisePrice(positionLossAdvice.stopLoss) }}</strong></div>
              <div><span>含预估滑点损失</span><strong>{{ formatAssetAmount(positionLossAdvice.estimatedLossWithSlippage) }}</strong></div>
              <div><span>账户计算基数</span><strong>{{ formatAssetAmount(positionLossAdvice.accountBase) }}</strong><small>{{ positionLossAdvice.accountBaseLabel }}</small></div>
            </div>
            <p v-if="positionLossAdvice.exceedsSuggestedRisk" class="binance-position-risk-warning">当前数量到达止损时预计亏损 {{ formatRatioPercent(positionLossAdvice.accountLossRatio) }}，高于参考风险比例 {{ formatRatioPercent(positionLossAdvice.suggestedRiskRatio) }}。仍可保存，但请确认你能承受该损失。</p>
          </template>
          <p v-else class="binance-position-risk-unavailable">{{ positionLossAdvice.reason }}</p>
        </section>
        <el-form-item label="备注">
          <el-input v-model="positionForm.note" type="textarea" :rows="3" maxlength="500" show-word-limit placeholder="记录建仓依据或复核要点" />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="binance-position-dialog-footer">
          <div v-if="executionPlan && !editingPositionId" class="binance-live-order-control" :class="{ 'is-disabled': executionPlanIsMonitoringOnly }">
            <span>已有持仓</span>
            <el-switch v-model="positionForm.submitRealLimitOrder" :disabled="executionPlanIsMonitoringOnly" aria-label="切换真实下单或已有持仓" />
            <span>真实下单</span>
          </div>
          <div class="binance-position-dialog-actions">
            <el-button @click="positionDialogVisible = false">取消</el-button>
            <el-button type="primary" :loading="positionSaving" @click="savePosition">保存监控计划</el-button>
          </div>
        </div>
      </template>
    </el-dialog>

    <el-dialog v-model="marketCloseDialogVisible" title="市价平仓" width="460px" class="binance-position-dialog binance-market-close-dialog" :style="{ maxWidth: 'calc(100vw - 24px)', margin: '12px auto' }" append-to-body align-center>
      <section v-if="marketClosePosition" class="binance-protection-context">
        <div><strong>{{ marketClosePosition.symbol }}</strong><span><em :class="directionClass(marketClosePosition.side)">{{ marketClosePosition.side === 'LONG' ? '平多' : '平空' }}</em> · 当前 {{ formatCrypto(marketClosePosition.quantity) }}</span></div>
        <small>交易所最小单位 {{ marketQuantityStepFor(marketClosePosition.symbol, marketClosePosition) > 0 ? formatCrypto(marketQuantityStepFor(marketClosePosition.symbol, marketClosePosition)) : '--' }} · 最小数量 {{ marketQuantityMinFor(marketClosePosition.symbol, marketClosePosition) > 0 ? formatCrypto(marketQuantityMinFor(marketClosePosition.symbol, marketClosePosition)) : '--' }}</small>
      </section>
      <div class="binance-market-close-slider">
        <div class="binance-market-close-slider-head"><span>平仓比例</span><strong>{{ marketCloseForm.quantityRatio }}%</strong></div>
        <el-slider v-model="marketCloseForm.quantityRatio" :min="1" :max="100" :step="1" show-input />
      </div>
      <div v-if="marketClosePosition" class="binance-market-close-preview">
        <div><span>按最小单位平仓数量</span><strong>{{ formatCrypto(marketCloseQuantity) }}</strong></div>
        <div><span>实际提交数量</span><strong>{{ formatCrypto(marketCloseQuantity) }}</strong></div>
        <p v-if="marketCloseQuantity <= 0" class="binance-settings-warning">当前比例低于交易所最小下单数量，请提高平仓比例。</p>
      </div>
      <template #footer>
        <el-button @click="marketCloseDialogVisible = false">取消</el-button>
        <el-button type="danger" :loading="marketCloseSaving" :disabled="marketCloseQuantity <= 0" @click="submitMarketClose">确认市价平仓</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="futuresProtectionDialogVisible" title="修改真实持仓保护单（数量型止盈 / 仓位止损）" width="500px" class="binance-position-dialog" :style="{ maxWidth: 'calc(100vw - 24px)', margin: '12px auto' }" append-to-body align-center>
      <div v-if="futuresProtectionPosition" class="binance-protection-context">
        <div><strong>{{ futuresProtectionPosition.symbol }}</strong><span><em :class="directionClass(futuresProtectionPosition.side)">{{ futuresProtectionPosition.side === 'LONG' ? '做多' : '做空' }}</em> · {{ futuresProtectionPosition.positionSide === 'BOTH' ? '单向持仓' : futuresProtectionPosition.positionSide }}</span></div>
         <small>最新成交价 {{ formatPrice(futuresLatestPriceFor(futuresProtectionPosition)) }} · 标记价 {{ formatPrice(futuresMarkPriceFor(futuresProtectionPosition)) }}。止损按标记价验证，止盈按最新成交价验证。</small>
      </div>
      <el-form label-position="top" class="binance-position-form binance-protection-form" @submit.prevent="saveFuturesProtection">
        <section class="binance-protection-section">
          <div class="binance-protection-section-head"><strong>比例保护</strong><span>按当前持仓数量比例止盈或止损，不使用全仓平仓。</span></div>
          <div class="binance-protection-form-grid">
            <el-form-item label="比例止损价">
              <el-input-number v-model="futuresProtectionForm.partialStopLoss" class="binance-position-field" :min="0" :precision="priceDecimalPlaces(futuresProtectionPosition?.markPrice)" :step="priceInputStep(futuresProtectionPosition?.markPrice)" controls-position="right" placeholder="留空则清除比例止损" />
            </el-form-item>
            <el-form-item label="比例止盈价">
              <el-input-number v-model="futuresProtectionForm.partialTakeProfit" class="binance-position-field" :min="0" :precision="priceDecimalPlaces(futuresProtectionPosition?.markPrice)" :step="priceInputStep(futuresProtectionPosition?.markPrice)" controls-position="right" placeholder="留空则清除比例止盈" />
            </el-form-item>
            <el-form-item label="平仓比例">
              <el-input-number v-model="futuresProtectionForm.quantityRatio" class="binance-position-field" :min="1" :max="100" :precision="1" :step="1" controls-position="right" />
            </el-form-item>
          </div>
        </section>
        <section class="binance-protection-section">
          <div class="binance-protection-section-head"><strong>仓位保护（仅止损全平）</strong><span>止损按标记价验证，触发后固定平掉该方向全部仓位；数量型止盈按最新成交价触发。</span></div>
          <div class="binance-protection-form-grid">
            <el-form-item label="仓位止损价（全平）">
              <el-input-number v-model="futuresProtectionForm.positionStopLoss" class="binance-position-field" :min="0" :precision="priceDecimalPlaces(futuresProtectionPosition?.markPrice)" :step="priceInputStep(futuresProtectionPosition?.markPrice)" controls-position="right" placeholder="留空则清除仓位止损" />
            </el-form-item>
          </div>
        </section>
      </el-form>
      <template #footer>
        <el-button @click="futuresProtectionDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="futuresProtectionSaving" @click="saveFuturesProtection">确认更新保护单</el-button>
      </template>
    </el-dialog>

  </section>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { gsap } from 'gsap'
import ElMessage from 'element-plus/es/components/message/index.mjs'
import ElMessageBox from 'element-plus/es/components/message-box/index.mjs'
import { Activity, Bitcoin, ChevronDown, ChevronUp, Link2, LockKeyhole, LogIn, Minus, Pencil, Plus, RefreshCw, ScanSearch, Search, SearchCheck, ShieldCheck, Trash2, WalletCards, X } from 'lucide-vue-next'
import BinanceKlineChart from './BinanceKlineChart.vue'
import BinancePositionPnlChart from './BinancePositionPnlChart.vue'
import AnimatedNumber from './AnimatedNumber.vue'
import { analyzeBinanceFutures, applyBinanceFuturesPlanProtection, captureBinanceSmartMoneyAuth, closeBinanceFuturesPositionMarket, connectBinanceAccount, createBinanceSimulatedPosition, deleteBinanceCredentials, deleteBinanceSimulatedPosition, fetchBinanceConnectionSettings, fetchBinanceCopyTradingSettings, fetchBinanceFuturesAnalysis, fetchBinanceFuturesSnapshot, fetchBinanceFuturesModelRuns, fetchBinanceStrategySettings, fetchBinanceUiSettings, fetchNotificationSettings, placeBinanceCopyTradingOrder, placeBinanceFuturesLimitPlanOrder, previewBinanceFuturesPositionMonitor, restoreBinanceSimulatedPosition, saveBinanceConnectionSettings, saveBinanceCopyTradingSettings, saveBinanceSmartMoneyPositionFollow, saveBinanceStrategySettings, saveBinanceUiSettings, saveNotificationSettings as requestSaveNotificationSettings, sendTestEmail, testBinanceFuturesAccountWebsocket, testBinanceFuturesWebsocket, updateBinanceFuturesPartialProtection, updateBinanceFuturesPositionLevelProtection, updateBinanceSimulatedPosition } from '../api'

const props = defineProps({ currentUser: { type: Object, default: null } })
const emit = defineEmits(['login-request'])

const binancePageRef = ref(null)
const chartStageRef = ref(null)
const chartPanelRef = ref(null)
const analysisDetailRef = ref(null)
const smartPositionDetailRef = ref(null)
const smartMoneyGridRef = ref(null)
let pageIntroContext
let chartMorphTween

const marketSearch = ref('')
// Keep the model's execution/reference scale first so the chart never
// presents 15m as the implicit minimum timeframe.
const intervalOptions = ['5m', '15m', '1h', '4h', '1d']
// Do not choose a market on page load.  The chart and target ticker are
// populated only after the user explicitly selects a symbol.
const selectedMarketSymbol = ref('')
const interval = ref('5m')
const chartMarketKey = computed(() => selectedMarketSymbol.value ? `${selectedMarketSymbol.value}:${interval.value}` : '')
const marketDataReadyKey = ref('')
const chartCollapsed = ref(false)
const analysisEvidenceCollapsed = ref(true)
const network = ref('mainnet')
const klines = ref([])
const marketLoading = ref(false)
const marketError = ref('')
const marketStale = ref(false)
const futuresMarkets = ref([])
const futuresLoading = ref(false)
const futuresError = ref('')
const futuresStale = ref(false)
const futuresUpdatedAtValue = ref(null)
const selectedTicker = ref(null)
const snapshotConnectionState = ref('connecting')
const snapshotConnectionError = ref('')
const futuresScanLimit = ref(100)
const futuresAnalysisJob = ref(null)
const futuresPlans = ref([])
const selectedFuturesPlanSymbol = ref('')
const futuresAnalyzing = ref(false)
const futuresAnalysisScope = ref('scan')
const apiKey = ref('')
const apiSecret = ref('')
  const credentialsProfile = ref({ configured: false, network: 'mainnet', apiKeyMasked: '', updatedAt: null, requiresReconfiguration: false })
const account = ref(null)
const connected = ref(false)
const connectionLoading = ref(false)
const accountSnapshotUpdatedAt = ref(null)
const accountSnapshotCheckedAt = ref(null)
const accountSnapshotStale = ref(false)
const accountSnapshotError = ref('')
const accountPositionPnlHistory = ref([])
const accountSettingsVisible = ref(false)
const accountSettingsTab = ref('connection')
const connectionSettings = ref({ connectionMode: 'REST', updatedAt: null })
const connectionSettingsLoading = ref(false)
const connectionSettingsSaving = ref(false)
const connectionSettingsLoaded = ref(false)
const connectionStatus = ref(null)
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
const STRATEGY_SETTINGS_KEYS = Object.keys(STRATEGY_SETTINGS_DEFAULTS)
const STRATEGY_ENUM_SETTINGS_KEYS = Object.freeze(['strategyEngine', 'modelBranch', 'strategyMode', 'levelStrategy', 'entryConfirmationMode', 'modelRunId'])
const STRATEGY_NUMERIC_SETTINGS_KEYS = STRATEGY_SETTINGS_KEYS.filter((key) => !STRATEGY_ENUM_SETTINGS_KEYS.includes(key))
const STRATEGY_MODES = Object.freeze(['MIDLINE', 'SHORT_TERM'])
const STRATEGY_ENGINES = Object.freeze(['CLASSIC', 'MODEL'])
const MODEL_BRANCHES = Object.freeze(['BEST', 'STABLE'])
const LEVEL_STRATEGIES = Object.freeze(['STRUCTURE_EXTREME', 'CONFIRMED_PLATFORM'])
const ENTRY_CONFIRMATION_MODES = Object.freeze(['RETEST_REQUIRED', 'TRIGGER_ONLY'])
const strategySettings = ref({ ...STRATEGY_SETTINGS_DEFAULTS })
const savedStrategySettings = ref({ ...STRATEGY_SETTINGS_DEFAULTS })
const strategySettingsLoading = ref(false)
const strategySettingsSaving = ref(false)
const strategySettingsLoaded = ref(false)
let strategySettingsLoadPromise = null
const modelRuns = ref([])
const modelRunsLatestRunId = ref('')
const modelRunsLoading = ref(false)
const monitorDurationNow = ref(Date.now())
const websocketTestLoading = ref(false)
const websocketTestKind = ref('')
const websocketTestResult = ref(null)
const websocketAccountTestLoading = ref(false)
const websocketAccountTestResult = ref(null)
const accountDrawerOpen = ref(false)
const accountDrawerTop = ref(220)
const accountDrawerDragging = ref(false)
const accountDrawerMoved = ref(false)
const accountDrawerDragStartY = ref(0)
const accountDrawerDragStartTop = ref(220)
const expandedFuturesPositionKey = ref('')
const futuresPositionSmoothing = ref(0.25)
const futuresPositionSmoothingLoaded = ref(false)
const futuresProtectionDialogVisible = ref(false)
const futuresProtectionSaving = ref(false)
const futuresProtectionPosition = ref(null)
const futuresProtectionForm = ref({ symbol: '', positionSide: 'BOTH', positionStopLoss: null, partialStopLoss: null, partialTakeProfit: null, quantityRatio: 50 })
const futuresProtectionInitial = ref(null)
const marketCloseDialogVisible = ref(false)
const marketCloseSaving = ref(false)
const marketClosePosition = ref(null)
const marketCloseForm = ref({ quantityRatio: 100 })
const livePositionMonitorPreviewVisible = ref(false)
const livePositionMonitorPreview = ref(null)
const monitorPreviewLoadingKey = ref('')
const livePositionMonitorSaving = ref(false)
const simulatedPositions = ref([])
const selectedExecutionPositionId = ref(null)
const simulatedPositionsLoading = ref(false)
const simulatedPositionsError = ref('')
const copyTradingRecords = ref([])
const copyTradingPositions = ref([])
const copyTradingFollows = ref([])
const copyTradingFollowedPositionSnapshots = ref([])
const copyTradingFollowSavingKey = ref('')
const selectedSmartMoneyPositionKey = ref('')
const copyTradingLoading = ref(false)
const copyTradingError = ref('')
const copyTradingUpdatedAt = ref(null)
const copyTradingSettings = ref({ topTraderId: '5132388877263187456', copyMultiplier: 1, copyMultipliers: {}, updatedAt: null })
const copyTradingSubscriptions = ref([])
const copyTradingSubscriptionAvatarErrors = ref({})
const copyTradingSourceMarginValue = ref(0)
const copyTradingSettingsLoading = ref(false)
const copyTradingSettingsSaving = ref(false)
const copyTradingSubscriptionSelecting = ref(false)
const notificationSettings = ref({ email: '' })
const notificationSettingsLoading = ref(false)
const notificationSettingsSaving = ref(false)
const notificationTestSending = ref(false)
const smartMoneyAuthForm = ref({ cookie: '', csrfToken: '' })
const smartMoneyAuthCapturing = ref(false)
const copyTradeDialogVisible = ref(false)
const copyTradeSaving = ref(false)
const copyTradeRecord = ref(null)
const copyOrderTypeOptions = [
  { label: '市价单', value: 'MARKET' },
  { label: '限价单', value: 'LIMIT' },
]
const copyTradeForm = ref({
  orderType: 'LIMIT',
  symbol: '',
  direction: 'LONG',
  quantity: 0.001,
  leverage: 1,
  costPrice: null,
  note: '',
})
const positionDialogVisible = ref(false)
const positionSaving = ref(false)
const editingPositionId = ref(null)
const executionPlan = ref(null)
const positionRiskAdviceExpanded = ref(false)
const positionTimeframes = ['4h', '1h', '15m', '5m']
const quantityModeOptions = [
  { label: '固定数量', value: 'QUANTITY' },
  { label: '按保证金比例', value: 'MARGIN_RATIO' },
]
const positionForm = ref({
  network: 'mainnet',
  marketMode: 'FUTURES',
  symbol: 'BTCUSDT',
  side: 'LONG',
  quantity: 0.001,
  quantityMode: 'QUANTITY',
  marginRatio: 50,
  costPrice: 0,
  leverage: 10,
  protectiveTakeProfitRatio: 25,
  firstTakeProfitRatio: 50,
  secondTakeProfitRatio: 75,
  submitRealLimitOrder: false,
  note: '',
})
let futuresAnalysisPollTimer = null
let futuresAnalysisRequestId = 0
let futuresAnalysisPollFailures = 0
let snapshotPollTimer = null
let snapshotSubscriptionRequestId = 0
let snapshotClientId = ''
let monitorDurationTimer = null
let copyTradingSubscriptionSelectionVersion = 0
let copyTradingSubscriptionSelectionQueue = null
let copyTradingSubscriptionSelectionWorker = null
let copyTradingSubscriptionSelectionInFlightTraderId = ''

const asNumber = (value) => {
  const number = Number(value)
  return Number.isFinite(number) ? number : 0
}

const COPY_TRADING_MULTIPLIER_MIN = 1
const COPY_TRADING_MULTIPLIER_MAX = 10

const normalizeCopyTradingMultiplier = (value) => {
  const number = Number(value)
  if (!Number.isFinite(number)) return COPY_TRADING_MULTIPLIER_MIN
  return Math.min(COPY_TRADING_MULTIPLIER_MAX, Math.max(COPY_TRADING_MULTIPLIER_MIN, Math.round(number)))
}
// Older MODEL scan snapshots may contain target-level R values but no
// top-level riskReward. Keep display and ranking compatible with those
// snapshots instead of turning a valid plan into a bare "--".
const planRiskRewardValue = (plan) => {
  const directValues = [plan?.riskReward, plan?.modelPrediction?.plan?.riskReward]
  const direct = directValues.map(Number).find((value) => Number.isFinite(value) && value > 0)
  if (direct !== undefined) return direct
  const targets = Array.isArray(plan?.takeProfits) ? plan.takeProfits : []
  const first = targets.find((item) => String(item?.role || '').toUpperCase() === 'FIRST_TARGET') || targets[0]
  const targetR = Number(first?.rMultiple)
  if (Number.isFinite(targetR) && targetR > 0) return targetR
  const trigger = Number(plan?.entry?.trigger)
  const stop = Number(plan?.stopLoss)
  const target = Number(first?.price ?? plan?.targetOne)
  const risk = Math.abs(trigger - stop)
  if ([trigger, stop, target].every((value) => Number.isFinite(value) && value > 0) && risk > 0) {
    const derived = Math.abs(target - trigger) / risk
    return Number.isFinite(derived) && derived > 0 ? derived : 0
  }
  return 0
}

const normalizeStrategySettings = (value) => {
  const source = value && typeof value === 'object' ? value : {}
  const numeric = Object.fromEntries(STRATEGY_NUMERIC_SETTINGS_KEYS.map((key) => {
    const number = Number(source[key])
    return [key, Number.isFinite(number) ? number : STRATEGY_SETTINGS_DEFAULTS[key]]
  }))
  const levelStrategy = String(source.levelStrategy || '').trim().toUpperCase()
  const strategyEngine = String(source.strategyEngine || '').trim().toUpperCase()
  const modelBranch = String(source.modelBranch || '').trim().toUpperCase()
  const modelRunId = String(source.modelRunId || '').trim()
  const strategyMode = String(source.strategyMode || '').trim().toUpperCase()
  const entryConfirmationMode = String(source.entryConfirmationMode || '').trim().toUpperCase()
  return {
    strategyEngine: STRATEGY_ENGINES.includes(strategyEngine) ? strategyEngine : STRATEGY_SETTINGS_DEFAULTS.strategyEngine,
    modelBranch: MODEL_BRANCHES.includes(modelBranch) ? modelBranch : STRATEGY_SETTINGS_DEFAULTS.modelBranch,
    modelRunId: modelRunId || null,
    strategyMode: STRATEGY_MODES.includes(strategyMode) ? strategyMode : STRATEGY_SETTINGS_DEFAULTS.strategyMode,
    levelStrategy: LEVEL_STRATEGIES.includes(levelStrategy) ? levelStrategy : STRATEGY_SETTINGS_DEFAULTS.levelStrategy,
    entryConfirmationMode: ENTRY_CONFIRMATION_MODES.includes(entryConfirmationMode) ? entryConfirmationMode : STRATEGY_SETTINGS_DEFAULTS.entryConfirmationMode,
    ...numeric,
  }
}

const strategySettingNumber = (settings, key) => normalizeStrategySettings(settings)[key]
const formatTargetPercent = (value) => `${Number(asNumber(value).toFixed(2))}%`
const targetForRole = (targets, role, legacyIndex = -1) => {
  const items = Array.isArray(targets) ? targets : []
  const hasRoles = items.some((item) => String(item?.role || '').trim())
  if (hasRoles) return items.find((item) => String(item?.role || '').trim().toUpperCase() === role) || null
  if (items.length >= 3) return legacyIndex >= 0 ? (items[legacyIndex] || null) : null
  return legacyIndex >= 0 ? (items[legacyIndex] || null) : null
}
const planTarget = (plan, role, legacyIndex = -1) => {
  if (!plan) return null
  const roleTarget = targetForRole(plan.takeProfits, role, legacyIndex)
  const direct = role === 'PROTECTIVE_TARGET'
    ? plan.targetProtection
    : role === 'FIRST_TARGET'
      ? plan.targetOne
      : plan.targetTwo
  const directPrice = asNumber(direct)
  if (directPrice > 0) {
    // Keep role metadata (especially available:false on an unconfirmed
    // extension) when a dynamic snapshot also exposes a legacy direct price.
    // Without this merge, a diagnostic targetTwo could be mistaken for an
    // executable second target and fail protection validation.
    return {
      ...(roleTarget && typeof roleTarget === 'object' ? roleTarget : {}),
      price: directPrice,
      label: roleTarget?.label || (role === 'PROTECTIVE_TARGET' ? '近端保护目标' : role === 'FIRST_TARGET' ? '第一目标' : '第二目标'),
    }
  }
  return roleTarget
}
const planTargetRatio = (plan, key, fallback) => {
  const role = key === 'protectiveTakeProfitRatio'
    ? 'PROTECTIVE_TARGET'
    : key === 'firstTakeProfitRatio'
      ? 'FIRST_TARGET'
      : 'EXTENSION_TARGET'
  const roleRatio = asNumber(targetForRole(plan?.takeProfits, role, -1)?.cumulativeRatio)
  if (roleRatio > 0) return roleRatio
  // A direct MODEL plan has two model-selected targets and intentionally no
  // near-term protective target. Do not display or validate the account's
  // default protective ratio as if that missing target existed.
  if (role === 'PROTECTIVE_TARGET' && !planTarget(plan, role, -1)?.price && !asNumber(plan?.targetProtection)) return 0
  const candidates = [
    plan?.[key],
    plan?.executionPlan?.[key],
    plan?.strategySettings?.[key],
    plan?.executionPlan?.strategySettings?.[key],
  ]
  return candidates.map(asNumber).find((value) => value > 0) || fallback
}
const normalizedPlanProtectionRatios = (plan) => {
  const protectiveTarget = planTarget(plan, 'PROTECTIVE_TARGET', -1)
  const extension = planTarget(plan, 'EXTENSION_TARGET', 1)
  const hasProtective = asNumber(protectiveTarget?.price) > 0 && protectiveTarget?.available !== false
  const hasExtension = asNumber(extension?.price) > 0 && extension?.available !== false
  const isDirectModelPlan = String(plan?.strategyEngine || '').toUpperCase() === 'MODEL'
    && String(plan?.modelTask || '').toUpperCase() === 'DIRECT_PLAN'
  let protective = planTargetRatio(plan, 'protectiveTakeProfitRatio', isDirectModelPlan ? 0 : configuredProtectiveTakeProfitRatio.value)
  let first = planTargetRatio(plan, 'firstTakeProfitRatio', isDirectModelPlan ? Number.NaN : configuredFirstTakeProfitRatio.value)
  let second = planTargetRatio(plan, 'secondTakeProfitRatio', isDirectModelPlan ? Number.NaN : configuredSecondTakeProfitRatio.value)
  if (!hasProtective) protective = 0
  if (hasExtension) {
    const secondValue = Number(second)
    const firstValue = Number(first)
    const protectiveValue = Number(protective)
    second = Number.isFinite(secondValue) ? Math.min(Math.max(secondValue, 2), 99.9) : Number.NaN
    first = Number.isFinite(firstValue) ? Math.min(Math.max(firstValue, 1.1), second - 0.1) : Number.NaN
    if (hasProtective) protective = Number.isFinite(protectiveValue) ? Math.min(Math.max(protectiveValue, 1), first - 0.1) : Number.NaN
  } else {
    const secondValue = Number(second)
    second = Number.isFinite(secondValue) ? Math.min(Math.max(secondValue, 1.1), 99.9) : Number.NaN
    first = second
    if (hasProtective) {
      const protectiveValue = Number(protective)
      protective = Number.isFinite(protectiveValue) ? Math.min(Math.max(protectiveValue, 1), first - 0.1) : Number.NaN
    }
  }
  return { protective, first, second, hasExtension, hasProtective }
}
const planHasExtensionTarget = (plan) => {
  const target = planTarget(plan, 'EXTENSION_TARGET', 1)
  return asNumber(target?.price) > 0 && target?.available !== false
}
const strategySecondTakeProfitLegRatio = computed(() => Math.max(0, strategySettingNumber(strategySettings.value, 'secondTakeProfitRatio') - strategySettingNumber(strategySettings.value, 'firstTakeProfitRatio')))
const strategyRemainingTakeProfitRatio = computed(() => Math.max(0, 100 - strategySettingNumber(strategySettings.value, 'secondTakeProfitRatio')))
const configuredRiskFraction = computed(() => strategySettingNumber(savedStrategySettings.value, 'maxAccountLossRatio') / 100)
const configuredProtectiveTakeProfitRatio = computed(() => strategySettingNumber(savedStrategySettings.value, 'protectiveTakeProfitRatio'))
const configuredFirstTakeProfitRatio = computed(() => strategySettingNumber(savedStrategySettings.value, 'firstTakeProfitRatio'))
const configuredSecondTakeProfitRatio = computed(() => strategySettingNumber(savedStrategySettings.value, 'secondTakeProfitRatio'))

const priceDecimalPlaces = (value) => {
  const number = Math.abs(asNumber(value))
  if (!number) return 4
  if (number >= 1) return 4
  return Math.max(0, 4 - Math.floor(Math.log10(number)) - 1)
}

const priceInputStep = (value) => 10 ** -priceDecimalPlaces(value)

const formatPrice = (value) => {
  const number = asNumber(value)
  if (!number) return '--'
  const digits = priceDecimalPlaces(number)
  return number.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: digits })
}

const formatPrecisePrice = (value) => {
  const number = asNumber(value)
  if (!number) return '--'
  const digits = Math.max(4, priceDecimalPlaces(number))
  return number.toLocaleString('en-US', { minimumFractionDigits: 4, maximumFractionDigits: digits })
}


const partialTakeProfitLevelsFor = (position) => {
  const levels = Array.isArray(position?.partialTakeProfitLevels) ? position.partialTakeProfitLevels : []
  if (levels.length) return levels
  const legacyPrice = asNumber(position?.partialTakeProfit)
  return legacyPrice > 0 ? [{ price: legacyPrice, quantity: position?.partialQuantity, quantityRatio: position?.partialQuantityRatio }] : []
}

const partialTakeProfitLevel = (position, index) => partialTakeProfitLevelsFor(position)[index] || null

// 账户资产卡片：触发方向系数（做多 +1 / 做空 -1）。
const positionDirectionSign = (position) => (String(position.side || '').toUpperCase() === 'SHORT' ? -1 : 1)

// 止损触发盈亏：按全部持仓数量估算。
const positionStopLossPnl = (position) => {
  const stop = asNumber(position.positionStopLoss ?? position.stopLoss)
  const entry = asNumber(position.entryPrice)
  const qty = asNumber(position.quantity)
  if (!stop || !entry || !qty) return null
  return (stop - entry) * qty * positionDirectionSign(position)
}

// 止盈档位触发盈亏：扣除前几档已止盈数量，按剩余数量与差价估算。
const positionTakeProfitPnl = (position, level) => {
  const price = asNumber(level?.price)
  const entry = asNumber(position.entryPrice)
  const qty = asNumber(position.quantity)
  if (!price || !entry || !qty) return null
  const cumulative = asNumber(level?.cumulativeQuantityRatio)
  const leg = asNumber(level?.quantityRatio)
  const remainingRatio = Math.max(0, 1 - (cumulative - leg) / 100)
  return (price - entry) * qty * remainingRatio * positionDirectionSign(position)
}

// 账户资产持仓卡底部空间的紧凑比例展示，只保留本档比例。
const partialTakeProfitRatioCompact = (position, index) => {
  const level = partialTakeProfitLevel(position, index)
  const cumulative = asNumber(level?.cumulativeQuantityRatio)
  const leg = asNumber(level?.quantityRatio)
  if (leg > 0) return `${leg}%`
  if (cumulative > 0) return `${cumulative}%`
  return '--'
}

const partialTakeProfitReached = (position, index) => {
  const level = partialTakeProfitLevel(position, index)
  const price = asNumber(level?.price)
  const latestPrice = futuresLatestPriceFor(position)
  if (price <= 0 || latestPrice <= 0) return false
  return position?.side === 'SHORT' ? latestPrice <= price : latestPrice >= price
}


const formatCrypto = (value) => asNumber(value).toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 8 })
const formatAssetAmount = (value) => asNumber(value).toLocaleString('en-US', { minimumFractionDigits: 4, maximumFractionDigits: 4 })
const formatAccountSummaryAmount = (value) => asNumber(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const formatAssetAmountTwoDecimals = (value) => asNumber(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const formatRatioPercent = (value) => `${(asNumber(value) * 100).toFixed(2)}%`
const formatSignedNumber = (value) => {
  const number = asNumber(value)
  return `${number > 0 ? '+' : ''}${number.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 8 })}`
}
const formatSignedAssetAmount = (value) => {
  const number = asNumber(value)
  return `${number > 0 ? '+' : ''}${formatAssetAmount(number)}`
}
const formatSignedAccountSummaryAmount = (value) => {
  const number = asNumber(value)
  return `${number > 0 ? '+' : ''}${formatAccountSummaryAmount(number)}`
}
const formatSignedAssetAmountTwoDecimals = (value) => {
  const number = asNumber(value)
  return `${number > 0 ? '+' : ''}${formatAssetAmountTwoDecimals(number)}`
}
const formatRatioCompletionPercent = (value) => `${asNumber(value).toFixed(2)}%`
const formatMonitoringPnl = (value) => asNumber(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
// 盈亏大数字拆出正负号：符号槽固定占位，有无符号时数值与百分比位置都不移动。
const monitoringPnlParts = (value) => {
  const formatted = formatSignedMonitoringPnl(value)
  const hasSign = formatted.startsWith('+') || formatted.startsWith('-')
  return { sign: hasSign ? formatted.slice(0, 1) : '', text: hasSign ? formatted.slice(1) : formatted }
}
const formatSignedMonitoringPnl = (value) => {
  const number = asNumber(value)
  return `${number > 0 ? '+' : ''}${formatMonitoringPnl(number)}`
}
const formatCompactNumber = (value) => {
  const number = asNumber(value)
  if (!number) return '--'
  return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 }).format(number)
}
const formatSignedPercent = (value) => {
  const number = asNumber(value)
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`
}
const changeClass = (value) => {
  const number = asNumber(value)
  return number > 0 ? 'binance-rise' : number < 0 ? 'binance-fall' : 'binance-neutral'
}
const changeDisplayClass = (value) => ['binance-change-value', changeClass(value)]
const futuresPositionRoePercent = (position) => {
  const reported = Number(position?.roePercent)
  const unrealizedProfit = Number(position?.unrealizedProfit ?? position?.unRealizedProfit)
  if (Number.isFinite(reported) && (Math.abs(reported) > Number.EPSILON || !Number.isFinite(unrealizedProfit) || Math.abs(unrealizedProfit) <= Number.EPSILON)) return reported

  const quantity = Math.abs(Number(position?.quantity ?? position?.positionAmt))
  const entryPrice = Number(position?.entryPrice)
  const leverage = Number(position?.leverage)
  const notional = Math.abs(Number(position?.notional))
  const initialMargin = Number(position?.initialMargin ?? position?.positionInitialMargin)
  const margin = quantity > 0 && entryPrice > 0 && leverage > 0
    ? quantity * entryPrice / leverage
    : (Number.isFinite(initialMargin) && initialMargin > 0
        ? initialMargin
        : (notional > 0 && leverage > 0 ? notional / leverage : 0))
  return Number.isFinite(unrealizedProfit) && margin > 0 ? unrealizedProfit / margin * 100 : 0
}
const directionLabel = (value) => ({ LONG: 'LONG', SHORT: 'SHORT', WAIT: 'WAIT', NONE: '--' }[String(value || '').toUpperCase()] || '--')
const directionClass = (value) => {
  const direction = String(value || '').toUpperCase()
  return direction === 'SHORT' ? 'binance-direction-short' : (direction === 'LONG' ? 'binance-direction-long' : 'binance-neutral')
}
const formatTime = (value) => value ? new Date(Number(value)).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '--'
const formatDateTime = (value) => value ? new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '--'
const copyTradingDirection = (record) => {
  const positionSide = String(record?.positionSide || record?.positionDirection || '').toUpperCase()
  if (positionSide === 'LONG' || positionSide === 'SHORT') return positionSide
  return String(record?.side || '').toUpperCase() === 'SELL' ? 'SHORT' : 'LONG'
}
const copyTradeTradingDirection = computed(() => copyTradingDirection(copyTradeRecord.value))
const copyTradingRecordKey = (record) => `${record?.symbol || 'unknown'}-${record?.side || ''}-${record?.time || record?.orderTime || record?.orderUpdateTime || ''}-${record?.price || record?.avgPrice || ''}-${record?.qty || record?.executedQty || ''}`
const formatCopyTradingTime = (record) => formatDateTime(record?.time || record?.orderUpdateTime || record?.orderTime)
const copyTradingTradeValue = (record) => {
  const storedValue = asNumber(record?.tradeValue)
  if (storedValue > 0) return storedValue
  const price = asNumber(record?.avgPrice || record?.price)
  const quantity = asNumber(record?.qty || record?.executedQty || record?.origQty)
  return price > 0 && quantity > 0 ? price * quantity : null
}
const copyTradingSourceMargin = computed(() => asNumber(copyTradingSourceMarginValue.value))
const copyTradingAccountMargin = computed(() => asNumber(account.value?.futures?.totalMarginBalance))
const copyTradingMultiplier = computed(() => normalizeCopyTradingMultiplier(copyTradingSettings.value.copyMultiplier))
const copyTradingMultiplierForTrader = (topTraderId) => {
  const traderId = String(topTraderId || '').trim()
  const configured = copyTradingSettings.value.copyMultipliers?.[traderId]
  if (configured !== undefined && configured !== null && configured !== '') return normalizeCopyTradingMultiplier(configured)
  return traderId === String(copyTradingSettings.value.topTraderId || '') ? copyTradingMultiplier.value : COPY_TRADING_MULTIPLIER_MIN
}
const formatCopyTradingRatio = (record) => {
  const sourceMargin = copyTradingSourceMargin.value
  const tradeValue = copyTradingTradeValue(record)
  if (!(sourceMargin > 0) || !(tradeValue > 0)) return '--'
  return `${(tradeValue / sourceMargin * 100).toFixed(2)}%`
}
const copyTradingRecommendedRawValue = (record) => {
  const sourceMargin = copyTradingSourceMargin.value
  const accountMargin = copyTradingAccountMargin.value
  const tradeValue = copyTradingTradeValue(record)
  if (!(sourceMargin > 0) || !(accountMargin > 0) || !(tradeValue > 0)) return null
  return accountMargin * (tradeValue / sourceMargin) * copyTradingMultiplier.value
}
const normalizeRecommendedValue = (value, price, symbol, orderType = 'LIMIT') => {
  const rawValue = asNumber(value)
  const referencePrice = asNumber(price)
  if (!(rawValue > 0) || !(referencePrice > 0)) return null
  const quantity = normalizeQuantityForOrder(rawValue / referencePrice, symbol, orderType)
  return quantity > 0 ? quantity * referencePrice : null
}
const copyTradingRecommendedValue = (record) => {
  const rawValue = copyTradingRecommendedRawValue(record)
  const price = asNumber(record?.avgPrice || record?.price)
  if (!(price > 0)) return rawValue
  return normalizeRecommendedValue(rawValue, price, record?.symbol, 'LIMIT') ?? rawValue
}
const copyTradingRecommendedQuantity = (record, orderType = 'LIMIT') => {
  const price = asNumber(record?.avgPrice || record?.price)
  const value = copyTradingRecommendedRawValue(record)
  return price > 0 && value > 0 ? normalizeQuantityForOrder(value / price, record?.symbol, orderType) : 0
}
const formatAssetAmountOrPlaceholder = (value) => value == null ? '--' : formatAssetAmount(value)
const formatAssetAmountTwoDecimalsOrPlaceholder = (value) => value == null ? '--' : formatAssetAmountTwoDecimals(value)
const formatRecommendedPositionValue = (value) => {
  if (value == null) return '--'
  const number = asNumber(value)
  if (!Number.isFinite(number)) return '--'
  return number < 0.01
    ? number.toLocaleString('en-US', { minimumFractionDigits: 4, maximumFractionDigits: 8 })
    : formatAssetAmountTwoDecimals(number)
}
const copyTradingPositionSymbol = (position) => String(position?.symbol || position?.pair || position?.contractSymbol || '--').toUpperCase()
const copyTradingPositionSide = (position) => {
  const side = String(position?.side || position?.positionSide || position?.direction || '').toUpperCase()
  if (side === 'SHORT' || side === 'SELL') return 'SHORT'
  if (side === 'LONG' || side === 'BUY') return 'LONG'
  return asNumber(position?.amount ?? position?.positionAmt ?? position?.quantity) < 0 ? 'SHORT' : 'LONG'
}
const copyTradingOwnPositionSide = (position) => {
  const side = String(position?.side || position?.positionSide || position?.direction || '').toUpperCase()
  if (side === 'SHORT' || side === 'SELL') return 'SHORT'
  if (side === 'LONG' || side === 'BUY') return 'LONG'
  return asNumber(position?.quantity ?? position?.positionAmt ?? position?.amount) < 0 ? 'SHORT' : 'LONG'
}
const copyTradingOwnPosition = (position) => {
  const symbol = copyTradingPositionSymbol(position)
  const side = copyTradingPositionSide(position)
  const positions = account.value?.futures?.positions
  if (!symbol || symbol === '--' || !Array.isArray(positions)) return null
  return positions.find((item) => (
    copyTradingPositionSymbol(item) === symbol
    && copyTradingOwnPositionSide(item) === side
    && Math.abs(asNumber(item?.quantity ?? item?.positionAmt ?? item?.amount)) > 0
  )) || null
}
const copyTradingFollowForPosition = (position) => {
  const symbol = copyTradingPositionSymbol(position)
  const side = copyTradingPositionSide(position)
  return copyTradingFollows.value.find((follow) => (
    follow?.enabled !== false
    && String(follow?.topTraderId || '') === String(copyTradingSettings.value.topTraderId || '')
    && String(follow?.symbol || '').toUpperCase() === symbol
    && String(follow?.positionSide || '').toUpperCase() === side
  )) || null
}
const isCopyTradingPositionFollowing = (position) => Boolean(
  copyTradingOwnPosition(position) && copyTradingFollowForPosition(position),
)
const isCopyTradingPositionSameCar = (position) => Boolean(
  copyTradingOwnPosition(position) && !copyTradingFollowForPosition(position),
)
const smartMoneyFollowsForAccountPosition = (position) => {
  const symbol = copyTradingPositionSymbol(position)
  const side = copyTradingOwnPositionSide(position)
  return copyTradingFollows.value.filter((follow) => (
    String(follow?.symbol || '').toUpperCase() === symbol
    && String(follow?.positionSide || '').toUpperCase() === side
  ))
}
const smartMoneyFollowKey = (follow) => `${follow?.topTraderId || ''}-${follow?.symbol || ''}-${follow?.positionSide || ''}`
const smartMoneyFollowSnapshotFor = (follow) => (
  copyTradingFollowedPositionSnapshots.value.find((snapshot) => (
    String(snapshot?.topTraderId || '') === String(follow?.topTraderId || '')
  )) || null
)
const smartMoneyFollowSnapshotError = (follow) => smartMoneyFollowSnapshotFor(follow)?.error || ''
const smartMoneyFollowSourceMargin = (follow) => asNumber(smartMoneyFollowSnapshotFor(follow)?.sourceTotalMargin)
const smartMoneyPositionForAccountFollow = (position, follow) => {
  const symbol = copyTradingPositionSymbol(position)
  const side = copyTradingOwnPositionSide(position)
  const positions = smartMoneyFollowSnapshotFor(follow)?.positions
  if (!Array.isArray(positions)) return null
  return positions.find((item) => (
    copyTradingPositionSymbol(item) === symbol && copyTradingPositionSide(item) === side
  )) || null
}
const smartMoneyFollowRecommendedPositionValue = (position, follow) => {
  const sourcePosition = smartMoneyPositionForAccountFollow(position, follow)
  return sourcePosition
    ? copyTradingRecommendedPositionValueForSource(
      sourcePosition,
      smartMoneyFollowSourceMargin(follow),
      copyTradingMultiplierForTrader(follow?.topTraderId),
    )
    : null
}
const smartMoneyFollowPositionValueDeviation = (position, follow) => {
  const recommended = Number(smartMoneyFollowRecommendedPositionValue(position, follow))
  const actual = Number(copyTradingOwnPositionValue(position))
  return Number.isFinite(recommended) && Number.isFinite(actual) ? Math.abs(recommended - actual) : null
}
const smartMoneyFollowPositionValueDeviationPercent = (position, follow) => {
  const recommended = Number(smartMoneyFollowRecommendedPositionValue(position, follow))
  const actual = Number(copyTradingOwnPositionValue(position))
  return Number.isFinite(recommended) && recommended > 0 && Number.isFinite(actual)
    ? actual / recommended * 100
    : null
}
const smartMoneyFollowPositionValueDifference = (position, follow) => {
  const recommended = Number(smartMoneyFollowRecommendedPositionValue(position, follow))
  const actual = Number(copyTradingOwnPositionValue(position))
  return Number.isFinite(recommended) && Number.isFinite(actual) ? actual - recommended : null
}
const smartMoneyFollowPositionValueAlert = (position, follow) => {
  const recommended = Number(smartMoneyFollowRecommendedPositionValue(position, follow))
  const actual = Number(copyTradingOwnPositionValue(position))
  if (!(recommended > 0) || !Number.isFinite(actual)) return false
  const ratio = actual / recommended
  const deviation = Math.abs(actual - recommended)
  return ratio > 1.25 || (ratio < 0.8 && deviation > 3)
}
const copyTradingPositionDeviationSummary = (position) => {
  const follow = copyTradingFollowForPosition(position)
  if (!follow) return null
  const recommended = Number(smartMoneyFollowRecommendedPositionValue(position, follow))
  const own = copyTradingOwnPosition(position)
  const actual = Number(own ? copyTradingOwnPositionValue(own) : NaN)
  if (!(recommended > 0) || !Number.isFinite(actual)) return null
  return {
    actual,
    recommended,
    difference: actual - recommended,
    ratio: actual / recommended * 100,
    alert: smartMoneyFollowPositionValueAlert(position, follow),
  }
}
const smartMoneyFollowSourceName = (follow) => {
  const traderId = String(follow?.topTraderId || '')
  const subscription = copyTradingSubscriptions.value.find((item) => String(item?.topTraderId || '') === traderId)
  return subscription?.traderName || subscription?.accountName || '聪明钱'
}
const toggleCopyTradingFollow = async (position) => {
  const key = copyTradingPositionTabKey(position)
  if (copyTradingFollowSavingKey.value) return
  const existing = copyTradingFollowForPosition(position)
  copyTradingFollowSavingKey.value = key
  try {
    const response = await saveBinanceSmartMoneyPositionFollow({
      topTraderId: copyTradingSettings.value.topTraderId,
      symbol: copyTradingPositionSymbol(position),
      positionSide: copyTradingPositionSide(position),
      enabled: !existing,
    })
    copyTradingFollows.value = response?.follows || []
    if (response?.copyTrading) applyBinanceSnapshot({ copyTrading: response.copyTrading })
    ElMessage.success(existing ? '已取消聪明钱持仓跟随' : '已开始跟随聪明钱持仓监控')
  } catch (error) {
    ElMessage.error(`聪明钱持仓跟随保存失败：${marketErrorMessage(error)}`)
  } finally {
    copyTradingFollowSavingKey.value = ''
  }
}
const cancelSmartMoneyFollow = async (follow) => {
  const key = smartMoneyFollowKey(follow)
  if (copyTradingFollowSavingKey.value) return
  copyTradingFollowSavingKey.value = key
  try {
    const response = await saveBinanceSmartMoneyPositionFollow({
      topTraderId: follow.topTraderId,
      symbol: follow.symbol,
      positionSide: follow.positionSide,
      enabled: false,
    })
    copyTradingFollows.value = response?.follows || []
    if (response?.copyTrading) applyBinanceSnapshot({ copyTrading: response.copyTrading })
    ElMessage.success('已取消聪明钱持仓跟随')
  } catch (error) {
    ElMessage.error(`取消聪明钱持仓跟随失败：${marketErrorMessage(error)}`)
  } finally {
    copyTradingFollowSavingKey.value = ''
  }
}
const copyTradingPositionForRecord = (record) => {
  const symbol = copyTradingPositionSymbol(record)
  const direction = copyTradingDirection(record)
  return copyTradingPositions.value.find((position) => copyTradingPositionSymbol(position) === symbol && copyTradingPositionSide(position) === direction)
    || copyTradingPositions.value.find((position) => copyTradingPositionSymbol(position) === symbol)
    || null
}
const copyTradingRecordAction = (record) => {
  const explicitAction = String(
    record?.openClose
    ?? record?.positionAction
    ?? record?.action
    ?? record?.tradeAction
    ?? record?.tradeType
    ?? '',
  ).toUpperCase()
  if (record?.reduceOnly === true || record?.isReduceOnly === true || record?.closePosition === true || record?.isClose === true) return 'CLOSE'
  if (explicitAction.includes('CLOSE') || explicitAction.includes('REDUCE') || explicitAction.includes('平仓')) return 'CLOSE'
  if (explicitAction.includes('OPEN') || explicitAction.includes('开仓')) return 'OPEN'
  const positionSide = String(record?.positionSide || record?.positionDirection || '').toUpperCase()
  const side = String(record?.side || record?.orderSide || '').toUpperCase()
  if ((positionSide === 'LONG' || positionSide === 'SHORT') && (side === 'BUY' || side === 'SELL')) {
    return positionSide === 'LONG' ? (side === 'SELL' ? 'CLOSE' : 'OPEN') : (side === 'BUY' ? 'CLOSE' : 'OPEN')
  }
  const currentPosition = copyTradingPositions.value.find((position) => copyTradingPositionSymbol(position) === copyTradingPositionSymbol(record))
  return currentPosition && copyTradingDirection(record) !== copyTradingPositionSide(currentPosition) ? 'CLOSE' : 'OPEN'
}
const copyTradingRecordActionDirection = (record) => {
  const positionSide = String(record?.positionSide || record?.positionDirection || '').toUpperCase()
  if (positionSide === 'LONG' || positionSide === 'SHORT') return positionSide
  if (copyTradingRecordAction(record) === 'CLOSE') {
    const currentPosition = copyTradingPositions.value.find((position) => copyTradingPositionSymbol(position) === copyTradingPositionSymbol(record))
    if (currentPosition) return copyTradingPositionSide(currentPosition)
  }
  return copyTradingDirection(record)
}
const copyTradingRecordActionLabel = (record) => {
  const direction = copyTradingRecordActionDirection(record)
  return copyTradingRecordAction(record) === 'CLOSE' ? (direction === 'LONG' ? '平多' : '平空') : (direction === 'LONG' ? '做多' : '做空')
}
const copyTradingRecordActionClass = (record) => {
  const action = copyTradingRecordAction(record)
  const direction = copyTradingRecordActionDirection(record).toLowerCase()
  return `binance-copy-action is-${action.toLowerCase()}-${direction}`
}
const copyTradingPositionLeverage = (position) => {
  const leverage = asNumber(position?.leverage)
  return leverage > 0 ? leverage : '--'
}
const copyTradingPositionQuantity = (position) => {
  const value = position?.positionAmt ?? position?.amount ?? position?.quantity ?? position?.qty ?? position?.executedQty
  return value == null ? value : Math.abs(asNumber(value))
}
const copyTradingOwnPositionValue = (position) => {
  const notional = Math.abs(asNumber(position?.notional ?? position?.positionValue ?? position?.positionNotional))
  if (notional > 0) return notional
  const quantity = Math.abs(asNumber(position?.quantity ?? position?.positionAmt ?? position?.amount))
  const entryPrice = asNumber(position?.entryPrice)
  return quantity > 0 && entryPrice > 0 ? quantity * entryPrice : null
}
const copyTradingOwnLeverage = (position) => {
  const leverage = asNumber(position?.leverage)
  return leverage > 0 ? `${leverage}x` : '--'
}
const copyTradingOwnPnl = (position) => position?.unrealizedProfit ?? position?.unRealizedProfit ?? position?.unrealizedPnl ?? position?.pnl ?? 0
const copyTradingOwnPnlPercent = (position) => {
  const reported = Number(position?.roePercent)
  return Number.isFinite(reported) ? reported : futuresPositionRoePercent(position)
}
const copyTradingOwnEntryClass = (position) => {
  const own = copyTradingOwnPosition(position)
  const smartEntry = asNumber(copyTradingPositionEntry(position))
  const ownEntry = asNumber(own?.entryPrice)
  if (!(smartEntry > 0) || !(ownEntry > 0)) return 'binance-smart-own-entry'
  const better = copyTradingPositionSide(position) === 'SHORT' ? ownEntry > smartEntry : ownEntry < smartEntry
  return better ? 'binance-smart-own-entry is-better' : 'binance-smart-own-entry is-worse'
}
const copyTradingPositionMark = (position) => position?.markPrice ?? position?.lastPrice ?? position?.currentPrice
const copyTradingPositionPnl = (position) => position?.unrealizedProfit ?? position?.unrealizedPnl ?? position?.unRealizedProfit ?? position?.pnl
const copyTradingPositionPnlPercent = (position) => {
  const reportedPercent = Number(position?.roiPercent)
  if (Number.isFinite(reportedPercent)) return reportedPercent
  const reportedRoi = Number(position?.roi)
  if (Number.isFinite(reportedRoi)) return reportedRoi * 100
  const pnl = asNumber(copyTradingPositionPnl(position))
  const margin = asNumber(position?.margin)
  return pnl !== 0 && margin > 0 ? pnl / margin * 100 : 0
}
const copyTradingPositionEntry = (position) => position?.entryPrice ?? position?.avgPrice ?? position?.averagePrice
const copyTradingPositionValue = (position) => {
  const quantity = Math.abs(asNumber(copyTradingPositionQuantity(position)))
  const entryPrice = asNumber(copyTradingPositionEntry(position))
  return quantity > 0 && entryPrice > 0 ? quantity * entryPrice : null
}
const smartMoneyPnlTone = (value) => {
  const number = asNumber(value)
  return number > 0 ? 'is-profit' : number < 0 ? 'is-loss' : 'is-neutral'
}
const copyTradingRecommendedPositionValueForSource = (position, sourceMargin, multiplier = copyTradingMultiplier.value) => {
  const normalizedSourceMargin = asNumber(sourceMargin)
  const accountMargin = copyTradingAccountMargin.value
  const positionValue = copyTradingPositionValue(position)
  if (!(normalizedSourceMargin > 0) || !(accountMargin > 0) || !(positionValue > 0)) return null
  const rawValue = accountMargin * (positionValue / normalizedSourceMargin) * multiplier
  return normalizeRecommendedValue(rawValue, copyTradingPositionEntry(position), copyTradingPositionSymbol(position), 'LIMIT') ?? rawValue
}
const copyTradingRecommendedPositionValue = (position) => (
  copyTradingRecommendedPositionValueForSource(position, copyTradingSourceMargin.value)
)
const copyTradingPositionValueDeviation = (position) => {
  const own = copyTradingOwnPosition(position)
  const recommended = Number(copyTradingRecommendedPositionValue(position))
  const actual = Number(own ? copyTradingOwnPositionValue(own) : NaN)
  return Number.isFinite(recommended) && Number.isFinite(actual) ? Math.abs(recommended - actual) : null
}
const copyTradingPositionValueAlert = (position) => {
  const deviation = copyTradingPositionValueDeviation(position)
  return deviation != null && deviation > 5
}
const copyTradingPositionKey = (position) => `${copyTradingPositionSymbol(position)}-${position?.positionSide || position?.side || ''}-${position?.updateTime || position?.time || position?.entryPrice || ''}`
const copyTradingPositionTabKey = (position) => `${copyTradingPositionSymbol(position)}-${copyTradingPositionSide(position)}`
const copyTradingPositionsDisplay = computed(() => [...copyTradingPositions.value].sort((left, right) => {
  const leftCopying = copyTradingOwnPosition(left) ? 1 : 0
  const rightCopying = copyTradingOwnPosition(right) ? 1 : 0
  return rightCopying - leftCopying
}))
const selectedCopyTradingPosition = computed(() => {
  const positions = copyTradingPositionsDisplay.value
  return positions.find((position) => copyTradingPositionTabKey(position) === selectedSmartMoneyPositionKey.value) || positions[0] || null
})

const openCopyTradeDialog = (record) => {
  if (!props.currentUser) {
    emit('login-request')
    return
  }
  if (!record?.symbol) return
  const referencePrice = asNumber(record.avgPrice || record.price || selectedTicker.value?.lastPrice) || null
  const smartPosition = copyTradingPositionForRecord(record)
  const smartLeverage = asNumber(smartPosition?.leverage)
  const recordQuantity = asNumber(record.qty || record.executedQty || record.origQty)
  const recommendedQuantity = copyTradingRecommendedQuantity(record, 'LIMIT')
    || normalizeQuantityForOrder(recordQuantity, record.symbol, 'LIMIT')
    || quantityMinFor(record.symbol, 'LIMIT')
    || quantityStepFor(record.symbol, 'LIMIT')
  copyTradeRecord.value = { ...record }
  copyTradeForm.value = {
    orderType: 'LIMIT',
    symbol: String(record.symbol).toUpperCase(),
    direction: copyTradingDirection(record),
    quantity: recommendedQuantity,
    leverage: smartLeverage > 0 ? Math.min(20, Math.max(1, Math.round(smartLeverage))) : 1,
    costPrice: referencePrice,
    note: `跟随公开操作：${record.symbol}`,
  }
  copyTradeDialogVisible.value = true
}

const openCopyTradeDialogForPosition = (position) => {
  const symbol = copyTradingPositionSymbol(position)
  const direction = copyTradingPositionSide(position)
  const entryPrice = asNumber(copyTradingPositionEntry(position))
  if (!symbol || symbol === '--' || !(entryPrice > 0)) return
  openCopyTradeDialog({
    symbol,
    side: direction === 'LONG' ? 'BUY' : 'SELL',
    positionSide: direction,
    avgPrice: entryPrice,
    qty: copyTradingPositionQuantity(position),
    time: position?.updateTime || position?.time || Date.now(),
  })
}

const selectSmartMoneyMarket = (symbol) => {
  const safeSymbol = String(symbol || '').trim().toUpperCase()
  if (!safeSymbol) return
  if (!selectMarket(safeSymbol)) {
    selectedExecutionPositionId.value = null
    selectedMarketSymbol.value = safeSymbol
    marketSearch.value = safeSymbol
    loadMarketKlines()
  }
}

const submitCopyTrade = async () => {
  if (!props.currentUser || copyTradeSaving.value) return
  const form = copyTradeForm.value
  const normalizedQuantity = normalizeQuantityForOrder(form.quantity, form.symbol, form.orderType)
  if (!(normalizedQuantity > 0)) {
    ElMessage.warning('跟单数量低于交易所最小单位，无法提交')
    return
  }
  form.quantity = normalizedQuantity
  if (!form.symbol || !asNumber(form.quantity) || !asNumber(form.costPrice)) {
    ElMessage.warning('请填写数量和参考成本价')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将提交 ${form.symbol} ${form.direction === 'LONG' ? '做多' : '做空'}${form.orderType === 'MARKET' ? '市价单' : '限价单'}，不会创建本地计划或保护点位。`,
      '确认真实跟单',
      { type: 'warning', confirmButtonText: '确认提交', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  copyTradeSaving.value = true
  try {
    await placeBinanceCopyTradingOrder({ ...form })
    copyTradeDialogVisible.value = false
    await startSnapshotSubscription()
    ElMessage.success('真实跟单入场单已提交，不会自动创建本地计划')
  } catch (error) {
    ElMessage.error(`跟单提交失败：${marketErrorMessage(error)}`)
  } finally {
    copyTradeSaving.value = false
  }
}
const timeframeBiasLabel = (timeframe, side = null) => {
  const label = ({ BULL: '偏多', BEAR: '偏空', NEUTRAL: '中性' }[timeframe?.bias] || '待判断')
  if (!side || !timeframe?.bias || timeframe.bias === 'NEUTRAL') return label
  const expectedBias = side === 'SHORT' ? 'BEAR' : 'BULL'
  return timeframe.bias === expectedBias ? label : `${label} · 预警`
}
const timeframeBiasClass = (timeframe) => timeframe?.bias === 'BULL' ? 'is-bull' : (timeframe?.bias === 'BEAR' ? 'is-bear' : 'is-neutral')
const timeframeBiasDisplayClass = (bias) => bias === 'BULL' ? 'binance-direction-long' : (bias === 'BEAR' ? 'binance-direction-short' : 'binance-neutral')
const accountDrawerStyle = computed(() => accountDrawerOpen.value ? {} : { top: `${accountDrawerTop.value}px` })
const accountDrawerUnrealizedPnl = computed(() => {
  const futures = account.value?.futures
  const rawValue = futures?.totalUnrealizedProfit
  if (!futures || futures.available === false || rawValue === null || rawValue === undefined || rawValue === '') return null
  const value = Number(rawValue)
  return Number.isFinite(value) ? value : null
})
const accountUnrealizedTone = computed(() => {
  const value = accountDrawerUnrealizedPnl.value
  if (value === null) return 'tone-zero'
  return value > 0 ? 'tone-positive' : value < 0 ? 'tone-negative' : 'tone-zero'
})
const accountBalanceRatio = computed(() => {
  const futures = account.value?.futures
  const available = Number(futures?.availableBalance)
  const total = Number(futures?.totalMarginBalance)
  if (!Number.isFinite(available) || !Number.isFinite(total) || total <= 0) return 0
  return Math.round(Math.min(100, Math.max(0, available / total * 100)))
})
const futuresPositions = computed(() => account.value?.futures?.positions || [])
const futuresPositionPnlHistoryFor = (position) => {
  const history = accountPositionPnlHistory.value
  if (!Array.isArray(history)) return []
  const symbol = String(position?.symbol || '').toUpperCase()
  const side = String(position?.side || position?.positionSide || '').toUpperCase()
  return history.find((item) => (
    String(item?.symbol || '').toUpperCase() === symbol
    && String(item?.positionSide || '').toUpperCase() === side
  ))?.points || []
}
const activeMarkets = computed(() => [...futuresMarkets.value].sort((left, right) => (
  asNumber(right?.quoteVolume) - asNumber(left?.quoteVolume)
  || String(left?.symbol || '').localeCompare(String(right?.symbol || ''))
)))
const activeMarketsLoading = computed(() => futuresLoading.value)
const activeMarketsError = computed(() => futuresError.value)
const activeMarketsStale = computed(() => futuresStale.value)
const ticker = computed(() => {
  if (selectedTicker.value?.symbol === selectedMarketSymbol.value) return selectedTicker.value
  return activeMarkets.value.find((item) => item.symbol === selectedMarketSymbol.value) || null
})
const chartCurrentPrice = computed(() => asNumber(ticker.value?.markPrice) || asNumber(ticker.value?.lastPrice) || 0)
const selectedMarketItem = computed(() => activeMarkets.value.find((item) => item.symbol === selectedMarketSymbol.value) || null)
const isMarketSearchSelection = computed(() => Boolean(selectedMarketItem.value) && String(marketSearch.value || '').trim().toUpperCase() === selectedMarketSymbol.value)
const futuresMarketForSymbol = (symbol) => futuresMarkets.value.find((item) => String(item?.symbol || '').toUpperCase() === String(symbol || '').toUpperCase()) || null
const quantityRuleFor = (symbol, orderType = 'LIMIT', position = null) => {
  const market = futuresMarketForSymbol(symbol)
  const isMarket = String(orderType || '').toUpperCase() === 'MARKET'
  const source = position || market
  const preferredStep = asNumber(isMarket ? source?.marketQuantityStep : source?.quantityStep)
  const preferredMin = asNumber(isMarket ? source?.marketMinQuantity : source?.minQuantity)
  const preferredMax = asNumber(isMarket ? source?.marketMaxQuantity : source?.maxQuantity)
  return {
    step: preferredStep > 0 ? preferredStep : asNumber(source?.quantityStep || market?.quantityStep),
    min: preferredMin > 0 ? preferredMin : asNumber(source?.minQuantity || market?.minQuantity),
    max: preferredMax > 0 ? preferredMax : asNumber(source?.maxQuantity || market?.maxQuantity),
  }
}
const quantityPrecisionForStep = (step) => {
  const text = String(step ?? '')
  if (!text || text === '0') return 8
  if (text.includes('e-')) return Math.min(8, Number(text.split('e-')[1]) || 8)
  return Math.min(8, Math.max(0, (text.split('.')[1] || '').length))
}
const floorQuantityToStep = (value, step) => {
  const quantity = asNumber(value)
  const unit = asNumber(step)
  if (quantity <= 0 || unit <= 0) return quantity > 0 ? quantity : 0
  const precision = quantityPrecisionForStep(unit)
  const rounded = Math.floor(quantity / unit + 1e-9) * unit
  return Number(rounded.toFixed(precision))
}
const normalizeQuantityForOrder = (value, symbol, orderType = 'LIMIT', position = null) => {
  const rules = quantityRuleFor(symbol, orderType, position)
  const quantity = floorQuantityToStep(value, rules.step)
  if (quantity <= 0 || (rules.min > 0 && quantity < rules.min) || (rules.max > 0 && quantity > rules.max)) return 0
  return quantity
}
const quantityStepFor = (symbol, orderType = 'LIMIT') => quantityRuleFor(symbol, orderType).step || 0.001
const quantityMinFor = (symbol, orderType = 'LIMIT') => quantityRuleFor(symbol, orderType).min
const marketQuantityStepFor = (symbol, position = null) => quantityRuleFor(symbol, 'MARKET', position).step
const marketQuantityMinFor = (symbol, position = null) => quantityRuleFor(symbol, 'MARKET', position).min
const marketCloseQuantity = computed(() => {
  const position = marketClosePosition.value
  if (!position) return 0
  const ratio = Math.min(100, Math.max(1, asNumber(marketCloseForm.value.quantityRatio) || 1))
  return normalizeQuantityForOrder(asNumber(position.quantity) * ratio / 100, position.symbol, 'MARKET', position)
})

const MAX_MARGIN_FRACTION = 0.1
const MICRO_ACCOUNT_THRESHOLD = 10
const MICRO_ACCOUNT_MAX_MARGIN_FRACTION = 0.9
const MICRO_ACCOUNT_MIN_LEVERAGE = 5
const EXPECTED_SLIPPAGE_RATE = 0.001
const MAX_EXCHANGE_LEVERAGE = 20
const matchingFuturesPosition = computed(() => {
  const symbol = String(positionForm.value.symbol || '').trim().toUpperCase()
  if (!symbol) return null
  const side = String(positionForm.value.side || '').toUpperCase()
  return futuresPositions.value.find((position) => String(position.symbol || '').toUpperCase() === symbol && position.side === side)
    || futuresPositions.value.find((position) => String(position.symbol || '').toUpperCase() === symbol)
    || null
})
const futuresAccountBase = computed(() => {
  const futures = account.value?.futures
  const availableBalance = asNumber(futures?.availableBalance)
  const marginBalance = asNumber(futures?.totalMarginBalance)
  if (!futures || futures.available === false || availableBalance <= 0 || marginBalance <= 0) return 0
  return Math.min(availableBalance, marginBalance)
})
const currentFormLeverage = computed(() => Math.min(MAX_EXCHANGE_LEVERAGE, Math.max(1, asNumber(positionForm.value.leverage) || 1)))
const quantityFromMarginRatio = computed(() => {
  if (!futuresAccountBase.value) return 0
  const costPrice = asNumber(positionForm.value.costPrice)
  const leverage = currentFormLeverage.value
  const marginRatio = Math.min(100, Math.max(1, asNumber(positionForm.value.marginRatio) || 1))
  if (costPrice <= 0 || leverage <= 0) return 0
  return futuresAccountBase.value * marginRatio / 100 * leverage / costPrice
})
const normalizedQuantityFromMarginRatio = computed(() => normalizeQuantityForOrder(quantityFromMarginRatio.value, positionForm.value.symbol, 'LIMIT'))
const effectivePositionQuantity = computed(() => {
  if (positionForm.value.quantityMode === 'MARGIN_RATIO') {
    return normalizedQuantityFromMarginRatio.value
  }
  return normalizeQuantityForOrder(positionForm.value.quantity, positionForm.value.symbol, 'LIMIT')
})
const positionRiskAdvice = computed(() => {
  const futures = account.value?.futures
  const availableBalance = asNumber(futures?.availableBalance)
  const marginBalance = asNumber(futures?.totalMarginBalance)
  if (!futures || futures.available === false || availableBalance <= 0 || marginBalance <= 0) {
    return { available: false, reason: '连接合约账户后才能根据可用余额和保证金余额计算风险建议。' }
  }
  const sourcePlan = executionPlan.value || selectedExecutionPosition.value?.plan?.executionPlan || null
  const costPrice = asNumber(positionForm.value.costPrice)
  const stopLoss = asNumber(sourcePlan?.stopLoss) || asNumber(selectedExecutionPosition.value?.plan?.initialStop)
  if (costPrice <= 0 || stopLoss <= 0) {
    return { available: false, reason: '当前计划缺少有效成本价或结构止损，暂不能计算风险建议。' }
  }
  const isLong = positionForm.value.side !== 'SHORT'
  if ((isLong && stopLoss >= costPrice) || (!isLong && stopLoss <= costPrice)) {
    return { available: false, reason: '结构止损必须位于成本价的防守侧，修正成本价后才能计算风险建议。' }
  }
  const accountBase = Math.min(availableBalance, marginBalance)
  const isMicroAccount = accountBase <= MICRO_ACCOUNT_THRESHOLD
  const riskFraction = configuredRiskFraction.value
  const maxMarginFraction = isMicroAccount ? MICRO_ACCOUNT_MAX_MARGIN_FRACTION : MAX_MARGIN_FRACTION
  const stopDistanceRate = Math.abs(costPrice - stopLoss) / costPrice + EXPECTED_SLIPPAGE_RATE
  const riskBudget = accountBase * riskFraction
  const riskLeverageLimit = riskFraction / (stopDistanceRate * maxMarginFraction)
  const observedLeverage = Math.min(MAX_EXCHANGE_LEVERAGE, Math.max(1, asNumber(matchingFuturesPosition.value?.leverage) || 1))
  const leverageFloor = isMicroAccount ? MICRO_ACCOUNT_MIN_LEVERAGE : 1
  const maxLeverage = Math.min(MAX_EXCHANGE_LEVERAGE, Math.max(1, Math.floor(Math.max(riskLeverageLimit, observedLeverage, leverageFloor))))
  const currentLeverage = currentFormLeverage.value
  const maxPositionRatio = Math.min(maxMarginFraction, riskFraction / (stopDistanceRate * currentLeverage))
  const maxQuantity = accountBase * maxPositionRatio * currentLeverage / costPrice
  const quantity = effectivePositionQuantity.value
  return {
    available: true,
    availableBalance,
    marginBalance,
    accountBase,
    riskBudget,
    riskFraction,
    maxMarginFraction,
    isMicroAccount,
    costPrice,
    stopLoss,
    stopDistanceRate,
    maxLeverage,
    observedLeverage: matchingFuturesPosition.value?.leverage ? observedLeverage : null,
    currentLeverage,
    maxPositionRatio,
    maxQuantity,
    quantityOverLimit: quantity > maxQuantity * 1.000000001,
  }
})
const positionLossAdvice = computed(() => {
  const sourcePlan = executionPlan.value || selectedExecutionPosition.value?.plan?.executionPlan || null
  const costPrice = asNumber(positionForm.value.costPrice)
  const stopLoss = asNumber(sourcePlan?.stopLoss) || asNumber(selectedExecutionPosition.value?.plan?.initialStop)
  // Read leverage even when quantity is fixed so the preview is recomputed
  // whenever either execution control changes. Leverage changes quantity
  // only in margin-ratio mode; it must not be multiplied into fixed-quantity
  // PnL a second time.
  const leverage = currentFormLeverage.value
  const quantity = effectivePositionQuantity.value
  if (costPrice <= 0 || stopLoss <= 0 || quantity <= 0) {
    return { available: false, reason: '填写有效的持仓成本、数量并选择包含结构止损的分析计划后，才能估算止损损失。' }
  }
  const futures = account.value?.futures
  const futuresMargin = asNumber(futures?.totalMarginBalance)
  const accountBase = futures && futures.available !== false ? futuresMargin : 0
  if (accountBase <= 0) {
    return { available: false, reason: '连接合约账户并取得总保证金余额后，才能计算账户亏损比。' }
  }
  const isLong = positionForm.value.side !== 'SHORT'
  const stopOnDefenseSide = isLong ? stopLoss < costPrice : stopLoss > costPrice
  const notional = costPrice * quantity
  const estimatedLoss = Math.abs(costPrice - stopLoss) * quantity
  const estimatedLossWithSlippage = estimatedLoss + costPrice * EXPECTED_SLIPPAGE_RATE * quantity
  const accountLossRatio = estimatedLoss / accountBase
  const suggestedRiskRatio = configuredRiskFraction.value
  return {
    available: true,
    costPrice,
    stopLoss,
    quantity,
    notional,
    accountBase,
    accountBaseLabel: '总保证金余额',
    estimatedLoss,
    estimatedLossWithSlippage,
    accountLossRatio,
    suggestedRiskRatio,
    leverage,
    quantityMode: positionForm.value.quantityMode,
    exceedsSuggestedRisk: !stopOnDefenseSide || (suggestedRiskRatio > 0 && accountLossRatio > suggestedRiskRatio),
    stopOnDefenseSide,
  }
})
const marginAmountFromRatio = computed(() => {
  const quantity = quantityFromMarginRatio.value
  const costPrice = asNumber(positionForm.value.costPrice)
  const leverage = currentFormLeverage.value
  return quantity > 0 && costPrice > 0 && leverage > 0 ? quantity * costPrice / leverage : 0
})
const notionalFromRatio = computed(() => quantityFromMarginRatio.value * asNumber(positionForm.value.costPrice))
watch([quantityFromMarginRatio, () => positionForm.value.quantityMode], ([quantity, mode]) => {
  if (mode === 'MARGIN_RATIO' && quantity > 0) positionForm.value.quantity = normalizedQuantityFromMarginRatio.value
})

const futuresAnalysisRunning = computed(() => ['QUEUED', 'RUNNING'].includes(futuresAnalysisJob.value?.status))
const sortFuturesPlans = (plans) => [...(plans || [])].sort((left, right) => {
  const priority = (plan) => plan?.status === 'ARMED' ? 2 : (plan?.trialEligible === true ? 1 : 0)
  const modelScore = (plan) => {
    const strategyScore = Number(plan?.modelStrategy?.selectionScore)
    if (Number.isFinite(strategyScore)) return strategyScore
    const direction = String(plan?.direction || '').toLowerCase()
    const details = plan?.modelPrediction?.[direction]
    const predictionScore = Number(details?.selectionScore ?? details?.timeEfficiencyScore ?? plan?.modelPrediction?.selectionScore)
    return Number.isFinite(predictionScore) ? predictionScore : 0
  }
  const timeEfficiency = (plan) => {
    const strategyValue = Number(plan?.modelStrategy?.timeEfficiency)
    if (Number.isFinite(strategyValue)) return strategyValue
    const direction = String(plan?.direction || '').toLowerCase()
    const detailsValue = Number(plan?.modelPrediction?.[direction]?.timeEfficiencyScore)
    const directValue = Number(plan?.modelPrediction?.timeEfficiencyScore)
    return Number.isFinite(detailsValue)
      ? detailsValue
      : (Number.isFinite(directValue) ? directValue : Number(plan?.timeCost?.efficiency) || 0)
  }
  const expectedR = (plan) => {
    const strategyValue = Number(plan?.modelStrategy?.expectedR)
    if (Number.isFinite(strategyValue)) return strategyValue
    const direction = String(plan?.direction || '').toLowerCase()
    const detailsValue = Number(plan?.modelPrediction?.[direction]?.expectedR)
    const directValue = Number(plan?.modelPrediction?.expectedR)
    return Number.isFinite(detailsValue) ? detailsValue : (Number.isFinite(directValue) ? directValue : 0)
  }
  const planRiskReward = (plan) => {
    return planRiskRewardValue(plan)
  }
  const priorityDelta = priority(right) - priority(left)
  const scoreDelta = modelScore(right) - modelScore(left)
  const efficiencyDelta = timeEfficiency(right) - timeEfficiency(left)
  const expectedRDelta = expectedR(right) - expectedR(left)
  const planRiskRewardDelta = planRiskReward(right) - planRiskReward(left)
  const confidenceDelta = asNumber(right.conditionCompleteness ?? right.confidence) - asNumber(left.conditionCompleteness ?? left.confidence)
  return priorityDelta || scoreDelta || efficiencyDelta || expectedRDelta || planRiskRewardDelta || confidenceDelta || (asNumber(right.quality) - asNumber(left.quality)) || (asNumber(right.quoteVolume) - asNumber(left.quoteVolume))
})
const isWaitAnalysisPlan = (plan) => (
  String(plan?.status || '').toUpperCase() === 'WAIT'
  || String(plan?.direction || '').toUpperCase() === 'WAIT'
)
const isVisibleAnalysisPlan = (plan) => {
  if (!plan?.symbol) return false
  // WAIT is a completed model inference, but it is not a candidate plan. Do
  // not put it in the selectable result list (the scan summary may still
  // report that no executable plan was found).
  if (isWaitAnalysisPlan(plan)) return false
  // New responses carry the backend's authoritative execution decision. A
  // target analysis may still expose a non-actionable named-symbol plan for
  // monitoring, but batch results must agree with the server gate.
  if (typeof plan.executionEligible === 'boolean') {
    if (plan.executionEligible) return true
    return futuresAnalysisScope.value === 'target' && plan.monitoringPlan === true
  }
  const status = String(plan.status || '').toUpperCase()
  // Compatibility for an in-flight/legacy response without the flag. Direct
  // MODEL results use their own one-condition contract; do not apply the
  // classic 10-condition filter to them.
  if (String(plan.strategyEngine || '').toUpperCase() === 'MODEL' && String(plan.modelTask || '').toUpperCase() === 'DIRECT_PLAN') return true
  if (status === 'ARMED') {
    const hasScore = plan.conditionMet !== undefined || plan.conditionTotal !== undefined
    return !hasScore || (asNumber(plan.conditionMet) === 10 && asNumber(plan.conditionTotal) === 10)
  }
  return status !== 'ARMED'
    && plan.trialEligible === true
    && asNumber(plan.conditionMet) === 9
    && asNumber(plan.conditionTotal) === 10
}
const futuresScanLimitMax = computed(() => Math.max(1, activeMarkets.value.length || 500))
const activeScanLimit = computed({
  get: () => futuresScanLimit.value,
  set: (value) => {
    const numericValue = Number(value)
    const normalized = Number.isFinite(numericValue) ? Math.min(futuresScanLimitMax.value, Math.max(1, Math.trunc(numericValue))) : 100
    futuresScanLimit.value = normalized
  },
})
const activeAnalysisJob = computed(() => futuresAnalysisJob.value)
const activeAnalysisPlans = computed(() => futuresPlans.value)
const activeArmedPlanCount = computed(() => activeAnalysisPlans.value.filter((plan) => plan.status === 'ARMED').length)
const activeTrialPlanCount = computed(() => activeAnalysisPlans.value.filter((plan) => plan.status !== 'ARMED' && plan.trialEligible).length)
const activeAnalysisRunning = computed(() => futuresAnalysisRunning.value)
const activeAnalyzing = computed(() => futuresAnalyzing.value)
const activeAnalysisScope = computed(() => futuresAnalysisScope.value)
const targetAnalyzing = computed(() => activeAnalysisRunning.value && activeAnalysisScope.value === 'target')
const activeAnalysisProgress = computed(() => activeAnalysisJob.value?.progress || { currentSymbol: null, completed: 0, total: 0, matched: 0, failed: 0, parallelWorkers: 0, message: '' })
const activeAnalysisPercent = computed(() => {
  const total = asNumber(activeAnalysisProgress.value.total)
  return total ? Math.min(100, Math.round((asNumber(activeAnalysisProgress.value.completed) / total) * 100)) : 0
})
const activeAnalysisPlan = computed(() => {
  const plans = activeAnalysisPlans.value
  return plans.find((plan) => plan.symbol === selectedFuturesPlanSymbol.value) || plans[0] || null
})
const isModelAnalysis = computed(() => String(activeAnalysisPlan.value?.strategyEngine || futuresAnalysisJob.value?.result?.strategyEngine || '').toUpperCase() === 'MODEL')
const activeModelPrediction = computed(() => {
  const plan = activeAnalysisPlan.value
  if (String(plan?.strategyEngine || '').toUpperCase() !== 'MODEL') return null
  return plan?.modelPrediction && typeof plan.modelPrediction === 'object' ? plan.modelPrediction : null
})
const activeModelDetails = computed(() => {
  const prediction = activeModelPrediction.value
  const side = String(prediction?.direction || activeAnalysisPlan.value?.direction || '').toLowerCase()
  const details = prediction?.[side]
  return details && typeof details === 'object' ? details : null
})
const formatModelProbability = (value) => {
  const number = Number(value)
  return Number.isFinite(number) ? `${(number * 100).toFixed(1)}%` : '--'
}
const formatModelPercent = (value) => formatModelProbability(value)
const formatModelDecimal = (value) => {
  if (value === null || value === undefined || value === '') return '--'
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(2) : '--'
}
const formatModelR = (value) => {
  const number = Number(value)
  return Number.isFinite(number) ? `${number.toFixed(3)}R` : '--'
}
const formatPlanRiskReward = (value) => {
  const number = planRiskRewardValue(value)
  return number > 0 ? `${number.toFixed(2)}R` : '--'
}
const formatTargetR = (value) => {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? `${number.toFixed(2)}R` : '--'
}
const intervalMinutes = (value) => {
  const match = String(value || '').trim().match(/^(\d+(?:\.\d+)?)\s*([mhdw])$/i)
  if (!match) return null
  const amount = Number(match[1])
  const unit = match[2].toLowerCase()
  const multiplier = { m: 1, h: 60, d: 1440, w: 10080 }[unit]
  const minutes = amount * multiplier
  return Number.isFinite(minutes) && minutes > 0 ? minutes : null
}
const formatHoursAndMinutes = (value) => {
  const totalMinutes = Math.max(0, Math.round(Number(value)))
  if (!Number.isFinite(totalMinutes)) return '--'
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  return `${hours}时${String(minutes).padStart(2, '0')}分钟`
}
const modelExecutionIntervalFor = (value) => (
  value?.modelStrategy?.executionInterval
  || value?.timeCost?.executionInterval
  || value?.executionInterval
  || value?.config?.executionInterval
  || '5m'
)
const formatModelBars = (value, executionInterval = '5m') => {
  const bars = Number(value)
  const minutesPerBar = intervalMinutes(executionInterval)
  return Number.isFinite(bars) && bars >= 0 && minutesPerBar ? formatHoursAndMinutes(bars * minutesPerBar) : '--'
}
const timestampMs = (value) => {
  if (value === null || value === undefined || value === '') return null
  if (typeof value === 'number' && Number.isFinite(value)) return Math.abs(value) < 1e11 ? value * 1000 : value
  const text = String(value).trim()
  if (!text) return null
  if (/^-?\d+(?:\.\d+)?$/.test(text)) {
    const numeric = Number(text)
    return Number.isFinite(numeric) ? (Math.abs(numeric) < 1e11 ? numeric * 1000 : numeric) : null
  }
  const parsed = Date.parse(text)
  return Number.isFinite(parsed) ? parsed : null
}
const formatMonitoringDuration = (position) => {
  const startedAt = timestampMs(position?.createdAt)
  if (startedAt === null) return '--'
  return formatHoursAndMinutes(Math.max(0, monitorDurationNow.value - startedAt) / 60000)
}
const formatForecastTriplet = (values, offset) => {
  const list = Array.isArray(values) ? values.slice(offset, offset + 3).map((value) => Number(value)) : []
  return list.length === 3 && list.every((value) => Number.isFinite(value)) ? list.map((value) => value.toFixed(4)).join(' / ') : '--'
}
const formatForecastDirection = (values) => {
  if (!values || typeof values !== 'object') return '--'
  return ['LONG', 'SHORT', 'WAIT'].map((key) => {
    const number = Number(values[key])
    return `${key} ${Number.isFinite(number) ? (number * 100).toFixed(1) : '--'}%`
  }).join(' · ')
}
const modelVerdictLabel = (value) => ({ FAVORABLE: '有利', UNCERTAIN: '不确定', WAIT: '等待' }[String(value || '').toUpperCase()] || '未知')
const modelVerdictClass = (value) => ({ FAVORABLE: 'favorable', UNCERTAIN: 'uncertain', WAIT: 'wait' }[String(value || '').toUpperCase()] || 'wait')
const modelEntryStateLabel = (value) => ({
  WAITING_BELOW_ZONE: '入场点下方等待向上突破',
  WAITING_ABOVE_ZONE: '入场点上方等待向下突破',
  WAITING_IN_ZONE: '等待具体入场点',
  TRIGGER_ALREADY_CROSSED: '触发价已越过，计划阻断',
}[String(value || '').toUpperCase()] || '等待触发')
const modelTargetAdjustmentLabel = (adjustment) => {
  if (!adjustment || typeof adjustment !== 'object') return '--'
  if (!adjustment.applied) return adjustment.reason || '未调整'
  const from = Number(adjustment.fromR)
  const to = Number(adjustment.toR)
  if (Number.isFinite(from) && Number.isFinite(to)) return `${from.toFixed(2)}R → ${to.toFixed(2)}R`
  return '已调整'
}
const planDefaultEntryPrice = (plan) => {
  const recommendedLimitPrice = asNumber(plan?.entryTiming?.recommendedLimitPrice)
  if (recommendedLimitPrice > 0) return recommendedLimitPrice

  const structurePrices = [asNumber(plan?.entry?.zoneLow), asNumber(plan?.entry?.zoneHigh)].filter((price) => price > 0)
  if (structurePrices.length) {
    const stopLoss = asNumber(plan?.stopLoss)
    if (stopLoss > 0) {
      return structurePrices.reduce((closest, price) => (
        Math.abs(price - stopLoss) < Math.abs(closest - stopLoss) ? price : closest
      ))
    }
    return plan?.direction === 'SHORT' ? Math.max(...structurePrices) : Math.min(...structurePrices)
  }
  return asNumber(plan?.entry?.trigger) || asNumber(plan?.lastPrice)
}
const entryPricePresets = computed(() => {
  const plan = executionPlan.value
  if (!plan) return []

  const zoneLow = asNumber(plan.entry?.zoneLow)
  const zoneHigh = asNumber(plan.entry?.zoneHigh)
  const presets = []
  const addPreset = (key, label, price) => {
    const normalizedPrice = asNumber(price)
    if (normalizedPrice <= 0 || presets.some((preset) => preset.price === normalizedPrice)) return
    presets.push({ key, label, price: normalizedPrice })
  }

  if (zoneLow > 0 && zoneHigh > 0) {
    const low = Math.min(zoneLow, zoneHigh)
    const high = Math.max(zoneLow, zoneHigh)
    addPreset('zone-low', '触发区最低', low)
    addPreset('zone-mid', '触发区中位', (low + high) / 2)
    addPreset('zone-high', '触发区最高', high)
  }

  addPreset('5m-entry', '5m 入场价', plan.entryTiming?.recommendedLimitPrice)
  return presets
})
const selectEntryPricePreset = (preset) => {
  const price = asNumber(preset?.price)
  if (price > 0) positionForm.value.costPrice = price
}
const activeEntryTimingDisplay = computed(() => {
  const timing = activeAnalysisPlan.value?.entryTiming || {}
  const recommendedPrice = asNumber(timing.recommendedLimitPrice)
  const referencePrice = asNumber(timing.referencePrice)
  if (recommendedPrice > 0) {
    return {
      ready: true,
      hasReference: false,
      title: timing.label || '5m 推荐挂单价',
      value: formatPrice(recommendedPrice),
    }
  }
  if (referencePrice > 0) {
    return {
      ready: false,
      hasReference: true,
      title: '5m 回测结构位',
      value: formatPrice(referencePrice),
    }
  }
  return {
    ready: false,
    hasReference: false,
    title: '5m 入场价',
    value: timing.state === 'UNAVAILABLE' ? '数据不足' : '等待确认',
  }
})
const activeAnalysisTimeframes = computed(() => ['4h', '1h', '15m', '5m'].map((id) => ({ id, ...(activeAnalysisPlan.value?.timeframes?.[id] || {}) })).filter((item) => item.state))
const isDisciplineExecutablePlan = (plan) => {
  if (!plan?.symbol || isWaitAnalysisPlan(plan)) return false
  // The backend flag is authoritative whenever present.  Do not fall back
  // to the legacy permissive checks for an explicit false value.
  if (typeof plan.executionEligible === 'boolean') return plan.executionEligible
  // Compatibility for a response produced before the flag was introduced.
  const status = String(plan.status || '').toUpperCase()
  if (String(plan.strategyEngine || '').toUpperCase() === 'MODEL') {
    return status === 'ARMED'
      && String(plan.modelTask || '').toUpperCase() === 'DIRECT_PLAN'
      && plan.modelGenerated === true
      && ['LONG', 'SHORT'].includes(String(plan.direction || '').toUpperCase())
      && asNumber(plan.entry?.trigger) > 0
      && asNumber(plan.stopLoss) > 0
      && Array.isArray(plan.takeProfits)
      && plan.takeProfits.length >= 2
  }
  if (status === 'ARMED') {
    return (
      (plan.conditionMet === undefined && plan.conditionTotal === undefined)
      || (asNumber(plan.conditionMet) === 10 && asNumber(plan.conditionTotal) === 10)
    )
  }
  return status !== 'ARMED'
    && plan.trialEligible === true
    && asNumber(plan.conditionMet) === 9
    && asNumber(plan.conditionTotal) === 10
}
const isMonitoringAnalysisPlan = (plan) => Boolean(plan?.symbol)
  && activeAnalysisScope.value === 'target'
  && plan?.monitoringPlan === true
  && !isDisciplineExecutablePlan(plan)
const isPlanExecutable = (plan) => isDisciplineExecutablePlan(plan) || isMonitoringAnalysisPlan(plan)
const executionPlanIsMonitoringOnly = computed(() => isMonitoringAnalysisPlan(executionPlan.value))
const selectedExecutionPosition = computed(() => (
  simulatedPositions.value.find((position) => position.id === selectedExecutionPositionId.value)
  || simulatedPositions.value[0]
  || null
))
const actualFuturesPositionFor = (execution) => {
  if (!execution || execution.marketMode !== 'FUTURES') return null
  const symbol = String(execution.symbol || '').trim().toUpperCase()
  const side = String(execution.side || '').trim().toUpperCase()
  return futuresPositions.value.find((position) => {
    const positionSymbol = String(position?.symbol || '').trim().toUpperCase()
    const positionSide = String(position?.side || '').trim().toUpperCase()
    const positionModeSide = String(position?.positionSide || '').trim().toUpperCase()
    const normalizedSide = positionSide || (
      positionModeSide === 'SHORT' ? 'SHORT' : positionModeSide === 'LONG' ? 'LONG' : ''
    )
    return positionSymbol === symbol && (
      normalizedSide === side
      || (!normalizedSide && positionModeSide === 'BOTH')
    )
  }) || null
}
const futuresPositionMonitorKey = (position) => `${String(position?.symbol || '').toUpperCase()}:${String(position?.side || '').toUpperCase()}`
const isFuturesPositionExpanded = (position) => expandedFuturesPositionKey.value === futuresPositionMonitorKey(position)
const toggleFuturesPositionExpanded = (position) => {
  const key = futuresPositionMonitorKey(position)
  expandedFuturesPositionKey.value = expandedFuturesPositionKey.value === key ? '' : key
}
const futuresPositionHasValueAlert = (position) => smartMoneyFollowsForAccountPosition(position).some((follow) => (
  smartMoneyFollowPositionValueAlert(position, follow)
))
const realPositionMonitorFor = (position) => {
  const key = futuresPositionMonitorKey(position)
  return simulatedPositions.value.find((candidate) => (
    candidate?.marketMode === 'FUTURES'
    && futuresPositionMonitorKey(candidate) === key
    && positionExecutionStatus(candidate) !== 'STOPPED'
  )) || null
}
const selectedActualFuturesPosition = computed(() => {
  return actualFuturesPositionFor(selectedExecutionPosition.value)
})
const executionLivePositionFor = (position) => actualFuturesPositionFor(position)
const executionUnrealizedPnlFor = (position) => {
  if (position?.executionStatus === 'STOPPED') return asNumber(position?.unrealizedPnl ?? position?.plan?.frozenPnl)
  const live = executionLivePositionFor(position)
  const value = asNumber(live?.unrealizedProfit ?? live?.unRealizedProfit)
  return live && Number.isFinite(value) ? value : asNumber(position?.unrealizedPnl)
}
const executionUnrealizedPnlPercentFor = (position) => {
  if (position?.executionStatus === 'STOPPED') return position?.unrealizedPnlPercent ?? position?.plan?.frozenPnlPercent
  const live = executionLivePositionFor(position)
  const reported = Number(live?.roePercent)
  if (live && Number.isFinite(reported)) return reported
  const persisted = Number(position?.unrealizedPnlPercent)
  if (Number.isFinite(persisted)) return persisted
  const pnl = executionUnrealizedPnlFor(position)
  const quantity = Math.abs(Number(live?.quantity ?? live?.positionAmt ?? position?.quantity))
  const entryPrice = Number(live?.entryPrice ?? position?.costPrice)
  const leverage = Number(live?.leverage ?? position?.leverage)
  const notional = Math.abs(Number(live?.notional ?? live?.positionValue))
  const initialMargin = Number(live?.initialMargin ?? live?.positionInitialMargin ?? live?.isolatedMargin)
  const margin = quantity > 0 && entryPrice > 0 && leverage > 0
    ? quantity * entryPrice / leverage
    : (Number.isFinite(initialMargin) && initialMargin > 0
        ? initialMargin
        : (notional > 0 && leverage > 0 ? notional / leverage : 0))
  return Number.isFinite(pnl) && margin > 0 ? pnl / margin * 100 : 0
}
// 计划监控的金额和百分比可能来自不同的账户字段，统一按百分比优先判色。
// 百分比缺失或为零时再回退到盈亏金额，避免有效盈亏被渲染成灰色。
const executionPnlClass = (position) => {
  const percent = Number(executionUnrealizedPnlPercentFor(position))
  if (Number.isFinite(percent) && Math.abs(percent) > Number.EPSILON) return changeClass(percent)
  return changeClass(executionUnrealizedPnlFor(position))
}
const executionQuantityFor = (position) => {
  const live = executionLivePositionFor(position)
  const quantity = asNumber(live?.quantity ?? live?.positionAmt)
  return live && quantity > 0 ? quantity : position?.quantity
}
const executionEntryPriceFor = (position) => {
  const live = executionLivePositionFor(position)
  const entry = asNumber(live?.entryPrice)
  return live && entry > 0 ? entry : position?.costPrice
}
const futuresMarketQuoteFor = (position) => {
  const symbol = String(position?.symbol || '').trim().toUpperCase()
  if (!symbol) return null
  return futuresMarkets.value.find((item) => String(item?.symbol || '').toUpperCase() === symbol) || null
}
const futuresLatestPriceFor = (position) => {
  const positionPrice = asNumber(position?.lastPrice)
  if (positionPrice > 0) return positionPrice
  const marketPrice = asNumber(futuresMarketQuoteFor(position)?.lastPrice)
  if (marketPrice > 0) return marketPrice
  return asNumber(position?.markPrice)
}
const futuresMarkPriceFor = (position) => {
  const positionPrice = asNumber(position?.markPrice)
  if (positionPrice > 0) return positionPrice
  return asNumber(futuresMarketQuoteFor(position)?.markPrice)
}
const executionLatestPriceFor = (position) => {
  const live = executionLivePositionFor(position)
  const livePrice = asNumber(live?.lastPrice)
  if (live && livePrice > 0) return livePrice
  const planPrice = asNumber(position?.plan?.latestPrice)
  if (planPrice > 0) return planPrice
  return futuresLatestPriceFor(position)
}
const executionMarkPriceFor = (position) => {
  const live = executionLivePositionFor(position)
  const livePrice = asNumber(live?.markPrice)
  if (live && livePrice > 0) return livePrice
  const planPrice = asNumber(position?.plan?.markPrice)
  if (planPrice > 0) return planPrice
  return futuresMarkPriceFor(actualFuturesPositionFor(position) || position)
}
const positionExecutionStatus = (position) => String(position?.executionStatus || position?.plan?.executionStatus || '').toUpperCase()
const isPendingEntry = (position) => (
  positionExecutionStatus(position) === 'PENDING_ENTRY'
  || String(position?.plan?.monitoringStatus || '').toUpperCase() === 'PENDING_ENTRY'
)
const waitingEntryLatestPrice = (position) => {
  const price = Number(position?.plan?.latestPrice)
  return Number.isFinite(price) && price > 0 ? price : null
}
const waitingEntryPriceGap = (position) => {
  const latestPrice = waitingEntryLatestPrice(position)
  const entryPrice = Number(position?.costPrice)
  return latestPrice !== null && Number.isFinite(entryPrice) && entryPrice > 0 ? latestPrice - entryPrice : null
}
const waitingEntryGapPercent = (position) => {
  const gap = waitingEntryPriceGap(position)
  const entryPrice = Number(position?.costPrice)
  return gap !== null && Number.isFinite(entryPrice) && entryPrice > 0 ? gap / entryPrice * 100 : null
}
const formatWaitingEntryGap = (position) => {
  const gap = waitingEntryPriceGap(position)
  return gap === null ? '--' : formatSignedNumber(gap)
}
const formatWaitingEntryGapPercent = (position) => {
  const percent = waitingEntryGapPercent(position)
  return percent === null ? '--' : formatSignedPercent(percent)
}
const hasConfirmedLivePosition = (position) => (
  position?.executionStatus === 'EXECUTING' && position?.plan?.livePositionConfirmed === true
)
const hasFrozenStopMetrics = (position) => (
  position?.executionStatus === 'STOPPED'
  && position?.plan?.frozenPnl !== null
  && position?.plan?.frozenPnl !== undefined
)
const hasDisplayedPositionMetrics = (position) => (
  hasConfirmedLivePosition(position) || hasFrozenStopMetrics(position)
)
const executionTabPnl = (position) => {
  if (!hasDisplayedPositionMetrics(position)) return null
  const rawValue = position?.executionStatus === 'STOPPED'
    ? (position?.unrealizedPnl ?? position?.plan?.frozenPnl)
    : executionUnrealizedPnlFor(position)
  const value = Number(rawValue)
  return Number.isFinite(value) ? value : null
}
const formatExecutionTabPnl = (position) => {
  const pnl = executionTabPnl(position)
  return pnl === null ? '--' : formatSignedMonitoringPnl(pnl)
}
const formatExecutionTabPnlPercent = (position) => {
  const pnl = executionTabPnl(position)
  return pnl === null ? '--' : formatSignedPercent(executionUnrealizedPnlPercentFor(position))
}
// 标签卡宽度固定：合约名按长度缩小字号而不是省略号截断。
// 标签卡宽度固定：渲染后实测合约名是否溢出，逐步缩小字号直到放下（不挤占多空标签空间）。
const fitExecutionTabSymbols = () => {
  document.querySelectorAll('.binance-execution-tab').forEach((tabEl) => {
    const head = tabEl.querySelector('.binance-execution-tab-head')
    const wrap = tabEl.querySelector('.binance-execution-tab-symbol')
    const strong = wrap?.querySelector('strong')
    const em = head?.querySelector('em')
    if (!head || !wrap || !strong) return
    strong.style.fontSize = ''
    // 可用宽度 = 头行容器宽 - 多空芯片宽 - 间距，避免以溢出后的布局宽度自证。
    const emWidth = em ? em.offsetWidth : 0
    const available = head.clientWidth - emWidth - 8
    if (available <= 20) return
    let size = 15
    strong.style.fontSize = `${size}px`
    let guard = 0
    while (strong.scrollWidth > available && size > 9 && guard < 24) {
      size -= 0.5
      strong.style.fontSize = `${size}px`
      guard++
    }
  })
}
let fitExecutionTabRaf = 0
const scheduleFitExecutionTabSymbols = () => {
  if (fitExecutionTabRaf) return
  fitExecutionTabRaf = requestAnimationFrame(() => {
    fitExecutionTabRaf = 0
    nextTick(fitExecutionTabSymbols)
  })
}
watch(simulatedPositions, () => nextTick(scheduleFitExecutionTabSymbols), { deep: true })
const isWaitingForLivePosition = (position) => (
  positionExecutionStatus(position) !== 'STOPPED' && !hasDisplayedPositionMetrics(position)
)
const monitoringStageLabel = (position) => {
  if (isPendingEntry(position)) return '挂单中'
  if (positionExecutionStatus(position) === 'STOPPED') return '已止损'
  if (hasConfirmedLivePosition(position)) return '监控中'
  return '挂单中'
}
const monitoringStageClass = (position) => {
  if (isPendingEntry(position)) return 'pending_entry'
  if (positionExecutionStatus(position) === 'STOPPED') return 'stopped'
  return hasConfirmedLivePosition(position) ? 'executing' : 'pending_entry'
}
const monitoringWaitingLabel = (position) => {
  if (isPendingEntry(position)) return '等待限价单成交'
  if (asNumber(position?.plan?.livePositionMissingChecks) > 0) return '正在复核持仓状态'
  return '等待账户确认'
}
const canRestoreStoppedMonitor = (position) => (
  position?.executionStatus === 'STOPPED'
  && connected.value
  && !accountSnapshotStale.value
  && Boolean(actualFuturesPositionFor(position))
)
const isMovingStopEffective = (plan) => plan?.activeStopSource
  ? plan.activeStopSource === 'MOVING'
  : Boolean(plan?.movingStopActive)
const isStructureStopEffective = (plan) => plan?.activeStopSource
  ? plan.activeStopSource === 'STRUCTURE'
  : !plan?.movingStopActive
const isPositionRiskStopEffective = (plan) => plan?.activeStopSource === 'POSITION_RISK'

const executionTargetSnapshot = (plan, role) => {
  const key = String(role || '').trim().toUpperCase()
  if (!key) return null
  const snapshots = plan?.targetPnlSnapshot || plan?.executionPlan?.targetPnlSnapshot
  const level = snapshots && typeof snapshots === 'object' ? snapshots.levels?.[key] : null
  return level && typeof level === 'object' ? level : null
}
const executionLevelPnl = (plan, side, levelPrice, role = null) => {
  const frozen = executionTargetSnapshot(plan, role)
  const frozenPnl = Number(frozen?.pnl)
  if (Number.isFinite(frozenPnl)) return frozenPnl
  const position = selectedExecutionPosition.value
  const costPrice = asNumber(position?.costPrice)
  const quantity = asNumber(position?.quantity)
  const price = asNumber(levelPrice)
  if (costPrice <= 0 || quantity <= 0 || price <= 0) return null
  const pnl = (side === 'SHORT' ? costPrice - price : price - costPrice) * quantity
  return pnl
}
const stopTriggerPnl = (plan, side, stopPrice) => executionLevelPnl(plan, side, stopPrice)
const executionTargetPnl = (plan, side, targetPrice, role) => executionLevelPnl(plan, side, targetPrice, role)
// 持仓计划监控的止盈位列表：近端/第一/第二，未设置的档位不出现，列数随数量变化。
const executionTpTargets = (position) => {
  const plan = position?.plan
  if (!plan) return []
  const targets = []
  const push = (key, label, price, ratio, role) => {
    if (asNumber(price) <= 0) return
    targets.push({ key, label, price, ratio, role, pnl: executionTargetPnl(plan, position.side, price, role) })
  }
  push('protective', '近端止盈', planTarget(plan, 'PROTECTIVE_TARGET', -1)?.price, formatTargetPercent(planTargetRatio(plan, 'protectiveTakeProfitRatio', configuredProtectiveTakeProfitRatio)), 'PROTECTIVE_TARGET')
  push('first', '第一止盈', planTarget(plan, 'FIRST_TARGET', 0)?.price, formatTargetPercent(planTargetRatio(plan, 'firstTakeProfitRatio', configuredFirstTakeProfitRatio)), 'FIRST_TARGET')
  if (planHasExtensionTarget(plan)) push('second', '第二止盈', planTarget(plan, 'EXTENSION_TARGET', 1)?.price, formatTargetPercent(planTargetRatio(plan, 'secondTakeProfitRatio', configuredSecondTakeProfitRatio)), 'EXTENSION_TARGET')
  return targets
}
const executionPlanSupportingReasons = (plan) => {
  const managementNote = String(plan?.managementNote || '').trim()
  const result = []
  const add = (value, muted = false) => {
    const text = String(value || '').trim()
    if (!text || text === managementNote || result.some((item) => item.text === text)) return
    result.push({ text, muted })
  }
  for (const reason of plan?.reasons || []) add(reason)
  for (const condition of plan?.missingConditions || []) add(condition, true)
  return result.slice(0, 2)
}
const stopTriggerResultLabel = (plan, side, stopPrice) => {
  const pnl = stopTriggerPnl(plan, side, stopPrice)
  if (pnl === null) return '--'
  return formatSignedMonitoringPnl(pnl)
}

const executionStopLevels = (plan, side) => [
  {
    key: 'STRUCTURE',
    label: '结构止损',
    price: plan?.initialStop,
    active: isStructureStopEffective(plan),
    activeClass: 'is-structure',
    pnl: stopTriggerPnl(plan, side, plan?.initialStop),
    description: stopTriggerResultLabel(plan, side, plan?.initialStop),
  },
  {
    key: 'MOVING',
    label: '移动止损',
    price: isMovingStopEffective(plan) ? plan?.activeStop : plan?.movingStopCandidate,
    active: isMovingStopEffective(plan),
    activeClass: 'is-moving',
    pnl: isMovingStopEffective(plan) ? stopTriggerPnl(plan, side, plan?.activeStop) : null,
    description: isMovingStopEffective(plan)
      ? stopTriggerResultLabel(plan, side, plan?.activeStop)
      : '--',
  },
  {
    key: 'POSITION_RISK',
    label: '持仓风险止损',
    price: plan?.positionRiskStop,
    active: isPositionRiskStopEffective(plan),
    activeClass: 'is-risk',
    pnl: stopTriggerPnl(plan, side, plan?.positionRiskStop),
    description: stopTriggerResultLabel(plan, side, plan?.positionRiskStop),
  },
]
const selectedExecutionStopLevels = computed(() => executionStopLevels(
  selectedExecutionPosition.value?.plan,
  selectedExecutionPosition.value?.side,
))
const currentExecutionStop = computed(() => (
  selectedExecutionStopLevels.value.find((stop) => stop.active)
  || selectedExecutionStopLevels.value[0]
  || null
))
const remainingExecutionStops = computed(() => selectedExecutionStopLevels.value.filter(
  (stop) => stop.key !== currentExecutionStop.value?.key,
))

const buildAnalysisChartLevels = (plan) => {
  if (!plan) return []
  const protectiveTarget = planTarget(plan, 'PROTECTIVE_TARGET', -1)
  const firstTarget = planTarget(plan, 'FIRST_TARGET', 0)
  const extensionTarget = planTarget(plan, 'EXTENSION_TARGET', 1)
  const level = (key, price, label, color, dash = [], width = 2) => ({ key, price: asNumber(price), label, color, dash, width })
  const activeStop = asNumber(plan.activeStop) || asNumber(plan.stopLoss)
  const activeStopLabel = plan.activeStopSource === 'MOVING' ? '移动止损' : '当前计划止损'
  return [
    level('current-stop', activeStop, activeStopLabel, 'var(--binance-down)', [3, 3]),
    level('target-protection', protectiveTarget?.price, protectiveTarget?.label || '近端保护目标', 'var(--binance-gold)', [2, 4], 1.8),
    level('target-one', firstTarget?.price, firstTarget?.label || '第一止盈', 'var(--binance-up)'),
    level('target-two', extensionTarget?.price, extensionTarget?.label || '第二目标', '#6d28d9', [8, 3]),
  ].filter((item) => item.price > 0)
}

const executionChartPlan = computed(() => {
  const position = selectedExecutionPosition.value
  const plan = position?.plan
  if (!position || !plan) return null
  const isLong = position.side !== 'SHORT'
  const sourcePlan = plan.executionPlan || {}
  const sourceEntry = sourcePlan.entry || plan.entry || {}
  const sourceEntryTiming = sourcePlan.entryTiming || plan.entryTiming || {}
  const targetPlan = {
    ...sourcePlan,
    targetProtection: plan.targetProtection || sourcePlan.targetProtection,
    targetOne: plan.targetOne || sourcePlan.targetOne,
    targetTwo: plan.targetTwo || sourcePlan.targetTwo,
    takeProfits: Array.isArray(sourcePlan.takeProfits) && sourcePlan.takeProfits.length
      ? sourcePlan.takeProfits
      : (Array.isArray(plan.takeProfits) ? plan.takeProfits : []),
  }
  const protectiveTarget = planTarget(targetPlan, 'PROTECTIVE_TARGET', -1)
  const firstTarget = planTarget(targetPlan, 'FIRST_TARGET', 0)
  const extensionTarget = planTarget(targetPlan, 'EXTENSION_TARGET', 1)
  const level = (key, price, label, color, dash = [], width = 2) => ({ key, price: asNumber(price), label, color, dash, width })
  const activeStop = asNumber(plan.activeStop) || asNumber(plan.initialStop) || asNumber(sourcePlan.stopLoss)
  const activeStopLabel = plan.activeStopSource === 'MOVING' ? '移动止损' : '当前计划止损'
  const chartLevels = [
    level('cost', position.costPrice, '实际成本', 'var(--text-primary)', [], 2.2),
    level('current-stop', activeStop, activeStopLabel, 'var(--binance-down)', [], 2.4),
    level('target-protection', protectiveTarget?.price, protectiveTarget?.label || '近端保护目标', 'var(--binance-gold)', [2, 4], 1.8),
    level('target-one', firstTarget?.price, firstTarget?.label || '第一止盈', 'var(--binance-up)', [], 2),
    level('target-two', extensionTarget?.price, extensionTarget?.label || '第二目标', '#6d28d9', [8, 3], 2),
  ].filter((item) => item.price > 0)
  return {
    ...plan,
    direction: isLong ? 'LONG' : 'SHORT',
    entry: sourceEntry,
    entryTiming: sourceEntryTiming,
    chartLevels,
    currentPrice: executionMarkPriceFor(position),
    symbol: position.symbol,
  }
})

const chartPlan = computed(() => {
  if (selectedExecutionPosition.value?.plan && selectedExecutionPosition.value.symbol === selectedMarketSymbol.value) {
    return executionChartPlan.value
  }
  const plan = activeAnalysisPlan.value
  if (!plan || plan.symbol !== selectedMarketSymbol.value) return null
  return {
    ...plan,
    chartLevels: buildAnalysisChartLevels(plan),
  }
})

const marketErrorMessage = (error) => error?.response?.data?.message || error?.message || '行情暂时不可用'
const isExchangeConnectionError = (message) => {
  const text = String(message || '').trim()
  return text.includes('无法连接交易所') || text.includes('请检查网络或接口地址')
}
const showMarketAnalysisFailure = async (rawMessage) => {
  const message = String(rawMessage || '').trim() || '合约扫描失败'
  if (isExchangeConnectionError(message)) {
    try {
      await ElMessageBox.alert(
        `${message}。寻找目标通过 REST 经本机 10808 代理拉取历史 K 线；请确认代理程序和 10808 端口可用。连接方式设置只影响账户与实时行情快照，不会改用 WebSocket 拉历史。`,
        '无法连接交易所',
        { type: 'error', confirmButtonText: '知道了' },
      )
    } catch {
      // Dialog dismissals are intentional; keep the analysis failure path quiet.
    }
    return
  }
  ElMessage.error(message)
}
const binanceConnectionNotice = computed(() => {
  const messages = [snapshotConnectionError.value, activeMarketsError.value, marketError.value]
    .map((message) => String(message || '').trim())
    .filter(Boolean)
  return [...new Set(messages)].join('；')
})

const normalizeConnectionMode = (value) => String(value || '').trim().toUpperCase() === 'WEBSOCKET' ? 'WEBSOCKET' : 'REST'
const normalizeConnectionSettings = (value) => {
  const source = value && typeof value === 'object' ? value : {}
  return {
    connectionMode: normalizeConnectionMode(source.connectionMode || source.mode),
    updatedAt: source.updatedAt || null,
  }
}
const connectionLineLabel = (line) => {
  const status = String(line?.status || '').trim().toUpperCase()
  return {
    REST: 'REST 轮询',
    CONNECTED: '已连接',
    CONNECTING: '握手中',
    AUTHENTICATING: '验证账户中',
    RECONNECTING: '重新连接中',
    ERROR: '连接异常',
    WAITING_CREDENTIALS: '等待账户配置',
    IDLE: '等待订阅目标',
    STOPPED: '已停止',
  }[status] || (line?.connected ? '已连接' : '等待状态')
}
const connectionLineClass = (line) => {
  const status = String(line?.status || '').trim().toUpperCase()
  if (status === 'REST') return 'is-rest'
  if (line?.connected || status === 'CONNECTED') return 'is-connected'
  if (status === 'ERROR') return 'is-error'
  return 'is-pending'
}
const connectionLineDetail = (line, label) => {
  if (!line) return `${label}状态等待后端快照`
  const subscriptions = line.subscriptions == null ? '' : ` · ${line.subscriptions} 路订阅`
  const error = String(line.error || '').trim()
  if (error) return `${error}${subscriptions}`
  if (line.lastMessageAt) return `最近数据 ${formatTime(line.lastMessageAt)}${subscriptions}`
  return `${label}由后端持续维护${subscriptions}`
}

const emptyCredentialsProfile = () => ({ configured: false, network: 'mainnet', apiKeyMasked: '', updatedAt: null, requiresReconfiguration: false, connectionMode: 'REST' })

const stableExecutionPositions = (items) => {
  const previousIndex = new Map(simulatedPositions.value.map((item, index) => [item.id, index]))
  const createdAtValue = (item) => {
    const parsed = Date.parse(String(item?.createdAt || ''))
    return Number.isFinite(parsed) ? parsed : Number(item?.id || 0)
  }
  return [...items].sort((left, right) => {
    const leftIndex = previousIndex.get(left.id)
    const rightIndex = previousIndex.get(right.id)
    if (leftIndex !== undefined && rightIndex !== undefined) return leftIndex - rightIndex
    if (leftIndex !== undefined) return -1
    if (rightIndex !== undefined) return 1
    return createdAtValue(left) - createdAtValue(right) || Number(left.id || 0) - Number(right.id || 0)
  })
}

const keepStaleCollection = (incoming, previous, stale) => (
  stale && incoming.length === 0 && previous.length > 0 ? previous : incoming
)

const hasConfirmedLiveSnapshot = (position) => (
  position?.executionStatus === 'STOPPED'
  || position?.plan?.livePositionConfirmed === true
  || Boolean(position?.plan?.livePositionConfirmedAt)
)

const mergeSimulatedPositionSnapshot = (incoming, previous) => {
  if (!previous || !incoming) return incoming || previous
  const preserveLiveMetrics = hasConfirmedLiveSnapshot(incoming)
  const merged = { ...previous, ...incoming }
  const incomingPlan = incoming.plan && typeof incoming.plan === 'object' ? incoming.plan : null
  const previousPlan = previous.plan && typeof previous.plan === 'object' ? previous.plan : null
  if (incomingPlan) {
    merged.plan = { ...(preserveLiveMetrics ? previousPlan || {} : {}), ...incomingPlan }
    if (preserveLiveMetrics && previousPlan) {
      for (const key of [
        'currentPrice',
        'latestPrice',
        'rMultiple',
        'favorableRMultiple',
        'favorableExtreme',
        'movingStopCandidate',
        'movingStop',
        'activeStop',
        'activeStopLoss',
        'movingStopActivationPrice',
        'movingStopActivationAt',
      ]) {
        if (merged.plan[key] === null || merged.plan[key] === undefined) merged.plan[key] = previousPlan[key]
      }
    }
  } else if (preserveLiveMetrics && previousPlan) {
    merged.plan = previousPlan
  }
  if (preserveLiveMetrics) {
    for (const key of ['currentPrice', 'unrealizedPnl', 'unrealizedPnlPercent', 'lastPrice', 'lastUnrealizedPnl', 'lastUnrealizedPnlPercent']) {
      if (merged[key] === null || merged[key] === undefined) merged[key] = previous[key]
    }
  }
  return merged
}

const mergeSimulatedPositionCollection = (incoming, previous, unavailable) => {
  const previousById = new Map(previous.map((item) => [item.id, item]))
  const incomingIds = new Set(incoming.map((item) => item.id))
  const merged = incoming.map((item) => mergeSimulatedPositionSnapshot(item, previousById.get(item.id)))
  if (unavailable) {
    for (const item of previous) {
      if (!incomingIds.has(item.id)) merged.push(item)
    }
  }
  return merged
}

const applyBinanceSnapshot = (snapshot) => {
  if (!snapshot || typeof snapshot !== 'object') return
  if (snapshot.connection) {
    connectionStatus.value = snapshot.connection
    if (!connectionSettingsLoaded.value && props.currentUser) {
      const incomingMode = snapshot.connection.connectionMode || snapshot.connection.mode || snapshot.account?.credentials?.connectionMode
      connectionSettings.value = normalizeConnectionSettings({ connectionMode: incomingMode })
    }
  }
  const futures = snapshot.markets?.futures || {}
  const incomingFuturesMarkets = Array.isArray(futures.items) ? futures.items : []
  futuresMarkets.value = keepStaleCollection(incomingFuturesMarkets, futuresMarkets.value, Boolean(futures.stale))
  futuresLoading.value = !futures.updatedAt && !futures.error
  futuresError.value = futures.error || ''
  futuresStale.value = Boolean(futures.stale)
  futuresUpdatedAtValue.value = futures.updatedAt || null

  const selected = snapshot.selected || {}
  if (!selectedMarketSymbol.value) {
    selectedTicker.value = null
    klines.value = []
    marketLoading.value = false
    marketStale.value = false
    marketError.value = ''
  }
  if (selected.marketType === 'FUTURES' && selected.symbol === selectedMarketSymbol.value && selected.interval === interval.value) {
    const incomingKlines = Array.isArray(selected.klines) ? selected.klines : []
    if (selected.ticker || !selected.stale || !selectedTicker.value) selectedTicker.value = selected.ticker || null
    klines.value = keepStaleCollection(incomingKlines, klines.value, Boolean(selected.stale))
    marketStale.value = Boolean(selected.stale)
    marketError.value = selected.error || ''
    marketLoading.value = false
    if (incomingKlines.length && !selected.stale) marketDataReadyKey.value = chartMarketKey.value
  }

  const accountSnapshot = snapshot.account
  if (accountSnapshot) {
    accountSnapshotUpdatedAt.value = accountSnapshot.updatedAt || null
    accountSnapshotCheckedAt.value = accountSnapshot.checkedAt || null
    accountSnapshotStale.value = Boolean(accountSnapshot.stale)
    accountSnapshotError.value = accountSnapshot.error || ''
    accountPositionPnlHistory.value = Array.isArray(accountSnapshot.positionPnlHistory) ? accountSnapshot.positionPnlHistory : []
    credentialsProfile.value = accountSnapshot.credentials || emptyCredentialsProfile()
    if (accountSnapshot.account) {
      account.value = accountSnapshot.account
      connected.value = true
    } else if (!accountSnapshot.stale) {
      account.value = null
      connected.value = false
    } // A first failed refresh has no payload; retain the previous account view.
    connectionLoading.value = false
  } else if (!props.currentUser) {
    accountSnapshotUpdatedAt.value = null
    accountSnapshotCheckedAt.value = null
    accountSnapshotStale.value = false
    accountSnapshotError.value = ''
    accountPositionPnlHistory.value = []
  } else {
    // A snapshot without an account belongs to a user with no ready cache;
    // never keep showing the previous user's or previous connection's data.
    account.value = null
    connected.value = false
    accountSnapshotUpdatedAt.value = null
    accountSnapshotCheckedAt.value = null
    accountSnapshotStale.value = false
    accountSnapshotError.value = ''
    accountPositionPnlHistory.value = []
    credentialsProfile.value = emptyCredentialsProfile()
    connectionLoading.value = false
  }

  const simulatedSnapshot = snapshot.simulatedPositions || {}
  if (Array.isArray(simulatedSnapshot.items)) {
    const incomingPositions = simulatedSnapshot.items.filter((position) => position?.marketMode === 'FUTURES')
    const simulatedDataUnavailable = Boolean(simulatedSnapshot.stale || simulatedSnapshot.error || !simulatedSnapshot.updatedAt)
    simulatedPositions.value = stableExecutionPositions(
      mergeSimulatedPositionCollection(incomingPositions, simulatedPositions.value, simulatedDataUnavailable),
    )
    simulatedPositionsLoading.value = false
    simulatedPositionsError.value = simulatedSnapshot.error || ''
    const selectedStillExists = simulatedPositions.value.some((position) => position.id === selectedExecutionPositionId.value)
    if (!selectedStillExists) selectedExecutionPositionId.value = simulatedPositions.value[0]?.id || null
  }
  const copySnapshot = snapshot.copyTrading || {}
  const selectedTraderId = String(copyTradingSettings.value.topTraderId || copySnapshot.topTraderId || '').trim()
  const selectedTraderSnapshot = Array.isArray(copySnapshot.traderSnapshots)
    ? copySnapshot.traderSnapshots.find((item) => String(item?.topTraderId || '').trim() === selectedTraderId)
    : null
  // The backend refreshes every subscribed trader into one cache. Switching
  // the selected trader only projects this cache locally; it never refetches.
  const snapshotTraderId = String(copySnapshot.topTraderId || '').trim()
  const canUseSingleTraderFallback = (
    (!Array.isArray(copySnapshot.traderSnapshots) || copySnapshot.traderSnapshots.length === 0)
    && (!snapshotTraderId || snapshotTraderId === selectedTraderId)
  )
  const selectedCopySnapshot = selectedTraderSnapshot || (canUseSingleTraderFallback ? copySnapshot : null)
  const snapshotMargin = selectedCopySnapshot?.sourceTotalMargin
    ?? selectedCopySnapshot?.umMarginBalance
    ?? selectedCopySnapshot?.profile?.umMarginBalance
  if (snapshotMargin != null) copyTradingSourceMarginValue.value = asNumber(snapshotMargin)
  if (Array.isArray(selectedCopySnapshot?.items)) {
    copyTradingRecords.value = selectedCopySnapshot.items
    copyTradingLoading.value = false
    copyTradingUpdatedAt.value = copySnapshot.updatedAt || null
    copyTradingError.value = selectedCopySnapshot.error || ''
  }
  if (Array.isArray(selectedCopySnapshot?.positions)) {
    copyTradingPositions.value = selectedCopySnapshot.positions
  }
  if (Array.isArray(copySnapshot.follows)) {
    copyTradingFollows.value = copySnapshot.follows
  }
  if (Array.isArray(copySnapshot.followedPositionSnapshots)) {
    copyTradingFollowedPositionSnapshots.value = copySnapshot.followedPositionSnapshots
  }
  if (Array.isArray(copySnapshot.subscriptions)) {
    copyTradingSubscriptions.value = copySnapshot.subscriptions
    ensureSelectedCopyTradingSubscription()
  }
}

const stopSnapshotSubscription = () => {
  snapshotSubscriptionRequestId += 1
  if (snapshotPollTimer) window.clearInterval(snapshotPollTimer)
  snapshotPollTimer = null
}

const startSnapshotSubscription = () => {
  if (typeof window === 'undefined') return
  stopSnapshotSubscription()
  const activeRequestId = ++snapshotSubscriptionRequestId
  snapshotConnectionState.value = 'connecting'
  snapshotConnectionError.value = ''
  marketLoading.value = Boolean(selectedMarketSymbol.value)
  if (selectedMarketSymbol.value) marketDataReadyKey.value = ''
  futuresLoading.value = true
  simulatedPositionsLoading.value = Boolean(props.currentUser)
  if (!snapshotClientId) snapshotClientId = `binance-${window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`}`
  let requestInFlight = false
  const pollSnapshot = async () => {
    if (activeRequestId !== snapshotSubscriptionRequestId || requestInFlight) return
    requestInFlight = true
    try {
      const snapshot = await fetchBinanceFuturesSnapshot({
        network: network.value,
        symbol: selectedMarketSymbol.value,
        interval: interval.value,
        clientId: snapshotClientId,
      })
      if (activeRequestId !== snapshotSubscriptionRequestId) return
      snapshotConnectionState.value = 'connected'
      snapshotConnectionError.value = ''
      applyBinanceSnapshot(snapshot)
    } catch (error) {
      if (activeRequestId !== snapshotSubscriptionRequestId) return
      snapshotConnectionState.value = 'error'
      snapshotConnectionError.value = marketErrorMessage(error)
      marketLoading.value = false
      // A polling failure is only a status message. Do not replace the last
      // successful market/price data with an error-shaped empty state.
      if (futuresMarkets.value.length) futuresStale.value = true
      if (selectedMarketSymbol.value) marketStale.value = true
      if (account.value) accountSnapshotStale.value = true
      simulatedPositionsLoading.value = false
    } finally {
      requestInFlight = false
    }
  }
  pollSnapshot()
  snapshotPollTimer = window.setInterval(pollSnapshot, 1000)
}

const loadSimulatedPositions = () => startSnapshotSubscription()

const openLivePositionMonitorPreview = async (position) => {
  if (!props.currentUser) {
    emit('login-request')
    return
  }
  if (realPositionMonitorFor(position)) return
  const key = futuresPositionMonitorKey(position)
  monitorPreviewLoadingKey.value = key
  livePositionMonitorPreview.value = null
  try {
    const preview = await previewBinanceFuturesPositionMonitor({
      symbol: position.symbol,
      side: position.side,
    })
    if (!preview?.plan || !preview?.position) throw new Error('监控计划预览格式无效')
    livePositionMonitorPreview.value = preview
    livePositionMonitorPreviewVisible.value = true
  } catch (error) {
    ElMessage.error(`生成持仓监控计划失败：${marketErrorMessage(error)}`)
  } finally {
    monitorPreviewLoadingKey.value = ''
  }
}

const confirmLivePositionMonitor = async () => {
  const preview = livePositionMonitorPreview.value
  if (!preview?.plan || !preview?.position || livePositionMonitorSaving.value) return
  const plan = preview.plan
  const position = preview.position
  livePositionMonitorSaving.value = true
  try {
    await createBinanceSimulatedPosition({
      network: 'mainnet',
      marketMode: 'FUTURES',
      symbol: position.symbol,
      side: position.side,
      quantity: asNumber(position.quantity),
      costPrice: asNumber(position.costPrice),
      leverage: asNumber(position.leverage) || 1,
      plan,
      hasExistingPosition: true,
      allowMonitoringPlan: true,
      existingPositionMonitor: true,
      allowTrial: plan.trialEligible === true,
      submitRealLimitOrder: false,
      note: '由真实合约持仓创建的监控计划',
    })
    livePositionMonitorPreviewVisible.value = false
    livePositionMonitorPreview.value = null
    await pollSnapshot()
    ElMessage.success('真实持仓已加入监控，止盈、止损和移动止损将由后台自动同步')
  } catch (error) {
    ElMessage.error(`添加持仓监控失败：${marketErrorMessage(error)}`)
  } finally {
    livePositionMonitorSaving.value = false
  }
}

const openPositionDialog = (position = null) => {
  if (!props.currentUser) return
  executionPlan.value = null
  positionRiskAdviceExpanded.value = false
  editingPositionId.value = position?.id || null
  positionForm.value = position
    ? {
        network: 'mainnet',
        marketMode: 'FUTURES',
        symbol: position.symbol,
        side: position.side,
        quantity: asNumber(position.quantity),
        quantityMode: 'QUANTITY',
        marginRatio: 50,
        costPrice: asNumber(position.costPrice),
        leverage: asNumber(position.leverage) || 1,
         protectiveTakeProfitRatio: normalizedPlanProtectionRatios(position.plan?.executionPlan || position.plan).protective,
         firstTakeProfitRatio: normalizedPlanProtectionRatios(position.plan?.executionPlan || position.plan).first,
         secondTakeProfitRatio: normalizedPlanProtectionRatios(position.plan?.executionPlan || position.plan).second,
        submitRealLimitOrder: false,
        allowTrial: false,
        note: position.note || '',
      }
    : {
        network: network.value,
        marketMode: 'FUTURES',
        symbol: selectedMarketSymbol.value || 'BTCUSDT',
        side: 'LONG',
        quantity: 0.001,
        quantityMode: 'MARGIN_RATIO',
        marginRatio: 50,
        costPrice: asNumber(ticker.value?.lastPrice),
        leverage: 10,
        protectiveTakeProfitRatio: configuredProtectiveTakeProfitRatio.value,
        firstTakeProfitRatio: configuredFirstTakeProfitRatio.value,
        secondTakeProfitRatio: configuredSecondTakeProfitRatio.value,
        submitRealLimitOrder: false,
        allowTrial: false,
        note: '',
      }
  syncPositionSide()
  positionDialogVisible.value = true
}

const syncPositionSide = () => {
  positionForm.value.marketMode = 'FUTURES'
  if (executionPlan.value) {
    positionForm.value.symbol = executionPlan.value.symbol
    positionForm.value.side = executionPlan.value.direction === 'SHORT' ? 'SHORT' : 'LONG'
  }
}

const startAnalysisPlanExecution = async () => {
  if (!props.currentUser) {
    ElMessage.warning('请先登录后执行分析计划')
    return
  }
  const plan = activeAnalysisPlan.value
  if (!isPlanExecutable(plan)) {
    ElMessage.warning('当前分析计划条件尚未完整，暂不能执行')
    return
  }
  const monitoringOnly = isMonitoringAnalysisPlan(plan)
  if (!strategySettingsLoaded.value) {
    const settings = await loadStrategySettings()
    if (!settings) return
  }
  editingPositionId.value = null
  if (monitoringOnly) {
    try {
      await ElMessageBox.confirm(
        `该标的当前条件完整度为 ${plan.conditionMet ?? '--'}/${plan.conditionTotal ?? '--'}，未达到“寻找目标”的执行筛选。将保存触发、止损与止盈点位用于监控，不会提交真实入场单。`,
        '确认创建监控计划',
        { type: 'warning', confirmButtonText: '创建监控', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  } else if (plan.trialEligible) {
    try {
      await ElMessageBox.confirm(
        `该计划当前条件完整度为 ${plan.conditionMet ?? '--'}/${plan.conditionTotal ?? '--'}，只差一个非关键条件。试错执行仍可能造成损失，请确认你接受该风险。`,
        '确认试错执行',
        { type: 'warning', confirmButtonText: '确认试错', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  executionPlan.value = JSON.parse(JSON.stringify(plan))
  positionRiskAdviceExpanded.value = false
  positionForm.value = {
    network: network.value,
    marketMode: 'FUTURES',
    symbol: plan.symbol,
    side: plan.direction === 'SHORT' ? 'SHORT' : 'LONG',
    quantity: 0.001,
    quantityMode: 'MARGIN_RATIO',
    marginRatio: 50,
    costPrice: planDefaultEntryPrice(plan),
    leverage: 1,
     protectiveTakeProfitRatio: normalizedPlanProtectionRatios(plan).protective,
     firstTakeProfitRatio: normalizedPlanProtectionRatios(plan).first,
     secondTakeProfitRatio: normalizedPlanProtectionRatios(plan).second,
    submitRealLimitOrder: false,
    allowTrial: plan.trialEligible === true,
    allowMonitoringPlan: monitoringOnly,
    note: '',
  }
  positionDialogVisible.value = true
}

const savePosition = async () => {
  if (!props.currentUser) return
  const payload = { ...positionForm.value, marketMode: 'FUTURES', symbol: String(positionForm.value.symbol || '').trim().toUpperCase() }
  if (executionPlanIsMonitoringOnly.value) {
    payload.submitRealLimitOrder = false
    payload.allowMonitoringPlan = true
  }
  payload.hasExistingPosition = payload.submitRealLimitOrder !== true
  if (payload.quantityMode === 'MARGIN_RATIO') {
    payload.quantity = normalizedQuantityFromMarginRatio.value
    if (!payload.quantity) {
      ElMessage.warning('按保证金比例换算后的数量低于交易所最小单位，或缺少有效账户、成本价')
      return
    }
  } else if (!editingPositionId.value) {
    payload.quantity = normalizeQuantityForOrder(payload.quantity, payload.symbol, 'LIMIT')
    if (!payload.quantity) {
      ElMessage.warning('持仓数量低于交易所最小单位，无法创建计划')
      return
    }
  }
  positionForm.value.quantity = payload.quantity
  if (!payload.symbol || !asNumber(payload.quantity) || !asNumber(payload.costPrice)) {
    ElMessage.warning('请完整填写交易对、持仓数量和持仓成本')
    return
  }
  if (positionLossAdvice.value.available && positionLossAdvice.value.exceedsSuggestedRisk) {
    ElMessage.warning(`风险提示：到达止损预计亏损 ${formatAssetAmount(positionLossAdvice.value.estimatedLoss)}，约占总保证金 ${formatRatioPercent(positionLossAdvice.value.accountLossRatio)}；仍可继续保存。`)
  } else if (positionRiskAdvice.value.available && asNumber(payload.quantity) > positionRiskAdvice.value.maxQuantity * 1.000000001) {
    ElMessage.warning(`当前数量超过风险建议上限 ${formatCrypto(positionRiskAdvice.value.maxQuantity)}，仅作风险提示，仍可继续保存。`)
  }
  if (!editingPositionId.value && !executionPlan.value) {
    ElMessage.warning('必须从符合条件的分析计划创建持仓计划监控')
    return
  }
  const sourcePlan = executionPlan.value
    ? { ...executionPlan.value }
    : null
  const planContext = sourcePlan || selectedExecutionPosition.value?.plan?.executionPlan || selectedExecutionPosition.value?.plan || null
  const normalizedRatios = normalizedPlanProtectionRatios(planContext)
  const hasProtectiveTarget = normalizedRatios.hasProtective
  const protectiveTakeProfitRatio = hasProtectiveTarget
    ? Number(payload.protectiveTakeProfitRatio || configuredProtectiveTakeProfitRatio.value)
    : 0
  const firstTakeProfitRatio = Number(payload.firstTakeProfitRatio || configuredFirstTakeProfitRatio.value)
  const secondTakeProfitRatio = Number(payload.secondTakeProfitRatio || configuredSecondTakeProfitRatio.value)
  const extensionTargetRecord = planTarget(planContext, 'EXTENSION_TARGET', 1)
  const hasExtensionTarget = Boolean(asNumber(extensionTargetRecord?.price)) && extensionTargetRecord?.available !== false
  const effectiveFirstTakeProfitRatio = hasExtensionTarget ? firstTakeProfitRatio : secondTakeProfitRatio
  const configuredRatiosValid = (!hasProtectiveTarget || (
    Number.isFinite(protectiveTakeProfitRatio)
    && protectiveTakeProfitRatio >= 1
    && protectiveTakeProfitRatio < firstTakeProfitRatio
  ))
    && Number.isFinite(firstTakeProfitRatio)
    && Number.isFinite(secondTakeProfitRatio)
    && (hasExtensionTarget ? firstTakeProfitRatio < secondTakeProfitRatio : firstTakeProfitRatio <= secondTakeProfitRatio)
    && secondTakeProfitRatio < 100
  const effectiveRatiosValid = (!hasProtectiveTarget || protectiveTakeProfitRatio < effectiveFirstTakeProfitRatio)
    && effectiveFirstTakeProfitRatio < 100
    && secondTakeProfitRatio < 100
    && (!hasExtensionTarget || effectiveFirstTakeProfitRatio < secondTakeProfitRatio)
  if (!configuredRatiosValid || !effectiveRatiosValid) {
    ElMessage.warning(hasExtensionTarget ? '三档止盈必须满足近端保护 < 第一档 < 第二档，且第二档小于 100%' : '当前计划的目标比例无效，请重新生成计划')
    return
  }
  if (hasProtectiveTarget) {
    payload.protectiveTakeProfitRatio = protectiveTakeProfitRatio
  } else {
    // MODEL direct plans have no PROTECTIVE_TARGET. Leaving the form's
    // normalized zero in the request makes the limit-plan endpoint validate
    // a non-existent third ladder leg.
    delete payload.protectiveTakeProfitRatio
  }
  payload.firstTakeProfitRatio = effectiveFirstTakeProfitRatio
  payload.secondTakeProfitRatio = secondTakeProfitRatio
  if (sourcePlan) {
    // The persisted strategy-settings schema still requires a valid
    // protective boundary even when this plan has no protective price. Keep a
    // harmless internal boundary below the first target, while omitting the
    // non-existent target itself from execution.
    const persistedProtectiveRatio = hasProtectiveTarget
      ? protectiveTakeProfitRatio
      : Math.max(1, Math.min(25, firstTakeProfitRatio - 0.1))
    sourcePlan.protectiveTakeProfitRatio = persistedProtectiveRatio
    sourcePlan.firstTakeProfitRatio = effectiveFirstTakeProfitRatio
    sourcePlan.secondTakeProfitRatio = secondTakeProfitRatio
    sourcePlan.strategySettings = {
      ...(sourcePlan.strategySettings && typeof sourcePlan.strategySettings === 'object'
        ? sourcePlan.strategySettings
        : {}),
      protectiveTakeProfitRatio: persistedProtectiveRatio,
      // Keep the configured boundary separate from the effective allocation
      // used when an unavailable extension makes the first target final.
      firstTakeProfitRatio,
      secondTakeProfitRatio,
    }
    if (Array.isArray(sourcePlan.takeProfits)) {
      sourcePlan.takeProfits = sourcePlan.takeProfits.map((target) => {
        if (!target || typeof target !== 'object') return target
        const role = String(target.role || '').trim().toUpperCase()
        const cumulativeRatio = role === 'PROTECTIVE_TARGET'
          ? protectiveTakeProfitRatio
          : role === 'FIRST_TARGET'
            ? effectiveFirstTakeProfitRatio
            : role === 'EXTENSION_TARGET'
              ? secondTakeProfitRatio
              : target.cumulativeRatio
        return cumulativeRatio === undefined ? target : { ...target, cumulativeRatio }
      })
    }
  }
  let realOrder = null
  if (payload.submitRealLimitOrder) {
    if (!sourcePlan) {
      ElMessage.warning('真实限价入场只支持从合约分析计划发起')
      return
    }
    const protectiveTakeProfit = asNumber(planTarget(sourcePlan, 'PROTECTIVE_TARGET', -1)?.price) || null
    const firstTakeProfit = asNumber(planTarget(sourcePlan, 'FIRST_TARGET', 0)?.price)
    const extensionTakeProfit = asNumber(planTarget(sourcePlan, 'EXTENSION_TARGET', 1)?.price) || null
    const stopLoss = asNumber(sourcePlan.stopLoss)
    if (!stopLoss || !firstTakeProfit) {
      ElMessage.warning('分析计划缺少止损或第一目标，不能挂真实保护单')
      return
    }
    try {
      await ElMessageBox.confirm(
         `${payload.symbol} 将以 ${formatPrecisePrice(payload.costPrice)} 挂真实 ${payload.side === 'LONG' ? '做多' : '做空'} 限价单，成交后保护配置为：仓位止损 ${formatPrecisePrice(stopLoss)} 全平（标记价条件止损）${protectiveTakeProfit ? `、近端保护目标 ${formatPrecisePrice(protectiveTakeProfit)} 累计 ${formatTargetPercent(protectiveTakeProfitRatio)}（数量型最新价条件止盈）` : ''}、第一目标 ${formatPrecisePrice(firstTakeProfit)} 累计至总仓位 ${formatTargetPercent(effectiveFirstTakeProfitRatio)}（第一目标本档 ${formatTargetPercent(effectiveFirstTakeProfitRatio - protectiveTakeProfitRatio)}，数量型最新价条件止盈）${extensionTakeProfit ? `、第二目标 ${formatPrecisePrice(extensionTakeProfit)} 累计至总仓位 ${formatTargetPercent(secondTakeProfitRatio)}（本档 ${formatTargetPercent(secondTakeProfitRatio - effectiveFirstTakeProfitRatio)}，数量型最新价条件止盈）` : '；未确认第二平台，不设置第二目标'}；最后剩余 ${formatTargetPercent(100 - (extensionTakeProfit ? secondTakeProfitRatio : effectiveFirstTakeProfitRatio))} 由移动止损管理。请确认账户、数量、杠杆和价格无误。`,
        '确认提交真实限价计划',
        { type: 'warning', confirmButtonText: '确认真实下单', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  positionSaving.value = true
  try {
    if (editingPositionId.value) {
      await updateBinanceSimulatedPosition(editingPositionId.value, payload)
    } else {
      payload.plan = sourcePlan
      if (payload.submitRealLimitOrder) {
        realOrder = await placeBinanceFuturesLimitPlanOrder({
          symbol: payload.symbol,
          direction: payload.side,
          quantity: asNumber(payload.quantity),
          costPrice: asNumber(payload.costPrice),
          leverage: asNumber(payload.leverage) || 1,
          stopLoss: asNumber(sourcePlan.stopLoss),
          protectiveTakeProfit: asNumber(planTarget(sourcePlan, 'PROTECTIVE_TARGET', -1)?.price) || null,
          firstTakeProfit: asNumber(planTarget(sourcePlan, 'FIRST_TARGET', 0)?.price),
          extensionTakeProfit: asNumber(planTarget(sourcePlan, 'EXTENSION_TARGET', 1)?.price) || null,
          protectiveTakeProfitRatio,
          firstTakeProfitRatio: effectiveFirstTakeProfitRatio,
          secondTakeProfitRatio,
          plan: sourcePlan,
          allowTrial: payload.allowTrial === true,
        })
        if (!realOrder?.entryAccepted || !realOrder?.entryOrder) {
          throw new Error('入场单提交失败：未收到交易所确认的入场订单')
        }
        if (!realOrder?.monitor?.id) {
          throw new Error('后端未确认计划监控绑定，已停止后续处理，请核对 Binance 委托')
        }
      } else {
        await createBinanceSimulatedPosition(payload)
      }
    }
    positionDialogVisible.value = false
    executionPlan.value = null
    await loadSimulatedPositions()
    ElMessage.success(realOrder
      ? `${realOrder.entryStatus === 'NEW' || realOrder.entryStatus === 'PENDING_NEW' ? '真实限价入场单已提交，等待成交' : '真实限价入场单已确认成交，等待账户同步'}；保护单将在确认真实持仓后自动设置（入场单 ${realOrder.entryOrder?.orderId || '--'}）`
      : (editingPositionId.value ? '持仓计划监控已更新' : '持仓计划监控已创建'))
  } catch (error) {
    ElMessage.error(realOrder ? `真实限价单已提交，但持仓计划监控保存失败：${marketErrorMessage(error)}` : marketErrorMessage(error))
  } finally {
    positionSaving.value = false
  }
}

const removePosition = async (position) => {
  try {
    await ElMessageBox.confirm(
      `确认删除 ${position.symbol} 的持仓计划监控？删除时会取消交易所内同合约、同方向的全部未成交开仓、平仓、止盈和止损委托。`,
      '删除持仓计划监控',
      { type: 'warning', confirmButtonText: '删除并取消委托', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const result = await deleteBinanceSimulatedPosition(position.id)
    if (!result.deleted) throw new Error('持仓计划监控不存在或已删除')
    await loadSimulatedPositions()
    const cancelledCount = asNumber(result.orderCleanup?.cancelledCount)
    if (result.orderCleanup?.status === 'SKIPPED_NO_CREDENTIALS') {
      ElMessage.warning('持仓计划监控已删除；未配置当前 Binance API，无法取消交易所委托')
    } else {
      ElMessage.success(cancelledCount > 0 ? `持仓计划监控已删除，已取消 ${cancelledCount} 笔同向委托` : '持仓计划监控已删除')
    }
  } catch (error) {
    ElMessage.error(marketErrorMessage(error))
  }
}

const restoreStoppedPosition = async (position) => {
  if (!position || position.executionStatus !== 'STOPPED') return
  try {
    await ElMessageBox.confirm(
      `${position.symbol} 将在账户确认同向真实持仓仍存在后恢复监控，浮动盈亏、收益率和移动止损重新开始计算。`,
      '恢复持仓计划监控',
      { type: 'warning', confirmButtonText: '恢复监控', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const restored = await restoreBinanceSimulatedPosition(position.id)
    await loadSimulatedPositions()
    if (restored?.executionStatus === 'STOPPED') {
      ElMessage.warning('未确认到可恢复的真实持仓，计划保持已止损状态')
    } else {
      ElMessage.success('持仓计划监控已恢复，浮动盈亏、收益率和移动止损已重新开始计算')
    }
  } catch (error) {
    ElMessage.error(marketErrorMessage(error))
  }
}

const loadMarketKlines = () => startSnapshotSubscription()

watch(selectedMarketSymbol, (symbol, previousSymbol) => {
  if (!previousSymbol || symbol === previousSymbol) return
  // Keep the old canvas visible while the new instrument loads, but remove its
  // source data so the chart can distinguish a completed symbol switch from a
  // regular refresh of the same instrument.
  klines.value = []
  marketStale.value = false
  marketError.value = ''
  marketDataReadyKey.value = ''
})

watch(interval, (value, previousValue) => {
  if (value === previousValue || !selectedMarketSymbol.value) return
  // A timeframe switch is a new data request, just like changing symbols.
  // Clear the ready marker so the chart stays covered until this interval's
  // response is committed.
  klines.value = []
  marketStale.value = false
  marketError.value = ''
  marketDataReadyKey.value = ''
})

const selectMarket = (item) => {
  const symbol = typeof item === 'string' ? item.trim().toUpperCase() : String(item?.symbol || '').trim().toUpperCase()
  const marketItem = activeMarkets.value.find((candidate) => candidate.symbol === symbol)
  if (!marketItem) return false
  selectedExecutionPositionId.value = null
  selectedMarketSymbol.value = marketItem.symbol
  marketSearch.value = marketItem.symbol
  loadMarketKlines()
  return true
}

const clearSelectedMarket = () => {
  selectedExecutionPositionId.value = null
  selectedMarketSymbol.value = ''
  marketSearch.value = ''
  selectedTicker.value = null
  klines.value = []
  marketLoading.value = false
  marketStale.value = false
  marketError.value = ''
  marketDataReadyKey.value = ''
  startSnapshotSubscription()
}

const queryMarketSymbols = (queryString, callback) => {
  const keyword = String(queryString || '').trim().toUpperCase()
  const suggestions = activeMarkets.value
    .filter((item) => !keyword || String(item.symbol || '').toUpperCase().includes(keyword))
    .slice(0, 16)
    .map((item) => ({ value: item.symbol, ...item }))
  callback(suggestions)
}

const selectMarketSuggestion = (item) => {
  selectMarket(item)
}

const selectTypedMarket = () => {
  selectMarket(marketSearch.value)
}

const clearAnalysisTimer = () => {
  if (futuresAnalysisPollTimer) window.clearTimeout(futuresAnalysisPollTimer)
  futuresAnalysisPollTimer = null
}

const applyMarketAnalysisJob = (job) => {
  futuresAnalysisJob.value = job
  const incomingPlans = job?.result?.plans ?? job?.progress?.plans
  if (!Array.isArray(incomingPlans)) return
  const includeTargetMonitoringPlan = futuresAnalysisScope.value === 'target'
  const isFullModelScan = !includeTargetMonitoringPlan
    && String(job?.result?.strategyEngine || job?.strategySettings?.strategyEngine || '').toUpperCase() === 'MODEL'
  // WAIT plans are never useful as a selectable target, including for a
  // named-symbol analysis.  Other non-actionable named-symbol plans remain
  // available only in the explicit target-monitoring workflow.
  futuresPlans.value = sortFuturesPlans(incomingPlans.filter((plan) => (
    isFullModelScan || (
      !isWaitAnalysisPlan(plan)
      && (includeTargetMonitoringPlan || isVisibleAnalysisPlan(plan))
    )
  )))
  const selectedStillVisible = futuresPlans.value.some((plan) => plan.symbol === selectedFuturesPlanSymbol.value)
  if (!selectedStillVisible) {
    selectedFuturesPlanSymbol.value = futuresPlans.value[0]?.symbol || ''
    if (selectedFuturesPlanSymbol.value) {
      selectedMarketSymbol.value = selectedFuturesPlanSymbol.value
      loadMarketKlines()
    }
  }
}

const pollMarketAnalysis = async (jobId, requestId) => {
  try {
    const job = await fetchBinanceFuturesAnalysis(jobId)
    if (requestId !== futuresAnalysisRequestId) return
    if (!job) throw new Error('扫描状态暂时不可用')
    futuresAnalysisPollFailures = 0
    applyMarketAnalysisJob(job)
    if (job.status === 'COMPLETED') {
      futuresAnalyzing.value = false
      clearAnalysisTimer()
      const trialPlans = (job.result?.plans || []).filter((plan) => plan?.trialEligible && plan.status !== 'ARMED')
      if (job.result?.matchedPlans?.length) ElMessage.success(`扫描完成，找到 ${job.result.matchedPlans.length} 个条件计划${trialPlans.length ? `，另有 ${trialPlans.length} 个可试错计划` : ''}`)
      else if (trialPlans.length) ElMessage.info(`扫描完成，找到 ${trialPlans.length} 个可试错计划`)
      else ElMessage.info('扫描完成，未找到等待触发的条件计划')
      return
    }
    if (job.status === 'FAILED') {
      futuresAnalyzing.value = false
      clearAnalysisTimer()
      await showMarketAnalysisFailure(job.error || '合约扫描失败')
      return
    }
    const schedule = () => pollMarketAnalysis(jobId, requestId)
    futuresAnalysisPollTimer = window.setTimeout(schedule, 800)
  } catch (error) {
    if (requestId !== futuresAnalysisRequestId) return
    const statusCode = Number(error?.response?.status || 0)
    const isTransient = !statusCode
      || statusCode >= 500
      || error?.code === 'ECONNABORTED'
      || error?.code === 'ERR_NETWORK'
    if (isTransient && futuresAnalyzing.value) {
      // A status poll failing does not mean the backend scan failed. Keep the
      // last progress/result visible and continue polling the same job.
      futuresAnalysisPollFailures = Math.min(futuresAnalysisPollFailures + 1, 6)
      const previousJob = futuresAnalysisJob.value || { id: jobId, status: 'RUNNING', progress: {} }
      futuresAnalysisJob.value = {
        ...previousJob,
        status: ['QUEUED', 'RUNNING'].includes(previousJob.status) ? previousJob.status : 'RUNNING',
        progress: {
          ...(previousJob.progress || {}),
          message: '状态读取暂时失败，后台扫描仍在继续，正在重试。',
        },
      }
      const retryDelay = Math.min(5000, 800 + futuresAnalysisPollFailures * 500)
      futuresAnalysisPollTimer = window.setTimeout(() => pollMarketAnalysis(jobId, requestId), retryDelay)
      return
    }
    futuresAnalyzing.value = false
    clearAnalysisTimer()
    await showMarketAnalysisFailure(marketErrorMessage(error))
  }
}

const runMarketAnalysis = async (targetSymbol = '') => {
  if (futuresAnalyzing.value) return
  clearAnalysisTimer()
  futuresAnalysisPollFailures = 0
  const requestId = futuresAnalysisRequestId + 1
  futuresAnalysisRequestId = requestId
  futuresAnalyzing.value = true
  futuresAnalysisScope.value = targetSymbol ? 'target' : 'scan'
  futuresPlans.value = []
  selectedFuturesPlanSymbol.value = ''
  try {
    const job = await analyzeBinanceFutures({
      network: network.value,
      limit: futuresScanLimit.value,
      symbol: targetSymbol,
      strategySettings: strategySettingsPayload(),
    })
    if (!job?.id) throw new Error('合约扫描任务未创建')
    applyMarketAnalysisJob(job)
    await pollMarketAnalysis(job.id, requestId)
  } catch (error) {
    await showMarketAnalysisFailure(marketErrorMessage(error))
    futuresAnalyzing.value = false
  }
}

const analyzeActiveMarket = () => runMarketAnalysis()

const analyzeMarketTarget = () => {
  const target = selectedMarketItem.value?.symbol
  if (!target) {
    ElMessage.warning('请先从合约搜索候选中选择交易对')
    return
  }
  runMarketAnalysis(target)
}

const selectAnalysisPlan = (plan) => {
  if (!plan?.symbol) return
  selectedExecutionPositionId.value = null
  selectedFuturesPlanSymbol.value = plan.symbol
  selectedMarketSymbol.value = plan.symbol
  marketSearch.value = plan.symbol
  loadMarketKlines()
}

const animateContentSwitch = async (targetRef) => {
  if (typeof window === 'undefined' || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
  await nextTick()
  const target = Array.isArray(targetRef.value) ? targetRef.value[0] : targetRef.value
  if (!target) return
  gsap.killTweensOf(target)
  gsap.fromTo(target, { autoAlpha: 0.08, y: 22, scale: 0.982 }, { autoAlpha: 1, y: 0, scale: 1, duration: 0.52, ease: 'power3.out', overwrite: true })
}

const selectAnalysisPlanWithMotion = (plan) => {
  selectAnalysisPlan(plan)
  void animateContentSwitch(analysisDetailRef)
}

const selectCopyTradingPosition = (position) => {
  selectedSmartMoneyPositionKey.value = copyTradingPositionTabKey(position)
  selectSmartMoneyMarket(copyTradingPositionSymbol(position))
}

const selectCopyTradingPositionWithMotion = (position) => {
  selectCopyTradingPosition(position)
  void animateContentSwitch(smartPositionDetailRef)
}

const selectExecutionPosition = (position) => {
  if (!position?.id || !position.symbol) return
  selectedExecutionPositionId.value = position.id
  selectedMarketSymbol.value = position.symbol
  marketSearch.value = position.symbol
  loadMarketKlines()
}

const futuresPlanStatusLabel = (status, plan = null) => {
  if (isMonitoringAnalysisPlan(plan)) return '监控'
  if (String(plan?.strategyEngine || '').toUpperCase() === 'MODEL' && String(status || '').toUpperCase() === 'WAIT') return '等待'
  if (status === 'ARMED' && isVisibleAnalysisPlan(plan)) return '符合'
  if (status !== 'ARMED' && isVisibleAnalysisPlan(plan)) return '试错'
  return '跳过'
}
const analysisPlanStatusLabel = futuresPlanStatusLabel

const refreshMarkets = () => {
  startSnapshotSubscription()
}

const toggleChartCollapsed = async () => {
  chartMorphTween?.kill()
  const stage = chartStageRef.value
  const panel = chartPanelRef.value
  if (!stage || !panel || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
    chartCollapsed.value = !chartCollapsed.value
    return
  }
  const isCollapsing = !chartCollapsed.value
  if (isCollapsing) {
    chartMorphTween = gsap.timeline({ defaults: { overwrite: true } })
      .to(panel, { autoAlpha: 0, y: -8, duration: 0.16, ease: 'power2.in' })
      .to(stage, { height: 0, minHeight: 0, marginTop: 0, marginBottom: 0, paddingTop: 0, paddingBottom: 0, duration: 0.24, ease: 'power3.inOut' })
      .set(stage, { clearProps: 'height,minHeight,marginTop,marginBottom,paddingTop,paddingBottom' })
      .add(() => { chartCollapsed.value = true })
  } else {
    chartCollapsed.value = false
    await nextTick()
    const expandedHeight = stage.getBoundingClientRect().height
    gsap.set(stage, { height: 0, minHeight: 0, overflow: 'hidden' })
    gsap.set(panel, { autoAlpha: 0, y: -8 })
    chartMorphTween = gsap.timeline({ defaults: { overwrite: true } })
      .to(stage, { height: expandedHeight, minHeight: expandedHeight, duration: 0.26, ease: 'power3.out' })
      .to(panel, { autoAlpha: 1, y: 0, duration: 0.22, ease: 'power2.out' }, '-=0.1')
      .set(stage, { clearProps: 'height,minHeight,overflow' })
  }
}

const modeLabel = (value) => ({ TREND: '趋势环境', RANGE: '区间环境', REBOUND: '过渡/反转观察' }[value] || '待判断')
const strategyLevelLabel = (value) => ({ STRUCTURE_EXTREME: '结构极值', CONFIRMED_PLATFORM: '确认平台/区域' }[value] || '结构极值')
const timeframeLabel = (timeframe) => ({ BULL: '偏多', BEAR: '偏空', NEUTRAL: '中性' }[timeframe.bias] || timeframe.state)

const clampAccountDrawerTop = (value) => {
  const viewportHeight = typeof window === 'undefined' ? 800 : window.innerHeight
  const numericValue = Number(value)
  const safeValue = Number.isFinite(numericValue) ? numericValue : 220
  return Math.min(Math.max(12, viewportHeight - 132), Math.max(12, safeValue))
}

const startAccountDrawerDrag = (event) => {
  if (accountDrawerOpen.value) return
  accountDrawerDragging.value = true
  accountDrawerMoved.value = false
  accountDrawerDragStartY.value = event.clientY
  accountDrawerDragStartTop.value = accountDrawerTop.value
  event.currentTarget?.setPointerCapture?.(event.pointerId)
}

const moveAccountDrawerDrag = (event) => {
  if (!accountDrawerDragging.value) return
  const offset = event.clientY - accountDrawerDragStartY.value
  if (Math.abs(offset) > 4) accountDrawerMoved.value = true
  accountDrawerTop.value = clampAccountDrawerTop(accountDrawerDragStartTop.value + offset)
}

const stopAccountDrawerDrag = (event) => {
  if (!accountDrawerDragging.value) return
  accountDrawerDragging.value = false
  event.currentTarget?.releasePointerCapture?.(event.pointerId)
}

const openAccountDrawer = () => {
  if (accountDrawerMoved.value) {
    accountDrawerMoved.value = false
    return
  }
  accountDrawerOpen.value = true
}

const closeAccountDrawer = () => {
  accountDrawerOpen.value = false
}

const strategySettingsPayload = () => {
  const normalized = normalizeStrategySettings(strategySettings.value)
  return {
    strategyEngine: normalized.strategyEngine,
    modelBranch: normalized.modelBranch,
    modelRunId: normalized.modelRunId,
    strategyMode: normalized.strategyMode,
    levelStrategy: normalized.levelStrategy,
    entryConfirmationMode: normalized.entryConfirmationMode,
    ...Object.fromEntries(STRATEGY_NUMERIC_SETTINGS_KEYS.map((key) => [key, Number(normalized[key])])),
  }
}

const applySavedStrategySettings = (value) => {
  const normalized = normalizeStrategySettings(value)
  savedStrategySettings.value = normalized
  strategySettings.value = { ...normalized }
  strategySettingsLoaded.value = true
  return normalized
}

const selectableModelOptions = computed(() => {
  const items = Array.isArray(modelRuns.value) ? modelRuns.value : []
  return items.filter((item) => item && item.selectable)
})
const modelSelectionKey = (model) => `${model?.runId || ''}:${model?.branch || 'BEST'}`
const selectedModel = computed(() => {
  const options = selectableModelOptions.value
  if (!options.length) return null
  const runId = String(strategySettings.value.modelRunId || '').trim()
  const branch = String(strategySettings.value.modelBranch || 'BEST').toUpperCase()
  if (runId) {
    return options.find((model) => String(model?.runId || '') === runId && String(model?.branch || '').toUpperCase() === branch)
      || options.find((model) => String(model?.runId || '') === runId)
      || null
  }
  const latestRunId = String(modelRunsLatestRunId.value || '').trim()
  return options.find((model) => String(model?.runId || '') === latestRunId && String(model?.branch || '').toUpperCase() === branch)
    || options.find((model) => String(model?.branch || '').toUpperCase() === branch)
    || options[0]
})
const handleModelSelectionEvent = (event) => {
  const runId = String(event?.detail?.runId || '').trim()
  const branch = String(event?.detail?.branch || '').trim().toUpperCase()
  if (runId && MODEL_BRANCHES.includes(branch)) {
    strategySettings.value = { ...strategySettings.value, strategyEngine: 'MODEL', modelRunId: runId, modelBranch: branch }
  }
}
const selectedModelSelectionKey = computed(() => strategySettings.value.modelRunId ? modelSelectionKey({ runId: strategySettings.value.modelRunId, branch: strategySettings.value.modelBranch }) : '')
const applyModelSelectionKey = (value) => {
  const text = String(value || '').trim()
  if (!text) {
    strategySettings.value = { ...strategySettings.value, modelRunId: null }
    return
  }
  const separator = text.lastIndexOf(':')
  const runId = separator >= 0 ? text.slice(0, separator) : text
  const branch = separator >= 0 ? text.slice(separator + 1).toUpperCase() : strategySettings.value.modelBranch
  strategySettings.value = {
    ...strategySettings.value,
    modelRunId: runId || null,
    modelBranch: MODEL_BRANCHES.includes(branch) ? branch : strategySettings.value.modelBranch,
  }
}
const modelVersionDisplay = (value) => {
  const version = String(value || '').trim()
  const match = version.match(/(?:^|[-_])v(\d+)(?:[-_]|$)/i)
  return match ? `v${match[1]}` : (version || '版本未知')
}
const modelHoldingProfileLabel = (value) => ({
  SHORT: '短线',
  SWING: '摆动（数天）',
  POSITION: '趋势持仓（数周）',
}[String(value || '').toUpperCase()] || '周期档案未知')
const modelOptionLabel = (model) => {
  const branch = model?.branch === 'STABLE' ? '稳定末期' : '验证最佳'
  const epoch = model?.selectedEpoch ? `第${model.selectedEpoch}轮` : '检查点'
  const version = modelVersionDisplay(model?.modelVersion)
  const coverage = formatModelPercent(model?.test?.selectedCoverage)
  const winRate = formatModelPercent(model?.test?.selectedWinRate)
  return `${version} · ${modelHoldingProfileLabel(model?.holdingProfile || model?.config?.holdingProfile)} · ${branch} ${epoch} · 覆盖 ${coverage} · 胜率 ${winRate}`
}
const loadModelRuns = async () => {
  if (modelRunsLoading.value) return
  modelRunsLoading.value = true
  try {
    const result = await fetchBinanceFuturesModelRuns({ network: 'mainnet', limit: 24 })
    modelRunsLatestRunId.value = String(result?.latestRunId || '').trim()
    modelRuns.value = Array.isArray(result?.models)
      ? result.models
      : Array.isArray(result?.runs) ? result.runs.flatMap((run) => Array.isArray(run.branches) ? run.branches : []) : []
  } catch {
    modelRunsLatestRunId.value = ''
    modelRuns.value = []
  } finally {
    modelRunsLoading.value = false
  }
}

watch(accountSettingsTab, (tab) => {
  if (tab === 'strategy' && accountSettingsVisible.value && props.currentUser) void loadModelRuns()
})

const applySavedCopyTradingSettings = (value) => {
  const topTraderId = String(value?.topTraderId || '5132388877263187456')
  const copyMultipliers = Object.fromEntries(
    Object.entries(value?.copyMultipliers || copyTradingSettings.value.copyMultipliers || {})
      .map(([traderId, multiplier]) => [String(traderId), normalizeCopyTradingMultiplier(multiplier)]),
  )
  const copyMultiplier = copyMultipliers[topTraderId] ?? normalizeCopyTradingMultiplier(value?.copyMultiplier)
  copyTradingSettings.value = {
    topTraderId,
    copyMultiplier: normalizeCopyTradingMultiplier(copyMultiplier),
    copyMultipliers,
    smartMoneyAuthConfigured: Boolean(value?.smartMoneyAuthConfigured),
    smartMoneyAuthUpdatedAt: value?.smartMoneyAuthUpdatedAt || null,
    updatedAt: value?.updatedAt || null,
  }
  return copyTradingSettings.value
}

const applyCopyTradingSettingsResponse = (response) => {
  const payload = response?.settings || response || {}
  applySavedCopyTradingSettings(payload)
  if (Array.isArray(response?.subscriptions)) {
    copyTradingSubscriptions.value = response.subscriptions
    copyTradingSubscriptionAvatarErrors.value = {}
    ensureSelectedCopyTradingSubscription()
  }
  if (Array.isArray(response?.follows)) copyTradingFollows.value = response.follows
  if (response?.copyTrading) applyBinanceSnapshot({ copyTrading: response.copyTrading })
  return copyTradingSettings.value
}

const copyTradingSubscriptionAvatarKey = (subscription) => String(subscription?.topTraderId || subscription?.traderName || subscription?.accountName || '')
const copyTradingSubscriptionAvatarFailed = (subscription) => Boolean(copyTradingSubscriptionAvatarErrors.value[copyTradingSubscriptionAvatarKey(subscription)])
const handleCopyTradingSubscriptionAvatarError = (subscription) => {
  const key = copyTradingSubscriptionAvatarKey(subscription)
  if (!key) return
  copyTradingSubscriptionAvatarErrors.value = { ...copyTradingSubscriptionAvatarErrors.value, [key]: true }
}
const copyTradingSubscriptionAvatarInitial = (subscription) => (
  String(subscription?.traderName || subscription?.accountName || '?').trim().slice(0, 1).toUpperCase()
)

function ensureSelectedCopyTradingSubscription() {
  const subscriptions = copyTradingSubscriptions.value
  if (!subscriptions.length) return
  const selectedTraderId = String(copyTradingSettings.value.topTraderId || '').trim()
  if (subscriptions.some((item) => String(item?.topTraderId || '').trim() === selectedTraderId)) return
  const topTraderId = String(subscriptions[0]?.topTraderId || '').trim()
  if (!topTraderId) return
  copyTradingSettings.value = {
    ...copyTradingSettings.value,
    topTraderId,
    copyMultiplier: copyTradingMultiplierForTrader(topTraderId),
  }
}

const processCopyTradingSubscriptionSelectionQueue = async () => {
  copyTradingSubscriptionSelecting.value = true
  try {
    while (copyTradingSubscriptionSelectionQueue) {
      const request = copyTradingSubscriptionSelectionQueue
      copyTradingSubscriptionSelectionQueue = null
      copyTradingSubscriptionSelectionInFlightTraderId = request.topTraderId

      let response = null
      try {
        // Keep writes ordered. This prevents an older request from finishing
        // after a newer one and restoring the wrong trader on the server.
        response = await saveBinanceCopyTradingSettings({ topTraderId: request.topTraderId })
      } catch (error) {
        if (request.version === copyTradingSubscriptionSelectionVersion) {
          ElMessage.error(`聪明钱用户切换失败：${marketErrorMessage(error)}`)
        }
        continue
      } finally {
        copyTradingSubscriptionSelectionInFlightTraderId = ''
      }

      // A newer click supersedes this response. Do not let stale data or an
      // old loading completion alter the visible trader or trigger motion.
      if (request.version !== copyTradingSubscriptionSelectionVersion) continue

      const responseTraderId = String(response?.settings?.topTraderId || response?.topTraderId || '').trim()
      if (responseTraderId && responseTraderId !== request.topTraderId) continue

      applyCopyTradingSettingsResponse(response)
      // The detail DOM must contain the newly selected snapshot before the
      // transition starts; otherwise the animation only replays old content.
      await nextTick()
      if (request.version === copyTradingSubscriptionSelectionVersion) {
        await animateContentSwitch(smartMoneyGridRef)
      }
    }
  } finally {
    copyTradingSubscriptionSelecting.value = false
    copyTradingSubscriptionSelectionInFlightTraderId = ''
  }
}

const selectCopyTradingSubscription = (subscription) => {
  const topTraderId = String(subscription?.topTraderId || '').trim()
  if (!topTraderId) return Promise.resolve()

  const pendingTraderId = copyTradingSubscriptionSelectionQueue?.topTraderId || copyTradingSubscriptionSelectionInFlightTraderId
  if (topTraderId === (pendingTraderId || String(copyTradingSettings.value.topTraderId || '').trim())) return copyTradingSubscriptionSelectionWorker || Promise.resolve()

  const version = ++copyTradingSubscriptionSelectionVersion
  copyTradingSubscriptionSelectionQueue = { topTraderId, version }
  if (!copyTradingSubscriptionSelectionWorker) {
    copyTradingSubscriptionSelectionWorker = processCopyTradingSubscriptionSelectionQueue()
      .finally(() => {
        copyTradingSubscriptionSelectionWorker = null
      })
  }
  return copyTradingSubscriptionSelectionWorker
}

const selectCopyTradingSubscriptionWithMotion = (subscription) => selectCopyTradingSubscription(subscription)

const loadCopyTradingSettings = async ({ silent = false } = {}) => {
  if (!props.currentUser) return null
  const selectionVersionAtStart = copyTradingSubscriptionSelectionVersion
  copyTradingSettingsLoading.value = true
  try {
    const response = await fetchBinanceCopyTradingSettings()
    const selectedTraderId = String(copyTradingSettings.value.topTraderId || '').trim()
    const settings = applyCopyTradingSettingsResponse(response)
    // Initial settings loading can overlap a user click. Keep the current
    // selection in that case; the queued save owns the eventual commit.
    if (selectionVersionAtStart !== copyTradingSubscriptionSelectionVersion && selectedTraderId) {
      copyTradingSettings.value = {
        ...copyTradingSettings.value,
        topTraderId: selectedTraderId,
        copyMultiplier: copyTradingMultiplierForTrader(selectedTraderId),
      }
    }
    return settings
  } catch (error) {
    if (!silent) ElMessage.error(`跟单参数加载失败：${marketErrorMessage(error)}`)
    return null
  } finally {
    copyTradingSettingsLoading.value = false
  }
}

const loadNotificationSettings = async ({ silent = false } = {}) => {
  if (!props.currentUser) return null
  notificationSettingsLoading.value = true
  try {
    const settings = await fetchNotificationSettings()
    notificationSettings.value = { email: String(settings?.email || '') }
    return notificationSettings.value
  } catch (error) {
    if (!silent) ElMessage.error(`邮件通知设置加载失败：${marketErrorMessage(error)}`)
    return null
  } finally {
    notificationSettingsLoading.value = false
  }
}

const saveNotificationSettings = async () => {
  if (!props.currentUser || notificationSettingsSaving.value) return
  notificationSettingsSaving.value = true
  try {
    const settings = await requestSaveNotificationSettings({ email: notificationSettings.value.email })
    notificationSettings.value = { email: String(settings?.email || '') }
    ElMessage.success('邮件通知设置已保存，A 股与 Binance 将同步使用')
  } catch (error) {
    ElMessage.error(`邮件通知设置保存失败：${marketErrorMessage(error)}`)
  } finally {
    notificationSettingsSaving.value = false
  }
}

const sendNotificationTestEmail = async () => {
  if (!notificationSettings.value.email || notificationTestSending.value) return
  notificationTestSending.value = true
  try {
    await sendTestEmail(notificationSettings.value.email)
    ElMessage.success(`测试邮件已发送至 ${notificationSettings.value.email}`)
  } catch (error) {
    ElMessage.error(`测试邮件发送失败：${marketErrorMessage(error)}`)
  } finally {
    notificationTestSending.value = false
  }
}

const saveCopyTradingSettings = async ({ saveMultiplier = true } = {}) => {
  if (!props.currentUser || copyTradingSettingsSaving.value) return
  copyTradingSettingsSaving.value = true
  try {
    const payload = { topTraderId: copyTradingSettings.value.topTraderId }
    if (saveMultiplier) payload.copyMultiplier = copyTradingMultiplier.value
    applyCopyTradingSettingsResponse(await saveBinanceCopyTradingSettings(payload))
    ElMessage.success(saveMultiplier ? '该聪明钱用户的跟单倍率已保存' : '聪明钱用户 ID 已保存，数据由后台定时刷新')
  } catch (error) {
    ElMessage.error(`跟单参数保存失败：${marketErrorMessage(error)}`)
  } finally {
    copyTradingSettingsSaving.value = false
  }
}

const saveCopyTradingMultiplier = () => saveCopyTradingSettings({ saveMultiplier: true })

const updateCopyTradingMultiplier = async (value) => {
  const next = normalizeCopyTradingMultiplier(value)
  if (next === copyTradingMultiplier.value) return
  copyTradingSettings.value = { ...copyTradingSettings.value, copyMultiplier: next }
  await saveCopyTradingMultiplier()
}

const adjustCopyTradingMultiplier = (delta) => {
  void updateCopyTradingMultiplier(copyTradingMultiplier.value + delta)
}

const commitCopyTradingMultiplier = (event) => {
  const next = normalizeCopyTradingMultiplier(event?.target?.value)
  if (event?.target) event.target.value = String(next)
  void updateCopyTradingMultiplier(next)
}

const BINANCE_SMART_MONEY_URL = 'https://www.binance.com/zh-CN/smart-money/my-subscriptions'

const captureSmartMoneyAuth = async () => {
  if (!props.currentUser || smartMoneyAuthCapturing.value) return
  const popup = window.open(BINANCE_SMART_MONEY_URL, '_blank')
  if (!popup) {
    ElMessage.warning('浏览器阻止了新标签页，请允许弹出窗口后重试')
    return
  }
  smartMoneyAuthCapturing.value = true
  try {
    const auth = await captureBinanceSmartMoneyAuth()
    if (!auth.cookie || !auth.csrfToken) throw new Error('自动化接口未返回完整登录态')
    smartMoneyAuthForm.value = { cookie: auth.cookie, csrfToken: auth.csrfToken }
    ElMessage.success('已从 Binance 请求 Header 获取登录态，请点击保存')
  } catch (error) {
    ElMessage.error(`聪明钱登录态获取失败：${marketErrorMessage(error)}`)
  } finally {
    smartMoneyAuthCapturing.value = false
  }
}

const saveSmartMoneyAuthSettings = async () => {
  if (!props.currentUser || copyTradingSettingsSaving.value) return
  copyTradingSettingsSaving.value = true
  try {
    const response = await saveBinanceCopyTradingSettings({
      topTraderId: copyTradingSettings.value.topTraderId,
      smartMoneyAuth: {
        cookie: smartMoneyAuthForm.value.cookie,
        csrfToken: smartMoneyAuthForm.value.csrfToken,
      },
    })
    applyCopyTradingSettingsResponse(response)
    ElMessage.success('聪明钱登录态已加密保存，后台将开始刷新数据')
  } catch (error) {
    ElMessage.error(`聪明钱登录态保存失败：${marketErrorMessage(error)}`)
  } finally {
    copyTradingSettingsSaving.value = false
  }
}

const loadStrategySettings = async ({ silent = false } = {}) => {
  if (!props.currentUser) return null
  if (strategySettingsLoaded.value) return strategySettings.value
  if (strategySettingsLoadPromise) return strategySettingsLoadPromise
  strategySettingsLoading.value = true
  strategySettingsLoadPromise = (async () => {
    try {
      return applySavedStrategySettings(await fetchBinanceStrategySettings())
    } catch (error) {
      if (!silent) ElMessage.error(`策略设置加载失败：${marketErrorMessage(error)}`)
      return null
    } finally {
      strategySettingsLoading.value = false
      strategySettingsLoadPromise = null
    }
  })()
  return strategySettingsLoadPromise
}

const loadBinanceUiSettings = async ({ silent = false } = {}) => {
  if (!props.currentUser) return null
  try {
    const settings = await fetchBinanceUiSettings()
    const value = Number(settings?.positionPnlSmoothing)
    futuresPositionSmoothing.value = Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : 0.25
    futuresPositionSmoothingLoaded.value = true
    return settings
  } catch (error) {
    futuresPositionSmoothingLoaded.value = false
    if (!silent) ElMessage.error(`盈亏走势设置加载失败：${marketErrorMessage(error)}`)
    return null
  }
}

const saveFuturesPositionSmoothing = async () => {
  if (!props.currentUser || !futuresPositionSmoothingLoaded.value) return
  try {
    const settings = await saveBinanceUiSettings({ positionPnlSmoothing: futuresPositionSmoothing.value })
    const value = Number(settings?.positionPnlSmoothing)
    if (Number.isFinite(value)) futuresPositionSmoothing.value = Math.min(1, Math.max(0, value))
  } catch (error) {
    ElMessage.error(`盈亏走势设置保存失败：${marketErrorMessage(error)}`)
  }
}

const applySavedConnectionSettings = (value) => {
  const normalized = normalizeConnectionSettings(value)
  connectionSettings.value = normalized
  connectionSettingsLoaded.value = true
  return normalized
}

const loadConnectionSettings = async ({ silent = false } = {}) => {
  if (!props.currentUser) return null
  connectionSettingsLoading.value = true
  try {
    const result = await fetchBinanceConnectionSettings()
    const settings = applySavedConnectionSettings(result?.settings || result)
    if (result?.connection) connectionStatus.value = result.connection
    return settings
  } catch (error) {
    if (!silent) ElMessage.error(`连接方式加载失败：${marketErrorMessage(error)}`)
    return null
  } finally {
    connectionSettingsLoading.value = false
  }
}

const saveConnectionSettings = async () => {
  if (!props.currentUser) return
  const settings = normalizeConnectionSettings(connectionSettings.value)
  connectionSettingsSaving.value = true
  try {
    const result = await saveBinanceConnectionSettings(settings)
    applySavedConnectionSettings(result?.settings || settings)
    if (result?.connection) connectionStatus.value = result.connection
    startSnapshotSubscription()
    ElMessage.success(`${settings.connectionMode === 'WEBSOCKET' ? 'WebSocket' : 'REST'} 连接方式已保存，后台将持续维护线路`)
  } catch (error) {
    ElMessage.error(`连接方式保存失败：${marketErrorMessage(error)}`)
  } finally {
    connectionSettingsSaving.value = false
  }
}

const saveStrategySettings = async () => {
  if (!props.currentUser) return
  const settings = strategySettingsPayload()
  const {
    strategyEngine,
    modelBranch,
    strategyMode,
    levelStrategy,
    maxAccountLossRatio,
    maxPortfolioRiskRatio,
    maxSameSidePositions,
    dailyLossLimitRatio,
    rangeEdgeFraction,
    rangeMinimumTargetR,
    trendMinimumTargetR,
    entryConfirmationMode,
    entryConfirmationExpiryBars,
    entryFailureExitBars,
    entryFailureBodyAtrMultiplier,
    trailingAtrMultiplier,
    structureStopAtrMultiplier,
    triggerZoneStopBufferAtrMultiplier,
    breakevenBufferAtrMultiplier,
    movingStopActivationR,
    nearTermMinimumTargetR,
    protectiveTakeProfitRatio,
    firstTakeProfitRatio,
    secondTakeProfitRatio,
  } = settings
  if (!STRATEGY_ENGINES.includes(strategyEngine)) {
    ElMessage.warning('请选择有效的策略引擎')
    return
  }
  if (!MODEL_BRANCHES.includes(modelBranch)) {
    ElMessage.warning('请选择有效的时序模型分支')
    return
  }
  if (!STRATEGY_MODES.includes(strategyMode)) {
    ElMessage.warning('请选择有效的策略模式')
    return
  }
  if (!LEVEL_STRATEGIES.includes(levelStrategy)) {
    ElMessage.warning('请选择有效的点位策略路线')
    return
  }
  if (!Number.isFinite(maxAccountLossRatio) || maxAccountLossRatio <= 0 || maxAccountLossRatio > 100) {
    ElMessage.warning('账户最大亏损比必须在 0% 到 100% 之间')
    return
  }
  if (!Number.isFinite(maxPortfolioRiskRatio) || maxPortfolioRiskRatio <= 0 || maxPortfolioRiskRatio > 100) {
    ElMessage.warning('组合结构风险上限必须在 0% 到 100% 之间')
    return
  }
  if (!Number.isInteger(maxSameSidePositions) || maxSameSidePositions < 1 || maxSameSidePositions > 20) {
    ElMessage.warning('同向持仓上限必须是 1 到 20 的整数')
    return
  }
  if (!Number.isFinite(dailyLossLimitRatio) || dailyLossLimitRatio <= 0 || dailyLossLimitRatio > 100) {
    ElMessage.warning('单日风险上限必须在 0% 到 100% 之间')
    return
  }
  if (!Number.isFinite(rangeEdgeFraction) || rangeEdgeFraction < 0.05 || rangeEdgeFraction >= 0.5) {
    ElMessage.warning('区间边缘比例必须在 0.05 到 0.5 之间')
    return
  }
  if (!Number.isFinite(rangeMinimumTargetR) || rangeMinimumTargetR < 0.5 || rangeMinimumTargetR > 10) {
    ElMessage.warning('区间最小目标 R 必须在 0.5 到 10 之间')
    return
  }
  if (!Number.isFinite(trendMinimumTargetR) || trendMinimumTargetR < 0.5 || trendMinimumTargetR > 10) {
    ElMessage.warning('趋势最小目标 R 必须在 0.5 到 10 之间')
    return
  }
  if (!ENTRY_CONFIRMATION_MODES.includes(entryConfirmationMode)) {
    ElMessage.warning('请选择有效的入场确认方式')
    return
  }
  if (!Number.isInteger(entryConfirmationExpiryBars) || entryConfirmationExpiryBars < 1 || entryConfirmationExpiryBars > 8) {
    ElMessage.warning('确认有效 K 线数必须是 1 到 8 的整数')
    return
  }
  if (!Number.isInteger(entryFailureExitBars) || entryFailureExitBars < 1 || entryFailureExitBars > 8) {
    ElMessage.warning('早期失败复核 K 线数必须是 1 到 8 的整数')
    return
  }
  if (!Number.isFinite(entryFailureBodyAtrMultiplier) || entryFailureBodyAtrMultiplier < 0.1 || entryFailureBodyAtrMultiplier > 5) {
    ElMessage.warning('强反向实体 ATR 倍数必须在 0.1 到 5 之间')
    return
  }
  if (!Number.isFinite(trailingAtrMultiplier) || trailingAtrMultiplier <= 0 || trailingAtrMultiplier > 20) {
    ElMessage.warning('极点 ATR 移动止损倍数必须在 0 到 20 之间')
    return
  }
  if (!Number.isFinite(structureStopAtrMultiplier) || structureStopAtrMultiplier < 0 || structureStopAtrMultiplier > 10 || !Number.isFinite(breakevenBufferAtrMultiplier) || breakevenBufferAtrMultiplier < 0 || breakevenBufferAtrMultiplier > 10) {
    ElMessage.warning('结构止损与保本缓冲的 ATR 倍数必须在 0 到 10 之间')
    return
  }
  if (!Number.isFinite(triggerZoneStopBufferAtrMultiplier) || triggerZoneStopBufferAtrMultiplier < 0.1 || triggerZoneStopBufferAtrMultiplier > 10) {
    ElMessage.warning('触发区防假突破缓冲必须在 0.1 到 10 之间')
    return
  }
  if (!Number.isFinite(movingStopActivationR) || movingStopActivationR <= 0 || movingStopActivationR > 20) {
    ElMessage.warning('移动止损启动 R 倍数必须在 0 到 20 之间')
    return
  }
  if (!Number.isFinite(nearTermMinimumTargetR) || nearTermMinimumTargetR < 0.05 || nearTermMinimumTargetR > 5) {
    ElMessage.warning('近端止盈最低 R 必须在 0.05 到 5 之间')
    return
  }
  if (!Number.isFinite(protectiveTakeProfitRatio) || !Number.isFinite(firstTakeProfitRatio) || !Number.isFinite(secondTakeProfitRatio) || protectiveTakeProfitRatio < 1 || protectiveTakeProfitRatio >= firstTakeProfitRatio || firstTakeProfitRatio >= secondTakeProfitRatio || secondTakeProfitRatio >= 100) {
    ElMessage.warning('三档止盈必须满足近端保护 < 第一档 < 第二档，且第二档小于 100%')
    return
  }
  strategySettingsSaving.value = true
  try {
    applySavedStrategySettings(await saveBinanceStrategySettings(settings))
    ElMessage.success('策略设置已保存')
  } catch (error) {
    ElMessage.error(`策略设置保存失败：${marketErrorMessage(error)}`)
  } finally {
    strategySettingsSaving.value = false
  }
}

const runWebsocketTest = async () => {
  if (!props.currentUser || websocketTestLoading.value) return
  websocketTestKind.value = 'market'
  websocketTestLoading.value = true
  websocketTestResult.value = null
  try {
    const result = await testBinanceFuturesWebsocket()
    websocketTestResult.value = result && result.ok
      ? result
      : { ok: false, message: 'WebSocket 未返回有效的测试结果' }
    if (websocketTestResult.value.ok) ElMessage.success('WebSocket 公开行情连接正常')
  } catch (error) {
    const message = marketErrorMessage(error)
    websocketTestResult.value = { ok: false, message }
    ElMessage.error(`WebSocket 测试失败：${message}`)
  } finally {
    websocketTestLoading.value = false
  }
}

const runWebsocketAccountTest = async () => {
  if (!props.currentUser || websocketAccountTestLoading.value) return
  websocketTestKind.value = 'account'
  websocketAccountTestLoading.value = true
  websocketAccountTestResult.value = null
  try {
    const result = await testBinanceFuturesAccountWebsocket()
    websocketAccountTestResult.value = result && result.ok
      ? result
      : { ok: false, message: 'WebSocket 未返回有效的账户测试结果' }
    if (websocketAccountTestResult.value.ok) ElMessage.success('账户持仓 WebSocket 查询正常')
  } catch (error) {
    const message = marketErrorMessage(error)
    websocketAccountTestResult.value = { ok: false, message }
    ElMessage.error(`账户持仓测试失败：${message}`)
  } finally {
    websocketAccountTestLoading.value = false
  }
}

const openAccountSettings = async () => {
  if (!props.currentUser) {
    emit('login-request')
    return
  }
  accountSettingsTab.value = 'connection'
  websocketTestKind.value = ''
  websocketTestResult.value = null
  websocketAccountTestResult.value = null
  accountSettingsVisible.value = true
  await Promise.all([
    loadCopyTradingSettings({ silent: true }),
    loadNotificationSettings({ silent: true }),
    loadStrategySettings({ silent: true }),
    loadConnectionSettings({ silent: true }),
    loadModelRuns(),
  ])
}

defineExpose({ openAccountSettings })

const connectAccount = async () => {
  if (!props.currentUser) return
  if (!apiKey.value.trim() || !apiSecret.value.trim()) {
    ElMessage.warning('请填写 API Key 和 Secret Key')
    return
  }
  connectionLoading.value = true
  try {
    const result = await connectBinanceAccount({ apiKey: apiKey.value, apiSecret: apiSecret.value })
    account.value = result.account || null
    credentialsProfile.value = result.credentials || { configured: true, network: 'mainnet', apiKeyMasked: '', updatedAt: null, requiresReconfiguration: false }
    connected.value = true
    apiKey.value = ''
    apiSecret.value = ''
    accountSettingsVisible.value = false
    startSnapshotSubscription()
    ElMessage.success('Binance 主网 API 配置已保存，后台将持续自动连接')
  } catch (error) {
    connected.value = false
    account.value = null
    ElMessage.error(error.response?.data?.message || 'API 连接失败')
  } finally {
    connectionLoading.value = false
  }
}

const disconnectAccount = () => {
  connected.value = false
  account.value = null
  apiKey.value = ''
  apiSecret.value = ''
}

const openFuturesProtectionDialog = (position) => {
  futuresProtectionPosition.value = position
  futuresProtectionForm.value = {
    symbol: position.symbol,
    positionSide: position.positionSide || 'BOTH',
    positionStopLoss: position.positionStopLoss ?? position.stopLoss ?? null,
    partialStopLoss: position.partialStopLoss ?? null,
    partialTakeProfit: position.partialTakeProfit ?? null,
    quantityRatio: position.partialQuantityRatio || 50,
  }
  futuresProtectionInitial.value = { ...futuresProtectionForm.value }
  futuresProtectionDialogVisible.value = true
}

const openMarketCloseDialog = (position) => {
  if (!props.currentUser) {
    emit('login-request')
    return
  }
  marketClosePosition.value = position
  marketCloseForm.value = { quantityRatio: 100 }
  marketCloseDialogVisible.value = true
}

const submitMarketClose = async () => {
  const position = marketClosePosition.value
  if (!position || marketCloseSaving.value || marketCloseQuantity.value <= 0) return
  const ratio = Math.min(100, Math.max(1, asNumber(marketCloseForm.value.quantityRatio) || 1))
  try {
    await ElMessageBox.confirm(
      `${position.symbol} 将以市价平仓 ${ratio}%（实际提交数量 ${formatCrypto(marketCloseQuantity.value)}），可能产生滑点，请确认。`,
      '确认市价平仓',
      { type: 'warning', confirmButtonText: '确认平仓', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  marketCloseSaving.value = true
  try {
    const result = await closeBinanceFuturesPositionMarket({
      symbol: position.symbol,
      positionSide: position.positionSide || 'BOTH',
      quantityRatio: ratio,
    })
    marketCloseDialogVisible.value = false
    await loadSavedAccount({ silent: true })
    ElMessage.success(`${position.symbol} 市价平仓单已提交，数量 ${formatCrypto(result?.quantity || marketCloseQuantity.value)}`)
  } catch (error) {
    ElMessage.error(`市价平仓失败：${marketErrorMessage(error)}`)
  } finally {
    marketCloseSaving.value = false
  }
}

const nullableProtectionNumber = (value) => {
  if (value === null || value === undefined || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : NaN
}

const protectionScopeChanged = (scope) => {
  const initial = futuresProtectionInitial.value || {}
  const current = futuresProtectionForm.value
  const keys = scope === 'partial'
    ? ['partialStopLoss', 'partialTakeProfit', 'quantityRatio']
    : ['positionStopLoss']
  return keys.some((key) => {
    const left = key === 'quantityRatio' ? Number(initial[key] || 50) : nullableProtectionNumber(initial[key])
    const right = key === 'quantityRatio' ? Number(current[key] || 50) : nullableProtectionNumber(current[key])
    return left !== right
  })
}

const protectionScopeValues = (scope) => {
  const isPartial = scope === 'partial'
  if (!isPartial) {
    return {
      stopLoss: nullableProtectionNumber(futuresProtectionForm.value.positionStopLoss),
    }
  }
  return {
    stopLoss: nullableProtectionNumber(futuresProtectionForm.value.partialStopLoss),
    takeProfit: nullableProtectionNumber(futuresProtectionForm.value.partialTakeProfit),
    quantityRatio: Number(futuresProtectionForm.value.quantityRatio || 50),
  }
}

const validateProtectionScope = (scope, values, markPrice, latestPrice, isLong) => {
  const isPartial = scope === 'partial'
  const { stopLoss, takeProfit, quantityRatio } = values
  if ((stopLoss !== null && (!Number.isFinite(stopLoss) || stopLoss <= 0)) || (takeProfit !== undefined && takeProfit !== null && (!Number.isFinite(takeProfit) || takeProfit <= 0))) {
    ElMessage.warning(`${isPartial ? '比例' : '仓位'}保护止盈止损价必须是正数，留空表示清除对应保护单`)
    return false
  }
  if (isPartial && (!Number.isFinite(quantityRatio) || quantityRatio <= 0 || quantityRatio > 100)) {
    ElMessage.warning('比例保护必须在 1% 到 100% 之间')
    return false
  }
  if (markPrice > 0 && stopLoss !== null && ((isLong && stopLoss >= markPrice) || (!isLong && stopLoss <= markPrice))) {
    ElMessage.warning(isLong ? '做多止损必须低于标记价' : '做空止损必须高于标记价')
    return false
  }
  if (latestPrice > 0 && takeProfit !== null && ((isLong && takeProfit <= latestPrice) || (!isLong && takeProfit >= latestPrice))) {
    ElMessage.warning(isLong ? '做多止盈必须高于最新成交价' : '做空止盈必须低于最新成交价')
    return false
  }
  return true
}

const saveFuturesProtection = async () => {
  const position = futuresProtectionPosition.value
  if (!position) return
  const changedScopes = ['partial', 'position'].filter((scope) => protectionScopeChanged(scope))
  if (!changedScopes.length) {
    ElMessage.info('没有修改保护价或平仓比例')
    return
  }
  const markPrice = futuresMarkPriceFor(position)
  const latestPrice = futuresLatestPriceFor(position)
  const isLong = position.side === 'LONG'
  const values = Object.fromEntries(changedScopes.map((scope) => [scope, protectionScopeValues(scope)]))
  if (changedScopes.some((scope) => !validateProtectionScope(scope, values[scope], markPrice, latestPrice, isLong))) return
  const summary = changedScopes.map((scope) => {
    const isPartial = scope === 'partial'
    const { stopLoss, takeProfit, quantityRatio } = values[scope]
    const stopLabel = stopLoss === null ? '清除止损' : `${isPartial ? '比例止损' : '仓位止损'} ${formatPrice(stopLoss)}`
    if (!isPartial) return `${stopLabel}，按标记价触发后全仓平仓`
    const takeProfitLabel = takeProfit === null ? '清除比例止盈' : `比例止盈 ${formatPrice(takeProfit)}`
    return `${stopLabel}、${takeProfitLabel}，按持仓数量 ${quantityRatio}% 触发`
  }).join('；')
  try {
    await ElMessageBox.confirm(
      `${position.symbol} 将更新：${summary}。数量型止盈使用带数量的最新价条件单；仓位止损使用标记价条件单。新的同类保护单全部创建成功后，才会撤销旧单。`,
      '确认更新保护单（数量型止盈 / 仓位止损）',
      { type: 'warning', confirmButtonText: '确认更新', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  futuresProtectionSaving.value = true
  try {
    for (const scope of changedScopes) {
      const isPartial = scope === 'partial'
      const update = isPartial ? updateBinanceFuturesPartialProtection : updateBinanceFuturesPositionLevelProtection
      const { stopLoss, takeProfit, quantityRatio } = values[scope]
      const updatePayload = {
        symbol: position.symbol,
        positionSide: futuresProtectionForm.value.positionSide,
        stopLoss,
      }
      if (isPartial || takeProfit !== undefined) updatePayload.takeProfit = takeProfit
      if (isPartial) updatePayload.quantityRatio = quantityRatio
      await update(updatePayload)
    }
    futuresProtectionDialogVisible.value = false
    await loadSavedAccount({ silent: true })
    ElMessage.success(changedScopes.length === 2 ? '比例止盈止损与仓位止损已更新' : `${changedScopes[0] === 'partial' ? '比例止盈止损' : '仓位止损'}已更新`)
  } catch (error) {
    ElMessage.error(marketErrorMessage(error))
  } finally {
    futuresProtectionSaving.value = false
  }
}

const applySelectedPlanToActualPosition = async () => {
  const execution = selectedExecutionPosition.value
  const plan = execution?.plan
  const actual = selectedActualFuturesPosition.value
  if (!execution || !plan || execution.marketMode !== 'FUTURES') return
  if (!actual) {
    ElMessage.warning('未读取到对应的实际合约持仓，请先连接账户并刷新账户资产')
    return
  }
  if (plan.status === 'STOPPED') {
    ElMessage.warning('已止损的执行计划不能再应用保护单')
    return
  }
  const stopLoss = asNumber(plan.activeStop)
  const protectiveTakeProfit = asNumber(planTarget(plan, 'PROTECTIVE_TARGET', -1)?.price) || null
  const firstTakeProfit = asNumber(planTarget(plan, 'FIRST_TARGET', 0)?.price)
  const extensionTakeProfit = asNumber(planTarget(plan, 'EXTENSION_TARGET', 1)?.price) || null
  const normalizedRatios = normalizedPlanProtectionRatios(plan)
  const protectiveTakeProfitRatio = normalizedRatios.protective
  const secondTakeProfitRatio = normalizedRatios.second
  const hasExtensionTarget = normalizedRatios.hasExtension && extensionTakeProfit !== null
  const firstTakeProfitRatio = normalizedRatios.first
  if (stopLoss <= 0 || firstTakeProfit <= 0) {
    ElMessage.warning('当前计划缺少有效止损或第一目标')
    return
  }
  // Ratios are normalized above, including legacy MODEL snapshots whose
  // extension cumulative boundary was stored as 100%. Keep only the basic
  // finite/order guard here; do not reject an otherwise usable old monitor.
  if (!Number.isFinite(protectiveTakeProfitRatio) || !Number.isFinite(firstTakeProfitRatio) || !Number.isFinite(secondTakeProfitRatio)) {
    ElMessage.warning('当前计划缺少有效止盈比例，请重新创建执行计划')
    return
  }
  try {
    await ElMessageBox.confirm(
      `${execution.symbol} 将把当前有效止损 ${formatPrice(stopLoss)} 设为仓位级全平（标记价条件止损）${protectiveTakeProfit !== null ? `；近端保护目标 ${formatPrice(protectiveTakeProfit)} 按总仓位 ${formatTargetPercent(protectiveTakeProfitRatio)} 数量止盈（最新价条件单）` : ''}；第一目标 ${formatPrice(firstTakeProfit)} 累计至总仓位 ${formatTargetPercent(firstTakeProfitRatio)}（本档 ${formatTargetPercent(firstTakeProfitRatio - (protectiveTakeProfit !== null ? protectiveTakeProfitRatio : 0))}）数量止盈（最新价条件单）${extensionTakeProfit !== null ? `；第二目标 ${formatPrice(extensionTakeProfit)} 累计至总仓位 ${formatTargetPercent(secondTakeProfitRatio)}（本档 ${formatTargetPercent(secondTakeProfitRatio - firstTakeProfitRatio)}）数量止盈（最新价条件单），最后剩余 ${formatTargetPercent(100 - secondTakeProfitRatio)} 继续由移动止损管理` : `；未确认第二平台，不设置第二目标，第一目标后的余仓继续由移动止损管理`}；仓位止盈保持为空（不设置仓位级止盈）。此操作会向 Binance 提交真实保护单。`,
      '确认应用计划点位到实际持仓',
      { type: 'warning', confirmButtonText: '确认提交保护单', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const protection = await applyBinanceFuturesPlanProtection({
      symbol: execution.symbol,
      positionSide: actual.positionSide || 'BOTH',
      stopLoss,
      protectiveTakeProfit,
      firstTakeProfit,
      extensionTakeProfit,
      protectiveTakeProfitRatio,
      firstTakeProfitRatio,
      secondTakeProfitRatio,
    })
    startSnapshotSubscription()
    if (protection?.fallback === 'FIRST_TARGET_FULL_QUANTITY') {
      ElMessage.warning('持仓数量无法拆分两档止盈，已在第一目标设置数量型全量止盈；扩展目标未设置，仓位止盈保持为空')
    } else if (protection?.fallback === 'EXTENSION_TARGET_HALF_POSITION') {
      ElMessage.warning('扩展目标的 25% 数量低于交易所最小单位，已按总仓位 50% 数量止盈')
    } else {
      ElMessage.success('计划点位已应用到实际合约持仓')
    }
  } catch (error) {
    ElMessage.error(marketErrorMessage(error))
  }
}

const loadSavedAccount = () => startSnapshotSubscription()

const removeSavedCredentials = async () => {
  try {
    await ElMessageBox.confirm('删除后需要重新输入 API Key 和 Secret Key 才能连接。', '删除已保存凭据', { type: 'warning', confirmButtonText: '删除凭据', cancelButtonText: '取消' })
  } catch {
    return
  }
  try {
    await deleteBinanceCredentials()
    credentialsProfile.value = emptyCredentialsProfile()
    applySavedConnectionSettings({ connectionMode: 'REST' })
    connectionStatus.value = null
    disconnectAccount()
    startSnapshotSubscription()
    ElMessage.success('已删除当前账户的 Binance 凭据')
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '凭据删除失败')
  }
}

// MODEL inference and live plan refresh are anchored to completed 5m bars.
// Keep the visible chart aligned with that same execution scale when the
// user switches engines, so the page does not appear to fall back to 15m.
watch(() => strategySettings.value.strategyEngine, (engine) => {
  if (String(engine || '').toUpperCase() !== 'MODEL' || interval.value === '5m') return
  interval.value = '5m'
  loadMarketKlines()
})

onMounted(() => {
  if (binancePageRef.value && !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
    pageIntroContext = gsap.context(() => {
      gsap.from('.binance-page-head', { autoAlpha: 0, y: -10, duration: 0.34, ease: 'power2.out' })
      gsap.from('.binance-content-grid > .binance-panel, .binance-account-drawer', {
        autoAlpha: 0,
        y: 14,
        duration: 0.4,
        stagger: 0.055,
        delay: 0.05,
        ease: 'power2.out',
        clearProps: 'transform,opacity,visibility',
      })
    }, binancePageRef.value)
  }
  if (typeof window !== 'undefined') {
    window.addEventListener('resize', scheduleFitExecutionTabSymbols)
    document.fonts?.ready?.then(() => fitExecutionTabSymbols())
  }
  if (typeof window !== 'undefined') window.addEventListener('binance-model-selected', handleModelSelectionEvent)
  if (typeof window !== 'undefined') accountDrawerTop.value = clampAccountDrawerTop(window.innerWidth <= 820 ? 360 : Math.round(window.innerHeight * 0.38))
  if (typeof window !== 'undefined') {
    monitorDurationNow.value = Date.now()
    monitorDurationTimer = window.setInterval(() => { monitorDurationNow.value = Date.now() }, 30000)
  }
  startSnapshotSubscription()
  if (props.currentUser) {
    void loadCopyTradingSettings({ silent: true })
    void loadStrategySettings({ silent: true })
    void loadConnectionSettings({ silent: true })
    void loadBinanceUiSettings({ silent: true })
    void loadModelRuns()
  }
})

watch(() => props.currentUser, (user, previousUser) => {
  if (user && (!previousUser || user.id !== previousUser.id)) {
    copyTradingSubscriptionSelectionVersion += 1
    copyTradingSubscriptionSelectionQueue = null
    copyTradingSubscriptionSelecting.value = false
    startSnapshotSubscription()
    copyTradingSettings.value = { topTraderId: '5132388877263187456', copyMultiplier: 1, copyMultipliers: {}, updatedAt: null }
    notificationSettings.value = { email: '' }
    notificationSettingsLoading.value = false
    notificationSettingsSaving.value = false
    copyTradingSubscriptions.value = []
    copyTradingFollows.value = []
    copyTradingFollowedPositionSnapshots.value = []
    copyTradingSubscriptionAvatarErrors.value = {}
    copyTradingSourceMarginValue.value = 0
    copyTradingSettingsLoading.value = false
    copyTradingSettingsSaving.value = false
    strategySettingsLoaded.value = false
    strategySettings.value = { ...STRATEGY_SETTINGS_DEFAULTS }
    savedStrategySettings.value = { ...STRATEGY_SETTINGS_DEFAULTS }
    connectionSettingsLoaded.value = false
    connectionSettings.value = normalizeConnectionSettings()
    connectionStatus.value = null
    futuresPositionSmoothing.value = 0.25
    futuresPositionSmoothingLoaded.value = false
    void loadCopyTradingSettings({ silent: true })
    void loadNotificationSettings({ silent: true })
    void loadStrategySettings({ silent: true })
    void loadConnectionSettings({ silent: true })
    void loadBinanceUiSettings({ silent: true })
    void loadModelRuns()
  }
  if (!user) {
    copyTradingSubscriptionSelectionVersion += 1
    copyTradingSubscriptionSelectionQueue = null
    copyTradingSubscriptionSelecting.value = false
    connected.value = false
    account.value = null
    accountSnapshotUpdatedAt.value = null
    accountSnapshotCheckedAt.value = null
    accountSnapshotStale.value = false
    accountSnapshotError.value = ''
    accountPositionPnlHistory.value = []
    credentialsProfile.value = emptyCredentialsProfile()
    apiKey.value = ''
    apiSecret.value = ''
    connectionSettingsLoaded.value = false
    connectionSettings.value = normalizeConnectionSettings()
    connectionSettingsLoading.value = false
    connectionSettingsSaving.value = false
    connectionStatus.value = null
    futuresPositionSmoothing.value = 0.25
    futuresPositionSmoothingLoaded.value = false
    accountSettingsVisible.value = false
    accountSettingsTab.value = 'connection'
    websocketTestKind.value = ''
    websocketTestResult.value = null
    websocketAccountTestResult.value = null
    strategySettingsLoaded.value = false
    strategySettings.value = { ...STRATEGY_SETTINGS_DEFAULTS }
    savedStrategySettings.value = { ...STRATEGY_SETTINGS_DEFAULTS }
    copyTradingSettings.value = { topTraderId: '5132388877263187456', copyMultiplier: 1, copyMultipliers: {}, updatedAt: null }
    notificationSettings.value = { email: '' }
    notificationSettingsLoading.value = false
    notificationSettingsSaving.value = false
    copyTradingSubscriptions.value = []
    copyTradingFollows.value = []
    copyTradingFollowedPositionSnapshots.value = []
    copyTradingSubscriptionAvatarErrors.value = {}
    copyTradingSourceMarginValue.value = 0
    copyTradingSettingsLoading.value = false
    copyTradingSettingsSaving.value = false
    simulatedPositions.value = []
    selectedExecutionPositionId.value = null
    simulatedPositionsError.value = ''
    startSnapshotSubscription()
  }
})

onUnmounted(() => {
  pageIntroContext?.revert()
  chartMorphTween?.kill()
  if (typeof window !== 'undefined') window.removeEventListener('resize', scheduleFitExecutionTabSymbols)

  if (typeof window !== 'undefined') window.removeEventListener('binance-model-selected', handleModelSelectionEvent)
  stopSnapshotSubscription()
  if (futuresAnalysisPollTimer) window.clearTimeout(futuresAnalysisPollTimer)
  if (monitorDurationTimer) window.clearInterval(monitorDurationTimer)
})
</script>

<style scoped>
.binance-page {
  --binance-gold: #7854c8;
  --binance-gold-soft: rgba(120, 84, 200, 0.12);
  --binance-ui-font: "LXGW WenKai Screen", var(--font-cjk);
  /* 盈亏数字与页面其他文案共用同一 UI 字体，避免数据区出现另一套字形。 */
  --font-pnl: var(--binance-ui-font);
  --binance-change-font: var(--binance-ui-font);
  --font-base: var(--binance-ui-font);
  --font-display: var(--binance-ui-font);
  --font-num: var(--binance-ui-font);
  --font-tab: var(--binance-ui-font);
  --font-mono: var(--binance-ui-font);
  --font-ui: var(--binance-ui-font);
  /* Direction/status colors keep their meaning; quote and PnL changes use
     explicit rise/fall colors so every percentage is red-up and green-down. */
  --binance-up: #0f9f73;
  --binance-down: #d85b68;
  --binance-rise: #d85b68;
  --binance-fall: #0f9f73;
  --binance-direction-long: #d85b68;
  --binance-direction-short: #0f9f73;
  max-width: 1880px;
  margin: 0 auto;
  color: var(--text-primary);
  font-family: var(--font-base);
  font-size: 14px;
  line-height: 1.5;
}

.animated-number {
  display: inline-block;
  font-variant-numeric: tabular-nums lining-nums;
  will-change: contents;
}

.binance-page-head,
.binance-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.binance-page-head { min-height: 58px; padding: 2px 2px 0; }
.binance-heading { display: flex; align-items: center; gap: 13px; min-width: 0; }
.binance-heading h1 { font-size: 27px; font-weight: 700; line-height: 1.25; letter-spacing: 0; }
.binance-heading p { margin-top: 5px; color: var(--text-muted); font-size: 13px; font-weight: 500; line-height: 1.5; }
.binance-brand-mark { display: grid; width: 46px; height: 46px; place-items: center; border: 1px solid color-mix(in srgb, var(--binance-gold) 52%, var(--border-line)); border-radius: 12px; background: var(--binance-gold-soft); color: var(--binance-gold); }
.binance-head-actions { display: flex; align-items: center; gap: 10px; }
.binance-network-indicator,
.binance-connection-state { display: inline-flex; align-items: center; gap: 7px; color: var(--text-muted); font-size: 12px; font-weight: 600; white-space: nowrap; }
.binance-network-indicator i,
.binance-connection-state i { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); }
.binance-network-indicator.is-mainnet i,
.binance-connection-state.is-connected i { background: var(--binance-up); }

.binance-up { color: var(--binance-up) !important; }
.binance-down { color: var(--binance-down) !important; }
.binance-rise { color: var(--binance-rise) !important; }
.binance-fall { color: var(--binance-fall) !important; }
.binance-direction-long { color: var(--binance-direction-long) !important; }
.binance-direction-short { color: var(--binance-direction-short) !important; }
.binance-neutral { color: var(--text-muted) !important; }

.binance-content-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); grid-template-areas: 'chart chart' 'search positions'; gap: 18px; margin-top: 18px; align-items: stretch; }
.binance-main-column { display: grid; gap: 14px; min-width: 0; }
.binance-panel { display: flex; min-width: 0; flex-direction: column; padding: 20px; border: 1px solid var(--border-line); border-radius: 8px; background: var(--panel-bg); box-shadow: var(--shadow-panel); transition: border-color 220ms ease, box-shadow 280ms cubic-bezier(.22, 1, .36, 1), transform 280ms cubic-bezier(.22, 1, .36, 1); }
.binance-content-grid > .binance-panel:hover { border-color: color-mix(in srgb, var(--binance-gold) 34%, var(--border-line)); box-shadow: 0 12px 30px rgba(27, 43, 58, .12); }
.binance-chart-stage { position: sticky; top: 78px; z-index: 20; grid-area: chart; align-self: start; }
.binance-chart-collapse-toggle { display: none; }
.binance-search-panel { grid-area: search; align-self: stretch; min-height: 420px; }
.binance-simulated-panel { min-height: 420px; }
.binance-smart-money-panel { grid-column: 1 / -1; min-width: 0; }
.binance-smart-money-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); grid-template-areas: 'positions records'; gap: 18px; min-width: 0; margin-top: 16px; }
.binance-copy-trading-panel { grid-area: records; min-width: 0; }
.binance-copy-trading-head { align-items: flex-start; }
.binance-copy-trading-controls { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; min-width: 0; }
.binance-copy-trader-list { display: flex; flex-wrap: wrap; align-items: center; justify-content: flex-start; gap: 5px; color: var(--text-faint); font-size: 10px; font-weight: 800; }
.binance-copy-trader-list-item { display: inline-flex; align-items: baseline; gap: 5px; max-width: 220px; padding: 3px 7px; border: 1px solid var(--border-line); border-radius: 4px; color: var(--text-muted); background: var(--panel-muted); cursor: pointer; font: inherit; }
.binance-copy-trader-list-item:hover,
.binance-copy-trader-list-item.is-active { border-color: var(--binance-gold); color: var(--text-primary); }
.binance-copy-trader-list-item strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.binance-copy-trader-list-item small { color: var(--text-faint); font-family: var(--font-tab); font-size: 9px; font-weight: 700; }
.binance-copy-trader-subscription-section { display: grid; gap: 6px; margin-top: 10px; min-width: 0; }
.binance-copy-trader-list-title { color: var(--text-faint); font-size: 10px; font-weight: 800; }
.binance-copy-trader-subscriptions { align-items: stretch; min-width: 0; }
.binance-copy-trader-subscription-item { flex: 0 0 270px; width: 270px; max-width: 270px; align-items: flex-start; padding: 8px 10px; text-align: left; }
.binance-copy-trader-subscription-item img,
.binance-copy-trader-subscription-avatar-fallback { flex: 0 0 42px; width: 42px; height: 42px; border-radius: 50%; object-fit: cover; background: var(--panel-solid); }
.binance-copy-trader-subscription-avatar-fallback { display: inline-grid; place-items: center; border: 1px solid var(--binance-gold); color: var(--binance-gold); font-family: var(--font-tab); font-size: 16px; font-weight: 900; }
.binance-copy-trader-subscription-content { display: grid; min-width: 0; gap: 3px; }
.binance-copy-trader-subscription-content > strong { color: var(--text-primary); font-size: 13px; }
.binance-copy-trader-subscription-content > small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.binance-copy-trader-subscription-stats { display: flex; flex-wrap: wrap; gap: 4px 9px; color: var(--text-faint); font-size: 10px; line-height: 1.3; }
.binance-copy-trader-subscription-stats b { font-family: var(--font-pnl); font-size: 11px; }
.binance-copy-trading-multiplier { display: grid; justify-self: start; gap: 5px; color: var(--text-muted); }
.binance-copy-trading-multiplier-label { color: var(--text-muted); font-size: 11px; font-weight: 800; line-height: 1; }
.binance-copy-trading-multiplier-control { display: inline-flex; align-items: center; gap: 9px; }
.binance-copy-trading-multiplier-step { display: inline-grid; width: 28px; height: 28px; place-items: center; flex: 0 0 28px; padding: 0; border: 1px solid var(--border-line); border-radius: 50%; background: var(--panel-solid); color: var(--brand); cursor: pointer; transition: color .18s ease, border-color .18s ease, background .18s ease, box-shadow .18s ease, transform .18s ease; }
.binance-copy-trading-multiplier-step:hover:not(:disabled) { border-color: color-mix(in srgb, var(--brand) 66%, var(--border-line)); background: color-mix(in srgb, var(--brand) 10%, var(--panel-solid)); box-shadow: 0 2px 7px color-mix(in srgb, var(--brand) 18%, transparent); transform: translateY(-1px); }
.binance-copy-trading-multiplier-step:focus-visible { outline: 2px solid color-mix(in srgb, var(--brand) 56%, transparent); outline-offset: 2px; }
.binance-copy-trading-multiplier-step:disabled { cursor: not-allowed; opacity: .38; }
.binance-copy-trading-multiplier-value { width: 42px; min-height: 26px; padding: 0 1px; border: 0; border-bottom: 2px solid color-mix(in srgb, var(--brand) 58%, var(--border-line)); border-radius: 0; outline: 0; background: transparent; color: var(--text-primary); font-family: var(--font-pnl); font-size: 22px; font-weight: 900; font-variant-numeric: tabular-nums; line-height: 1; text-align: center; appearance: textfield; }
.binance-copy-trading-multiplier-value:hover { border-bottom-color: var(--brand); }
.binance-copy-trading-multiplier-value:focus { border-bottom-color: var(--brand); box-shadow: 0 3px 0 -1px color-mix(in srgb, var(--brand) 28%, transparent); }
.binance-copy-trading-multiplier-value:disabled { color: var(--text-muted); cursor: not-allowed; }
.binance-copy-trading-multiplier-unit { margin-left: -6px; margin-right: 1px; align-self: end; padding-bottom: 3px; color: var(--text-muted); font-family: var(--font-tab); font-size: 13px; font-weight: 800; line-height: 1; }
.binance-copy-trading-multiplier-value::-webkit-inner-spin-button,
.binance-copy-trading-multiplier-value::-webkit-outer-spin-button { margin: 0; appearance: none; }
.binance-copy-trading-updated { color: var(--text-faint); font-size: 11px; }
.binance-copy-trading-list { display: grid; gap: 8px; max-height: 430px; overflow-y: auto; padding-right: 4px; }
.binance-smart-positions-panel { grid-area: positions; min-width: 0; }
.binance-smart-money-subhead { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; min-height: 39px; margin-bottom: 8px; }
.binance-smart-money-subhead h3 { margin: 0; color: var(--text-primary); font-size: 17px; font-weight: 900; line-height: 1.25; }
.binance-smart-positions-workspace { display: grid; gap: 8px; min-width: 0; }
.binance-smart-position-tabs { display: flex; flex-wrap: nowrap; gap: 6px; min-width: 0; max-height: none; overflow-x: auto; overflow-y: hidden; padding: 1px 4px 7px 1px; scrollbar-width: thin; }
.binance-smart-position-tab { display: grid; flex: 0 0 154px; width: 154px; gap: 5px; min-width: 0; padding: 8px 9px; border: 1px solid var(--border-line); border-radius: 6px; background: var(--panel-muted); color: var(--text-secondary); cursor: pointer; text-align: left; transition: border-color .2s ease, background .2s ease, box-shadow .2s ease, transform .24s cubic-bezier(.22, 1, .36, 1); }
.binance-smart-position-tab:hover,
.binance-smart-position-tab.is-active { border-color: color-mix(in srgb, var(--binance-gold) 62%, var(--border-line)); background: color-mix(in srgb, var(--binance-gold) 7%, var(--panel-muted)); }
.binance-smart-position-tab.is-active { box-shadow: 0 0 0 1px color-mix(in srgb, var(--binance-gold) 24%, transparent); }
.binance-smart-position-tab.is-copying { border-color: color-mix(in srgb, var(--binance-up) 70%, var(--border-line)); }
.binance-smart-position-tab.is-same-car { border-color: color-mix(in srgb, var(--binance-gold) 70%, var(--border-line)); }
.binance-smart-position-tab.is-value-alert { border-color: var(--binance-down); box-shadow: 0 0 0 1px color-mix(in srgb, var(--binance-down) 35%, transparent); }
.binance-smart-position-tab-head { display: flex; align-items: center; gap: 6px; min-width: 0; }
.binance-smart-position-tab-head strong { min-width: 0; overflow: hidden; color: var(--text-primary); font-family: var(--font-tab); font-size: 12px; font-weight: 900; text-overflow: ellipsis; white-space: nowrap; }
.binance-smart-position-tab-head em { flex: 0 0 auto; padding: 2px 5px; border-radius: 3px; background: color-mix(in srgb, var(--binance-gold) 16%, transparent); color: var(--binance-gold); font-size: 9px; font-style: normal; font-weight: 900; line-height: 1.2; }
.binance-smart-position-tab-head em.is-following { background: color-mix(in srgb, var(--binance-up) 16%, transparent); color: var(--binance-up); }
.binance-smart-position-tab-head em.is-same-car { background: color-mix(in srgb, var(--binance-gold) 16%, transparent); color: var(--binance-gold); }
.binance-smart-position-tab-head em.is-alert { background: color-mix(in srgb, var(--binance-down) 14%, transparent); color: var(--binance-down); }
.binance-smart-position-tab-pnl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; min-width: 0; }
.binance-smart-position-tab-pnl > span { display: grid; gap: 2px; min-width: 0; }
.binance-smart-position-tab-pnl small { color: var(--text-faint); font-size: 9px; font-weight: 800; line-height: 1.1; }
.binance-smart-position-tab-pnl strong { overflow-wrap: anywhere; font-family: var(--font-pnl); font-size: 11px; font-weight: 900; line-height: 1.15; }
.binance-smart-position-item { min-width: 0; padding: 11px 12px; border: 1px solid var(--border-line); background: var(--panel-muted); overflow: hidden; transition: border-color .2s ease, box-shadow .2s ease; }
.binance-smart-position-item.is-copying { border-color: color-mix(in srgb, var(--binance-up) 65%, var(--border-line)); box-shadow: 0 0 0 1px color-mix(in srgb, var(--binance-up) 30%, transparent); }
.binance-smart-position-item.is-same-car { border-color: color-mix(in srgb, var(--binance-gold) 65%, var(--border-line)); box-shadow: 0 0 0 1px color-mix(in srgb, var(--binance-gold) 24%, transparent); }
.binance-smart-position-head { display: flex; align-items: flex-start; min-width: 0; margin: -11px -12px 8px; padding: 9px 10px 8px; overflow: hidden; }
.binance-smart-symbol-link { min-width: 0; max-width: 100%; padding: 0; border: 0; background: transparent; color: var(--text-primary); cursor: pointer; font: inherit; font-family: var(--font-tab); font-weight: 900; text-align: left; text-decoration: none; text-overflow: ellipsis; white-space: nowrap; }
.binance-smart-symbol-link:hover { color: var(--binance-gold); text-decoration: underline; }
.binance-smart-position-grid { display: grid; grid-template-columns: minmax(58px, .52fr) minmax(0, 1fr) minmax(0, 1fr); min-width: 0; }
.binance-smart-position-detail { display: grid; grid-template-columns: minmax(0, 1fr) minmax(220px, 1fr); gap: 16px; align-items: stretch; min-width: 0; }
.binance-smart-position-main { min-width: 0; }
.binance-smart-grid-spacer { min-width: 0; }
.binance-smart-grid-title,
.binance-smart-grid-pnl,
.binance-smart-grid-value { min-width: 0; padding-left: 10px; }
.binance-smart-grid-title.is-own,
.binance-smart-grid-pnl.is-own,
.binance-smart-grid-value.is-own { padding-right: 1px; border-left: 1px solid var(--border-line); }
.binance-smart-grid-title { padding-top: 7px; padding-bottom: 6px; color: var(--text-muted); font-size: 12px; font-weight: 900; line-height: 1.2; text-align: center; }
.binance-smart-grid-title.is-profit,
.binance-smart-grid-pnl.is-profit { background: color-mix(in srgb, var(--binance-rise) 13%, transparent); }
.binance-smart-grid-title.is-loss,
.binance-smart-grid-pnl.is-loss { background: color-mix(in srgb, var(--binance-fall) 13%, transparent); }
.binance-smart-grid-title.is-neutral,
.binance-smart-grid-pnl.is-neutral { background: color-mix(in srgb, var(--text-muted) 8%, transparent); }
.binance-smart-grid-pnl { display: grid; gap: 2px; padding-top: 5px; padding-bottom: 9px; }
.binance-smart-grid-pnl > strong { overflow-wrap: anywhere; font-family: var(--font-pnl); font-size: 17px; font-weight: 900; line-height: 1.15; white-space: normal; word-break: break-word; }
.binance-smart-grid-pnl > small { overflow-wrap: anywhere; font-family: var(--font-pnl); font-size: 11px; font-weight: 800; line-height: 1.2; }
.binance-smart-grid-label { align-self: center; min-width: 0; padding: 8px 3px 8px 0; color: var(--text-muted); font-size: 10px; font-weight: 800; line-height: 1.2; }
.binance-smart-grid-value { display: grid; align-content: center; gap: 2px; padding-top: 8px; padding-bottom: 8px; }
.binance-smart-grid-value strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-pnl); font-size: 12px; font-weight: 900; line-height: 1.25; word-break: break-word; }
.binance-smart-grid-value small { overflow-wrap: anywhere; color: var(--text-faint); font-size: 9px; font-weight: 700; line-height: 1.2; }
.binance-smart-grid-value.is-value-alert { box-shadow: inset 0 0 0 1px var(--binance-down); }
.binance-smart-position-actions { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-top: 9px; }
.binance-smart-position-side { display: flex; min-width: 0; flex-direction: column; justify-content: center; gap: 12px; padding-left: 14px; border-left: 1px solid var(--border-line); }
.binance-smart-position-side .binance-smart-position-actions { display: grid; gap: 8px; margin-top: 0; }
.binance-smart-position-side .binance-smart-position-actions .el-button { width: 100%; margin-left: 0; }
.binance-smart-position-deviation { display: grid; gap: 5px; min-width: 0; padding: 9px 10px; border-left: 3px solid var(--border-line); background: color-mix(in srgb, var(--panel-muted) 88%, transparent); color: var(--text-muted); font-size: 10px; font-weight: 800; line-height: 1.35; }
.binance-smart-position-deviation > span { color: var(--text-primary); font-size: 11px; font-weight: 900; }
.binance-smart-position-deviation > strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-pnl); font-size: 12px; font-weight: 900; }
.binance-smart-position-deviation > strong.is-alert,
.binance-smart-position-deviation > small.is-alert { color: var(--binance-down); }
.binance-smart-position-deviation > small { overflow-wrap: anywhere; color: var(--text-muted); font-size: 10px; }
.binance-smart-position-mark { min-width: 0; color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-smart-position-mark strong { color: var(--text-primary); font-family: var(--font-pnl); font-size: 12px; font-weight: 900; }
.binance-smart-follow-account-info { display: grid; grid-column: 1 / -1; gap: 3px; min-width: 0; width: 100%; margin-top: 8px; padding: 7px 9px; border-left: 3px solid var(--binance-gold); background: color-mix(in srgb, var(--binance-gold) 7%, transparent); color: var(--text-muted); font-size: 10px; font-weight: 800; line-height: 1.35; }
.binance-smart-follow-account-source { display: grid; gap: 3px; min-width: 0; padding: 3px 0; }
.binance-smart-follow-account-source + .binance-smart-follow-account-source { padding-top: 7px; border-top: 1px solid color-mix(in srgb, var(--border-line) 75%, transparent); }
.binance-smart-follow-account-source > div { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 8px; }
.binance-smart-follow-account-source > div > span { color: var(--text-primary); font-size: 12px; font-weight: 900; }
.binance-smart-follow-account-source > div > strong { justify-self: start; min-width: 0; overflow: hidden; text-align: left; text-overflow: ellipsis; white-space: nowrap; }
.binance-smart-follow-account-info strong { color: var(--text-primary); font-size: 11px; }
.binance-smart-follow-account-info small { overflow-wrap: anywhere; }
.binance-smart-follow-account-info small.is-warning { color: var(--binance-gold); }
.binance-smart-follow-account-info .binance-smart-follow-account-value { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-width: 0; color: var(--text-primary); font-family: var(--font-pnl); font-size: 12px; font-weight: 900; line-height: 1.4; }
.binance-smart-follow-account-info .binance-smart-follow-account-value > span { min-width: 0; overflow-wrap: anywhere; }
.binance-smart-follow-account-info .binance-smart-follow-account-value > em { flex: 0 0 auto; font-size: 11px; font-style: normal; white-space: nowrap; }
.binance-smart-follow-account-info .binance-smart-follow-account-value.is-value-alert { padding: 2px 5px; box-shadow: inset 0 0 0 1px var(--binance-down); color: var(--binance-down); }
.binance-smart-copying-state { font-size: 10px; font-weight: 900; }
.binance-smart-copying-state.is-following { color: var(--binance-up); }
.binance-smart-copying-state.is-same-car { color: var(--binance-gold); }
.binance-smart-own-entry { color: var(--text-muted); }
.binance-smart-own-entry.is-better { color: var(--binance-down); }
.binance-smart-own-entry.is-worse { color: var(--binance-up); }
.binance-copy-trading-item { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-width: 0; padding: 11px 12px; border: 1px solid var(--border-line); background: var(--panel-muted); }
.binance-copy-trading-main { min-width: 0; }
.binance-copy-trading-title-row { display: flex; align-items: center; gap: 8px; min-width: 0; }
.binance-copy-trading-title-row > strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--font-tab); font-size: 14px; }
.binance-copy-action { display: inline-flex; flex: 0 0 auto; align-items: center; padding: 2px 6px; border-radius: 4px; font-family: var(--font-tab); font-size: 10px; font-weight: 900; line-height: 1.2; }
.binance-copy-action.is-open-long { background: color-mix(in srgb, var(--binance-rise) 16%, transparent); color: var(--binance-rise); }
.binance-copy-action.is-close-long { background: color-mix(in srgb, var(--binance-gold) 18%, transparent); color: var(--binance-gold); }
.binance-copy-action.is-open-short { background: color-mix(in srgb, var(--binance-fall) 16%, transparent); color: var(--binance-fall); }
.binance-copy-action.is-close-short { background: color-mix(in srgb, #0891b2 16%, transparent); color: #0891b2; }
.binance-copy-trading-order-type { color: var(--text-faint); font-size: 10px; font-weight: 800; }
.binance-copy-trading-meta { display: flex; flex-wrap: wrap; gap: 5px 12px; margin-top: 6px; color: var(--text-muted); font-size: 11px; line-height: 1.35; }
.binance-copy-trading-stale { margin: 9px 0 0; color: var(--binance-gold); font-size: 11px; }
.binance-copy-trade-context { display: grid; gap: 5px; margin-bottom: 14px; padding: 11px 12px; border-left: 3px solid var(--binance-gold); background: var(--panel-muted); }
.binance-copy-trade-context > div { display: flex; align-items: center; gap: 8px; }
.binance-copy-trade-context strong { font-family: var(--font-tab); font-size: 16px; }
.binance-copy-trade-context small { color: var(--text-muted); font-size: 11px; }
.binance-copy-trade-ratio-row { grid-column: 1 / -1; }
.binance-copy-trade-warning { margin: 0; padding: 9px 11px; border: 1px solid color-mix(in srgb, var(--binance-gold) 42%, var(--border-line)); background: color-mix(in srgb, var(--binance-gold) 8%, transparent); color: var(--text-muted); font-size: 11px; line-height: 1.5; }
.binance-chart-stage-head { align-items: center; }
.binance-chart-stage-head > div:first-child { min-width: 0; }
.binance-chart-stage .binance-market-chart-panel { flex: 1 1 auto; min-height: 0; margin-top: 0; }
.binance-chart-layout { min-width: 0; }
.binance-chart-main { min-width: 0; }
.binance-chart-shell { position: relative; min-width: 0; }
.binance-chart-periods { min-width: 0; }
.binance-search-empty { display: flex; align-items: center; gap: 8px; min-height: 120px; margin-top: 16px; padding: 14px; border: 1px dashed var(--border-line); color: var(--text-muted); font-size: 12px; font-weight: 700; line-height: 1.45; }
.binance-search-empty svg { flex: 0 0 auto; color: var(--binance-gold); }
.binance-panel-head { min-height: 32px; }
.binance-panel-kicker { display: block; margin-bottom: 3px; color: var(--text-faint); font-family: var(--font-base); font-size: 10px; font-weight: 600; letter-spacing: .07em; text-transform: uppercase; }
.binance-panel h2,
.binance-panel h3 { color: var(--text-primary); font-size: 17px; font-weight: 700; line-height: 1.35; }
.binance-market-head { align-items: flex-start; }
.binance-market-head > div:first-child { min-width: 132px; }
.binance-market-actions { display: grid; flex: 0 1 640px; grid-template-columns: minmax(0, 1fr); align-items: center; justify-content: end; gap: 8px; min-width: 0; }
.binance-market-target-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; justify-content: flex-start; gap: 6px; width: 100%; min-width: 0; }
.binance-market-scan-row { display: flex; align-items: center; justify-content: flex-start; flex-wrap: wrap; gap: 8px; width: 100%; min-width: 0; }
.binance-market-target-row,
.binance-market-scan-row { justify-content: flex-start; width: 100%; }
.binance-market-target-row { gap: 6px; }
.binance-market-scan-row { gap: 4px; }
.binance-market-scan-row :deep(.el-button + .el-button) { margin-left: 0; }
.binance-market-target-row > :deep(.binance-market-search) { width: 100%; min-width: 0; }
.binance-market-suggestion { display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%; }
.binance-market-suggestion strong { color: var(--text-primary); font-family: var(--font-tab); font-size: 12px; font-weight: 900; }
.binance-market-suggestion span { color: var(--text-muted); font-family: var(--font-pnl); font-size: 10px; font-weight: 800; }
.binance-market-suggestion em { font-style: normal; }
.binance-market-workspace { display: grid; grid-template-columns: minmax(390px, .95fr) minmax(0, 1.25fr); gap: 18px; margin-top: 15px; }
.binance-market-list-wrap,
.binance-market-chart-panel { min-width: 0; }
.binance-market-selection-hint { display: flex; align-items: flex-start; gap: 10px; margin-top: 24px; padding: 14px; border: 1px dashed var(--border-line); color: var(--text-muted); }
.binance-market-selection-hint svg { flex: 0 0 auto; margin-top: 1px; color: var(--binance-gold); }
.binance-market-selection-hint div { display: grid; gap: 4px; min-width: 0; }
.binance-market-selection-hint strong { color: var(--text-primary); font-size: 13px; font-weight: 900; }
.binance-market-selection-hint span { color: var(--text-faint); font-size: 11px; font-weight: 700; line-height: 1.45; }
.binance-selected-market-card { margin-top: 12px; padding: 14px; border: 1px solid var(--border-line); background: var(--panel-muted); }
.binance-selected-market-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; padding-bottom: 12px; border-bottom: 1px solid var(--border-subtle); }
.binance-selected-market-head div { display: grid; gap: 3px; min-width: 0; }
.binance-selected-market-head div span { color: var(--text-faint); font-size: 10px; font-weight: 800; }
.binance-selected-market-head strong { color: var(--text-primary); font-family: var(--font-tab); font-size: 19px; font-weight: 900; }
.binance-selected-market-head > span { font-family: var(--font-pnl); font-size: 14px; font-weight: 900; }
.binance-selected-market-stats { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; padding-top: 13px; }
.binance-selected-market-stats div { display: grid; gap: 4px; min-width: 0; }
.binance-selected-market-stats span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-selected-market-stats strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-pnl); font-size: 12px; font-weight: 900; line-height: 1.35; word-break: break-word; }
.binance-market-chart-panel { margin-top: 21px; padding-left: 0; }
.binance-market-meta { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-top: 9px; color: var(--text-faint); font-family: var(--font-tab); font-size: 10px; font-weight: 800; }

.binance-market-stale { display: flex; justify-content: flex-end; margin-top: 10px; color: var(--binance-gold); font-size: 11px; font-weight: 800; }
.binance-chart-shell > .binance-market-stale.is-connection-warning { position: absolute; top: 8px; right: 8px; z-index: 4; max-width: min(78%, 560px); margin: 0; padding: 5px 8px; border: 1px solid color-mix(in srgb, var(--binance-gold) 42%, var(--border-line)); background: color-mix(in srgb, var(--panel-solid) 88%, transparent); color: var(--binance-gold); font-size: 10px; line-height: 1.35; text-align: right; overflow-wrap: anywhere; pointer-events: none; }

.binance-market-analysis { margin-top: 20px; padding-top: 18px; border-top: 1px solid var(--border-line); }
.binance-simulated-panel { grid-area: positions; margin-top: 0; align-self: stretch; }
.binance-simulated-workspace { display: flex; align-items: flex-start; gap: 14px; margin-top: 16px; }
.binance-simulated-workspace > .binance-simulated-item { flex: 1 1 auto; min-width: 0; }
.binance-execution-tabs { display: flex; flex: 0 0 auto; flex-direction: column; align-items: flex-start; gap: 8px; min-width: 0; padding: 2px; }
.binance-execution-tab { --execution-tab-accent: var(--binance-gold); position: relative; display: flex; flex-direction: column; justify-content: center; gap: 5px; width: 172px; min-width: 172px; max-width: 172px; padding: 9px 12px 9px 17px; border: 1px solid color-mix(in srgb, var(--execution-tab-accent) 26%, var(--border-line)); border-radius: 12px; background: radial-gradient(130% 90% at 100% 0%, color-mix(in srgb, var(--execution-tab-accent) 12%, transparent) 0%, transparent 55%), linear-gradient(180deg, color-mix(in srgb, var(--panel-solid) 97%, transparent), var(--panel-solid)); color: var(--text-secondary); box-shadow: 0 2px 6px rgba(16, 42, 67, 0.08); cursor: pointer; text-align: left; overflow: hidden; transition: border-color 0.25s var(--ease-out-soft, ease), box-shadow 0.25s var(--ease-out-soft, ease), transform 0.25s var(--ease-out-soft, ease), background 0.25s ease; }
.binance-execution-tab::before { content: ''; position: absolute; inset: 0 auto 0 0; width: 3px; background: linear-gradient(180deg, var(--execution-tab-accent), color-mix(in srgb, var(--execution-tab-accent) 30%, transparent)); opacity: .55; transition: opacity .25s ease, width .25s ease, box-shadow .25s ease; }
.binance-execution-tab::after { content: ''; position: absolute; top: 0; right: 0; width: 56px; height: 56px; background: radial-gradient(closest-side, color-mix(in srgb, var(--execution-tab-accent) 16%, transparent), transparent 72%); opacity: .5; pointer-events: none; transition: opacity .25s ease; }
.binance-execution-tab:hover { border-color: color-mix(in srgb, var(--execution-tab-accent) 50%, var(--border-line)); box-shadow: 0 8px 22px color-mix(in srgb, var(--execution-tab-accent) 15%, rgba(16, 42, 67, 0.18)); }
.binance-execution-tab:hover::before { opacity: .95; width: 4px; }
.binance-execution-tab:hover::after { opacity: .85; }
.binance-execution-tab.is-active { border-color: color-mix(in srgb, var(--execution-tab-accent) 58%, var(--border-line)); background: radial-gradient(130% 100% at 100% 0%, color-mix(in srgb, var(--execution-tab-accent) 20%, transparent) 0%, transparent 58%), linear-gradient(180deg, color-mix(in srgb, var(--execution-tab-accent) 7%, var(--panel-solid)), var(--panel-solid)); box-shadow: 0 0 0 1px color-mix(in srgb, var(--execution-tab-accent) 24%, transparent), 0 10px 26px color-mix(in srgb, var(--execution-tab-accent) 16%, rgba(16, 42, 67, 0.2)); }
.binance-execution-tab.is-active::before { opacity: 1; width: 4px; box-shadow: 0 0 10px color-mix(in srgb, var(--execution-tab-accent) 55%, transparent); }
.binance-execution-tab.is-active::after { opacity: .9; }
.binance-execution-tab-head { display: flex; align-items: center; justify-content: space-between; gap: 9px; min-width: 0; max-width: 100%; }
.binance-execution-tab > .binance-execution-tab-head { overflow: hidden; }
.binance-execution-tab > .binance-execution-tab-head .binance-execution-tab-symbol { display: flex; align-items: baseline; min-width: 0; color: var(--text-primary); font-family: var(--font-tab); letter-spacing: .02em; white-space: nowrap; }
.binance-execution-tab-symbol strong { font-size: 16px; font-weight: 900; }
.binance-execution-tab-head > em { flex: 0 0 auto; padding: 2px 6px; border-radius: 4px; font-size: 9.5px; font-style: normal; font-weight: 900; letter-spacing: .08em; line-height: 1.2; }
.binance-execution-tab-head > em.binance-direction-long { background: color-mix(in srgb, var(--binance-direction-long) 16%, transparent); }
.binance-execution-tab-head > em.binance-direction-short { background: color-mix(in srgb, var(--binance-direction-short) 16%, transparent); }
.binance-execution-tab-summary { display: flex; align-items: center; justify-content: space-between; gap: 6px; min-width: 0; max-width: 100%; }
.binance-execution-tab-pnl { display: grid; gap: 2px; min-width: 0; overflow: hidden; font-family: var(--font-pnl); line-height: 1.05; }
.binance-execution-tab-pnl > strong { overflow: hidden; font-size: 17px; font-weight: 900; text-overflow: ellipsis; white-space: nowrap; }
.binance-execution-tab-pnl > small { overflow: hidden; font-size: 10px; font-weight: 800; text-overflow: ellipsis; white-space: nowrap; }
.binance-execution-tab-summary > .binance-simulated-status { flex: 0 0 auto; }
.binance-execution-tab .binance-waiting-entry-gap { color: var(--binance-gold); }
.binance-execution-tab.is-executing { --execution-tab-accent: var(--binance-up); }
.binance-execution-tab.is-pending_entry { --execution-tab-accent: var(--binance-gold); }
.binance-execution-tab.is-stopped { --execution-tab-accent: var(--binance-down); }
.binance-execution-tab.is-watch { --execution-tab-accent: var(--binance-gold); }
.binance-execution-tab.is-active { border-color: var(--execution-tab-accent); background: linear-gradient(145deg, color-mix(in srgb, var(--execution-tab-accent) 12%, var(--panel-solid)), var(--panel-solid)); box-shadow: 0 0 0 2px color-mix(in srgb, var(--execution-tab-accent) 18%, transparent), 0 8px 20px color-mix(in srgb, var(--text-primary) 10%, transparent); }
.binance-execution-tab.is-active::before { background: var(--execution-tab-accent); opacity: 1; }
.binance-simulated-auth-gate { min-height: 190px; }
.binance-simulated-empty { display: flex; align-items: center; justify-content: center; gap: 8px; min-height: 136px; color: var(--text-faint); font-size: 12px; font-weight: 800; }
.binance-simulated-empty.is-error { color: var(--binance-down); }
.binance-simulated-empty.compact { min-height: 72px; justify-content: flex-start; }
.binance-simulated-list { display: grid; gap: 10px; margin-top: 16px; }
.binance-simulated-item { padding: 14px; border: 1px solid var(--border-line); background: var(--panel-muted); }
.binance-simulated-item-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding-bottom: 11px; border-bottom: 1px solid var(--border-subtle); }
.binance-simulated-symbol { min-width: 0; }
.binance-simulated-symbol-main { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; min-width: 0; }
.binance-simulated-symbol-main strong { overflow: hidden; color: var(--text-primary); font-family: var(--font-tab); font-size: 21px; font-weight: 900; letter-spacing: .02em; text-overflow: ellipsis; white-space: nowrap; }
.binance-simulated-item-actions { display: flex; align-items: center; gap: 5px; flex: 0 0 auto; }
.binance-simulated-item-actions :deep(.binance-monitor-action) { min-height: 30px; height: 30px; padding: 0 9px; border: 1px solid var(--border-line); border-radius: 4px; background: transparent !important; color: var(--text-secondary) !important; box-shadow: none !important; }
.binance-simulated-item-actions :deep(.binance-monitor-action:hover),
.binance-simulated-item-actions :deep(.binance-monitor-action:focus-visible) { border-color: var(--text-secondary); background: var(--panel-muted) !important; color: var(--text-primary) !important; }
.binance-simulated-item-actions :deep(.binance-monitor-action.is-disabled),
.binance-simulated-item-actions :deep(.binance-monitor-action:disabled) { border-color: var(--border-line); background: transparent !important; color: var(--text-faint) !important; opacity: .65; }
.binance-simulated-item-actions :deep(.el-button + .el-button) { margin-left: 0; }
.binance-simulated-side { display: inline-flex; min-height: 24px; align-items: center; padding: 0; border: 0; background: transparent; font-family: var(--font-tab); font-size: 12px; font-weight: 900; line-height: 1; white-space: nowrap; }
.binance-simulated-status { box-sizing: border-box; display: inline-flex; min-height: 24px; height: 24px; align-items: center; padding: 0 8px; border: 1px solid var(--border-line); font-family: var(--font-tab); font-size: 11px; font-weight: 900; line-height: 1; white-space: nowrap; }
.binance-simulated-side.is-long { color: var(--binance-direction-long); }
.binance-simulated-side.is-short { color: var(--binance-direction-short); }
.binance-simulated-market-error { display: flex; align-items: center; gap: 7px; min-height: 90px; color: var(--binance-down); font-size: 12px; font-weight: 800; }
.binance-simulated-pnl-metric { display: grid; align-content: center; gap: 4px; min-width: 0; }
.binance-simulated-pnl-value { display: block; min-width: 0; overflow: visible; font-family: var(--font-pnl); letter-spacing: .01em; line-height: 1.1; font-variant-numeric: tabular-nums; }
.binance-simulated-pnl-metric .binance-pnl-percent { display: block; overflow-wrap: anywhere; font-size: clamp(27px, 2.25vw, 34px) !important; font-weight: 900 !important; line-height: 1.1; }
.binance-simulated-pnl-metric .binance-pnl-amount { display: block; margin-top: 4px; color: inherit; font-size: 12px !important; font-weight: 800 !important; line-height: 1.2; white-space: normal; }
.binance-simulated-metrics { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 14px; align-items: stretch; padding: 14px 0; border-bottom: 1px solid var(--border-subtle); }
.binance-simulated-metrics > div,
.binance-simulated-levels > div { display: grid; gap: 4px; min-width: 0; }
.binance-simulated-metrics span,
.binance-simulated-levels span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-simulated-metrics strong { overflow: hidden; color: var(--text-primary); font-family: var(--font-pnl); font-size: 15px; font-weight: 900; text-overflow: ellipsis; white-space: nowrap; }
.binance-simulated-metrics > .binance-simulated-pnl-metric .binance-simulated-pnl-value { overflow: visible; text-overflow: clip; }
.binance-simulated-metrics strong small { font-size: 10px; }
.binance-simulated-metrics > .binance-simulated-pnl-metric .binance-pnl-amount { font-size: 12px !important; }
.binance-simulated-price-pair { grid-column: 2; }
.binance-simulated-price-pair strong { overflow-wrap: anywhere; white-space: normal; }
.binance-simulated-metrics > .binance-waiting-entry-gap strong,
.binance-simulated-metrics > .binance-waiting-entry-gap strong small { color: var(--binance-gold); }
.binance-simulated-plan-bar { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; padding: 10px 0; color: var(--text-muted); font-size: 11px; font-weight: 800; }
.binance-simulated-plan-bar strong { color: var(--text-primary); font-family: var(--font-num); }
.binance-real-protection-sync { display: flex; align-items: flex-start; gap: 6px; margin: 0 0 10px; padding: 8px 9px; border-left: 3px solid var(--binance-up); background: color-mix(in srgb, var(--binance-up) 6%, var(--panel-solid)); color: var(--text-muted); font-size: 10px; font-weight: 800; line-height: 1.4; }
.binance-real-protection-sync svg { flex: 0 0 auto; color: var(--binance-up); }
.binance-real-protection-sync.is-waiting { border-left-color: var(--binance-gold); background: color-mix(in srgb, var(--binance-gold) 6%, var(--panel-solid)); }
.binance-real-protection-sync.is-waiting svg { color: var(--binance-gold); }
.binance-real-protection-sync.is-error { border-left-color: var(--binance-down); background: color-mix(in srgb, var(--binance-down) 6%, var(--panel-solid)); }
.binance-real-protection-sync.is-error svg { color: var(--binance-down); }
.binance-simulated-status.is-protecting,
.binance-simulated-status.is-profit { border-color: color-mix(in srgb, var(--binance-up) 45%, var(--border-line)); color: var(--binance-up); }
.binance-simulated-status.is-at_risk { border-color: color-mix(in srgb, var(--binance-down) 45%, var(--border-line)); color: var(--binance-down); }
.binance-simulated-status.is-pending_entry { border-color: color-mix(in srgb, var(--binance-gold) 55%, var(--border-line)); background: color-mix(in srgb, var(--binance-gold) 8%, var(--panel-solid)); color: var(--binance-gold); }
.binance-simulated-status.is-executing { border-color: color-mix(in srgb, var(--binance-up) 45%, var(--border-line)); background: color-mix(in srgb, var(--binance-up) 6%, var(--panel-solid)); color: var(--binance-up); }
.binance-simulated-status.is-stopped { border-color: var(--binance-down); background: color-mix(in srgb, var(--binance-down) 10%, var(--panel-solid)); color: var(--binance-down); }
.binance-simulated-status.is-watch { border-color: color-mix(in srgb, var(--binance-gold) 45%, var(--border-line)); color: var(--binance-gold); }
.binance-execution-tab-status { flex: 0 0 auto; width: max-content; min-width: 0; min-height: 20px; height: 20px; padding: 0 9px; border-radius: 999px; font-size: 10.5px; }
.binance-simulated-stale { color: var(--binance-gold); }
.binance-simulated-levels { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0; border: 1px solid var(--border-line); border-bottom: 0; background: var(--panel-solid); }
.binance-simulated-levels > div { padding: 12px; border-right: 1px solid var(--border-line); border-bottom: 1px solid var(--border-line); }
.binance-simulated-levels > .binance-current-stop { grid-column: 1 / -1; border-right: 0; }
.binance-simulated-levels > div:last-child { border-right: 0; }
.binance-simulated-tp-row { display: grid; gap: 0; border: 1px solid var(--border-line); border-top: 0; background: var(--panel-solid); }
.binance-simulated-tp-row > div { display: grid; gap: 4px; min-width: 0; padding: 12px; border-right: 1px solid var(--border-line); }
.binance-simulated-tp-row > div:last-child { border-right: 0; }
.binance-simulated-tp-row span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-simulated-tp-row strong { overflow: hidden; color: var(--text-primary); font-family: var(--font-pnl); font-size: 15px; font-weight: 900; text-overflow: ellipsis; white-space: nowrap; }
.binance-level-result { display: flex; align-items: baseline; gap: 0; white-space: nowrap; }
.binance-level-result > span { color: var(--text-faint); font-size: 10px; }
.binance-level-result > strong { font-family: var(--font-pnl); font-size: 12px; font-weight: 800; }
.binance-simulated-levels strong { overflow: hidden; color: var(--text-primary); font-family: var(--font-pnl); font-size: 15px; font-weight: 900; text-overflow: ellipsis; white-space: nowrap; }
.binance-simulated-levels small { color: var(--text-faint); font-size: 10px; line-height: 1.35; overflow-wrap: anywhere; white-space: normal; }
.binance-simulated-levels small.binance-change-value { font-family: var(--font-pnl); font-size: 12px; font-weight: 700; }

/* ===== 界面精修：排版、微交互、状态过渡 ===== */
/* 面板小标签：加大字距 + 前置短线，更克制的层级表达 */
.binance-page .binance-panel-head .binance-panel-kicker {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  letter-spacing: 0.14em;
}

.binance-page .binance-panel-head .binance-panel-kicker::before {
  content: '';
  width: 14px;
  height: 1px;
  background: currentColor;
  opacity: 0.45;
}

/* 页面主标题：克制的明度渐变 */
.binance-page .binance-page-head .binance-heading h1 {
  background: linear-gradient(180deg, var(--text-primary), color-mix(in srgb, var(--text-primary) 74%, transparent));
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}

/* 品牌金标：悬停柔和辉光 */
.binance-page .binance-brand-mark {
  transition: box-shadow 0.4s var(--ease-out-soft), transform 0.4s var(--ease-out-soft);
}

.binance-page .binance-brand-mark:hover { box-shadow: 0 6px 20px color-mix(in srgb, var(--binance-gold) 30%, transparent); }

/* 状态胶囊：阶段切换颜色柔和过渡 */
.binance-simulated-status,
.binance-execution-tab-status {
  transition: color 0.3s var(--ease-out-soft), background-color 0.3s var(--ease-out-soft),
    border-color 0.3s var(--ease-out-soft);
}

/* 持仓计划监控面板内细滚动条 */
.binance-simulated-panel :deep(.binance-futures-position-list),
.binance-simulated-panel :deep(.binance-kline-range-panel) {
  scrollbar-width: thin;
  scrollbar-color: color-mix(in srgb, var(--text-faint) 45%, transparent) transparent;
}

/* 抽屉把手：悬停抬升与投影 */
.binance-account-drawer-handle {
  transition: opacity 220ms var(--ease-out-soft), transform 300ms var(--ease-out-soft),
    box-shadow 260ms var(--ease-out-soft);
}

.binance-account-drawer-handle:hover {
  box-shadow: -6px 10px 26px rgba(27, 43, 58, 0.22);
}

.binance-change-value { font-size: 12px; font-weight: 700; }
.binance-simulated-levels > .binance-active-stop-level { position: relative; background: var(--panel-solid); }
.binance-simulated-levels > .binance-active-stop-level.is-structure { background: color-mix(in srgb, var(--binance-down) 8%, var(--panel-solid)); box-shadow: inset 0 3px 0 var(--binance-down); }
.binance-simulated-levels > .binance-active-stop-level.is-moving { background: color-mix(in srgb, var(--binance-gold) 11%, var(--panel-solid)); box-shadow: inset 0 3px 0 var(--binance-gold); }
.binance-simulated-levels > .binance-active-stop-level.is-risk { background: color-mix(in srgb, #f97316 10%, var(--panel-solid)); box-shadow: inset 0 3px 0 #f97316; }
.binance-active-stop-level > span { display: flex; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; gap: 4px 7px; }
.binance-active-stop-level > span em { color: var(--binance-down); font-size: 9px; font-style: normal; font-weight: 900; white-space: nowrap; }
.binance-active-stop-level.is-moving > span em { color: var(--binance-gold); }
.binance-active-stop-level.is-risk > span em { color: #f97316; }
.binance-active-stop-level > strong { color: var(--text-primary); }
.binance-active-stop-level.is-structure > strong { color: var(--binance-down); }
.binance-active-stop-level.is-moving > strong { color: var(--binance-gold); }
.binance-active-stop-level.is-risk > strong { color: #f97316; }
.binance-simulated-timeframes { display: flex; flex-wrap: wrap; gap: 7px; padding: 12px 0 8px; }
.binance-simulated-timeframes span { display: inline-flex; min-height: 24px; align-items: center; padding: 0 8px; border: 1px solid var(--border-line); font-family: var(--font-tab); font-size: 10px; font-weight: 900; }
.binance-simulated-timeframes span.is-bull { border-color: color-mix(in srgb, var(--binance-direction-long) 35%, var(--border-line)); color: var(--binance-direction-long); }
.binance-simulated-timeframes span.is-bear { border-color: color-mix(in srgb, var(--binance-direction-short) 35%, var(--border-line)); color: var(--binance-direction-short); }
.binance-simulated-timeframes span.is-neutral { color: var(--text-muted); }
.binance-simulated-reasons { display: grid; gap: 4px; }
.binance-simulated-reasons p { margin: 0; color: var(--text-muted); font-size: 11px; font-weight: 700; line-height: 1.45; }
.binance-simulated-reasons p.is-muted { color: var(--text-faint); }
.binance-simulated-management-note { display: grid; gap: 4px; margin-top: 11px; padding: 9px 10px; border-left: 3px solid var(--binance-up); background: color-mix(in srgb, var(--binance-up) 5%, var(--panel-solid)); }
.binance-simulated-management-note.is-warning { border-left-color: var(--binance-gold); background: color-mix(in srgb, var(--binance-gold) 7%, var(--panel-solid)); }
.binance-simulated-management-note span { color: var(--text-primary); font-size: 10px; font-weight: 900; }
.binance-simulated-management-note p { margin: 0; color: var(--text-muted); font-size: 11px; font-weight: 700; line-height: 1.5; overflow-wrap: anywhere; }
.binance-simulated-item-foot { display: flex; align-items: flex-start; flex-direction: column; justify-content: space-between; gap: 8px; margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--border-subtle); color: var(--text-faint); font-family: var(--font-tab); font-size: 10px; font-weight: 700; }
.binance-simulated-item-foot > span { min-width: 0; overflow-wrap: anywhere; }
.binance-simulated-item-foot-actions { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; width: 100%; min-width: 0; }
.binance-position-form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 14px; }
.binance-position-form .el-form-item { margin-bottom: 14px; }
.binance-position-quantity-mode { grid-column: 1 / -1; }
.binance-position-quantity-mode-control { width: 100%; }
.binance-position-quantity-mode-control :deep(.el-segmented) { width: 100% !important; }
.binance-position-quantity-mode-control :deep(.el-segmented__item-label) { overflow: visible; white-space: nowrap; }
.binance-position-field { width: 100%; }
.binance-margin-ratio-presets { display: flex; flex-wrap: wrap; gap: 6px; width: 100%; min-width: 0; margin: 0 0 9px; }
.binance-margin-ratio-preset { flex: 1 1 auto; min-width: 44px; padding: 5px 8px; border: 1px solid var(--border-line); border-radius: 6px; background: var(--panel-solid); color: var(--text-secondary); font-family: var(--font-tab); font-size: 11px; font-weight: 800; line-height: 1.2; cursor: pointer; transition: border-color 140ms ease, background 140ms ease, color 140ms ease; }
.binance-margin-ratio-preset:hover { border-color: color-mix(in srgb, var(--binance-gold) 55%, var(--border-line)); color: var(--text-primary); }
.binance-margin-ratio-preset.is-selected { border-color: var(--binance-gold); background: color-mix(in srgb, var(--binance-gold) 12%, var(--panel-solid)); color: var(--text-primary); }
.binance-tp-ratio-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 12px; min-width: 0; }
.binance-entry-price-presets { display: flex; flex-wrap: wrap; gap: 5px; width: 100%; min-width: 0; margin: 0 0 7px; }
.binance-entry-price-preset { display: inline-flex; align-items: baseline; justify-content: space-between; gap: 5px; min-width: 0; max-width: 100%; padding: 5px 7px; border: 1px solid var(--border-line); border-radius: 4px; background: var(--panel-muted); color: var(--text-muted); cursor: pointer; font: inherit; font-size: 10px; font-weight: 800; line-height: 1.25; text-align: left; transition: border-color 140ms ease, background 140ms ease, color 140ms ease; }
.binance-entry-price-preset span { min-width: 0; overflow-wrap: anywhere; }
.binance-entry-price-preset strong { color: var(--text-primary); font-family: var(--font-pnl); font-size: 10px; font-weight: 900; white-space: nowrap; }
.binance-entry-price-preset:hover,
.binance-entry-price-preset:focus-visible,
.binance-entry-price-preset.is-selected { border-color: var(--binance-gold); background: color-mix(in srgb, var(--binance-gold) 12%, var(--panel-solid)); color: var(--text-primary); outline: none; }
.binance-entry-price-preset.is-selected strong { color: var(--binance-gold); }
.binance-position-field-note { display: block; margin-top: 5px; color: var(--text-faint); font-size: 10px; font-weight: 700; line-height: 1.4; overflow-wrap: anywhere; }
.binance-position-form .el-slider { width: 100%; }
.binance-position-loss-inline { grid-column: 1 / -1; display: flex; align-items: center; justify-content: space-between; gap: 12px; min-width: 0; margin: -2px 0 12px; padding: 9px 10px; border: 1px solid color-mix(in srgb, var(--binance-down) 32%, var(--border-line)); background: color-mix(in srgb, var(--binance-down) 4%, var(--panel-solid)); }
.binance-position-loss-inline-label { display: grid; gap: 3px; min-width: 0; }
.binance-position-loss-inline-label span { color: var(--text-muted); font-size: 10px; font-weight: 900; }
.binance-position-loss-inline-label small,
.binance-position-loss-inline-unavailable { color: var(--text-faint); font-size: 9px; line-height: 1.35; overflow-wrap: anywhere; }
.binance-position-loss-inline-values { display: flex; flex-direction: column; align-items: flex-end; justify-content: flex-end; gap: 2px; min-width: 0; text-align: right; }
.binance-position-loss-main { display: grid; gap: 2px; min-width: 0; }
.binance-position-loss-main strong { color: var(--binance-down); font-family: var(--font-pnl); font-size: 14px; font-weight: 900; line-height: 1.15; white-space: nowrap; }
.binance-position-loss-main small { color: var(--binance-down); font-family: var(--font-pnl); font-size: 9px; font-weight: 800; line-height: 1.2; }
.binance-position-loss-secondary { max-width: 100%; color: var(--text-faint); font-size: 9px; font-weight: 700; line-height: 1.35; overflow-wrap: anywhere; }
.binance-position-risk-advice { margin: 0 0 14px; border: 1px solid color-mix(in srgb, var(--binance-gold) 38%, var(--border-line)); background: color-mix(in srgb, var(--binance-gold) 6%, var(--panel-solid)); }
.binance-position-risk-toggle { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; align-items: center; width: 100%; min-height: 42px; gap: 7px; padding: 9px 12px; border: 0; background: transparent; color: var(--text-primary); cursor: pointer; text-align: left; }
.binance-position-risk-toggle-copy { display: flex; align-items: baseline; gap: 7px; min-width: 0; }
.binance-position-risk-toggle-copy strong { color: var(--binance-gold); font-size: 11px; font-weight: 900; }
.binance-position-risk-toggle-copy small { overflow: hidden; color: var(--text-muted); font-size: 10px; font-weight: 800; text-overflow: ellipsis; white-space: nowrap; }
.binance-position-risk-toggle-state { color: var(--text-faint); font-size: 10px; font-weight: 900; }
.binance-position-risk-toggle-icon { color: var(--binance-gold); transition: transform 160ms ease; }
.binance-position-risk-toggle-icon.is-expanded { transform: rotate(180deg); }
.binance-position-risk-content { padding: 0 12px 12px; }
.binance-position-risk-settings { display: block; margin: 0 0 10px; color: var(--text-muted); font-size: 10px; font-weight: 700; line-height: 1.4; }
.binance-protection-dialog em { font-style: normal; }
.binance-position-risk-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px 12px; }
.binance-position-risk-grid > div { display: grid; gap: 3px; min-width: 0; }
.binance-position-risk-grid span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-position-risk-grid strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-pnl); font-size: 12px; font-weight: 900; line-height: 1.3; }
.binance-position-risk-grid small { color: var(--text-faint); font-size: 9px; line-height: 1.3; }
.binance-position-risk-warning { margin: 10px 0 0; color: var(--binance-down); font-size: 11px; font-weight: 800; line-height: 1.45; overflow-wrap: anywhere; }
 .binance-position-risk-unavailable { margin: 0; color: var(--text-muted); font-size: 11px; font-weight: 700; line-height: 1.45; overflow-wrap: anywhere; }
 .binance-position-loss-advice { margin: 0 0 14px; padding: 12px; border: 1px solid color-mix(in srgb, var(--binance-down) 34%, var(--border-line)); background: color-mix(in srgb, var(--binance-down) 5%, var(--panel-solid)); }
 .binance-position-loss-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
 .binance-position-loss-head > div { display: grid; gap: 3px; min-width: 0; }
 .binance-position-loss-head span { color: var(--text-muted); font-size: 11px; font-weight: 900; }
 .binance-position-loss-head > div strong { color: var(--binance-down); font-family: var(--font-pnl); font-size: 16px; font-weight: 900; }
 .binance-position-loss-ratio { flex: 0 0 auto; color: var(--binance-down); font-size: 12px; font-weight: 900; line-height: 1.4; text-align: right; }
 .binance-position-loss-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px 12px; }
 .binance-position-loss-grid > div { display: grid; gap: 3px; min-width: 0; }
 .binance-position-loss-grid span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
 .binance-position-loss-grid strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-pnl); font-size: 12px; font-weight: 900; line-height: 1.3; }
 .binance-position-loss-grid small,
 .binance-position-loss-note { color: var(--text-faint); font-size: 10px; line-height: 1.4; }
 .binance-position-loss-note { margin: 10px 0 0; }
.binance-position-ratio-preview { display: grid; grid-template-columns: auto auto minmax(0, 1fr); align-items: baseline; gap: 6px 10px; margin-top: 10px; padding-top: 10px; border-top: 1px solid color-mix(in srgb, var(--binance-gold) 24%, var(--border-line)); }
.binance-position-ratio-preview span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-position-ratio-preview strong { color: var(--text-primary); font-family: var(--font-pnl); font-size: 12px; font-weight: 900; }
.binance-position-ratio-preview small { min-width: 0; color: var(--text-faint); font-size: 10px; line-height: 1.4; overflow-wrap: anywhere; }
:deep(.binance-position-dialog) { max-width: calc(100vw - 24px); margin: 12px auto; }

.binance-connection-panel { min-height: 100%; }
.binance-connection-state { color: var(--text-faint); }
.binance-connection-state.is-connected { color: var(--binance-up); }
.binance-connection-state.is-stale { color: var(--binance-gold); }
.binance-connection-mode-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
.binance-connection-mode-head > div { display: grid; gap: 4px; min-width: 0; }
.binance-connection-mode-head strong { color: var(--text-primary); font-size: 13px; font-weight: 900; }
.binance-connection-mode-head small,
.binance-connection-help,
.binance-connection-subscription-count { color: var(--text-muted); font-size: 11px; font-weight: 700; line-height: 1.5; overflow-wrap: anywhere; }
.binance-connection-mode-badge { display: inline-flex; flex: 0 0 auto; min-height: 24px; align-items: center; padding: 0 8px; border: 1px solid var(--border-line); font-family: var(--font-tab); font-size: 10px; font-weight: 900; }
.binance-connection-mode-badge.is-rest { border-color: color-mix(in srgb, var(--text-muted) 45%, var(--border-line)); color: var(--text-muted); }
.binance-connection-mode-badge.is-websocket { border-color: color-mix(in srgb, var(--binance-gold) 60%, var(--border-line)); color: var(--binance-gold); }
.binance-connection-mode-switch { display: flex; width: 100%; margin-bottom: 8px; }
.binance-connection-mode-switch :deep(.el-radio-button) { flex: 1 1 0; min-width: 0; }
.binance-connection-mode-switch :deep(.el-radio-button__inner) { width: 100%; }
.binance-connection-help { margin: 0 0 15px; color: var(--text-faint); }
.binance-connection-lines { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; }
.binance-connection-line-card { display: grid; gap: 5px; min-width: 0; padding: 8px 9px; border: 1px solid var(--border-line); background: var(--panel-muted); }
.binance-connection-line-card > div { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
.binance-connection-line-card span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-connection-line-card strong { color: var(--text-primary); font-family: var(--font-tab); font-size: 11px; font-weight: 900; }
.binance-connection-line-card small { min-width: 0; color: var(--text-faint); font-size: 10px; font-weight: 700; line-height: 1.4; overflow-wrap: anywhere; }
.binance-connection-line-card.is-connected { border-color: color-mix(in srgb, var(--binance-up) 45%, var(--border-line)); }
.binance-connection-line-card.is-connected strong { color: var(--binance-up); }
.binance-connection-line-card.is-rest strong { color: var(--text-muted); }
.binance-connection-line-card.is-error { border-color: color-mix(in srgb, var(--binance-down) 45%, var(--border-line)); }
.binance-connection-line-card.is-error strong { color: var(--binance-down); }
.binance-connection-line-card.is-pending strong { color: var(--binance-gold); }
.binance-connection-subscription-count { display: block; margin-top: 9px; color: var(--text-faint); }
.binance-account-settings-status { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 10px; padding-bottom: 9px; border-bottom: 1px solid var(--border-subtle); }
.binance-account-settings-status small { color: var(--text-faint); font-size: 10px; font-weight: 800; text-align: right; }
.binance-account-settings-tabs :deep(.el-tabs__header) { margin: 0 0 11px; }
.binance-account-settings-tabs :deep(.el-tabs__item) { font-family: var(--font-tab); font-size: 12px; font-weight: 900; }
.binance-connection-settings-sections { display: grid; gap: 10px; }
.binance-connection-settings-section { padding-top: 9px; border-top: 1px solid var(--border-subtle); }
.binance-connection-settings-section:first-child { padding-top: 0; border-top: 0; }
.binance-connection-settings-section-head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 4px 10px; margin-bottom: 7px; }
.binance-smart-money-auth-head-copy { display: grid; gap: 4px; min-width: 0; }
.binance-smart-money-auth-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 14px; align-items: start; }
.binance-smart-money-auth-grid .el-form-item { margin-bottom: 9px; }
.binance-connection-settings-section-head .el-button { flex: 0 0 auto; max-width: 100%; }
.binance-connection-settings-section-head strong { color: var(--text-primary); font-size: 11px; font-weight: 900; }
.binance-connection-settings-section-head small { color: var(--text-faint); font-size: 10px; overflow-wrap: anywhere; }
.binance-websocket-test-panel { display: grid; gap: 9px; padding: 1px 0; }
.binance-websocket-test-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.binance-websocket-test-actions .el-button { width: 100%; margin-left: 0; }
.binance-websocket-test-result { display: grid; gap: 4px; padding: 10px 11px; border: 1px solid var(--border-line); background: var(--panel-muted); }
.binance-websocket-test-result strong { font-size: 12px; font-weight: 900; }
.binance-websocket-test-result small { color: var(--text-muted); font-family: var(--font-num); font-size: 10px; font-weight: 700; line-height: 1.45; overflow-wrap: anywhere; }
.binance-websocket-test-result.is-success { border-color: color-mix(in srgb, var(--binance-up) 42%, var(--border-line)); }
.binance-websocket-test-result.is-success strong { color: var(--binance-up); }
.binance-websocket-test-result.is-error { border-color: color-mix(in srgb, var(--binance-down) 42%, var(--border-line)); }
.binance-websocket-test-result.is-error strong { color: var(--binance-down); }
.binance-websocket-account-summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin-top: 6px; padding-top: 10px; border-top: 1px solid var(--border-subtle); }
.binance-websocket-account-summary > div { display: grid; gap: 3px; min-width: 0; }
.binance-websocket-account-summary span,
.binance-websocket-account-position span { color: var(--text-muted); font-size: 9px; font-weight: 800; }
.binance-websocket-account-summary strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-pnl); font-size: 11px; font-weight: 900; line-height: 1.3; }
.binance-websocket-account-positions { display: grid; gap: 7px; margin-top: 8px; }
.binance-websocket-account-position { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px 11px; padding-top: 8px; border-top: 1px solid var(--border-subtle); }
.binance-websocket-account-position > div { display: grid; gap: 3px; min-width: 0; }
.binance-websocket-account-position > div:first-child { display: flex; align-items: baseline; flex-wrap: wrap; gap: 3px 7px; grid-column: 1 / -1; }
.binance-websocket-account-position > div:first-child small { color: var(--text-faint); font-size: 9px; font-weight: 800; }
.binance-websocket-account-position strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-pnl); font-size: 11px; font-weight: 900; line-height: 1.3; }
.binance-websocket-account-position > div:first-child strong { font-family: var(--font-tab); font-size: 12px; }
.binance-websocket-account-position em { font-size: 9px; font-style: normal; font-weight: 900; }
.binance-websocket-account-empty { display: block; margin-top: 7px; color: var(--text-faint) !important; font-family: var(--font-ui) !important; }
.binance-strategy-settings-form { min-height: 0; }
.binance-strategy-settings-sections { display: grid; gap: 14px; }
.binance-strategy-settings-section { padding-top: 12px; border-top: 1px solid var(--border-subtle); }
.binance-strategy-settings-section:first-child { padding-top: 0; border-top: 0; }
.binance-strategy-settings-section-head { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 4px 10px; margin-bottom: 10px; }
.binance-strategy-settings-section-head strong { color: var(--text-primary); font-size: 11px; font-weight: 900; }
.binance-strategy-settings-section-head small { color: var(--text-faint); font-size: 10px; overflow-wrap: anywhere; }
.binance-strategy-settings-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 14px; }
.binance-strategy-settings-grid .el-form-item { margin-bottom: 12px; }
.binance-strategy-settings-grid .el-input-number { width: 100%; }
.binance-strategy-route-field { grid-column: 1 / -1; }
.binance-selected-model-summary { display: grid; gap: 8px; margin-top: 10px; padding: 10px 11px; border: 1px solid color-mix(in srgb, var(--binance-gold) 34%, var(--border-line)); background: color-mix(in srgb, var(--binance-gold) 5%, var(--panel-muted)); }
.binance-selected-model-summary-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.binance-selected-model-summary-head span { color: var(--text-primary); font-size: 11px; font-weight: 900; }
.binance-selected-model-summary-head em { color: var(--binance-gold); font-size: 10px; font-style: normal; font-weight: 800; }
.binance-selected-model-summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px 12px; }
.binance-selected-model-summary-grid > div { display: grid; gap: 3px; min-width: 0; }
.binance-selected-model-summary-grid span { color: var(--text-muted); font-size: 9px; font-weight: 800; }
.binance-selected-model-summary-grid strong { min-width: 0; color: var(--text-primary); font-family: var(--font-num); font-size: 11px; font-weight: 900; overflow-wrap: anywhere; }
.binance-selected-model-summary-grid strong.is-breakable { word-break: break-word; }
.binance-selected-model-summary > small { color: var(--text-faint); font-size: 10px; font-weight: 700; line-height: 1.4; }
.binance-level-strategy-switch { display: flex; width: 100%; }
.binance-level-strategy-switch :deep(.el-radio-button) { flex: 1 1 0; min-width: 0; }
.binance-level-strategy-switch :deep(.el-radio-button__inner) { width: 100%; padding-inline: 8px; overflow-wrap: anywhere; white-space: normal; }
.binance-strategy-setting-help { display: block; margin-top: 6px; color: var(--text-faint); font-size: 10px; font-weight: 700; line-height: 1.45; overflow-wrap: anywhere; }
.binance-strategy-settings-note { margin: 2px 0 0; padding-top: 11px; border-top: 1px solid var(--border-subtle); color: var(--text-faint); font-size: 10px; font-weight: 700; line-height: 1.45; }
.binance-settings-warning { margin: 0 0 13px; color: var(--binance-gold); font-size: 11px; font-weight: 800; line-height: 1.45; }
.binance-auth-gate { display: grid; justify-items: start; gap: 9px; padding: 28px 2px 8px; }
.binance-auth-icon { display: grid; width: 42px; height: 42px; place-items: center; border: 1px solid var(--border-line); border-radius: 8px; background: var(--panel-muted); color: var(--text-muted); }
.binance-auth-gate strong { color: var(--text-primary); font-size: 15px; font-weight: 900; }
.binance-auth-gate p { color: var(--text-muted); font-size: 12px; font-weight: 700; }
.binance-stored-credentials { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin: 0 0 10px; padding: 8px 9px; border: 1px solid var(--border-line); background: var(--panel-muted); }
.binance-stored-credentials > div:first-child { display: grid; gap: 3px; min-width: 0; }
.binance-stored-credentials span,
.binance-stored-credentials small { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-stored-credentials strong { overflow: hidden; color: var(--text-primary); font-family: var(--font-tab); font-size: 11px; font-weight: 900; text-overflow: ellipsis; white-space: nowrap; }
.binance-stored-credentials > div:last-child { display: flex; flex: 0 0 auto; gap: 2px; }
.binance-credential-form { display: grid; gap: 1px; }
.binance-credential-form .el-form-item { margin-bottom: 9px; }
.binance-secret-note { display: flex; align-items: flex-start; gap: 6px; margin: 1px 0 14px; color: var(--text-faint); font-size: 11px; font-weight: 700; line-height: 1.45; }
.binance-secret-note svg { flex: 0 0 auto; color: var(--binance-up); }
.binance-connection-actions { display: flex; justify-content: flex-end; gap: 8px; }
.binance-panel-icon { color: var(--text-muted); }
.binance-account-drawer { position: fixed; top: clamp(58px, 12vh, 108px); right: 0; z-index: 1200; width: min(448px, calc(100vw - 18px)); height: min(56vh, calc(100vh - 130px)); max-height: 56vh; overflow: visible; pointer-events: none; transition: width 360ms cubic-bezier(.22, 1, .36, 1), height 360ms cubic-bezier(.22, 1, .36, 1), top 360ms cubic-bezier(.22, 1, .36, 1), max-height 360ms cubic-bezier(.22, 1, .36, 1); }
.binance-account-drawer.is-open { pointer-events: auto; }
.binance-account-drawer.is-collapsed { top: 220px; right: 0; bottom: auto; width: 96px; height: 46px; max-height: 46px; pointer-events: auto; }
.binance-account-panel { position: absolute; inset: 0; display: flex; height: 100%; flex-direction: column; overflow: hidden; border-radius: 8px 0 0 8px; box-shadow: -8px 12px 34px rgba(27, 43, 58, .2); opacity: 1; transform: translateX(0); transition: opacity 220ms cubic-bezier(.22, 1, .36, 1), transform 300ms cubic-bezier(.22, 1, .36, 1); }
.binance-account-panel.is-hidden { opacity: 0; transform: translateX(28px); pointer-events: none; }
.binance-account-drawer-handle { position: absolute; top: 0; right: 0; display: inline-flex; width: 96px; min-height: 46px; align-items: center; justify-content: center; gap: 6px; padding: 8px; border: 1px solid var(--border-line); border-right: 0; border-radius: 7px 0 0 7px; background: var(--panel-bg); box-shadow: -5px 8px 22px rgba(27, 43, 58, .16); color: var(--text-primary); cursor: grab; touch-action: none; opacity: 1; transform: translateX(0); transition: opacity 220ms cubic-bezier(.22, 1, .36, 1), transform 300ms cubic-bezier(.22, 1, .36, 1); }
.binance-account-drawer-handle.is-hidden { opacity: 0; transform: translateX(28px); pointer-events: none; }
.binance-account-drawer-handle:active,
.binance-account-drawer-handle.is-dragging { cursor: grabbing; }
.binance-account-drawer-status-dot { flex: 0 0 auto; width: 8px; height: 8px; border-radius: 50%; background: var(--text-faint); }
.binance-account-drawer-status-dot.is-connected { background: var(--binance-up); }
.binance-account-drawer-pnl { min-width: 0; overflow: hidden; font-family: var(--font-pnl); font-size: 12px; font-weight: 900; line-height: 1.1; text-overflow: ellipsis; white-space: nowrap; }
.binance-balance-list { display: flex; flex: 1 1 auto; flex-direction: column; gap: 0; margin-top: 8px; min-height: 0; }
.binance-account-status-rows { display: grid; gap: 3px; padding: 2px 0 6px; border-bottom: 1px solid var(--border-subtle); }
.binance-account-status-line { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 9px; }
.binance-account-status-line small { color: var(--text-faint); font-size: 10px; font-weight: 700; overflow-wrap: anywhere; }
.binance-account-sync-error { flex: 1 1 100%; color: var(--binance-down) !important; line-height: 1.35; }
.binance-balance-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-height: 53px; border-bottom: 1px solid var(--border-subtle); }
.binance-asset-symbol { color: var(--text-primary); font-family: var(--font-tab); font-size: 13px; font-weight: 900; }
.binance-balance-row div { display: grid; justify-items: end; gap: 3px; }
.binance-balance-row strong { color: var(--text-primary); font-family: var(--font-pnl); font-size: 13px; font-weight: 900; }
.binance-balance-row small { color: var(--text-faint); font-size: 10px; font-weight: 700; }
.binance-futures-account-summary { display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(0, .8fr); gap: 8px; margin: 2px 0 0; padding: 0 0 5px; border-bottom: 1px solid var(--border-subtle); }
.binance-futures-account-summary > div { display: grid; grid-template-rows: auto auto; align-content: start; gap: 3px; min-width: 0; min-height: 0; padding: 7px 9px 8px; border: 1px solid var(--border-line); border-radius: 6px; background: var(--panel-muted); }
.binance-balance-summary { --balance-fill: 0%; border-color: color-mix(in srgb, var(--binance-gold) 52%, var(--border-line)) !important; background: linear-gradient(90deg, color-mix(in srgb, var(--binance-gold) 19%, var(--panel-muted)) 0 var(--balance-fill), color-mix(in srgb, var(--binance-gold) 5%, var(--panel-muted)) var(--balance-fill) 100%) !important; }
.binance-unrealized-summary { border-color: color-mix(in srgb, var(--binance-rise) 20%, var(--border-line)); }
.binance-futures-account-summary > div > span { color: var(--text-muted); font-family: var(--font-tab); font-size: 11px; font-weight: 900; letter-spacing: .04em; }
.binance-futures-position-metrics span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-futures-account-summary strong { overflow: hidden; align-self: start; color: var(--text-primary); font-family: var(--font-pnl); font-size: clamp(24px, 1.9vw, 28px); font-weight: 900; letter-spacing: 0; line-height: 1.05; text-overflow: ellipsis; white-space: nowrap; }
.binance-balance-summary strong { color: color-mix(in srgb, var(--text-primary) 58%, var(--binance-gold)) !important; text-shadow: 0 1px 0 color-mix(in srgb, var(--panel-solid) 70%, transparent); }
.binance-balance-summary strong i { margin: 0 3px; color: color-mix(in srgb, var(--text-primary) 45%, var(--binance-gold)); font-size: .72em; font-style: normal; vertical-align: .08em; }
.binance-page :deep(.animated-number.binance-account-balance-number),
.binance-page :deep(.animated-number.binance-account-pnl-number) { font-size: clamp(20px, 1.55vw, 22px) !important; line-height: 1.05 !important; }
.binance-page :deep(.animated-number.binance-account-balance-number) { color: color-mix(in srgb, var(--text-primary) 58%, var(--binance-gold)) !important; text-shadow: 0 1px 0 color-mix(in srgb, var(--panel-solid) 70%, transparent); }
.binance-page :deep(.animated-number.binance-account-pnl-number.tone-positive) { color: #ef5969 !important; text-shadow: 0 0 16px rgba(239, 89, 105, .48); }
.binance-page :deep(.animated-number.binance-account-pnl-number.tone-negative) { color: #19b787 !important; text-shadow: 0 0 16px rgba(25, 183, 135, .48); }
.binance-page :deep(.animated-number.binance-account-pnl-number.tone-zero) { color: var(--binance-gold) !important; text-shadow: 0 0 14px color-mix(in srgb, var(--binance-gold) 34%, transparent); }
.binance-page :deep(.animated-number.binance-drawer-pnl-number) { font-size: 13px !important; line-height: 1.1 !important; }
.binance-page :deep(.animated-number.binance-drawer-pnl-number.tone-positive) { color: #ef5969 !important; }
.binance-page :deep(.animated-number.binance-drawer-pnl-number.tone-negative) { color: #19b787 !important; }
.binance-page :deep(.animated-number.binance-drawer-pnl-number.tone-zero) { color: var(--binance-gold) !important; }
.binance-futures-position-list { display: flex; flex: 1 1 auto; align-items: stretch; flex-direction: column; gap: 6px; margin-top: 6px; min-width: 0; min-height: 0; overflow-x: hidden; overflow-y: auto; scrollbar-width: thin; padding-right: 2px; }
.binance-futures-position-smoothing { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 8px; margin-top: 6px; padding: 6px 4px 1px; border-top: 1px solid var(--border-subtle); color: var(--text-faint); font-size: 9px; font-weight: 800; }
.binance-futures-position-smoothing strong { min-width: 30px; color: var(--text-muted); font-family: var(--font-pnl); font-size: 9px; font-weight: 800; text-align: right; }
.binance-futures-position-smoothing :deep(.el-slider) { width: 100%; margin: 0; }
.binance-futures-position-smoothing :deep(.el-slider__runway) { margin: 5px 0; }
.binance-futures-position-smoothing :deep(.el-slider__button) { width: 10px; height: 10px; }
.binance-account-panel .binance-futures-position-item { flex: 0 0 auto; min-width: 0; min-height: 42px; overflow: hidden; border: 1px solid var(--border-line); background: var(--panel-muted); transition: border-color 220ms ease, box-shadow 220ms ease, background-color 220ms ease; }
.binance-account-panel .binance-futures-position-item.is-value-alert { border-color: var(--binance-down); box-shadow: 0 0 0 1px color-mix(in srgb, var(--binance-down) 34%, transparent); }
.binance-account-panel .binance-futures-position-head { box-sizing: border-box; display: flex; width: 100%; min-width: 0; align-items: baseline; justify-content: space-between; gap: 8px; margin: 0; padding: 9px 10px; border: 0; border-radius: 0; color: inherit; cursor: pointer; font: inherit; text-align: left; transition: background-color 180ms ease, filter 180ms ease; }
.binance-account-panel .binance-futures-position-head:hover { filter: brightness(1.03); }
.binance-account-panel .binance-futures-position-head.is-profit { background: linear-gradient(135deg, color-mix(in srgb, var(--binance-rise) 15%, transparent), color-mix(in srgb, var(--binance-rise) 7%, transparent)); }
.binance-account-panel .binance-futures-position-head.is-loss { background: linear-gradient(135deg, color-mix(in srgb, var(--binance-fall) 15%, transparent), color-mix(in srgb, var(--binance-fall) 7%, transparent)); }
.binance-futures-position-details { display: grid; grid-template-rows: 1fr; min-height: 0; opacity: 1; transform: translateY(0); overflow: hidden; transition: grid-template-rows 420ms cubic-bezier(.22, 1, .36, 1), opacity 260ms ease-out, transform 420ms cubic-bezier(.22, 1, .36, 1); }
.binance-account-panel .binance-futures-position-details-inner { min-height: 0; overflow: hidden; padding: 0 10px 10px; }
.binance-futures-position-expand-enter-active,
.binance-futures-position-expand-leave-active { display: grid; grid-template-rows: 1fr; min-height: 0; overflow: hidden; transition: grid-template-rows 420ms cubic-bezier(.22, 1, .36, 1), opacity 260ms ease-out, transform 420ms cubic-bezier(.22, 1, .36, 1); }
.binance-futures-position-expand-enter-from,
.binance-futures-position-expand-leave-to { grid-template-rows: 0fr; opacity: 0; transform: translateY(-8px); }
.binance-futures-position-expand-enter-to,
.binance-futures-position-expand-leave-from { grid-template-rows: 1fr; opacity: 1; transform: translateY(0); }
.binance-futures-direction { flex: 0 0 auto; padding: 2px 6px; border-radius: 4px; font-family: var(--font-tab); font-size: 10px; font-weight: 900; letter-spacing: .04em; line-height: 1.2; }
.binance-futures-direction.is-long { background: color-mix(in srgb, var(--binance-direction-long) 18%, transparent); color: var(--binance-direction-long); }
.binance-futures-direction.is-short { background: color-mix(in srgb, var(--binance-direction-short) 18%, transparent); color: var(--binance-direction-short); }
.binance-futures-position-name { display: flex; align-items: baseline; gap: 7px; min-width: 0; }
.binance-futures-position-name strong { overflow: hidden; color: var(--text-primary); font-family: var(--font-tab); font-size: 15px; font-weight: 900; text-overflow: ellipsis; white-space: nowrap; }
.binance-futures-leverage { flex: 0 0 auto; color: var(--text-muted); font-family: var(--font-tab); font-size: 11px; font-weight: 900; letter-spacing: .02em; }
.binance-smart-position-pnl-pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 14px; min-width: 0; max-width: 300px; }
.binance-smart-position-pnl { display: grid; min-width: 0; gap: 2px; }
.binance-smart-position-pnl > span { color: var(--text-muted); font-size: 10px; font-weight: 800; line-height: 1.2; }
.binance-smart-position-pnl > strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-pnl); font-size: 18px; font-weight: 900; line-height: 1.1; white-space: normal; }
.binance-smart-position-pnl > small { font-family: var(--font-pnl); font-size: 11px; font-weight: 800; line-height: 1.2; }
.binance-futures-position-pnl { display: flex; flex: 0 0 auto; align-items: baseline; gap: 5px; }
.binance-futures-position-pnl > strong { color: var(--text-primary); font-family: var(--font-pnl); font-size: 20px; font-weight: 900; line-height: 1; }
.binance-futures-position-pnl > small { font-family: var(--font-pnl); font-size: 11px; font-weight: 800; line-height: 1.2; }
.binance-quantity-rule-note { display: block; margin-top: 4px; color: var(--text-faint); font-size: 10px; line-height: 1.35; }
.binance-market-close-slider { display: grid; gap: 8px; padding: 12px 0; }
.binance-market-close-slider-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; color: var(--text-muted); font-size: 12px; font-weight: 800; }
.binance-market-close-slider-head strong { color: var(--binance-gold); font-family: var(--font-pnl); font-size: 20px; }
.binance-market-close-preview { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 12px; padding: 10px 0; border-top: 1px solid var(--border-subtle); }
.binance-market-close-preview div { display: grid; gap: 3px; min-width: 0; }
.binance-market-close-preview span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-market-close-preview strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-pnl); font-size: 14px; font-weight: 900; }
.binance-market-close-preview p { grid-column: 1 / -1; margin: 2px 0 0; }
.binance-futures-position-metrics { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px 12px; padding: 10px 0; }
.binance-futures-position-metrics div { display: grid; gap: 3px; min-width: 0; }
.binance-futures-position-metrics strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-pnl); font-size: 11px; font-weight: 900; line-height: 1.3; word-break: break-word; }
.binance-futures-position-bottom { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: end; gap: 8px; padding-top: 8px; border-top: 1px solid var(--border-subtle); }
.binance-futures-stop-tp { display: grid; grid-column: 1; gap: 4px; min-width: 0; }
.binance-futures-stop-row { display: flex; align-items: baseline; gap: 6px; min-width: 0; }
.binance-futures-stop-row > span { flex: 0 0 auto; color: var(--text-faint); font-size: 10px; font-weight: 800; }
.binance-futures-stop-row > strong { color: var(--text-primary); font-family: var(--font-pnl); font-size: 12px; font-weight: 900; }
.binance-futures-stop-row > small { color: var(--text-faint); font-family: var(--font-pnl); font-size: 10px; font-weight: 800; white-space: nowrap; }
.binance-futures-target-level { padding: 1px 5px; margin: -1px -5px; border: 1px solid transparent; border-radius: 5px; }
.binance-futures-target-level small { color: var(--text-faint); font-family: var(--font-pnl); font-size: 9px; font-weight: 800; line-height: 1.3; }
.binance-futures-target-level.is-reached { border-color: color-mix(in srgb, var(--binance-rise) 45%, var(--border-line)); background: color-mix(in srgb, var(--binance-rise) 9%, transparent); }
.binance-futures-target-level.is-reached strong,
.binance-futures-target-level.is-reached small { color: var(--binance-rise); }
.binance-futures-position-foot-actions { display: inline-flex; grid-column: 2; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 6px; }
.binance-live-monitor-preview-head { display: grid; gap: 5px; padding-bottom: 12px; border-bottom: 1px solid var(--border-subtle); }
.binance-live-monitor-preview-head > div { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
.binance-live-monitor-preview-head strong { color: var(--text-primary); font-family: var(--font-tab); font-size: 16px; font-weight: 900; }
.binance-live-monitor-preview-head span { font-size: 12px; font-weight: 900; }
.binance-live-monitor-preview-head small { color: var(--text-muted); font-size: 10px; font-weight: 700; line-height: 1.45; }
.binance-live-monitor-preview-stale { margin: 11px 0 0; padding: 8px 9px; border: 1px solid color-mix(in srgb, var(--binance-gold) 45%, var(--border-line)); background: color-mix(in srgb, var(--binance-gold) 7%, transparent); color: var(--binance-gold); font-size: 10px; font-weight: 800; line-height: 1.45; }
.binance-live-monitor-preview-grid,
.binance-live-monitor-preview-levels { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 12px; padding: 12px 0; }
.binance-live-monitor-preview-grid { border-bottom: 1px solid var(--border-subtle); }
.binance-live-monitor-preview-grid div,
.binance-live-monitor-preview-levels div { display: grid; gap: 3px; min-width: 0; }
.binance-live-monitor-preview-grid span,
.binance-live-monitor-preview-levels span { color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-live-monitor-preview-grid strong,
.binance-live-monitor-preview-levels strong { overflow-wrap: anywhere; color: var(--text-primary); font-family: var(--font-pnl); font-size: 11px; font-weight: 900; line-height: 1.35; }
.binance-live-monitor-preview-levels small { color: var(--text-faint); font-size: 9px; font-weight: 700; line-height: 1.4; overflow-wrap: anywhere; }
.binance-live-monitor-preview-reasons { display: grid; gap: 6px; padding-top: 12px; border-top: 1px solid var(--border-subtle); }
.binance-live-monitor-preview-reasons > strong { color: var(--text-primary); font-size: 12px; font-weight: 900; }
.binance-live-monitor-preview-reasons p { margin: 0; color: var(--text-muted); font-size: 10px; font-weight: 700; line-height: 1.5; overflow-wrap: anywhere; }
.binance-live-monitor-preview-reasons p.is-warning { color: var(--binance-gold); }
.binance-live-monitor-preview-reasons ul { display: grid; gap: 4px; margin: 0; padding-left: 17px; color: var(--text-faint); font-size: 10px; font-weight: 700; line-height: 1.45; }
.binance-live-monitor-preview-confirmation { margin: 13px 0 0; padding: 9px 10px; border: 1px solid color-mix(in srgb, var(--binance-up) 36%, var(--border-line)); background: color-mix(in srgb, var(--binance-up) 6%, transparent); color: var(--text-muted); font-size: 10px; font-weight: 700; line-height: 1.5; }
.binance-futures-pagination { flex: 0 0 auto; justify-content: center; margin-top: 6px; }
.binance-protection-context { display: grid; gap: 5px; margin-bottom: 16px; padding: 10px 11px; border: 1px solid var(--border-line); background: var(--panel-muted); }
.binance-protection-context div { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 5px 9px; }
.binance-protection-context strong { color: var(--text-primary); font-family: var(--font-tab); font-size: 14px; font-weight: 900; }
.binance-protection-context span,
.binance-protection-context small { color: var(--text-muted); font-size: 10px; font-weight: 800; line-height: 1.4; }
.binance-inline-empty { display: flex; align-items: center; justify-content: center; gap: 7px; min-height: 132px; color: var(--text-faint); font-size: 12px; font-weight: 800; }
.binance-permission-state { display: inline-flex; flex: 0 0 auto; align-items: center; color: var(--text-muted); font-size: 12px; font-weight: 700; line-height: 1.5; white-space: nowrap; }
.binance-permission-state.is-on { color: var(--binance-up); }
.binance-permission-state.is-off { color: var(--binance-down); }

.binance-futures-analysis { display: grid; grid-template-columns: minmax(128px, 160px) minmax(0, 1fr); column-gap: 14px; align-items: start; margin-top: 14px; }
.binance-futures-analysis > .binance-panel-head,
.binance-futures-analysis > .binance-futures-progress,
.binance-futures-analysis > .binance-futures-empty { grid-column: 1 / -1; }
.binance-futures-analysis-detail { display: grid; grid-column: 2; align-content: start; min-width: 0; }
.binance-futures-limit { display: inline-flex; align-items: center; gap: 5px; min-height: 32px; color: var(--text-muted); font-size: 11px; font-weight: 800; white-space: nowrap; }
.binance-futures-limit .el-input-number { width: 86px; }
.binance-futures-limit .el-input-number :deep(.el-input__wrapper) { border-radius: 8px; background: color-mix(in srgb, var(--panel-solid) 92%, transparent); box-shadow: 0 0 0 1px var(--border-line) inset; transition: box-shadow .2s ease; }
.binance-futures-limit .el-input-number :deep(.el-input__wrapper):hover { box-shadow: 0 0 0 1px color-mix(in srgb, var(--text-faint) 55%, transparent) inset; background: var(--panel-solid); }
.binance-futures-limit .el-input-number :deep(.el-input__wrapper.is-focus) { box-shadow: 0 0 0 1.5px color-mix(in srgb, var(--binance-gold) 55%, transparent) inset, 0 0 0 3px color-mix(in srgb, var(--binance-gold) 12%, transparent); background: var(--panel-solid); }
.binance-futures-limit .el-input-number :deep(.el-input__inner) { font-family: var(--font-tab); font-weight: 800; color: var(--text-primary); }
.binance-futures-limit .el-input-number :deep(.el-input__wrapper:hover) { box-shadow: 0 0 0 1px color-mix(in srgb, var(--brand, #7453c6) 45%, var(--border-line)) inset; }
.binance-futures-limit .el-input-number :deep(.el-input__wrapper.is-focus) { box-shadow: 0 0 0 1px color-mix(in srgb, var(--brand, #7453c6) 70%, var(--border-line)) inset, 0 0 0 3px color-mix(in srgb, var(--brand, #7453c6) 18%, transparent); }
.binance-scan-btn { position: relative; overflow: hidden; border: 0; border-radius: 8px; font-weight: 800; letter-spacing: .02em; background: linear-gradient(145deg, #9877df, #6444ac); color: #fff; box-shadow: 0 2px 10px rgba(100, 68, 172, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.28); transition: transform .2s var(--ease-out-soft, ease), box-shadow .2s ease, filter .2s ease; }
.binance-scan-btn:not(.is-disabled):hover { filter: brightness(1.06); box-shadow: 0 5px 18px rgba(100, 68, 172, 0.45), inset 0 1px 0 rgba(255, 255, 255, 0.28); }
.binance-scan-btn:not(.is-disabled):active { transform: translateY(1px) scale(.97); }
.binance-futures-empty { display: flex; align-items: center; justify-content: center; gap: 8px; min-height: 120px; color: var(--text-faint); font-size: 12px; font-weight: 800; }

.binance-position-dialog-footer { display: flex; align-items: center; justify-content: space-between; gap: 14px; width: 100%; min-width: 0; }
.binance-position-dialog-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex: 0 0 auto; }
.binance-live-order-control { display: inline-flex; align-items: center; gap: 7px; min-width: 0; color: var(--text-muted); font-size: 10px; font-weight: 800; white-space: nowrap; }
.binance-live-order-control .el-switch { flex: 0 0 auto; }
.binance-live-order-control.is-disabled { opacity: .65; }
.binance-protection-form { display: grid; gap: 18px; }
.binance-protection-section { min-width: 0; padding-top: 2px; }
.binance-protection-section + .binance-protection-section { padding-top: 17px; border-top: 1px solid var(--border-line); }
.binance-protection-section-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 11px; }
.binance-protection-section-head strong { color: var(--text-primary); font-size: 12px; font-weight: 900; }
.binance-protection-section-head span { color: var(--text-faint); font-size: 10px; line-height: 1.4; text-align: right; }
.binance-protection-form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 14px; }
.binance-protection-form-grid .el-form-item { margin-bottom: 10px; }
.binance-futures-progress { margin-top: 16px; padding: 14px; border: 1px solid var(--border-line); background: var(--panel-muted); }
.binance-futures-progress-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 16px; margin-bottom: 12px; }
.binance-futures-progress-head > div { display: grid; gap: 4px; min-width: 0; }
.binance-futures-progress-head > div:last-child { justify-items: end; text-align: right; }
.binance-futures-progress-head span { color: var(--text-muted); font-size: 11px; font-weight: 800; }
.binance-futures-progress-head strong { overflow: hidden; color: var(--text-primary); font-family: var(--font-tab); font-size: 15px; font-weight: 900; text-overflow: ellipsis; white-space: nowrap; }
.binance-futures-progress .el-progress-bar__outer { background: var(--border-subtle); }
.binance-futures-progress .el-progress-bar__inner { background: var(--binance-gold); }
.binance-futures-result-tabs { display: grid; grid-column: 1; align-self: start; align-content: start; gap: 8px; min-width: 0; margin-top: 14px; }
.binance-futures-result-tabs > span { color: var(--text-muted); font-size: 11px; font-weight: 800; }
.binance-futures-result-tabs > div { display: flex; align-self: start; flex-direction: column; align-items: flex-start; gap: 8px; min-width: 0; max-height: min(520px, 66vh); padding: 2px 4px 5px 1px; overflow-x: hidden; overflow-y: auto; scrollbar-width: thin; }
.binance-futures-result-tab { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 7px 10px; flex: 0 0 auto; width: 160px; min-width: 160px; max-width: 160px; min-height: 62px; padding: 10px; border: 1px solid var(--border-line); border-radius: 10px; background: linear-gradient(145deg, color-mix(in srgb, var(--panel-muted) 92%, var(--panel-solid)), var(--panel-solid)); color: var(--text-muted); box-shadow: 0 1px 0 color-mix(in srgb, var(--text-primary) 5%, transparent); cursor: pointer; text-align: left; transition: border-color 160ms ease, background 160ms ease, box-shadow 160ms ease, transform 160ms ease; }
.binance-futures-result-tab:hover { border-color: color-mix(in srgb, var(--binance-gold) 52%, var(--border-line)); box-shadow: 0 5px 13px color-mix(in srgb, var(--text-primary) 10%, transparent); }
.binance-futures-result-tab-symbol { display: grid; gap: 4px; min-width: 0; }
.binance-futures-result-tab-symbol strong { overflow: hidden; color: var(--text-primary); font-family: var(--font-tab); font-size: 15px; font-weight: 900; letter-spacing: .02em; text-overflow: ellipsis; white-space: nowrap; }
.binance-futures-result-tab-symbol em { font-family: var(--font-tab); font-size: 11px; font-style: normal; font-weight: 900; letter-spacing: .06em; }
.binance-futures-result-tab-meta { display: grid; align-content: space-between; justify-items: end; gap: 5px; min-width: 0; }
.binance-futures-result-tab-meta b { color: var(--text-primary); font-family: var(--font-num); font-size: 14px; font-weight: 900; font-variant-numeric: tabular-nums; white-space: nowrap; }
.binance-futures-result-tab-meta small { padding: 2px 5px; border: 1px solid var(--border-line); border-radius: 999px; color: var(--text-muted); font-size: 10px; font-weight: 900; line-height: 1.2; white-space: nowrap; }
.binance-futures-result-tab.is-armed { border-left: 3px solid var(--binance-up); }
.binance-futures-result-tab.is-armed .binance-futures-result-tab-meta small { border-color: color-mix(in srgb, var(--binance-up) 46%, var(--border-line)); color: var(--binance-up); }
.binance-futures-result-tab.is-trial { border-left: 3px solid #2563eb; }
.binance-futures-result-tab.is-trial .binance-futures-result-tab-meta small { border-color: color-mix(in srgb, #2563eb 42%, var(--border-line)); color: #2563eb; }
.binance-futures-result-tab:not(.is-armed):not(.is-trial) { border-left: 3px solid var(--border-line); }
.binance-futures-result-tab:not(.is-armed):not(.is-trial) .binance-futures-result-tab-meta small { color: var(--text-muted); }
.binance-futures-result-tab.is-active { border-color: var(--binance-gold); background: linear-gradient(145deg, color-mix(in srgb, var(--binance-gold) 12%, var(--panel-solid)), var(--panel-solid)); box-shadow: 0 0 0 2px color-mix(in srgb, var(--binance-gold) 18%, transparent), 0 8px 20px color-mix(in srgb, var(--text-primary) 11%, transparent); }
.binance-futures-result-tab.is-active.is-armed { border-color: var(--binance-up); background: linear-gradient(145deg, color-mix(in srgb, var(--binance-up) 12%, var(--panel-solid)), var(--panel-solid)); }
.binance-futures-result-tab.is-active.is-trial { border-color: #2563eb; background: linear-gradient(145deg, color-mix(in srgb, #2563eb 10%, var(--panel-solid)), var(--panel-solid)); }
.binance-futures-plan-summary { display: grid; grid-template-columns: minmax(0, 1fr) minmax(156px, 200px); align-items: stretch; gap: 12px; min-width: 0; margin-top: 14px; padding-bottom: 14px; border-bottom: 1px solid var(--border-subtle); }
.binance-futures-plan-summary span,
.binance-futures-plan-summary small,
.binance-futures-level-grid span,
.binance-futures-level-grid small { color: var(--text-muted); font-size: 11px; font-weight: 800; }
.binance-futures-plan-summary small { color: var(--text-faint); line-height: 1.35; }
.binance-futures-scan-contract { display: grid; grid-template-columns: minmax(0, 1fr) minmax(84px, 112px); min-width: 0; border-left: 3px solid var(--binance-gold); background: var(--panel-muted); cursor: pointer; }
.binance-futures-scan-contract.is-long { border-color: var(--binance-direction-long); }
.binance-futures-scan-contract.is-short { border-color: var(--binance-direction-short); }
.binance-futures-scan-contract.is-wait { border-color: var(--text-muted); }
.binance-futures-scan-contract:focus-visible { outline: 0; box-shadow: 0 0 0 3px var(--focus-ring); }
.binance-futures-scan-copy { display: grid; gap: 4px; min-width: 0; padding: 10px 12px; }
.binance-futures-scan-symbol { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.binance-futures-scan-symbol strong { overflow: hidden; color: var(--text-primary); font-family: var(--font-tab); font-size: 21px; font-weight: 900; text-overflow: ellipsis; white-space: nowrap; }
.binance-futures-scan-symbol em { font-family: var(--font-tab); font-size: 16px; font-style: normal; font-weight: 900; letter-spacing: .04em; }
.binance-futures-scan-contract small { overflow-wrap: anywhere; }
.binance-futures-risk-reward { display: grid; align-content: center; justify-items: center; gap: 5px; min-width: 0; padding: 10px; border-left: 1px solid var(--border-line); background: color-mix(in srgb, var(--panel-solid) 76%, transparent); text-align: center; }
.binance-futures-risk-reward strong { color: var(--text-primary); font-family: var(--font-num); font-size: 21px; font-weight: 900; }
.binance-futures-create-monitor { width: 100%; height: 100%; min-height: 56px; align-self: stretch; padding: 0 15px; border-color: color-mix(in srgb, var(--binance-gold) 38%, var(--border-line)); border-radius: 0; background: color-mix(in srgb, var(--binance-gold) 8%, transparent) !important; color: var(--text-primary); font-size: 15px; font-weight: 900; line-height: 1.2; }
.binance-futures-create-monitor:hover:not(.is-disabled) { border-color: var(--binance-gold); background: color-mix(in srgb, var(--binance-gold) 13%, transparent) !important; color: var(--text-primary); }
.binance-futures-level-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0; min-width: 0; margin-top: 16px; border: 1px solid var(--border-line); }
.binance-futures-level-grid > div { display: grid; gap: 6px; min-width: 0; padding: 13px; border-right: 1px solid var(--border-line); }
.binance-futures-level-grid > div:last-child { border-right: 0; }
.binance-futures-level-grid strong { overflow: hidden; color: var(--text-primary); font-family: var(--font-pnl); font-size: 16px; font-weight: 900; text-overflow: ellipsis; white-space: nowrap; }
.binance-futures-level-grid small { overflow: hidden; color: var(--text-faint); text-overflow: ellipsis; white-space: nowrap; }
.binance-futures-level-grid .binance-trigger-zone { display: grid; gap: 2px; overflow: visible !important; line-height: 1.35; white-space: normal !important; }
.binance-futures-level-grid .binance-trigger-zone span { color: var(--text-muted); }
.binance-futures-level-grid .binance-trigger-zone em { display: block; color: var(--text-faint); font-size: 10px; font-style: normal; font-weight: 700; line-height: 1.45; overflow-wrap: anywhere; white-space: normal; }
.binance-futures-level-grid .binance-trigger-zone strong { overflow-wrap: anywhere; color: var(--text-faint); font-family: var(--font-pnl); font-size: 10px; font-weight: 800; white-space: normal !important; }
.binance-futures-level-grid .binance-entry-timing-level { background: color-mix(in srgb, #0891b2 6%, var(--panel-solid)); }
.binance-futures-level-grid .binance-entry-timing-level.is-ready { box-shadow: inset 0 3px 0 #0891b2; }
.binance-futures-level-grid .binance-entry-timing-level.is-ready strong { color: #0891b2; }
.binance-futures-level-grid .binance-entry-timing-level.has-reference { box-shadow: inset 0 3px 0 #64748b; }
.binance-futures-level-grid .binance-entry-timing-level strong.is-pending { color: var(--text-muted); font-size: 14px; }
.binance-futures-model-output { min-width: 0; margin-top: 16px; border: 1px solid color-mix(in srgb, var(--binance-gold) 45%, var(--border-line)); background: color-mix(in srgb, var(--binance-gold) 4%, var(--panel-solid)); }
.binance-futures-model-output-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; min-width: 0; padding: 10px 13px; border-bottom: 1px solid var(--border-line); background: color-mix(in srgb, var(--binance-gold) 8%, var(--panel-muted)); }
.binance-futures-model-output-head > div { display: grid; gap: 3px; min-width: 0; }
.binance-futures-model-output-kicker { color: var(--text-faint); font-family: var(--font-tab); font-size: 10px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }
.binance-futures-model-output-head strong { color: var(--text-primary); font-size: 14px; font-weight: 900; }
.binance-futures-model-verdict { flex: 0 0 auto; padding: 3px 8px; border: 1px solid var(--border-line); color: var(--text-muted); font-size: 11px; font-weight: 900; }
.binance-futures-model-verdict.is-favorable { border-color: color-mix(in srgb, var(--binance-up) 52%, var(--border-line)); color: var(--binance-up); }
.binance-futures-model-verdict.is-uncertain { border-color: color-mix(in srgb, var(--binance-gold) 60%, var(--border-line)); color: var(--binance-gold); }
.binance-futures-model-verdict.is-wait { border-color: color-mix(in srgb, var(--text-muted) 55%, var(--border-line)); color: var(--text-muted); }
.binance-futures-model-output-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); min-width: 0; }
.binance-futures-model-output-group { display: grid; align-content: start; gap: 7px; min-width: 0; padding: 12px 13px; border-right: 1px solid var(--border-line); }
.binance-futures-model-output-group:last-child { border-right: 0; }
.binance-futures-model-output-group.is-impact { grid-column: 1 / -1; grid-template-columns: repeat(3, minmax(0, 1fr)); border-top: 1px solid var(--border-line); border-right: 0; }
.binance-futures-model-output-group > strong { grid-column: 1 / -1; margin-bottom: 1px; color: var(--text-primary); font-size: 12px; font-weight: 900; }
.binance-futures-model-output-group > div { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; min-width: 0; }
.binance-futures-model-output-group > div span { min-width: 0; color: var(--text-muted); font-size: 11px; font-weight: 700; line-height: 1.35; }
.binance-futures-model-output-group > div b { min-width: 0; color: var(--text-primary); font-family: var(--font-num); font-size: 12px; font-weight: 900; text-align: right; overflow-wrap: anywhere; }
.binance-futures-model-output-group > div b.is-breakable { overflow-wrap: anywhere; }
.binance-futures-evidence { min-width: 0; margin-top: 16px; border: 1px solid var(--border-line); }
.binance-futures-evidence-head { display: flex; align-items: center; justify-content: space-between; min-height: 38px; padding: 0 10px 0 13px; background: var(--panel-muted); }
.binance-futures-evidence-head strong { color: var(--text-primary); font-size: 12px; font-weight: 900; }
.binance-futures-evidence-toggle { display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; padding: 0; border: 1px solid transparent; border-radius: 4px; background: transparent; color: var(--text-muted); cursor: pointer; }
.binance-futures-evidence-toggle:hover { border-color: var(--border-line); background: var(--panel-solid); color: var(--text-primary); }
.binance-futures-evidence-toggle:focus-visible { outline: 0; box-shadow: 0 0 0 3px var(--focus-ring); }
.binance-futures-timeframes { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0; margin-top: 0; border-top: 1px solid var(--border-line); border-bottom: 1px solid var(--border-line); }
.binance-futures-timeframes article { min-width: 0; padding: 13px 14px; border-right: 1px solid var(--border-line); }
.binance-futures-timeframes article:last-child { border-right: 0; }
.binance-futures-timeframes article > div { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
.binance-futures-timeframes article span { color: var(--text-primary); font-family: var(--font-tab); font-size: 13px; font-weight: 900; }
.binance-futures-timeframes article strong { font-size: 12px; font-weight: 900; }
.binance-futures-timeframes p,
.binance-futures-reason-grid p { margin: 0 0 5px; color: var(--text-muted); font-size: 11px; font-weight: 700; line-height: 1.5; }
.binance-futures-timeframes p:last-child,
.binance-futures-reason-grid p:last-child { margin-bottom: 0; }
.binance-futures-reason-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; margin-top: 16px; padding: 0 14px 14px; }
.binance-futures-reason-grid > div { min-width: 0; }
.binance-futures-reason-grid > div > span { display: block; margin-bottom: 8px; color: var(--text-primary); font-size: 12px; font-weight: 900; }


@media (max-width: 1080px) {
  .binance-content-grid { grid-template-columns: minmax(0, 1fr); grid-template-areas: 'chart' 'search' 'positions'; gap: 16px; }
.binance-search-panel,
  .binance-simulated-panel { min-height: 0; }
  .binance-smart-money-grid { grid-template-columns: minmax(0, 1fr); grid-template-areas: 'positions' 'records'; }
}

@media (max-width: 1400px) {
  .binance-market-workspace { grid-template-columns: 1fr; }
  .binance-simulated-item-head { align-items: stretch; flex-direction: column; }
  .binance-simulated-item-actions { justify-content: flex-end; }
  .binance-market-chart-panel { padding-top: 16px; border-top: 1px solid var(--border-subtle); }
}

@media (max-width: 820px) {
  .binance-copy-trading-head { align-items: stretch; flex-direction: column; gap: 10px; }
  .binance-copy-trading-controls { align-items: stretch; width: 100%; }
  .binance-copy-trader-list { justify-content: flex-start; }
  .binance-copy-trader-subscriptions { flex-wrap: nowrap; overflow-x: auto; overflow-y: hidden; padding: 1px 1px 7px; scrollbar-width: thin; }
  .binance-copy-trader-subscription-item { flex: 0 0 218px; width: 218px; max-width: 218px; min-height: 58px; padding: 7px 8px; gap: 7px; }
  .binance-copy-trader-subscription-item img,
  .binance-copy-trader-subscription-avatar-fallback { flex-basis: 34px; width: 34px; height: 34px; }
  .binance-copy-trader-subscription-content { gap: 2px; }
  .binance-copy-trader-subscription-content > strong { font-size: 12px; }
  .binance-copy-trader-subscription-stats { gap: 2px 6px; font-size: 9px; }
  .binance-copy-trader-subscription-stats b { font-size: 10px; }
  .binance-smart-position-tabs { flex-wrap: nowrap; overflow-x: auto; overflow-y: hidden; }
  .binance-smart-position-detail { grid-template-columns: minmax(0, 1fr); gap: 10px; }
  .binance-smart-position-side { padding-top: 10px; padding-left: 0; border-top: 1px solid var(--border-line); border-left: 0; }
  .binance-smart-position-grid { grid-template-columns: minmax(52px, .5fr) minmax(0, 1fr) minmax(0, 1fr); }
  .binance-smart-grid-title,
  .binance-smart-grid-pnl,
  .binance-smart-grid-value { padding-left: 7px; }
  .binance-smart-grid-pnl > strong { font-size: 15px; }
  .binance-smart-position-pnl-pair { flex: 1 1 100%; width: 100%; max-width: none; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .binance-smart-position-pnl > strong { font-size: 17px; line-height: 1.2; }
  .binance-futures-analysis { display: block; }
  .binance-futures-result-tabs { margin-top: 16px; }
  .binance-futures-result-tabs > div { flex-direction: row; align-items: stretch; max-height: none; padding: 2px 1px 5px; overflow-x: auto; overflow-y: hidden; }
  .binance-futures-result-tab { flex: 0 0 148px; width: 148px; min-width: 0; max-width: 148px; min-height: 58px; }
  .binance-simulated-workspace { align-items: stretch; flex-direction: column; }
  .binance-execution-tabs { flex: none; flex-direction: row; align-items: stretch; width: 100%; padding-bottom: 7px; overflow-x: auto; scrollbar-width: thin; }
  .binance-execution-tab { flex: 0 0 148px; width: 148px; min-width: 148px; max-width: 148px; }
  .binance-execution-tab-head,
  .binance-execution-tab-summary { width: 100%; max-width: 100%; }
  .binance-page-head { align-items: flex-start; flex-direction: column; }
  .binance-head-actions { width: 100%; justify-content: space-between; }
  .binance-panel { padding: 14px; }
  .binance-copy-trading-head { flex-direction: column; }
  .binance-copy-trading-controls { align-items: stretch; width: 100%; }
  .binance-copy-trader-list { justify-content: flex-start; }
  /* A fixed chart hides the currently selected Smart Money position on a
     narrow screen, especially while the feed is waiting for market data. */
  .binance-chart-stage { top: 72px; align-self: start; margin: 0; padding: 14px; border: 1px solid var(--border-line); border-radius: 8px; background: var(--panel-bg); box-shadow: var(--shadow-panel); overflow: visible; }
  .binance-chart-stage-head { display: none; }
  .binance-chart-collapse-toggle {
    position: absolute;
    right: 4px;
    bottom: 4px;
    /* Chart controls may float above the chart, but never above account overlays. */
    z-index: 1101;
    display: inline-grid;
    width: 30px;
    height: 30px;
    place-items: center;
    padding: 0;
    border: 1px solid var(--brand-border);
    border-radius: 6px;
    background: color-mix(in srgb, var(--panel-solid) 92%, var(--brand));
    color: var(--brand);
    box-shadow: 0 4px 12px rgba(31, 48, 64, 0.18);
    cursor: pointer;
  }
  .binance-chart-collapse-toggle:hover { border-color: var(--brand); }
  .binance-chart-collapse-toggle:focus-visible {
    outline: 0;
    box-shadow: 0 0 0 3px var(--focus-ring), 0 4px 12px rgba(31, 48, 64, 0.18);
  }
  .binance-chart-stage.is-chart-collapsed {
    height: 0;
    min-height: 0;
    margin: 0;
    padding: 0;
    border: 0;
    background: transparent;
    box-shadow: none;
  }
  .binance-chart-stage.is-chart-collapsed .binance-market-chart-panel { display: none; }
  .binance-chart-stage.is-chart-collapsed .binance-chart-collapse-toggle {
    position: fixed;
    right: 4px;
    top: 58px;
    bottom: auto;
  }
  .binance-chart-stage .binance-market-chart-panel { padding-top: 0; border-top: 0; }
  .binance-chart-layout { display: grid; grid-template-columns: minmax(0, 1fr) 44px; align-items: start; gap: 8px; overflow: visible; }
  .binance-chart-main { grid-column: 1; grid-row: 1; }
  .binance-chart-periods { position: relative; z-index: 1100; grid-column: 2; grid-row: 1; padding-top: 2px; }
  .binance-chart-periods :deep(.el-segmented) { width: 44px; padding: 2px; }
  .binance-chart-periods :deep(.el-segmented__group) { flex-direction: column; width: 100%; }
  .binance-chart-periods :deep(.el-segmented__item-selected) { display: none !important; }
  .binance-chart-periods :deep(.el-segmented__item) { width: 100%; min-height: 29px; padding: 0 3px; }
  .binance-chart-periods :deep(.el-segmented__item.is-selected) { border-radius: 4px; background: var(--brand-soft); color: var(--brand); box-shadow: inset 0 0 0 1px var(--brand-border); }
  .binance-chart-periods :deep(.el-segmented__item-label) { font-size: 10px; line-height: 27px; }
  .binance-chart-shell > .binance-market-stale.is-connection-warning { top: 5px; right: 5px; max-width: calc(100% - 10px); padding: 4px 6px; font-size: 9px; }
  .binance-market-head { align-items: stretch; flex-direction: column; }
  .binance-market-actions { grid-template-columns: 1fr; justify-content: stretch; }
  .binance-market-actions { width: 100%; flex-basis: auto; }
  .binance-market-target-row,
  .binance-market-scan-row { justify-content: flex-start; }
  .binance-market-workspace { grid-template-columns: 1fr; }
  .binance-market-chart-panel { padding-top: 16px; border-top: 1px solid var(--border-subtle); }
  .binance-chart-stage .binance-market-chart-panel { padding-top: 0; }
  .binance-simulated-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); row-gap: 12px; }
  .binance-simulated-levels { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .binance-simulated-levels > div:nth-child(2n) { border-right: 0; }
  .binance-simulated-levels > div:nth-child(5) { border-right: 0; }
  .binance-simulated-levels > div:nth-child(-n + 2) { border-bottom: 1px solid var(--border-line); }
  .binance-futures-progress-head { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .binance-futures-plan-summary { grid-template-columns: minmax(0, 1fr) minmax(146px, 170px); }
  .binance-futures-level-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .binance-futures-level-grid > div:nth-child(2) { border-right: 0; }
  .binance-futures-level-grid > div:nth-child(-n + 2) { border-bottom: 1px solid var(--border-line); }
  .binance-futures-model-output-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .binance-futures-model-output-group:nth-child(2) { border-right: 0; }
  .binance-futures-model-output-group:nth-child(-n + 2) { border-bottom: 1px solid var(--border-line); }
  .binance-futures-model-output-group.is-impact { grid-template-columns: 1fr; }
  .binance-selected-model-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .binance-futures-timeframes { grid-template-columns: 1fr; }
  .binance-futures-timeframes article { border-right: 0; border-bottom: 1px solid var(--border-line); }
  .binance-futures-timeframes article:last-child { border-bottom: 0; }
  .binance-futures-reason-grid { grid-template-columns: 1fr; gap: 14px; }
}

@media (max-width: 480px) {
  .binance-heading h1 { font-size: 23px; }
  .binance-heading p { max-width: 255px; font-size: 12px; line-height: 1.4; }
  .binance-market-actions { align-items: stretch; }
  .binance-market-target-row { grid-template-columns: 1fr; align-items: stretch; }
  .binance-market-target-row > :deep(.binance-market-search) { width: 100%; }
  .binance-market-target-row .el-button { width: 100%; }
  .binance-connection-lines { grid-template-columns: 1fr; }
  .binance-connection-settings-section-head .el-button { width: 100%; margin-top: 2px; }
  .binance-stored-credentials { align-items: flex-start; }
  .binance-selected-model-summary-grid { grid-template-columns: 1fr; }
  .binance-futures-limit { flex: 0 0 auto; }
  .binance-futures-progress { padding: 12px; }
  .binance-futures-progress-head { grid-template-columns: 1fr; gap: 12px; }
  .binance-futures-progress-head > div:last-child { justify-items: start; text-align: left; }
  .binance-futures-plan-summary { grid-template-columns: 1fr; gap: 9px; }
  .binance-futures-create-monitor { width: 100%; min-width: 0; height: auto; min-height: 48px; padding: 10px 15px; }
  .binance-futures-model-output-grid { grid-template-columns: 1fr; }
  .binance-futures-model-output-group,
  .binance-futures-model-output-group:nth-child(2) { border-right: 0; border-bottom: 1px solid var(--border-line); }
  .binance-futures-model-output-group:last-child { border-bottom: 0; }
  .binance-simulated-symbol-main { gap: 6px; }
  .binance-simulated-symbol-main strong { font-size: 18px; }
  .binance-simulated-item-actions { flex-wrap: wrap; justify-content: flex-end; }
  .binance-simulated-pnl-metric .binance-pnl-percent { font-size: 27px !important; }
  .binance-simulated-item-foot { align-items: flex-start; flex-direction: column; gap: 8px; }
  .binance-simulated-item-foot-actions { align-items: flex-start; flex-direction: column; width: 100%; }
  .binance-futures-account-summary { grid-template-columns: minmax(0, 1.2fr) minmax(0, .8fr); }
  .binance-futures-position-bottom { grid-template-columns: minmax(0, 1fr); align-items: start; }
  .binance-futures-stop-tp,
  .binance-futures-position-foot-actions { grid-column: 1; }
  .binance-futures-position-foot-actions { width: 100%; justify-content: flex-end; }
  .binance-live-monitor-preview-grid,
  .binance-live-monitor-preview-levels { grid-template-columns: 1fr; }
  .binance-position-form-grid { grid-template-columns: 1fr; }
  .binance-strategy-settings-grid { grid-template-columns: 1fr; }
  .binance-smart-money-auth-grid,
  .binance-websocket-test-actions { grid-template-columns: 1fr; }
  .binance-protection-form-grid { grid-template-columns: 1fr; }
  .binance-protection-section-head { align-items: flex-start; flex-direction: column; gap: 4px; }
  .binance-protection-section-head span { text-align: left; }
  .binance-position-risk-toggle-copy { align-items: flex-start; flex-direction: column; gap: 2px; }
  .binance-position-risk-grid { grid-template-columns: 1fr; }
  .binance-position-loss-inline { align-items: flex-start; flex-direction: column; }
  .binance-position-loss-inline-values { align-items: flex-start; justify-content: flex-start; text-align: left; }
  .binance-position-dialog-footer { align-items: stretch; flex-direction: column; gap: 10px; }
  .binance-live-order-control { justify-content: flex-start; }
  .binance-position-dialog-actions { justify-content: flex-end; }
  .binance-position-loss-head { flex-direction: column; gap: 5px; }
  .binance-position-loss-ratio { text-align: left; }
  .binance-position-loss-grid { grid-template-columns: 1fr; }
  .binance-position-ratio-preview { grid-template-columns: 1fr 1fr; }
  .binance-position-ratio-preview small { grid-column: 1 / -1; }
  .binance-websocket-account-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .binance-websocket-account-position { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .binance-connection-mode-head { align-items: flex-start; flex-direction: column; }
  .binance-connection-mode-badge { align-self: flex-start; }
  .binance-connection-lines { grid-template-columns: 1fr; }
  .binance-account-drawer { top: 58px; bottom: auto; width: calc(100vw - 8px); height: min(60vh, calc(100vh - 90px)); max-height: 60vh; }
  .binance-account-drawer.is-collapsed { top: auto; right: 8px; bottom: calc(76px + env(safe-area-inset-bottom)); width: 96px; height: 46px; max-height: 46px; }
  .binance-account-panel { border-radius: 8px 0 0 8px; }
}

/* Binance typography: LXGW WenKai is the page UI face; data values keep
   their dedicated numeric faces for alignment and quick scanning. */
.binance-simulated-empty,
.binance-futures-empty {
  font-size: 13px;
  font-weight: 500;
}

.binance-futures-progress-head span,
.binance-futures-result-tabs > span {
  font-size: 12px;
  font-weight: 500;
  line-height: 1.5;
}

.binance-simulated-metrics span,
.binance-simulated-levels span,
.binance-futures-plan-summary span,
.binance-futures-level-grid span,
.binance-futures-plan-summary small,
.binance-futures-level-grid small {
  font-size: 11px;
  font-weight: 500;
}

.binance-futures-reason-grid p,
.binance-simulated-reasons p,
.binance-simulated-management-note p {
  font-size: 12px;
  font-weight: 500;
  line-height: 1.55;
}

.binance-auth-gate strong {
  font-size: 15px;
  font-weight: 700;
}

.binance-auth-gate p {
  font-size: 13px;
  font-weight: 500;
}

.binance-page :deep(.el-button),
.binance-page :deep(.el-input__inner),
.binance-page :deep(.el-textarea__inner),
.binance-page :deep(.el-form-item__label),
.binance-page :deep(.el-radio-button__inner),
.binance-page :deep(.el-checkbox__label),
.binance-page :deep(.el-switch__label),
.binance-page :deep(.el-tabs__item),
.binance-page :deep(.el-segmented__item-label) {
  font-family: var(--font-base) !important;
}

.binance-page :deep(.el-button) {
  font-size: 13px;
  font-weight: 600;
}

.binance-page :deep(.el-input__inner),
.binance-page :deep(.el-textarea__inner) {
  font-size: 13px;
  font-weight: 400;
}

.binance-page :deep(.el-form-item__label),
.binance-page :deep(.el-radio-button__inner),
.binance-page :deep(.el-checkbox__label),
.binance-page :deep(.el-switch__label),
.binance-page :deep(.el-tabs__item),
.binance-page :deep(.el-segmented__item-label) {
  font-size: 13px;
  font-weight: 500;
}

/* Dialogs are teleported to body, so they need a global selector. */
:global(.binance-position-dialog),
:global(.binance-position-dialog *) {
  font-family: var(--font-base) !important;
}

:global(.binance-position-dialog .el-dialog__title) {
  font-size: 18px;
  font-weight: 700;
}

/* 自定义类挂在 .el-dialog 根元素自身，必须是复合选择器而非后代选择器。 */
:global(.binance-create-monitor-dialog.el-dialog) {
  display: flex;
  flex-direction: column;
  max-height: calc(100dvh - 24px);
  margin: 12px auto !important;
}

:global(.binance-create-monitor-dialog .el-dialog__body) {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
}

:global(.binance-create-monitor-dialog .el-dialog__footer) {
  flex: 0 0 auto;
}

:global(.binance-copy-trade-dialog.el-dialog) {
  display: flex;
  flex-direction: column;
  max-height: calc(100dvh - 24px);
  margin: 12px auto !important;
}

:global(.binance-copy-trade-dialog .el-dialog__body) {
  flex: 1 1 auto;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
}

:global(.binance-copy-trade-dialog .el-dialog__footer) {
  flex: 0 0 auto;
}

:global(.binance-copy-trade-dialog .binance-position-form-grid > *) {
  min-width: 0;
}

.binance-change-value {
  font-family: var(--binance-change-font) !important;
  font-variant-numeric: tabular-nums lining-nums;
  font-weight: 600 !important;
  letter-spacing: .01em;
}

/* Information hierarchy: labels remain calm, while tradable values and the
   current context can be read from a normal desktop viewing distance. */
.binance-page .binance-heading h1 {
  font-size: 30px;
  font-weight: 800;
  line-height: 1.2;
}

.binance-page .binance-panel-kicker {
  margin-bottom: 4px;
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: .08em;
}

.binance-page .binance-panel h2,
.binance-page .binance-panel h3,
.binance-page .binance-smart-money-subhead h3 {
  font-size: 19px;
  font-weight: 800;
  line-height: 1.3;
}

.binance-page .binance-copy-trader-list,
.binance-page .binance-copy-trader-list-title,
.binance-page .binance-copy-trader-subscription-stats,
.binance-page .binance-smart-grid-label,
.binance-page .binance-smart-position-deviation,
.binance-page .binance-smart-position-mark,
.binance-page .binance-smart-follow-account-info {
  font-size: 11px;
}

.binance-page .binance-copy-trader-list-item small,
.binance-page .binance-smart-position-tab-head em,
.binance-page .binance-smart-position-tab-pnl small,
.binance-page .binance-smart-grid-value small {
  font-size: 10px;
}

.binance-page .binance-copy-trader-subscription-stats b,
.binance-page .binance-smart-position-tab-pnl strong,
.binance-page .binance-smart-grid-value strong,
.binance-page .binance-smart-position-deviation > strong,
.binance-page .binance-smart-position-mark strong {
  font-size: 13px;
}

.binance-page .binance-smart-position-tab-head strong {
  font-size: 13px;
}

/* Logged-in Smart Money data is a decision surface, not metadata. Keep the
   trader comparison and the latest executable record legible at a glance. */
.binance-page .binance-copy-trader-subscription-item {
  min-height: 76px;
  padding: 10px 11px;
}

.binance-page .binance-copy-trader-subscription-content > strong {
  font-size: 14px;
  font-weight: 800;
}

.binance-page .binance-copy-trader-subscription-content > small,
.binance-page .binance-copy-trader-subscription-stats {
  font-size: 12px;
  line-height: 1.4;
}

.binance-page .binance-copy-trader-subscription-stats b {
  font-size: 13px;
  font-weight: 800;
}

.binance-page .binance-smart-position-tab {
  flex-basis: 168px;
  width: 168px;
  padding: 9px 10px;
}

.binance-page .binance-smart-position-tab-pnl small,
.binance-page .binance-smart-grid-label {
  font-size: 11px;
}

.binance-page .binance-smart-position-tab-pnl strong,
.binance-page .binance-smart-grid-value strong {
  font-size: 14px;
}

.binance-page .binance-smart-position-deviation {
  gap: 6px;
  padding: 11px 12px;
  font-size: 12px;
  line-height: 1.45;
}

.binance-page .binance-smart-position-deviation > span {
  font-size: 13px;
}

.binance-page .binance-smart-position-deviation > strong {
  font-size: 14px;
}

.binance-page .binance-smart-position-deviation > small,
.binance-page .binance-smart-position-mark {
  font-size: 12px;
}

.binance-page .binance-smart-position-mark strong {
  font-size: 14px;
}

.binance-page .binance-copy-trading-item {
  padding: 13px 14px;
}

.binance-page .binance-copy-trading-order-type,
.binance-page .binance-copy-action {
  font-size: 11px;
}

.binance-page .binance-copy-trading-meta {
  gap: 6px 14px;
  margin-top: 7px;
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 600;
  line-height: 1.45;
}

@media (max-width: 820px) {
  .binance-page .binance-heading h1 { font-size: 25px; }

  .binance-page .binance-panel h2,
  .binance-page .binance-panel h3,
  .binance-page .binance-smart-money-subhead h3 { font-size: 18px; }

  .binance-page .binance-copy-trader-subscription-item {
    flex-basis: 232px;
    width: 232px;
    min-height: 68px;
    padding: 8px 9px;
  }

  .binance-page .binance-copy-trader-subscription-content > strong { font-size: 13px; }

  .binance-page .binance-copy-trader-subscription-content > small,
  .binance-page .binance-copy-trader-subscription-stats { font-size: 11px; }

  .binance-page .binance-copy-trader-subscription-stats b { font-size: 12px; }

  .binance-page .binance-smart-position-tab {
    flex-basis: 158px;
    width: 158px;
  }

  .binance-page .binance-copy-trading-item { padding: 12px; }

  .binance-page .binance-copy-trading-meta { font-size: 11px; }
}

@media (prefers-reduced-motion: reduce) {
  .binance-panel,
  .binance-smart-position-tab,
  .binance-account-drawer,
  .binance-account-drawer-handle { transition-duration: 0.01ms !important; }
}
</style>
