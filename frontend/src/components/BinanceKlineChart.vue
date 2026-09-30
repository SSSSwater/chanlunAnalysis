<template>
  <div ref="chartRoot" class="binance-kline-chart">
    <div :class="['binance-kline-chart-frame', { 'is-market-switching': marketSwitching }]">
      <canvas ref="chartCanvas" role="img" aria-label="Binance K 线图"></canvas>
      <div v-if="!hasMarket && !displayItems.length" class="binance-kline-chart-placeholder">等待 K 线数据</div>
      <div v-else-if="loading && !displayItems.length" class="binance-kline-chart-placeholder">正在读取 K 线...</div>
      <div v-if="loadingOverlayVisible" class="binance-kline-loading-overlay" role="status" aria-live="polite">
        <span class="binance-kline-loading-spinner" aria-hidden="true"></span>
        <span>正在读取 K 线</span>
      </div>
    </div>
    <div class="range-panel binance-kline-range-panel" :class="{ 'is-empty': !displayItems.length }">
      <div class="range-meta" :aria-hidden="!displayItems.length">
        <span>显示区间</span>
        <strong>{{ visibleRangeText }}</strong>
      </div>
      <el-slider
        v-model="visibleRange"
        range
        :min="0"
        :max="rangeMax"
        :step="1"
        :show-tooltip="false"
        :disabled="!displayItems.length"
        @input="updateChartWindow"
        @change="finishChartWindow"
      />
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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

Chart.register(BarController, BarElement, CategoryScale, LinearScale, LineController, LineElement, PointElement, CandlestickController, CandlestickElement, Tooltip, Legend)

const props = defineProps({
  klines: { type: Array, default: () => [] },
  marketKey: { type: String, default: '' },
  marketDataReadyKey: { type: String, default: '' },
  plan: { type: Object, default: null },
  currentPrice: { type: Number, default: 0 },
  interval: { type: String, default: '5m' },
  loading: { type: Boolean, default: false },
  stale: { type: Boolean, default: false },
  hasMarket: { type: Boolean, default: true },
})

const chartCanvas = ref(null)
const chartRoot = ref(null)
const visibleRange = ref([0, 0])
const hoveredIndex = ref(null)
const crosshair = ref({ x: null, y: null })
let chartInstance = null
let releaseHoverCleanup = null
let rangeInitialized = false
let lastBarCount = 0
let lastInterval = null
let visibleStartTime = null
let visibleEndTime = null
let resizeListener = null
let pendingMarketSwitch = false
let pendingRangeRestore = null
const marketSwitching = ref(false)
let marketSwitchTimer = null

// 单实例渲染：行情更新只改数据 + 缓动，不销毁重建图表。
const lastGoodItems = ref([])
let morphRaf = 0
let currentItems = []
let renderedOpenTimes = []
let lastApplied = null // 最近一次绘制完成的数据状态，作为下一次缓动的起点

const CHART_TAB_FONT = "'LXGW WenKai Screen', ui-monospace, 'SF Mono', 'Cascadia Mono', 'Roboto Mono', Menlo, Consolas, monospace, 'PingFang SC', 'HarmonyOS Sans SC', 'MiSans', 'Noto Sans SC', 'Microsoft YaHei UI', 'Microsoft YaHei', sans-serif"
const CHART_UI_FONT = "'LXGW WenKai Screen', 'Inter', -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', Roboto, 'Helvetica Neue', Arial, 'PingFang SC', 'HarmonyOS Sans SC', 'MiSans', 'Noto Sans SC', 'Microsoft YaHei UI', 'Microsoft YaHei', sans-serif"
const CHART_LINE_DASH = [4, 4]
const MORPH_MS = 300

const prefersReducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches

const asNumber = (value) => {
  const number = Number(value)
  return Number.isFinite(number) ? number : 0
}

const priceDecimalPlaces = (value) => {
  const number = Math.abs(asNumber(value))
  if (!number) return 4
  if (number >= 1) return 4
  return Math.max(0, 4 - Math.floor(Math.log10(number)) - 1)
}

const barItems = computed(() => props.klines
  .map((item) => ({
    openTime: asNumber(item.openTime),
    open: asNumber(item.open),
    high: asNumber(item.high),
    low: asNumber(item.low),
    close: asNumber(item.close),
    volume: asNumber(item.volume),
  }))
  .filter((item) => item.open > 0 && item.high > 0 && item.low > 0 && item.close > 0 && item.high >= item.low))

// 空数据可能是快照缺口的瞬时状态；宽限期内保留上一份完整数据，避免图表闪烁。
const displayItems = computed(() => barItems.value.length ? barItems.value : lastGoodItems.value)
const rangeMax = computed(() => Math.max(displayItems.value.length - 1, 0))
const dataReadyForMarket = computed(() => Boolean(
  props.marketKey
  && props.marketDataReadyKey === props.marketKey
  && barItems.value.length,
))
const loadingOverlayVisible = computed(() => Boolean(
  props.hasMarket && !dataReadyForMarket.value && !props.stale && (props.loading || pendingMarketSwitch),
))
const cssVar = (name, fallback) => getComputedStyle(chartRoot.value || document.documentElement).getPropertyValue(name).trim() || fallback

watch(barItems, (items) => {
  // 快照缺口（stale/切换中）时保留上一份完整数据，图表保持旧渲染直到新数据到达。
  if (items.length) lastGoodItems.value = items
})

const formatPrice = (value) => {
  const number = asNumber(value)
  if (!number) return '--'
  const digits = priceDecimalPlaces(number)
  return number.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: digits })
}

