"""
Gemini helper for the Notion Photo Uploader.

Uses the official Gemini REST endpoint (generativelanguage.googleapis.com) with plain
'requests', structured JSON output, retries and model fallback. No SDK needed.
The API key is sent only in the x-goog-api-key header and is never printed.
"""

import base64
import difflib
import io
import json
import os
import re
import time

import requests

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_FALLBACKS = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]


class GeminiError(Exception):
    """Gemini could not give a usable answer after all retries."""


class GeminiKeyError(GeminiError):
    """The key itself is wrong or not allowed. Retrying will not help."""


# ----------------------------------------------------------------- text cleanup

_ARROWS = re.compile(r"\s*(?:->|=>|-->|<-|\u2190|\u2192|\u2191|\u2193|\u21d2|\u21d0|\u27f6|\u2794|\u279c|\\rightarrow|\\to|\\Rightarrow)\s*")


def clean_text(s):
    """Plain words only: no arrows, no LaTeX, no dollar math, no em dashes (Avi's rules)."""
    if not isinstance(s, str):
        return s
    s = _ARROWS.sub(" to ", s)
    s = re.sub(r"\$([^$]{0,80})\$", r"\1", s)
    s = s.replace("\u2014", ", ").replace(" ,", ",")
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def clean_obj(o):
    if isinstance(o, str):
        return clean_text(o)
    if isinstance(o, list):
        return [clean_obj(x) for x in o]
    if isinstance(o, dict):
        return {k: clean_obj(v) for k, v in o.items()}
    return o


# ----------------------------------------------------------------- schemas

_PHOTO = {
    "type": "OBJECT",
    "properties": {
        "photo": {"type": "INTEGER", "description": "Photo number as given (1, 2, 3 ...)."},
        "starts_new_answer": {"type": "BOOLEAN", "description": "True if a NEW question is written at the top of this photo, so a new answer begins here. False if the page simply continues the answer on the previous photo."},
        "question_at_top": {"type": "STRING", "description": "The question text written at the top of this photo, copied exactly, or empty string if the page is a continuation."},
        "first_words": {"type": "STRING", "description": "The first few words written on this photo (up to 12 words)."},
    },
    "required": ["photo", "starts_new_answer", "question_at_top", "first_words"],
}

_PHOTO_TEXT = {"type": "STRING", "description": (
    "Everything handwritten on this photo, transcribed word for word in reading order (headings, points, "
    "labels). Keep line breaks between points. Write [diagram: what it shows] for a diagram or map, and "
    "[unclear] for a word you cannot read. Do not correct or improve the answer.")}

_ANSWER = {
    "type": "OBJECT",
    "properties": {
        "question_written": {"type": "STRING", "description": "The question written at the top of this answer's first page, copied exactly, or empty string if none is written."},
        "photos": {"type": "ARRAY", "items": {"type": "INTEGER"}, "description": "Photo numbers of this answer in correct READING order (decided from the content, not from the order sent)."},
        "looks_complete": {"type": "BOOLEAN", "description": "False only if a page is clearly missing: the first page has no question and starts mid-sentence, or the last page stops mid-sentence or mid-point with no page that continues it."},
        "note": {"type": "STRING", "description": "If looks_complete is false: one short sentence saying what seems missing. Otherwise empty."},
    },
    "required": ["question_written", "photos", "looks_complete", "note"],
}

READ_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "answers": {"type": "ARRAY", "items": _ANSWER, "description": "Every separate answer in the batch, each photo in exactly one answer."},
        "photos": {"type": "ARRAY", "items": _PHOTO, "description": "One entry for EVERY photo, in the order given."},
        "question_written": {"type": "STRING", "description": "The exam question exactly as written on the pages, or empty string if no question is written."},
        "question_guess": {"type": "STRING", "description": "If no question is written: one sentence describing what question this answer seems to address. Otherwise repeat the written question."},
        "page_order": {"type": "ARRAY", "items": {"type": "INTEGER"}, "description": "Photo numbers in correct reading order, each photo exactly once."},
        "answer_summary": {"type": "STRING", "description": "Short summary of what the (first) answer covers (main headings, thinkers, diagrams), under 120 words."},
        "legible": {"type": "BOOLEAN"},
    },
    "required": ["answers", "photos", "question_written", "question_guess", "page_order", "answer_summary", "legible"],
}


