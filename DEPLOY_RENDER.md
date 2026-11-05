# Put it on Render (free), so it works with your PC off

Render runs the tool as a small website. Telegram sends each of your messages to it straight away. You do not need to know git or programming. Allow about 30 minutes.

Before you start: finish SETUP.md Steps 4, 5 and 6, so you have your Notion key, Gemini key and Telegram token. It also helps to run check_setup.bat on the PC once to be sure the keys work.

---

## Step 1. Make a free GitHub account

1. Go to github.com and click "Sign up". Use your email, choose a password and username, and verify your email.

## Step 2. Make a private repository and upload the files

1. On github.com, click the + at the top right, then "New repository".
2. Name: notion-photo-uploader. Choose **Private**. Click "Create repository".
3. On the next page, click the link "uploading an existing file".
4. Unzip a fresh copy of Notion_Photo_Uploader.zip (its config.json has no keys). Open the folder, select everything inside it (Ctrl+A) and drag it into the GitHub page. This includes the tests folder.
5. Wait for all files to appear, then click "Commit changes".
6. Never upload a config.json that has your keys in it. On Render the keys go into "Environment Variables" instead (Step 4).

## Step 3. Sign up for Render with GitHub

1. Go to render.com and click "Get Started". Choose "GitHub" and allow access.
2. When GitHub asks which repositories Render may see, choose "Only select repositories" and pick notion-photo-uploader.

## Step 4. Create the web service

1. In Render, click "New", then "Web Service".
2. Pick the notion-photo-uploader repository.
3. Fill in:
   - Name: notion-photo-uploader (this becomes part of the web address)
   - Language: Python 3
   - Branch: main
   - Build Command: pip install -r requirements.txt
   - Start Command: gunicorn webhook_server:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT
   - Instance Type: **Free**
4. Under "Environment Variables", click "Add Environment Variable" for each of these:

| Key | Value |
|---|---|
| TELEGRAM_BOT_TOKEN | your Telegram token |
| NOTION_TOKEN | your Notion key (ntn_...) |
| GEMINI_API_KEY | your Gemini key |
| WEBHOOK_SECRET | a password you make up: 20 to 40 letters, numbers, _ or -, no spaces. Example: Avi_upsc_2026_x7Kp9Lm2Qw |
| ALLOWED_USER_IDS | 8308385292 |
| PYTHON_VERSION | 3.13.5 |

5. Click "Deploy Web Service". Wait until the log says "Your service is live" (2 to 5 minutes).
6. At the top you see your address, for example https://notion-photo-uploader.onrender.com. Open it in the browser. You should see a short line starting with {"ok": true. That is the health check.

Shortcut: the Deploy to Render button in README.md fills in all of Steps 3 and 4 for you (FRIEND_SETUP.md walks through it), and makes WEBHOOK_SECRET by itself.

## Step 5. Connect Telegram to Render (once)

The service now does this by itself every time it starts, so you can usually skip this step. Doing it by hand is still fine.

1. If run_telegram.bat is running on your PC, close it. Only one of the two can receive messages.
2. In your browser, open your address followed by /setup/ and your WEBHOOK_SECRET. Example:
   https://notion-photo-uploader.onrender.com/setup/Avi_upsc_2026_x7Kp9Lm2Qw
3. You should see "Webhook set." Any messages that were waiting (up to 24 hours old) now arrive.

## Step 6. Test

1. In Telegram, send /status to your bot. It should list every key as OK.
2. Send the photos of one answer. The bot replies "Got N pages, working..." and, a minute or two later, your marks and evaluation.

---

## Good to know about Render's free plan

- **It sleeps.** After 15 minutes with no messages the server goes to sleep. Your next message wakes it, which takes about a minute. Telegram keeps trying to deliver until it is awake, so nothing is lost. Just wait a little longer for the first reply.
- **It forgets files when it sleeps.** That only matters if the bot asked you to choose 1, 2 or 3. Reply within 15 minutes. If you reply later, it will ask you to send the photos again.
- **750 free hours a month.** One service running all month needs at most 744, so it fits. Do not create a second free service, or both may be paused near the end of the month.
- **Nothing is copied to Google Drive** from Render. Your pages are stored in Notion.
- **Repeats are skipped.** Each photo's Telegram ID is saved as the image caption in Notion, so a photo that is delivered twice is not uploaded twice.

## Switching back to your PC

Telegram allows either Render (webhook) or the PC (polling), never both.

- **To use the PC:** first, in Render, open Environment, add AUTO_WEBHOOK with the value off, and click Save Changes. Otherwise Render takes Telegram back each time it restarts. Then open your address + /unset/ + WEBHOOK_SECRET in the browser (example: https://notion-photo-uploader.onrender.com/unset/Avi_upsc_2026_x7Kp9Lm2Qw), then double-click run_telegram.bat. Or, on the PC, run: python set_webhook.py --delete
- **To go back to Render:** close run_telegram.bat, delete AUTO_WEBHOOK in Render's Environment (or open the /setup/ link again).
- **To check which one is active:** on the PC, run python set_webhook.py --info (it needs webhook_secret in config.json only for setting, not for checking).

## Optional settings

- **ALLOWED_USER_IDS**: who may use the bot, Telegram IDs separated by commas. Send /myid to the bot to see yours.
- **EVALUATE**: off by default (upload-only: photos, answer text, Status Done, Evaluated "Not evaluated"). Set it to on to have Gemini evaluate every answer again.
- **SAVE_ANSWER_TEXT**: on by default. Off stops saving the handwriting as text (the read reply gets shorter).
- **AUTO_WEBHOOK**: off stops the service from connecting Telegram by itself when it starts.
- **GS_TRACKER_DATABASE_ID, TRACKER_DATABASE_ID, PROOF_LOG_DATABASE_ID, REMARKS_DATABASE_ID**: only needed if the bot cannot find a database by its name. The value off switches that database off.

## Changing a key or setting later

In Render, open your service, then "Environment". Edit the value and click "Save Changes". Render restarts the service with the new value. You do not need to touch GitHub.

---

## Backup host: PythonAnywhere (short version)

Use this only if Render stops working for you.

- PythonAnywhere's free plan does not suit this tool: its web apps cannot run the background worker this tool needs, and free accounts can only reach an approved list of websites.
- The paid "Hacker" plan (about 5 US dollars a month) works. Upload the files under "Files", open a "Bash console", run pip install --user -r requirements.txt, fill in config.json there, and create an "Always-on task" with the command:
  python3.13 /home/YOUR_USERNAME/notion-photo-uploader/notion_attach.py --telegram
  This is the same Telegram listener as run_telegram.bat. Delete the Render webhook first (see "Switching back to your PC" above).
