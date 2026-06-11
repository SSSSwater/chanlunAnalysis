import axios from 'axios'

const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:5000',
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