const formatTime = (value) => {
  if (!value) return '--'
  const date = new Date(value)
  if (props.interval.endsWith('d') || props.interval === '1w' || props.interval === '1M') {
    return date.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
  }
  return date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

const visibleRangeText = computed(() => {
  const [start, end] = normalizedRange()
  const items = displayItems.value
  if (!items.length) return '等待 K 线数据'
  return `${formatTime(items[start]?.openTime)} 至 ${formatTime(items[end]?.openTime)}`
})

function calculateEma(items, period = 20) {
  if (!items.length) return []
  const alpha = 2 / (period + 1)
  let previous = items[0].close
  return items.map((item, index) => {
    previous = index ? item.close * alpha + previous * (1 - alpha) : item.close
    return { x: index, y: previous }
  })
}

function withAlpha(color, alpha) {
  const match = String(color).trim().match(/^#([0-9a-f]{6})$/i)
  if (!match) return color
  const value = parseInt(match[1], 16)
  return `rgba(${(value >> 16) & 255}, ${(value >> 8) & 255}, ${value & 255}, ${alpha})`
}

function resolveColor(color, fallback) {
  const value = String(color || '').trim()
  if (!value) return fallback
  const variable = value.match(/^var\((--[\w-]+)(?:,\s*([^\)]+))?\)$/)
  return variable ? cssVar(variable[1], variable[2]?.trim() || fallback) : value
}

function planLevels(plan) {
  if (!plan) return []
  const stopColor = cssVar('--binance-down', '#d85b68')
  const baseLevelItems = Array.isArray(plan.chartLevels)
    ? plan.chartLevels
    : [
        { key: 'current-stop', price: plan.activeStop || plan.stopLoss, label: plan.activeStopSource === 'MOVING' ? '移动止损' : '当前计划止损', color: 'var(--binance-down)', dash: [3, 3], width: 2 },
        { key: 'cost', price: plan.costPrice, label: '实际成本', color: 'var(--text-primary)', dash: [], width: 2.2 },
        { key: 'target-protection', price: plan.targetProtection?.price, label: plan.targetProtection?.label || '近端保护目标', color: 'var(--binance-gold)', dash: [2, 4], width: 1.8 },
        { key: 'target-one', price: plan.takeProfits?.[0]?.price, label: plan.takeProfits?.[0]?.label || '第一止盈', color: 'var(--binance-up)', dash: [], width: 2 },
        { key: 'target-two', price: plan.takeProfits?.[1]?.price, label: plan.takeProfits?.[1]?.label || '扩展止盈', color: '#6d28d9', dash: [8, 3], width: 2 },
      ]
  const zoneLow = asNumber(plan.entry?.zoneLow)
  const zoneHigh = asNumber(plan.entry?.zoneHigh)
  const zoneBoundaryItems = zoneLow > 0 && zoneHigh > 0 && zoneLow !== zoneHigh
    ? [
        { key: 'entry-zone-low', price: Math.min(zoneLow, zoneHigh), label: '触发区下沿', color: 'var(--binance-gold)', showInLegend: false },
        { key: 'entry-zone-high', price: Math.max(zoneLow, zoneHigh), label: '触发区上沿', color: 'var(--binance-gold)', showInLegend: false },
      ]
    : []
  const seen = new Set()
  return [...baseLevelItems, ...zoneBoundaryItems].map((level) => {
    const price = asNumber(level?.price)
    if (price <= 0) return null
    const key = `${level?.key || level?.label || 'level'}:${price.toFixed(8)}`
    if (seen.has(key)) return null
    seen.add(key)
    return {
      key: level?.key || level?.label || 'level',
      price,
      label: level?.label || '计划点位',
      color: resolveColor(level?.color, stopColor),
      dash: CHART_LINE_DASH,
      width: Number(level?.width) > 0 ? Number(level.width) : 1.8,
      showInLegend: level?.showInLegend !== false,
    }
  }).filter(Boolean)
}

function planTriggerZone(plan) {
  const zoneLow = asNumber(plan?.entry?.zoneLow)
  const zoneHigh = asNumber(plan?.entry?.zoneHigh)
  if (zoneLow <= 0 || zoneHigh <= 0 || zoneLow === zoneHigh) return null
  return { low: Math.min(zoneLow, zoneHigh), high: Math.max(zoneLow, zoneHigh) }
}

function planCurrentPrice(currentPrice) {
  const price = asNumber(currentPrice)
  return price > 0 ? price : 0
}

const chartTriggerZonePlugin = {
  id: 'binance-chart-trigger-zone',
  beforeDatasetsDraw(chart, _args, options) {
    const area = chart.chartArea
    const scale = chart.scales.y
    const zone = options?.zone
    if (!area || !scale || !zone || !Number.isFinite(zone.low) || !Number.isFinite(zone.high)) return
    if (chart.$binanceTriggerZoneVisible === false) return
    if (zone.high < scale.min || zone.low > scale.max) return
    const highY = scale.getPixelForValue(zone.high)
    const lowY = scale.getPixelForValue(zone.low)
    if (!Number.isFinite(highY) || !Number.isFinite(lowY)) return
    const top = Math.max(area.top, Math.min(area.bottom, Math.min(highY, lowY)))
    const bottom = Math.min(area.bottom, Math.max(area.top, Math.max(highY, lowY)))
    if (bottom <= top) return
    const context = chart.ctx
    context.save()
    context.globalAlpha = 0.1
    context.fillStyle = resolveColor(options.color, '#f0b90b')
    context.fillRect(area.left, top, area.right - area.left, bottom - top)
    context.restore()
  },
}

function normalizedRange() {
  const max = rangeMax.value
  const start = Math.max(0, Math.min(Math.round(visibleRange.value?.[0] || 0), max))
  const end = Math.max(start, Math.min(Math.round(visibleRange.value?.[1] ?? max), max))
  return [start, end]
}

function resetVisibleRange() {
  const count = displayItems.value.length
  clearChartHover()
  visibleRange.value = count ? [Math.max(0, count - 20), count - 1] : [0, 0]
  rangeInitialized = true
  lastBarCount = count
  lastInterval = props.interval
  visibleStartTime = null
  visibleEndTime = null
  rememberVisibleRange()
}

function barTime(item) {
  const value = Number(item?.openTime)
  return Number.isFinite(value) && value > 0 ? value : null
}

function nearestBarIndex(items, timestamp) {
  if (!items.length || !Number.isFinite(timestamp)) return null
  let nearest = 0
  let distance = Number.POSITIVE_INFINITY
  items.forEach((item, index) => {
    const time = barTime(item)
    if (time === null) return
    const nextDistance = Math.abs(time - timestamp)
    if (nextDistance < distance) {
      distance = nextDistance
      nearest = index
    }
  })
  return Number.isFinite(distance) ? nearest : null
}

function rememberVisibleRange(items = displayItems.value) {
  if (!items.length) return
  const [start, end] = normalizedRange()
  visibleStartTime = barTime(items[start])
  visibleEndTime = barTime(items[end])
}

function preserveVisibleRange() {
  const items = displayItems.value
  const count = items.length
  if (!count) {
    clearChartHover()
    if (!rangeInitialized || lastBarCount === 0) visibleRange.value = [0, 0]
    rangeInitialized = true
    lastInterval = props.interval
    return
  }
  if (!rangeInitialized || lastBarCount === 0) {
    resetVisibleRange()
    return
  }

  const previousMax = Math.max(lastBarCount - 1, 0)
  const previousStart = Math.max(0, Math.min(Math.round(visibleRange.value?.[0] ?? 0), previousMax))
  const previousEnd = Math.max(previousStart, Math.min(Math.round(visibleRange.value?.[1] ?? previousMax), previousMax))
  const intervalChanged = lastInterval !== props.interval
  const wasTrailingWindow = previousEnd >= previousMax
  let start = previousStart
  let end = previousEnd

  if (intervalChanged) {
    // A timeframe change replaces the candle set. Keep both slider handles at
    // the same relative positions instead of jumping back to the latest bars.
    const max = count - 1
    start = previousMax > 0 ? Math.round(previousStart / previousMax * max) : 0
    end = previousMax > 0 ? Math.round(previousEnd / previousMax * max) : max
  } else if (wasTrailingWindow) {
    // A live feed may append bars or replace the fixed-size oldest bars. Keep
    // the selected window width while following the newest bar.
    const span = Math.max(0, previousEnd - previousStart)
    end = count - 1
    start = Math.max(0, end - span)
  } else {
    const anchoredStart = nearestBarIndex(items, visibleStartTime)
    const anchoredEnd = nearestBarIndex(items, visibleEndTime)
    if (anchoredStart !== null && anchoredEnd !== null) {
      start = Math.min(anchoredStart, anchoredEnd)
      end = Math.max(anchoredStart, anchoredEnd)
    }
  }
  const max = count - 1
  start = Math.max(0, Math.min(start, max))
  end = Math.max(start, Math.min(end, max))
  visibleRange.value = [start, end]
  clearChartHover()
  lastBarCount = count
  lastInterval = props.interval
  rememberVisibleRange(items)
}

function priceBounds(items) {
  const values = items.flatMap((item) => [item.low, item.high])
  if (!values.length) return { min: 0, max: 1 }
  const min = Math.min(...values)
  const max = Math.max(...values)
  const spread = max - min
  const padding = spread > 0 ? spread * 0.08 : Math.max(Math.abs(max) * 0.001, 0.0001)
  return { min: Math.max(0, min - padding), max: max + padding }
}

function fullWidthLine(price, count) {
  return [{ x: -1, y: price }, { x: Math.max(count, 1), y: price }]
}

function volumeMax(items) {
  return Math.max(...items.map((item) => item.volume), 1) * 1.25
}

const chartPriceLabelsPlugin = {
  id: 'binance-chart-price-labels',
  afterDraw(chart, _args, options) {
    const scale = chart.scales.y
    const area = chart.chartArea
    const levels = Array.isArray(options?.levels) ? options.levels : []
    if (!scale || !area || !levels.length) return
    const context = chart.ctx
    const labelX = area.left + 5
    context.save()
    context.font = `700 ${(chart.width || 0) < 560 ? 8 : 9}px ${CHART_TAB_FONT}`
    levels.forEach((level) => {
      if (level.price < scale.min || level.price > scale.max) return
      const y = scale.getPixelForValue(level.price)
      const text = formatPrice(level.price)
      const isOutsideAxis = Boolean(level.outsideAxis)
      if (isOutsideAxis) {
        // 最新价标签：右缘贴图表右壁，方块底部压在最新价虚线上，底色半透明。
        // 右侧与下侧不描边（下侧以最新价虚线为界），文字白字黑描边。
        const fontSize = (chart.width || 0) < 560 ? 10 : 11
        context.font = `700 ${fontSize}px ${CHART_TAB_FONT}`
        const textWidth = context.measureText(text).width
        const boxWidth = textWidth + 10
        const boxHeight = fontSize + 6
        const boxLeft = area.right - boxWidth
        const boxTop = Math.max(area.top, y - boxHeight)
        context.textAlign = 'right'
        context.textBaseline = 'middle'
        context.fillStyle = withAlpha(cssVar('--chart-surface', cssVar('--panel-solid', '#ffffff')), 0.55)
        context.fillRect(boxLeft, boxTop, boxWidth, boxHeight)
        context.strokeStyle = level.color
        context.lineWidth = 1
        context.beginPath()
        context.moveTo(boxLeft + boxWidth - 0.5, boxTop + 0.5)
        context.lineTo(boxLeft + 0.5, boxTop + 0.5)
        context.lineTo(boxLeft + 0.5, boxTop + boxHeight)
        context.stroke()
        const textX = boxLeft + boxWidth - 5
        const textY = boxTop + boxHeight / 2
        context.lineWidth = 2
        context.lineJoin = 'round'
        context.strokeStyle = withAlpha('#000000', 0.75)
        context.strokeText(text, textX, textY)
        context.fillStyle = withAlpha('#ffffff', 0.75)
        context.fillText(text, textX, textY)
        return
      }
      context.textAlign = 'left'
      context.textBaseline = 'bottom'
      context.fillStyle = level.color
      context.fillText(text, labelX, Math.max(area.top + 10, y - 3))
    })
    context.restore()
  },
}

const chartCrosshairPlugin = {
  id: 'binance-chart-crosshair',
  afterDraw(chart) {
    const area = chart.chartArea
    const point = crosshair.value
    const scale = chart.scales.y
    if (!area || !scale || !Number.isFinite(point?.x) || !Number.isFinite(point?.y)) return
    const context = chart.ctx
    const lineColor = cssVar('--chart-crosshair', '#64748b')
    const y = Math.max(area.top, Math.min(area.bottom, point.y))
    context.save()
    context.strokeStyle = lineColor
    context.lineWidth = 1
    context.setLineDash([4, 4])
    context.beginPath()
    context.moveTo(point.x, area.top)
    context.lineTo(point.x, area.bottom)
    context.moveTo(area.left, y)
    context.lineTo(area.right, y)
    context.stroke()

    // Keep the live price readout on the chart's right edge, independent of
    // the y-axis and any plan-level labels drawn on the left.
    const price = scale.getValueForPixel(y)
    if (Number.isFinite(price)) {
      const fontSize = (chart.width || 0) < 560 ? 10 : 11
      const text = formatPrice(price)
      const labelHeight = fontSize + 8
      const labelRight = Math.max(area.right + 4, chart.width - 4)
      context.font = `700 ${fontSize}px ${CHART_TAB_FONT}`
      const textWidth = context.measureText(text).width
      const labelWidth = textWidth + 10
      const labelLeft = Math.max(area.left, labelRight - labelWidth)
      const labelTop = Math.max(area.top, Math.min(area.bottom - labelHeight, y - labelHeight / 2))
      context.textAlign = 'right'
      context.textBaseline = 'middle'
      context.fillStyle = cssVar('--chart-surface', cssVar('--panel-solid', '#ffffff'))
      context.fillRect(labelLeft, labelTop, labelWidth, labelHeight)
      context.strokeStyle = lineColor
      context.setLineDash([])
      context.strokeRect(labelLeft, labelTop, labelWidth, labelHeight)
      context.fillStyle = lineColor
      context.fillText(text, labelRight - 5, labelTop + labelHeight / 2)
    }
    context.restore()
  },
}

function clearChartHover(chart = chartInstance) {
  hoveredIndex.value = null
  crosshair.value = { x: null, y: null }
  chart?.draw()
}

function setChartHover(chart, index, x, y) {
  if (!Number.isFinite(index) || index < 0 || index >= displayItems.value.length) {
    clearChartHover(chart)
    return
  }
  hoveredIndex.value = index
  crosshair.value = { x, y }
  chart.draw()
}

// 触屏松手后，移动端浏览器会在指尖位置补发一组合成鼠标事件（mousemove/click）。
// Chart.js 会把它们当作真实悬停，把刚清掉的 tooltip 和十字线再次唤出，且该位置
// 会被记为“最近事件”，在后续行情刷新重放时反复复活浮窗。这里在松手后短暂
// 吞掉图表区内的合成 mousemove/click；图例区（图表区外）的 click 不拦，
// 保证触屏仍可点图例切换显隐。
const GHOST_MOUSE_LOCK_MS = 600
let ghostMouseLockUntil = 0

const chartTouchGuardPlugin = {
  id: 'binance-chart-touch-guard',
  beforeEvent(_chart, args) {
    if (performance.now() >= ghostMouseLockUntil) return
    const nativeType = args?.event?.native?.type
    if (nativeType === 'mousemove') return false
    if (nativeType === 'click' && args.inChartArea) return false
  },
}

function clearTouchHover() {
  const chart = chartInstance
  if (!chart) return
  // Chart.js 默认不监听 touchend：按住时激活的内置 tooltip 必须在这里手动收起
  // （本图表禁用动画，setActiveElements 的透明度变化同步生效，draw 即隐藏）。
  if (chart.tooltip) chart.tooltip.setActiveElements([], { x: 0, y: 0 })
  // 顺带清空 chart._active 与内部记录的“最近事件”，避免行情刷新时重放触摸事件。
  chart.setActiveElements([])
  clearChartHover(chart)
  ghostMouseLockUntil = performance.now() + GHOST_MOUSE_LOCK_MS
}

function updateChartWindow() {
  if (!chartInstance || !displayItems.value.length) return
  const items = displayItems.value
  const [start, end] = normalizedRange()
  rememberVisibleRange(items)
  const bounds = priceBounds(items.slice(start, end + 1))
  const scales = chartInstance.options?.scales
  if (!scales?.x || !scales?.y || !scales?.volume) return
  scales.x.min = start - 0.5
  scales.x.max = end + 0.5
  scales.y.min = bounds.min
  scales.y.max = bounds.max
  scales.volume.max = volumeMax(items.slice(start, end + 1))
  clearChartHover(chartInstance)
  chartInstance.update('none')
}

async function finishChartWindow() {
  await nextTick()
  await new Promise((resolve) => requestAnimationFrame(resolve))
  syncChart()
  await new Promise((resolve) => requestAnimationFrame(resolve))
  chartInstance?.resize()
  chartInstance?.update('none')
}

function bindTouchRelease() {
  releaseHoverCleanup?.()
  if (!chartCanvas.value) return
  const canvas = chartCanvas.value
  const clear = clearTouchHover
  const update = (event) => {
    const touch = event.touches?.[0] || event.changedTouches?.[0]
    const chart = chartInstance
    if (!touch || !chart) return
    const rect = canvas.getBoundingClientRect()
    const x = touch.clientX - rect.left
    const y = touch.clientY - rect.top
    const area = chart.chartArea
    const inside = area && x >= area.left && x <= area.right && y >= area.top && y <= area.bottom
    if (!inside) {
      clearChartHover(chart)
      return
    }
    const rawIndex = chart.scales.x.getValueForPixel(x)
    const index = Number.isFinite(rawIndex) ? Math.round(rawIndex) : null
    setChartHover(chart, index, x, y)
  }
  canvas.addEventListener('touchstart', update, { passive: true })
  canvas.addEventListener('touchmove', update, { passive: true })
  canvas.addEventListener('touchend', clear, { passive: true })
  canvas.addEventListener('touchcancel', clear, { passive: true })
  releaseHoverCleanup = () => {
    canvas.removeEventListener('touchstart', update)
    canvas.removeEventListener('touchmove', update)
    canvas.removeEventListener('touchend', clear)
    canvas.removeEventListener('touchcancel', clear)
  }
}

// ---- 单实例更新与缓动 ----

function buildChartState(items) {
  return {
    candles: items.map((item, index) => ({ x: index, o: item.open, h: item.high, l: item.low, c: item.close })),
    volumes: items.map((item, index) => ({ x: index, y: item.volume, up: item.close >= item.open })),
    ema: calculateEma(items),
  }
}

const lerp = (from, to, p) => from + (to - from) * p
const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3)

