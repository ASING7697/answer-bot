# Troubleshooting

First, send /status to your bot. It checks every key and shows the last run. If the bot does not answer at all, see section 3 or 4.

When something fails, the bot tells you on Telegram which part failed. The message starts with words like NOTION KEY problem, GEMINI KEY problem, GEMINI MODEL problem or NOTION COLUMN problem. Your photos are kept and it tries again in 30 minutes, so nothing is lost while you fix it.

Where keys live:
- On Render: your service, then "Environment". Edit, then "Save Changes".
- On the PC: config.json (open with Notepad). After changing it, close and restart run_telegram.bat.

---

## 1. A key expired, was revoked or was mistyped

### Notion key (NOTION KEY problem)
1. Open notion.so/profile/integrations and click your "Photo Uploader" integration.
2. Under "Internal Integration Secret", click Show and copy it. If you think it leaked, click "Refresh" to make a new one, then copy that.
3. Put it in NOTION_TOKEN (Render) or notion_token (config.json).
4. If /status says a database is "NOT reachable", reconnect it: open that database or page in Notion, the ... menu, Connections, add Photo Uploader. You need all three: "Geography optional", "200 Answer Challenge: Proof Log" and "Remarks to revise".

### Gemini key (GEMINI KEY problem)
1. Open aistudio.google.com, then "Get API key".
2. Click "Create API key" and copy the new key. You can delete the old one there.
3. Put it in GEMINI_API_KEY (Render) or gemini_api_key (config.json).

### Telegram token (the bot does not answer at all, or the PC window says the token is not valid)
1. In Telegram, open @BotFather, send /mybots, choose your bot, then "API Token". Copy it. If it leaked, choose "Revoke current token" to get a new one.
2. Put it in TELEGRAM_BOT_TOKEN (Render) or telegram_bot_token (config.json).
3. On Render, open the /setup/ link again afterwards (see DEPLOY_RENDER.md, Step 5), because a new token needs the webhook set again.

## 1b. GS PYQs answers are not found ("GS PYQs tracker: NOT reachable")

The bot searches both trackers. If /status says "GS PYQs tracker: NOT reachable, add the connection", open the GS PYQs database in Notion, click the ... menu (top right), Connections, and add the same integration you use for Geography optional. Until then, Geography optional answers keep working as before.

The GS database is built in. Only if you ever move it to a new database: on Render add GS_TRACKER_DATABASE_ID (the 32 characters in its link) under Environment. Setting it to off switches GS PYQs off.

GS rows in the proof log and Remarks have no "Tracker question" link (those link columns only point to Geography optional). Instead the row names the paper in Section and has a line with the GS question's link.

## 2. Gemini says the daily limit is reached

The free plan has a daily limit. It resets at midnight US Pacific time (about 1:30 PM India time). Meanwhile the tool still attaches your pages, adds an "Evaluation pending" line and writes "Evaluation pending" in Gemini Remark. That Gemini Remark text is the waiting list, so do not edit it by hand.

You do not need to do anything. Whenever you message the bot (any message, even "hi"), it tries the waiting answers in the background, oldest first, and stops as soon as Gemini is still busy. After a failure it waits at least 10 minutes before the next automatic try, then longer each time, so it never hammers Gemini. When an answer is evaluated, Notion is filled exactly as usual and the bot sends you the evaluation with the question.

- /status shows how many answers are waiting.
- /retry tries straight away and tells you how many were done and how many are still waiting.
- Optional: a free cron website (for example cron-job.org) can open your Render address + /retry/ + your WEBHOOK_SECRET every hour, so waiting answers are evaluated even if you do not message the bot. Keep that link private.
- If you would rather not wait, the bot also gives you a note to paste into ChatGPT. You can see your limits in AI Studio under "Rate limit".

## 3. Render: the bot is slow or silent

- **Asleep:** the free server sleeps after 15 minutes without messages. The next message wakes it in about a minute. Telegram retries by itself. Wait two minutes before worrying.
- **Check it is alive:** open your Render address in the browser. You should see {"ok": true ...}. If "last_error" shows something, it tells you what failed.
- **Suspended:** Render pauses free services if the month's 750 free hours or the free bandwidth run out (very unlikely with one service), or if the account has a problem. Log in to render.com and look at the service page and the email Render sent. Meanwhile, switch to the PC (section 7). Free hours reset on the 1st of each month.
- **Webhook not set:** on the PC run python set_webhook.py --info. If it says "PC listener", open the /setup/ link again.
- **Logs:** in Render, open your service, then "Logs", to see what happened.

