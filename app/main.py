from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from typing import Dict, List, Any, Optional
from pathlib import Path
import random, re, uuid

from openpyxl import load_workbook


# =============================================================================
# App setup
# =============================================================================
app = FastAPI(title="AI Maths Prep API", version="0.1.0")

# CORS (open for dev; restrict in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Data roots (relative to this file)
APP_DIR = Path(__file__).parent
TUTORIALS_DIR = APP_DIR / "data" / "tutorials"
QUIZZES_DIR   = APP_DIR / "data" / "quizzes"

# Serve tutorials as static files (so Streamlit can embed PDFs reliably)
app.mount("/static/tutorials", StaticFiles(directory=str(TUTORIALS_DIR)), name="tutorials")


# =============================================================================
# Health check
# =============================================================================
@app.get("/healthz")
def healthz():
    return {"status": "ok"}


# =============================================================================
# Tutorials
# =============================================================================
@app.get("/tutorials_list")
def tutorials_list(subject: str):
    """
    Return list of tutorial PDFs for the given subject ("LA" or "Stats").
    Response: [{ "filename": "...pdf", "title": "1.1 Intro to ..."}]
    """
    subject = subject.strip()
    subject_dir = TUTORIALS_DIR / subject
    if not subject_dir.exists() or not subject_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Subject not found: {subject}")

    files = sorted([p.name for p in subject_dir.glob("*.pdf")])
    items: List[Dict[str, Any]] = []
    for name in files:
        title = name[:-4].replace("_", " ").replace("-", " ").strip()  # drop .pdf
        items.append({"filename": name, "title": title, "subject": subject})
    return items


@app.get("/tutorials_file")
def tutorials_file(subject: str, filename: str):
    """
    Return a JSON object with a public URL to the tutorial file.
    The frontend will embed this, and also has a static fallback.
    """
    subject_dir = TUTORIALS_DIR / subject
    file_path = subject_dir / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Tutorial file not found")
    # Return a relative API URL (frontend can prefix with API_BASE_URL)
    return {"url": f"/static/tutorials/{subject}/{filename}"}


# =============================================================================
# =============================================================================
# =============================================================================
# Quiz Engine (Excel-backed, step-based, deterministic finish)
# =============================================================================
from pydantic import BaseModel
import uuid

class QuizStart(BaseModel):
    subject: str   # "LA" | "Stats"
    topic: str     # "1.1" or "1.1_Intro_to_Vectors"

class QuizAnswer(BaseModel):
    session_id: str
    answer: int    # 0..3

# In-memory sessions (MVP)
_SESSIONS: Dict[str, Dict[str, Any]] = {}

def _sheet_key(topic: str) -> str:
    m = re.search(r"(\d+)\.(\d+)", topic or "")
    return f"{int(m.group(1))}.{int(m.group(2))}" if m else str(topic or "").strip()

def _load_questions(subject: str, sheet: str) -> List[Dict[str, Any]]:
    xlsx = QUIZZES_DIR / f"{subject}.xlsx"
    if not xlsx.exists():
        raise HTTPException(status_code=400, detail=f"Workbook not found: {xlsx}")
    wb = load_workbook(filename=str(xlsx), read_only=True, data_only=True)
    if sheet not in wb.sheetnames:
        avail = ", ".join(wb.sheetnames)
        wb.close()
        raise HTTPException(status_code=400, detail=f"Sheet '{sheet}' missing in {xlsx.name}; have: {avail}")
    ws = wb[sheet]
    hdr = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    cols = {(str(c).strip().lower() if c else ""): i for i, c in enumerate(hdr)}
    need = ["question text","option a","option b","option c","option d","correct answer","difficulty"]
    miss = [c for c in need if c not in cols]
    if miss:
        wb.close()
        raise HTTPException(status_code=400, detail=f"Missing columns {miss} in sheet '{sheet}'")
    def cidx(raw, options):
        if raw is None: return 0
        s = str(raw).strip()
        if s.isdigit():
            i = int(s);  return i if 0 <= i <= 3 else 0
        L = {"A":0,"B":1,"C":2,"D":3}
        if s.upper() in L: return L[s.upper()]
        for i,o in enumerate(options):
            if s == str(o): return i
        return 0
    out = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        qtext = row[cols["question text"]]
        if not qtext: continue
        opts = [row[cols["option a"]], row[cols["option b"]], row[cols["option c"]], row[cols["option d"]]]
        opts = ["" if o is None else str(o) for o in opts]
        corr = cidx(row[cols["correct answer"]], opts)
        diff = str(row[cols["difficulty"]] or "").strip().lower()
        diff = "easy" if diff.startswith("e") else "medium" if diff.startswith("m") else "hard"
        out.append({"text": str(qtext), "options": opts, "correct": corr, "difficulty": diff})
    wb.close()
    if not out:
        raise HTTPException(status_code=400, detail="No questions found")
    return out

