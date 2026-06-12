<template>
  <section class="analysis-row">
    <aside class="analysis-info">
      <slot name="info"></slot>
    </aside>

    <section class="chart-area">
      <div v-if="result" class="toolbar">
        <div class="chart-heading">
          <div v-if="displayKline" class="hover-kline-head">
            <div class="hover-price-block">
              <strong :class="['hover-change', changeClass(resolvedPctChange)]">
                {{ formatPercent(resolvedPctChange) }}
              </strong>
              <span :class="['hover-close', changeClass(resolvedPctChange)]">
                {{ formatPrice(displayKline.close) }}
              </span>
            </div>
            <div class="hover-meta-block">
              <div class="title-line">
                <h2>{{ hoverTitle }}</h2>
              </div>
              <div class="hover-kline-strip">
                <span>{{ displayKline.date || displayKline.endDate }}</span>
                <span>开 {{ formatPrice(displayKline.open) }}</span>
                <span>低 {{ formatPrice(displayKline.low) }}</span>
                <span>高 {{ formatPrice(displayKline.high) }}</span>
                <span>量 {{ formatVolume(displayKline.volume) }}</span>
                <span>换 {{ formatTurnover(displayKline.turnoverRate) }}</span>
              </div>
            </div>
          </div>
          <div v-else class="title-line">
            <h2>{{ hoverTitle }}</h2>
          </div>
          <p>{{ chartSubtitle }}</p>
        </div>
        <div class="chart-switches">
          <el-segmented v-model="analysisMode" :options="analysisModeOptions" />
        <el-segmented
          v-if="analysisMode === 'intraday'"
          v-model="intradayPeriod"
          :options="intradayPeriodOptions"
          :disabled="intradayLoading"
        />
          <el-segmented v-if="analysisMode === 'daily'" v-model="displayMode" :options="modeOptions" />
        </div>
      </div>

      <div
        v-loading="loading || intradayLoading"
        ref="chartFrame"
        class="chart-frame"
        @mouseleave="resetHoverKline"
        @pointerleave="resetHoverKline"
      >
        <canvas
          :key="chartKey"
          ref="chartCanvas"
          @mouseleave="resetHoverKline"
          @pointerleave="resetHoverKline"
        ></canvas>
        <el-empty v-if="!result && !loading" :description="emptyText" />
        <el-empty v-else-if="result && !currentBars.length && !loading && !intradayLoading" description="暂无该周期短线数据" />
      </div>

      <div v-if="result" class="range-panel">
        <div class="range-meta">
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
          @input="updateChartWindow"
        />
      </div>
    </section>

    <section class="signals-section">
      <div class="section-title">
        <h3>{{ signalSectionTitle }}</h3>
        <el-tag v-if="latestSignal" :type="latestSignal.direction === 'buy' ? 'success' : 'danger'">
          最新：{{ signalName(latestSignal.type) }}
        </el-tag>
      </div>

      <el-table
        v-if="displaySignals.length"
        :data="displaySignals"
        height="548"
        class="signal-table"
        highlight-current-row
        @row-click="focusSignalRow"
      >
        <el-table-column prop="date" label="买卖日期" width="110" />
        <el-table-column label="类型" width="130">
          <template #default="{ row }">
            <div class="type-tags">
              <el-tag
                v-for="type in row.types || [row.type]"
                :key="type"
                :type="signalTagType(row)"
                effect="light"
              >
                {{ signalName(type) }}
              </el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="price" label="价格" width="90" />
        <el-table-column prop="confidence" label="置信度" width="90" />
        <el-table-column label="回测收益" width="150">
          <template #default="{ row }">
            <div class="return-stack">
              <span :class="returnClass(row.backtestSegmentReturn)">
                段 {{ formatReturn(row.backtestSegmentReturn ?? 0) }}
              </span>
              <span :class="returnClass(row.backtestTotalReturn)">
                累 {{ formatReturn(row.backtestTotalReturn ?? 0) }}
              </span>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="属性" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.future" type="warning" effect="plain">锚点</el-tag>
            <el-tag v-else type="info" effect="plain">已出现</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="依据" min-width="230">
          <template #default="{ row }">
            <div class="reason-cell" @click.stop>
              <span class="reason-preview">{{ row.reason }}</span>
              <el-popover
                :visible="Boolean(reasonPopoverVisible[reasonRowKey(row)])"
                placement="left"
                :width="380"
                trigger="click"
                popper-class="reason-popover"
                @update:visible="setReasonPopover(row, $event)"
              >
                <template #reference>
                  <el-button
                    class="reason-expand"
                    text
                    type="primary"
                    size="small"
                    @click.stop
                  >
                    展开
                  </el-button>
                </template>
                <div class="reason-popover-content" @click.stop>
                  <div class="reason-popover-title">完整依据</div>
                  <p>{{ row.reason }}</p>
                  <el-button size="small" type="primary" plain @click="closeReasonPopover(row)">
                    关闭
                  </el-button>
                </div>
              </el-popover>
            </div>
          </template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="当前区间未识别出买卖点或未来锚点" />
    </section>
  </section>
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

