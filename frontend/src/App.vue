<template>
  <main class="app-layout">
    <header class="top-nav">
      <div class="top-nav-brand">
        <img class="top-nav-logo" src="/title.webp" alt="三倍做多家务ETF分析站" />
        <strong class="top-nav-title"><span class="brand-main-title">三倍做多家务ETF分析站</span></strong>
        <button
          type="button"
          :class="['top-nav-binance-entry', isBinanceWorkspace ? 'is-a-share-target' : 'is-binance-target']"
          :aria-label="isBinanceWorkspace ? '进入 A 股' : '进入 Binance'"
          :title="isBinanceWorkspace ? '进入 A 股' : '进入 Binance'"
          @click="switchPrimaryWorkspace"
        >
          <component :is="isBinanceWorkspace ? LineChart : Bitcoin" size="16" />
          <span>{{ isBinanceWorkspace ? 'A股' : 'Binance' }}</span>
        </button>
      </div>

      <div class="top-nav-actions">
        <el-button
          v-if="!currentUser"
          class="top-nav-action-btn account-entry-btn"
          :icon="CircleUserRound"
          plain
          circle
          :loading="authRestoring"
          title="登录或注册账户"
          aria-label="登录或注册账户"
          @click="openAuthDialog('login')"
        />
        <div v-else class="account-current">
          <span :title="`当前账户：${currentUser.username}`"><CircleUserRound size="17" /><strong>{{ currentUser.username }}</strong></span>
          <el-button class="top-nav-action-btn" circle plain :icon="LogOut" title="退出账户" aria-label="退出账户" @click="signOut" />
        </div>
        <el-button class="top-nav-action-btn theme-toggle-btn" :icon="isDark ? Sun : Moon" plain circle @click="toggleTheme" :title="isDark ? '切换浅色模式' : '切换深色模式'" />
        <el-button class="top-nav-action-btn update-log-btn" :icon="History" plain circle title="更新日志" aria-label="更新日志" @click="updateLogVisible = true" />
        <el-button
          class="top-nav-action-btn settings-btn"
          :icon="Settings"
          plain
          circle
          :aria-label="activePage === 'binance' || activePage === 'binanceBacktest' ? '账户 API 设置' : (isBinanceWorkspace ? '账户 API 设置' : '应用设置')"
          :title="activePage === 'binance' || activePage === 'binanceBacktest' ? '账户 API 设置' : (isBinanceWorkspace ? '账户 API 设置' : '应用设置')"
          @click="openCurrentSettings"
        />
      </div>
    </header>

    <aside class="main-sidebar">
      <div class="sidebar-label">{{ sidebarLabel }}</div>
      <nav class="main-nav">
        <button
          v-for="item in sidebarNavItems"
          :key="item.value"
          type="button"
          :class="['main-nav-item', activePage === item.value ? 'is-active' : '']"
          @click="setActivePage(item.value)"
        >
          <component :is="item.icon" size="18" />
          <span>{{ item.label }}</span>
        </button>
      </nav>
    </aside>

      <section :class="['page-shell', { 'a-share-workspace-shell': !isBinanceWorkspace }]">
      <section v-if="analysisProgress.visible && analysisProgress.page === activePage" class="analysis-progress-card">
        <div class="analysis-progress-head">
          <span>{{ analysisProgress.title }}</span>
          <strong>{{ analysisProgress.title === '完成' ? '已完成' : '进行中' }}</strong>
        </div>
        <div v-if="analysisProgress.steps.length" class="analysis-progress-steps">
          <span
            v-for="(step, index) in analysisProgress.steps"
            :key="index"
            :class="['analysis-progress-step', `is-${step.status}`]"
          >
            <i></i><b>{{ step.label }}</b>
          </span>
        </div>
        <div
          :class="['analysis-progress-track', analysisProgress.title === '完成' ? 'is-complete' : 'is-indeterminate']"
          role="progressbar"
          aria-label="分析进度"
          :aria-valuemin="0"
          :aria-valuemax="100"
          :aria-busy="analysisProgress.title !== '完成'"
        >
          <div class="analysis-progress-fill"></div>
        </div>
        <p class="analysis-progress-message">{{ analysisProgress.message }}</p>
      </section>

      <section ref="homePageRef" v-show="activePage === 'home'" class="page-content home-page">
        <section ref="homeGridRef" class="home-grid" :style="homeGridStyle">
          <section class="home-apology-panel" aria-label="对不起的小人表情">
            <div class="apology-panel-top">
              <span class="apology-kicker">すみません</span>
              <span :class="['apology-dot', marketPhaseClass]" :aria-label="marketPhaseLabel"></span>
            </div>
            <div class="apology-art" role="img" aria-label="道歉表情"></div>
            <div class="apology-copy">
              <span>事到如今</span>
              <div class="apology-main-line">
                <strong>对不起</strong>
                <em class="apology-de">的</em>
              </div>
              <p>是家人</p>
            </div>
          </section>

          <div class="home-info-column">
            <section class="market-overview-panel">
              <div class="home-section-head">
                <div class="home-section-title">
                  <span class="section-glyph"><component :is="Gauge" size="18" /></span>
                  <div class="home-section-text">
                    <span>今日市场</span>
                    <strong>{{ marketToday?.heat || '--' }}</strong>
                  </div>
                </div>
                <div class="market-head-right">
                  <div class="market-times">
                    <span>实际时间：{{ marketActualTimeText }}</span>
                    <span class="market-data-line">
                      <span class="market-source-label">行情来源：<br />{{ marketSourceLabel(marketSync?.source || marketToday?.source) }}</span>
                      <span>数据时间：{{ formatMarketTimestamp(marketToday?.updatedAt) }}</span>
                    </span>
                  </div>
                  <el-button
                    circle
                    plain
                    :icon="RefreshCw"
                    :loading="marketLoading"
                    title="刷新市场数据"
                    aria-label="刷新市场数据"
                    @click="loadHome(true)"
                  />
                </div>
              </div>
              <div class="market-metrics">
                <div class="market-breadth-tile">
                  <span>市场情绪</span>
                  <div v-if="marketBreadth.total" class="market-breadth-wrap">
                    <div
                      class="market-breadth-bar"
                      role="img"
                      aria-label="上涨、平盘、下跌家数占比示意图"
                    >
                      <span
                        v-if="marketBreadth.up"
                        class="is-up"
                        :style="marketBreadthSegmentStyle.up"
                        :title="`上涨 ${marketBreadth.up} 只（${marketBreadth.upPct.toFixed(1)}%）`"
                      ></span>
                      <span
                        v-if="marketBreadth.flat"
                        class="is-flat"
                        :style="marketBreadthSegmentStyle.flat"
                        :title="`平盘 ${marketBreadth.flat} 只（${marketBreadth.flatPct.toFixed(1)}%）`"
                      ></span>
                      <span
                        v-if="marketBreadth.down"
                        class="is-down"
                        :style="marketBreadthSegmentStyle.down"
                        :title="`下跌 ${marketBreadth.down} 只（${marketBreadth.downPct.toFixed(1)}%）`"
                      ></span>
                    </div>
                    <span
                      v-if="marketBreadth.flat"
                      class="market-breadth-flat-label"
                      :style="{ left: (marketBreadth.upPct + marketBreadth.flatPct / 2) + '%' }"
                    >平 {{ marketBreadth.flat }}</span>
                  </div>
                  <div v-if="marketBreadth.total" class="market-breadth-legend">
                    <span class="is-up">涨 {{ marketBreadth.up }}（{{ marketBreadth.upPct.toFixed(1) }}%）</span>
                    <span class="is-down">跌 {{ marketBreadth.down }}（{{ marketBreadth.downPct.toFixed(1) }}%）</span>
                  </div>
                </div>
                <div class="market-limit-tile">
                  <span class="market-metric-label">涨跌停</span>
                  <strong class="market-limit-counts">
                    <b class="limit-up-count">{{ marketToday?.limitUpCount ?? '--' }}</b>
                    <i aria-hidden="true">/</i>
                    <b class="limit-down-count">{{ marketToday?.limitDownCount ?? '--' }}</b>
                  </strong>
                </div>
                <div>
                  <span>平均涨幅</span>
                  <strong :class="profitClass(marketToday?.avgPctChange)">{{ formatPercent(marketToday?.avgPctChange) }}</strong>
                </div>
                <div>
                  <span>成交额</span>
                  <strong>{{ formatMoney(marketToday?.totalAmount) }}</strong>
                  <small v-if="marketAmountDiffText" class="market-amount-diff" :class="marketAmountDiffClass">{{ marketAmountDiffText }}</small>
                </div>
              </div>
              <p v-if="marketToday?.stale || marketSync?.stale" class="stale-tip">{{ marketDataStatusText }}</p>
            </section>

            <section class="home-index-panel">
              <div class="home-section-head">
                <div class="home-section-title">
                  <span class="section-glyph"><component :is="BarChart3" size="18" /></span>
                  <div class="home-section-text">
                    <span>主要指数</span>
                    <strong>点击进入指数分析</strong>
                  </div>
                </div>
              </div>
              <div class="home-index-grid">
                <button
                  v-for="item in homeIndices"
                  :key="item.symbol"
                  type="button"
                  class="home-index-card"
                  @click="openIndexFromHome(item.symbol)"
                >
                  <span>{{ item.name }}</span>
                  <strong>{{ formatPrice(item.latestPrice) }}</strong>
                  <em :class="profitClass(item.pctChange)">{{ formatPercent(item.pctChange) }}</em>
                </button>
              </div>
            </section>

            <section class="ranking-panel ranking-switcher-panel">
              <div class="home-section-head ranking-head">
                <div class="ranking-head-top">
                  <div class="home-section-title">
                    <span class="section-glyph"><component :is="homeRankingIcon" size="18" /></span>
                    <div class="home-section-text">
                      <span>市场榜单</span>
                      <strong>{{ homeRankingTitle }} · Top 10</strong>
                    </div>
                  </div>
                  <div class="ranking-filter" title="包含或排除创业板与科创板">
                    <span>创业 / 科创</span>
                    <el-switch
                      v-model="includeGrowthBoards"
                      inline-prompt
                      active-text="含"
                      inactive-text="不含"
                      aria-label="榜单是否包含创业板与科创板"
                      :loading="rankingFilterLoading"
                      @change="reloadHomeRankings"
                    />
                  </div>
                </div>
                <el-segmented
                  v-model="homeRankingMode"
                  class="home-ranking-tabs"
                  :options="homeRankingOptions"
                  aria-label="市场榜单类型"
                />
              </div>
              <div class="mini-rank-list">
                <button v-for="item in homeRankingItems" :key="`${homeRankingMode}-${item.symbol}`" type="button" @click="openStockFromHome(item)">
                  <span><b class="rank-symbol">{{ item.symbol }}</b> {{ item.name }}</span>
                  <strong :class="homeRankingValueClass(item)">{{ homeRankingValue(item) }}</strong>
                </button>
                <p v-if="!homeRankingItems.length" class="ranking-empty">暂无榜单数据</p>
              </div>
            </section>
          </div>

        </section>
      </section>

      <section ref="disciplinePageRef" v-show="activePage === 'discipline'" class="page-content discipline-page">
        <template v-if="currentUser">
        <p v-if="portfolioMarketData?.stale || marketSync?.stale" class="stale-tip discipline-stale-tip">{{ marketDataStatusText }}</p>
        <div ref="disciplineWorkspaceRef" class="discipline-workspace-grid" :style="disciplineWorkspaceStyle">
          <aside class="discipline-left-rail">
            <section class="discipline-account-panel">
              <div class="discipline-account-head">
                <div>
                  <span>短线账户</span>
                  <strong>{{ formatMoney(portfolioSummary.totalAccountValue) }}</strong>
                  <small>持仓市值与可用现金合计</small>
                </div>
                <el-button circle plain :icon="RefreshCw" :loading="disciplinePortfolioLoading" title="刷新账户" aria-label="刷新账户" @click="refreshDisciplineWorkspace(true)" />
              </div>
              <div class="discipline-cash-editor">
                <span>可用现金</span>
                <el-input v-model="cashBalanceDraft" inputmode="decimal" placeholder="输入可用现金" @keyup.enter="saveCashBalance" />
                <el-button circle type="primary" plain :icon="Save" :loading="cashSaving" title="保存现金" aria-label="保存现金" @click="saveCashBalance" />
              </div>
              <div class="discipline-account-grid">
                <div><span>持仓市值</span><strong>{{ formatMoney(portfolioSummary.totalMarketValue) }}</strong></div>
                <div><span>浮动盈亏</span><strong :class="profitClass(portfolioSummary.floatingProfit)">{{ formatSignedMoney(portfolioSummary.floatingProfit) }}</strong></div>
                <div><span>预估风险</span><strong>{{ formatMoney(disciplinePortfolioSummary.plannedRisk) }}</strong></div>
                <div><span>待处理</span><strong>卖 {{ disciplinePortfolioSummary.actionCounts?.SELL || 0 }} / 减 {{ disciplinePortfolioSummary.actionCounts?.REDUCE || 0 }}</strong></div>
              </div>
            </section>

            <section class="discipline-panel discipline-trade-history">
              <div class="discipline-panel-head">
                <div class="discipline-panel-title">
                  <strong>成交记录</strong>
                  <small>{{ tradeHistory.length }} 笔最近成交</small>
                </div>
                <el-button circle text :icon="RefreshCw" :loading="tradeHistoryLoading" title="刷新成交记录" aria-label="刷新成交记录" @click="loadTradeHistory" />
              </div>
              <div v-loading="tradeHistoryLoading" class="discipline-trade-list" element-loading-text="正在更新成交记录...">
                <p v-if="!tradeHistoryLoading && !tradeHistory.length" class="discipline-list-empty">暂无成交记录</p>
                <article v-for="row in tradeHistory" :key="row.id || `${row.createdAt}-${row.symbol}-${row.action}`" class="discipline-trade-card">
                  <header>
                    <div>
                      <strong :class="row.action === 'BUY' ? 'discipline-action-positive' : 'discipline-action-negative'">{{ row.action === 'BUY' ? '买入' : '卖出' }}</strong>
                      <span>{{ row.symbol }} {{ row.name }}</span>
                    </div>
                    <time>{{ formatMarketTimestamp(row.createdAt) }}</time>
                  </header>
                  <div class="discipline-trade-compact-grid">
                    <span><small>成交</small><strong>{{ formatPrice(row.price) }} / {{ row.shares }} 股</strong></span>
                    <span><small>手续费</small><strong>{{ formatMoney(row.fee) }}</strong></span>
                    <span><small>已实现</small><strong :class="profitClass(row.realizedProfit)">{{ formatSignedMoney(row.realizedProfit) }}</strong></span>
                  </div>
                  <small v-if="row.note" class="discipline-trade-note">{{ row.note }}</small>
                  <small v-if="row.disciplineOverrideReasons?.length" class="discipline-exception-reason">{{ disciplineViolationText(row.disciplineOverrideReasons) }}</small>
                </article>
              </div>
            </section>
          </aside>

          <section class="discipline-panel discipline-holdings-panel">
            <div class="discipline-panel-head discipline-list-panel-head">
              <div class="discipline-panel-title">
                <strong>持仓列表</strong>
                <small>{{ disciplinePortfolioSummary.positionCount }} 只持仓 / 可用现金 {{ formatMoney(portfolioSummary.cashBalance) }}</small>
                <small class="discipline-level-source">{{ disciplinePortfolioSummary.errorCount ? `${disciplinePortfolioSummary.errorCount} 只行情暂缺` : '点位来自最近完整日K · 隔日与持仓管理；日内买卖点请在分析页切换分钟K' }}</small>
              </div>
              <div class="discipline-panel-actions">
                <el-button plain :icon="Sparkles" :loading="disciplinePortfolioLoading" @click="analyzeHoldings">一键分析</el-button>
                <el-button type="primary" :icon="Plus" @click="openTradeDialog('BUY')">新开仓</el-button>
              </div>
            </div>
            <div v-if="disciplinePortfolioItems.length" class="discipline-mobile-stock-tabs" role="tablist" aria-label="手机持仓选择">
              <button
                v-for="row in disciplinePortfolioItems"
                :key="`holding-tab-${row.symbol}`"
                type="button"
                role="tab"
                :aria-selected="isSelectedHolding(row)"
                :class="['discipline-mobile-stock-tab', isSelectedHolding(row) ? 'is-active' : '']"
                @click="selectedHoldingSymbol = row.symbol"
              >
                <span>{{ row.name || row.symbol }}</span>
                <strong :class="profitClass(row.floatingProfit)">{{ formatSignedMoney(row.floatingProfit) }}</strong>
              </button>
            </div>
            <div v-loading="disciplinePortfolioLoading" class="discipline-position-list" element-loading-text="正在更新持仓与纪律点位...">
              <p v-if="!disciplinePortfolioLoading && !disciplinePortfolioItems.length" class="discipline-list-empty">暂无持仓</p>
              <article v-for="row in disciplinePortfolioItems" :key="row.symbol" :class="['discipline-position-card', isSelectedHolding(row) ? 'is-mobile-selected' : '']">
                <div
                  class="discipline-card-hit-area"
                  role="button"
                  tabindex="0"
                  :aria-label="`查看 ${row.name || row.symbol} 的个股分析`"
                  @click="openDisciplineStock(row)"
                  @keydown.enter="openDisciplineStock(row)"
                  @keydown.space.prevent="openDisciplineStock(row)"
                >
                  <header class="discipline-position-card-head">
                    <div class="discipline-stock-cell">
                      <strong>{{ row.name || row.symbol }}</strong>
                      <span>{{ row.symbol }}</span>
                    </div>
                    <span :class="disciplineActionClass(row.plan?.tone)">{{ row.plan?.actionLabel || (row.error ? '分析失败' : '待分析') }}</span>
                  </header>
                  <div class="discipline-position-overview">
                    <div class="discipline-position-profit" :class="profitClass(row.floatingProfit)" title="持仓盈亏">
                      <strong>{{ formatSignedMoney(row.floatingProfit) }}</strong>
                      <em>{{ formatPercent(positionProfitRate(row)) }}</em>
                    </div>
                    <div class="discipline-position-metrics">
                      <span><small>最新价</small><strong>{{ formatPrice(row.latestPrice) }}</strong></span>
                      <span><small>成本价</small><strong>{{ formatPrice(row.entryPrice ?? row.costPrice) }}</strong></span>
                      <span><small>股数</small><strong>{{ row.shares }} 股</strong></span>
                      <span><small>市值</small><strong>{{ formatMoney(row.marketValue) }}</strong></span>
                    </div>
                  </div>
                  <p v-if="row.error" class="discipline-row-error">{{ row.error }}</p>
                </div>
                <template v-if="!row.error">
                  <PriceActionDecision
                    :price-action="row.plan.priceAction"
                    :plan="row.plan"
                    context="position"
                    :minimum-reward-risk="row.plan.riskRules?.minimumRewardRisk || row.plan.priceAction?.profile?.thresholds?.minimumRoomR || 1.5"
                    :title="row.name || row.symbol"
                    compact
                  />
                  <NextSessionPlan :plan="row.plan.nextSessionPlan" />
                  <p v-if="row.plan?.levels?.riskBudgetExceeded" class="discipline-risk-warning">
                    止损价距离较远，先减少仓位；不要把止损价下调。
                  </p>
                </template>
                <footer class="discipline-position-actions">
                  <el-button link :icon="Pencil" @click.stop="openPortfolioDialog(row)">修改</el-button>
                  <el-button link type="primary" :icon="Plus" @click.stop="openTradeDialog('BUY', row)">加仓</el-button>
                  <el-button link type="danger" :icon="ArrowDownToLine" @click.stop="openTradeDialog('SELL', row)">卖出</el-button>
                </footer>
              </article>
            </div>
          </section>

          <aside class="discipline-panel discipline-watchlist-panel">
            <div class="discipline-panel-head discipline-list-panel-head">
              <div class="discipline-panel-title">
                <strong>自选观察</strong>
                <small>{{ watchlistDisplayItems.length }} 只股票{{ disciplineWatchlistSummary.errorCount ? ` / ${disciplineWatchlistSummary.errorCount} 只待补行情` : '' }}</small>
              </div>
              <div class="discipline-panel-actions">
                <el-button plain :icon="RefreshCw" :loading="watchlistAnalysisLoading" title="更新自选的买入条件" @click="analyzeWatchlist">刷新分析</el-button>
                <el-button type="primary" plain :icon="Plus" @click="openWatchlistDialog">添加</el-button>
              </div>
            </div>
            <div v-if="watchlistDisplayItems.length" class="discipline-mobile-stock-tabs" role="tablist" aria-label="手机自选选择">
              <button
                v-for="row in watchlistDisplayItems"
                :key="`watchlist-tab-${row.symbol}`"
                type="button"
                role="tab"
                :aria-selected="isSelectedWatchlist(row)"
                :class="['discipline-mobile-stock-tab', isSelectedWatchlist(row) ? 'is-active' : '']"
                @click="selectedWatchlistSymbol = row.symbol"
              >
                <span>{{ row.name || row.symbol }}</span>
                <strong :class="profitClass(row.pctChange)">{{ formatPercent(row.pctChange) }}</strong>
              </button>
            </div>
            <div v-loading="watchlistLoading || watchlistAnalysisLoading" class="discipline-watchlist-list" element-loading-text="正在更新自选观察...">
              <p v-if="!watchlistLoading && !watchlistDisplayItems.length" class="discipline-list-empty">暂无自选股票</p>
              <article v-for="row in watchlistDisplayItems" :key="row.symbol" :class="['discipline-watchlist-card', isSelectedWatchlist(row) ? 'is-mobile-selected' : '']">
                <div
                  class="discipline-card-hit-area"
                  role="button"
                  tabindex="0"
                  :aria-label="`查看 ${row.name || row.symbol} 的个股分析`"
                  @click="openDisciplineStock(row)"
                  @keydown.enter="openDisciplineStock(row)"
                  @keydown.space.prevent="openDisciplineStock(row)"
                >
                  <header>
                    <div class="discipline-stock-cell">
                      <strong>{{ row.name || row.symbol }}</strong>
                      <span>{{ row.symbol }}</span>
                    </div>
                    <span :class="disciplineActionClass(row.plan?.tone)">{{ row.plan?.actionLabel || (row.error ? '分析失败' : '待分析') }}</span>
                  </header>
                  <div class="discipline-watchlist-metrics">
                    <span><small>最新价</small><strong>{{ formatPrice(row.latestPrice) }}</strong></span>
                    <span><small>涨跌幅</small><strong :class="profitClass(row.pctChange)">{{ formatPercent(row.pctChange) }}</strong></span>
                    <span><small>换手率</small><strong>{{ formatPercent(row.turnoverRate) }}</strong></span>
                  </div>
                  <p v-if="row.note" class="discipline-watchlist-note">{{ row.note }}</p>
                  <p v-if="row.error" class="discipline-row-error">{{ row.error }}</p>
                </div>
                <template v-if="!row.error && row.plan">
                  <PriceActionDecision
                    :price-action="row.plan.priceAction"
                    :plan="row.plan"
                    :context="watchlistHasPosition(row) ? 'position' : 'watchlist'"
                    :minimum-reward-risk="row.plan.riskRules?.minimumRewardRisk || row.plan.priceAction?.profile?.thresholds?.minimumRoomR || 1.5"
                    :title="row.name || row.symbol"
                    compact
                  />
                  <NextSessionPlan :plan="row.plan.nextSessionPlan" />
                </template>
                <footer class="discipline-watchlist-actions">
                  <el-button link type="primary" :icon="Plus" @click.stop="openTradeDialog('BUY', row)">买入</el-button>
                  <el-button link type="danger" :icon="Trash2" @click.stop="removeWatchlist(row)">删除</el-button>
                </footer>
              </article>
            </div>
          </aside>
        </div>
        </template>
        <section v-else class="discipline-auth-gate" aria-label="纪律交易账户访问">
          <div class="discipline-auth-gate-icon"><LogIn size="28" /></div>
          <div>
            <h2>登录后查看纪律交易</h2>
            <p>资金、持仓、自选和交易记录仅在当前账户内保存。</p>
          </div>
          <el-button type="primary" :icon="LogIn" @click="openAuthDialog('login')">登录或注册</el-button>
        </section>
      </section>

      <section v-if="activePage === 'index'" class="page-content">
        <div class="sub-nav">
          <button
            v-for="item in indexItems"
            :key="item.symbol"
            type="button"
            :class="['sub-nav-item', activeIndexSymbol === item.symbol ? 'is-active' : '']"
            @click="selectIndex(item.symbol)"
          >
            {{ item.name }}
          </button>
        </div>

        <section class="analysis-stack single-analysis">
          <AnalysisPanel
            :title="activeIndexItem.name"
            :result="indexResult"
            :loading="indexLoading"
            :is-dark="isDark"
            :load-intraday="loadIndexIntraday"
            :empty-text="`正在自动分析${activeIndexItem.name}`"
          >
            <template #info>
              <PanelInfo
                :title="activeIndexItem.name"
                :heading-value="`${activeIndexItem.symbol} - ${activeIndexItem.name}`"
                :summary="indexSummary"
                :result="indexResult"
                :loading="indexLoading"
              />
            </template>
          </AnalysisPanel>
          <section class="detail-wide-section">
            <el-tabs class="detail-tabs" model-value="ai">
              <el-tab-pane label="AI分析" name="ai">
                <AiPanel
                  :report="indexAiReport"
                  :analysis-date="indexResult?.dateRange?.end"
                  :loading="indexAiLoading"
                  :disabled="!indexResult"
                  @run="runAiAnalyze('index')"
                />
              </el-tab-pane>
            </el-tabs>
          </section>
        </section>
      </section>

      <BinanceWorkspace
        v-if="activePage === 'binance' || activePage === 'binanceBacktest' || activePage === 'binanceModelResearch'"
        ref="binanceWorkspaceRef"
        :active-page="activePage"
        :current-user="currentUser"
        @change-page="setActivePage"
        @login-request="openAuthDialog('login')"
      />

      <section v-if="activePage === 'stock'" class="page-content">
        <div class="stock-topbar">
          <section v-if="stockAnalysisHistory.length" class="stock-analysis-history" aria-label="最近分析的股票">
            <div class="stock-analysis-history-head">
              <span>最近分析</span>
            </div>
            <div class="stock-analysis-history-list">
              <div
                v-for="item in stockAnalysisHistory"
                :key="item.symbol"
                class="stock-analysis-history-item"
              >
                <button
                  type="button"
                  class="stock-history-select"
                  :title="stockLabel(item.symbol, item.name)"
                  @click="selectStockHistory(item)"
                >
                  <strong>{{ item.name || '名称待同步' }}</strong>
                  <span>{{ item.symbol }}</span>
                </button>
                <button
                  type="button"
                  class="stock-history-remove"
                  :aria-label="`删除 ${item.name || item.symbol} 的分析记录`"
                  :title="`删除 ${item.name || item.symbol} 的分析记录`"
                  @click.stop="removeStockHistory(item)"
                >
                  <X :size="12" :stroke-width="2.4" />
                </button>
              </div>
            </div>
          </section>

          <div class="stock-search-block">
            <el-autocomplete
              v-model="keyword"
              class="stock-search-input"
              :fetch-suggestions="querySearch"
              value-key="label"
              clearable
              :debounce="0"
              size="large"
              placeholder="股票代码、名称或拼音"
              @select="handleSelect"
            >
              <template #default="{ item }">
                <div class="suggestion-row">
                  <span>{{ item.symbol }}</span>
                  <strong>{{ item.name || '名称待同步' }}</strong>
                </div>
              </template>
            </el-autocomplete>
          </div>

          <el-button
            class="stock-topbar-btn stock-analyze-btn"
            type="primary"
            size="large"
            :icon="Activity"
            :loading="stockLoading"
            @click="runAnalyze"
          >
            分析个股
          </el-button>

          <el-button
            class="stock-topbar-btn stock-find-btn"
            type="success"
            size="large"
            :icon="TrendingUp"
            :loading="findLoading"
            @click="runFindMainRiseStock"
          >
            寻找主升
          </el-button>

          <el-button
            class="stock-topbar-btn stock-golden-btn golden-find-btn"
            type="warning"
            size="large"
            :icon="Target"
            :loading="goldenLoading"
            @click="runFindGoldenPillarStock"
          >
            寻找黄金柱
          </el-button>

          <div class="stock-price-range">
            <span>股价</span>
            <el-input-number
              v-model="findMinPrice"
              :min="0"
              :max="300"
              :step="1"
              :precision="2"
              controls-position="right"
              size="large"
              placeholder="最低"
            />
            <span>至</span>
            <el-input-number
              v-model="findMaxPrice"
              :min="1"
              :max="300"
              :step="1"
              :precision="2"
              controls-position="right"
              size="large"
              placeholder="最高"
            />
          </div>
        </div>

        <section class="analysis-stack single-analysis">
          <AnalysisPanel
            :title="selectedLabel || '个股分析'"
            :result="stockResult"
            :discipline-plan="disciplineChartPlan"
            :loading="stockLoading"
            :is-dark="isDark"
            :load-intraday="loadStockIntraday"
            empty-text="输入股票后查看个股价格行为图表"
          >
            <template #info>
              <section class="side-panel">
                <PanelInfo
                  title="个股分析"
                  :heading-value="selectedLabel || keyword || '等待输入'"
                  :summary="stockSummary"
                  :result="stockResult"
                  :loading="stockLoading"
                />

                <section v-if="stockResult?.goldenPillarMeta" class="golden-meta-card">
                  <div class="golden-meta-head">
                    <span>黄金柱待观察</span>
                    <strong>{{ stockResult.goldenPillarMeta.status }}</strong>
                  </div>
                  <div class="golden-meta-grid">
                    <div>
                      <span>支撑横线</span>
                      <strong>{{ formatMetaPrice(stockResult.goldenPillarMeta.supportPrice) }}</strong>
                    </div>
                    <div>
                      <span>当前日期</span>
                      <strong>{{ stockResult.goldenPillarMeta.latestDate }}</strong>
                    </div>
                    <div>
                      <span>首日量比</span>
                      <strong>{{ stockResult.goldenPillarMeta.volumeRatio }}</strong>
                    </div>
                    <div>
                      <span>后续缩量</span>
                      <strong>{{ stockResult.goldenPillarMeta.avgShrinkRatio }}</strong>
                    </div>
                  </div>
                  <p>{{ stockResult.goldenPillarMeta.reason }}</p>
                  <p>{{ stockResult.goldenPillarMeta.notice || '黄金柱仅为量价观察，不构成独立买入建议。' }}</p>
                </section>
              </section>
            </template>
          </AnalysisPanel>
          <section class="detail-wide-section" :class="{ 'detail-wide-section-fundamental': stockDetailTab === 'fundamental' }">
            <el-tabs v-model="stockDetailTab" class="detail-tabs">
              <el-tab-pane label="基本面 F10" name="fundamental">
                <FundamentalInfo
                  :symbol="stockResult?.symbol"
                  :fundamentals="stockResult?.fundamentals"
                  :loading="fundamentalLoading"
                  :refresh="refreshStockFundamentals"
                />
              </el-tab-pane>
              <el-tab-pane label="AI分析" name="ai">
                <AiPanel
                  :report="stockAiReport"
                  :analysis-date="stockResult?.dateRange?.end"
                  :loading="stockAiLoading"
                  :disabled="!stockResult"
                  @run="runAiAnalyze('stock')"
                />
              </el-tab-pane>
            </el-tabs>
          </section>
        </section>
      </section>

      <el-dialog v-model="settingsVisible" title="应用设置" width="520px" class="responsive-dialog ai-settings-dialog" align-center>
        <el-form label-position="top">
          <el-form-item label="OpenAI Base URL">
            <el-input v-model="aiSettings.baseUrl" placeholder="例如 https://api.openai.com/v1" />
          </el-form-item>
          <el-form-item label="API Key">
            <el-input v-model="aiSettings.apiKey" type="password" show-password :placeholder="aiSettings.apiKeyConfigured ? '已配置，留空则保留原 Key' : '请输入账户 API Key'" />
            <small class="ai-settings-secret-status">{{ aiSettings.apiKeyConfigured ? '当前账户已配置 API Key' : '当前账户尚未配置 API Key' }}</small>
          </el-form-item>
          <el-form-item label="模型">
            <el-input v-model="aiSettings.model" placeholder="例如 gpt-4.1-mini" />
          </el-form-item>
          <el-divider content-position="left">邮件通知</el-divider>
          <el-form-item label="提醒邮箱">
            <el-input v-model="notificationSettings.email" type="email" placeholder="留空则关闭 A 股与 Binance 的邮件提醒" @keyup.enter="saveNotificationSettings" />
            <small class="ai-settings-secret-status">A 股与 Binance 共用此邮箱。发件地址和 Resend API Key 只从项目根目录 .env 读取。</small>
          </el-form-item>
          <el-button type="primary" plain :loading="notificationSaving" @click="saveNotificationSettings">保存邮件设置</el-button>
          <el-button plain :loading="testEmailSending" :disabled="!notificationSettings.email" @click="sendTestEmail">发送测试邮件</el-button>
        </el-form>
        <template #footer>
          <el-button @click="settingsVisible = false">取消</el-button>
          <el-button type="primary" @click="saveAiSettings">保存</el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="updateLogVisible" title="更新日志" width="640px" class="responsive-dialog update-log-dialog" align-center>
        <section class="update-log-current" aria-labelledby="current-update-title">
          <div class="update-log-section-head">
            <div>
              <span class="update-log-kicker">当前版本</span>
              <strong id="current-update-title">v{{ currentVersion }} · {{ currentUpdate.title }}</strong>
            </div>
            <time>{{ currentUpdate.date }}</time>
          </div>
          <ul>
            <li v-for="item in currentUpdate.items" :key="item">{{ item }}</li>
          </ul>
        </section>

        <section class="update-log-history" aria-labelledby="update-history-title">
          <div class="update-log-section-head">
            <strong id="update-history-title">历史版本</strong>
            <span>{{ previousUpdates.length }} 个版本</span>
          </div>
          <article v-for="log in previousUpdates" :key="log.version" class="update-log-entry">
            <div class="update-log-entry-head">
              <strong>v{{ log.version }} · {{ log.title }}</strong>
              <time>{{ log.date }}</time>
            </div>
            <ul>
              <li v-for="item in log.items" :key="item">{{ item }}</li>
            </ul>
          </article>
          <p v-if="!previousUpdates.length" class="update-log-empty">暂无更早版本记录</p>
        </section>
      </el-dialog>

      <el-dialog v-model="authDialogVisible" :title="authMode === 'login' ? '登录账户' : '注册账户'" width="420px" class="responsive-dialog auth-dialog" align-center>
        <el-form label-position="top" @submit.prevent>
          <el-form-item label="用户名">
            <el-input v-model="authForm.username" maxlength="32" autocomplete="username" placeholder="2 至 32 个字符" />
          </el-form-item>
          <el-form-item label="密码">
            <el-input v-model="authForm.password" type="password" show-password maxlength="128" autocomplete="current-password" placeholder="8 至 128 个字符" @keyup.enter="submitAuthentication" />
          </el-form-item>
          <p class="auth-dialog-switch">
            <span>{{ authMode === 'login' ? '还没有账户？' : '已有账户？' }}</span>
            <el-button link type="primary" @click="switchAuthMode">{{ authMode === 'login' ? '注册' : '登录' }}</el-button>
          </p>
        </el-form>
        <template #footer>
          <el-button @click="authDialogVisible = false">取消</el-button>
          <el-button type="primary" :icon="authMode === 'login' ? LogIn : CircleUserRound" :loading="authSaving" @click="submitAuthentication">
            {{ authMode === 'login' ? '登录' : '注册' }}
          </el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="portfolioDialogVisible" title="修改持仓信息" width="520px" class="responsive-dialog portfolio-dialog" align-center>
        <el-form label-position="top">
          <el-form-item label="股票代码">
            <el-input v-model="portfolioForm.symbol" disabled />
          </el-form-item>
          <el-form-item label="名称">
            <el-input v-model="portfolioForm.name" placeholder="股票名称" />
          </el-form-item>
          <el-form-item label="账面均摊成本">
            <el-input-number v-model="portfolioForm.costPrice" :min="0" :precision="3" :step="0.1" controls-position="right" />
          </el-form-item>
          <el-form-item label="账面持仓股数">
            <el-input-number v-model="portfolioForm.shares" :min="0" :step="100" :precision="0" controls-position="right" />
          </el-form-item>
          <el-form-item label="备注">
            <el-input v-model="portfolioForm.note" type="textarea" :rows="3" placeholder="可记录买入理由或观察点" />
          </el-form-item>
          <p class="portfolio-dialog-tip">此处用于账面资料修正，不会自动增减可用现金；实际成交请使用持仓表中的“加仓 / 卖出”或右上角“新开仓”。</p>
        </el-form>
        <template #footer>
          <el-button @click="portfolioDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="portfolioSaving" @click="submitPortfolio">保存</el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="watchlistDialogVisible" title="添加自选观察" width="520px" class="responsive-dialog portfolio-dialog watchlist-dialog" align-center>
        <el-form label-position="top">
          <el-form-item label="股票">
            <el-autocomplete
              v-model="watchlistForm.keyword"
              :fetch-suggestions="querySearch"
              value-key="label"
              clearable
              :debounce="0"
              placeholder="输入股票代码或名称"
              @select="handleWatchlistSelect"
            >
              <template #default="{ item }">
                <div class="suggestion-row">
                  <span>{{ item.symbol }}</span>
                  <strong>{{ item.name || '名称待同步' }}</strong>
                </div>
              </template>
            </el-autocomplete>
          </el-form-item>
          <el-form-item label="观察备注">
            <el-input v-model="watchlistForm.note" type="textarea" :rows="3" placeholder="可记录关注的价格行为或条件" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="watchlistDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="watchlistSaving" @click="submitWatchlist">加入观察</el-button>
        </template>
      </el-dialog>

      <el-dialog v-model="tradeDialogVisible" :title="tradeForm.action === 'BUY' ? '按价买入并更新均摊成本' : '按价卖出并释放现金'" width="560px" class="responsive-dialog portfolio-dialog trade-dialog" align-center>
        <el-form label-position="top">
          <el-form-item label="股票">
            <el-autocomplete
              v-model="tradeForm.keyword"
              :fetch-suggestions="querySearch"
              value-key="label"
              clearable
              :disabled="tradeForm.lockedSymbol"
              :debounce="0"
              placeholder="输入股票代码或名称"
              @select="handleTradeSelect"
            >
              <template #default="{ item }">
                <div class="suggestion-row">
                  <span>{{ item.symbol }}</span>
                  <strong>{{ item.name || '名称待同步' }}</strong>
                </div>
              </template>
            </el-autocomplete>
          </el-form-item>
          <div class="trade-form-grid">
            <el-form-item label="成交价格">
              <el-input-number v-model="tradeForm.price" :min="0" :precision="3" :step="0.1" controls-position="right" />
            </el-form-item>
            <el-form-item label="成交股数">
              <el-input-number v-model="tradeForm.shares" :min="0" :step="100" :precision="0" controls-position="right" />
            </el-form-item>
            <el-form-item :label="tradeFeeManuallyEdited ? '手续费（手动）' : '手续费（默认）'">
              <el-input-number v-model="tradeForm.fee" :min="0" :precision="2" :step="1" controls-position="right" @change="handleTradeFeeChange" />
            </el-form-item>
          </div>
          <section class="trade-fee-breakdown" aria-label="默认手续费构成">
            <div class="trade-fee-breakdown-head">
              <span>默认手续费构成（{{ tradeDefaultFee.exchange === 'SH' ? '沪交所' : '深交所' }}）</span>
              <strong>{{ formatMoney(tradeDefaultFee.total) }}</strong>
            </div>
            <div class="trade-fee-breakdown-items">
              <div><span>佣金</span><strong>{{ formatMoney(tradeDefaultFee.commission) }}</strong></div>
              <div><span>印花税</span><strong>{{ formatMoney(tradeDefaultFee.stampDuty) }}</strong></div>
              <div><span>其他费用</span><strong>{{ formatMoney(tradeDefaultFee.otherFees) }}</strong></div>
            </div>
            <small v-if="tradeFeeManuallyEdited">手续费已手动修改，预计现金将按当前输入值计算。</small>
          </section>
          <section class="trade-preview">
            <div><span>成交金额</span><strong>{{ formatMoney(tradeGrossAmount) }}</strong></div>
            <div><span>预计现金</span><strong :class="tradeCashAfter < 0 ? 'profit-negative' : ''">{{ formatMoney(tradeCashAfter) }}</strong></div>
            <div v-if="tradeForm.action === 'BUY'"><span>成交后均摊成本</span><strong>{{ formatPrice(tradeAverageCostAfter) }}</strong></div>
            <div v-else><span>成交后剩余股数</span><strong>{{ tradeSharesAfter }}</strong></div>
          </section>
          <el-form-item label="成交备注">
            <el-input v-model="tradeForm.note" type="textarea" :rows="3" placeholder="记录交易依据、点位或执行偏差" />
          </el-form-item>
          <p class="portfolio-dialog-tip">买入必须为 100 股的整数倍，且成交额加手续费不得超过可用现金。低于均摊成本的加仓会要求二次确认并在账本标注为未按纪律执行；卖出数量按可用数量校验。</p>
        </el-form>
        <template #footer>
          <el-button @click="tradeDialogVisible = false">取消</el-button>
          <el-button :type="tradeForm.action === 'BUY' ? 'primary' : 'danger'" :loading="tradeSaving" @click="submitTrade">
            确认{{ tradeForm.action === 'BUY' ? '买入' : '卖出' }}
          </el-button>
        </template>
      </el-dialog>
    </section>

    <nav :class="['mobile-bottom-nav', isBinanceWorkspace ? 'is-binance-nav' : '']" aria-label="主导航">
      <button
        v-for="item in sidebarNavItems"
        :key="item.value"
        type="button"
        :class="['mobile-bottom-nav-item', activePage === item.value ? 'is-active' : '']"
        :aria-current="activePage === item.value ? 'page' : undefined"
        @click="setActivePage(item.value)"
      >
        <component :is="item.icon" size="19" />
        <span>{{ item.label }}</span>
      </button>
    </nav>
  </main>
