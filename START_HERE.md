# Start here

This folder is your own answer-checking tool. It runs without any paid service.

You send photos of a handwritten answer to your Telegram bot. The tool then:

1. reads the question written at the top of your first page,
2. finds that question in your Notion "Geography optional" tracker,
3. attaches the pages in the right order,
4. has Gemini evaluate the answer like a strict UPSC examiner (out of 10, 15 or 20),
5. fills Notion: the "Evaluation by Gemini" section, Gemini Marks, Gemini Remark, Status, the proof log row with the score, and a Remarks card,
6. replies on Telegram with the question, your marks, the evaluation and the remarks.

## Which file to read

1. **SETUP.md**: read this first. It gets your three free keys (Notion, Gemini, Telegram) and runs the tool on your Windows PC. Do this even if you plan to use Render, because it is the easiest way to check that the keys work.
2. **DEPLOY_RENDER.md**: read this next if you want it to work while your PC is off. It puts the same tool on Render's free plan. This is the recommended main home.
3. **TROUBLESHOOTING.md**: read this when something stops working. It covers expired keys, a sleeping server, a retired Gemini model, renamed Notion columns and more.

## The two ways to run it (use only one at a time)

- **Render (main):** works 24 hours a day, even with your PC off. See DEPLOY_RENDER.md.
- **Your PC (backup):** double-click run_telegram.bat. It works while the PC is on.

Telegram can deliver your messages to only one of them at a time. TROUBLESHOOTING.md explains how to switch.

## Telegram commands

- /status: checks every key and shows the last run.
- /test: a dry run. It checks everything and, if you have just sent photos, shows which question it would attach them to. It writes nothing.
- done: finishes an answer set straight away, without waiting a minute.

## What is in this folder

| File | What it is |
|---|---|
| notion_attach.py | The main program |
| gemini_eval.py | Talks to Gemini (reading and evaluation) |
| telegram_mode.py | Talks to your Telegram bot |
| webhook_server.py | The web server version for Render |
| set_webhook.py | Switches Telegram between Render and your PC |
| config.json | Your settings and keys (PC only) |
| requirements.txt | The exact helper packages, with fixed versions |
| render.yaml | Settings for Render |
| run_telegram.bat | PC: listen to Telegram (recommended PC mode) |
| run.bat | PC: process photos in the answers_inbox folder once |
| run_watch.bat | PC: keep watching the answers_inbox folder |
| check_setup.bat | PC: test all keys |
| install.bat | PC: install the helper packages |
| start_on_boot.bat, remove_from_boot.bat | PC: start the Telegram listener with Windows, or stop doing that |
| tests | Automatic checks of the program (you never need to run them) |
