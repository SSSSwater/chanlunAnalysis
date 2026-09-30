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
    // Served through a tunnel/custom domain: call the backend via the same
    // origin under /api/* (routed locally by the tunnel to the Flask port).
    if (hostname && hostname !== '127.0.0.1' && hostname !== 'localhost') {
      return `${protocol}//${hostname}`
    }
  }

  return 'http://127.0.0.1:5000'
}

const api = axios.create({
  baseURL: normalizeBaseUrl(import.meta.env.VITE_API_BASE_URL),
  // Ordinary reads should fail fast enough for the UI to keep rendering. Long
  // analysis/replay jobs override this value at their call site below.
  timeout: 15000,
  // The HttpOnly session cookie is the fallback when localStorage is cleared
  // or unavailable. This also works for the local frontend/backend split.
  withCredentials: true,
})

const AUTH_TOKEN_STORAGE_KEY = 'jiaren-auth-token'
const AUTH_USER_STORAGE_KEY = 'jiaren-auth-user'
let authToken = ''
let authFailureHandler = null

const readStoredAuthToken = () => {
  if (typeof window === 'undefined') return ''
  try {
    return String(window.localStorage.getItem(AUTH_TOKEN_STORAGE_KEY) || '').trim()
  } catch {
    return ''
  }
}

authToken = readStoredAuthToken()

const readStoredAuthUser = () => {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.localStorage.getItem(AUTH_USER_STORAGE_KEY)
    if (!raw) return null
    const user = JSON.parse(raw)
    if (!user || !Number.isFinite(Number(user.id)) || !String(user.username || '').trim()) return null
    return {
      id: Number(user.id),
      username: String(user.username).trim(),
      createdAt: user.createdAt || user.created_at || '',
    }
  } catch {
    return null
  }
}

export const setStoredAuthUser = (user) => {
  if (typeof window === 'undefined') return
  try {
    if (user && Number.isFinite(Number(user.id)) && String(user.username || '').trim()) {
      window.localStorage.setItem(AUTH_USER_STORAGE_KEY, JSON.stringify({
        id: Number(user.id),
        username: String(user.username).trim(),
        createdAt: user.createdAt || user.created_at || '',
      }))
    } else {
      window.localStorage.removeItem(AUTH_USER_STORAGE_KEY)
    }
  } catch {
    // Storage access can be unavailable in private browser contexts.
  }
}

export const getStoredAuthUser = readStoredAuthUser

export const clearStoredAuthUser = () => setStoredAuthUser(null)

export const setAuthToken = (token) => {
  authToken = String(token || '').trim()
  if (typeof window === 'undefined') return
  try {
    if (authToken) window.localStorage.setItem(AUTH_TOKEN_STORAGE_KEY, authToken)
    else window.localStorage.removeItem(AUTH_TOKEN_STORAGE_KEY)
  } catch {
    // Storage access can be unavailable in private browser contexts.
  }
}

export const clearAuthToken = () => setAuthToken('')

export const hasAuthToken = () => Boolean(authToken)

export const setAuthFailureHandler = (handler) => {
  authFailureHandler = typeof handler === 'function' ? handler : null
}

api.interceptors.request.use((config) => {
  if (authToken) {
    config.headers = config.headers || {}
    config.headers.Authorization = `Bearer ${authToken}`
  }
  return config
})