def _pop_from_pool(pools: Dict[str, List[Dict[str,Any]]], want: str) -> Dict[str,Any]:
    # pop from preferred pool, else fall back to any non-empty pool
    want = want.lower()
    order = {"hard":["hard","medium","easy"], "medium":["medium","easy","hard"], "easy":["easy","medium","hard"]}[want]
    for d in order:
        if pools[d]:
            return pools[d].pop(0)
    raise HTTPException(status_code=400, detail="Question bank exhausted")

def _status_after_phase1(score5: int) -> str:
    return "Ready for next" if score5 >= 3 else "Review & retry"

def _status_after_phase2(total: int, score: int) -> str:
    if total == 6 and score == 5: return "Confident"
    if total == 7 and score == 6: return "Proficient"
    if total == 8 and score == 7: return "Master"
    if total == 8 and score == 8: return "Champion"
    return "Great progress"

@app.post("/quiz/start")
def quiz_start(req: QuizStart):
    sheet = _sheet_key(req.topic)
    all_qs = _load_questions(req.subject, sheet)
    # Build FIFO pools so we never repeat a question
    pools = {"easy":[], "medium":[], "hard":[]}
    for q in all_qs:
        pools[q["difficulty"]].append(q)

    sid = str(uuid.uuid4())
    _SESSIONS[sid] = {
        # immutable info
        "subject": req.subject, "sheet": sheet, "pools": pools,
        # progress
        "step": 1,             # absolute step 1..8
        "score": 0,            # total correct so far
        "p1_correct": 0,       # correct among first 5
        "q6_correct": None,    # correctness of Q6
        "q7_correct": None,    # correctness of Q7
        # current question
        "current": None,
    }
    # Step 1 difficulty: Easy
    q = _pop_from_pool(pools, "easy")
    _SESSIONS[sid]["current"] = q
    return {"session_id": sid,
            "question": {"text": q["text"], "options": q["options"], "difficulty": q["difficulty"]},
            "done": False}

@app.post("/quiz/answer")
def quiz_answer(req: QuizAnswer):
    s = _SESSIONS.get(req.session_id)
    if not s: raise HTTPException(status_code=400, detail="Invalid session_id")
    q = s.get("current")
    if not q: raise HTTPException(status_code=400, detail="No current question")

    # grade this step
    correct = (int(req.answer) == int(q["correct"]))
    if correct: s["score"] += 1

    # --- If in steps 1..5 (Phase 1) ---
    if 1 <= s["step"] <= 5:
        if correct: s["p1_correct"] += 1
        s["step"] += 1
        if s["step"] <= 5:
            # next P1 difficulty: step 2 -> Easy, steps 3..5 -> Medium
            next_diff = "easy" if s["step"] <= 2 else "medium"
            nq = _pop_from_pool(s["pools"], next_diff)
            s["current"] = nq
            return {"correct": correct, "done": False,
                    "question": {"text": nq["text"], "options": nq["options"], "difficulty": nq["difficulty"]}}
        # completed 5
        if s["p1_correct"] < 5:
            unlock = (s["p1_correct"] >= 3)
            s["current"] = None
            return {"correct": correct, "done": True,
                    "total": 5, "score": s["score"],
                    "unlock_next": unlock,
                    "status": _status_after_phase1(s["p1_correct"])}
        # perfect 5 → go to step 6
        s["step"] = 6
        nq = _pop_from_pool(s["pools"], "hard")   # Q6 Hard
        s["current"] = nq
        return {"correct": correct, "done": False,
                "question": {"text": nq["text"], "options": nq["options"], "difficulty": nq["difficulty"]}}

    # --- Steps 6..8 (Phase 2) ---
    if s["step"] == 6:
        s["q6_correct"] = correct
        s["step"] = 7
        next_diff = "hard" if correct else "medium"   # Q7
        nq = _pop_from_pool(s["pools"], next_diff)
        s["current"] = nq
        return {"correct": correct, "done": False,
                "question": {"text": nq["text"], "options": nq["options"], "difficulty": nq["difficulty"]}}

    if s["step"] == 7:
        s["q7_correct"] = correct
        s["step"] = 8
        next_diff = "hard" if correct else "easy"     # Q8
        nq = _pop_from_pool(s["pools"], next_diff)
        s["current"] = nq
        return {"correct": correct, "done": False,
                "question": {"text": nq["text"], "options": nq["options"], "difficulty": nq["difficulty"]}}

    # s["step"] == 8  → this answer finishes the quiz
    s["step"] = 9
    s["current"] = None
    total = 8
    status = _status_after_phase2(total, s["score"])
    return {"correct": correct, "done": True,
            "total": total, "score": s["score"],
            "unlock_next": True, "status": status}
