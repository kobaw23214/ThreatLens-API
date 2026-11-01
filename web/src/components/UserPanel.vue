<script setup>
import { ref, onMounted, computed } from 'vue'
import { authFetch, getUser } from '../utils/auth.js'

const user = ref(null)
const usage = ref(null)
const apiKeys = ref([])
const loading = ref(true)

// API Key 创建
const showCreateKey = ref(false)
const newKeyName = ref('')
const newKeyExpireDays = ref(30)
const creatingKey = ref(false)
const createdKey = ref(null) // 创建成功后展示完整 key

// 修改密码
const showChangePwd = ref(false)
const oldPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const pwdMsg = ref('')
const pwdError = ref(false)
const changingPwd = ref(false)

// 复制成功提示
const copyTip = ref('')

const usagePercent5h = computed(() => {
  if (!usage.value || usage.value.limit_5h === 0) return 0
  return Math.min(100, (usage.value.used_5h / usage.value.limit_5h) * 100)
})

const usagePercentWeekly = computed(() => {
  if (!usage.value || usage.value.limit_weekly === 0) return 0
  return Math.min(100, (usage.value.used_weekly / usage.value.limit_weekly) * 100)
})

function formatError(err, defaultMsg) {
  let errMsg = defaultMsg
  if (err && err.detail) {
    if (typeof err.detail === 'string') {
      errMsg = err.detail
    } else if (Array.isArray(err.detail)) {
      errMsg = err.detail.map(d => {
        const field = d.loc && d.loc.length > 1 ? `${d.loc[1]}: ` : ''
        return `${field}${d.msg || JSON.stringify(d)}`
      }).join('\n')
    } else if (typeof err.detail === 'object') {
      errMsg = err.detail.message || JSON.stringify(err.detail)
    }
  }
  return errMsg
}

async function fetchData() {
  loading.value = true
  try {
    const [meRes, keysRes] = await Promise.all([
      authFetch('/auth/me'),
      authFetch('/auth/api-keys'),
    ])
    if (meRes.ok) {
      const data = await meRes.json()
      user.value = data.user
      usage.value = data.usage
    }
    if (keysRes.ok) {
      apiKeys.value = await keysRes.json()
    }
  } catch (e) {
    console.error('获取用户信息失败', e)
  } finally {
    loading.value = false
  }
}

async function createApiKey() {
  creatingKey.value = true
  createdKey.value = null
  try {
    const res = await authFetch('/auth/api-keys', {
      method: 'POST',
      body: { name: newKeyName.value, expires_days: newKeyExpireDays.value },
    })
    if (res.ok) {
      const data = await res.json()
      createdKey.value = data
      showCreateKey.value = false
      newKeyName.value = ''
      newKeyExpireDays.value = 30
      // 刷新列表
      const keysRes = await authFetch('/auth/api-keys')
      if (keysRes.ok) apiKeys.value = await keysRes.json()
    } else {
      const err = await res.json()
      alert(formatError(err, '创建失败'))
    }
  } finally {
    creatingKey.value = false
  }
}

async function deleteKey(keyId) {
  if (!confirm('确定要删除这个 API Key 吗？')) return
  const res = await authFetch(`/auth/api-keys/${keyId}`, { method: 'DELETE' })
  if (res.ok) {
    apiKeys.value = apiKeys.value.filter(k => k.id !== keyId)
  }
}

async function changePassword() {
  pwdMsg.value = ''
  pwdError.value = false
  if (newPassword.value !== confirmPassword.value) {
    pwdMsg.value = '两次输入的新密码不一致'
    pwdError.value = true
    return
  }
  changingPwd.value = true
  try {
    const res = await authFetch('/auth/change-password', {
      method: 'POST',
      body: { old_password: oldPassword.value, new_password: newPassword.value },
    })
    const data = await res.json()
    if (res.ok) {
      pwdMsg.value = '密码修改成功'
      pwdError.value = false
      oldPassword.value = ''
      newPassword.value = ''
      confirmPassword.value = ''
      setTimeout(() => { showChangePwd.value = false; pwdMsg.value = '' }, 1500)
    } else {
      pwdMsg.value = formatError(data, '修改失败')
      pwdError.value = true
    }
  } finally {
    changingPwd.value = false
  }
}

