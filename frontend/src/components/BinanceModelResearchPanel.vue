<template>
  <section class="binance-ml-panel" aria-labelledby="binance-ml-title">
    <div class="binance-ml-head">
      <div>
        <span>Time-series research</span>
        <h2 id="binance-ml-title">时序模型研究</h2>
      </div>
      <div class="binance-ml-head-actions">
        <i :class="{ 'is-ready': modelAvailable, 'is-busy': trainingRunning }" aria-hidden="true"></i>
        <el-button circle plain :icon="RefreshCw" :loading="loadingStatus" title="刷新模型状态" aria-label="刷新模型状态" @click="loadStatus" />
      </div>
    </div>

    <div class="binance-ml-config-grid">
      <label><span>训练样本</span><el-input-number v-model="config.maxTrainSamples" :min="1000" :max="120000" :step="1000" :precision="0" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>验证样本</span><el-input-number v-model="config.maxValidationSamples" :min="500" :max="30000" :step="500" :precision="0" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>测试样本</span><el-input-number v-model="config.maxTestSamples" :min="500" :max="30000" :step="500" :precision="0" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>采样步长</span><el-input-number v-model="config.sampleStride" :min="1" :max="48" :step="1" :precision="0" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>训练轮数</span><el-input-number v-model="config.epochs" :min="1" :max="50" :step="1" :precision="0" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>批次大小</span><el-input-number v-model="config.batchSize" :min="16" :max="512" :step="16" :precision="0" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>学习率</span><el-input-number v-model="config.learningRate" :min="0.00001" :max="0.01" :step="0.00005" :precision="5" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>权重衰减</span><el-input-number v-model="config.weightDecay" :min="0" :max="0.1" :step="0.00005" :precision="5" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>模型宽度</span><el-input-number v-model="config.dModel" :min="32" :max="192" :step="16" :precision="0" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>注意力头数</span><el-input-number v-model="config.numHeads" :min="2" :max="8" :step="1" :precision="0" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>Transformer层数</span><el-input-number v-model="config.numLayers" :min="1" :max="4" :step="1" :precision="0" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>Dropout</span><el-input-number v-model="config.dropout" :min="0" :max="0.5" :step="0.05" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>随机种子</span><el-input-number v-model="config.seed" :min="1" :max="2147483647" :step="1" :precision="0" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>训练设备</span><el-select v-model="config.device" :disabled="trainingRunning" aria-label="训练设备"><el-option label="自动选择" value="AUTO" /><el-option label="CPU" value="CPU" /><el-option label="CUDA" value="CUDA" /></el-select></label>
      <label><span>每状态回放动作</span><div class="binance-ml-input-suffix"><el-input-number v-model="config.rolloutsPerState" :min="4" :max="24" :step="1" :precision="0" controls-position="right" :disabled="trainingRunning" /><b>次</b></div></label>
      <label><span>推理候选数</span><el-input-number v-model="config.proposalCount" :min="8" :max="64" :step="4" :precision="0" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>风险惩罚</span><el-input-number v-model="config.rewardRiskPenalty" :min="0" :max="2" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>持仓档案</span><el-select v-model="config.holdingProfile" :disabled="trainingRunning" aria-label="模型持仓档案"><el-option label="短线 · 5m 执行" value="SHORT" /><el-option label="摆动 · 15m 执行 / 数天" value="SWING" /><el-option label="趋势持仓 · 1h 执行 / 数周" value="POSITION" /></el-select></label>
      <label><span>推理风险厌恶</span><el-input-number v-model="config.riskAversion" :min="0" :max="3" :step="0.05" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>超过 WAIT 的余量</span><el-input-number v-model="config.selectionMargin" :min="0" :max="2" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>选择分数下限</span><el-input-number v-model="config.selectionFloor" :min="-1" :max="2" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>目标概率权重</span><el-input-number v-model="config.targetProbabilityWeight" :min="0" :max="2" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>目标概率下限</span><el-input-number v-model="config.minimumTargetProbability" :min="0" :max="1" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>成交概率下限</span><el-input-number v-model="config.minimumFillProbability" :min="0" :max="1" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>成交正样本权重</span><el-input-number v-model="config.fillPositiveWeight" :min="0.1" :max="8" :step="0.05" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>目标正样本权重</span><el-input-number v-model="config.targetPositiveWeight" :min="0.1" :max="8" :step="0.05" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>目标覆盖率</span><el-input-number v-model="config.coverageTarget" :min="0" :max="0.5" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>覆盖率权重</span><el-input-number v-model="config.coverageWeight" :min="0" :max="2" :step="0.05" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>最低选择胜率</span><el-input-number v-model="config.minimumWinRate" :min="0.5" :max="0.95" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>校准最少成交</span><el-input-number v-model="config.minimumCalibrationTrades" :min="4" :max="500" :step="4" :precision="0" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>校准置信度</span><el-input-number v-model="config.riskCoverageConfidence" :min="0.5" :max="0.99" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>安全下限</span><el-input-number v-model="config.minimumSafetyLowerBound" :min="0.4" :max="0.8" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>降级胜率容差</span><el-input-number v-model="config.fallbackWinRateTolerance" :min="0" :max="0.15" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>概率参考下限</span><el-input-number v-model="config.relaxedProbabilityFloor" :min="0" :max="0.5" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>不确定性惩罚</span><el-input-number v-model="config.uncertaintyPenalty" :min="0" :max="1" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>质量头损失</span><el-input-number v-model="config.qualityLossWeight" :min="0" :max="2" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>质量排序权重</span><el-input-number v-model="config.qualityScoreWeight" :min="0" :max="2" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>质量回撤惩罚</span><el-input-number v-model="config.qualityRiskWeight" :min="0" :max="2" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>预测方向支持</span><el-input-number v-model="config.forecastSupportWeight" :min="0" :max="2" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label><span>最低交易效用</span><el-input-number v-model="config.minimumTradeUtility" :min="0" :max="1" :step="0.01" :precision="2" controls-position="right" :disabled="trainingRunning" /></label>
      <label class="binance-ml-profile-control"><span>档案语义</span><small class="binance-ml-profile-note">{{ holdingProfileSummary }}。窗口、入场有效期、风险/时间成本由所选档案冻结，避免把摆动或趋势训练误设回超短线。</small></label>
      <label class="binance-ml-profile-control"><span>模型输入</span><el-radio-group v-model="config.inputProfile" disabled aria-label="模型输入周期"><el-radio-button value="MULTI_TIMEFRAME">4h / 1h / 15m / 5m</el-radio-button></el-radio-group></label>
    </div>

    <div class="binance-ml-actions">
      <small>{{ inputSequenceDescription }}；每个历史状态的动作由回放后的净R、风险和时间成本共同评分，模型不学习经典策略计划。</small>
      <el-button type="primary" :icon="BrainCircuit" :loading="trainingStarting || trainingRunning" :disabled="trainingRunning" @click="startTraining">训练、测试并保存</el-button>
    </div>

    <section class="binance-ml-trained" aria-labelledby="binance-ml-trained-title">
      <div class="binance-ml-subhead">
        <span id="binance-ml-trained-title">已保存训练模型</span>
        <small>训练分支、测试集表现与可加载状态</small>
      </div>
      <div v-if="modelCatalogModels.length" class="binance-ml-trained-table" role="table" aria-label="已保存训练模型列表">
        <div class="binance-ml-trained-row is-head" role="row">
          <span>训练任务 / 分支</span><span>测试集性能</span><span>训练与回测参数</span><span>用途</span>
        </div>
        <div v-for="model in modelCatalogModels" :key="`${model.runId}-${model.branch}`" class="binance-ml-trained-row" :class="{ 'is-selected': selectedModelKey === modelKey(model) }" role="row">
          <div><strong>{{ model.runId }}</strong><small>{{ model.branch === 'BEST' ? '验证最佳' : '稳定末期' }} · 第 {{ model.selectedEpoch || '--' }} 轮</small><small>{{ model.taskType === 'DIRECT_PLAN' ? `直接生成完整计划 · ${holdingProfileLabel(model.holdingProfile)}` : '旧候选可行度模型（不可加载）' }} · {{ shortDate(model.updatedAt || model.createdAt) }}</small></div>
          <div><strong>回放效用 {{ rValue(model.test?.selectedRealizedUtility ?? model.test?.meanReplayUtility) }}</strong><small>计划覆盖 {{ percent(model.test?.selectedCoverage) }} · 实际盈利胜率 {{ percent(model.test?.selectedWinRate) }}</small><small>实现R {{ rValue(model.test?.selectedRealizedR ?? model.test?.meanRealizedR) }} · Critic误差 {{ metric(model.test?.criticUtilityMae ?? model.test?.utilityMae) }}</small><small>平均止损 {{ metric(model.test?.selectedMeanStopDistanceAtr) }} ATR · 目标 {{ rValue(model.test?.selectedMeanFirstTargetR) }} / {{ rValue(model.test?.selectedMeanSecondTargetR) }}</small><small>盈利概率误差 {{ metric(model.test?.qualityProfitProbabilityMae) }} · 时间效率误差 {{ metric(model.test?.qualityTimeEfficiencyMae) }}</small></div>
          <div><strong>{{ inputProfileLabel(model.config?.inputProfile) }} · {{ model.config?.epochs || '--' }} epoch</strong><small>样本 {{ model.config?.maxTrainSamples || '--' }} / {{ model.config?.maxValidationSamples || '--' }} / {{ model.config?.maxTestSamples || '--' }} · 步长 {{ model.config?.sampleStride || '--' }}</small><small>超参 d{{ model.config?.dModel || '--' }} · {{ model.config?.numHeads || '--' }}头 · {{ model.config?.numLayers || '--' }}层 · lr {{ hyperparameter(model.config?.learningRate) }} · wd {{ hyperparameter(model.config?.weightDecay) }} · drop {{ metric(model.config?.dropout) }}</small><small>{{ holdingProfileLabel(model.holdingProfile || model.config?.holdingProfile) }}：回放 {{ model.config?.rolloutsPerState || '--' }} 动作 / {{ model.config?.labelHorizonBars ?? '--' }} 根{{ model.executionInterval || model.config?.executionInterval || '--' }}；计划参数由模型输出</small><small>{{ backtestParameterSummary(model) }}</small></div>
          <div class="binance-ml-trained-use"><el-button size="small" :type="selectedModelKey === modelKey(model) ? 'success' : 'primary'" plain :disabled="!model.selectable" @click="selectModel(model)">{{ selectedModelKey === modelKey(model) ? '当前使用' : '用于推理/回测' }}</el-button><small>{{ model.selectable ? (model.eligible ? '通过自动质量门' : '显式选择可用') : (model.availabilityReason || '版本不兼容') }}</small></div>
        </div>
      </div>
      <p v-else class="binance-ml-empty">{{ modelCatalogEmptyMessage }}</p>
    </section>

    <div v-if="activeJob" class="binance-ml-progress" role="status" aria-live="polite">
      <div><span>{{ activeJob.progress?.phase || activeJob.status }}</span><strong>{{ activeJob.progress?.message || '正在准备本地模型任务。' }}</strong></div>
      <div><strong>{{ progressLabel }}</strong><small>{{ activeJob.progress?.currentSymbol || '本地历史库' }}</small></div>
      <el-progress :percentage="progressPercent" :show-text="false" :stroke-width="6" />
    </div>

    <section v-if="epochHistory.length" class="binance-ml-epoch-monitor" aria-labelledby="binance-ml-epoch-title">
      <div class="binance-ml-subhead">
        <span id="binance-ml-epoch-title">逐轮训练监控</span>
        <small>测试集每轮只读评估，不参与 BEST 选择</small>
      </div>
      <div v-if="currentEpochMetrics" class="binance-ml-epoch-live">
        <div><span>当前轮</span><strong>{{ currentEpochMetrics.epoch }} / {{ activeJob?.config?.epochs || latestModel?.config?.epochs || currentEpochMetrics.epoch }}</strong><small>验证最佳第 {{ activeJob?.metrics?.bestEpoch || latestModel?.metrics?.bestEpoch || '--' }} 轮</small></div>
        <div><span>训练损失</span><strong>{{ metric(currentEpochMetrics.trainLoss?.total) }}</strong></div>
        <div><span>验证损失</span><strong>{{ metric(currentEpochMetrics.validation?.loss) }}</strong></div>
        <div><span>测试损失</span><strong>{{ metric(currentEpochMetrics.test?.loss) }}</strong></div>
        <div><span>测试回放效用</span><strong>{{ rValue(currentEpochMetrics.test?.meanReplayUtility) }}</strong></div>
        <div><span>测试动作覆盖</span><strong>{{ percent(currentEpochMetrics.test?.selectedCoverage) }}</strong></div>
      </div>
      <div class="binance-ml-epoch-table" role="table" aria-label="逐轮训练和测试指标">
        <div class="binance-ml-epoch-row is-head" role="row"><span>轮次</span><span>训练损失</span><span>验证损失</span><span>测试损失</span><span>测试回放效用</span><span>测试动作覆盖</span></div>
        <div v-for="row in epochRows" :key="row.epoch" class="binance-ml-epoch-row" role="row"><strong>{{ row.epoch }}</strong><span>{{ metric(row.trainLoss?.total) }}</span><span>{{ metric(row.validation?.loss) }}</span><span>{{ metric(row.test?.loss) }}</span><span>{{ rValue(row.test?.meanReplayUtility) }}</span><span>{{ percent(row.test?.selectedCoverage) }}</span></div>
      </div>
    </section>

    <p v-if="statusError" class="binance-ml-error" role="alert">{{ statusError }}</p>
    <p v-else-if="activeJob?.status === 'FAILED'" class="binance-ml-error" role="alert">{{ activeJob.error || '模型训练未完成' }}</p>

    <template v-if="latestModel">
      <div class="binance-ml-model-meta">
        <span>{{ latestModel.metrics?.model?.architecture || 'multi-resolution-transformer' }}</span>
        <strong>{{ latestModel.metrics?.model?.parameterCount?.toLocaleString?.() || '--' }} 参数</strong>
        <small>{{ latestModel.metrics?.model?.trainingSeconds ?? '--' }}s · 最佳第 {{ latestModel.metrics?.model?.bestEpoch ?? '--' }} 轮 · 稳定末期第 {{ latestModel.metrics?.model?.stableEpoch ?? '--' }} 轮</small>
        <small v-if="targetHeadGateSummary">{{ targetHeadGateSummary }}</small>
        <small v-if="status?.availabilityReason">{{ status.availabilityReason }}</small>
      </div>

      <div v-if="latestModel.metrics?.validation?.selectionCalibration" class="binance-ml-calibration-note">
        校准工作点：{{ latestModel.metrics.validation.selectionCalibration.selectionCalibrationStatus || '--' }} · 覆盖 {{ percent(latestModel.metrics.validation.selectionCalibration.selectionCalibrationCoverage) }} · 经验胜率 {{ percent(latestModel.metrics.validation.selectionCalibration.selectionCalibrationEmpiricalWinRate ?? latestModel.metrics.validation.selectionCalibration.selectionCalibrationWinRate) }} · Wilson 下限 {{ percent(latestModel.metrics.validation.selectionCalibration.selectionCalibrationLowerBound) }}
      </div>

      <div v-if="latestModel.dataset?.split" class="binance-ml-split-grid">
        <div v-for="name in ['train', 'validation', 'test']" :key="name"><span>{{ splitLabel(name) }}</span><strong>{{ latestModel.dataset?.splits?.[name]?.sampleCount || 0 }}</strong><small>{{ shortDate(latestModel.dataset?.split?.[name]?.start) }} - {{ shortDate(latestModel.dataset?.split?.[name]?.end) }}</small></div>
      </div>

      <div v-if="testMetrics" class="binance-ml-metric-grid">
        <div><span>Critic 回放效用误差</span><strong>{{ metric(testMetrics.criticUtilityMae ?? testMetrics.utilityMae) }}</strong><small>测试动作 {{ testMetrics.actionCount ?? testMetrics.sampleCount ?? '--' }}</small></div>
        <div><span>模型选择实现效用</span><strong>{{ rValue(testMetrics.selectedRealizedUtility) }}</strong><small>实现 R {{ rValue(testMetrics.selectedRealizedR) }}</small></div>
        <div><span>选择覆盖率</span><strong>{{ percent(testMetrics.selectedCoverage) }}</strong><small>WAIT {{ percent(testMetrics.waitRate) }}</small></div>
        <div><span>选择胜率</span><strong>{{ percent(testMetrics.selectedWinRate) }}</strong><small>选择后平均回撤 {{ rValue(testMetrics.selectedMeanDrawdown) }}</small></div>
        <div><span>持仓时间</span><strong>{{ metric(testMetrics.selectedMeanDurationBars) }} 根{{ latestModel?.config?.executionInterval || latestModel?.metrics?.model?.executionInterval || '--' }}</strong><small>全动作均值 {{ metric(testMetrics.meanDurationBars) }} 根{{ latestModel?.config?.executionInterval || latestModel?.metrics?.model?.executionInterval || '--' }}</small></div>
        <div><span>目标概率校准误差</span><strong>{{ metric(testMetrics.targetCalibrationMae) }}</strong><small>成交概率误差 {{ metric(testMetrics.fillCalibrationMae) }}</small></div>
        <div><span>计划质量头</span><strong>{{ qualitySummary(testMetrics.qualityMean) }}</strong><small>预期R误差 {{ metric(testMetrics.qualityExpectedRMae) }} · 盈利概率误差 {{ metric(testMetrics.qualityProfitProbabilityMae) }}</small></div>
        <div><span>选择后的计划宽度</span><strong>止损 {{ metric(testMetrics.selectedMeanStopDistanceAtr) }} ATR</strong><small>第一 / 第二目标 {{ rValue(testMetrics.selectedMeanFirstTargetR) }} / {{ rValue(testMetrics.selectedMeanSecondTargetR) }}</small></div>
        <div><span>动作多样性</span><strong>{{ percent(testMetrics.actionDiversity) }}</strong><small>低分位收益与高分位回撤共同参与选择</small></div>
        <div><span>选择后的仓位分配</span><strong>{{ allocationSummary(testMetrics.selectedAllocationMean) }}</strong><small>第一档 / 第二档 / 移动止损剩余；由动作回放结果学习</small></div>
      </div>

      <div v-if="testMetrics?.examples?.length" class="binance-ml-examples">
        <div class="binance-ml-subhead"><span>时间外高置信样本</span><small>显示触发后目标先达的完整条件计划概率，不计单月收益。</small></div>
        <div v-for="item in testMetrics.examples" :key="`${item.symbol}-${item.asOf}`" class="binance-ml-example">
          <strong>{{ item.symbol }}</strong><em :class="directionClass(item.predictedDirection)">{{ directionLabel(item.predictedDirection) }}</em><span>{{ rValue(item.expectedR) }}</span><b :class="outcomeClass(item.actualOutcome)">{{ outcomeLabel(item.actualOutcome) }}</b><small>首盈 {{ percent(item.targetFirstProbability) }} · 实现 {{ rValue(item.actualRealizedR) }} · {{ item.actualDurationBars }} 根{{ latestModel?.config?.executionInterval || '--' }}</small>
        </div>
      </div>
    </template>

    <p v-else-if="!trainingRunning" class="binance-ml-empty">尚无已完成的本地模型训练记录。</p>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import ElMessage from 'element-plus/es/components/message/index.mjs'
