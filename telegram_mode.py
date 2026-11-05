"""
Telegram listener for the Notion Photo Uploader.

Long-polls the Telegram Bot API (getUpdates) with plain requests. Only messages from
allowed_user_ids are used; everything else is ignored. Photos are saved to disk at once
and the offset is saved after each update, so nothing is lost if the PC restarts.
Telegram keeps unread updates for 24 hours.
"""

import json
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path

import requests

import notion_attach as na

TG_API = "https://api.telegram.org"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
MAX_MSG = 3900


class TelegramError(Exception):
    pass


class TelegramBot:
    def __init__(self, token, session=None):
        self.token = token
        self.s = session or requests.Session()

    def _redact(self, text):
        return str(text).replace(self.token, "[token hidden]") if self.token else str(text)

    def call(self, method, timeout=30, **params):
        url = f"{TG_API}/bot{self.token}/{method}"
        try:
            r = self.s.post(url, json=params, timeout=timeout)
        except requests.exceptions.RequestException as e:
            raise TelegramError(self._redact(f"network problem: {e.__class__.__name__}"))
        try:
            data = r.json()
        except ValueError:
            raise TelegramError(f"Telegram sent a non-JSON reply (HTTP {r.status_code})")
        if not data.get("ok"):
            code = data.get("error_code", r.status_code)
            desc = self._redact(data.get("description", ""))
            if code == 401 or code == 404:
                raise na.FatalError("TELEGRAM TOKEN problem: Telegram says the bot token is not valid. Copy it again from @BotFather "
                                    "and paste it into config.json as telegram_bot_token.")
            if code == 409:
                raise TelegramError("CONFLICT: " + desc)
            raise TelegramError(f"Telegram error {code}: {desc}")
        return data.get("result")

    def get_updates(self, offset, timeout=20):
        url = f"{TG_API}/bot{self.token}/getUpdates"
        try:
            r = self.s.post(url, json={"offset": offset, "timeout": timeout, "allowed_updates": ["message"]},
                            timeout=timeout + 15)
        except requests.exceptions.RequestException as e:
            raise TelegramError(self._redact(f"network problem: {e.__class__.__name__}"))
        try:
            data = r.json()
        except ValueError:
            raise TelegramError(f"Telegram sent a non-JSON reply (HTTP {r.status_code})")
        if not data.get("ok"):
            code = data.get("error_code", r.status_code)
            if code in (401, 404):
                raise na.FatalError("TELEGRAM TOKEN problem: Telegram says the bot token is not valid. Copy it again from @BotFather "
                                    "and paste it into config.json as telegram_bot_token.")
            if code == 409:
                raise TelegramError("CONFLICT: " + self._redact(data.get("description", "")))
            raise TelegramError(f"Telegram error {code}: {self._redact(data.get('description', ''))}")
        return data.get("result") or []

    def send(self, chat_id, text):
        for chunk in split_message(text):
            try:
                self.call("sendMessage", chat_id=chat_id, text=chunk, disable_web_page_preview=True)
            except TelegramError as e:
                print(f"  (could not send a Telegram reply: {e})")

    def download(self, file_id, dest):
        info = self.call("getFile", file_id=file_id)
        path = info.get("file_path")
        if not path:
            raise TelegramError("Telegram did not give a download path (file over 20 MB?)")
        url = f"{TG_API}/file/bot{self.token}/{path}"
        for attempt in range(3):
            try:
                r = self.s.get(url, timeout=120)
                if r.status_code == 200:
                    Path(dest).write_bytes(r.content)
                    return dest
            except requests.exceptions.RequestException:
                pass
            time.sleep(2 * (attempt + 1))
        raise TelegramError("Could not download a photo from Telegram")


def split_message(text, limit=MAX_MSG):
    text = (text or "").strip()
    if not text:
        return []
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    out.append(text)
    return out


# ----------------------------------------------------------------- state on disk

