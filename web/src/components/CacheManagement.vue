<script setup>
import { ref, computed, onMounted } from 'vue'
import { authFetch, getFeatures } from '../utils/auth.js'

const emit = defineEmits(['open-graph'])

// 功能模块开关：控制缓存分类 tab 的显示
const features = getFeatures()

// ─── 缓存分类 Tab ────────────────────────────────────────────────────────────
// ner / attack：提取缓存（tasks 表，NER 与 ATT&CK 共用下方全局策略，仅视图/统计/清理分开）
// ti：联网解析缓存（ti_tasks 表，策略独立，走 /ti/settings）
const CACHE_TABS = [
  { value: 'ner',    label: 'NER 缓存' },
  { value: 'attack', label: 'ATT&CK 缓存' },
  { value: 'ti',     label: '联网解析缓存' },
]
// 按功能开关过滤后的可见分类；默认落在第一个可用分类上
const visibleTabs = CACHE_TABS.filter(t => features[t.value])
const cacheTab = ref((visibleTabs[0] || CACHE_TABS[0]).value)
const currentTabLabel = computed(
  () => (CACHE_TABS.find(t => t.value === cacheTab.value) || {}).label || ''
)
const PLATFORM_LABELS = { threatbook: '微步在线', qianxin: '奇安信' }
const SEED_TYPE_LABELS = { ip: 'IP 地址', domain: '域名', file: '样本哈希' }

// ─── 策略设置（提取缓存全局策略）──────────────────────────────────────────────
const settings = ref({ enabled: true, ttl_seconds: 28800, refresh_on_hit: true })
const loadingSettings = ref(true)
const savingSettings = ref(false)
const saveTip = ref('')

// TTL 用「小时」在界面上编辑，更直观
const ttlHours = ref(8)

// ─── 策略设置（联网解析缓存，/ti/settings 的 cache_enabled / cache_ttl）────────
const tiSettings = ref({ cache_enabled: true, cache_ttl: 315360000 })
const tiTtlHours = ref(6)
const loadingTiSettings = ref(false)
const savingTiSettings = ref(false)
const tiSaveTip = ref('')

// ─── 统计 ────────────────────────────────────────────────────────────────────
const stats = ref({ active: 0, expired: 0, soonest_expire: null, latest_expire: null })
const loadingStats = ref(true)

// ─── 缓存条目列表 ────────────────────────────────────────────────────────────
const entries = ref([])
const entryTotal = ref(0)
const page = ref(1)
const pageSize = ref(10)
const pageSizeOptions = [10, 20, 30, 50]
const onlyActive = ref(true)
const loadingEntries = ref(true)

function showError(err, defaultMsg) {
  let msg = defaultMsg
  if (err && err.detail) {
    if (typeof err.detail === 'string') msg = err.detail
    else if (Array.isArray(err.detail)) {
      msg = err.detail.map(d => {
        const field = d.loc && d.loc.length > 1 ? `${d.loc[1]}: ` : ''
        return `${field}${d.msg || JSON.stringify(d)}`
      }).join('\n')
    } else if (typeof err.detail === 'object') msg = err.detail.message || JSON.stringify(err.detail)
  }
  alert(msg)
}

function formatDate(d) {
  if (!d) return '-'
  return new Date(d).toLocaleString('zh-CN')
}

function formatDuration(seconds) {
  if (!seconds || seconds <= 0) return '0'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  const s = seconds % 60
  const parts = []
  if (h) parts.push(`${h} 小时`)
  if (m) parts.push(`${m} 分`)
  if (s) parts.push(`${s} 秒`)
  return parts.join(' ')
}

// ─── 加载 ────────────────────────────────────────────────────────────────────

async function fetchSettings() {
  loadingSettings.value = true
  try {
    const res = await authFetch('/admin/cache/settings')
    if (res.ok) {
      settings.value = await res.json()
      ttlHours.value = +(settings.value.ttl_seconds / 3600).toFixed(2)
    }
  } finally {
    loadingSettings.value = false
  }
}

