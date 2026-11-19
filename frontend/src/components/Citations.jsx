import { useEffect, useRef, useState } from 'react'
import { errorMessage, http } from '../api'
import { KIND_ICON, fmtTs, sourceMeta } from '../format'

const FLAG_TEXT = {
  uncited_claim: 'No citation',
  unsupported_claim: 'Not found in cited passage',
  weak_support: 'Only partly supported',
  numeric_mismatch: 'Number not found in cited passage',
  quote_unverified: 'Quote could not be verified',
  invalid_citation_removed: 'Invalid citation removed',
}

export function ConfidenceBadge({ conf }) {
  if (!conf || conf.level === 'none') return null
  const f = conf.factors || {}
  const title = `Evidence strength (not a probability). Retrieval relevance ${f.retrieval_relevance ?? '-'}, ` +
    `citation support ${f.citation_support ?? '-'}, unsupported sentences ${f.unsupported_sentences ?? 0}, uncited ${f.uncited_sentences ?? 0}.`
  return (
    <span className={`conf conf-${conf.level}`} title={title}>
      <span className="conf-dot" aria-hidden="true" />
      {conf.level === 'high' ? 'Well supported' : conf.level === 'medium' ? 'Partly supported' : 'Weak evidence'} · {conf.score}
    </span>
  )
}

// Flags that mean "part of this answer is not backed by the cited text" get a prominent, pre-expanded warning;
// softer flags (missing citation, partial support) stay behind the collapsed link.
const SERIOUS_FLAGS = new Set(['numeric_mismatch', 'unsupported_claim'])

export function Flags({ flags }) {
  const shown = flags || []
  const serious = shown.some(f => SERIOUS_FLAGS.has(f.type))
  const [open, setOpen] = useState(serious)
  if (!shown.length) return null
  const numeric = shown.some(f => f.type === 'numeric_mismatch')
  return (
    <div className={`flags${serious ? ' flags-serious' : ''}`} role={serious ? 'alert' : 'note'}>
      <button type="button" className="link-btn" onClick={() => setOpen(o => !o)} aria-expanded={open}>
        {serious
          ? `⚠ Please verify: ${numeric ? 'a number in this answer was not found in the cited passages' : 'part of this answer is not supported by the cited passages'}`
          : `⚠ ${shown.length} check${shown.length > 1 ? 's' : ''} flagged on this answer`} {open ? '▲' : '▼'}
      </button>
      {open && (
        <ul>
          {shown.map((f, i) => (
            <li key={i}><strong>{FLAG_TEXT[f.type] || f.type}</strong>{f.sentence ? `: “${f.sentence.replace(/\[S\d+\]/g, '').trim().slice(0, 160)}”` : ''}{f.detail && !f.sentence ? ` — ${f.detail}` : ''}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function CitationCard({ c, index, onOpen }) {
  const isVideo = c.source_kind === 'youtube'
  return (
    <div className="cite-card" id={`cite-${index}`}>
      <div className="cite-head">
        <span className="cite-num">{index}</span>
        <span className="cite-src" title={c.source_name}>{KIND_ICON[c.source_kind] || '📄'} {c.source_name}</span>
        {c.deep_link ? (
          <a className="cite-loc" href={c.deep_link} target="_blank" rel="noopener noreferrer"
             title={isVideo ? 'Open the video at this moment on YouTube' : 'Open the original page'}>
            {isVideo ? '▶ ' : '↗ '}{c.label || 'open'}
          </a>
        ) : c.label ? <span className="cite-loc plain">{c.label}</span> : null}
      </div>
      <p className="cite-snippet">“{c.snippet}”</p>
      <div className="cite-foot">
        {c.quote_verified && <span className="ok-tag" title="The quoted words were found verbatim in this passage">✓ quote verified</span>}
        {c.suspicious && <span className="warn-tag" title="This passage contains text that looks like instructions to an AI. It was treated as untrusted data.">⚠ contains instruction-like text</span>}
        <button type="button" className="link-btn" onClick={() => onOpen?.(c)}>View in source</button>
      </div>
    </div>
  )
}

export function RelatedList({ items, onOpen }) {
  if (!items?.length) return null
  return (
    <div className="related">
      <div className="related-title">Closest passages found (not used as evidence)</div>
      {items.map(c => (
        <button type="button" key={c.id} className="related-item" onClick={() => onOpen?.(c)}>
          <span className="related-src">{KIND_ICON[c.source_kind] || '📄'} {c.source_name}{c.label ? ` · ${c.label}` : ''}</span>
          <span className="related-snip">{c.snippet}</span>
        </button>
      ))}
    </div>
  )
}

/** Side drawer showing the exact extracted text the AI saw, scrolled to the cited passage. */
export function SourceViewer({ target, onClose }) {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const focusRef = useRef(null)

  useEffect(() => {
    let cancelled = false
    const params = target.chunk_id ? { chunk_id: target.chunk_id } : target.page ? { page: target.page } : { limit_: 20 }
    http.get(`/api/sources/${target.source_id}/content`, { params })
      .then(r => { if (!cancelled) setData(r.data) })
      .catch(e => { if (!cancelled) setErr(errorMessage(e)) })
    return () => { cancelled = true }
  }, [target])

  useEffect(() => { focusRef.current?.scrollIntoView({ block: 'center' }) }, [data])
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const render = (ch) => {
    const hl = ch.id === target.chunk_id && target.highlight
    if (hl) {
      return <>{ch.text.slice(0, hl[0])}<mark>{ch.text.slice(hl[0], hl[1])}</mark>{ch.text.slice(hl[1])}</>
    }
    return ch.text
  }

  return (
    <aside className="viewer" role="dialog" aria-label="Source viewer">
      <header className="viewer-head">
        <div>
          <div className="viewer-title">{data ? `${KIND_ICON[data.source.kind]} ${data.source.name}` : 'Loading…'}</div>
          {data && <div className="viewer-sub">{sourceMeta(data.source)}</div>}
        </div>
        <button type="button" className="icon-btn" onClick={onClose} aria-label="Close source viewer">✕</button>
      </header>
      <div className="viewer-note">This is the extracted text TalkDox indexed — exactly what the AI could read. If something looks missing or garbled here, the answer cannot use it.</div>
      <div className="viewer-body">
        {err && <div className="alert alert-error">{err}</div>}
        {!data && !err && <div className="skeleton" />}
        {data?.chunks.map(ch => (
          <section key={ch.id} ref={ch.id === target.chunk_id ? focusRef : null}
                   className={`passage ${ch.id === target.chunk_id ? 'focus' : ''} ${ch.suspicious ? 'suspicious' : ''}`}>
            <div className="passage-label">
              {ch.label || ch.heading || `Passage ${ch.ord + 1}`}
              {ch.t_start != null && data.source.meta?.video_id && (
                <a href={`https://www.youtube.com/watch?v=${data.source.meta.video_id}&t=${Math.floor(ch.t_start)}s`} target="_blank"
                   rel="noopener noreferrer"> ▶ {fmtTs(ch.t_start)}</a>
              )}
              {ch.url && <a href={ch.url} target="_blank" rel="noopener noreferrer"> ↗ page</a>}
            </div>
            <pre className="passage-text">{render(ch)}</pre>
          </section>
        ))}
        {data && !data.chunks.length && <p className="muted">No passages on that page.</p>}
      </div>
    </aside>
  )
}
