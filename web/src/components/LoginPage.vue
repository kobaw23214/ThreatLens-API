<script setup>
import { ref } from 'vue'
import { setToken, setUser } from '../utils/auth.js'

const emit = defineEmits(['login-success'])

const username = ref('')
const password = ref('')
const loading = ref(false)
const errorMsg = ref('')

async function handleLogin() {
  if (!username.value.trim() || !password.value) {
    errorMsg.value = '请输入用户名和密码'
    return
  }

  loading.value = true
  errorMsg.value = ''

  try {
    const res = await fetch('/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        username: username.value.trim(),
        password: password.value,
      }),
    })

    const data = await res.json()

    if (!res.ok) {
      errorMsg.value = data.detail || '登录失败'
      return
    }

    // 保存 token 和用户信息
    setToken(data.access_token)
    setUser(data.user)

    emit('login-success', data.user)
  } catch (err) {
    errorMsg.value = '网络错误，请检查后端服务'
  } finally {
    loading.value = false
  }
}

function handleKeydown(e) {
  if (e.key === 'Enter') handleLogin()
}
</script>

<template>
  <div class="login-wrapper">
    <!-- 背景装饰 -->
    <div class="bg-decoration">
      <div class="bg-circle bg-circle-1"></div>
      <div class="bg-circle bg-circle-2"></div>
      <div class="bg-circle bg-circle-3"></div>
    </div>

    <div class="login-card">
      <!-- Logo -->
      <div class="login-logo">
        <svg class="logo-icon" viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <linearGradient id="login-bg" x1="0%" y1="0%" x2="100%" y2="100%">
              <stop offset="0%" style="stop-color:#5470c6"/>
              <stop offset="100%" style="stop-color:#3ba272"/>
            </linearGradient>
          </defs>
          <rect width="48" height="48" rx="10" fill="url(#login-bg)"/>
          <text x="24" y="33" text-anchor="middle" font-family="Arial,sans-serif" font-weight="700" font-size="24" fill="#fff">N</text>
          <circle cx="38" cy="10" r="6" fill="#f59e0b"/>
          <path d="M35.5 10 L37 11.5 L40.5 8" stroke="#fff" stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        <h2>ThreatLens</h2>
        <p class="login-subtitle">威胁情报分析平台</p>
      </div>

      <!-- 表单 -->
      <div class="login-form">
        <div class="form-group">
          <label for="login-username">
            <svg viewBox="0 0 20 20" fill="currentColor" class="field-icon">
              <circle cx="10" cy="7" r="4" fill="none" stroke="currentColor" stroke-width="1.5"/>
              <path d="M3 18c0-4 3.5-7 7-7s7 3 7 7" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            </svg>
            用户名
          </label>
          <input
            id="login-username"
            v-model="username"
            type="text"
            placeholder="请输入用户名"
            autocomplete="username"
            @keydown="handleKeydown"
          />
        </div>

        <div class="form-group">
          <label for="login-password">
            <svg viewBox="0 0 20 20" fill="currentColor" class="field-icon">
              <rect x="4" y="9" width="12" height="9" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/>
              <path d="M7 9V6a3 3 0 016 0v3" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
              <circle cx="10" cy="13.5" r="1.2" fill="currentColor"/>
            </svg>
            密码
          </label>
          <input
            id="login-password"
            v-model="password"
            type="password"
            placeholder="请输入密码"
            autocomplete="current-password"
            @keydown="handleKeydown"
          />
        </div>

        <div v-if="errorMsg" class="error-msg">
          <svg viewBox="0 0 16 16" class="error-icon">
            <circle cx="8" cy="8" r="7" fill="none" stroke="currentColor" stroke-width="1.2"/>
            <line x1="8" y1="5" x2="8" y2="9" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            <circle cx="8" cy="11.5" r="0.8" fill="currentColor"/>
          </svg>
          {{ errorMsg }}
        </div>

        <button
          class="login-btn"
          :disabled="loading"
          @click="handleLogin"
        >
          <span v-if="loading" class="spinner"></span>
          <span v-else>登 录</span>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-wrapper {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #0f0c29 0%, #1a1a2e 40%, #16213e 100%);
  position: relative;
  overflow: hidden;
}

