<template>
  <article :class="['price-action-plan-card', `is-${tone}`]">
    <header class="price-action-plan-head">
      <div class="price-action-plan-title">
        <span class="price-action-plan-kicker">{{ marketModeLabel }} · {{ modeLabel }} · {{ statusLabel }}</span>
        <strong>{{ marketModeLabel }} · {{ setupLabel }}</strong>
      </div>
      <span class="price-action-plan-action">{{ directionLabel }}</span>
    </header>

    <p class="price-action-plan-summary">{{ summaryText }}</p>

    <div class="price-action-plan-grid">
      <div class="price-action-plan-field is-defense">
        <small>{{ defenseLabel }}</small>
        <strong>{{ price(plan.activeStop || plan.initialStop || plan.structuralInvalidation) }}</strong>
        <span>{{ defenseText }}</span>
      </div>
      <div class="price-action-plan-field is-target">
        <small>{{ targetLabel }}</small>
        <strong>{{ price(targetPrice(plan.firstTarget)) }}</strong>
        <span>{{ targetText }}</span>
      </div>
      <div class="price-action-plan-field is-entry">
        <small>{{ entryLabel }}</small>
        <strong>{{ price(plan.triggerPrice) }} <span v-if="entryLimitValue">→ {{ price(entryLimitValue) }}</span></strong>
        <span>{{ entryText }}</span>
      </div>
      <div class="price-action-plan-field is-extension">
        <small>{{ extensionLabel }}</small>
        <strong>{{ price(targetPrice(plan.extensionTarget)) }}</strong>
        <span>{{ extensionText }}</span>
      </div>
      <div v-if="riskLevel" class="price-action-plan-field is-account-risk">
        <small>账户风险线</small>
        <strong>{{ price(riskLevel) }}</strong>
        <span>触及后按账户风险纪律退出或减仓。</span>
      </div>
    </div>

    <div class="price-action-plan-meta">
      <span><b>量能</b>{{ volumeLabel }}</span>
      <span v-if="reductionShares"><b>本次减仓</b>{{ reductionShares }} 股</span>
      <span v-if="defenseOnly || (plan.roomR !== null && plan.roomR !== undefined)"><b>买入空间</b>{{ roomRLabel }}</span>
      <span v-if="plan.stopState"><b>止损价状态</b>{{ stopStateLabel }}</span>
    </div>

    <div v-if="evidenceItems.length" class="price-action-plan-evidence">
      <b>依据</b>
      <span v-for="item in evidenceItems" :key="item">{{ item }}</span>
    </div>

    <div v-if="blockedItems.length" class="price-action-plan-blocked">
      <b>暂不买入</b>
      <span v-for="item in blockedItems" :key="item.code"><strong>{{ item.label }}</strong>{{ item.detail }}</span>
    </div>
    <details v-if="cancellationItems.length" class="price-action-plan-details">
      <summary>查看放弃条件</summary>
      <ul>
        <li v-for="item in cancellationItems" :key="item">{{ item }}</li>
      </ul>
      <p v-if="plan.alternativeScenario">替代情景：{{ plan.alternativeScenario }}</p>
      </details>

    <footer class="price-action-plan-footer">
      <PointDerivationDialog
        :plan="plan"
        :context="context"
        :minimum-reward-risk="minimumRewardRisk"
        :account-risk-exit="riskLevel"
      />
    </footer>
  </article>
</template>

<script setup>
import { computed } from 'vue'
import PointDerivationDialog from './PointDerivationDialog.vue'

const props = defineProps({
  plan: { type: Object, required: true },
  context: { type: String, default: 'analysis' },
  volume: { type: Object, default: null },
  accountRiskExit: { type: [Number, String], default: null },
  minimumRewardRisk: { type: [Number, String], default: 1.5 },
})

