<script setup>
import { ref, onMounted, computed, watch } from 'vue'
import { authFetch } from '../utils/auth.js'
import VueJsonPretty from 'vue-json-pretty'
import 'vue-json-pretty/lib/styles.css'

const props = defineProps({
  userId: { type: [String, Number], default: '' }
})

// ─── 状态 ───────────────────────────────────────────────────────────────────
const failedLogs = ref([])
const loading = ref(false)
const error = ref('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const statusFilter = ref('')
const taskIdFilter = ref('')
const kindFilter = ref('')  // 回调来源类型：'' 全部 / ner / attack / ti

// 默认时间范围：1年前 ~ 今天 23:59:59
function getDefaultStartTime() {
  const d = new Date()
  d.setFullYear(d.getFullYear() - 1)
  return d.toISOString().slice(0, 16)
}
function getDefaultEndTime() {
  const d = new Date()
  return d.toISOString().slice(0, 11) + '23:59'
}
const startTimeFilter = ref(getDefaultStartTime())
const endTimeFilter = ref(getDefaultEndTime())

// 详情弹窗
const showDetailModal = ref(false)
const detailLog = ref(null)
const detailLoading = ref(false)

// 批量选择
const selectedIds = ref([])

// ─── 计算属性 ─────────────────────────────────────────────────────────────────
const totalPages = computed(() => Math.ceil(total.value / pageSize.value))
const allSelected = computed(() => 
  failedLogs.value.length > 0 && selectedIds.value.length === failedLogs.value.length
)

// ─── 加载失败记录列表 ─────────────────────────────────────────────────────────
async function loadFailedLogs() {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({
      page: page.value,
      page_size: pageSize.value,
    })
    if (statusFilter.value) {
      params.append('status', statusFilter.value)
    }
    if (taskIdFilter.value.trim()) {
      params.append('task_id', taskIdFilter.value.trim())
    }
    if (kindFilter.value) {
      params.append('kind', kindFilter.value)
    }
    if (startTimeFilter.value) {
      params.append('start_time', startTimeFilter.value)
    }
    if (endTimeFilter.value) {
      params.append('end_time', endTimeFilter.value)
    }
    if (props.userId) {
      params.append('user_id', props.userId)
    }
    
    const res = await authFetch(`/callback/failed?${params}`)
    const data = await res.json()
    
    if (!res.ok) {
      error.value = data.detail || '加载失败'
      return
    }
    
    failedLogs.value = data.items || []
    total.value = data.total || 0
    selectedIds.value = []
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

watch(() => props.userId, () => {
  page.value = 1
  loadFailedLogs()
})

// ─── 查看详情 ─────────────────────────────────────────────────────────────────
async function viewDetail(logId) {
  showDetailModal.value = true
  detailLoading.value = true
  detailLog.value = null
  
  try {
    const res = await authFetch(`/callback/failed/${logId}`)
    const data = await res.json()
    
    if (!res.ok) {
      error.value = data.detail || '加载详情失败'
      showDetailModal.value = false
      return
    }
    
    detailLog.value = data
  } catch (e) {
    error.value = e.message
    showDetailModal.value = false
  } finally {
    detailLoading.value = false
  }
}

// ─── 手动重试 ─────────────────────────────────────────────────────────────────
async function retryCallback(logId) {
  if (!confirm('确定要重试此回调吗？')) return
  
  try {
    const res = await authFetch(`/callback/retry/${logId}`, { method: 'POST' })
    const data = await res.json()
    
    if (!res.ok) {
      alert(data.detail || '重试失败')
      return
    }
    
    alert('回调已重新加入队列')
    loadFailedLogs()
  } catch (e) {
    alert('重试失败: ' + e.message)
  }
}

// ─── 批量重试 ─────────────────────────────────────────────────────────────────
async function retryBatch() {
  if (selectedIds.value.length === 0) {
    alert('请先选择要重试的记录')
    return
  }
  
  if (!confirm(`确定要重试选中的 ${selectedIds.value.length} 条记录吗？`)) return
  
  try {
    const res = await authFetch('/callback/retry/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(selectedIds.value),
    })
    const data = await res.json()
    
    if (!res.ok) {
      alert(data.detail || '批量重试失败')
      return
    }
    
    alert(`成功: ${data.success_count}, 失败: ${data.failed_count}`)
    loadFailedLogs()
  } catch (e) {
    alert('批量重试失败: ' + e.message)
  }
}

// ─── 标记已解决 ───────────────────────────────────────────────────────────────
async function resolveCallback(logId) {
  if (!confirm('确定要标记为已解决吗？')) return
  
  try {
    const res = await authFetch(`/callback/resolve/${logId}`, { method: 'POST' })
    const data = await res.json()
    
    if (!res.ok) {
      alert(data.detail || '标记失败')
      return
    }
    
    alert('已标记为已解决')
    loadFailedLogs()
  } catch (e) {
    alert('标记失败: ' + e.message)
  }
}

// ─── 删除记录 ─────────────────────────────────────────────────────────────────
async function deleteCallback(logId) {
  if (!confirm('确定要删除此记录吗？此操作不可恢复。')) return
  
  try {
    const res = await authFetch(`/callback/failed/${logId}`, { method: 'DELETE' })
    const data = await res.json()
    
    if (!res.ok) {
      alert(data.detail || '删除失败')
      return
    }
    
    alert('记录已删除')
    loadFailedLogs()
  } catch (e) {
    alert('删除失败: ' + e.message)
  }
}

// ─── 批量删除 ─────────────────────────────────────────────────────────────────
async function deleteBatch() {
  if (selectedIds.value.length === 0) {
    alert('请先选择要删除的记录')
    return
  }
  
  if (!confirm(`确定要删除选中的 ${selectedIds.value.length} 条记录吗？此操作不可恢复。`)) return
  
  try {
    const res = await authFetch('/callback/failed/delete/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(selectedIds.value),
    })
    const data = await res.json()
    
    if (!res.ok) {
      alert(data.detail || '批量删除失败')
      return
    }
    
    alert(`已删除 ${data.deleted_count} 条记录`)
    loadFailedLogs()
  } catch (e) {
    alert('批量删除失败: ' + e.message)
  }
}