/* 背景装饰 */
.bg-decoration {
  position: absolute;
  inset: 0;
  pointer-events: none;
}

.bg-circle {
  position: absolute;
  border-radius: 50%;
  filter: blur(80px);
  opacity: 0.15;
}

.bg-circle-1 {
  width: 400px;
  height: 400px;
  background: #5470c6;
  top: -100px;
  right: -100px;
  animation: float 8s ease-in-out infinite;
}

.bg-circle-2 {
  width: 300px;
  height: 300px;
  background: #3ba272;
  bottom: -50px;
  left: -80px;
  animation: float 10s ease-in-out infinite reverse;
}

.bg-circle-3 {
  width: 200px;
  height: 200px;
  background: #f59e0b;
  top: 40%;
  left: 60%;
  animation: float 12s ease-in-out infinite;
}

@keyframes float {
  0%, 100% { transform: translate(0, 0); }
  50% { transform: translate(20px, -30px); }
}

/* 登录卡片 */
.login-card {
  width: 400px;
  max-width: 90vw;
  background: rgba(255, 255, 255, 0.06);
  backdrop-filter: blur(24px);
  -webkit-backdrop-filter: blur(24px);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 20px;
  padding: 48px 40px 40px;
  box-shadow: 0 24px 48px rgba(0, 0, 0, 0.3);
  position: relative;
  z-index: 1;
  animation: slideUp 0.5s ease-out;
}

@keyframes slideUp {
  from {
    opacity: 0;
    transform: translateY(20px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

/* Logo 区域 */
.login-logo {
  text-align: center;
  margin-bottom: 36px;
}

.logo-icon {
  width: 56px;
  height: 56px;
  margin-bottom: 12px;
}

.login-logo h2 {
  color: #fff;
  font-size: 22px;
  font-weight: 700;
  letter-spacing: 1px;
  margin-bottom: 4px;
}

.login-subtitle {
  color: rgba(255, 255, 255, 0.5);
  font-size: 13px;
}

/* 表单 */
.login-form {
  display: flex;
  flex-direction: column;
  gap: 20px;
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.form-group label {
  color: rgba(255, 255, 255, 0.7);
  font-size: 13px;
  font-weight: 500;
  display: flex;
  align-items: center;
  gap: 6px;
}

.field-icon {
  width: 14px;
  height: 14px;
  color: rgba(255, 255, 255, 0.5);
}

.form-group input {
  background: rgba(255, 255, 255, 0.08);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 10px;
  padding: 12px 16px;
  color: #fff;
  font-size: 14px;
  outline: none;
  transition: all 0.2s;
}

.form-group input::placeholder {
  color: rgba(255, 255, 255, 0.3);
}

.form-group input:focus {
  border-color: #5470c6;
  background: rgba(255, 255, 255, 0.12);
  box-shadow: 0 0 0 3px rgba(84, 112, 198, 0.2);
}

/* 错误提示 */
.error-msg {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #ef4444;
  font-size: 13px;
  padding: 8px 12px;
  background: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.2);
  border-radius: 8px;
}

.error-icon {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
}

/* 登录按钮 */
.login-btn {
  background: linear-gradient(135deg, #5470c6, #3ba272);
  border: none;
  border-radius: 10px;
  padding: 13px;
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  margin-top: 4px;
}

.login-btn:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 8px 24px rgba(84, 112, 198, 0.3);
}

.login-btn:active:not(:disabled) {
  transform: translateY(0);
}

.login-btn:disabled {
  opacity: 0.7;
  cursor: not-allowed;
}

.spinner {
  width: 18px;
  height: 18px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

@media (max-width: 480px) {
  .login-card {
    padding: 36px 24px 32px;
    border-radius: 16px;
  }
}
</style>
