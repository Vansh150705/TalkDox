import { useNavigate } from 'react-router-dom'
import AddSources from '../components/AddSources'
import SourcesPanel from '../components/SourcesPanel'
import Onboarding from '../components/Onboarding'
import ConnectionGate from '../components/ConnectionGate'
import { useWorkspace } from '../context/ctx'
import { isBusy, isReady } from '../format'
import { SourceViewer } from '../components/Citations'
import { useState } from 'react'

function Inner() {
  const ws = useWorkspace()
  const nav = useNavigate()
  const [viewer, setViewer] = useState(null)
  const anyReady = ws.sources.some(isReady)
  const anyBusy = ws.sources.some(isBusy)
  return (
    <div className="page-shell">
      <header className="topbar">
        <button type="button" className="pill" onClick={() => nav('/')}>← Home</button>
        <div className="logo">TalkDox 🧠</div>
        <span className="spacer" />
        {anyReady && <button type="button" className="btn primary" onClick={() => nav('/chat')}>Start chatting →</button>}
      </header>
      <main className="upload-main">
        <section className="card">
          <h1>Add your sources</h1>
          <p className="muted">Documents, websites and YouTube videos all end up in one workspace you can question together.</p>
          <AddSources />
        </section>
        <section className="card">
          <SourcesPanel onAdd={() => document.getElementById('urls')?.focus()} onOpen={setViewer} />
          {anyBusy && <p className="hint" aria-live="polite">You can start chatting as soon as the first source shows “Ready”.</p>}
          {anyReady && <button type="button" className="btn primary wide" onClick={() => nav('/chat')}>Start chatting →</button>}
        </section>
      </main>
      {viewer && <SourceViewer key={`${viewer.source_id}:${viewer.chunk_id || viewer.page || ''}`} target={viewer} onClose={() => setViewer(null)} />}
      <Onboarding />
    </div>
  )
}

export default function Upload() { return <ConnectionGate><Inner /></ConnectionGate> }