// ─── 全部清空 ─────────────────────────────────────────────────────────────────
async function clearAll() {
  if (!confirm('确定要清空全部回调失败记录吗？此操作不可恢复。')) return

  try {
    const res = await authFetch('/callback/failed/clear', { method: 'POST' })
    const data = await res.json()

    if (!res.ok) {
      alert(data.detail || '清空失败')
      return
    }

    page.value = 1
    alert(`已清空 ${data.deleted_count} 条记录`)
    loadFailedLogs()
  } catch (e) {
    alert('清空失败: ' + e.message)
  }
}

// ─── 分页 ─────────────────────────────────────────────────────────────────────
function changePage(newPage) {
  if (newPage < 1 || newPage > totalPages.value) return
  page.value = newPage
  loadFailedLogs()
}

function changePageSize(newSize) {
  pageSize.value = newSize
  page.value = 1
  loadFailedLogs()
}

// ─── 筛选 ─────────────────────────────────────────────────────────────────────
function filterByStatus(status) {
  statusFilter.value = status
  page.value = 1
  loadFailedLogs()
}

function applyFilters() {
  page.value = 1
  loadFailedLogs()
}

function clearFilters() {
  taskIdFilter.value = ''
  kindFilter.value = ''
  startTimeFilter.value = getDefaultStartTime()
  endTimeFilter.value = getDefaultEndTime()
  page.value = 1
  loadFailedLogs()
}

// ─── 批量选择 ─────────────────────────────────────────────────────────────────
function toggleSelectAll() {
  if (allSelected.value) {
    selectedIds.value = []
  } else {
    selectedIds.value = failedLogs.value.map(log => log.id)
  }
}

