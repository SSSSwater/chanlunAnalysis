<template>
  <el-button
      text
      size="small"
      class="point-guide-trigger"
      :icon="Info"
      @click.stop="visible = true"
    >
      点位依据
  </el-button>

    <el-dialog
      v-model="visible"
      :title="`点位依据 · ${displayTitle}`"
      width="min(580px, calc(100vw - 24px))"
      append-to-body
      class="point-guide-dialog"
      align-center
    >
      <div class="point-guide-summary">
        <span>{{ modeLabel }}</span>
        <span>{{ directionLabel }}</span>
        <span v-if="latestPrice > 0">现价 {{ formatPrice(latestPrice) }}</span>
      </div>

      <div class="point-guide-list">
        <article v-for="item in pointItems" :key="item.key" class="point-guide-item">
          <div class="point-guide-item-head">
            <strong>{{ item.label }}</strong>
            <b :class="item.available ? 'is-available' : 'is-missing'">{{ item.valueText }}</b>
          </div>
          <p>{{ item.how }}</p>
          <small v-if="item.source">来源：{{ item.source }}</small>
        </article>
      </div>

      <div v-if="riskSummary" class="point-guide-risk">
        <strong>{{ riskHeading }}</strong>
        <p>{{ riskSummary }}</p>
      </div>

      <div v-if="sourcePages.length" class="point-guide-sources">
        <strong>规则索引</strong>
        <span v-for="source in sourcePages" :key="source">{{ source }}</span>
      </div>
  </el-dialog>
</template>

<script setup>
import { computed, ref } from 'vue'
import { Info } from 'lucide-vue-next'

const props = defineProps({
  plan: { type: Object, default: () => ({}) },
  levels: { type: Object, default: () => ({}) },
  context: { type: String, default: 'analysis' },
  minimumRewardRisk: { type: [Number, String], default: 1.5 },
  accountRiskExit: { type: [Number, String], default: null },
  title: { type: String, default: '' },
})

const visible = ref(false)
const plan = computed(() => props.plan || {})
const levels = computed(() => props.levels || {})
const actionValue = computed(() => String(plan.value.action || '').toUpperCase())
const isReduction = computed(() => actionValue.value === 'REDUCE')
const directionLabel = computed(() => {
  if (plan.value.defenseWatch) return '等待卖出条件'
  if (props.context === 'position' && !isReduction.value && plan.value.direction !== 'SELL') return '持有观察'
  return ({ BUY: '买入 / 加仓', REDUCE: '部分减仓', SELL: '卖出 / 减仓', NONE: '继续观察' }[actionValue.value] || ({ BUY: '买入 / 加仓', SELL: '卖出 / 减仓', NONE: '继续观察' }[plan.value.direction] || '继续观察'))
})
const categoryLabels = {
  TREND_PULLBACK_H1: '趋势篇', TREND_PULLBACK_H2: '趋势篇', TREND_PULLBACK_L1: '趋势篇', TREND_PULLBACK_L2: '趋势篇', TREND_FIRST_PULLBACK: '趋势篇', TREND_TWO_LEG_PULLBACK: '趋势篇', BULL_FLAG: '趋势篇', BEAR_FLAG: '趋势篇', BREAKOUT_UP: '趋势篇', BREAKOUT_DOWN: '趋势篇', BREAKOUT_RETEST_UP: '趋势篇', BREAKOUT_RETEST_DOWN: '趋势篇', MICRO_CHANNEL_BREAK: '趋势篇', SPIKE_CHANNEL: '趋势篇', WIDE_CHANNEL: '趋势篇', STEP_CHANNEL: '趋势篇', EMA_GAP_CONTEXT: '趋势篇',
  RANGE_EDGE_FADE_UP: '区间篇', RANGE_EDGE_FADE_DOWN: '区间篇', RANGE_LOWER_REVERSAL: '区间篇', RANGE_UPPER_REVERSAL: '区间篇', RANGE_MIDDLE: '区间篇', TIGHT_RANGE_IRON_WIRE: '区间篇', TRIANGLE_COMPRESSION: '区间篇', TRIANGLE_EXPANSION: '区间篇', ABC_RANGE_LEGS: '区间篇', RANGE_BREAKOUT_PENDING: '区间篇', RANGE_BREAKOUT_CONFIRMED: '区间篇', RANGE_BREAKOUT_CONTINUATION: '区间篇', RANGE_BREAKOUT_RETEST: '区间篇', FAILED_BULLISH_BREAKOUT: '区间篇', FAILED_BEARISH_BREAKOUT: '区间篇', BREAKOUT_FAILURE_OF_FAILURE: '区间篇',
  DOUBLE_BOTTOM: '反转篇', DOUBLE_TOP: '反转篇', WEDGE_THIRD_PUSH: '反转篇', CLIMAX_SPIKE_REVERSAL: '反转篇', CLIMAX_REVERSAL_CANDIDATE: '反转篇', V_REVERSAL_WARNING: '反转篇', HEAD_SHOULDERS_REVERSAL: '反转篇', EXPANSION_REVERSAL: '反转篇', FINAL_FLAG_OR_INSIDE_BREAK: '反转篇', MAJOR_BEARISH_REVERSAL: '反转篇', MAJOR_BULLISH_REVERSAL: '反转篇', REVERSAL_FAILURE: '反转篇', REVERSAL_FAILURE_OF_FAILURE: '反转篇',
}
const categoryLabel = computed(() => {
  if (categoryLabels[plan.value.setupType]) return categoryLabels[plan.value.setupType]
  const pages = (plan.value.sourcePages || []).map((page) => String(page))
  if (pages.some((page) => page.includes('趋势篇'))) return '趋势篇'
  if (pages.some((page) => page.includes('区间篇'))) return '区间篇'
  if (pages.some((page) => page.includes('反转篇'))) return '反转篇'
  return '价格行为'
})
const marketModeLabel = computed(() => {
  const mode = ({ TREND: '趋势', RANGE: '区间', REBOUND: '反弹' })[plan.value.marketMode]
  if (mode) return mode
  if (categoryLabel.value === '反转篇') return '反弹'
  return categoryLabel.value.replace('篇', '') || '价格行为'
})
const modeLabel = computed(() => ({ SCALP: '分钟K', SWING: '日K' }[plan.value.mode] || (props.context === 'position' ? '持仓' : '分析')))
const displayTitle = computed(() => props.title || `${marketModeLabel.value} · 操作说明`)
const latestPrice = computed(() => number(levels.value.latestPrice ?? plan.value.latestPrice))
const minimumR = computed(() => number(props.minimumRewardRisk) || 1.5)

