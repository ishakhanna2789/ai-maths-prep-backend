from fastapi import FastAPI, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from typing import Dict, List, Any, Optional
from pathlib import Path
import uuid, re, random
from openpyxl import load_workbook

# -------------------------------------------------------------------
# App setup
# -------------------------------------------------------------------
app = FastAPI(title="AI Maths Prep API", version="0.1.0")

# Allow frontend calls (relax for dev; restrict in prod)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Paths
BASE_DIR = Path(__file__).parent
TUTORIALS_DIR = BASE_DIR / "data" / "tutorials"
QUIZ_DIR = BASE_DIR / "data" / "quizzes"

# Serve PDFs statically
app.mount("/static/tutorials", StaticFiles(directory=str(TUTORIALS_DIR)), name="tutorials")

# -------------------------------------------------------------------
# Healthcheck
# -------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    return {"status": "ok"}

# -------------------------------------------------------------------
# Tutorials endpoints
# -------------------------------------------------------------------
@app.get("/tutorials_list")
def tutorials_list(subject: str):
    """
    Return a list of tutorial PDFs for subject (LA or Stats).
    """
    subject_dir = TUTORIALS_DIR / subject
    if not subject_dir.exists():
        raise HTTPException(status_code=404, detail="Subject not found")
    files = sorted([f.name for f in subject_dir.glob("*.pdf")])
    # Minimal payload; frontend computes titles, sorting, locking.
    return files

@app.get("/tutorials_file")
def tutorials_file(subject: str, filename: str):
    """
    Return a URL for the tutorial file (static mount).
    """
    subject_dir = TUTORIALS_DIR / subject
    fpath = subject_dir / filename
    if not fpath.exists():
        raise HTTPException(status_code=404, detail="Tutorial file not found")
    return {"url": f"/static/tutorials/{subject}/{filename}"}

