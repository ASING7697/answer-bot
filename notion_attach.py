#!/usr/bin/env python3
"""
Notion Photo Uploader (with Gemini evaluation) for Avi's UPSC PYQ tracker.

One step: put the answer photos in answers_inbox and double-click run.bat.
  1. Gemini reads the photos: question text, reading order, what the answer covers.
  2. The script finds the matching question in the 'Geography optional' tracker
     (local fuzzy matching, Gemini helps if it is unclear) and asks you to confirm.
  3. Gemini evaluates the answer like a strict UPSC examiner (out of 10, 15 or 20).
  4. Only after all Gemini work succeeds, it writes to Notion: photos under "My answer",
     "Evaluation by Gemini (date)", Gemini Marks and Gemini Remark, Status, proof log row
     with the score, and a Remarks card.
  5. Copies the photos to your Google Drive folder, moves the originals to answers_inbox/done,
     and saves the evaluation as a .txt file next to them.

Photo-only mode (no Gemini): --no-eval, or "evaluate": false in config.json.

Usage:
  python notion_attach.py                      (finds the question by itself)
  python notion_attach.py <notion link>        (skip the search, use this question)
  python notion_attach.py --no-eval <link>     (photos only, prints a ChatGPT note)
  python notion_attach.py --check              (tests the Notion and Gemini keys)
  python notion_attach.py --dry-run            (shrinks photos only, writes nothing)
"""

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:  # pragma: no cover
    print("ERROR: the 'requests' package is missing. Run:  pip install -r requirements.txt")
    sys.exit(1)
try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover
    print("ERROR: the 'Pillow' package is missing. Run:  pip install -r requirements.txt")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import gemini_eval as ge  # noqa: E402
API = "https://api.notion.com/v1"
DEFAULT_NOTION_VERSION = "2026-03-11"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
PLACEHOLDER_TEXT = "images pending"

DEFAULT_CONFIG = {
    "notion_token": "PASTE_YOUR_NOTION_SECRET_HERE",
    "tracker_database_id": "f051f0c54ccc8340ae6281b044b2e5da",
    "tracker_data_source_id": "e091f0c5-4ccc-8231-b554-070a930b6c20",
    "proof_log_database_id": "c46cec3441814d9194432652c73ec675",
    "proof_log_data_source_id": "7cdb751d-238a-4498-9fb7-137c980c6d34",
    "drive_folder": "G:\\My Drive\\UPSC_Answers",
    "inbox_folder": "answers_inbox",
    "order_by": "name",
    "max_long_side": 1800,
    "jpeg_quality": 85,
    "max_file_mb": 4.5,
    "status_after_upload": "Done",
    "update_proof_log": True,
    "notion_version": DEFAULT_NOTION_VERSION,
    "remarks_database_id": "7f9e63e326d24a24aba8771959de0eb9",
    "remarks_data_source_id": "68e1fafa-bba9-4298-a9c7-aa87a13d56c5",
    "evaluate": True,
    "gemini_api_key": "PASTE_YOUR_GEMINI_KEY_HERE",
    "gemini_model": "gemini-3.8-flash",
    "gemini_fallback_models": ["gemini-3.6-flash", "gemini-3.5-flash-lite"],
    "gemini_timeout_seconds": 180,
    "gemini_retries": 4,
    "gemini_help_matching": "auto",
    "gemini_marks_property": "Gemini Marks",
    "gemini_remark_property": "Gemini Remark",
    "create_remarks_card": True,
    "status_when_misread": "Knowledge gap",
    "status_when_eval_pending": "Done",
    "match_strong_score": 0.80,
    "match_clear_margin": 0.10,
    "match_min_score": 0.45,
    "match_min_score_with_gemini": 0.25,
    "match_tie_margin": 0.05,
    "upload_if_eval_fails": True,
    "set_gap_seconds": 60,
    "dedupe_with_notion": True,
    "telegram_bot_token": "PASTE_YOUR_TELEGRAM_BOT_TOKEN_HERE",
    "allowed_user_ids": [8308385292],
    "telegram_set_gap_seconds": 60,
}

# Proof log Section to Remarks Section (Remarks has no "Indian Geography")
REMARKS_SECTION_MAP = {"Indian Geography": "Other"}

# Tracker "Original Section" to proof log "Section"
SECTION_MAP = {
    "PERSPECTIVES IN HUMAN GEOGRAPHY": "Perspectives",
    "ECONOMIC GEOGRAPHY & REGIONAL PLANNING": "Economic",
    "AGRICULTURE": "Economic",
    "POPULATION AND SETTLEMENT GEOGRAPHY": "Population",
    "SETTLEMENTS": "Settlement",
    "REGIONAL DEVELOPMENT AND PLANNING": "Regional",
    "GEOMORPHOLOGY": "Physical",
    "CLIMATOLOGY": "Physical",
    "OCEANOGRAPHY": "Physical",
    "BIOGEOGRAPHY & ENVIRONMENTAL GEOGRAPHY": "Physical",
    "PHYSICAL SETTING": "Indian Geography",
    "CULTURAL SETTING & DEMOGRAPHY": "Indian Geography",
    "POLITICAL ASPECTS & CONTEMPORARY ISSUES": "Indian Geography",
}

# Word limit to "Out of" (UPSC marking)
OUT_OF_MAP = {150: 10, 200: 15, 250: 20}


class UserError(Exception):
    """An error with a plain-English message for Avi."""


class FatalError(UserError):
    """A problem that affects every answer (bad key, missing connection). Never moves files."""


# ----------------------------------------------------------------- helpers

def setup_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def load_config(path=None):
    path = Path(path) if path else SCRIPT_DIR / "config.json"
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                user = json.load(f)
        except json.JSONDecodeError as e:
            raise UserError(
                f"config.json has a typing mistake on line {e.lineno} (column {e.colno}).\n"
                "Common causes: a missing comma, or a single backslash in the Drive path.\n"
                "Write Windows paths with double backslashes, like G:\\\\My Drive\\\\UPSC_Answers")
        cfg.update(user)
    env_token = os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY")
    if env_token:
        cfg["notion_token"] = env_token.strip()
    env_tg = os.environ.get("TELEGRAM_BOT_TOKEN")
    if env_tg:
        cfg["telegram_bot_token"] = env_tg.strip()
    env_ids = os.environ.get("ALLOWED_USER_IDS")
    if env_ids:
        cfg["allowed_user_ids"] = [int(x) for x in re.split(r"[,\s]+", env_ids.strip()) if x.strip().lstrip("-").isdigit()]
    if os.environ.get("WEBHOOK_SECRET"):
        cfg["webhook_secret"] = os.environ["WEBHOOK_SECRET"].strip()
    if "DRIVE_FOLDER" in os.environ:
        cfg["drive_folder"] = os.environ["DRIVE_FOLDER"].strip()
    if os.environ.get("GEMINI_MODEL"):
        cfg["gemini_model"] = os.environ["GEMINI_MODEL"].strip()
    if os.environ.get("GEMINI_FALLBACK_MODELS"):
        cfg["gemini_fallback_models"] = [m.strip() for m in os.environ["GEMINI_FALLBACK_MODELS"].split(",") if m.strip()]
    env_gem = os.environ.get("GEMINI_API_KEY")
    if env_gem:
        cfg["gemini_api_key"] = env_gem.strip()
    cfg["_path"] = str(path)
    return cfg


