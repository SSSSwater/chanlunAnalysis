<template>
  <section class="analysis-row">
    <aside class="analysis-info">
      <slot name="info"></slot>
    </aside>

    <section :class="['chart-area', result ? 'has-result' : 'is-empty']">
      <div class="toolbar">
        <div class="chart-heading">
          <div class="chart-quote-row chart-market-row">
            <div class="chart-quote-main">
              <strong :class="['chart-latest-price', latestQuote.changeClass]">{{ latestQuote.latestPrice }}</strong>
              <small :class="['chart-quote-change', latestQuote.changeClass]">{{ latestQuote.changeText }}</small>
              <small class="chart-quote-date">{{ latestQuote.date }}</small>
            </div>
            <div class="chart-quote-stats" :aria-label="latestQuote.isHovering ? '悬浮位置行情' : '当前交易日行情'">
              <span><small>最高价</small><div class="chart-quote-value"><strong>{{ latestQuote.high }}</strong><em v-if="latestQuote.highPctText !== '--'" :class="['chart-level-pct', latestQuote.highPctClass]">{{ latestQuote.highPctText }}</em></div></span>
              <span><small>最低价</small><div class="chart-quote-value"><strong>{{ latestQuote.low }}</strong><em v-if="latestQuote.lowPctText !== '--'" :class="['chart-level-pct', latestQuote.lowPctClass]">{{ latestQuote.lowPctText }}</em></div></span>
              <span><small>量比</small><strong :class="['chart-volume-ratio', latestQuote.volumeRatioClass]">{{ latestQuote.volumeRatio }}</strong></span>
              <span><small>换手率</small><strong>{{ latestQuote.turnoverRate }}</strong></span>
            </div>
          </div>
          <div v-if="result && valuationQuote.available" class="chart-quote-row chart-valuation-row" aria-label="估值与行情">
            <span v-for="item in valuationQuote.items" :key="item.label">
              <small>{{ item.label }}</small>
              <strong>{{ item.value }}</strong>
            </span>
          </div>
        </div>
        <div class="chart-switches">
          <el-segmented v-model="analysisMode" :options="analysisModeOptions" :disabled="!result" />
          <el-segmented
            v-if="result && analysisMode === 'intraday'"
            v-model="intradayPeriod"
            :options="intradayPeriodOptions"
            :disabled="intradayLoading"
          />
        </div>
      </div>

      <div v-loading="loading || intradayLoading" :element-loading-text="loading ? '正在等待分析服务返回行情与分析结果...' : '正在加载分时价格行为...'" class="chart-frame">
        <canvas ref="chartCanvas"></canvas>
        <el-empty v-if="!result && !loading" :description="emptyText" />
        <el-empty v-else-if="result && !currentBars.length && !loading && !intradayLoading" description="暂无该周期完整K线" />
      </div>

      <div v-if="result" class="range-panel">
        <div class="range-meta">
          <span>显示区间</span>
          <strong>{{ visibleRangeText }}</strong>
        </div>
        <el-slider v-model="visibleRange" range :min="0" :max="rangeMax" :step="1" :show-tooltip="false" @input="updateChartWindow" @change="finishChartWindow" />
      </div>
    </section>

    <section class="signals-section">
      <div class="section-title">
        <h3>{{ analysisMode === 'daily' ? '价格行为计划' : '分时价格行为' }}</h3>
        <el-tag :type="assessmentTagType">{{ assessmentLabel }}</el-tag>
      </div>

      <div v-if="priceAction" class="price-action-overview">
        <div class="price-action-overview-main">
          <small>现在先看什么</small>
          <strong>{{ actionLabel }}</strong>
          <p>{{ conclusionText }}</p>
        </div>
        <div class="price-action-overview-facts">
          <span><small>市场环境</small><strong>{{ environmentLabel }}</strong></span>
          <span><small>量能</small><strong>{{ volumeContextLabel }}</strong></span>
          <span v-if="analysisMode === 'intraday' && sessionContext?.timeWindow"><small>交易时段</small><strong>{{ sessionLabel }}</strong></span>
          <span><small>当前状态</small><strong>{{ disciplineStateLabel }}</strong></span>
        </div>
      </div>

      <PriceActionDecision
        v-if="priceAction"
        :price-action="priceAction"
        :plan="disciplinePlan || {}"
        :intraday="analysisMode === 'intraday'"
        :minimum-reward-risk="priceAction?.profile?.thresholds?.minimumRoomR || 1.5"
        :title="chartTitle"
      />

      <template v-if="!contractPlanRows.length && setupRows.length">
        <div :class="['structure-inspector', selectedStructure ? 'has-selection' : '']">
          <div class="structure-results">
            <el-table :data="setupRows" class="signal-table signal-table-desktop" :row-class-name="structureRowClassName" @row-click="selectStructure">
              <el-table-column label="候选结构" min-width="150"><template #default="{ row }">{{ setupLabel(row.type) }}</template></el-table-column>
              <el-table-column label="状态" width="100"><template #default="{ row }">{{ statusLabel(row.status) }}</template></el-table-column>
              <el-table-column label="位置" min-width="120" prop="location" />
              <el-table-column label="缺失条件" min-width="190"><template #default="{ row }">{{ (row.missingConditions || []).join('；') || '--' }}</template></el-table-column>
            </el-table>

            <div class="signal-mobile-list">
              <button
                v-for="row in setupRows"
                :key="structureKey(row)"
                type="button"
                :class="['signal-mobile-card', 'signal-mobile-candidate', isSelectedStructure(row) ? 'is-selected' : '']"
                @click="selectStructure(row)"
              >
                <span class="signal-mobile-card-head"><span><strong>{{ setupLabel(row.type) }}</strong><small>{{ statusLabel(row.status) }}</small></span></span>
                <span class="signal-mobile-anchor"><small>位置</small><strong>{{ row.location || '--' }}</strong></span>
                <span class="signal-mobile-reason">{{ (row.missingConditions || []).join('；') || '等待结构进一步确认' }}</span>
              </button>
            </div>
          </div>

          <aside v-if="selectedStructureDetails" class="structure-popover" role="dialog" aria-label="结构说明">
            <header class="structure-popover-head">
              <div>
                <small>结构说明</small>
                <strong>{{ selectedStructureDetails.title }}</strong>
              </div>
              <button type="button" class="structure-popover-close" aria-label="关闭结构说明" title="关闭" @click="selectedStructure = null">
                <X :size="16" />
              </button>
            </header>
            <div class="structure-popover-meta">
              <span><small>当前状态</small><strong>{{ selectedStructureDetails.status }}</strong></span>
              <span><small>所在位置</small><strong>{{ selectedStructureDetails.location }}</strong></span>
            </div>
            <p class="structure-popover-meaning">{{ selectedStructureDetails.meaning }}</p>
            <div class="structure-popover-section">
              <small>怎么看</small>
              <p>{{ selectedStructureDetails.guidance }}</p>
            </div>
            <div v-if="selectedStructureDetails.evidence.length" class="structure-popover-section">
              <small>当前证据</small>
              <ul><li v-for="item in selectedStructureDetails.evidence" :key="item">{{ item }}</li></ul>
            </div>
            <div class="structure-popover-section is-next">
              <small>下一步</small>
              <p>{{ selectedStructureDetails.next }}</p>
            </div>
          </aside>
        </div>
      </template>
      <p v-else-if="priceAction" class="signal-empty-hint">暂未识别到结构候选，先参考上方环境和关键价位。</p>

    </section>
  </section>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import {
  BarController,
  BarElement,
  CategoryScale,
  Chart,
  Legend,
  LinearScale,
  LineController,
  LineElement,
  PointElement,
  Tooltip,
} from 'chart.js'
import { CandlestickController, CandlestickElement } from 'chartjs-chart-financial'
import { X } from 'lucide-vue-next'
import PriceActionDecision from './PriceActionDecision.vue'