function copyToClipboard(text) {
  navigator.clipboard.writeText(text).then(() => {
    copyTip.value = text
    setTimeout(() => copyTip.value = '', 2000)
  })
}

function formatDate(d) {
  if (!d) return '-'
  return new Date(d).toLocaleString('zh-CN')
}

function isExpired(d) {
  if (!d) return false
  return new Date(d) < new Date()
}

onMounted(fetchData)
</script>

<template>
  <div class="user-panel">
    <div v-if="loading" class="loading-state">加载中...</div>

    <template v-else>
      <!-- 额度概览 -->
      <div class="section">
        <h3 class="section-title">
          <svg viewBox="0 0 20 20" class="section-icon">
            <circle cx="10" cy="10" r="8" fill="none" stroke="currentColor" stroke-width="1.5"/>
            <path d="M10 6v4l3 2" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round"/>
          </svg>
          调用额度
        </h3>
        <div class="quota-grid" v-if="usage">
          <div class="quota-card">
            <div class="quota-header">
              <span class="quota-label">5小时限额</span>
              <span class="quota-value" :class="{ warning: usagePercent5h > 80 }">
                {{ usage.limit_5h === 0 ? '无限制' : `${usage.used_5h} / ${usage.limit_5h}` }}
              </span>
            </div>
            <div class="progress-bar" v-if="usage.limit_5h > 0">
              <div class="progress-fill"
                :style="{ width: usagePercent5h + '%' }"
                :class="{ warning: usagePercent5h > 80, danger: usagePercent5h >= 100 }"
              ></div>
            </div>
            <div v-else class="unlimited-badge">∞ 无限制</div>
          </div>

          <div class="quota-card">
            <div class="quota-header">
              <span class="quota-label">每周限额</span>
              <span class="quota-value" :class="{ warning: usagePercentWeekly > 80 }">
                {{ usage.limit_weekly === 0 ? '无限制' : `${usage.used_weekly} / ${usage.limit_weekly}` }}
              </span>
            </div>
            <div class="progress-bar" v-if="usage.limit_weekly > 0">
              <div class="progress-fill"
                :style="{ width: usagePercentWeekly + '%' }"
                :class="{ warning: usagePercentWeekly > 80, danger: usagePercentWeekly >= 100 }"
              ></div>
            </div>
            <div v-else class="unlimited-badge">∞ 无限制</div>
          </div>
        </div>
      </div>

      <!-- API Key 管理 -->
      <div class="section">
        <div class="section-header">
          <h3 class="section-title">
            <svg viewBox="0 0 20 20" class="section-icon">
              <path d="M12 2a4 4 0 00-3.46 6L2 14.54V18h3.46L6 17.5l1 .5h1.5l1-1L15 11.46A4 4 0 0012 2z"
                fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>
              <circle cx="13" cy="5" r="1.2" fill="currentColor"/>
            </svg>
            API Key
          </h3>
          <button class="btn-primary btn-sm" @click="showCreateKey = true">
            + 创建 Key
          </button>
        </div>

        <!-- 创建成功提示 -->
        <div v-if="createdKey" class="key-created-alert">
          <div class="alert-header">
            <svg viewBox="0 0 16 16" class="alert-icon success">
              <circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" stroke-width="1.2"/>
              <path d="M5 8l2 2 4-4" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            API Key 创建成功！请立即复制保存，此 Key 仅显示一次。
          </div>
          <div class="key-display">
            <code>{{ createdKey.key }}</code>
            <button class="btn-copy" @click="copyToClipboard(createdKey.key)">
              {{ copyTip === createdKey.key ? '✓ 已复制' : '复制' }}
            </button>
          </div>
          <button class="btn-text" @click="createdKey = null">关闭</button>
        </div>

        <!-- 创建对话框 -->
        <div v-if="showCreateKey" class="inline-form">
          <div class="form-row">
            <div class="form-field">
              <label>名称描述</label>
              <input v-model="newKeyName" placeholder="例如：外部系统调用" />
            </div>
            <div class="form-field" style="width: 140px">
              <label>过期天数</label>
              <input v-model.number="newKeyExpireDays" type="number" min="1" max="365" />
            </div>
          </div>
          <div class="form-actions">
            <button class="btn-primary btn-sm" :disabled="creatingKey" @click="createApiKey">
              {{ creatingKey ? '创建中...' : '确认创建' }}
            </button>
            <button class="btn-text" @click="showCreateKey = false">取消</button>
          </div>
        </div>

        <!-- Key 列表 -->
        <table class="data-table" v-if="apiKeys.length">
          <thead>
            <tr>
              <th>前缀</th>
              <th>名称</th>
              <th>创建时间</th>
              <th>过期时间</th>
              <th>状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="key in apiKeys" :key="key.id" :class="{ expired: isExpired(key.expires_at) }">
              <td><code>{{ key.key_prefix }}...</code></td>
              <td>{{ key.name || '-' }}</td>
              <td>{{ formatDate(key.created_at) }}</td>
              <td :class="{ 'text-danger': isExpired(key.expires_at) }">
                {{ formatDate(key.expires_at) }}
                <span v-if="isExpired(key.expires_at)" class="badge badge-danger">已过期</span>
              </td>
              <td>
                <span class="badge" :class="key.is_active ? 'badge-success' : 'badge-muted'">
                  {{ key.is_active ? '启用' : '停用' }}
                </span>
              </td>
              <td>
                <button class="btn-danger btn-xs" @click="deleteKey(key.id)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="empty-state">暂无 API Key</div>
      </div>

      <!-- 修改密码 -->
      <div class="section">
        <div class="section-header">
          <h3 class="section-title">
            <svg viewBox="0 0 20 20" class="section-icon">
              <rect x="4" y="9" width="12" height="9" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/>
              <path d="M7 9V6a3 3 0 016 0v3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            </svg>
            修改密码
          </h3>
          <button v-if="!showChangePwd" class="btn-outline btn-sm" @click="showChangePwd = true">修改</button>
        </div>
        <div v-if="showChangePwd" class="inline-form pwd-form">
          <div class="form-field">
            <label>旧密码</label>
            <input v-model="oldPassword" type="password" placeholder="请输入旧密码" />
          </div>
          <div class="form-field">
            <label>新密码</label>
            <input v-model="newPassword" type="password" placeholder="请输入新密码（至少6位）" />
          </div>
          <div class="form-field">
            <label>确认新密码</label>
            <input v-model="confirmPassword" type="password" placeholder="再次输入新密码" />
          </div>
          <div v-if="pwdMsg" class="pwd-msg" :class="{ error: pwdError, success: !pwdError }">
            {{ pwdMsg }}
          </div>
          <div class="form-actions">
            <button class="btn-primary btn-sm" :disabled="changingPwd" @click="changePassword">
              {{ changingPwd ? '提交中...' : '确认修改' }}
            </button>
            <button class="btn-text" @click="showChangePwd = false; pwdMsg = ''">取消</button>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.user-panel {
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.loading-state {
  text-align: center;
  padding: 40px;
  color: #888;
}

/* Section */
.section {
  background: #fff;
  border-radius: 12px;
  padding: 24px;
  box-shadow: 0 1px 3px rgba(0,0,0,.06);
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}

.section-title {
  font-size: 16px;
  font-weight: 600;
  color: #1a1a2e;
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
}

.section-header .section-title {
  margin-bottom: 0;
}

.section-icon {
  width: 18px;
  height: 18px;
  color: #5470c6;
}

/* 额度卡片 */
.quota-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.quota-card {
  background: #f8f9fc;
  border-radius: 10px;
  padding: 16px 20px;
  border: 1px solid #e8ecf4;
}

.quota-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
}

