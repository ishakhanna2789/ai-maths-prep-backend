from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import random
import pandas as pd
from typing import Dict, List, Any
from fastapi import Body
import uuid
from fastapi.staticfiles import StaticFiles

from typing import Dict, List, Any, Optional
import re, uuid, random

from fastapi import Body, HTTPException



app = FastAPI(title="AI Maths Prep API")

from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles  # add with imports

# mount your existing folder:
app.mount("/static/tutorials", StaticFiles(directory="app/data/tutorials"), name="tutorials")

# ================== QUIZ ENGINE (Excel-backed, adaptive) ==================
QUIZ_ROOT = Path(__file__).parent / "data" / "quizzes"

class QuizManager:
    """
    Phase 1 (Q1–Q5): Easy/Medium only.
      - >=3/5 -> unlock next; end quiz (unless 5/5).
      - 5/5   -> unlock next; continue to Phase 2.

    Phase 2 (Q6–Q8) — only if perfect 5/5:
      Q6 Hard;
      Q7 Hard if Q6 correct else Medium;
      Q8 Hard if Q7 correct else Easy.
    """

    def __init__(self) -> None:
        self.cache: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        self.sessions: Dict[str, Dict[str, Any]] = {}

    # -------- loading/helpers --------
    def _subject_xlsx(self, subject: str) -> Path:
        s = subject.strip()
        if s not in {"LA", "Stats"}:
            raise ValueError("Invalid subject (use 'LA' or 'Stats')")
        return QUIZ_ROOT / f"{s}.xlsx"

    def _sheet_key_from_topic(self, topic: str) -> str:
        # e.g., "1.1_Intro_to_Vectors" -> "1.1"
        m = re.search(r"(\d+)\.(\d+)", str(topic))
        if m:
            return f"{int(m.group(1))}.{int(m.group(2))}"
        m2 = re.search(r"(\d+)", str(topic))
        if m2:
            return str(int(m2.group(1)))
        raise ValueError("Cannot infer sheet key from topic")

    def _norm_cols(self, df: pd.DataFrame) -> pd.DataFrame:
        return df.rename(columns={c: c.strip().lower() for c in df.columns})

    def _idx_from_correct(self, raw: Any, options: List[str]) -> int:
        # Accept 0–3, A–D, or exact option text
        if raw is None:
            return 0
        s = str(raw).strip()
        if s.isdigit():
            i = int(s)
            if 0 <= i <= 3:
                return i
        letter = {"A":0, "B":1, "C":2, "D":3}.get(s.upper())
        if letter is not None:
            return letter
        for i, opt in enumerate(options):
            if s == opt:
                return i
        return 0

    def _load_sheet(self, subject: str, sheet_key: str) -> List[Dict[str, Any]]:
        subj_cache = self.cache.setdefault(subject, {})
        if sheet_key in subj_cache:
            return subj_cache[sheet_key]

        xlsx = self._subject_xlsx(subject)
        if not xlsx.exists():
            raise FileNotFoundError(f"Quiz workbook missing: {xlsx}")

        try:
            df = pd.read_excel(xlsx, sheet_name=sheet_key)
        except Exception as e:
            raise FileNotFoundError(f"Sheet '{sheet_key}' missing in {xlsx.name}: {e}")

        df = self._norm_cols(df)
        needed = ["question text", "option a", "option b", "option c", "option d", "correct answer", "difficulty"]
        for c in needed:
            if c not in df.columns:
                raise ValueError(f"Column '{c}' missing in sheet '{sheet_key}' of {xlsx.name}")

        items: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            q = str(row["question text"])
            options = [str(row["option a"]), str(row["option b"]), str(row["option c"]), str(row["option d"])]
            correct = self._idx_from_correct(row.get("correct answer"), options)
            diff = str(row.get("difficulty") or "").strip().lower()
            if diff.startswith("e"):   diff = "easy"
            elif diff.startswith("m"): diff = "medium"
            elif diff.startswith("h"): diff = "hard"
            else:                      diff = "medium"

            item = {"text": q, "options": options, "correct_index": correct, "difficulty": diff}

            # Optional metadata
            opt_map = {
                "hint": "hint",
                "explanation": "explanation",
                "image_url": "image link (optional)",
                "qid": "question id",
                "module": "module",
                "tags": "tags",
                "type": "question type",
            }
            for k, col in opt_map.items():
                key = col.lower()
                if key in df.columns and not pd.isna(row.get(key)):
                    item[k] = str(row.get(key))

            items.append(item)

        subj_cache[sheet_key] = items
        return items

    def _split_pools(self, items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        pools = {"easy": [], "medium": [], "hard": []}
        for it in items:
            pools.get(it.get("difficulty", "medium"), pools["medium"]).append(it)
        return pools

    def _draw(self, pool: List[Dict[str, Any]], k: int) -> List[Dict[str, Any]]:
        if k <= 0: return []
        if len(pool) <= k:
            return random.sample(pool, len(pool)) if pool else []
        return random.sample(pool, k)

    def _fallback_take(self, pools: Dict[str, List[Dict[str, Any]]], wanted: List[str], k: int) -> List[Dict[str, Any]]:
        chosen: List[Dict[str, Any]] = []
        for name in wanted:
            take = min(k - len(chosen), len(pools.get(name, [])))
            if take > 0:
                chosen.extend(self._draw(pools[name], take))
        if len(chosen) < k:
            all_pool = [x for name, arr in pools.items() for x in arr if x not in chosen]
            chosen.extend(self._draw(all_pool, k - len(chosen)))
        return chosen[:k]

    # --------------- public API ---------------
    def start(self, subject: str, topic: str) -> Dict[str, Any]:
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
            "phase": 1,
            "index": 0,
            "first5": first5,
            "first5_correct": 0,
            "pools": pools,
            "q6_result": None,
            "q7_result": None,
            "adaptive": [],
            "score": 0,
            "asked": 0,
            "unlock_next": False,
        }

        q = first5[0]
        return {
            "session_id": sid,
            "question": {"text": q["text"], "options": q["options"], "difficulty": q["difficulty"]},
            "done": False,
        }

    def _select_adaptive(self, pools: Dict[str, List[Dict[str, Any]]], q6_correct: bool, q7_correct: Optional[bool]) -> List[Dict[str, Any]]:
        seq: List[str] = []
        seq.append("hard")                                        # Q6
        seq.append("hard" if q6_correct else "medium")            # Q7
        seq.append("hard" if (q7_correct is True) else ("easy" if (q7_correct is False) else "medium"))  # Q8

        chosen: List[Dict[str, Any]] = []
        for want in seq:
            order = {
                "hard":   ["hard", "medium", "easy"],
                "medium": ["medium", "easy", "hard"],
                "easy":   ["easy", "medium", "hard"],
            }[want]
            pick = self._fallback_take(pools, order, 1)
            if pick:
                # avoid duplicates
                ids = set(id(x) for x in chosen)
                p = next((x for x in pick if id(x) not in ids), None)
                if p is not None:
                    chosen.append(p)
        return chosen

    def answer(self, session_id: str, answer_index: int) -> Dict[str, Any]:
        sess = self.sessions.get(session_id)
        if not sess:
            raise ValueError("Invalid session_id")

        if sess["phase"] == 1:
            i = sess["index"]
            curr = sess["first5"][i]
            correct = (int(answer_index) == int(curr["correct_index"]))
            if correct:
                sess["first5_correct"] += 1
                sess["score"] += 1
            sess["asked"] += 1

            if i < 4:
                sess["index"] += 1
                nxt = sess["first5"][sess["index"]]
                return {"correct": correct, "done": False, "question": {"text": nxt["text"], "options": nxt["options"], "difficulty": nxt["difficulty"]}}

            # completed first 5
            passed = (sess["first5_correct"] >= 3)
            sess["unlock_next"] = passed

            if sess["first5_correct"] < 5:
                return {
                    "correct": correct,
                    "done": True,
                    "score": sess["score"],
                    "total": sess["asked"],
                    "unlock_next": passed,
                    "status": "Ready for next" if passed else "Review & retry",
                }

            # perfect 5 -> phase 2
            pools = sess["pools"]
            adaptive = self._select_adaptive(pools, q6_correct=True, q7_correct=None)
            if not adaptive:
                return {
                    "correct": correct,
                    "done": True,
                    "score": sess["score"],
                    "total": sess["asked"],
                    "unlock_next": True,
                    "status": "Ready for next",
                }

            sess["phase"] = 2
            sess["adaptive"] = adaptive
            sess["index"] = 0
            nxt = adaptive[0]
            return {"correct": correct, "done": False, "question": {"text": nxt["text"], "options": nxt["options"], "difficulty": nxt.get("difficulty", "hard")}}

        # phase 2
        i = sess["index"]
        curr = sess["adaptive"][i]
        correct = (int(answer_index) == int(curr["correct_index"]))
        if correct:
            sess["score"] += 1
        sess["asked"] += 1

        if i == 0:
            # Q6 result drives Q7
            sess["q6_result"] = correct
            pools = sess["pools"]
            rem = self._select_adaptive(pools, q6_correct=correct, q7_correct=None)
            sess["adaptive"] = rem[:2] if len(rem) >= 2 else rem
            sess["index"] = 0
            if sess["adaptive"]:
                nxt = sess["adaptive"][0]
                return {"correct": correct, "done": False, "question": {"text": nxt["text"], "options": nxt["options"], "difficulty": nxt.get("difficulty", "medium")}}
            return self._finish(sess, correct)

        elif i == 1:
            # Q7 result drives Q8
            sess["q7_result"] = correct
            pools = sess["pools"]
            q8 = self._select_adaptive(pools, q6_correct=bool(sess["q6_result"]), q7_correct=correct)
            sess["adaptive"] = q8[:1] if q8 else []
            sess["index"] = 0
            if sess["adaptive"]:
                nxt = sess["adaptive"][0]
                return {"correct": correct, "done": False, "question": {"text": nxt["text"], "options": nxt["options"], "difficulty": nxt.get("difficulty", "easy")}}
            return self._finish(sess, correct)

        else:
            return self._finish(sess, correct)

    def _finish(self, sess: Dict[str, Any], last_correct: bool) -> Dict[str, Any]:
        total = sess["asked"]
        score = sess["score"]
        unlock = sess.get("unlock_next", False) or True  # phase 2 implies unlocked already
        status = "Ready for next"
        if total >= 6:
            if total == 6 and score == 5:           status = "Confident"
            elif total == 7 and score == 6:         status = "Proficient"
            elif total == 8 and score == 7:         status = "Master"
            elif total == 8 and score == 8:         status = "Champion"
            else:                                    status = "Great progress"
        return {"correct": last_correct, "done": True, "score": score, "total": total, "unlock_next": unlock, "status": status}

QUIZ = QuizManager()

@app.post("/quiz/start")
def quiz_start(payload: Dict[str, Any] = Body(...)):
    subject = str(payload.get("subject") or "LA")
    topic   = str(payload.get("topic") or "1.1")
    try:
        return QUIZ.start(subject=subject, topic=topic)
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
# ================== END QUIZ ENGINE ==================


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