</template>

<script setup>
import { computed, defineAsyncComponent, defineComponent, h, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import ElButton from 'element-plus/es/components/button/index.mjs'
import ElMessage from 'element-plus/es/components/message/index.mjs'
import ElMessageBox from 'element-plus/es/components/message-box/index.mjs'
import { Activity, ArrowDownToLine, BarChart3, Bitcoin, BrainCircuit, CircleUserRound, Coins, Gauge, History, Home, LineChart, LogIn, LogOut, Moon, Pencil, Plus, RefreshCw, Save, Search, Settings, ShieldCheck, Sparkles, Sun, Target, Trash2, TrendingDown, TrendingUp, X } from 'lucide-vue-next'
import { pinyin } from 'pinyin-pro'
import NextSessionPlan from './components/NextSessionPlan.vue'
import PriceActionDecision from './components/PriceActionDecision.vue'
import {
  analyzeAi,
  analyzeIndex,
  analyzeIndexIntraday,
  analyzeStock,
  analyzeStockIntraday,
  clearAuthToken,
  fetchAiSettings as fetchAccountAiSettings,
  fetchNotificationSettings,
  deleteWatchlistItem,
  deleteStockAnalysisHistory,
  executePortfolioTrade,
  fetchFundamentals,
  fetchCurrentUser,
  clearStoredAuthUser,
  getStoredAuthUser,
  fetchDisciplinePortfolio,
  fetchDisciplineWatchlist,
  fetchMarketToday,
  refreshMarketData,
  fetchPortfolio,
  fetchPortfolioTrades,
  fetchStockAnalysisHistory,
  fetchWatchlist,
  fetchStocks,
  fetchUpdateLog,
  findGoldenPillarStock,
  findMainRiseStock,
  hasAuthToken,
  loginAccount,
  logoutAccount,
  registerAccount,
  savePortfolioPosition,
  savePortfolioCash,
  saveAiSettings as saveAccountAiSettings,
  saveNotificationSettings as requestSaveNotificationSettings,
  sendTestEmail as requestTestEmail,
  saveStockAnalysisHistory,
  saveWatchlistItem,
  setAuthFailureHandler,
  setStoredAuthUser,
  setAuthToken,
} from './api'

// The Binance workspace contains the charting and backtest dependencies. Keep
// it out of the initial home bundle until the user actually opens that page.
const BinanceWorkspace = defineAsyncComponent({
  loader: () => import('./components/BinanceWorkspace.vue'),
  delay: 80,
  timeout: 30000,
  loadingComponent: defineComponent({
    setup: () => () => h('section', { class: 'page-content workspace-loading', role: 'status' }, '正在加载 Binance 工作区...'),
  }),
  errorComponent: defineComponent({
    setup: () => () => h('section', { class: 'page-content workspace-loading is-error', role: 'alert' }, 'Binance 工作区暂时无法加载，请稍后重试。'),
  }),
})

const AnalysisPanel = defineAsyncComponent({
  loader: () => import('./components/AnalysisPanel.vue'),
  delay: 80,
  timeout: 30000,
  loadingComponent: defineComponent({
    setup: () => () => h('section', { class: 'page-content workspace-loading', role: 'status' }, '正在加载价格行为图表...'),
  }),
  errorComponent: defineComponent({
    setup: () => () => h('section', { class: 'page-content workspace-loading is-error', role: 'alert' }, '价格行为图表暂时无法加载，请稍后重试。'),
  }),
})

const keyword = ref('')
const PAGE_PATHS = Object.freeze({ home: '/', discipline: '/discipline', index: '/index', stock: '/stock', binance: '/binance', binanceBacktest: '/binance/backtest', binanceModelResearch: '/binance/models' })
const pageFromPath = () => {
  const pathname = String(window.location.pathname || '/').replace(/\/$/, '') || '/'
  return Object.entries(PAGE_PATHS).find(([, path]) => path === pathname || (path !== '/' && pathname.endsWith(path)))?.[0] || 'home'
}
const activePage = ref(pageFromPath())
const currentUser = ref(hasAuthToken() ? getStoredAuthUser() : null)
const binanceWorkspaceRef = ref(null)
const authRestoring = ref(hasAuthToken())
const authDialogVisible = ref(false)
const authMode = ref('login')
const authSaving = ref(false)
const authForm = ref({ username: '', password: '' })
let authRestoreRetryTimer = 0
let authRestoreAttempts = 0
const updateLogs = ref([])
const currentUpdate = computed(() => updateLogs.value[0] || {
  version: '',
  date: '',
  title: '更新日志加载中',
  items: [],
})
const currentVersion = computed(() => currentUpdate.value.version)
const previousUpdates = computed(() => updateLogs.value.slice(1))
const activeIndexSymbol = ref('000001')
const selectedStock = ref(null)
const selectedLabel = ref('')
const stockLoading = ref(false)
const findLoading = ref(false)
const goldenLoading = ref(false)
const indexLoading = ref(false)
const stocksLoading = ref(false)
const findMinPrice = ref(0)
const findMaxPrice = ref(30)
const stockOptions = ref([])

const isUsableStockName = (value, symbol = '') => {
  const name = String(value || '').trim()
  const normalizedSymbol = String(symbol || '').trim()
  return Boolean(name)
    && !['代码直查', normalizedSymbol, '--', '-'].includes(name)
    && !/^\d+$/.test(name)
}

const stockLabel = (symbol, name) => {
  const normalizedSymbol = String(symbol || '').trim()
  const resolvedName = isUsableStockName(name, normalizedSymbol) ? String(name).trim() : ''
  return resolvedName ? `${normalizedSymbol} - ${resolvedName}` : normalizedSymbol
}

const resolveStockName = (symbol, ...candidates) => {
  const normalizedSymbol = String(symbol || '').trim()
  for (const candidate of candidates) {
    const name = candidate && typeof candidate === 'object' ? candidate.name : candidate
    if (isUsableStockName(name, normalizedSymbol)) return String(name).trim()
  }
  const matched = stockOptions.value.find((item) => String(item?.symbol || '').trim() === normalizedSymbol)
  return isUsableStockName(matched?.name, normalizedSymbol) ? String(matched.name).trim() : ''
}

const normalizeStockIdentity = (item = {}, fallback = {}) => {
  const source = item && typeof item === 'object' ? item : {}
  const identityFallback = fallback && typeof fallback === 'object' ? fallback : {}
  const symbol = String(source.symbol || identityFallback.symbol || '').trim()
  const name = resolveStockName(symbol, identityFallback, source)
  return {
    ...identityFallback,
    ...source,
    symbol,
    name,
    label: stockLabel(symbol, name),
  }
}

const stockAnalysisHistory = ref([])
const stockResult = ref(null)
const fundamentalLoading = ref(false)
const disciplineChartPlan = ref(null)
const indexResult = ref(null)
const marketToday = ref(null)
const marketLoading = ref(false)
const marketSync = ref(null)
const portfolioMarketData = ref(null)
let marketSyncInFlight = null
const portfolioLoading = ref(false)
const portfolioSaving = ref(false)
const portfolioItems = ref([])
const portfolioSummary = ref({})
const portfolioDialogVisible = ref(false)
const portfolioEditing = ref(false)
const portfolioForm = ref({
  symbol: '',
  name: '',
  costPrice: 0,
  shares: 0,
  note: '',
})
const disciplinePortfolioItems = ref([])
const disciplinePortfolioSummary = ref({ positionCount: 0, actionCounts: {}, errorCount: 0, plannedRisk: 0 })
const disciplinePortfolioLoading = ref(false)
const selectedHoldingSymbol = ref('')
const watchlistItems = ref([])
const disciplineWatchlistItems = ref([])
const disciplineWatchlistSummary = ref({ watchCount: 0, actionCounts: {}, errorCount: 0 })
const watchlistLoading = ref(false)
const watchlistAnalysisLoading = ref(false)
const selectedWatchlistSymbol = ref('')
const watchlistSaving = ref(false)
const watchlistDialogVisible = ref(false)
const watchlistForm = ref({
  keyword: '',
  symbol: '',
  name: '',
  note: '',
})
const cashBalanceDraft = ref(100000)
const cashSaving = ref(false)
const tradeDialogVisible = ref(false)
const tradeSaving = ref(false)
const tradeHistoryLoading = ref(false)
const tradeHistory = ref([])
const tradeFeeManuallyEdited = ref(false)
const tradeForm = ref({
  action: 'BUY',
  keyword: '',
  symbol: '',
  name: '',
  price: 0,
  shares: 100,
  fee: 0,
  note: '',
  lockedSymbol: false,
})
const settingsVisible = ref(false)
const updateLogVisible = ref(false)
const isDark = ref(document.documentElement.classList.contains('dark'))
const homePageRef = ref(null)
const homeGridRef = ref(null)
const homeGridHeight = ref('')
const disciplinePageRef = ref(null)
const disciplineWorkspaceRef = ref(null)
const disciplineWorkspaceHeight = ref('')
let disciplineWorkspaceFrame = 0
let viewportWorkspaceResizeObserver = null

const toggleTheme = () => {
  isDark.value = !isDark.value
  document.documentElement.classList.toggle('dark', isDark.value)
  try { localStorage.setItem('jiaren-theme', isDark.value ? 'dark' : 'light') } catch { /* ignore */ }
}
const indexAiLoading = ref(false)
const stockAiLoading = ref(false)
const indexAiReport = ref(null)
const stockAiReport = ref(null)
const stockDetailTab = ref('fundamental')

/* --- 分析进度：后端接口是同步请求，因此只展示可确认的请求状态 --- */
const emptyAnalysisProgress = () => ({
  visible: false,
  page: '',
  title: '',
  message: '',
  steps: [],
  dismissTimer: null,
})

const STOCK_ANALYSIS_STEPS = [
  { label: '分析请求', detail: '正在请求个股分析服务，等待行情、指标、结构和基本面数据...' },
]

const INDEX_ANALYSIS_STEPS = [
  { label: '分析请求', detail: '正在请求指数分析服务，等待日 K、指标、结构和暴露评估数据...' },
]

const MAIN_RISE_STEPS = [
  { label: '逐只检查', detail: '正在逐只随机检查候选，最多检查 30 只，命中主升条件立即停止...' },
]

const GOLDEN_PILLAR_STEPS = [
  { label: '筛选请求', detail: '正在请求黄金柱筛选服务，后端正在检查量价结构和支撑横线...' },
]

const analysisProgress = ref(emptyAnalysisProgress())
const DEFAULT_AI_SETTINGS = {
  baseUrl: 'https://api.openai.com/v1',
  apiKey: '',
  model: 'gpt-4.1-mini',
  apiKeyConfigured: false,
}
const aiSettings = ref({ ...DEFAULT_AI_SETTINGS })
const notificationSettings = ref({ email: '' })
const notificationSaving = ref(false)
const testEmailSending = ref(false)

const mainNavItems = [
  { label: '首页', value: 'home', icon: Home },
  { label: '纪律交易', value: 'discipline', icon: ShieldCheck },
  { label: '指数分析', value: 'index', icon: BarChart3 },
  { label: '个股分析', value: 'stock', icon: LineChart },
]

const binanceNavItems = [
  { label: '纪律交易', value: 'binance', icon: ShieldCheck },
  { label: '回测复盘', value: 'binanceBacktest', icon: Activity },
  { label: '模型研究', value: 'binanceModelResearch', icon: BrainCircuit },
]

const STOCK_WORKSPACE_PAGES = new Set(['home', 'discipline', 'index', 'stock'])
const STOCK_SEARCH_PAGES = new Set(['discipline', 'stock'])
const isStockWorkspacePage = (page = activePage.value) => STOCK_WORKSPACE_PAGES.has(page)
const isStockSearchPage = (page = activePage.value) => STOCK_SEARCH_PAGES.has(page)
const isBinanceWorkspacePage = (page = activePage.value) => ['binance', 'binanceBacktest', 'binanceModelResearch'].includes(page)
const isBinanceWorkspace = computed(() => isBinanceWorkspacePage())
const sidebarNavItems = computed(() => (isBinanceWorkspace.value ? binanceNavItems : mainNavItems))
const sidebarLabel = computed(() => (isBinanceWorkspace.value ? 'Binance' : '分页切换'))
const switchPrimaryWorkspace = () => {
  setActivePage(isBinanceWorkspace.value ? 'home' : 'binance')
}

const setActivePage = (page) => {
  activePage.value = page
  const nextPath = PAGE_PATHS[page] || PAGE_PATHS.home
  if (window.location.pathname !== nextPath) window.history.pushState({ page }, '', nextPath)
  window.requestAnimationFrame(() => {
    window.scrollTo({ top: 0, left: 0, behavior: 'auto' })
  })
}

const handleBrowserRoute = () => {
  activePage.value = pageFromPath()
  window.scrollTo({ top: 0, left: 0, behavior: 'auto' })
}

const disciplineWorkspaceStyle = computed(() => (
  disciplineWorkspaceHeight.value ? { '--discipline-workspace-height': disciplineWorkspaceHeight.value } : {}
))

const homeGridStyle = computed(() => (
  homeGridHeight.value ? { '--home-grid-height': homeGridHeight.value } : {}
))

const updateHomeGridHeight = () => {
  const grid = homeGridRef.value
  if (!grid || activePage.value !== 'home' || window.matchMedia('(max-width: 1200px)').matches) {
    homeGridHeight.value = ''
    return
  }

  const pageTop = grid.getBoundingClientRect().top + window.scrollY
  const viewportHeight = window.visualViewport?.height || window.innerHeight
  const nextHeight = `${Math.max(0, Math.floor(viewportHeight - pageTop - 24))}px`
  if (homeGridHeight.value !== nextHeight) homeGridHeight.value = nextHeight
}

const updateDisciplineWorkspaceHeight = () => {
  const workspace = disciplineWorkspaceRef.value
  if (!workspace || activePage.value !== 'discipline' || window.matchMedia('(max-width: 820px)').matches) {
    disciplineWorkspaceHeight.value = ''
    return
  }

  const pageTop = workspace.getBoundingClientRect().top + window.scrollY
  const viewportHeight = window.visualViewport?.height || window.innerHeight
  const bottomGap = 24
  const availableHeight = Math.max(0, Math.floor(viewportHeight - pageTop - bottomGap))
  const nextHeight = `${availableHeight}px`
  if (disciplineWorkspaceHeight.value !== nextHeight) disciplineWorkspaceHeight.value = nextHeight
}

const scheduleDisciplineWorkspaceHeight = () => {
  if (disciplineWorkspaceFrame) window.cancelAnimationFrame(disciplineWorkspaceFrame)
  disciplineWorkspaceFrame = window.requestAnimationFrame(() => {
    disciplineWorkspaceFrame = 0
    updateHomeGridHeight()
    updateDisciplineWorkspaceHeight()
  })
}

const resetPrivateWorkspace = () => {
  portfolioMarketData.value = null
  portfolioItems.value = []
  portfolioSummary.value = {}
  disciplinePortfolioItems.value = []
  disciplinePortfolioSummary.value = { positionCount: 0, actionCounts: {}, errorCount: 0, plannedRisk: 0 }
  selectedHoldingSymbol.value = ''
  watchlistItems.value = []
  disciplineWatchlistItems.value = []
  disciplineWatchlistSummary.value = { watchCount: 0, actionCounts: {}, errorCount: 0 }
  selectedWatchlistSymbol.value = ''
  tradeHistory.value = []
  stockAnalysisHistory.value = []
  cashBalanceDraft.value = 100000
  portfolioDialogVisible.value = false
  watchlistDialogVisible.value = false
  tradeDialogVisible.value = false
}

const resetAiSettings = () => {
  aiSettings.value = { ...DEFAULT_AI_SETTINGS }
  notificationSettings.value = { email: '' }
  notificationSaving.value = false
  settingsVisible.value = false
}

const handleAuthenticationExpired = () => {
  const wasAuthenticated = Boolean(currentUser.value)
  if (authRestoreRetryTimer) window.clearTimeout(authRestoreRetryTimer)
  authRestoreRetryTimer = 0
  authRestoreAttempts = 0
  clearStoredAuthUser()
  currentUser.value = null
  resetPrivateWorkspace()
  resetAiSettings()
  if (isStockWorkspacePage()) void loadStockAnalysisHistory()
  scheduleDisciplineWorkspaceHeight()
  if (wasAuthenticated) ElMessage.warning('登录状态已失效，请重新登录')
}

const openAuthDialog = (mode = 'login') => {
  authMode.value = mode === 'register' ? 'register' : 'login'
  authForm.value = { username: '', password: '' }
  authDialogVisible.value = true
}

const switchAuthMode = () => {
  authMode.value = authMode.value === 'login' ? 'register' : 'login'
  authForm.value.password = ''
}

const completeAuthentication = async (payload) => {
  if (!payload?.token || !payload?.user) throw new Error('登录会话创建失败')
  setAuthToken(payload.token)
  setStoredAuthUser(payload.user)
  currentUser.value = payload.user
  if (isStockWorkspacePage()) {
    await loadAiSettings({ migrateLegacy: true })
    if (activePage.value === 'stock') await loadStockAnalysisHistory()
  }
  authDialogVisible.value = false
  authForm.value = { username: '', password: '' }
  ElMessage.success(authMode.value === 'login' ? '已登录账户' : '账户已注册')
  if (activePage.value === 'discipline') await refreshDisciplineWorkspace()
}

const submitAuthentication = async () => {
  const username = authForm.value.username.trim()
  const password = authForm.value.password
  if (username.length < 2) {
    ElMessage.warning('用户名至少需要 2 个字符')
    return
  }
  if (password.length < 8) {
    ElMessage.warning('密码至少需要 8 个字符')
    return
  }
  authSaving.value = true
  try {
    const payload = authMode.value === 'login'
      ? await loginAccount({ username, password })
      : await registerAccount({ username, password })
    await completeAuthentication(payload)
  } catch (error) {
    ElMessage.error(error.response?.data?.message || error.message || '账户操作失败')
  } finally {
    authSaving.value = false
  }
}

const restoreAuthentication = async () => {
  if (!hasAuthToken()) {
    authRestoring.value = false
    return
  }
  authRestoring.value = true
  authRestoreAttempts += 1
  try {
    const restoredUser = await fetchCurrentUser()
    if (!restoredUser) throw new Error('登录会话未返回账户信息')
    currentUser.value = restoredUser
    setStoredAuthUser(restoredUser)
    authRestoreAttempts = 0
    if (isStockWorkspacePage()) {
      await loadAiSettings({ migrateLegacy: true })
      if (activePage.value === 'stock') await loadStockAnalysisHistory()
    }
    if (activePage.value === 'discipline') await refreshDisciplineWorkspace()
  } catch (error) {
    if (error.response?.status !== 401 && hasAuthToken() && authRestoreAttempts < 3) {
      if (authRestoreRetryTimer) window.clearTimeout(authRestoreRetryTimer)
      authRestoreRetryTimer = window.setTimeout(() => {
        authRestoreRetryTimer = 0
        void restoreAuthentication()
      }, 2000)
    } else if (error.response?.status !== 401) {
      ElMessage.warning(error.response?.data?.message || '登录状态恢复失败，已保留本机登录状态')
    }
  } finally {
    if (!authRestoreRetryTimer) authRestoring.value = false
  }
}

const signOut = async () => {
  try {
    if (hasAuthToken()) await logoutAccount()
  } catch (error) {
    if (error.response?.status !== 401) ElMessage.warning(error.response?.data?.message || '退出请求未完成，已清除本机登录状态')
  } finally {
    clearAuthToken()
    clearStoredAuthUser()
    currentUser.value = null
    resetPrivateWorkspace()
    resetAiSettings()
    if (isStockWorkspacePage()) await loadStockAnalysisHistory()
    scheduleDisciplineWorkspaceHeight()
    ElMessage.success('已退出账户')
  }
}

const indexItems = [
  { symbol: '000001', name: '上证指数' },
  { symbol: '399001', name: '深证成指' },
  { symbol: '399006', name: '创业板指' },
  { symbol: '000300', name: '沪深300' },
  { symbol: '000905', name: '中证500' },
  { symbol: '000852', name: '中证1000' },
  { symbol: '000688', name: '科创50' },
]
const indexResults = ref({})

const activeIndexItem = computed(() => indexItems.find((item) => item.symbol === activeIndexSymbol.value) || indexItems[0])
/* 主要指数：始终展示全部指数项，行情快照按代码合并（缺数据的先显示 --） */
const homeIndices = computed(() => {
  const quotes = marketToday.value?.indices || []
  const bySymbol = new Map(quotes.map((item) => [item.symbol, item]))
  return indexItems.map((item) => ({ ...item, ...(bySymbol.get(item.symbol) || {}) }))
})
const homeRankingOptions = [
  { label: '涨幅', value: 'gainers' },
  { label: '跌幅', value: 'losers' },
  { label: '成交额', value: 'amounts' },
  { label: '换手率', value: 'turnover' },
]
const homeRankingMode = ref('gainers')
const includeGrowthBoards = ref(false)
const rankingFilterLoading = ref(false)
const homeRankingTitle = computed(() => homeRankingOptions.find((item) => item.value === homeRankingMode.value)?.label || '涨幅')
const homeRankingIcon = computed(() => ({
  gainers: TrendingUp,
  losers: TrendingDown,
  amounts: Coins,
  turnover: Activity,
}[homeRankingMode.value] || TrendingUp))
const homeRankingItems = computed(() => ({
  gainers: marketToday.value?.topGainers,
  losers: marketToday.value?.topLosers,
  amounts: marketToday.value?.topAmounts,
  turnover: marketToday.value?.topTurnover,
}[homeRankingMode.value] || []))
const homeRankingValue = (item) => {
  if (homeRankingMode.value === 'amounts') return formatMoney(item.amount)
  if (homeRankingMode.value === 'turnover') return formatRate(item.turnoverRate)
  return formatPercent(item.pctChange)
}
const homeRankingValueClass = (item) => {
  if (homeRankingMode.value === 'gainers') return 'profit-positive'
  if (homeRankingMode.value === 'losers') return 'profit-negative'
  return item.pctChange > 0 ? 'profit-positive' : item.pctChange < 0 ? 'profit-negative' : ''
}
/* 涨跌平分布：上涨左、平盘中、下跌右，按占比分段 */
const marketBreadth = computed(() => {
  const up = Number(marketToday.value?.upCount || 0)
  const down = Number(marketToday.value?.downCount || 0)
  const flat = Number(marketToday.value?.flatCount || 0)
  const total = up + down + flat
  if (!total) return { up: 0, flat: 0, down: 0, total: 0, upPct: 0, flatPct: 0, downPct: 0 }
  return {
    up,
    flat,
    down,
    total,
    upPct: (up / total) * 100,
    flatPct: (flat / total) * 100,
    downPct: (down / total) * 100,
  }
})
/* 成交额与上一交易日的差额 */
const marketAmountDiff = computed(() => {
  const total = Number(marketToday.value?.totalAmount || 0)
  const prev = Number(marketToday.value?.prevTotalAmount || 0)
  if (!prev) return null
  return total - prev
})
const marketAmountDiffText = computed(() => {
  const diff = marketAmountDiff.value
  if (diff === null) return ''
  if (diff === 0) return '0'
  const sign = diff > 0 ? '+' : '-'
  const abs = Math.abs(diff)
  if (abs >= 100000000) return `${sign}${(abs / 100000000).toFixed(1)}亿`
  if (abs >= 10000) return `${sign}${(abs / 10000).toFixed(1)}万`
  return `${sign}${abs.toFixed(0)}`
})
const marketAmountDiffClass = computed(() => {
  const diff = marketAmountDiff.value
  if (diff === null) return ''
  return diff > 0 ? 'profit-positive' : diff < 0 ? 'profit-negative' : ''
})
/* 分段采用绝对定位（top:0 / bottom:0），任何浏览器下三段都贴顶贴底、无缝衔接 */
const marketBreadthSegmentStyle = computed(() => {
  const { upPct, flatPct, downPct } = marketBreadth.value
  return {
    up: { width: `${upPct}%` },
    flat: { width: `${flatPct}%` },
    down: { width: `${downPct}%` },
  }
})
/* 实际时间：日期 + 当前具体时间；已收盘时括号内显示“收盘” */
const marketActualTimeText = ref('--')
const tickMarketActualTime = () => {
  const now = new Date()
  const pad = (value) => String(value).padStart(2, '0')
  const date = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
  const status = marketQuoteText(marketToday.value?.quoteMinute)
  const detail = status === '已收盘' || !status || status === '--'
    ? '收盘'
    : `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`
  marketActualTimeText.value = `${date} ${detail}`
}
let marketActualTimeTimer = null
let homeMarketRefreshTimer = null
let homeMarketRefreshInFlight = null
const HOME_MARKET_REFRESH_INTERVAL = 60 * 1000
const marketDataStatusText = computed(() => {
  const state = marketSync.value || marketToday.value || portfolioMarketData.value || {}
  const quoteMinute = state.quoteMinute || '上次可用快照'
  if (state.error) return `全市场行情同步失败，当前展示 ${quoteMinute} 的本地缓存。`
  return `当前展示 ${quoteMinute} 的本地行情缓存，尚未确认属于最新交易时段。`
})
const marketPhase = computed(() => {
  const state = marketSync.value || marketToday.value || {}
  if (state.error || state.stale) return 'stale'
  if (['morning', 'afternoon'].includes(state.session)) return 'open'
  if (state.session === 'lunch_break') return 'break'
  if (state.session === 'pre_open') return 'pre-open'
  if (['closed', 'after_close'].includes(state.session)) return 'closed'

  const now = new Date()
  const weekday = now.getDay()
  const minutes = now.getHours() * 60 + now.getMinutes()
  if (weekday === 0 || weekday === 6) return 'closed'
  if (minutes < 9 * 60 + 30) return 'pre-open'
  if (minutes < 11 * 60 + 30 || minutes >= 13 * 60) {
    return minutes < 15 * 60 ? 'open' : 'closed'
  }
  return 'break'
})
const marketPhaseClass = computed(() => `is-${marketPhase.value}`)
const marketPhaseLabel = computed(() => ({
  open: '交易中',
  break: '午间休市',
  'pre-open': '未开盘',
  closed: '已收盘',
  stale: '行情过期或同步失败',
}[marketPhase.value] || '行情状态未知'))
const marketQuoteText = (value) => {
  if (value === null || value === undefined || value === '') return '--'
  return String(value)
    .replace(/\bclosed\b/gi, '已收盘')
    .replace(/\bclose\b/gi, '已收盘')
    .replace(/\bopen\b/gi, '交易中')
    .replace(/\bweak\b/gi, '偏弱')
}
const stockSummary = computed(() => stockResult.value?.summary)
const indexSummary = computed(() => indexResult.value?.summary)
const tradePosition = computed(() => portfolioItems.value.find((item) => item.symbol === tradeForm.value.symbol) || null)
const watchlistDisplayItems = computed(() => {
  const analysisBySymbol = new Map(disciplineWatchlistItems.value.map((item) => [item.symbol, item]))
  return watchlistItems.value.map((item) => normalizeStockIdentity(analysisBySymbol.get(item.symbol) || {}, item))
})
const selectedHoldingRow = computed(() => (
  disciplinePortfolioItems.value.find((item) => item.symbol === selectedHoldingSymbol.value)
  || disciplinePortfolioItems.value[0]
  || null
))
const selectedWatchlistRow = computed(() => (
  watchlistDisplayItems.value.find((item) => item.symbol === selectedWatchlistSymbol.value)
  || watchlistDisplayItems.value[0]
  || null
))
const isSelectedHolding = (row) => selectedHoldingRow.value?.symbol === row?.symbol
const isSelectedWatchlist = (row) => selectedWatchlistRow.value?.symbol === row?.symbol
const tradeGrossAmount = computed(() => Math.max(0, Number(tradeForm.value.price || 0) * Number(tradeForm.value.shares || 0)))
const roundFeeToCent = (value) => Math.round((Number(value) + Number.EPSILON) * 100) / 100
const resolveTradeExchange = (symbol) => {
  const symbolText = String(symbol || '').trim()
  const stockCode = symbolText.match(/\d{6}/)?.[0] || symbolText
  return /^[569]/.test(stockCode) ? 'SH' : 'SZ'
}
const estimateDefaultTradeFee = ({ action, symbol, amount }) => {
  const orderAmount = Math.max(0, Number(amount) || 0)
  const exchange = resolveTradeExchange(symbol)
  const isShanghai = exchange === 'SH'
  if (!orderAmount) {
    return { exchange, commission: 0, stampDuty: 0, otherFees: 0, total: 0 }
  }
  const commission = Math.max(roundFeeToCent(orderAmount * 0.0001), 0.3)
  const stampDuty = action === 'SELL' ? roundFeeToCent(orderAmount * 0.0005) : 0
  const otherFees = isShanghai ? 0 : roundFeeToCent(orderAmount * 0.00001)
  return {
    exchange,
    commission,
    stampDuty,
    otherFees,
    total: roundFeeToCent(commission + stampDuty + otherFees),
  }
}
const tradeDefaultFee = computed(() => estimateDefaultTradeFee({
  action: tradeForm.value.action,
  symbol: tradeForm.value.symbol,
  amount: tradeGrossAmount.value,
}))
const tradeNeedsDisciplineConfirmation = computed(() => (
  tradeForm.value.action === 'BUY'
  && Boolean(tradePosition.value)
  && Number(tradeForm.value.price || 0) <= Number(tradePosition.value?.costPrice || 0)
))
const tradeCashAfter = computed(() => {
  const cash = Number(portfolioSummary.value.cashBalance || 0)
  const fee = Number(tradeForm.value.fee || 0)
  return tradeForm.value.action === 'BUY' ? cash - tradeGrossAmount.value - fee : cash + tradeGrossAmount.value - fee
})
const tradeSharesAfter = computed(() => {
  const before = Number(tradePosition.value?.shares || 0)
  const shares = Number(tradeForm.value.shares || 0)
  return tradeForm.value.action === 'BUY' ? before + shares : Math.max(0, before - shares)
})
const tradeAverageCostAfter = computed(() => {
  const beforeShares = Number(tradePosition.value?.shares || 0)
  const beforeCost = Number(tradePosition.value?.costPrice || 0)
  const shares = Number(tradeForm.value.shares || 0)
  const totalShares = beforeShares + shares
  if (!totalShares || tradeForm.value.action !== 'BUY') return null
  return ((beforeShares * beforeCost) + tradeGrossAmount.value + Number(tradeForm.value.fee || 0)) / totalShares
})
watch(
  () => [tradeForm.value.action, tradeForm.value.symbol, tradeForm.value.price, tradeForm.value.shares],
  () => {
    if (!tradeFeeManuallyEdited.value) tradeForm.value.fee = tradeDefaultFee.value.total
  },
)
const priceActionSetupLabel = (value) => ({
  TREND_PULLBACK_H1: '上涨后的第一次回踩', TREND_PULLBACK_H2: '上涨后的第二次回踩', TREND_PULLBACK_L1: '下跌后的第一次反弹', TREND_PULLBACK_L2: '下跌后的第二次反弹', TREND_FIRST_PULLBACK: '趋势第一次回踩', TREND_TWO_LEG_PULLBACK: '两次回踩后的再启动', BULL_FLAG: '上涨后的整理', BEAR_FLAG: '下跌后的整理', BREAKOUT_UP: '向上突破', BREAKOUT_DOWN: '向下突破', BREAKOUT_RETEST_UP: '向上突破后回踩', BREAKOUT_RETEST_DOWN: '向下突破后反弹', MICRO_CHANNEL_BREAK: '短线通道被突破', SPIKE_CHANNEL: '快速推进后的通道', WIDE_CHANNEL: '宽幅通道', STEP_CHANNEL: '阶梯式推进', EMA_GAP_CONTEXT: '价格明显离开20周期线',
  RANGE_EDGE_FADE_UP: '区间下沿反弹', RANGE_EDGE_FADE_DOWN: '区间上沿回落', RANGE_LOWER_REVERSAL: '区间下沿反弹', RANGE_UPPER_REVERSAL: '区间上沿回落', RANGE_MIDDLE: '区间中部观望', TIGHT_RANGE_IRON_WIRE: '窄幅反复拉锯', TRIANGLE_COMPRESSION: '波动收窄，等待突破', TRIANGLE_EXPANSION: '波动放大，方向不稳', ABC_RANGE_LEGS: '区间内三段摆动', RANGE_BREAKOUT_PENDING: '突破出现，等待确认', RANGE_BREAKOUT_CONFIRMED: '突破已经站稳', RANGE_BREAKOUT_CONTINUATION: '突破后继续推进', RANGE_BREAKOUT_RETEST: '突破后回踩确认', FAILED_BULLISH_BREAKOUT: '向上突破失败', FAILED_BEARISH_BREAKOUT: '向下突破失败', BREAKOUT_FAILURE_OF_FAILURE: '失败突破后的再测试',
  DOUBLE_BOTTOM: '两次探底', DOUBLE_TOP: '两次冲高', WEDGE_THIRD_PUSH: '第三次冲击，留意衰竭', CLIMAX_SPIKE_REVERSAL: '快速推进后的反转警示', CLIMAX_REVERSAL_CANDIDATE: '高潮后的反转候选', V_REVERSAL_WARNING: '快速反向波动', HEAD_SHOULDERS_REVERSAL: '头肩形反转候选', EXPANSION_REVERSAL: '波动放大后的反转候选', FINAL_FLAG_OR_INSIDE_BREAK: '末端整理后的突破', MAJOR_BEARISH_REVERSAL: '主要向下反转', MAJOR_BULLISH_REVERSAL: '主要向上反转', REVERSAL_FAILURE: '反转尝试失败', REVERSAL_FAILURE_OF_FAILURE: '反转失败后的再测试',
}[value] || '等待结构确认')

const priceActionStatusLabel = (value) => ({
  CONFIRMED: '已确认',
  PENDING: '待确认',
  FAILED: '已失败',
  WATCH: '观察',
  NEEDS_REVIEW: '需复核',
  EXPIRED: '已过期',
  DATA_INSUFFICIENT: '数据不足',
}[value] || '观察')

const watchlistHasPosition = (row) => {
  const context = row?.plan?.positionContext || {}
  return Boolean(context.hasPosition) && Number(context.shares || 0) > 0
}


const formatMetaPrice = (value) => {
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(2) : '--'
}

const formatPrice = (value) => {
  if (value === null || value === undefined || value === '') return '--'
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(2) : '--'
}

const formatMarketTimestamp = (value) => String(value || '').replace('T', ' ') || '--'

const formatPercent = (value) => {
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return `${number > 0 ? '+' : ''}${number.toFixed(2)}%`
}

const formatRate = (value) => {
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return `${number.toFixed(2)}%`
}

const formatMoney = (value) => {
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  const abs = Math.abs(number)
  if (abs >= 100000000) return `${(number / 100000000).toFixed(2)}亿`
  if (abs >= 10000) return `${(number / 10000).toFixed(2)}万`
  return number.toFixed(2)
}

const formatSignedMoney = (value) => {
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return `${number > 0 ? '+' : ''}${formatMoney(number)}`
}

const positionProfitRate = (row = {}) => {
  const cost = Number(row.entryPrice ?? row.costPrice)
  const shares = Number(row.shares)
  const floatingProfit = Number(row.floatingProfit)
  if (![cost, shares, floatingProfit].every(Number.isFinite) || cost <= 0 || shares <= 0) return null
  return floatingProfit / (cost * shares) * 100
}

const marketSourceLabel = (source) => ({
  tushare: 'Tushare',
  eastmoney: '东方财富',
  tencent: '腾讯行情',
  cache: '完整缓存',
  akshare: 'AkShare',
}[String(source || '').toLowerCase()] || (source ? String(source) : '待获取'))

const profitClass = (value) => {
  const number = Number(value)
  if (!Number.isFinite(number) || number === 0) return ''
  return number > 0 ? 'profit-positive' : 'profit-negative'
}

const clearAnalysisProgressTimers = () => {
  const dismissTimer = analysisProgress.value.dismissTimer
  if (dismissTimer) window.clearTimeout(dismissTimer)
}

const setAnalysisProgress = (page, title, steps, message) => {
  if (analysisProgress.value.visible && analysisProgress.value.page !== page && activePage.value !== page) return
  clearAnalysisProgressTimers()
  const scheduled = (Array.isArray(steps) ? steps : []).map((step, index) => ({
    label: step.label,
    detail: step.detail || step.label,
    status: index === 0 ? 'active' : 'pending',
  }))
  analysisProgress.value = {
    visible: true,
    page,
    title,
    message: message || scheduled[0]?.detail || '',
    steps: scheduled,
    dismissTimer: null,
  }
}

const finishAnalysisProgress = (page, message = '分析完成，正在刷新图表') => {
  if (analysisProgress.value.page !== page) return
  clearAnalysisProgressTimers()
  const steps = (analysisProgress.value.steps || []).map((step) => ({ ...step, status: 'done' }))
  const dismissTimer = window.setTimeout(() => {
    analysisProgress.value = emptyAnalysisProgress()
  }, 1100)
  analysisProgress.value = {
    visible: true,
    page,
    title: '完成',
    message,
    steps,
    dismissTimer,
  }
}

const clearAnalysisProgress = (page = '') => {
  if (page && analysisProgress.value.page !== page) return
  clearAnalysisProgressTimers()
  analysisProgress.value = emptyAnalysisProgress()
}

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
    const displayText = (value) => ({
      WEAK: '偏弱', MODERATE: '一般', STRONG: '偏强', VERY_STRONG: '很强', NORMAL: '正常',
      CLOSE: '已收盘', CLOSED: '已收盘', UNKNOWN: '待判断',
      CONFIRMED: '已确认', PENDING: '待确认', WATCH: '观察', NEEDS_REVIEW: '需复核',
      FAILED: '已失败', EXPIRED: '已过期', DATA_INSUFFICIENT: '数据不足',
      WAIT: '等待', BLOCKED: '条件未满足', ARMED: '等待触发', ENTERED: '已买入', MANAGING: '持仓中',
    }[String(value || '').toUpperCase()] || valueText(value))
    const priceAction = () => props.result?.priceAction || {}
    const environment = () => priceAction().environment || {}
    const assessment = () => priceAction().assessment || {}
    const activeSetup = () => (priceAction().setups || []).find((item) => item.status === 'CONFIRMED') || (priceAction().setups || [])[0]
    const setupText = () => priceActionSetupLabel(activeSetup()?.type)
    const setupStatus = () => priceActionStatusLabel(activeSetup()?.status)
    const strengthText = (value) => displayText(value)
    const analysisSource = () => {
      const result = props.result || {}
      const bars = Array.isArray(result.rawKlines) ? result.rawKlines : []
      const latestBar = [...bars].reverse().find((item) => {
        const source = String(item?.source || item?.provider || '').trim().toLowerCase()
        return source && source !== 'unknown'
      })
      return latestBar?.source || latestBar?.provider || result.source || result.provider || result.quote?.source || result.quote?.provider || ''
    }
    const scoreText = () => {
      const rawScore = environment().score
      if (rawScore === null || rawScore === undefined || rawScore === '') return strengthText(environment().strength)
      const score = Number(rawScore)
      if (!Number.isFinite(score)) return strengthText(environment().strength)
      return `${Math.round(Math.max(0, Math.min(1, score)) * 100)}分`
    }
    return () =>
      h(
        'section',
        { class: ['side-panel', props.compact ? 'side-panel-compact' : ''], 'data-loading': props.loading },
        [
          props.title
            ? h('div', { class: 'side-panel-title' }, [
                h('span', props.title),
                h('div', { class: 'side-panel-title-value' }, [
                  h('strong', props.loading ? '分析中' : props.headingValue || displayText(environment().label)),
                  h('small', `行情来源：${marketSourceLabel(analysisSource())}`),
                ]),
              ])
            : null,
          h('div', { class: 'info-grid' }, [
            h('div', [h('span', '环境'), h('strong', displayText(environment().label))]),
            h('div', [h('span', '评分'), h('strong', scoreText())]),
            h('div', [h('span', '结构'), h('strong', setupText())]),
            h('div', [h('span', '计划状态'), h('strong', displayText(assessment().label))]),
            h('div', [h('span', '条件信号'), h('strong', valueText(props.summary?.signalCount))]),
          ]),
          h('div', { class: 'meta-list' }, [
            h('div', [
              h('span', '确认状态'),
              h('strong', setupStatus()),
            ]),
            h('div', [h('span', '最近支撑 / 阻力'), h('strong', props.summary ? `${valueText(props.summary.nearestSupport)} / ${valueText(props.summary.nearestResistance)}` : '--')]),
          ]),
        ],
      )
  },
})

