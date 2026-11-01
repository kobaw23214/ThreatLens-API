<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { authFetch } from '../utils/auth.js'
import { highlightCurl, highlightJson } from '../utils/highlight.js'
import VueJsonPretty from 'vue-json-pretty'
import 'vue-json-pretty/lib/styles.css'

const emit = defineEmits(['open-graph'])

// consoleMode: 'ner'(默认,NER 调用台) | 'attack'(ATT&CK 调用台)
// attack 模式下:模型下拉只显示 type==='attack' 的模型(TRAM / attack-llm),
// labels 默认 ['full'](全量);ner 模式显示其余模型。
// 两个调用台的上传入口都接受 .eml / .msg:选中后自动改走 POST /extract/eml
// (.eml 后端用 mail-parser、.msg 用 extract-msg 解析邮件为 JSON,再做实体/IOC 提取)。
const props = defineProps({
  consoleMode: { type: String, default: 'ner' },
})
const isAttack = computed(() => props.consoleMode === 'attack')

// ─── 请求参数 ───────────────────────────────────────────────────────────────
const apiBase         = ref(location.origin)
const selectedModel   = ref('')
const availableModels = ref([])
const modelsInfo      = ref([])  // [{name, type}] 用于区分 NER / LLM / ATT&CK 模型
const textInput       = ref('')
const labelsJson      = ref(
  props.consoleMode === 'attack'
    ? ['full']
    : ['IP地址', 'IPv6', 'URL', '邮箱', 'MD5', 'SHA1', 'SHA256', 'SHA512', '哈希']
)
const labelMappingJson = ref(
  props.consoleMode === 'attack'
    ? {}
    : { 'IP地址': 'IP', 'IPv6': 'IPV6', 'URL': 'URL', '邮箱': 'EMAIL', 'MD5': 'MD5', 'SHA1': 'SHA1', 'SHA256': 'SHA256', 'SHA512': 'SHA512', '哈希': 'HASH' }
)
const asyncMode       = ref(true)
const reuseCache      = ref(false)  // 结果复用开关
const informationJson = ref({ "file_id": "0537ef0d8ac6a2179a58067615825924" })  // 用户自定义信息，默认值
const labelMappingEnabled = ref(false)  // label_mapping 开关，默认关闭

// 分块参数 (long-text-chunking-ner)
const chunkEnabled      = ref(null)   // null = 使用后端 env 默认值
const chunkOverlap      = ref(null)   // null / 0-512 字符
const chunkSafetyMargin = ref(null)   // null / 0-64 tokens
const chunkThreshold    = ref(null)   // null / 0.0-1.0
const chunkAdvancedEnabled = ref(false)  // 是否展开"分块参数"高级配置,默认折叠

// ─── 文本文件上传 (extract/upload) ─────────────────────────────────────
// 传了文件时,后端用文件内容替代 text 字段处理,textInput 被忽略。
const inputMode = ref('text')             // 'text' | 'file',默认文本输入模式
const uploadFile = ref(null)              // File 对象 | null
const uploadFileInput = ref(null)         // <input type=file> ref,用于重置
const ALLOWED_TEXT_EXTENSIONS = [
  '.txt', '.md', '.markdown', '.rst',
  '.log', '.csv', '.tsv',
  '.json', '.jsonl', '.ndjson',
  '.xml', '.yaml', '.yml', '.ini', '.cfg', '.toml',
  '.html', '.htm',
  '.srt', '.vtt',
]
// PDF 由后端提取文字层处理,与纯文本文件一起视为合法上传类型
const ALLOWED_PDF_EXTENSIONS = ['.pdf']
// 邮件(.eml / .msg)走专用端点 /extract/eml:后端先解析成 JSON 再做 IOC 提取
const ALLOWED_EML_EXTENSIONS = ['.eml', '.msg']
const ALLOWED_UPLOAD_EXTENSIONS = [...ALLOWED_TEXT_EXTENSIONS, ...ALLOWED_PDF_EXTENSIONS, ...ALLOWED_EML_EXTENSIONS]
const UPLOAD_ACCEPT = ALLOWED_UPLOAD_EXTENSIONS.join(',')
const UPLOAD_MAX_BYTES = 10 * 1024 * 1024  // 10 MB,与后端一致

const uploadFileInfo = computed(() => {
  const f = uploadFile.value
  if (!f) return null
  const ext = (f.name.match(/\.[^.]+$/) || ['(无扩展名)'])[0].toLowerCase()
  const kb = (f.size / 1024).toFixed(1)
  const tooLarge = f.size > UPLOAD_MAX_BYTES
  const badExt = !ALLOWED_UPLOAD_EXTENSIONS.includes(ext)
  const isEml = ALLOWED_EML_EXTENSIONS.includes(ext)
  return { name: f.name, ext, size: f.size, kb, tooLarge, badExt, isEml }
})

const uploadModeActive = computed(
  () => inputMode.value === 'file'
    && uploadFile.value != null
    && !uploadFileInfo.value?.badExt
    && !uploadFileInfo.value?.tooLarge,
)

function onFilePicked(evt) {
  const file = evt.target.files?.[0] ?? null
  uploadFile.value = file
}

function clearUploadFile() {
  uploadFile.value = null
  if (uploadFileInput.value) {
    uploadFileInput.value.value = ''
  }
}

function switchInputMode(mode) {
  inputMode.value = mode
  if (mode === 'text') {
    // 切回文本模式时保留已选文件(用户可能想切回来,不强制清空)
    // 但若有文件且非法,顺便清掉以免残留错误提示
    if (uploadFile.value && (uploadFileInfo.value?.badExt || uploadFileInfo.value?.tooLarge)) {
      clearUploadFile()
    }
  }
}

// ─── 文本长度限制(与后端 MAX_TEXT_LENGTH 对齐) ─────────────────────────
// 前端从 /server-info 拿不到 MAX_TEXT_LENGTH,所以硬编码默认值 50000。
// 如果后端改了 .env,前端这里也要同步改(或者后端暴露一个 /config 接口)。
const MAX_TEXT_LENGTH = 50000
const textLenWarning = computed(() => {
  const n = textInput.value.length
  if (n > MAX_TEXT_LENGTH) return 'over'
  if (n > MAX_TEXT_LENGTH * 0.85) return 'warn'
  return 'ok'
})
const UPLOAD_TOOLTIP_HTML =
  '<strong>上传文件</strong>(用于处理大文档)<br>' +
  '• 支持纯文本格式: .txt / .md / .csv / .json / .log / .xml / .yaml 等<br>' +
  '• 支持 PDF: 自动提取文字层(扫描件/纯图片 PDF 暂不支持)<br>' +
  '• 支持 EML / MSG 邮件: 解析为 JSON 后提取 IOC(走 /extract/eml)<br>' +
  '• 单文件最大 10 MB<br>' +
  '• 文本文件自动识别编码: UTF-8 / UTF-16 / GB18030<br>' +
  '• 其它二进制文件(docx / zip 等)会被拒绝<br>' +
  '• 上传文件后, 下方的输入文本字段会被忽略'

// ─── 文件上传拖拽支持 ─────────────────────────────────────────────────────
const isDragging = ref(false)

function onDragOver(evt) {
  evt.preventDefault()
  isDragging.value = true
}

function onDragLeave(evt) {
  // 只有真正离开 dropzone 才取消(子元素触发 dragleave 时 relatedTarget 仍在内部)
  if (!evt.currentTarget.contains(evt.relatedTarget)) {
    isDragging.value = false
  }
}

function onDrop(evt) {
  evt.preventDefault()
  isDragging.value = false
  const file = evt.dataTransfer?.files?.[0] ?? null
  if (file) uploadFile.value = file
}

// 回调配置
const callbackEnabled = ref(false)  // 回调开关，默认关闭
const callbackUrl = ref('')  // 默认为空，启用时自动设置
const callbackMethod = ref('POST')
const callbackHeaders = ref({})
const callbackHeadersEnabled = ref(false)  // 自定义请求头开关，默认关闭
const callbackTimeout = ref(30)

// 后端实际地址（回调由后端线程直接发送，不经过 Vite 代理）
const backendBase = ref('')
const defaultCallbackUrl = computed(() => 
  backendBase.value ? `${backendBase.value}/callback/default` : ''
)

// JSON 编辑器的字符串表示
const labelsStr = ref(JSON.stringify(labelsJson.value, null, 2))
const labelMappingStr = ref(JSON.stringify(labelMappingJson.value, null, 2))
const informationStr = ref(JSON.stringify(informationJson.value, null, 2))
const callbackHeadersStr = ref(JSON.stringify(callbackHeaders.value, null, 2))

// ─── 状态 ───────────────────────────────────────────────────────────────────
const taskId       = ref('')
const step1Loading = ref(false)
const step2Loading = ref(false)
const step1Res     = ref(null)
const step2Res     = ref(null)
const step1Error   = ref('')
const step2Error   = ref('')
let pollTimer = null

// ─── JSON 验证 ───────────────────────────────────────────────────────────────
const labelsError = ref('')
const labelMappingError = ref('')
const informationError = ref('')
const callbackHeadersError = ref('')

// ─── Tooltip 状态 ───────────────────────────────────────────────────────────
const tooltipVisible = ref(false)
const tooltipContent = ref('')
const tooltipPosition = ref({ x: 0, y: 0 })
const tooltipArrowOffset = ref('50%')  // 箭头水平位置,随 tooltip 偏移动态调整

function showTooltip(event, content) {
  tooltipContent.value = content
  tooltipVisible.value = true
  updateTooltipPosition(event)
}

function hideTooltip() {
  tooltipVisible.value = false
}

