import { useState } from 'react'
import { useWorkspace } from '../context/ctx'
import { KIND_ICON, STATUS_LABEL, fmtTs, isBusy, isReady, plural, sourceMeta } from '../format'

const TIPS = {
  needs_js: 'This page builds its content with JavaScript. Try printing it to PDF and uploading that, or ask the server admin to enable JS rendering.',
  scanned_no_ocr: 'This PDF is a scan. The server needs OCR (tesseract or a Gemini key) to read it.',
  robots_disallowed: 'The site asks automated tools not to read this page, and TalkDox respects that.',
  blocked_by_site: 'The site blocks automated access. Save the page as PDF and upload it instead.',
  login_required: 'This page needs a login, which TalkDox can’t use. Save it as PDF and upload it instead.',
  paywalled: 'This page is behind a paywall.',
  blocked_by_youtube: 'YouTube is refusing transcript requests from this server. The admin can configure a transcript fallback (see docs).',
  captions_disabled: 'The uploader turned captions off, so there is no transcript to read.',
  password_protected: 'Remove the password in the original app, then upload again.',
  password_protected_pdf: 'Remove the password in the original app, then upload again.',
}

function Report({ s }) {
  const d = s.diagnostics || {}
  const rows = []
  if (d.pages_total != null) rows.push(`${d.pages_with_text ?? '?'} of ${d.pages_total} pages contain readable text`)
  if (d.pages_ocr?.length) rows.push(`${plural(d.pages_ocr.length, 'page')} read with OCR (${d.ocr_engine})`)
  if (d.tables_found) rows.push(`${plural(d.tables_found, 'table')} kept as tables`)
  if (d.sheets) rows.push(`Sheets: ${d.sheets.map(x => `${x.name} (${plural(x.rows, 'row')})`).join(', ')}`)
  if (d.provider) rows.push(`Transcript source: ${d.provider}${d.language ? `, language ${d.language}` : ''}${d.chapters ? `, ${plural(d.chapters, 'chapter')}` : ''}`)
  if (d.covered_s) rows.push(`Transcript covers ${fmtTs(d.covered_s)}${d.duration_s ? ` of ${fmtTs(d.duration_s)}` : ''}`)
  if (d.chunk_kinds) rows.push(`Indexed ${Object.entries(d.chunk_kinds).map(([k, v]) => `${v} ${k}`).join(', ')} passages`)
  if (d.embeddings_reused) rows.push(`${d.embeddings_reused} unchanged passages reused (no re-embedding)`)
  if (d.changed_since_last_index === false) rows.push('Content unchanged since the last index')
  return (
    <div className="report">
      {rows.map((r, i) => <div key={i}>• {r}</div>)}
      {(s.warnings || []).map((w, i) => <div key={'w' + i} className="warn-line">⚠ {w}</div>)}
      {d.pages && d.pages.length > 1 && (
        <details><summary>{d.pages_ok} of {d.pages.length} pages read</summary>
          <ul className="pagelist">{d.pages.map((p, i) => (
            <li key={i} className={p.status}>{p.status === 'ok' ? '✓' : p.status === 'skipped' ? '↷' : '✕'} <span title={p.url}>{(p.title || p.url || '').slice(0, 70)}</span>{p.message ? ` — ${p.message}` : ''}</li>
          ))}</ul>
        </details>
      )}
    </div>
  )
}

export default function SourcesPanel({ onAdd, onOpen, onClose }) {
  const ws = useWorkspace()
  const [open, setOpen] = useState({})
  const [err, setErr] = useState('')
  const act = async (fn) => { setErr(''); try { await fn() } catch (e) { setErr(ws.errorMessage(e)) } }

  return (
    <div className="sources">
      <div className="sources-head">
        <h2>Sources</h2>
        <div className="row">
          <button type="button" className="btn small primary" onClick={onAdd}>+ Add</button>
          {onClose && <button type="button" className="icon-btn show-mobile" onClick={onClose} aria-label="Close sources">✕</button>}
        </div>
      </div>
      {ws.sources.length === 0 && <p className="muted pad">No sources yet. Add a document, website or YouTube video to start.</p>}
      {err && <div className="alert alert-error">{err}</div>}
      <ul className="srclist">
        {ws.sources.map(s => {
          const ready = isReady(s), busy = isBusy(s)
          return (
            <li key={s.id} className={`src ${s.status}`}>
              <div className="src-top">
                <input type="checkbox" disabled={!ready} checked={ws.selected.has(s.id)} onChange={() => ws.toggle(s.id)}
                       aria-label={`Include ${s.name} in questions`} />
                <span className="src-icon" aria-hidden="true">{KIND_ICON[s.kind]}</span>
                <button type="button" className="src-name" title={s.name} disabled={!ready} onClick={() => onOpen({ source_id: s.id })}>{s.name}</button>
              </div>
              <div className="src-meta">
                <span className={`badge ${s.status}`}>{STATUS_LABEL[s.status]}</span>
                <span>{sourceMeta(s)}</span>
              </div>
              {busy && (
                <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round((s.job?.progress || 0) * 100)}>
                  <div className="bar" style={{ width: `${Math.max(4, Math.round((s.job?.progress || 0) * 100))}%` }} />
                  <span className="progress-msg" aria-live="polite">{s.job?.message || 'Queued…'}</span>
                </div>
              )}
              {s.status === 'failed' && (
                <div className="src-error">
                  <strong>{s.error || 'Processing failed.'}</strong>
                  {TIPS[s.error_code] && <div>{TIPS[s.error_code]}</div>}
                </div>
              )}
              {s.status === 'partial' && s.error && <div className="src-error soft">{s.error}</div>}
              <div className="src-actions">
                {(s.warnings?.length > 0 || ready) && (
                  <button type="button" className="link-btn" onClick={() => setOpen(o => ({ ...o, [s.id]: !o[s.id] }))} aria-expanded={!!open[s.id]}>
                    {s.warnings?.length ? `⚠ ${plural(s.warnings.length, 'note')} · ` : ''}Extraction report
                  </button>
                )}
                {busy && s.job && <button type="button" className="link-btn" onClick={() => act(() => ws.cancelJob(s.job.id))}>Cancel</button>}
                {ready && s.kind !== 'document' && <button type="button" className="link-btn" onClick={() => act(() => ws.refreshSource(s.id))}>Refresh</button>}
                {s.kind === 'youtube' && s.meta?.video_id && <a className="link-btn" href={`https://www.youtube.com/watch?v=${s.meta.video_id}`} target="_blank" rel="noopener noreferrer">Open ↗</a>}
                {s.kind === 'web' && s.uri && <a className="link-btn" href={s.uri} target="_blank" rel="noopener noreferrer">Open ↗</a>}
                <button type="button" className="link-btn danger" onClick={() => act(() => ws.removeSource(s.id))}>Remove</button>
              </div>
              {open[s.id] && <Report s={s} />}
            </li>
          )
        })}
      </ul>
      {ws.selectedReady.length > 0 && <div className="scope-note">Questions use {plural(ws.selectedReady.length, 'selected source')}.</div>}
    </div>
  )
}
