"""
Telegram webhook server for the Notion Photo Uploader (for Render's free web service).

Plain WSGI app, no framework. Start with:
  gunicorn webhook_server:app --workers 1 --threads 4 --timeout 120
Routes:
  GET  /                         health check
  POST /telegram/<path>          Telegram webhook (checks X-Telegram-Bot-Api-Secret-Token)
  GET  /setup/<WEBHOOK_SECRET>   registers the webhook with Telegram (open once after deploy)
  GET  /unset/<WEBHOOK_SECRET>   removes the webhook (to switch back to the PC listener)
  GET  /retry/<WEBHOOK_SECRET>   evaluates answers still waiting for Gemini (optional, for a free cron pinger)
Settings come from env vars: TELEGRAM_BOT_TOKEN, NOTION_TOKEN, GEMINI_API_KEY, WEBHOOK_SECRET,
ALLOWED_USER_IDS (comma separated). Optional: PUBLIC_URL (Render sets RENDER_EXTERNAL_URL itself).
"""

import argparse
import collections
import hashlib
import hmac
import json
import os
import queue
import tempfile
import threading
import time
from pathlib import Path

import notion_attach as na
import telegram_mode as tm



# Show times in Avi's time zone (the Render server clock is UTC).
os.environ.setdefault("TZ", "Asia/Kolkata")
if hasattr(time, "tzset"):
    time.tzset()

def webhook_path(secret):
    """URL path segment derived from the secret, so the secret itself never shows in request logs."""
    return hashlib.sha256(("path:" + secret).encode()).hexdigest()[:32]


class WebhookService:
    def __init__(self, cfg=None, bot=None, client=None, gem=None, data_dir=None, log=print, start_worker=True):
        self.cfg = cfg if cfg is not None else na.load_config()
        if cfg is None:  # on a server there is no Google Drive folder unless DRIVE_FOLDER is set
            self.cfg["drive_folder"] = os.environ.get("DRIVE_FOLDER", "")
        self.secret = (self.cfg.get("webhook_secret") or "").strip()
        self.log = log
        data = Path(data_dir or os.environ.get("UPLOADER_DATA_DIR") or Path(tempfile.gettempdir()) / "notion_uploader")
        data.mkdir(parents=True, exist_ok=True)
        args = argparse.Namespace(link=None, folder=str(data / "inbox"), config=None, watch=False, telegram=True,
                                  no_eval=False, dry_run=False, check=False)
        self.ctx = na.Context(args, self.cfg, client, gem, log=log)
        token = (self.cfg.get("telegram_bot_token") or "").strip()
        self.bot = bot or tm.TelegramBot(token)
        if cfg is None:  # a real deploy: work out who owns this bot
            self.cfg["allowed_user_ids"] = self.owner_ids()
        self.listener = tm.Listener(self.ctx, bot=self.bot, state=tm.State(data / "telegram_state"), log=log,
                                    allow_unclaimed=True)
        self.seen = collections.deque(maxlen=2000)
        self.seen_set = set()
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.last_error = None
        self.processed = 0
        self.worker = None
        if start_worker:
            self.start()

    def owner_ids(self):
        """ALLOWED_USER_IDS if set. Otherwise the user IDs in config.json, but only on the original owner's deploy,
        recognised by its Notion key opening the tracker IDs in config.json (no one else's key can). A friend's copy
        without ALLOWED_USER_IDS has no owner: it ignores photos and tells whoever writes how to claim it (/myid)."""
        legacy = [int(x) for x in self.cfg.get("allowed_user_ids") or []]
        if self.cfg.get("_allowed_from_env") or not legacy:
            return legacy
        try:
            found = self.ctx.databases()
        except na.UserError as e:  # cannot tell right now (no internet, bad key): keep the original behaviour
            self.log(f"owner check postponed: {str(e).splitlines()[0]}")
            return legacy
        if any(found.get(r) == "configured" for r in ("geo", "gs")):
            return legacy
        self.log("No ALLOWED_USER_IDS set: the bot has no owner yet. Send it /myid on Telegram.")
        return []

    def auto_webhook(self, base_url):
        """Points Telegram at this server if it is not already (safe to repeat; does nothing when already set)."""
        if not self.secret or not base_url:
            return "skipped"
        want = base_url.rstrip("/") + "/telegram/" + webhook_path(self.secret)
        info = self.bot.call("getWebhookInfo") or {}
        if info.get("url") == want:
            return "already set"
        self.set_webhook(base_url)
        self.log("Telegram webhook set to this server.")
        return "set"

    # ---- background worker
    def start(self):
        if self.worker is None or not self.worker.is_alive():
            self.worker = threading.Thread(target=self._loop, name="uploader-worker", daemon=True)
            self.worker.start()

    def _loop(self):
        while True:
            self.step(block=True)

    def step(self, block=False):
        """Handle queued updates, then process any answer set that is complete."""
        updates = []
        try:
            updates.append(self.q.get(timeout=3) if block else self.q.get_nowait())
            while True:  # take everything that is waiting, so an album stays together
                updates.append(self.q.get_nowait())
        except queue.Empty:
            pass
        with self.lock:
            try:
                for upd in updates:
                    try:
                        self.listener.handle_update(upd)
                    except na.FatalError:
                        raise
                    except Exception as e:
                        self.log(f"skipped a message ({e.__class__.__name__}: {e})")
                for meta in self.listener.ready_sets():
                    self.listener.process(meta)
                    self.processed += 1
                self.listener.after_updates(updates)  # retries waiting evaluations in its own thread
            except na.FatalError as e:
                self.last_error = str(e)
                self.log("SETUP PROBLEM: " + str(e))
                time.sleep(30 if block else 0)
            except Exception as e:  # keep the worker alive
                self.last_error = f"{e.__class__.__name__}: {e}"
                self.log("ERROR: " + self.last_error)

    # ---- webhook
    def accept(self, update, header_token):
        """Returns an HTTP status. Never does slow work here."""
        if not self.secret or not hmac.compare_digest(header_token or "", self.secret):
            return 403
        try:
            uid = int(update["update_id"])
        except (KeyError, TypeError, ValueError):
            return 200
        if uid in self.seen_set:
            return 200  # repeat delivery
        if len(self.seen) == self.seen.maxlen:
            self.seen_set.discard(self.seen[0])
        self.seen.append(uid)
        self.seen_set.add(uid)
        self.q.put(update)
        return 200

    def set_webhook(self, base_url):
        url = base_url.rstrip("/") + "/telegram/" + webhook_path(self.secret)
        self.bot.call("setWebhook", url=url, secret_token=self.secret, drop_pending_updates=False,
                      allowed_updates=["message"], max_connections=5)
        return url

    def health(self):
        return {"ok": True, "service": "notion-photo-uploader", "owner_set": bool(self.listener.allowed),
                "evaluate": self.listener.mode() == "evaluate", "mode": self.listener.mode(),
                "queued": self.q.qsize(),
                "open_sets": len(self.listener.state.sets()), "sets_processed": self.processed,
                "last_error": self.last_error}


