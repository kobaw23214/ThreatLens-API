<script setup>
import { ref } from 'vue'
import { authFetch } from '../utils/auth.js'

const url = ref('')
const method = ref('POST')
const headers = ref('{}')
const timeout = ref(10)
const testing = ref(false)
const result = ref(null)
const error = ref('')

async function testCallback() {
  error.value = ''
  result.value = null
  
  // 验证 URL
  if (!url.value.trim()) {
    error.value = '请输入回调 URL'
    return
  }
  
  // 验证 headers JSON
  let parsedHeaders = null
  if (headers.value.trim()) {
    try {
      parsedHeaders = JSON.parse(headers.value)
    } catch (e) {
      error.value = 'Headers JSON 格式错误: ' + e.message
      return
    }
  }
  
  testing.value = true
  
  try {
    const res = await authFetch('/callback/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: url.value.trim(),
        method: method.value,
        headers: parsedHeaders,
        timeout: timeout.value,
      }),
    })
    
    if (!res.ok) {
      const data = await res.json()
      throw new Error(data.detail || '测试失败')
    }
    
    result.value = await res.json()
  } catch (e) {
    error.value = e.message
  } finally {
    testing.value = false
  }
}

function formatHeaders() {
  try {
    const parsed = JSON.parse(headers.value)
    headers.value = JSON.stringify(parsed, null, 2)
    error.value = ''
  } catch (e) {
    error.value = 'JSON 格式错误: ' + e.message
  }
}

function clearForm() {
  url.value = ''
  method.value = 'POST'
  headers.value = '{}'
  timeout.value = 10
  result.value = null
  error.value = ''
}
</script>

<template>
  <div class="callback-tester">
    <div class="tester-header">
      <h2>回调测试工具</h2>
      <p class="subtitle">测试回调 URL 的连通性和响应时间</p>
    </div>

    <div class="tester-form">
      <div class="form-section">
        <h3>测试配置</h3>
        
        <div class="form-field">
          <label>回调 URL *</label>
          <input
            v-model="url"
            type="url"
            placeholder="https://api.example.com/callback"
            @keyup.enter="testCallback"
          />
        </div>

        <div class="form-row">
          <div class="form-field">
            <label>请求方式</label>
            <select v-model="method">
              <option value="POST">POST</option>
              <option value="PUT">PUT</option>
              <option value="GET">GET</option>
            </select>
          </div>

          <div class="form-field">
            <label>超时时间（秒）</label>
            <input
              v-model.number="timeout"
              type="number"
              min="1"
              max="60"
            />
          </div>
        </div>

        <div class="form-field">
          <div class="label-with-action">
            <label>自定义请求头（可选，JSON 格式）</label>
            <button class="format-btn" @click="formatHeaders" type="button">
              格式化
            </button>
          </div>
          <textarea
            v-model="headers"
            rows="6"
            placeholder='{"Authorization": "Bearer xxx", "X-Custom": "value"}'
            class="json-editor"
          />
        </div>

        <div v-if="error" class="error-message">{{ error }}</div>

        <div class="form-actions">
          <button
            class="test-btn"
            @click="testCallback"
            :disabled="testing || !url.trim()"
          >
            <span v-if="testing" class="spinner"></span>
            {{ testing ? '测试中...' : '🚀 开始测试' }}
          </button>
          <button class="clear-btn" @click="clearForm" :disabled="testing">
            清空
          </button>
        </div>
      </div>

      <!-- 测试结果 -->
      <div v-if="result" class="result-section">
        <h3>测试结果</h3>
        
        <div class="result-card" :class="{ success: result.success, failed: !result.success }">
          <div class="result-header">
            <div class="result-icon">{{ result.success ? '✅' : '❌' }}</div>
            <div class="result-title">
              {{ result.success ? '测试成功' : '测试失败' }}
            </div>
          </div>

          <div class="result-details">
            <div v-if="result.status_code" class="result-item">
              <span class="result-label">HTTP 状态码:</span>
              <span class="result-value">{{ result.status_code }}</span>
            </div>

            <div v-if="result.response_time_ms" class="result-item">
              <span class="result-label">响应时间:</span>
              <span class="result-value">{{ result.response_time_ms }} ms</span>
            </div>

            <div v-if="result.error_message" class="result-item">
              <span class="result-label">错误信息:</span>
              <span class="result-value error">{{ result.error_message }}</span>
            </div>

            <div v-if="result.response_body" class="result-item full">
              <span class="result-label">响应体:</span>
              <pre class="result-body">{{ result.response_body }}</pre>
            </div>
          </div>
        </div>

        <!-- 性能评估 -->
        <div v-if="result.success && result.response_time_ms" class="performance-card">
          <h4>性能评估</h4>
          <div class="performance-bar">
            <div
              class="performance-fill"
              :class="{
                excellent: result.response_time_ms < 100,
                good: result.response_time_ms >= 100 && result.response_time_ms < 500,
                fair: result.response_time_ms >= 500 && result.response_time_ms < 1000,
                poor: result.response_time_ms >= 1000
              }"
              :style="{ width: Math.min(result.response_time_ms / 10, 100) + '%' }"
            ></div>
          </div>
          <div class="performance-label">
            <span v-if="result.response_time_ms < 100" class="excellent">优秀 (&lt; 100ms)</span>
            <span v-else-if="result.response_time_ms < 500" class="good">良好 (100-500ms)</span>
            <span v-else-if="result.response_time_ms < 1000" class="fair">一般 (500-1000ms)</span>
            <span v-else class="poor">较慢 (&gt; 1000ms)</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 使用说明 -->
    <div class="help-section">
      <h3>使用说明</h3>
      <ul>
        <li>输入要测试的回调 URL，点击"开始测试"按钮</li>
        <li>工具会发送一个测试请求到指定的 URL</li>
        <li>测试请求体包含 <code>{"test": true, "message": "这是一个回调测试请求", "timestamp": ...}</code></li>
        <li>可以自定义请求头，例如添加 Authorization 等认证信息</li>
        <li>测试结果会显示 HTTP 状态码、响应时间和响应体</li>
        <li>响应时间 &lt; 100ms 为优秀，100-500ms 为良好，500-1000ms 为一般，&gt; 1000ms 为较慢</li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.callback-tester {
  max-width: 1000px;
  margin: 0 auto;
}