def gemini_key_ok(key):
    return bool(key) and not key.startswith("PASTE_") and len(key) > 20


def token_ok(token):
    return bool(token) and not token.startswith("PASTE_") and (
        token.startswith("ntn_") or token.startswith("secret_"))


def extract_page_id(text):
    """Pull the 32-character Notion page ID out of any Notion link or raw ID."""
    if not text:
        return None
    text = text.strip().strip('"').strip("'")
    text = text.split("?")[0].split("#")[0]
    m = re.findall(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}", text)
    if not m:
        return None
    raw = m[-1].replace("-", "").lower()
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def plain_id(any_id):
    return (any_id or "").replace("-", "").lower()


def natural_key(name):
    parts = re.split(r"(\d+)", name.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def capture_time(path):
    """EXIF DateTimeOriginal if present, else file modified time. Returns a sortable float."""
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            val = None
            try:
                val = exif.get_ifd(0x8769).get(36867)  # DateTimeOriginal
            except Exception:
                val = None
            val = val or exif.get(36867) or exif.get(306)  # 306 = DateTime
            if val:
                dt = _dt.datetime.strptime(str(val).strip()[:19], "%Y:%m:%d %H:%M:%S")
                return dt.timestamp()
    except Exception:
        pass
    return os.path.getmtime(path)


def list_photos(folder, order_by="name"):
    folder = Path(folder)
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    if order_by == "time":
        files.sort(key=lambda p: (capture_time(p), natural_key(p.name)))
    else:
        files.sort(key=lambda p: natural_key(p.name))
    return files


def shrink_image(src, dst, max_side=1800, quality=85, max_bytes=int(4.5 * 1024 * 1024)):
    """Shrink like Notion handler did: fix rotation, long side 1800 px, JPEG quality 85.
    If still too big, lower quality, then size, until under max_bytes."""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        side = max_side
        q = quality
        while True:
            work = im.copy()
            work.thumbnail((side, side), Image.LANCZOS)
            work.save(dst, "JPEG", quality=q, optimize=True)
            size = os.path.getsize(dst)
            if size <= max_bytes:
                return {"width": work.width, "height": work.height, "bytes": size, "quality": q}
            if q > 60:
                q -= 10
            elif side > 1000:
                side = int(side * 0.85)
            else:
                raise UserError(f"Could not get {Path(src).name} under the size limit.")


def slugify(text, max_len=48):
    words = re.sub(r"[^A-Za-z0-9 ]+", " ", text or "").split()
    slug = "_".join(words)[:max_len].strip("_")
    return slug or "question"


def rich_text_plain(rt):
    return "".join(part.get("plain_text") or part.get("text", {}).get("content", "") for part in rt or [])


def today_local():
    return _dt.date.today()


def copy_to_clipboard(text):
    if os.name != "nt":
        return False
    try:
        subprocess.run(["clip"], input=text.encode("utf-16le"), check=True)
        return True
    except Exception:
        return False


# ----------------------------------------------------------------- Notion client

class NotionClient:
    def __init__(self, token, version=DEFAULT_NOTION_VERSION, session=None):
        self.token = token
        self.version = version
        self.s = session or requests.Session()

    def _headers(self, json_body=True):
        h = {"Authorization": f"Bearer {self.token}", "Notion-Version": self.version}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def request(self, method, path, what="", **kw):
        url = path if path.startswith("http") else API + path
        json_body = "files" not in kw
        for attempt in range(5):
            try:
                r = self.s.request(method, url, headers=self._headers(json_body), timeout=120, **kw)
            except requests.exceptions.RequestException as e:
                if attempt < 4:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise UserError(f"Could not reach Notion ({what}). Check your internet. Details: {e}")
            if r.status_code == 429 or r.status_code >= 500:
                if attempt < 4:
                    wait = float(r.headers.get("Retry-After", 2 * (attempt + 1)))
                    time.sleep(min(wait, 30))
                    continue
            if r.status_code >= 400:
                self._raise(r, what)
            return r.json() if r.content else {}
        self._raise(r, what)

    @staticmethod
    def _raise(r, what):
        try:
            body = r.json()
        except Exception:
            body = {"message": r.text[:300]}
        code = body.get("code", "")
        msg = body.get("message", "")
        if r.status_code == 401 or code == "unauthorized":
            raise FatalError(
                "NOTION KEY problem: Notion says the secret key is not valid (expired, revoked or mistyped).\n"
                "Fix: notion.so/profile/integrations, open your integration, copy the Internal Integration Secret\n"
                "again (it starts with ntn_) and put it in config.json (PC) or NOTION_TOKEN (Render).")
        if r.status_code == 404 or code == "object_not_found":
            raise UserError(
                f"Notion could not find {what or 'that page'}.\n"
                "This almost always means the page or database is not connected to your integration.\n"
                "Fix: open the database in Notion, click the ... menu (top right), choose Connections,\n"
                "and add your integration (for example 'Photo Uploader'). Do this for BOTH the\n"
                "'Geography optional' tracker and the '200 Answer Challenge: Proof Log' page.\n"
                "Also check that you copied the full question link.")
        if r.status_code == 403 or code == "restricted_resource":
            raise FatalError("NOTION ACCESS problem: " + 
                f"Notion refused access to {what}. Open your integration settings at\n"
                "notion.so/profile/integrations and make sure 'Read content', 'Update content'\n"
                "and 'Insert content' are all ticked.")
        if code == "validation_error" and ("is not a property that exists" in msg or "property" in msg.lower()
                                           and "does not exist" in msg.lower()):
            raise FatalError(
                f"NOTION COLUMN problem: {msg}\nA column in your tracker, proof log or Remarks database was renamed or\n"
                "deleted. Rename it back to its original name (see TROUBLESHOOTING.md for the list).")
        if code == "validation_error" and "file" in msg.lower() and "size" in msg.lower():
            raise UserError(f"Notion rejected a photo as too large: {msg}")
        raise UserError(f"Notion error while {what or 'talking to Notion'} (HTTP {r.status_code}, {code}): {msg}")

    # convenience wrappers
    def get_page(self, page_id):
        return self.request("GET", f"/pages/{page_id}", what="the question page")

    def update_page(self, page_id, properties, what="updating the page"):
        return self.request("PATCH", f"/pages/{page_id}", what=what, json={"properties": properties})

    def list_children(self, block_id, what="reading the page content"):
        out, cursor = [], None
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            data = self.request("GET", f"/blocks/{block_id}/children", what=what, params=params)
            out.extend(data.get("results", []))
            if not data.get("has_more"):
                return out
            cursor = data.get("next_cursor")

    def append_children(self, block_id, children, after=None, what="adding the photos"):
        body = {"children": children}
        if after:
            if self.version >= "2026-03-11":
                body["position"] = {"type": "after_block", "after_block": {"id": after}}
            else:
                body["after"] = after
        return self.request("PATCH", f"/blocks/{block_id}/children", what=what, json=body)

    def trash_block(self, block_id):
        return self.request("DELETE", f"/blocks/{block_id}", what="removing the 'Images pending' line")

    def upload_file(self, path, filename=None):
        filename = filename or Path(path).name
        created = self.request("POST", "/file_uploads", what=f"starting the upload of {filename}",
                               json={"mode": "single_part", "filename": filename, "content_type": "image/jpeg"})
        fid = created["id"]
        send_url = created.get("upload_url") or f"{API}/file_uploads/{fid}/send"
        with open(path, "rb") as f:
            sent = self.request("POST", send_url, what=f"uploading {filename}",
                                files={"file": (filename, f, "image/jpeg")})
        if sent.get("status") != "uploaded":
            raise UserError(f"Upload of {filename} did not finish (status: {sent.get('status')}).")
        return fid

    def query_data_source(self, ds_id, body, what):
        return self.request("POST", f"/data_sources/{ds_id}/query", what=what, json=body)

    def create_page(self, body, what="creating the proof log row"):
        return self.request("POST", "/pages", what=what, json=body)

    def get_database(self, db_id, what):
        return self.request("GET", f"/databases/{db_id}", what=what)

    def get_data_source(self, ds_id, what):
        return self.request("GET", f"/data_sources/{ds_id}", what=what)

    def add_properties(self, ds_id, props, what="adding columns"):
        return self.request("PATCH", f"/data_sources/{ds_id}", what=what, json={"properties": props})

    def all_rows(self, ds_id, what):
        out, cursor = [], None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = self.query_data_source(ds_id, body, what)
            out.extend(data.get("results", []))
            if not data.get("has_more"):
                return out
            cursor = data.get("next_cursor")


# ----------------------------------------------------------------- block builders

def image_block(upload_id, caption=None):
    img = {"type": "file_upload", "file_upload": {"id": upload_id}}
    if caption:
        img["caption"] = [{"type": "text", "text": {"content": caption}}]
    return {"object": "block", "type": "image", "image": img}


def heading2(text):
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def rich(parts):
    """parts: list of (text, bold) tuples. Splits long text into 1900-character pieces (Notion limit 2000)."""
    rt = []
    for t, b in parts:
        t = t or ""
        for i in range(0, max(len(t), 1), 1900):
            rt.append({"type": "text", "text": {"content": t[i:i + 1900]}, "annotations": {"bold": bool(b)}})
    return rt


def paragraph(parts):
    """parts: list of (text, bold) tuples."""
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich(parts)}}


