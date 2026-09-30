<template>
  <main class="binance-model-research-page page-content" aria-labelledby="binance-model-research-page-title">
    <header class="binance-model-research-page-head">
      <div>
        <span class="binance-model-research-kicker">Binance futures · model lab</span>
        <h1 id="binance-model-research-page-title">时序模型研究</h1>
        <p>训练、测试和选择直接生成交易计划的模型。训练任务独立于回测运行，切换页面不会中断轮询。</p>
      </div>
      <el-button plain @click="$emit('back-to-binance')">返回 Binance</el-button>
    </header>
    <BinanceModelResearchPanel @model-selected="$emit('model-selected', $event)" />
  </main>
</template>

<script setup>
import BinanceModelResearchPanel from './BinanceModelResearchPanel.vue'

defineEmits(['back-to-binance', 'model-selected'])
</script>

<style scoped>
.binance-model-research-page { display: grid; gap: 14px; width: 100%; min-width: 0; }
.binance-model-research-page-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; padding: 4px 2px 5px; }
.binance-model-research-page-head > div { display: grid; gap: 4px; min-width: 0; }
.binance-model-research-kicker { color: var(--text-faint); font-family: var(--font-tab); font-size: 10px; font-weight: 900; letter-spacing: .08em; text-transform: uppercase; }
.binance-model-research-page h1 { margin: 0; color: var(--text-primary); font-size: clamp(24px, 3vw, 34px); line-height: 1.05; }
.binance-model-research-page p { margin: 0; color: var(--text-muted); font-size: 12px; line-height: 1.5; }
@media (max-width: 620px) {
  .binance-model-research-page-head { align-items: stretch; flex-direction: column; }
  .binance-model-research-page-head .el-button { width: 100%; }
}
</style>