function updateTooltipPosition(event) {
  const rect = event.target.getBoundingClientRect()
  const TOOLTIP_WIDTH = 320   // 与 CSS max-width 对齐
  const MARGIN = 12           // 距视口边缘最小留白

  // 理想中心 x
  let cx = rect.left + rect.width / 2
  // tooltip 左右边界
  let left = cx - TOOLTIP_WIDTH / 2
  let right = cx + TOOLTIP_WIDTH / 2

  // 右侧超出 → 向左移
  if (right > window.innerWidth - MARGIN) {
    const shift = right - (window.innerWidth - MARGIN)
    cx -= shift
    left -= shift
  }
  // 左侧超出 → 向右移
  if (left < MARGIN) {
    const shift = MARGIN - left
    cx += shift
  }

  // 箭头始终指向触发元素中心
  const triggerCx = rect.left + rect.width / 2
  const arrowX = triggerCx - (cx - TOOLTIP_WIDTH / 2)
  tooltipArrowOffset.value = Math.max(16, Math.min(TOOLTIP_WIDTH - 16, arrowX)) + 'px'

  tooltipPosition.value = {
    x: cx,
    y: rect.bottom + 10,
  }
}

// ─── 格式化函数 ───────────────────────────────────────────────────────────────
function formatLabels() {
  try {
    const parsed = JSON.parse(labelsStr.value)
    labelsStr.value = JSON.stringify(parsed, null, 2)
    labelsError.value = ''
  } catch (e) {
    labelsError.value = 'JSON 格式错误，无法格式化: ' + e.message
  }
}

function formatLabelMapping() {
  try {
    const parsed = JSON.parse(labelMappingStr.value)
    labelMappingStr.value = JSON.stringify(parsed, null, 2)
    labelMappingError.value = ''
  } catch (e) {
    labelMappingError.value = 'JSON 格式错误，无法格式化: ' + e.message
  }
}

function formatInformation() {
  try {
    const parsed = JSON.parse(informationStr.value)
    informationStr.value = JSON.stringify(parsed, null, 2)
    informationError.value = ''
  } catch (e) {
    informationError.value = 'JSON 格式错误，无法格式化: ' + e.message
  }
}

function formatCallbackHeaders() {
  try {
    const parsed = JSON.parse(callbackHeadersStr.value)
    callbackHeadersStr.value = JSON.stringify(parsed, null, 2)
    callbackHeadersError.value = ''
  } catch (e) {
    callbackHeadersError.value = 'JSON 格式错误，无法格式化: ' + e.message
  }
}

// 监听 labels 变化，自动同步 label_mapping
watch(labelsStr, (newVal) => {
  try {
    const parsed = JSON.parse(newVal)
    if (Array.isArray(parsed)) {
      labelsJson.value = parsed
      labelsError.value = ''
      
      // 只有在 label_mapping 启用时才同步
      if (labelMappingEnabled.value) {
        // 同步 label_mapping：新增的标签添加空映射，删除的标签移除映射
        const currentMapping = JSON.parse(labelMappingStr.value)
        const newMapping = {}
        
        parsed.forEach(label => {
          // 保留已有映射，新标签设为空字符串
          newMapping[label] = currentMapping[label] || ''
        })
        
        labelMappingJson.value = newMapping
        labelMappingStr.value = JSON.stringify(newMapping, null, 2)
      }
    } else {
      labelsError.value = 'labels 必须是数组格式'
    }
  } catch (e) {
    labelsError.value = 'JSON 格式错误: ' + e.message
  }
})

watch(labelMappingStr, (newVal) => {
  try {
    const parsed = JSON.parse(newVal)
    if (typeof parsed === 'object' && !Array.isArray(parsed)) {
      labelMappingJson.value = parsed
      labelMappingError.value = ''
    } else {
      labelMappingError.value = 'label_mapping 必须是对象格式'
    }
  } catch (e) {
    labelMappingError.value = 'JSON 格式错误: ' + e.message
  }
})

watch(informationStr, (newVal) => {
  try {
    const parsed = JSON.parse(newVal)
    if (typeof parsed === 'object' && !Array.isArray(parsed)) {
      informationJson.value = parsed
      informationError.value = ''
    } else {
      informationError.value = 'information_json 必须是对象格式'
    }
  } catch (e) {
    informationError.value = 'JSON 格式错误: ' + e.message
  }
})

watch(callbackHeadersStr, (newVal) => {
  try {
    const parsed = JSON.parse(newVal)
    if (typeof parsed === 'object' && !Array.isArray(parsed)) {
      callbackHeaders.value = parsed
      callbackHeadersError.value = ''
    } else {
      callbackHeadersError.value = 'headers 必须是对象格式'
    }
  } catch (e) {
    callbackHeadersError.value = 'JSON 格式错误: ' + e.message
  }
})

// ─── 工具 ───────────────────────────────────────────────────────────────────
const labels = computed(() => labelsJson.value)

const labelMapping = computed(() => {
  // ATT&CK 台输出标签是技术编号(如 T1059),不做标签映射,不发送 label_mapping
  if (isAttack.value) {
    return null
  }
  // 如果 label_mapping 未启用，返回 null
  if (!labelMappingEnabled.value) {
    return null
  }
  // 过滤掉空值，让后端使用原标签
  const filtered = {}
  Object.entries(labelMappingJson.value).forEach(([key, value]) => {
    if (value && value.trim()) {
      filtered[key] = value
    }
  })
  return Object.keys(filtered).length > 0 ? filtered : null
})

// ─── 构建 curl 字符串 ────────────────────────────────────────────────────────
const step1Body = computed(() => {
  const body = {
    text: textInput.value || '（在此填写文本）',
    labels: labels.value.length ? labels.value : ['PERSON', 'LOCATION'],
    reuse_cache: reuseCache.value,
  }
  if (selectedModel.value) body.model_name = selectedModel.value
  if (labelMapping.value)  body.label_mapping = labelMapping.value
  // 只有非空对象时才添加 information_json
  if (informationJson.value && Object.keys(informationJson.value).length > 0) {
    body.information_json = informationJson.value
  }
  // 分块参数: null 时不发送,走后端 env 默认 (long-text-chunking-ner)
  // 仅在"启用高级配置"展开时才考虑把字段放进请求体;折叠时统一用后端 env 默认值。
  if (chunkAdvancedEnabled.value) {
    if (chunkEnabled.value      !== null) body.chunk_enabled       = chunkEnabled.value
    if (chunkOverlap.value      !== null) body.chunk_overlap       = chunkOverlap.value
    if (chunkSafetyMargin.value !== null) body.chunk_safety_margin = chunkSafetyMargin.value
    if (chunkThreshold.value    !== null) body.threshold           = chunkThreshold.value
  }
  // 添加回调配置
  if (callbackEnabled.value) {
    // 如果没有填写 URL，使用默认的系统内置端点
    const url = callbackUrl.value.trim() || defaultCallbackUrl.value
    body.callback = {
      url: url,
      method: callbackMethod.value,
      timeout: callbackTimeout.value,
    }
    if (callbackHeadersEnabled.value && Object.keys(callbackHeaders.value).length > 0) {
      body.callback.headers = callbackHeaders.value
    }
  }
  return JSON.stringify(body, null, 2)
})

const curlStep1 = computed(() => {
  const base = apiBase.value
  // 文件模式 → 输出 multipart curl
  if (uploadModeActive.value) {
    const file = uploadFile.value
    const isEml = uploadFileInfo.value?.isEml
    const lines = [
      `curl -X POST "${base}${isEml ? '/extract/eml' : '/extract/upload'}" \\`,
      `  -F "file=@${file?.name || 'your-file.txt'}" \\`,
      `  -F 'labels=${JSON.stringify(labels.value).replace(/'/g, "'\\''")}' \\`,
    ]
    if (selectedModel.value) lines.push(`  -F "model_name=${selectedModel.value}" \\`)
    // /extract/eml 不支持 label_mapping 与分块参数(邮件由后端展平后整体提取)
    if (!isEml && labelMapping.value) {
      lines.push(`  -F 'label_mapping=${JSON.stringify(labelMapping.value).replace(/'/g, "'\\''")}' \\`)
    }
    if (reuseCache.value) lines.push(`  -F "reuse_cache=true" \\`)
    if (informationJson.value && Object.keys(informationJson.value).length > 0) {
      lines.push(`  -F 'information_json=${JSON.stringify(informationJson.value).replace(/'/g, "'\\''")}' \\`)
    }
    if (callbackEnabled.value) {
      const url = callbackUrl.value.trim() || defaultCallbackUrl.value
      const cb = { url, method: callbackMethod.value, timeout: callbackTimeout.value }
      if (callbackHeadersEnabled.value && Object.keys(callbackHeaders.value).length > 0) {
        cb.headers = callbackHeaders.value
      }
      lines.push(`  -F 'callback=${JSON.stringify(cb).replace(/'/g, "'\\''")}' \\`)
    }
    if (!isEml && chunkAdvancedEnabled.value) {
      if (chunkEnabled.value !== null)      lines.push(`  -F "chunk_enabled=${chunkEnabled.value}" \\`)
      if (chunkOverlap.value !== null)      lines.push(`  -F "chunk_overlap=${chunkOverlap.value}" \\`)
      if (chunkSafetyMargin.value !== null) lines.push(`  -F "chunk_safety_margin=${chunkSafetyMargin.value}" \\`)
      if (chunkThreshold.value !== null)    lines.push(`  -F "threshold=${chunkThreshold.value}" \\`)
    }
    // 去掉最后一行尾部的反斜杠
    if (lines.length > 0 && lines[lines.length - 1].endsWith(' \\')) {
      lines[lines.length - 1] = lines[lines.length - 1].slice(0, -2)
    }
    return lines.join('\n')
  }
  // JSON 模式(原逻辑)
  return `curl -X POST "${base}/extract" \\
  -H "Content-Type: application/json" \\
  -d '${step1Body.value.replace(/'/g, "'\\''")}'`
})

