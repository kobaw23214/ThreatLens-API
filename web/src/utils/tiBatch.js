/**
 * TI 联网解析批量任务：模版 / 校验 / 批次（任务组）存取 / SQL 导出（纯逻辑，便于单测）
 * - 模版 JSON：version + task_type + options + seeds，用户下载后填写再上传
 * - 批次（任务组）记录存 localStorage（key: ti_batches），供队列状态页后台恢复跟踪
 */
import { authFetch } from './auth.js'

// ─── 种子校验（与后端 api/ti_expand/schemas.py 口径一致）─────────────────────

const DOMAIN_RE = /^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}$/
const HASH_RE = /^([a-fA-F0-9]{32}|[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$/
// IPv6 全形态（压缩 / 内嵌 IPv4 / 链路本地 zone）
const IPV6_RE = /^(([0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,7}:|([0-9a-fA-F]{1,4}:){1,6}:[0-9a-fA-F]{1,4}|([0-9a-fA-F]{1,4}:){1,5}(:[0-9a-fA-F]{1,4}){1,2}|([0-9a-fA-F]{1,4}:){1,4}(:[0-9a-fA-F]{1,4}){1,3}|([0-9a-fA-F]{1,4}:){1,3}(:[0-9a-fA-F]{1,4}){1,4}|([0-9a-fA-F]{1,4}:){1,2}(:[0-9a-fA-F]{1,4}){1,5}|[0-9a-fA-F]{1,4}:((:[0-9a-fA-F]{1,4}){1,6})|:((:[0-9a-fA-F]{1,4}){1,7}|:)|fe80:(:[0-9a-fA-F]{0,4}){0,4}%[0-9a-zA-Z]+|::(ffff(:0{1,4})?:)?((25[0-5]|(2[0-4]|1?\d)?\d)\.){3}(25[0-5]|(2[0-4]|1?\d)?\d)|([0-9a-fA-F]{1,4}:){1,4}:((25[0-5]|(2[0-4]|1?\d)?\d)\.){3}(25[0-5]|(2[0-4]|1?\d)?\d))$/

// 与后端 ipaddress 一致：前导零（如 010.1.2.3）视为非法
export function isIPv4(v) {
  const parts = v.split('.')
  if (parts.length !== 4) return false
  return parts.every(p => /^\d{1,3}$/.test(p) && (p === '0' || !p.startsWith('0')) && Number(p) <= 255)
}

export function isIP(v) {
  return isIPv4(v) || IPV6_RE.test(v)
}

export function isDomain(v) {
  return DOMAIN_RE.test(v)
}

export function isFileHash(v) {
  return HASH_RE.test(v)
}

/** 按类型校验种子值，合法返回 null，非法返回中文错误信息 */
export function validateSeedValue(seedType, seedValue) {
  if (seedType === 'ip' && !isIP(seedValue)) return `非法 IP 地址: ${seedValue}`
  if (seedType === 'domain' && !isDomain(seedValue)) return `非法域名: ${seedValue}`
  if (seedType === 'file' && !isFileHash(seedValue)) return `非法样本哈希（支持 MD5/SHA1/SHA256）: ${seedValue}`
  return null
}

// ─── 模版 ────────────────────────────────────────────────────────────────────

export const ALL_PLATFORMS = ['threatbook', 'qianxin']
export const SEED_TYPES = ['ip', 'domain', 'file']
export const MAX_SEEDS = 100

export const TEMPLATE = {
  version: 1,
  task_type: 'ti_expand',
  options: {
    platforms: ['threatbook', 'qianxin'],
    reuse_cache: true,
  },
  seeds: [
    { seed_type: 'ip', seed_value: '8.8.8.8' },
    { seed_type: 'domain', seed_value: 'example.com' },
    { seed_type: 'file', seed_value: '44d88612fea8a8f36de82e1278abb02f' },
  ],
}

/** 通用 JSON 文件下载（Blob → a[download] → revoke） */
export function downloadJson(obj, filename) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export function downloadTemplate() {
  downloadJson(TEMPLATE, 'ti_batch_template.json')
}

/**
 * 校验上传的批量任务文件。
 * @returns {{ ok: boolean, errors: string[], normalized: null | { options: object, seeds: Array } }}
 * normalized.options：platforms（缺省全平台）/ reuse_cache（缺省 true）；
 * 旧版模版里的 export_on_complete 字段直接忽略（不报错、不归一化）
 * normalized.seeds：按 (seed_type, seed_value 小写) 去重后的种子列表
 */
export function validateTemplate(obj) {
  const errors = []

  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
    return { ok: false, errors: ['文件必须是 JSON 对象'], normalized: null }
  }

  if (obj.version !== 1) errors.push('version: 必须为 1')
  if (obj.task_type !== 'ti_expand') errors.push('task_type: 必须为 "ti_expand"')

  // ── options ──
  const rawOpts = obj.options ?? {}
  if (typeof rawOpts !== 'object' || Array.isArray(rawOpts)) {
    errors.push('options: 必须是对象')
  }
  const opts = (typeof rawOpts === 'object' && !Array.isArray(rawOpts)) ? rawOpts : {}

  let platforms = [...ALL_PLATFORMS]
  if (opts.platforms !== undefined) {
    if (!Array.isArray(opts.platforms) || opts.platforms.length === 0) {
      errors.push('options.platforms: 必须是非空数组（可选 threatbook / qianxin）')
    } else {
      platforms = []
      opts.platforms.forEach((p, i) => {
        const v = String(p).trim().toLowerCase()
        if (!ALL_PLATFORMS.includes(v)) {
          errors.push(`options.platforms[${i}]: 未知平台 "${p}"（可选 threatbook / qianxin）`)
        } else if (!platforms.includes(v)) {
          platforms.push(v)
        }
      })
      if (!platforms.length && !errors.some(e => e.startsWith('options.platforms'))) {
        errors.push('options.platforms: 不能为空数组')
      }
    }
  }

  let reuseCache = true
  if (opts.reuse_cache !== undefined) {
    if (typeof opts.reuse_cache !== 'boolean') errors.push('options.reuse_cache: 必须是布尔值')
    else reuseCache = opts.reuse_cache
  }

  // ── seeds ──
  const seeds = []
  if (!Array.isArray(obj.seeds)) {
    errors.push('seeds: 必须是数组')
  } else if (obj.seeds.length === 0) {
    errors.push('seeds: 不能为空数组')
  } else if (obj.seeds.length > MAX_SEEDS) {
    errors.push(`seeds: 最多 ${MAX_SEEDS} 条（当前 ${obj.seeds.length} 条）`)
  } else {
    const seen = new Set()
    obj.seeds.forEach((s, i) => {
      if (!s || typeof s !== 'object') {
        errors.push(`seeds[${i}]: 必须是对象 {seed_type, seed_value}`)
        return
      }
      const seedType = String(s.seed_type ?? '').trim().toLowerCase()
      if (!SEED_TYPES.includes(seedType)) {
        errors.push(`seeds[${i}]: seed_type 必须是 ip / domain / file`)
        return
      }
      const seedValue = String(s.seed_value ?? '').trim()
      if (!seedValue || seedValue.length > 256) {
        errors.push(`seeds[${i}]: seed_value 不能为空且不超过 256 字符`)
        return
      }
      const valueErr = validateSeedValue(seedType, seedValue)
      if (valueErr) {
        errors.push(`seeds[${i}]: ${valueErr}`)
        return
      }
      // 按 (seed_type, seed_value 小写) 去重，保留首次出现
      const key = `${seedType}|${seedValue.toLowerCase()}`
      if (!seen.has(key)) {
        seen.add(key)
        seeds.push({ seed_type: seedType, seed_value: seedValue })
      }
    })
  }

  if (errors.length) return { ok: false, errors, normalized: null }
  return {
    ok: true,
    errors: [],
    normalized: {
      options: { platforms, reuse_cache: reuseCache },
      seeds,
    },
  }
}

// ─── 批次 ID ─────────────────────────────────────────────────────────────────

/** batch-YYYYMMDD-HHMMSS-xxxx（4 位随机） */
export function makeBatchId() {
  const d = new Date()
  const p = (n, w = 2) => String(n).padStart(w, '0')
  const stamp = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`
  const rand = Math.random().toString(36).slice(2, 6).padEnd(4, '0')
  return `batch-${stamp}-${rand}`
}

// ─── 批次存取（localStorage）─────────────────────────────────────────────────

const STORAGE_KEY = 'ti_batches'

/** @returns {Object<string, object>} batch_id → batch 记录 */
export function loadBatches() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    const map = raw ? JSON.parse(raw) : {}
    return (map && typeof map === 'object' && !Array.isArray(map)) ? map : {}
  } catch {
    return {}
  }
}

function writeBatches(map) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(map))
  } catch (e) {
    console.warn('保存批次记录失败:', e)
  }
}

export function saveBatch(batch) {
  const map = loadBatches()
  map[batch.batch_id] = batch
  writeBatches(map)
}

export function removeBatch(batchId) {
  const map = loadBatches()
  delete map[batchId]
  writeBatches(map)
}

/** 任务组列表：按创建时间倒序 */
export function listBatchesSorted() {
  return Object.values(loadBatches())
    .filter(b => b && b.batch_id)
    .sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))
}

// ─── SQL 导出（GET /ti/export?batch_id=...，管理员接口）───────────────────────

/**
 * 下载该任务组的 SQL 备份文件（只含本组任务）。
 * 文件名优先取响应 Content-Disposition，缺省 ti_export_{batch_id}.sql。
 * 失败抛出中文错误（403 仅管理员可导出；401 由 authFetch 自动登出）。
 */
export async function exportBatchSql(batch) {
  const res = await authFetch(`/ti/export?batch_id=${encodeURIComponent(batch.batch_id)}`)
  if (!res.ok) {
    if (res.status === 403) throw new Error('仅管理员可导出 SQL')
    const data = await res.json().catch(() => ({}))
    throw new Error(data.detail || `导出失败（HTTP ${res.status}）`)
  }
  const cd = res.headers.get('Content-Disposition') || ''
  const m = cd.match(/filename\*?=(?:UTF-8''|")?([^";]+)/i)
  const filename = m ? decodeURIComponent(m[1].trim()) : `ti_export_${batch.batch_id}.sql`
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}
