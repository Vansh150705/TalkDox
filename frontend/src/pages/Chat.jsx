import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import ChatPane from '../components/ChatPane'
import StudyTools from '../components/StudyTools'
import Insights from '../components/Insights'
import SourcesPanel from '../components/SourcesPanel'
import AddSources from '../components/AddSources'
import ConnectionGate from '../components/ConnectionGate'
import Onboarding from '../components/Onboarding'
import { SourceViewer } from '../components/Citations'
import { errorMessage, http } from '../api'

const TABS = [['chat', '💬', 'Chat'], ['tools', '🛠', 'Study tools'], ['insights', '📊', 'Insights']]

function Inner() {
  const nav = useNavigate()
  const [tab, setTab] = useState('chat')
  const [viewer, setViewer] = useState(null)
  const [adding, setAdding] = useState(false)
  const [sidebar, setSidebar] = useState(false)
  const [convs, setConvs] = useState([])
  const [convId, setConvId] = useState(null)
  const [help, setHelp] = useState(false)

  const loadConvs = useCallback(() => http.get('/api/conversations').then(r => setConvs(r.data.conversations)).catch(() => {}), [])
  useEffect(() => { loadConvs() }, [loadConvs])

  const delConv = async (id) => {
    try { await http.delete(`/api/conversations/${id}`); if (id === convId) setConvId(null); loadConvs() } catch (e) { alert(errorMessage(e)) }
  }

  const openCitation = (c) => setViewer({ source_id: c.source_id, chunk_id: c.chunk_id, highlight: c.highlight, page: c.chunk_id ? null : c.page })

  return (
    <div className={`workspace ${sidebar ? 'sidebar-open' : ''}`}>
      <aside className="sidebar" aria-label="Sources and history">
        <div className="logo side" onClick={() => nav('/')} role="link" tabIndex={0} onKeyDown={e => e.key === 'Enter' && nav('/')}>TalkDox 🧠</div>
        <SourcesPanel onAdd={() => setAdding(true)} onOpen={openCitation} onClose={() => setSidebar(false)} />
        <div className="history">
          <h2>History</h2>
          {convs.length === 0 && <p className="muted pad">Your past chats appear here.</p>}
          <ul>
            {convs.map(c => (
              <li key={c.id} className={c.id === convId ? 'on' : ''}>
                <button type="button" className="hist-item" onClick={() => { setConvId(c.id); setTab('chat'); setSidebar(false) }} title={c.title}>{c.title}</button>
                <button type="button" className="icon-btn" aria-label={`Delete chat ${c.title}`} onClick={() => delConv(c.id)}>🗑</button>
              </li>
            ))}
          </ul>
        </div>
        <button type="button" className="link-btn pad" onClick={() => setHelp(true)}>How TalkDox works</button>
      </aside>

      <section className="main">
        <header className="main-head">
          <button type="button" className="icon-btn show-mobile" onClick={() => setSidebar(true)} aria-label="Open sources and history">☰</button>
          <div role="tablist" aria-label="Workspace sections" className="tabs head">
            {TABS.map(([id, icon, label]) => (
              <button key={id} role="tab" aria-selected={tab === id} className={`tab ${tab === id ? 'on' : ''}`} onClick={() => setTab(id)}>
                <span aria-hidden="true">{icon}</span> {label}
              </button>
            ))}
          </div>
        </header>
        <div className="main-body">
          {tab === 'chat' && <ChatPane onOpen={openCitation} conversationId={convId} setConversationId={setConvId} onConversationChange={loadConvs} />}
          {tab === 'tools' && <div className="scroll-pad"><StudyTools onOpen={openCitation} /></div>}
          {tab === 'insights' && <div className="scroll-pad"><Insights /></div>}
        </div>
      </section>

      {sidebar && <button type="button" className="scrim" aria-label="Close sidebar" onClick={() => setSidebar(false)} />}
      {viewer && <SourceViewer key={`${viewer.source_id}:${viewer.chunk_id || viewer.page || ''}`} target={viewer} onClose={() => setViewer(null)} />}
      {adding && (
        <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Add sources">
          <div className="modal">
            <div className="row between"><h2>Add sources</h2><button type="button" className="icon-btn" aria-label="Close" onClick={() => setAdding(false)}>✕</button></div>
            <AddSources compact onDone={() => setAdding(false)} />
          </div>
        </div>
      )}
      <Onboarding force={help} onClose={() => setHelp(false)} />
    </div>
  )
}

export default function Chat() { return <ConnectionGate><Inner /></ConnectionGate> }
