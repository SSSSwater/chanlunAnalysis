<template>
  <section v-if="hasReading" :class="['price-action-decision', compact ? 'is-compact' : '']">
    <header class="price-action-decision-head">
      <div>
        <small>{{ contextLabel }}</small>
        <strong>{{ modeLabel }} · {{ patternLabel }}</strong>
      </div>
      <span :class="`is-${String(lifecycle).toLowerCase()}`">{{ lifecycleLabel }}</span>
    </header>

    <section v-if="hasStrategyPlan" :class="['a-share-strategy-summary', compact ? 'is-compact' : '']" aria-label="A股策略计划状态">
      <div>
        <small>策略</small>
        <strong>{{ strategyPlan.strategyModeLabel }}</strong>
      </div>
      <div>
        <small>条件</small>
        <strong>{{ strategyPlan.conditionMet }}/{{ strategyPlan.conditionTotal }}</strong>
      </div>
      <div>
        <small>执行时机</small>
        <strong>{{ strategyTimingLabel }}</strong>
      </div>
      <span :class="`is-${String(strategyPlan.status || 'watch').toLowerCase()}`">{{ strategyStatusLabel }}</span>
    </section>

    <p v-if="hasStrategyPlan && strategyMissingCondition" class="a-share-strategy-missing"><b>仍需满足</b>{{ strategyMissingCondition }}</p>

    <p v-if="hasPosition && trailingStop.applicable" :class="['a-share-trailing-stop', trailingStop.active ? 'is-active' : 'is-waiting']">
      <b>移动止损</b>
      <strong>{{ trailingStop.active ? price(trailingStop.stopPrice) : trailingStop.stateLabel }}</strong>
      <span>{{ trailingStopDetail }}</span>
    </p>

    <details v-if="hasStrategyPlan && strategyChecks.length" class="a-share-strategy-conditions">
      <summary>查看策略条件</summary>
      <span v-for="item in strategyChecks" :key="item.id" :class="item.passed ? 'is-passed' : 'is-missing'">
        <b>{{ item.passed ? '已满足' : '待满足' }}</b>{{ item.label }}
      </span>
    </details>

    <p class="price-action-decision-next"><b>下一步</b>{{ nextCondition }}</p>

    <div class="price-action-decision-levels">
      <template v-if="hasPosition">
        <span class="is-defense">
          <small>{{ defenseLabel }}</small>
          <strong>{{ price(defensePrice) }}</strong>
          <em>{{ defenseMeaning }}</em>
        </span>
        <span class="is-target">
          <small>{{ targetLabel }}</small>
          <strong>{{ price(firstTarget) }}</strong>
          <em>{{ targetMeaning }}</em>
        </span>
        <span class="is-trigger">
          <small>{{ entryLabel }}</small>
          <strong>{{ price(entryPrice) }}</strong>
          <em>{{ entryMeaning }}</em>
        </span>
      </template>
      <template v-else>
        <span class="is-trigger">
          <small>{{ entryLabel }}</small>
          <strong>{{ price(entryPrice) }}</strong>
          <em>{{ entryMeaning }}</em>
        </span>
        <span class="is-defense">
          <small>{{ defenseLabel }}</small>
          <strong>{{ price(defensePrice) }}</strong>
          <em>{{ defenseMeaning }}</em>
        </span>
        <span class="is-target">
          <small>{{ targetLabel }}</small>
          <strong>{{ price(firstTarget) }}</strong>
          <em>{{ targetMeaning }}</em>
        </span>
      </template>
    </div>

    <div v-if="sessionPatterns.length" class="price-action-session-conditions">
      <span v-for="item in sessionPatterns" :key="`${item.type}-${item.observedAt}`">
        <b>{{ patternName(item.type) }}</b>{{ statusName(item.status) }}：{{ item.nextCondition || (item.missingConditions || [])[0] || '等待完整分钟K确认' }}
      </span>
    </div>

    <details v-if="evidence.length || sourcePages.length" class="price-action-decision-audit">
      <summary>结构依据</summary>
      <p v-for="item in evidence" :key="item">{{ item }}</p>
      <small v-if="sourcePages.length">页码索引：{{ sourcePages.join('；') }}</small>
    </details>

    <footer>
      <PointDerivationDialog
        :plan="derivationPlan"
        :levels="levels"
        :context="context"
        :minimum-reward-risk="minimumRewardRisk"
        :account-risk-exit="levels.accountRiskExit"
        :title="title"
      />
    </footer>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import PointDerivationDialog from './PointDerivationDialog.vue'