const curlStep2 = computed(() => {
  const base = apiBase.value
  const id   = taskId.value || '<task_id>'
  return `curl "${base}/tasks/${id}/result"`
})

// ─── 加载模型列表 ─────────────────────────────────────────────────────────────
// ─── 模型类型辅助 ───────────────────────────────────────────────────────────
function getModelType(modelName) {
  const info = modelsInfo.value.find(m => m.name === modelName)
  return info?.type || 'ner'
}

function getModelTypeLabel(modelName) {
  const labels = {
    llm: '[LLM] ',
    ioc: '[IOC] ',
    auto: '[AUTO] ',
    attack: '[ATT&CK] ',
  }
  return labels[getModelType(modelName)] || '[NER] '
}

// 按调用台模式过滤模型:attack 台只显示 ATT&CK 模型,ner 台显示其余模型。
const visibleModels = computed(() =>
  availableModels.value.filter(m =>
    isAttack.value ? getModelType(m) === 'attack' : getModelType(m) !== 'attack'
  )
)

onMounted(async () => {
  try {
    const res = await authFetch('/models')
    const data = await res.json()
    availableModels.value = data.models || []
    modelsInfo.value = data.models_info || []
    if (visibleModels.value.length) selectedModel.value = visibleModels.value[0]
  } catch {}
  // 获取后端实际地址（回调由后端线程直接发 HTTP，不走 Vite 代理）
  try {
    const res = await authFetch('/server-info')
    const data = await res.json()
    backendBase.value = data.base_url || ''
    // 如果回调 URL 为空，自动填入默认地址
    if (!callbackUrl.value && defaultCallbackUrl.value) {
      callbackUrl.value = defaultCallbackUrl.value
    }
  } catch {}
})

// ─── 执行第一步：发送任务 ─────────────────────────────────────────────────────
// 读取响应体:优先按 JSON 解析;后端 5xx 返回纯文本(如 "Internal Server Error")时
// 退化为原文显示,避免 res.json() 抛出 "Unexpected token ... is not valid JSON" 掩盖真实错误。
async function readResponseData(res) {
  const text = await res.text()
  try {
    return { data: JSON.parse(text), text }
  } catch {
    return { data: null, text }
  }
}

async function runStep1() {
  step1Error.value = ''
  step2Error.value = ''
  step1Res.value   = null
  step2Res.value   = null
  taskId.value     = ''
  step1Loading.value = true
  clearTimeout(pollTimer)

  // 文本模式超限时弹框警告,不发请求
  if (inputMode.value === 'text' && textLenWarning.value === 'over') {
    step1Loading.value = false
    const over = (textInput.value.length - MAX_TEXT_LENGTH).toLocaleString()
    alert(
      `文本长度超出上限 ${over} 字符！\n\n` +
      `当前: ${textInput.value.length.toLocaleString()} 字符\n` +
      `上限: ${MAX_TEXT_LENGTH.toLocaleString()} 字符\n\n` +
      `请缩短文本，或切换到「上传文件」标签页上传 .txt 等文本文件或 PDF（最大 10 MB）。`
    )
    return
  }
  try {
    let res
    if (uploadModeActive.value) {
      // 文件模式:multipart/form-data → /extract/upload;.eml / .msg 走专用端点 /extract/eml
      const isEml = uploadFileInfo.value?.isEml
      const fd = new FormData()
      fd.append('file', uploadFile.value)
      fd.append('labels', JSON.stringify(labels.value))
      if (selectedModel.value) fd.append('model_name', selectedModel.value)
      // /extract/eml 不支持 label_mapping 与分块参数
      if (!isEml && labelMapping.value) fd.append('label_mapping', JSON.stringify(labelMapping.value))
      fd.append('reuse_cache', String(reuseCache.value))
      if (informationJson.value && Object.keys(informationJson.value).length > 0) {
        fd.append('information_json', JSON.stringify(informationJson.value))
      }
      if (callbackEnabled.value) {
        const url = callbackUrl.value.trim() || defaultCallbackUrl.value
        const cb = { url, method: callbackMethod.value, timeout: callbackTimeout.value }
        if (callbackHeadersEnabled.value && Object.keys(callbackHeaders.value).length > 0) {
          cb.headers = callbackHeaders.value
        }
        fd.append('callback', JSON.stringify(cb))
      }
      if (!isEml && chunkAdvancedEnabled.value) {
        if (chunkEnabled.value      !== null) fd.append('chunk_enabled', String(chunkEnabled.value))
        if (chunkOverlap.value      !== null) fd.append('chunk_overlap', String(chunkOverlap.value))
        if (chunkSafetyMargin.value !== null) fd.append('chunk_safety_margin', String(chunkSafetyMargin.value))
        if (chunkThreshold.value    !== null) fd.append('threshold', String(chunkThreshold.value))
      }
      res = await authFetch(isEml ? '/extract/eml' : '/extract/upload', { method: 'POST', body: fd })
    } else {
      // JSON 模式 → /extract
      const body = JSON.parse(step1Body.value)
      res = await authFetch('/extract', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
    }
    const { data, text } = await readResponseData(res)
    step1Res.value = data ?? text
    if (!res.ok) {
      step1Error.value = data?.detail || `HTTP ${res.status}: ${text.slice(0, 300) || res.statusText}`
      return
    }
    if (!data) { step1Error.value = '响应不是合法 JSON: ' + text.slice(0, 300); return }
    if (data.code !== 200) { step1Error.value = data.msg || '提交失败'; return }
    taskId.value = data.task_id
    // 自动开始轮询第二步
    pollStep2()
  } catch (e) {
    step1Error.value = e.message
  } finally {
    step1Loading.value = false
  }
}

// ─── 执行第二步：查询结果（带轮询）────────────────────────────────────────────
function pollStep2() {
  clearTimeout(pollTimer)
  runStep2(true)
}

async function runStep2(retry = false) {
  if (!taskId.value) { step2Error.value = '请先获取 task_id'; return }
  step2Error.value = ''
  step2Loading.value = true
  try {
    const res = await authFetch(`/tasks/${taskId.value}/result`)
    const { data, text } = await readResponseData(res)
    step2Res.value = data ?? text
    if (!res.ok) {
      step2Error.value = data?.detail || `HTTP ${res.status}: ${text.slice(0, 300) || res.statusText}`
      return
    }
    if (!data) { step2Error.value = '响应不是合法 JSON: ' + text.slice(0, 300); return }
    // 若任务未完成则继续轮询 (status: 0=进行中, 1=成功, -1=失败)
    if (retry && data.status === 0) {
      pollTimer = setTimeout(() => runStep2(true), 1500)
    }
  } catch (e) {
    step2Error.value = e.message
  } finally {
    step2Loading.value = false
  }
}

// ─── 跳转实体图 ──────────────────────────────────────────────────────────────
function openGraph() {
  emit('open-graph', taskId.value)
}

// ─── 复制到剪贴板 ─────────────────────────────────────────────────────────
const copiedKey = ref('')  // 当前显示"已复制"的按钮 key

async function copyToClipboard(text, key) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text)
    } else {
      // 降级方案:创建临时 textarea 用 execCommand 复制
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.cssText = 'position:fixed;top:-9999px;left:-9999px;opacity:0'
      document.body.appendChild(ta)
      ta.focus()
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    copiedKey.value = key
    setTimeout(() => { if (copiedKey.value === key) copiedKey.value = '' }, 1800)
  } catch (e) {
    console.warn('复制失败:', e)
  }
}
function statusClass(status) {
  if (!status && status !== 0) return ''
  if (status === 1) return 'done'
  if (status === -1) return 'failed'
  return 'pending'
}
</script>