Chart.register(
  BarController,
  BarElement,
  CategoryScale,
  LinearScale,
  LineController,
  LineElement,
  PointElement,
  CandlestickController,
  CandlestickElement,
  Tooltip,
  Legend,
)

const props = defineProps({
  result: {
    type: Object,
    default: null,
  },
  loading: {
    type: Boolean,
    default: false,
  },
  loadIntraday: {
    type: Function,
    default: null,
  },
  title: {
    type: String,
    default: '等待分析',
  },
  emptyText: {
    type: String,
    default: '等待分析结果',
  },
})

const displayMode = ref('raw')
const analysisMode = ref('daily')
const intradayPeriod = ref('15')
const chartCanvas = ref(null)
const chartFrame = ref(null)
const chartKey = ref(0)
const visibleRange = ref([0, 0])
const chartDataCache = ref(null)
const reasonPopoverVisible = ref({})
const hoverKline = ref(null)
const intradayLoading = ref(false)
let chartInstance = null

const modeOptions = [
  { label: '原始 K 线', value: 'raw' },
  { label: '合并 K 线', value: 'merged' },
]
const analysisModeOptions = [
  { label: '日线', value: 'daily' },
  { label: '日内', value: 'intraday' },
]
const intradayPeriodOptions = [
  { label: '30分钟', value: '30' },
  { label: '15分钟', value: '15' },
  { label: '5分钟', value: '5' },
]
const intradayPeriodValues = intradayPeriodOptions.map((item) => item.value)

