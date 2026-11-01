<script setup>
import { inject, computed, ref, onMounted, onUnmounted } from 'vue'
import { getFeatures, authFetch } from '../utils/auth.js'
import TiBatchUpload from './TiBatchUpload.vue'
import { loadBatches, saveBatch } from '../utils/tiBatch.js'

// 点击某个队列卡片时通知父组件打开全屏任务列表
// payload: { type: 'extract' | 'ti' | 'callback', tab?: 'pending' | 'failed' | 'success' }
const emit = defineEmits(['open-queue'])

// 功能模块开关：控制队列卡片的显示
const features = getFeatures()
// 模型提取队列是 NER / ATT&CK 共用的，两者全关时才隐藏
const showExtractQueue = computed(() => features.ner || features.attack)

const sseData     = inject('sseData')
// SSE 字段可能缺失（对应功能未启用时后端不下发），统一容错
const queueSize   = computed(() => sseData.value.queue_size ?? 0)
const workers     = computed(() => sseData.value.workers || [])
const busyWorkers = computed(() => workers.value.filter(w => w.busy).length)
const totalWorkers= computed(() => workers.value.length)

// 联网情报（TI）队列：每平台一个串行 worker
const TI_PLATFORM_NAMES = { threatbook: '微步在线', qianxin: '奇安信' }
const tiPlatforms = computed(() => {
  const plats = sseData.value.ti_queue?.platforms
  if (!plats) return []
  return Object.values(plats).map(p => ({
    ...p,
    name: TI_PLATFORM_NAMES[p.platform] || p.platform,
  }))
})
const tiBusyCount = computed(() => tiPlatforms.value.filter(p => p.busy).length)
// qsize 只含等待任务；串行 worker 执行中的任务已出队，统计时补上
const tiWaitingTotal = computed(() =>
  tiPlatforms.value.reduce((sum, p) => sum + p.queue_size + (p.busy ? 1 : 0), 0)
)

// 回调队列相关
const callbackQueue = computed(() => sseData.value.callback_queue || null)
const callbackFailedQueue = computed(() => sseData.value.callback_failed_queue || null)
const busyCallbackWorkers = computed(() => {
  if (!callbackQueue.value?.workers) return 0
  return callbackQueue.value.workers.filter(w => w.busy).length
})
const totalCallbackWorkers = computed(() => callbackQueue.value?.workers?.length || 0)

function openQueue(type, tab) {
  emit('open-queue', { type, tab })
}

// ─── TI 批量任务：上传弹窗 + 后台恢复跟踪 ─────────────────────────────────────
const showBatchUpload = ref(false)

// 批次任务详情（task_id → GET /ti/tasks/{id} 响应），仅内存态；批次本体在 localStorage
const batchDetails = ref({})
let batchPollTimer = null

const TERMINAL_STATUS = ['done', 'failed']

/** 需要后台跟踪的批次：未跑完 */
function activeBatches() {
  if (!features.ti) return []
  return Object.values(loadBatches()).filter(b => b && Array.isArray(b.tasks) && !b.finished)
}

async function pollBatches() {
  const batches = activeBatches()
  if (!batches.length) return
  for (const batch of batches) {
    // 提交中的批次由提交流程自己落盘，避免任务未齐时误判 finished
    if (batch.submit_done === false) continue
    await Promise.all(batch.tasks
      .filter(t => !TERMINAL_STATUS.includes(batchDetails.value[t.task_id]?.status))
      .map(async t => {
        try {
          const res = await authFetch(`/ti/tasks/${t.task_id}`)
          if (res.ok) batchDetails.value = { ...batchDetails.value, [t.task_id]: await res.json() }
          // 任务被删除（404）视为终态失败，否则批次永远跑不完、提示残留
          else if (res.status === 404) batchDetails.value = { ...batchDetails.value, [t.task_id]: { status: 'failed', error_msg: '任务已被删除' } }
        } catch { /* 网络异常下一轮再试 */ }
      }))
    if (!batch.tasks.length) {
      // 提交中断留下的空批次（创建超过 3 分钟仍无任务）视为结束，避免状态提示残留
      if (Date.now() - new Date(batch.created_at).getTime() > 3 * 60 * 1000) {
        batch.finished = true
        saveBatch(batch)
      }
      continue
    }
    const allDone = batch.tasks.every(t => TERMINAL_STATUS.includes(batchDetails.value[t.task_id]?.status))
    if (allDone) {
      batch.finished = true
      saveBatch(batch)
    }
  }
}

// TI 卡片上的小字状态提示：汇总进行中批次的进度
const batchStatusText = computed(() => {
  const batches = activeBatches()
  if (!batches.length) return ''
  let total = 0
  let finished = 0
  for (const b of batches) {
    total += b.tasks.length
    finished += b.tasks.filter(t => TERMINAL_STATUS.includes(batchDetails.value[t.task_id]?.status)).length
  }
  return `批量任务进行中：已完成 ${finished}/${total}`
})