function toggleSelect(logId) {
  const index = selectedIds.value.indexOf(logId)
  if (index > -1) {
    selectedIds.value.splice(index, 1)
  } else {
    selectedIds.value.push(logId)
  }
}

// ─── 工具函数 ─────────────────────────────────────────────────────────────────
// 回调来源类型徽标
const KIND_LABELS = { ner: 'NER', attack: 'ATT&CK', ti: '联网情报', unknown: '未知' }
function kindLabel(kind) {
  return KIND_LABELS[kind] || KIND_LABELS.unknown
}

function formatDate(dateStr) {
  if (!dateStr) return '—'
  return new Date(dateStr).toLocaleString('zh-CN')
}

function statusBadgeClass(status) {
  if (status === 'pending') return 'badge-pending'
  if (status === 'retrying') return 'badge-retrying'
  if (status === 'resolved') return 'badge-resolved'
  return ''
}

function statusText(status) {
  if (status === 'pending') return '待处理'
  if (status === 'retrying') return '重试中'
  if (status === 'resolved') return '已解决'
  return status
}

// ─── 初始化 ───────────────────────────────────────────────────────────────────
onMounted(() => {
  loadFailedLogs()
})
</script>

<template>
  <div class="callback-failed">
    <div class="header">
      <h2>回调失败管理</h2>
      <p class="subtitle">查看和管理回调失败的记录，支持手动重试</p>
    </div>

    <!-- 工具栏 -->
    <div class="toolbar">
      <div class="filters">
        <button 
          :class="['filter-btn', { active: statusFilter === '' }]"
          @click="filterByStatus('')"
        >全部</button>
        <button 
          :class="['filter-btn', { active: statusFilter === 'pending' }]"
          @click="filterByStatus('pending')"
        >待处理</button>
        <button 
          :class="['filter-btn', { active: statusFilter === 'retrying' }]"
          @click="filterByStatus('retrying')"
        >重试中</button>
        <button 
          :class="['filter-btn', { active: statusFilter === 'resolved' }]"
          @click="filterByStatus('resolved')"
        >已解决</button>
      </div>
      
      <div class="actions">
        <button class="action-btn" @click="loadFailedLogs" :disabled="loading">刷新</button>
        <button 
          class="action-btn primary" 
          @click="retryBatch"
          :disabled="selectedIds.length === 0"
        >批量重试 ({{ selectedIds.length }})</button>
        <button
          class="action-btn danger"
          @click="deleteBatch"
          :disabled="selectedIds.length === 0"
        >批量删除 ({{ selectedIds.length }})</button>
        <button class="action-btn danger-outline" @click="clearAll">全部清空</button>
      </div>
    </div>

    <!-- 高级筛选 -->
    <div class="advanced-filters">
      <div class="filter-field">
        <label>任务ID</label>
        <input v-model="taskIdFilter" placeholder="输入任务ID" @keyup.enter="applyFilters" />
      </div>
      <div class="filter-field">
        <label>类型</label>
        <select v-model="kindFilter" @change="applyFilters">
          <option value="">全部</option>
          <option value="ner">NER</option>
          <option value="attack">ATT&CK</option>
          <option value="ti">联网情报</option>
        </select>
      </div>
      <div class="filter-field">
        <label>开始时间</label>
        <input v-model="startTimeFilter" type="datetime-local" />
      </div>
      <div class="filter-field">
        <label>结束时间</label>
        <input v-model="endTimeFilter" type="datetime-local" />
      </div>
      <div class="filter-actions">
        <button class="filter-apply-btn" @click="applyFilters">筛选</button>
        <button class="filter-clear-btn" @click="clearFilters">清除</button>
      </div>
    </div>

    <!-- 错误提示 -->
    <div v-if="error" class="error-banner">{{ error }}</div>

    <!-- 加载中 -->
    <div v-if="loading && failedLogs.length === 0" class="loading">
      <span class="spinner"></span> 加载中...
    </div>

    <!-- 空状态 -->
    <div v-else-if="!loading && failedLogs.length === 0" class="empty-state">
      <div class="empty-icon">✅</div>
      <div class="empty-text">暂无失败记录</div>
      <div class="empty-hint">所有回调都成功了！</div>
    </div>

    <!-- 失败记录表格 -->
    <div v-else class="table-container">
      <table class="failed-table">
        <thead>
          <tr>
            <th class="col-checkbox">
              <input 
                type="checkbox" 
                :checked="allSelected"
                @change="toggleSelectAll"
              />
            </th>
            <th class="col-id">ID</th>
            <th class="col-task-id">任务ID</th>
            <th class="col-kind">类型</th>
            <th class="col-url">回调地址</th>
            <th class="col-error">错误信息</th>
            <th class="col-time">失败时间</th>
            <th class="col-retry">重试次数</th>
            <th class="col-status">状态</th>
            <th class="col-actions">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="log in failedLogs" :key="log.id">
            <td class="col-checkbox">
              <input 
                type="checkbox" 
                :checked="selectedIds.includes(log.id)"
                @change="toggleSelect(log.id)"
              />
            </td>
            <td class="col-id">{{ log.id }}</td>
            <td class="col-task-id">
              <span class="task-id-text" :title="log.task_id">{{ log.task_id }}</span>
            </td>
            <td class="col-kind">
              <span class="kind-badge" :class="`kind-${log.callback_kind || 'unknown'}`">
                {{ kindLabel(log.callback_kind) }}
              </span>
            </td>
            <td class="col-url">
              <div class="url-cell" :title="log.callback_url">
                {{ log.callback_url }}
              </div>
            </td>
            <td class="col-error">
              <div class="error-cell" :title="log.error_message">
                {{ log.error_message }}
              </div>
            </td>
            <td class="col-time">{{ formatDate(log.failed_at) }}</td>
            <td class="col-retry">
              <span v-if="log.retry_count > 0" class="retry-badge">
                {{ log.retry_count }}
              </span>
              <span v-else class="retry-none">0</span>
            </td>
            <td class="col-status">
              <span :class="['status-badge', statusBadgeClass(log.status)]">
                {{ statusText(log.status) }}
              </span>
            </td>
            <td class="col-actions">
              <div class="action-buttons">
                <button class="btn-small btn-view" @click="viewDetail(log.id)" title="查看详情">
                  <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg"><path d="M8 3C4.5 3 1.5 8 1.5 8s3 5 6.5 5 6.5-5 6.5-5-3-5-6.5-5z" fill="none" stroke="currentColor" stroke-width="1.3"/><circle cx="8" cy="8" r="2" fill="currentColor"/></svg>
                </button>
                <button class="btn-small btn-retry" @click="retryCallback(log.id)" title="重试">
                  <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg"><path d="M13 8A5 5 0 1 1 8 3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><polyline points="8 1 11 4 8 7" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
                </button>
                <button v-if="log.status !== 'resolved'" class="btn-small btn-resolve" @click="resolveCallback(log.id)" title="标记已解决">
                  <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg"><circle cx="8" cy="8" r="6.5" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M5 8l2 2 4-4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
                </button>
                <button class="btn-small btn-delete" @click="deleteCallback(log.id)" title="删除">
                  <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg"><polyline points="3 4 13 4" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/><path d="M6 4V3h4v1M5 4l.5 9h5L11 4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg>
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 分页 -->
    <div v-if="total > 0" class="pagination">
      <div class="pagination-info">
        共 {{ total }} 条记录，第 {{ page }} / {{ totalPages }} 页
      </div>
      <div class="pagination-controls">
        <button 
          class="page-btn" 
          @click="changePage(page - 1)"
          :disabled="page === 1"
        >
          ‹ 上一页
        </button>
        <span class="page-number">{{ page }}</span>
        <button 
          class="page-btn" 
          @click="changePage(page + 1)"
          :disabled="page === totalPages"
        >
          下一页 ›
        </button>
      </div>
      <div class="page-size-selector">
        每页
        <select v-model.number="pageSize" @change="changePageSize(pageSize)">
          <option :value="10">10</option>
          <option :value="20">20</option>
          <option :value="50">50</option>
          <option :value="100">100</option>
        </select>
        条
      </div>
    </div>

    <!-- 详情弹窗 -->
    <div v-if="showDetailModal" class="modal-overlay" @click.self="showDetailModal = false">
      <div class="modal-content">
        <div class="modal-header">
          <h3>回调失败详情 - ID: {{ detailLog?.id }}</h3>
          <button class="modal-close" @click="showDetailModal = false">×</button>
        </div>
        
        <div v-if="detailLoading" class="modal-loading">
          <span class="spinner"></span> 加载中...
        </div>
        
        <div v-else-if="detailLog" class="modal-body">
          <div class="detail-section">
            <h4>基本信息</h4>
            <div class="detail-grid">
              <div class="detail-item">
                <span class="detail-label">任务ID:</span>
                <code class="detail-value">{{ detailLog.task_id }}</code>
              </div>
              <div class="detail-item">
                <span class="detail-label">回调地址:</span>
                <span class="detail-value">{{ detailLog.callback_url }}</span>
              </div>
              <div class="detail-item">
                <span class="detail-label">请求方式:</span>
                <span class="detail-value">{{ detailLog.callback_method }}</span>
              </div>
              <div class="detail-item">
                <span class="detail-label">失败时间:</span>
                <span class="detail-value">{{ formatDate(detailLog.failed_at) }}</span>
              </div>
              <div class="detail-item">
                <span class="detail-label">重试次数:</span>
                <span class="detail-value">{{ detailLog.retry_count }}</span>
              </div>
              <div class="detail-item">
                <span class="detail-label">最后重试:</span>
                <span class="detail-value">{{ formatDate(detailLog.last_retry_at) }}</span>
              </div>
              <div class="detail-item">
                <span class="detail-label">状态:</span>
                <span :class="['status-badge', statusBadgeClass(detailLog.status)]">
                  {{ statusText(detailLog.status) }}
                </span>
              </div>
            </div>
          </div>

          <div class="detail-section">
            <h4>错误信息</h4>
            <div class="error-box">
              <div class="error-message">{{ detailLog.error_message }}</div>
              <div v-if="detailLog.http_status_code" class="http-status">
                HTTP Status Code: {{ detailLog.http_status_code }}
              </div>
            </div>
          </div>

          <div class="detail-section">
            <h4>请求头</h4>
            <VueJsonPretty
              v-if="detailLog.callback_headers && Object.keys(detailLog.callback_headers).length"
              :data="detailLog.callback_headers"
              :deep="3"
              class="json-viewer"
            />
            <div v-else class="no-data-hint">暂无请求头数据（旧记录或 URL 验证失败前未记录）</div>
          </div>

          <div class="detail-section">
            <h4>请求体（回调内容）</h4>
            <VueJsonPretty :data="detailLog.request_body || detailLog.result_data || {}" :deep="3" class="json-viewer" />
          </div>

          <div class="modal-actions">
            <button class="btn-modal btn-retry" @click="retryCallback(detailLog.id); showDetailModal = false">
              重试回调
            </button>
            <button
              v-if="detailLog.status !== 'resolved'"
              class="btn-modal btn-resolve"
              @click="resolveCallback(detailLog.id); showDetailModal = false"
            >
              标记已解决
            </button>
            <button class="btn-modal btn-delete" @click="deleteCallback(detailLog.id); showDetailModal = false">
              删除记录
            </button>
            <button class="btn-modal btn-cancel" @click="showDetailModal = false">
              关闭
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.callback-failed {
  max-width: 100%;
  margin: 0;
  padding: 0;
}

