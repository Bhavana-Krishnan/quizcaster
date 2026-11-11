"""Question sets: LLM generation, file upload (CSV / JSON / XLSX), validation, approval."""
from __future__ import annotations

import csv
import io
import json
import random
from dataclasses import asdict, dataclass

from . import config, db
from .llm import LLMConfig, LLMError, acomplete, extract_json

LETTERS = ("A", "B", "C", "D")


@dataclass
class Question:
    question: str
    options: list[str]          # exactly 4, in A..D order
    answer: str                 # 'A'..'D'
    explanation: str
    id: int | None = None
    approved: bool = False

    def correct_text(self) -> str:
        return self.options[LETTERS.index(self.answer)]

    def to_dict(self) -> dict:
        return asdict(self)


class QuestionFormatError(ValueError):
    pass


# ------------------------------------------------------------------ validation
def normalize(raw: dict, where: str = "") -> Question:
    """Accepts several shapes and returns a validated Question.

    {question, options:[4], answer: 'B' | 2 | 'option text', explanation}
    {question, option_a..option_d (or a..d / A..D), answer, explanation | context | why}
    """
    def g(*keys):
        for k in keys:
            for variant in (k, k.lower(), k.upper(), k.title()):
                if variant in raw and str(raw[variant]).strip() != "":
                    return str(raw[variant]).strip()
        return ""

    q = g("question", "q")
    if isinstance(raw.get("options"), list):
        options = [str(o).strip() for o in raw["options"]]
    else:
        options = [g(f"option_{c}", f"option {c}", c, f"choice_{c}") for c in "abcd"]
    answer_raw = g("answer", "correct", "correct_answer")
    explanation = g("explanation", "context", "why", "reason")

    prefix = f"{where}: " if where else ""
    if not q:
        raise QuestionFormatError(f"{prefix}question text is missing")
    if len(options) != 4 or any(not o for o in options):
        raise QuestionFormatError(f"{prefix}needs exactly 4 non-empty options")
    if len({o.lower() for o in options}) != 4:
        raise QuestionFormatError(f"{prefix}options must all be different")
    answer = _parse_answer(answer_raw, options)
    if answer is None:
        raise QuestionFormatError(f"{prefix}answer must be A-D, 1-4, or the exact option text (got {answer_raw!r})")
    if not explanation:
        raise QuestionFormatError(f"{prefix}explanation (why it's correct) is missing")
    return Question(q, options, answer, explanation)


def _parse_answer(raw: str, options: list[str]) -> str | None:
    s = raw.strip().rstrip(".)").lower().removeprefix("option").strip()
    if s.upper() in LETTERS:
        return s.upper()
    if s in ("1", "2", "3", "4"):
        return LETTERS[int(s) - 1]
    for i, o in enumerate(options):
        if raw.strip().lower() == o.lower():
            return LETTERS[i]
    return None


def shuffle_options(q: Question, rng: random.Random | None = None) -> Question:
    """LLMs over-pick A/B as the right answer; shuffling spreads answers evenly."""
    rng = rng or random.Random()
    correct = q.correct_text()
    opts = q.options[:]
    rng.shuffle(opts)
    return Question(q.question, opts, LETTERS[opts.index(correct)], q.explanation)


# ------------------------------------------------------------------ uploads
def parse_upload(filename: str, content: bytes) -> tuple[list[Question], list[str]]:
    """Returns (valid questions, error messages for skipped rows)."""
    name = filename.lower()
    if name.endswith(".json"):
        rows = _rows_from_json(content)
    elif name.endswith(".csv"):
        rows = _rows_from_csv(content)
    elif name.endswith((".xlsx", ".xlsm")):
        rows = _rows_from_xlsx(content)
    else:
        raise QuestionFormatError("Upload a .csv, .json or .xlsx file (see the template).")
    good, errors = [], []
    for i, row in enumerate(rows, start=1):
        try:
            good.append(normalize(row, f"Row {i}"))
        except QuestionFormatError as e:
            errors.append(str(e))
    if not good and not errors:
        errors.append("The file has no questions.")
    return good, errors


def _rows_from_json(content: bytes) -> list[dict]:
    try:
        data = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise QuestionFormatError(f"Invalid JSON: {e}") from e
    if isinstance(data, dict):
        data = data.get("questions", [])
    if not isinstance(data, list):
        raise QuestionFormatError("JSON must be a list of questions or {\"questions\": [...]}")
    return [r for r in data if isinstance(r, dict)]


def _rows_from_csv(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip().lower(): v for k, v in row.items()} for row in reader]


def _rows_from_xlsx(content: bytes) -> list[dict]:
    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise QuestionFormatError("Excel support needs openpyxl (pip install openpyxl), or upload CSV.") from e
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(h or "").strip().lower() for h in rows[0]]
    return [{header[i]: ("" if v is None else v) for i, v in enumerate(r) if i < len(header)}
            for r in rows[1:] if any(v not in (None, "") for v in r)]


TEMPLATE_CSV = (
    "question,option_a,option_b,option_c,option_d,answer,explanation\n"
    "Which planet is known as the Red Planet?,Venus,Mars,Jupiter,Mercury,B,"
    "\"Iron oxide (rust) on its surface gives Mars its reddish colour.\"\n"
)


