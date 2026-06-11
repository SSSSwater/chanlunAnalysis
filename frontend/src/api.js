import axios from 'axios'

const normalizeBaseUrl = (value) => {
  const text = String(value || '').trim()
  if (text) {
    if (/^https?:\/\//i.test(text)) return text.replace(/\/$/, '')
    return `https://${text.replace(/\/$/, '')}`
  }

  if (typeof window !== 'undefined') {
    const { protocol, hostname } = window.location
    if (hostname.endsWith('.onrender.com') && hostname.includes('-web')) {
      return `${protocol}//${hostname.replace('-web', '-api')}`
    }
  }

  return 'http://127.0.0.1:5000'
}

const api = axios.create({
  baseURL: normalizeBaseUrl(import.meta.env.VITE_API_BASE_URL),
  timeout: 240000,
})

export const searchStocks = async (keyword) => {
  const { data } = await api.get('/api/stocks/search', { params: { keyword } })
  return data.items || []
}

export const fetchStocks = async () => {
  const { data } = await api.get('/api/stocks')
  return data.items || []
}

export const analyzeStock = async (symbol) => {
  const { data } = await api.get('/api/analyze', { params: { symbol } })
  return data
}

export const analyzeStockIntraday = async (symbol, period) => {
  const { data } = await api.get('/api/intraday/analyze', { params: { symbol, period } })
  return data
}

export const findGoodStock = async ({ minPrice, maxPrice }, options = {}) => {
  const { data } = await api.get('/api/stocks/find-good', {
    params: {
      minPrice,
      maxPrice,
      maxAttempts: options.maxAttempts ?? 50,
      threshold: options.threshold ?? 0.05,
    },
  })
  return data
}

export const analyzeIndex = async () => {
  const { data } = await api.get('/api/index/analyze')
  return data
}

export const analyzeIndexIntraday = async (period) => {
  const { data } = await api.get('/api/index/intraday/analyze', { params: { period } })
  return data
}