def read_schema(transcribe=False):
    """READ_SCHEMA, optionally with a word-for-word transcription of every photo (same single request)."""
    if not transcribe:
        return READ_SCHEMA
    photo = dict(_PHOTO, properties=dict(_PHOTO["properties"], text=_PHOTO_TEXT),
                 required=_PHOTO["required"] + ["text"])
    photos = dict(READ_SCHEMA["properties"]["photos"], items=photo)
    return dict(READ_SCHEMA, properties=dict(READ_SCHEMA["properties"], photos=photos))

PICK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "choice": {"type": "INTEGER", "description": "Number of the best matching candidate (1 to 5), or 0 if none matches."},
        "reason": {"type": "STRING"},
    },
    "required": ["choice", "reason"],
}

_POINT = {"type": "OBJECT", "properties": {"point": {"type": "STRING"}, "detail": {"type": "STRING"}},
          "required": ["point", "detail"]}

EVAL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "page_order": {"type": "ARRAY", "items": {"type": "INTEGER"}},
        "score": {"type": "NUMBER", "description": "Marks awarded, in steps of 0.5."},
        "out_of": {"type": "NUMBER"},
        "misread_demand": {"type": "BOOLEAN", "description": "True only if the answer misread or missed the core demand of the question."},
        "demand_check": {"type": "STRING", "description": "One or two sentences: what the question demands and whether each part was covered."},
        "strengths": {"type": "ARRAY", "items": _POINT},
        "mistakes": {"type": "ARRAY", "items": _POINT},
        "remark": {"type": "STRING", "description": "One compact paragraph under 900 characters for the Gemini Remark column."},
        "card_name": {"type": "STRING", "description": "Remarks card title: topic, thinkers or year, and the two or three key fixes."},
        "card_sections": {"type": "ARRAY", "items": {
            "type": "OBJECT",
            "properties": {"heading": {"type": "STRING"}, "bullets": {"type": "ARRAY", "items": {"type": "STRING"}}},
            "required": ["heading", "bullets"]}},
        "chat_summary": {"type": "STRING", "description": "Three to five plain sentences for Avi: score, what worked, what to fix."},
    },
    "required": ["page_order", "score", "out_of", "misread_demand", "demand_check", "strengths", "mistakes",
                 "remark", "card_name", "card_sections", "chat_summary"],
}

# ----------------------------------------------------------------- prompts
# Taken from how the Notion handler bot evaluated (its handoff notes and real Evaluation sections).

STYLE_RULES = (
    "Write in plain English only. Never use arrows, LaTeX, dollar-sign math or em dashes; use words such as "
    "'leads to', 'then', 'because'. Never use Hindi."
)

READ_SYSTEM = (
    "You read photos of handwritten UPSC Civil Services Mains answers (Geography Optional or General Studies). "
    "IMPORTANT: one batch of photos may hold SEVERAL DIFFERENT answers sent together, one after another, in the "
    "order given. There are usually no question numbers or page numbers. Decide purely from the content of each "
    "photo: a new answer starts on a photo whose top has a new question written out (often a quoted or underlined "
    "question, sometimes with the year or word limit); a photo that carries on the sentences, headings or "
    "diagrams of the previous photo is a continuation. For EVERY photo, fill one entry in 'photos': "
    "starts_new_answer, the question at its top copied exactly (empty for continuations) and its first words. "
    "The photos may be SHUFFLED (the camera or Telegram can change their order), so never trust the order sent: "
    "group and order them purely from the content (the question at the top of a first page, then pages whose "
    "sentences, numbering, headings and diagrams continue each other). List every answer in 'answers' with its "
    "photos in reading order; every photo goes in exactly one answer. Set looks_complete to false only when a "
    "page is clearly missing (a first page without its question that starts mid-sentence, or a last page that "
    "stops mid-sentence with no page continuing it). A short or weak answer is still complete. "
    "Do not merge two different questions into one answer. "
    "question_written, question_guess and answer_summary describe the FIRST answer. page_order lists all photo "
    "numbers in reading order (keep the given order unless the photos of a single answer are clearly shuffled). "
    + STYLE_RULES
)

