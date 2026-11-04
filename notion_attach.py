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
import tempfile
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
PENDING_MARK = "Evaluation pending"  # start of Gemini Remark on rows waiting for a retry (the retry queue lives in Notion)

DEFAULT_CONFIG = {
    "notion_token": "PASTE_YOUR_NOTION_SECRET_HERE",
    "tracker_database_id": "f051f0c54ccc8340ae6281b044b2e5da",
    "tracker_data_source_id": "e091f0c5-4ccc-8231-b554-070a930b6c20",
    "gs_tracker_database_id": "bb075aa07c154ac9b79f44564db64d8c",
    "gs_tracker_data_source_id": "808e70c4-24ed-4756-888c-89d66fe5aed3",
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


# The four Notion databases. If no ID is set by an environment variable, the configured IDs are used when this
# Notion key can open them (that is Avi's own setup); otherwise the bot looks for a database with one of these
# names among the pages shared with the integration (that is a friend who duplicated the templates).
ROLES = {
    "geo": ("tracker_database_id", "tracker_data_source_id", ["Geography optional"], "Tracker"),
    "gs": ("gs_tracker_database_id", "gs_tracker_data_source_id", ["GS PYQs"], "GS PYQs tracker"),
    "proof": ("proof_log_database_id", "proof_log_data_source_id",
              ["Answers written", "200 Answer Challenge: Proof Log", "Proof log"], "Proof log"),
    "remarks": ("remarks_database_id", "remarks_data_source_id", ["Remarks", "Remarks to revise"], "Remarks"),
}
ROLE_ENV = {"geo": "TRACKER", "gs": "GS_TRACKER", "proof": "PROOF_LOG", "remarks": "REMARKS"}
USABLE = ("configured", "env", "found by name")


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
        cfg["allowed_user_ids"] = [int(x) for x in re.split(r"[,\s]+", env_ids.strip())
                                   if x.strip().isdigit() and int(x) > 0]  # "0" or blank means: not set yet
        env_ids = cfg["allowed_user_ids"] if cfg["allowed_user_ids"] else ""
    if os.environ.get("WEBHOOK_SECRET"):
        cfg["webhook_secret"] = os.environ["WEBHOOK_SECRET"].strip()
    if "DRIVE_FOLDER" in os.environ:
        cfg["drive_folder"] = os.environ["DRIVE_FOLDER"].strip()
    if os.environ.get("GEMINI_MODEL"):
        cfg["gemini_model"] = os.environ["GEMINI_MODEL"].strip()
    if os.environ.get("GEMINI_FALLBACK_MODELS"):
        cfg["gemini_fallback_models"] = [m.strip() for m in os.environ["GEMINI_FALLBACK_MODELS"].split(",") if m.strip()]
    env_roles = set()
    for role, (db_key, ds_key, _, _) in ROLES.items():
        prefix = ROLE_ENV[role]
        for env, key in ((prefix + "_DATABASE_ID", db_key), (prefix + "_DATA_SOURCE_ID", ds_key)):
            if env in os.environ:  # set it to "off" to switch that database off
                val = os.environ[env].strip()
                cfg[key] = "" if val.lower() in ("", "off", "none") else val
                env_roles.add(role)
                if key == db_key and prefix + "_DATA_SOURCE_ID" not in os.environ:
                    cfg[ds_key] = ""  # found from the database ID
    cfg["_env_roles"] = sorted(env_roles)
    cfg["_allowed_from_env"] = bool(env_ids)
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


def tracker_of(page, cfg):
    """'geo' for the Geography optional tracker, 'gs' for GS PYQs, None for any other page."""
    parent = page.get("parent", {})
    parent_ids = {plain_id(parent.get("data_source_id")), plain_id(parent.get("database_id"))} - {""}
    for key, ds, db in (("geo", "tracker_data_source_id", "tracker_database_id"),
                        ("gs", "gs_tracker_data_source_id", "gs_tracker_database_id")):
        if ({plain_id(cfg.get(ds)), plain_id(cfg.get(db))} - {""}) & parent_ids:
            return key
    return None


def read_question(page, cfg):
    props = page.get("properties", {})
    title, status, word_limit, section, years = "", None, None, None, []
    paper = topic = marks = None
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
        elif name == "Year" and t == "number" and p.get("number") is not None:
            years = [fmt_score(p["number"])]
        elif name == "Paper" and t == "select":
            paper = (p.get("select") or {}).get("name")
        elif name == "Topic" and t == "select":
            topic = (p.get("select") or {}).get("name")
        elif name == "Marks" and t == "number":
            marks = p.get("number")
    tracker = tracker_of(page, cfg)
    if tracker == "gs":
        label = "GS PYQs" + (f", {paper}" if paper else "")
    else:
        label = "Geography optional"
    return {"id": page.get("id"), "url": page.get("url"), "title": title, "status": status,
            "word_limit": word_limit, "section": section,
            "years": sorted(y for y in years if y), "in_tracker": tracker is not None,
            "tracker": tracker or "geo", "paper": paper if tracker == "gs" else None, "topic": topic,
            "marks": marks, "label": label, "edited": page.get("last_edited_time") or ""}


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
    if q.get("paper"):
        return marking(q)[0]
    try:
        return OUT_OF_MAP.get(int(q["word_limit"]))
    except (TypeError, ValueError):
        return None


def _num(x):
    try:
        return int(float(x)) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def marking(q):
    """(out_of, word_limit) for a question. GS Mains: Marks column if filled, else 10 marks/150 words,
    15/250 when the word limit is 250, GS4 case studies 20/250, Essay 125 marks/1000 to 1200 words."""
    paper = (q.get("paper") or "").strip()
    wl, marks = _num(q.get("word_limit")), _num(q.get("marks"))
    if not paper:
        return OUT_OF_MAP.get(wl) or 15, wl
    if paper.lower() == "essay":
        return 125, wl or 1200
    if marks:
        return marks, wl or {10: 150, 15: 250, 20: 250}.get(marks, 250 if marks > 10 else 150)
    title = (q.get("title") or "").lower()
    if paper == "GS4" and (wl == 250 or "case" in title[:300] or len(title) > 400
                           or re.search(r"\byou (are|have|were)\b", title)):
        return 20, 250
    if wl and wl >= 250:
        return 15, 250
    return 10, wl or 150


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
    near = float(cfg.get("match_near_exact_score", 0.93))
    if best >= near and best - second >= float(cfg.get("match_near_exact_margin", 0.03)):
        return top[0][1], f"near-exact match ({best:.0%})", top
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

TRACKER_KEYS = {"geo": ("tracker_data_source_id", "tracker_database_id", "tracker"),
                "gs": ("gs_tracker_data_source_id", "gs_tracker_database_id", "GS PYQs")}


def tracker_ds(client, cfg, tracker="geo"):
    return resolve_data_source(client, cfg, *TRACKER_KEYS[tracker])


def gs_enabled(cfg):
    return bool(cfg.get("gs_tracker_database_id") or cfg.get("gs_tracker_data_source_id"))


def role_set(cfg, role):
    db_key, ds_key = ROLES[role][:2]
    return bool(cfg.get(db_key) or cfg.get(ds_key))


def _is_network(e):
    return str(e).startswith("Could not reach Notion")


def can_open(client, cfg, role):
    """True if this Notion key can open the configured database for role. Raises on a bad key or no internet."""
    db_key, ds_key = ROLES[role][:2]
    try:
        if cfg.get(ds_key):
            client.get_data_source(cfg[ds_key], what=f"the {ROLES[role][3]} database")
        else:
            client.get_database(cfg[db_key], what=f"the {ROLES[role][3]} database")
        return True
    except FatalError:
        raise
    except UserError as e:
        if _is_network(e):
            raise
        return False


def _title_of(obj):
    t = obj.get("title")
    if isinstance(t, list):
        return rich_text_plain(t)
    return str(t or obj.get("name") or "")


def _norm_name(text):
    """'🌏 Geography optional' and 'Geography Optional' both become 'geography optional'."""
    return " ".join(re.sub(r"[^0-9a-z]+", " ", (text or "").lower()).split())


def discover_database(client, names, log=print):
    """Finds a database shared with the integration whose name is one of names (ignoring emoji, case and
    punctuation). Returns (db_id, ds_id) or None."""
    want = [_norm_name(n) for n in names]
    hits = []
    for name in names:
        try:
            data = client.request("POST", "/search", what=f"searching for the '{name}' database",
                                  json={"query": name, "filter": {"property": "object", "value": "data_source"},
                                        "page_size": 50})
        except FatalError:
            raise
        except UserError as e:
            if _is_network(e):
                raise
            log(f"  search for '{name}' failed: {str(e).splitlines()[0]}")
            continue
        for obj in data.get("results", []):
            title = _norm_name(_title_of(obj))
            if title not in want or obj.get("in_trash") or obj.get("archived"):
                continue
            parent = obj.get("parent") or {}
            if obj.get("object") == "database":
                hits.append((want.index(title), obj.get("id"), ""))
            else:
                hits.append((want.index(title), parent.get("database_id") or "", obj.get("id")))
        if hits:
            break
    if not hits:
        return None
    hits.sort(key=lambda h: h[0])
    if len({h[1] or h[2] for h in hits}) > 1:
        log(f"  more than one database called '{names[0]}' is shared with the integration; using the first one")
    return hits[0][1], hits[0][2]


def find_databases(client, cfg, log=print):
    """Works out which Notion databases to use and writes their IDs into cfg. Returns {role: how}, where how is
    'env', 'configured', 'found by name', 'not found' or 'off'. Raises on a bad key or no internet (nothing cached)."""
    found = {}
    env_roles = set(cfg.get("_env_roles") or [])
    for role, (db_key, ds_key, names, label) in ROLES.items():
        if role in env_roles:
            found[role] = "env" if role_set(cfg, role) else "off"
            continue
        discovered = set(cfg.get("_discovered") or [])
        if role_set(cfg, role) and can_open(client, cfg, role):
            found[role] = "found by name" if role in discovered else "configured"
            continue
        hit = discover_database(client, names, log)
        if hit:
            cfg[db_key], cfg[ds_key] = hit
            cfg["_discovered"] = sorted(discovered | {role})
            found[role] = "found by name"
            log(f"  {label}: found by name in Notion")
        else:
            cfg[db_key], cfg[ds_key] = "", ""
            found[role] = "not found"
    return found


def ensure_gemini_columns(client, cfg, log=print, tracker="geo"):
    """Create 'Gemini Marks' (number) and 'Gemini Remark' (text) on the tracker if missing.
    Returns the set of column names that are safe to write (plus 'Date' if the tracker has a date column)."""
    ds = tracker_ds(client, cfg, tracker)
    schema = client.get_data_source(ds, what=f"the {TRACKER_KEYS[tracker][2]}'s columns").get("properties", {})
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
    if (schema.get("Date") or {}).get("type") == "date":
        ok.add("Date")
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
                       "It will be evaluated automatically when Gemini is available again "
                       "(or send /retry to the bot).", False)])