<template>
  <div class="playground">
    <h2>{{ isAttack ? 'ATT&CK 调用台' : 'NER 调用台' }}</h2>
    <p class="subtitle" v-if="isAttack">选择 ATT&CK 模型(TRAM / 大模型),默认 full 全量提取技战术;如需限定可在标签里填技术编号(如 T1059)或战术</p>
    <p class="subtitle" v-else>在下方填写参数，实时预览 curl 请求，一键执行并查看结果;支持上传 .eml / .msg 邮件(解析后提取 IOC)</p>

    <!-- 自定义 Tooltip -->
    <div 
      v-if="tooltipVisible" 
      class="custom-tooltip"
      :style="{ left: tooltipPosition.x + 'px', top: tooltipPosition.y + 'px' }"
    >
      <div class="tooltip-content" v-html="tooltipContent"></div>
      <div class="tooltip-arrow" :style="{ left: tooltipArrowOffset }"></div>
    </div>

    <!-- ═══════ 参数配置区 ═══════ -->
    <section class="card params-card">
      <h3>请求参数</h3>
      <div class="form-grid">

        <div class="field">
          <label>API 地址</label>
          <input v-model="apiBase" placeholder="http://localhost:8000" />
        </div>

        <div class="field">
          <label>模型</label>
          <select v-model="selectedModel">
            <option value="" v-if="!isAttack">（自动选择）</option>
            <option v-for="m in visibleModels" :key="m" :value="m">{{ getModelTypeLabel(m) }}{{ m }}</option>
          </select>
        </div>

        <div class="field">
          <label class="toggle-label">
            <span class="toggle-text">启用结果复用</span>
            <span class="toggle-hint">（相同文本和标签时复用历史结果）</span>
            <span
              class="help-icon"
              @mouseenter="showTooltip($event,
                '<strong>结果复用(缓存命中)</strong><br>' +
                '• 启用后,如果存在 <strong>相同文本 + 相同标签 + 相同模型 + 相同分块参数</strong> 的历史任务,直接复用其提取结果,不再调用模型推理<br>' +
                '• 缓存键 <code>task_hash = md5(text + sorted(labels) + model + chunk_cfg)</code><br>' +
                '• 任一参数不同 → 不同缓存,不会串味<br>' +
                '• <code>information_json</code> 不参与哈希,仍可用当前值覆盖<br>' +
                '• 命中时响应 <code>cached: true</code>,并携带 <code>cached_from</code> 指向原任务<br>' +
                '• <strong>文本输入与文件上传共享同一缓存</strong>:上传文件后文件内容会被解码为文本,与直接输入文本走相同的哈希计算,内容相同即可互相命中')"
              @mouseleave="hideTooltip"
            >?</span>
            <label class="switch">
              <input type="checkbox" v-model="reuseCache" />
              <span class="slider"></span>
            </label>
          </label>
        </div>

        <!-- 输入来源:文本直接输入 / 文件上传 -->
        <div class="field full input-source">
          <div class="input-source-header">
            <div class="input-source-tabs">
              <button
                type="button"
                :class="['input-tab', { active: inputMode === 'text' }]"
                @click="switchInputMode('text')"
              >
                <span class="tab-icon">✎</span> 直接输入
              </button>
              <button
                type="button"
                :class="['input-tab', { active: inputMode === 'file' }]"
                @click="switchInputMode('file')"
              >
                <span class="tab-icon">📄</span> 上传文件
              </button>
            </div>
            <span
              class="help-icon"
              @mouseenter="showTooltip($event, UPLOAD_TOOLTIP_HTML)"
              @mouseleave="hideTooltip"
            >?</span>
          </div>

          <!-- 文本输入模式 -->
          <div v-if="inputMode === 'text'" class="input-pane">
            <textarea
              v-model="textInput"
              rows="4"
              :class="{ 'textarea-warn': textLenWarning === 'warn', 'textarea-over': textLenWarning === 'over' }"
              :placeholder="`请输入待提取实体的文本…（最大长度 ${MAX_TEXT_LENGTH.toLocaleString()} 字符）`"
            />
            <div class="text-counter" :class="textLenWarning">
              <span class="counter-num">{{ textInput.length.toLocaleString() }} / {{ MAX_TEXT_LENGTH.toLocaleString() }}</span>
              <template v-if="textLenWarning === 'over'">
                <span class="counter-msg">
                  ⚠ 超出上限 {{ (textInput.length - MAX_TEXT_LENGTH).toLocaleString() }} 字符,提交会被拒绝(413)。
                  <button type="button" class="counter-switch-btn" @click="switchInputMode('file')">
                    切换到上传文件 →
                  </button>
                </span>
              </template>
              <template v-else-if="textLenWarning === 'warn'">
                <span class="counter-msg">
                  接近上限,超过 {{ MAX_TEXT_LENGTH.toLocaleString() }} 字符时请改用
                  <button type="button" class="counter-switch-btn" @click="switchInputMode('file')">
                    上传文件
                  </button>
                </span>
              </template>
            </div>
          </div>

          <!-- 文件上传模式 -->
          <div v-else class="input-pane">
            <div class="upload-row">
              <label
                class="upload-dropzone"
                :class="{
                  active: uploadModeActive,
                  dragging: isDragging,
                  invalid: uploadFile && (uploadFileInfo?.badExt || uploadFileInfo?.tooLarge),
                }"
                @dragover="onDragOver"
                @dragleave="onDragLeave"
                @drop="onDrop"
              >
                <input
                  ref="uploadFileInput"
                  type="file"
                  :accept="UPLOAD_ACCEPT"
                  @change="onFilePicked"
                  class="upload-input-hidden"
                />
                <template v-if="!uploadFile && !isDragging">
                  <!-- Heroicons document-text (MIT License, tailwindlabs/heroicons) -->
                  <svg class="dropzone-svg-icon" viewBox="0 0 24 24" fill="none"
                       xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
                          d="M19.5 14.25v-2.625a3.375 3.375 0 0 0-3.375-3.375h-1.5A1.125 1.125 0 0 1 13.5 7.125v-1.5a3.375 3.375 0 0 0-3.375-3.375H8.25m0 12.75h7.5m-7.5 3H12M10.5 2.25H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 0 0-9-9Z"/>
                  </svg>
                  <span class="dropzone-text">点击选择或拖拽文件</span>
                  <span class="dropzone-sub">支持 .txt / .md / .csv / .json 等文本格式、PDF 及 .eml / .msg 邮件,最大 10 MB</span>
                </template>
                <template v-else-if="isDragging">
                  <svg class="dropzone-svg-icon dragging-icon" viewBox="0 0 24 24" fill="none"
                       xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5"
                          d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5m-13.5-9L12 3m0 0 4.5 4.5M12 3v13.5"/>
                  </svg>
                  <span class="dropzone-text">松开鼠标以上传</span>
                </template>
                <template v-else>
                  <span class="dropzone-file">
                    <span class="file-name">{{ uploadFileInfo.name }}</span>
                    <span class="file-meta">
                      <span class="file-ext" :class="{ bad: uploadFileInfo.badExt }">{{ uploadFileInfo.ext }}</span>
                      <span class="file-size" :class="{ bad: uploadFileInfo.tooLarge }">{{ uploadFileInfo.kb }} KB</span>
                    </span>
                  </span>
                </template>
              </label>
              <button
                v-if="uploadFile"
                type="button"
                class="upload-clear-btn"
                @click="clearUploadFile"
                title="移除文件"
              >清除</button>
            </div>

            <div v-if="uploadFile && uploadFileInfo?.badExt" class="upload-error">
              ⚠ 不支持的文件类型 <code>{{ uploadFileInfo.ext }}</code>,请使用 PDF、.eml / .msg 或 {{ ALLOWED_TEXT_EXTENSIONS.slice(0, 8).join(', ') }} 等文本格式
            </div>
            <div v-else-if="uploadFile && uploadFileInfo?.tooLarge" class="upload-error">
              ⚠ 文件过大({{ uploadFileInfo.kb }} KB),上限 10240 KB
            </div>
            <div v-else-if="uploadModeActive" class="upload-notice">
              ✓ 已选择文件,后端将调用 <code>POST {{ uploadFileInfo?.isEml ? '/extract/eml' : '/extract/upload' }}</code><template v-if="uploadFileInfo?.isEml">(解析邮件 JSON 后提取 IOC)</template>
            </div>
            <div v-else class="upload-hint">
              💡 选择文件后自动走 <code>/extract/upload</code>(.eml / .msg 走 <code>/extract/eml</code>);暂未选择时可切回"直接输入"标签页
            </div>
          </div>
        </div>

        <!-- 三个 JSON 字段排成一行(ATT&CK 台隐藏 label_mapping,只有两列) -->
        <div :class="['json-row', { 'json-row--two': isAttack }]">
          <div class="field">
            <div class="label-with-action">
              <div class="label-with-help">
                <label>标签列表 <small>（JSON 数组）</small></label>
                <span 
                  class="help-icon" 
                  @mouseenter="showTooltip($event, '填写要提取的实体类型<br><strong>示例：</strong><code>[&quot;人物&quot;, &quot;地点&quot;, &quot;机构&quot;, &quot;时间&quot;]</code><br>• 支持中文标签<br>• 可自定义任意实体类型')"
                  @mouseleave="hideTooltip"
                >?</span>
              </div>
              <button class="format-btn" @click="formatLabels" type="button" title="格式化 JSON">
                格式化
              </button>
            </div>
            <textarea
              v-model="labelsStr"
              :class="{ 'input-error': labelsError, 'json-editor': true }"
              rows="8"
              placeholder='["人物", "地点", "机构", "时间"]'
              spellcheck="false"
            />
            <span v-if="labelsError" class="field-error">{{ labelsError }}</span>
          </div>

          <div class="field" v-if="!isAttack">
            <div class="label-with-action">
              <div class="label-with-help">
                <label>
                  label_mapping
                  <small>（标签映射）</small>
                </label>
                <span 
                  class="help-icon"
                  @mouseenter="showTooltip($event, '将中文标签映射为英文标准标签<br><strong>示例：</strong><code>{&quot;人物&quot;: &quot;PERSON&quot;, &quot;地点&quot;: &quot;LOCATION&quot;}</code><br>• 可选项，留空则使用原始标签<br>• 修改标签列表时会自动同步')"
                  @mouseleave="hideTooltip"
                >?</span>
              </div>
              <div class="mapping-controls">
                <button 
                  :class="['toggle-mapping-btn', { active: labelMappingEnabled }]"
                  @click="labelMappingEnabled = !labelMappingEnabled"
                  type="button"
                >
                  {{ labelMappingEnabled ? '✓ 已启用' : '启用映射' }}
                </button>
                <button 
                  v-if="labelMappingEnabled"
                  class="format-btn" 
                  @click="formatLabelMapping" 
                  type="button" 
                  title="格式化 JSON"
                >
                  格式化
                </button>
              </div>
            </div>
            <div v-if="!labelMappingEnabled" class="disabled-placeholder">
              <div class="placeholder-content">
                <span class="placeholder-icon">🔒</span>
                <span class="placeholder-text">标签映射未启用</span>
                <span class="placeholder-hint">点击上方"启用映射"按钮开启</span>
              </div>
            </div>
            <textarea
              v-else
              v-model="labelMappingStr"
              :class="{ 'input-error': labelMappingError, 'json-editor': true }"
              rows="8"
              placeholder='{"人物": "PERSON", "地点": "LOCATION"}'
              spellcheck="false"
            />
            <span v-if="labelMappingError && labelMappingEnabled" class="field-error">{{ labelMappingError }}</span>
            <span v-if="labelMappingEnabled" class="field-hint">💡 修改标签时自动同步</span>
          </div>

          <div class="field">
            <div class="label-with-action">
              <div class="label-with-help">
                <label>
                  information_json
                  <small>（消息绑定）</small>
                </label>
                <span 
                  class="help-icon"
                  @mouseenter="showTooltip($event, '用于绑定自定义信息，会在结果中原封不动返回<br><strong>示例：</strong><code>{&quot;file_id&quot;: &quot;0537ef0d8ac6a2179a58067615825924&quot;}</code><br>• 可用于追踪请求来源、文件标识等<br>• 默认包含 file_id 字段')"
                  @mouseleave="hideTooltip"
                >?</span>
              </div>
              <button class="format-btn" @click="formatInformation" type="button" title="格式化 JSON">
                格式化
              </button>
            </div>
            <textarea
              v-model="informationStr"
              :class="{ 'input-error': informationError, 'json-editor': true }"
              rows="8"
              placeholder='{"file_id": "0537ef0d8ac6a2179a58067615825924"}'
              spellcheck="false"
            />
            <span v-if="informationError" class="field-error">{{ informationError }}</span>
            <span class="field-hint">💡 可选，原封不动返回</span>
          </div>
        </div>

        <!-- 分块参数 (long-text-chunking-ner) -->
        <div class="field full chunk-section">
          <div class="chunk-header">
            <div class="label-with-help">
              <label>分块参数 <small>(长文本自动分块,默认已启用)</small></label>
              <span
                class="help-icon"
                @mouseenter="showTooltip($event,
                  '长文本自动按句/字符分块,避免 tokenizer 静默截断尾部实体。<br>' +
                  '• 默认使用后端 <code>.env</code> 里的推荐参数<br>' +
                  '• <strong>大多数用户不需要修改</strong><br>' +
                  '• 启用高级配置后可对单次请求覆盖 4 个参数:<br>' +
                  '&nbsp;&nbsp;chunk_enabled / chunk_overlap / chunk_safety_margin / threshold')"
                @mouseleave="hideTooltip"
              >?</span>
            </div>
            <button
              :class="['toggle-chunk-btn', { active: chunkAdvancedEnabled }]"
              @click="chunkAdvancedEnabled = !chunkAdvancedEnabled"
              type="button"
            >
              {{ chunkAdvancedEnabled ? '✓ 已启用高级配置' : '高级配置' }}
            </button>
          </div>

          <div v-if="!chunkAdvancedEnabled" class="disabled-placeholder chunk-placeholder">
            <div class="placeholder-content">
              <span class="placeholder-icon">📦</span>
              <span class="placeholder-text">使用后端默认分块参数</span>
              <span class="placeholder-hint">如需自定义,点击上方"高级配置"按钮</span>
            </div>
          </div>

          <div v-else>
            <div class="chunk-row">
              <div class="field">
                <div class="label-with-help">
                  <label>chunk_enabled</label>
                  <span
                    class="help-icon"
                    @mouseenter="showTooltip($event,
                      '<strong>是否启用分块</strong><br>' +
                      '• 默认 <code>true</code> — 长文本自动按句/字符切分,避免尾部实体被静默截断<br>' +
                      '• <code>false</code> — 整段文本直接送模型,超长会被截断<br>' +
                      '• 留空(<code>后端默认</code>) — 使用 <code>.env</code> 中的 <code>CHUNK_ENABLED</code>')"
                    @mouseleave="hideTooltip"
                  >?</span>
                </div>
                <select v-model="chunkEnabled">
                  <option :value="null">（后端默认）</option>
                  <option :value="true">true（启用分块）</option>
                  <option :value="false">false（不分块)</option>
                </select>
              </div>
              <div class="field">
                <div class="label-with-help">
                  <label>chunk_overlap <small>(0-512)</small></label>
                  <span
                    class="help-icon"
                    @mouseenter="showTooltip($event,
                      '<strong>相邻 Chunk 的字符重叠数</strong><br>' +
                      '• 单位: 字符(不是 token)<br>' +
                      '• 默认 <code>32</code>,建议范围 <code>16-64</code><br>' +
                      '• 避免实体被切在块边界上而漏掉<br>' +
                      '• 设为 <code>0</code> → 相邻块无重叠<br>' +
                      '• 实际生效值不会超过 chunk 长度的一半')"
                    @mouseleave="hideTooltip"
                  >?</span>
                </div>
                <input
                  type="number"
                  v-model.number="chunkOverlap"
                  min="0"
                  max="512"
                  placeholder="后端默认 32"
                />
              </div>
              <div class="field">
                <div class="label-with-help">
                  <label>chunk_safety_margin <small>(0-64)</small></label>
                  <span
                    class="help-icon"
                    @mouseenter="showTooltip($event,
                      '<strong>token 安全余量</strong><br>' +
                      '<code>effective_max_tokens = model_max_tokens − labels_token_count − safety_margin</code><br>' +
                      '• 默认 <code>16</code>,范围 <code>0-64</code><br>' +
                      '• 越大越保守,模型上限用不满但更稳<br>' +
                      '• 设为 <code>0</code> 会贴着模型上限跑,tokenizer 个别边界情况可能超限')"
                    @mouseleave="hideTooltip"
                  >?</span>
                </div>
                <input
                  type="number"
                  v-model.number="chunkSafetyMargin"
                  min="0"
                  max="64"
                  placeholder="后端默认 16"
                />
              </div>
              <div class="field">
                <div class="label-with-help">
                  <label>threshold <small>(0.0-1.0)</small></label>
                  <span
                    class="help-icon"
                    @mouseenter="showTooltip($event,
                      '<strong>predict_entities 的置信度阈值</strong><br>' +
                      '• 默认 <code>0.5</code>,范围 <code>0.0-1.0</code><br>' +
                      '• 越高越严格 → 实体数少但质量高<br>' +
                      '• 越低越宽松 → 实体数多但假阳性上升<br>' +
                      '• 该参数也会纳入 task_hash,不同阈值对应不同缓存')"
                    @mouseleave="hideTooltip"
                  >?</span>
                </div>
                <input
                  type="number"
                  v-model.number="chunkThreshold"
                  min="0"
                  max="1"
                  step="0.05"
                  placeholder="后端默认 0.5"
                />
              </div>
            </div>
            <div class="chunk-hint">
              💡 各字段留空表示使用后端 .env 中的默认值;提供值时会在请求体中新增对应字段并影响
              <code>compute_task_hash</code>(不同参数对应不同缓存)。
            </div>
          </div>
        </div>

        <!-- 回调配置区域 -->
        <div class="field full callback-section">
          <div class="callback-header">
            <div class="label-with-help">
              <label>回调配置 <small>（可选）</small></label>
              <span 
                class="help-icon"
                @mouseenter="showTooltip($event, '任务完成后自动推送结果到指定地址<br><strong>注意：</strong>回调失败不会自动重试<br>• 可在回调失败管理中查看失败记录<br>• 支持手动重试失败的回调')"
                @mouseleave="hideTooltip"
              >?</span>
            </div>
            <button 
              :class="['toggle-callback-btn', { active: callbackEnabled }]"
              @click="callbackEnabled = !callbackEnabled"
              type="button"
            >
              {{ callbackEnabled ? '✓ 已启用' : '启用回调' }}
            </button>
          </div>

          <div v-if="!callbackEnabled" class="disabled-placeholder callback-placeholder">
            <div class="placeholder-content">
              <span class="placeholder-icon">📤</span>
              <span class="placeholder-text">回调功能未启用</span>
              <span class="placeholder-hint">启用后可自动推送结果，无需轮询</span>
            </div>
          </div>

          <div v-else class="callback-config">
            <div class="callback-row">
              <div class="field">
                <label>回调地址（可选，留空使用默认测试端点）</label>
                <input 
                  v-model="callbackUrl" 
                  :placeholder="defaultCallbackUrl"
                  type="url"
                />
                <span class="field-hint">💡 留空将使用系统内置测试端点，回调数据会在后端控制台打印</span>
              </div>
              <div class="field callback-method">
                <label>请求方式</label>
                <select v-model="callbackMethod">
                  <option value="POST">POST</option>
                  <option value="PUT">PUT</option>
                </select>
              </div>
              <div class="field callback-timeout">
                <label>超时时间（秒）</label>
                <input 
                  v-model.number="callbackTimeout" 
                  type="number"
                  min="1"
                  max="300"
                />
              </div>
            </div>

            <div class="field">
              <div class="label-with-action">
                <div class="label-with-help">
                  <label>自定义请求头 <small>（可选，JSON 对象）</small></label>
                </div>
                <div class="mapping-controls">
                  <button 
                    :class="['toggle-mapping-btn', { active: callbackHeadersEnabled }]"
                    @click="callbackHeadersEnabled = !callbackHeadersEnabled"
                    type="button"
                  >
                    {{ callbackHeadersEnabled ? '✓ 已启用' : '启用请求头' }}
                  </button>
                  <button 
                    v-if="callbackHeadersEnabled"
                    class="format-btn" 
                    @click="formatCallbackHeaders" 
                    type="button" 
                    title="格式化 JSON"
                  >
                    格式化
                  </button>
                </div>
              </div>
              <div v-if="!callbackHeadersEnabled" class="disabled-placeholder" style="min-height: calc(4 * 1.5em + 14px);">
                <div class="placeholder-content">
                  <span class="placeholder-icon">🔒</span>
                  <span class="placeholder-text">自定义请求头未启用</span>
                  <span class="placeholder-hint">点击上方"启用请求头"按钮开启</span>
                </div>
              </div>
              <template v-else>
                <textarea
                  v-model="callbackHeadersStr"
                  :class="{ 'input-error': callbackHeadersError, 'json-editor': true }"
                  rows="4"
                  placeholder='{"Authorization": "Bearer xxx", "X-Custom": "value"}'
                  spellcheck="false"
                />
                <span v-if="callbackHeadersError" class="field-error">{{ callbackHeadersError }}</span>
              </template>
            </div>

            <div class="callback-notice">
              <div class="notice-item">
                💡 <strong>默认测试端点：</strong>留空回调地址将使用系统内置端点 <code>{{ defaultCallbackUrl }}</code>，回调数据会在后端控制台打印，方便测试。
              </div>
              <div class="notice-item">
                ⚠️ <strong>注意：</strong>回调失败不会自动重试，请在"回调失败管理"中查看失败记录并手动重试。
              </div>
            </div>
          </div>
        </div>

      </div>
    </section>

    <!-- ═══════ 两步请求并排 ═══════ -->
    <div class="steps-row">

      <!-- ── Step 1 ── -->
      <section class="card step-card">
        <div class="step-header">
          <span class="step-badge">Step 1</span>
          <span class="step-title">POST /extract &nbsp;→ 获取 task_id</span>
        </div>

        <!-- curl 展示 -->
        <div class="curl-block">
          <div class="curl-topbar">
            <span>curl</span>
            <button
              class="copy-btn"
              :class="{ copied: copiedKey === 'step1' }"
              @click="copyToClipboard(curlStep1, 'step1')"
            >{{ copiedKey === 'step1' ? '✓ 已复制' : '复制' }}</button>
          </div>
          <pre class="curl-pre" v-html="highlightCurl(curlStep1)"></pre>
        </div>

        <button
          class="run-btn"
          :disabled="step1Loading || !!labelsError || !!labelMappingError || !!informationError || (!uploadModeActive && !textInput.trim()) || (uploadFile && (uploadFileInfo?.badExt || uploadFileInfo?.tooLarge))"
          @click="runStep1"
        >
          <span v-if="step1Loading" class="spinner"></span>
          {{ step1Loading ? '请求中…' : '▶ 执行请求' }}
        </button>

        <!-- 结果 -->
        <div v-if="step1Error" class="resp-error">{{ step1Error }}</div>
        <div v-if="step1Res" class="resp-block">
          <div class="resp-topbar">
            <span class="resp-label">Response</span>
            <span v-if="step1Res.task_id" class="task-id-badge">task_id: {{ step1Res.task_id }}</span>
            <span v-if="step1Res.cached" class="cached-badge">🔄 使用缓存</span>
          </div>
          <pre class="resp-pre" v-html="highlightJson(step1Res)"></pre>
        </div>
      </section>

      <!-- ── Step 2 ── -->
      <section class="card step-card">
        <div class="step-header">
          <span class="step-badge step-badge-2">Step 2</span>
          <span class="step-title">GET /tasks/{task_id}/result &nbsp;→ 查询结果</span>
        </div>

        <!-- task_id 输入 -->
        <div class="taskid-row">
          <label>task_id</label>
          <input v-model="taskId" placeholder="执行 Step 1 后自动填入，或手动输入" />
        </div>

        <!-- curl 展示 -->
        <div class="curl-block">
          <div class="curl-topbar">
            <span>curl</span>
            <button
              class="copy-btn"
              :class="{ copied: copiedKey === 'step2' }"
              @click="copyToClipboard(curlStep2, 'step2')"
            >{{ copiedKey === 'step2' ? '✓ 已复制' : '复制' }}</button>
          </div>
          <pre class="curl-pre" v-html="highlightCurl(curlStep2)"></pre>
        </div>

        <div class="step2-actions">
          <button
            class="run-btn"
            :disabled="step2Loading || !taskId.trim()"
            @click="runStep2(false)"
          >
            <span v-if="step2Loading" class="spinner"></span>
            {{ step2Loading ? '查询中…' : '▶ 执行查询' }}
          </button>

          <button
            v-if="step2Res?.status === 1"
            class="graph-btn"
            @click="openGraph"
          >
            📊 查看实体图表
          </button>
        </div>

        <!-- 结果 -->
        <div v-if="step2Error" class="resp-error">{{ step2Error }}</div>
        <div v-if="step2Res" class="resp-block">
          <div class="resp-topbar">
            <span class="resp-label">Response</span>
            <span :class="['status-badge', statusClass(step2Res.status)]">
              {{ step2Res.status === 1 ? 'done' : step2Res.status === -1 ? 'failed' : 'pending' }}
            </span>
            <span v-if="step2Res.latency_ms != null" class="latency">{{ step2Res.latency_ms?.toFixed(0) }} ms</span>
            <span v-if="step2Res.entities?.length" class="entity-count">{{ step2Res.entities.length }} 个实体</span>
            <span v-if="step2Res.information_json && Object.keys(step2Res.information_json).length" class="info-badge" title="包含自定义信息">📎 Info</span>
            <button
              class="copy-btn resp-copy-btn"
              :class="{ copied: copiedKey === 'step2-resp' }"
              @click="copyToClipboard(JSON.stringify(step2Res, null, 2), 'step2-resp')"
              title="复制完整 JSON 响应"
            >{{ copiedKey === 'step2-resp' ? '✓ 已复制' : '复制 JSON' }}</button>
          </div>
          <!-- 任务未完成时展示轮询动画 -->
          <div v-if="step2Res.status === 0" class="polling-hint">
            <span class="spinner"></span> {{ step2Res.msg || '任务执行中，自动轮询…' }}
          </div>
          <!-- 实体快览 -->
          <div v-if="step2Res.status === 1 && step2Res.entities?.length" class="entity-preview">
            <span
              v-for="(e, i) in step2Res.entities"
              :key="i"
              class="ent-chip"
            >{{ e.text }}<em>{{ e.label }}</em><em v-if="e.technique_name" class="tech-name">{{ e.technique_name }}</em></span>
          </div>
          <pre class="resp-pre" v-html="highlightJson(step2Res)"></pre>
        </div>
      </section>

    </div><!-- /steps-row -->

  </div>
