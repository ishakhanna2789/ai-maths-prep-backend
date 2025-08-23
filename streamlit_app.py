# ---- Config ----
# Use 8010 as the standard backend port.
DEFAULT_BASE = "http://localhost:8010"
BASE_URL = DEFAULT_BASE


st.set_page_config(page_title="AI-Prep Math", page_icon="🧮", layout="wide")

# ---- Session State ----
def ss_init():
    for k, v in {
        "logged_in": False,
        "user_id": None,
        "email": None,
        "subject": "LA",
        "tutorials": [],
        "selected_title": None,
        "acknowledged": False,
        "quiz_id": None,
        "question": None,
        "hint_text": None,
        "hint_used": False,
        "q_start": None,
        "score": None,
        "finished": False,
        "category": None,
    }.items():
        if k not in st.session_state:
            st.session_state[k] = v
ss_init()

# ---- API helpers ----
def api_get(path, params=None):
    try:
        r = requests.get(f"{BASE_URL}{path}", params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"GET {path} failed: {e}")
        return None

def api_post(path, payload=None):
    try:
        r = requests.post(f"{BASE_URL}{path}", json=payload, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"POST {path} failed: {e}")
        if hasattr(e, "response") and e.response is not None:
            try:
                st.code(e.response.text)
            except:
                pass
        return None

def fetch_tutorials():
    data = api_get("/tutorials", params={
        "subject": st.session_state["subject"],
        "user_id": st.session_state["user_id"]
    })
    if data:
        st.session_state["tutorials"] = data.get("tutorials", [])

def start_quiz(title):
    resp = api_post("/quiz/start", {
        "user_id": st.session_state["user_id"],
        "subject": st.session_state["subject"],
        "tutorial_title": title
    })
    if resp and "quiz_id" in resp and "question" in resp:
        st.session_state["quiz_id"] = resp["quiz_id"]
        st.session_state["question"] = resp["question"]
        st.session_state["hint_text"] = None
        st.session_state["hint_used"] = False
        st.session_state["q_start"] = time.time()
        st.session_state["finished"] = False
        st.session_state["score"] = None
        st.session_state["category"] = None
        st.success(f"Quiz started for {title}")
    else:
        st.error("Could not start quiz. Make sure the tutorial is unlocked and acknowledged.")

def get_hint():
    qid = st.session_state["quiz_id"]
    if not qid:
        st.warning("No active quiz.")
        return
    data = api_get("/quiz/hint", params={"quiz_id": qid})
    if data and "hint" in data:
        st.session_state["hint_text"] = data["hint"]
        st.session_state["hint_used"] = True

def submit_answer(chosen_letter):
    if not st.session_state["quiz_id"] or not st.session_state["question"]:
        st.warning("No active question.")
        return
    rt = int(time.time() - st.session_state["q_start"]) if st.session_state["q_start"] else 0
    payload = {
        "quiz_id": st.session_state["quiz_id"],
        "question_id": st.session_state["question"]["id"],
        "chosen": chosen_letter,
        "rt": rt,
        "hint_used": st.session_state["hint_used"],
        "edits": 0
    }
    resp = api_post("/quiz/answer", payload)
    if not resp:
        return

    if resp.get("question"):
        st.session_state["question"] = resp["question"]
        st.session_state["hint_text"] = None
        st.session_state["hint_used"] = False
        st.session_state["q_start"] = time.time()
        st.toast("Answer submitted. Next question loaded.", icon="✅")
    else:
        st.session_state["finished"] = True
        st.session_state["score"] = resp.get("score")
        st.session_state["category"] = resp.get("category")
        st.session_state["question"] = None
        st.session_state["hint_text"] = None
        st.session_state["hint_used"] = False
        st.session_state["q_start"] = None
        fetch_tutorials()

# ---- UI ----
st.sidebar.header("Login")
if not st.session_state["logged_in"]:
    with st.sidebar.form("login_form"):
        email = st.text_input("Email", value="test_user_1@example.com")
        pw = st.text_input("Password", type="password", value="test_password")
        login_btn = st.form_submit_button("Login")
    if login_btn:
        resp = api_post("/auth/login", {"email": email, "password": pw})
        if resp and resp.get("user_id"):
            st.session_state["logged_in"] = True
            st.session_state["user_id"] = resp["user_id"]
            st.session_state["email"] = resp.get("email", email)
            fetch_tutorials()
            st.success("Logged in!")
