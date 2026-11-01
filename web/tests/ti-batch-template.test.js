/**
 * 前端单元测试: utils/tiBatch.js 的模版校验 / 种子校验 / 任务组存取
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  TEMPLATE,
  validateTemplate,
  validateSeedValue,
  makeBatchId,
  loadBatches,
  saveBatch,
  removeBatch,
  listBatchesSorted,
} from '../src/utils/tiBatch.js'

// 合法模版的便捷副本
function validObj() {
  return JSON.parse(JSON.stringify(TEMPLATE))
}

beforeEach(() => localStorage.clear())

describe('validateTemplate 合法模版', () => {
  it('官方模版通过，options/seeds 归一化返回（不含 export_on_complete）', () => {
    expect('export_on_complete' in TEMPLATE.options).toBe(false)
    const { ok, errors, normalized } = validateTemplate(validObj())
    expect(ok).toBe(true)
    expect(errors).toEqual([])
    expect(normalized.options).toEqual({
      platforms: ['threatbook', 'qianxin'],
      reuse_cache: true,
    })
    expect(normalized.seeds).toHaveLength(3)
  })

  it('旧版模版带 export_on_complete 字段 → 校验通过且被忽略', () => {
    const obj = validObj()
    obj.options.export_on_complete = true
    const { ok, normalized } = validateTemplate(obj)
    expect(ok).toBe(true)
    expect('export_on_complete' in normalized.options).toBe(false)
  })

  it('options 缺省 → platforms 全平台 / reuse_cache=true', () => {
    const obj = validObj()
    delete obj.options
    const { ok, normalized } = validateTemplate(obj)
    expect(ok).toBe(true)
    expect(normalized.options).toEqual({
      platforms: ['threatbook', 'qianxin'],
      reuse_cache: true,
    })
  })

  it('重复种子按 (seed_type, seed_value 小写) 去重', () => {
    const obj = validObj()
    obj.seeds = [
      { seed_type: 'ip', seed_value: '8.8.8.8' },
      { seed_type: 'ip', seed_value: '8.8.8.8' },
      { seed_type: 'file', seed_value: '44D88612FEA8A8F36DE82E1278ABB02F' },
      { seed_type: 'file', seed_value: '44d88612fea8a8f36de82e1278abb02f' },
    ]
    const { ok, normalized } = validateTemplate(obj)
    expect(ok).toBe(true)
    expect(normalized.seeds).toHaveLength(2)
  })
})

describe('validateTemplate 结构性错误', () => {
  it('version 错误', () => {
    const obj = validObj()
    obj.version = 2
    const { ok, errors } = validateTemplate(obj)
    expect(ok).toBe(false)
    expect(errors.some(e => e.includes('version'))).toBe(true)
  })

  it('task_type 错误', () => {
    const obj = validObj()
    obj.task_type = 'extract'
    const { ok, errors } = validateTemplate(obj)
    expect(ok).toBe(false)
    expect(errors.some(e => e.includes('task_type'))).toBe(true)
  })

  it('platforms 含非法值', () => {
    const obj = validObj()
    obj.options.platforms = ['threatbook', 'virustotal']
    const { ok, errors } = validateTemplate(obj)
    expect(ok).toBe(false)
    expect(errors.some(e => e.includes('未知平台'))).toBe(true)
  })

  it('platforms 空数组', () => {
    const obj = validObj()
    obj.options.platforms = []
    const { ok, errors } = validateTemplate(obj)
    expect(ok).toBe(false)
    expect(errors.some(e => e.includes('platforms'))).toBe(true)
  })

  it('seeds 空数组 / 超过 100 条', () => {
    const obj = validObj()
    obj.seeds = []
    expect(validateTemplate(obj).ok).toBe(false)

    const obj2 = validObj()
    obj2.seeds = Array.from({ length: 101 }, (_, i) => ({ seed_type: 'domain', seed_value: `a${i}.example.com` }))
    const { ok, errors } = validateTemplate(obj2)
    expect(ok).toBe(false)
    expect(errors.some(e => e.includes('最多'))).toBe(true)
  })
})

describe('种子值校验（validateSeedValue）', () => {
  it('IPv4：合法 / 非法（超界、前导零、格式错）', () => {
    expect(validateSeedValue('ip', '8.8.8.8')).toBeNull()
    expect(validateSeedValue('ip', '192.168.0.1')).toBeNull()
    expect(validateSeedValue('ip', '256.1.1.1')).toContain('非法 IP')
    expect(validateSeedValue('ip', '010.1.2.3')).toContain('非法 IP')
    expect(validateSeedValue('ip', '1.2.3')).toContain('非法 IP')
  })

  it('IPv6：全形态 / 压缩形态合法，乱串非法', () => {
    expect(validateSeedValue('ip', '2001:0db8:85a3:0000:0000:8a2e:0370:7334')).toBeNull()
    expect(validateSeedValue('ip', '2001:db8::1')).toBeNull()
    expect(validateSeedValue('ip', '::1')).toBeNull()
    expect(validateSeedValue('ip', '::ffff:192.0.2.1')).toBeNull()
    expect(validateSeedValue('ip', '2001:::1')).toContain('非法 IP')
  })

  it('域名：合法 / 非法', () => {
    expect(validateSeedValue('domain', 'example.com')).toBeNull()
    expect(validateSeedValue('domain', 'a-b.example.co.uk')).toBeNull()
    expect(validateSeedValue('domain', '-bad.com')).toContain('非法域名')
    expect(validateSeedValue('domain', 'no_dot')).toContain('非法域名')
    expect(validateSeedValue('domain', 'bad_domain.com')).toContain('非法域名')
  })

  it('样本哈希：MD5/SHA1/SHA256 合法，长度不符非法', () => {
    expect(validateSeedValue('file', '44d88612fea8a8f36de82e1278abb02f')).toBeNull()           // MD5 32
    expect(validateSeedValue('file', 'da39a3ee5e6b4b0d3255bfef95601890afd80709')).toBeNull()       // SHA1 40
    expect(validateSeedValue('file', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')).toBeNull() // SHA256 64
    expect(validateSeedValue('file', '44d88612fea8a8f36de82e1278abb0')).toContain('非法样本哈希')   // 30 位
    expect(validateSeedValue('file', 'zzd88612fea8a8f36de82e1278abb02f')).toContain('非法样本哈希') // 非 hex
  })

  it('validateTemplate 透传种子错误并带 seeds[i] 定位', () => {
    const obj = validObj()
    obj.seeds = [
      { seed_type: 'ip', seed_value: '8.8.8.8' },
      { seed_type: 'ip', seed_value: '999.1.1.1' },
    ]
    const { ok, errors } = validateTemplate(obj)
    expect(ok).toBe(false)
    expect(errors).toHaveLength(1)
    expect(errors[0]).toContain('seeds[1]')
    expect(errors[0]).toContain('非法 IP 地址')
  })
})

describe('任务组存取（localStorage）', () => {
  it('saveBatch / loadBatches / removeBatch 闭环', () => {
    expect(loadBatches()).toEqual({})
    const b = { batch_id: 'batch-20260811-120000-abcd', tasks: [], finished: false }
    saveBatch(b)
    expect(loadBatches()[b.batch_id].batch_id).toBe(b.batch_id)
    removeBatch(b.batch_id)
    expect(loadBatches()).toEqual({})
  })

  it('listBatchesSorted 按创建时间倒序', () => {
    saveBatch({ batch_id: 'b1', created_at: '2026-08-11T10:00:00.000Z', tasks: [] })
    saveBatch({ batch_id: 'b2', created_at: '2026-08-12T10:00:00.000Z', tasks: [] })
    saveBatch({ batch_id: 'b3', created_at: '2026-08-11T12:00:00.000Z', tasks: [] })
    expect(listBatchesSorted().map(b => b.batch_id)).toEqual(['b2', 'b3', 'b1'])
  })

  it('makeBatchId 格式 batch-YYYYMMDD-HHMMSS-xxxx', () => {
    expect(makeBatchId()).toMatch(/^batch-\d{8}-\d{6}-[a-z0-9]{4}$/)
  })
})