const props = defineProps({
  priceAction: { type: Object, default: () => ({}) },
  plan: { type: Object, default: () => ({}) },
  context: { type: String, default: 'analysis' },
  title: { type: String, default: '' },
  compact: { type: Boolean, default: false },
  intraday: { type: Boolean, default: false },
  minimumRewardRisk: { type: [Number, String], default: 1.5 },
})

const labels = {
  TREND_PULLBACK_H1: '上涨后的第一次回踩', TREND_PULLBACK_H2: '上涨后的第二次回踩', TREND_PULLBACK_L1: '下跌后的第一次反弹', TREND_PULLBACK_L2: '下跌后的第二次反弹',
  TREND_FIRST_PULLBACK: '趋势第一次回踩', TREND_TWO_LEG_PULLBACK: '两次回踩后的再启动', BULL_FLAG: '上涨后的整理', BEAR_FLAG: '下跌后的整理',
  BREAKOUT_UP: '向上突破', BREAKOUT_DOWN: '向下突破', BREAKOUT_RETEST_UP: '向上突破后回踩', BREAKOUT_RETEST_DOWN: '向下突破后反弹',
  MICRO_CHANNEL_BREAK: '短线通道被突破', SPIKE_CHANNEL: '快速推进后的通道', WIDE_CHANNEL: '宽幅通道', STEP_CHANNEL: '阶梯式推进', EMA_GAP_CONTEXT: '价格明显离开20周期线',
  RANGE_EDGE_FADE_UP: '区间下沿反弹', RANGE_EDGE_FADE_DOWN: '区间上沿回落', RANGE_LOWER_REVERSAL: '区间下沿反弹', RANGE_UPPER_REVERSAL: '区间上沿回落', RANGE_MIDDLE: '区间中部观望',
  TIGHT_RANGE_IRON_WIRE: '窄幅反复拉锯', TRIANGLE_COMPRESSION: '波动收窄，等待突破', TRIANGLE_EXPANSION: '波动放大，方向不稳', ABC_RANGE_LEGS: '区间内三段摆动',
  RANGE_BREAKOUT_PENDING: '突破出现，等待确认', RANGE_BREAKOUT_CONFIRMED: '突破已经站稳', RANGE_BREAKOUT_CONTINUATION: '突破后继续推进', RANGE_BREAKOUT_RETEST: '突破后回踩确认', FAILED_BULLISH_BREAKOUT: '向上突破失败', FAILED_BEARISH_BREAKOUT: '向下突破失败', BREAKOUT_FAILURE_OF_FAILURE: '失败突破后的再测试',
  DOUBLE_BOTTOM: '双底', DOUBLE_TOP: '双顶', WEDGE_THIRD_PUSH: '楔形第三推动', CLIMAX_SPIKE_REVERSAL: '高潮/尖峰反转候选', CLIMAX_REVERSAL_CANDIDATE: '高潮反转候选', V_REVERSAL_WARNING: 'V形反转警示',
  HEAD_SHOULDERS_REVERSAL: '头肩形反转', EXPANSION_REVERSAL: '扩张反转候选', FINAL_FLAG_OR_INSIDE_BREAK: '最终旗形/内包突破', MAJOR_BEARISH_REVERSAL: '主要向下反转', MAJOR_BULLISH_REVERSAL: '主要向上反转', REVERSAL_FAILURE: '反转尝试失败', REVERSAL_FAILURE_OF_FAILURE: '反转失败再测试',
  OPENING_TREND: '开盘趋势', OPENING_REVERSAL: '开盘反转', TREND_FROM_RANGE: '开盘区间后起趋势', TREND_DAY: '趋势日候选', TREND_RESUMPTION: '午后趋势恢复', CLOSE_WINDOW_REVIEW: '收盘窗口复核', OPENING_DATA_INSUFFICIENT: '开盘数据不足',
  NO_CONFIRMED_PATTERN: '等待结构确认', WAIT_FOR_ENTRY: '等待合规买入位置',
}
const lifecycleLabels = { CONFIRMED: '已确认', ARMED: '已就绪', TRIAL: '试探复核', BLOCKED: '已阻断', PENDING: '待确认', WATCH: '观察', NEEDS_REVIEW: '需复核', FAILED: '已失败', EXPIRED: '已过期', DATA_INSUFFICIENT: '数据不足' }