function interpolateState(from, to, p) {
  const count = to.candles.length
  const lastFrom = from.candles[from.candles.length - 1]
  const candles = []
  const volumes = []
  for (let i = 0; i < count; i++) {
    const target = to.candles[i]
    // 新增的 K 线从上一根收盘价“长”出来；已有 K 线逐值缓动。
    const source = i < from.candles.length
      ? from.candles[i]
      : { x: i, o: lastFrom?.c ?? target.o, h: lastFrom?.c ?? target.o, l: lastFrom?.c ?? target.o, c: lastFrom?.c ?? target.o }
    candles.push({
      x: i,
      o: lerp(source.o, target.o, p),
      h: lerp(source.h, target.h, p),
      l: lerp(source.l, target.l, p),
      c: lerp(source.c, target.c, p),
    })
    const volumeTarget = to.volumes[i]
    const volumeSource = i < from.volumes.length ? from.volumes[i] : { y: 0 }
    volumes.push({ x: i, y: lerp(volumeSource.y, volumeTarget.y, p), up: volumeTarget.up })
  }
  const lastEmaY = from.ema[from.ema.length - 1]?.y
  const ema = to.ema.map((point, i) => {
    const source = i < from.ema.length ? from.ema[i] : { x: i, y: lastEmaY ?? point.y }
    return { x: i, y: lerp(source.y, point.y, p) }
  })
  return { candles, volumes, ema }
}