.header h2 {
  font-size: 18px;
  margin-bottom: 4px;
}

.subtitle {
  color: #888;
  font-size: 13px;
  margin-bottom: 20px;
}

/* 工具栏 */
.toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  gap: 16px;
  flex-wrap: wrap;
}

.filters {
  display: flex;
  gap: 8px;
}

.filter-btn {
  background: #fff;
  border: 1px solid #ddd;
  color: #666;
  padding: 6px 16px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.2s;
}

.filter-btn:hover {
  border-color: #5470c6;
  color: #5470c6;
}

.filter-btn.active {
  background: #5470c6;
  border-color: #5470c6;
  color: #fff;
}

.actions {
  display: flex;
  gap: 8px;
}

.action-btn {
  background: #fff;
  border: 1px solid #ddd;
  color: #666;
  padding: 6px 16px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.2s;
}

.action-btn:hover:not(:disabled) {
  border-color: #5470c6;
  color: #5470c6;
}

.action-btn.primary {
  background: #3ba272;
  border-color: #3ba272;
  color: #fff;
}

.action-btn.primary:hover:not(:disabled) {
  background: #2e8060;
  border-color: #2e8060;
}

.action-btn.danger {
  background: #d32f2f;
  border-color: #d32f2f;
  color: #fff;
}

