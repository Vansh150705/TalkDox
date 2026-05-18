import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import axios from 'axios'

const API = import.meta.env.VITE_API_URL || ''

export default function Upload() {
  const navigate = useNavigate()
  const [activeTab, setActiveTab] = useState('pdf')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [files, setFiles] = useState([])
  const [url, setUrl] = useState('')
  const [ytUrl, setYtUrl] = useState('')
  const [videoId, setVideoId] = useState('')
  const [dragOver, setDragOver] = useState(false)

  const extractVideoId = (url) => {
    const m = url.match(/(?:v=|youtu\.be\/|embed\/|shorts\/)([a-zA-Z0-9_-]{11})/)
    return m ? m[1] : null
  }

  const handleYtUrlChange = (val) => {
    setYtUrl(val)
    setVideoId(extractVideoId(val) || '')
  }

  const handleDrop = (e) => {
    e.preventDefault()
    setDragOver(false)
    const dropped = [...e.dataTransfer.files].filter(f => f.type === 'application/pdf')
    if (dropped.length) setFiles(dropped)
  }

  const handlePDF = async () => {
    if (!files.length) return
    setLoading(true); setError('')
    try {
      const fd = new FormData()
      files.forEach(f => fd.append('files', f))
      await axios.post(`${API}/api/upload/pdf`, fd)
      navigate('/chat')
    } catch(e) {
      setError(e.response?.data?.error || 'Upload failed.')
    } finally { setLoading(false) }
  }

  const handleWeb = async () => {
    if (!url) return
    setLoading(true); setError('')
    try {
      const fd = new FormData()
      fd.append('url', url)
      await axios.post(`${API}/api/upload/web`, fd)
      navigate('/chat')
    } catch(e) {
      setError(e.response?.data?.error || 'Failed to scrape page.')
    } finally { setLoading(false) }
  }

  const handleYT = async () => {
    if (!ytUrl) return
    setLoading(true); setError('')
    try {
      const fd = new FormData()
      fd.append('url', ytUrl)
      await axios.post(`${API}/api/upload/youtube`, fd)
      navigate('/chat')
    } catch(e) {
      setError(e.response?.data?.error || 'Failed to fetch transcript.')
    } finally { setLoading(false) }
  }

  const tabs = [
    { id: 'pdf', icon: '📄', label: 'Paige', sub: 'PDF Upload' },
    { id: 'web', icon: '🌐', label: 'Webb', sub: 'Website URL' },
    { id: 'youtube', icon: '▶', label: 'Yuki', sub: 'YouTube Video' },
  ]

  return (
    <>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Syne:wght@400;500;600;700;800&family=DM+Sans:wght@300;400;500;600&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: 'DM Sans', sans-serif; }

        .upload-page {
          min-height: 100vh;
          background: #0a0a0a;
          display: grid;
          grid-template-columns: 1fr 1fr;
          position: relative;
          overflow: hidden;
        }

        /* Left panel */
        .left-panel {
          display: flex;
          flex-direction: column;
          justify-content: center;
          padding: 60px;
          position: relative;
          z-index: 2;
        }
        .back-btn {
          position: fixed;
          top: 24px; left: 24px;
          background: rgba(255,255,255,0.05);
          border: 1px solid rgba(255,255,255,0.1);
          border-radius: 100px;
          padding: 8px 18px;
          font-size: 13px;
          font-weight: 500;
          color: rgba(255,255,255,0.6);
          cursor: pointer;
          font-family: 'DM Sans', sans-serif;
          transition: all 0.2s;
          z-index: 100;
        }
        .back-btn:hover { background: rgba(255,255,255,0.1); color: white; }

        .left-logo {
          font-family: 'Syne', sans-serif;
          font-weight: 800;
          font-size: 24px;
          color: white;
          margin-bottom: 60px;
        }

        .left-heading {
          font-family: 'Syne', sans-serif;
          font-size: 56px;
          font-weight: 800;
          color: white;
          line-height: 1.05;
          letter-spacing: -0.04em;
          margin-bottom: 20px;
        }
        .left-heading em {
          font-style: italic;
          font-weight: 300;
          color: rgba(255,255,255,0.5);
        }

        .left-sub {
          font-size: 16px;
          color: rgba(255,255,255,0.4);
          line-height: 1.7;
          max-width: 380px;
          margin-bottom: 48px;
        }

        .feature-pills {
          display: flex;
          flex-direction: column;
          gap: 12px;
        }
        .feature-pill {
          display: flex;
          align-items: center;
          gap: 12px;
          font-size: 14px;
          color: rgba(255,255,255,0.5);
        }
        .pill-icon {
          width: 32px; height: 32px;
          background: rgba(255,255,255,0.05);
          border: 1px solid rgba(255,255,255,0.08);
          border-radius: 8px;
          display: flex; align-items: center; justify-content: center;
          font-size: 14px;
          flex-shrink: 0;
        }

        /* Grid decoration */
        .grid-bg {
          position: absolute;
          inset: 0;
          background-image:
            linear-gradient(rgba(255,255,255,0.03) 1px, transparent 1px),
            linear-gradient(90deg, rgba(255,255,255,0.03) 1px, transparent 1px);
          background-size: 48px 48px;
        }
        .glow {
          position: absolute;
          bottom: -200px; left: -200px;
          width: 600px; height: 600px;
          background: radial-gradient(circle, rgba(99,102,241,0.15) 0%, transparent 70%);
          pointer-events: none;
        }

        /* Right panel */
        .right-panel {
          display: flex;
          align-items: center;
          justify-content: center;
          padding: 40px;
          position: relative;
          z-index: 2;
        }

        .upload-card {
          background: #ffffff;
          border-radius: 24px;
          padding: 36px;
          width: 100%;
          max-width: 480px;
          box-shadow: 0 40px 120px rgba(0,0,0,0.5);
        }

        /* Tab switcher */
        .tab-switcher {
          display: grid;
          grid-template-columns: 1fr 1fr 1fr;
          gap: 6px;
          background: #f0f0f0;
          border-radius: 14px;
          padding: 4px;
          margin-bottom: 28px;
        }
        .tab-btn {
          border: none;
          border-radius: 10px;
          padding: 10px 6px;
          font-family: 'DM Sans', sans-serif;
          font-size: 13px;
          font-weight: 600;
          cursor: pointer;
          transition: all 0.2s;
          display: flex;
          flex-direction: column;
          align-items: center;
          gap: 3px;
          background: transparent;
          color: #a0a0a0;
        }
        .tab-btn.active {
          background: #ffffff;
          color: #0a0a0a;
          box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .tab-btn span { font-size: 18px; }

        /* Upload area */
        .upload-title {
          font-family: 'Syne', sans-serif;
          font-size: 22px;
          font-weight: 700;
          color: #0a0a0a;
          margin-bottom: 6px;
        }
        .upload-sub {
          font-size: 14px;
          color: #a0a0a0;
          margin-bottom: 20px;
        }

        .drop-zone {
          border: 2px dashed #e2e2e2;
          border-radius: 16px;
          padding: 32px 20px;
          text-align: center;
          cursor: pointer;
          transition: all 0.2s;
          background: #fafafa;
          margin-bottom: 14px;
        }
        .drop-zone:hover, .drop-zone.drag-over {
          border-color: #0a0a0a;
          background: #f0f0f0;
        }
        .drop-icon {
          font-size: 32px;
          margin-bottom: 10px;
          display: block;
        }
        .drop-text {
          font-size: 14px;
          font-weight: 500;
          color: #374151;
        }
        .drop-hint {
          font-size: 12px;
          color: #a0a0a0;
          margin-top: 4px;
        }

        .file-tag {
          background: #f0fdf4;
          border: 1px solid #bbf7d0;
          border-radius: 10px;
          padding: 10px 14px;
          font-size: 13px;
          color: #15803d;
          margin-bottom: 8px;
          display: flex;
          align-items: center;
          gap: 8px;
          font-weight: 500;
        }

        .url-input {
          width: 100%;
          border: 1.5px solid #e2e2e2;
          border-radius: 12px;
          padding: 14px 16px;
          font-size: 15px;
          font-family: 'DM Sans', sans-serif;
          color: #0a0a0a;
          outline: none;
          transition: border-color 0.2s;
          margin-bottom: 14px;
          background: #fafafa;
        }
        .url-input:focus { border-color: #0a0a0a; background: #fff; }
        .url-input::placeholder { color: #c0c0c0; }

        .yt-thumb {
          width: 100%;
          border-radius: 12px;
          overflow: hidden;
          border: 1px solid #e2e2e2;
          margin-bottom: 14px;
          position: relative;
        }
        .yt-thumb img { width: 100%; display: block; }
        .yt-play {
          position: absolute;
          top: 50%; left: 50%;
          transform: translate(-50%, -50%);
          width: 48px; height: 48px;
          background: rgba(0,0,0,0.7);
          border-radius: 50%;
          display: flex; align-items: center; justify-content: center;
          font-size: 18px;
          color: white;
        }

        .submit-btn {
          width: 100%;
          background: #0a0a0a;
          color: white;
          border: none;
          border-radius: 12px;
          padding: 16px;
          font-size: 15px;
          font-weight: 600;
          font-family: 'DM Sans', sans-serif;
          cursor: pointer;
          transition: all 0.2s;
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 8px;
        }
        .submit-btn:hover { background: #333; transform: translateY(-1px); box-shadow: 0 8px 24px rgba(0,0,0,0.15); }
        .submit-btn:disabled { background: #e2e2e2; color: #a0a0a0; cursor: not-allowed; transform: none; box-shadow: none; }

        .error-box {
          background: #fef2f2;
          border: 1px solid #fca5a5;
          border-radius: 10px;
          padding: 10px 14px;
          font-size: 13px;
          color: #dc2626;
          margin-top: 12px;
        }

        .loading-row {
          display: flex;
          align-items: center;
          gap: 8px;
          margin-top: 12px;
          font-size: 13px;
          color: #a0a0a0;
        }
        .loading-dots { display: flex; gap: 3px; }
        .loading-dots span {
          width: 5px; height: 5px;
          background: #a0a0a0;
          border-radius: 50%;
          display: block;
          animation: ldot 1.2s ease-in-out infinite;
        }
        .loading-dots span:nth-child(2) { animation-delay: 0.2s; }
        .loading-dots span:nth-child(3) { animation-delay: 0.4s; }
        @keyframes ldot { 0%,80%,100%{opacity:0.3;transform:scale(0.8)} 40%{opacity:1;transform:scale(1.1)} }

        /* Divider */
        .divider {
          display: flex;
          align-items: center;
          gap: 10px;
          margin: 16px 0;
          font-size: 12px;
          color: #c0c0c0;
        }
        .divider::before, .divider::after {
          content: '';
          flex: 1;
          height: 1px;
          background: #e2e2e2;
        }

        @media (max-width: 768px) {
          .upload-page { grid-template-columns: 1fr; }
          .left-panel { display: none; }
          .right-panel { padding: 24px; }
        }
      `}</style>

      <div className="upload-page">
        <div className="grid-bg" />
        <div className="glow" />

        {/* Back button */}
        <button className="back-btn" onClick={() => navigate('/')}>← Back</button>

        {/* Left Panel */}
        <div className="left-panel">
          <div className="left-logo">TalkDox 🧠</div>
          <h1 className="left-heading">
            Your AI<br/>
            <em>reading</em><br/>
            assistant.
          </h1>
          <p className="left-sub">
            Upload a PDF, paste a URL, or drop a YouTube link.
            TalkDox understands it and lets you have a full conversation with it.
          </p>
          <div className="feature-pills">
            {[
              { icon: '🤖', text: '3 Agentic AI layers — Clarification, Reflection, Confidence' },
              { icon: '🧬', text: 'Document DNA fingerprinting on upload' },
              { icon: '⚡', text: 'Powered by Google Gemini 2.5 Flash' },
              { icon: '🔒', text: 'Your content is never stored permanently' },
            ].map(f => (
              <div key={f.text} className="feature-pill">
                <div className="pill-icon">{f.icon}</div>
                <span>{f.text}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Right Panel */}
        <div className="right-panel">
          <div className="upload-card">

            {/* Tab switcher */}
            <div className="tab-switcher">
              {tabs.map(tab => (
                <button
                  key={tab.id}
                  className={`tab-btn ${activeTab === tab.id ? 'active' : ''}`}
                  onClick={() => { setActiveTab(tab.id); setError('') }}
                >
                  <span>{tab.icon}</span>
                  {tab.label}
                </button>
              ))}
            </div>

            {/* PDF */}
            {activeTab === 'pdf' && (
              <div>
                <div className="upload-title">Upload PDF</div>
                <div className="upload-sub">Drop one or more PDF files to chat with them.</div>
                <label
                  className={`drop-zone ${dragOver ? 'drag-over' : ''}`}
                  onDragOver={e => { e.preventDefault(); setDragOver(true) }}
                  onDragLeave={() => setDragOver(false)}
                  onDrop={handleDrop}
                >
                  <input type="file" accept=".pdf" multiple style={{ display: 'none' }} onChange={e => setFiles([...e.target.files])} />
                  <span className="drop-icon">📄</span>
                  <div className="drop-text">Click to upload or drag & drop</div>
                  <div className="drop-hint">PDF files only · Max 50MB</div>
                </label>
                {[...files].map(f => (
                  <div key={f.name} className="file-tag">✅ {f.name}</div>
                ))}
                <button className="submit-btn" onClick={handlePDF} disabled={!files.length || loading}>
                  {loading ? 'Processing...' : '⚡ Process & Index PDF'}
                </button>
              </div>
            )}

            {/* Web */}
            {activeTab === 'web' && (
              <div>
                <div className="upload-title">Paste Website URL</div>
                <div className="upload-sub">Any article, blog, documentation, or website.</div>
                <input
                  className="url-input"
                  value={url}
                  onChange={e => setUrl(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter' && url) handleWeb() }}
                  placeholder="https://example.com/article"
                />
                {url && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14, padding: '8px 12px', background: '#f8f8f8', borderRadius: 10, border: '1px solid #e2e2e2' }}>
                    <span style={{ fontSize: 16 }}>🌐</span>
                    <span style={{ fontSize: 12, color: '#5a5a5a', wordBreak: 'break-all' }}>{url.slice(0, 50)}{url.length > 50 ? '...' : ''}</span>
                  </div>
                )}
                <button className="submit-btn" onClick={handleWeb} disabled={!url || loading}>
                  {loading ? 'Scraping...' : '🌐 Scrape & Index Page'}
                </button>
              </div>
            )}

            {/* YouTube */}
            {activeTab === 'youtube' && (
              <div>
                <div className="upload-title">Paste YouTube Link</div>
                <div className="upload-sub">Lectures, tutorials, interviews — any video with captions.</div>
                <input
                  className="url-input"
                  value={ytUrl}
                  onChange={e => handleYtUrlChange(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter' && ytUrl) handleYT() }}
                  placeholder="https://www.youtube.com/watch?v=..."
                />
                {videoId && (
                  <div className="yt-thumb">
                    <img src={`https://img.youtube.com/vi/${videoId}/hqdefault.jpg`} alt="thumbnail" />
                    <div className="yt-play">▶</div>
                  </div>
                )}
                <button className="submit-btn" onClick={handleYT} disabled={!ytUrl || loading}>
                  {loading ? 'Fetching transcript...' : '▶ Extract Transcript & Index'}
                </button>
              </div>
            )}

            {/* Error */}
            {error && <div className="error-box">❌ {error}</div>}

            {/* Loading */}
            {loading && (
              <div className="loading-row">
                <div className="loading-dots">
                  <span/><span/><span/>
                </div>
                Indexing your content with Gemini...
              </div>
            )}

            {/* Footer */}
            <div style={{ marginTop: 24, paddingTop: 20, borderTop: '1px solid #f0f0f0', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6 }}>
              <span style={{ fontSize: 11, color: '#c0c0c0' }}>Powered by</span>
              <span style={{ fontSize: 11, fontWeight: 600, color: '#a0a0a0' }}>Google Gemini 2.5 Flash</span>
              <span style={{ fontSize: 11, color: '#c0c0c0' }}>·</span>
              <span style={{ fontSize: 11, color: '#c0c0c0' }}>FAISS Vector Search</span>
            </div>
          </div>
        </div>
      </div>
    </>
  )
}