.quota-label {
  font-size: 13px;
  color: #666;
  font-weight: 500;
}

.quota-value {
  font-size: 14px;
  font-weight: 600;
  color: #333;
}

.quota-value.warning {
  color: #f59e0b;
}

.progress-bar {
  height: 6px;
  background: #e5e7eb;
  border-radius: 3px;
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, #5470c6, #3ba272);
  border-radius: 3px;
  transition: width 0.5s ease;
}

.progress-fill.warning {
  background: linear-gradient(90deg, #f59e0b, #ef4444);
}

.progress-fill.danger {
  background: #ef4444;
}

.unlimited-badge {
  color: #3ba272;
  font-size: 14px;
  font-weight: 600;
}

/* 表格 */
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
  margin-top: 12px;
}

.data-table th {
  text-align: left;
  padding: 10px 12px;
  background: #f8f9fc;
  color: #666;
  font-weight: 500;
  border-bottom: 1px solid #e8ecf4;
}

.data-table td {
  padding: 10px 12px;
  border-bottom: 1px solid #f0f2f5;
  color: #333;
}

.data-table tr.expired td {
  opacity: 0.6;
}

.data-table code {
  background: #f0f2f5;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 12px;
}

/* Badge */
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 500;
}

.badge-success {
  background: #dcfce7;
  color: #15803d;
}