.action-btn.danger:hover:not(:disabled) {
  background: #b71c1c;
  border-color: #b71c1c;
  color: #fff;
}

.action-btn.danger-outline {
  background: #fff;
  border-color: #d32f2f;
  color: #d32f2f;
}

.action-btn.danger-outline:hover:not(:disabled) {
  background: #ffebee;
  border-color: #b71c1c;
  color: #b71c1c;
}

.action-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* 高级筛选 */
.advanced-filters {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  margin-bottom: 16px;
  padding: 12px 16px;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,.06);
  flex-wrap: wrap;
}

.filter-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.filter-field label {
  font-size: 12px;
  color: #888;
  font-weight: 500;
}

.filter-field input {
  border: 1px solid #ddd;
  border-radius: 5px;
  padding: 6px 10px;
  font-size: 13px;
  outline: none;
  min-width: 180px;
}

.filter-field select {
  border: 1px solid #ddd;
  border-radius: 5px;
  padding: 6px 10px;
  font-size: 13px;
  outline: none;
  min-width: 120px;
  background: #fff;
  cursor: pointer;
}

.filter-field input:focus {
  border-color: #5470c6;
}

.filter-actions {
  display: flex;
  gap: 6px;
  align-self: flex-end;
}

.filter-apply-btn {
  background: #5470c6;
  color: #fff;
  border: none;
  border-radius: 5px;
  padding: 6px 16px;
  font-size: 13px;
  cursor: pointer;
  transition: background .15s;
}