onMounted(() => {
  if (!features.ti) return
  pollBatches()
  batchPollTimer = setInterval(pollBatches, 5000)
})
onUnmounted(() => { if (batchPollTimer) clearInterval(batchPollTimer) })
</script>

<template>
  <div class="queue-status">
    <div class="page-header">
      <h2>队列状态</h2>
      <span class="last-update">
        最后更新 {{ sseData.timestamp ? new Date(sseData.timestamp).toLocaleTimeString() : '—' }}
      </span>
    </div>
    <p class="page-hint">三条队列相互独立，点击卡片查看该队列的任务列表</p>

    <div class="queue-cards">
      <!-- ① 模型提取队列（NER / ATT&CK 共用，两者全关时隐藏） -->
      <div v-if="showExtractQueue" class="queue-card" @click="openQueue('extract')">
        <div class="queue-card-header">
          <span class="queue-card-title">模型提取队列</span>
          <span class="queue-card-arrow">查看任务列表 →</span>
        </div>
        <div class="queue-card-stats">
          <div class="stat">
            <span class="stat-label">等待任务</span>
            <span class="stat-value" :class="{ warn: queueSize > 10 }">{{ queueSize }}</span>
          </div>
          <div class="stat">
            <span class="stat-label">忙碌 Worker</span>
            <span class="stat-value">{{ busyWorkers }} / {{ totalWorkers }}</span>
          </div>
        </div>
        <div class="workers">
          <div
            v-for="w in workers"
            :key="w.worker_id"
            class="worker-card"
            :class="w.busy ? 'busy' : 'idle'"
          >
            <div class="worker-id">Worker #{{ w.worker_id }}</div>
            <div class="worker-status">{{ w.busy ? '忙碌' : '空闲' }}</div>
            <template v-if="w.busy">
              <div class="worker-model">{{ w.model }}</div>
              <div class="worker-task">{{ w.task_id?.slice(0, 8) }}…</div>
            </template>
          </div>
        </div>
      </div>

      <!-- ② 联网解析队列（TI 功能关闭时隐藏） -->
      <div v-if="features.ti" class="queue-card" @click="openQueue('ti')">
        <div class="queue-card-header">
          <span class="queue-card-title">联网解析队列</span>
          <span class="queue-card-arrow">查看任务列表 →</span>
        </div>
        <div class="queue-card-stats">
          <div class="stat">
            <span class="stat-label">等待任务</span>
            <span class="stat-value">{{ tiWaitingTotal }}</span>
          </div>
          <div class="stat">
            <span class="stat-label">查询中平台</span>
            <span class="stat-value">{{ tiBusyCount }} / {{ tiPlatforms.length }}</span>
          </div>
        </div>
        <div class="workers">
          <div
            v-for="p in tiPlatforms"
            :key="p.platform"
            class="worker-card ti-worker"
            :class="p.busy ? 'busy' : 'idle'"
          >
            <div class="worker-id">{{ p.name }}</div>
            <div class="worker-status">{{ p.busy ? '查询中' : '空闲' }}</div>
            <!-- qsize 只含等待任务；串行 worker 执行中的任务已出队，显示时补上 -->
            <div class="worker-model" :title="`等待 ${p.queue_size} + 执行中 ${p.busy ? 1 : 0}；上限仅约束等待队列`">
              队列 {{ p.queue_size + (p.busy ? 1 : 0) }} / {{ p.queue_max }}
            </div>
            <div v-if="p.busy" class="worker-task">{{ p.task_id?.slice(0, 8) }}…</div>
          </div>
          <div v-if="!tiPlatforms.length" class="worker-card idle">
            <div class="worker-id">平台</div>
            <div class="worker-status">未启用</div>
          </div>
        </div>
        <div v-if="batchStatusText" class="batch-status">{{ batchStatusText }}</div>
        <button class="batch-upload-btn" @click.stop="showBatchUpload = true">
          批量任务 / 任务组
        </button>
      </div>

      <!-- ③ 回调队列 -->
      <div class="queue-card" @click="openQueue('callback')">
        <div class="queue-card-header">
          <span class="queue-card-title">回调队列</span>
          <span class="queue-card-arrow">查看任务列表 →</span>
        </div>
        <div class="queue-card-stats">
          <div class="stat">
            <span class="stat-label">等待回调</span>
            <span class="stat-value" :class="{ warn: callbackQueue && callbackQueue.size > 50 }">
              {{ callbackQueue ? `${callbackQueue.size} / ${callbackQueue.max_size}` : '—' }}
            </span>
          </div>
          <div class="stat">
            <span class="stat-label">忙碌 Worker</span>
            <span class="stat-value">{{ busyCallbackWorkers }} / {{ totalCallbackWorkers }}</span>
          </div>
          <div class="stat">
            <span class="stat-label">失败记录</span>
            <span
              class="stat-value"
              :class="{ warn: callbackFailedQueue && callbackFailedQueue.size > 0 }"
            >
              {{ callbackFailedQueue ? `${callbackFailedQueue.size} / ${callbackFailedQueue.max_size}` : 0 }}
            </span>
          </div>
        </div>
        <div class="workers">
          <div
            v-for="w in callbackQueue?.workers || []"
            :key="w.worker_id"
            class="worker-card callback-worker"
            :class="w.busy ? 'busy' : 'idle'"
          >
            <div class="worker-id">Callback Worker #{{ w.worker_id }}</div>
            <div class="worker-status">{{ w.busy ? '忙碌' : '空闲' }}</div>
            <div v-if="w.busy && w.url" class="worker-url" :title="w.url">{{ w.url }}</div>
          </div>
        </div>
        <button
          v-if="callbackFailedQueue && callbackFailedQueue.size > 0"
          class="failed-badge"
          @click.stop="openQueue('callback', 'failed')"
        >
          查看失败记录 ({{ callbackFailedQueue.size }})
        </button>
      </div>
    </div>

    <!-- TI 批量任务上传弹窗（保持挂载，关闭不中断提交/跟踪） -->
    <TiBatchUpload :visible="showBatchUpload" @close="showBatchUpload = false" />
  </div>
