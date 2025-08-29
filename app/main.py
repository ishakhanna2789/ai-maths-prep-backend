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
# Quiz Engine (Excel-backed via openpyxl, adaptive)
# =============================================================================
class QuizManager:
    """
    Adaptive quiz logic:

    Phase 1 (Q1–Q5): draw 5 questions from Easy/Medium only (column 'Difficulty').
      - If correct >= 3 -> unlock_next = True and finish with status "Ready for next".
      - If correct == 5 -> unlock_next = True and seamlessly continue to Phase 2.

    Phase 2 (Q6–Q8): only when Phase 1 is perfect (5/5)
      - Q6 difficulty = Hard
      - Q7 difficulty = Hard if Q6 correct else Medium
      - Q8 difficulty = Hard if Q7 correct else Easy

    Final statuses (when 6–8 asked):
      - 5/6 -> "Confident"
      - 6/7 -> "Proficient"
      - 7/8 -> "Master"
      - 8/8 -> "Champion"
    """

    def __init__(self) -> None:
        # cache[subject][sheet_key] -> list of question dicts
        self.cache: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        # sessions[session_id] -> session state
        self.sessions: Dict[str, Dict[str, Any]] = {}

    # ----------------- Loading helpers -----------------
    def _xlsx_path(self, subject: str) -> Path:
        s = subject.strip()
        if s not in {"LA", "Stats"}:
            raise ValueError("Invalid subject (use 'LA' or 'Stats')")
        return QUIZZES_DIR / f"{s}.xlsx"

    def _sheet_key_from_topic(self, topic: str) -> str:
        """
        Extract the sheet key (e.g., '1.1') from a topic like '1.1_Intro_to_Vectors'.
        """
        m = re.search(r"(\d+)\.(\d+)", str(topic))
        if m:
            return f"{int(m.group(1))}.{int(m.group(2))}"
        m2 = re.search(r"(\d+)", str(topic))
        if m2:
            return str(int(m2.group(1)))
        raise ValueError("Cannot infer sheet key from topic")

    def _idx_from_correct(self, raw: Any, options: List[str]) -> int:
        """
        Accept 'A'/'B'/'C'/'D' (case-insensitive), 0..3, or exact option text.
        """
        if raw is None:
            return 0
        s = str(raw).strip()
        if s.isdigit():
            i = int(s)
            if 0 <= i <= 3:
                return i
        letter_map = {"A": 0, "B": 1, "C": 2, "D": 3}
        if s.upper() in letter_map:
            return letter_map[s.upper()]
        for i, opt in enumerate(options):
            if s == opt:
                return i
        return 0

    def _load_sheet(self, subject: str, sheet_key: str) -> List[Dict[str, Any]]:
        """
        Read a sheet using openpyxl.
        Required headers (case-insensitive):
            Question Text | Option A | Option B | Option C | Option D | Correct Answer | Difficulty
        Optional:
            Module | Question ID | Question Type | Hint | Explanation | Tags | Image Link (optional)
        """
        subj_cache = self.cache.setdefault(subject, {})
        if sheet_key in subj_cache:
            return subj_cache[sheet_key]

        xlsx = self._xlsx_path(subject)
        if not xlsx.exists():
            raise FileNotFoundError(f"Quiz workbook missing: {xlsx}")

        try:
            wb = load_workbook(filename=str(xlsx), read_only=True, data_only=True)
        except Exception as e:
            raise FileNotFoundError(f"Cannot open workbook {xlsx.name}: {e}")

        if sheet_key not in wb.sheetnames:
            wb.close()
            raise FileNotFoundError(f"Sheet '{sheet_key}' missing in {xlsx.name}")

        ws = wb[sheet_key]

        # Header row
        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        if not header_row:
            wb.close()
            raise ValueError(f"No header in sheet '{sheet_key}' of {xlsx.name}")

        headers = [str(c).strip().lower() if c is not None else "" for c in header_row]
        col_index = {h: i for i, h in enumerate(headers)}

        # Validate required columns
        required = [
            "question text", "option a", "option b", "option c",
            "option d", "correct answer", "difficulty"
        ]
        missing = [c for c in required if c not in col_index]
        if missing:
            wb.close()
            raise ValueError(f"Missing columns {missing} in sheet '{sheet_key}' of {xlsx.name}")

        # Optional columns map
        optional_map = {
            "hint": "hint",
            "explanation": "explanation",
            "image_url": "image link (optional)",
            "qid": "question id",
            "module": "module",
            "tags": "tags",
            "type": "question type",
        }

        # Rows -> questions
        items: List[Dict[str, Any]] = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            def cell(col_name: str) -> Any:
                i = col_index.get(col_name)
                return row[i] if i is not None and i < len(row) else None

            qtext = cell("question text")
            if qtext is None or str(qtext).strip() == "":
                continue  # skip empty rows

            options = [
                "" if cell("option a") is None else str(cell("option a")),
                "" if cell("option b") is None else str(cell("option b")),
                "" if cell("option c") is None else str(cell("option c")),
                "" if cell("option d") is None else str(cell("option d")),
            ]
            correct = self._idx_from_correct(cell("correct answer"), options)

            diff_raw = str(cell("difficulty") or "").strip().lower()
            if diff_raw.startswith("e"):
                diff = "easy"
            elif diff_raw.startswith("m"):
                diff = "medium"
            elif diff_raw.startswith("h"):
                diff = "hard"
            else:
                diff = "medium"

            item: Dict[str, Any] = {
                "text": str(qtext),
                "options": options,
                "correct_index": correct,
                "difficulty": diff,
            }

            # Attach optional metadata if present
            for k, col_name in optional_map.items():
                i = col_index.get(col_name)
                if i is not None and i < len(row) and row[i] is not None:
                    item[k] = str(row[i])

            items.append(item)

        wb.close()

        if not items:
            raise ValueError(f"No questions in sheet '{sheet_key}' of {xlsx.name}")

        subj_cache[sheet_key] = items
        return items

    # ----------------- Pool & selection helpers -----------------
    def _split_pools(self, items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        pools = {"easy": [], "medium": [], "hard": []}
        for it in items:
            pools.get(it.get("difficulty", "medium"), pools["medium"]).append(it)
        return pools

    def _draw(self, pool: List[Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
        if k <= 0:
            return []
        if len(pool) <= k:
            return random.sample(pool, len(pool)) if pool else []
        return random.sample(pool, k)

    def _fallback_take(self, pools: Dict[str, List[Dict[str, Any]]], wanted: List[str], k: int) -> List[Dict[str, Any]]:
        """
        Take up to k from pools in order of 'wanted' difficulties; if short, fill from any available.
        """
        chosen: List[Dict[str, Any]] = []
        for name in wanted:
            take = min(k - len(chosen), len(pools.get(name, [])))
            if take > 0:
                chosen.extend(self._draw(pools[name], take))
        if len(chosen) < k:
            # Fill from all (avoid duplicates)
            all_pool = [x for arr in pools.values() for x in arr if x not in chosen]
            chosen.extend(self._draw(all_pool, k - len(chosen)))
        return chosen[:k]

    # ----------------- Public API -----------------
    def start(self, subject: str, topic: str) -> Dict[str, Any]:
        """
        Initialize a session with 5 questions (Easy/Medium). If perfect 5/5,
        the session will continue into adaptive Phase 2 (Q6–Q8).
        """
        sheet_key = self._sheet_key_from_topic(topic)
        all_items = self._load_sheet(subject, sheet_key)
        pools = self._split_pools(all_items)

        first5 = self._fallback_take(pools, ["easy", "medium"], 5)
        if not first5:
            raise ValueError("No questions available for Phase 1")

        sid = str(uuid.uuid4())
        self.sessions[sid] = {
            "subject": subject,
            "topic": topic,
            "sheet_key": sheet_key,
            "phase": 1,           # 1 or 2
            "index": 0,           # index within current phase
            "first5": first5,
            "first5_correct": 0,
            "pools": pools,
            "score": 0,
            "asked": 0,
            "unlock_next": False, # set True if >=3/5 in Phase 1
            "q6_result": None,    # set in Phase 2
            "q7_result": None,    # set in Phase 2
            "adaptive": [],       # questions for Phase 2
        }

        q = first5[0]
        return {
            "session_id": sid,
            "question": {"text": q["text"], "options": q["options"], "difficulty": q["difficulty"]},
            "done": False,
        }

    def _select_adaptive_seq(self, pools: Dict[str, List[Dict[str, Any]]], q6_correct: bool, q7_correct: Optional[bool]) -> List[Dict[str, Any]]:
        """
        Build the desired sequence Q6..Q8 with the specified branching rules,
        using safe fallbacks if a difficulty pool is short.
        """
        desired = [
            "hard",                                   # Q6
            "hard" if q6_correct else "medium",       # Q7
            "hard" if (q7_correct is True) else ("easy" if (q7_correct is False) else "medium"),  # Q8
        ]

        chosen: List[Dict[str, Any]] = []
        for want in desired:
            order = {
                "hard":   ["hard", "medium", "easy"],
                "medium": ["medium", "easy", "hard"],
                "easy":   ["easy", "medium", "hard"],
            }[want]
            pick = self._fallback_take(pools, order, 1)
            if pick:
                # avoid duplicates across adaptive picks
                ids = set(id(x) for x in chosen)
                p = next((x for x in pick if id(x) not in ids), None)
                if p is not None:
                    chosen.append(p)
        return chosen

    def answer(self, session_id: str, answer_index: int) -> Dict[str, Any]:
        sess = self.sessions.get(session_id)
        if not sess:
            raise ValueError("Invalid session_id")

        # ---------------- Phase 1 ----------------
        if sess["phase"] == 1:
            i = sess["index"]
            curr = sess["first5"][i]
            correct = (int(answer_index) == int(curr["correct_index"]))
            if correct:
                sess["first5_correct"] += 1
                sess["score"] += 1
            sess["asked"] += 1

            if i < 4:
                # Next question within Phase 1
                sess["index"] += 1
                nxt = sess["first5"][sess["index"]]
                return {
                    "correct": correct,
                    "done": False,
                    "question": {"text": nxt["text"], "options": nxt["options"], "difficulty": nxt["difficulty"]},
                }

            # Completed first 5
            passed = (sess["first5_correct"] >= 3)
            sess["unlock_next"] = passed

            if sess["first5_correct"] < 5:
                # End here (not perfect): show "Ready for next" if passed
                return {
                    "correct": correct,
                    "done": True,
                    "score": sess["score"],
                    "total": sess["asked"],
                    "unlock_next": passed,
                    "status": "Ready for next" if passed else "Review & retry",
                }

            # Perfect 5 → continue to Phase 2 (adaptive)
            pools = sess["pools"]
            adaptive = self._select_adaptive_seq(pools, q6_correct=True, q7_correct=None)
            if not adaptive:
                # If somehow nothing available, still end gracefully
                return {
                    "correct": correct,
                    "done": True,
                    "score": sess["score"],
                    "total": sess["asked"],
                    "unlock_next": True,
                    "status": "Ready for next",
                }

            sess["phase"] = 2
            sess["index"] = 0
            sess["adaptive"] = adaptive
            nxt = adaptive[0]  # Q6
            return {
                "correct": correct,
                "done": False,
                "question": {"text": nxt["text"], "options": nxt["options"], "difficulty": nxt.get("difficulty", "hard")},
            }

        # ---------------- Phase 2 ----------------
        i = sess["index"]
        curr = sess["adaptive"][i]
        correct = (int(answer_index) == int(curr["correct_index"]))
        if correct:
            sess["score"] += 1
        sess["asked"] += 1

        if i == 0:
            # After Q6 → decide Q7 difficulty
            sess["q6_result"] = correct
            pools = sess["pools"]
            rem = self._select_adaptive_seq(pools, q6_correct=correct, q7_correct=None)
            # Keep the next 2 (Q7, Q8)
            sess["adaptive"] = rem[:2] if len(rem) >= 2 else rem
            sess["index"] = 0
            if sess["adaptive"]:
                nxt = sess["adaptive"][0]  # Q7
                return {
                    "correct": correct,
                    "done": False,
                    "question": {"text": nxt["text"], "options": nxt["options"], "difficulty": nxt.get("difficulty", "medium")},
                }
            return self._finish(sess, correct)

        if i == 1:
            # After Q7 → decide Q8 difficulty
            sess["q7_result"] = correct
            pools = sess["pools"]
            q8 = self._select_adaptive_seq(pools, q6_correct=bool(sess["q6_result"]), q7_correct=correct)
            sess["adaptive"] = q8[:1] if q8 else []
            sess["index"] = 0
            if sess["adaptive"]:
                nxt = sess["adaptive"][0]  # Q8
                return {
                    "correct": correct,
                    "done": False,
                    "question": {"text": nxt["text"], "options": nxt["options"], "difficulty": nxt.get("difficulty", "easy")},
                }
            return self._finish(sess, correct)

        # i == 0 for final Q8 (due to resets above)
        return self._finish(sess, correct)

    def _finish(self, sess: Dict[str, Any], last_correct: bool) -> Dict[str, Any]:
        total = sess["asked"]
        score = sess["score"]
        # If we reached Phase 2, unlock_next is inherently True (Phase 1 was perfect)
        unlock = sess.get("unlock_next", False) or True

        # Status decisions when 6–8 questions were asked:
        status = "Ready for next"
        if total >= 6:
            # Map exact totals & scores to your labels
            if total == 6 and score == 5:
                status = "Confident"
            elif total == 7 and score == 6:
                status = "Proficient"
            elif total == 8 and score == 7:
                status = "Master"
            elif total == 8 and score == 8:
                status = "Champion"
            else:
                status = "Great progress"

        return {
            "correct": last_correct,
            "done": True,
            "score": score,
            "total": total,
            "unlock_next": unlock,
            "status": status,
        }


QUIZ = QuizManager()


# =============================================================================
# Quiz endpoints
# =============================================================================
@app.post("/quiz/start")
def quiz_start(payload: Dict[str, Any] = Body(...)):
    """
    Body: { "subject": "LA"|"Stats", "topic": "1.1_Intro_to_..." }
    """
    try:
        subject = str(payload.get("subject") or "LA")
        topic   = str(payload.get("topic") or "1.1")
        return QUIZ.start(subject=subject, topic=topic)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not start quiz: {e}")


@app.post("/quiz/answer")
def quiz_answer(payload: Dict[str, Any] = Body(...)):
    """
    Body: { "session_id": "...", "answer": 0|1|2|3 }
    """
    try:
        session_id = str(payload.get("session_id") or "")
        if not session_id:
            raise ValueError("Missing 'session_id'")
        if "answer" not in payload:
            raise ValueError("Missing 'answer'")
        answer = int(payload["answer"])
        return QUIZ.answer(session_id=session_id, answer_index=answer)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not submit answer: {e}")