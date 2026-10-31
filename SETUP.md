# Setup on your Windows PC

This takes about 30 minutes, once. You need three free keys: Notion, Gemini and Telegram.

---

## Step 1. Install Python

1. Go to python.org, click Downloads, and download Python 3.13 for Windows. (3.12 or 3.14 also work.)
2. Open the downloaded file.
3. IMPORTANT: on the first screen, tick "Add python.exe to PATH" at the bottom.
4. Click "Install Now" and wait for "Setup was successful".

## Step 2. Unzip the tool

1. Right-click Notion_Photo_Uploader.zip and choose "Extract All".
2. Put it somewhere easy, for example Documents. You get a folder named Notion_Photo_Uploader.
3. Keep the original zip as well. Its config.json has no keys in it, which is the copy you will upload to GitHub later.

## Step 3. Install the helper packages

Double-click install.bat. Wait for "Successfully installed" or "Requirement already satisfied", then press any key.

## Step 4. Notion key (free)

1. Open notion.so/profile/integrations and log in.
2. Click "New integration". Name: Photo Uploader. Workspace: yours. Type: Internal. Save.
3. Under Capabilities, tick Read content, Update content and Insert content. Save.
4. Next to "Internal Integration Secret", click Show, then Copy. It starts with ntn_
5. Connect it to your pages. For EACH of these three, open it in Notion, click the ... menu at the top right, click Connections, find Photo Uploader and confirm:
   - the "Geography optional" database (your PYQ tracker)
   - the page "200 Answer Challenge: Proof Log" (this covers the "Answers written" database)
   - the page "Remarks to revise" (this covers the "Remarks" database)

## Step 5. Gemini key (free)

1. Open aistudio.google.com and sign in with your Google account.
2. Click "Get API key", then "Create API key". Copy it. It usually starts with AIza
3. Free plan facts, in plain words:
   - It costs nothing and needs no card.
   - Google sets daily and per-minute limits for each model. It no longer publishes fixed numbers, so see yours in AI Studio under "Rate limit". The limits apply per Google project, not per key.
   - The daily count resets at midnight US Pacific time, which is about 1:30 PM India time (12:30 PM during US summer time).
   - Each answer uses 2 Gemini requests (3 if the question is hard to match), so even a small daily limit covers several answers a day.
   - On the free plan, Google may use what you send to improve its products.
   - If you hit the limit, the tool still attaches your photos, adds an "Evaluation pending" line and gives you a note for ChatGPT.

## Step 6. Telegram bot token

You already have the bot @thisbot_is_mybest_bot.

1. In Telegram, open @BotFather and send /mybots.
2. Choose your bot, then "API Token". Copy the token (it looks like 123456789:ABC...).
3. If you would rather start fresh, send /newbot to @BotFather instead and follow the steps. It gives you a new token.

Only your Telegram account (user ID 8308385292) is allowed. Messages from anyone else are ignored.

## Step 7. Fill in config.json

1. Right-click config.json, choose "Open with", then Notepad.
2. Replace these three placeholders, keeping the quote marks:
   - PASTE_YOUR_NOTION_SECRET_HERE with the Notion key
   - PASTE_YOUR_GEMINI_KEY_HERE with the Gemini key
   - PASTE_YOUR_TELEGRAM_BOT_TOKEN_HERE with the Telegram token
3. Do not change the database lines. They already hold your real IDs:
   - tracker: f051f0c54ccc8340ae6281b044b2e5da (data source e091f0c5-4ccc-8231-b554-070a930b6c20)
   - proof log "Answers written": c46cec3441814d9194432652c73ec675 (data source 7cdb751d-238a-4498-9fb7-137c980c6d34)
   - Remarks: 7f9e63e326d24a24aba8771959de0eb9 (data source 68e1fafa-bba9-4298-a9c7-aa87a13d56c5)
4. Google Drive copy (optional, PC only): install "Google Drive for desktop", create the folder My Drive, UPSC_Answers, and check the line "drive_folder": "G:\\My Drive\\UPSC_Answers". Write every backslash twice. To switch it off, write "drive_folder": "",
5. Save (Ctrl+S). Never upload this filled-in config.json anywhere.

## Step 8. Test

Double-click check_setup.bat. You should see the Notion key, all three databases, the Gemini key and the Telegram bot listed as working. If something fails, the message says which key. See TROUBLESHOOTING.md.

---

## Daily use on the PC

### Recommended: Telegram

1. Double-click run_telegram.bat and leave the window open (you can minimise it).
2. On your phone, send the photos of one answer to your bot. Sending them as files ("File", then choose the photos) keeps full quality. Normal photos also work.
3. Photos sent within a minute of each other count as one answer. Send done to finish straight away.
4. The bot replies "Got 3 pages, working...", then the matched question, your marks, the evaluation and the remarks, with the Notion link.
5. If it is not sure which question it is, it lists 3 choices. Reply 1, 2 or 3, paste the question's Notion link, or reply skip.

Good to know:
- Photos sent while the PC is off are processed when you next start run_telegram.bat, but only if that is within 24 hours. Telegram deletes waiting messages after 24 hours.
- Only one program can read the bot's messages at a time. If the Render server (DEPLOY_RENDER.md) is active, the PC listener waits and tells you so. TROUBLESHOOTING.md shows how to switch.
- To start the listener automatically with Windows, double-click start_on_boot.bat. To stop that, double-click remove_from_boot.bat. (Manual way: press Windows key + R, type shell:startup, press Enter, and put a shortcut to run_telegram.bat in that folder.)

### Other ways (backup)

- Folder, once: put photos in answers_inbox (or one subfolder per answer) and double-click run.bat.
- Folder, watching: double-click run_watch.bat. Each photo set is processed one minute after its last photo arrives.
- Photos only, no Gemini: open Command Prompt in the folder and run
  python notion_attach.py --no-eval (question link)
  It attaches the photos and gives you a note to paste into ChatGPT.

### Where things end up

- Question page: your pages under "My answer", then "Evaluation by Gemini (date)" with the score, demand check, strengths and mistakes to fix.
- Tracker columns: Status (Done, or Knowledge gap if the answer missed the question's demand), Gemini Marks and Gemini Remark.
- Proof log: a new row with the next number, date, section, score, out of, attempt and question link.
- Remarks: a card with the revision points.
- On the PC: the originals and an evaluation.txt in answers_inbox, done. A copy goes to your Drive folder if set.
- If anything is unclear, the photos go to answers_inbox, needs_review with a REASON.txt. Nothing is ever written to Notion for a set that needs review.