async function fetchStats() {
  loadingStats.value = true
  try {
    const res = await authFetch(`/admin/cache/stats?category=${cacheTab.value}`)
    if (res.ok) stats.value = await res.json()
  } finally {
    loadingStats.value = false
  }
}

async function fetchEntries() {
  loadingEntries.value = true
  try {
    const res = await authFetch(
      `/admin/cache/entries?page=${page.value}&page_size=${pageSize.value}&only_active=${onlyActive.value}&category=${cacheTab.value}`
    )
    if (res.ok) {
      const data = await res.json()
      entries.value = data.items
      entryTotal.value = data.total
    }
  } finally {
    loadingEntries.value = false
  }
}

// ─── 联网解析缓存策略（GET/PUT /ti/settings，与「联网解析代理」同一接口）────────

async function fetchTiSettings() {
  loadingTiSettings.value = true
  try {
    const res = await authFetch('/ti/settings')
    if (res.ok) {
      const data = await res.json()
      tiSettings.value = {
        cache_enabled: data.cache_enabled !== false,
        cache_ttl: data.cache_ttl ?? 315360000,
      }
      tiTtlHours.value = +(tiSettings.value.cache_ttl / 3600).toFixed(2)
    }
  } finally {
    loadingTiSettings.value = false
  }
}

async function saveTiSettings() {
  savingTiSettings.value = true
  tiSaveTip.value = ''
  try {
    const res = await authFetch('/ti/settings', {
      method: 'PUT',
      body: {
        cache_enabled: tiSettings.value.cache_enabled,
        cache_ttl: Math.round(tiTtlHours.value * 3600),
      },
    })
    if (res.ok) {
      const data = await res.json()
      tiSettings.value = {
        cache_enabled: data.settings.cache_enabled !== false,
        cache_ttl: data.settings.cache_ttl ?? 315360000,
      }
      tiTtlHours.value = +(tiSettings.value.cache_ttl / 3600).toFixed(2)
      tiSaveTip.value = '已保存'
      setTimeout(() => (tiSaveTip.value = ''), 2000)
    } else {
      showError(await res.json(), '保存失败')
    }
  } finally {
    savingTiSettings.value = false
  }
}

// ─── 操作 ────────────────────────────────────────────────────────────────────

async function saveSettings() {
  savingSettings.value = true
  saveTip.value = ''
  try {
    const ttl_seconds = Math.round(ttlHours.value * 3600)
    const res = await authFetch('/admin/cache/settings', {
      method: 'PUT',
      body: {
        enabled: settings.value.enabled,
        ttl_seconds,
        refresh_on_hit: settings.value.refresh_on_hit,
      },
    })
    if (res.ok) {
      settings.value = await res.json()
      ttlHours.value = +(settings.value.ttl_seconds / 3600).toFixed(2)
      saveTip.value = '已保存'
      setTimeout(() => (saveTip.value = ''), 2000)
    } else {
      showError(await res.json(), '保存失败')
    }
  } finally {
    savingSettings.value = false
  }
}

async function invalidateEntry(taskId) {
  if (!confirm('让此任务缓存失效？任务本身不会被删除，只是不再用于复用。')) return
  const res = await authFetch(`/admin/cache/entries/${taskId}`, { method: 'DELETE' })
  if (res.ok) {
    await Promise.all([fetchEntries(), fetchStats()])
  } else {
    showError(await res.json(), '操作失败')
  }
}

async function clearExpired() {
  if (!confirm(`清理「${currentTabLabel.value}」所有已过期缓存？（仅置为不可复用，不删除任务）`)) return
  const res = await authFetch(`/admin/cache/clear-expired?category=${cacheTab.value}`, { method: 'POST' })
  if (res.ok) {
    const data = await res.json()
    alert(data.message)
    await Promise.all([fetchEntries(), fetchStats()])
  } else {
    showError(await res.json(), '操作失败')
  }
}