def proof_log_question_filter(q, page_id):
    """The proof log's 'Tracker question' relation points to the Geography tracker, so GS rows are found by title."""
    if q.get("tracker") == "gs":
        return {"property": "Question", "title": {"equals": (q.get("title") or "")[:1900]}}
    return {"property": "Tracker question", "relation": {"contains": page_id}}


def proof_log_update(client, cfg, page_id, q, upload_ids, attempt, today, score=None, captions=None):
    ds = resolve_data_source(client, cfg, "proof_log_data_source_id", "proof_log_database_id", "proof log")
    today_s = today.isoformat()
    out_of = out_of_for(q)
    gs = q.get("tracker") == "gs"
    found = client.query_data_source(ds, {
        "filter": {"and": [
            proof_log_question_filter(q, page_id),
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
    wl = marking(q)[1] if gs else (int(q["word_limit"]) if q["word_limit"] else None)
    props = {
        "Question": {"title": rich([(q["title"], False)])},
        "No.": {"number": no},
        "Date sent": {"date": {"start": today_s}},
        "Attempt": {"select": {"name": attempt}},
    }
    if not gs:
        props["Tracker question"] = {"relation": [{"id": page_id}]}
    if out_of:
        props["Out of"] = {"number": out_of}
    if score is not None:
        props["Score"] = {"number": score}
    sec = (q.get("paper") or "GS") if gs else SECTION_MAP.get(q["section"] or "")
    if sec:
        props["Section"] = {"select": {"name": sec}}
    label = ", ".join(x for x in [q["label"] if gs else "", ", ".join(q["years"]), f"{wl} words" if wl else ""] if x)
    score_line = (f"Score: {fmt_score(score)} / {out_of or '?'}" if score is not None
                  else f"Score: pending / {out_of or '?'}")
    children = [
        paragraph([(f"PYQ ({label}):" if label else "PYQ:", True), (f' "{q["title"]}"', False)]),
        paragraph([(score_line, True), (f" ({attempt.lower()} attempt, evaluated by Gemini)"
                                        if score is not None else f" ({attempt.lower()} attempt)", False)]),
    ] + images
    if gs:
        children.insert(1, paragraph([("Tracker: ", True), (f"{q['label']} ({q.get('url') or page_id})", False)]))
    page = create_with_section_fallback(client, {"parent": {"type": "data_source_id", "data_source_id": ds},
                                                 "properties": props, "children": children}, "creating the proof log row")
    return {"action": "created", "url": page.get("url"), "no": no}


def create_with_section_fallback(client, body, what):
    """Creates a page; if Notion refuses the Section option (for example GS2 is not an option and cannot be
    added), tries once more without Section."""
    try:
        return client.create_page(body, what=what)
    except FatalError:
        raise
    except UserError:
        if "Section" not in body["properties"]:
            raise
        body = dict(body, properties={k: v for k, v in body["properties"].items() if k != "Section"})
        return client.create_page(body, what=what)


def already_uploaded(client, cfg, unique_ids, today=None, days_back=2):
    """True if a recent proof log row holds exactly these Telegram photo IDs (stored as image captions 'tg:<id>').
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
                cap = rich_text_plain((b.get("image") or {}).get("caption")).strip()
                if cap.startswith("tg:"):
                    found.add(cap)
        # Only the exact same set counts as a repeat. Part of an earlier batch sent again
        # (for example one answer re-sent on its own after a mixed batch) is new work.
        if want == found:
            return row.get("url") or row["id"]
    return None


def create_remarks_card(client, cfg, page_id, q, ev, today):
    ds = resolve_data_source(client, cfg, "remarks_data_source_id", "remarks_database_id", "Remarks")
    gs = q.get("tracker") == "gs"
    if gs:
        sec = q.get("paper") or "Other"
    else:
        sec = SECTION_MAP.get(q["section"] or "", "Other")
        sec = REMARKS_SECTION_MAP.get(sec, sec)
    props = {
        "Name": {"title": rich([(ev.get("card_name") or q["title"][:120], False)])},
        "Date": {"date": {"start": today.isoformat()}},
        "Section": {"select": {"name": sec}},
        "Score": {"rich_text": rich([(f"{fmt_score(ev['score'])}/{fmt_score(ev['out_of'])}", False)])},
    }
    if not gs:  # the Question relation points to the Geography tracker
        props["Question"] = {"relation": [{"id": page_id}]}
    children = []
    if gs:
        children.append(paragraph([("Question: ", True), (f"{q['label']}: {q['title'][:1500]} ({q.get('url') or page_id})", False)]))
    for s in ev.get("card_sections") or []:
        children.append(heading3(s.get("heading", "")))
        children += [bullet([(b, False)]) for b in s.get("bullets", [])]
    page = create_with_section_fallback(client, {"parent": {"type": "data_source_id", "data_source_id": ds},
                                                 "properties": props, "children": children[:100]},
                                        "creating the Remarks card")
    return page.get("url")


def gpt_note(q, page_url, file_names, drive_dir, proof):
    out_of, wl = marking(q)
    years = ", ".join(q["years"]) or "year unknown"
    subject = f"UPSC Mains {q['paper']}" if q.get("paper") else "UPSC Geography optional"
    lines = [
        f"Please evaluate my {subject} answer and write the result into Notion.",
        "",
        f'Question ({years}, {wl or "?"} words): "{q["title"]}"',
        f"Notion question page: {page_url}",
        f"My answer photos (in reading order) are already attached on that page under 'My answer': {', '.join(file_names)}",
    ]
    if drive_dir:
        lines.append(f"The same photos are in my Google Drive folder: {drive_dir}")
    lines += [
        "",
        "Please:",
        f"1. Read the photos and evaluate strictly like a UPSC examiner, out of {out_of or 'the right total'}"
        + (" (150 words = 10, 200 words = 15, 250 words = 20)." if not q.get("paper") else "."),
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
        self.http = None  # session used to download photos back from Notion (tests put a fake here)
        self._rows = None
        self._rows_at = 0.0
        self._columns = {}
        self.tracker_errors = {}  # tracker key: why it could not be read (GS PYQs not connected, for example)
        self.found = None  # {role: how it was found}, see find_databases
        self._found_at = 0.0
        self.in_progress = set()  # question pages being evaluated right now (the retry queue leaves them alone)

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

    def databases(self, refresh=False):
        """Which Notion databases are used (cached for tracker_cache_minutes; /status refreshes it)."""
        max_age = float(self.cfg.get("tracker_cache_minutes", 30)) * 60
        if self.found is None or refresh or time.time() - self._found_at > max_age:
            self.found = find_databases(self.notion(), self.cfg, self.log)
            self._found_at = time.time()
            self._columns = {}
        return self.found

    def has(self, role):
        return self.databases().get(role) in USABLE

    def trackers(self):
        keys = [k for k in ("geo", "gs") if self.has(k)]
        if not keys:
            raise FatalError("NO TRACKER FOUND: the bot cannot see a 'Geography optional' or 'GS PYQs' database.\n"
                             "Fix: open your tracker database in Notion, click the ... menu (top right), Connections, "
                             "and add your integration. Then send /status.")
        return keys

    def tracker_rows(self, refresh=False):
        """Questions from both trackers. If GS PYQs cannot be read, Geography optional still works."""
        max_age = float(self.cfg.get("tracker_cache_minutes", 30)) * 60
        if self._rows is None or refresh or time.time() - self._rows_at > max_age:
            rows = []
            for key in self.trackers():
                try:
                    ds = tracker_ds(self.notion(), self.cfg, key)
                    pages = self.notion().all_rows(ds, what=f"reading the {TRACKER_KEYS[key][2]} questions")
                    self.tracker_errors.pop(key, None)
                except UserError as e:
                    if key == "geo" or isinstance(e, FatalError):
                        raise
                    self.tracker_errors[key] = str(e).splitlines()[0]
                    self.log(f"  {TRACKER_KEYS[key][2]} skipped: {self.tracker_errors[key]}")
                    continue
                for p in pages:
                    r = read_question(p, self.cfg)
                    if key == "gs" and r["tracker"] != "gs":  # data source ID given but parent shows the database ID
                        r.update(tracker="gs", in_tracker=True)
                    if r["title"]:
                        rows.append(r)
            self._rows, self._rows_at = rows, time.time()
        return self._rows

    def columns(self, tracker="geo"):
        if tracker not in self._columns:
            self._columns[tracker] = ensure_gemini_columns(self.notion(), self.cfg, self.log, tracker)
        return self._columns[tracker]


class GeminiBusy(Exception):
    """Gemini failed to read a set (timeout, rate limit). Nothing was written; the photos should be kept."""


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


def process_batch(ctx, s, groups):
    """A batch that holds several different answers: each group of photos is handled as if it had been sent on its
    own (own question, own attach, own evaluation). A group whose question cannot be matched is attached nowhere.
    Returns dict(status='multi', results=[(photo numbers, result)])."""
    log = ctx.log
    log(f"This set holds {len(groups)} different answers: "
        + "; ".join(photo_range(g["photos"]) for g in groups))
    results = []
    caps = s.get("captions") or [None] * len(s["photos"])
    for k, g in enumerate(groups, 1):
        sub = {"name": f"{s['name']}_answer{k}", "photos": [s["photos"][i - 1] for i in g["photos"]],
               "folder": None, "captions": [caps[i - 1] for i in g["photos"]]}
        try:
            res = process_set(ctx, sub, ask_on_low_confidence=False, read=g.get("read") or "group")
        except FatalError:
            raise
        except UserError as e:
            res = needs_review(ctx, sub, f"Notion problem: {e}")
        except Exception as e:  # one answer must not stop the others
            res = needs_review(ctx, sub, f"Unexpected error: {e.__class__.__name__}: {e}")
        results.append((g["photos"], res))
    return {"status": "multi", "results": results, "groups": len(groups)}


def photo_range(nums):
    nums = list(nums)
    if not nums:
        return ""
    return f"photo {nums[0]}" if len(nums) == 1 else f"photos {nums[0]} to {nums[-1]}"


def pending_start_block(today_str):
    """Placed under the photos BEFORE the slow evaluation, so a crash or restart leaves the answer in the retry queue."""
    return paragraph([("Evaluation pending: ", True),
                      (f"Gemini started evaluating this answer on {today_str}. If no evaluation appears here, the bot "
                       "will retry by itself (or send /retry to the bot).", False)])


def process_set(ctx, s, link=None, ask_on_low_confidence=False, read=None, keep_on_busy=False):
    """Runs the whole pipeline for one answer set. Returns a result dict with 'status':
    done, review (moved to needs_review), ambiguous (only with ask_on_low_confidence; nothing moved), dry,
    multi (the set held several answers; see process_batch).
    read: Gemini's reading of these photos when already known (one answer out of a split batch), or 'group'
    for a split-off group whose question still has to be read (it is then never split again)."""
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

    ctx.databases()
    order = list(range(1, len(shrunk) + 1))
    gem = ctx.gemini() if ctx.evaluate else None
    part_of_batch = read is not None
    if read == "group":
        read = None

    # ---- 1. Gemini reads the photos and splits a batch of several answers (no Notion writes yet)
    if gem and not link and read is None:
        log("Gemini is reading the photos...")
        try:
            read = gem.read_answer(shrunk)
        except ge.GeminiKeyError as e:
            raise FatalError(str(e))
        except ge.GeminiError as e:
            shutil.rmtree(work, ignore_errors=True)
            if keep_on_busy and not part_of_batch:
                raise GeminiBusy(str(e))  # the listener keeps the photos and tries again later
            return needs_review(ctx, s, f"Gemini could not read the photos ({e}).")
        if not part_of_batch and cfg.get("split_batches", True):
            groups = ge.group_photos(read, len(shrunk), s.get("breaks") or ())
            if len(groups) > 1:
                shutil.rmtree(work, ignore_errors=True)
                return process_batch(ctx, s, groups)
        order = ge.repair_order(read.get("page_order"), len(shrunk))
    elif read is not None:
        order = ge.repair_order(read.get("page_order"), len(shrunk))

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
    log(f'Question ({q["label"]}): "{q["title"]}" ({reason})')
    out_of, eval_wl = marking(q)

    ev, pending_why = None, None
    pending_first = bool(gem) and cfg.get("mark_pending_first", True) and cfg.get("upload_if_eval_fails", True)

    def run_eval():
        """Gemini evaluates the photos in reading order. Returns (evaluation or None, why it failed or None)."""
        log("Gemini is evaluating the answer...")
        try:
            got = gem.evaluate(ordered_for_eval, q["title"], q["years"], eval_wl, out_of,
                               paper=q.get("paper"), topic=q.get("topic"))
        except ge.GeminiKeyError as e:
            raise FatalError(str(e))
        except ge.GeminiError as e:
            log(f"  Evaluation failed ({e}). The answer stays 'Evaluation pending' for the retry.")
            return None, str(e)
        return got, None

    # ---- 3. Gemini evaluates now only in the old mode; normally the photos are saved first (see 4b)
    ordered_for_eval = [shrunk[i - 1] for i in order]
    if gem and not pending_first:
        ev, pending_why = run_eval()
        if pending_why and not cfg.get("upload_if_eval_fails", True):
            return needs_review(ctx, s, f"Gemini could not evaluate the answer ({pending_why}).")

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
    columns = ctx.columns(q["tracker"]) if (ev or pending_why or pending_first) else set()
    log("Uploading to Notion...")
    upload_ids = [client.upload_file(f) for f in processed]
    blocks = client.list_children(page_id)
    plan = plan_tracker_insert(blocks, today_str)
    if pending_first:
        extra = [pending_start_block(today_str)]
    else:
        extra = evaluation_blocks(ev, today_str) if ev else ([pending_block(today_str, pending_why)] if pending_why else [])
    images = [image_block(u, c) for u, c in zip(upload_ids, captions)]
    added = client.append_children(page_id, plan["header"] + images + extra, after=plan["after"])
    if plan["placeholder"]:
        client.trash_block(plan["placeholder"])
    log("  photos" + (" and evaluation" if ev else "") + " added to the question page")

    # ---- 4b. photos are safe in Notion and marked 'Evaluation pending': now the slow evaluation
    if pending_first:
        rk0 = cfg.get("gemini_remark_property", "Gemini Remark")
        start_id = next((b.get("id") for b in (added or {}).get("results") or [] if is_pending_block(b)), None)
        ctx.in_progress.add(plain_id(page_id))
        try:
            pre = {}
            if rk0 in columns:  # the retry queue: if the server dies during the evaluation, the retry finds this row
                pre[rk0] = {"rich_text": rich([(f"{PENDING_MARK} since {today_str}: evaluation started", False)])}
            if pre:
                client.update_page(page_id, pre, what="marking the answer as 'Evaluation pending'")
            ordered_for_eval = list(processed)  # the same photos, already renamed in reading order
            ev, pending_why = run_eval()
        finally:
            ctx.in_progress.discard(plain_id(page_id))
        if ev:
            client.append_children(page_id, evaluation_blocks(ev, today_str), after=start_id,
                                   what="adding the evaluation")
        else:
            client.append_children(page_id, [pending_block(today_str, pending_why)], after=start_id,
                                   what="adding the 'Evaluation pending' line")
        if start_id:  # the bot's own placeholder line, never Avi's content
            client.trash_block(start_id)

    props = {}
    if ev and ev.get("misread_demand") and cfg.get("status_when_misread"):
        status = cfg["status_when_misread"]
    elif pending_why:
        status = cfg.get("status_when_eval_pending", "Done")
    else:
        status = cfg.get("status_after_upload", "Done")
    if status:
        props["Status"] = {"status": {"name": status}}
    if "Date" in columns:
        props["Date"] = {"date": {"start": today.isoformat()}}
    if ev:
        mk, rk = cfg.get("gemini_marks_property", "Gemini Marks"), cfg.get("gemini_remark_property", "Gemini Remark")
        if mk in columns:
            props[mk] = {"number": ev["score"]}
        if rk in columns:
            remark = ev.get("remark") or ""
            if not remark.lower().startswith("score"):
                remark = f"Score: {fmt_score(ev['score'])}/{out_of}. " + remark
            props[rk] = {"rich_text": rich([(remark[:1990], False)])}
    elif pending_why:
        rk = cfg.get("gemini_remark_property", "Gemini Remark")
        if rk in columns:  # this marker is the retry queue: the bot finds these rows later and evaluates them
            props[rk] = {"rich_text": rich([(f"{PENDING_MARK} since {today_str}: {pending_why}"[:1990], False)])}
    if props:
        client.update_page(page_id, props, what="updating Status and Gemini columns")
        log(f"  Status: {status}" + (f", Gemini Marks: {fmt_score(ev['score'])}/{out_of}" if ev else ""))

    proof = None
    if cfg.get("update_proof_log", True) and role_set(cfg, "proof"):
        attempt = "Repeat" if plan["repeat"] else "First"
        try:
            proof = proof_log_update(client, cfg, page_id, q, upload_ids, attempt, today,
                                     ev["score"] if ev else None, captions)
            log(f"  proof log row No. {proof['no']} {proof['action']}")
        except FatalError:
            raise
        except UserError as e:  # the answer itself is already saved; do not lose it over the log
            log(f"  proof log skipped: {str(e).splitlines()[0]}")
    card = None
    if ev and cfg.get("create_remarks_card", True) and role_set(cfg, "remarks"):
        try:
            card = create_remarks_card(client, cfg, page_id, q, ev, today)
            log("  Remarks card created")
        except FatalError:
            raise
        except UserError as e:
            log(f"  Remarks card skipped: {str(e).splitlines()[0]}")

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


# ----------------------------------------------------------------- retrying pending evaluations
# When Gemini fails, the photos are still attached and Gemini Remark starts with "Evaluation pending".
# These functions find such rows, download the photos back from Notion, evaluate, and write
# everything the normal path writes. No local state is needed, so they survive restarts.

def is_stop_error(err):
    """True when a Gemini failure means 'Gemini is busy or out of quota': stop and try again later.
    False only for a reply problem with one answer, so the next answer can still be tried."""
    return "unusable reply" not in str(err).lower()


def find_pending(ctx):
    """Tracker rows whose Gemini Remark starts with 'Evaluation pending', oldest first."""
    rk = ctx.cfg.get("gemini_remark_property", "Gemini Remark")
    client = ctx.notion()
    out = []
    for key in ctx.trackers():
        try:
            if rk not in ctx.columns(key):
                continue
            ds = tracker_ds(client, ctx.cfg, key)
            cursor = None
            while True:
                body = {"filter": {"property": rk, "rich_text": {"starts_with": PENDING_MARK}},
                        "sorts": [{"timestamp": "last_edited_time", "direction": "ascending"}], "page_size": 100}
                if cursor:
                    body["start_cursor"] = cursor
                data = client.query_data_source(ds, body, what="looking for answers waiting for evaluation")
                for p in data.get("results", []):
                    q = read_question(p, ctx.cfg)
                    q["tracker"] = key
                    out.append(q)
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")
        except FatalError:
            raise
        except UserError as e:
            if key == "geo":
                raise
            ctx.tracker_errors[key] = str(e).splitlines()[0]
    out.sort(key=lambda q: q.get("edited") or "")
    return out


def is_pending_block(b):
    return b.get("type") == "paragraph" and block_text(b).strip().lower().startswith(PENDING_MARK.lower())


def pending_photos(blocks):
    """Returns (pending paragraph block id or None, [(url, caption)]) for the answer waiting for evaluation:
    the images right above the last 'Evaluation pending' line, else the last run of images on the page."""
    idx = max((i for i, b in enumerate(blocks) if is_pending_block(b)), default=None)
    end = idx if idx is not None else len(blocks)
    if idx is None:
        while end > 0 and blocks[end - 1].get("type") != "image":
            end -= 1
    start = end
    while start > 0 and blocks[start - 1].get("type") == "image":
        start -= 1
    photos = []
    for b in blocks[start:end]:
        img = b.get("image") or {}
        kind = img.get("type")
        url = (img.get(kind) or {}).get("url") if kind in ("file", "external") else None
        if url:
            photos.append((url, rich_text_plain(img.get("caption")).strip() or None))
    return (blocks[idx]["id"] if idx is not None else None), photos


def download_url(ctx, url, dest):
    s = ctx.http or requests
    last = None
    for attempt in range(3):
        try:
            r = s.get(url, timeout=120)
            if r.status_code == 200 and r.content:
                Path(dest).write_bytes(r.content)
                return dest
            last = f"HTTP {r.status_code}"
        except requests.exceptions.RequestException as e:
            last = e.__class__.__name__
        time.sleep(2 * attempt)
    raise UserError(f"Could not download an answer photo back from Notion ({last}).")


def proof_log_add_score(client, cfg, page_id, ev, today_str, q=None):
    """Puts the score on the newest proof log row of this question that has no score yet. None if there is none."""
    ds = resolve_data_source(client, cfg, "proof_log_data_source_id", "proof_log_database_id", "proof log")
    rows = client.query_data_source(ds, {
        "filter": proof_log_question_filter(q or {}, page_id),
        "sorts": [{"property": "No.", "direction": "descending"}], "page_size": 10},
        what="looking for the proof log row").get("results", [])
    for row in rows:
        props = row.get("properties", {})
        if (props.get("Score") or {}).get("number") is None:
            client.update_page(row["id"], {"Score": {"number": ev["score"]}}, what="writing the score in the proof log")
            client.append_children(row["id"], [paragraph([
                (f"Score: {fmt_score(ev['score'])} / {fmt_score(ev['out_of'])}", True),
                (f" (evaluated by Gemini on {today_str}, after Gemini was busy)", False)])],
                what="adding the score to the proof log row")
            return {"action": "scored", "url": row.get("url"), "no": (props.get("No.") or {}).get("number")}
    return None


def evaluate_pending(ctx, q):
    """Evaluates one pending tracker row and writes everything the normal path writes.
    Raises ge.GeminiError when Gemini fails (nothing written), FatalError for a bad key."""
    cfg, log, client = ctx.cfg, ctx.log, ctx.notion()
    ctx.databases()
    today = today_local()
    today_str = today.strftime("%d %b %Y")
    page_id, tracker = q["id"], q.get("tracker") or "geo"
    q = read_question(client.get_page(page_id), cfg)
    q["tracker"] = tracker
    if tracker == "gs":
        q["label"] = "GS PYQs" + (f", {q['paper']}" if q.get("paper") else "")
    out_of, eval_wl = marking(q)
    rk, mk = cfg.get("gemini_remark_property", "Gemini Remark"), cfg.get("gemini_marks_property", "Gemini Marks")
    columns = ctx.columns(tracker)
    blocks = client.list_children(page_id)
    pending_id, photos = pending_photos(blocks)
    page_url = f"https://www.notion.so/{plain_id(page_id)}"
    if not photos:
        if rk in columns:  # take it out of the queue, it can never work
            client.update_page(page_id, {rk: {"rich_text": rich([(
                "Evaluation not possible: no answer photos found on the page. Send the photos again.", False)])}},
                what="updating Gemini Remark")
        return {"status": "no_photos", "question": q, "page_url": page_url}
    work = Path(tempfile.mkdtemp(prefix="retry_"))
    try:
        max_bytes = int(float(cfg.get("max_file_mb", 4.5)) * 1024 * 1024)
        paths = []
        for i, (url, _) in enumerate(photos, 1):
            raw = download_url(ctx, url, work / f"raw_{i}")
            dst = work / f"p{i}.jpg"
            shrink_image(raw, dst, int(cfg.get("max_long_side", 1800)), int(cfg.get("jpeg_quality", 85)), max_bytes)
            paths.append(dst)
        log(f'Retrying the evaluation of "{q["title"][:80]}" ({len(paths)} photo(s))')
        others = 0
        try:
            if len(paths) >= int(cfg.get("split_check_min_photos", 7)) and cfg.get("split_batches", True):
                paths, others = answer_part_for(ctx, paths, q)
            ev = ctx.gemini().evaluate(paths, q["title"], q["years"], eval_wl, out_of,
                                       paper=q.get("paper"), topic=q.get("topic"))
        except ge.GeminiKeyError as e:
            raise FatalError(str(e))

        # ---- Notion writes, same content as the normal path
        client.append_children(page_id, evaluation_blocks(ev, today_str), after=pending_id,
                               what="adding the evaluation")
        if pending_id:
            client.trash_block(pending_id)
        if ev.get("misread_demand") and cfg.get("status_when_misread"):
            status = cfg["status_when_misread"]
        else:
            status = cfg.get("status_after_upload", "Done")
        props = {}
        if status:
            props["Status"] = {"status": {"name": status}}
        if mk in columns:
            props[mk] = {"number": ev["score"]}
        if "Date" in columns:
            props["Date"] = {"date": {"start": today.isoformat()}}
        if rk in columns:
            remark = ev.get("remark") or ""
            if not remark.lower().startswith("score"):
                remark = f"Score: {fmt_score(ev['score'])}/{out_of}. " + remark
            props[rk] = {"rich_text": rich([(remark[:1990], False)])}
        client.update_page(page_id, props, what="updating Status and Gemini columns")

        proof = None
        if cfg.get("update_proof_log", True) and role_set(cfg, "proof"):
            try:
                proof = proof_log_add_score(client, cfg, page_id, ev, today_str, q)
                if proof is None:  # no row waiting for a score: make one like the normal path does
                    ids = [client.upload_file(p, f"{slugify(q['title'], 20).lower()}_p{i}.jpg")
                           for i, p in enumerate(paths, 1)]
                    attempt = "Repeat" if sum(1 for b in blocks if b.get("type") == "image") > len(photos) else "First"
                    proof = proof_log_update(client, cfg, page_id, q, ids, attempt, today, ev["score"],
                                             [c for _, c in photos])
            except FatalError:
                raise
            except UserError as e:
                log(f"  proof log skipped: {e}")
        card = None
        if cfg.get("create_remarks_card", True) and role_set(cfg, "remarks"):
            try:
                card = create_remarks_card(client, cfg, page_id, q, ev, today)
            except FatalError:
                raise
            except UserError as e:
                log(f"  Remarks card skipped: {e}")
        return {"status": "done", "question": q, "page_url": page_url, "eval": ev, "pending": None,
                "status_set": status, "proof": proof, "card_url": card, "retried": True, "other_answers": others}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def answer_part_for(ctx, paths, q):
    """Many photos waiting on one page may be several answers that were sent together (before the bot could split
    batches). Gemini reads them; only the group whose question matches this page is evaluated.
    Returns (paths to evaluate, number of other answers seen). Nothing on the page is moved or deleted."""
    read = ctx.gemini().read_answer(paths)
    groups = ge.group_photos(read, len(paths))
    if len(groups) < 2:
        return paths, 0
    best, best_sc = groups[0], -1.0
    for g in groups:
        sc = ge.similarity(g.get("question_written") or "", q["title"])
        if sc > best_sc:
            best, best_sc = g, sc
    ctx.log(f"  the page holds {len(groups)} answers; evaluating {photo_range(best['photos'])} "
            f"({best_sc:.0%} match with this question)")
    return [paths[i - 1] for i in best["photos"]], len(groups) - 1


def rearrange_page(ctx, page_id):
    """One-off clean-up for a page that got the photos of several answers at once (before batches were split).
    Reads the photos of the last answer on the page, splits them into answers, attaches every group that belongs
    to another question to that question's page (appending, evaluated like a normal answer), and only after a
    group is safely attached removes its image blocks from this page. Groups that cannot be matched stay put.
    Finally evaluates what is left on this page if it is waiting."""
    cfg, client, gem = ctx.cfg, ctx.notion(), ctx.gemini()
    ctx.databases()
    q = read_question(client.get_page(page_id), cfg)
    blocks = client.list_children(page_id)
    pending_id, photos = pending_photos(blocks)
    ids = []
    if photos:  # block ids of the same images pending_photos found (the run right above the pending line)
        idx = max((i for i, b in enumerate(blocks) if is_pending_block(b)), default=None)
        end = idx if idx is not None else len(blocks)
        if idx is None:
            while end > 0 and blocks[end - 1].get("type") != "image":
                end -= 1
        start = end
        while start > 0 and blocks[start - 1].get("type") == "image":
            start -= 1
        ids = [b["id"] for b in blocks[start:end]]
    out = {"photos": len(photos), "groups": 1, "moved": [], "left": [], "removed": 0, "own": None, "own_status": None}
    if len(photos) < 2 or len(ids) != len(photos):
        return out
    work = Path(tempfile.mkdtemp(prefix="sort_"))
    try:
        max_bytes = int(float(cfg.get("max_file_mb", 4.5)) * 1024 * 1024)
        paths = []
        for i, (url, _) in enumerate(photos, 1):
            raw = download_url(ctx, url, work / f"raw_{i}")
            dst = work / f"photo_{i:03d}.jpg"
            shrink_image(raw, dst, int(cfg.get("max_long_side", 1800)), int(cfg.get("jpeg_quality", 85)), max_bytes)
            paths.append(dst)
        groups = ge.group_photos(gem.read_answer(paths), len(paths))
        out["groups"] = len(groups)
        if len(groups) < 2:
            return out
        own = max(groups, key=lambda g: ge.similarity(g.get("question_written") or "", q["title"]))
        to_remove = []
        for k, g in enumerate(groups, 1):
            if g is own:
                continue
            where = photo_range(g["photos"])
            sub = {"name": f"sorted_{k}", "photos": [paths[i - 1] for i in g["photos"]], "folder": None,
                   "captions": [photos[i - 1][1] for i in g["photos"]]}
            try:
                read = g.get("read") or gem.read_answer(sub["photos"])
                cand, reason, _ = choose_question(cfg, gem, read, ctx.tracker_rows(), ctx.log)
                if cand is None or plain_id(cand["id"]) == plain_id(page_id):
                    out["left"].append((where, reason if cand is None else "belongs to this page"))
                    continue
                res = process_set(ctx, sub, link=cand["id"])
            except FatalError:
                raise
            except (UserError, ge.GeminiError) as e:
                res = {"status": "failed", "reason": str(e)}
            if res.get("status") == "done":
                out["moved"].append(res)
                to_remove += [ids[i - 1] for i in g["photos"]]
            else:
                out["left"].append((where, res.get("reason") or "could not attach"))
        for bid in to_remove:  # only photos already attached on their right page
            client.trash_block(bid)
        out["removed"] = len(to_remove)
        if pending_id:
            try:
                res = evaluate_pending(ctx, {"id": page_id, "tracker": q.get("tracker")})
                out["own"], out["own_status"] = res, "evaluated" if res.get("status") == "done" else res.get("status")
            except ge.GeminiError as e:
                out["own_status"] = f"still waiting for Gemini ({e})"
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def retry_pending(ctx, on_result=None, skip=None):
    """Evaluates every pending row, oldest first. Stops at the first 'Gemini busy or out of quota' failure.
    Returns dict(found, done, pending, stopped, failed, results)."""
    busy = getattr(ctx, "in_progress", set())
    rows = [q for q in find_pending(ctx) if not (skip and q["id"] in skip) and plain_id(q["id"]) not in busy]
    out = {"found": len(rows), "done": 0, "pending": len(rows), "stopped": None, "failed": [], "results": []}
    for q in rows:
        try:
            res = evaluate_pending(ctx, q)
        except FatalError:
            raise
        except ge.GeminiError as e:
            if is_stop_error(e):
                out["stopped"] = str(e)
                ctx.log(f"  Gemini still busy, stopping the retry: {e}")
                break
            out["failed"].append((q["id"], str(e)))
            continue
        except UserError as e:
            out["failed"].append((q["id"], str(e)))
            ctx.log(f"  could not retry {q['title'][:60]}: {e}")
            continue
        out["results"].append(res)
        out["pending"] -= 1
        if res["status"] == "done":
            out["done"] += 1
        if on_result:
            on_result(res)
    return out


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