def heading3(text):
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": rich([(text, False)])}}


def bullet(parts):
    return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rich(parts)}}


def numbered(parts):
    return {"object": "block", "type": "numbered_list_item", "numbered_list_item": {"rich_text": rich(parts)}}


def block_text(block):
    t = block.get("type")
    return rich_text_plain((block.get(t) or {}).get("rich_text"))


def plan_tracker_insert(blocks, today_str):
    """Decide where the new images go on the question page, matching Notion handler:
      - replace an 'Images pending' line if there is one;
      - else put them right under an empty '## My answer' heading;
      - else, if earlier answer images exist, add a new '## My answer (attempt N, date)' section at the end;
      - else add '## My answer' at the end.
    Returns dict(after=<block id or None>, header=<list of blocks to add before images>,
                 placeholder=<block id to remove or None>, repeat=<bool>)."""
    has_images = any(b.get("type") == "image" for b in blocks)
    for b in blocks:
        if b.get("type") == "paragraph" and PLACEHOLDER_TEXT in block_text(b).lower():
            return {"after": b["id"], "header": [], "placeholder": b["id"], "repeat": False}
    for i, b in enumerate(blocks):
        if b.get("type", "").startswith("heading") and block_text(b).strip().lower().startswith("my answer"):
            section = []
            for nb in blocks[i + 1:]:
                if nb.get("type", "").startswith("heading"):
                    break
                section.append(nb)
            if not any(x.get("type") == "image" for x in section):
                return {"after": b["id"], "header": [], "placeholder": None, "repeat": False}
    if has_images:
        attempts = sum(1 for b in blocks if b.get("type", "").startswith("heading")
                       and block_text(b).strip().lower().startswith("my answer")) or 1
        return {"after": None, "header": [heading2(f"My answer (attempt {attempts + 1}, {today_str})")],
                "placeholder": None, "repeat": True}
    return {"after": None, "header": [heading2("My answer")], "placeholder": None, "repeat": False}


# ----------------------------------------------------------------- reading Notion

RESERVED_DIRS = {"done", "needs_review", "processed"}