function applyScales(target) {
  const scales = chartInstance.options.scales
  scales.x.min = target.xMin
  scales.x.max = target.xMax
  scales.y.min = target.yMin
  scales.y.max = target.yMax
  scales.volume.max = target.vMax
}

function applyState(state, target) {
  chartInstance.data.datasets[0].data = state.candles
  chartInstance.data.datasets[1].data = state.volumes
  chartInstance.data.datasets[2].data = state.ema
  applyScales(target)
  chartInstance.update('none')
}

function syncTailDatasets(items) {
  const datasets = chartInstance.data.datasets
  const count = items.length
  // 最新价优先取最后一根收盘价（与最右 K 线位置完全一致），ticker 仅作兜底。
  const lastClose = asNumber(items[items.length - 1]?.close)
  const tickerPrice = planCurrentPrice(props.currentPrice)
  const currentPrice = lastClose > 0 ? lastClose : tickerPrice
  const priceLineData = currentPrice > 0 ? fullWidthLine(currentPrice, count) : []
  const levels = planLevels(props.plan)
  const tailTargets = [
    { label: '当前价格', data: priceLineData, borderColor: '#8a97a6', borderWidth: 1.2, borderDash: CHART_LINE_DASH, binanceLegend: false },
    ...levels.map((level) => ({
      label: level.label,
      data: fullWidthLine(level.price, count),
      borderColor: level.color,
      borderWidth: level.width,
      borderDash: level.dash,
      binanceLegend: level.showInLegend,
    })),
  ]
  // 基础三组（K 线 / 成交量 / EMA）之后全是价格横线类数据集，按 label 对齐同步。
  const tailOffset = 3
  const existing = datasets.slice(tailOffset)
  const existingByLabel = new Map(existing.map((ds) => [ds.label, ds]))
  const nextTail = []
  tailTargets.forEach((target) => {
    const ds = existingByLabel.get(target.label)
    if (ds) {
      ds.data = target.data
      ds.borderColor = target.borderColor
      ds.borderWidth = target.borderWidth
      if (target.borderDash) ds.borderDash = target.borderDash
      ds.binanceLegend = target.binanceLegend
      nextTail.push(ds)
      existingByLabel.delete(target.label)
    } else {
      nextTail.push({
        type: 'line',
        label: target.label,
        data: target.data,
        parsing: false,
        borderColor: target.borderColor,
        borderWidth: target.borderWidth,
        borderDash: target.borderDash || [],
        pointRadius: 0,
        binanceLegend: target.binanceLegend,
      })
    }
  })
  chartInstance.data.datasets = [...datasets.slice(0, tailOffset), ...nextTail]
  const triggerZone = planTriggerZone(props.plan)
  chartInstance.options.plugins['binance-chart-trigger-zone'].zone = triggerZone
  const currentPriceLevel = currentPrice > 0 ? { price: currentPrice, color: '#8a97a6', outsideAxis: true } : null
  chartInstance.options.plugins['binance-chart-price-labels'].levels = [
    ...levels.map((level) => ({ price: level.price, color: level.color, outsideAxis: false })),
    ...[currentPriceLevel].filter(Boolean),
  ]
  chartInstance.$binanceTriggerZone = triggerZone
}

