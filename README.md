# answer-bot

A Telegram bot for UPSC answer writing. Send photos of a handwritten answer, and it finds the question in your Notion trackers ("GS PYQs" and/or "Geography optional"), attaches the pages under "My answer", saves the handwriting as text, and sets Status to Done and Evaluated to "Not evaluated".

By default it does NOT evaluate (upload-only). Gemini only reads each batch once: to split several answers (even
shuffled pages), put the pages in reading order, find each question and copy the handwriting. You then evaluate
from Notion with any AI (Grok, Gemini, ChatGPT can use the saved answer text). To have Gemini evaluate and write
marks and remarks again, set `EVALUATE` to `on` on Render (or `"evaluate": true` in config.json).

Switch from Telegram, no Render change needed: `/evaluate` (attach and evaluate) or `/attach` (also `/upload`,
attach only) lasts until you change it or the free server restarts, then EVALUATE decides again. For one set only,
send the word `evaluate` or `attach` just before the photos, or as the photo caption. `/status` shows the mode.

Everything it uses is free: Telegram, Notion, Gemini (Google AI Studio) and Render.

## Get your own copy (about 15 minutes)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/ASING7697/answer-bot)

Follow **FRIEND_SETUP.md** step by step. You get your own bot, your own Notion and your own Gemini key. Nothing is shared with anyone else.

## Other guides

- **START_HERE.md**: what the bot does and which file to read.
- **FRIEND_SETUP.md**: set up your own copy on Render, from a phone or a computer.
- **DEPLOY_RENDER.md**: the longer manual Render guide.
- **SETUP.md**: run it on a Windows PC instead.
- **TROUBLESHOOTING.md**: when something stops working.
- **AVI_SHARING.md**: for the owner of this repository, how to share it.
