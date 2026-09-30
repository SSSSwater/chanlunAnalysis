<template>
  <section class="binance-workspace-shell" aria-label="Binance 工作区">
    <div class="binance-workspace-panel" role="tabpanel">
      <KeepAlive>
        <BinancePage
          v-if="props.activePage === 'binance'"
          ref="binancePageRef"
          :current-user="currentUser"
          @login-request="$emit('login-request')"
        />
        <BinanceModelResearchPage
          v-else-if="props.activePage === 'binanceModelResearch'"
          @back-to-binance="emit('change-page', 'binance')"
          @model-selected="emit('model-selected', $event)"
        />
        <BinanceBacktestPage
          v-else
          :current-user="currentUser"
          @back-to-binance="emit('change-page', 'binance')"
          @open-model-research="emit('change-page', 'binanceModelResearch')"
        />
      </KeepAlive>
    </div>
  </section>
</template>

<script setup>
import { defineAsyncComponent, nextTick, ref } from 'vue'
const BinanceBacktestPage = defineAsyncComponent(() => import('./BinanceBacktestPage.vue'))
const BinanceModelResearchPage = defineAsyncComponent(() => import('./BinanceModelResearchPage.vue'))
import BinancePage from './BinancePage.vue'

const props = defineProps({
  activePage: { type: String, default: 'binance' },
  currentUser: { type: Object, default: null },
})

const emit = defineEmits(['change-page', 'login-request', 'model-selected'])
const binancePageRef = ref(null)

const openAccountSettings = async () => {
  if (props.activePage !== 'binance') {
    emit('change-page', 'binance')
    await nextTick()
  }
  await binancePageRef.value?.openAccountSettings()
}

defineExpose({ openAccountSettings })
</script>

<style scoped>
.binance-workspace-shell {
  --binance-workspace-bg-position: 66% 46%;
  --binance-workspace-panel-bg: rgba(255, 255, 255, 0.88);
  --binance-workspace-panel-solid: rgba(255, 255, 255, 0.94);
  --binance-workspace-panel-muted: rgba(247, 244, 252, 0.88);
  --binance-workspace-panel-grad: linear-gradient(180deg, rgba(255, 255, 255, 0.94), rgba(246, 241, 251, 0.86));
  --binance-workspace-border-line: rgba(125, 103, 159, 0.42);
  --binance-workspace-border-subtle: rgba(125, 103, 159, 0.26);
  --binance-workspace-shadow-panel: 0 1px 2px rgba(61, 40, 91, 0.05), 0 12px 32px rgba(61, 40, 91, 0.1);
  position: relative;
  isolation: isolate;
  min-width: 0;
}

.binance-workspace-shell::before,
.binance-workspace-shell::after {
  position: fixed;
  inset: 0 0 0 168px;
  pointer-events: none;
}

.binance-workspace-shell::before {
  z-index: -1;
  content: '';
  background-image: url('/binance_bg.png');
  background-position: var(--binance-workspace-bg-position);
  background-repeat: no-repeat;
  background-size: cover;
  filter: saturate(0.82) contrast(0.93);
}

.binance-workspace-shell::after {
  z-index: -1;
  content: '';
  background: rgba(249, 247, 253, 0.6);
  backdrop-filter: blur(2px);
  -webkit-backdrop-filter: blur(2px);
}

.binance-workspace-panel {
  --panel-bg: var(--binance-workspace-panel-bg);
  --panel-solid: var(--binance-workspace-panel-solid);
  --panel-muted: var(--binance-workspace-panel-muted);
  --panel-grad: var(--binance-workspace-panel-grad);
  --border-line: var(--binance-workspace-border-line);
  --border-subtle: var(--binance-workspace-border-subtle);
  --shadow-panel: var(--binance-workspace-shadow-panel);
  position: relative;
  z-index: 1;
  min-width: 0;
}

.binance-workspace-panel :deep(.binance-panel),
.binance-workspace-panel :deep(.binance-backtest-panel) {
  --panel-bg: var(--binance-workspace-panel-bg);
  --panel-solid: var(--binance-workspace-panel-solid);
  --panel-muted: var(--binance-workspace-panel-muted);
  --panel-grad: var(--binance-workspace-panel-grad);
  --border-line: var(--binance-workspace-border-line);
  --border-subtle: var(--binance-workspace-border-subtle);
  --shadow-panel: var(--binance-workspace-shadow-panel);
}

html.dark .binance-workspace-shell {
  --binance-workspace-bg-position: 66% 46%;
  --binance-workspace-panel-bg: rgba(24, 16, 35, 0.84);
  --binance-workspace-panel-solid: rgba(27, 18, 39, 0.92);
  --binance-workspace-panel-muted: rgba(39, 27, 55, 0.84);
  --binance-workspace-panel-grad: linear-gradient(180deg, rgba(43, 30, 60, 0.92), rgba(23, 15, 34, 0.84));
  --binance-workspace-border-line: rgba(130, 105, 161, 0.56);
  --binance-workspace-border-subtle: rgba(130, 105, 161, 0.34);
  --binance-workspace-shadow-panel: 0 1px 2px rgba(0, 0, 0, 0.18), 0 16px 40px rgba(0, 0, 0, 0.22);
}

html.dark .binance-workspace-shell::before {
  filter: saturate(0.72) contrast(0.94) brightness(0.68);
}

html.dark .binance-workspace-shell::after {
  background: rgba(13, 8, 20, 0.6);
}

.binance-workspace-panel {
  min-width: 0;
}

@media (max-width: 820px) {
  .binance-workspace-shell {
    --binance-workspace-bg-position: 67% 42%;
    --binance-workspace-panel-bg: rgba(255, 255, 255, 0.9);
    --binance-workspace-panel-solid: rgba(255, 255, 255, 0.95);
    --binance-workspace-panel-muted: rgba(247, 244, 252, 0.9);
    --binance-workspace-panel-grad: linear-gradient(180deg, rgba(255, 255, 255, 0.95), rgba(246, 241, 251, 0.88));
  }

  .binance-workspace-shell::before,
  .binance-workspace-shell::after {
    inset: 0;
  }

  .binance-workspace-shell::after {
    background: rgba(249, 247, 253, 0.68);
  }

  html.dark .binance-workspace-shell {
    --binance-workspace-bg-position: 67% 42%;
    --binance-workspace-panel-bg: rgba(24, 16, 35, 0.88);
    --binance-workspace-panel-solid: rgba(27, 18, 39, 0.94);
    --binance-workspace-panel-muted: rgba(39, 27, 55, 0.88);
    --binance-workspace-panel-grad: linear-gradient(180deg, rgba(43, 30, 60, 0.94), rgba(23, 15, 34, 0.88));
  }

  html.dark .binance-workspace-shell::after {
    background: rgba(13, 8, 20, 0.6);
  }
}

@media (max-width: 480px) {
  .binance-workspace-shell {
    --binance-workspace-bg-position: 70% 38%;
  }

  html.dark .binance-workspace-shell {
    --binance-workspace-bg-position: 70% 38%;
  }
}
</style>