class State:
    """offset plus answer sets, saved in telegram_state/ next to the script."""

    def __init__(self, root):
        self.root = Path(root)
        self.sets_dir = self.root / "sets"
        self.sets_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.root / "offset.json"

    def offset(self):
        try:
            return int(json.loads(self.file.read_text()).get("offset", 0))
        except Exception:
            return 0

    def save_offset(self, value):
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"offset": value}))
        os.replace(tmp, self.file)

    def sets(self):
        out = []
        for d in sorted(self.sets_dir.iterdir()) if self.sets_dir.exists() else []:
            m = d / "meta.json"
            if d.is_dir() and m.exists():
                try:
                    meta = json.loads(m.read_text(encoding="utf-8"))
                except Exception:
                    continue
                meta["_dir"] = d
                out.append(meta)
        return out

    def save(self, meta):
        d = meta["_dir"]
        data = {k: v for k, v in meta.items() if k != "_dir"}
        (d / "meta.json").write_text(json.dumps(data, indent=1), encoding="utf-8")

    def new_set(self, chat_id, msg_date):
        sid = time.strftime("%Y%m%d_%H%M%S", time.localtime(msg_date)) + f"_{len(list(self.sets_dir.iterdir()))}"
        d = self.sets_dir / sid
        d.mkdir(parents=True, exist_ok=True)
        meta = {"id": sid, "chat_id": chat_id, "status": "collecting", "first_date": msg_date, "last_date": msg_date,
                "media_groups": [], "count": 0, "link": None, "candidates": []}
        meta["_dir"] = d
        self.save(meta)
        return meta


# ----------------------------------------------------------------- update handling

HELP = ("Commands: /status checks every key and shows the last run; /test is a dry run that writes nothing; "
        "/retry evaluates answers that are still waiting because Gemini was busy; /myid shows your Telegram ID.\n"
        "Send me the photos of one answer (as photos or, for best quality, as files). "
        "Send 'done' to finish a set straight away. "
        "You can send several answers in one go, in any mix: I split them by page content (a new question written "
        "at the top of a page starts a new answer), never by time. To force a split, send 'next' between two answers. "
        "I find each question, attach its pages under My answer, save the answer text, and set Status Done and "
        "Evaluated 'Not evaluated'. I do not evaluate (upload-only), so another AI can evaluate from Notion later.")


def best_file(msg):
    """Returns (file_id, extension) for an image photo or image document, else None."""
    if msg.get("photo"):
        ph = max(msg["photo"], key=lambda p: (p.get("file_size") or 0, p.get("width", 0) * p.get("height", 0)))
        return ph["file_id"], ".jpg", ph.get("file_unique_id")
    doc = msg.get("document")
    if doc:
        name = doc.get("file_name") or ""
        ext = Path(name).suffix.lower()
        mime = doc.get("mime_type") or ""
        if mime.startswith("image/") or ext in IMAGE_EXTS:
            if ext not in IMAGE_EXTS:
                ext = ".jpg" if "jpeg" in mime else "." + mime.split("/")[-1]
            return doc["file_id"], ext, doc.get("file_unique_id")
    return None


class Retrier:
    """Evaluates answers left as 'Evaluation pending' when Gemini was busy or out of quota.
    The list of waiting answers lives in Notion, so nothing is lost when the server sleeps or restarts.
    Runs in a background thread, never twice at the same time, and waits longer after each failure."""

    def __init__(self, listener, base_wait=600, max_wait=6 * 3600, min_gap=60, background=True):
        self.l = listener
        self.lock = threading.Lock()
        self.base_wait, self.max_wait, self.min_gap = base_wait, max_wait, min_gap
        self.wait = base_wait
        self.next_auto = 0.0  # automatic runs wait until this time after Gemini failed
        self.last_auto = 0.0
        self.bad = {}  # page id: times Gemini gave an unusable reply (skipped after 2, until restart)
        self.background = background
        self.thread = None

    def note_failure(self):
        self.next_auto = self.l.now() + self.wait
        self.wait = min(self.wait * 2, self.max_wait)

    def note_success(self):
        self.wait = self.base_wait
        self.next_auto = 0.0

    def start(self, chat_id, force=False, report=False):
        """Returns 'started', 'busy' (a retry is running), 'later' (backing off) or 'off' (Gemini switched off)."""
        if not self.l.ctx.evaluate:
            return "off"
        now = self.l.now()
        if not force and (now < self.next_auto or now - self.last_auto < self.min_gap):
            return "later"
        if not self.lock.acquire(blocking=False):
            return "busy"
        if not force:
            self.last_auto = now

        def job():
            try:
                self.run_locked(chat_id, report)
            except Exception as e:  # never kill the server
                self.l.log(f"retry problem: {e.__class__.__name__}: {e}")
                if report:
                    self.l.bot.send(chat_id, f"Retry problem: {e}")
            finally:
                self.lock.release()

        if self.background:
            self.thread = threading.Thread(target=job, name="retry-pending", daemon=True)
            self.thread.start()
        else:
            job()
        return "started"

    def run_locked(self, chat_id, report):
        def sent(res):
            if res.get("status") == "done":
                msgs = result_messages(res)
                msgs[0] = "Gemini is back: here is the evaluation that was pending.\n" + msgs[0]
                if res.get("other_answers"):
                    msgs[0] += (f"\nNote: this page also has photos of {res['other_answers']} other answer(s) that "
                                "were sent together with it. I evaluated only the part that answers this question "
                                "and left every photo where it is.")
                for part in msgs:
                    self.l.bot.send(chat_id, part)
            elif res.get("status") == "no_photos":
                self.l.bot.send(chat_id, f'I could not find the answer photos on "{res["question"]["title"]}", '
                                         "so I took it off the waiting list. Please send the photos again.")

        skip = {k for k, n in self.bad.items() if n >= 2}
        out = na.retry_pending(self.l.ctx, on_result=sent, skip=skip)
        for pid, _ in out["failed"]:
            self.bad[pid] = self.bad.get(pid, 0) + 1
        if out["stopped"]:
            self.note_failure()
        elif out["done"]:
            self.note_success()
        if out["done"] or out["found"]:
            self.l.record_run({"result": "retry", "reason": f"{out['done']} evaluated, {out['pending']} still waiting"})
        if report:
            if not out["found"]:
                text = "No answers are waiting for evaluation."
            else:
                text = f"Evaluated {out['done']} waiting answer(s). Still waiting: {out['pending']}."
                if out["stopped"]:
                    text += ("\nGemini is still busy or out of its free limit, so I stopped. I will try again by "
                             "myself when you next message me (the daily limit resets around 1:30 PM India time).")
                if out["failed"]:
                    text += f"\n{len(out['failed'])} answer(s) could not be evaluated this time."
            self.l.bot.send(chat_id, text)
        return out


