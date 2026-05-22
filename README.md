# 🧠 TalkDox

> Chat with any **PDF**, **Website**, or **YouTube video** — powered by Google Gemini 2.5 Flash.  
> Agentic RAG with 12+ AI features in one unified intelligence layer.

[![Live Demo](https://img.shields.io/badge/Live_Demo-talkdox--ai.vercel.app-black?style=for-the-badge)](https://talkdox-ai.vercel.app)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)

---

## 🎯 What is TalkDox?

TalkDox transforms any document, webpage, or video into an intelligent conversation partner. Upload a PDF, paste a URL, or drop a YouTube link - TalkDox reads, understands, and lets you have a full AI conversation with it.

Unlike basic chatbots, TalkDox uses a **3-layer Agentic RAG pipeline** with self-reflection and confidence scoring, plus 12+ built-in AI tools.

---

## ✨ Features

### 📂 3 Source Types
- **📄 PDF Reader** - Research papers, contracts, textbooks, reports
- **🌐 Web Surfer** - Any website URL, full-page scraping
- **▶️ YouTube Extractor** - Auto-transcribes any video

### 🤖 12 AI Capabilities

| Feature | What It Does |
|---------|-------------|
| 💬 Smart Q&A | Grounded answers with source citations |
| 🧬 Document DNA | Auto-profile: domain, tone, complexity, themes |
| 🔀 Doc vs Doc Compare | Side-by-side compare with colour-coded sources |
| 🕐 Timeline Extractor | Auto-extract dates and milestones |
| 🃏 Smart Flashcards | Study cards with difficulty filters |
| 🛠 5 Automation Tools | Summary, Quiz, Email, Contradictions, Actions |
| 🤖 3 Agentic Layers | Clarification → Self-Reflection → Confidence |
| 📊 Session Analytics | Track confidence trends and patterns |
| ⚖️ AI Personas | Lawyer, Doctor, Teacher, Analyst, Journalist |
| 🎯 Response Modes | ELI5, Executive Brief, Devil's Advocate |
| 🌐 Multilingual | Auto-detects language, responds in kind |
| 🎙 Voice Input | Hands-free via Web Speech API |

---

## 🧠 Agentic RAG Pipeline

```
Query → Clarification Layer → Retrieval (FAISS) → Self-Reflection → Confidence Score → Response
```

1. **Clarification** - Detects ambiguous queries and asks for specifics
2. **Retrieval** - Embeds + searches FAISS for top-k relevant chunks
3. **Self-Reflection** - Critiques its own answer and refines if needed
4. **Confidence** - Outputs final answer with 0–100% confidence score

---

## 🛠 Tech Stack

**Frontend** • React 18 (Vite) • DM Sans + Syne • Web Speech API • Vercel  
**Backend** • Python 3.12 • FastAPI • LangChain • FAISS • HuggingFace Spaces (Docker)  
**AI** • Google Gemini 2.5 Flash • gemini-embedding-001  
**Integrations** • Supadata API (YouTube) • BeautifulSoup (web scraping) • PyPDF

---

## 📁 Project Structure

```
TalkDox/
├── backend/
│   ├── main.py              # FastAPI entry point
│   ├── utils/
│   │   ├── agents.py        # Agentic RAG layers
│   │   ├── ai_helpers.py    # Gemini calls, embeddings
│   │   ├── pdf_processor.py # PDF/web/YouTube ingestion
│   │   └── styles.py        # Response formatting
│   ├── Dockerfile
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   │   ├── Landing.jsx  # Landing page
│   │   │   ├── Upload.jsx   # Source upload
│   │   │   └── Chat.jsx     # Chat interface
│   │   ├── App.jsx
│   │   └── main.jsx
│   ├── vercel.json
│   └── package.json
└── README.md
```

---

## 👨‍💻 Author

**Vansh Mahajan**

- 🌐 Website: https://vansh-portfolio-mauve.vercel.app/
- 💼 LinkedIn: https://www.linkedin.com/in/vansh-mahajan-napv/
- 📧 Email: vansh150705@gmail.com
- 🐙 GitHub: https://github.com/Vansh150705

---

## 📄 License

MIT License - free to use, modify, and share.

---

## 🌟 Show Your Support

If TalkDox helped you:
- ⭐ Star this repo
- 🔀 Share with friends
- 🐛 Report bugs in Issues

Built with 💙 by Vansh Mahajan