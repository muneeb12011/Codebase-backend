# Codebase Visualizer — Backend

Python serverless API (Flask) deployed on **Vercel**.

## Deploy to Vercel

1. Push this `backend/` folder to a GitHub repository (or the full project)
2. Go to [vercel.com](https://vercel.com) → **New Project** → Import your repo
3. Set **Root Directory** to `backend`
4. Vercel auto-detects Python + Flask — no extra config needed
5. Click **Deploy**

## Local Development (Windows PowerShell)

```powershell
cd backend

# Create a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Run Flask locally
$env:FLASK_APP = "api/index.py"
$env:FLASK_ENV = "development"
flask run --port 3001
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | /api/healthz | Health check |
| POST | /api/analysis/analyze | Full repo analysis |
| POST | /api/analysis/graph | Dependency graph only |
| POST | /api/analysis/file | Single file details + code |
| POST | /api/analysis/summary | Summary stats |

## Tech Stack

- **Flask** — lightweight Python web framework
- **NetworkX** — graph algorithms & circular dependency detection  
- **Pygments** — language detection
- **chardet** — encoding detection
- **Python AST** — code structure analysis (built-in, no extra install)