def read_question(page, cfg):
    props = page.get("properties", {})
    title, status, word_limit, section, years = "", None, None, None, []
    for name, p in props.items():
        t = p.get("type")
        if t == "title":
            title = rich_text_plain(p.get("title"))
        elif name == "Status" and t == "status":
            status = (p.get("status") or {}).get("name")
        elif name == "Word Limit" and t == "number":
            word_limit = p.get("number")
        elif name == "Original Section" and t == "select":
            section = (p.get("select") or {}).get("name")
        elif name == "Year" and t == "multi_select":
            years = [o.get("name") for o in p.get("multi_select") or []]
    parent = page.get("parent", {})
    parent_ids = {plain_id(parent.get("data_source_id")), plain_id(parent.get("database_id"))}
    in_tracker = bool({plain_id(cfg["tracker_data_source_id"]), plain_id(cfg["tracker_database_id"])} & parent_ids)
    return {"id": page.get("id"), "url": page.get("url"), "title": title, "status": status,
            "word_limit": word_limit, "section": section,
            "years": sorted(y for y in years if y), "in_tracker": in_tracker}


def resolve_data_source(client, cfg, key_ds, key_db, label):
    ds = cfg.get(key_ds)
    if ds:
        return ds
    db = client.get_database(cfg[key_db], what=f"the {label} database")
    sources = db.get("data_sources") or []
    if not sources:
        raise UserError(f"Could not find the data source of the {label} database.")
    return sources[0]["id"]


def out_of_for(q):
    try:
        return OUT_OF_MAP.get(int(q["word_limit"]))
    except (TypeError, ValueError):
        return None


def fmt_score(x):
    if x is None:
        return "?"
    x = float(x)
    return str(int(x)) if x.is_integer() else str(x)


# ----------------------------------------------------------------- matching

def choose_question(cfg, gem, read, candidates, log=print):
    """Returns (candidate or None, reason, top5). Never guesses when confidence is low."""
    query = (read.get("question_written") or "").strip() or (read.get("question_guess") or "").strip()
    if not query:
        return None, "Gemini could not find any question text on the photos.", []
    top = ge.top_matches(query, candidates, 5)
    if not top:
        return None, "The tracker returned no questions.", []
    best, second = top[0][0], (top[1][0] if len(top) > 1 else 0.0)
    strong = float(cfg.get("match_strong_score", 0.80))
    floor = float(cfg.get("match_min_score", 0.45))
    tie = float(cfg.get("match_tie_margin", 0.05))
    clear = float(cfg.get("match_clear_margin", 0.10))
    gem_floor = float(cfg.get("match_min_score_with_gemini", 0.25))
    log(f'  Question read from the photos: "{query}"')
    for i, (sc, c) in enumerate(top, 1):
        log(f"    {i}. ({sc:.0%}) {c['title'][:110]}")
    if best >= strong and best - second >= clear:
        return top[0][1], f"clear match ({best:.0%})", top
    help_mode = str(cfg.get("gemini_help_matching", "auto")).lower()
    if gem is not None and help_mode in ("auto", "always", "true", "yes"):
        try:
            pick = gem.pick_candidate(read, [c for _, c in top])
        except ge.GeminiKeyError:
            raise
        except ge.GeminiError as e:
            pick = None
            log(f"  Gemini could not help choose ({e}).")
        if pick:
            k = int(pick.get("choice", 0))
            if k == 0:
                return None, "Gemini says none of the top 5 tracker questions match. " + pick.get("reason", ""), top
            sc, c = top[k - 1]
            if sc >= gem_floor:
                return c, f"Gemini chose option {k} ({sc:.0%}): {pick.get('reason', '')}", top
            return None, f"Gemini chose option {k} but its text match is only {sc:.0%} (below {gem_floor:.0%}).", top
    if best < floor:
        return None, f"Best text match is only {best:.0%} (below {floor:.0%}).", top
    if best - second < tie:
        return None, f"Top two matches are almost equal ({best:.0%} and {second:.0%}).", top
    return top[0][1], f"best text match ({best:.0%})", top


# ----------------------------------------------------------------- Notion writing

def ensure_gemini_columns(client, cfg, log=print):
    """Create 'Gemini Marks' (number) and 'Gemini Remark' (text) on the tracker if missing.
    Returns the set of column names that are safe to write."""
    ds = resolve_data_source(client, cfg, "tracker_data_source_id", "tracker_database_id", "tracker")
    schema = client.get_data_source(ds, what="the tracker's columns").get("properties", {})
    want = {cfg.get("gemini_marks_property", "Gemini Marks"): "number",
            cfg.get("gemini_remark_property", "Gemini Remark"): "rich_text"}
    ok, missing = set(), {}
    for name, typ in want.items():
        if name in schema:
            if schema[name].get("type") == typ:
                ok.add(name)
            else:
                log(f"  WARNING: column '{name}' exists but is not a {typ} column, so it is skipped.")
        else:
            missing[name] = {"number": {"format": "number"}} if typ == "number" else {"rich_text": {}}
    if missing:
        client.add_properties(ds, missing, what="creating the Gemini columns")
        log(f"  Created tracker column(s): {', '.join(missing)}")
        ok |= set(missing)
    return ok


def evaluation_blocks(ev, today_str):
    blocks = [heading2(f"Evaluation by Gemini ({today_str})"),
              paragraph([(f"Score: {fmt_score(ev['score'])} / {fmt_score(ev['out_of'])}", True)])]
    if ev.get("demand_check"):
        blocks.append(paragraph([("Demand check: ", True), (ev["demand_check"], False)]))
    blocks.append(heading3("Strengths"))
    blocks += [bullet([(s["point"] + ": ", True), (s["detail"], False)]) for s in ev["strengths"]]
    blocks.append(heading3("Mistakes to fix"))
    blocks += [numbered([(m["point"] + ": ", True), (m["detail"], False)]) for m in ev["mistakes"]]
    return blocks


def pending_block(today_str, why):
    return paragraph([("Evaluation pending: ", True),
                      (f"Gemini could not evaluate this answer on {today_str} ({why}). "
                       "Ask ChatGPT or run the tool again for this question.", False)])


