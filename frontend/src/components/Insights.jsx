import { useEffect, useState } from 'react'
import { useWorkspace } from '../context/ctx'
import { errorMessage, http } from '../api'

const OUTCOME = { answered: 'Answered with citations', no_evidence: 'Not in sources', unverified: 'Could not verify', partial: 'Partly answered', clarify: 'Needed clarification', meta: 'Source info' }

export default function Insights() {
  const ws = useWorkspace()
  const [a, setA] = useState(null)
  const [err, setErr] = useState('')
  const [confirm, setConfirm] = useState(false)
  useEffect(() => { http.get('/api/analytics').then(r => setA(r.data)).catch(e => setErr(errorMessage(e))) }, [])
  const max = a ? Math.max(1, ...Object.values(a.outcomes || {})) : 1
  const wipe = async () => { try { await ws.deleteEverything(); setConfirm(false); window.location.assign('/upload') } catch (e) { setErr(errorMessage(e)) } }
  return (
    <div className="insights">
      <h2>Session insights</h2>
      {err && <div className="alert alert-error">{err}</div>}
      {a && (a.questions === 0 ? <p className="muted">Ask a few questions and your answer quality statistics will appear here.</p> : (
        <>
          <div className="stats">
            <div><b>{a.questions}</b><span>questions</span></div>
            <div><b>{a.avg_confidence ?? '—'}</b><span>avg evidence score</span></div>
            <div><b>{a.avg_latency_ms ? (a.avg_latency_ms / 1000).toFixed(1) + 's' : '—'}</b><span>avg response time</span></div>
            <div><b>{a.flagged_answers}</b><span>answers with flagged checks</span></div>
          </div>
          <h3>How questions were handled</h3>
          {Object.entries(a.outcomes).map(([k, v]) => (
            <div key={k} className="barrow"><span>{OUTCOME[k] || k}</span><div className="bartrack"><div style={{ width: `${(v / max) * 100}%` }} /></div><b>{v}</b></div>
          ))}
          {a.top_terms.length > 0 && <><h3>Topics you asked about</h3><div className="chips">{a.top_terms.map(([t, n]) => <span key={t} className="chip static">{t} · {n}</span>)}</div></>}
        </>
      ))}

      <h2>Privacy &amp; data</h2>
      <p className="muted">Your workspace is private to this browser (a random token is stored locally). Nothing is shared between workspaces.
        {ws.config?.privacy?.retention_days ? ` Workspaces unused for ${ws.config.privacy.retention_days} days are deleted automatically.` : ''}</p>
      <h3>What is sent to external AI providers</h3>
      <ul className="plain">{(ws.config?.privacy?.sent_to || []).map(p => <li key={p.provider + p.purpose}><b>{p.provider}</b> — {p.purpose}: {p.data}.</li>)}</ul>
      <p className="muted">Uploaded files are not stored; only extracted text, search vectors and your chat history are kept until you delete them.</p>
      {!confirm ? <button type="button" className="btn danger" onClick={() => setConfirm(true)}>Delete all my data</button> : (
        <div className="alert alert-error">This permanently deletes every source, search index and conversation in this workspace.
          <div className="row"><button type="button" className="btn danger" onClick={wipe}>Yes, delete everything</button><button type="button" className="btn" onClick={() => setConfirm(false)}>Cancel</button></div></div>
      )}
    </div>
  )
}