function syncResponsiveOptions() {
  if (!chartInstance) return
  const isNarrow = (chartCanvas.value?.clientWidth || 0) < 560
  const scales = chartInstance.options.scales
  scales.x.ticks.maxTicksLimit = isNarrow ? 5 : 8
  scales.x.ticks.font.size = isNarrow ? 9 : 10
  scales.y.ticks.font.size = isNarrow ? 9 : 10
  scales.volume.afterFit = (axis) => { axis.width = isNarrow ? 7 : 42 }
  chartInstance.options.plugins.legend.labels.boxWidth = isNarrow ? 8 : 10
  chartInstance.options.plugins.legend.labels.padding = isNarrow ? 7 : 11
  chartInstance.options.plugins.legend.labels.font.size = isNarrow ? 10 : 11
}

function createChart(items) {
  const [start, end] = normalizedRange()
  const bounds = priceBounds(items.slice(start, end + 1))
  const state = buildChartState(items)
  const upColor = cssVar('--binance-rise', '#d85b68')
  const downColor = cssVar('--binance-fall', '#0f9f73')
  const chartTick = cssVar('--text-muted', '#5f7080')
  const chartGrid = cssVar('--border-subtle', '#d9e2e8')
  const isNarrow = (chartCanvas.value.clientWidth || 0) < 560
  const levels = planLevels(props.plan)
  const triggerZone = planTriggerZone(props.plan)
  const lastCloseInit = asNumber(items[items.length - 1]?.close)
  const currentPrice = lastCloseInit > 0 ? lastCloseInit : planCurrentPrice(props.currentPrice)
  const currentPriceLevel = currentPrice > 0
    ? { price: currentPrice, color: '#8a97a6', outsideAxis: true }
    : null
  const levelLabels = [...levels, currentPriceLevel].filter(Boolean).map((level) => ({
    price: level.price,
    color: level.color,
    outsideAxis: Boolean(level.outsideAxis),
  }))
  const triggerZoneLegendColor = resolveColor('var(--binance-gold)', '#f0b90b')

  chartInstance = new Chart(chartCanvas.value, {
    data: {
      datasets: [
        {
          type: 'candlestick',
          label: `${props.interval} K`,
          data: state.candles,
          parsing: false,
          borderColors: () => ({ up: upColor, down: downColor, unchanged: chartTick }),
          backgroundColors: () => ({ up: upColor, down: downColor, unchanged: chartTick }),
          borderWidth: 1,
        },
        {
          type: 'bar',
          label: '成交量',
          data: state.volumes,
          yAxisID: 'volume',
          parsing: false,
          order: 5,
          backgroundColor: (context) => context.raw?.up ? `${upColor}55` : `${downColor}55`,
          borderWidth: 0,
        },
        {
          type: 'line',
          label: '20EMA',
          data: state.ema,
          parsing: false,
          borderColor: '#2563eb',
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.1,
          spanGaps: true,
        },
        {
          type: 'line',
          label: '当前价格',
          data: currentPrice > 0 ? fullWidthLine(currentPrice, items.length) : [],
          parsing: false,
          borderColor: '#8a97a6',
          borderWidth: 1.2,
          borderDash: CHART_LINE_DASH,
          pointRadius: 0,
          binanceLegend: false,
        },
        ...levels.map((level) => ({
          type: 'line',
          label: level.label,
          data: fullWidthLine(level.price, items.length),
          parsing: false,
          borderColor: level.color,
          borderWidth: level.width,
          borderDash: level.dash,
          pointRadius: 0,
          binanceLegend: level.showInLegend,
        })),
      ],
    },
    plugins: [chartTriggerZonePlugin, chartCrosshairPlugin, chartPriceLabelsPlugin, chartTouchGuardPlugin],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { intersect: false, mode: 'index' },
      onHover: (event, _active, chart) => {
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
          onClick: (_event, legendItem, legend) => {
            if (legendItem?.text === '触发区') {
              legend.chart.$binanceTriggerZoneVisible = legend.chart.$binanceTriggerZoneVisible === false
              legend.chart.update('none')
              return
            }
            const datasetIndex = legendItem?.datasetIndex
            if (!Number.isInteger(datasetIndex)) return
            const chart = legend.chart
            chart.setDatasetVisibility(datasetIndex, !chart.isDatasetVisible(datasetIndex))
            chart.update('none')
          },
          onHover: (event) => {
            if (event?.native?.target) event.native.target.style.cursor = 'pointer'
          },
          onLeave: (event) => {
            if (event?.native?.target) event.native.target.style.cursor = ''
          },
          labels: {
            boxWidth: isNarrow ? 8 : 10,
            padding: isNarrow ? 7 : 11,
            usePointStyle: true,
            color: chartTick,
            font: { size: isNarrow ? 10 : 11, weight: '700', family: CHART_UI_FONT },
            filter: (item, data) => {
              const dataset = data.datasets?.[item.datasetIndex]
              return dataset?.type !== 'candlestick'
                && item.text !== '成交量'
                && item.text !== '当前价格'
            },
            generateLabels: (chart) => {
              const labels = Chart.defaults.plugins.legend.labels.generateLabels(chart)
                .filter((item) => {
                  const dataset = chart.data.datasets[item.datasetIndex]
                  return dataset?.type !== 'candlestick'
                    && item.text !== '成交量'
                    && item.text !== '当前价格'
                    && dataset?.binanceLegend !== false
                })
              const zone = planTriggerZone(props.plan)
              if (!zone) return labels
              return [...labels, {
                text: '触发区',
                fillStyle: triggerZoneLegendColor,
                strokeStyle: triggerZoneLegendColor,
                lineWidth: 1,
                hidden: chart.$binanceTriggerZoneVisible === false,
                datasetIndex: null,
              }]
            },
          },
        },
        tooltip: {
          enabled: true,
          callbacks: {
            title: (itemsAtPoint) => formatTime(currentItems[itemsAtPoint[0]?.dataIndex]?.openTime),
            label: (context) => {
              if (context.dataset.type === 'candlestick') {
                const candle = context.raw || {}
                return `开 ${formatPrice(candle.o)} · 高 ${formatPrice(candle.h)} · 低 ${formatPrice(candle.l)} · 收 ${formatPrice(candle.c)}`
              }
              if (context.dataset.label === '成交量') return `成交量 ${asNumber(context.raw?.y).toLocaleString('en-US', { maximumFractionDigits: 2 })}`
              if (context.dataset.label === '20EMA') return `20EMA ${formatPrice(context.raw?.y)}`
              return `${context.dataset.label} ${formatPrice(context.raw?.y)}`
            },
          },
        },
        'binance-chart-trigger-zone': { zone: triggerZone, color: 'var(--binance-gold)' },
        'binance-chart-price-labels': { levels: levelLabels },
      },
      scales: {
        x: {
          type: 'linear',
          offset: false,
          min: start - 0.5,
          max: end + 0.5,
          ticks: {
            maxTicksLimit: isNarrow ? 5 : 8,
            color: chartTick,
            font: { size: isNarrow ? 9 : 10, weight: '600', family: CHART_TAB_FONT },
            callback: (value) => formatTime(currentItems[Math.round(value)]?.openTime),
          },
          grid: { display: false },
        },
        y: {
          position: 'left',
          min: bounds.min,
          max: bounds.max,
          ticks: {
            color: chartTick,
            maxTicksLimit: 6,
            font: { size: isNarrow ? 9 : 10, weight: '600', family: CHART_TAB_FONT },
            callback: (value) => formatPrice(value),
          },
          grid: { color: chartGrid, lineWidth: 1 },
        },
        volume: {
          position: 'right',
          min: 0,
          max: volumeMax(items.slice(start, end + 1)),
          ticks: { display: false },
          grid: { display: false },
          afterFit: (axis) => { axis.width = isNarrow ? 7 : 42 },
        },
      },
    },
  })
  lastApplied = state
  lastApplied.price = currentPrice > 0 ? currentPrice : undefined
  bindTouchRelease()
}

