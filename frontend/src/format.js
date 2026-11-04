export function fmtTs(sec) {
  if (sec == null) return ''
  const s = Math.floor(sec), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(x).padStart(2, '0')}` : `${m}:${String(x).padStart(2, '0')}`
}
export const fmtBytes = (n) => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1048576).toFixed(1)} MB`
export const plural = (n, a, b = a + 's') => `${n} ${n === 1 ? a : b}`
export const KIND_ICON = { document: '📄', web: '🌐', youtube: '▶️' }
export const STATUS_LABEL = { queued: 'Queued', processing: 'Processing', completed: 'Ready', partial: 'Ready (partial)', failed: 'Failed', cancelled: 'Cancelled' }
export const isBusy = (s) => s.status === 'queued' || s.status === 'processing'
export const isReady = (s) => (s.status === 'completed' || s.status === 'partial') && s.n_chunks > 0

export function sourceMeta(s) {
  const bits = []
  if (s.kind === 'youtube') {
    if (s.meta?.channel) bits.push(s.meta.channel)
    if (s.meta?.duration) bits.push(fmtTs(s.meta.duration))
    if (s.meta?.transcript_kind) bits.push({ manual: 'captions', generated: 'auto captions', model_generated: 'AI-generated transcript', unknown: 'transcript' }[s.meta.transcript_kind] || 'transcript')
  } else if (s.kind === 'web') {
    if (s.n_pages) bits.push(plural(s.n_pages, 'page'))
  } else if (s.n_pages) {
    bits.push(plural(s.n_pages, { pptx: 'slide', xlsx: 'sheet', csv: 'table' }[s.meta?.format] || 'page'))
  }
  if (s.n_chunks) bits.push(plural(s.n_chunks, 'passage'))
  return bits.join(' · ')
}
