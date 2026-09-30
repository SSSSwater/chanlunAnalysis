<template>
  <section v-if="branches.length" class="next-session-plan" aria-label="下次交易">
    <header class="next-session-plan-head">
      <small>下次交易</small>
      <strong>{{ heading }}</strong>
    </header>
    <p class="next-session-plan-headline">{{ summary }}</p>
    <ol class="next-session-plan-branches">
      <li v-for="branch in branches" :key="branch.id" :class="`is-${String(branch.action || 'WAIT').toLowerCase()}`">
        <div class="next-session-plan-branch-head">
          <strong>{{ branchTitle(branch) }}</strong>
          <span v-if="price(branch.triggerPrice)">价位 {{ price(branch.triggerPrice) }}</span>
          <span v-if="branch.shares > 0">{{ branch.shares }} 股</span>
        </div>
        <p>{{ conditionText(branch) }}</p>
        <p><b>操作</b>{{ operationText(branch) }}</p>
      </li>
    </ol>
  </section>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  plan: { type: Object, default: () => ({}) },
})

const branches = computed(() => Array.isArray(props.plan?.branches) ? props.plan.branches : [])
const heading = computed(() => props.plan?.hasPosition ? `持仓 ${props.plan.positionShares || 0} 股` : '准备买入')
const summary = computed(() => props.plan?.hasPosition ? '价格到下面位置再操作。' : '价格到位并确认后再买入。')

function branchTitle(branch) {
  const label = String(branch?.label || '')
  if (/止盈|目标/.test(label)) return '达到止盈'
  if (/止损|防守|跌破/.test(label)) return '跌破止损价'
  if (String(branch?.action || '').toUpperCase() === 'BUY') return '买入条件'
  if (String(branch?.action || '').toUpperCase() === 'REDUCE') return '到价减仓'
  return '继续观察'
}

function conditionText(branch) {
  const label = String(branch?.label || '')
  const action = String(branch?.action || '').toUpperCase()
  if (/止盈|目标/.test(label)) return '日K收盘到达或站上该价。'
  if (/止损|防守|跌破/.test(label)) return '日K收盘跌破该价。'
  if (action === 'BUY') return '日K收盘站上该价，并确认量能。'
  return '等待新的完整K线给出买入条件。'
}

function operationText(branch) {
  const action = String(branch?.action || '').toUpperCase()
  const shares = Number(branch?.shares || 0)
  if (action === 'BUY') return shares > 0 ? `买入 ${shares} 股。` : '确认三个价格后再买入。'
  if (action === 'SELL') return shares > 0 ? `卖出 ${shares} 股。` : '卖出全部持仓。'
  if (action === 'REDUCE') return shares > 0 ? `卖出 ${shares} 股。` : '卖出一部分持仓。'
  return '继续观察。'
}

function price(value) {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number.toFixed(2) : ''
}
</script>