async function clearAll() {
  if (!confirm(`清空「${currentTabLabel.value}」全部缓存复用？该分类下所有任务都将不再被复用（不删除任务），其他分类不受影响。此操作不可撤销。`)) return
  const res = await authFetch(`/admin/cache/clear-all?category=${cacheTab.value}`, { method: 'POST' })
  if (res.ok) {
    const data = await res.json()
    alert(data.message)
    await Promise.all([fetchEntries(), fetchStats()])
  } else {
    showError(await res.json(), '操作失败')
  }
}

function changePage(delta) {
  const next = page.value + delta
  if (next < 1 || (next - 1) * pageSize.value >= entryTotal.value) return
  page.value = next
  fetchEntries()
}

function changePageSize(size) {
  pageSize.value = Number(size)
  page.value = 1
  fetchEntries()
}

function toggleOnlyActive() {
  onlyActive.value = !onlyActive.value
  page.value = 1
  fetchEntries()
}

function refreshAll() {
  fetchStats()
  fetchEntries()
}

// 切换分类：重置分页，重新拉统计与条目；TI 分类顺带拉自己的策略
function switchTab(tab) {
  if (cacheTab.value === tab) return
  cacheTab.value = tab
  page.value = 1
  fetchStats()
  fetchEntries()
  if (tab === 'ti') fetchTiSettings()
}

function openGraph(taskId) {
  emit('open-graph', taskId)
}

onMounted(() => {
  // 初始分类可能是 ti（NER/ATT&CK 被开关禁用时）：此时拉 TI 自己的策略，无需拉提取缓存策略
  if (cacheTab.value === 'ti') {
    fetchTiSettings()
  } else {
    fetchSettings()
  }
  fetchStats()
  fetchEntries()
})
</script>