const hierarchy = computed(() => props.plan?.decisionHierarchy || {})
const levels = computed(() => props.plan?.levels || {})
const strategyPlan = computed(() => props.plan?.strategyPlan || props.priceAction?.strategyPlan || props.plan?.priceAction?.strategyPlan || {})
const hasStrategyPlan = computed(() => Boolean(strategyPlan.value?.strategyId))
const strategyChecks = computed(() => Array.isArray(strategyPlan.value?.conditionChecks) ? strategyPlan.value.conditionChecks : [])
const strategyMissingCondition = computed(() => (strategyPlan.value?.missingConditions || [])[0] || '')
const strategyStatusLabel = computed(() => ({ ARMED: '已就绪', TRIAL: '试探复核', WATCH: '等待', BLOCKED: '已阻断' })[strategyPlan.value?.status] || '等待')
const strategyTimingLabel = computed(() => ({ WAIT_TRIGGER: '等触发', READY: '可执行', NEXT_SESSION_SELL: '次日卖出', MANAGING: '持仓管理', EXPOSURE_ONLY: '暴露建议', REASSESS: '重新评估', WAIT_STRUCTURE: '等结构', BLOCKED: '受限' })[strategyPlan.value?.entryTiming?.state] || '等待')
const trailingStop = computed(() => strategyPlan.value?.trailingStop || {})
const trailingStopDetail = computed(() => {
  const trail = trailingStop.value
  if (!trail?.active) return trail?.reason || '完整日K达到成本 + 1R 后启用。'
  const peak = Number(trail.peakPrice)
  return `${trail.stateLabel || 'ATR跟踪'}${peak > 0 ? `，持仓峰值 ${peak.toFixed(2)}` : ''}；已启用后只收紧。`
})
const setups = computed(() => props.priceAction?.setups || props.plan?.priceAction?.setups || [])
const tradePlans = computed(() => props.priceAction?.tradePlans || props.plan?.priceAction?.tradePlans || props.plan?.tradePlans || [])
const activeSetup = computed(() => {
  const type = hierarchy.value.patternType
  return setups.value.find((item) => item.type === type || item.setupType === type)
    || setups.value.find((item) => item.status === 'CONFIRMED' && !item.reviewRequired)
    || setups.value.find((item) => item.direction === 'BUY' || item.direction === 'SELL')
    || {}
})
const activePlan = computed(() => tradePlans.value.find((item) => item.setupId && item.setupId === activeSetup.value.setupId) || tradePlans.value[0] || {})
const mode = computed(() => strategyPlan.value?.marketMode || hierarchy.value.marketMode || activeSetup.value.marketMode || activePlan.value.marketMode || 'REBOUND')
const modeLabel = computed(() => strategyPlan.value?.marketModeLabel || hierarchy.value.marketModeLabel || activeSetup.value.marketModeLabel || ({ TREND: '趋势', RANGE: '区间', REBOUND: '反弹' })[mode.value] || '反弹')
const patternType = computed(() => hierarchy.value.patternType || activeSetup.value.type || activePlan.value.setupType || activePlan.value.type || 'NO_CONFIRMED_PATTERN')
const patternLabel = computed(() => labels[patternType.value] || '等待结构确认')
const lifecycle = computed(() => strategyPlan.value?.status || hierarchy.value.lifecycle || activeSetup.value.lifecycle || activeSetup.value.status || activePlan.value.status || 'WATCH')
const lifecycleLabel = computed(() => lifecycleLabels[lifecycle.value] || lifecycle.value)
const nextCondition = computed(() => strategyMissingCondition.value || strategyPlan.value?.entryTiming?.reason || hierarchy.value.nextCondition || activeSetup.value.nextCondition || (activeSetup.value.missingConditions || [])[0] || '等待下一根完整K线，重新确认环境、位置和结构。')
const entryPrice = computed(() => hasStrategyPlan.value
  ? (levels.value.plannedEntryPrice ?? levels.value.plannedEntryCandidate ?? strategyPlan.value?.entry?.trigger)
  : (activePlan.value.entryLimit ?? activePlan.value.triggerPrice ?? levels.value.triggerPrice ?? activeSetup.value.triggerPrice))
