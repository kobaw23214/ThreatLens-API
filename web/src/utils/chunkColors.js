/**
 * chunk_index 稳定颜色映射 (long-text-chunking-ner, Requirement 8.5)
 *
 * 同一 chunk_index 在「实体 chips / 实体表 chunk 列 / Chunk 明细表」三处应呈现相同颜色。
 * colorFor 是纯函数:
 *   - 多次调用同一 index 返回同一值
 *   - 超出调色板长度时按周期环绕 (colorFor(i) === colorFor(i + CHUNK_PALETTE.length))
 *   - null / undefined / 非有限数 → 中性灰 '#f5f5f5'
 *
 * 对应 Design Property 13。
 */
export const CHUNK_PALETTE = [
  '#e0f2fe', '#fce7f3', '#dcfce7', '#fef3c7', '#ede9fe',
  '#ffedd5', '#cffafe', '#fee2e2', '#e0e7ff', '#f5f5f4',
]

export const MISSING_CHUNK_COLOR = '#f5f5f5'

export function colorFor(chunkIndex) {
  if (chunkIndex == null || !Number.isFinite(chunkIndex)) {
    return MISSING_CHUNK_COLOR
  }
  const len = CHUNK_PALETTE.length
  const idx = ((chunkIndex % len) + len) % len
  return CHUNK_PALETTE[idx]
}
