<script setup>
// 队列任务列表全屏 Overlay
// queueType: 'extract'(模型提取队列) | 'ti'(联网解析队列) | 'callback'(回调队列)
// 三个队列相互独立,各自的数据源/筛选/列结构在此组件内按 queueType 切换。
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { authFetch } from '../utils/auth.js'
import CallbackFailedManagement from './CallbackFailedManagement.vue'
import CallbackSuccessLogs from './CallbackSuccessLogs.vue'

const props = defineProps({
  queueType: { type: String, required: true },
  // 仅 callback 有效: 初始 Tab
  initialTab: { type: String, default: 'pending' },
})
const emit = defineEmits(['close', 'open-graph'])

const TITLES = {
  extract: '模型提取队列 · 任务列表',
  ti: '联网解析队列 · 任务列表',
  callback: '回调队列 · 任务列表',
}

// ─── 通用状态 ────────────────────────────────────────────────────────────────
const loading    = ref(false)
const loadError  = ref('')
const items      = ref([])
const total      = ref(0)
const page       = ref(1)
const pageSize   = 20
const autoRefresh = ref(true)
const lastRefresh = ref('')
let timer = null

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

// ─── 提取队列筛选 ─────────────────────────────────────────────────────────────
const extractStatus = ref('')       // '' | pending | running | done | failed
const extractModel  = ref('')
const modelOptions  = ref([])

// ─── TI 队列筛选 ──────────────────────────────────────────────────────────────
const tiStatus   = ref('')
const tiPlatform = ref('')

// ─── 回调队列 Tab ─────────────────────────────────────────────────────────────
const callbackTab = ref(props.initialTab)   // pending | failed | success
const pendingMax  = ref(0)

const STATUS_TEXT = { pending: '等待中', running: '执行中', done: '完成', failed: '失败' }
const PLATFORM_TEXT = { threatbook: '微步在线', qianxin: '奇安信' }

// ─── 数据加载 ────────────────────────────────────────────────────────────────
async function fetchModels() {
  try {
    const res = await authFetch('/models')
    if (res.ok) {
      const data = await res.json()
      modelOptions.value = data.models || []
    }
  } catch { /* 模型下拉加载失败不阻塞列表 */ }
}

async function load(silent = false) {
  if (!silent) loading.value = true
  loadError.value = ''
  try {
    let url = ''
    if (props.queueType === 'extract') {
      const params = new URLSearchParams({ page: page.value, page_size: pageSize })
      if (extractStatus.value) params.set('status', extractStatus.value)
      if (extractModel.value)  params.set('model_name', extractModel.value)
      url = `/tasks?${params}`
    } else if (props.queueType === 'ti') {
      const params = new URLSearchParams({ page: page.value, page_size: pageSize })
      if (tiStatus.value)   params.set('status', tiStatus.value)
      if (tiPlatform.value) params.set('platform', tiPlatform.value)
      url = `/ti/tasks?${params}`
    } else {
      // callback: 仅 pending Tab 需要在此加载,失败/成功 Tab 由嵌入组件自己加载
      if (callbackTab.value === 'pending') {
        url = '/callback/pending'
      } else {
        return
      }
    }
    const res = await authFetch(url)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    items.value = data.items || []
    total.value = data.total ?? items.value.length
    if (props.queueType === 'callback') pendingMax.value = data.max_size || 0
    lastRefresh.value = new Date().toLocaleTimeString()
  } catch (e) {
    loadError.value = `加载失败: ${e.message}`
  } finally {
    loading.value = false
  }
}

// 筛选变化 → 回到第一页重新加载
watch([extractStatus, extractModel, tiStatus, tiPlatform], () => { page.value = 1; load() })
watch(page, () => load())
watch(callbackTab, () => { page.value = 1; load() })