const summary = computed(() => props.result?.summary)
const intradayPeriods = computed(() => props.result?.intraday?.periods || {})
const intradayData = computed(() => props.result?.intraday?.periods?.[intradayPeriod.value] || null)
const intradaySignalRows = computed(() =>
  [
    ...(props.result?.signalRows || props.result?.signals || []).map((signal) => ({ ...signal, period: '日' })),
    ...Object.values(intradayPeriods.value)
      .flatMap((item) => item?.signalRows || item?.signals || [])
      .map((signal) => ({ ...signal, period: signal.period || signal.periodName })),
  ],
)
const activeSummary = computed(() => (analysisMode.value === 'intraday' ? props.result?.intraday?.summary : summary.value))
const signals = computed(() => (analysisMode.value === 'intraday' ? intradaySignalRows.value : props.result?.signals || []))
const signalRows = computed(() =>
  analysisMode.value === 'intraday'
    ? intradaySignalRows.value
    : props.result?.signalRows || props.result?.signals || [],
)
const futureSignals = computed(() => (analysisMode.value === 'intraday' ? [] : props.result?.futureSignals || []))
const centers = computed(() => (analysisMode.value === 'intraday' ? [] : props.result?.centers || []))
const sortedSignals = computed(() =>
  [...signalRows.value].sort((a, b) => {
    if (b.date !== a.date) return b.date.localeCompare(a.date)
    return (b.index ?? 0) - (a.index ?? 0)
  }),
)
const futureRows = computed(() => mergeFutureRows(futureSignals.value))
const displaySignals = computed(() =>
  [...futureRows.value, ...sortedSignals.value].sort((a, b) => {
    if (Number(b.future || false) !== Number(a.future || false)) {
      return Number(b.future || false) - Number(a.future || false)
    }
    if (b.date !== a.date) return b.date.localeCompare(a.date)
    return (b.index ?? 0) - (a.index ?? 0)
  }),
)
const latestSignal = computed(() => {
  if (analysisMode.value !== 'intraday') return activeSummary.value?.latestSignal
  return [...intradaySignalRows.value].sort((a, b) => String(b.date).localeCompare(String(a.date)))[0] || null
})
const signalSectionTitle = computed(() => (analysisMode.value === 'intraday' ? '日内短线买卖点' : '买卖点与未来锚点'))
const currentBars = computed(() => {
  if (!props.result) return []
  if (analysisMode.value === 'intraday') return intradayData.value?.rawKlines || []
  return displayMode.value === 'raw' ? props.result.rawKlines : props.result.mergedKlines
})
const rangeMax = computed(() => Math.max((currentBars.value.length || 1) - 1, 0))
const displayKline = computed(() => hoverKline.value || currentBars.value.at(-1) || null)
const resolvedPctChange = computed(() => {
  const kline = displayKline.value
  if (!kline) return null
  if (kline.pctChange !== null && kline.pctChange !== undefined && kline.pctChange !== '') {
    const direct = Number(kline.pctChange)
    if (Number.isFinite(direct)) return direct
  }
  const open = Number(kline.open)
  const close = Number(kline.close)
  if (!Number.isFinite(open) || !Number.isFinite(close) || open === 0) return null
  return ((close - open) / open) * 100
})
const hoverTitle = computed(() => props.result?.name ? `${props.result.symbol || ''} - ${props.result.name}` : props.title)
const chartSubtitle = computed(() => {
  if (!props.result) return '后端完成全部计算，前端仅渲染分析结果'
  if (analysisMode.value === 'intraday') {
    if (intradayLoading.value) return `${intradayPeriod.value} 分钟短线数据加载中`
    const range = intradayData.value?.dateRange
    if (!range?.start) return '暂无该周期短线数据'
    return `${intradayPeriod.value} 分钟 ${range.start} - ${range.end}`
  }
  return `${props.result.dateRange.start} - ${props.result.dateRange.end}`
})
const visibleRangeText = computed(() => {
  const bars = currentBars.value
  if (!bars.length) return ''
  const [start, end] = normalizeRange(visibleRange.value, bars.length)
  const startDate = bars[start]?.date || bars[start]?.endDate
  const endDate = bars[end]?.date || bars[end]?.endDate
  return `${startDate} 至 ${endDate}`
})

const signalName = (type) => {
  const names = {
    first_buy: '一买',
    first_sell: '一卖',
    second_buy: '二买',
    second_sell: '二卖',
    third_buy: '三买',
    third_sell: '三卖',
    future_third_buy: '潜在三买',
    future_third_sell: '潜在三卖',
    future_center_buy: '中枢低吸',
    future_center_sell: '中枢减仓',
    future_reclaim_buy: '站回转强',
    future_stop_loss: '跌破风控',
    intraday_rebound_buy: '\u65e5\u5185\u53cd\u5f39\u4e70',
    intraday_trend_buy: '\u65e5\u5185\u8d8b\u52bf\u4e70',
    intraday_pullback_sell: '\u65e5\u5185\u56de\u843d\u5356',
    intraday_trend_sell: '\u65e5\u5185\u8d8b\u52bf\u5356',
  }
  return names[type] || type
}

const signalPeriodText = (signal) => {
  if (!signal?.period) return ''
  return signal.period === '日' ? '日线 ' : `${signal.period}分钟 `
}

const mergeFutureRows = (items) => {
  const rows = new Map()
  items.forEach((signal) => {
    const key = `${signal.date}|${signal.direction}|${Number(signal.price).toFixed(3)}`
    if (!rows.has(key)) {
      rows.set(key, {
        ...signal,
        types: [],
        reasonParts: [],
        backtestSegmentReturn: 0,
        backtestTotalReturn: 0,
      })
    }
    const row = rows.get(key)
    row.types.push(signal.type)
    row.confidence = Math.max(Number(row.confidence || 0), Number(signal.confidence || 0))
    if (!row.reasonParts.includes(signal.reason)) row.reasonParts.push(signal.reason)
  })
  return [...rows.values()].map((row) => ({
    ...row,
    type: row.types.join('+'),
    reason: row.reasonParts.join('；'),
    reasonParts: undefined,
  }))
}