const setupLabels = {
  TREND_PULLBACK_H1: '上涨后的第一次回踩', TREND_PULLBACK_H2: '上涨后的第二次回踩', TREND_PULLBACK_L1: '下跌后的第一次反弹', TREND_PULLBACK_L2: '下跌后的第二次反弹',
  TREND_FIRST_PULLBACK: '趋势第一次回踩', TREND_TWO_LEG_PULLBACK: '两次回踩后的再启动', BULL_FLAG: '上涨后的整理', BEAR_FLAG: '下跌后的整理',
  BREAKOUT_UP: '向上突破', BREAKOUT_DOWN: '向下突破', BREAKOUT_RETEST_UP: '向上突破后回踩', BREAKOUT_RETEST_DOWN: '向下突破后反弹', MICRO_CHANNEL_BREAK: '短线通道被突破', SPIKE_CHANNEL: '快速推进后的通道', WIDE_CHANNEL: '宽幅通道', STEP_CHANNEL: '阶梯式推进', EMA_GAP_CONTEXT: '价格明显离开20周期线',
  RANGE_EDGE_FADE_UP: '区间下沿反弹', RANGE_EDGE_FADE_DOWN: '区间上沿回落', RANGE_LOWER_REVERSAL: '区间下沿反弹', RANGE_UPPER_REVERSAL: '区间上沿回落', RANGE_MIDDLE: '区间中部观望', TIGHT_RANGE_IRON_WIRE: '窄幅反复拉锯', TRIANGLE_COMPRESSION: '波动收窄，等待突破', TRIANGLE_EXPANSION: '波动放大，方向不稳', ABC_RANGE_LEGS: '区间内三段摆动',
  RANGE_BREAKOUT_PENDING: '突破出现，等待确认', RANGE_BREAKOUT_CONFIRMED: '突破已经站稳', RANGE_BREAKOUT_CONTINUATION: '突破后继续推进', RANGE_BREAKOUT_RETEST: '突破后回踩确认', FAILED_BULLISH_BREAKOUT: '向上突破失败', FAILED_BEARISH_BREAKOUT: '向下突破失败', BREAKOUT_FAILURE_OF_FAILURE: '失败突破后的再测试',
  DOUBLE_BOTTOM: '两次探底', DOUBLE_TOP: '两次冲高', WEDGE_THIRD_PUSH: '第三次冲击，留意衰竭', CLIMAX_SPIKE_REVERSAL: '快速推进后的反转警示', CLIMAX_REVERSAL_CANDIDATE: '高潮后的反转候选', V_REVERSAL_WARNING: '快速反向波动', HEAD_SHOULDERS_REVERSAL: '头肩形反转候选', EXPANSION_REVERSAL: '波动放大后的反转候选', FINAL_FLAG_OR_INSIDE_BREAK: '末端整理后的突破', MAJOR_BEARISH_REVERSAL: '主要向下反转', MAJOR_BULLISH_REVERSAL: '主要向上反转', REVERSAL_FAILURE: '反转尝试失败', REVERSAL_FAILURE_OF_FAILURE: '反转失败后的再测试',
  OPENING_TREND: '开盘趋势', OPENING_REVERSAL: '开盘反转', TREND_FROM_RANGE: '开盘区间后起趋势', TREND_DAY: '趋势日候选', TREND_RESUMPTION: '午后趋势恢复', CLOSE_WINDOW_REVIEW: '收盘窗口复核', OPENING_DATA_INSUFFICIENT: '开盘数据不足', WAIT_FOR_ENTRY: '等待合规买入位置',
}
const categoryLabels = {
  TREND_PULLBACK_H1: '趋势篇', TREND_PULLBACK_H2: '趋势篇', BREAKOUT_RETEST_UP: '趋势篇', BREAKOUT_UP: '趋势篇', BEAR_FLAG: '趋势篇', SPIKE_CHANNEL: '趋势篇', FINAL_FLAG_OR_INSIDE_BREAK: '趋势篇',
  RANGE_LOWER_REVERSAL: '区间篇', RANGE_UPPER_REVERSAL: '区间篇', RANGE_MIDDLE: '区间篇', TRIANGLE_COMPRESSION: '区间篇',
  DOUBLE_BOTTOM: '反转篇', DOUBLE_TOP: '反转篇', FAILED_BULLISH_BREAKOUT: '反转篇', FAILED_BEARISH_BREAKOUT: '反转篇', WEDGE_THIRD_PUSH: '反转篇', MAJOR_BEARISH_REVERSAL: '反转篇', MAJOR_BULLISH_REVERSAL: '反转篇', CLIMAX_REVERSAL_CANDIDATE: '反转篇',
}
const statusLabels = { CONFIRMED: '已确认', BLOCKED: '已阻断', WAIT: '等待确认', PENDING: '待确认', FAILED: '已失效', WATCH: '观察', NEEDS_REVIEW: '需复核', EXPIRED: '已过期', DATA_INSUFFICIENT: '数据不足', OBSERVE_ONLY: '仅观察' }
const actionLabels = { BUY: '买入 / 加仓', REDUCE: '部分减仓', SELL: '卖出 / 减仓', NONE: '观察，不交易' }
const volumeLabels = {
  NORMAL: '量能正常', WEAK: '量能偏弱', STRONG: '放量确认', CONFIRM: '放量确认', CONFIRMING: '价量配合', EXPANSION: '放量', CONTRACTION: '缩量回撤', DRY_UP: '量能枯竭/缩量', CLIMAX: '高潮量候选', CLIMAX_CANDIDATE: '高潮量候选', DIVERGING: '价量背离', UNKNOWN: '量能待复核', DATA_INSUFFICIENT: '量能数据不足',
}