def proof_log_update(client, cfg, page_id, q, upload_ids, attempt, today, score=None, captions=None):
    ds = resolve_data_source(client, cfg, "proof_log_data_source_id", "proof_log_database_id", "proof log")
    today_s = today.isoformat()
    out_of = out_of_for(q)
    found = client.query_data_source(ds, {
        "filter": {"and": [
            {"property": "Tracker question", "relation": {"contains": page_id}},
            {"property": "Date sent", "date": {"equals": today_s}},
        ]}, "page_size": 5}, what="looking for today's proof log row").get("results", [])
    captions = captions or [None] * len(upload_ids)
    images = [image_block(u, c) for u, c in zip(upload_ids, captions)]
    if found:
        row = found[0]
        blocks = client.list_children(row["id"], what="reading the proof log row")
        placeholder = next((b for b in blocks if b.get("type") == "paragraph"
                            and PLACEHOLDER_TEXT in block_text(b).lower()), None)
        if placeholder:
            client.append_children(row["id"], images, after=placeholder["id"], what="adding photos to the proof log row")
            client.trash_block(placeholder["id"])
        else:
            client.append_children(row["id"], images, what="adding photos to the proof log row")
        props = row.get("properties", {})
        if score is not None and (props.get("Score") or {}).get("number") is None:
            client.update_page(row["id"], {"Score": {"number": score}}, what="writing the score in the proof log")
        no = (props.get("No.") or {}).get("number")
        return {"action": "updated", "url": row.get("url"), "no": no}

    latest = client.query_data_source(ds, {"sorts": [{"property": "No.", "direction": "descending"}],
                                           "page_size": 1}, what="reading the proof log numbers").get("results", [])
    last_no = 0
    if latest:
        last_no = (latest[0].get("properties", {}).get("No.") or {}).get("number") or 0
    no = int(last_no) + 1
    wl = int(q["word_limit"]) if q["word_limit"] else None
    props = {
        "Question": {"title": rich([(q["title"], False)])},
        "No.": {"number": no},
        "Date sent": {"date": {"start": today_s}},
        "Attempt": {"select": {"name": attempt}},
        "Tracker question": {"relation": [{"id": page_id}]},
    }
    if out_of:
        props["Out of"] = {"number": out_of}
    if score is not None:
        props["Score"] = {"number": score}
    sec = SECTION_MAP.get(q["section"] or "")
    if sec:
        props["Section"] = {"select": {"name": sec}}
    label = ", ".join(x for x in [", ".join(q["years"]), f"{wl} words" if wl else ""] if x)
    score_line = (f"Score: {fmt_score(score)} / {out_of or '?'}" if score is not None
                  else f"Score: pending / {out_of or '?'}")
    children = [
        paragraph([(f"PYQ ({label}):" if label else "PYQ:", True), (f' "{q["title"]}"', False)]),
        paragraph([(score_line, True), (f" ({attempt.lower()} attempt, evaluated by Gemini)"
                                        if score is not None else f" ({attempt.lower()} attempt)", False)]),
    ] + images
    page = client.create_page({"parent": {"type": "data_source_id", "data_source_id": ds},
                               "properties": props, "children": children})
    return {"action": "created", "url": page.get("url"), "no": no}


def already_uploaded(client, cfg, unique_ids, today=None, days_back=2):
    """True if every Telegram photo ID is already in a recent proof log row (stored as image captions 'tg:<id>').
    Used to skip repeats after a restart of the webhook server."""
    want = {f"tg:{u}" for u in unique_ids if u}
    if not want:
        return None
    today = today or today_local()
    ds = resolve_data_source(client, cfg, "proof_log_data_source_id", "proof_log_database_id", "proof log")
    since = (today - _dt.timedelta(days=days_back)).isoformat()
    rows = client.query_data_source(ds, {"filter": {"property": "Date sent", "date": {"on_or_after": since}},
                                         "page_size": 50}, what="checking for repeats").get("results", [])
    for row in rows:
        found = set()
        for b in client.list_children(row["id"], what="checking for repeats"):
            if b.get("type") == "image":
                found.add(rich_text_plain((b.get("image") or {}).get("caption")).strip())
        if want <= found:
            return row.get("url") or row["id"]
    return None


def create_remarks_card(client, cfg, page_id, q, ev, today):
    ds = resolve_data_source(client, cfg, "remarks_data_source_id", "remarks_database_id", "Remarks")
    sec = SECTION_MAP.get(q["section"] or "", "Other")
    sec = REMARKS_SECTION_MAP.get(sec, sec)
    props = {
        "Name": {"title": rich([(ev.get("card_name") or q["title"][:120], False)])},
        "Date": {"date": {"start": today.isoformat()}},
        "Section": {"select": {"name": sec}},
        "Score": {"rich_text": rich([(f"{fmt_score(ev['score'])}/{fmt_score(ev['out_of'])}", False)])},
        "Question": {"relation": [{"id": page_id}]},
    }
    children = []
    for s in ev.get("card_sections") or []:
        children.append(heading3(s.get("heading", "")))
        children += [bullet([(b, False)]) for b in s.get("bullets", [])]
    page = client.create_page({"parent": {"type": "data_source_id", "data_source_id": ds},
                               "properties": props, "children": children[:100]}, what="creating the Remarks card")
    return page.get("url")


def gpt_note(q, page_url, file_names, drive_dir, proof):
    out_of = out_of_for(q)
    years = ", ".join(q["years"]) or "year unknown"
    lines = [
        "Please evaluate my UPSC Geography optional answer and write the result into Notion.",
        "",
        f'Question ({years}, {int(q["word_limit"]) if q["word_limit"] else "?"} words): "{q["title"]}"',
        f"Notion question page: {page_url}",
        f"My answer photos (in reading order) are already attached on that page under 'My answer': {', '.join(file_names)}",
    ]
    if drive_dir:
        lines.append(f"The same photos are in my Google Drive folder: {drive_dir}")
    lines += [
        "",
        "Please:",
        f"1. Read the photos and evaluate strictly like a UPSC examiner, out of {out_of or 'the right total'} "
        "(150 words = 10, 200 words = 15, 250 words = 20).",
        "2. On the question page, under the photos, add '## Evaluation (today's date)' "
        "with the score, Strengths and Mistakes to fix.",
        "3. Fill GPT Marks and GPT Remark on the question page.",
    ]
    if proof:
        lines.append(f"4. Put the score in the 'Score' column of proof log row No. {proof.get('no')} ({proof.get('url')}).")
        lines.append("5. Add a short card to my 'Remarks' database linked to this question.")
    else:
        lines.append("4. Add a short card to my 'Remarks' database linked to this question.")
    lines.append("Use plain words only: no arrows, no LaTeX, no math codes.")
    return "\n".join(lines)


# ----------------------------------------------------------------- answer sets

def arrival_time(path):
    st = os.stat(path)
    # On Windows st_ctime is when the file was created in this folder (copied in).
    return max(st.st_mtime, st.st_ctime) if os.name == "nt" else st.st_mtime