<template>
  <div class="cache-mgmt">
    <!-- 分类 Tab：NER / ATT&CK 共用提取缓存全局策略；联网解析缓存策略独立；按功能开关过滤 -->
    <div class="cache-tabs">
      <button
        v-for="t in visibleTabs"
        :key="t.value"
        :class="{ active: cacheTab === t.value }"
        @click="switchTab(t.value)"
      >{{ t.label }}</button>
    </div>

    <!-- 策略设置（提取缓存全局策略，NER / ATT&CK 共用） -->
    <section v-if="cacheTab !== 'ti'" class="card">
      <h4 class="card-title">缓存复用策略<span class="total-info">NER 与 ATT&CK 共用</span></h4>
      <div v-if="loadingSettings" class="loading-text">加载中...</div>
      <div v-else class="settings-form">
        <div class="setting-row">
          <label class="switch-label">
            <input type="checkbox" v-model="settings.enabled" />
            <span>启用缓存复用（全局总开关）</span>
          </label>
          <p class="hint">关闭后即使请求带 reuse_cache=true 也不会命中或写入缓存</p>
        </div>

        <div class="setting-row">
          <label class="field-label">缓存有效期 TTL（小时）</label>
          <div class="ttl-input">
            <input v-model.number="ttlHours" type="number" min="0" step="0.5" :disabled="!settings.enabled" />
            <span class="ttl-eq">= {{ formatDuration(Math.round(ttlHours * 3600)) }}</span>
          </div>
          <p class="hint">默认 8 小时。任务完成后此时长内可被复用；设为 0 表示不缓存</p>
        </div>

        <div class="setting-row">
          <label class="switch-label">
            <input type="checkbox" v-model="settings.refresh_on_hit" :disabled="!settings.enabled" />
            <span>命中时刷新有效期（滑动过期）</span>
          </label>
          <p class="hint">开启后每次命中缓存都会把过期时间重置为 NOW() + TTL</p>
        </div>

        <div class="form-actions">
          <button class="btn-primary" :disabled="savingSettings" @click="saveSettings">
            {{ savingSettings ? '保存中...' : '保存策略' }}
          </button>
          <span v-if="saveTip" class="save-tip">✓ {{ saveTip }}</span>
        </div>
      </div>
    </section>

    <!-- 策略设置（联网解析缓存，独立策略） -->
    <section v-if="cacheTab === 'ti'" class="card">
      <h4 class="card-title">联网解析缓存策略</h4>
      <div v-if="loadingTiSettings" class="loading-text">加载中...</div>
      <div v-else class="settings-form">
        <div class="setting-row">
          <label class="switch-label">
            <input type="checkbox" v-model="tiSettings.cache_enabled" />
            <span>启用缓存复用（联网解析总开关）</span>
          </label>
          <p class="hint">关闭后 /ti/expand 即使带 reuse_cache=true 也不会命中缓存，任务结果也不再写入缓存窗口</p>
        </div>

        <div class="setting-row">
          <label class="field-label">缓存有效期 TTL（小时）</label>
          <div class="ttl-input">
            <input v-model.number="tiTtlHours" type="number" min="0" step="0.5" :disabled="!tiSettings.cache_enabled" />
            <span class="ttl-eq">= {{ formatDuration(Math.round(tiTtlHours * 3600)) }}</span>
          </div>
          <p class="hint">默认 10 年（87600 小时），可任意调整（上限 876000 小时/100 年）。同种子 + 同平台在 TTL 内直接复用历史结果，不触达外部平台、不占日调用上限；命中自动滑动续期</p>
        </div>

        <div class="form-actions">
          <button class="btn-primary" :disabled="savingTiSettings" @click="saveTiSettings">
            {{ savingTiSettings ? '保存中...' : '保存策略' }}
          </button>
          <span v-if="tiSaveTip" class="save-tip">✓ {{ tiSaveTip }}</span>
        </div>
      </div>
    </section>

    <!-- 统计概览 -->
    <section class="card">
      <div class="card-head">
        <h4 class="card-title">缓存概览<span class="total-info">{{ currentTabLabel }}</span></h4>
        <button class="btn-text" @click="refreshAll">刷新</button>
      </div>
      <div v-if="loadingStats" class="loading-text">加载中...</div>
      <div v-else class="stat-grid">
        <div class="stat-box">
          <div class="stat-num active">{{ stats.active }}</div>
          <div class="stat-label">可复用</div>
        </div>
        <div class="stat-box">
          <div class="stat-num expired">{{ stats.expired }}</div>
          <div class="stat-label">已过期</div>
        </div>
        <div class="stat-box wide">
          <div class="stat-sub">最近过期：{{ formatDate(stats.soonest_expire) }}</div>
          <div class="stat-sub">最远过期：{{ formatDate(stats.latest_expire) }}</div>
        </div>
      </div>
      <div class="danger-actions">
        <button class="btn-warn" @click="clearExpired">清理已过期</button>
        <button class="btn-danger" @click="clearAll">清空全部缓存</button>
        <span class="hint danger-hint">操作仅作用于当前分类「{{ currentTabLabel }}」</span>
      </div>
    </section>

    <!-- 缓存条目列表 -->
    <section class="card">
      <div class="card-head">
        <h4 class="card-title">缓存条目<span class="total-info">{{ currentTabLabel }} · 共 {{ entryTotal }} 条</span></h4>
        <label class="switch-label small">
          <input type="checkbox" :checked="onlyActive" @change="toggleOnlyActive" />
          <span>仅看未过期</span>
        </label>
      </div>

      <div v-if="loadingEntries" class="loading-text">加载中...</div>
      <template v-else>
        <table v-if="entries.length" class="data-table">
          <thead>
            <tr v-if="cacheTab !== 'ti'">
              <th>任务 ID</th>
              <th>模型</th>
              <th>文本预览</th>
              <th>实体数</th>
              <th>过期时间</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
            <tr v-else>
              <th>任务 ID</th>
              <th>平台</th>
              <th>种子类型</th>
              <th>种子值</th>
              <th>过期时间</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="e in entries" :key="e.task_id">
              <td>
                <button v-if="cacheTab !== 'ti'" class="link-btn" :title="`查看任务 ${e.task_id} 的解析详情`" @click="openGraph(e.task_id)">
                  <code>{{ e.task_id.slice(0, 8) }}…</code>
                </button>
                <code v-else :title="e.task_id">{{ e.task_id.slice(0, 8) }}…</code>
              </td>
              <template v-if="cacheTab !== 'ti'">
                <td>{{ e.model_name }}</td>
                <td class="preview-cell clickable" :title="e.input_text_preview" @click="openGraph(e.task_id)">{{ e.input_text_preview || '-' }}</td>
                <td>{{ e.entity_count ?? '-' }}</td>
              </template>
              <template v-else>
                <td>{{ PLATFORM_LABELS[e.platform] || e.platform }}</td>
                <td>{{ SEED_TYPE_LABELS[e.seed_type] || e.seed_type }}</td>
                <td class="preview-cell" :title="e.seed_value">{{ e.seed_value || '-' }}</td>
              </template>
              <td :class="{ 'text-danger': e.expired }">{{ formatDate(e.cache_expires_at) }}</td>
              <td>
                <span class="badge" :class="e.expired ? 'badge-muted' : 'badge-success'">
                  {{ e.expired ? '已过期' : '可复用' }}
                </span>
              </td>
              <td>
                <button class="btn-action danger" title="使缓存失效" @click="invalidateEntry(e.task_id)">
                  <svg viewBox="0 0 16 16" class="action-icon">
                    <path d="M4 4h8l-.5 10H4.5L4 4zM6 2h4M2 4h12" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/>
                  </svg>
                </button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-state">暂无缓存条目</div>

        <div v-if="entryTotal" class="pager">
          <div class="page-size-select">
            <span>每页</span>
            <select :value="pageSize" @change="changePageSize($event.target.value)">
              <option v-for="opt in pageSizeOptions" :key="opt" :value="opt">{{ opt }}</option>
            </select>
            <span>条</span>
          </div>
          <button class="btn-text" :disabled="page === 1" @click="changePage(-1)">上一页</button>
          <span class="page-info">第 {{ page }} 页 / 共 {{ Math.max(1, Math.ceil(entryTotal / pageSize)) }} 页</span>
          <button class="btn-text" :disabled="page * pageSize >= entryTotal" @click="changePage(1)">下一页</button>
        </div>
      </template>
    </section>
  </div>