.badge-danger {
  background: #fee2e2;
  color: #dc2626;
  margin-left: 4px;
}

.badge-muted {
  background: #f0f0f0;
  color: #888;
}

.text-danger {
  color: #dc2626;
}

/* Key 创建成功提示 */
.key-created-alert {
  background: #f0fdf4;
  border: 1px solid #86efac;
  border-radius: 10px;
  padding: 16px;
  margin-bottom: 16px;
}

.alert-header {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 500;
  color: #15803d;
  margin-bottom: 10px;
}

.alert-icon {
  width: 16px;
  height: 16px;
  flex-shrink: 0;
}

.key-display {
  display: flex;
  align-items: center;
  gap: 8px;
  background: #fff;
  padding: 10px 14px;
  border-radius: 8px;
  border: 1px solid #d1fae5;
  margin-bottom: 8px;
}

.key-display code {
  flex: 1;
  font-size: 12px;
  word-break: break-all;
  color: #333;
}

/* 表单 */
.inline-form {
  background: #f8f9fc;
  border-radius: 10px;
  padding: 16px;
  margin-top: 12px;
  border: 1px solid #e8ecf4;
}

.form-row {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}

.form-field {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-bottom: 12px;
}

.form-field label {
  font-size: 12px;
  color: #666;
  font-weight: 500;
}

.form-field input {
  padding: 8px 12px;
  border: 1px solid #d1d5db;
  border-radius: 8px;
  font-size: 13px;
  outline: none;
  transition: border-color 0.2s;
}

.form-field input:focus {
  border-color: #5470c6;
  box-shadow: 0 0 0 2px rgba(84,112,198,0.15);
}

.form-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.pwd-form {
  max-width: 400px;
}

.pwd-msg {
  font-size: 13px;
  padding: 6px 10px;
  border-radius: 6px;
  margin-bottom: 8px;
}

.pwd-msg.error {
  color: #dc2626;
  background: #fee2e2;
}

.pwd-msg.success {
  color: #15803d;
  background: #dcfce7;
}

/* Buttons */
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

.btn-primary:hover:not(:disabled) {
  opacity: 0.9;
  transform: translateY(-1px);
}

.btn-primary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.btn-outline {
  background: transparent;
  color: #5470c6;
  border: 1px solid #5470c6;
  border-radius: 8px;
  padding: 8px 16px;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
}

.btn-outline:hover {
  background: #5470c6;
  color: #fff;
}

.btn-danger {
  background: #fee2e2;
  color: #dc2626;
  border: none;
  border-radius: 6px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
}

.btn-danger:hover {
  background: #dc2626;
  color: #fff;
}

.btn-text {
  background: transparent;
  border: none;
  color: #888;
  font-size: 13px;
  cursor: pointer;
  padding: 4px 8px;
}

.btn-text:hover {
  color: #555;
}

.btn-copy {
  background: #5470c6;
  color: #fff;
  border: none;
  border-radius: 6px;
  padding: 4px 12px;
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
}

.btn-sm {
  padding: 6px 14px;
  font-size: 12px;
}

.btn-xs {
  padding: 3px 8px;
  font-size: 11px;
}

.empty-state {
  text-align: center;
  padding: 32px;
  color: #aaa;
  font-size: 14px;
}

@media (max-width: 640px) {
  .quota-grid {
    grid-template-columns: 1fr;
  }
  .form-row {
    flex-direction: column;
  }
}
</style>
