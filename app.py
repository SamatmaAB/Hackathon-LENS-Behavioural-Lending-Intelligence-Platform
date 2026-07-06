import os, sys
# Ensure the backend package directory is on the Python path
sys.path.append(os.path.join(os.path.dirname(__file__), "backend"))

# Import the FastAPI instance defined in backend/app.py
from backend.app import app

# Vercel looks for a top‑level variable named `app`, `application`, or `handler`.
# By re‑exporting the imported FastAPI object here we satisfy that requirement.