const FundamentalInfo = defineComponent({
  name: 'FundamentalInfo',
  props: {
    symbol: { type: String, default: '' },
    fundamentals: { type: Object, default: null },
    loading: { type: Boolean, default: false },
    refresh: { type: Function, default: null },
  },
  setup(props) {
    const valueText = (value, suffix = '') => {
      if (value === null || value === undefined || value === '') return '--'
      return `${value}${suffix}`
    }
    const numberText = (value, digits = 2) => {
      if (value === null || value === undefined || value === '') return '--'
      const number = Number(value)
      return Number.isFinite(number) ? number.toFixed(digits) : '--'
    }
    const moneyText = (value) => {
      if (value === null || value === undefined || value === '') return '--'
      const number = Number(value)
      if (!Number.isFinite(number)) return '--'
      const sign = number < 0 ? '-' : ''
      const absolute = Math.abs(number)
      if (absolute >= 100000000) return `${sign}${(absolute / 100000000).toFixed(1)}亿`
      if (absolute >= 10000) return `${sign}${(absolute / 10000).toFixed(1)}万`
      return number.toFixed(2)
    }
    const percentText = (value) => {
      if (value === null || value === undefined || value === '') return '--'
      const number = Number(value)
      return Number.isFinite(number) ? `${number.toFixed(2)}%` : '--'
    }
    const dateText = (value) => value ? String(value).slice(0, 10) : '--'
    const latestFinance = () => props.fundamentals?.finance?.[0] || {}
    const financeRows = () => (props.fundamentals?.finance || []).filter(Boolean).slice(0, 8)
    const holderRows = () => (props.fundamentals?.holder || []).filter((item) => item?.name || item?.shares).slice(0, 10)
    const themes = () => (props.fundamentals?.themes || []).filter((item) => item?.name).slice(0, 30)
    const operationRows = () => (props.fundamentals?.business?.mainOperations || []).filter((item) => item?.name || item?.income).slice(0, 12)
    const managementRows = () => (props.fundamentals?.business?.management || []).filter((item) => item?.name || item?.position).slice(0, 10)
    const dividendRows = () => (props.fundamentals?.business?.dividends || []).filter((item) => item?.noticeDate || item?.plan).slice(0, 8)
    const statusInfo = () => props.fundamentals?.f10Status || {}
    const statusText = () => {
      const status = statusInfo().status
      if (status === 'running') {
        const completed = Number(statusInfo().completedReports || 0)
        const total = Number(statusInfo().totalReports || 0)
        return total > 0 ? `F10 同步中 ${completed}/${total}` : 'F10 全量同步中'
      }
      if (status === 'completed') return 'F10 全量已同步'
      if (status === 'partial') return 'F10 部分同步'
      return 'F10 待同步'
    }
    const field = (label, value, formatter = valueText) => h('div', { class: 'fundamental-field', key: label }, [
      h('span', label),
      h('strong', formatter(value)),
    ])
    const fields = (items) => h('div', { class: 'fundamental-grid' }, items.map((item) => field(item.label, item.value, item.formatter || valueText)))
    const table = (headers, rows) => h('div', { class: 'fundamental-table-wrap' }, [
      h('table', { class: 'fundamental-table' }, [
        h('thead', [h('tr', headers.map((header) => h('th', { key: header }, header)))]),
        h('tbody', rows.length ? rows : [h('tr', [h('td', { colspan: headers.length, class: 'fundamental-table-empty' }, '暂无可用数据')])]),
      ]),
    ])
    const financeTable = () => table(
      ['报告期', '营业收入', '净利润', '每股收益', 'ROE', '毛利率', '资产负债率'],
      financeRows().map((row) => h('tr', { key: `${row.reportDate}-${row.eps}` }, [
        h('td', dateText(row.reportDate)),
        h('td', moneyText(row.revenue)),
        h('td', moneyText(row.netProfit)),
        h('td', numberText(row.eps, 4)),
        h('td', percentText(row.roe)),
        h('td', percentText(row.grossMargin)),
        h('td', percentText(row.debtRatio)),
      ])),
    )
    const financeExtraFields = () => fields([
      { label: '最新报告期', value: dateText(latestFinance().reportDate) },
      { label: '每股净资产', value: latestFinance().bps, formatter: (value) => numberText(value, 4) },
      { label: '净利率', value: latestFinance().netMargin, formatter: percentText },
      { label: '营收同比', value: latestFinance().revenueYoy, formatter: percentText },
      { label: '净利润同比', value: latestFinance().netProfitYoy, formatter: percentText },
      { label: 'ROE同比', value: latestFinance().roeYoy, formatter: percentText },
      { label: '经营现金流', value: latestFinance().operatingCashFlow, formatter: moneyText },
      { label: '现金流/股', value: latestFinance().cashFlowPerShare, formatter: (value) => numberText(value, 4) },
      { label: '总资产', value: latestFinance().totalAssets, formatter: moneyText },
      { label: '总权益', value: latestFinance().totalEquity, formatter: moneyText },
    ])
    const operationTable = () => table(
      ['报告期', '主营项目', '收入', '收入占比', '毛利率', '利润'],
      operationRows().map((row, index) => h('tr', { key: `${row.reportDate}-${row.name}-${index}` }, [
        h('td', dateText(row.reportDate)),
        h('td', valueText(row.name)),
        h('td', moneyText(row.income)),
        h('td', percentText(row.incomeRatio != null && row.incomeRatio <= 1 ? row.incomeRatio * 100 : row.incomeRatio)),
        h('td', percentText(row.grossMargin != null && row.grossMargin <= 1 ? row.grossMargin * 100 : row.grossMargin)),
        h('td', moneyText(row.profit)),
      ])),
    )
    const managementTable = () => table(
      ['姓名', '职务', '学历', '年龄', '任职日期'],
      managementRows().map((row, index) => h('tr', { key: `${row.name}-${row.position}-${index}` }, [
        h('td', valueText(row.name)),
        h('td', valueText(row.position)),
        h('td', valueText(row.education)),
        h('td', numberText(row.age, 0)),
        h('td', dateText(row.incumbentDate)),
      ])),
    )
    const dividendTable = () => table(
      ['公告日期', '报告期', '方案', '进度', '现金分红'],
      dividendRows().map((row, index) => h('tr', { key: `${row.noticeDate}-${row.reportDate}-${index}` }, [
        h('td', dateText(row.noticeDate)),
        h('td', valueText(row.reportDate)),
        h('td', valueText(row.plan)),
        h('td', valueText(row.progress)),
        h('td', moneyText(row.totalDividend)),
      ])),
    )
    const holderTable = () => table(
      ['排名', '股东', '持股数', '持股比例', '变动', '截止日期'],
      holderRows().map((row, index) => h('tr', { key: `${row.name}-${row.endDate}-${index}` }, [
        h('td', valueText(row.rank || index + 1)),
        h('td', valueText(row.name)),
        h('td', moneyText(row.shares)),
        h('td', percentText(row.ratio)),
        h('td', valueText(row.change || (row.changeRate != null ? percentText(row.changeRate) : null))),
        h('td', dateText(row.endDate)),
      ])),
    )
    const themeList = () => h('div', { class: 'fundamental-tags' }, themes().length
      ? themes().map((item) => h('span', { key: `${item.code}-${item.name}`, class: 'fundamental-tag' }, item.type ? `${item.name} · ${item.type}` : item.name))
      : [h('span', { class: 'fundamental-table-empty' }, '暂无题材数据')])
    const profile = () => props.fundamentals?.profile || {}
    const business = () => props.fundamentals?.business || {}
    const activeFundamentalSection = ref('profile')
    watch(() => props.symbol, () => {
      activeFundamentalSection.value = 'profile'
    })
    const fundamentalSections = () => [
      {
        key: 'profile',
        label: '公司资料',
        note: () => '',
        render: () => fields([
          { label: '公司名称', value: profile().companyName },
          { label: '行业', value: profile().industry },
          { label: '地区', value: profile().area },
          { label: '上市日期', value: dateText(profile().listDate) },
          { label: '成立日期', value: dateText(profile().foundDate) },
          { label: '董事长 / 法人', value: profile().chairman },
          { label: '董事会秘书', value: profile().secretary },
          { label: '注册资本', value: profile().registeredCapital, formatter: moneyText },
          { label: '员工人数', value: profile().employeeCount, formatter: (value) => valueText(value, ' 人') },
          { label: '实际控制人', value: profile().actualHolder },
          { label: '注册地址', value: profile().address },
          { label: '公司网站', value: profile().website },
        ]),
      },
      {
        key: 'finance',
        label: '财务摘要',
        note: () => financeRows().length ? `${financeRows().length}期` : '',
        render: () => h('div', { class: 'fundamental-stack' }, [financeTable(), financeRows().length ? financeExtraFields() : null]),
      },
      {
        key: 'holders',
        label: '前十大股东',
        note: () => holderRows().length ? `${holderRows().length}条` : '',
        render: () => holderTable(),
      },
      {
        key: 'themes',
        label: '核心题材',
        note: () => themes().length ? `${themes().length}项` : '',
        render: () => themeList(),
      },
      {
        key: 'operations',
        label: '主营构成',
        note: () => operationRows().length ? `${operationRows().length}项` : '',
        render: () => operationTable(),
      },
      {
        key: 'management',
        label: '董事及高管',
        note: () => managementRows().length ? `${managementRows().length}位` : '',
        render: () => managementTable(),
      },
      {
        key: 'dividends',
        label: '分红送转',
        note: () => dividendRows().length ? `${dividendRows().length}条` : '',
        render: () => dividendTable(),
      },
      {
        key: 'business',
        label: '经营情况',
        note: () => business().reportDate ? dateText(business().reportDate) : '',
        render: () => h('div', { class: 'fundamental-copy-stack' }, [
          h('div', [h('span', '主营业务'), h('p', profile().mainBusiness || '--')]),
          h('div', [h('span', '经营情况'), h('p', business().summary || '--')]),
          h('div', [h('span', '未来规划'), h('p', business().futureExpect || '--')]),
        ]),
      },
    ]
    const renderFundamentalTabs = (sections) => h('div', {
      class: 'fundamental-subtabs',
      role: 'tablist',
      'aria-label': '基本面分类',
    }, sections.map((item) => h('button', {
      key: item.key,
      type: 'button',
      role: 'tab',
      class: ['fundamental-subtab', { 'is-active': activeFundamentalSection.value === item.key }],
      'aria-selected': activeFundamentalSection.value === item.key,
      onClick: () => { activeFundamentalSection.value = item.key },
    }, [
      h('span', item.label),
      item.note() ? h('small', item.note()) : null,
    ])))
    const renderData = () => {
      const sections = fundamentalSections()
      const active = sections.find((item) => item.key === activeFundamentalSection.value) || sections[0]
      return [
        h('div', { class: 'fundamental-head' }, [
          h('div', { class: 'fundamental-title' }, [
            h('span', '基本面 F10'),
            h('small', props.fundamentals?.updatedAt ? `数据更新：${dateText(props.fundamentals.updatedAt)}` : ''),
          ]),
          h('div', { class: 'fundamental-head-actions' }, [
            h('strong', statusText()),
            h(ElButton, {
              circle: true,
              text: true,
              plain: true,
              icon: RefreshCw,
              loading: props.loading,
              title: '刷新 F10',
              'aria-label': '刷新 F10',
              onClick: () => props.refresh?.(),
            }),
          ]),
        ]),
        renderFundamentalTabs(sections),
        h('section', {
          class: 'fundamental-section fundamental-active-section',
          'aria-label': active.label,
        }, [active.render()]),
      ]
    }
    return () => h('section', {
      class: ['fundamental-card', props.fundamentals ? 'has-data' : 'is-empty'],
    }, props.fundamentals ? renderData() : [
      h('div', { class: 'fundamental-head' }, [h('span', '基本面 F10'), h('strong', props.loading ? '正在读取' : '等待个股分析')]),
      h('p', { class: 'detail-empty' }, props.loading ? '正在读取基本面与 F10 数据...' : '分析个股后将在这里显示基本面与 F10 数据。'),
    ])
  },
})