</template>

<style scoped>
.playground { 
  max-width: 100%;
  margin: 0;
  padding: 0;
}
h2 { font-size: 18px; margin-bottom: 4px; }
.subtitle { color: #888; font-size: 13px; margin-bottom: 20px; }
h3 { font-size: 15px; margin-bottom: 14px; color: #444; }

/* ── 卡片 ── */
.card { background: #fff; border-radius: 8px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 16px; }

/* ── 参数表单 ── */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.field { display: flex; flex-direction: column; gap: 5px; }
.field.full { grid-column: 1 / -1; }
.field.third { grid-column: span 1; }
.json-row { 
  display: grid; 
  grid-column: 1 / -1;
  grid-template-columns: repeat(3, 1fr); 
  gap: 14px; 
}
/* ATT&CK 台隐藏 label_mapping 后只剩两列 */
.json-row--two { grid-template-columns: repeat(2, 1fr); }
.field label { font-size: 13px; font-weight: 600; color: #555; }
.field label small { font-weight: 400; color: #999; }

/* ── 复用开关样式 ── */
.toggle-label {
  display: flex;
  align-items: center;
  gap: 12px;
  user-select: none;
}
.toggle-text {
  color: #555;
  font-weight: 600;
  font-size: 13px;
}
.toggle-hint {
  color: #999;
  font-size: 12px;
  font-weight: 400;
  flex: 1;
}

/* Toggle Switch 样式 */
.switch {
  position: relative;
  display: inline-block;
  width: 48px;
  height: 24px;
  flex-shrink: 0;
}

.switch input {
  opacity: 0;
  width: 0;
  height: 0;
}

.slider {
  position: absolute;
  cursor: pointer;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background-color: #ccc;
  transition: .3s;
  border-radius: 24px;
}

.slider:before {
  position: absolute;
  content: "";
  height: 18px;
  width: 18px;
  left: 3px;
  bottom: 3px;
  background-color: white;
  transition: .3s;
  border-radius: 50%;
  box-shadow: 0 2px 4px rgba(0,0,0,0.2);
}

.switch input:checked + .slider {
  background-color: #5470c6;
}

.switch input:focus + .slider {
  box-shadow: 0 0 1px #5470c6;
}

.switch input:checked + .slider:before {
  transform: translateX(24px);
}

.switch input:disabled + .slider {
  opacity: 0.5;
  cursor: not-allowed;
}

.label-with-action {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
}
.label-with-help {
  display: flex;
  align-items: center;
  gap: 6px;
}
.mapping-controls {
  display: flex;
  align-items: center;
  gap: 8px;
}
.toggle-mapping-btn {
  background: #e0e0e0;
  color: #666;
  border: 1px solid #d0d0d0;
  border-radius: 4px;
  padding: 4px 12px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
  font-weight: 500;
}
.toggle-mapping-btn:hover {
  background: #d0d0d0;
  border-color: #c0c0c0;
}
.toggle-mapping-btn.active {
  background: #3ba272;
  color: #fff;
  border-color: #2e8060;
}
.toggle-mapping-btn.active:hover {
  background: #2e8060;
  border-color: #256b4f;
}
.disabled-placeholder {
  border: 1px solid #ddd;
  border-radius: 6px;
  background: repeating-linear-gradient(
    45deg,
    #f8f8f8,
    #f8f8f8 10px,
    #f0f0f0 10px,
    #f0f0f0 20px
  );
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.5;
  padding: 7px 10px;
  min-height: calc(8 * 1.5em + 14px);
}
.placeholder-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  color: #999;
}
.placeholder-icon {
  font-size: 32px;
  opacity: 0.5;
}
.placeholder-text {
  font-size: 14px;
  font-weight: 600;
  color: #888;
}
.placeholder-hint {
  font-size: 12px;
  color: #aaa;
}
.help-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  background: #5470c6;
  color: #fff;
  border-radius: 50%;
  font-size: 11px;
  font-weight: bold;
  cursor: help;
  flex-shrink: 0;
  transition: all 0.2s;
  font-family: Arial, sans-serif;
}
.help-icon:hover {
  background: #3f56a0;
  transform: scale(1.1);
}
.format-btn {
  background: #5470c6;
  color: #fff;
  border: 1px solid #4a63b3;
  border-radius: 4px;
  padding: 4px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
  font-weight: 500;
}
.format-btn:hover {
  background: #4a63b3;
  border-color: #3f56a0;
}
.format-btn:active {
  background: #3f56a0;
  transform: scale(0.98);
}
.field input,
.field select,
.field textarea {
  border: 1px solid #ddd; border-radius: 6px; padding: 7px 10px;
  font-size: 13px; outline: none; font-family: inherit;
  transition: border-color .15s;
}
.field input:focus, .field select:focus, .field textarea:focus { border-color: #5470c6; }
.field textarea { resize: vertical; }
.json-editor {
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
  font-size: 13px;
  line-height: 1.5;
  background: #f8f9fa;
  tab-size: 2;
}
.input-error { border-color: #e74c3c !important; }
.field-error { color: #e74c3c; font-size: 12px; }
.field-hint { color: #3ba272; font-size: 12px; margin-top: 2px; }

/* ── 两列步骤 ── */
.steps-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.step-card { display: flex; flex-direction: column; gap: 12px; }
.step-header { display: flex; align-items: center; gap: 10px; border-bottom: 1px solid #f0f0f0; padding-bottom: 12px; }
.step-badge   { background: #5470c6; color: #fff; font-size: 12px; font-weight: 700; padding: 2px 8px; border-radius: 4px; }
.step-badge-2 { background: #3ba272; }
.step-title   { font-size: 13px; color: #555; font-family: monospace; }

/* ── task_id 输入 ── */
.taskid-row { display: flex; align-items: center; gap: 8px; }
.taskid-row label { font-size: 13px; font-weight: 600; color: #555; white-space: nowrap; }
.taskid-row input { flex: 1; border: 1px solid #ddd; border-radius: 6px; padding: 6px 10px; font-size: 12px; outline: none; }
.taskid-row input:focus { border-color: #3ba272; }

/* ── curl 块 ── */
.curl-block { background: #1e1e1e; border-radius: 6px; overflow: hidden; }
.curl-topbar { display: flex; justify-content: space-between; align-items: center; padding: 6px 12px; background: #2d2d2d; }
.curl-topbar > span { color: #888; font-size: 12px; }
.copy-btn { background: transparent; border: 1px solid #555; color: #ccc; padding: 2px 10px; border-radius: 4px; cursor: pointer; font-size: 11px; transition: all .15s; }
.copy-btn:hover { background: #555; }
.copy-btn.copied { border-color: #3ba272; color: #3ba272; }
.curl-pre { color: #f8f8f2; font-size: 12px; font-family: 'Consolas', monospace; padding: 12px; overflow-x: auto; white-space: pre-wrap; word-break: break-all; margin: 0; }
/* curl 高亮 token（v-html 注入的 span 不带 scoped 属性，必须 :deep；monokai 与响应 JSON 面板一致） */
.curl-pre :deep(.ck-c)  { color: #f8f8f2; font-weight: 700; }   /* curl 命令 */
.curl-pre :deep(.ck-m)  { color: #ae81ff; }                     /* HTTP 方法 */
.curl-pre :deep(.ck-f)  { color: #f92672; }                     /* -X/-F/-H/-d 等旗标 */
.curl-pre :deep(.ck-u)  { color: #66d9ef; }                     /* URL */
.curl-pre :deep(.ck-s)  { color: #a6e22e; }                     /* 引号字符串 */
.curl-pre :deep(.ck-ct) { color: #75715e; }                     /* 行尾续行符 */
.curl-pre :deep(.ck-hk) { color: #66d9ef; }                     /* -H 头部名（值按 ck-s 字符串色） */
/* -d JSON 请求体 token（与响应 JSON 面板同一套类名/配色） */
.curl-pre :deep(.k)  { color: #66d9ef; }
.curl-pre :deep(.s)  { color: #a6e22e; }
.curl-pre :deep(.n)  { color: #ae81ff; }
.curl-pre :deep(.b)  { color: #f92672; }
.curl-pre :deep(.nl) { color: #75715e; }

/* ── 按钮 ── */
.run-btn {
  align-self: flex-start;
  background: #5470c6; color: #fff;
  border: none; border-radius: 6px; padding: 8px 20px;
  cursor: pointer; font-size: 14px; display: flex; align-items: center; gap: 8px;
}
.run-btn:disabled { background: #bbb; cursor: not-allowed; }
.step2-actions { display: flex; gap: 10px; align-items: center; }
.graph-btn {
  background: #3ba272; color: #fff; border: none; border-radius: 6px;
  padding: 8px 16px; cursor: pointer; font-size: 13px;
}
.graph-btn:hover { background: #2e8060; }

/* ── 响应块 ── */
.resp-error { color: #e74c3c; background: #fee2e2; padding: 8px 12px; border-radius: 6px; font-size: 13px; }
.resp-block { background: #1e1e2e; border-radius: 6px; overflow: hidden; }
.resp-topbar { display: flex; align-items: center; gap: 10px; padding: 8px 12px; background: #2a2a3e; flex-wrap: wrap; }
.resp-topbar .resp-copy-btn { margin-left: auto; }
.resp-label  { color: #888; font-size: 12px; }
.task-id-badge { background: #5470c6; color: #fff; font-size: 11px; padding: 2px 8px; border-radius: 10px; font-family: monospace; }
.cached-badge { background: #3ba272; color: #fff; font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 600; }
.info-badge { background: #f59e0b; color: #fff; font-size: 11px; padding: 2px 8px; border-radius: 10px; font-weight: 600; cursor: help; }
.status-badge  { font-size: 12px; padding: 2px 8px; border-radius: 10px; font-weight: 600; }
.status-badge.done    { background: #d1fae5; color: #065f46; }
.status-badge.failed  { background: #fee2e2; color: #991b1b; }
.status-badge.pending { background: #fef3c7; color: #92400e; }
.latency, .entity-count { font-size: 12px; color: #aaa; }
.resp-pre { color: #f8f8f2; font-size: 12px; font-family: 'Consolas', monospace; padding: 12px; overflow-x: auto; white-space: pre-wrap; word-break: break-all; margin: 0; max-height: 360px; overflow-y: auto; }
/* v-html 注入的高亮 span 不带 scoped 属性，必须 :deep 才能命中（与 TiPlayground 一致） */
.resp-pre :deep(.k)  { color: #66d9ef; }   /* key */
.resp-pre :deep(.s)  { color: #a6e22e; }   /* string */
.resp-pre :deep(.n)  { color: #ae81ff; }   /* number */
.resp-pre :deep(.b)  { color: #f92672; }   /* bool */
.resp-pre :deep(.nl) { color: #75715e; }   /* null */

/* ── 轮询 / 实体快览 ── */
.polling-hint { display: flex; align-items: center; gap: 8px; color: #f59e0b; font-size: 13px; padding: 6px 12px; }
.entity-preview { display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 12px; border-bottom: 1px solid #2a2a3e; }
.ent-chip { background: #2a2a3e; color: #f8f8f2; border-radius: 4px; padding: 3px 8px; font-size: 12px; }
.ent-chip em { font-style: normal; color: #66d9ef; margin-left: 4px; }
.ent-chip em.tech-name { color: #f59e0b; }

/* ── spinner ── */
.spinner {
  width: 14px; height: 14px; border: 2px solid rgba(255,255,255,.3);
  border-top-color: #fff; border-radius: 50%; animation: spin .7s linear infinite; display: inline-block;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* ── 回调配置样式 ── */
.callback-section {
  border-top: 2px solid #f0f0f0;
  padding-top: 20px;
  margin-top: 20px;
}
.callback-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 12px;
}
.toggle-callback-btn {
  background: #e0e0e0;
  color: #666;
  border: 1px solid #d0d0d0;
  border-radius: 4px;
  padding: 6px 16px;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
  font-weight: 500;
}
.toggle-callback-btn:hover {
  background: #d0d0d0;
  border-color: #c0c0c0;
}
.toggle-callback-btn.active {
  background: #5470c6;
  color: #fff;
  border-color: #4a63b3;
}
.toggle-callback-btn.active:hover {
  background: #4a63b3;
  border-color: #3f56a0;
}
.callback-placeholder {
  min-height: 120px;
}
.callback-config {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 16px;
  background: #f8f9fa;
  border-radius: 6px;
  border: 1px solid #e0e0e0;
}
.callback-row {
  display: grid;
  grid-template-columns: 2fr 1fr 1fr;
  gap: 14px;
}
.callback-method {
  min-width: 120px;
}
.callback-timeout {
  min-width: 140px;
}
.callback-notice {
  background: #f8f9fa;
  border: 1px solid #e0e0e0;
  border-radius: 6px;
  padding: 12px 16px;
  font-size: 13px;
  color: #555;
  line-height: 1.6;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.callback-notice .notice-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.callback-notice .notice-item strong {
  color: #333;
}
.callback-notice .notice-item code {
  background: #e0e0e0;
  padding: 2px 6px;
  border-radius: 3px;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 12px;
  color: #5470c6;
}

@media (max-width: 900px) {
  .steps-row, .form-grid { grid-template-columns: 1fr; }
  .field.full { grid-column: 1; }
  .json-row { grid-template-columns: 1fr; }
  .callback-row { grid-template-columns: 1fr; }
  .chunk-row { grid-template-columns: 1fr 1fr; }
}

/* ── 分块参数区块 ── */
.chunk-section {
  background: #f8faff;
  border: 1px dashed #c7d2fe;
  border-radius: 6px;
  padding: 14px 16px;
  margin-top: 4px;
}
.chunk-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.toggle-chunk-btn {
  background: #e0e0e0;
  color: #666;
  border: 1px solid #d0d0d0;
  border-radius: 4px;
  padding: 4px 12px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
  font-weight: 500;
}
.toggle-chunk-btn:hover {
  background: #d0d0d0;
  border-color: #c0c0c0;
}
.toggle-chunk-btn.active {
  background: #5470c6;
  color: #fff;
  border-color: #4a63b3;
}
.toggle-chunk-btn.active:hover {
  background: #4a63b3;
  border-color: #3f558f;
}
.chunk-placeholder {
  min-height: auto;
  padding: 14px 16px;
}
.chunk-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}
.chunk-row .field label small {
  color: #9ca3af;
  font-weight: 400;
  margin-left: 2px;
}
.chunk-row input[type="number"],
.chunk-row select {
  border: 1px solid #ddd;
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 13px;
  outline: none;
  background: #fff;
}
.chunk-row input[type="number"]:focus,
.chunk-row select:focus {
  border-color: #5470c6;
}
.chunk-hint {
  margin-top: 10px;
  font-size: 12px;
  color: #6b7280;
  background: #eef2ff;
  border-radius: 4px;
  padding: 8px 10px;
  line-height: 1.5;
}
.chunk-hint code {
  background: rgba(84, 112, 198, .15);
  padding: 1px 6px;
  border-radius: 3px;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 12px;
  color: #3556a3;
}

/* ── 输入来源(文本 / 文件 Tab 切换)── */
.input-source {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fff;
  overflow: hidden;
}
.input-source-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  padding: 0 12px;
  background: #f9fafb;
  border-bottom: 1px solid #e5e7eb;
}
.input-source-tabs {
  display: flex;
  gap: 0;
}
.input-tab {
  background: transparent;
  border: none;
  padding: 10px 16px;
  font-size: 13px;
  font-weight: 500;
  color: #6b7280;
  cursor: pointer;
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  transition: all .15s;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
}
.input-tab:hover {
  color: #374151;
  background: rgba(0, 0, 0, .02);
}
.input-tab.active {
  color: #5470c6;
  border-bottom-color: #5470c6;
}
.tab-icon {
  font-size: 14px;
  line-height: 1;
}
.input-pane {
  padding: 12px;
}
.input-pane > textarea {
  width: 100%;
  min-height: 92px;
  resize: vertical;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 13px;
  line-height: 1.6;
  outline: none;
  transition: border-color .15s;
  font-family: inherit;
}
.input-pane > textarea:focus {
  border-color: #5470c6;
}
.input-pane > textarea.textarea-warn {
  border-color: #f59e0b;
}
.input-pane > textarea.textarea-over {
  border-color: #ef4444;
  background: #fff8f8;
}

/* 字符计数器 */
.text-counter {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 5px;
  font-size: 12px;
  flex-wrap: wrap;
}
.text-counter.ok   .counter-num { color: #9ca3af; }
.text-counter.warn .counter-num { color: #d97706; font-weight: 600; }
.text-counter.over .counter-num { color: #dc2626; font-weight: 700; }
.counter-msg { color: #6b7280; }
.text-counter.warn .counter-msg { color: #92400e; }
.text-counter.over .counter-msg { color: #991b1b; }
.counter-switch-btn {
  background: none;
  border: none;
  padding: 0;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  text-decoration: underline;
  color: #5470c6;
}
.counter-switch-btn:hover { color: #3556a3; }
.upload-hint {
  margin-top: 8px;
  padding: 8px 12px;
  background: #f3f4f6;
  color: #6b7280;
  border-radius: 4px;
  font-size: 12px;
  line-height: 1.5;
}
.upload-hint code {
  background: rgba(0, 0, 0, 0.05);
  padding: 1px 6px;
  border-radius: 3px;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 11px;
}

/* ── 文件上传样式(保留,被 input-pane 复用)── */
.upload-row {
  display: flex;
  align-items: stretch;
  gap: 10px;
}
.upload-dropzone {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-height: 76px;
  padding: 12px 16px;
  border: 1.5px dashed #c7d2fe;
  border-radius: 8px;
  background: #fff;
  cursor: pointer;
  transition: all 0.15s;
  text-align: center;
}
.upload-dropzone:hover {
  border-color: #818cf8;
  background: #fafbff;
}
.upload-dropzone.active {
  border-color: #3ba272;
  border-style: solid;
  background: #ecfdf5;
}
.upload-dropzone.dragging {
  border-color: #5470c6;
  border-style: solid;
  background: #eef2ff;
  transform: scale(1.01);
  box-shadow: 0 0 0 3px rgba(84, 112, 198, .18);
}
.upload-dropzone.invalid {
  border-color: #f87171;
  background: #fef2f2;
}
.upload-input-hidden {
  display: none;
}
/* Heroicons SVG 图标 */
.dropzone-svg-icon {
  width: 40px;
  height: 40px;
  stroke: #6b7280;
  flex-shrink: 0;
  transition: stroke .15s;
}
.upload-dropzone:hover .dropzone-svg-icon,
.upload-dropzone.dragging .dropzone-svg-icon {
  stroke: #5470c6;
}
.upload-dropzone.active .dropzone-svg-icon {
  stroke: #3ba272;
}
.upload-dropzone.invalid .dropzone-svg-icon {
  stroke: #f87171;
}
.dragging-icon {
  stroke: #5470c6;
  animation: bounce-up .6s ease infinite alternate;
}
@keyframes bounce-up {
  from { transform: translateY(0); }
  to   { transform: translateY(-5px); }
}
.dropzone-text {
  font-size: 14px;
  font-weight: 600;
  color: #374151;
}
.dropzone-sub {
  font-size: 12px;
  color: #9ca3af;
}
.dropzone-file {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  width: 100%;
  min-width: 0;
}
.file-name {
  font-size: 13px;
  font-weight: 600;
  color: #111827;
  font-family: 'Consolas', 'Monaco', monospace;
  word-break: break-all;
  max-width: 100%;
  overflow-wrap: anywhere;
}
.file-meta {
  display: inline-flex;
  gap: 8px;
  font-size: 11px;
  align-items: center;
}
.file-ext, .file-size {
  padding: 2px 8px;
  background: #e0e7ff;
  color: #3730a3;
  border-radius: 10px;
  font-family: 'Consolas', 'Monaco', monospace;
}
.file-ext.bad, .file-size.bad {
  background: #fee2e2;
  color: #991b1b;
}
.upload-clear-btn {
  background: #fff;
  color: #6b7280;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  padding: 0 14px;
  font-size: 12px;
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
}
.upload-clear-btn:hover {
  background: #f3f4f6;
  color: #111827;
  border-color: #9ca3af;
}
.upload-error {
  margin-top: 8px;
  padding: 8px 12px;
  background: #fef2f2;
  color: #991b1b;
  border-radius: 4px;
  font-size: 12px;
  line-height: 1.5;
}
.upload-notice {
  margin-top: 8px;
  padding: 8px 12px;
  background: #ecfdf5;
  color: #047857;
  border-radius: 4px;
  font-size: 12px;
  line-height: 1.5;
}
.upload-error code, .upload-notice code {
  background: rgba(0, 0, 0, 0.05);
  padding: 1px 6px;
  border-radius: 3px;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 11px;
}
textarea.text-dimmed {
  background: #f9fafb;
  color: #9ca3af;
}

/* ── 结束输入来源样式 ── */

/* ── 自定义 Tooltip ── */
.custom-tooltip {
  position: fixed;
  z-index: 9999;
  pointer-events: none;
  transform: translate(-50%, 0);
  animation: tooltipFadeIn 0.15s ease-out;
}

@keyframes tooltipFadeIn {
  from {
    opacity: 0;
    transform: translate(-50%, -5px);
  }
  to {
    opacity: 1;
    transform: translate(-50%, 0);
  }
}

.tooltip-content {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: #fff;
  padding: 12px 16px;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.6;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15), 0 0 0 1px rgba(255, 255, 255, 0.1);
  max-width: 320px;
  text-align: left;
  margin-top: 8px;
}

.tooltip-content strong {
  color: #ffd700;
  font-weight: 600;
}

.tooltip-content code {
  background: rgba(255, 255, 255, 0.2);
  padding: 2px 6px;
  border-radius: 3px;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 12px;
  color: #fff;
  display: inline-block;
  margin: 2px 0;
}

.tooltip-content br {
  display: block;
  content: "";
  margin: 4px 0;
}

.tooltip-arrow {
  position: absolute;
  top: -6px;
  transform: translateX(-50%);
  width: 0;
  height: 0;
  border-left: 8px solid transparent;
  border-right: 8px solid transparent;
  border-bottom: 8px solid #667eea;
}
</style>
