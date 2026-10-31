"""
Switch Telegram between the Render server (webhook) and the PC listener (polling).

  python set_webhook.py https://your-app.onrender.com   (send messages to Render)
  python set_webhook.py --delete                         (back to the PC listener, run_telegram.bat)
  python set_webhook.py --info                           (show the current setting)

Uses telegram_bot_token and webhook_secret from config.json, or the TELEGRAM_BOT_TOKEN and
WEBHOOK_SECRET environment variables. Waiting messages are kept (drop_pending_updates is false).
"""

import sys

import notion_attach as na
import telegram_mode as tm
from webhook_server import webhook_path


def main(argv):
    na.setup_console()
    cfg = na.load_config()
    token = (cfg.get("telegram_bot_token") or "").strip()
    if not token or token.startswith("PASTE_"):
        print("No Telegram bot token in config.json (telegram_bot_token).")
        return 2
    bot = tm.TelegramBot(token)
    try:
        if not argv or argv[0] in ("--info", "-i"):
            info = bot.call("getWebhookInfo")
            url = info.get("url") or ""
            print("Current mode: " + ("WEBHOOK (Render): " + url.split("/telegram/")[0] if url
                                      else "PC listener (no webhook set)"))
            print(f"Messages waiting: {info.get('pending_update_count', 0)}")
            if info.get("last_error_message"):
                print("Last delivery error: " + info["last_error_message"])
            return 0
        if argv[0] in ("--delete", "-d"):
            bot.call("deleteWebhook", drop_pending_updates=False)
            print("Webhook removed. Start run_telegram.bat on the PC to receive messages.")
            return 0
        secret = (cfg.get("webhook_secret") or "").strip()
        if not secret:
            print("Set webhook_secret in config.json (or the WEBHOOK_SECRET variable) to the same value as on Render.")
            return 2
        url = argv[0].rstrip("/") + "/telegram/" + webhook_path(secret)
        bot.call("setWebhook", url=url, secret_token=secret, drop_pending_updates=False,
                 allowed_updates=["message"], max_connections=5)
        print("Webhook set. Telegram now sends messages to " + argv[0])
        print("Stop run_telegram.bat on the PC: only one of the two can receive messages.")
        return 0
    except (tm.TelegramError, na.UserError) as e:
        print("PROBLEM: " + str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