.filter-apply-btn:hover {
  background: #4a63b3;
}

.filter-clear-btn {
  background: #e0e0e0;
  color: #666;
  border: none;
  border-radius: 5px;
  padding: 6px 16px;
  font-size: 13px;
  cursor: pointer;
  transition: background .15s;
}

.filter-clear-btn:hover {
  background: #d0d0d0;
}

/* 错误提示 */
.error-banner {
  background: #fee2e2;
  color: #e74c3c;
  padding: 12px 16px;
  border-radius: 6px;
  margin-bottom: 16px;
  font-size: 13px;
}

/* 加载中 */
.loading {
  text-align: center;
  padding: 40px;
  color: #888;
  font-size: 14px;
}

/* 空状态 */
.empty-state {
  text-align: center;
  padding: 60px 20px;
}

.empty-icon {
  font-size: 64px;
  margin-bottom: 16px;
}

.empty-text {
  font-size: 18px;
  font-weight: 600;
  color: #555;
  margin-bottom: 8px;
}

.empty-hint {
  font-size: 14px;
  color: #888;
}

/* 表格 */
.table-container {
  background: #fff;
  border-radius: 8px;
  overflow: hidden;
  box-shadow: 0 1px 3px rgba(0,0,0,.08);
  margin-bottom: 16px;
}

