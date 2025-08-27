from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import random
import pandas as pd
from typing import Dict, List, Any
from fastapi import Body
import uuid
from fastapi.staticfiles import StaticFiles



app = FastAPI(title="AI Maths Prep API")

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="AI Maths Prep API")
from fastapi.staticfiles import StaticFiles  # add with imports

# mount your existing folder:
app.mount("/static/tutorials", StaticFiles(directory="app/data/tutorials"), name="tutorials")


# 1) See exact file size on the server (Render)
@app.get("/debug/file-info")
def debug_file_info(subject: str, filename: str):
    fpath = (TUTORIALS_DIR / subject / filename).resolve()
    if not fpath.exists():
        raise HTTPException(status_code=404, detail=f"Not found: {fpath}")
    return {
        "path": str(fpath),
        "exists": True,
        "size_bytes": fpath.stat().st_size,
    }

# 2) Force-serve the PDF via FileResponse (bypasses StaticFiles if needed)
@app.get("/tutorials_file")
def tutorials_file(subject: str, filename: str):
    fpath = (TUTORIALS_DIR / subject / filename).resolve()
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="File not found on server")
    return FileResponse(path=fpath, media_type="application/pdf", filename=filename)

# Allow all origins for demo (you can restrict later)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # or put your Streamlit URL here later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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

# ---- QUIZ MANAGER ----
QUIZ_ROOT = Path(__file__).parent / "data" / "quizzes"

class QuizManager:
    def __init__(self):
        self.cache: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        self.sessions: Dict[str, Dict[str, Any]] = {}

    def _subject_path(self, subject: str) -> Path:
        subj = subject.strip()
        if subj not in {"LA", "Stats"}:
            raise ValueError("Invalid subject")
        return QUIZ_ROOT / f"{subj}.xlsx"

    def _load_sheet(self, subject: str, sheet_key: str) -> List[Dict[str, Any]]:
        """
        Load and cache questions for subject+sheet_key.
        sheet_key examples: '1.1', '2.3', '3.1'
        """
        subj_cache = self.cache.setdefault(subject, {})
        if sheet_key in subj_cache:
            return subj_cache[sheet_key]

        xlsx = self._subject_path(subject)
        if not xlsx.exists():
            raise FileNotFoundError(f"Quiz workbook missing: {xlsx}")

        try:
            df = pd.read_excel(xlsx, sheet_name=sheet_key)
        except Exception as e:
            raise FileNotFoundError(f"Sheet '{sheet_key}' missing in {xlsx.name}: {e}")

        needed_cols = ["question", "option_a", "option_b", "option_c", "option_d", "correct_index"]
        for col in needed_cols:
            if col not in df.columns:
                raise ValueError(f"Column '{col}' missing in sheet '{sheet_key}' of {xlsx.name}")

        # normalize to list of dicts
        items: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            q = str(row["question"])
            options = [str(row["option_a"]), str(row["option_b"]), str(row["option_c"]), str(row["option_d"])]
            try:
                correct = int(row["correct_index"])
            except Exception:
                correct = 0
            items.append({"text": q, "options": options, "correct_index": correct})

        subj_cache[sheet_key] = items
        return items

    def _sheet_key_from_topic(self, topic: str) -> str:
        """
        Extract 'major.minor' from topic string like '1.1_Intro_to_Vectors'.
        Fallback: first number in the string.
        """
        m = re.search(r"(\d+)\.(\d+)", topic)
        if m:
            return f"{int(m.group(1))}.{int(m.group(2))}"
        m2 = re.search(r"(\d+)", topic)
        if m2:
            return str(int(m2.group(1)))
        raise ValueError("Cannot infer sheet key from topic")

    def start(self, subject: str, topic: str, num_questions: int = 5) -> Dict[str, Any]:
        sheet_key = self._sheet_key_from_topic(topic)
        all_qs = self._load_sheet(subject, sheet_key)
        if not all_qs:
            raise ValueError("No questions in sheet")

        k = min(num_questions, len(all_qs))
        chosen = random.sample(all_qs, k)

        sid = str(uuid.uuid4())
        self.sessions[sid] = {
            "subject": subject,
            "topic": topic,
            "sheet_key": sheet_key,
            "remaining": chosen[1:],   # everything after the first
            "current": chosen[0],
            "done": False,
            "score": 0,
            "total": k,
            "answered": 0,
        }
        return {
            "session_id": sid,
            "question": {"text": chosen[0]["text"], "options": chosen[0]["options"]},
            "done": False,
        }

    def answer(self, session_id: str, answer_index: int) -> Dict[str, Any]:
        sess = self.sessions.get(session_id)
        if not sess:
            raise ValueError("Invalid session_id")

        if sess["done"]:
            return {"done": True, "score": sess["score"], "total": sess["total"]}

        curr = sess["current"]
        correct = int(curr.get("correct_index", 0))
        is_right = (int(answer_index) == correct)
        if is_right:
            sess["score"] += 1
        sess["answered"] += 1

        if sess["remaining"]:
            nxt = sess["remaining"][0]
            sess["remaining"] = sess["remaining"][1:]
            sess["current"] = nxt
            return {
                "correct": is_right,
                "done": False,
                "question": {"text": nxt["text"], "options": nxt["options"]},
            }
        else:
            sess["done"] = True
            return {
                "correct": is_right,
                "done": True,
                "score": sess["score"],
                "total": sess["total"],
                # optional extras for your UI:
                "mastery": round(100.0 * sess["score"] / max(1, sess["total"]), 1),
                "mood_snapshot": "🎯 Keep going!" if sess["score"] >= (sess["total"] // 2) else "🌱 Practice more",
            }

QUIZ = QuizManager()

@app.post("/quiz/start")
def quiz_start(payload: Dict[str, Any] = Body(...)):
    subject = str(payload.get("subject") or "LA")
    topic   = str(payload.get("topic") or "1.1")
    try:
        return QUIZ.start(subject=subject, topic=topic, num_questions=5)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not start quiz: {e}")

@app.post("/quiz/answer")
def quiz_answer(payload: Dict[str, Any] = Body(...)):
    session_id = str(payload.get("session_id") or "")
    answer     = payload.get("answer")
    if answer is None:
        raise HTTPException(status_code=422, detail="Missing 'answer'")
    try:
        return QUIZ.answer(session_id=session_id, answer_index=int(answer))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not submit answer: {e}")