import { BrainCircuit, RefreshCw } from 'lucide-vue-next'
import { fetchBinanceFuturesModelRuns, fetchBinanceFuturesModelStatus, fetchBinanceFuturesModelTraining, startBinanceFuturesModelTraining } from '../api'

const emit = defineEmits(['model-selected'])

const NETWORK = 'mainnet'
const MODEL_SELECTION_STORAGE_KEY = 'binance.model.selection'
const config = ref({
  maxTrainSamples: 48000,
  maxValidationSamples: 9000,
  maxTestSamples: 15000,
  sampleStride: 4,
  epochs: 25,
  batchSize: 128,
  learningRate: 0.0003,
  weightDecay: 0.0001,
  dModel: 64,
  numHeads: 4,
  numLayers: 2,
  dropout: 0.1,
  seed: 20260908,
  device: 'AUTO',
  rolloutsPerState: 8,
  proposalCount: 32,
  stateSamplingMultiplier: 3,
  rewardRiskPenalty: 0.22,
  rewardWaitPenalty: 0.05,
  minimumTradeUtility: 0.05,
  riskAversion: 0.35,
  selectionMargin: 0.02,
  selectionFloor: -0.08,
  minimumFillProbability: 0.08,
  minimumTargetProbability: 0.08,
  targetProbabilityWeight: 0.08,
  fillPositiveWeight: 1.25,
  targetPositiveWeight: 1.75,
  coverageTarget: 0.08,
  coverageWeight: 0.85,
  minimumWinRate: 0.55,
  minimumCalibrationTrades: 16,
  riskCoverageConfidence: 0.8,
  minimumSafetyLowerBound: 0.45,
  fallbackWinRateTolerance: 0.03,
  relaxedProbabilityFloor: 0.05,
  uncertaintyPenalty: 0.03,
  qualityLossWeight: 0.1,
  qualityScoreWeight: 0.18,
  qualityRiskWeight: 0.08,
  forecastSupportWeight: 0.05,
  holdingProfile: 'SWING',
  inputProfile: 'MULTI_TIMEFRAME',
})
const status = ref(null)
const activeJob = ref(null)
const loadingStatus = ref(false)
const trainingStarting = ref(false)
const statusError = ref('')
const modelOptions = ref([])
const modelCatalogRuns = ref([])
const modelCatalogRuntimeAvailable = ref(null)
const modelCatalogReason = ref('')
const selectedModelKey = ref('')
let pollTimer = null
let requestId = 0

