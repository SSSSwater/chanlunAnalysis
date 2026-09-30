<template>
  <div class="binance-position-pnl-chart">
    <div class="binance-position-pnl-chart-head">
      <span>盈亏走势</span>
      <strong>{{ rangeText }}</strong>
    </div>
    <div v-if="points.length" class="binance-position-pnl-chart-frame">
      <canvas ref="canvas" role="img" aria-label="持仓盈亏折线图"></canvas>
    </div>
    <small v-else class="binance-position-pnl-chart-empty">等待持仓盈亏记录</small>
    <el-slider
      v-if="points.length"
      v-model="visibleRange"
      range
      :min="0"
      :max="rangeMax"
      :step="1"
      :show-tooltip="false"
      :disabled="points.length < 2"
    />
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import {
  CategoryScale,
  Chart,
  LineController,
  LineElement,
  LinearScale,
  PointElement,
  Tooltip,
} from 'chart.js'

Chart.register(CategoryScale, LineController, LineElement, LinearScale, PointElement, Tooltip)

const props = defineProps({
  points: { type: Array, default: () => [] },
  smoothing: { type: Number, default: 0.25 },
})

const canvas = ref(null)
const visibleRange = ref([0, 0])
let chart = null
let rangeInitialized = false
let previousPointCount = 0

const pnlGradientFillPlugin = {
  id: 'binancePositionPnlGradientFill',
  beforeDatasetsDraw(currentChart) {
    const meta = currentChart.getDatasetMeta(0)
    const line = meta?.dataset
    const points = meta?.data || []
    const { chartArea, ctx, scales } = currentChart
    if (!line || points.length === 0 || !chartArea || !scales?.y) return

    const positiveColor = cssVar('--binance-rise', '#d85b68')
    const negativeColor = cssVar('--binance-fall', '#0f9f73')
    const zeroY = Math.max(chartArea.top, Math.min(chartArea.bottom, scales.y.getPixelForValue(0)))
    const positiveGradient = ctx.createLinearGradient(0, zeroY, 0, chartArea.top)
    positiveGradient.addColorStop(0, withAlpha(positiveColor, 0.03))
    positiveGradient.addColorStop(1, withAlpha(positiveColor, 0.24))
    const negativeGradient = ctx.createLinearGradient(0, zeroY, 0, chartArea.bottom)
    negativeGradient.addColorStop(0, withAlpha(negativeColor, 0.03))
    negativeGradient.addColorStop(1, withAlpha(negativeColor, 0.24))

    const firstPoint = points[0]
    const lastPoint = points[points.length - 1]
    const drawHalf = (top, bottom, fillStyle) => {
      if (bottom <= top) return
      ctx.save()
      ctx.beginPath()
      ctx.rect(chartArea.left, top, chartArea.right - chartArea.left, bottom - top)
      ctx.clip()
      ctx.beginPath()
      line.path(ctx)
      ctx.lineTo(lastPoint.x, zeroY)
      ctx.lineTo(firstPoint.x, zeroY)
      ctx.closePath()
      ctx.fillStyle = fillStyle
      ctx.fill()
      ctx.restore()
    }

    drawHalf(chartArea.top, zeroY, positiveGradient)
    drawHalf(zeroY, chartArea.bottom, negativeGradient)
  },
}

const normalizedPoints = computed(() => props.points
  .filter((point) => point && Number.isFinite(Number(point.recordedAt)) && Number.isFinite(Number(point.unrealizedPnl)))
  .map((point) => ({
    recordedAt: Number(point.recordedAt),
    unrealizedPnl: Number(point.unrealizedPnl),
  }))
  .sort((left, right) => left.recordedAt - right.recordedAt))
const points = computed(() => normalizedPoints.value)
const rangeMax = computed(() => Math.max(points.value.length - 1, 0))
const visiblePoints = computed(() => {
  if (!points.value.length) return []
  const start = Math.max(0, Math.min(visibleRange.value[0], rangeMax.value))
  const end = Math.max(start, Math.min(visibleRange.value[1], rangeMax.value))
  return points.value.slice(start, end + 1)
})
const formatTime = (timestamp) => new Date(timestamp).toLocaleString('zh-CN', {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
})
const formatPnl = (value) => {
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return `${number > 0 ? '+' : ''}${number.toFixed(2)} USDT`
}
const rangeText = computed(() => {
  const selected = visiblePoints.value
  if (!selected.length) return '--'
  if (selected.length === 1) return formatTime(selected[0].recordedAt)
  return `${formatTime(selected[0].recordedAt)} - ${formatTime(selected[selected.length - 1].recordedAt)}`
})

