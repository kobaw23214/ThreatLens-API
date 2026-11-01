<script setup>
import { ref, onMounted, watch, computed } from 'vue'
import { authFetch } from '../utils/auth.js'
import VueJsonPretty from 'vue-json-pretty'
import 'vue-json-pretty/lib/styles.css'

const props = defineProps({
  userId: { type: [String, Number], default: '' }
})

const logs = ref([])
const loading = ref(false)
const error = ref('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const taskIdFilter = ref('')
const kindFilter = ref('')  // 回调来源类型：'' 全部 / ner / attack / ti

// 回调来源类型徽标
const KIND_LABELS = { ner: 'NER', attack: 'ATT&CK', ti: '联网情报', unknown: '未知' }
function kindLabel(kind) {
  return KIND_LABELS[kind] || KIND_LABELS.unknown
}

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
const showDetail = ref(false)
const detailLog = ref(null)
const detailLoading = ref(false)

// 批量选择
const selectedIds = ref([])
const allSelected = computed(() =>
  logs.value.length > 0 && selectedIds.value.length === logs.value.length
)

function toggleSelectAll() {
  if (allSelected.value) {
    selectedIds.value = []
  } else {
    selectedIds.value = logs.value.map(log => log.id)
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

// 尝试将响应体解析为 JSON 对象，失败则返回 null
function parseResponseBody(body) {
  if (!body) return null
  try {    return JSON.parse(body)
  } catch {
    return null
  }
}

async function loadLogs() {
  loading.value = true
  error.value = ''
  try {
    const params = new URLSearchParams({
      page: page.value,
      page_size: pageSize.value,
    })
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
    
    const res = await authFetch(`/callback/success?${params}`)
    if (!res.ok) throw new Error('加载日志失败')
    const data = await res.json()
    logs.value = data.items
    total.value = data.total
    selectedIds.value = []
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

watch(() => props.userId, () => {
  page.value = 1
  loadLogs()
})

async function viewDetail(log) {
  showDetail.value = true
  detailLoading.value = true
  try {
    const res = await authFetch(`/callback/success/${log.id}`)
    if (!res.ok) throw new Error('加载详情失败')
    detailLog.value = await res.json()
  } catch (e) {
    error.value = e.message
    showDetail.value = false
  } finally {
    detailLoading.value = false
  }
}

function closeDetail() {
  showDetail.value = false
  detailLog.value = null
}

async function deleteLog(logId) {
  if (!confirm('确定要删除此日志吗？此操作不可恢复。')) return
  try {
    const res = await authFetch(`/callback/success/${logId}`, { method: 'DELETE' })
    const data = await res.json()
    if (!res.ok) {
      alert(data.detail || '删除失败')
      return
    }
    alert('日志已删除')
    loadLogs()
  } catch (e) {
    alert('删除失败: ' + e.message)
  }
}

async function deleteBatch() {
  if (selectedIds.value.length === 0) {
    alert('请先选择要删除的日志')
    return
  }
  if (!confirm(`确定要删除选中的 ${selectedIds.value.length} 条日志吗？此操作不可恢复。`)) return
  try {
    const res = await authFetch('/callback/success/delete/batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(selectedIds.value),
    })
    const data = await res.json()
    if (!res.ok) {
      alert(data.detail || '批量删除失败')
      return
    }
    alert(`已删除 ${data.deleted_count} 条日志`)
    loadLogs()
  } catch (e) {
    alert('批量删除失败: ' + e.message)
  }
}

async function clearAll() {
  if (!confirm('确定要清空全部回调成功日志吗？此操作不可恢复。')) return
  try {
    const res = await authFetch('/callback/success/clear', { method: 'POST' })
    const data = await res.json()
    if (!res.ok) {
      alert(data.detail || '清空失败')
      return
    }
    page.value = 1
    alert(`已清空 ${data.deleted_count} 条日志`)
    loadLogs()
  } catch (e) {
    alert('清空失败: ' + e.message)
  }
}

function prevPage() {
  if (page.value > 1) {
    page.value--
    loadLogs()
  }
}

function nextPage() {
  if (page.value < Math.ceil(total.value / pageSize.value)) {
    page.value++
    loadLogs()
  }
}

function applyFilter() {
  page.value = 1
  loadLogs()
}

function clearFilter() {
  taskIdFilter.value = ''
  kindFilter.value = ''
  startTimeFilter.value = getDefaultStartTime()
  endTimeFilter.value = getDefaultEndTime()
  page.value = 1
  loadLogs()
}

onMounted(loadLogs)
</script>

<template>
  <div class="callback-success-logs">
    <div class="logs-header">
      <h2>回调成功日志</h2>
      <div class="header-actions">
        <button
          class="batch-delete-btn"
          @click="deleteBatch"
          :disabled="selectedIds.length === 0"
        >批量删除 ({{ selectedIds.length }})</button>
        <button class="batch-delete-btn clear-all-btn" @click="clearAll">全部清空</button>
        <button class="refresh-btn" @click="loadLogs" :disabled="loading">
          {{ loading ? '刷新中...' : '刷新' }}
        </button>
      </div>
    </div>

    <!-- 筛选区域 -->
    <div class="filter-section">
      <div class="filter-row">
        <div class="filter-field">
          <label>任务ID</label>
          <input v-model="taskIdFilter" placeholder="输入任务ID" @keyup.enter="applyFilter" />
        </div>
        <div class="filter-field">
          <label>类型</label>
          <select v-model="kindFilter" @change="applyFilter">
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
          <button class="apply-btn" @click="applyFilter">筛选</button>
          <button class="clear-btn" @click="clearFilter">清除</button>
        </div>
      </div>
    </div>

    <div v-if="error" class="error-message">{{ error }}</div>

    <!-- 日志列表 -->
    <div class="logs-table-container">
      <table class="logs-table">
        <thead>
          <tr>
            <th class="col-checkbox">
              <input
                type="checkbox"
                :checked="allSelected"
                @change="toggleSelectAll"
              />
            </th>
            <th>ID</th>
            <th>任务ID</th>
            <th>类型</th>
            <th>回调地址</th>
            <th>方法</th>
            <th>状态码</th>
            <th>响应时间</th>
            <th>创建时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td colspan="10" class="loading-cell">
              <div class="spinner"></div>
              加载中...
            </td>
          </tr>
          <tr v-else-if="logs.length === 0">
            <td colspan="10" class="empty-cell">暂无日志记录</td>
          </tr>
          <tr v-else v-for="log in logs" :key="log.id">
            <td class="col-checkbox">
              <input
                type="checkbox"
                :checked="selectedIds.includes(log.id)"
                @change="toggleSelect(log.id)"
              />
            </td>
            <td>{{ log.id }}</td>
            <td class="task-id" :title="log.task_id">{{ log.task_id }}</td>
            <td>
              <span class="kind-badge" :class="`kind-${log.callback_kind || 'unknown'}`">
                {{ kindLabel(log.callback_kind) }}
              </span>
            </td>
            <td class="callback-url" :title="log.callback_url">
              {{ log.callback_url }}
            </td>
            <td>
              <span class="method-badge">{{ log.callback_method }}</span>
            </td>
            <td>
              <span class="status-badge success">{{ log.http_status_code }}</span>
            </td>
            <td class="response-time">{{ log.response_time_ms.toFixed(2) }} ms</td>
            <td class="created-at">{{ new Date(log.created_at).toLocaleString() }}</td>
            <td>
              <div class="row-actions">
                <button class="detail-btn" @click="viewDetail(log)" title="查看详情">
                  <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg"><path d="M8 3C4.5 3 1.5 8 1.5 8s3 5 6.5 5 6.5-5 6.5-5-3-5-6.5-5z" fill="none" stroke="currentColor" stroke-width="1.3"/><circle cx="8" cy="8" r="2" fill="currentColor"/></svg>
                  查看详情
                </button>
                <button class="delete-btn" @click="deleteLog(log.id)" title="删除">
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
      <button @click="prevPage" :disabled="page === 1">上一页</button>
      <span class="page-info">第 {{ page }} / {{ Math.ceil(total / pageSize) }} 页，共 {{ total }} 条</span>
      <button @click="nextPage" :disabled="page >= Math.ceil(total / pageSize)">下一页</button>
    </div>

    <!-- 详情弹窗 -->
    <div v-if="showDetail" class="modal-overlay" @click="closeDetail">
      <div class="modal-content" @click.stop>
        <div class="modal-header">
          <h3>回调成功日志详情 - ID: {{ detailLog?.id }}</h3>
          <button class="close-btn" @click="closeDetail">×</button>
        </div>

        <div v-if="detailLoading" class="modal-loading">
          <div class="spinner"></div>
          加载中...
        </div>

        <div v-else-if="detailLog" class="modal-body">
          <div class="detail-section">
            <h4>基本信息</h4>
            <div class="detail-grid">
              <div class="detail-item">
                <span class="detail-label">任务ID:</span>
                <span class="detail-value">{{ detailLog.task_id }}</span>
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
                <span class="detail-label">HTTP 状态码:</span>
                <span class="detail-value">{{ detailLog.http_status_code }}</span>
              </div>
              <div class="detail-item">
                <span class="detail-label">响应时间:</span>
                <span class="detail-value">{{ detailLog.response_time_ms.toFixed(2) }} ms</span>
              </div>
              <div class="detail-item">
                <span class="detail-label">创建时间:</span>
                <span class="detail-value">{{ new Date(detailLog.created_at).toLocaleString() }}</span>
              </div>
            </div>
          </div>

          <div v-if="detailLog.request_headers" class="detail-section">
            <h4>请求头</h4>
            <VueJsonPretty :data="detailLog.request_headers" :deep="3" class="json-viewer" />
          </div>

          <div class="detail-section">
            <h4>请求体（回调内容）</h4>
            <VueJsonPretty
              v-if="detailLog.request_body"
              :data="detailLog.request_body"
              :deep="3"
              class="json-viewer"
            />
            <div v-else class="no-data-hint">暂无请求体数据（旧记录未记录）</div>
          </div>

          <div v-if="detailLog.response_headers" class="detail-section">
            <h4>响应头</h4>
            <VueJsonPretty :data="detailLog.response_headers" :deep="2" class="json-viewer" />
          </div>

          <div v-if="detailLog.response_body" class="detail-section">
            <h4>响应体</h4>
            <VueJsonPretty
              v-if="parseResponseBody(detailLog.response_body)"
              :data="parseResponseBody(detailLog.response_body)"
              :deep="3"
              class="json-viewer"
            />
            <pre v-else class="detail-json">{{ detailLog.response_body }}</pre>
          </div>
        </div>

        <div class="modal-footer">
          <button class="close-modal-btn" @click="closeDetail">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.callback-success-logs {
  max-width: 100%;
  margin: 0;
}

.logs-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;
}

.logs-header h2 {
  font-size: 24px;
  font-weight: 600;
  color: #333;
}

.refresh-btn {
  background: #5470c6;
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 8px 16px;
  font-size: 14px;
  cursor: pointer;
  transition: all 0.2s;
}

.refresh-btn:hover:not(:disabled) {
  background: #4a63b3;
}

.refresh-btn:disabled {
  background: #bbb;
  cursor: not-allowed;
}

.header-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.batch-delete-btn {
  background: #d32f2f;
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 8px 16px;
  font-size: 14px;
  cursor: pointer;
  transition: all 0.2s;
}

.batch-delete-btn:hover:not(:disabled) {
  background: #b71c1c;
}

.batch-delete-btn:disabled {
  background: #bbb;
  cursor: not-allowed;
}

.clear-all-btn {
  background: #fff;
  color: #d32f2f;
  border: 1px solid #d32f2f;
}

.clear-all-btn:hover {
  background: #ffebee;
}

.col-checkbox {
  width: 40px;
  text-align: center;
}

.row-actions {
  display: flex;
  gap: 6px;
  align-items: center;
}

.delete-btn {
  background: #fff;
  border: 1px solid #ddd;
  border-radius: 4px;
  padding: 5px;
  cursor: pointer;
  color: #d32f2f;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  transition: all 0.2s;
}

.delete-btn svg {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}

.delete-btn:hover {
  background: #ffebee;
  border-color: #ef9a9a;
}

/* 筛选区域 */
.filter-section {
  background: #fff;
  border-radius: 8px;
  padding: 16px;
  margin-bottom: 16px;
  box-shadow: 0 1px 3px rgba(0,0,0,.1);
}

.filter-row {
  display: flex;
  align-items: flex-end;
  gap: 12px;
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
  min-width: 180px;
}

.filter-field select {
  border: 1px solid #ddd;
  border-radius: 5px;
  padding: 6px 10px;
  font-size: 13px;
  min-width: 120px;
  background: #fff;
  cursor: pointer;
}

.filter-field input:focus,
.filter-field select:focus {
  outline: none;
  border-color: #5470c6;
}

.filter-actions {
  display: flex;
  gap: 6px;
  align-self: flex-end;
}

.apply-btn,
.clear-btn {
  padding: 8px 16px;
  border: none;
  border-radius: 6px;
  font-size: 14px;
  cursor: pointer;
  transition: all 0.2s;
}

.apply-btn {
  background: #3ba272;
  color: #fff;
}

.apply-btn:hover {
  background: #2e8060;
}

.clear-btn {
  background: #e0e0e0;
  color: #666;
}

.clear-btn:hover {
  background: #d0d0d0;
}

.error-message {
  background: #fee2e2;
  color: #e74c3c;
  padding: 12px 16px;
  border-radius: 6px;
  margin-bottom: 16px;
}

/* 表格 */
.logs-table-container {
  background: #fff;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,.1);
  overflow-x: auto;
}

.logs-table {
  width: 100%;
  border-collapse: collapse;
}

.logs-table th {
  background: #f8f9fa;
  padding: 12px 16px;
  text-align: left;
  font-size: 13px;
  font-weight: 600;
  color: #555;
  border-bottom: 2px solid #e0e0e0;
}

.logs-table td {
  padding: 12px 16px;
  font-size: 14px;
  color: #333;
  border-bottom: 1px solid #f0f0f0;
}

.logs-table tbody tr:hover {
  background: #f8f9fa;
}

.task-id {
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 12px;
  font-weight: 700;
  color: #333;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: default;
}

.callback-url {
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.method-badge {
  display: inline-block;
  padding: 2px 8px;
  background: #5470c6;
  color: #fff;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
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

.status-badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 600;
}

.status-badge.success {
  background: #d1fae5;
  color: #065f46;
}

.response-time {
  font-family: monospace;
  color: #666;
}

.created-at {
  font-size: 13px;
  color: #888;
}

.detail-btn {
  background: #5470c6;
  color: #fff;
  border: none;
  border-radius: 4px;
  padding: 5px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s;
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.detail-btn svg {
  width: 13px;
  height: 13px;
  flex-shrink: 0;
}

.detail-btn:hover {
  background: #4a63b3;
  transform: translateY(-1px);
}

.loading-cell,
.empty-cell {
  text-align: center;
  padding: 40px 20px;
  color: #888;
}

.spinner {
  width: 20px;
  height: 20px;
  border: 2px solid rgba(0,0,0,.1);
  border-top-color: #5470c6;
  border-radius: 50%;
  animation: spin .8s linear infinite;
  display: inline-block;
  margin-right: 8px;
  vertical-align: middle;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* 分页 */
.pagination {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: 16px;
  margin-top: 16px;
  padding: 16px;
}

.pagination button {
  background: #5470c6;
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 8px 16px;
  font-size: 14px;
  cursor: pointer;
  transition: all 0.2s;
}

.pagination button:hover:not(:disabled) {
  background: #4a63b3;
}

.pagination button:disabled {
  background: #bbb;
  cursor: not-allowed;
}

.page-info {
  font-size: 14px;
  color: #666;
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
  border-radius: 8px;
  max-width: 800px;
  width: 100%;
  max-height: 90vh;
  overflow-y: auto;
  box-shadow: 0 4px 12px rgba(0,0,0,.15);
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 20px 24px;
  border-bottom: 1px solid #e0e0e0;
}

.modal-header h3 {
  font-size: 18px;
  font-weight: 600;
  color: #333;
}

.close-btn {
  background: none;
  border: none;
  font-size: 28px;
  color: #888;
  cursor: pointer;
  line-height: 1;
  padding: 0;
  width: 32px;
  height: 32px;
}

.close-btn:hover {
  color: #333;
}

.modal-loading {
  padding: 60px 20px;
  text-align: center;
  color: #888;
}

.modal-body {
  padding: 24px;
}

.detail-section {
  margin-bottom: 24px;
}

.detail-section:last-child {
  margin-bottom: 0;
}

.detail-section h4 {
  font-size: 16px;
  font-weight: 600;
  color: #333;
  margin-bottom: 12px;
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
  font-size: 13px;
  color: #888;
  font-weight: 500;
}

.detail-value {
  font-size: 14px;
  color: #333;
  word-break: break-all;
}

.detail-json {
  background: #f8f9fa;
  border: 1px solid #e0e0e0;
  border-radius: 6px;
  padding: 12px;
  font-size: 13px;
  font-family: 'Consolas', 'Monaco', monospace;
  overflow-x: auto;
  max-height: 300px;
  overflow-y: auto;
}

.json-viewer {
  background: #f8f9fa;
  border: 1px solid #e0e0e0;
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

.modal-footer {
  padding: 16px 24px;
  border-top: 1px solid #e0e0e0;
  display: flex;
  justify-content: flex-end;
}

.close-modal-btn {
  background: #e0e0e0;
  color: #666;
  border: none;
  border-radius: 6px;
  padding: 8px 24px;
  font-size: 14px;
  cursor: pointer;
  transition: all 0.2s;
}

.close-modal-btn:hover {
  background: #d0d0d0;
}

@media (max-width: 900px) {
  .detail-grid {
    grid-template-columns: 1fr;
  }
}
</style>