const modelCatalogModels = computed(() => {
  const runs = Array.isArray(modelCatalogRuns.value) ? modelCatalogRuns.value : []
  const branches = runs.flatMap((run) => Array.isArray(run?.branches) ? run.branches : [])
  const expected = String(status.value?.modelVersion || 'binance-market-to-plan-offline-rl-transformer-v23-5m-relative-wait-risk-coverage')
  const expectedDataset = String(status.value?.datasetVersion || 'binance-action-outcome-jun-aug-2026-v5-5m-context96-search')
  const isCurrentArtifact = (item) => {
    const datasetVersion = String(item?.dataset?.datasetVersion || '')
    return item?.taskType === 'DIRECT_PLAN'
      && (!item?.modelVersion || String(item.modelVersion) === expected)
      && item?.artifactAvailable === true
      && (!datasetVersion || datasetVersion === expectedDataset)
  }
  const current = branches.filter((item) => (
    isCurrentArtifact(item)
  ))
  const options = Array.isArray(modelOptions.value) ? modelOptions.value.filter((item) => (
    isCurrentArtifact(item)
  )) : []
  return current.length ? current : options
})

const latestModel = computed(() => {
  // Do not render the previous checkpoint's report beside a new in-flight
  // run; that was easily mistaken for the metrics of the current training.
  if (['QUEUED', 'RUNNING'].includes(activeJob.value?.status)) return null
  const candidate = status.value?.latestModel || (activeJob.value?.status === 'COMPLETED' ? activeJob.value : null)
  if (!candidate) return null
  const expected = String(status.value?.modelVersion || '').trim()
  const actual = String(candidate?.metrics?.model?.modelVersion || candidate?.modelVersion || candidate?.config?.modelVersion || '').trim()
  // A v16/15m report can remain in the database after the ABI changes, but
  // it must not be presented as the current 5m training result.
  return expected && actual && expected !== actual ? null : candidate
})
const testMetrics = computed(() => latestModel.value?.metrics?.test || null)
const latestModelIsCurrent = computed(() => {
  const expected = String(status.value?.modelVersion || '').trim()
  const actual = String(latestModel.value?.metrics?.model?.modelVersion || latestModel.value?.modelVersion || '').trim()
  return Boolean(expected && actual && expected === actual)
})
const modelAvailable = computed(() => Boolean(status.value?.available || status.value?.explicitUseAvailable))
const trainingRunning = computed(() => ['QUEUED', 'RUNNING'].includes(activeJob.value?.status))
const targetHeadGateSummary = computed(() => {
  const gate = latestModel.value?.metrics?.calibration?.targetHeadEvidence
  if (!gate || typeof gate !== 'object') return ''
  const state = (side) => gate?.[side]?.accepted ? '保留' : '回退基础率'
  return `成交后目标头：多头${state('long')}，空头${state('short')}`
})
const holdingProfileDefinitions = {
  SHORT: { label: '短线', execution: '5m', horizon: 144, expiry: 18 },
  SWING: { label: '摆动', execution: '15m', horizon: 480, expiry: 48 },
  POSITION: { label: '趋势持仓', execution: '1h', horizon: 336, expiry: 36 },
}
const holdingProfileLabel = (value) => holdingProfileDefinitions[String(value || '').toUpperCase()]?.label || '周期档案未知'
const holdingProfileSummary = computed(() => {
  const profile = holdingProfileDefinitions[String(config.value.holdingProfile || 'SWING').toUpperCase()] || holdingProfileDefinitions.SWING
  return `${profile.label}：${profile.execution} 管理与回放，最多 ${profile.horizon} 根，入场有效 ${profile.expiry} 根；5m 仅作为多周期输入与入场时机`
})
const inputSequenceDescription = computed(() => `${holdingProfileSummary.value}；模型输入固定使用 4h / 1h / 15m / 5m 已收盘序列，历史训练和回测不读取 1m`)
const progressPercent = computed(() => {
  const total = Number(activeJob.value?.progress?.total || 0)
  const completed = Number(activeJob.value?.progress?.completed || 0)
  return total > 0 ? Math.max(0, Math.min(100, Math.round(completed / total * 100))) : 0
})
const modelCatalogEmptyMessage = computed(() => {
  if (modelCatalogRuntimeAvailable.value === false && modelCatalogReason.value) {
    return `已有训练记录，但当前后端不能加载模型文件：${modelCatalogReason.value}。请安装运行时后刷新。`
  }
  return '尚无可直接加载的 BEST/STABLE 模型文件；训练完成后会自动出现在这里。'
})
const progressLabel = computed(() => {
  const total = Number(activeJob.value?.progress?.total || 0)
  const completed = Number(activeJob.value?.progress?.completed || 0)
  return total > 0 ? `${completed} / ${total}` : '运行中'
})
const epochHistory = computed(() => {
  const rows = activeJob.value?.metrics?.epochs || (latestModelIsCurrent.value ? latestModel.value?.metrics?.epochs : null)
  return Array.isArray(rows) ? rows.filter((row) => row && row.epoch != null) : []
})
const currentEpochMetrics = computed(() => epochHistory.value.length ? epochHistory.value[epochHistory.value.length - 1] : null)
const epochRows = computed(() => epochHistory.value.slice(-12).reverse())

