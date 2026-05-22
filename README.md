# 🧠 TalkDox

> Chat with any **PDF**, **Website**, or **YouTube video** — powered by Google Gemini 2.5 Flash.  
> Agentic RAG with 12+ AI features in one unified intelligence layer.

🌐 **Live Demo:** [talkdox-ai.vercel.app](https://talkdox-ai.vercel.app)  
⭐ **Star this repo** if you like it!

---

## ✨ Features

### 📂 3 Source Types
- **📄 PDF Reader** — Upload any PDF (research papers, contracts, textbooks)
- **🌐 Web Surfer** — Paste any website URL, scrapes the entire page
- **▶️ YouTube Extractor** — Drop any YouTube link, pulls the transcript

### 🤖 12 AI Capabilities
| Feature | Description |
|---------|-------------|
| 💬 Smart Q&A | Ask anything, grounded answers from your source |
| 🧬 Document DNA | Auto-profile any source — domain, tone, complexity |
| 🔀 Doc vs Doc Compare | Dual vector retrieval with colour-coded sources |
| 🕐 Timeline Extractor | Visualise every date, deadline, milestone |
| 🃏 Smart Flashcards | Auto-generate study cards with difficulty levels |
| 🛠 5 Automation Tools | Summary, Quiz, Email, Contradictions, Actions |
| 🤖 3 Agentic Layers | Clarification, Self-Reflection, Confidence |
| 📊 Session Analytics | Track confidence trends and keyword patterns |
| ⚖️ AI Personas | Chat as Lawyer, Doctor, Teacher, or Analyst |
| 🎯 Response Modes | ELI5, Executive Brief, or Devil's Advocate |
| 🌐 Multilingual | Auto-detects language, responds in same one |
| 🎙 Voice Input | Speak your questions using browser speech API |

---

## 🛠 Tech Stack

### Frontend
- **React 18** (Vite)
- **DM Sans** + **Syne** typography
- **react-router-dom** for routing
- **axios** for API calls
- **Web Speech API** for voice input
- **Vercel** for deployment

### Backend
- **Python 3.12**
- **FastAPI** + **Uvicorn**
- **LangChain** for orchestration
- **Google Gemini 2.5 Flash** (LLM)
- **gemini-embedding-001** (embeddings)
- **FAISS** for vector search
- **PyPDF** for PDF processing
- **BeautifulSoup** for web scraping
- **Supadata API** for YouTube transcripts
- **HuggingFace Spaces** + Docker for deployment

---

### 🧠 Agentic RAG Pipeline
1. **Clarification Layer** — Detects ambiguous queries, asks for specifics
2. **Retrieval Layer** — Embeds + searches FAISS for top-k relevant chunks
3. **Self-Reflection Layer** — Critiques its own answer, refines if needed
4. **Confidence Layer** — Outputs final answer with confidence score (0-100%)

---


## 📁 Project Structure

TalkDox/
├── backend/
│   ├── main.py                    # FastAPI entry point
│   ├── utils/
│   │   ├── agents.py              # Agentic RAG layers
│   │   ├── ai_helpers.py          # Gemini calls, embeddings
│   │   ├── pdf_processor.py       # PDF/web/YouTube ingestion
│   │   └── styles.py              # Response formatting
│   ├── Dockerfile                 # HuggingFace deployment
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   │   ├── Landing.jsx        # Landing page
│   │   │   ├── Upload.jsx         # Source upload page
│   │   │   └── Chat.jsx           # Chat interface
│   │   ├── App.jsx                # Router
│   │   └── main.jsx               # Entry point
│   ├── vercel.json                # API proxy config
│   └── package.json
└── README.md

## 👨‍💻 Author

**Vansh Mahajan**
- 🌐 Website: [talkdox-ai.vercel.app](https://talkdox-ai.vercel.app)
- 💼 LinkedIn: [vansh-mahajan-napv](https://www.linkedin.com/in/vansh-mahajan-napv/)
- 📧 Email: vansh150705@gmail.com
- 🐙 GitHub: [@Vansh150705](https://github.com/Vansh150705)

---

## 📄 License

MIT License — feel free to use, modify, and share.

---

## 🌟 Show Your Support

If TalkDox helped you, please:
- ⭐ Star this repo
- 🔀 Share with friends
- 🐛 Report bugs or suggest features in Issues

Built with 💙 by Vansh Mahajan