const AI_SETTINGS_KEY = 'chanlun-ai-settings'
const AI_REPORT_PREFIX = 'chanlun-ai-report'
const UPDATE_LOG_CACHE_KEY = 'jiaren-update-log-cache:v1'
const STOCK_OPTIONS_CACHE_KEY = 'jiaren-stock-options-cache:v1'

const aiReportKey = (type, symbol) => `${AI_REPORT_PREFIX}:${type}:${symbol || '000001'}`

const loadJson = (key, fallback = null) => {
  try {
    const raw = localStorage.getItem(key)
    return raw ? JSON.parse(raw) : fallback
  } catch {
    return fallback
  }
}

const loadCachedUpdateLog = () => {
  const cached = loadJson(UPDATE_LOG_CACHE_KEY, null)
  if (!Array.isArray(cached?.updates) || !cached.updates.length) return false
  updateLogs.value = cached.updates
  return true
}

const saveCachedUpdateLog = (data) => {
  if (!Array.isArray(data?.updates) || !data.updates.length) return
  try {
    saveJson(UPDATE_LOG_CACHE_KEY, {
      updates: data.updates,
      savedAt: Date.now(),
    })
  } catch {
    // A public cache is optional and must not affect the page.
  }
}

const saveJson = (key, value) => {
  localStorage.setItem(key, JSON.stringify(value))
}