READ_TRANSCRIBE = (
    " Also fill 'text' for every photo: a faithful word-for-word transcription of the handwriting on that photo, "
    "so the answer can be evaluated later from text alone. Copy the student's words exactly, even mistakes; "
    "the plain-English style rules above apply only to your own descriptions, not to this transcription."
)

EVAL_SYSTEM = """You are a strict, honest UPSC Civil Services examiner for Geography Optional, acting as Avi's answer evaluator.

Marking scale: a 150-word answer is out of 10, 200 words out of 15, 250 words out of 20. Be strict like a real UPSC examiner. Typical good scores sit around 45 to 60 percent of the total. Give marks in steps of 0.5.

How to evaluate:
1. Check the demand of the question first. Does the answer address every part (for example "causes, approaches and criticism")? Missing a part is the biggest deduction. If the answer misreads the core demand, set misread_demand to true.
2. Fact-check every claim: names, dates, books, what a thinker actually said. If Avi is wrong, say so clearly and give the correct version. Never build on a wrong foundation just to agree with him.
3. Attribute ideas to named thinkers with years, for example "David Harvey (1973)", not "Marxists argued". Only suggest thinkers and models that appear in the UPSC Geography Optional syllabus or past questions. Fewer thinkers, explained properly; no long lists of obscure names.
4. Suggest a diagram wherever one would earn marks, and say exactly what it should show.
5. Check structure: a contextual introduction (the problem or observation that raised the idea), a body that maps to the question's parts with clear headings, and a balanced one or two line conclusion. Suggest one precise Indian example where it fits.
6. Watch his recurring weak spots: too much background and too little core demand; multi-part questions not split into headed parts; missing the internal disciplinary critique; small pen and spelling slips under time pressure (list them).
7. Keep mistakes and knowledge gaps visible. Never hide or soften them.

Output layout:
- strengths: 3 to 5 items. 'point' is a short bold label, 'detail' is one or two sentences.
- mistakes: numbered fixes in order of marks lost. 'point' is a short bold statement of the problem, 'detail' gives the correct content Avi should write (names, books, years, the missing part, the diagram).
- remark: one compact paragraph in this style: "Score: 7/15 (200 words, 2018). Strengths: ...; .... Fix: ...; ...".
- card_name: like "Sense of place: Tuan and Relph (2018), space vs place, Relph's 7 modes, placelessness".
- card_sections: 3 or 4 crisp revision sections. Headings like "Tuan: open with space versus place" or "Framing and slips". Each has 2 to 4 short bullets of pure content to remember. No full sentences of praise.
- chat_summary: plain sentences for Avi.

""" + STYLE_RULES


EVAL_SYSTEM_GS = """You are a strict, honest UPSC Civil Services Mains examiner for General Studies and the Essay paper, acting as Avi's answer evaluator.

Marking: use the marks given with the question. GS1 to GS3 answers are 10 marks for 150 words or 15 marks for 250 words; GS4 questions are 10 or 20 marks, case studies usually 20 marks for 250 words; an essay is 125 marks for about 1000 to 1200 words. Be strict like a real UPSC examiner. Typical good GS scores sit around 40 to 50 percent of the total (4 to 5 out of 10, 6 to 7.5 out of 15); an essay rarely gets above 60 percent. Give marks in steps of 0.5.

How to evaluate:
1. Check the demand of the question first: the directive word (discuss, examine, critically analyse, comment) and every part asked. Missing a part is the biggest deduction. If the answer misreads the core demand, set misread_demand to true.
2. Fact-check every claim: articles of the Constitution, judgments, committee and commission reports, schemes, data, dates and names. If Avi is wrong, say so clearly and give the correct version.
3. Judge content by the paper:
   - GS1 (history, culture, society, world and Indian geography): correct chronology and facts, causes and consequences, a map or diagram where it earns marks, Indian examples.
   - GS2 (polity, governance, social justice, international relations): constitutional provisions and articles, Supreme Court judgments, committee reports (for example ARC, Punchhi), recent developments, a balanced way forward.
   - GS3 (economy, agriculture, environment, science and technology, security, disaster management): data from the Economic Survey or official reports, government schemes and laws, multidimensional analysis, a flowchart or diagram where it helps.
   - GS4 (ethics): define the key terms, use thinkers and examples from public life; for case studies identify the stakeholders and ethical issues, list the options with their merits and demerits, choose a course of action and justify it.
   - Essay: a clear thesis, multidimensional coverage (social, economic, political, ethical, historical, global), a strong introduction (anecdote or quote) and a hopeful conclusion, flow between paragraphs, counter views.
4. Use the topic given to judge which syllabus area the answer must show depth in.
5. Check structure: a short introduction tied to the demand, a body with clear subheadings or points that map to the question's parts, and a forward-looking conclusion. Suggest one precise Indian example, report or data point where it fits.
6. Watch for: too much background and too little core demand; points without examples or evidence; missing way forward; going over the word limit; small spelling slips under time pressure (list them).
7. Keep mistakes and knowledge gaps visible. Never hide or soften them.

Output layout:
- strengths: 3 to 5 items. 'point' is a short bold label, 'detail' is one or two sentences.
- mistakes: numbered fixes in order of marks lost. 'point' is a short bold statement of the problem, 'detail' gives the correct content Avi should write (articles, cases, reports, data, the missing part, the diagram).
- remark: one compact paragraph in this style: "Score: 4.5/10 (GS2, 150 words, 2019). Strengths: ...; .... Fix: ...; ...".
- card_name: like "GS2 2019 Governor's discretion: Art 163, Nabam Rebia (2016), Punchhi Commission".
- card_sections: 3 or 4 crisp revision sections, each with 2 to 4 short bullets of pure content to remember. No full sentences of praise.
- chat_summary: plain sentences for Avi.

""" + STYLE_RULES


