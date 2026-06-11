<template>
  <main class="page-shell">
    <header class="app-header">
      <div class="brand-block">
        <div class="brand-mark">
          <img src="/chanlun-mark.svg" alt="缠论分析" />
        </div>
        <div>
          <h1>缠论分析</h1>
          <p>上证指数自动分析，个股独立交互分析</p>
        </div>
      </div>
    </header>

    <section class="analysis-stack">
      <AnalysisPanel
        title="上证指数"
        :result="indexResult"
        :loading="indexLoading"
        :load-intraday="loadIndexIntraday"
        empty-text="正在自动分析上证指数"
      >
        <template #info>
          <PanelInfo
            title="上证指数"
            heading-value="000001 - 上证指数"
            :summary="indexSummary"
            :result="indexResult"
            :loading="indexLoading"
          />
        </template>
      </AnalysisPanel>

      <AnalysisPanel
        :title="selectedLabel || '个股分析'"
        :result="stockResult"
        :loading="stockLoading"
        :load-intraday="loadStockIntraday"
        empty-text="输入股票后查看个股缠论图表"
      >
        <template #info>
          <section class="side-panel">
            <div class="side-panel-title">
              <span>个股分析</span>
              <strong>{{ selectedLabel || '等待输入' }}</strong>
            </div>

            <el-form label-position="top" class="stock-form">
              <el-form-item label="股票代码或名称">
                <el-autocomplete
                  v-model="keyword"
                  :fetch-suggestions="querySearch"
                  value-key="label"
                  clearable
                  :debounce="0"
                  :disabled="stocksLoading"
                  placeholder="例如 000001 或 平安银行"
                  @select="handleSelect"
                >
                  <template #default="{ item }">
                    <div class="suggestion-row">
                      <span>{{ item.symbol }}</span>
                      <strong>{{ item.name }}</strong>
                    </div>
                  </template>
                </el-autocomplete>
              </el-form-item>

              <el-button
                class="analyze-btn"
                type="primary"
                size="large"
                :icon="Activity"
                :loading="stockLoading"
                @click="runAnalyze"
              >
                分析个股
              </el-button>

              <el-button
                class="find-btn"
                type="success"
                size="large"
                :icon="Search"
                :loading="findLoading"
                @click="runFindGoodStock"
              >
                寻找好标
              </el-button>

              <el-form-item label="寻找股价区间" class="find-price-field">
                <div class="price-range-row">
                  <el-input-number
                    v-model="findMinPrice"
                    :min="0"
                    :max="300"
                    :step="1"
                    :precision="2"
                    controls-position="right"
                    placeholder="最低"
                  />
                  <span class="price-range-separator">至</span>
                  <el-input-number
                    v-model="findMaxPrice"
                    :min="1"
                    :max="300"
                    :step="1"
                    :precision="2"
                    controls-position="right"
                    placeholder="最高"
                  />
                </div>
              </el-form-item>
            </el-form>

            <PanelInfo title="" :summary="stockSummary" :result="stockResult" :loading="stockLoading" compact />
          </section>
        </template>
      </AnalysisPanel>
    </section>
  </main>
</template>