const applyAiSettings = (settings = {}) => {
  aiSettings.value = {
    ...DEFAULT_AI_SETTINGS,
    baseUrl: settings.baseUrl || DEFAULT_AI_SETTINGS.baseUrl,
    model: settings.model || DEFAULT_AI_SETTINGS.model,
    apiKey: '',
    apiKeyConfigured: Boolean(settings.apiKeyConfigured),
  }
}

const removeLegacyAiSettings = () => {
  try { localStorage.removeItem(AI_SETTINGS_KEY) } catch { /* ignore */ }
}

const loadAiSettings = async ({ migrateLegacy = false } = {}) => {
  if (!isStockWorkspacePage()) return false
  if (!currentUser.value) {
    resetAiSettings()
    syncAiReports()
    return true
  }

  try {
    const saved = await fetchAccountAiSettings()
    const legacy = migrateLegacy ? loadJson(AI_SETTINGS_KEY, {}) : null
    const legacyBaseUrl = String(legacy?.baseUrl || '').trim()
    const legacyModel = String(legacy?.model || '').trim()
    const legacyApiKey = String(legacy?.apiKey || '').trim()
    const hasLegacySettings = Boolean(legacyApiKey || legacyBaseUrl || legacyModel)

    if (!saved.apiKeyConfigured && hasLegacySettings) {
      const migrated = await saveAccountAiSettings({
        baseUrl: legacyBaseUrl || DEFAULT_AI_SETTINGS.baseUrl,
        model: legacyModel || DEFAULT_AI_SETTINGS.model,
        ...(legacyApiKey ? { apiKey: legacyApiKey } : {}),
      })
      applyAiSettings(migrated)
      removeLegacyAiSettings()
      ElMessage.success('旧 AI 设置已迁移到账户')
    } else {
      applyAiSettings(saved)
      if (migrateLegacy && hasLegacySettings) removeLegacyAiSettings()
    }
    syncAiReports()
    return true
  } catch (error) {
    ElMessage.warning(error.response?.data?.message || 'AI设置加载失败')
    return false
  }
}