# ----------------------------------------------------------------- matching

_STOP = set("""a an the of in on to and or for by with as at from is are was were be been its it this that these those
their his her what which how why who whom whose discuss examine critically explain describe elaborate comment
analyse analyze evaluate bring out give account brief outline note write short notes upon about into between
your you do does did has have had not no can could should would may might will shall also than then""".split())


def normalize(s):
    s = (s or "").lower()
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _words(s):
    return {w for w in normalize(s).split() if w not in _STOP and len(w) > 2}


def similarity(query, title):
    a, b = normalize(query), normalize(title)
    if not a or not b:
        return 0.0
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    qa, tb = _words(query), _words(title)
    cover = len(qa & tb) / len(qa) if qa else 0.0
    back = len(qa & tb) / len(tb) if tb else 0.0
    return round(0.4 * seq + 0.35 * cover + 0.25 * back, 4)


def top_matches(query, candidates, n=5):
    """candidates: list of dicts with at least 'title'. Returns [(score, cand)] best first."""
    scored = [(similarity(query, c["title"]), c) for c in candidates]
    scored.sort(key=lambda x: -x[0])
    return scored[:n]


# ----------------------------------------------------------------- client

MAX_INLINE_BYTES = 12 * 1024 * 1024  # photos per Gemini request (base64 adds a third; Gemini's limit is about 20 MB)
READ_SIDE = 1280  # long side of the copies used only to read and split a batch (enough for handwriting)
TRANSCRIBE_MAX_PHOTOS = 16  # bigger batches are read without transcription so the one reply stays fast


def _b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _smaller_jpeg(path, steps, budget):
    """Smaller JPEG copy in memory: tries each long side in steps until it fits the byte budget."""
    try:
        from PIL import Image
    except ImportError:  # Pillow missing: send the file as it is
        with open(path, "rb") as f:
            return f.read()
    data = b""
    with Image.open(path) as im:
        im = im.convert("RGB")
        for side in steps:
            w = im.copy()
            w.thumbnail((side, side))
            buf = io.BytesIO()
            w.save(buf, "JPEG", quality=75, optimize=True)
            data = buf.getvalue()
            if len(data) <= budget:
                break
    return data


def valid_order(order, n):
    try:
        order = [int(x) for x in order]
    except Exception:
        return None
    return order if sorted(order) == list(range(1, n + 1)) else None


def repair_order(order, n):
    """A usable reading order even when Gemini's list is imperfect (a photo missing, repeated or out of range):
    keeps the first valid mention of each photo, then adds any missing ones in the order they were sent.
    Gemini often gets this list slightly wrong with many photos; that must never block an evaluation."""
    out, seen = [], set()
    for x in order if isinstance(order, (list, tuple)) else []:
        try:
            k = int(x)
        except (TypeError, ValueError):
            continue
        if 1 <= k <= n and k not in seen:
            seen.add(k)
            out.append(k)
    return out + [k for k in range(1, n + 1) if k not in seen]