# ------------------------------------------------------------------ generation
GEN_SYSTEM = """You write multiple-choice quiz questions for a live YouTube quiz show for adults.
Rules:
- Exactly 4 options per question, exactly one correct, all plausible, all different.
- Keep the question under 120 characters and each option under 40 characters (they're shown on a phone screen).
- Facts must be accurate and not time-sensitive (avoid "current" holders, prices, records likely to change).
- The explanation is 1-2 short sentences (under 220 characters) saying why the answer is right.
- Friendly, family-safe wording. No trick questions.
Reply with ONLY JSON: {"questions":[{"question":"...","options":["...","...","...","..."],"answer":"A","explanation":"..."}]}"""


async def generate(cfg: LLMConfig, topic: str, count: int, difficulty: str = "mixed",
                   progress=None) -> tuple[list[Question], list[str]]:
    """Generate `count` questions in batches. Returns (questions, warnings)."""
    out: list[Question] = []
    warnings: list[str] = []
    seen: set[str] = set()
    attempts = 0
    while len(out) < count and attempts < (count // config.QUESTION_BATCH_SIZE + 3):
        attempts += 1
        n = min(config.QUESTION_BATCH_SIZE, count - len(out))
        avoid = "\n".join(f"- {q.question}" for q in out[-40:])
        user = (f"Topic: {topic}\nDifficulty: {difficulty}\nWrite {n} questions."
                + (f"\nDo NOT repeat these:\n{avoid}" if avoid else ""))
        try:
            data = extract_json(await acomplete(cfg, GEN_SYSTEM, user, max_tokens=350 * n + 500))
        except LLMError as e:
            if not out:
                raise
            warnings.append(f"Stopped early: {e}")
            break
        items = data.get("questions", []) if isinstance(data, dict) else data
        for i, raw in enumerate(items):
            try:
                q = shuffle_options(normalize(raw, f"Generated #{len(out) + 1}"))
            except QuestionFormatError as e:
                warnings.append(f"Skipped a malformed question ({e})")
                continue
            key = q.question.lower().strip()
            if key in seen:
                continue
            seen.add(key)
            out.append(q)
            if len(out) >= count:
                break
        if progress:
            await progress(len(out), count)
    return out, warnings


# ------------------------------------------------------------------ persistence
def create_set(name: str, topic: str | None, source: str, questions: list[Question]) -> int:
    cur = db.execute("INSERT INTO question_sets(name,topic,source,created_at) VALUES(?,?,?,?)",
                     (name, topic, source, db.now_iso()))
    set_id = cur.lastrowid
    add_questions(set_id, questions)
    return set_id


def add_questions(set_id: int, questions: list[Question]) -> None:
    start = db.query("SELECT COALESCE(MAX(position),0) AS p FROM questions WHERE set_id=?", (set_id,))[0]["p"]
    for i, q in enumerate(questions, start=start + 1):
        db.execute("INSERT INTO questions(set_id,position,question,option_a,option_b,option_c,option_d,"
                   "answer,explanation,approved) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (set_id, i, q.question, *q.options, q.answer, q.explanation, int(q.approved)))


def _row_to_q(r: dict) -> Question:
    return Question(r["question"], [r["option_a"], r["option_b"], r["option_c"], r["option_d"]],
                    r["answer"], r["explanation"], id=r["id"], approved=bool(r["approved"]))


def list_sets() -> list[dict]:
    return db.query("""SELECT s.*, COUNT(q.id) AS total, COALESCE(SUM(q.approved),0) AS approved
                       FROM question_sets s LEFT JOIN questions q ON q.set_id=s.id
                       GROUP BY s.id ORDER BY s.id DESC""")


def get_questions(set_id: int, approved_only: bool = False) -> list[Question]:
    sql = "SELECT * FROM questions WHERE set_id=?" + (" AND approved=1" if approved_only else "") + " ORDER BY position"
    return [_row_to_q(r) for r in db.query(sql, (set_id,))]


def update_question(qid: int, data: dict) -> Question:
    q = normalize(data, "Question")
    approved = int(bool(data.get("approved", False)))
    db.execute("UPDATE questions SET question=?,option_a=?,option_b=?,option_c=?,option_d=?,answer=?,"
               "explanation=?,approved=? WHERE id=?", (q.question, *q.options, q.answer, q.explanation, approved, qid))
    q.id, q.approved = qid, bool(approved)
    return q


def set_approval(set_id: int, approved: bool, qids: list[int] | None = None) -> None:
    if qids:
        marks = ",".join("?" * len(qids))
        db.execute(f"UPDATE questions SET approved=? WHERE set_id=? AND id IN ({marks})", (int(approved), set_id, *qids))
    else:
        db.execute("UPDATE questions SET approved=? WHERE set_id=?", (int(approved), set_id))


def delete_question(qid: int) -> None:
    db.execute("DELETE FROM questions WHERE id=?", (qid,))


def delete_set(set_id: int) -> None:
    db.execute("DELETE FROM question_sets WHERE id=?", (set_id,))