</template>

<style scoped>
.cache-mgmt {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* 分类 Tab（视觉对齐管理页二级 tab） */
.cache-tabs {
  display: flex;
  gap: 4px;
  border-bottom: 2px solid #e9ecef;
}

.cache-tabs button {
  background: transparent;
  border: none;
  color: #888;
  padding: 10px 18px;
  cursor: pointer;
  font-size: 13.5px;
  font-weight: 500;
  transition: all .15s;
  border-bottom: 2px solid transparent;
  margin-bottom: -2px;
}

.cache-tabs button:hover { color: #555; }
.cache-tabs button.active {
  color: #5470c6;
  border-bottom-color: #5470c6;
}

.card {
  background: #f8f9fc;
  border: 1px solid #e8ecf4;
  border-radius: 10px;
  padding: 18px 20px;
}

.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 14px;
}

.card-title {
  font-size: 15px;
  color: #1a1a2e;
  margin: 0 0 14px 0;
}

.card-head .card-title { margin: 0; }

.total-info {
  font-size: 12px;
  color: #999;
  font-weight: 400;
  margin-left: 6px;
}

/* 策略表单 */
.settings-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.setting-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.field-label {
  font-size: 13px;
  color: #444;
  font-weight: 500;
}

.switch-label {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-size: 13px;
  color: #333;
}

.switch-label.small { font-size: 12px; color: #666; }

.switch-label input[type="checkbox"] {
  width: 16px;
  height: 16px;
  accent-color: #5470c6;
}

.ttl-input {
  display: flex;
  align-items: center;
  gap: 10px;
}

.ttl-input input {
  width: 120px;
  padding: 8px 12px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  font-size: 13px;
  outline: none;
}

.ttl-input input:focus { border-color: #5470c6; box-shadow: 0 0 0 2px rgba(84,112,198,0.15); }
.ttl-input input:disabled { background: #eef0f4; color: #999; }

.ttl-eq {
  font-size: 12px;
  color: #888;
}

.hint {
  font-size: 12px;
  color: #aaa;
  margin: 0;
}

.form-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.save-tip {
  font-size: 13px;
  color: #15803d;
}

/* 统计 */
.stat-grid {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.stat-box {
  background: #fff;
  border: 1px solid #eef0f4;
  border-radius: 10px;
  padding: 16px 20px;
  min-width: 110px;
  text-align: center;
}

.stat-box.wide {
  text-align: left;
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 6px;
}

.stat-num {
  font-size: 28px;
  font-weight: 700;
  line-height: 1;
}

.stat-num.active { color: #15803d; }
.stat-num.expired { color: #999; }

.stat-label {
  font-size: 12px;
  color: #888;
  margin-top: 6px;
}

.stat-sub {
  font-size: 12px;
  color: #666;
}

.danger-actions {
  display: flex;
  gap: 10px;
  align-items: center;
}

.danger-hint {
  margin-left: 4px;
}

/* 表格 */
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  background: #fff;
  border-radius: 8px;
  overflow: hidden;
}

.data-table th {
  text-align: left;
  padding: 10px 12px;
  background: #f0f2f8;
  color: #666;
  font-weight: 500;
  border-bottom: 1px solid #e8ecf4;
}

.data-table td {
  padding: 10px 12px;
  border-bottom: 1px solid #f0f2f5;
  color: #333;
}

.data-table code {
  background: #f0f2f5;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 12px;
}

.preview-cell {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.preview-cell.clickable {
  cursor: pointer;
}

.preview-cell.clickable:hover {
  color: #5470c6;
}

.link-btn {
  background: transparent;
  border: none;
  padding: 0;
  cursor: pointer;
}

.link-btn code {
  background: #eef2ff;
  color: #5470c6;
  transition: background 0.15s;
}

.link-btn:hover code {
  background: #dbe4ff;
  text-decoration: underline;
}

.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 500;
}

.badge-success { background: #dcfce7; color: #15803d; }
.badge-muted   { background: #f0f0f0; color: #888; }
.text-danger   { color: #dc2626; }

.btn-action {
  background: #f0f2f5;
  border: none;
  border-radius: 6px;
  padding: 5px;
  cursor: pointer;
  transition: all 0.15s;
}

.btn-action.danger:hover { background: #fee2e2; }
.action-icon { width: 14px; height: 14px; color: #555; display: block; }
.btn-action.danger .action-icon { color: #dc2626; }

/* 按钮 */
.btn-primary {
  background: linear-gradient(135deg, #5470c6, #3ba272);
  color: #fff;
  border: none;
  border-radius: 8px;
  padding: 8px 16px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
}

.btn-primary:hover:not(:disabled) { opacity: 0.9; }
.btn-primary:disabled { opacity: 0.6; cursor: not-allowed; }

.btn-warn {
  background: #fff7ed;
  color: #c2410c;
  border: 1px solid #fed7aa;
  border-radius: 8px;
  padding: 7px 14px;
  font-size: 13px;
  cursor: pointer;
}

.btn-warn:hover { background: #ffedd5; }

.btn-danger {
  background: #fef2f2;
  color: #dc2626;
  border: 1px solid #fecaca;
  border-radius: 8px;
  padding: 7px 14px;
  font-size: 13px;
  cursor: pointer;
}

.btn-danger:hover { background: #fee2e2; }

.btn-text {
  background: transparent;
  border: none;
  color: #888;
  font-size: 13px;
  cursor: pointer;
  padding: 4px 8px;
}

.btn-text:hover:not(:disabled) { color: #555; }
.btn-text:disabled { opacity: 0.4; cursor: not-allowed; }

.pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  margin-top: 14px;
}

.page-info { font-size: 12px; color: #888; }

.page-size-select {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: #888;
}

.page-size-select select {
  padding: 4px 8px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  font-size: 12px;
  color: #333;
  background: #fff;
  cursor: pointer;
  outline: none;
}

.page-size-select select:focus { border-color: #5470c6; }

.loading-text {
  text-align: center;
  padding: 28px;
  color: #888;
  font-size: 14px;
}

.empty-state {
  text-align: center;
  padding: 24px;
  color: #aaa;
  font-size: 13px;
}

@media (max-width: 900px) {
  .stat-grid { flex-direction: column; }
}
</style>