class Listener:
    def __init__(self, ctx, bot=None, state=None, now=time.time, log=print, allow_unclaimed=False):
        cfg = ctx.cfg
        self.ctx = ctx
        self.log = log
        self.now = now
        self.allowed = {int(x) for x in cfg.get("allowed_user_ids") or []}
        self.gap = float(cfg.get("telegram_set_gap_seconds", 30))
        token = (cfg.get("telegram_bot_token") or "").strip()
        if bot is None:
            if not token or token.startswith("PASTE_"):
                raise na.FatalError("TELEGRAM TOKEN problem: no Telegram bot token found. Paste it into config.json as telegram_bot_token.")
            bot = TelegramBot(token)
        if not self.allowed and not allow_unclaimed:
            raise na.FatalError("allowed_user_ids in config.json is empty, so every message would be ignored.")
        self.bot = bot
        self.state = state or State(na.SCRIPT_DIR / "telegram_state")
        self.retrier = Retrier(self)

    def after_updates(self, updates):
        """After handling messages from Avi, quietly try the answers still waiting for Gemini (in the background)."""
        for upd in reversed(updates or []):
            msg = upd.get("message") or {}
            if (msg.get("from") or {}).get("id") in self.allowed:
                self.retrier.start(msg["chat"]["id"])
                return

    # ---- sets
    def open_set(self, chat_id):
        for m in self.state.sets():
            if m["chat_id"] == chat_id and m["status"] == "collecting":
                return m
        return None

    def awaiting_set(self, chat_id):
        for m in self.state.sets():
            if m["chat_id"] == chat_id and m["status"] == "awaiting_choice":
                return m
        return None

    def handle_update(self, upd):
        msg = upd.get("message") or {}
        user = (msg.get("from") or {}).get("id")
        said = (msg.get("text") or "").strip().lower()
        if user not in self.allowed:
            # Strangers are ignored, except /myid (harmless: it only tells them their own ID). A bot with no owner
            # yet explains how to claim it, so a friend setting up their copy can find their ID.
            if user is not None and (said in ("/myid", "myid") or (not self.allowed and said)):
                self.bot.send(msg["chat"]["id"], self.myid_text(user))
            return
        if said in ("/myid", "myid"):
            self.bot.send(msg["chat"]["id"], self.myid_text(user))
            return
        chat_id = msg["chat"]["id"]
        date = float(msg.get("date") or self.now())
        f = best_file(msg)
        if f:
            file_id, ext, unique_id = f
            group = msg.get("media_group_id")
            # Time never decides which photos belong to one answer: a photo joins the open set, and the set is
            # split by page content (question written at the top) when it is processed.
            cur = self.open_set(chat_id)
            if cur is None:
                cur = self.state.new_set(chat_id, date)
            cur["count"] += 1
            dest = cur["_dir"] / f"page_{cur['count']:03d}{ext}"
            self.bot.download(file_id, dest)
            cur["last_date"] = max(cur["last_date"], date)
            cur["last_seen"] = self.now()
            cur.setdefault("unique_ids", {})[dest.name] = unique_id
            if group and group not in cur["media_groups"]:
                cur["media_groups"].append(group)
            self.state.save(cur)
            self.log(f"  received page {cur['count']} for set {cur['id']}")
            return
        if msg.get("document") or msg.get("video") or msg.get("audio"):
            self.bot.send(chat_id, "Only photos of answer pages, please (jpg or png).")
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text:
            return
        low = text.lower()
        waiting = self.awaiting_set(chat_id)
        link_id = na.extract_page_id(text) if ("notion" in low or re.fullmatch(r"[0-9a-fA-F-]{32,36}", text)) else None
        if waiting and (re.fullmatch(r"[1-9]", low) or link_id or low in ("skip", "cancel")):
            if low in ("skip", "cancel"):
                waiting["status"] = "skip"
            elif link_id:
                waiting["link"] = link_id
                waiting["status"] = "ready"
            else:
                k = int(low)
                if k > len(waiting["candidates"]):
                    self.bot.send(chat_id, f"Please reply with a number from 1 to {len(waiting['candidates'])}, or paste a link.")
                    return
                waiting["link"] = waiting["candidates"][k - 1]["id"]
                waiting["status"] = "ready"
            self.state.save(waiting)
            return
        if re.fullmatch(r"[1-9]", low) and not waiting:
            self.bot.send(chat_id, "I have no question waiting for a number. If you were answering my question, the "
                                   "server restarted and lost those photos: please send the photos again.")
            return
        cur = self.open_set(chat_id)
        if low in ("done", "/done", "finish", "end") and cur:
            cur["status"] = "ready"
            self.state.save(cur)
            return
        if low.startswith(("/rearrange", "rearrange", "/sort", "sort")) and link_id:
            self.bot.send(chat_id, "Sorting the photos on that page into separate answers. This can take a few minutes...")
            try:
                out = na.rearrange_page(self.ctx, link_id)
            except na.FatalError:
                raise
            except (na.UserError, na.ge.GeminiError) as e:
                self.bot.send(chat_id, f"Could not sort that page: {e}\nNothing was removed.")
                return
            self.bot.send(chat_id, rearrange_text(out))
            for res in out["moved"] + ([out["own"]] if out.get("own") else []):
                if res.get("status") == "done" and res.get("eval"):
                    for part in result_messages(res):
                        self.bot.send(chat_id, part)
            return
        if low in ("next", "/next", "split", "/split"):
            if cur:
                cur.setdefault("breaks", [])
                if cur["count"] + 1 not in cur["breaks"]:
                    cur["breaks"].append(cur["count"] + 1)
                self.state.save(cur)
            self.bot.send(chat_id, "OK, the next photo starts a new answer.")
            return
        if link_id and cur:
            cur["link"] = link_id
            self.state.save(cur)
            self.bot.send(chat_id, "Noted: I will attach this set to that question.")
            return
        if low in ("/status", "status"):
            self.bot.send(chat_id, self.status_report())
            return
        if low in ("/retry", "retry"):
            r = self.retrier.start(chat_id, force=True, report=True)
            if r == "started" and self.retrier.background:
                self.bot.send(chat_id, "Checking the answers waiting for evaluation...")
            elif r == "busy":
                self.bot.send(chat_id, "I am already evaluating the waiting answers. Results will come here.")
            elif r == "off":
                self.bot.send(chat_id, "Evaluation is switched off (upload-only mode), so there is nothing to retry.")
            return
        if low in ("/test", "test"):
            self.bot.send(chat_id, self.test_report(chat_id))
            return
        if low in ("/start", "help", "/help", "you on?", "hi", "hello"):
            self.bot.send(chat_id, "I'm on. " + HELP)
            return
        if low in ("done", "/done"):
            self.bot.send(chat_id, "There is no open set. Send the answer photos first.")
            return
        self.bot.send(chat_id, HELP)

    def myid_text(self, user):
        text = f"Your Telegram user ID is {user}."
        if not self.allowed:
            text += ("\n\nThis bot has no owner yet, so it ignores photos. To make it yours: on render.com open this "
                     "service, then Environment, set ALLOWED_USER_IDS to "
                     f"{user}, and click Save Changes. The bot restarts in about a minute. Then send /status.")
        return text

    # ---- processing
    def ready_sets(self):
        now = self.now()
        out = []
        for m in self.state.sets():
            if m["status"] == "collecting" and now - max(m["last_date"], m.get("last_seen") or 0) >= self.gap:
                m["status"] = "ready"
                self.state.save(m)
            if m["status"] in ("ready", "skip") and now >= float(m.get("retry_after") or 0):
                out.append(m)
        return out

    def process(self, meta):
        chat = meta["chat_id"]
        d = meta["_dir"]
        photos = na.list_photos(d, "name")
        if meta["status"] == "skip":
            r = na.needs_review(self.ctx, {"name": f"telegram_{meta['id']}", "photos": photos, "folder": None},
                                "You chose to skip this set.")
            self.bot.send(chat, f"Skipped. The photos are in {r['folder']}")
            shutil.rmtree(d, ignore_errors=True)
            return
        if not photos:
            shutil.rmtree(d, ignore_errors=True)
            return
        if not meta.get("announced"):
            self.bot.send(chat, f"Got {len(photos)} page{'s' if len(photos) != 1 else ''}, working...")
            meta["announced"] = True
            self.state.save(meta)
        uids = meta.get("unique_ids") or {}
        s = {"name": f"telegram_{meta['id']}", "photos": photos, "folder": None,
             "captions": [f"tg:{uids[p.name]}" if uids.get(p.name) else None for p in photos],
             "breaks": list(meta.get("breaks") or [])}
        link = meta.get("link")
        if self.ctx.cfg.get("dedupe_with_notion", True) and all(uids.get(p.name) for p in photos) \
                and na.role_set(self.ctx.cfg, "proof"):
            try:
                dup = na.already_uploaded(self.ctx.notion(), self.ctx.cfg, [uids[p.name] for p in photos])
            except na.UserError:  # a key problem is reported by the main run just below
                dup = None
            if dup:
                self.bot.send(chat, f"These pages are already in Notion ({dup}), so I skipped them.")
                shutil.rmtree(d, ignore_errors=True)
                return
        try:
            res = na.process_set(self.ctx, s, link=link, ask_on_low_confidence=True, keep_on_busy=True)
        except na.GeminiBusy as e:
            # Gemini is slow or out of quota. Keep the photos and try again soon (Render sleeps after
            # 15 idle minutes, so retry within that window, at most 3 times).
            tries = int(meta.get("busy_tries") or 0) + 1
            if tries <= 3:
                wait = [5, 10, 12][tries - 1] * 60
                meta["busy_tries"] = tries
                meta["retry_after"] = self.now() + wait
                self.state.save(meta)
                self.bot.send(chat, f"Gemini is busy right now ({str(e)[:150]}). Your photos are kept and I will "
                                    f"try again at {time.strftime('%H:%M', time.localtime(meta['retry_after']))}.")
                self.record_run({"result": "gemini busy", "error": str(e)})
                return
            res = na.needs_review(self.ctx, s, f"Gemini could not read the photos after several tries ({e}). "
                                               "Please send them again later.")
        except na.FatalError as e:
            # A key or setup problem: keep the photos, tell Avi once, try again in 30 minutes.
            wait = float(self.ctx.cfg.get("retry_after_setup_error_minutes", 30)) * 60
            meta["retry_after"] = self.now() + wait
            if meta.get("last_error") != str(e):
                self.bot.send(chat, "SETUP PROBLEM, your photos are kept and I will retry in "
                                    f"{int(wait // 60)} minutes:\n\n{e}\n\nSee TROUBLESHOOTING.md. Send /status to check.")
            meta["last_error"] = str(e)
            self.state.save(meta)
            self.record_run({"result": "setup problem", "error": str(e)})
            return
        except na.UserError as e:
            res = na.needs_review(self.ctx, s, f"Notion problem: {e}")
        except Exception as e:  # never lose the photos
            res = na.needs_review(self.ctx, s, f"Unexpected error: {e.__class__.__name__}: {e}")
        status = res.get("status")
        if status == "multi":
            shutil.rmtree(d, ignore_errors=True)
            self.report_batch(chat, res)
            return
        self.record_run({"result": status, "question": (res.get("question") or {}).get("title"),
                         "score": na.fmt_score((res.get("eval") or {}).get("score")) if res.get("eval") else None,
                         "reason": res.get("reason"), "page": res.get("page_url")})
        if status == "ambiguous":
            top = res["top"][:3]
            meta["status"] = "awaiting_choice"
            meta["candidates"] = [{"id": c["id"], "title": c["title"]} for _, c in top]
            self.state.save(meta)
            lines = ["I'm not sure which question this is (" + res.get("reason", "low match") + ").",
                     "Reply 1, 2 or 3, paste the question's Notion link, or reply skip:"]
            for i, (sc, c) in enumerate(top, 1):
                where = ", ".join(x for x in [c.get("label") or "Geography optional", ", ".join(c.get("years") or [])] if x)
                lines.append(f"{i}. {c['title']} ({where}, {sc:.0%} match)")
            self.bot.send(chat, "\n".join(lines))
            return
        shutil.rmtree(d, ignore_errors=True)
        if status == "review":
            self.bot.send(chat, f"I could not finish this set: {res.get('reason')}\n"
                                f"The photos are saved on the PC in {res.get('folder')}")
            return
        if status == "done":
            if res.get("pending"):
                self.retrier.note_failure()  # Gemini just failed: do not retry at once
            for part in result_messages(res):
                self.bot.send(chat, part)

    def report_batch(self, chat, res):
        """Replies for a batch that held several answers: one block per answer, then a short summary."""
        results = res["results"]
        self.bot.send(chat, f"These photos hold {len(results)} different answers: "
                      + "; ".join(na.photo_range(nums) for nums, _ in results) + ". I handled each one separately.")
        done = pend = 0
        unmatched = []
        for nums, r in results:
            where = na.photo_range(nums)
            st = r.get("status")
            if st == "done":
                done += 1
                if r.get("pending"):
                    pend += 1
                    self.retrier.note_failure()
                msgs = result_messages(r)
                msgs[0] = f"Answer in {where}:\n" + msgs[0]
                for part in msgs:
                    self.bot.send(chat, part)
            else:
                unmatched.append((where, r))
        for where, r in unmatched:
            lines = [f"Answer in {where}: NOT attached anywhere ({r.get('reason') or r.get('status')})."]
            top = (r.get("top") or [])[:3]
            if top:
                lines.append("Closest questions:")
                lines += [f"- {c['title']}" for _, c in top]
            lines.append(f"Please send {where} again on their own (you can add the question's Notion link).")
            self.bot.send(chat, "\n".join(lines))
        summary = f"Batch finished: {done} of {len(results)} answers attached"
        if pend:
            summary += f", {pend} waiting for Gemini (I will evaluate them by myself, or send /retry)"
        if unmatched:
            summary += f", {len(unmatched)} not matched (see above)"
        self.bot.send(chat, summary + ".")
        self.record_run({"result": "batch", "reason": summary})

    # ---- status and test
    def record_run(self, info):
        info = dict(info, time=time.strftime("%Y-%m-%d %H:%M"))
        try:
            (self.state.root / "last_run.json").write_text(json.dumps(info), encoding="utf-8")
        except OSError:
            pass

    def last_run(self):
        try:
            return json.loads((self.state.root / "last_run.json").read_text(encoding="utf-8"))
        except Exception:
            return None

    def checks(self):
        """Read-only checks of every key. Returns a list of lines."""
        cfg, lines = self.ctx.cfg, []
        lines.append("Telegram bot token: OK (you are reading this)")
        try:
            client = self.ctx.notion()
            client.request("GET", "/users/me", what="checking the Notion key")
            lines.append("Notion key: OK")
            lines += self.database_lines(client)
        except na.UserError as e:
            lines.append("Notion key: FAILED. " + str(e))
        if self.ctx.uses_gemini():
            try:
                model = self.ctx.gemini().check()
                lines.append(f"Gemini key: OK (model {model})")
            except (na.UserError, na.ge.GeminiError) as e:
                lines.append("Gemini key/model: FAILED. " + str(e))
            lines.append("Evaluation: ON (Gemini marks every answer)" if self.ctx.evaluate else
                         "Evaluation: OFF (upload-only; Gemini only reads each batch once to split, match and copy the text)")
        else:
            lines.append("Gemini: switched off (photo-only mode)")
        return lines

    def database_lines(self, client):
        """One line per Notion database: connected (and whether it was found by name), not found, or off."""
        cfg, lines = self.ctx.cfg, []
        try:
            found = self.ctx.databases(refresh=True)
        except na.FatalError:
            raise
        except na.UserError as e:
            return ["  Databases: could not check. " + str(e).splitlines()[0]]
        connect = "(open it in Notion, ... menu, Connections, add your integration)"
        for role, (_, _, names, label) in na.ROLES.items():
            how = found.get(role)
            if how == "off":
                lines.append(f"  {label}: switched off")
            elif how == "not found":
                if role == "gs":
                    lines.append("  GS PYQs tracker: NOT reachable, add the connection (open GS PYQs in Notion, "
                                 "... menu, Connections, add your integration)."
                                 + (" Geography optional still works." if found.get("geo") in na.USABLE else ""))
                elif role == "geo":
                    lines.append("  Geography optional tracker: not found. Fine if you only use GS PYQs; "
                                 f"otherwise add the connection {connect}.")
                else:
                    lines.append(f"  {label} database: not found (optional; to use it, share '{names[0]}' "
                                 f"with your integration {connect}).")
            elif how == "env" and not na.can_open(client, cfg, role):
                lines.append(f"  {label} database: NOT reachable. Check {na.ROLE_ENV[role]}_DATABASE_ID on Render "
                             f"and the connection {connect}.")
            else:
                lines.append(f"  {label} database: connected" + (" (found by name)" if how == "found by name" else ""))
        return lines

    def status_report(self):
        lines = ["Status report"] + self.checks()
        waiting = [m for m in self.state.sets()]
        lines.append(f"Answer sets waiting: {len(waiting)}")
        for m in waiting:
            extra = f", retry at {time.strftime('%H:%M', time.localtime(m['retry_after']))}" if m.get("retry_after") else ""
            lines.append(f"  {m['id']}: {m['status']}, {m.get('count', 0)} page(s){extra}")
        if self.ctx.evaluate:
            try:
                pend = na.find_pending(self.ctx)
                lines.append(f"Answers waiting for evaluation: {len(pend)}" + (" (send /retry to try now)" if pend else ""))
                for q in pend[:5]:
                    lines.append(f"  {q['title'][:80]}")
                if self.retrier.next_auto > self.now():
                    lines.append("  Next automatic try after " + time.strftime(
                        "%H:%M", time.gmtime(self.retrier.next_auto + 19800)) + " India time")
            except na.UserError as e:
                lines.append("Answers waiting for evaluation: could not check. " + str(e).splitlines()[0])
        lr = self.last_run()
        if lr:
            desc = f"{lr.get('time')}: {lr.get('result')}"
            if lr.get("question"):
                desc += f', "{lr["question"][:90]}"'
            if lr.get("score"):
                desc += f", marks {lr['score']}"
            if lr.get("error") or lr.get("reason"):
                desc += f" ({lr.get('error') or lr.get('reason')})"
            lines.append("Last run: " + desc)
        else:
            lines.append("Last run: none yet")
        return "\n".join(lines)

    def test_report(self, chat_id):
        """Dry run: checks keys and, if photos are waiting, reads them and finds the question. Writes nothing."""
        lines = ["Test (dry run, nothing is written)"] + self.checks()
        try:
            rows = self.ctx.tracker_rows(refresh=True)
            n_gs = sum(1 for r in rows if r.get("tracker") == "gs")
            lines.append(f"Tracker questions readable: {len(rows)} (Geography optional {len(rows) - n_gs}, GS PYQs {n_gs})")
        except na.UserError as e:
            lines.append("Tracker questions: FAILED. " + str(e))
            return "\n".join(lines)
        cur = self.open_set(chat_id) or next((m for m in self.state.sets() if m["chat_id"] == chat_id), None)
        if not cur or not self.ctx.uses_gemini():
            lines.append("To test question matching too: send the answer photos, then /test within a minute.")
            return "\n".join(lines)
        photos = na.list_photos(cur["_dir"], "name")
        tmpdir = Path(tempfile.mkdtemp())
        try:
            shrunk = []
            for i, p in enumerate(photos, 1):
                na.shrink_image(p, tmpdir / f"t{i}.jpg")
                shrunk.append(tmpdir / f"t{i}.jpg")
            read = self.ctx.gemini().read_answer(shrunk, side=self.ctx.cfg.get("gemini_read_side", na.ge.READ_SIDE))
            cand, reason, top = na.choose_question(self.ctx.cfg, self.ctx.gemini(), read, rows, log=lambda *a: None)
            lines.append(f'Question read from {len(photos)} photo(s): "{read.get("question_written") or read.get("question_guess")}"')
            lines.append(f'Would attach to: "{cand["title"]}" in {cand.get("label") or "Geography optional"} ({reason})'
                         if cand else f"Would ask you to choose: {reason}")
        except (na.UserError, na.ge.GeminiError) as e:
            lines.append("Reading/matching FAILED: " + str(e))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return "\n".join(lines)

    # ---- loop
    def run(self, max_cycles=None, sleep=time.sleep, poll_timeout=20):
        offset = self.state.offset()
        self.log("Listening to Telegram. Send answer photos to your bot. Press Ctrl+C to stop.")
        cycles, conflict_wait = 0, 5
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            try:
                wait = poll_timeout
                for m in self.state.sets():
                    if m["status"] == "collecting":
                        wait = min(wait, max(1, int(self.gap - (self.now() - max(m["last_date"], m.get("last_seen") or 0))) + 1))
                updates = self.bot.get_updates(offset, wait)
                conflict_wait = 5
            except TelegramError as e:
                if str(e).startswith("CONFLICT"):
                    self.log("Another program is reading this bot's messages (only one can at a time). "
                             f"Waiting {conflict_wait} s. Stop the other listener, or use a new bot from @BotFather.")
                else:
                    self.log(f"Telegram problem: {e}. Retrying in {conflict_wait} s.")
                sleep(conflict_wait)
                conflict_wait = min(conflict_wait * 2, 120)
                updates = []
            for upd in updates:
                try:
                    self.handle_update(upd)
                except TelegramError as e:
                    self.log(f"  could not save a photo: {e}")
                except na.FatalError:
                    raise
                except Exception as e:  # one odd message must not stop the listener
                    self.log(f"  skipped a message ({e.__class__.__name__}: {e})")
                offset = int(upd["update_id"]) + 1
                self.state.save_offset(offset)
            for meta in self.ready_sets():
                self.process(meta)
            self.after_updates(updates)
        return 0