const signalTagType = (signal) => {
  if (signal.future) return 'warning'
  return signal.direction === 'buy' ? 'success' : 'danger'
}

const formatReturn = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '--'
  const number = Number(value)
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`
}

const returnClass = (value) => {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return 'return-neutral'
  return Number(value) >= 0 ? 'return-positive' : 'return-negative'
}

const reasonRowKey = (row) => `${row.date}-${row.direction}-${row.type}-${row.price}-${row.future ? 'future' : 'history'}`

const setReasonPopover = (row, visible) => {
  const key = reasonRowKey(row)
  reasonPopoverVisible.value = visible ? { [key]: true } : { ...reasonPopoverVisible.value, [key]: false }
}

const closeReasonPopover = (row) => {
  setReasonPopover(row, false)
}

const formatVolume = (value) => {
  const number = Number(value || 0)
  if (Math.abs(number) >= 100000000) return `${(number / 100000000).toFixed(2)}亿`
  if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(1)}万`
  return number.toFixed(0)
}

const formatPrice = (value) => {
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return number.toFixed(2)
}

const formatPercent = (value) => {
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`
}

const formatTurnover = (value) => {
  if (value === null || value === undefined || value === '') return '--'
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return `${number.toFixed(2)}%`
}

const changeClass = (value) => {
  const number = Number(value)
  if (!Number.isFinite(number) || number === 0) return 'change-neutral'
  return number > 0 ? 'change-positive' : 'change-negative'
}

const setHoverKlineByX = (xValue) => {
  const bars = chartDataCache.value?.bars || []
  if (!bars.length) {
    hoverKline.value = null
    return
  }
  const index = Math.max(0, Math.min(Math.round(Number(xValue)), bars.length - 1))
  hoverKline.value = bars[index] || null
}

const resetHoverKline = () => {
  hoverKline.value = null
}

const handlePointerMove = (event) => {
  if (!hoverKline.value || !chartFrame.value) return
  const rect = chartFrame.value.getBoundingClientRect()
  const { clientX, clientY } = event
  const inside = clientX >= rect.left && clientX <= rect.right && clientY >= rect.top && clientY <= rect.bottom
  if (!inside) {
    hoverKline.value = null
  }
}

const centerPlugin = {
  id: 'centerZone',
  beforeDatasetsDraw(chart) {
    const boxes = chart.$centerBoxes || []
    const { ctx, chartArea, scales } = chart
    if (!boxes.length || !chartArea) return
    ctx.save()
    boxes.forEach((box) => {
      const left = Math.max(chartArea.left, scales.x.getPixelForValue(box.xMin - 0.5))
      const right = Math.min(chartArea.right, scales.x.getPixelForValue(box.xMax + 0.5))
      const top = scales.y.getPixelForValue(box.yMax)
      const bottom = scales.y.getPixelForValue(box.yMin)
      if (right <= chartArea.left || left >= chartArea.right) return
      ctx.fillStyle = 'rgba(245, 158, 11, 0.13)'
      ctx.strokeStyle = 'rgba(245, 158, 11, 0.62)'
      ctx.lineWidth = 1
      ctx.fillRect(left, top, Math.max(right - left, 2), Math.max(bottom - top, 2))
      ctx.strokeRect(left, top, Math.max(right - left, 2), Math.max(bottom - top, 2))
    })
    ctx.restore()
  },
}

const renderChart = () => {
  if (!chartCanvas.value || !props.result) return
  const bars = currentBars.value
  if (!bars.length) {
    chartDataCache.value = null
    chartInstance?.destroy()
    chartInstance = null
    chartKey.value += 1
    return
  }
  const labels = bars.map((item) => item.date || item.endDate)
  const displayIndexByDate = new Map(labels.map((label, index) => [label, index]))
  const displayIndexByRawIndex = new Map()
  bars.forEach((item, index) => {
    if (displayMode.value === 'raw') {
      displayIndexByRawIndex.set(index, index)
    } else if (analysisMode.value === 'intraday') {
      displayIndexByRawIndex.set(index, index)
    } else {
      for (let rawIndex = item.startIndex; rawIndex <= item.endIndex; rawIndex += 1) {
        displayIndexByRawIndex.set(rawIndex, index)
      }
    }
  })
  const nearestDisplayIndexByDate = (date) => {
    if (!date || !labels.length) return null
    const exact = displayIndexByDate.get(date)
    if (exact != null) return exact
    const target = Date.parse(String(date).replace(' ', 'T'))
    if (!Number.isFinite(target)) return null
    let bestIndex = null
    let bestDistance = Number.POSITIVE_INFINITY
    labels.forEach((label, index) => {
      const value = Date.parse(String(label).replace(' ', 'T'))
      if (!Number.isFinite(value)) return
      const distance = Math.abs(value - target)
      if (distance < bestDistance) {
        bestDistance = distance
        bestIndex = index
      }
    })
    return bestIndex
  }
  const xOfRawIndex = (rawIndex, date, period) => {
    if (analysisMode.value === 'intraday' && period && period !== intradayPeriod.value) {
      return nearestDisplayIndexByDate(date)
    }
    return displayIndexByRawIndex.get(rawIndex) ?? nearestDisplayIndexByDate(date)
  }

  const candles = bars.map((item, index) => ({
    x: index,
    o: item.open,
    h: item.high,
    l: item.low,
    c: item.close,
  }))
  const volumes = bars.map((item, index) => ({
    x: index,
    y: Number(item.volume || 0),
    direction: Number(item.close) >= Number(item.open) ? 'up' : 'down',
  }))
  const candleByX = new Map(candles.map((candle) => [candle.x, candle]))
  const visualPriceForSignal = (signal, x) => {
    const candle = candleByX.get(x)
    if (!candle) return signal.price
    const spread = Math.max(candle.h - candle.l, Math.abs(candle.c) * 0.01, 0.04)
    return signal.direction === 'buy' ? candle.l - spread * 0.32 : candle.h + spread * 0.32
  }
  const strokePoints = []
  const activeStrokes = analysisMode.value === 'intraday' ? [] : props.result.strokes || []
  activeStrokes.forEach((stroke, index) => {
    const startX = xOfRawIndex(stroke.startIndex, stroke.startDate)
    const endX = xOfRawIndex(stroke.endIndex, stroke.endDate)
    if (startX == null || endX == null) return
    if (index > 0) strokePoints.push({ x: startX, y: stroke.startPrice })
    strokePoints.push({ x: endX, y: stroke.endPrice })
  })
  const signalPoints = signals.value
    .map((signal) => {
      const x = xOfRawIndex(signal.index, signal.date, signal.period)
      return {
        x,
        y: x == null ? signal.price : visualPriceForSignal(signal, x),
        signal,
      }
    })
    .filter((point) => point.x != null)
  const futureSignalPoints = futureSignals.value
    .map((signal) => ({
      x: xOfRawIndex(signal.index, signal.date) ?? bars.length - 1,
      y: signal.price,
      signal,
    }))
    .filter((point) => point.x != null)
  const centerBoxes = centers.value
    .map((center) => ({
      xMin: xOfRawIndex(center.startIndex, center.startDate),
      xMax: xOfRawIndex(center.endIndex, center.endDate),
      yMin: center.low,
      yMax: center.high,
      center,
    }))
    .filter((box) => box.xMin != null && box.xMax != null)

  chartDataCache.value = { bars, labels, candles, volumes, strokePoints, signalPoints, futureSignalPoints, centerBoxes }
  chartInstance?.destroy()
  chartKey.value += 1
  nextTick(() => {
    if (!chartCanvas.value) return
    drawChart(chartDataCache.value)
  })
}

const drawChart = ({ bars, labels, candles, volumes, strokePoints, signalPoints, futureSignalPoints, centerBoxes }) => {
  const [rangeStart, rangeEnd] = normalizeRange(visibleRange.value, bars.length)
  const yBounds = getVisibleYBounds(candles, strokePoints, signalPoints, futureSignalPoints, centerBoxes, rangeStart, rangeEnd)
  chartInstance?.destroy()
  chartInstance = new Chart(chartCanvas.value, {
    data: {
      datasets: [
        {
          type: 'candlestick',
          label: analysisMode.value === 'intraday' ? `${intradayPeriod.value}分钟K` : displayMode.value === 'raw' ? '日 K' : '合并 K',
          kind: 'candle',
          data: candles,
          parsing: false,
          normalized: true,
          borderColor: '#566070',
          borderWidth: 1.4,
          barPercentage: 0.92,
          categoryPercentage: 0.92,
          borderColors: { up: '#d84a4a', down: '#1c9b63', unchanged: '#8a94a6' },
          backgroundColors: { up: '#d84a4a', down: '#1c9b63', unchanged: '#8a94a6' },
        },
        {
          type: 'bar',
          label: '成交量',
          kind: 'volume',
          data: volumes,
          yAxisID: 'volume',
          backgroundColor: (ctx) => (ctx.raw?.direction === 'up' ? 'rgba(216, 74, 74, 0.28)' : 'rgba(28, 155, 99, 0.28)'),
          borderColor: (ctx) => (ctx.raw?.direction === 'up' ? 'rgba(216, 74, 74, 0.48)' : 'rgba(28, 155, 99, 0.48)'),
          borderWidth: 1,
          barPercentage: 0.86,
          categoryPercentage: 0.86,
          parsing: false,
          order: 5,
        },
        {
          type: 'line',
          label: '笔线',
          kind: 'stroke',
          data: strokePoints,
          borderColor: '#1f6feb',
          borderWidth: 3,
          pointRadius: 4,
          pointHoverRadius: 6,
          pointBackgroundColor: '#1f6feb',
          parsing: false,
          spanGaps: true,
        },
        {
          type: 'line',
          label: '买卖点',
          kind: 'signal',
          data: signalPoints,
          showLine: false,
          pointRadius: 8,
          pointHoverRadius: 10,
          pointStyle: 'triangle',
          pointRotation: (ctx) => (ctx.raw?.signal?.direction === 'sell' ? 180 : 0),
          pointBackgroundColor: (ctx) => (ctx.raw?.signal?.direction === 'buy' ? '#2563eb' : '#7c3aed'),
          pointBorderColor: (ctx) => (ctx.raw?.signal?.direction === 'buy' ? '#dbeafe' : '#ede9fe'),
          pointBorderWidth: 2.5,
          parsing: false,
        },
        {
          type: 'line',
          label: '未来锚点',
          kind: 'futureSignal',
          data: futureSignalPoints,
          showLine: false,
          pointRadius: 9,
          pointHoverRadius: 11,
          pointStyle: 'rectRot',
          pointBackgroundColor: (ctx) => (ctx.raw?.signal?.direction === 'buy' ? '#f59e0b' : '#9333ea'),
          pointBorderColor: '#ffffff',
          pointBorderWidth: 2,
          parsing: false,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      events: ['mousemove', 'mouseout', 'click', 'touchstart', 'touchmove'],
      interaction: { intersect: false, mode: 'index' },
      onHover: (event, _elements, chart) => {
        const eventType = event?.type ?? event?.native?.type
        if (eventType === 'mouseout') {
          hoverKline.value = null
          return
        }
        const x = event?.x ?? event?.native?.offsetX
        const y = event?.y ?? event?.native?.offsetY
        if (!chart?.chartArea || x == null || y == null) {
          hoverKline.value = null
          return
        }
        if (x < chart.chartArea.left || x > chart.chartArea.right || y < chart.chartArea.top || y > chart.chartArea.bottom) {
          hoverKline.value = null
          return
        }
        setHoverKlineByX(chart.scales.x.getValueForPixel(x))
      },
      plugins: {
        legend: { position: 'top', labels: { usePointStyle: true, boxWidth: 8 } },
        tooltip: {
          callbacks: {
            title: (items) => {
              const x = Math.round(items[0]?.parsed?.x ?? items[0]?.raw?.x ?? 0)
              return labels[x] || ''
            },
            label: (context) => {
              if (context.dataset.kind === 'signal') {
                const signal = context.raw.signal
                return `${signalPeriodText(signal)}${signalName(signal.type)} ${signal.price} ${signal.date}：${signal.reason}`
              }
              if (context.dataset.kind === 'futureSignal') {
                const signal = context.raw.signal
                return `${signalName(signal.type)} ${signal.price}：${signal.reason}`
              }
              if (context.dataset.kind === 'candle') {
                const x = Math.round(context.parsed?.x ?? context.raw?.x ?? 0)
                const candle = candles[x]
                const bar = bars[x]
                if (!candle) return ''
                return `开 ${formatPrice(candle.o)} 高 ${formatPrice(candle.h)} 低 ${formatPrice(candle.l)} 收 ${formatPrice(candle.c)} 量 ${formatVolume(bar?.volume)}`
              }
              if (context.dataset.kind === 'volume') {
                return `成交量 ${formatVolume(context.raw?.y)}`
              }
              return `${context.dataset.label}: ${context.parsed.y}`
            },
          },
        },
      },
      scales: {
        x: {
          type: 'linear',
          min: -1,
          max: bars.length,
          offset: true,
          ticks: {
            maxTicksLimit: 8,
            color: '#637083',
            callback: (value) => labels[Math.round(value)] || '',
          },
          grid: { display: false },
        },
        y: {
          position: 'right',
          min: yBounds.min,
          max: yBounds.max,
          ticks: { color: '#637083' },
          grid: { color: '#e8edf4' },
        },
        volume: {
          position: 'left',
          min: 0,
          max: getVisibleVolumeMax(volumes, rangeStart, rangeEnd),
          display: true,
          ticks: {
            color: '#8a94a6',
            maxTicksLimit: 3,
            callback: (value) => formatVolume(value),
          },
          grid: { display: false },
          afterFit: (axis) => {
            axis.width = 58
          },
        },
      },
    },
    plugins: [centerPlugin],
  })
  chartInstance.$centerBoxes = centerBoxes
  updateChartWindow()
}

const normalizeRange = (range, length) => {
  if (!length) return [0, 0]
  const start = Math.max(0, Math.min(Math.round(range?.[0] ?? 0), length - 1))
  const end = Math.max(start, Math.min(Math.round(range?.[1] ?? length - 1), length - 1))
  return [start, end]
}

const resetVisibleRange = () => {
  const count = currentBars.value.length
  if (!count) {
    visibleRange.value = [0, 0]
    return
  }
  const windowSize = Math.min(count, analysisMode.value === 'intraday' ? 80 : displayMode.value === 'raw' ? 90 : 70)
  visibleRange.value = [Math.max(0, count - windowSize), count - 1]
}

const focusSignalRow = (row) => {
  if (displayMode.value !== 'raw') {
    if (analysisMode.value === 'intraday') {
      focusSignalRange(row)
      return
    }
    displayMode.value = 'raw'
    nextTick(() => focusSignalRange(row))
    return
  }
  focusSignalRange(row)
}

const focusSignalRange = (row) => {
  const bars = currentBars.value
  if (!row || !bars.length) return
  const targetIndex = findSignalBarIndex(row, bars)
  if (targetIndex == null) return
  const windowSize = Math.min(34, bars.length)
  const halfWindow = Math.floor(windowSize / 2)
  let start = Math.max(0, targetIndex - halfWindow)
  let end = Math.min(bars.length - 1, start + windowSize - 1)
  start = Math.max(0, end - windowSize + 1)
  visibleRange.value = [start, end]
  nextTick(updateChartWindow)
}

const findSignalBarIndex = (row, bars) => {
  if (analysisMode.value === 'intraday' && row.period && row.period !== intradayPeriod.value) {
    return findNearestBarIndexByDate(row.date, bars)
  }
  if (Number.isInteger(row.index) && row.index >= 0 && row.index < bars.length) {
    return row.index
  }
  const date = row.date
  const exact = bars.findIndex((bar) => bar.date === date)
  if (exact >= 0) return exact
  return findNearestBarIndexByDate(date, bars)
}

const findNearestBarIndexByDate = (date, bars) => {
  const target = Date.parse(String(date || '').replace(' ', 'T'))
  if (!Number.isFinite(target)) return null
  let bestIndex = null
  let bestDistance = Number.POSITIVE_INFINITY
  bars.forEach((bar, index) => {
    const value = Date.parse(String(bar.date || bar.endDate || '').replace(' ', 'T'))
    if (!Number.isFinite(value)) return
    const distance = Math.abs(value - target)
    if (distance < bestDistance) {
      bestDistance = distance
      bestIndex = index
    }
  })
  return bestIndex
}

const getVisibleYBounds = (candles, strokePoints, signalPoints, futureSignalPoints, centerBoxes, start, end) => {
  const values = []
  candles.forEach((candle) => {
    if (candle.x >= start && candle.x <= end) values.push(candle.h, candle.l, candle.o, candle.c)
  })
  strokePoints.forEach((point) => {
    if (point.x >= start && point.x <= end) values.push(point.y)
  })
  signalPoints.forEach((point) => {
    if (point.x >= start && point.x <= end) values.push(point.y)
  })
  futureSignalPoints.forEach((point) => {
    if (point.x >= start && point.x <= end) values.push(point.y)
  })
  centerBoxes.forEach((box) => {
    if (box.xMax >= start && box.xMin <= end) values.push(box.yMin, box.yMax)
  })

  if (!values.length) return { min: 0, max: 1 }
  const min = Math.min(...values)
  const max = Math.max(...values)
  const padding = Math.max((max - min) * 0.12, Math.abs(max) * 0.01, 0.05)
  return {
    min: Number((min - padding).toFixed(3)),
    max: Number((max + padding).toFixed(3)),
  }
}

const getVisibleVolumeMax = (volumes, start, end) => {
  const visible = volumes
    .filter((item) => item.x >= start && item.x <= end)
    .map((item) => Number(item.y || 0))
  const max = visible.length ? Math.max(...visible) : 0
  return Math.max(max * 4.2, 1)
}

const updateChartWindow = () => {
  if (!chartInstance || !chartDataCache.value?.bars?.length) return
  const { bars, candles, volumes, strokePoints, signalPoints, futureSignalPoints, centerBoxes } = chartDataCache.value
  const [start, end] = normalizeRange(visibleRange.value, bars.length)
  const yBounds = getVisibleYBounds(candles, strokePoints, signalPoints, futureSignalPoints, centerBoxes, start, end)
  chartInstance.options.scales.x.min = start - 0.7
  chartInstance.options.scales.x.max = end + 0.7
  chartInstance.options.scales.y.min = yBounds.min
  chartInstance.options.scales.y.max = yBounds.max
  chartInstance.options.scales.volume.max = getVisibleVolumeMax(volumes, start, end)
  chartInstance.update('none')
}

watch(
  () => props.result,
  async () => {
    if (!props.result) return
    resetVisibleRange()
    hoverKline.value = null
    await nextTick()
    renderChart()
  },
)

watch(displayMode, () => {
  resetVisibleRange()
  hoverKline.value = null
  nextTick(renderChart)
})

watch([analysisMode, intradayPeriod], () => {
  resetVisibleRange()
  hoverKline.value = null
  ensureIntradayData()
  nextTick(renderChart)
})

const ensureIntradayData = async () => {
  if (analysisMode.value !== 'intraday' || !props.result || !props.loadIntraday) return
  const periods = props.result?.intraday?.periods || {}
  const hasAllPeriods = intradayPeriodValues.every((period) => periods[period]?.rawKlines?.length)
  if (hasAllPeriods) return
  intradayLoading.value = true
  try {
    await props.loadIntraday('')
  } finally {
    intradayLoading.value = false
    resetVisibleRange()
    await nextTick()
    renderChart()
  }
}

onBeforeUnmount(() => {
  window.removeEventListener('pointermove', handlePointerMove)
  chartInstance?.destroy()
})

onMounted(() => {
  window.addEventListener('pointermove', handlePointerMove, { passive: true })
})
</script>
