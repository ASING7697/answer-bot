# Sharing the bot with friends (for Avi)

Your own bot keeps working exactly as before. You do not need to change anything on your Render service.

Each friend gets their own bot, their own Notion, their own Gemini key and their own free Render service. They never see your tokens, your Notion or your answers, and their usage does not count against your Gemini limit.

## What you do once (about 20 minutes)

1. **The GitHub repository is already a template.** Friends can click "Use this template" if they want their own copy of the code. They can also skip GitHub completely and use the Deploy to Render button.

2. **Make one clean Notion template page.** Friends need empty copies of your databases, without your photos, marks or remarks.
   1. In Notion, make a new page called `Answer bot template`.
   2. For **GS PYQs** and **Geography optional**: open the database, click **•••**, then **Export**, choose **Markdown & CSV**, and turn **off** "Include subpages". This gives you the questions only, without the photos and evaluations inside each row.
   3. Inside the template page, type `/` and choose **Import**, then **CSV**, and import each file. Name the new databases exactly `GS PYQs` and `Geography optional`.
   4. In the copies, empty the columns that hold your results: Gemini Marks, Gemini Remark, GPT and Grok columns, Files & media and Date. Set Status back to the starting value. Check the column types match yours (Year, Marks, Word Limit and Gemini Marks as numbers, Paper as a select, Status as a status).
   5. Add two empty databases on the same page: **Answers written** (columns: Question as the title, Tracker question as a relation to Geography optional, Score, Out of, Attempt, Date sent, Section) and **Remarks** (columns: Name as the title, Question as a relation to Geography optional, Score, Section, Date). These two are optional; the bot works without them.
   6. Click **Share**, then **Publish**, and switch on **Allow duplicate as template**. Copy the link.
   7. In FRIEND_SETUP.md on GitHub, replace `TEMPLATE_LINK_HERE` with that link (open the file, click the pencil, paste, then Commit changes).

   Keep the database names as they are. The bot finds a friend's databases by these names: GS PYQs, Geography optional, Answers written, Remarks. Emoji in front of the name are fine.

3. **Send your friend the link** to the repository and tell them to follow FRIEND_SETUP.md. That is all. You do not need to add their ID anywhere: they put their own Telegram ID into their own Render service.

## Never share

- Your TELEGRAM_BOT_TOKEN, NOTION_TOKEN, GEMINI_API_KEY or WEBHOOK_SECRET.
- Your Render account or your bot. Each friend must make their own bot with BotFather.

## Good to know

- Your pushes to GitHub do not change your friends' bots. Their copies update only when they click Manual Deploy on Render.
- If you delete or make the repository private, friends who used the Deploy button keep running, but cannot update or redeploy. Friends who clicked "Use this template" are not affected.
- Nothing here depends on Grok Bot. Your bot and your friends' bots run on Render, Notion, Gemini and Telegram only.