def result_messages(res):
    q, ev = res["question"], res.get("eval")
    years = ", ".join(q.get("years") or [])
    wl = na.marking(q)[1] if q.get("paper") else (int(q["word_limit"]) if q.get("word_limit") else "?")
    where = ", ".join(x for x in [q.get("label") or "Geography optional", years, f"{wl or '?'} words"] if x)
    head = [f'Matched question ({where}):\n"{q["title"]}"']
    msgs = []
    if res.get("upload_only") and not ev:
        n = res.get("photos") or 0
        line = f"Attached {n} page{'s' if n != 1 else ''}. Status: {res.get('status_set')}"
        if res.get("evaluated_set"):
            line += f", Evaluated: {res['evaluated_set']}"
        head.append(line + ".")
        head.append("Answer text saved under the photos." if res.get("answer_text") else "No answer text saved (only photos).")
        head.append(f"Notion: {res['page_url']}")
        return ["\n".join(head)]
    if ev:
        head.append(f"Marks: {na.fmt_score(ev['score'])} / {na.fmt_score(ev['out_of'])} (Gemini)")
        head.append(f"Status: {res.get('status_set')}")
        head.append(f"Notion: {res['page_url']}")
        msgs.append("\n".join(head))
        body = [ev.get("chat_summary", ""), "", "Strengths:"]
        body += [f"- {s['point']}: {s['detail']}" for s in ev["strengths"]]
        body += ["", "Mistakes to fix:"]
        body += [f"{i}. {m['point']}: {m['detail']}" for i, m in enumerate(ev["mistakes"], 1)]
        msgs.append("\n".join(body))
        rem = ["Remarks card: " + ev.get("card_name", "")]
        for sec in ev.get("card_sections") or []:
            rem.append("")
            rem.append(sec.get("heading", ""))
            rem += [f"- {b}" for b in sec.get("bullets", [])]
        if res.get("card_url"):
            rem += ["", res["card_url"]]
        msgs.append("\n".join(rem))
    else:
        head.append("Photos attached. Status: " + str(res.get("status_set")))
        head.append(f"Notion: {res['page_url']}")
        if res.get("pending"):
            head.append("Gemini could not evaluate it, so I added an 'Evaluation pending' line. I will evaluate it "
                        "by myself once Gemini is back (or send /retry). If you prefer, paste the note below into ChatGPT.")
        msgs.append("\n".join(head))
        if res.get("gpt_note"):
            msgs.append(res["gpt_note"])
    return msgs


def rearrange_text(out):
    lines = [f"That page had {out['photos']} photos holding {out['groups']} answers."]
    if out["groups"] < 2:
        return lines[0] + " Nothing to move."
    lines.append(f"Moved to their own question pages: {len(out['moved'])} answer(s), "
                 f"{out['removed']} photo(s) taken off the first page.")
    for where, why in out["left"]:
        lines.append(f"Left in place ({why}): {where}")
    if out.get("own_status"):
        lines.append("This page's own answer: " + out["own_status"])
    return "\n".join(lines)


def run_telegram(ctx, **kw):
    return Listener(ctx).run(**kw)