.tester-header {
  margin-bottom: 24px;
}

.tester-header h2 {
  font-size: 24px;
  font-weight: 600;
  color: #333;
  margin-bottom: 8px;
}

.subtitle {
  font-size: 14px;
  color: #888;
}

.tester-form {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
  margin-bottom: 24px;
}

.form-section,
.result-section {
  background: #fff;
  border-radius: 8px;
  padding: 24px;
  box-shadow: 0 1px 3px rgba(0,0,0,.1);
}

.form-section h3,
.result-section h3 {
  font-size: 18px;
  font-weight: 600;
  color: #333;
  margin-bottom: 20px;
}

.form-field {
  margin-bottom: 16px;
}

.form-field label {
  display: block;
  font-size: 14px;
  font-weight: 500;
  color: #555;
  margin-bottom: 6px;
}

.form-field input,
.form-field select,
.form-field textarea {
  width: 100%;
  border: 1px solid #ddd;
  border-radius: 6px;
  padding: 8px 12px;
  font-size: 14px;
  font-family: inherit;
}

.form-field input:focus,
.form-field select:focus,
.form-field textarea:focus {
  outline: none;
  border-color: #5470c6;
}

.json-editor {
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 13px;
  resize: vertical;
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.label-with-action {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}

.format-btn {
  background: #5470c6;
  color: #fff;
  border: none;
  border-radius: 4px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s;
}

.format-btn:hover {
  background: #4a63b3;
}

.error-message {
  background: #fee2e2;
  color: #e74c3c;
  padding: 10px 12px;
  border-radius: 6px;
  font-size: 13px;
  margin-bottom: 16px;
}

.form-actions {
  display: flex;
  gap: 12px;
}

.test-btn,
.clear-btn {
  flex: 1;
  padding: 10px 20px;
  border: none;
  border-radius: 6px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}

.test-btn {
  background: #3ba272;
  color: #fff;
}

.test-btn:hover:not(:disabled) {
  background: #2e8060;
  transform: translateY(-1px);
}

.test-btn:disabled {
  background: #bbb;
  cursor: not-allowed;
}

.clear-btn {
  background: #e0e0e0;
  color: #666;
}

.clear-btn:hover:not(:disabled) {
  background: #d0d0d0;
}

.spinner {
  width: 16px;
  height: 16px;
  border: 2px solid rgba(255,255,255,.3);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin .7s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* 测试结果 */
.result-card {
  border-radius: 8px;
  padding: 20px;
  margin-bottom: 16px;
}

.result-card.success {
  background: #d1fae5;
  border: 2px solid #3ba272;
}

.result-card.failed {
  background: #fee2e2;
  border: 2px solid #e74c3c;
}

.result-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.result-icon {
  font-size: 32px;
}

.result-title {
  font-size: 18px;
  font-weight: 600;
  color: #333;
}

.result-details {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.result-item {
  display: flex;
  gap: 8px;
}

.result-item.full {
  flex-direction: column;
}

.result-label {
  font-size: 14px;
  font-weight: 500;
  color: #555;
  min-width: 100px;
}

.result-value {
  font-size: 14px;
  color: #333;
}

.result-value.error {
  color: #e74c3c;
  font-weight: 500;
}

.result-body {
  background: #f8f9fa;
  border: 1px solid #e0e0e0;
  border-radius: 6px;
  padding: 12px;
  font-size: 13px;
  font-family: 'Consolas', 'Monaco', monospace;
  overflow-x: auto;
  max-height: 200px;
  overflow-y: auto;
  margin-top: 6px;
}

/* 性能评估 */
.performance-card {
  background: #f8f9fa;
  border-radius: 8px;
  padding: 16px;
}

.performance-card h4 {
  font-size: 14px;
  font-weight: 600;
  color: #555;
  margin-bottom: 12px;
}

.performance-bar {
  height: 24px;
  background: #e0e0e0;
  border-radius: 12px;
  overflow: hidden;
  margin-bottom: 8px;
}

.performance-fill {
  height: 100%;
  transition: width 0.3s;
}

.performance-fill.excellent {
  background: linear-gradient(90deg, #3ba272 0%, #2e8060 100%);
}

.performance-fill.good {
  background: linear-gradient(90deg, #5470c6 0%, #4a63b3 100%);
}

.performance-fill.fair {
  background: linear-gradient(90deg, #f59e0b 0%, #d97706 100%);
}

.performance-fill.poor {
  background: linear-gradient(90deg, #e74c3c 0%, #c0392b 100%);
}

.performance-label {
  font-size: 14px;
  font-weight: 500;
  text-align: center;
}

.performance-label .excellent {
  color: #3ba272;
}

.performance-label .good {
  color: #5470c6;
}

.performance-label .fair {
  color: #f59e0b;
}

.performance-label .poor {
  color: #e74c3c;
}

/* 使用说明 */
.help-section {
  background: #fff;
  border-radius: 8px;
  padding: 24px;
  box-shadow: 0 1px 3px rgba(0,0,0,.1);
}

.help-section h3 {
  font-size: 18px;
  font-weight: 600;
  color: #333;
  margin-bottom: 16px;
}

.help-section ul {
  list-style: none;
  padding: 0;
}

.help-section li {
  font-size: 14px;
  color: #555;
  line-height: 1.8;
  padding-left: 20px;
  position: relative;
  margin-bottom: 8px;
}

.help-section li:before {
  content: '•';
  position: absolute;
  left: 0;
  color: #5470c6;
  font-weight: bold;
}

.help-section code {
  background: #f8f9fa;
  padding: 2px 6px;
  border-radius: 3px;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 13px;
  color: #e74c3c;
}

@media (max-width: 900px) {
  .tester-form {
    grid-template-columns: 1fr;
  }
  
  .form-row {
    grid-template-columns: 1fr;
  }
}
</style>
