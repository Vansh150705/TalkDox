import { useMemo, useRef, useState } from 'react'
import { useWorkspace } from '../context/ctx'
import { errorMessage } from '../api'
import { fmtBytes, plural } from '../format'

const YT_ID = /^[A-Za-z0-9_-]{11}$/
function ytId(raw) {
  try {
    const s = raw.trim()
    if (YT_ID.test(s)) return s
    const u = new URL(/^https?:\/\//i.test(s) ? s : `https://${s}`)
    const host = u.hostname.replace(/^www\./, '').replace(/^m\./, '').replace(/^music\./, '')
    if (!['youtube.com', 'youtu.be', 'youtube-nocookie.com'].includes(host)) return null
    const parts = u.pathname.split('/').filter(Boolean)
    const id = host === 'youtu.be' ? parts[0] : ['shorts', 'embed', 'live', 'v'].includes(parts[0]) ? parts[1] : u.searchParams.get('v')
    return id && YT_ID.test(id) ? id : null
  } catch { return null }
}
const isPlaylist = (raw) => { try { const u = new URL(/^https?:\/\//i.test(raw) ? raw : `https://${raw}`); return !ytId(raw) && !!u.searchParams.get('list') } catch { return false } }

export default function AddSources({ onDone, compact = false }) {
  const ws = useWorkspace()
  const [tab, setTab] = useState('files')
  const [files, setFiles] = useState([])
  const [drag, setDrag] = useState(false)
  const [urls, setUrls] = useState('')
  const [crawl, setCrawl] = useState(false)
  const [depth, setDepth] = useState(1)
  const [maxPages, setMaxPages] = useState(20)
  const [prefix, setPrefix] = useState('')
  const [combine, setCombine] = useState(false)
  const [yt, setYt] = useState('')
  const [lang, setLang] = useState('')
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState(0)
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')
  const inputRef = useRef(null)

  const limits = ws.config?.limits || {}
  const maxBytes = (limits.max_upload_mb || 25) * 1024 * 1024
  const accept = (ws.config?.formats || []).join(',')
  const vid = useMemo(() => ytId(yt), [yt])
  const urlList = useMemo(() => urls.split(/[\s,]+/).map(u => u.trim()).filter(Boolean), [urls])

  const addPicked = (list) => {
    const arr = [...list]
    setFiles(prev => [...prev, ...arr.filter(f => !prev.some(p => p.name === f.name && p.size === f.size))].slice(0, 10))
    setError('')
  }

  const report = (results, what) => {
    const bad = results.filter(r => r.error)
    const dup = results.filter(r => r.duplicate)
    const ok = results.filter(r => !r.error)
    const msgs = []
    if (dup.length) msgs.push(`${plural(dup.length, what)} already in your workspace — reusing the existing copy.`)
    if (bad.length) setError(bad.map(b => `${b.filename || 'Item'}: ${b.error}`).join(' '))
    if (msgs.length) setInfo(msgs.join(' '))
    return ok.length
  }

  const submit = async (fn, what) => {
    setBusy(true); setError(''); setInfo(''); setProgress(0)
    try {
      const results = await fn()
      const n = report(results, what)
      if (n) onDone?.(results)
    } catch (e) {
      setError(errorMessage(e))
    } finally { setBusy(false) }
  }

  const uploadFiles = () => submit(async () => {
    const r = await ws.addFiles(files, setProgress)
    setFiles([])
    return r
  }, 'file')

  const addWeb = () => submit(() => ws.addWeb({
    urls: urlList.map(u => (/^https?:\/\//i.test(u) ? u : `https://${u}`)), crawl, max_depth: crawl ? depth : 0,
    max_pages: crawl ? maxPages : 1, path_prefix: crawl && prefix ? prefix : null, combine,
  }).then(r => { setUrls(''); return r }), 'page')

  const addYt = () => submit(() => ws.addYouTube(yt.trim(), lang ? [lang.trim()] : []).then(r => { setYt(''); return r }), 'video')

  const tooBig = files.filter(f => f.size > maxBytes)
  const tabs = [['files', '📄', 'Files'], ['web', '🌐', 'Websites'], ['youtube', '▶️', 'YouTube']]

  return (
    <div className={`add ${compact ? 'compact' : ''}`}>
      <div role="tablist" aria-label="Source type" className="tabs">
        {tabs.map(([id, icon, label]) => (
          <button key={id} role="tab" aria-selected={tab === id} className={`tab ${tab === id ? 'on' : ''}`}
                  onClick={() => { setTab(id); setError(''); setInfo('') }}>
            <span aria-hidden="true">{icon}</span> {label}
          </button>
        ))}
      </div>

      {tab === 'files' && (
        <div role="tabpanel">
          <div className={`drop ${drag ? 'over' : ''}`} onDragOver={e => { e.preventDefault(); setDrag(true) }}
               onDragLeave={() => setDrag(false)} onDrop={e => { e.preventDefault(); setDrag(false); addPicked(e.dataTransfer.files) }}
               onClick={() => inputRef.current?.click()} role="button" tabIndex={0}
               onKeyDown={e => (e.key === 'Enter' || e.key === ' ') && inputRef.current?.click()}
               aria-label="Choose files to upload">
            <div className="drop-icon" aria-hidden="true">⬆</div>
            <div className="drop-title">Drop files here or click to browse</div>
            <div className="drop-sub">PDF, Word, PowerPoint, Excel, CSV, Markdown, text, HTML · up to {limits.max_upload_mb || 25} MB each · scanned PDFs are read with OCR when the server supports it</div>
            <input ref={inputRef} type="file" multiple hidden accept={accept} onChange={e => { addPicked(e.target.files); e.target.value = '' }} />
          </div>
          {files.length > 0 && (
            <ul className="filelist">
              {files.map(f => (
                <li key={f.name + f.size} className={f.size > maxBytes ? 'bad' : ''}>
                  <span className="fname">{f.name}</span><span className="fsize">{fmtBytes(f.size)}</span>
                  <button type="button" className="icon-btn" aria-label={`Remove ${f.name}`} onClick={() => setFiles(files.filter(x => x !== f))}>✕</button>
                </li>
              ))}
            </ul>
          )}
          {tooBig.length > 0 && <div className="alert alert-error">{tooBig.map(f => f.name).join(', ')} exceeds the {limits.max_upload_mb || 25} MB limit.</div>}
          <button type="button" className="btn primary" disabled={!files.length || busy || tooBig.length > 0} onClick={uploadFiles}>
            {busy ? `Uploading… ${Math.round(progress * 100)}%` : `Add ${files.length ? plural(files.length, 'file') : 'files'}`}
          </button>
        </div>
      )}

      {tab === 'web' && (
        <div role="tabpanel">
          <label className="field-label" htmlFor="urls">Web page addresses (one per line)</label>
          <textarea id="urls" className="input" rows={compact ? 3 : 4} placeholder={'https://example.com/docs/getting-started\nhttps://blog.example.com/post'}
                    value={urls} onChange={e => setUrls(e.target.value)} />
          <label className="check"><input type="checkbox" checked={crawl} onChange={e => setCrawl(e.target.checked)} /> Also read linked pages on the same site (documentation / multi-page sites)</label>
          {crawl && (
            <div className="grid3">
              <label>Link depth<select className="input" value={depth} onChange={e => setDepth(+e.target.value)}>{[1, 2, 3].filter(d => d <= (limits.crawl_max_depth || 2)).map(d => <option key={d}>{d}</option>)}</select></label>
              <label>Max pages<input className="input" type="number" min={1} max={limits.crawl_max_pages || 50} value={maxPages} onChange={e => setMaxPages(Math.min(+e.target.value || 1, limits.crawl_max_pages || 50))} /></label>
              <label>Only paths starting with<input className="input" placeholder="/docs/" value={prefix} onChange={e => setPrefix(e.target.value)} /></label>
            </div>
          )}
          {urlList.length > 1 && !crawl && <label className="check"><input type="checkbox" checked={combine} onChange={e => setCombine(e.target.checked)} /> Combine these pages into one source</label>}
          <p className="hint">TalkDox identifies itself, obeys robots.txt, and can't read pages behind logins, paywalls or CAPTCHAs. Pages that only render with JavaScript {ws.config?.features?.js_render ? 'are rendered in a sandboxed browser' : 'need server-side rendering, which is not enabled on this server'}.</p>
          <button type="button" className="btn primary" disabled={!urlList.length || busy} onClick={addWeb}>{busy ? 'Checking…' : crawl ? 'Read site' : `Add ${plural(urlList.length || 1, 'page')}`}</button>
        </div>
      )}

      {tab === 'youtube' && (
        <div role="tabpanel">
          <label className="field-label" htmlFor="yt">YouTube video or playlist link</label>
          <input id="yt" className="input" placeholder="https://www.youtube.com/watch?v=…" value={yt} onChange={e => setYt(e.target.value)} />
          {yt && !vid && !isPlaylist(yt) && <div className="hint warn">That doesn't look like a YouTube link.</div>}
          {vid && <div className="yt-preview"><img alt="" referrerPolicy="no-referrer" src={`https://i.ytimg.com/vi/${vid}/mqdefault.jpg`} /><span>Video ID <code>{vid}</code></span></div>}
          {isPlaylist(yt) && <div className="hint">Playlist detected — up to {ws.config?.limits?.youtube_max_playlist_videos || 25} videos will be added as separate sources.</div>}
          <label className="field-label" htmlFor="ytlang">Preferred caption language (optional)</label>
          <input id="ytlang" className="input short" placeholder="en" value={lang} maxLength={10} onChange={e => setLang(e.target.value)} />
          <p className="hint">TalkDox reads the video's <strong>captions/transcript</strong>, not the picture. Answers link to the exact moment. Videos with captions disabled, private, age-restricted or live can't be processed — you'll see the specific reason.</p>
          <button type="button" className="btn primary" disabled={(!vid && !isPlaylist(yt)) || busy} onClick={addYt}>{busy ? 'Checking…' : 'Add video'}</button>
        </div>
      )}

      <div aria-live="polite">
        {error && <div className="alert alert-error">{error}</div>}
        {info && <div className="alert alert-info">{info}</div>}
      </div>
    </div>
  )
}
