"""ASGI entrypoint: `uvicorn main:app`. All logic lives in the `app` package."""
from pathlib import Path

try:  # local development convenience: read backend/.env (real environment variables always win)
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:  # python-dotenv is optional in production images
    pass

from app.main import create_app  # noqa: E402  (must come after the .env is loaded)

app = create_app()

if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