# ----------------------------------------------------------------- WSGI

_service = None
_service_lock = threading.Lock()


def get_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = WebhookService()
            start_auto_webhook(_service)
        return _service


def start_auto_webhook(svc, env=os.environ):
    """On Render, set the Telegram webhook by itself at start-up, so the /setup/ link is not needed.
    Switch off with AUTO_WEBHOOK=off (for example while using the PC listener)."""
    base = (env.get("PUBLIC_URL") or env.get("RENDER_EXTERNAL_URL") or "").strip()
    if not base or env.get("AUTO_WEBHOOK", "").strip().lower() in ("off", "0", "no", "false"):
        return None

    def job():
        try:
            svc.auto_webhook(base)
        except Exception as e:  # never stop the server over this; /setup/ still works
            svc.log(f"auto webhook skipped: {e}".replace(svc.bot.token or "~", "[token]"))
    t = threading.Thread(target=job, name="auto-webhook", daemon=True)
    t.start()
    return t


def _respond(start_response, status, body, ctype="application/json"):
    data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json" else body).encode()
    reasons = {200: "OK", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 400: "Bad Request", 500: "Error"}
    start_response(f"{status} {reasons.get(status, 'OK')}", [("Content-Type", ctype), ("Content-Length", str(len(data)))])
    return [data]


def make_app(service_getter=get_service):
    def app(environ, start_response):
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")
        try:
            svc = service_getter()
        except Exception as e:  # e.g. a missing key: show it on the health page instead of crashing
            return _respond(start_response, 500, {"ok": False, "error": str(e)})
        if path in ("/", "/health") and method in ("GET", "HEAD"):
            return _respond(start_response, 200, svc.health())
        if path.startswith("/telegram/"):
            if method != "POST":
                return _respond(start_response, 405, {"ok": False})
            if not svc.secret or not hmac.compare_digest(path.split("/", 2)[2], webhook_path(svc.secret)):
                return _respond(start_response, 404, {"ok": False})
            try:
                n = int(environ.get("CONTENT_LENGTH") or 0)
                update = json.loads(environ["wsgi.input"].read(n) or b"{}")
            except (ValueError, KeyError):
                return _respond(start_response, 400, {"ok": False})
            code = svc.accept(update, environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN"))
            return _respond(start_response, code, {"ok": code == 200})
        if path.startswith("/retry/"):
            if not svc.secret or not hmac.compare_digest(path.split("/", 2)[2], svc.secret):
                return _respond(start_response, 404, {"ok": False})
            ids = sorted(svc.listener.allowed)
            result = svc.listener.retrier.start(ids[0]) if ids else "off"
            return _respond(start_response, 200, {"ok": True, "retry": result})
        if path.startswith("/setup/") or path.startswith("/unset/"):
            given = path.split("/", 2)[2]
            if not svc.secret or not hmac.compare_digest(given, svc.secret):
                return _respond(start_response, 404, {"ok": False})
            try:
                if path.startswith("/unset/"):
                    svc.bot.call("deleteWebhook", drop_pending_updates=False)
                    msg = "Webhook removed. Telegram messages now wait for the PC listener (run_telegram.bat)."
                else:
                    base = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
                            or f"https://{environ.get('HTTP_HOST', '')}")
                    svc.set_webhook(base)
                    msg = ("Webhook set. Telegram now sends your messages to this server. "
                           "Send the bot a photo to test.")
            except Exception as e:
                return _respond(start_response, 500, f"Failed: {e}".replace(svc.bot.token or "~", "[token]"),
                                "text/plain; charset=utf-8")
            return _respond(start_response, 200, msg, "text/plain; charset=utf-8")
        return _respond(start_response, 404, {"ok": False})
    return app


app = make_app()


if __name__ == "__main__":  # local test: python webhook_server.py
    from wsgiref.simple_server import make_server
    port = int(os.environ.get("PORT", 8000))
    print(f"Serving on http://localhost:{port}")
    make_server("", port, app).serve_forever()