const defensePrice = computed(() => hasStrategyPlan.value
  ? (levels.value.activeDefense ?? strategyPlan.value?.stopLoss)
  : (activePlan.value.activeStop ?? activePlan.value.initialStop ?? activeSetup.value.invalidationPrice))
const firstTarget = computed(() => hasStrategyPlan.value
  ? (levels.value.firstTarget ?? targetPrice(strategyPlan.value?.takeProfits?.find((item) => item.role === 'FIRST_TARGET')))
  : targetPrice(activePlan.value.firstTarget))
const hasPosition = computed(() => props.context === 'position' || hierarchy.value.context === 'HOLDING')
const entryLabel = computed(() => hasPosition.value ? '观察价' : '买入确认价')
const defenseLabel = computed(() => hasPosition.value ? '止损价' : '放弃买入价')
const targetLabel = computed(() => '首次止盈价')
const entryMeaning = computed(() => hasPosition.value
  ? (Number(entryPrice.value) > 0 ? '到这里后再看是否适合加仓。' : '未到止损价或止盈价前，先持有。')
  : '日K收盘站上后，再确认量能和风险。')
const defenseMeaning = computed(() => hasPosition.value ? '日K收盘跌破此价，卖出。' : '日K收盘低于此价，取消本次买入。')
const targetMeaning = computed(() => Number(firstTarget.value) > 0 ? '日K收盘到达或站上此价，先卖出一部分。' : '形成买入计划后才计算。')
const evidence = computed(() => [...new Set([...(strategyPlan.value?.reasons || []), ...(activeSetup.value.evidence || []), ...(activePlan.value.reasons || [])])].filter(Boolean).slice(0, 4))
const sourcePages = computed(() => strategyPlan.value?.sourcePages || activeSetup.value.sourcePages || activePlan.value.sourcePages || hierarchy.value.sourcePages || [])
const sessionPatterns = computed(() => props.intraday ? (props.priceAction?.sessionPatterns || props.plan?.priceAction?.sessionPatterns || []).slice(0, 3) : [])
const derivationPlan = computed(() => ({ ...activeSetup.value, ...activePlan.value, setupType: patternType.value, marketMode: mode.value, marketModeLabel: modeLabel.value, firstTarget: activePlan.value.firstTarget || firstTarget.value, activeStop: defensePrice.value, initialStop: defensePrice.value, sourcePages: sourcePages.value, roomR: activePlan.value.roomR }))
const contextLabel = computed(() => ({ position: '已有仓位', watchlist: '准备买入', analysis: '下一步操作' })[props.context] || '下一步操作')
const hasReading = computed(() => Boolean(props.priceAction || props.plan?.decisionHierarchy || hasStrategyPlan.value || activeSetup.value.type || activePlan.value.planId))

function targetPrice(value) { return typeof value === 'object' ? value?.price : value }
function price(value) { const number = Number(value); return Number.isFinite(number) && number > 0 ? number.toFixed(2) : '--' }
function patternName(value) { return labels[value] || '会话结构待复核' }
function statusName(value) { return lifecycleLabels[value] || '观察' }
</script>