def collect_sets(inbox, quiet_seconds=60, require_quiet=False, now=None, order_by="name"):
    """Each subfolder of the inbox is one answer. Loose photos are grouped: photos that arrived
    within quiet_seconds of each other form one answer. With require_quiet, sets that are still
    arriving (newest photo younger than quiet_seconds) are left for later."""
    now = now or time.time()
    inbox = Path(inbox)
    sets = []
    for d in sorted((p for p in inbox.iterdir() if p.is_dir() and p.name.lower() not in RESERVED_DIRS),
                    key=lambda p: natural_key(p.name)):
        photos = list_photos(d, order_by)
        if not photos:
            continue
        newest = max(arrival_time(p) for p in photos)
        if require_quiet and now - newest < quiet_seconds:
            continue
        sets.append({"name": d.name, "photos": photos, "folder": d})
    loose = list_photos(inbox, order_by)
    if loose:
        by_arrival = sorted(loose, key=lambda p: (arrival_time(p), natural_key(p.name)))
        groups, cur, last = [], [], None
        for p in by_arrival:
            t = arrival_time(p)
            if cur and t - last > quiet_seconds:
                groups.append(cur)
                cur = []
            cur.append(p)
            last = t
        groups.append(cur)
        for g in groups:
            if require_quiet and now - max(arrival_time(p) for p in g) < quiet_seconds:
                continue
            g.sort(key=lambda p: natural_key(p.name) if order_by != "time" else (capture_time(p), natural_key(p.name)))
            stamp = _dt.datetime.fromtimestamp(arrival_time(g[0])).strftime("%H%M%S")
            sets.append({"name": f"photos_{stamp}", "photos": g, "folder": None})
    return sets


def move_set(photos, folder, target_dir, extra_files=None):
    target_dir.mkdir(parents=True, exist_ok=True)
    for p in photos:
        target = target_dir / p.name
        n = 1
        while target.exists():
            target = target_dir / f"{p.stem}_{n}{p.suffix}"
            n += 1
        shutil.move(str(p), str(target))
    for name, text in (extra_files or {}).items():
        (target_dir / name).write_text(text, encoding="utf-8")
    if folder is not None:
        try:
            leftovers = list(Path(folder).iterdir())
            if not leftovers:
                Path(folder).rmdir()
        except OSError:
            pass
    return target_dir


# ----------------------------------------------------------------- the main job

class Context:
    def __init__(self, args, cfg, client=None, gem=None, log=print):
        self.args, self.cfg, self.log = args, cfg, log
        inbox = Path(args.folder) if args.folder else Path(cfg["inbox_folder"])
        self.inbox = inbox if inbox.is_absolute() else SCRIPT_DIR / inbox
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.evaluate = bool(cfg.get("evaluate", True)) and not args.no_eval
        self.client = client
        self.gem = gem
        self._rows = None
        self._columns = None

    def notion(self):
        if self.client is None:
            if not token_ok(self.cfg.get("notion_token", "")):
                raise FatalError("NOTION KEY problem: no Notion secret found. Open config.json and paste your Internal Integration Secret\n"
                                 "(it starts with ntn_) in place of PASTE_YOUR_NOTION_SECRET_HERE.")
            self.client = NotionClient(self.cfg["notion_token"], self.cfg.get("notion_version", DEFAULT_NOTION_VERSION))
        return self.client

    def gemini(self):
        if self.gem is None:
            key = self.cfg.get("gemini_api_key", "")
            if not gemini_key_ok(key):
                raise FatalError("GEMINI KEY problem: no Gemini API key found. Get a free key at aistudio.google.com (Get API key)\n"
                                 "and paste it into config.json as gemini_api_key, or run with --no-eval.")
            self.gem = ge.GeminiClient(key, self.cfg.get("gemini_model", ge.DEFAULT_MODEL),
                                       self.cfg.get("gemini_fallback_models", ge.DEFAULT_FALLBACKS),
                                       int(self.cfg.get("gemini_timeout_seconds", 180)),
                                       int(self.cfg.get("gemini_retries", 4)), log=self.log)
        return self.gem

    def tracker_rows(self, refresh=False):
        if self._rows is None or refresh:
            ds = resolve_data_source(self.notion(), self.cfg, "tracker_data_source_id", "tracker_database_id", "tracker")
            pages = self.notion().all_rows(ds, what="reading the tracker questions")
            self._rows = [read_question(p, self.cfg) for p in pages]
            self._rows = [r for r in self._rows if r["title"]]
        return self._rows

    def columns(self):
        if self._columns is None:
            self._columns = ensure_gemini_columns(self.notion(), self.cfg, self.log)
        return self._columns


def needs_review(ctx, s, reason, top=None, work=None):
    stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    target = ctx.inbox / "needs_review" / f"{stamp}_{s['name']}"
    lines = ["This answer set was NOT uploaded.", "", "Reason: " + reason, ""]
    if top:
        lines.append("Closest tracker questions:")
        for i, (sc, c) in enumerate(top, 1):
            lines.append(f"  {i}. ({sc:.0%}) {c['title']}")
            lines.append(f"     https://www.notion.so/{plain_id(c['id'])}")
        lines.append("")
    lines += ["To finish it: open Command Prompt in the tool's folder and run",
              f'  python notion_attach.py <question link> --folder "{target}"',
              "or move these photos back into answers_inbox (in a subfolder) after fixing the problem."]
    move_set(s["photos"], s["folder"], target, {"REASON.txt": "\n".join(lines)})
    ctx.log(f"  NEEDS REVIEW: {reason}\n  Photos moved to {target} (see REASON.txt).")
    return {"status": "review", "reason": reason, "folder": str(target), "top": top or []}


