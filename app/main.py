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
# Quiz Engine (Excel-backed via openpyxl, adaptive — FIXED PHASE-2 FINISH)
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
    if m:
        return f"{int(m.group(1))}.{int(m.group(2))}"
    return str(topic or "").strip()

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
    header = next(ws.iter_rows(min_row=1, max_row=1, values_only=True))
    cols = { (str(c).strip().lower() if c else ""): i for i, c in enumerate(header) }
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

def _pick(questions: List[Dict[str,Any]], want: str, used: set) -> Dict[str,Any]:
    pool = [ (i,q) for i,q in enumerate(questions) if q["difficulty"]==want and i not in used ]
    if not pool:
        # fallback: any unused
        pool = [ (i,q) for i,q in enumerate(questions) if i not in used ]
        if not pool:
            raise HTTPException(status_code=400, detail="Question bank exhausted")
    i,q = pool[0]  # deterministic
    used.add(i)
    return {**q, "index": i}

@app.post("/quiz/start")
def quiz_start(req: QuizStart):
    sheet = _sheet_key(req.topic)
    qs = _load_questions(req.subject, sheet)
    sid = str(uuid.uuid4())
    _SESSIONS[sid] = {
        "subject": req.subject,
        "sheet": sheet,
        "qs": qs,
        "used": set(),
        "phase": "p1",       # p1 -> p2
        "p1_asked": 0,
        "p1_correct": 0,
        "p2_asked": 0,       # 0..2 (Q6, Q7, Q8)
        "score": 0,
        "asked": 0,
        "last_correct": None,
        "current": None
    }
    # Q1: start easy
    q = _pick(qs, "easy", _SESSIONS[sid]["used"])
    _SESSIONS[sid]["current"] = q
    return {"session_id": sid, "question": {"text": q["text"], "options": q["options"], "difficulty": q["difficulty"]}}

@app.post("/quiz/answer")
def quiz_answer(req: QuizAnswer):
    s = _SESSIONS.get(req.session_id)
    if not s:
        raise HTTPException(status_code=400, detail="Invalid session_id")
    q = s["current"]
    if not q:
        raise HTTPException(status_code=400, detail="No current question")

    # grade
    corr = (int(req.answer) == int(q["correct"]))
    if corr: s["score"] += 1
    s["asked"] += 1
    s["last_correct"] = corr

    # ---------- PHASE 1 (first 5) ----------
    if s["phase"] == "p1":
        s["p1_asked"] += 1
        if corr: s["p1_correct"] += 1

        if s["p1_asked"] < 5:
            # Serve next easy/medium (easier first 2, then medium)
            next_diff = "easy" if s["p1_asked"] <= 2 else "medium"
            nq = _pick(s["qs"], next_diff, s["used"])
            s["current"] = nq
            return {"correct": corr, "done": False,
                    "question": {"text": nq["text"], "options": nq["options"], "difficulty": nq["difficulty"]}}

        # Completed 5
        if s["p1_correct"] < 5:
            unlock = s["p1_correct"] >= 3
            s["current"] = None
            return {"correct": corr, "done": True, "total": s["asked"], "score": s["score"],
                    "unlock_next": unlock, "status": ("Ready for next" if unlock else "Review & retry")}

        # perfect -> phase 2
        s["phase"] = "p2"; s["p2_asked"] = 0
        nq = _pick(s["qs"], "hard", s["used"])  # Q6 hard
        s["current"] = nq
        return {"correct": corr, "done": False,
                "question": {"text": nq["text"], "options": nq["options"], "difficulty": nq["difficulty"]}}

    # ---------- PHASE 2 (Q6..Q8) ----------
    s["p2_asked"] += 1  # just answered Q6/Q7/Q8?

    if s["p2_asked"] >= 3:
        # finished after Q8
        s["current"] = None
        total, score = s["asked"], s["score"]
        # status map
        if total == 6 and score == 5: status = "Confident"
        elif total == 7 and score == 6: status = "Proficient"
        elif total == 8 and score == 7: status = "Master"
        elif total == 8 and score == 8: status = "Champion"
        else: status = "Great progress"
        return {"correct": corr, "done": True, "total": total, "score": score,
                "unlock_next": True, "status": status}

    # pick next difficulty
    if s["p2_asked"] == 1:
        # we just answered Q6 -> choose Q7
        next_diff = "hard" if corr else "medium"
    else:
        # we just answered Q7 -> choose Q8
        next_diff = "hard" if corr else "easy"

    nq = _pick(s["qs"], next_diff, s["used"])
    s["current"] = nq
    return {"correct": corr, "done": False,
            "question": {"text": nq["text"], "options": nq["options"], "difficulty": nq["difficulty"]}}