const openAiSettings = () => {
  if (!currentUser.value) {
    ElMessage.warning('请先登录账户，再配置 AI 设置')
    openAuthDialog('login')
    return
  }
  settingsVisible.value = true
  void loadNotificationSettings()
}

const loadNotificationSettings = async () => {
  if (!currentUser.value) return
  try {
    notificationSettings.value = { email: String((await fetchNotificationSettings()).email || '') }
  } catch (error) {
    ElMessage.warning(error.response?.data?.message || '邮件通知设置加载失败')
  }
}

const saveNotificationSettings = async () => {
  if (!currentUser.value) return
  notificationSaving.value = true
  try {
    const saved = await requestSaveNotificationSettings({ email: notificationSettings.value.email })
    notificationSettings.value = { email: String(saved.email || '') }
    ElMessage.success('邮件通知设置已保存，A 股与 Binance 将同步使用')
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '邮件通知设置保存失败')
  } finally {
    notificationSaving.value = false
  }
}

const openCurrentSettings = () => {
  if (isBinanceWorkspace.value) {
    binanceWorkspaceRef.value?.openAccountSettings()
    return
  }
  openAiSettings()
}

const saveAiSettings = async () => {
  if (!currentUser.value) {
    openAiSettings()
    return
  }
  try {
    const apiKey = String(aiSettings.value.apiKey || '').trim()
    const saved = await saveAccountAiSettings({
      baseUrl: aiSettings.value.baseUrl,
      model: aiSettings.value.model,
      ...(apiKey ? { apiKey } : {}),
    })
    applyAiSettings(saved)
    settingsVisible.value = false
    ElMessage.success('AI设置已保存到账户')
  } catch (error) {
    ElMessage.error(error.response?.data?.message || 'AI设置保存失败')
  }
}

const sendTestEmail = async () => {
  const recipient = String(notificationSettings.value.email || '').trim()
  if (!recipient) {
    ElMessage.warning('请填写测试收件邮箱')
    return
  }
  testEmailSending.value = true
  try {
    await requestTestEmail(recipient)
    ElMessage.success(`测试邮件已提交至 ${recipient}，请检查收件箱和垃圾邮件。`)
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '测试邮件发送失败')
  } finally {
    testEmailSending.value = false
  }
}

const loadAiReport = (type, symbol) => loadJson(aiReportKey(type, symbol), null)

const saveAiReport = (report) => {
  if (!report?.targetType || !report?.symbol) return
  saveJson(aiReportKey(report.targetType, report.symbol), report)
}

const syncAiReports = () => {
  indexAiReport.value = loadAiReport('index', indexResult.value?.symbol || '000001')
  if (stockResult.value?.symbol) {
    stockAiReport.value = loadAiReport('stock', stockResult.value.symbol)
  }
}

const AiPanel = defineComponent({
  name: 'AiPanel',
  props: {
    report: { type: Object, default: null },
    analysisDate: { type: String, default: '' },
    loading: { type: Boolean, default: false },
    disabled: { type: Boolean, default: false },
  },
  emits: ['run'],
  setup(props, { emit }) {
    const parseReport = () => parseAiAnalysis(props.report?.analysis)
    const generatedText = () => {
      if (!props.report?.generatedAt) return '暂无本地AI分析'
      return `${props.report.generatedAt} / ${props.report.marketActionDate || '--'}`
    }
    const dateMismatch = () => {
      const savedDate = String(props.report?.marketActionDate || '').match(/\d{4}-\d{2}-\d{2}/)?.[0]
      const currentDate = String(props.analysisDate || '').match(/\d{4}-\d{2}-\d{2}/)?.[0]
      return Boolean(savedDate && currentDate && savedDate !== currentDate)
    }
    return () =>
      h('section', { class: 'ai-panel' }, [
        h('div', { class: 'ai-panel-head' }, [
          h('div', [
            h('span', 'AI建议（仅供参考）'),
            h('strong', [
              generatedText(),
              dateMismatch() ? h('em', { class: 'ai-date-warning' }, ' · 与当前交易日不一致') : null,
            ]),
          ]),
          h(
            ElButton,
            {
              class: 'ai-run-button',
              type: 'primary',
              plain: true,
              icon: Sparkles,
              loading: props.loading,
              disabled: props.disabled || props.loading,
              onClick: () => emit('run'),
            },
            () => 'AI分析',
          ),
        ]),
        props.report?.analysis
          ? renderAiReport(parseReport(), props.report.analysis)
          : h('p', { class: 'ai-empty' }, '点击AI分析后会联网搜索并结合技术面数据生成建议。'),
      ])
  },
})

const parseAiAnalysis = (text) => {
  if (!text) return { ok: false, data: null }
  try {
    const trimmed = String(text).trim().replace(/^```json\s*/i, '').replace(/```$/i, '').trim()
    const data = JSON.parse(trimmed)
    if (!data.levels && data.anchors) data.levels = data.anchors
    const required = ['longTerm', 'shortTerm', 'levels', 'risks', 'conclusion', 'publicInfo']
    const ok = required.every((key) => Object.prototype.hasOwnProperty.call(data, key))
    return { ok, data }
  } catch {
    return { ok: false, data: null }
  }
}

const renderAiReport = (parsed, rawText) => {
  if (!parsed.ok) {
    return h('div', { class: 'ai-report ai-report-raw' }, [
      h('div', { class: 'ai-format-warning' }, 'AI返回内容不符合标准JSON格式，已显示原文。'),
      h('pre', rawText),
    ])
  }
  const data = parsed.data
  return h('div', { class: 'ai-report ai-report-structured' }, [
    h('div', { class: 'ai-report-grid' }, [
      renderAiBlock('长期走势', data.longTerm),
      renderAiBlock('短期走势', data.shortTerm),
      renderPlanLevelBlock(data.levels),
      renderListBlock('风险提示', data.risks),
      renderListBlock('公开信息', data.publicInfo),
      h('section', { class: 'ai-report-block conclusion-block' }, [
        h('span', '一句话结论'),
        h('strong', data.conclusion || '--'),
      ]),
    ]),
  ])
}

const renderAiBlock = (title, value = {}) =>
  h('section', { class: 'ai-report-block' }, [
    h('div', { class: 'ai-block-head' }, [h('span', title), h('strong', value.stance || '--')]),
    h('p', value.summary || '--'),
    renderMiniList(value.evidence || []),
  ])

const renderPlanLevelBlock = (value = {}) =>
  h('section', { class: 'ai-report-block' }, [
    h('div', { class: 'ai-block-head' }, [h('span', '交易安排'), h('strong', value.actionDate || '--')]),
    h('dl', { class: 'anchor-list' }, [
      h('dt', '买入观察'),
      h('dd', value.buyWatch || '--'),
      h('dt', '风险止损'),
      h('dd', value.riskStop || '--'),
      h('dt', '卖出观察'),
      h('dd', value.sellWatch || '--'),
    ]),
  ])

const renderListBlock = (title, items = []) =>
  h('section', { class: 'ai-report-block' }, [
    h('div', { class: 'ai-block-head' }, [h('span', title), h('strong', `${items.length || 0}条`)]),
    renderMiniList(items),
  ])

const renderMiniList = (items = []) =>
  items.length
    ? h('ul', { class: 'ai-mini-list' }, items.map((item) => h('li', item)))
    : h('p', { class: 'ai-muted' }, '--')

const STOCK_HISTORY_GUEST_KEY = 'jiaren-stock-analysis-history:guest'

const normalizeStockHistoryItem = (item) => ({
  ...normalizeStockIdentity(item),
  lastAnalyzedAt: item?.lastAnalyzedAt || new Date().toISOString(),
})

const mergeStockAnalysisHistory = (items, latest = null) => {
  const source = latest ? [latest, ...(items || [])] : (items || [])
  const unique = []
  const seen = new Set()
  for (const raw of source) {
    const item = normalizeStockHistoryItem(raw)
    if (!item.symbol || seen.has(item.symbol)) continue
    seen.add(item.symbol)
    unique.push(item)
    if (unique.length >= 10) break
  }
  return unique
}

const loadGuestStockAnalysisHistory = () => {
  try {
    const raw = JSON.parse(localStorage.getItem(STOCK_HISTORY_GUEST_KEY) || '[]')
    stockAnalysisHistory.value = mergeStockAnalysisHistory(Array.isArray(raw) ? raw : [])
  } catch {
    stockAnalysisHistory.value = []
  }
}

const loadStockAnalysisHistory = async () => {
  if (!isStockWorkspacePage()) return false
  if (!currentUser.value) {
    loadGuestStockAnalysisHistory()
    return true
  }
  try {
    stockAnalysisHistory.value = mergeStockAnalysisHistory(await fetchStockAnalysisHistory())
    return true
  } catch {
    // Keep the last successful history visible during a transient API failure.
    return false
  }
}

const rememberAnalyzedStock = async (result) => {
  const symbol = String(result?.symbol || '').trim()
  if (!symbol) return
  const item = normalizeStockHistoryItem({
    symbol,
    name: result?.name || selectedStock.value?.name || '',
    lastAnalyzedAt: new Date().toISOString(),
  })
  stockAnalysisHistory.value = mergeStockAnalysisHistory(stockAnalysisHistory.value, item)

  if (!currentUser.value) {
    try {
      localStorage.setItem(STOCK_HISTORY_GUEST_KEY, JSON.stringify(stockAnalysisHistory.value))
    } catch {
      // Local history is optional when browser storage is unavailable.
    }
    return
  }

  try {
    const saved = await saveStockAnalysisHistory(item)
    if (saved) stockAnalysisHistory.value = mergeStockAnalysisHistory(stockAnalysisHistory.value, saved)
  } catch {
    // Keep the successful analysis visible for this session if persistence is unavailable.
  }
}

const removeStockHistory = async (item) => {
  const symbol = String(item?.symbol || '').trim()
  if (!symbol) return

  const previous = stockAnalysisHistory.value
  stockAnalysisHistory.value = previous.filter((historyItem) => historyItem.symbol !== symbol)

  try {
    if (!currentUser.value) {
      localStorage.setItem(STOCK_HISTORY_GUEST_KEY, JSON.stringify(stockAnalysisHistory.value))
    } else {
      await deleteStockAnalysisHistory(symbol)
    }
  } catch (error) {
    stockAnalysisHistory.value = previous
    ElMessage.error(error.response?.data?.message || '历史记录删除失败')
  }
}

const enrichStockSearchItem = (item) => {
  const normalized = normalizeStockIdentity(item)
  const name = normalized.name
  return {
    ...normalized,
    _searchPinyin: pinyin(name, { toneType: 'none', type: 'array' }).join('').toLowerCase(),
    _searchInitials: pinyin(name, { pattern: 'first', toneType: 'none' }).replace(/\s+/g, '').toLowerCase(),
  }
}

const querySearch = (queryString, cb) => {
  const word = queryString.trim().toLowerCase()
  const normalizedWord = word.replace(/\s+/g, '')
  if (!word) {
    cb([])
    return
  }
  const matched = stockOptions.value
    .filter((item) => {
      const symbol = String(item.symbol || '').toLowerCase()
      const name = String(item.name || '').toLowerCase()
      const label = String(item.label || '').toLowerCase()
      return symbol.includes(normalizedWord)
        || name.includes(word)
        || label.includes(word)
        || String(item._searchPinyin || '').includes(normalizedWord)
        || String(item._searchInitials || '').includes(normalizedWord)
    })
    .slice(0, 20)

  cb(matched)
}

const handleSelect = (item) => {
  const stock = normalizeStockIdentity(item)
  selectedStock.value = stock
  selectedLabel.value = stock.label
  keyword.value = stock.label
  disciplineChartPlan.value = null
  stockAiReport.value = loadAiReport('stock', item.symbol)
}

const selectStockHistory = (item) => {
  handleSelect(normalizeStockIdentity(item))
}

const disciplineActionClass = (tone) => ({
  positive: 'discipline-action-positive',
  warning: 'discipline-action-warning',
  negative: 'discipline-action-negative',
}[tone] || 'discipline-action-muted')

const loadDisciplinePortfolio = async () => {
  if (!currentUser.value) return false
  disciplinePortfolioLoading.value = true
  try {
    const data = await fetchDisciplinePortfolio()
    const positionsBySymbol = new Map(portfolioItems.value.map((item) => [item.symbol, item]))
    disciplinePortfolioItems.value = (data.items || []).map((item) => normalizeStockIdentity(item, positionsBySymbol.get(item.symbol)))
    disciplinePortfolioSummary.value = data.summary || { positionCount: 0, actionCounts: {}, errorCount: 0, plannedRisk: 0 }
    portfolioMarketData.value = data.marketData || portfolioMarketData.value
    return true
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '持仓纪律分析失败')
    return false
  } finally {
    disciplinePortfolioLoading.value = false
  }
}

const analyzeHoldings = async () => {
  if (!currentUser.value) return
  const loaded = await loadDisciplinePortfolio()
  if (!loaded) return
  const count = disciplinePortfolioItems.value.length
  const errors = disciplinePortfolioSummary.value.errorCount || 0
  ElMessage.success(errors ? `已完成 ${count} 只持仓分析，${errors} 只待补行情` : `已完成 ${count} 只持仓分析`)
}

const loadWatchlist = async () => {
  if (!currentUser.value) return false
  watchlistLoading.value = true
  try {
    const data = await fetchWatchlist()
    watchlistItems.value = (data.items || []).map((item) => normalizeStockIdentity(item))
    portfolioMarketData.value = data.marketData || portfolioMarketData.value
    const currentSymbols = new Set(watchlistItems.value.map((item) => item.symbol))
    disciplineWatchlistItems.value = disciplineWatchlistItems.value.filter((item) => currentSymbols.has(item.symbol))
    return true
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '自选观察加载失败')
    return false
  } finally {
    watchlistLoading.value = false
  }
}