.failed-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.failed-table thead {
  background: #f8f9fa;
  border-bottom: 2px solid #e9ecef;
}

.failed-table th {
  padding: 12px 8px;
  text-align: left;
  font-weight: 600;
  color: #555;
  white-space: nowrap;
}

.failed-table td {
  padding: 12px 8px;
  border-bottom: 1px solid #f0f0f0;
}

.failed-table tbody tr:hover {
  background: #f8f9fa;
}

.col-checkbox {
  width: 40px;
  text-align: center;
}

.col-id {
  width: 60px;
}

.col-task-id {
  width: 280px;
  min-width: 200px;
}

.col-kind {
  width: 90px;
}

/* 回调来源类型徽标 */
.kind-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}
.kind-ner    { background: #ede9fe; color: #6d28d9; }
.kind-attack { background: #fef3c7; color: #b45309; }
.kind-ti     { background: #dbeafe; color: #1d4ed8; }
.kind-unknown { background: #f3f4f6; color: #9ca3af; }

.task-id-text {
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 12px;
  font-weight: 700;
  color: #333;
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 260px;
  cursor: default;
}

.col-url {
  min-width: 200px;
  max-width: 300px;
}

.col-error {
  min-width: 200px;
  max-width: 300px;
}

.col-time {
  width: 160px;
}

.col-retry {
  width: 80px;
  text-align: center;
}

.col-status {
  width: 80px;
}

.col-actions {
  width: 160px;
}

.task-id-code {
  background: #f0f0f0;
  padding: 2px 6px;
  border-radius: 3px;
  font-family: monospace;
  font-size: 12px;
}

.url-cell,
.error-cell {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.retry-badge {
  background: #fef3c7;
  color: #92400e;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
}

.retry-none {
  color: #aaa;
}

.status-badge {
  display: inline-block;
  padding: 3px 10px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 600;
}

.badge-pending {
  background: #fef3c7;
  color: #92400e;
}

.badge-retrying {
  background: #dbeafe;
  color: #1e40af;
}

.badge-resolved {
  background: #d1fae5;
  color: #065f46;
}

.action-buttons {
  display: flex;
  gap: 4px;
}

.btn-small {
  background: #fff;
  border: 1px solid #ddd;
  padding: 5px;
  border-radius: 5px;
  cursor: pointer;
  transition: all 0.15s;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  color: #666;
}

.btn-small svg {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}

.btn-small:hover {
  transform: translateY(-1px);
  box-shadow: 0 2px 4px rgba(0,0,0,.1);
}

.btn-view { color: #2196f3; }
.btn-view:hover { background: #e3f2fd; border-color: #90caf9; }

.btn-retry { color: #f57c00; }
.btn-retry:hover { background: #fff3e0; border-color: #ffb74d; }

.btn-resolve { color: #388e3c; }
.btn-resolve:hover { background: #e8f5e9; border-color: #81c784; }

.btn-delete { color: #d32f2f; }
.btn-delete:hover { background: #ffebee; border-color: #ef9a9a; }

/* 分页 */
.pagination {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px;
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,.08);
  font-size: 13px;
  flex-wrap: wrap;
  gap: 16px;
}

.pagination-info {
  color: #666;
}

.pagination-controls {
  display: flex;
  align-items: center;
  gap: 12px;
}

.page-btn {
  background: #fff;
  border: 1px solid #ddd;
  color: #666;
  padding: 6px 12px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.2s;
}

.page-btn:hover:not(:disabled) {
  border-color: #5470c6;
  color: #5470c6;
}

.page-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.page-number {
  font-weight: 600;
  color: #5470c6;
}

.page-size-selector {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #666;
}

.page-size-selector select {
  border: 1px solid #ddd;
  border-radius: 4px;
  padding: 4px 8px;
  font-size: 13px;
  outline: none;
}

.page-size-selector select:focus {
  border-color: #5470c6;
}

/* 弹窗 */
.modal-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 20px;
}

.modal-content {
  background: #fff;
  border-radius: 12px;
  max-width: 800px;
  width: 100%;
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: 0 10px 40px rgba(0,0,0,0.2);
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 20px 24px;
  border-bottom: 1px solid #e9ecef;
}

.modal-header h3 {
  font-size: 16px;
  margin: 0;
}

.modal-close {
  background: none;
  border: none;
  font-size: 28px;
  color: #999;
  cursor: pointer;
  padding: 0;
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 4px;
  transition: all 0.2s;
}

.modal-close:hover {
  background: #f0f0f0;
  color: #666;
}

.modal-loading {
  padding: 40px;
  text-align: center;
  color: #888;
}

.modal-body {
  padding: 24px;
}

.detail-section {
  margin-bottom: 24px;
}

.detail-section h4 {
  font-size: 14px;
  font-weight: 600;
  color: #555;
  margin-bottom: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid #e9ecef;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
}

.detail-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.detail-label {
  font-size: 12px;
  color: #888;
  font-weight: 500;
}

.detail-value {
  font-size: 13px;
  color: #333;
  word-break: break-all;
}

.error-box {
  background: #fee2e2;
  border: 1px solid #fecaca;
  border-radius: 6px;
  padding: 12px;
}

.error-message {
  color: #991b1b;
  font-size: 13px;
  margin-bottom: 8px;
}

.http-status {
  color: #dc2626;
  font-size: 12px;
  font-weight: 600;
}

.json-pre {
  background: #f8f9fa;
  border: 1px solid #e9ecef;
  border-radius: 6px;
  padding: 12px;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 12px;
  line-height: 1.5;
  overflow-x: auto;
  max-height: 300px;
  overflow-y: auto;
}

.json-viewer {
  background: #f8f9fa;
  border: 1px solid #e9ecef;
  border-radius: 6px;
  padding: 12px;
  font-size: 12px;
  max-height: 300px;
  overflow-y: auto;
}

.no-data-hint {
  color: #aaa;
  font-size: 13px;
  padding: 10px 12px;
  background: #f8f9fa;
  border: 1px dashed #ddd;
  border-radius: 6px;
}

.modal-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
  padding-top: 16px;
  border-top: 1px solid #e9ecef;
  flex-wrap: wrap;
}

.btn-modal {
  padding: 8px 16px;
  border-radius: 6px;
  border: none;
  cursor: pointer;
  font-size: 13px;
  font-weight: 500;
  transition: all 0.2s;
}

.btn-retry {
  background: #ff9800;
  color: #fff;
}

.btn-retry:hover {
  background: #f57c00;
}

.btn-resolve {
  background: #4caf50;
  color: #fff;
}

.btn-resolve:hover {
  background: #388e3c;
}

.btn-delete {
  background: #f44336;
  color: #fff;
}

.btn-delete:hover {
  background: #d32f2f;
}

.btn-cancel {
  background: #e0e0e0;
  color: #666;
}

.btn-cancel:hover {
  background: #bdbdbd;
}

/* Spinner */
.spinner {
  width: 14px;
  height: 14px;
  border: 2px solid rgba(0,0,0,.1);
  border-top-color: #5470c6;
  border-radius: 50%;
  animation: spin .7s linear infinite;
  display: inline-block;
  vertical-align: middle;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* 响应式 */
@media (max-width: 1200px) {
  .detail-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 768px) {
  .toolbar {
    flex-direction: column;
    align-items: stretch;
  }
  
  .filters,
  .actions {
    justify-content: center;
  }
  
  .pagination {
    flex-direction: column;
    align-items: stretch;
    text-align: center;
  }
  
  .pagination-controls {
    justify-content: center;
  }
}
</style>