const stopValue = computed(() => props.context === 'position'
  ? number(levels.value.movingStop ?? levels.value.technicalStop ?? levels.value.activeDefense ?? plan.value.activeStop ?? plan.value.initialStop)
  : number(plan.value.activeStop ?? plan.value.initialStop ?? plan.value.structuralInvalidation ?? levels.value.activeDefense))
const entryValue = computed(() => number(plan.value.triggerPrice ?? plan.value.entryLimit ?? levels.value.plannedEntryPrice ?? levels.value.entryPrice))
const targetValue = computed(() => number(levels.value.firstTarget ?? targetPrice(plan.value.firstTarget)))
const extensionValue = computed(() => number(levels.value.extensionTarget ?? targetPrice(plan.value.extensionTarget)))
const accountRiskValue = computed(() => number(props.accountRiskExit ?? levels.value.accountRiskExit ?? levels.value.costStop))
const structuralValue = computed(() => number(plan.value.structuralInvalidation ?? levels.value.structuralInvalidation ?? levels.value.structureBoundary))
const roomR = computed(() => number(plan.value.roomR))
const defenseOnly = computed(() => props.context === 'position' && plan.value.direction === 'SELL' && !isReduction.value)
const sellObservation = computed(() => plan.value.direction === 'SELL')
const targetSource = computed(() => {
  const target = plan.value.firstTarget
  return target && typeof target === 'object' ? target.source : ''
})
const sourcePages = computed(() => (plan.value.sourcePages || []).map((item) => {
  if (typeof item === 'string') return item
  return item?.label || item?.page || item?.source || ''
}).filter(Boolean))