## 4. PC: the bot does not answer

- Is run_telegram.bat running? Its window must be open (it can be minimised).
- If the window says "Another program is reading this bot's messages", the Render webhook is active. Either use Render, or switch to the PC (section 7).
- Run check_setup.bat to test every key.

## 5. The Gemini model was retired (GEMINI MODEL problem)

Google retires old models from time to time. The tool first tries the model in gemini_model, then the ones in gemini_fallback_models. If all are gone:
1. Open ai.google.dev/gemini-api/docs/models and pick the newest "Flash" model that is marked Stable, for example gemini-3.8-flash. Copy its exact name from the "Endpoint" or model code column.
2. On Render: change GEMINI_MODEL in Environment. On the PC: change this line in config.json:
   "gemini_model": "gemini-3.8-flash",
3. Optionally also update the line "gemini_fallback_models" with one or two other current Flash names.

## 6. A Notion column was renamed or deleted (NOTION COLUMN problem)

The tool needs these exact column names. Rename the column back in Notion (click the column header, then rename).

- Tracker "Geography optional": Question, Status, Word Limit, Original Section, Year, Gemini Marks, Gemini Remark. Status must still have the options Done and Knowledge gap.
- Tracker "GS PYQs": Question, Status, Year, Paper, Topic, Marks, Word Limit, Gemini Marks, Gemini Remark, Date. Status must still have the options Done and Knowledge gap.
- Proof log "Answers written": Question, No., Date sent, Section, Score, Out of, Attempt, Tracker question.
- "Remarks": Name, Date, Section, Score, Question.

If Gemini Marks or Gemini Remark are missing, the tool creates them by itself. If you prefer different names for those two, change gemini_marks_property and gemini_remark_property in config.json (PC only).

## 7. Switching between Render and the PC

Only one can receive Telegram messages at a time.
- Render to PC: first add AUTO_WEBHOOK with the value off in Render's Environment and click Save Changes (otherwise Render takes Telegram back whenever it restarts). Then open your Render address + /unset/ + your WEBHOOK_SECRET in the browser (or run python set_webhook.py --delete on the PC), then start run_telegram.bat.
- PC to Render: close run_telegram.bat, delete AUTO_WEBHOOK in Render's Environment, or open your Render address + /setup/ + your WEBHOOK_SECRET.
- Other host: see the PythonAnywhere section at the end of DEPLOY_RENDER.md.

## 8. The question was not found

- The bot lists the 3 closest questions. Reply 1, 2 or 3, paste the question's Notion link (Notion: open the question, Share, Copy link), or reply skip.
- Make sure the question is written clearly at the top of the first page, in the same words as the PYQ.
- If the question is not in your tracker at all, add it to the tracker first, then send the photos again.
- On the PC, sets that could not be matched go to answers_inbox, needs_review, with a REASON.txt listing the closest questions and the command to finish them.
- Use /test after sending photos to see which question it would choose, without writing anything.

## 9. Telegram's 24-hour limit

Telegram keeps undelivered messages for only 24 hours. If neither Render nor the PC listener is running for more than a day, photos sent in that time are lost and must be sent again. Render normally prevents this, since it is always reachable.

## 10. Photos look wrong or in the wrong order

Gemini puts the pages in reading order. If the order is wrong, number your pages at the top (1, 2, 3). The tool keeps every photo under Notion's 5 MB limit automatically.

## 11. Starting over on the PC

Close the window, then delete the folder telegram_state next to the program. Waiting answer sets are lost, but nothing in Notion is touched.

## 12. The bot only replies with "Your Telegram user ID is ..."

The bot answers only the Telegram IDs in ALLOWED_USER_IDS. Everyone else just gets their own ID back. On render.com open the service, then Environment, put your ID in ALLOWED_USER_IDS (several IDs separated by commas) and click Save Changes. Wait about 2 minutes for the restart, then send /status.

## 13. "No tracker found" or a database "not found"

The bot looks for databases named GS PYQs, Geography optional, Answers written and Remarks that your Notion key can see. Connect the key to each database (the ••• menu, then Connections) and keep those names. If you renamed a database, either change the name back or put its ID in Render's Environment as GS_TRACKER_DATABASE_ID, TRACKER_DATABASE_ID, PROOF_LOG_DATABASE_ID or REMARKS_DATABASE_ID. /status lists what it found.