function syncChart() {
  const items = displayItems.value
  if (!chartCanvas.value || !items.length || !props.hasMarket) {
    releaseHoverCleanup?.()
    if (chartInstance && !props.hasMarket) {
      // 用户主动清除市场选择：销毁图表并回到占位符。
      chartInstance.destroy()
      chartInstance = null
      lastApplied = null
    }
    return
  }
  currentItems = items
  if (!chartInstance) {
    createChart(items)
    const [start, end] = normalizedRange()
    const bounds = priceBounds(items.slice(start, end + 1))
    lastApplied = buildChartState(items)
    applyState(lastApplied, { xMin: start - 0.5, xMax: end + 0.5, yMin: bounds.min, yMax: bounds.max, vMax: volumeMax(items.slice(start, end + 1)) })
    renderedOpenTimes = items.map((item) => item.openTime)
    pendingMarketSwitch = false
    return
  }
  const previousOpenTimes = renderedOpenTimes
  const nextOpenTimes = items.map((item) => item.openTime)
  const hasNewBar = previousOpenTimes.length > 0 && (
    previousOpenTimes.length !== nextOpenTimes.length
    || previousOpenTimes[previousOpenTimes.length - 1] !== nextOpenTimes[nextOpenTimes.length - 1]
  )
  syncTailDatasets(items)
  syncResponsiveOptions()
  const [start, end] = normalizedRange()
  const bounds = priceBounds(items.slice(start, end + 1))
  const toState = buildChartState(items)
  const target = {
    xMin: start - 0.5,
    xMax: end + 0.5,
    yMin: bounds.min,
    yMax: bounds.max,
    vMax: volumeMax(items.slice(start, end + 1)),
  }
  const shouldAnimateMarketSwitch = pendingMarketSwitch && dataReadyForMarket.value
  if (shouldAnimateMarketSwitch) {
    pendingMarketSwitch = false
    playMarketSwitchTransition()
    startMorph(toState, target)
    renderedOpenTimes = nextOpenTimes
    return
  }

  if (hasNewBar) {
    // A completed interval adds/replaces one candle. Let the whole series
    // settle together so the newest candle enters without a chart flash.
    startMorph(toState, target)
  } else {
    // Polling the same candle only animates its OHLC/volume and the price line;
    // older candles and the visible window stay fixed.
    startLatestBarMorph(toState, target)
  }
  renderedOpenTimes = nextOpenTimes
}