const message = (error) => error?.response?.data?.message || error?.message || '时序模型服务暂时不可用'
const metric = (value) => Number.isFinite(Number(value)) ? Number(value).toFixed(3) : '--'
const hyperparameter = (value) => Number.isFinite(Number(value)) ? Number(value).toExponential(2) : '--'
const rValue = (value) => Number.isFinite(Number(value)) ? `${Number(value).toFixed(3)}R` : '--'
const percent = (value) => Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(1)}%` : '--'
const allocationSummary = (value) => Array.isArray(value) && value.length >= 3
  ? value.slice(0, 3).map((item) => `${Number(item).toFixed(1)}%`).join(' / ')
  : '--'
const qualitySummary = (value) => Array.isArray(value) && value.length >= 5
  ? `R ${Number(value[0]).toFixed(2)} · 盈利概率 ${(Number(value[1]) * 100).toFixed(1)}% · 风险 ${Number(value[2]).toFixed(2)}`
  : '--'
const splitLabel = (name) => ({ train: '训练', validation: '验证', test: '测试' }[name] || name)
const shortDate = (value) => value ? String(value).replace('T', ' ').replace(':00Z', 'Z').slice(5, 16) : '--'
const directionLabel = (value) => ({ LONG: '做多', SHORT: '做空' }[String(value || '').toUpperCase()] || '等待')
const outcomeLabel = (value) => ({ TARGET: '目标先达', STOP: '止损先达', EXPIRE: '窗口结束' }[String(value || '').toUpperCase()] || '--')
const directionClass = (value) => String(value || '').toUpperCase() === 'SHORT' ? 'is-short' : 'is-long'
const outcomeClass = (value) => String(value || '').toUpperCase() === 'TARGET' ? 'is-target' : String(value || '').toUpperCase() === 'STOP' ? 'is-stop' : 'is-neutral'
const modelKey = (model) => `${model?.runId || ''}:${model?.branch || ''}`
const inputProfileLabel = () => '4h/1h/15m/5m'
const backtestParameterSummary = (model) => {
  const config = model?.config && typeof model.config === 'object' ? model.config : {}
  const device = String(config.device || 'AUTO').toUpperCase()
  return `保存检查点：${model?.branch || '--'} · ${holdingProfileLabel(model?.holdingProfile || config.holdingProfile)} · ${device} · seed ${config.seed ?? '--'}`
}
const selectModel = (model) => {
  if (!model?.runId) return
  selectedModelKey.value = modelKey(model)
  const selection = { runId: String(model.runId), branch: String(model.branch || 'BEST').toUpperCase(), model }
  if (typeof window !== 'undefined') {
    try { window.localStorage.setItem(MODEL_SELECTION_STORAGE_KEY, JSON.stringify({ runId: selection.runId, branch: selection.branch })) } catch {}
    window.dispatchEvent(new CustomEvent('binance-model-selected', { detail: selection }))
  }
  emit('model-selected', selection)
}

const clearTimer = () => {
  if (pollTimer && typeof window !== 'undefined') window.clearTimeout(pollTimer)
  pollTimer = null
}
const loadStatus = async () => {
  if (loadingStatus.value) return
  loadingStatus.value = true
  try {
    const next = await fetchBinanceFuturesModelStatus({ network: NETWORK })
    if (next) {
      status.value = next
      if (next.activeJob && (!activeJob.value || activeJob.value.id !== next.activeJob.id || !trainingRunning.value)) {
        activeJob.value = next.activeJob
        // Reconnect polling after a page refresh so an already-running job
        // continues to stream epoch metrics into the panel.
        if (next.activeJob.id && !pollTimer) {
          const id = ++requestId
          void pollJob(next.activeJob.id, id)
        }
      }
      statusError.value = ''
      if (!modelCatalogRuns.value.length) void loadModelRuns()
    }
  } catch (error) {
    statusError.value = message(error)
  } finally {
    loadingStatus.value = false
  }
}
const loadModelRuns = async () => {
  try {
    const result = await fetchBinanceFuturesModelRuns({ network: NETWORK, limit: 24 })
    modelCatalogRuns.value = Array.isArray(result?.runs) ? result.runs : []
    modelCatalogRuntimeAvailable.value = result?.runtimeAvailable !== false
    modelCatalogReason.value = String(result?.runtimeReason || '')
    modelOptions.value = Array.isArray(result?.models) ? result.models : []
  } catch (error) {
    modelCatalogRuns.value = []
    modelCatalogRuntimeAvailable.value = null
    modelCatalogReason.value = ''
    if (!statusError.value) statusError.value = message(error)
  }
}
const pollJob = async (jobId, id) => {
  try {
    const job = await fetchBinanceFuturesModelTraining(jobId)
    if (id !== requestId || !job) return
    activeJob.value = job
    trainingStarting.value = false
    if (['QUEUED', 'RUNNING'].includes(job.status)) {
      pollTimer = window.setTimeout(() => pollJob(jobId, id), 800)
      return
    }
    clearTimer()
    await loadStatus()
    await loadModelRuns()
    if (job.status === 'COMPLETED') ElMessage.success('时序模型训练与时间外测试完成')
    else ElMessage.error(job.error || '时序模型训练失败')
  } catch (error) {
    if (id !== requestId) return
    statusError.value = message(error)
    pollTimer = window.setTimeout(() => pollJob(jobId, id), 1500)
  }
}
const startTraining = async () => {
  if (trainingRunning.value || trainingStarting.value) return
  clearTimer()
  const id = ++requestId
  trainingStarting.value = true
  statusError.value = ''
  try {
    const job = await startBinanceFuturesModelTraining({ network: NETWORK, config: { ...config.value } })
    if (!job?.id) throw new Error('模型训练任务未创建')
    if (id !== requestId) return
    activeJob.value = job
    await pollJob(job.id, id)
  } catch (error) {
    if (id !== requestId) return
    trainingStarting.value = false
    statusError.value = message(error)
    ElMessage.error(statusError.value)
  }
}

onMounted(() => { void loadStatus(); void loadModelRuns() })
onUnmounted(() => { requestId += 1; clearTimer() })
</script>

<style scoped>
.binance-ml-panel { display: grid; gap: 12px; padding: 15px; border: 1px solid var(--border-line); background: var(--panel-solid); }
.binance-ml-head, .binance-ml-head-actions, .binance-ml-actions, .binance-ml-model-meta, .binance-ml-subhead, .binance-ml-example { display: flex; align-items: center; }
.binance-ml-head { justify-content: space-between; gap: 12px; }
.binance-ml-head > div:first-child { display: grid; gap: 3px; }
.binance-ml-head span, .binance-ml-subhead > span { color: var(--text-faint); font-family: var(--font-tab); font-size: 9px; font-weight: 900; text-transform: uppercase; }
.binance-ml-head h2 { margin: 0; color: var(--text-primary); font-size: 16px; line-height: 1.2; }
.binance-ml-head-actions { gap: 8px; }
.binance-ml-head-actions > i { width: 8px; height: 8px; border-radius: 50%; background: var(--text-faint); }
.binance-ml-head-actions > i.is-ready { background: var(--binance-up); }
.binance-ml-head-actions > i.is-busy { background: var(--binance-gold); }
.binance-ml-config-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px 10px; }
.binance-ml-config-grid label { display: grid; gap: 4px; min-width: 0; }
.binance-ml-profile-control { grid-column: 1 / -1; }
.binance-ml-profile-control :deep(.el-radio-group) { display: flex; width: 100%; }
.binance-ml-profile-control :deep(.el-radio-button) { flex: 1; min-width: 0; }
.binance-ml-profile-control :deep(.el-radio-button__inner) { width: 100%; overflow-wrap: anywhere; }
.binance-ml-config-grid label > span, .binance-ml-actions small, .binance-ml-progress span, .binance-ml-progress small, .binance-ml-model-meta span, .binance-ml-model-meta small, .binance-ml-split-grid span, .binance-ml-split-grid small, .binance-ml-metric-grid span, .binance-ml-metric-grid small, .binance-ml-subhead small, .binance-ml-example small { color: var(--text-faint); font-size: 9px; font-weight: 700; line-height: 1.35; }
.binance-ml-config-grid :deep(.el-input-number) { width: 100%; }
.binance-ml-input-suffix { display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 5px; }
.binance-ml-input-suffix b { color: var(--text-muted); font-family: var(--font-num); font-size: 10px; }
.binance-ml-actions { justify-content: space-between; align-items: flex-end; gap: 12px; padding-top: 10px; border-top: 1px solid var(--border-subtle); }
.binance-ml-actions small { max-width: 440px; }
.binance-ml-progress { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 5px 12px; padding: 10px; border-left: 3px solid var(--binance-gold); background: var(--panel-muted); }
.binance-ml-progress > div { display: grid; gap: 2px; }
.binance-ml-progress > div:last-of-type { justify-items: end; text-align: right; }
.binance-ml-progress strong { color: var(--text-primary); font-size: 11px; line-height: 1.35; }
.binance-ml-progress :deep(.el-progress) { grid-column: 1 / -1; }
.binance-ml-epoch-monitor { display: grid; gap: 1px; border: 1px solid var(--border-line); background: var(--border-line); }
.binance-ml-epoch-live { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 1px; background: var(--border-line); }
.binance-ml-epoch-live > div { display: grid; gap: 3px; min-width: 0; padding: 8px; background: var(--panel-solid); }
.binance-ml-epoch-live span, .binance-ml-epoch-live small, .binance-ml-epoch-row span { color: var(--text-faint); font-size: 9px; font-weight: 700; line-height: 1.35; }
.binance-ml-epoch-live strong { color: var(--text-primary); font-family: var(--font-num); font-size: 13px; overflow-wrap: anywhere; }
.binance-ml-epoch-table { display: grid; gap: 1px; overflow-x: auto; background: var(--border-line); }
.binance-ml-epoch-row { display: grid; grid-template-columns: 58px repeat(5, minmax(88px, 1fr)); gap: 8px; min-width: 560px; align-items: center; padding: 7px 9px; background: var(--panel-solid); }
.binance-ml-epoch-row.is-head { background: var(--panel-muted); }
.binance-ml-epoch-row.is-head span { color: var(--text-faint); font-family: var(--font-tab); font-size: 9px; font-weight: 900; }
.binance-ml-epoch-row strong, .binance-ml-epoch-row > span { color: var(--text-primary); font-family: var(--font-num); font-size: 10px; }
.binance-ml-epoch-row.is-head > span { font-family: var(--font-tab); }
.binance-ml-error { margin: 0; padding: 9px 10px; border-left: 3px solid var(--binance-down); background: var(--panel-muted); color: var(--binance-down); font-size: 10px; line-height: 1.4; }
.binance-ml-model-meta { flex-wrap: wrap; gap: 4px 10px; padding-top: 9px; border-top: 1px solid var(--border-subtle); }
.binance-ml-model-meta strong { color: var(--text-primary); font-family: var(--font-num); font-size: 11px; }
.binance-ml-calibration-note { padding: 7px 9px; border: 1px solid var(--border-subtle); color: var(--text-muted); font-size: 10px; line-height: 1.45; }
.binance-ml-split-grid, .binance-ml-metric-grid { display: grid; gap: 1px; border: 1px solid var(--border-line); background: var(--border-line); }
.binance-ml-split-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.binance-ml-metric-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.binance-ml-split-grid > div, .binance-ml-metric-grid > div { display: grid; gap: 3px; min-width: 0; padding: 9px; background: var(--panel-solid); }
.binance-ml-split-grid strong, .binance-ml-metric-grid strong { color: var(--text-primary); font-family: var(--font-num); font-size: 14px; overflow-wrap: anywhere; }
.binance-ml-examples { display: grid; gap: 1px; border: 1px solid var(--border-line); background: var(--border-line); }
.binance-ml-trained { display: grid; gap: 1px; border: 1px solid var(--border-line); background: var(--border-line); }
.binance-ml-trained-table { display: grid; gap: 1px; background: var(--border-line); overflow-x: auto; }
.binance-ml-trained-row { display: grid; grid-template-columns: minmax(150px, 1.05fr) minmax(170px, 1.2fr) minmax(210px, 1.45fr) minmax(116px, .7fr); gap: 10px; min-width: 720px; padding: 9px; background: var(--panel-solid); }
.binance-ml-trained-row.is-head { padding-top: 7px; padding-bottom: 7px; background: var(--panel-muted); }
.binance-ml-trained-row.is-head > span { color: var(--text-faint); font-family: var(--font-tab); font-size: 9px; font-weight: 900; }
.binance-ml-trained-row.is-selected { box-shadow: inset 3px 0 0 var(--binance-gold); background: color-mix(in srgb, var(--binance-gold) 6%, var(--panel-solid)); }
.binance-ml-trained-row > div { display: grid; align-content: start; gap: 3px; min-width: 0; }
.binance-ml-trained-row strong { color: var(--text-primary); font-family: var(--font-num); font-size: 10px; font-weight: 900; line-height: 1.35; overflow-wrap: anywhere; }
.binance-ml-trained-row small { color: var(--text-faint); font-size: 9px; line-height: 1.35; overflow-wrap: anywhere; }
.binance-ml-trained-use { align-items: start; }
.binance-ml-trained-use .el-button { justify-self: start; }
.binance-ml-subhead { justify-content: space-between; gap: 10px; padding: 8px 9px; background: var(--panel-muted); }
.binance-ml-subhead small { text-align: right; }
.binance-ml-example { flex-wrap: wrap; gap: 4px 8px; padding: 8px 9px; background: var(--panel-solid); }
.binance-ml-example strong { color: var(--text-primary); font-family: var(--font-num); font-size: 11px; }
.binance-ml-example em, .binance-ml-example b { font-size: 10px; font-style: normal; font-weight: 900; }
.binance-ml-example span { color: var(--text-muted); font-family: var(--font-num); font-size: 10px; font-weight: 800; }
.binance-ml-example small { margin-left: auto; }
.binance-ml-example .is-long { color: var(--binance-direction-long); }
.binance-ml-example .is-short { color: var(--binance-direction-short); }
.binance-ml-example .is-target { color: var(--binance-up); }
.binance-ml-example .is-stop { color: var(--binance-down); }
.binance-ml-example .is-neutral { color: var(--text-muted); }
.binance-ml-empty { margin: 0; color: var(--text-faint); font-size: 10px; }
@media (max-width: 720px) {
  .binance-ml-config-grid, .binance-ml-metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .binance-ml-epoch-live { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .binance-ml-actions { align-items: stretch; flex-direction: column; }
  .binance-ml-actions .el-button { width: 100%; }
}
@media (max-width: 420px) {
  .binance-ml-config-grid, .binance-ml-split-grid, .binance-ml-metric-grid { grid-template-columns: 1fr; }
  .binance-ml-epoch-live { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .binance-ml-progress { grid-template-columns: 1fr; }
  .binance-ml-progress > div:last-of-type { justify-items: start; text-align: left; }
  .binance-ml-example small { flex-basis: 100%; margin-left: 0; }
  .binance-ml-profile-control :deep(.el-radio-group) { display: grid; grid-template-columns: 1fr; }
}
</style>