// ─── 工具 ────────────────────────────────────────────────────────────────────
function fmtTime(t) {
  if (!t) return '—'
  const d = new Date(t)
  return isNaN(d) ? String(t) : d.toLocaleString()
}
function fmtLatency(ms) {
  if (ms === null || ms === undefined) return '—'
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)} s` : `${Math.round(ms)} ms`
}
function shortId(id) {
  return id ? String(id).slice(0, 8) : '—'
}
async function copyId(id) {
  try { await navigator.clipboard.writeText(id) } catch { /* 剪贴板不可用时忽略 */ }
}
function viewTask(taskId) {
  emit('open-graph', taskId)
}

// ─── 生命周期 ────────────────────────────────────────────────────────────────
function onKeydown(e) {
  if (e.key === 'Escape') emit('close')
}

onMounted(() => {
  document.body.style.overflow = 'hidden'
  window.addEventListener('keydown', onKeydown)
  if (props.queueType === 'extract') fetchModels()
  load()
  timer = setInterval(() => { if (autoRefresh.value) load(true) }, 5000)
})
onUnmounted(() => {
  document.body.style.overflow = ''
  window.removeEventListener('keydown', onKeydown)
  clearInterval(timer)
})
</script>

<template>
  <div class="overlay" @click.self="emit('close')">
    <div class="panel">
      <!-- 顶栏 -->
      <div class="topbar">
        <button class="back-btn" @click="emit('close')">← 返回</button>
        <h2 class="title">{{ TITLES[queueType] }}</h2>
        <div class="topbar-right">
          <span v-if="lastRefresh" class="refresh-time">{{ lastRefresh }} 已刷新</span>
          <label class="auto-refresh">
            <input type="checkbox" v-model="autoRefresh" />
            自动刷新
          </label>
          <button class="refresh-btn" @click="load()">⟳ 刷新</button>
        </div>
      </div>

      <!-- 回调队列: Tab 切换 -->
      <div v-if="queueType === 'callback'" class="tabs">
        <button :class="{ active: callbackTab === 'pending' }" @click="callbackTab = 'pending'">
          等待中<span v-if="callbackTab === 'pending' && total" class="tab-count">{{ total }}</span>
        </button>
        <button :class="{ active: callbackTab === 'failed' }" @click="callbackTab = 'failed'">失败记录</button>
        <button :class="{ active: callbackTab === 'success' }" @click="callbackTab = 'success'">成功记录</button>
      </div>

      <!-- 筛选条 -->
      <div v-if="queueType === 'extract'" class="filters">
        <label>状态
          <select v-model="extractStatus">
            <option value="">全部</option>
            <option value="pending">等待中</option>
            <option value="running">执行中</option>
            <option value="done">完成</option>
            <option value="failed">失败</option>
          </select>
        </label>
        <label>模型
          <select v-model="extractModel">
            <option value="">全部</option>
            <option v-for="m in modelOptions" :key="m" :value="m">{{ m }}</option>
          </select>
        </label>
      </div>
      <div v-else-if="queueType === 'ti'" class="filters">
        <label>状态
          <select v-model="tiStatus">
            <option value="">全部</option>
            <option value="pending">等待中</option>
            <option value="running">执行中</option>
            <option value="done">完成</option>
            <option value="failed">失败</option>
          </select>
        </label>
        <label>平台
          <select v-model="tiPlatform">
            <option value="">全部</option>
            <option value="threatbook">微步在线</option>
            <option value="qianxin">奇安信</option>
          </select>
        </label>
      </div>

      <!-- 内容区 -->
      <div class="content">
        <div v-if="loadError" class="error-box">{{ loadError }}</div>
        <div v-else-if="loading" class="placeholder">加载中…</div>

        <!-- 提取队列表格 -->
        <template v-else-if="queueType === 'extract'">
          <table v-if="items.length" class="task-table">
            <thead>
              <tr>
                <th>任务 ID</th><th>模型</th><th>状态</th><th>标签</th>
                <th>文本预览</th><th>提交时间</th><th>耗时</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="t in items" :key="t.task_id">
                <td class="mono" :title="t.task_id">
                  <span class="clickable" @click="copyId(t.task_id)">{{ shortId(t.task_id) }}…</span>
                </td>
                <td>{{ t.model_name }}</td>
                <td>
                  <span class="badge" :class="t.status" :title="t.status === 'failed' ? t.error_msg : ''">
                    {{ STATUS_TEXT[t.status] || t.status }}
                  </span>
                </td>
                <td class="labels-cell" :title="(t.labels || []).join(', ')">
                  {{ (t.labels || []).slice(0, 3).join(', ') }}{{ (t.labels || []).length > 3 ? ' …' : '' }}
                </td>
                <td class="preview-cell" :title="t.input_text_preview">{{ t.input_text_preview || '—' }}</td>
                <td class="nowrap">{{ fmtTime(t.created_at) }}</td>
                <td class="nowrap">{{ fmtLatency(t.latency_ms) }}</td>
                <td><button class="link-btn" @click="viewTask(t.task_id)">查看详情</button></td>
              </tr>
            </tbody>
          </table>
          <div v-else class="placeholder">当前没有任务</div>
        </template>

        <!-- TI 队列表格 -->
        <template v-else-if="queueType === 'ti'">
          <table v-if="items.length" class="task-table">
            <thead>
              <tr>
                <th>任务 ID</th><th>平台</th><th>类型</th><th>查询词</th>
                <th>状态</th><th>提交时间</th><th>耗时</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="t in items" :key="t.task_id">
                <td class="mono" :title="t.task_id">
                  <span class="clickable" @click="copyId(t.task_id)">{{ shortId(t.task_id) }}…</span>
                </td>
                <td>{{ PLATFORM_TEXT[t.platform] || t.platform }}</td>
                <td>{{ t.seed_type }}</td>
                <td class="preview-cell" :title="t.seed_value">{{ t.seed_value }}</td>
                <td>
                  <span class="badge" :class="t.status" :title="t.status === 'failed' ? t.error_msg : ''">
                    {{ STATUS_TEXT[t.status] || t.status }}
                  </span>
                </td>
                <td class="nowrap">{{ fmtTime(t.created_at) }}</td>
                <td class="nowrap">{{ fmtLatency(t.latency_ms) }}</td>
                <td><button class="link-btn" @click="viewTask(t.task_id)">查看详情</button></td>
              </tr>
            </tbody>
          </table>
          <div v-else class="placeholder">当前没有任务</div>
        </template>

        <!-- 回调队列 -->
        <template v-else>
          <!-- 等待中 -->
          <template v-if="callbackTab === 'pending'">
            <p class="pending-hint">内存队列实时快照{{ pendingMax ? `，容量上限 ${pendingMax}` : '' }}，不持久化</p>
            <table v-if="items.length" class="task-table">
              <thead>
                <tr><th>任务 ID</th><th>回调 URL</th><th>方法</th><th>入队时间</th><th>类型</th></tr>
              </thead>
              <tbody>
                <tr v-for="(c, i) in items" :key="c.task_id + '-' + i">
                  <td class="mono" :title="c.task_id">
                    <span class="clickable" @click="copyId(c.task_id)">{{ shortId(c.task_id) }}…</span>
                  </td>
                  <td class="url-cell" :title="c.url">{{ c.url }}</td>
                  <td>{{ c.method }}</td>
                  <td class="nowrap">{{ fmtTime(c.enqueued_at) }}</td>
                  <td>
                    <span class="badge" :class="c.is_retry ? 'running' : 'pending'">
                      {{ c.is_retry ? '失败重试' : '首次发送' }}
                    </span>
                  </td>
                </tr>
              </tbody>
            </table>
            <div v-else class="placeholder">回调队列为空</div>
          </template>
          <!-- 失败/成功: 复用现有管理组件 -->
          <CallbackFailedManagement v-else-if="callbackTab === 'failed'" />
          <CallbackSuccessLogs v-else />
        </template>
      </div>

      <!-- 分页(回调 pending 不分页;失败/成功 Tab 由嵌入组件自带分页) -->
      <div
        v-if="(queueType === 'extract' || queueType === 'ti') && total > 0"
        class="pagination"
      >
        <button :disabled="page <= 1" @click="page--">上一页</button>
        <span>第 {{ page }} / {{ totalPages }} 页 · 共 {{ total }} 条</span>
        <button :disabled="page >= totalPages" @click="page++">下一页</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(15, 20, 35, 0.55);
  z-index: 500;
  display: flex;
}
.panel {
  background: #f0f2f5;
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  animation: slide-in .18s ease-out;
}
@keyframes slide-in {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}

/* ── 顶栏 ── */
.topbar {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 20px;
  background: #fff;
  border-bottom: 1px solid #e5e5e5;
  flex-shrink: 0;
}
.back-btn {
  background: #fff;
  border: 1px solid #ddd;
  border-radius: 6px;
  padding: 7px 14px;
  font-size: 13px;
  cursor: pointer;
  color: #444;
}
.back-btn:hover { border-color: #5470c6; color: #5470c6; }
.title { font-size: 17px; color: #333; flex: 1; }
.topbar-right { display: flex; align-items: center; gap: 14px; }
.refresh-time { font-size: 12px; color: #999; }
.auto-refresh { display: flex; align-items: center; gap: 5px; font-size: 13px; color: #555; cursor: pointer; }
.refresh-btn {
  background: #5470c6;
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 7px 14px;
  font-size: 13px;
  cursor: pointer;
}
.refresh-btn:hover { background: #4060b0; }

/* ── Tab ── */
.tabs {
  display: flex;
  gap: 0;
  background: #fff;
  padding: 0 20px;
  border-bottom: 2px solid #e9ecef;
  flex-shrink: 0;
}
.tabs button {
  background: transparent;
  border: none;
  color: #888;
  padding: 11px 20px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -2px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.tabs button.active { color: #5470c6; border-bottom-color: #5470c6; }
.tab-count {
  background: #5470c6;
  color: #fff;
  font-size: 11px;
  border-radius: 8px;
  padding: 1px 7px;
}

/* ── 筛选条 ── */
.filters {
  display: flex;
  gap: 18px;
  padding: 12px 20px;
  background: #fff;
  border-bottom: 1px solid #e9ecef;
  flex-shrink: 0;
}
.filters label { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #555; }
.filters select {
  padding: 5px 10px;
  border: 1px solid #ddd;
  border-radius: 4px;
  font-size: 13px;
  background: #fff;
  cursor: pointer;
  outline: none;
}
.filters select:focus { border-color: #5470c6; }

/* ── 内容区 ── */
.content { flex: 1; overflow: auto; padding: 16px 20px; }
.pending-hint { font-size: 12px; color: #999; margin-bottom: 10px; }
.placeholder {
  text-align: center;
  color: #999;
  font-size: 14px;
  padding: 60px 0;
}
.error-box {
  background: #fdf0ef;
  border: 1px solid #f5c6c0;
  color: #c0392b;
  border-radius: 6px;
  padding: 14px 18px;
  font-size: 14px;
}

/* ── 表格 ── */
.task-table {
  width: 100%;
  border-collapse: collapse;
  background: #fff;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 1px 3px rgba(0,0,0,.08);
  font-size: 13px;
}
.task-table th {
  text-align: left;
  padding: 10px 12px;
  background: #f7f8fa;
  color: #666;
  font-weight: 600;
  border-bottom: 1px solid #e9ecef;
  white-space: nowrap;
}
.task-table td {
  padding: 10px 12px;
  border-bottom: 1px solid #f0f0f0;
  color: #444;
  vertical-align: middle;
}
.task-table tr:last-child td { border-bottom: none; }
.task-table tr:hover td { background: #f8f9ff; }

.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.clickable { cursor: pointer; color: #5470c6; }
.clickable:hover { text-decoration: underline; }
.nowrap { white-space: nowrap; }
.labels-cell { max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.preview-cell { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #777; }
.url-cell { max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 10px;
  font-size: 12px;
  white-space: nowrap;
}
.badge.pending { background: #fff8e1; color: #f57f17; }
.badge.running { background: #e3f2fd; color: #1565c0; }
.badge.done    { background: #e8f5e9; color: #2e7d32; }
.badge.failed  { background: #fdf0ef; color: #c0392b; }

.link-btn {
  background: none;
  border: none;
  color: #5470c6;
  font-size: 13px;
  cursor: pointer;
  padding: 0;
}
.link-btn:hover { text-decoration: underline; }

/* ── 分页 ── */
.pagination {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 16px;
  padding: 12px;
  background: #fff;
  border-top: 1px solid #e5e5e5;
  flex-shrink: 0;
  font-size: 13px;
  color: #666;
}
.pagination button {
  background: #fff;
  border: 1px solid #ddd;
  border-radius: 6px;
  padding: 6px 14px;
  font-size: 13px;
  cursor: pointer;
  color: #444;
}
.pagination button:hover:not(:disabled) { border-color: #5470c6; color: #5470c6; }
.pagination button:disabled { opacity: .45; cursor: not-allowed; }
</style>