# -------------------------------------------------------------------
# Quiz engine (Excel-backed via openpyxl) implementing your logic
# -------------------------------------------------------------------
class QuizManager:
    """
    Phase 1 (Q1–Q5): choose only Easy/Medium.
      - If correct >= 3/5  -> unlock next, end (status "Ready for next"), unless it's a perfect 5/5.
      - If perfect 5/5     -> unlock next and continue to Phase 2 seamlessly.

    Phase 2 (Q6–Q8) — only if perfect 5/5:
      Q6 difficulty = Hard
      Q7 difficulty = Hard if Q6 correct else Medium
      Q8 difficulty = Hard if Q7 correct else Easy

    Final statuses:
      5/6 -> Confident
      6/7 -> Proficient
      7/8 -> Master
      8/8 -> Champion
    """

    def __init__(self) -> None:
        self.cache: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        self.sessions: Dict[str, Dict[str, Any]] = {}

    def _topic_keys(self, topic: str):
        """
        Returns (slug_without_ext, code) from topic string.
        topic may be '1.1_Intro_to_Vectors', '1.1_Intro_to_Vectors.pdf', or just '1.1'.
        """
        t = str(topic).strip().split("/")[-1]
        # drop .pdf if present
        if t.lower().endswith(".pdf"):
            t = t[:-4]
        slug = t  # e.g., '1.1_Intro_to_Vectors' or '1.1'
        m = re.search(r"(\d+)\.(\d+)", t)
        code = f"{int(m.group(1))}.{int(m.group(2))}" if m else slug
        return slug, code

    # -------- loading/helpers --------
    def _xlsx_path(self, subject: str) -> Path:
        s = subject.strip()
        if s not in {"LA", "Stats"}:
            raise ValueError("Invalid subject (use 'LA' or 'Stats')")
        return QUIZ_DIR / f"{s}.xlsx"

        # (intentionally unused now; kept if you referenced elsewhere)
    def _sheet_key_from_topic(self, topic: str) -> str:
        slug, code = self._topic_keys(topic)
        return code


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

        def _load_sheet(self, subject: str, slug: str, code: str) -> List[Dict[str, Any]]:
        """
        Load questions from Excel. Accept sheets named exactly like:
          - slug: '1.1_Intro_to_Vectors'
          - code: '1.1'
        Or sheets that start with / contain either.
        """
        subj_cache = self.cache.setdefault(subject, {})
        cache_key = f"{slug}||{code}"
        if cache_key in subj_cache:
            return subj_cache[cache_key]

        xlsx = self._xlsx_path(subject)
        if not xlsx.exists():
            raise FileNotFoundError(f"Quiz workbook missing: {xlsx}")

        wb = load_workbook(filename=str(xlsx), read_only=True, data_only=True)

        # ---- resolve sheet name: exact -> prefix -> contains (case-insensitive) ----
        slug_l = slug.lower()
        code_l = code.lower()
        name = None

        # exact
        if slug in wb.sheetnames:
            name = slug
        elif code in wb.sheetnames:
            name = code
        else:
            # prefix
            for s in wb.sheetnames:
                sl = s.strip().lower()
                if sl.startswith(slug_l) or sl.startswith(code_l):
                    name = s
                    break
            # contains
            if not name:
                for s in wb.sheetnames:
                    sl = s.strip().lower()
                    if slug_l in sl or code_l in sl:
                        name = s
                        break

        if not name:
            wb.close()
            raise FileNotFoundError(f"Sheet not found for topic '{slug}' or code '{code}' in {xlsx.name}")

        ws = wb[name]

        # Header
        header = [str(c).strip().lower() if c else "" for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        col = {h: i for i, h in enumerate(header)}

        def cell(row, name):
            i = col.get(name)
            return row[i] if i is not None and i < len(row) else None

        required = ["question text", "option a", "option b", "option c", "option d", "correct answer", "difficulty"]
        miss = [r for r in required if r not in col]
        if miss:
            wb.close()
            raise ValueError(f"Missing columns {miss} in sheet '{name}' of {xlsx.name}")

        # Optional columns
        optional_names = {
            "hint": "hint",
            "explanation": "explanation",
            "image_url": "image link (optional)",
            "qid": "question id",
            "module": "module",
            "tags": "tags",
            "type": "question type",
        }

        items: List[Dict[str, Any]] = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            qtext = cell(row, "question text")
            if qtext is None or str(qtext).strip() == "":
                continue
            options = [
                "" if cell(row, "option a") is None else str(cell(row, "option a")),
                "" if cell(row, "option b") is None else str(cell(row, "option b")),
                "" if cell(row, "option c") is None else str(cell(row, "option c")),
                "" if cell(row, "option d") is None else str(cell(row, "option d")),
            ]
            correct = self._idx_from_correct(cell(row, "correct answer"), options)

            diff_raw = str(cell(row, "difficulty") or "").strip().lower()
            if diff_raw.startswith("e"):   diff = "easy"
            elif diff_raw.startswith("m"): diff = "medium"
            elif diff_raw.startswith("h"): diff = "hard"
            else:                          diff = "medium"

            item = {"text": str(qtext), "options": options, "correct_index": correct, "difficulty": diff}

            for k, header_name in optional_names.items():
                j = col.get(header_name)
                if j is not None and j < len(row) and row[j] is not None:
                    item[k] = str(row[j])

            items.append(item)

        wb.close()

        if not items:
            raise ValueError(f"No questions in sheet '{name}' of {xlsx.name}")

        subj_cache[cache_key] = items
        return items


        xlsx = self._xlsx_path(subject)
        if not xlsx.exists():
            raise FileNotFoundError(f"Quiz workbook missing: {xlsx}")

        wb = load_workbook(filename=str(xlsx), read_only=True, data_only=True)
        if sheet_key not in wb.sheetnames:
            raise FileNotFoundError(f"Sheet '{sheet_key}' missing in {xlsx.name}")
        ws = wb[sheet_key]

        # Header
        header = [str(c).strip().lower() if c else "" for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
        col = {h: i for i, h in enumerate(header)}

        def cell(row, name):
            i = col.get(name)
            return row[i] if i is not None and i < len(row) else None

        required = ["question text", "option a", "option b", "option c", "option d", "correct answer", "difficulty"]
        miss = [r for r in required if r not in col]
        if miss:
            raise ValueError(f"Missing columns {miss} in sheet '{sheet_key}' of {xlsx.name}")

        # Optional columns (not required by the UI but supported)
        optional_names = {
            "hint": "hint",
            "explanation": "explanation",
            "image_url": "image link (optional)",
            "qid": "question id",
            "module": "module",
            "tags": "tags",
            "type": "question type",
        }

        items: List[Dict[str, Any]] = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            qtext = cell(row, "question text")
            if qtext is None or str(qtext).strip() == "":
                continue
            options = [
                "" if cell(row, "option a") is None else str(cell(row, "option a")),
                "" if cell(row, "option b") is None else str(cell(row, "option b")),
                "" if cell(row, "option c") is None else str(cell(row, "option c")),
                "" if cell(row, "option d") is None else str(cell(row, "option d")),
            ]
            correct = self._idx_from_correct(cell(row, "correct answer"), options)

            diff_raw = str(cell(row, "difficulty") or "").strip().lower()
            if diff_raw.startswith("e"):   diff = "easy"
            elif diff_raw.startswith("m"): diff = "medium"
            elif diff_raw.startswith("h"): diff = "hard"
            else:                          diff = "medium"

            item = {"text": str(qtext), "options": options, "correct_index": correct, "difficulty": diff}

            # Attach optional metadata if present
            for k, header_name in optional_names.items():
                j = col.get(header_name)
                if j is not None and j < len(row) and row[j] is not None:
                    item[k] = str(row[j])

            items.append(item)

        wb.close()

        if not items:
            raise ValueError(f"No questions in sheet '{sheet_key}' of {xlsx.name}")

        subj_cache[sheet_key] = items
        return items

    # -------- pool helpers --------
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

    def _select_adaptive(self, pools: Dict[str, List[Dict[str, Any]]], q6_correct: bool, q7_correct: Optional[bool]) -> List[Dict[str, Any]]:
        """
        Build Q6–Q8 difficulties per your rules, with safe fallbacks if a pool is short.
        """
        seq: List[str] = []
        seq.append("hard")                                        # Q6
        seq.append("hard" if q6_correct else "medium")            # Q7
        seq.append("hard" if (q7_correct is True) else ("easy" if (q7_correct is False) else "medium"))  # Q8

        chosen: List[Dict[str, Any]] = []
        for want in seq:
            order = {"hard": ["hard", "medium", "easy"], "medium": ["medium", "easy", "hard"], "easy": ["easy", "medium", "hard"]}[want]
            pick = self._fallback_take(pools, order, 1)
            if pick:
                ids = set(id(x) for x in chosen)  # avoid duplicates
                p = next((x for x in pick if id(x) not in ids), None)
                if p is not None:
                    chosen.append(p)
        return chosen

    # -------- public API --------
    def start(self, subject: str, topic: str) -> Dict[str, Any]:
        slug, code = self._topic_keys(topic)
        all_items = self._load_sheet(subject, slug, code)

        #all_items = self._load_sheet(subject, sheet_key)
        pools = self._split_pools(all_items)

        # Phase 1: 5 questions from Easy/Medium only
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

    def answer(self, session_id: str, answer_index: int) -> Dict[str, Any]:
        sess = self.sessions.get(session_id)
        if not sess:
            raise ValueError("Invalid session_id")

        # ---------- Phase 1 ----------
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
                return {
                    "correct": correct,
                    "done": False,
                    "question": {"text": nxt["text"], "options": nxt["options"], "difficulty": nxt["difficulty"]},
                }

            # i == 4 -> finished first 5
            passed = (sess["first5_correct"] >= 3)
            sess["unlock_next"] = passed

            if sess["first5_correct"] < 5:
                # End now: either Ready for next (>=3/5) or Review & retry (<3/5)
                return {
                    "correct": correct,
                    "done": True,
                    "score": sess["score"],
                    "total": sess["asked"],
                    "unlock_next": passed,
                    "status": "Ready for next" if passed else "Review & retry",
                }

            # Perfect 5/5 -> continue to Phase 2
            pools = sess["pools"]
            adaptive = self._select_adaptive(pools, q6_correct=True, q7_correct=None)
            if not adaptive:
                # No adaptive questions available; finish gracefully
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
            nxt = adaptive[0]  # Q6
            return {
                "correct": correct,
                "done": False,
                "question": {"text": nxt["text"], "options": nxt["options"], "difficulty": nxt.get("difficulty", "hard")},
            }

        # ---------- Phase 2 ----------
        i = sess["index"]
        curr = sess["adaptive"][i]
        correct = (int(answer_index) == int(curr["correct_index"]))
        if correct:
            sess["score"] += 1
        sess["asked"] += 1

        if i == 0:
            # After Q6 -> pick Q7 based on Q6 result
            sess["q6_result"] = correct
            pools = sess["pools"]
            rem = self._select_adaptive(pools, q6_correct=correct, q7_correct=None)
            sess["adaptive"] = rem[:2] if len(rem) >= 2 else rem  # Q7, Q8 candidates
            sess["index"] = 0
            if sess["adaptive"]:
                nxt = sess["adaptive"][0]  # Q7
                return {
                    "correct": correct,
                    "done": False,
                    "question": {"text": nxt["text"], "options": nxt["options"], "difficulty": nxt.get("difficulty", "medium")},
                }
            return self._finish(sess, correct)

        elif i == 1:
            # After Q7 -> pick Q8 based on Q7 result
            sess["q7_result"] = correct
            pools = sess["pools"]
            q8 = self._select_adaptive(pools, q6_correct=bool(sess["q6_result"]), q7_correct=correct)
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

        else:
            # After Q8 -> finish
            return self._finish(sess, correct)

    def _finish(self, sess: Dict[str, Any], last_correct: bool) -> Dict[str, Any]:
        total = sess["asked"]
        score = sess["score"]
        # If we reached Phase 2 we already unlocked; otherwise use unlock_next set after Phase 1.
        unlock = sess.get("unlock_next", False) or (sess.get("phase") == 2)

        # Status mapping for extended rounds
        status = "Ready for next"
        if total >= 6:
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