def process_set(ctx, s, link=None, ask_on_low_confidence=False):
    """Runs the whole pipeline for one answer set. Returns a result dict with 'status':
    done, review (moved to needs_review), ambiguous (only with ask_on_low_confidence; nothing moved), dry."""
    cfg, log = ctx.cfg, ctx.log
    today = today_local()
    today_str = today.strftime("%d %b %Y")
    log(f"\n=== Answer set '{s['name']}' ({len(s['photos'])} photo(s)) ===")
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    work = SCRIPT_DIR / "processed" / f"{stamp}_{slugify(s['name'], 30)}"
    work.mkdir(parents=True, exist_ok=True)
    max_bytes = int(float(cfg.get("max_file_mb", 4.5)) * 1024 * 1024)

    log("Shrinking photos...")
    shrunk = []
    for i, p in enumerate(s["photos"], 1):
        dst = work / f"src_{i}.jpg"
        info = shrink_image(p, dst, int(cfg.get("max_long_side", 1800)), int(cfg.get("jpeg_quality", 85)), max_bytes)
        log(f"  {p.name}: {os.path.getsize(p) / 1048576:.1f} MB to {info['bytes'] / 1048576:.2f} MB")
        shrunk.append(dst)
    if ctx.args.dry_run:
        log(f"Dry run: nothing uploaded or moved. Shrunk copies are in {work}")
        return {"status": "dry"}

    order = list(range(1, len(shrunk) + 1))
    read = None
    gem = ctx.gemini() if ctx.evaluate else None

    # ---- 1. Gemini reads the photos (no Notion writes yet)
    if gem and not link:
        log("Gemini is reading the photos...")
        try:
            read = gem.read_answer(shrunk)
        except ge.GeminiKeyError as e:
            raise FatalError(str(e))
        except ge.GeminiError as e:
            return needs_review(ctx, s, f"Gemini could not read the photos ({e}).")
        order = ge.valid_order(read.get("page_order"), len(shrunk)) or order

    # ---- 2. find the question
    top = None
    if link:
        page_id = extract_page_id(link)
        if not page_id:
            raise FatalError("That does not look like a Notion link.")
        q = read_question(ctx.notion().get_page(page_id), cfg)
        reason = "link given"
    elif gem:
        cand, reason, top = choose_question(cfg, gem, read, ctx.tracker_rows(), log)
        if cand is None:
            if ask_on_low_confidence and top:
                shutil.rmtree(work, ignore_errors=True)
                return {"status": "ambiguous", "reason": reason, "top": top, "read": read}
            return needs_review(ctx, s, reason, top)
        page_id = cand["id"]
        q = read_question(ctx.notion().get_page(page_id), cfg)
    else:
        if sys.stdin.isatty():
            link = ask("Paste the Notion link of the question, then press Enter:\n> ")
        page_id = extract_page_id(link or "")
        if not page_id:
            return needs_review(ctx, s, "Photo-only mode needs the question's Notion link.")
        q = read_question(ctx.notion().get_page(page_id), cfg)
        reason = "link given"
    log(f'Question: "{q["title"]}" ({reason})')
    out_of = out_of_for(q) or 15

    # ---- 3. Gemini evaluates (still no Notion writes)
    ev, pending_why = None, None
    if gem:
        log("Gemini is evaluating the answer...")
        ordered_for_eval = [shrunk[i - 1] for i in order]
        try:
            ev = gem.evaluate(ordered_for_eval, q["title"], q["years"], q["word_limit"], out_of)
            if not read:
                o2 = ge.valid_order(ev.get("page_order"), len(shrunk))
                if o2:
                    order = [order[i - 1] for i in o2]
        except ge.GeminiKeyError as e:
            raise FatalError(str(e))
        except ge.GeminiError as e:
            pending_why = str(e)
            if not cfg.get("upload_if_eval_fails", True):
                return needs_review(ctx, s, f"Gemini could not evaluate the answer ({e}).")
            log(f"  Evaluation failed ({e}). Uploading the photos with an 'Evaluation pending' line.")

    # ---- final names in reading order
    short = slugify(q["title"], 20).lower()
    slug = slugify(q["title"])
    processed = []
    for k, i in enumerate(order, 1):
        dst = work / f"{short}_p{k}.jpg"
        shutil.move(str(shrunk[i - 1]), dst)
        processed.append(dst)
    originals = [s["photos"][i - 1] for i in order]
    captions = [s["captions"][i - 1] for i in order] if s.get("captions") else [None] * len(order)
    log("Reading order: " + ", ".join(p.name for p in originals))

    # ---- 4. Notion writes
    client = ctx.notion()
    columns = ctx.columns() if ev else set()
    log("Uploading to Notion...")
    upload_ids = [client.upload_file(f) for f in processed]
    blocks = client.list_children(page_id)
    plan = plan_tracker_insert(blocks, today_str)
    extra = evaluation_blocks(ev, today_str) if ev else ([pending_block(today_str, pending_why)] if pending_why else [])
    images = [image_block(u, c) for u, c in zip(upload_ids, captions)]
    client.append_children(page_id, plan["header"] + images + extra, after=plan["after"])
    if plan["placeholder"]:
        client.trash_block(plan["placeholder"])
    log("  photos" + (" and evaluation" if ev else "") + " added to the question page")

    props = {}
    if ev and ev.get("misread_demand") and cfg.get("status_when_misread"):
        status = cfg["status_when_misread"]
    elif pending_why:
        status = cfg.get("status_when_eval_pending", "Done")
    else:
        status = cfg.get("status_after_upload", "Done")
    if status:
        props["Status"] = {"status": {"name": status}}
    if ev:
        mk, rk = cfg.get("gemini_marks_property", "Gemini Marks"), cfg.get("gemini_remark_property", "Gemini Remark")
        if mk in columns:
            props[mk] = {"number": ev["score"]}
        if rk in columns:
            remark = ev.get("remark") or ""
            if not remark.lower().startswith("score"):
                remark = f"Score: {fmt_score(ev['score'])}/{out_of}. " + remark
            props[rk] = {"rich_text": rich([(remark[:1990], False)])}
    if props:
        client.update_page(page_id, props, what="updating Status and Gemini columns")
        log(f"  Status: {status}" + (f", Gemini Marks: {fmt_score(ev['score'])}/{out_of}" if ev else ""))

    proof = None
    if cfg.get("update_proof_log", True):
        attempt = "Repeat" if plan["repeat"] else "First"
        proof = proof_log_update(client, cfg, page_id, q, upload_ids, attempt, today, ev["score"] if ev else None,
                                 captions)
        log(f"  proof log row No. {proof['no']} {proof['action']}")
    card = None
    if ev and cfg.get("create_remarks_card", True):
        card = create_remarks_card(client, cfg, page_id, q, ev, today)
        log("  Remarks card created")

    # ---- 5. files
    drive_dir = None
    drive_root = (cfg.get("drive_folder") or "").strip()
    if drive_root:
        root = Path(drive_root)
        if root.drive and not Path(root.drive + os.sep).exists():
            log(f"WARNING: Drive folder {drive_root} not found (is Google Drive for desktop running?). Skipped.")
        else:
            drive_dir = root / f"{today.isoformat()}_{slug}"
            drive_dir.mkdir(parents=True, exist_ok=True)
            for f in processed:
                shutil.copy2(f, drive_dir / f.name)
            log(f"Copied photos to Drive: {drive_dir}")

    page_url = f"https://www.notion.so/{plain_id(page_id)}"
    files = {}
    if ev:
        text = ge.evaluation_text(q, ev, today_str) + f"\n\nNotion: {page_url}"
        files["evaluation.txt"] = text
        if drive_dir:
            (drive_dir / "evaluation.txt").write_text(text, encoding="utf-8")
    else:
        files["chatgpt_note.txt"] = gpt_note(q, page_url, [f.name for f in processed], drive_dir, proof)
    done_dir = move_set(originals, s["folder"], ctx.inbox / "done" / f"{today.isoformat()}_{slug}", files)
    log(f"Originals moved to: {done_dir}")

    if ev:
        log("\n" + "=" * 70 + "\n" + files["evaluation.txt"] + "\n" + "=" * 70)
    else:
        note = files["chatgpt_note.txt"]
        log("\n" + "=" * 70 + "\nPASTE THIS INTO CHATGPT:\n" + "=" * 70 + "\n" + note + "\n" + "=" * 70)
        if copy_to_clipboard(note):
            log("(Already copied: paste it into ChatGPT with Ctrl+V.)")
    if card:
        log(f"Remarks card: {card}")
    return {"status": "done", "question": q, "page_url": page_url, "eval": ev, "pending": pending_why,
            "status_set": status, "proof": proof, "card_url": card, "done_dir": str(done_dir),
            "drive_dir": str(drive_dir) if drive_dir else None, "match_reason": reason,
            "gpt_note": files.get("chatgpt_note.txt")}


