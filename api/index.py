import os, sys

# Add the repository root (contains the backend package) to PYTHONPATH
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(PROJECT_ROOT)

# Import the FastAPI instance defined in backend/app.py
from backend.app import app
