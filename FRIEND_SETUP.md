# Your own answer bot in about 15 minutes

You will make your own Telegram bot, your own Notion trackers, and your own free server on Render. It all stays yours. You do not need to know programming, and you do not need a GitHub account.

Keep a notes app open. You will copy four things into it: a Telegram token, your Telegram ID, a Notion key and a Gemini key.

---

## Step 1. Make your Telegram bot (2 minutes)

1. In Telegram, search for **@BotFather** (it has a blue tick) and open it.
2. Send `/newbot`.
3. Type a name for your bot, for example `Riya Answer Bot`.
4. Type a username that ends in `bot`, for example `riya_answer_bot`.
5. BotFather replies with a long token that looks like `1234567890:AAH...`. Copy it into your notes as **Telegram token**. Keep it secret.

## Step 2. Get your Telegram ID (1 minute)

1. In Telegram, search for **@userinfobot** and press Start.
2. It replies with your **Id**, a number like `5123456789`. Copy it into your notes as **Telegram ID**.

(If you skip this, your bot can tell you later: send it `/myid`.)

## Step 3. Copy the Notion trackers (2 minutes)

1. Make a free account at notion.so if you do not have one.
2. Open the template link below and click **Duplicate** (top right). It copies everything into your own Notion.
   - Answer bot template: TEMPLATE_LINK_HERE
3. Do not rename the databases. The bot finds them by name:
   - **GS PYQs** (GS1 to GS4 and Essay questions)
   - **Geography optional** (only needed if Geography is your optional)
   - **Answers written** (your proof log, optional)
   - **Remarks** (remarks to revise, optional)

   If you only write GS answers, you can delete Geography optional. The bot works with either one or both.

## Step 4. Make a Notion key and connect it (3 minutes)

1. Open **notion.so/profile/integrations** and click **New integration**.
2. Name it `Answer bot`, pick your workspace, type **Internal**, and click Save.
3. Copy the **Internal Integration Secret** (it starts with `ntn_`). Put it in your notes as **Notion key**.
4. Go back to the page you duplicated in Step 3. Click the **•••** menu at the top right, then **Connections** (or "Add connections"), search for `Answer bot`, and confirm.
   Connecting the main page connects every database inside it. If a database sits somewhere else, connect it the same way.

## Step 5. Get a Gemini key (2 minutes)

1. Open **aistudio.google.com** and sign in with Google.
2. Click **Get API key**, then **Create API key**.
3. Copy the key. Put it in your notes as **Gemini key**.

## Step 6. Put the bot on Render (4 minutes, then a 5 minute wait)

1. Open this button link: https://render.com/deploy?repo=https://github.com/ASING7697/answer-bot
2. Sign up for Render (free; Google or email is fine) if it asks.
3. Render shows a form. Fill in:
   - **TELEGRAM_BOT_TOKEN**: your Telegram token
   - **NOTION_TOKEN**: your Notion key
   - **GEMINI_API_KEY**: your Gemini key
   - **ALLOWED_USER_IDS**: your Telegram ID (if you do not have it yet, type `0` and see Step 7)
   - WEBHOOK_SECRET is made for you. Leave it alone.
4. If it asks for a name, keep `answer-bot` or pick your own. Make sure the plan is **Free**.
5. Click **Deploy Blueprint** (or **Apply**). Wait until it says **Live**, usually 3 to 5 minutes.

The bot connects itself to Telegram when it starts. You do not need to do anything for that.

## Step 7. Claim your bot (1 minute, only if you typed 0)

1. In Telegram, open your bot (search its username) and send `/myid`.
2. It replies with your ID. On render.com, open your service, then **Environment**, set **ALLOWED_USER_IDS** to that number, and click **Save Changes**.
3. Wait about 2 minutes while it restarts.

Only the IDs in ALLOWED_USER_IDS can use your bot. Anyone else only gets their own ID back. To let a second person use it, put both IDs separated by a comma, like `5123456789,6987654321`.

## Step 8. Check it (1 minute)

1. Send `/status` to your bot. The first reply can take a minute if the server was asleep.
2. Every key should say OK, and it should list the databases it found, for example "GS PYQs tracker database: connected (found by name)".
3. If a database says "not found", go back to Step 4 and connect it.

## Step 9. Try a real answer

1. Write the question at the top of the first page, as it appears in your tracker.
2. Send the photos of all pages to your bot, then send `done`.
3. In a minute or two you get your marks and the evaluation, and Notion is filled in.

---

## Good to know

- **Free server sleeps.** After 15 minutes with no messages, Render puts it to sleep. Your next message wakes it in about a minute. Nothing is lost.
- **Gemini busy.** If Gemini is busy or out of its free limit, your pages are still attached and the bot evaluates them later by itself. `/retry` does it right away.
- **Commands:** `/status`, `/test`, `/retry`, `/myid`, and `done`.
- **Updates.** Your copy does not change by itself. To get a newer version, open your service on render.com and click **Manual Deploy**, then **Deploy latest commit**.
- **Want your own copy of the code?** Make a GitHub account, open github.com/ASING7697/answer-bot, click **Use this template**, then **Create a new repository**. On Render, use New, then Blueprint, and pick your repository. Your bot then keeps working even if the original repository changes or disappears.

## If something goes wrong

- **The bot does not answer at all.** Open your service on render.com and look at **Logs**. Check that TELEGRAM_BOT_TOKEN is exactly the token from BotFather. Then open your service web address in a browser: it should show a line starting with `{"ok": true`.
- **It only tells you your ID.** Your ID is not in ALLOWED_USER_IDS yet. See Step 7.
- **"No tracker found".** The Notion key cannot see GS PYQs or Geography optional. Do Step 4 again and check the database names were not changed.
- **A Gemini error.** Make a new key at aistudio.google.com and replace GEMINI_API_KEY on Render (Environment, then Save Changes).
- More help: TROUBLESHOOTING.md.