Chart.register(BarController, BarElement, CategoryScale, LinearScale, LineController, LineElement, PointElement, CandlestickController, CandlestickElement, Tooltip, Legend)

const props = defineProps({
  result: { type: Object, default: null },
  disciplinePlan: { type: Object, default: null },
  loading: { type: Boolean, default: false },
  isDark: { type: Boolean, default: false },
  loadIntraday: { type: Function, default: null },
  title: { type: String, default: '等待分析' },
  emptyText: { type: String, default: '等待分析结果' },
})

const analysisMode = ref('daily')
const intradayPeriod = ref('15')
const intradayLoading = ref(false)
const chartCanvas = ref(null)
const visibleRange = ref([0, 0])
const hoveredIndex = ref(null)
const selectedStructure = ref(null)
let chartInstance = null

// Chart typography: tabular mono digits for axes, UI sans for mixed text.
// CJK glyphs fall through to the CJK faces at the end of each stack.
const CHART_TAB_FONT = "'LXGW WenKai Screen', 'PingFang SC', 'HarmonyOS Sans SC', 'MiSans', 'Microsoft YaHei UI', 'Microsoft YaHei', '微软雅黑', 'Noto Sans CJK SC', 'Source Han Sans SC', sans-serif"
const CHART_UI_FONT = "'LXGW WenKai Screen', 'PingFang SC', 'HarmonyOS Sans SC', 'MiSans', 'Microsoft YaHei UI', 'Microsoft YaHei', '微软雅黑', 'Noto Sans CJK SC', 'Source Han Sans SC', sans-serif"

const analysisModeOptions = [{ label: '日线', value: 'daily' }, { label: '日内', value: 'intraday' }]
const intradayPeriodOptions = [{ label: '30分钟', value: '30' }, { label: '15分钟', value: '15' }, { label: '5分钟', value: '5' }]
const intradayData = computed(() => props.result?.intraday?.periods?.[intradayPeriod.value] || null)
const activeResult = computed(() => (analysisMode.value === 'intraday' ? intradayData.value : props.result))
const currentBars = computed(() => activeResult.value?.rawKlines || [])
const priceAction = computed(() => activeResult.value?.priceAction || null)
const tradePlans = computed(() => priceAction.value?.tradePlans || [])
const setupById = computed(() => new Map((priceAction.value?.setups || []).map((setup) => [setup.setupId, setup.type || setup.kind])))
const contractPlanRows = computed(() => uniqueContractPlans(tradePlans.value.map((plan) => {
  const normalized = { ...plan, setupType: setupById.value.get(plan.setupId) || plan.setupType }
  if (normalized.direction === 'SELL') {
    // Normalize stale cached contracts from before the A-share long-only
    // contract so analysis cards cannot display legacy short math.
    return {
      ...normalized,
      executionAction: 'SELL_EXISTING_LONG',
      executionConstraint: 'EXISTING_LONG_ONLY',
      positionRequired: true,
      shortSellingAllowed: false,
      executable: false,
      targetSemantics: 'DOWNSIDE_REFERENCE_ONLY',
      entryZone: null,
      entryLimit: null,
      initialStop: null,
      activeStop: null,
      firstTarget: null,
      extensionTarget: null,
      roomR: null,
      entryOrderType: 'SELL_CONFIRMATION',
      riskStatus: 'NOT_APPLICABLE',
    }
  }
  return normalized
})))
const volumeContext = computed(() => priceAction.value?.volumeTurnover || {})
const displayLabel = (value, fallback = '待判断') => ({
  WEAK: '偏弱', MODERATE: '一般', STRONG: '偏强', VERY_STRONG: '很强', NORMAL: '正常',
  CLOSE: '已收盘', CLOSED: '已收盘', UNKNOWN: '待判断',
  CONFIRMED: '已确认', PENDING: '待确认', WATCH: '观察', NEEDS_REVIEW: '需复核',
  FAILED: '已失败', EXPIRED: '已过期', DATA_INSUFFICIENT: '数据不足',
  WAIT: '等待', BLOCKED: '条件未满足', ARMED: '等待触发', ENTERED: '已买入', MANAGING: '持仓中',
}[String(value || '').toUpperCase()] || value || fallback)
const environmentLabel = computed(() => displayLabel(priceAction.value?.environment?.label, '环境未判定'))
const volumeContextLabel = computed(() => {
  const state = volumeContext.value.state || priceAction.value?.volume?.label || 'UNKNOWN'
  const ratio = Number(volumeContext.value.relativeVolume)
  const label = ({ NORMAL: '量能正常', WEAK: '量能偏弱', STRONG: '放量确认', EXPANSION: '放量', CONTRACTION: '缩量回撤', DRY_UP: '量能枯竭/缩量', CLIMAX: '高潮量候选', CLIMAX_CANDIDATE: '高潮量候选', CONFIRMING: '价量配合', DIVERGING: '价量背离', UNKNOWN: '量能待复核', DATA_INSUFFICIENT: '量能数据不足' })[state] || '量能待复核'
  return Number.isFinite(ratio) && ratio > 0 ? `${label}（均量 ${ratio.toFixed(2)} 倍）` : label
})
const sessionContext = computed(() => priceAction.value?.sessionContext || {})
const sessionLabel = computed(() => ({ PRE_OPEN: '集合竞价/开盘前', OPENING_RANGE: '开盘区间', REGULAR: '常规交易时段', LUNCH_BREAK: '午间休市', CLOSE_30_60: '收盘前 30-60 分钟', AFTER_CLOSE: '收盘后', UNKNOWN: '时段待复核' })[sessionContext.value.timeWindow] || sessionContext.value.timeWindow || '时段待复核')
const disciplineStateLabel = computed(() => {
  const state = priceAction.value?.discipline?.state || 'WAIT'
  return ({ ARMED: '已武装', BLOCKED: '已阻断', ENTERED: '已入场', MANAGING: '管理中', WAIT: '等待' })[state] || '待复核'
})
const disciplineChartLevels = computed(() => (
  analysisMode.value === 'daily' ? selectDisciplineChartLevels(props.disciplinePlan?.levels || {}) : []
))
const planRows = computed(() => contractPlanRows.value.length ? contractPlanRows.value : priceAction.value?.futurePlans || priceAction.value?.signals || priceAction.value?.setups || [])
const setupRows = computed(() => priceAction.value?.setups || [])