def _content_groups(read, n):
    """Groups from Gemini's 'answers' list (content based, so shuffled photos are fine). Every photo ends up in
    exactly one group; photos Gemini placed nowhere (or twice) form an extra group marked incomplete.
    Returns None when the reply has no usable 'answers' list."""
    answers = (read or {}).get("answers")
    if not isinstance(answers, list) or not answers:
        return None
    used, groups = set(), []
    for a in answers:
        if not isinstance(a, dict):
            continue
        nums = []
        for x in a.get("photos") or []:
            try:
                k = int(x)
            except (TypeError, ValueError):
                continue
            if 1 <= k <= n and k not in used:
                used.add(k)
                nums.append(k)
        if not nums:
            continue
        complete = a.get("looks_complete") is not False
        groups.append({"photos": nums, "question_written": clean_text((a.get("question_written") or "").strip()),
                       "incomplete": not complete, "note": clean_text(a.get("note") or "") if not complete else ""})
    if not groups:
        return None
    left = [k for k in range(1, n + 1) if k not in used]
    if left:
        groups.append({"photos": left, "question_written": "", "incomplete": True,
                       "note": "Gemini could not tell which answer these pages belong to."})
    return groups


def group_photos(read, n, hard_breaks=()):
    """Splits a batch of n photos into separate answers, keeping the order they were sent.
    read: the reply of read_answer (its 'photos' list says where a new question starts).
    hard_breaks: photo numbers where Avi forced a new answer (he sent 'next' between photos).
    Returns a list of dicts: photos (1-based numbers), question_written, read (a read_answer style dict or None
    when the question is not known and the group has to be read on its own)."""
    texts_by = {}
    for e in (read or {}).get("photos") or []:
        try:
            texts_by.setdefault(int(e.get("photo")), e.get("text") or "")
        except (TypeError, ValueError, AttributeError):
            pass
    content = None if hard_breaks else _content_groups(read, n)  # 'next' from Avi means: trust the order sent
    if content is not None:
        first_q = clean_text((read or {}).get("question_written") or "")
        for gi, g in enumerate(content):
            q = g["question_written"] or (first_q if gi == 0 and len(content) == 1 else "")
            g["read"] = None
            if q:
                g["read"] = {"question_written": q, "question_guess": q,
                             "answer_summary": (read or {}).get("answer_summary", "") if gi == 0 else "",
                             "page_order": list(range(1, len(g["photos"]) + 1)), "legible": True,
                             "photo_texts": [texts_by.get(k, "") for k in g["photos"]]}
            elif len(content) == 1 and (read or {}).get("question_guess"):
                g["read"] = {"question_written": "", "question_guess": read["question_guess"],
                             "answer_summary": read.get("answer_summary", ""),
                             "page_order": list(range(1, len(g["photos"]) + 1)), "legible": True,
                             "photo_texts": [texts_by.get(k, "") for k in g["photos"]]}
        return content
    info = {}
    for e in (read or {}).get("photos") or []:
        try:
            k = int(e.get("photo"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= k <= n and k not in info:
            info[k] = e
    starts = {1} | {int(b) for b in hard_breaks or () if 1 <= int(b) <= n}
    current_q = ""
    for k in range(1, n + 1):
        e = info.get(k) or {}
        qtop = clean_text((e.get("question_at_top") or "").strip())
        if k == 1 or k in starts:
            current_q = qtop
            continue
        if e.get("starts_new_answer") and qtop:
            starts.add(k)
            current_q = qtop
        elif qtop and current_q and similarity(qtop, current_q) < 0.5 and len(_words(qtop)) >= 4:
            starts.add(k)  # a different question at the top even though the flag was missed
            current_q = qtop
        elif qtop and not current_q:
            current_q = qtop
    bounds = sorted(starts) + [n + 1]
    groups = []
    for a, b in zip(bounds, bounds[1:]):
        photos = list(range(a, b))
        qtop = clean_text(((info.get(a) or {}).get("question_at_top") or "").strip())
        groups.append({"photos": photos, "question_written": qtop, "read": None})
    texts = {k: (e.get("text") or "") for k, e in info.items()}
    if len(groups) == 1:
        one = dict(read or {})
        one["page_order"] = repair_order(one.get("page_order"), n)
        one["photo_texts"] = [texts.get(k, "") for k in range(1, n + 1)]
        groups[0]["read"] = one if (one.get("question_written") or one.get("question_guess")) else None
        groups[0]["question_written"] = one.get("question_written") or groups[0]["question_written"]
        return groups
    first_q = clean_text((read or {}).get("question_written") or "")
    for g in groups:
        q = g["question_written"]
        if g["photos"][0] == 1 and not q:
            q = first_q
        if q:
            summary = (read or {}).get("answer_summary", "") if g["photos"][0] == 1 else ""
            g["read"] = {"question_written": q, "question_guess": q, "answer_summary": summary,
                         "page_order": list(range(1, len(g["photos"]) + 1)), "legible": True,
                         "photo_texts": [texts.get(k, "") for k in g["photos"]]}
    return groups


def photo_texts(read, n):
    """The transcription of each of the n photos (in the order sent), from a read_answer reply or a group's read.
    Empty strings when Gemini gave none (transcription off, or an old reply)."""
    read = read or {}
    got = read.get("photo_texts")
    if isinstance(got, list) and len(got) == n:
        return [t if isinstance(t, str) else "" for t in got]
    out = [""] * n
    for e in read.get("photos") or []:
        try:
            k = int(e.get("photo"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= k <= n and not out[k - 1] and isinstance(e.get("text"), str):
            out[k - 1] = e["text"]
    return out


class GeminiClient:
    def __init__(self, api_key, model=DEFAULT_MODEL, fallbacks=None, timeout=100, retries=4,
                 session=None, sleep=time.sleep, log=print, budget=None, clock=time.monotonic):
        self.key = (api_key or "").strip()
        self.models = [model] + [m for m in (fallbacks if fallbacks is not None else DEFAULT_FALLBACKS) if m != model]
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.s = session or requests.Session()
        self.sleep = sleep
        self.log = log
        self.used_model = None
        # Total seconds one request may take across all retries and models. After that the answer is
        # left as 'Evaluation pending' for the retry queue instead of blocking the bot for many minutes.
        self.budget = float(budget if budget is not None else os.environ.get("GEMINI_BUDGET_SECONDS", 300))
        self.clock = clock

    def _redact(self, text):
        text = str(text)
        return text.replace(self.key, "[key hidden]") if self.key else text

    def check(self):
        """Cheap key test that does not use generation quota."""
        model = self.models[0]
        try:
            r = self.s.get(f"{GEMINI_API}/models/{model}", headers={"x-goog-api-key": self.key}, timeout=30)
        except requests.exceptions.RequestException as e:
            raise GeminiError(self._redact(f"Could not reach Gemini: {e}"))
        if r.status_code == 200:
            return model
        if r.status_code in (400, 401, 403):
            raise GeminiKeyError("GEMINI KEY problem: the API key is not valid. Make a new one at aistudio.google.com "
                                 "(Get API key).")
        if r.status_code == 404:
            raise GeminiError(f"GEMINI MODEL problem: model '{model}' was not found. Change gemini_model in config.json "
                              "(or GEMINI_MODEL on Render).")
        raise GeminiError(f"Gemini check failed (HTTP {r.status_code}).")

    def generate_json(self, system, parts, schema, validate=None, what="Gemini"):
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema,
                                 "temperature": 0.2},
        }
        last = "unknown problem"
        start = self.clock()

        def left():
            return self.budget - (self.clock() - start) if self.budget > 0 else float("inf")

        def nap(sec):
            self.sleep(max(0, min(sec, left())))

        for mi, model in enumerate(self.models):
            url = f"{GEMINI_API}/models/{model}:generateContent"
            more = mi < len(self.models) - 1  # another model left to fall back to
            for attempt in range(self.retries):
                if left() <= 5:
                    raise GeminiError(f"{what} took too long ({last}); it will be tried again later")
                wait = min(60, 4 * (2 ** attempt))
                try:
                    r = self.s.post(url, headers={"x-goog-api-key": self.key, "Content-Type": "application/json"},
                                    json=body, timeout=min(self.timeout, max(10, left())))
                except requests.exceptions.RequestException as e:
                    last = self._redact(f"network problem: {e.__class__.__name__}")
                    if more:  # a slow or overloaded model: the next model is usually quicker
                        self.log(f"  {what}: {last}, trying the next model")
                        break
                    self.log(f"  {what}: {last}, retrying in {wait} s")
                    nap(wait)
                    continue
                status = r.status_code
                if status == 200:
                    try:
                        data = self._parse(r.json())
                        if validate:
                            validate(data)
                        self.used_model = model
                        return clean_obj(data)
                    except (ValueError, KeyError, TypeError) as e:
                        last = f"unusable reply ({self._redact(e)})"
                        self.log(f"  {what}: {last}, asking again")
                        nap(2)
                        continue
                err = self._error_text(r)
                if status in (400, 401, 403) and ("api key" in err.lower() or "permission" in err.lower()
                                                  or status in (401, 403)):
                    raise GeminiKeyError("GEMINI KEY problem: Gemini rejected the API key (" + err + "). Make a new key at "
                                         "aistudio.google.com (Get API key) and put it in config.json or GEMINI_API_KEY.")
                if status == 404 or (status == 400 and ("not found" in err.lower() or "not supported" in err.lower())):
                    last = f"model {model} not available"
                    self.log(f"  {what}: {last}, trying the next model")
                    break
                if status == 429:
                    if "per day" in err.lower() or "perday" in err.lower().replace(" ", ""):
                        last = f"daily free limit reached for {model}"
                        self.log(f"  {what}: {last}, trying the next model")
                        break
                    delay = self._retry_delay(r) or wait
                    last = "rate limit (429)"
                    self.log(f"  {what}: busy (429), waiting {delay:.0f} s")
                    nap(min(delay, 90))
                    continue
                if status >= 500:
                    last = f"Gemini server error {status}"
                    if more and (attempt >= 1 or status == 503):  # overloaded: move on instead of waiting on this model
                        self.log(f"  {what}: {last}, trying the next model")
                        break
                    self.log(f"  {what}: {last}, retrying in {wait} s")
                    nap(wait)
                    continue
                last = f"HTTP {status}: {err}"
                break
        if "not available" in last:
            raise GeminiError(f"GEMINI MODEL problem: none of the models {', '.join(self.models)} is available any more. "
                              "Change gemini_model in config.json (or GEMINI_MODEL on Render) to a current Flash model "
                              "listed at ai.google.dev/gemini-api/docs/models.")
        raise GeminiError(f"{what} failed after retries: {last}")

    @staticmethod
    def _parse(resp):
        cands = resp.get("candidates") or []
        if not cands:
            reason = (resp.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise ValueError(f"empty reply ({reason})")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?|```$", "", text).strip()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("reply is not a JSON object")
        return data

    def _error_text(self, r):
        try:
            return self._redact((r.json().get("error") or {}).get("message", ""))[:300]
        except Exception:
            return self._redact(r.text[:300])

    @staticmethod
    def _retry_delay(r):
        try:
            for d in (r.json().get("error") or {}).get("details", []):
                if "retryDelay" in d:
                    return float(str(d["retryDelay"]).rstrip("s"))
        except Exception:
            pass
        try:
            return float(r.headers.get("Retry-After"))
        except Exception:
            return None

    # ------------------------------------------------------------- tasks

    @staticmethod
    def image_parts(paths, max_total_bytes=MAX_INLINE_BYTES, side=None):
        """Photos as inline parts. Gemini refuses requests over about 20 MB, so when the photos together are
        bigger than max_total_bytes (or side is given) smaller copies are made in memory, one at a time."""
        sizes = [os.path.getsize(p) for p in paths]
        need = side is not None or sum(sizes) > max_total_bytes
        parts = []
        steps = [side] if side else [1600, 1280, 1024, 800, 640]
        budget = max_total_bytes / max(1, len(paths))
        for i, p in enumerate(paths, 1):
            parts.append({"text": f"Photo {i}:"})
            if need and (side or sizes[i - 1] > budget):
                data = _smaller_jpeg(p, steps, budget)
            else:
                with open(p, "rb") as f:
                    data = f.read()
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(data).decode("ascii")}})
        return parts

    def read_answer(self, paths, transcribe=False, side=READ_SIDE):
        """ONE request: splits a batch, reads the question and (transcribe=True) copies the handwriting of every
        photo, so no second Gemini call is ever needed for the answer text. Photos go as small copies (side)."""
        n = len(paths)
        transcribe = bool(transcribe) and n <= TRANSCRIBE_MAX_PHOTOS

        def check(d):
            if not isinstance(d.get("question_written", ""), str):
                raise ValueError("question_written missing")

        ask = (f"There are {n} photos, in the order they were sent. For every photo say whether a new question starts "
               "at its top. Then give the reading order as photo numbers, the first question if written, otherwise "
               "your best description of it, and a short summary of the first answer.")
        if transcribe:
            ask += " Transcribe the handwriting of every photo in its 'text' field."
        parts = self.image_parts(paths, side=int(side) if side else None) + [{"text": ask}]
        data = self.generate_json(READ_SYSTEM + (READ_TRANSCRIBE if transcribe else ""), parts,
                                  read_schema(transcribe), check, what="Reading the photos")
        data["page_order"] = repair_order(data.get("page_order"), n)
        return data

    def pick_candidate(self, read, candidates):
        lines = [f"{i}. " + (f"[{c['label']}] " if c.get("label") else "") + c["title"]
                 for i, c in enumerate(candidates, 1)]
        text = ("Which of these past UPSC questions does this answer respond to? Pick the one whose exact demand "
                "matches, or 0 if none.\n\nQuestion written on the answer: "
                f"{read.get('question_written') or '(none)'}\nAnswer summary: {read.get('answer_summary')}\n\n"
                "Candidates:\n" + "\n".join(lines))

        def check(d):
            if int(d.get("choice", -1)) not in range(0, len(candidates) + 1):
                raise ValueError("choice out of range")

        return self.generate_json("You match handwritten answers to the exact UPSC question they answer.",
                                  [{"text": text}], PICK_SCHEMA, check, what="Choosing the question")

    def evaluate(self, paths, question, years, word_limit, out_of, paper=None, topic=None):
        """paper set (GS1 to GS4 or Essay) means a GS Mains question; otherwise Geography optional."""
        n = len(paths)

        def check(d):  # page_order is not checked: the photos are already in reading order here
            float(d["score"])
            if not d.get("strengths") or not d.get("mistakes"):
                raise ValueError("strengths or mistakes missing")

        where = ""
        if paper:
            where = f"UPSC Mains {paper}" + (f", topic: {topic}" if topic else "") + ", "
        intro = (f'Question ({where}{", ".join(years) or "year unknown"}, {word_limit or "?"} words, marked out of {out_of}):\n'
                 f'"{question}"\n\nEvaluate the handwritten answer in the {n} photos below. '
                 "Return page_order as the photo numbers in reading order.")
        parts = [{"text": intro}] + self.image_parts(paths)
        data = self.generate_json(EVAL_SYSTEM_GS if paper else EVAL_SYSTEM, parts, EVAL_SCHEMA, check,
                                  what="Evaluating the answer")
        score = float(data["score"])
        score = max(0.0, min(float(out_of), round(score * 2) / 2))
        data["score"] = int(score) if score.is_integer() else score
        data["out_of"] = out_of
        return data


def evaluation_text(q, ev, today_str):
    """Plain text version for the console and the .txt file."""
    out = [f"Evaluation by Gemini ({today_str})", "", f'Question: "{q["title"]}"',
           f"Score: {ev['score']} / {ev['out_of']}", "", f"Demand check: {ev.get('demand_check', '')}", "", "Strengths"]
    out += [f"- {s['point']}: {s['detail']}" for s in ev["strengths"]]
    out += ["", "Mistakes to fix"]
    out += [f"{i}. {m['point']}: {m['detail']}" for i, m in enumerate(ev["mistakes"], 1)]
    out += ["", "Remarks card: " + ev.get("card_name", "")]
    for sec in ev.get("card_sections", []):
        out.append(f"  {sec['heading']}")
        out += [f"    - {b}" for b in sec.get("bullets", [])]
    out += ["", ev.get("chat_summary", "")]
    return "\n".join(out)
