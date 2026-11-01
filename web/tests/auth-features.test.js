/**
 * 前端单元测试: utils/auth.js 的功能模块开关（getFeatures）
 * - 缓存缺失 / 字段不完整 / JSON 损坏时缺省视为全部开启
 * - fetchAuthConfig 正确缓存 features 字段
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { getFeatures, fetchAuthConfig } from '../src/utils/auth.js'

beforeEach(() => {
  sessionStorage.clear()
  vi.unstubAllGlobals()
})

describe('getFeatures', () => {
  it('无缓存（旧后端未下发 features）→ 缺省三个全开', () => {
    expect(getFeatures()).toEqual({ ner: true, attack: true, ti: true })
  })

  it('缓存为 false 值 → 对应功能关闭', () => {
    sessionStorage.setItem('ner_api_features', JSON.stringify({ ner: true, attack: false, ti: true }))
    expect(getFeatures()).toEqual({ ner: true, attack: false, ti: true })
  })

  it('缓存字段不完整 → 缺失的字段缺省为开启', () => {
    sessionStorage.setItem('ner_api_features', JSON.stringify({ attack: false }))
    expect(getFeatures()).toEqual({ ner: true, attack: false, ti: true })
  })

  it('缓存 JSON 损坏 → 缺省三个全开', () => {
    sessionStorage.setItem('ner_api_features', '{bad json')
    expect(getFeatures()).toEqual({ ner: true, attack: true, ti: true })
  })
})

describe('fetchAuthConfig 缓存 features', () => {
  function mockConfig(data) {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      json: async () => data,
    })))
  }

  it('响应含 features → 写入缓存，getFeatures 可读到', async () => {
    mockConfig({ auth_enabled: false, features: { ner: false, attack: true, ti: true } })
    await fetchAuthConfig()
    expect(getFeatures()).toEqual({ ner: false, attack: true, ti: true })
  })

  it('响应不含 features（旧后端）→ 清除旧缓存，getFeatures 缺省全开', async () => {
    sessionStorage.setItem('ner_api_features', JSON.stringify({ ner: false, attack: false, ti: false }))
    mockConfig({ auth_enabled: false })
    await fetchAuthConfig()
    expect(getFeatures()).toEqual({ ner: true, attack: true, ti: true })
  })
})
