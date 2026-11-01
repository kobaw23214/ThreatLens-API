/**
 * 前端单元测试: colorFor / CHUNK_PALETTE 的纯性与周期性
 * 覆盖 Design Property 13,Requirement 8.5。
 */
import { describe, it, expect } from 'vitest'
import { colorFor, CHUNK_PALETTE, MISSING_CHUNK_COLOR } from '../src/utils/chunkColors.js'

describe('colorFor (Property 13 — 纯性与周期性)', () => {
  it('同一 chunk_index 多次调用返回同一字符串 (纯性)', () => {
    for (let i = 0; i < 200; i++) {
      const ci = (i * 7) % 50
      const a = colorFor(ci)
      const b = colorFor(ci)
      const c = colorFor(ci)
      expect(a).toBe(b)
      expect(b).toBe(c)
    }
  })

  it('按调色板长度周期性: colorFor(i) === colorFor(i + palette.length)', () => {
    for (let i = 0; i < 1000; i++) {
      expect(colorFor(i)).toBe(colorFor(i + CHUNK_PALETTE.length))
    }
  })

  it('每种返回值都在 CHUNK_PALETTE 里 (缺失除外)', () => {
    for (let i = 0; i < 1000; i++) {
      const c = colorFor(i)
      expect(CHUNK_PALETTE).toContain(c)
    }
  })

  it('null / undefined / NaN / Infinity 返回中性灰', () => {
    for (const bad of [null, undefined, NaN, Infinity, -Infinity]) {
      expect(colorFor(bad)).toBe(MISSING_CHUNK_COLOR)
    }
  })

  it('负数 chunk_index 依然稳定周期性', () => {
    // colorFor(-1) 必须等于 colorFor(-1 + len) 等于 colorFor(-1 + 2*len)
    const len = CHUNK_PALETTE.length
    expect(colorFor(-1)).toBe(colorFor(-1 + len))
    expect(colorFor(-1)).toBe(colorFor(-1 + 2 * len))
    expect(colorFor(-5)).toBe(colorFor(-5 + len))
  })

  it('调色板包含 10 个预定义颜色且每个为有效 hex', () => {
    expect(CHUNK_PALETTE).toHaveLength(10)
    for (const c of CHUNK_PALETTE) {
      expect(c).toMatch(/^#[0-9a-fA-F]{6}$/)
    }
  })
})