def ask(prompt):
    try:
        return input(prompt)
    except EOFError:
        return ""


def run_once(ctx):
    link = ctx.args.link
    if link:
        photos = list_photos(ctx.inbox, ctx.cfg.get("order_by", "name"))
        if not photos:
            raise UserError(f"No photos found in {ctx.inbox}")
        sets = [{"name": "linked", "photos": photos, "folder": None}]
    else:
        sets = collect_sets(ctx.inbox, float(ctx.cfg.get("set_gap_seconds", 60)), False,
                            order_by=ctx.cfg.get("order_by", "name"))
    if not sets:
        ctx.log(f"No photos found in {ctx.inbox}\nDrop your answer photos (jpg or png) there and run again.")
        return 0
    results = {}
    for s in sets:
        try:
            r = process_set(ctx, s, link)
        except FatalError:
            raise
        except UserError as e:
            r = needs_review(ctx, s, f"Notion problem: {e}") if not ctx.args.dry_run else {"status": "failed"}
        results[r["status"]] = results.get(r["status"], 0) + 1
    ctx.log("\nSummary: " + ", ".join(f"{v} {k}" for k, v in results.items()))
    return 0


def run_watch(ctx, poll=10, max_cycles=None, sleep=time.sleep):
    gap = float(ctx.cfg.get("set_gap_seconds", 60))
    ctx.log(f"Watching {ctx.inbox}\nDrop photos there (or a folder of photos per answer). "
            f"A set is processed {int(gap)} seconds after its last photo arrives. Press Ctrl+C to stop.")
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        try:
            sets = collect_sets(ctx.inbox, gap, True, order_by=ctx.cfg.get("order_by", "name"))
            if sets:
                ctx.tracker_rows(refresh=True)
            for s in sets:
                try:
                    process_set(ctx, s)
                except FatalError:
                    raise
                except UserError as e:
                    needs_review(ctx, s, f"Notion problem: {e}")
                except Exception as e:  # keep watching no matter what
                    needs_review(ctx, s, f"Unexpected error: {e.__class__.__name__}: {e}")
            if sets:
                ctx.log(f"\nWatching {ctx.inbox} again...")
        except FatalError:
            raise
        except UserError as e:
            ctx.log(f"PROBLEM: {e}\nWill try again shortly.")
        sleep(poll)
    return 0


def check_setup(ctx):
    cfg = ctx.cfg
    client = ctx.notion()
    me = client.request("GET", "/users/me", what="checking your secret")
    print(f"Notion secret works. Integration name: {me.get('name')}")
    for key, label in (("tracker_database_id", "Geography optional tracker"),
                       ("proof_log_database_id", "Answers written proof log"),
                       ("remarks_database_id", "Remarks")):
        db = client.get_database(cfg[key], what=f"the {label} database")
        print(f"  {label}: connected ({rich_text_plain(db.get('title'))})")
    if ctx.evaluate:
        try:
            model = ctx.gemini().check()
            print(f"Gemini key works. Model: {model}")
        except ge.GeminiError as e:
            raise FatalError(str(e))
    else:
        print("Gemini evaluation is switched off (photo-only mode).")
    tg = (cfg.get("telegram_bot_token") or "").strip()
    if tg and not tg.startswith("PASTE_"):
        import telegram_mode
        bot = telegram_mode.TelegramBot(tg)
        try:
            me = bot.call("getMe")
            print(f"Telegram bot works: @{me.get('username')} (only user IDs {cfg.get('allowed_user_ids')} are accepted)")
        except telegram_mode.TelegramError as e:
            print(f"Telegram check failed: {e}")
    else:
        print("Telegram bot token not set (only needed for run_telegram.bat).")
    print("Everything is connected.")
    return 0


def main(argv=None, client=None, gem=None):
    setup_console()
    ap = argparse.ArgumentParser(description="Attach answer photos to Notion and evaluate them with Gemini.")
    ap.add_argument("link", nargs="?", help="optional: Notion link of the question (skips the automatic search)")
    ap.add_argument("--folder", help="folder with the answer photos (default: answers_inbox next to this script)")
    ap.add_argument("--config", help="path to config.json")
    ap.add_argument("--watch", action="store_true", help="keep running and process new photos as they arrive")
    ap.add_argument("--telegram", action="store_true", help="listen to your Telegram bot for answer photos")
    ap.add_argument("--no-eval", action="store_true", help="photo-only mode: no Gemini, prints a ChatGPT note")
    ap.add_argument("--dry-run", action="store_true", help="shrink photos only, no Gemini and no Notion")
    ap.add_argument("--check", action="store_true", help="test the Notion and Gemini keys and connections")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
        ctx = Context(args, cfg, client, gem)
        if args.check:
            return check_setup(ctx)
        if args.telegram:
            import telegram_mode
            return telegram_mode.run_telegram(ctx)
        if args.watch:
            return run_watch(ctx)
        return run_once(ctx)
    except UserError as e:
        print("\nPROBLEM: " + str(e))
        return 2
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


if __name__ == "__main__":
    # Import ourselves by name so telegram_mode and this file share the same error classes.
    import notion_attach
    sys.exit(notion_attach.main())
