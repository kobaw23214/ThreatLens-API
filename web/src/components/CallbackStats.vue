<script setup>
import { ref, onMounted, computed, watch } from 'vue'
import { authFetch } from '../utils/auth.js'

const props = defineProps({
  userId: { type: [String, Number], default: '' }
})

const stats = ref(null)
const loading = ref(false)
const error = ref('')

const successRate = computed(() => {
  if (!stats.value) return '0%'
  return (stats.value.success_rate * 100).toFixed(2) + '%'
})

const avgResponseTime = computed(() => {
  if (!stats.value || !stats.value.avg_response_time_ms) return 'N/A'
  return stats.value.avg_response_time_ms.toFixed(2) + ' ms'
})

async function loadStats() {
  loading.value = true
  error.value = ''
  try {
    const url = props.userId ? `/callback/stats?user_id=${props.userId}` : '/callback/stats'
    const res = await authFetch(url)
    if (!res.ok) throw new Error('加载统计失败')
    stats.value = await res.json()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

watch(() => props.userId, () => {
  loadStats()
})

onMounted(() => {
  loadStats()
  // 每 30 秒刷新一次
  setInterval(loadStats, 30000)
})
</script>

<template>
  <div class="callback-stats">
    <div class="stats-header">
      <h2>回调统计</h2>
      <button class="refresh-btn" @click="loadStats" :disabled="loading">
        {{ loading ? '刷新中...' : '刷新' }}
      </button>
    </div>

    <div v-if="error" class="error-message">{{ error }}</div>

    <div v-if="stats" class="stats-content">
      <!-- 概览卡片 -->
      <div class="stats-cards">
        <div class="stat-card">
          <div class="stat-icon">
            <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><rect x="3" y="14" width="4" height="7" rx="1" fill="currentColor"/><rect x="10" y="9" width="4" height="12" rx="1" fill="currentColor" opacity=".7"/><rect x="17" y="4" width="4" height="17" rx="1" fill="currentColor" opacity=".45"/></svg>
          </div>
          <div class="stat-info">
            <div class="stat-label">总回调数</div>
            <div class="stat-value">{{ stats.total_callbacks }}</div>
          </div>
        </div>

        <div class="stat-card success">
          <div class="stat-icon">
            <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="2"/><path d="M8 12l3 3 5-5" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
          </div>
          <div class="stat-info">
            <div class="stat-label">成功回调</div>
            <div class="stat-value">{{ stats.success_callbacks }}</div>
          </div>
        </div>

        <div class="stat-card failed">
          <div class="stat-icon">
            <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="2"/><line x1="8.5" y1="8.5" x2="15.5" y2="15.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="15.5" y1="8.5" x2="8.5" y2="15.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>
          </div>
          <div class="stat-info">
            <div class="stat-label">失败回调</div>
            <div class="stat-value">{{ stats.failed_callbacks }}</div>
          </div>
        </div>

        <div class="stat-card pending">
          <div class="stat-icon">
            <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="2"/><path d="M12 7v5l3 3" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/></svg>
          </div>
          <div class="stat-info">
            <div class="stat-label">待重试</div>
            <div class="stat-value">{{ stats.pending_retries }}</div>
          </div>
        </div>
      </div>

      <!-- 性能指标 -->
      <div class="performance-section">
        <h3>性能指标</h3>
        <div class="performance-cards">
          <div class="perf-card">
            <div class="perf-label">成功率</div>
            <div class="perf-value" :class="{ good: stats.success_rate >= 0.95, warning: stats.success_rate < 0.95 && stats.success_rate >= 0.8, bad: stats.success_rate < 0.8 }">
              {{ successRate }}
            </div>
            <div class="perf-bar">
              <div class="perf-bar-fill" :style="{ width: successRate }"></div>
            </div>
          </div>

          <div class="perf-card">
            <div class="perf-label">平均响应时间</div>
            <div class="perf-value">{{ avgResponseTime }}</div>
          </div>
        </div>
      </div>

      <!-- 失败原因分布 -->
      <div v-if="Object.keys(stats.failed_by_reason).length > 0" class="failure-section">
        <h3>失败原因分布</h3>
        <div class="failure-list">
          <div
            v-for="(count, reason) in stats.failed_by_reason"
            :key="reason"
            class="failure-item"
          >
            <div class="failure-reason">{{ reason }}</div>
            <div class="failure-count">{{ count }} 次</div>
            <div class="failure-bar">
              <div
                class="failure-bar-fill"
                :style="{ width: (count / stats.failed_callbacks * 100) + '%' }"
              ></div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div v-else-if="loading" class="loading-state">
      <div class="spinner"></div>
      <div>加载中...</div>
    </div>
  </div>
</template>

<style scoped>
.callback-stats {
  max-width: 100%;
  margin: 0;
}

.stats-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;
}

.stats-header h2 {
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
  transform: translateY(-1px);
}

.refresh-btn:disabled {
  background: #bbb;
  cursor: not-allowed;
}

.error-message {
  background: #fee2e2;
  color: #e74c3c;
  padding: 12px 16px;
  border-radius: 6px;
  margin-bottom: 16px;
}

.stats-content {
  display: flex;
  flex-direction: column;
  gap: 24px;
}

/* 概览卡片 */
.stats-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

.stat-card {
  background: #fff;
  border-radius: 8px;
  padding: 20px;
  box-shadow: 0 1px 3px rgba(0,0,0,.1);
  display: flex;
  align-items: center;
  gap: 16px;
  border-left: 4px solid #5470c6;
}

.stat-card.success {
  border-left-color: #3ba272;
}

.stat-card.failed {
  border-left-color: #e74c3c;
}

.stat-card.pending {
  border-left-color: #f59e0b;
}

.stat-icon {
  width: 40px;
  height: 40px;
  flex-shrink: 0;
  color: #5470c6;
}

.stat-card.success .stat-icon { color: #3ba272; }
.stat-card.failed .stat-icon { color: #e74c3c; }
.stat-card.pending .stat-icon { color: #f59e0b; }

.stat-icon svg {
  width: 100%;
  height: 100%;
}

.stat-info {
  flex: 1;
}

.stat-label {
  font-size: 13px;
  color: #888;
  margin-bottom: 4px;
}

.stat-value {
  font-size: 28px;
  font-weight: 700;
  color: #333;
}

/* 性能指标 */
.performance-section,
.failure-section {
  background: #fff;
  border-radius: 8px;
  padding: 24px;
  box-shadow: 0 1px 3px rgba(0,0,0,.1);
}

.performance-section h3,
.failure-section h3 {
  font-size: 18px;
  font-weight: 600;
  color: #333;
  margin-bottom: 16px;
}

.performance-cards {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}

.perf-card {
  padding: 16px;
  background: #f8f9fa;
  border-radius: 6px;
}

.perf-label {
  font-size: 13px;
  color: #888;
  margin-bottom: 8px;
}

.perf-value {
  font-size: 32px;
  font-weight: 700;
  color: #333;
  margin-bottom: 8px;
}

.perf-value.good {
  color: #3ba272;
}

.perf-value.warning {
  color: #f59e0b;
}

.perf-value.bad {
  color: #e74c3c;
}

.perf-bar {
  height: 8px;
  background: #e0e0e0;
  border-radius: 4px;
  overflow: hidden;
}

.perf-bar-fill {
  height: 100%;
  background: linear-gradient(90deg, #3ba272 0%, #5470c6 100%);
  transition: width 0.3s;
}

/* 失败原因分布 */
.failure-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.failure-item {
  display: grid;
  grid-template-columns: 150px 80px 1fr;
  align-items: center;
  gap: 12px;
}

.failure-reason {
  font-size: 14px;
  color: #555;
  font-weight: 500;
}

.failure-count {
  font-size: 14px;
  color: #888;
  text-align: right;
}

.failure-bar {
  height: 24px;
  background: #f0f0f0;
  border-radius: 4px;
  overflow: hidden;
}

.failure-bar-fill {
  height: 100%;
  background: #e74c3c;
  transition: width 0.3s;
}

/* 加载状态 */
.loading-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 60px 20px;
  color: #888;
}

.spinner {
  width: 40px;
  height: 40px;
  border: 4px solid rgba(0,0,0,.1);
  border-top-color: #5470c6;
  border-radius: 50%;
  animation: spin .8s linear infinite;
  margin-bottom: 16px;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 900px) {
  .stats-cards {
    grid-template-columns: repeat(2, 1fr);
  }
  
  .performance-cards {
    grid-template-columns: 1fr;
  }
  
  .failure-item {
    grid-template-columns: 1fr;
    gap: 4px;
  }
  
  .failure-count {
    text-align: left;
  }
}
</style>