const loadDisciplineWatchlist = async ({ notify = false } = {}) => {
  if (!currentUser.value) return
  if (!watchlistItems.value.length && !(await loadWatchlist())) return
  if (!watchlistItems.value.length) {
    disciplineWatchlistItems.value = []
    disciplineWatchlistSummary.value = { watchCount: 0, actionCounts: {}, errorCount: 0 }
    if (notify) ElMessage.info('暂无自选股票')
    return true
  }
  watchlistAnalysisLoading.value = true
  try {
    const data = await fetchDisciplineWatchlist()
    const watchlistBySymbol = new Map(watchlistItems.value.map((item) => [item.symbol, item]))
    disciplineWatchlistItems.value = (data.items || []).map((item) => normalizeStockIdentity(item, watchlistBySymbol.get(item.symbol)))
    disciplineWatchlistSummary.value = data.summary || { watchCount: 0, actionCounts: {}, errorCount: 0 }
    portfolioMarketData.value = data.marketData || portfolioMarketData.value
    const count = disciplineWatchlistItems.value.length
    const errors = disciplineWatchlistSummary.value.errorCount || 0
    if (notify) ElMessage.success(errors ? `已完成 ${count} 只自选分析，${errors} 只待补行情` : `已完成 ${count} 只自选分析`)
    return true
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '自选观察分析失败')
    return false
  } finally {
    watchlistAnalysisLoading.value = false
  }
}

const analyzeWatchlist = () => loadDisciplineWatchlist({ notify: true })

const refreshDisciplineWorkspace = async (force = false) => {
  if (!currentUser.value) {
    resetPrivateWorkspace()
    return false
  }
  // Keep cached holdings usable while a provider refresh is still running.
  const syncTask = synchronizeCurrentMarketData(force)
  await loadPortfolio()
  await Promise.all([loadDisciplinePortfolio(), loadTradeHistory(), loadWatchlist()])
  await loadDisciplineWatchlist()
  await syncTask
  await Promise.all([loadPortfolio(), loadDisciplinePortfolio(), loadWatchlist()])
  await loadDisciplineWatchlist()
  await nextTick()
  scheduleDisciplineWorkspaceHeight()
}

const openWatchlistDialog = () => {
  watchlistForm.value = { keyword: '', symbol: '', name: '', note: '' }
  watchlistDialogVisible.value = true
}

const handleWatchlistSelect = (item) => {
  const stock = normalizeStockIdentity(item)
  watchlistForm.value.symbol = stock.symbol
  watchlistForm.value.name = stock.name
  watchlistForm.value.keyword = stock.label
}

const submitWatchlist = async () => {
  const symbol = watchlistForm.value.symbol || watchlistForm.value.keyword.match(/\d{1,6}/)?.[0]
  if (!symbol) {
    ElMessage.warning('请选择或输入有效股票')
    return
  }
  watchlistSaving.value = true
  try {
    const saved = await saveWatchlistItem({
      symbol,
      name: resolveStockName(symbol, watchlistForm.value.name),
      note: watchlistForm.value.note,
    })
    const normalizedSaved = normalizeStockIdentity(saved, { symbol, name: watchlistForm.value.name })
    watchlistItems.value = [normalizedSaved, ...watchlistItems.value.filter((item) => item.symbol !== normalizedSaved.symbol)]
    disciplineWatchlistItems.value = disciplineWatchlistItems.value.filter((item) => item.symbol !== saved.symbol)
    disciplineWatchlistSummary.value = { ...disciplineWatchlistSummary.value, watchCount: watchlistItems.value.length }
    watchlistDialogVisible.value = false
    await loadDisciplineWatchlist()
    ElMessage.success('已加入自选观察')
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '自选观察保存失败')
  } finally {
    watchlistSaving.value = false
  }
}

const removeWatchlist = async (row) => {
  if (!row?.symbol) return
  try {
    await ElMessageBox.confirm(`将 ${row.symbol} ${row.name || ''} 移出自选观察？`, '删除自选观察', {
      confirmButtonText: '删除',
      cancelButtonText: '取消',
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await deleteWatchlistItem(row.symbol)
    watchlistItems.value = watchlistItems.value.filter((item) => item.symbol !== row.symbol)
    disciplineWatchlistItems.value = disciplineWatchlistItems.value.filter((item) => item.symbol !== row.symbol)
    disciplineWatchlistSummary.value = { ...disciplineWatchlistSummary.value, watchCount: watchlistItems.value.length }
    ElMessage.success('已移出自选观察')
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '自选观察删除失败')
  }
}

const openPortfolioDialog = (row = null) => {
  if (!row?.symbol) return
  portfolioEditing.value = true
  portfolioForm.value = {
    symbol: row.symbol,
    name: resolveStockName(row.symbol, row),
    costPrice: Number(row.costPrice || 0),
    shares: Number(row.shares || 0),
    note: row.note || '',
  }
  portfolioDialogVisible.value = true
}

const submitPortfolio = async () => {
  const symbol = portfolioForm.value.symbol
  if (!symbol) {
    ElMessage.warning('缺少有效股票代码')
    return
  }
  if (Number(portfolioForm.value.costPrice) <= 0 || Number(portfolioForm.value.shares) <= 0) {
    ElMessage.warning('成本价和股数必须大于 0')
    return
  }
  portfolioSaving.value = true
  try {
    await savePortfolioPosition({
      symbol,
      costPrice: portfolioForm.value.costPrice,
      shares: portfolioForm.value.shares,
      note: portfolioForm.value.note,
      name: resolveStockName(symbol, portfolioForm.value.name),
    })
    portfolioDialogVisible.value = false
    ElMessage.success('持仓信息已修正')
    await refreshDisciplineWorkspace()
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '持仓保存失败')
  } finally {
    portfolioSaving.value = false
  }
}

const saveCashBalance = async () => {
  const cashBalance = Number(cashBalanceDraft.value)
  if (!Number.isFinite(cashBalance) || cashBalance < 0) {
    ElMessage.warning('请输入不小于 0 的可用现金')
    return
  }
  cashSaving.value = true
  try {
    await savePortfolioCash(cashBalance)
    ElMessage.success('可用现金已保存')
    await refreshDisciplineWorkspace()
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '现金余额保存失败')
  } finally {
    cashSaving.value = false
  }
}

const handleTradeSelect = (item) => {
  const stock = normalizeStockIdentity(item)
  const changedSymbol = Boolean(tradeForm.value.symbol) && tradeForm.value.symbol !== stock.symbol
  tradeForm.value.symbol = stock.symbol
  tradeForm.value.name = stock.name
  tradeForm.value.keyword = stock.label
  // 默认成交价跟随所选标的最新价；换标的时同步刷新，避免沿用上一只的价格。
  if ((!tradeForm.value.price || changedSymbol) && item.latestPrice) tradeForm.value.price = Number(item.latestPrice)
}

const handleTradeFeeChange = () => {
  tradeFeeManuallyEdited.value = true
}

const disciplineViolationText = (violations = []) => {
  const labels = {
    AVERAGING_DOWN: '加仓价格未高于当前均摊成本，属于向下摊平。',
  }
  if (!Array.isArray(violations)) return ''
  return violations
    .map((violation) => {
      if (typeof violation === 'string') return labels[violation] || violation
      return violation?.message || labels[violation?.code] || ''
    })
    .filter(Boolean)
    .join('；')
}

const confirmDisciplineException = async (violations) => {
  const reason = disciplineViolationText(violations) || '本次交易偏离既定纪律。'
  try {
    await ElMessageBox.confirm(
      `${reason} 继续买入会按当前价格成交，并在成交账本永久标记为“未按纪律执行”。`,
      '确认纪律例外',
      {
        confirmButtonText: '仍要买入',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
    return true
  } catch {
    return false
  }
}

const submitTradeRequest = (symbol, disciplineOverride) => executePortfolioTrade({
  action: tradeForm.value.action,
  symbol,
  name: resolveStockName(symbol, tradeForm.value.name, tradePosition.value),
  price: Number(tradeForm.value.price),
  shares: Number(tradeForm.value.shares),
  fee: Number(tradeForm.value.fee || 0),
  note: tradeForm.value.note,
  disciplineOverride,
})

const openTradeDialog = (action, row = null) => {
  const isBuy = action === 'BUY'
  // 默认成交价直接采用当前最新价，可手动调整为实际成交价。
  const price = Number(row?.latestPrice || 0)
  const shares = isBuy
    ? Number(row?.plan?.order?.shares || 100)
    : Number(row?.plan?.order?.shares || row?.shares || 0)
  const symbol = row?.symbol || ''
  const name = resolveStockName(symbol, row)
  const defaultFee = estimateDefaultTradeFee({ action, symbol, amount: price * shares })
  const note = (() => {
    if (!isBuy) return '默认按最新价卖出，可手动调整为实际成交价'
    if (!row) return '按纪律规则建立持仓'
    return Number(row.shares) > 0
      ? '默认按最新价加仓，可手动调整为实际成交价'
      : '默认按最新价买入，可手动调整为实际成交价'
  })()
  tradeFeeManuallyEdited.value = false
  tradeForm.value = {
    action,
    keyword: row ? stockLabel(symbol, name) : '',
    symbol,
    name,
    price,
    shares,
    fee: defaultFee.total,
    note,
    lockedSymbol: Boolean(row),
  }
  tradeDialogVisible.value = true
}

const submitTrade = async () => {
  const symbol = tradeForm.value.symbol || tradeForm.value.keyword.match(/\d{6}/)?.[0]
  if (!symbol) {
    ElMessage.warning('请选择或输入有效股票')
    return
  }
  if (Number(tradeForm.value.price) <= 0 || Number(tradeForm.value.shares) <= 0) {
    ElMessage.warning('成交价格和成交股数必须大于 0')
    return
  }
  if (tradeForm.value.action === 'BUY' && Number(tradeForm.value.shares) % 100 !== 0) {
    ElMessage.warning('买入股数必须为 100 股的整数倍')
    return
  }
  if (tradeForm.value.action === 'SELL' && Number(tradeForm.value.shares) > Number(tradePosition.value?.shares || 0)) {
    ElMessage.warning('卖出股数不能超过当前持仓')
    return
  }
  if (tradeCashAfter.value < 0) {
    ElMessage.warning('可用现金不足以完成本次买入')
    return
  }
  let disciplineOverride = false
  if (tradeNeedsDisciplineConfirmation.value) {
    const confirmed = await confirmDisciplineException([
      {
        code: 'AVERAGING_DOWN',
        price: Number(tradeForm.value.price),
        costPrice: Number(tradePosition.value?.costPrice),
      },
    ])
    if (!confirmed) return
    disciplineOverride = true
  }
  tradeSaving.value = true
  try {
    try {
      await submitTradeRequest(symbol, disciplineOverride)
    } catch (error) {
      const response = error.response?.data
      if (disciplineOverride || !response?.requiresDisciplineConfirmation) throw error
      tradeSaving.value = false
      const confirmed = await confirmDisciplineException(response.disciplineViolations)
      if (!confirmed) return
      tradeSaving.value = true
      await submitTradeRequest(symbol, true)
    }
    tradeDialogVisible.value = false
    ElMessage.success(tradeForm.value.action === 'BUY' ? '买入已记账，并更新均摊成本' : '卖出已记账，并释放可用现金')
    await refreshDisciplineWorkspace()
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '成交执行失败')
  } finally {
    tradeSaving.value = false
  }
}

const loadTradeHistory = async () => {
  if (!currentUser.value) return false
  tradeHistoryLoading.value = true
  try {
    const positionsBySymbol = new Map(portfolioItems.value.map((item) => [item.symbol, item]))
    tradeHistory.value = (await fetchPortfolioTrades()).map((item) => normalizeStockIdentity(item, positionsBySymbol.get(item.symbol)))
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '成交账本加载失败')
  } finally {
    tradeHistoryLoading.value = false
  }
}

const openStockFromHome = (item) => {
  if (!item?.symbol) return
  const stock = normalizeStockIdentity(item)
  const label = stock.label
  selectedStock.value = stock
  selectedLabel.value = label
  keyword.value = label
  disciplineChartPlan.value = null
  stockAiReport.value = loadAiReport('stock', item.symbol)
  setActivePage('stock')
}

const openIndexFromHome = (symbol) => {
  setActivePage('index')
  selectIndex(symbol)
}

watch(keyword, (value) => {
  if (selectedStock.value && value !== selectedLabel.value) {
    selectedStock.value = null
  }
  const symbol = value.match(/\d{6}/)?.[0]
  if (symbol) {
    stockAiReport.value = loadAiReport('stock', symbol)
  }
})

/* --- 个股/指数分析结果缓存与新鲜度判断：数据已是最新时不再重复请求接口 --- */
const ANALYSIS_CACHE_PREFIX = 'chanlun-analysis:'
const STOCK_ANALYSIS_CACHE_VERSION = '1.3.02-turnover'
const analysisCacheKey = (type, symbol) => `${ANALYSIS_CACHE_PREFIX}${type}:${symbol}`
const indexQuoteMinutes = ref({})
const stockAnalysisCache = ref({})

const readAnalysisCache = (type, symbol) => {
  try {
    const raw = localStorage.getItem(analysisCacheKey(type, symbol))
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (type === 'stock' && parsed?.cacheVersion !== STOCK_ANALYSIS_CACHE_VERSION) return null
    return parsed?.result ? parsed : null
  } catch {
    return null
  }
}

const saveAnalysisCache = (type, symbol, result) => {
  try {
    localStorage.setItem(analysisCacheKey(type, symbol), JSON.stringify({
      result,
      ...(type === 'stock' ? { cacheVersion: STOCK_ANALYSIS_CACHE_VERSION } : {}),
      quoteMinute: marketToday.value?.quoteMinute || '',
      savedAt: Date.now(),
    }))
  } catch {
    /* 本地存储配额不足时忽略缓存，不影响分析 */
  }
}

const analysisNeedsHistoricalTurnoverRefresh = (result) => {
  const bars = Array.isArray(result?.rawKlines) ? result.rawKlines : []
  return bars.some((bar) => {
    const turnover = bar?.turnoverRate ?? bar?.turnover_rate ?? bar?.providerTurnover
    return !Number.isFinite(Number(turnover))
  })
}

/* 新鲜度：收盘后最后交易日已定型，或行情快照分钟未变化，均视为最新 */
const isAnalysisFresh = (result, fetchedMinute = '') => {
  if (analysisNeedsHistoricalTurnoverRefresh(result)) return false
  const market = marketToday.value || {}
  const end = String(result?.dateRange?.end || '').slice(0, 10)
  const tradeDate = String(market.tradeDate || '').slice(0, 10)
  const session = String(market.session || '')
  if (end && tradeDate && ['after_close', 'closed', 'pre_open'].includes(session) && end === tradeDate) return true
  if (fetchedMinute && market.quoteMinute && String(fetchedMinute) === String(market.quoteMinute)) return true
  return false
}

const restoreAnalysisCaches = () => {
  try {
    for (let index = 0; index < localStorage.length; index++) {
      const key = localStorage.key(index)
      if (!key || !key.startsWith(ANALYSIS_CACHE_PREFIX)) continue
      const [, type, symbol] = key.split(':')
      if (type === 'index') {
        const cached = readAnalysisCache('index', symbol)
        if (cached?.result?.symbol) {
          indexResults.value = { ...indexResults.value, [symbol]: cached.result }
          indexQuoteMinutes.value[symbol] = cached.quoteMinute || ''
        }
      } else if (type === 'stock') {
        const cached = readAnalysisCache('stock', symbol)
        if (cached?.result?.symbol) stockAnalysisCache.value[symbol] = cached
      }
    }
  } catch {
    /* 忽略损坏的本地缓存 */
  }
}

const runAnalyze = async () => {
  const symbol = selectedStock.value?.symbol || keyword.value.match(/\d{6}/)?.[0]
  if (!symbol) {
    ElMessage.warning('请输入或选择一个有效股票')
    return
  }

  // 已有结果且数据仍是最新：直接复用，不请求接口
  const cached = stockAnalysisCache.value[symbol] || null
  const cachedResult = stockResult.value?.symbol === symbol ? stockResult.value : cached?.result
  const fetchedMinute = cached?.quoteMinute || ''
  if (cachedResult && isAnalysisFresh(cachedResult, fetchedMinute)) {
    stockResult.value = cachedResult
    selectedLabel.value = selectedStock.value?.label || symbol
    stockAiReport.value = loadAiReport('stock', symbol)
    ElMessage.info('个股数据已是最新，未重复请求接口')
    return
  }

  stockLoading.value = true
  try {
    setAnalysisProgress('stock', '个股分析', STOCK_ANALYSIS_STEPS)
    stockResult.value = normalizeStockIdentity(await analyzeStock(symbol), selectedStock.value)
    stockAnalysisCache.value = { ...stockAnalysisCache.value, [symbol]: { result: stockResult.value, quoteMinute: marketToday.value?.quoteMinute || '' } }
    saveAnalysisCache('stock', symbol, stockResult.value)
    void rememberAnalyzedStock(stockResult.value)
    selectedLabel.value = selectedStock.value?.label || symbol
    stockAiReport.value = loadAiReport('stock', symbol)
    finishAnalysisProgress('stock', '个股分析完成')
  } catch (error) {
    clearAnalysisProgress('stock')
    ElMessage.error(error.response?.data?.message || '分析失败')
  } finally {
    stockLoading.value = false
  }
}

let fundamentalRefreshTimer = null
let fundamentalRefreshPromise = null

const refreshStockFundamentals = async ({ silent = false } = {}) => {
  if (activePage.value !== 'stock') return null
  const symbol = stockResult.value?.symbol
  if (!symbol) return null
  if (fundamentalRefreshPromise) return fundamentalRefreshPromise

  fundamentalLoading.value = true
  fundamentalRefreshPromise = fetchFundamentals(symbol)
    .then((fundamentals) => {
      if (stockResult.value?.symbol === symbol) {
        stockResult.value = { ...stockResult.value, fundamentals }
      }
      return fundamentals
    })
    .catch((error) => {
      if (!silent) ElMessage.error(error.response?.data?.message || 'F10 数据加载失败')
      return null
    })
    .finally(() => {
      fundamentalLoading.value = false
      fundamentalRefreshPromise = null
    })
  return fundamentalRefreshPromise
}

const stopFundamentalPolling = () => {
  if (fundamentalRefreshTimer) clearInterval(fundamentalRefreshTimer)
  fundamentalRefreshTimer = null
}

const startFundamentalPolling = async () => {
  if (activePage.value !== 'stock') return
  stopFundamentalPolling()
  const fundamentals = await refreshStockFundamentals({ silent: true })
  const status = fundamentals?.f10Status?.status
  if (!fundamentals || status === 'completed' || status === 'partial') return
  fundamentalRefreshTimer = setInterval(async () => {
    const next = await refreshStockFundamentals({ silent: true })
    const nextStatus = next?.f10Status?.status
    if (nextStatus === 'completed' || nextStatus === 'partial') stopFundamentalPolling()
  }, 2500)
}

