"""Run the API with offline test doubles (NO AI): hashed embeddings + an extractive 'reader'.

Purpose: try the UI / run browser tests without any API key. Answers are sentences copied from the retrieved
passages, not model output. The UI shows an 'Offline demo mode' banner. Never use this in production.

    python scripts/run_offline_demo.py            # http://localhost:8000
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import uvicorn  # noqa: E402

from app.config import Settings, finalize  # noqa: E402
from app.main import create_app  # noqa: E402
from app.providers.embeddings import HashEmbedder  # noqa: E402
from app.providers.llm import FakeLLM  # noqa: E402
from app.state import build_state  # noqa: E402
from conftest import extractive_handler  # noqa: E402

data = Path(os.getenv("DATA_DIR") or tempfile.mkdtemp(prefix="talkdox-demo-"))
s = finalize(Settings(env="development", data_dir=data, cors_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                      ocr_engine="none", embedding_dim=512, retention_days=0))
state = build_state(s, llm=FakeLLM(s, extractive_handler), embedder=HashEmbedder(512))
app = create_app(state=state)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PORT", "8000")), log_level="warning")