function playMarketSwitchTransition() {
  if (prefersReducedMotion()) return
  marketSwitching.value = false
  if (marketSwitchTimer) window.clearTimeout(marketSwitchTimer)
  requestAnimationFrame(() => {
    marketSwitching.value = true
    marketSwitchTimer = window.setTimeout(() => {
      marketSwitching.value = false
      marketSwitchTimer = null
    }, MORPH_MS + 120)
  })
}

// 快速连发更新时，从当前插值状态继续缓动，保持视觉连续。
function currentInterpolatedState() {
  if (morphRaf && chartInstance) {
    return {
      candles: chartInstance.data.datasets[0].data,
      volumes: chartInstance.data.datasets[1].data,
      ema: chartInstance.data.datasets[2].data,
    }
  }
  return lastApplied
}

function priceLineTargetPrice(candles) {
  const lastClose = asNumber(candles[candles.length - 1]?.c)
  return lastClose > 0 ? lastClose : planCurrentPrice(props.currentPrice)
}

function startLatestBarMorph(toState, target) {
  if (!lastApplied || !chartInstance) {
    lastApplied = toState
    lastApplied.price = priceLineTargetPrice(toState.candles)
    applyState(toState, target)
    return
  }
  if (prefersReducedMotion()) {
    if (morphRaf) cancelAnimationFrame(morphRaf)
    morphRaf = 0
    lastApplied = toState
    lastApplied.price = priceLineTargetPrice(toState.candles)
    applyState(toState, target)
    return
  }
  if (morphRaf) cancelAnimationFrame(morphRaf)
  const from = currentInterpolatedState() || lastApplied
  const lastIndex = toState.candles.length - 1
  const fromCandle = from.candles[Math.min(lastIndex, from.candles.length - 1)] || toState.candles[lastIndex]
  const fromVolume = from.volumes[Math.min(lastIndex, from.volumes.length - 1)]?.y ?? toState.volumes[lastIndex]?.y ?? 0
  const fromEma = from.ema[Math.min(lastIndex, from.ema.length - 1)]?.y ?? toState.ema[lastIndex]?.y ?? 0
  const targetCandle = toState.candles[lastIndex]
  const targetVolume = toState.volumes[lastIndex]?.y ?? 0
  const targetEma = toState.ema[lastIndex]?.y ?? 0
  const fromPrice = Number.isFinite(lastApplied.price) ? lastApplied.price : fromCandle?.c
  const toPrice = priceLineTargetPrice(toState.candles)
  const scales = {
    xMin: chartInstance.options.scales.x.min,
    xMax: chartInstance.options.scales.x.max,
    yMin: target.yMin,
    yMax: target.yMax,
    vMax: target.vMax,
  }
  const t0 = performance.now()
  const step = () => {
    const raw = Math.min(1, (performance.now() - t0) / MORPH_MS)
    const p = easeOutCubic(raw)
    const state = {
      candles: toState.candles.map((item, index) => index === lastIndex
        ? { ...item, o: lerp(fromCandle.o, targetCandle.o, p), h: lerp(fromCandle.h, targetCandle.h, p), l: lerp(fromCandle.l, targetCandle.l, p), c: lerp(fromCandle.c, targetCandle.c, p) }
        : item),
      volumes: toState.volumes.map((item, index) => index === lastIndex ? { ...item, y: lerp(fromVolume, targetVolume, p) } : item),
      ema: toState.ema.map((item, index) => index === lastIndex ? { ...item, y: lerp(fromEma, targetEma, p) } : item),
    }
    applyState(state, scales)
    const priceLineDs = chartInstance.data.datasets.find((dataset) => dataset.label === '当前价格')
    if (priceLineDs && Number.isFinite(fromPrice) && Number.isFinite(toPrice)) {
      priceLineDs.data = fullWidthLine(lerp(fromPrice, toPrice, p), toState.candles.length)
      chartInstance.update('none')
    }
    if (raw < 1) {
      morphRaf = requestAnimationFrame(step)
    } else {
      lastApplied = toState
      lastApplied.price = toPrice
      morphRaf = 0
    }
  }
  morphRaf = requestAnimationFrame(step)
}