else:
    st.sidebar.write(f"**User:** {st.session_state['email']}")
    if st.sidebar.button("Logout"):
        for k in list(st.session_state.keys()):
            del st.session_state[k]
        ss_init()
        st.experimental_rerun()

st.title("AI-Prep Math • Tutorials & Adaptive Quiz")

# Silent health check
_ = api_get("/healthz")

if not st.session_state["logged_in"]:
    st.info("Please login from the left panel to continue.")
    st.stop()

cols = st.columns(3)
with cols[0]:
    subj = st.selectbox("Subject", ["LA", "Stats"], index=0 if st.session_state["subject"]=="LA" else 1)
    if subj != st.session_state["subject"]:
        st.session_state["subject"] = subj
        st.session_state["selected_title"] = None
        st.session_state["acknowledged"] = False
        fetch_tutorials()
with cols[1]:
    if st.button("Refresh tutorials"):
        fetch_tutorials()

st.subheader(f"Tutorials: {st.session_state['subject']}")
if not st.session_state["tutorials"]:
    st.warning("No tutorials found.")
else:
    for t in st.session_state["tutorials"]:
        title = t["title"]
        locked = t["locked"]
        row = st.container(border=True)
        with row:
            c1, c2, c3, c4 = st.columns([4,1,2,2])
            c1.markdown(f"**{title}** {'🔒' if locked else '🔓'}")
            if not locked:
                if c2.button("Open PDF", key=f"open_{title}"):
                    st.session_state["selected_title"] = title
                if c3.button("Acknowledge Read", key=f"ack_{title}"):
                    st.session_state["selected_title"] = title
                    st.session_state["acknowledged"] = True
                if c4.button("Start Quiz", key=f"start_{title}"):
                    if st.session_state["acknowledged"] and st.session_state["selected_title"] == title:
                        start_quiz(title)
                    else:
                        st.warning("Please open and acknowledge the tutorial before starting the quiz.")

# --- PDF viewer (updated path!) ---
if st.session_state["selected_title"]:
    st.markdown("---")
    st.subheader(f"Tutorial: {st.session_state['selected_title']}")
    pdf_url = f"{BASE_URL}/tutorials/{st.session_state['subject']}/{st.session_state['selected_title']}.pdf"
    st.markdown(f"[Open in new tab]({pdf_url})")
    st.components.v1.html(
        f'<iframe src="{pdf_url}" width="100%" height="600px"></iframe>',
        height=620,
    )

# --- Quiz panel ---
if st.session_state["quiz_id"] and not st.session_state["finished"]:
    st.markdown("---")
    st.subheader("Quiz In Progress")
    q = st.session_state["question"]
    if q:
        st.write(f"**Q:** {q['prompt']}")
        opts = q.get("options", [])
        letters = ["A","B","C","D"]
        btn_cols = st.columns(4)
        for i, opt in enumerate(opts[:4]):
            if btn_cols[i].button(f"{letters[i]}. {opt}", key=f"opt_{letters[i]}"):
                submit_answer(letters[i])

        hcols = st.columns([1,3])
        with hcols[0]:
            if q.get("has_hint", False) and st.button("Show Hint"):
                get_hint()
        with hcols[1]:
            if st.session_state["hint_text"]:
                st.info(f"Hint: {st.session_state['hint_text']}")

if st.session_state["finished"]:
    st.markdown("---")
    st.success(f"Quiz finished! Score: {st.session_state['score']}. Category: {st.session_state['category']}")
    unlocked = [t["title"] for t in st.session_state["tutorials"] if not t["locked"]]
    if unlocked:
        st.write("Unlocked tutorials:", ", ".join(unlocked))
    if st.button("Start another tutorial from list above"):
        st.session_state["quiz_id"] = None
        st.session_state["finished"] = False
        st.session_state["selected_title"] = None
        st.session_state["acknowledged"] = False