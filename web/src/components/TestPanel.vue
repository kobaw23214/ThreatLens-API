<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { authFetch } from '../utils/auth.js'

const text           = ref('')
const labelsInput    = ref('PERSON,LOCATION,ORGANIZATION,DATE,PRODUCT')
const selectedModel  = ref('')
const availableModels= ref([])
const loading        = ref(false)
const currentTaskId  = ref(null)
const taskResult     = ref(null)
const taskStatus     = ref('')
let pollTimer        = null

async function fetchModels() {
  try {
    const res = await authFetch('/models')
    const data = await res.json()
    availableModels.value = data.models
    if (data.models.length) selectedModel.value = data.models[0]
  } catch (e) {
    console.error('Fetch models failed:', e)
  }
}

async function submit() {
  if (!text.value.trim() || !labelsInput.value.trim()) return
  const labels = labelsInput.value.split(',').map(l => l.trim()).filter(Boolean)

  loading.value    = true
  taskResult.value = null
  taskStatus.value = 'pending'
  currentTaskId.value = null

  try {
    const res = await authFetch('/extract', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: text.value,
        labels,
        model_name: selectedModel.value || null,
      }),
    })
    if (!res.ok) {
      const err = await res.json()
      taskStatus.value = 'error: ' + (err.detail || res.statusText)
      loading.value = false
      return
    }
    const data = await res.json()
    currentTaskId.value = data.task_id
    taskStatus.value    = 'pending'
    startPolling(data.task_id)
  } catch (e) {
    taskStatus.value = 'error: ' + e.message
    loading.value    = false
  }
}

function startPolling(taskId) {
  clearInterval(pollTimer)
  pollTimer = setInterval(async () => {
    try {
      const res  = await authFetch(`/tasks/${taskId}`)
      const data = await res.json()
      taskStatus.value = data.status
      if (data.status === 'done' || data.status === 'failed') {
        clearInterval(pollTimer)
        taskResult.value = data
        loading.value    = false
      }
    } catch (e) {
      clearInterval(pollTimer)
      loading.value = false
    }
  }, 2000)
}

onMounted(fetchModels)
onUnmounted(() => clearInterval(pollTimer))
</script>

<template>
  <div class="test-panel">
    <h2>手动测试</h2>

    <div class="form">
      <div class="field">
        <label>模型</label>
        <select v-model="selectedModel">
          <option v-for="m in availableModels" :key="m" :value="m">{{ m }}</option>
        </select>
      </div>

      <div class="field">
        <label>实体标签 <span class="hint">（逗号分隔）</span></label>
        <input v-model="labelsInput" placeholder="PERSON,LOCATION,ORGANIZATION" />
      </div>

      <div class="field">
        <label>输入文本</label>
        <textarea v-model="text" rows="5" placeholder="请输入待提取实体的文本…"></textarea>
      </div>

      <button
        class="submit-btn"
        :disabled="loading || !text.trim()"
        @click="submit"
      >
        {{ loading ? '处理中…' : '提交' }}
      </button>
    </div>

    <!-- 结果区 -->
    <div v-if="currentTaskId" class="result-box">
      <div class="result-header">
        <span>Task ID: <code>{{ currentTaskId }}</code></span>
        <span class="status-badge" :class="taskStatus">{{ taskStatus }}</span>
      </div>

      <template v-if="taskResult">
        <div v-if="taskResult.latency_ms" class="meta">
          延迟: {{ taskResult.latency_ms.toFixed(0) }} ms &nbsp;|&nbsp; 模型: {{ taskResult.model_name }}
        </div>

        <template v-if="taskResult.status === 'done'">
          <div v-if="taskResult.result?.length">
            <h4>识别结果（{{ taskResult.result.length }} 个实体）</h4>
            <div class="entities">
              <span v-for="(e, i) in taskResult.result" :key="i" class="entity-tag">
                {{ e.text }} <em>{{ e.label }}</em>
                <small>{{ (e.score * 100).toFixed(0) }}%</small>
              </span>
            </div>
          </div>
          <p v-else class="no-result">未识别到实体</p>
        </template>

        <div v-else-if="taskResult.status === 'failed'" class="error-msg">
          {{ taskResult.error_message }}
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.test-panel { background: #fff; border-radius: 8px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
h2 { margin-bottom: 20px; font-size: 18px; }
.form { display: flex; flex-direction: column; gap: 16px; max-width: 720px; }
.field { display: flex; flex-direction: column; gap: 6px; }
label { font-size: 14px; font-weight: 500; }
.hint { font-weight: 400; color: #aaa; font-size: 13px; }
input, select, textarea { border: 1px solid #ddd; border-radius: 6px; padding: 8px 12px; font-size: 14px; width: 100%; outline: none; font-family: inherit; }
input:focus, select:focus, textarea:focus { border-color: #5470c6; }
textarea { resize: vertical; }
.submit-btn { padding: 10px 28px; background: #5470c6; color: #fff; border: none; border-radius: 6px; font-size: 15px; cursor: pointer; align-self: flex-start; }
.submit-btn:disabled { background: #ccc; cursor: not-allowed; }
.submit-btn:not(:disabled):hover { background: #4560b0; }

.result-box { margin-top: 24px; border: 1px solid #eee; border-radius: 8px; padding: 16px; }
.result-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; font-size: 13px; }
.result-header code { background: #f5f5f5; padding: 2px 6px; border-radius: 3px; font-size: 12px; }
.status-badge { padding: 3px 10px; border-radius: 12px; font-size: 12px; font-weight: 500; }
.status-badge.pending, .status-badge.running { background: #fff3cd; color: #856404; }
.status-badge.done    { background: #d1fae5; color: #065f46; }
.status-badge.failed  { background: #fee2e2; color: #991b1b; }
.meta { font-size: 13px; color: #888; margin-bottom: 12px; }
h4 { font-size: 15px; margin-bottom: 10px; }
.entities { display: flex; flex-wrap: wrap; gap: 8px; }
.entity-tag { background: #eef2ff; border: 1px solid #c7d2fe; border-radius: 6px; padding: 4px 10px; font-size: 13px; }
.entity-tag em    { font-style: normal; color: #5470c6; font-weight: 600; margin-left: 4px; }
.entity-tag small { color: #aaa; margin-left: 4px; }
.no-result { color: #888; font-size: 14px; }
.error-msg { color: #e74c3c; font-size: 14px; background: #fee2e2; padding: 10px; border-radius: 6px; }
</style>