const openDisciplineStock = async (row) => {
  if (!row?.symbol || stockLoading.value) return
  disciplineChartPlan.value = row.plan || null
  openStockFromHome(row)
  disciplineChartPlan.value = row.plan || null
  await nextTick()
  await runAnalyze()
}

const runFindMainRiseStock = async () => {
  const minPrice = Number(findMinPrice.value)
  const maxPrice = Number(findMaxPrice.value)
  if (!Number.isFinite(minPrice) || !Number.isFinite(maxPrice) || minPrice < 0 || maxPrice <= 0 || minPrice > maxPrice) {
    ElMessage.warning('请输入有效的股价区间')
    return
  }

  findLoading.value = true
  disciplineChartPlan.value = null
  stockResult.value = null
  stockAiReport.value = null
  try {
    setAnalysisProgress('stock', '寻找主升', MAIN_RISE_STEPS)
    const result = normalizeStockIdentity(await findMainRiseStock({ minPrice, maxPrice }))
    stockResult.value = result
    void rememberAnalyzedStock(result)
    const label = stockLabel(result.symbol, result.name || '已命中主升候选')
    selectedStock.value = { ...result, name: resolveStockName(result.symbol, result.name || '已命中主升候选'), label }
    selectedLabel.value = label
    keyword.value = label
    stockAiReport.value = loadAiReport('stock', result.symbol)
    const meta = result.mainRiseMeta || {}
    const setupType = meta.matchedSetup?.setupType || '主升启动结构'
    ElMessage.success(`找到 ${label}，${setupType}，实际检查 ${meta.attempts ?? 0} 只`)
    finishAnalysisProgress('stock', `已找到主升候选，实际检查 ${meta.attempts ?? 0} 只`)
  } catch (error) {
    const response = error.response
    if (response?.status === 404) {
      const attempts = response.data?.attempts
      const message = response.data?.message || '未找到符合主升启动条件的标的'
      finishAnalysisProgress('stock', `${message}${attempts != null ? `，实际检查 ${attempts} 只` : ''}`)
      ElMessage.warning(`${message}${attempts != null ? `，实际检查 ${attempts} 只` : ''}`)
    } else {
      clearAnalysisProgress('stock')
      ElMessage.error(response?.data?.message || '寻找主升失败')
    }
  } finally {
    findLoading.value = false
  }
}

const runFindGoldenPillarStock = async () => {
  const minPrice = Number(findMinPrice.value)
  const maxPrice = Number(findMaxPrice.value)
  if (!Number.isFinite(minPrice) || !Number.isFinite(maxPrice) || minPrice < 0 || maxPrice <= 0 || minPrice > maxPrice) {
    ElMessage.warning('请输入有效的股价区间')
    return
  }

  goldenLoading.value = true
  disciplineChartPlan.value = null
  try {
    setAnalysisProgress('stock', '寻找黄金柱', GOLDEN_PILLAR_STEPS)
    const result = normalizeStockIdentity(await findGoldenPillarStock({ minPrice, maxPrice }, { maxAttempts: 100, supportTolerance: 0.01 }))
    stockResult.value = result
    void rememberAnalyzedStock(result)
    const label = stockLabel(result.symbol, result.name || '黄金柱待观察')
    selectedStock.value = { ...result, name: resolveStockName(result.symbol, result.name || '黄金柱待观察'), label }
    selectedLabel.value = label
    keyword.value = label
    stockAiReport.value = loadAiReport('stock', result.symbol)
    const meta = result.goldenPillarMeta || {}
    ElMessage.success(
      `找到 ${label}，${meta.status || '黄金柱待观察'}，支撑 ${formatMetaPrice(meta.supportPrice)}${meta.attempts ? `，尝试 ${meta.attempts} 次` : ''}`,
    )
    finishAnalysisProgress('stock', '寻找完成')
  } catch (error) {
    clearAnalysisProgress('stock')
    ElMessage.error(error.response?.data?.message || '寻找黄金柱失败')
  } finally {
    goldenLoading.value = false
  }
}

const selectIndex = (symbol) => {
  if (activeIndexSymbol.value === symbol) return
  activeIndexSymbol.value = symbol
  indexResult.value = indexResults.value[symbol] || null
  indexAiReport.value = loadAiReport('index', symbol)
  loadIndex()
}

const loadIndex = async () => {
  const item = activeIndexItem.value
  // 已有结果且数据仍是最新：直接复用，不请求接口
  const cached = indexResults.value[item.symbol]
  if (cached && isAnalysisFresh(cached, indexQuoteMinutes.value[item.symbol] || '')) {
    indexResult.value = cached
    return
  }
  indexLoading.value = true
  try {
    const indexSteps = INDEX_ANALYSIS_STEPS.map((step, index) => index === 0
      ? { ...step, detail: `正在获取${item.name}近一年日 K 数据...` }
      : step)
    setAnalysisProgress('index', '指数分析', indexSteps)
    const result = await analyzeIndex({ symbol: item.symbol, name: item.name })
    indexResults.value = { ...indexResults.value, [item.symbol]: result }
    indexResult.value = result
    indexQuoteMinutes.value[item.symbol] = marketToday.value?.quoteMinute || ''
    saveAnalysisCache('index', item.symbol, result)
    indexAiReport.value = loadAiReport('index', result?.symbol || item.symbol)
    finishAnalysisProgress('index', `${item.name}分析完成`)
  } catch (error) {
    clearAnalysisProgress('index')
    ElMessage.error(error.response?.data?.message || `${item.name}分析失败`)
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
    const item = activeIndexItem.value
    const intraday = await analyzeIndexIntraday(period, { symbol: item.symbol })
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

const hasAllIntraday = (result) => ['30', '15', '5'].every((period) => result?.intraday?.periods?.[period]?.rawKlines?.length)

const ensureAiReadyIntraday = async (type) => {
  if (type === 'index') {
    if (indexResult.value && !hasAllIntraday(indexResult.value)) {
      const item = activeIndexItem.value
      const intraday = await analyzeIndexIntraday('', { symbol: item.symbol })
      mergeIntradayResult(indexResult.value, intraday)
    }
    return
  }

  if (stockResult.value?.symbol && !hasAllIntraday(stockResult.value)) {
    const intraday = await analyzeStockIntraday(stockResult.value.symbol, '')
    mergeIntradayResult(stockResult.value, intraday)
  }
}

const validateAiSettings = () => {
  if (!currentUser.value) {
    ElMessage.warning('请先登录账户，再使用 AI 分析')
    openAuthDialog('login')
    return false
  }
  if (!aiSettings.value.baseUrl || !aiSettings.value.apiKeyConfigured) {
    ElMessage.warning('请先在当前账户的 AI 设置中填写 URL 和 API Key')
    settingsVisible.value = true
    return false
  }
  return true
}

const runAiAnalyze = async (type) => {
  const result = type === 'index' ? indexResult.value : stockResult.value
  if (!result || !validateAiSettings()) return

  const loadingRef = type === 'index' ? indexAiLoading : stockAiLoading
  loadingRef.value = true
  try {
    await ensureAiReadyIntraday(type)
    const current = type === 'index' ? indexResult.value : stockResult.value
    const report = await analyzeAi({
      result: current,
      intraday: current?.intraday,
      targetType: type,
    })
    saveAiReport(report)
    if (type === 'index') {
      indexAiReport.value = report
    } else {
      stockAiReport.value = report
    }
    ElMessage.success('AI分析已完成并保存到本账户浏览器')
  } catch (error) {
    ElMessage.error(error.response?.data?.message || 'AI分析失败')
  } finally {
    loadingRef.value = false
  }
}

const synchronizeCurrentMarketData = async (force = false) => {
  if (marketSyncInFlight) return marketSyncInFlight
  marketSyncInFlight = refreshMarketData(force)
    .then((data) => {
      marketSync.value = data
      return data
    })
    .catch((error) => {
      marketSync.value = {
        stale: true,
        error: error.response?.data?.message || '全市场行情同步失败',
      }
      return marketSync.value
    })
    .finally(() => {
      marketSyncInFlight = null
    })
  return marketSyncInFlight
}

const loadMarketToday = async ({ silent = false, refreshIndices = false } = {}) => {
  try {
    marketToday.value = await fetchMarketToday(includeGrowthBoards.value, refreshIndices)
    return marketToday.value
  } catch (error) {
    if (!silent) ElMessage.error(error.response?.data?.message || '今日行情加载失败')
    return null
  }
}

const reloadHomeRankings = async () => {
  rankingFilterLoading.value = true
  try {
    await loadMarketToday()
  } finally {
    rankingFilterLoading.value = false
  }
}

const loadPortfolio = async () => {
  if (!currentUser.value) return false
  portfolioLoading.value = true
  try {
    const data = await fetchPortfolio()
    portfolioItems.value = (data.items || []).map((item) => normalizeStockIdentity(item))
    portfolioSummary.value = data.summary || {}
    portfolioMarketData.value = data.marketData || data
    cashBalanceDraft.value = Number(data.summary?.cashBalance ?? data.account?.cashBalance ?? 0)
    if (!portfolioItems.value.length) {
      disciplinePortfolioItems.value = []
      disciplinePortfolioSummary.value = { positionCount: 0, actionCounts: {}, errorCount: 0, plannedRisk: 0 }
    }
  } catch (error) {
    ElMessage.error(error.response?.data?.message || '持仓加载失败')
  } finally {
    portfolioLoading.value = false
  }
}

let homeLoadInFlight = null

const completeHomeRefresh = async (syncTask) => {
  // Keep the first cache read detached from the caller. The page can paint its
  // shell immediately while this request and the provider refresh continue.
  await loadMarketToday()
  await nextTick()
  if (activePage.value === 'home') void refreshHomeMarket({ allowWhileLoading: true })
  await syncTask
  await loadMarketToday()
}

const loadHome = (force = false) => {
  if (homeLoadInFlight) return homeLoadInFlight
  marketLoading.value = true
  const syncTask = synchronizeCurrentMarketData(force)
  homeLoadInFlight = completeHomeRefresh(syncTask)
    .catch(() => null)
    .finally(async () => {
      marketLoading.value = false
      await nextTick()
      scheduleDisciplineWorkspaceHeight()
      homeLoadInFlight = null
    })
  // Do not keep the refresh button or the page transition waiting for the
  // full-market provider call. Cached data is already usable above.
  marketLoading.value = false
  return homeLoadInFlight
}

const refreshHomeMarket = async ({ allowWhileLoading = false } = {}) => {
  if (activePage.value !== 'home' || homeMarketRefreshInFlight || (marketLoading.value && !allowWhileLoading)) return
  homeMarketRefreshInFlight = loadMarketToday({ silent: true, refreshIndices: true })
    .finally(() => {
      homeMarketRefreshInFlight = null
    })
  await homeMarketRefreshInFlight
}

let stocksLoaded = false
let stocksRequestInFlight = null
let stocksRetryTimer = null
let stocksRetryAttempts = 0
const scheduleStocksRetry = () => {
  if (stocksRetryTimer || stocksRetryAttempts >= 2 || !isStockSearchPage()) return
  const delay = stocksRetryAttempts === 0 ? 2000 : 5000
  stocksRetryAttempts += 1
  stocksRetryTimer = window.setTimeout(() => {
    stocksRetryTimer = null
    void loadStocks()
  }, delay)
}
const hydrateCachedStockOptions = () => {
  if (stockOptions.value.length) return false
  const cached = loadJson(STOCK_OPTIONS_CACHE_KEY, null)
  if (!Array.isArray(cached?.items) || !cached.items.length) return false
  stockOptions.value = cached.items.map(enrichStockSearchItem)
  return true
}

const loadStocks = async ({ force = false, silent = true } = {}) => {
  if (!isStockSearchPage()) return false
  if (!force && stocksLoaded) return true
  if (stocksRequestInFlight) return stocksRequestInFlight

  hydrateCachedStockOptions()
  stocksLoading.value = true
  stocksRequestInFlight = (async () => {
    try {
      const items = await fetchStocks()
      const enriched = Array.isArray(items) ? items.map(enrichStockSearchItem) : []
      if (enriched.length) {
        stockOptions.value = enriched
        portfolioItems.value = portfolioItems.value.map((item) => normalizeStockIdentity(item))
        disciplinePortfolioItems.value = disciplinePortfolioItems.value.map((item) => normalizeStockIdentity(item, portfolioItems.value.find((position) => position.symbol === item.symbol)))
        watchlistItems.value = watchlistItems.value.map((item) => normalizeStockIdentity(item))
        disciplineWatchlistItems.value = disciplineWatchlistItems.value.map((item) => normalizeStockIdentity(item, watchlistItems.value.find((watchlist) => watchlist.symbol === item.symbol)))
        tradeHistory.value = tradeHistory.value.map((item) => normalizeStockIdentity(item, portfolioItems.value.find((position) => position.symbol === item.symbol)))
        stockAnalysisHistory.value = mergeStockAnalysisHistory(stockAnalysisHistory.value)
        stocksLoaded = true
        stocksRetryAttempts = 0
        if (stocksRetryTimer) {
          window.clearTimeout(stocksRetryTimer)
          stocksRetryTimer = null
        }
        try { saveJson(STOCK_OPTIONS_CACHE_KEY, { items, savedAt: Date.now() }) } catch { /* optional cache */ }
      }
      return true
    } catch (error) {
      // Cached suggestions and numeric direct lookup remain usable. Only an
      // explicit refresh should turn a transient failure into a toast.
      if (!silent && !stockOptions.value.length) {
        ElMessage.warning(error.response?.data?.message || '股票候选列表暂时无法刷新，仍可直接输入代码')
      }
      scheduleStocksRetry()
      return false
    } finally {
      stocksLoading.value = false
    }
  })()
  try {
    return await stocksRequestInFlight
  } finally {
    stocksRequestInFlight = null
  }
}

let updateLogRequestInFlight = null
let updateLogRetryTimer = null
let updateLogRetryAttempts = 0
const scheduleUpdateLogRetry = () => {
  if (updateLogRetryTimer || updateLogRetryAttempts >= 2) return
  const delay = updateLogRetryAttempts === 0 ? 2000 : 5000
  updateLogRetryAttempts += 1
  updateLogRetryTimer = window.setTimeout(() => {
    updateLogRetryTimer = null
    void loadUpdateLog()
  }, delay)
}
const loadUpdateLog = async ({ force = false, silent = true } = {}) => {
  if (!updateLogs.value.length) loadCachedUpdateLog()
  if (updateLogRequestInFlight && !force) return updateLogRequestInFlight

  updateLogRequestInFlight = (async () => {
    try {
      const data = await fetchUpdateLog()
      if (Array.isArray(data.updates) && data.updates.length) {
        updateLogs.value = data.updates
        updateLogRetryAttempts = 0
        if (updateLogRetryTimer) {
          window.clearTimeout(updateLogRetryTimer)
          updateLogRetryTimer = null
        }
        saveCachedUpdateLog(data)
      }
      return true
    } catch (error) {
      // Keep the cached/current announcement. A temporary API failure should
      // never blank the title-bar announcement or interrupt page loading.
      if (!silent && !updateLogs.value.length) {
        ElMessage.warning(error.response?.data?.message || '更新公告暂时无法刷新')
      }
      scheduleUpdateLogRetry()
      return false
    }
  })()
  try {
    return await updateLogRequestInFlight
  } finally {
    updateLogRequestInFlight = null
  }
}

onMounted(() => {
  setAuthFailureHandler(handleAuthenticationExpired)
  window.addEventListener('popstate', handleBrowserRoute)
  if (isStockWorkspacePage()) restoreAnalysisCaches()
  void restoreAuthentication()
  if (activePage.value === 'stock') void loadStockAnalysisHistory()
  if (isStockSearchPage()) void loadStocks()
  void loadUpdateLog()
  if (activePage.value === 'home') loadHome()
  tickMarketActualTime()
  marketActualTimeTimer = setInterval(tickMarketActualTime, 1000)
  homeMarketRefreshTimer = setInterval(refreshHomeMarket, HOME_MARKET_REFRESH_INTERVAL)
  window.addEventListener('resize', scheduleDisciplineWorkspaceHeight)
  window.visualViewport?.addEventListener('resize', scheduleDisciplineWorkspaceHeight)
  if (typeof ResizeObserver !== 'undefined') {
    viewportWorkspaceResizeObserver = new ResizeObserver(scheduleDisciplineWorkspaceHeight)
    if (homePageRef.value) viewportWorkspaceResizeObserver.observe(homePageRef.value)
    if (disciplinePageRef.value) viewportWorkspaceResizeObserver.observe(disciplinePageRef.value)
  }
  scheduleDisciplineWorkspaceHeight()
})

onUnmounted(() => {
  setAuthFailureHandler(null)
  window.removeEventListener('popstate', handleBrowserRoute)
  if (marketActualTimeTimer) clearInterval(marketActualTimeTimer)
  if (homeMarketRefreshTimer) clearInterval(homeMarketRefreshTimer)
  if (authRestoreRetryTimer) window.clearTimeout(authRestoreRetryTimer)
  if (stocksRetryTimer) window.clearTimeout(stocksRetryTimer)
  if (updateLogRetryTimer) window.clearTimeout(updateLogRetryTimer)
  stopFundamentalPolling()
  if (disciplineWorkspaceFrame) window.cancelAnimationFrame(disciplineWorkspaceFrame)
  window.removeEventListener('resize', scheduleDisciplineWorkspaceHeight)
  window.visualViewport?.removeEventListener('resize', scheduleDisciplineWorkspaceHeight)
  viewportWorkspaceResizeObserver?.disconnect()
})

watch([stockDetailTab, () => stockResult.value?.symbol], ([tab, symbol]) => {
  if (activePage.value === 'stock' && tab === 'fundamental' && symbol) startFundamentalPolling()
  else stopFundamentalPolling()
})

watch(activePage, async (page) => {
  if (page !== 'stock') stopFundamentalPolling()
  nextTick(scheduleDisciplineWorkspaceHeight)
  if (page === 'discipline' && currentUser.value) refreshDisciplineWorkspace()
  else if (page === 'home') loadHome()
  else if (page === 'index') {
    // 先取行情快照用于新鲜度判断，再决定是否请求分析接口
    if (!marketToday.value) await loadMarketToday({ silent: true })
    if (!indexResult.value) {
      const cached = indexResults.value[activeIndexSymbol.value]
      if (cached) indexResult.value = cached
    }
    loadIndex()
  }
  if (isStockSearchPage(page)) void loadStocks()
  if (page === 'stock') void loadStockAnalysisHistory()
  if (isStockWorkspacePage(page) && currentUser.value) void loadAiSettings({ migrateLegacy: true })
})
</script>
