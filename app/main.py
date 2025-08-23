from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path

app = FastAPI(title="AI Maths Prep API")

BASE_DIR = Path(__file__).resolve().parent
TUTORIALS_DIR = BASE_DIR / "data" / "tutorials"
if TUTORIALS_DIR.exists():
    app.mount("/tutorials", StaticFiles(directory=str(TUTORIALS_DIR)), name="tutorials")

@app.get("/healthz")
def healthz():
    return {"ok": True, "tutorial_subjects": ["LA", "Stats"]}


# Example tutorials listing
@app.get("/tutorials_list")
def tutorials_list(subject: str):
    subject_path = TUTORIALS_DIR / subject
    if not subject_path.exists():
        raise HTTPException(status_code=404, detail=f"No tutorials found for subject {subject}")
    files = [f.name for f in subject_path.glob("*.pdf")]
    return {"subject": subject, "files": files}