api.interceptors.response.use(
  (response) => response,
  (error) => {
    // A transient/private-data request must not erase a valid persisted
    // session. Only /api/auth/me is authoritative for session validity.
    const requestUrl = String(error?.config?.url || '')
    const isAuthCheck = /\/api\/auth\/me(?:$|[?#])/.test(requestUrl)
    const requestAuthorization = error?.config?.headers?.Authorization
      || error?.config?.headers?.authorization
      || ''
    const failedToken = String(requestAuthorization).replace(/^Bearer\s+/i, '').trim()
    const belongsToCurrentSession = !failedToken || failedToken === authToken
    if (error?.response?.status === 401 && isAuthCheck && belongsToCurrentSession) {
      clearAuthToken()
      authFailureHandler?.()
    }
    return Promise.reject(error)
  },
)

export const registerAccount = async ({ username, password }) => {
  const { data } = await api.post('/api/auth/register', { username, password })
  return data
}

export const loginAccount = async ({ username, password }) => {
  const { data } = await api.post('/api/auth/login', { username, password })
  return data
}

export const fetchCurrentUser = async () => {
  const { data } = await api.get('/api/auth/me', { timeout: 12000 })
  return data.user || null
}

export const fetchUpdateLog = async () => {
  const { data } = await api.get('/api/update-log', {
    timeout: 8000,
    headers: { 'Cache-Control': 'no-cache', Pragma: 'no-cache' },
  })
  return data
}

export const logoutAccount = async () => {
  const { data } = await api.post('/api/auth/logout')
  return data
}

export const fetchAiSettings = async () => {
  const { data } = await api.get('/api/account/ai-settings', { timeout: 10000 })
  return data.settings || {}
}

export const saveAiSettings = async (settings) => {
  const { data } = await api.put('/api/account/ai-settings', settings)
  return data.settings || {}
}

export const fetchNotificationSettings = async () => {
  const { data } = await api.get('/api/account/notification-settings', { timeout: 10000 })
  return data.settings || {}
}

export const saveNotificationSettings = async (settings) => {
  const { data } = await api.put('/api/account/notification-settings', settings)
  return data.settings || {}
}

export const sendTestEmail = async (recipient = '') => {
  const { data } = await api.post('/api/notifications/test-email', { recipient }, { timeout: 20000 })
  return data
}

export const searchStocks = async (keyword) => {
  const { data } = await api.get('/api/stocks/search', { params: { keyword }, timeout: 8000 })
  return data.items || []
}

export const fetchStocks = async () => {
  const { data } = await api.get('/api/stocks', { timeout: 15000 })
  return data.items || []
}

export const fetchStockAnalysisHistory = async () => {
  const { data } = await api.get('/api/stocks/history', { timeout: 10000 })
  return data.items || []
}

export const saveStockAnalysisHistory = async ({ symbol, name }) => {
  const { data } = await api.post('/api/stocks/history', { symbol, name })
  return data.item || null
}

export const deleteStockAnalysisHistory = async (symbol) => {
  const { data } = await api.delete(`/api/stocks/history/${encodeURIComponent(symbol)}`)
  return data.deleted !== false
}

export const fetchMarketToday = async (includeGrowthBoards = false, refreshIndices = false) => {
  const { data } = await api.get('/api/market/today', {
    params: { includeGrowthBoards, refreshIndices },
    timeout: refreshIndices ? 15000 : 8000,
  })
  return data
}

export const refreshMarketData = async (force = false) => {
  const { data } = await api.post('/api/market/refresh', null, { params: { force }, timeout: 90000 })
  return data
}

export const fetchStockQuotes = async (symbols = []) => {
  const { data } = await api.get('/api/stocks/quotes', { params: { symbols: symbols.join(',') }, timeout: 10000 })
  return data
}

export const fetchPortfolio = async () => {
  const { data } = await api.get('/api/portfolio', { timeout: 10000 })
  return data
}

export const savePortfolioPosition = async (payload) => {
  const { data } = await api.post('/api/portfolio', payload)
  return data.item
}

export const savePortfolioCash = async (cashBalance) => {
  const { data } = await api.put('/api/portfolio/account', { cashBalance })
  return data.account
}

export const fetchPortfolioTrades = async () => {
  const { data } = await api.get('/api/portfolio/trades', { timeout: 10000 })
  return data.items || []
}

export const executePortfolioTrade = async (payload) => {
  const { data } = await api.post('/api/portfolio/trades', payload)
  return data.item
}

export const deletePortfolioPosition = async (symbol) => {
  const { data } = await api.delete(`/api/portfolio/${symbol}`)
  return data
}

export const fetchWatchlist = async () => {
  const { data } = await api.get('/api/watchlist', { timeout: 10000 })
  return data
}

export const saveWatchlistItem = async (payload) => {
  const { data } = await api.post('/api/watchlist', payload)
  return data.item
}

export const deleteWatchlistItem = async (symbol) => {
  const { data } = await api.delete(`/api/watchlist/${symbol}`)
  return data
}

export const fetchDisciplinePlan = async ({ symbol }) => {
  const { data } = await api.get('/api/discipline/plan', { params: { symbol }, timeout: 20000 })
  return data
}

export const fetchDisciplinePortfolio = async () => {
  const { data } = await api.get('/api/discipline/portfolio', { timeout: 20000 })
  return data
}

export const fetchDisciplineWatchlist = async () => {
  const { data } = await api.get('/api/discipline/watchlist', { timeout: 20000 })
  return data
}

export const fetchDisciplineJournal = async () => {
  const { data } = await api.get('/api/discipline/journal', { timeout: 10000 })
  return data.items || []
}

export const saveDisciplineJournal = async (payload) => {
  const { data } = await api.post('/api/discipline/journal', payload)
  return data.item
}

export const analyzeStock = async (symbol) => {
  const { data } = await api.get('/api/analyze', { params: { symbol }, timeout: 120000 })
  return data
}

export const analyzeStockIntraday = async (symbol, period) => {
  const { data } = await api.get('/api/intraday/analyze', { params: { symbol, period }, timeout: 120000 })
  return data
}

export const fetchFundamentals = async (symbol) => {
  const { data } = await api.get(`/api/stocks/${symbol}/fundamentals`)
  return data
}

export const fetchF10Status = async (symbol) => {
  const { data } = await api.get(`/api/stocks/${symbol}/f10/status`)
  return data
}

export const syncF10 = async (symbol, options = {}) => {
  const { data } = await api.post(`/api/stocks/${symbol}/f10/sync`, null, {
    params: { mode: options.mode || 'background' },
  })
  return data
}

export const findMainRiseStock = async ({ minPrice, maxPrice }) => {
  const { data } = await api.get('/api/stocks/find-main-rise', {
    params: {
      minPrice,
      maxPrice,
      maxAttempts: 30,
    },
    timeout: 120000,
  })
  return data
}

export const findGoldenPillarStock = async ({ minPrice, maxPrice }, options = {}) => {
  const { data } = await api.get('/api/stocks/find-golden-pillar', {
    params: {
      minPrice,
      maxPrice,
      maxAttempts: options.maxAttempts ?? 80,
      supportTolerance: options.supportTolerance ?? 0.01,
    },
    timeout: 120000,
  })
  return data
}

export const analyzeIndex = async (params = {}) => {
  const { data } = await api.get('/api/index/analyze', { params, timeout: 120000 })
  return data
}

export const analyzeIndexIntraday = async (period, params = {}) => {
  const { data } = await api.get('/api/index/intraday/analyze', { params: { ...params, period }, timeout: 120000 })
  return data
}

export const analyzeAi = async ({ result, intraday, targetType }) => {
  const { data } = await api.post('/api/ai/analyze', {
    result,
    intraday,
    targetType,
  }, { timeout: 120000 })
  return data
}

export const fetchBinanceSnapshot = async ({
  network = 'mainnet',
  marketType = 'SPOT',
  symbol = '',
  interval = '1h',
  clientId = 'default',
} = {}) => {
  const { data } = await api.get('/api/binance/snapshot', {
    params: {
      network,
      market: String(marketType || 'SPOT').toUpperCase() === 'FUTURES' ? 'FUTURES' : 'SPOT',
      symbol: String(symbol || '').trim().toUpperCase(),
      interval,
      clientId,
    },
    headers: { 'Cache-Control': 'no-cache', Pragma: 'no-cache' },
    // A stalled snapshot request must not block the next one-second poll.
    timeout: 5000,
  })
  return data
}

export const fetchBinanceFuturesSnapshot = async ({
  network = 'mainnet',
  symbol = '',
  interval = '5m',
  clientId = 'default',
} = {}) => fetchBinanceSnapshot({
  network,
  marketType: 'FUTURES',
  symbol,
  interval,
  clientId,
})

export const fetchBinanceTicker = async ({ network = 'mainnet', symbol = 'BTCUSDT' } = {}) => {
  const { data } = await api.get('/api/binance/ticker', { params: { network, symbol } })
  return data
}

export const fetchBinanceKlines = async ({ network = 'mainnet', symbol = 'BTCUSDT', interval = '1h', limit = 72, marketType = 'SPOT' } = {}) => {
  const market = String(marketType).toUpperCase() === 'FUTURES' ? 'futures' : 'spot'
  const { data } = await api.get('/api/binance/klines', { params: { network, symbol, interval, limit, market } })
  return { items: data.items || [], stale: Boolean(data.stale) }
}

export const fetchBinanceSpotMarkets = async ({ network = 'mainnet' } = {}) => {
  const { data } = await api.get('/api/binance/spot/markets', { params: { network } })
  return { items: data.items || [], limit: data.limit || 100, stale: Boolean(data.stale), updatedAt: data.updatedAt || null }
}

export const fetchBinanceFuturesMarkets = async ({ network = 'mainnet' } = {}) => {
  const { data } = await api.get('/api/binance/futures/markets', { params: { network } })
  return { items: data.items || [], limit: data.limit || 100, stale: Boolean(data.stale), updatedAt: data.updatedAt || null }
}

export const analyzeBinanceFutures = async ({ network = 'mainnet', limit = 24, symbol = '', strategySettings = null } = {}) => {
  const payload = { network, limit }
  if (symbol) payload.symbol = String(symbol).trim().toUpperCase()
  if (strategySettings && typeof strategySettings === 'object') payload.strategySettings = strategySettings
  const { data } = await api.post('/api/binance/futures/analyze', payload)
  return data.job || null
}

export const fetchBinanceFuturesAnalysis = async (jobId) => {
  const { data } = await api.get(`/api/binance/futures/analyze/${encodeURIComponent(jobId)}`, {
    // A status poll must fail fast so the page can retry while the backend
    // continues the scan in its own worker.
    timeout: 5000,
    headers: { 'Cache-Control': 'no-cache', Pragma: 'no-cache' },
  })
  return data.job || null
}

export const analyzeBinanceMarket = async ({ marketType = 'FUTURES', network = 'mainnet', limit = 24, symbol = '', strategySettings = null } = {}) => {
  const normalizedType = String(marketType).toUpperCase() === 'SPOT' ? 'SPOT' : 'FUTURES'
  const endpoint = normalizedType === 'SPOT' ? '/api/binance/spot/analyze' : '/api/binance/futures/analyze'
  const payload = { network, limit }
  if (symbol) payload.symbol = String(symbol).trim().toUpperCase()
  if (strategySettings && typeof strategySettings === 'object' && normalizedType === 'FUTURES') payload.strategySettings = strategySettings
  const { data } = await api.post(endpoint, payload)
  return data.job || null
}

export const fetchBinanceMarketAnalysis = async (marketType = 'FUTURES', jobId) => {
  const normalizedType = String(marketType).toUpperCase() === 'SPOT' ? 'SPOT' : 'FUTURES'
  const endpoint = normalizedType === 'SPOT' ? '/api/binance/spot/analyze' : '/api/binance/futures/analyze'
  const { data } = await api.get(`${endpoint}/${encodeURIComponent(jobId)}`)
  return data.job || null
}

export const analyzeBinanceSpot = async ({ network = 'mainnet', limit = 24 } = {}) => {
  return analyzeBinanceMarket({ marketType: 'SPOT', network, limit })
}

export const fetchBinanceSpotAnalysis = async (jobId) => {
  return fetchBinanceMarketAnalysis('SPOT', jobId)
}

export const startBinanceFuturesStrategyBacktest = async ({ network = 'mainnet', backtestOptions = null, strategySettings = null } = {}) => {
  const payload = { network }
  if (backtestOptions && typeof backtestOptions === 'object') payload.backtestOptions = backtestOptions
  if (strategySettings && typeof strategySettings === 'object') payload.strategySettings = strategySettings
  const { data } = await api.post('/api/binance/futures/backtest', payload)
  return data.job || null
}

export const fetchBinanceFuturesStrategyBacktest = async (jobId) => {
  const { data } = await api.get(`/api/binance/futures/backtest/${encodeURIComponent(jobId)}`)
  return data.job || null
}

export const fetchBinanceFuturesBacktestHistoryStatus = async ({ network = 'mainnet' } = {}) => {
  // Completeness checks scan persisted coverage and candle rows for the fixed
  // corpus; on a cold cache this can exceed the ordinary 15s UI read timeout.
  const { data } = await api.get('/api/binance/futures/backtest/history-status', { params: { network }, timeout: 60000 })
  return data.status || null
}

export const startBinanceFuturesBacktestHistoryFill = async ({ network = 'mainnet' } = {}) => {
  const { data } = await api.post('/api/binance/futures/backtest/history-fill', { network })
  return data.job || null
}

export const fetchBinanceFuturesBacktestHistoryFill = async (jobId) => {
  const { data } = await api.get(`/api/binance/futures/backtest/history-fill/${encodeURIComponent(jobId)}`)
  return data.job || null
}

export const fetchBinanceFuturesModelStatus = async ({ network = 'mainnet' } = {}) => {
  const { data } = await api.get('/api/binance/futures/model/status', { params: { network }, timeout: 15000 })
  return data.status || null
}

export const fetchBinanceFuturesModelRuns = async ({ network = 'mainnet', limit = 24 } = {}) => {
  const { data } = await api.get('/api/binance/futures/model/runs', { params: { network, limit }, timeout: 15000 })
  return data || null
}

export const startBinanceFuturesModelTraining = async ({ network = 'mainnet', config = null } = {}) => {
  const payload = { network }
  if (config && typeof config === 'object') payload.config = config
  const { data } = await api.post('/api/binance/futures/model/train', payload, { timeout: 15000 })
  return data.job || null
}

export const fetchBinanceFuturesModelTraining = async (jobId) => {
  const { data } = await api.get(`/api/binance/futures/model/train/${encodeURIComponent(jobId)}`, { timeout: 15000 })
  return data.job || null
}

export const connectBinanceAccount = async ({ apiKey, apiSecret }) => {
  const { data } = await api.post('/api/binance/connect', { apiKey, apiSecret })
  return data
}

export const fetchBinanceAccount = async () => {
  const { data } = await api.get('/api/binance/account')
  return data
}

export const fetchBinanceConnectionSettings = async () => {
  const { data } = await api.get('/api/binance/connection-settings')
  return { settings: data.settings || {}, connection: data.connection || null }
}

export const saveBinanceConnectionSettings = async (settings) => {
  const { data } = await api.put('/api/binance/connection-settings', settings)
  return { settings: data.settings || {}, connection: data.connection || null }
}

export const fetchBinanceUiSettings = async () => {
  const { data } = await api.get('/api/binance/ui-settings')
  return data.settings || {}
}

export const saveBinanceUiSettings = async (settings) => {
  const { data } = await api.put('/api/binance/ui-settings', settings)
  return data.settings || {}
}

export const testBinanceFuturesWebsocket = async ({ network = 'mainnet', scope = 'market' } = {}) => {
  const { data } = await api.post('/api/binance/websocket-test', { network, scope })
  return data.result || null
}

export const testBinanceFuturesAccountWebsocket = async () => {
  const { data } = await api.post('/api/binance/websocket-test', { scope: 'account' })
  return data.result || null
}

export const fetchBinanceStrategySettings = async () => {
  const { data } = await api.get('/api/binance/strategy-settings')
  return data.settings || {}
}

export const saveBinanceStrategySettings = async (settings) => {
  const { data } = await api.put('/api/binance/strategy-settings', settings)
  return data.settings || {}
}

export const fetchBinanceCopyTradingSettings = async () => {
  const { data } = await api.get('/api/binance/copy-trading-settings')
  return data
}

export const saveBinanceCopyTradingSettings = async (settings) => {
  const { data } = await api.put('/api/binance/copy-trading-settings', settings)
  return data
}

export const captureBinanceSmartMoneyAuth = async () => {
  const { data } = await api.post('/api/binance/smart-money/auth/capture', null, { timeout: 30000 })
  return data.auth || {}
}

export const saveBinanceSmartMoneyPositionFollow = async ({ topTraderId, symbol, positionSide, enabled = true } = {}) => {
  const { data } = await api.put('/api/binance/smart-money/follows', { topTraderId, symbol, positionSide, enabled })
  return data
}

export const updateBinanceFuturesPositionProtection = async ({ symbol, positionSide = 'BOTH', stopLoss = null, takeProfit } = {}) => {
  const payload = { symbol, positionSide, stopLoss }
  if (takeProfit !== undefined) payload.takeProfit = takeProfit
  const { data } = await api.put('/api/binance/futures/positions/protection', payload)
  return data.protection || null
}

export const updateBinanceFuturesPositionLevelProtection = async ({ symbol, positionSide = 'BOTH', stopLoss = null, takeProfit } = {}) => {
  const payload = { symbol, positionSide, stopLoss }
  if (takeProfit !== undefined) payload.takeProfit = takeProfit
  const { data } = await api.put('/api/binance/futures/positions/position-protection', payload)
  return data.protection || null
}

export const updateBinanceFuturesPartialProtection = async ({ symbol, positionSide = 'BOTH', stopLoss = null, takeProfit = null, quantityRatio = 50 } = {}) => {
  const { data } = await api.put('/api/binance/futures/positions/partial-protection', { symbol, positionSide, stopLoss, takeProfit, quantityRatio })
  return data.protection || null
}

export const closeBinanceFuturesPositionMarket = async ({ symbol, positionSide = 'BOTH', quantityRatio = 100 } = {}) => {
  const { data } = await api.post('/api/binance/futures/positions/market-close', { symbol, positionSide, quantityRatio }, { timeout: 45000 })
  return data.position || null
}

export const applyBinanceFuturesPlanProtection = async ({ symbol, positionSide = 'BOTH', stopLoss, protectiveTakeProfit = null, firstTakeProfit, extensionTakeProfit, protectiveTakeProfitRatio = 25, firstTakeProfitRatio = 50, secondTakeProfitRatio = 75 } = {}) => {
  const payload = { symbol, positionSide, stopLoss, protectiveTakeProfit, firstTakeProfit, extensionTakeProfit, firstTakeProfitRatio, secondTakeProfitRatio }
  // Direct MODEL plans intentionally have no near-term protective target.
  // Do not serialize a zero ratio: the server treats the presence of this
  // field as a request to validate a three-target ladder.
  if (Number.isFinite(Number(protectiveTakeProfit)) && Number(protectiveTakeProfit) > 0) {
    payload.protectiveTakeProfitRatio = protectiveTakeProfitRatio
  }
  const { data } = await api.post('/api/binance/futures/positions/apply-plan', payload)
  return data.protection || null
}

export const placeBinanceFuturesLimitPlanOrder = async ({ symbol, direction, quantity, costPrice, leverage = 1, stopLoss, protectiveTakeProfit = null, extensionTakeProfit, protectiveTakeProfitRatio = 25, firstTakeProfitRatio = 50, secondTakeProfitRatio = 75, positionSide = null, plan, allowTrial = false } = {}) => {
  const payload = { symbol, direction, quantity, costPrice, leverage, stopLoss, protectiveTakeProfit, extensionTakeProfit, firstTakeProfitRatio, secondTakeProfitRatio, positionSide, plan, allowTrial, marketMode: 'FUTURES' }
  // A two-target MODEL plan must not carry a zero protective ratio. The
  // absence of this field is the wire-level representation of no protective
  // target; the remaining targets are still sent normally.
  if (Number.isFinite(Number(protectiveTakeProfit)) && Number(protectiveTakeProfit) > 0) {
    payload.protectiveTakeProfitRatio = protectiveTakeProfitRatio
  }
  const { data } = await api.post('/api/binance/futures/orders/limit-plan', payload)
  // Keep the server-created monitor together with the exchange order. The
  // entry route creates the monitor before submitting the real order so the
  // UI must not create a second monitor or discard this binding.
  return data.order ? { ...data.order, monitor: data.monitor || null } : null
}

export const placeBinanceCopyTradingOrder = async ({ symbol, direction, quantity, costPrice, leverage = 1, orderType = 'LIMIT', positionSide = null, note = '' } = {}) => {
  const { data } = await api.post('/api/binance/futures/copy-trading/order', {
    symbol,
    direction,
    quantity,
    costPrice,
    leverage,
    orderType,
    positionSide,
    note,
  }, { timeout: 45000 })
  return { order: data.order || null, monitor: data.monitor || null }
}

export const deleteBinanceCredentials = async () => {
  const { data } = await api.delete('/api/binance/credentials')
  return data.deleted !== false
}

export const fetchBinanceSimulatedPositions = async () => {
  const { data } = await api.get('/api/binance/simulated-positions')
  return { items: data.items || [], updatedAt: data.updatedAt || null }
}

export const previewBinanceFuturesPositionMonitor = async ({ symbol, side }) => {
  const { data } = await api.post('/api/binance/futures/positions/monitor-preview', { symbol, side }, { timeout: 30000 })
  return data
}

export const createBinanceSimulatedPosition = async (position) => {
  const { data } = await api.post('/api/binance/simulated-positions', position)
  return data.item || null
}

export const updateBinanceSimulatedPosition = async (positionId, position) => {
  const { data } = await api.put(`/api/binance/simulated-positions/${encodeURIComponent(positionId)}`, position)
  return data.item || null
}

export const deleteBinanceSimulatedPosition = async (positionId) => {
  const { data } = await api.delete(`/api/binance/simulated-positions/${encodeURIComponent(positionId)}`)
  return {
    deleted: data.deleted !== false,
    orderCleanup: data.orderCleanup || null,
  }
}

export const restoreBinanceSimulatedPosition = async (positionId) => {
  const { data } = await api.post(`/api/binance/simulated-positions/${encodeURIComponent(positionId)}/restore`)
  return data.item || null
}