const planSetup = computed(() => props.plan.setupType || props.plan.type || props.plan.kind || '')
const actionValue = computed(() => String(props.plan.action || '').toUpperCase())
const setupLabel = computed(() => props.plan.defenseWatch ? '防守观察' : actionValue.value === 'REDUCE' ? '确认强反向后的部分减仓' : setupLabels[planSetup.value] || (props.plan.direction === 'BUY' ? '买入计划' : props.plan.direction === 'SELL' ? '持仓卖出观察' : '等待结构确认'))
const categoryLabel = computed(() => {
  if (categoryLabels[planSetup.value]) return categoryLabels[planSetup.value]
  const pages = (props.plan.sourcePages || []).map((page) => String(page))
  if (pages.some((page) => page.includes('趋势篇'))) return '趋势篇'
  if (pages.some((page) => page.includes('区间篇'))) return '区间篇'
  if (pages.some((page) => page.includes('反转篇'))) return '反转篇'
  return '价格行为'
})
const statusLabel = computed(() => statusLabels[props.plan.status] || props.plan.status || '等待确认')
const modeLabel = computed(() => ({ SCALP: '分钟周期 / 日内买卖点候选', SWING: '日线周期 / 隔日与持仓管理' }[props.plan.mode] || '周期待定'))
const marketModeLabel = computed(() => {
  const mode = ({ TREND: '趋势', RANGE: '区间', REBOUND: '反弹' })[props.plan.marketMode]
  if (mode) return mode
  if (categoryLabel.value === '反转篇') return '反弹'
  return categoryLabel.value.replace('篇', '') || '价格行为'
})
const directionLabel = computed(() => props.plan.defenseWatch ? '防守观察' : actionLabels[actionValue.value] || actionLabels[props.plan.direction] || '观察，不交易')
const tone = computed(() => props.plan.defenseWatch ? 'watch' : actionValue.value === 'REDUCE' ? 'warning' : props.plan.direction === 'BUY' ? 'entry' : props.plan.direction === 'SELL' ? 'defense' : 'watch')
const riskLevel = computed(() => props.accountRiskExit ?? null)
const entryZone = computed(() => props.plan.entryZone || {})
const isSellObservation = computed(() => props.plan.direction === 'SELL')
const isReduction = computed(() => actionValue.value === 'REDUCE')
const reductionShares = computed(() => Number(props.plan.reductionShares || props.plan.order?.shares || 0) > 0 ? Number(props.plan.reductionShares || props.plan.order?.shares) : 0)
const entryLimitValue = computed(() => isSellObservation.value ? null : props.plan.entryLimit)
const entryLabel = computed(() => props.plan.defenseWatch ? '卖出确认价' : isReduction.value ? '减仓确认价' : isSellObservation.value ? '卖出确认价' : '买入确认价')
const defenseLabel = computed(() => {
  if (props.context === 'position') return '止损价'
  return isSellObservation.value ? '放弃卖出价' : '放弃买入价'
})
const targetLabel = computed(() => props.context === 'position' ? '首次止盈价' : isSellObservation.value ? '下方参考价' : '首次止盈价')
const extensionLabel = computed(() => props.context === 'position' ? '后续目标价' : isSellObservation.value ? '下方参考价' : '后续目标价')
const volumeState = computed(() => props.plan.volumeEvidence?.state || props.plan.volume?.state || props.volume?.state || 'UNKNOWN')
const volumeLabel = computed(() => {
  const base = volumeLabels[volumeState.value] || '量能待复核'
  const ratio = Number(props.plan.volumeEvidence?.relativeVolume ?? props.plan.volume?.latestRatio ?? props.volume?.relativeVolume)
  return Number.isFinite(ratio) && ratio > 0 ? `${base}（均量 ${ratio.toFixed(2)} 倍）` : base
})
const minimumRewardRisk = computed(() => Number(props.minimumRewardRisk) > 0 ? Number(props.minimumRewardRisk) : 1.5)
const defenseOnly = computed(() => props.context === 'position' && props.plan.direction === 'SELL')
const roomRLabel = computed(() => {
  if (defenseOnly.value) return '已有仓位，只按止损价管理'
  if (isSellObservation.value) return '下行观察，不计算买入空间'
  const room = Number(props.plan.roomR)
  if (!Number.isFinite(room)) return '--'
  return `${room.toFixed(2)} 倍 / 最低 ${minimumRewardRisk.value.toFixed(2)} 倍`
})
const entryText = computed(() => {
  if (entryZone.value.low || entryZone.value.high) return `价格进入 ${price(entryZone.value.low)} - ${price(entryZone.value.high)}，${orderType(props.plan.entryOrderType)}。`
  return props.plan.direction === 'BUY' ? '日K收盘站上此价，量能确认后买入。' : isReduction.value ? '减仓条件已确认，卖出指定数量。' : props.plan.direction === 'SELL' ? '日K收盘跌破此价后卖出或减仓。' : '等下一根完整K线再判断。'
})
const defenseText = computed(() => props.context === 'position' ? (props.plan.stopState === 'moved' ? '止损价已上调。' : '跌破后卖出，不下调止损价。') : '日K收盘低于此价，不买入。')
const targetText = computed(() => {
  if (props.context === 'position') return props.plan.firstTarget?.source ? `到价后先卖出一部分；依据：${props.plan.firstTarget.source}` : '到价后先卖出一部分。'
  return props.plan.firstTarget?.source ? `依据：${props.plan.firstTarget.source}` : props.plan.direction === 'SELL' ? '观察下行空间。' : '买入后到价先卖出一部分。'
})
const extensionText = computed(() => {
  if (props.context === 'position') return props.plan.extensionTarget?.source ? `首次止盈后，趋势继续走强时再看；依据：${props.plan.extensionTarget.source}` : '首次止盈后，趋势继续走强时再看。'
  return props.plan.extensionTarget?.source ? `首次止盈后，趋势继续走强时再看；依据：${props.plan.extensionTarget.source}` : props.plan.direction === 'SELL' ? '仅用于观察下行空间。' : '首次止盈后，趋势继续走强时再看。'
})
const summaryText = computed(() => {
  if (props.plan.defenseWatch) return '到止损价再卖出；到首次止盈价先卖出一部分。'
  if (isReduction.value) return `${props.plan.reductionReason || '减仓条件已确认。'}${reductionShares.value ? ` 卖出 ${reductionShares.value} 股。` : ''}`
  if (defenseOnly.value) return '到止损价再卖出或减仓。'
  if (props.plan.blockedReasons?.length) return '买入条件尚不完整，继续等价格和量能确认。'
  if (props.plan.direction === 'BUY') return '日K收盘站上买入确认价，量能和买入空间合格后买入。'
  if (props.plan.direction === 'SELL') return '观察下行走势；持仓时才按止损价处理。'
  return '等待下一根完整K线确认方向。'
})
const evidenceItems = computed(() => (props.plan.reasons || props.plan.reasonClusters?.flat() || []).filter(Boolean).slice(0, 2))
const cancellationItems = computed(() => props.plan.cancellationConditions || [])
const stopStateLabel = computed(() => {
  if (props.plan.defenseWatch) return '防守观察'
  return ({ initial: '初始防守', moved: '已移动', manual_review: '待人工复核', not_applicable: '卖出管理' }[props.plan.stopState] || props.plan.stopState)
})
const blockedItems = computed(() => [...new Set((props.plan.blockedReasons || []))].filter((code) => !(defenseOnly.value && ['INSUFFICIENT_ROOM', 'EXISTING_LONG_REQUIRED', 'SHORT_SELLING_DISABLED'].includes(code))).map(blockReason))