const pointItems = computed(() => [
  {
    key: 'stop',
    label: props.context === 'position' ? '止损价' : sellObservation.value ? '取消卖出观察价' : '放弃买入价',
    value: stopValue.value,
    how: props.context === 'position'
      ? '日K收盘跌破此价才卖出。这个价只会随新结构上调，不会为了等待反弹而下调。'
      : sellObservation.value
        ? `价格重新站上${structuralValue.value > 0 ? ` ${formatPrice(structuralValue.value)}` : '此价'}时，取消走弱观察。`
        : `由放弃买入价${structuralValue.value > 0 ? ` ${formatPrice(structuralValue.value)}` : ''} 加上波动缓冲得出；后续只会收紧。`,
    source: props.context === 'position' ? '最近确认低点与止损价' : sellObservation.value ? '走弱形态确认位' : '形态边界与波动范围',
  },
  {
    key: 'entry',
    label: plan.value.defenseWatch ? '卖出确认价' : isReduction.value ? '减仓确认价' : sellObservation.value ? '卖出确认价' : props.context === 'position' ? '观察价' : '买入确认价',
    value: entryValue.value,
    how: plan.value.defenseWatch
      ? `日K收盘跌破 ${stopValue.value > 0 ? formatPrice(stopValue.value) : '--'} 并有后续走弱时，再决定卖出数量。`
      : isReduction.value
      ? `${plan.value.reductionReason || '减仓条件已满足'}；先卖出${number(plan.value.reductionShares) > 0 ? ` ${number(plan.value.reductionShares)} 股` : ''}。`
      : sellObservation.value
        ? `价格跌到${entryValue.value > 0 ? ` ${formatPrice(entryValue.value)}` : '此价'}后，等日K收盘、量能和已有仓位同时符合，再卖出或减仓。`
      : props.context === 'position'
        ? `价格到 ${entryValue.value > 0 ? formatPrice(entryValue.value) : '--'} 后，等日K收盘和量能确认后，再判断是否加仓。`
        : `价格到 ${entryValue.value > 0 ? formatPrice(entryValue.value) : '--'} 后，等日K收盘、量能和风险条件同时通过再买入。`,
    source: '形态确认价',
  },
  {
    key: 'reduction',
    label: '本次减仓数量',
    value: number(plan.value.reductionShares),
    how: isReduction.value
      ? '数量按账户风险和当前持仓计算；剩余仓位继续按止损价管理。'
      : '走弱形态还未确认，暂不减仓。',
    source: isReduction.value ? '账户风险与持仓数量' : '尚未触发减仓条件',
  },
  {
    key: 'target',
    label: props.context === 'position' ? '首次止盈价' : sellObservation.value ? '下方参考价' : '首次止盈价',
    value: targetValue.value,
    how: props.context === 'position'
      ? '日K收盘到达或站上此价，先卖出一部分。'
      : sellObservation.value ? '用于观察卖出确认后的下方空间。' : '买入后日K收盘到达或站上此价，先卖出一部分。',
    source: targetSource.value || (sellObservation.value ? '下方支撑位' : '最近确认压力位'),
  },
  {
    key: 'extension',
    label: props.context === 'position' ? '下一目标价' : sellObservation.value ? '下方延伸价' : '下一目标价',
    value: extensionValue.value,
    how: props.context === 'position'
      ? '首次止盈后走势继续走强时才启用；没有确认时不使用。'
      : sellObservation.value ? '仅用于观察走弱后的下方空间。' : '只在趋势继续走强时使用；区间和反弹不外推。',
    source: props.context === 'position' ? '上方下一压力位' : sellObservation.value ? '下方参考位置' : '上方下一压力位',
  },
  {
    key: 'accountRisk',
    label: '账户保护价',
    value: accountRiskValue.value,
    how: '由成本价和账户风险计算，用于控制单笔损失；不替代止损价。',
    source: '账户风险设置',
  },
].map((item) => ({ ...item, available: item.value > 0, valueText: item.value > 0 ? formatPrice(item.value) : '--' })))

const riskHeading = computed(() => props.context === 'position' ? '加仓空间判断' : '买入空间判断')
const riskSummary = computed(() => {
  if (plan.value.defenseWatch) return '先看止损价；未跌破前不因短暂回落卖出。'
  if (isReduction.value) return `${plan.value.reductionReason || '减仓条件已满足'}；按上方数量执行。`
  if (defenseOnly.value || sellObservation.value) return '卖出前需确认收盘和后续走弱。'
  if (props.context === 'position') return roomR.value >= minimumR.value && roomR.value > 0 ? '加仓价、止损价和止盈价之间的空间足够。' : '当前加仓条件不完整，先按止损价和首次止盈价管理。'
  if (roomR.value > 0) return roomR.value >= minimumR.value ? '买入价、放弃买入价和止盈价之间的空间足够。' : '买入价到止盈价的空间偏小，暂不买入。'
  return '买入价、放弃买入价或止盈价不完整，暂不买入。'
})

function number(value) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function targetPrice(value) {
  return typeof value === 'object' ? value?.price : value
}

function formatPrice(value) {
  return number(value) > 0 ? number(value).toFixed(2) : '--'
}
</script>