function uniqueContractPlans(plans = []) {
  const seen = new Set()
  return plans.filter((plan) => {
    const target = (value) => Number(typeof value === 'object' ? value?.price : value) || 0
    const key = [plan.direction, plan.triggerPrice, plan.entryLimit, plan.structuralInvalidation, plan.initialStop, target(plan.firstTarget), target(plan.extensionTarget)]
      .map((value) => Number.isFinite(Number(value)) ? Number(value).toFixed(4) : String(value || '')).join('|')
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

const actionLabel = computed(() => {
  const action = priceAction.value?.assessment?.action
  if (action === 'BUY') return '等待买入条件'
  if (action === 'SELL') return '等待走弱确认'
  if (priceAction.value?.discipline?.state === 'BLOCKED') return '条件尚不完整'
  return '观察，等待确认'
})
const conclusionText = computed(() => {
  if (!priceAction.value) return '等完整K线后，再判断方向和关键价位。'
  if (priceAction.value.assessment?.blockedReasons?.length) return '买入条件尚不完整，先记住关键价位，不追价。'
  if (priceAction.value.assessment?.action === 'BUY') return '价格到买入区间后，等日K收盘和量能确认；没有确认就继续等。'
  if (priceAction.value.assessment?.action === 'SELL') return '走弱确认后，有持仓再按卖出价或减仓价处理；没有持仓则继续观察。'
  return '方向或位置还不够清楚，先等价格靠近关键位。'
})
const rangeMax = computed(() => Math.max(currentBars.value.length - 1, 0))
const chartTitle = computed(() => (props.result?.name ? `${props.result.symbol || ''} - ${props.result.name}` : props.title))
const quoteNumber = (value, digits = 2) => {
  if (value === null || value === undefined || value === '') return '--'
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(digits) : '--'
}
const quotePercent = (value) => {
  if (value === null || value === undefined || value === '') return '--'
  const number = Number(value)
  return Number.isFinite(number) ? `${number > 0 ? '+' : ''}${number.toFixed(2)}%` : '--'
}
const finiteQuoteNumber = (value) => {
  if (value === null || value === undefined || value === '') return null
  const number = Number(value)
  return Number.isFinite(number) ? number : null
}
const firstQuoteValue = (...values) => values.find((value) => finiteQuoteNumber(value) !== null)
const latestQuote = computed(() => {
  const bars = currentBars.value
  const active = activeResult.value || props.result || {}
  const selectedIndex = Number.isInteger(hoveredIndex.value) && hoveredIndex.value >= 0 && hoveredIndex.value < bars.length
    ? hoveredIndex.value
    : bars.length - 1
  const isHovering = selectedIndex >= 0 && selectedIndex < bars.length && selectedIndex !== bars.length - 1
  const latest = bars[selectedIndex] || {}
  const previous = bars[selectedIndex - 1] || {}
  const latestClose = finiteQuoteNumber(firstQuoteValue(latest.close, props.result?.summary?.latestClose, props.result?.quote?.latestPrice))
  const previousClose = finiteQuoteNumber(previous.close)
  const snapshotChange = isHovering
    ? null
    : finiteQuoteNumber(firstQuoteValue(active?.quote?.pctChange, props.result?.fundamentals?.valuation?.pctChange))
  const barChange = finiteQuoteNumber(latest.pctChange)
  const change = barChange !== null
    ? barChange
    : snapshotChange !== null
      ? snapshotChange
    : latestClose !== null && previousClose !== null && previousClose !== 0
      ? ((latestClose - previousClose) / previousClose) * 100
      : null
  const tradingDate = String(latest.date || '').slice(0, 10)
  const tradingDayBars = isHovering
    ? [latest]
    : tradingDate ? bars.filter((bar) => String(bar.date || '').slice(0, 10) === tradingDate) : [latest]
  const high = tradingDayBars.reduce((value, bar) => Math.max(value, Number(bar.high)), Number.NEGATIVE_INFINITY)
  const low = tradingDayBars.reduce((value, bar) => Math.min(value, Number(bar.low)), Number.POSITIVE_INFINITY)
  const highValue = Number.isFinite(high) ? high : null
  const lowValue = Number.isFinite(low) ? low : null
  const highPct = highValue !== null && previousClose ? ((highValue - previousClose) / previousClose) * 100 : null
  const lowPct = lowValue !== null && previousClose ? ((lowValue - previousClose) / previousClose) * 100 : null
  const volumeContext = active?.priceAction?.volumeTurnover || props.result?.priceAction?.volumeTurnover || {}
  const metrics = active?.priceAction?.metrics || props.result?.priceAction?.metrics || {}
  const volumeRatio = finiteQuoteNumber(firstQuoteValue(
    latest.volumeRatio,
    volumeRatioForBar(bars, selectedIndex),
    isHovering ? null : volumeContext.relativeVolume,
    isHovering ? null : metrics.volumeRatio,
  ))
  const displayVolumeRatio = volumeRatio !== null ? Number(volumeRatio.toFixed(2)) : null
  const turnoverRate = finiteQuoteNumber(firstQuoteValue(
    latest.turnoverRate,
    latest.providerTurnover,
    isHovering ? null : active?.quote?.turnoverRate,
    isHovering ? null : props.result?.quote?.turnoverRate,
    isHovering ? null : volumeContext.turnoverRate,
    isHovering ? null : props.result?.fundamentals?.valuation?.turnoverRate,
  ))
  return {
    latestPrice: quoteNumber(latestClose),
    changeText: quotePercent(change),
    changeClass: change > 0 ? 'profit-positive' : change < 0 ? 'profit-negative' : '',
    high: Number.isFinite(high) ? quoteNumber(high) : '--',
    low: Number.isFinite(low) ? quoteNumber(low) : '--',
    highPctText: quotePercent(highPct),
    highPctClass: highPct !== null && highPct > 0 ? 'profit-positive' : highPct !== null && highPct < 0 ? 'profit-negative' : '',
    lowPctText: quotePercent(lowPct),
    lowPctClass: lowPct !== null && lowPct > 0 ? 'profit-positive' : lowPct !== null && lowPct < 0 ? 'profit-negative' : '',
    volumeRatio: displayVolumeRatio !== null ? displayVolumeRatio.toFixed(2) : '--',
    volumeRatioClass: displayVolumeRatio !== null && displayVolumeRatio > 1 ? 'profit-positive' : displayVolumeRatio !== null && displayVolumeRatio < 1 ? 'profit-negative' : '',
    turnoverRate: turnoverRate !== null ? `${turnoverRate.toFixed(2)}%` : '--',
    date: latest.date || '--',
    isHovering,
  }
})

const valuationQuote = computed(() => {
  const valuation = props.result?.fundamentals?.valuation || {}
  const numberText = (value, digits = 2) => {
    const number = finiteQuoteNumber(value)
    return number === null ? '--' : number.toFixed(digits)
  }
  const moneyText = (value) => {
    const number = finiteQuoteNumber(value)
    if (number === null) return '--'
    const absolute = Math.abs(number)
    const sign = number < 0 ? '-' : ''
    if (absolute >= 100000000) return `${sign}${(absolute / 100000000).toFixed(2)}亿`
    if (absolute >= 10000) return `${sign}${(absolute / 10000).toFixed(2)}万`
    return number.toFixed(2)
  }
  return {
    available: Object.keys(valuation).length > 0,
    items: [
      { label: '动态市盈率', value: numberText(valuation.peDynamic) },
      { label: '滚动市盈率', value: numberText(valuation.peTtm) },
      { label: '市净率', value: numberText(valuation.pb) },
      { label: '总市值', value: moneyText(valuation.totalMarketCap) },
      { label: '流通市值', value: moneyText(valuation.circulatingMarketCap) },
    ],
  }
})
const assessmentLabel = computed(() => displayLabel(priceAction.value?.assessment?.label, '等待数据'))
const assessmentTagType = computed(() => {
  const action = priceAction.value?.assessment?.action
  return action === 'BUY' ? 'success' : action === 'SELL' ? 'danger' : 'info'
})
const visibleRangeText = computed(() => {
  const [start, end] = normalizedRange()
  const bars = currentBars.value
  if (!bars.length) return '等待分析结果'
  return `${bars[start]?.date || '--'} 至 ${bars[end]?.date || '--'}`
})

const ensureIntraday = async () => {
  if (analysisMode.value !== 'intraday' || !props.result || intradayData.value?.rawKlines?.length || !props.loadIntraday) return
  intradayLoading.value = true
  try {
    await props.loadIntraday(intradayPeriod.value)
  } finally {
    intradayLoading.value = false
  }
}

watch([analysisMode, intradayPeriod], async () => {
  selectedStructure.value = null
  await ensureIntraday()
  resetVisibleRange()
  renderChart()
})
watch(() => props.result, async () => {
  selectedStructure.value = null
  analysisMode.value = 'daily'
  resetVisibleRange()
  await nextTick()
  renderChart()
}, { deep: false })
watch([currentBars, priceAction], () => {
  selectedStructure.value = null
  resetVisibleRange()
  renderChart()
}, { deep: true })
watch(() => props.disciplinePlan, () => {
  renderChart()
}, { deep: true })
watch(() => props.isDark, async () => {
  await nextTick()
  renderChart()
}, { flush: 'post' })

function resetVisibleRange() {
  const count = currentBars.value.length
  hoveredIndex.value = null
  visibleRange.value = count ? [Math.max(0, count - 20), count - 1] : [0, 0]
}

function normalizedRange() {
  const max = rangeMax.value
  const start = Math.max(0, Math.min(Math.round(visibleRange.value?.[0] || 0), max))
  const end = Math.max(start, Math.min(Math.round(visibleRange.value?.[1] ?? max), max))
  return [start, end]
}

function updateChartWindow() {
  if (!chartInstance || !currentBars.value.length) return

  const bars = currentBars.value
  const [start, end] = normalizedRange()
  const ema = bars
    .map((bar, index) => ({ x: index, y: Number(bar.ema20) }))
    .filter((item) => Number.isFinite(item.y))
  const anchorPlans = selectAnchorPlans(planRows.value, Number(bars.at(-1)?.close))
  const levels = selectChartLevels(priceAction.value?.levels || [], Boolean(ema.length), Number(bars.at(-1)?.close), anchorPlans)
  const bounds = priceBounds(bars, levels, anchorPlans.slice(0, 3), start, end, disciplineChartLevels.value, ema)
  const volumes = bars.map((bar) => ({ y: Number(bar.volume || 0) }))
  const scales = chartInstance.options?.scales
  if (!scales?.x || !scales?.y || !scales?.volume) return

  scales.x.min = start - 0.5
  scales.x.max = end + 0.5
  scales.y.min = bounds.min
  scales.y.max = bounds.max
  scales.volume.max = volumeMax(volumes.slice(start, end + 1))
  chartInstance.update('none')
}

async function finishChartWindow() {
  await nextTick()
  await new Promise((resolve) => requestAnimationFrame(resolve))
  renderChart()
  await new Promise((resolve) => requestAnimationFrame(resolve))
  chartInstance?.resize()
  chartInstance?.update('none')
}

/* 移动端触摸结束后清除十字轴和顶部的悬浮数据。 */
let touchReleaseBound = false
let hoverReleaseCleanup = null
const bindTouchReleaseTooltip = () => {
  if (touchReleaseBound || !chartCanvas.value) return
  touchReleaseBound = true
  const canvas = chartCanvas.value
  const clearHover = () => {
    const chart = Chart.getChart(canvas)
    if (!chart) return
    clearChartHover(chart)
    // Touch browsers can dispatch a synthetic mouse event after touchend.
    window.setTimeout(() => clearChartHover(chart), 60)
  }
  const canvasEvents = ['touchend', 'touchcancel', 'mouseleave', 'mouseout', 'pointerleave', 'pointerout']
  canvasEvents.forEach((eventName) => canvas.addEventListener(eventName, clearHover, { passive: true }))
  const clearWhenOutside = (event) => {
    const target = event.target
    if (target === canvas || canvas.contains(target)) return
    clearHover()
  }
  window.addEventListener('pointermove', clearWhenOutside, { passive: true })
  window.addEventListener('mousemove', clearWhenOutside, { passive: true })
  hoverReleaseCleanup = () => {
    canvasEvents.forEach((eventName) => canvas.removeEventListener(eventName, clearHover))
    window.removeEventListener('pointermove', clearWhenOutside)
    window.removeEventListener('mousemove', clearWhenOutside)
    hoverReleaseCleanup = null
  }
}

function setChartHover(chart, index, x, y) {
  const nextIndex = Number.isInteger(index) && index >= 0 && index < currentBars.value.length ? index : null
  chart.$hoverIndex = nextIndex
  chart.$hoverPoint = nextIndex === null ? null : { x, y }
  if (hoveredIndex.value !== nextIndex) hoveredIndex.value = nextIndex
  chart.draw()
}

function clearChartHover(chart) {
  if (chart.$hoverIndex === null && chart.$hoverPoint === null && hoveredIndex.value === null) return
  setChartHover(chart, null, 0, 0)
}

const chartCrosshairPlugin = {
  id: 'chart-crosshair',
  afterDraw(chart) {
    const index = chart.$hoverIndex
    const point = chart.$hoverPoint
    const xScale = chart.scales.x
    const area = chart.chartArea
    if (!Number.isInteger(index) || !point || !xScale || !area) return
    const x = xScale.getPixelForValue(index)
    const y = Math.max(area.top, Math.min(area.bottom, point.y))
    const context = chart.ctx
    context.save()
    context.strokeStyle = cssVar('--chart-crosshair', '#64748b')
    context.lineWidth = 1
    context.setLineDash([4, 4])
    context.beginPath()
    context.moveTo(x, area.top)
    context.lineTo(x, area.bottom)
    context.moveTo(area.left, y)
    context.lineTo(area.right, y)
    context.stroke()
    context.restore()
  },
}

const chartPriceLevelLabelsPlugin = {
  id: 'chart-price-level-labels',
  afterDraw(chart, _args, options) {
    const scale = chart.scales.y
    const area = chart.chartArea
    const levels = Array.isArray(options?.levels) ? options.levels : []
    if (!scale || !area || !levels.length) return

    const context = chart.ctx
    const surface = cssVar('--chart-surface', '#fbfdff')
    const fontSize = options?.fontSize || 11
    const labelX = scale.options.position === 'right' ? scale.left + 5 : scale.right - 5
    context.save()
    context.font = `700 ${fontSize}px ${CHART_TAB_FONT}`
    context.textAlign = scale.options.position === 'right' ? 'left' : 'right'
    context.textBaseline = 'middle'

    for (const level of levels) {
      const price = Number(level?.price)
      if (!Number.isFinite(price) || price < scale.min || price > scale.max) continue
      const y = scale.getPixelForValue(price)
      if (!Number.isFinite(y) || y < area.top - 8 || y > area.bottom + 8) continue
      const text = formatPrice(price)
      const width = context.measureText(text).width
      const left = context.textAlign === 'right' ? labelX - width - 4 : labelX - 2
      context.fillStyle = surface
      context.fillRect(left, y - fontSize * 0.65, width + 6, fontSize * 1.3)
      context.fillStyle = level.color || cssVar('--chart-tick', '#435363')
      context.fillText(text, labelX, y)
    }
    context.restore()
  },
}

function renderChart() {
  if (!chartCanvas.value || !currentBars.value.length) {
    chartInstance?.destroy()
    chartInstance = null
    return
  }
  const bars = currentBars.value
  const labels = bars.map((bar) => bar.date || '')
  const candles = bars.map((bar, index) => ({ x: index, o: Number(bar.open), h: Number(bar.high), l: Number(bar.low), c: Number(bar.close) }))
  const volumes = bars.map((bar, index) => ({ x: index, y: Number(bar.volume || 0), up: Number(bar.close) >= Number(bar.open) }))
  const ema = bars.map((bar, index) => ({ x: index, y: Number(bar.ema20) })).filter((item) => Number.isFinite(item.y))
  const anchorPlans = selectAnchorPlans(planRows.value, Number(bars.at(-1)?.close))
  // 20EMA already has a dynamic curve. Prefer an actionable future anchor over
  // a coincident structural line, and keep one representative per line role.
  const levels = selectChartLevels(priceAction.value?.levels || [], Boolean(ema.length), Number(bars.at(-1)?.close), anchorPlans)
  const anchorGuidePlans = anchorPlans.slice(0, 3)
  const disciplineLevels = disciplineChartLevels.value
  const markers = anchorGuidePlans.map((plan) => {
    const index = Number.isInteger(Number(plan.index)) ? Number(plan.index) : bars.findIndex((bar) => bar.date === plan.date)
    const bar = bars[index]
    if (!bar || index < 0) return null
    return { x: index, y: Number(plan.anchorPrice), plan }
  }).filter(Boolean)
  const bounds = priceBounds(bars, levels, anchorGuidePlans, ...normalizedRange(), disciplineLevels, ema)
  const upColor = cssVar('--up', '#d44747')
  const downColor = cssVar('--down', '#177f58')
  const flatColor = cssVar('--flat', '#718096')
  const chartTick = cssVar('--chart-tick', '#435363')
  const chartGrid = cssVar('--chart-grid', '#d6e0e9')
  const isNarrowChart = (chartCanvas.value?.clientWidth || 0) < 520
  const priceLevelLabels = [...levels.map((level) => ({ price: level.price, color: levelColor(level) })),
    ...disciplineLevels.map((level) => ({ price: level.price, color: level.color })),
    ...anchorGuidePlans.map((plan) => ({ price: plan.anchorPrice, color: planAnchorStyle(plan).color }))]
    .filter((level) => Number.isFinite(Number(level.price)) && Number(level.price) > 0)
    .reduce((unique, level) => {
      const key = Number(level.price).toFixed(3)
      if (!unique.some((item) => item.key === key)) unique.push({ ...level, key })
      return unique
    }, [])

  chartInstance?.destroy()
  chartInstance = new Chart(chartCanvas.value, {
    data: {
      datasets: [
        {
          type: 'candlestick',
          label: analysisMode.value === 'daily' ? '日K' : `${intradayPeriod.value}分钟K`,
          data: candles,
          parsing: false,
          borderColors: () => ({ up: upColor, down: downColor, unchanged: flatColor }),
          backgroundColors: () => ({ up: upColor, down: downColor, unchanged: flatColor }),
          borderWidth: 1,
        },
        {
          type: 'bar', label: '成交量', data: volumes, yAxisID: 'volume', parsing: false, order: 5,
          backgroundColor: (context) => hexToRgba(context.raw?.up ? upColor : downColor, 0.25),
          borderWidth: 0,
        },
        { type: 'line', label: '20EMA', data: ema, parsing: false, borderColor: cssVar('--chart-ema', '#2563eb'), borderWidth: 2, pointRadius: 0, tension: 0.1, spanGaps: true },
        ...levels.map((level, index) => ({
          type: 'line', label: levelLabel(level), data: fullWidthHorizontalLine(level.price, bars.length), parsing: false,
          borderColor: levelColor(level), borderWidth: levelWidth(level, index), borderDash: levelDash(level), pointRadius: 0,
        })),
        ...disciplineLevels.map((level) => ({
          type: 'line', label: level.label, data: fullWidthHorizontalLine(level.price, bars.length), parsing: false,
          borderColor: level.color, borderWidth: level.width, borderDash: level.dash, pointRadius: 0,
          pointHitRadius: 8,
        })),
        ...anchorGuidePlans.flatMap((plan) => {
          const price = Number(plan.anchorPrice)
          const style = planAnchorStyle(plan)
          return [
            {
              type: 'line', label: `锚点 · ${setupLabel(plan.type)}`, data: fullWidthHorizontalLine(price, bars.length),
              parsing: false, borderColor: style.color, borderWidth: 2, borderDash: style.dash, pointRadius: 0,
            },
          ]
        }),
        {
          type: 'line', label: '未来锚点', data: markers, parsing: false, showLine: false, pointRadius: 7, pointStyle: 'triangle',
          pointRotation: (context) => context.raw?.plan?.direction === 'SELL' ? 180 : 0,
          pointBackgroundColor: (context) => planAnchorColor(context.raw?.plan || {}),
          pointBorderColor: '#ffffff', pointBorderWidth: 1.5,
        },
      ],
    },
    plugins: [chartCrosshairPlugin, chartPriceLevelLabelsPlugin],
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { intersect: false, mode: 'index' },
      onHover: (event, active, chart) => {
        const nativeType = event?.native?.type || event?.type
        const area = chart.chartArea
        const inside = area && event.x >= area.left && event.x <= area.right && event.y >= area.top && event.y <= area.bottom
        if (nativeType === 'mouseout' || !inside) {
          clearChartHover(chart)
          return
        }
        const rawIndex = chart.scales.x.getValueForPixel(event.x)
        const index = Number.isFinite(rawIndex) ? Math.round(rawIndex) : null
        setChartHover(chart, index, event.x, event.y)
      },
      plugins: {
        legend: {
          position: 'bottom',
          labels: {
            boxWidth: isNarrowChart ? 8 : 10,
            padding: isNarrowChart ? 7 : 11,
            usePointStyle: true,
            color: chartTick,
            font: { size: isNarrowChart ? 10 : 12, weight: '700', family: CHART_UI_FONT },
            filter: (item) => item.text !== '成交量' && !item.text.endsWith('K') && !item.text.startsWith('锚点 ·'),
          },
        },
        tooltip: {
          enabled: false,
        },
        'chart-price-level-labels': {
          levels: priceLevelLabels,
          fontSize: isNarrowChart ? 10 : 11,
        },
      },
      scales: {
        x: { type: 'linear', offset: false, min: normalizedRange()[0] - 0.5, max: normalizedRange()[1] + 0.5, ticks: { maxTicksLimit: isNarrowChart ? 5 : 7, color: chartTick, font: { size: isNarrowChart ? 10 : 12, weight: '600', family: CHART_TAB_FONT }, callback: (value) => labels[Math.round(value)] || '' }, grid: { display: false } },
        y: { position: 'left', min: bounds.min, max: bounds.max, ticks: { color: chartTick, font: { size: isNarrowChart ? 10 : 12, weight: '600', family: CHART_TAB_FONT } }, grid: { color: chartGrid, lineWidth: 1 } },
        volume: { position: 'right', min: 0, max: volumeMax(volumes), ticks: { display: false, maxTicksLimit: 3, color: chartTick, font: { size: 11, weight: '600', family: CHART_TAB_FONT } }, grid: { display: false }, afterFit: (axis) => { axis.width = isNarrowChart ? 8 : 64 } },
      },
    },
  })
  bindTouchReleaseTooltip()
}

function selectChartLevels(rawLevels, hasEma, referencePrice, anchorPlans = []) {
  const priority = {
    RANGE_LOW: 0,
    RANGE_HIGH: 0,
    SWING_LOW: 1,
    SWING_HIGH: 1,
    MEASURED_MOVE_UP: 2,
    MEASURED_MOVE_DOWN: 2,
    GAP_EDGE: 3,
    RANGE_MID: 4,
    EMA20: 5,
  }
  const anchorPrices = anchorPlans
    .map((plan) => Number(plan?.anchorPrice))
    .filter((price) => Number.isFinite(price) && price > 0)
  const proximity = Math.max(Math.abs(referencePrice || 0) * 0.0015, 0.01)
  const uniqueByPrice = new Map()
  for (const level of rawLevels) {
    if (level?.kind === 'EMA20' && hasEma) continue
    const price = Number(level?.price)
    if (!Number.isFinite(price) || price <= 0) continue
    const key = price.toFixed(3)
    if (anchorPrices.some((anchorPrice) => Math.abs(anchorPrice - price) <= proximity)) continue
    const current = uniqueByPrice.get(key)
    if (!current || (priority[level.kind] ?? 6) < (priority[current.kind] ?? 6)) {
      uniqueByPrice.set(key, level)
    }
  }
  const uniqueByKind = new Map()
  for (const level of uniqueByPrice.values()) {
    const current = uniqueByKind.get(level.kind)
    if (!current || levelRelevance(level, referencePrice) < levelRelevance(current, referencePrice)) {
      uniqueByKind.set(level.kind, level)
    }
  }
  const selected = []
  for (const level of [...uniqueByKind.values()]
    .sort((left, right) => {
      const priorityDelta = (priority[left.kind] ?? 6) - (priority[right.kind] ?? 6)
      if (priorityDelta) return priorityDelta
      return Math.abs(Number(left.price) - referencePrice) - Math.abs(Number(right.price) - referencePrice)
    })) {
    if (selected.some((item) => Math.abs(Number(item.price) - Number(level.price)) <= proximity)) continue
    selected.push(level)
    if (selected.length === 4) break
  }
  return selected
}

function levelRelevance(level, referencePrice) {
  const price = Number(level?.price)
  const kind = level?.kind
  const shouldBeAbove = kind === 'SWING_HIGH' || kind === 'MEASURED_MOVE_UP'
  const shouldBeBelow = kind === 'SWING_LOW' || kind === 'MEASURED_MOVE_DOWN'
  const isOnExpectedSide = !Number.isFinite(referencePrice)
    || (!shouldBeAbove && !shouldBeBelow)
    || (shouldBeAbove && price >= referencePrice)
    || (shouldBeBelow && price <= referencePrice)
  return (isOnExpectedSide ? 0 : 1) * 1e9 + Math.abs(price - referencePrice)
}

function selectAnchorPlans(plans, referencePrice) {
  const statusPriority = { CONFIRMED: 0, PENDING: 1, NEEDS_REVIEW: 2, WATCH: 3 }
  const uniqueByPrice = new Map()
  for (const rawPlan of plans) {
    const price = Number(rawPlan?.anchorPrice ?? rawPlan?.triggerPrice ?? rawPlan?.entryLimit)
    if (!Number.isFinite(price) || price <= 0) continue
    const key = price.toFixed(3)
    const plan = {
      ...rawPlan,
      anchorPrice: price,
      anchorLabel: rawPlan?.anchorLabel || (rawPlan?.direction === 'BUY' ? '买入触发价' : rawPlan?.direction === 'SELL' ? '卖出确认点' : '观察价'),
      anchorCondition: rawPlan?.anchorCondition || rawPlan?.alternativeScenario || (rawPlan?.direction === 'SELL' ? '匹配持仓后检查收盘跌破、量能和后续跟随。' : '到达后重新检查收盘确认、量能和后续跟随。'),
      type: rawPlan?.type || rawPlan?.setupType,
    }
    const current = uniqueByPrice.get(key)
    if (!current || (statusPriority[plan.status] ?? 4) < (statusPriority[current.status] ?? 4)) {
      uniqueByPrice.set(key, plan)
    }
  }
  return [...uniqueByPrice.values()].sort((left, right) => {
    const statusDelta = (statusPriority[left.status] ?? 4) - (statusPriority[right.status] ?? 4)
    if (statusDelta) return statusDelta
    return Math.abs(Number(left.anchorPrice) - referencePrice) - Math.abs(Number(right.anchorPrice) - referencePrice)
  })
}

function selectDisciplineChartLevels(levels) {
  const technicalStop = Number(levels?.technicalStop ?? levels?.activeDefense)
  const accountRiskExit = Number(levels?.accountRiskExit ?? levels?.costStop)
  const definitions = [
    { key: 'entryPrice', label: '实际入场价', color: cssVar('--chart-position-entry', '#334155'), width: 2.15, dash: [] },
    { key: 'plannedEntryPrice', label: '加仓 / 再入场上限', color: cssVar('--chart-position-entry-plan', '#047857'), width: 2.2, dash: [9, 4] },
    { key: 'plannedEntryCandidate', label: '待确认入场观察价', color: cssVar('--chart-position-entry-watch', '#007c91'), width: 1.95, dash: [3, 4] },
    { key: 'technicalStop', price: technicalStop, label: '当前技术止损位', color: cssVar('--chart-position-stop', '#c62828'), width: 2.4, dash: [] },
    { key: 'accountRiskExit', price: accountRiskExit, label: '账户风险退出线', color: cssVar('--chart-position-account-risk', '#b45309'), width: 1.8, dash: [2, 4] },
    { key: 'firstTarget', label: '第一止盈位', color: cssVar('--chart-position-target', '#8a5a00'), width: 2.1, dash: [12, 4] },
    { key: 'extensionTarget', label: '第二止盈位', color: cssVar('--chart-position-extension', '#6d28d9'), width: 1.9, dash: [7, 3, 2, 3] },
    { key: 'downsideTarget', label: '下方路径参考', color: cssVar('--chart-position-extension', '#6d28d9'), width: 1.9, dash: [9, 4] },
  ]
  const selected = []
  const tolerance = Math.max(Number(levels?.entryPrice || 0) * 0.0005, 0.005)
  for (const definition of definitions) {
    const price = Number(definition.price ?? levels?.[definition.key])
    if (!Number.isFinite(price) || price <= 0) continue
    if (definition.key === 'plannedEntryCandidate' && levels?.plannedEntryPrice) continue
    if (definition.key === 'technicalStop' && levels?.technicalStopAvailable === false) continue
    if (selected.some((item) => Math.abs(item.price - price) <= tolerance)) continue
    selected.push({ ...definition, price })
  }
  return selected
}

function focusPlanRow(row) {
  const index = Number(row.index)
  if (!Number.isInteger(index) || index < 0) return
  const max = rangeMax.value
  visibleRange.value = [Math.max(0, index - 22), Math.min(max, index + 22)]
  finishChartWindow()
}

const structureDescriptions = {
  TREND_PULLBACK_H1: { meaning: '价格沿上涨方向走了一段后，第一次回落整理。', guidance: '先等价格重新突破回踩高点，并确认没有马上跌回整理区。' },
  TREND_PULLBACK_H2: { meaning: '第一次回落没有启动，价格再次回踩后准备向上。第二次尝试通常比第一次更有信息量。', guidance: '观察第二次向上突破和后续跟随，不在回落中间追价。' },
  TREND_PULLBACK_L1: { meaning: '价格沿下跌方向走了一段后，第一次反弹整理。', guidance: '先等价格重新跌破反弹低点，再确认下跌是否继续。' },
  TREND_PULLBACK_L2: { meaning: '第一次反弹没有结束下跌，价格第二次反弹后再次转弱。', guidance: '观察第二次向下突破和后续跟随，突破前只作观察。' },
  TREND_FIRST_PULLBACK: { meaning: '趋势中的第一次回踩或反弹，是寻找趋势是否恢复的常见位置。', guidance: '看顺势K线是否突破触发位，并检查止损和第一目标空间。' },
  TREND_TWO_LEG_PULLBACK: { meaning: '价格经过两次逆向摆动后，尝试回到原趋势方向。', guidance: '两腿不等于马上入场，仍要等突破和跟随确认。' },
  BULL_FLAG: { meaning: '上涨后出现小幅回落或横盘，可能是上涨过程中的整理。', guidance: '整理上沿被收盘突破并有跟随，才按上涨延续观察。' },
  BEAR_FLAG: { meaning: '下跌后出现小幅反弹或横盘，可能是下跌过程中的整理。', guidance: '整理下沿被收盘跌破并有跟随，才按下跌延续观察。' },
  BREAKOUT_UP: { meaning: '价格收盘越过前方区间或结构上沿，尝试转为上涨。', guidance: '重点看下一根是否坚持，靠近明显压力位时不要直接追。' },
  BREAKOUT_DOWN: { meaning: '价格收盘跌破前方区间或结构下沿，尝试转为下跌。', guidance: '重点看是否快速回到区间；回到区间通常按失败突破处理。' },
  BREAKOUT_RETEST_UP: { meaning: '向上突破后回到原边界附近测试，但暂时没有有效跌回去。', guidance: '边界守住并再次向上时，结构质量比第一根突破K更好。' },
  BREAKOUT_RETEST_DOWN: { meaning: '向下突破后反弹回原边界附近测试，但暂时没有有效站回去。', guidance: '边界压住并再次向下时，才确认下跌延续。' },
  MICRO_CHANNEL_BREAK: { meaning: '连续几根K线形成短线同向通道，当前出现通道边界被破坏的迹象。', guidance: '单次刺穿不代表反转，要等收盘越过关键位和后续跟随。' },
  SPIKE_CHANNEL: { meaning: '价格先快速单向推进，随后沿较窄通道运行。', guidance: '通道末端不追价，等待第一次回踩或通道边界确认。' },
  WIDE_CHANNEL: { meaning: '价格在较宽的上下通道内推进，方向存在但回撤也较大。', guidance: '优先看通道边缘和回踩，不把通道中部当成买点。' },
  STEP_CHANNEL: { meaning: '价格一段推进后横盘，再继续推进，形成台阶式运动。', guidance: '等下一段台阶突破或回踩确认，不在横盘中间下注。' },
  EMA_GAP_CONTEXT: { meaning: '价格已经明显离开20周期均线，趋势可能较强，也可能短线过度延伸。', guidance: '不因远离均线追价，等待回踩、旗形或二次入场。' },
  RANGE_EDGE_FADE_UP: { meaning: '价格在区间下沿附近出现向上反弹尝试。', guidance: '先看反弹K线和后续跟随，第一目标通常是区间中线。' },
  RANGE_EDGE_FADE_DOWN: { meaning: '价格在区间上沿附近出现向下回落尝试。', guidance: '先看回落K线和后续跟随，第一目标通常是区间中线。' },
  RANGE_LOWER_REVERSAL: { meaning: '价格测试区间下沿后出现向上反弹结构。', guidance: '下沿位置比形态名称重要，等向上触发和足够空间。' },
  RANGE_UPPER_REVERSAL: { meaning: '价格测试区间上沿后出现向下回落结构。', guidance: '上沿位置比形态名称重要，等向下触发和足够空间。' },
  RANGE_MIDDLE: { meaning: '价格位于区间中部，多空优势暂时不明显。', guidance: '中部通常不追单，等待价格到边缘或出现确认后的突破回踩。' },
  TIGHT_RANGE_IRON_WIRE: { meaning: 'K线小、相互重叠多、上下影线较多，说明买卖双方在反复拉锯。', guidance: '不要因为单根信号K追价，等待强突破和回测。' },
  TRIANGLE_COMPRESSION: { meaning: '高点和低点逐步收窄，波动正在压缩，市场接近选择方向。', guidance: '三角形内部先观察，等突破、跟随和回测确认。' },
  TRIANGLE_EXPANSION: { meaning: '高点和低点同时向外扩张，波动加大，双方都在用力但方向不稳定。', guidance: '先降低判断确定性，等关键摆动点被突破并有跟随。' },
  ABC_RANGE_LEGS: { meaning: '区间内先走一段，再反向回撤，再朝第一段方向测试，属于多空反复争夺的三段摆动。', guidance: '重点看是否靠近区间上沿或下沿，以及C段后有没有突破和跟随。' },
  RANGE_BREAKOUT_PENDING: { meaning: '价格刚越过区间边界，但还没有证明突破能够持续。', guidance: '等待后续K线坚持或突破回测，不把第一根突破K当成确认。' },
  RANGE_BREAKOUT_CONFIRMED: { meaning: '价格已经越过区间边界，并出现了较可信的收盘和跟随。', guidance: '仍要检查前方压力和回测，不因确认就忽略目标空间。' },
  RANGE_BREAKOUT_CONTINUATION: { meaning: '区间突破后，价格继续沿突破方向推进。', guidance: '关注回踩和新的小旗形，避免在连续大K末端追价。' },
  RANGE_BREAKOUT_RETEST: { meaning: '突破后的价格回到边界附近测试，暂时没有有效跌回或站回区间。', guidance: '边界守住并出现顺势K线后，延续判断才更可靠。' },
  FAILED_BULLISH_BREAKOUT: { meaning: '向上突破没有坚持，价格重新回到区间内部。', guidance: '先看区间中线和下沿，不因失败突破直接追反向。' },
  FAILED_BEARISH_BREAKOUT: { meaning: '向下突破没有坚持，价格重新回到区间内部。', guidance: '先看区间中线和上沿，不因失败突破直接追反向。' },
  DOUBLE_BOTTOM: { meaning: '价格两次测试相近低点，说明下方有承接，但还不能单独证明反转。', guidance: '等颈线突破、趋势受损和后续跟随。' },
  DOUBLE_TOP: { meaning: '价格两次测试相近高点，说明上方有压力，但还不能单独证明反转。', guidance: '等颈线跌破、趋势受损和后续跟随。' },
  WEDGE_THIRD_PUSH: { meaning: '价格向同一方向完成第三次推进，末端可能出现动能衰竭。', guidance: '第三次推动只提高警惕，仍需关键位突破和反向跟随。' },
}

const selectedStructureDetails = computed(() => {
  const row = selectedStructure.value
  if (!row) return null
  const description = structureDescriptions[row.type] || {
    meaning: '这是根据近期K线整理出的候选结构，用来帮助定位当前价格行为。',
    guidance: '先看位置、收盘确认、后续跟随和风险空间，不只根据名称做判断。',
  }
  return {
    ...description,
    title: setupLabel(row.type),
    status: statusLabel(row.status),
    location: row.location || '未标明',
    evidence: (row.evidence || []).filter(Boolean).slice(0, 3),
    next: row.nextCondition || (row.missingConditions || [])[0] || description.guidance,
  }
})

function structureKey(row) {
  return row.setupId || [row.type, row.observedAt || row.date || row.index || '', row.location || ''].join('|')
}

function isSelectedStructure(row) {
  return Boolean(selectedStructure.value && structureKey(selectedStructure.value) === structureKey(row))
}

function selectStructure(row) {
  selectedStructure.value = row
  focusPlanRow(row)
}

function structureRowClassName({ row }) {
  return isSelectedStructure(row) ? 'is-structure-selected' : ''
}

function priceBounds(bars, levels, plans, start, end, disciplineLevels = [], emaPoints = []) {
  const visible = bars.slice(start, end + 1)
  const values = visible.flatMap((bar) => [Number(bar.low), Number(bar.high)]).filter(Number.isFinite)
  levels.forEach((level) => values.push(Number(level.price)))
  plans.forEach((plan) => values.push(Number(plan.anchorPrice)))
  disciplineLevels.forEach((level) => values.push(Number(level.price)))
  emaPoints.forEach((point) => {
    if (point.x >= start && point.x <= end) values.push(Number(point.y))
  })
  if (!values.length) return { min: 0, max: 1 }
  const min = Math.min(...values)
  const max = Math.max(...values)
  const padding = Math.max((max - min) * 0.08, max * 0.005)
  return { min: Math.max(0, min - padding), max: max + padding }
}

function fullWidthHorizontalLine(price, barCount) {
  const numericPrice = Number(price)
  const end = Math.max(Number(barCount) || 0, 1)
  return [{ x: -1, y: numericPrice }, { x: end, y: numericPrice }]
}

function volumeMax(volumes) {
  return Math.max(...volumes.map((item) => item.y), 1) * 3
}

function volumeRatioForBar(bars, index) {
  const current = finiteQuoteNumber(bars[index]?.volume)
  if (current === null || current <= 0 || index <= 0) return null
  const baseline = bars
    .slice(Math.max(0, index - 20), index)
    .map((bar) => finiteQuoteNumber(bar.volume))
    .filter((value) => value !== null && value > 0)
    .sort((left, right) => left - right)
  if (!baseline.length) return null
  const middle = Math.floor(baseline.length / 2)
  const median = baseline.length % 2 ? baseline[middle] : (baseline[middle - 1] + baseline[middle]) / 2
  return median > 0 ? current / median : null
}

function cssVar(name, fallback) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback
}

function planAnchorColor(plan) {
  if (plan.status === 'CONFIRMED' && plan.direction === 'BUY') return cssVar('--chart-support-swing', '#059669')
  if (plan.status === 'CONFIRMED' && plan.direction === 'SELL') return cssVar('--chart-resistance-swing', '#dc2626')
  if (plan.direction === 'SELL') return cssVar('--chart-resistance-range', '#ea580c')
  if (plan.direction === 'BUY') return cssVar('--chart-support-range', '#0891b2')
  return cssVar('--warning', '#b67b26')
}

function planAnchorStyle(plan) {
  const typeColors = {
    TREND_PULLBACK_H1: ['--chart-support-swing', '#059669'],
    TREND_PULLBACK_H2: ['--chart-support-range', '#0891b2'],
    BREAKOUT_RETEST_UP: ['--chart-target-up', '#ca8a04'],
    BREAKOUT_UP: ['--chart-target-up', '#ca8a04'],
    RANGE_LOWER_REVERSAL: ['--chart-support-range', '#0891b2'],
    RANGE_UPPER_REVERSAL: ['--chart-resistance-range', '#ea580c'],
    RANGE_MIDDLE: ['--chart-range-mid', '#7c3aed'],
    DOUBLE_BOTTOM: ['--chart-support-swing', '#059669'],
    DOUBLE_TOP: ['--chart-resistance-swing', '#dc2626'],
    FAILED_BULLISH_BREAKOUT: ['--chart-resistance-swing', '#dc2626'],
    FAILED_BEARISH_BREAKOUT: ['--chart-support-swing', '#059669'],
    BEAR_FLAG: ['--chart-resistance-swing', '#dc2626'],
    WEDGE_THIRD_PUSH: ['--chart-gap', '#d946ef'],
    MAJOR_BEARISH_REVERSAL: ['--chart-resistance-swing', '#dc2626'],
    MAJOR_BULLISH_REVERSAL: ['--chart-support-swing', '#059669'],
  }
  const colorToken = typeColors[plan.type]
  const dash = ({ CONFIRMED: [], PENDING: [7, 3], NEEDS_REVIEW: [3, 3], WATCH: [2, 4] })[plan.status] || [6, 3]
  return {
    color: colorToken ? cssVar(...colorToken) : planAnchorColor(plan),
    dash,
  }
}

function hexToRgba(color, alpha) {
  const value = String(color).trim().replace('#', '')
  if (value.length !== 3 && value.length !== 6) return color
  const hex = value.length === 3 ? value.split('').map((part) => part + part).join('') : value
  const number = Number.parseInt(hex, 16)
  const red = (number >> 16) & 255
  const green = (number >> 8) & 255
  const blue = number & 255
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`
}

function formatPrice(value) {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number.toFixed(2) : '--'
}

function setupLabel(value) {
  return ({
    TREND_PULLBACK_H1: '上涨后的第一次回踩', TREND_PULLBACK_H2: '上涨后的第二次回踩', TREND_PULLBACK_L1: '下跌后的第一次反弹', TREND_PULLBACK_L2: '下跌后的第二次反弹', TREND_FIRST_PULLBACK: '趋势第一次回踩', TREND_TWO_LEG_PULLBACK: '两次回踩后的再启动', BULL_FLAG: '上涨后的整理', BEAR_FLAG: '下跌后的整理', BREAKOUT_UP: '向上突破', BREAKOUT_DOWN: '向下突破', BREAKOUT_RETEST_UP: '向上突破后回踩', BREAKOUT_RETEST_DOWN: '向下突破后反弹', MICRO_CHANNEL_BREAK: '短线通道被突破', SPIKE_CHANNEL: '快速推进后的通道', WIDE_CHANNEL: '宽幅通道', STEP_CHANNEL: '阶梯式推进', EMA_GAP_CONTEXT: '价格明显离开20周期线',
    RANGE_EDGE_FADE_UP: '区间下沿反弹', RANGE_EDGE_FADE_DOWN: '区间上沿回落', RANGE_LOWER_REVERSAL: '区间下沿反弹', RANGE_UPPER_REVERSAL: '区间上沿回落', RANGE_MIDDLE: '区间中部观望', TIGHT_RANGE_IRON_WIRE: '窄幅反复拉锯', TRIANGLE_COMPRESSION: '波动收窄，等待突破', TRIANGLE_EXPANSION: '波动放大，方向不稳', ABC_RANGE_LEGS: '区间内三段摆动', RANGE_BREAKOUT_PENDING: '突破出现，等待确认', RANGE_BREAKOUT_CONFIRMED: '突破已经站稳', RANGE_BREAKOUT_CONTINUATION: '突破后继续推进', RANGE_BREAKOUT_RETEST: '突破后回踩确认', FAILED_BULLISH_BREAKOUT: '向上突破失败', FAILED_BEARISH_BREAKOUT: '向下突破失败', BREAKOUT_FAILURE_OF_FAILURE: '失败突破后的再测试',
    DOUBLE_BOTTOM: '两次探底', DOUBLE_TOP: '两次冲高', WEDGE_THIRD_PUSH: '第三次冲击，留意衰竭', CLIMAX_SPIKE_REVERSAL: '快速推进后的反转警示', CLIMAX_REVERSAL_CANDIDATE: '高潮后的反转候选', V_REVERSAL_WARNING: '快速反向波动', HEAD_SHOULDERS_REVERSAL: '头肩形反转候选', EXPANSION_REVERSAL: '波动放大后的反转候选', FINAL_FLAG_OR_INSIDE_BREAK: '末端整理后的突破', MAJOR_BEARISH_REVERSAL: '主要向下反转', MAJOR_BULLISH_REVERSAL: '主要向上反转', REVERSAL_FAILURE: '反转尝试失败', REVERSAL_FAILURE_OF_FAILURE: '反转失败后的再测试', OPENING_TREND: '开盘后形成方向', OPENING_REVERSAL: '开盘反转', TREND_FROM_RANGE: '开盘区间后起趋势', TREND_DAY: '趋势日候选', TREND_RESUMPTION: '午后趋势恢复', CLOSE_WINDOW_REVIEW: '收盘前复核', OPENING_DATA_INSUFFICIENT: '开盘数据不足',
  })[value] || '等待结构确认'
}

function statusLabel(value) {
  return ({ CONFIRMED: '已确认', PENDING: '待确认', FAILED: '已失败', WATCH: '观察', NEEDS_REVIEW: '需复核', EXPIRED: '已过期', DATA_INSUFFICIENT: '数据不足' })[value] || '观察'
}

function levelLabel(level) {
  return ({ EMA20: '20EMA', RANGE_LOW: '区间下沿', RANGE_MID: '区间中轴', RANGE_HIGH: '区间上沿', SWING_HIGH: '摆动高点', SWING_LOW: '摆动低点', GAP_EDGE: '跳空边缘', MEASURED_MOVE_UP: '上方测量目标', MEASURED_MOVE_DOWN: '下方测量目标' })[level.kind] || level.kind
}

function levelColor(level) {
  if (level.kind === 'EMA20') return cssVar('--chart-ema', '#2563eb')
  const colors = {
    SWING_LOW: ['--chart-support-swing', '#059669'],
    RANGE_LOW: ['--chart-support-range', '#0891b2'],
    SWING_HIGH: ['--chart-resistance-swing', '#dc2626'],
    RANGE_HIGH: ['--chart-resistance-range', '#ea580c'],
    RANGE_MID: ['--chart-range-mid', '#7c3aed'],
    GAP_EDGE: ['--chart-gap', '#d946ef'],
    MEASURED_MOVE_UP: ['--chart-target-up', '#ca8a04'],
    MEASURED_MOVE_DOWN: ['--chart-target-down', '#4f46e5'],
  }
  const [token, fallback] = colors[level.kind] || (
    String(level.role).includes('SUPPORT')
      ? ['--chart-support-swing', '#059669']
      : String(level.role).includes('RESISTANCE')
        ? ['--chart-resistance-swing', '#dc2626']
        : ['--warning', '#b67b26']
  )
  return cssVar(token, fallback)
}

function levelWidth(level, index) {
  if (['RANGE_LOW', 'RANGE_HIGH', 'RANGE_MID'].includes(level.kind)) return 1.8
  if (['MEASURED_MOVE_UP', 'MEASURED_MOVE_DOWN'].includes(level.kind)) return 1.55
  return index < 3 ? 1.45 : 1.25
}

function levelDash(level) {
  return {
    EMA20: [],
    SWING_LOW: [4, 3],
    SWING_HIGH: [2, 4],
    RANGE_LOW: [10, 4],
    RANGE_HIGH: [8, 3],
    RANGE_MID: [2, 5],
    GAP_EDGE: [1, 5],
    MEASURED_MOVE_UP: [12, 4],
    MEASURED_MOVE_DOWN: [12, 2, 2, 2],
  }[level.kind] || [4, 4]
}

onBeforeUnmount(() => {
  hoverReleaseCleanup?.()
  chartInstance?.destroy()
})
</script>
