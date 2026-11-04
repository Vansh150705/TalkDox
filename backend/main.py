"""ASGI entrypoint: `uvicorn main:app`. All logic lives in the `app` package."""
from app.main import create_app

app = create_app()

if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