<script setup>
import { computed, defineComponent, h, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { Activity, Search } from 'lucide-vue-next'
import AnalysisPanel from './components/AnalysisPanel.vue'
import {
  analyzeIndex,
  analyzeIndexIntraday,
  analyzeStock,
  analyzeStockIntraday,
  fetchStocks,
  findGoodStock,
  searchStocks,
} from './api'

const keyword = ref('')
const selectedStock = ref(null)
const selectedLabel = ref('')
const stockLoading = ref(false)
const findLoading = ref(false)
const indexLoading = ref(false)
const stocksLoading = ref(false)
const findMinPrice = ref(0)
const findMaxPrice = ref(30)
const stockOptions = ref([])
const stockResult = ref(null)
const indexResult = ref(null)

const stockSummary = computed(() => stockResult.value?.summary)
const indexSummary = computed(() => indexResult.value?.summary)
const indexSummaryText = computed(() => {
  const summary = indexResult.value?.summary
  if (!summary) return '等待自动分析'
  return `${summary.latestClose} / ${summary.trend} / ${summary.futureSignalCount} 个锚点`
})

const PanelInfo = defineComponent({
  name: 'PanelInfo',
  props: {
    title: { type: String, default: '' },
    headingValue: { type: String, default: '' },
    summary: { type: Object, default: null },
    result: { type: Object, default: null },
    loading: { type: Boolean, default: false },
    compact: { type: Boolean, default: false },
  },
  setup(props) {
    const valueText = (value) => (value === null || value === undefined || value === '' ? '--' : value)
    const centerText = () => {
      const center = props.summary?.latestCenter
      return center ? `${center.low} - ${center.high}` : '--'
    }
    return () =>
      h(
        'section',
        { class: ['side-panel', props.compact ? 'side-panel-compact' : ''], 'data-loading': props.loading },
        [
          props.title
            ? h('div', { class: 'side-panel-title' }, [
                h('span', props.title),
                h('strong', props.loading ? '分析中' : props.headingValue || valueText(props.summary?.trend)),
              ])
            : null,
          h('div', { class: 'info-grid' }, [
            h('div', [h('span', '最新价'), h('strong', valueText(props.summary?.latestClose))]),
            h('div', [h('span', '走势'), h('strong', valueText(props.summary?.trend))]),
            h('div', [h('span', '笔数'), h('strong', valueText(props.summary?.strokeCount))]),
            h('div', [h('span', '买卖点'), h('strong', valueText(props.summary?.signalCount))]),
            h('div', [h('span', '中枢数'), h('strong', valueText(props.summary?.centerCount))]),
            h('div', [h('span', '未来锚点'), h('strong', valueText(props.summary?.futureSignalCount))]),
          ]),
          h('div', { class: 'meta-list' }, [
            h('div', [
              h('span', '分析区间'),
              h('strong', props.result?.dateRange ? `${props.result.dateRange.start} 至 ${props.result.dateRange.end}` : '--'),
            ]),
            h('div', [
              h('span', '原始/合并 K 线'),
              h(
                'strong',
                props.summary ? `${valueText(props.summary.rawCount)} / ${valueText(props.summary.mergedCount)}` : '--',
              ),
            ]),
            h('div', [h('span', '最新中枢区间'), h('strong', centerText())]),
          ]),
        ],
      )
  },
})

const querySearch = (queryString, cb) => {
  const word = queryString.trim().toLowerCase()
  if (!word) {
    cb([])
    return
  }
  const matched = stockOptions.value
    .filter((item) => {
      const symbol = String(item.symbol || '').toLowerCase()
      const name = String(item.name || '').toLowerCase()
      const label = String(item.label || '').toLowerCase()
      return symbol.includes(word) || name.includes(word) || label.includes(word)
    })
    .slice(0, 20)

  if (matched.length || !/^\d{1,6}$/.test(word)) {
    cb(matched)
    return
  }

  cb([
    {
      symbol: word.padStart(6, '0'),
      name: '代码直查',
      label: `${word.padStart(6, '0')} - 代码直查`,
    },
  ])
}

const handleSelect = (item) => {
  selectedStock.value = item
  selectedLabel.value = item.label
  keyword.value = item.label
}

watch(keyword, (value) => {
  if (selectedStock.value && value !== selectedLabel.value) {
    selectedStock.value = null
  }
})

const runAnalyze = async () => {
  const symbol = selectedStock.value?.symbol || keyword.value.match(/\d{6}/)?.[0]
  if (!symbol) {
    ElMessage.warning('请输入或选择一个有效股票')
    return
  }

  stockLoading.value = true
  try {
    stockResult.value = await analyzeStock(symbol)
    selectedLabel.value = selectedStock.value?.label || symbol
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '分析失败')
  } finally {
    stockLoading.value = false
  }
}

const runFindGoodStock = async () => {
  const minPrice = Number(findMinPrice.value)
  const maxPrice = Number(findMaxPrice.value)
  if (!Number.isFinite(minPrice) || !Number.isFinite(maxPrice) || minPrice < 0 || maxPrice <= 0 || minPrice > maxPrice) {
    ElMessage.warning('请输入有效的股价区间')
    return
  }

  findLoading.value = true
  try {
    const result = await findGoodStock({ minPrice, maxPrice }, { maxAttempts: 50, threshold: 0.05 })
    stockResult.value = result
    const label = `${result.symbol} - ${result.name || '已命中标的'}`
    selectedStock.value = { symbol: result.symbol, name: result.name || '', label }
    selectedLabel.value = label
    keyword.value = label
    const attempts = result.findMeta?.attempts
    const distance = result.findMeta?.matchedSignal?.distancePct
    ElMessage.success(`找到 ${label}${attempts ? `，尝试 ${attempts} 次` : ''}${distance != null ? `，偏离 ${distance}%` : ''}`)
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '寻找好标失败')
  } finally {
    findLoading.value = false
  }
}

const loadIndex = async () => {
  indexLoading.value = true
  try {
    indexResult.value = await analyzeIndex()
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '上证指数分析失败')
  } finally {
    indexLoading.value = false
  }
}


const mergeIntradayResult = (target, intraday) => {
  if (!target || !intraday) return
  target.intraday = {
    periods: {
      ...(target.intraday?.periods || {}),
      ...(intraday.periods || {}),
    },
    summary: intraday.summary || target.intraday?.summary || {},
    errors: {
      ...(target.intraday?.errors || {}),
      ...(intraday.errors || {}),
    },
  }
}

const loadIndexIntraday = async (period) => {
  if (!indexResult.value) return
  try {
    const intraday = await analyzeIndexIntraday(period)
    mergeIntradayResult(indexResult.value, intraday)
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '指数日内分析失败')
  }
}

const loadStockIntraday = async (period) => {
  if (!stockResult.value?.symbol) return
  try {
    const intraday = await analyzeStockIntraday(stockResult.value.symbol, period)
    mergeIntradayResult(stockResult.value, intraday)
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '日内分析失败')
  }
}

const loadStocks = async () => {
  stocksLoading.value = true
  try {
    stockOptions.value = await fetchStocks()
  } catch (error) {
    try {
      stockOptions.value = await searchStocks('000')
    } catch {
      stockOptions.value = []
    }
    ElMessage.warning('股票候选列表加载失败，已保留代码直查')
  } finally {
    stocksLoading.value = false
  }
}

onMounted(() => {
  loadStocks()
  loadIndex()
})
</script>