</template>

<style scoped>
.queue-status { max-width: 1600px; margin: 0 auto; }

.page-header { display: flex; align-items: baseline; gap: 14px; }
.page-header h2 { font-size: 20px; color: #333; }
.last-update { font-size: 12px; color: #999; }
.page-hint { font-size: 13px; color: #888; margin: 6px 0 18px; }

.queue-cards {
  display: grid;
  grid-template-columns: 1fr;
  gap: 20px;
  align-items: start;
}

.queue-card {
  background: #fff;
  border-radius: 10px;
  padding: 24px;
  box-shadow: 0 1px 3px rgba(0,0,0,.1);
  border: 1px solid #e8e8e8;
  cursor: pointer;
  transition: box-shadow .18s, transform .18s, border-color .18s;
}
.queue-card:hover {
  box-shadow: 0 6px 18px rgba(0,0,0,.14);
  transform: translateY(-2px);
  border-color: #5470c6;
}

.queue-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-bottom: 14px;
  margin-bottom: 18px;
  border-bottom: 1px solid #f0f0f0;
}
.queue-card-title { font-size: 17px; font-weight: 600; color: #333; }
.queue-card-arrow { font-size: 12px; color: #5470c6; opacity: 0; transition: opacity .18s; }
.queue-card:hover .queue-card-arrow { opacity: 1; }

.queue-card-stats { display: flex; gap: 36px; flex-wrap: wrap; margin-bottom: 20px; }
.stat { display: flex; flex-direction: column; gap: 6px; }
.stat-label { font-size: 13px; color: #888; }
.stat-value { font-size: 32px; font-weight: 700; color: #333; }
.stat-value.warn { color: #e74c3c; }

/* ── Worker 卡片(沿用原样式) ── */
.workers { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; }
.worker-card { border-radius: 8px; padding: 16px; text-align: center; }
.worker-card.idle { background: #e8f5e9; border: 1px solid #a5d6a7; }
.worker-card.busy { background: #fff3e0; border: 1px solid #ffb74d; }
.worker-id { font-size: 12px; color: #888; margin-bottom: 4px; }
.worker-status { font-size: 15px; font-weight: 600; }
.idle .worker-status { color: #388e3c; }
.busy .worker-status { color: #e65100; }
.worker-model { margin-top: 6px; font-size: 12px; font-weight: 500; color: #444; }
.worker-task { font-size: 11px; color: #aaa; margin-top: 2px; }

/* 回调 Worker 卡片 */
.worker-card.callback-worker { background: #e3f2fd; border: 1px solid #90caf9; }
.worker-card.callback-worker.busy { background: #fff3e0; border: 1px solid #ffb74d; }

/* 联网解析（TI）Worker 卡片 */
.worker-card.ti-worker { background: #f3e5f5; border: 1px solid #ce93d8; }
.worker-card.ti-worker.busy { background: #fff3e0; border: 1px solid #ffb74d; }

.worker-url {
  margin-top: 6px;
  font-size: 11px;
  color: #666;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 100%;
}

.failed-badge {
  margin-top: 16px;
  width: 100%;
  background: #e74c3c;
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 10px 16px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background .15s;
}
.failed-badge:hover { background: #c0392b; }

/* TI 批量任务入口按钮（参照 failed-badge 的卡片底部按钮模式） */
.batch-upload-btn {
  margin-top: 16px;
  width: 100%;
  background: #5470c6;
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 10px 16px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background .15s;
}
.batch-upload-btn:hover { background: #455fae; }

.batch-status {
  margin-top: 14px;
  font-size: 12px;
  color: #e65100;
}

@media (max-width: 900px) {
  .workers { grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); }
}
</style>