const cssVar = (name, fallback) => {
  const value = getComputedStyle(canvas.value || document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}
const withAlpha = (color, alpha) => {
  const value = String(color || '').trim()
  const hex = value.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i)
  if (hex) {
    const digits = hex[1].length === 3 ? hex[1].split('').map((item) => item + item).join('') : hex[1]
    const red = Number.parseInt(digits.slice(0, 2), 16)
    const green = Number.parseInt(digits.slice(2, 4), 16)
    const blue = Number.parseInt(digits.slice(4, 6), 16)
    return `rgba(${red}, ${green}, ${blue}, ${alpha})`
  }
  const rgb = value.match(/^rgba?\(([^)]+)\)$/i)
  if (rgb) {
    const channels = rgb[1].split(',').slice(0, 3).map((item) => item.trim()).join(', ')
    return `rgba(${channels}, ${alpha})`
  }
  return value
}
const smoothValues = (values, level) => {
  if (level <= 0 || values.length < 2) return values
  const alpha = Math.max(0.06, 1 - level * 0.92)
  return values.reduce((result, value, index) => {
    result.push(index === 0 ? value : result[index - 1] + (value - result[index - 1]) * alpha)
    return result
  }, [])
}
const resetRange = () => {
  const max = rangeMax.value
  if (!rangeInitialized || !previousPointCount) {
    visibleRange.value = [0, max]
    rangeInitialized = true
  } else {
    const start = Math.min(visibleRange.value[0], max)
    visibleRange.value = [start, max]
  }
  previousPointCount = points.value.length
}
const renderChart = async () => {
  await nextTick()
  if (!canvas.value || !visiblePoints.value.length) {
    chart?.destroy()
    chart = null
    return
  }
  const labels = visiblePoints.value.map((point) => formatTime(point.recordedAt))
  const originalValues = visiblePoints.value.map((point) => point.unrealizedPnl)
  const values = smoothValues(originalValues, props.smoothing)
  const positiveColor = cssVar('--binance-rise', '#d85b68')
  const negativeColor = cssVar('--binance-fall', '#0f9f73')
  chart?.destroy()
  chart = new Chart(canvas.value, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data: values,
        borderColor: positiveColor,
        backgroundColor: 'transparent',
        borderWidth: 2,
        pointRadius: 0,
        pointHoverRadius: 4,
        pointHitRadius: 8,
        tension: Math.min(props.smoothing * 0.35, 0.35),
        fill: false,
        segment: {
          borderColor: (context) => {
            const first = Number(context.p0?.parsed?.y)
            const second = Number(context.p1?.parsed?.y)
            return (first + second) / 2 >= 0 ? positiveColor : negativeColor
          },
        },
      }],
    },
    options: {
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: 'index' },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: (items) => items[0]?.label || '',
            label: (item) => formatPnl(originalValues[item.dataIndex]),
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          ticks: { maxTicksLimit: 4, color: cssVar('--text-faint', '#8795a3'), font: { size: 9 } },
        },
        y: {
          grid: { color: cssVar('--border-subtle', '#dbe3ea') },
          ticks: { maxTicksLimit: 4, color: cssVar('--text-faint', '#8795a3'), font: { size: 9 }, callback: (value) => formatPnl(value).replace(' USDT', '') },
        },
      },
    },
    plugins: [pnlGradientFillPlugin],
  })
}

watch(() => props.points, () => {
  resetRange()
  renderChart()
}, { deep: true })
watch(visibleRange, () => renderChart(), { deep: true })
watch(() => props.smoothing, () => renderChart())

onMounted(() => {
  resetRange()
  renderChart()
})
onBeforeUnmount(() => chart?.destroy())
</script>

<style scoped>
.binance-position-pnl-chart { display: grid; gap: 5px; margin: 1px 0 7px; padding: 7px 0 2px; border-top: 1px solid var(--border-subtle); }
.binance-position-pnl-chart-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; color: var(--text-muted); font-size: 10px; font-weight: 800; }
.binance-position-pnl-chart-head strong { min-width: 0; overflow: hidden; color: var(--text-faint); font-family: var(--font-pnl); font-size: 9px; font-weight: 700; text-overflow: ellipsis; white-space: nowrap; }
.binance-position-pnl-chart-frame { position: relative; height: 112px; min-height: 112px; }
.binance-position-pnl-chart-frame canvas { display: block; width: 100% !important; height: 100% !important; }
.binance-position-pnl-chart :deep(.el-slider) { width: calc(100% - 8px); margin: 0 4px; }
.binance-position-pnl-chart :deep(.el-slider__runway) { margin: 6px 0; }
.binance-position-pnl-chart :deep(.el-slider__button) { width: 12px; height: 12px; }
.binance-position-pnl-chart-empty { color: var(--text-faint); font-size: 10px; }
</style>