function targetPrice(value) { return typeof value === 'object' ? value?.price : value }
function price(value) { const n = Number(value); return Number.isFinite(n) && n > 0 ? n.toFixed(2) : '--' }
function orderType(value) { return ({ STOP_ENTRY: '突破触发单', LIMIT_REVIEW: '限价回踩', MARKET_REVIEW: '确认后人工复核', SELL_CONFIRMATION: '卖出确认' }[value] || '确认后复核') }
function blockReason(value) {
  const room = Number(props.plan.roomR)
  const minimum = minimumRewardRisk.value.toFixed(2)
  const descriptions = {
    DATA_INSUFFICIENT: ['数据不足', '完整K线或关键字段不足，不能可靠计算结构和点位。'],
    INCOMPLETE_BAR: ['当前K线未收盘', '未收盘K线可能改变形态，等待完整K线确认。'],
    UNKNOWN_TRIGGER: ['触发价未知', '形态尚未给出可执行触发价，不提前挂单。'],
    UNKNOWN_STOP: ['止损价未确认', '还不能算清最大损失，不买入。'],
    UNKNOWN_TARGET: ['止盈价未确认', '还没有可参考的首次止盈价，不用猜测代替。'],
    INSUFFICIENT_ROOM: ['买入空间偏小', `买入确认价到首次止盈价的空间${Number.isFinite(room) ? `约为止损价距离的${room.toFixed(2)}倍` : '尚未算清'}，最低要求${minimum}倍。`],
    EXISTING_LONG_REQUIRED: ['卖出条件待匹配', '该下行结构需要与当前持仓和卖出权限匹配，确认后才处理卖出或减仓。'],
    SHORT_SELLING_DISABLED: ['执行条件不匹配', '当前计划只保留买入和持仓卖出路径，未生成不适用的方向性订单。'],
    CASH_LIMIT: ['现金或仓位不足', '按账户现金、整手和单笔风险限制无法建立合规仓位。'],
    RISK_LIMIT: ['超出账户风险预算', '结构止损距离过大，超过账户允许的单笔风险。'],
    REVIEW_REQUIRED: ['需要人工复核', '形态证据尚未达到自动确认标准，只记录观察。'],
    SESSION_EXPIRED: ['交易时段已过期', '该日内计划已超出有效交易时段。'],
  }
  const [label, detail] = descriptions[value] || ['其他约束', '该条件尚未满足，暂不生成可执行订单。']
  return { code: value, label, detail }
}
</script>
