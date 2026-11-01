/**
 * 前端单元测试: ChunkOverview.vue 渲染逻辑
 * 覆盖 Requirement 7.1 / 7.5 / 7.7
 */
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ChunkOverview from '../src/components/ChunkOverview.vue'

describe('ChunkOverview.vue', () => {
  it('chunkMetadata=null → 渲染"历史任务 / 未分块"占位,不渲染指标卡片', () => {
    const w = mount(ChunkOverview, { props: { chunkMetadata: null } })
    expect(w.text()).toContain('历史任务 / 未分块')
    expect(w.findAll('.metric-card').length).toBe(0)
    expect(w.find('.placeholder.legacy').exists()).toBe(true)
  })

  it('chunking_enabled=false → 渲染"本次未启用分块"提示,不渲染指标卡片', () => {
    const meta = {
      chunking_enabled: false,
      model_max_tokens: 384,
      labels_token_count: 12,
      effective_max_tokens: 356,
      overlap: 32,
      safety_margin: 16,
      threshold: 0.5,
      chunk_count: 1,
      chunks: [],
    }
    const w = mount(ChunkOverview, { props: { chunkMetadata: meta } })
    expect(w.text()).toContain('本次未启用分块')
    expect(w.findAll('.metric-card').length).toBe(0)
    expect(w.find('.placeholder.disabled').exists()).toBe(true)
  })

  it('chunking_enabled=true → 渲染 6 个指标卡片 (chunk_count/model/effective/overlap/threshold/状态)', () => {
    const meta = {
      chunking_enabled: true,
      model_max_tokens: 384,
      labels_token_count: 18,
      effective_max_tokens: 350,
      overlap: 32,
      safety_margin: 16,
      threshold: 0.5,
      chunk_count: 3,
      chunks: [],
    }
    const w = mount(ChunkOverview, { props: { chunkMetadata: meta } })
    const cards = w.findAll('.metric-card')
    expect(cards.length).toBe(6)
    const text = w.text()
    // 6 个指标都出现在页面
    expect(text).toContain('分块数')
    expect(text).toContain('3')           // chunk_count
    expect(text).toContain('384 tok')     // model_max_tokens
    expect(text).toContain('350 tok')     // effective_max_tokens
    expect(text).toContain('32 字符')     // overlap
    expect(text).toContain('0.5')         // threshold
    expect(text).toContain('已启用')
    // 不应显示占位
    expect(w.find('.placeholder').exists()).toBe(false)
  })

  it('chunking_enabled=true 但字段部分缺失 → 缺失字段显示 "—"', () => {
    const meta = {
      chunking_enabled: true,
      model_max_tokens: null,
      labels_token_count: null,
      effective_max_tokens: null,
      overlap: 0,
      safety_margin: 0,
      threshold: 0.5,
      chunk_count: 1,
      chunks: [],
    }
    const w = mount(ChunkOverview, { props: { chunkMetadata: meta } })
    const cards = w.findAll('.metric-card')
    expect(cards.length).toBe(6)
    const text = w.text()
    expect(text).toContain('—')
  })
})