function startMorph(toState, target) {
  if (!lastApplied) {
    lastApplied = toState
    applyState(toState, target)
    return
  }
  const from = currentInterpolatedState() || lastApplied
  if (prefersReducedMotion()) {
    lastApplied = toState
    lastApplied.price = priceLineTargetPrice(toState.candles)
    const priceLabel = chartInstance.options.plugins['binance-chart-price-labels'].levels?.find((l) => l.outsideAxis)
    if (priceLabel) priceLabel.price = lastApplied.price
    applyState(toState, target)
    return
  }
  if (morphRaf) cancelAnimationFrame(morphRaf)
  const fromScales = {
    xMin: chartInstance.options.scales.x.min,
    xMax: chartInstance.options.scales.x.max,
    yMin: chartInstance.options.scales.y.min,
    yMax: chartInstance.options.scales.y.max,
    vMax: chartInstance.options.scales.volume.max,
  }
  const priceLineDs = chartInstance.data.datasets.find((d) => d.label === '当前价格')
  const toPrice = priceLineDs?.data?.[0]?.y
  const fromPrice = Number.isFinite(lastApplied.price) ? lastApplied.price : toPrice
  const t0 = performance.now()
  const step = () => {
    const raw = Math.min(1, (performance.now() - t0) / MORPH_MS)
    const p = easeOutCubic(raw)
    const state = interpolateState(from, toState, p)
    const scales = {
      xMin: lerp(fromScales.xMin, target.xMin, p),
      xMax: lerp(fromScales.xMax, target.xMax, p),
      yMin: lerp(fromScales.yMin, target.yMin, p),
      yMax: lerp(fromScales.yMax, target.yMax, p),
      vMax: lerp(fromScales.vMax, target.vMax, p),
    }
    applyState(state, scales)
    if (priceLineDs && Number.isFinite(toPrice) && toPrice > 0 && Number.isFinite(fromPrice) && fromPrice > 0) {
      const y = lerp(fromPrice, toPrice, p)
      priceLineDs.data = fullWidthLine(y, toState.candles.length)
      // 最新价标签与虚线一起缓动移动
      const priceLabel = chartInstance.options.plugins['binance-chart-price-labels'].levels?.find((l) => l.outsideAxis)
      if (priceLabel) priceLabel.price = y
    }
    if (raw < 1) {
      morphRaf = requestAnimationFrame(step)
    } else {
      lastApplied = toState
      lastApplied.price = toPrice
      morphRaf = 0
    }
  }
  morphRaf = requestAnimationFrame(step)
}

watch(() => props.interval, async (value, previousValue) => {
  if (value === previousValue) return
  await nextTick()
  syncChart()
})

watch(() => props.marketKey, async (value, previousValue) => {
  if (value === previousValue) return
  pendingMarketSwitch = Boolean(value && previousValue)
  pendingRangeRestore = pendingMarketSwitch
    ? (pendingRangeRestore || [...visibleRange.value])
    : null
  // Do not let the previous instrument's bars be mistaken for the newly
  // selected instrument while its request is still in flight.
  lastGoodItems.value = []
  await nextTick()
  syncChart()
})

watch(() => props.klines, async () => {
  // The range slider is a user-selected window. A normal polling refresh must
  // not move either handle or silently follow the newest candle.
  if (barItems.value.length && pendingRangeRestore) {
    const max = Math.max(barItems.value.length - 1, 0)
    const savedStart = Math.max(0, Math.min(Math.round(pendingRangeRestore[0] ?? 0), max))
    const savedEnd = Math.max(savedStart, Math.min(Math.round(pendingRangeRestore[1] ?? max), max))
    visibleRange.value = [savedStart, savedEnd]
    rangeInitialized = true
    lastBarCount = barItems.value.length
    lastInterval = props.interval
    rememberVisibleRange(barItems.value)
    pendingRangeRestore = null
  } else if (!rangeInitialized || lastBarCount === 0) preserveVisibleRange()
  await nextTick()
  syncChart()
}, { deep: true })

watch(() => props.loading, async () => {
  await nextTick()
  syncChart()
})

watch(() => props.plan, async () => {
  await nextTick()
  syncChart()
}, { deep: true })

watch(() => props.currentPrice, async () => {
  await nextTick()
  syncChart()
})

onMounted(() => {
  resetVisibleRange()
  syncChart()
  resizeListener = () => syncResponsiveOptions()
  window.addEventListener('resize', resizeListener)
})

defineExpose({
  getChart: () => chartInstance,
})

onBeforeUnmount(() => {
  if (morphRaf) cancelAnimationFrame(morphRaf)
  if (marketSwitchTimer) window.clearTimeout(marketSwitchTimer)
  if (resizeListener) window.removeEventListener('resize', resizeListener)
  releaseHoverCleanup?.()
  chartInstance?.destroy()
})
</script>

<style scoped>
.binance-kline-chart { width: 100%; max-width: 100%; min-width: 0; }
.binance-kline-chart-frame { position: relative; height: 390px; min-width: 0; overflow: hidden; }
.binance-kline-chart-frame canvas { display: block; width: 100% !important; height: 100% !important; }
.binance-kline-chart-frame.is-market-switching canvas { animation: binance-kline-market-enter 300ms cubic-bezier(.22, 1, .36, 1); }
.binance-kline-chart-placeholder { position: absolute; inset: 0; display: grid; place-items: center; color: var(--text-faint); font-size: 12px; font-weight: 800; pointer-events: none; }
.binance-kline-loading-overlay { position: absolute; inset: 0; z-index: 2; display: inline-flex; align-items: center; justify-content: center; gap: 9px; background: color-mix(in srgb, var(--panel-solid) 62%, transparent); color: var(--text-primary); font-size: 13px; font-weight: 800; letter-spacing: 0; pointer-events: none; backdrop-filter: blur(2px); }
.binance-kline-loading-spinner { width: 18px; height: 18px; border: 2px solid color-mix(in srgb, var(--binance-gold) 24%, transparent); border-top-color: var(--binance-gold); border-radius: 50%; animation: binance-kline-loading-spin .72s linear infinite; }
.binance-kline-range-panel { width: 100%; max-width: 100%; min-width: 0; margin-top: 10px; padding: 10px 18px 6px; overflow: hidden; box-sizing: border-box; border: 0; border-radius: 0; background: transparent; box-shadow: none; }
.binance-kline-range-panel :deep(.el-slider) { width: 100%; max-width: 100%; margin: 0; box-sizing: border-box; }
.binance-kline-range-panel.is-empty > * { visibility: hidden; }
@keyframes binance-kline-market-enter { from { opacity: .24; transform: translateY(10px) scale(.992); } to { opacity: 1; transform: translateY(0) scale(1); } }
@keyframes binance-kline-loading-spin { to { transform: rotate(360deg); } }
@media (max-width: 480px) {
  .binance-kline-chart-frame { height: 310px; }
}
@media (max-width: 820px) {
  .binance-kline-chart-frame { height: clamp(200px, 32vh, 250px); }
  .binance-kline-range-panel { margin-top: 2px; padding: 0 8px; background: transparent; }
  .binance-kline-range-panel .range-meta { display: none; }
  .binance-kline-range-panel :deep(.el-slider) { --el-slider-button-size: 12px; width: calc(100% - 12px); height: 16px; margin: 0 6px; }
  .binance-kline-range-panel :deep(.el-slider__runway) { margin: 6px 0; }
  .binance-kline-range-panel :deep(.el-slider__button) { width: 12px; height: 12px; }
}
</style>
