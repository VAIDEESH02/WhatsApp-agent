# WhatsApp Agent

[![Tests](https://github.com/VAIDEESH02/WhatsApp-agent/actions/workflows/tests.yml/badge.svg)](https://github.com/VAIDEESH02/WhatsApp-agent/actions/workflows/tests.yml)

Put your WhatsApp on cruise control. This project is a small AI assistant that reads incoming WhatsApp messages, works out who sent them, decides whether it's safe to answer, and drafts a reply in **your own voice** for that relationship. You don't text your mother the way you text a client, and neither does the agent.

It has two "brains":

- **The Persona Brain** (`persona/persona.json`) describes how you talk to each kind of person: family, friends, partner, colleagues, and film-industry clients.
- **The History Brain** (a ChromaDB database) remembers how you've actually replied to similar messages in the past.

Each draft also sees the **last few messages of the current chat**: up to 8 from the past 12 hours, including replies you typed on your phone. They're kept in memory only, never saved to disk, and cleared when the WhatsApp client restarts. They help the draft understand follow-ups like "Nee?". The decision on whether a message is safe to answer is still made on the new message alone.

Anything risky, like plans, money, serious topics, group chats, or strangers, is never answered automatically. It waits for you in **Reply Desk**, a local web page where you can approve, edit, or dismiss each draft.

> **Safe by default.** Out of the box, auto-replies are **off** and the WhatsApp client runs in **dry-run** mode, so nothing is sent until you deliberately switch both on. Because the WhatsApp connection uses Baileys, an unofficial WhatsApp Web library, use a spare or secondary number if you can. Unofficial clients can break WhatsApp's terms of service.

For how everything fits together, see [Architecture.md](Architecture.md).

---

## Contents

1. [Prerequisites](#1-prerequisites)
2. [Installation](#2-installation)
3. [Configuration](#3-configuration)
4. [One-time setup: build the History Brain](#4-one-time-setup-build-the-history-brain)
5. [Running the application](#5-running-the-application)
6. [Verification](#6-verification)
7. [Going live safely](#7-going-live-safely)
8. [Common issues](#8-common-issues)
9. [Running tests](#9-running-tests)

---

## 1. Prerequisites

| What | Version | Why you need it |
|---|---|---|
| macOS or Linux | any recent | The commands below use a Unix shell. On Windows, use WSL. |
| **Python** | 3.10 or newer (tested on 3.13.7) | Runs the agent, the bridge, Reply Desk, and the data scripts. |
| **Node.js** | 20 or newer (tested on 24.22.0) | Runs the WhatsApp client. Baileys refuses to run on older versions. |
| **npm** | comes with Node (tested on 11.19.1) | Installs the WhatsApp client's packages. |
| **Git** | any | To clone the repository. |
| **Gemini API key** | free | The AI model that writes replies. Get one at [Google AI Studio](https://aistudio.google.com/apikey). |
| **WhatsApp on a phone** | — | You'll link the agent as a "linked device", like WhatsApp Web. |
| Disk and network | about 2 GB free, internet on first run | The first run downloads a ~1 GB language model for searching your history. |

Check what you have:

```bash
python3 --version
node --version
npm --version
git --version
```

You don't install ChromaDB separately. It comes with the Python packages, and you start it with one command (see [Running the application](#5-running-the-application)).

---

## 2. Installation

**1. Clone the repository and move into it.**

```bash
git clone https://github.com/VAIDEESH02/WhatsApp-agent.git
cd WhatsApp-agent
```

Every command in this guide is run **from this project folder**.

**2. Create a Python virtual environment.** This is a private box of Python packages just for this project, so nothing clashes with the rest of your computer.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

You'll see `(.venv)` at the start of your prompt. Run `source .venv/bin/activate` again in every new terminal you open for this project.

**3. Install the Python packages.**

```bash
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
```

This installs Gemini's SDK (`google-genai`), `python-dotenv`, `chromadb`, `sentence-transformers`, `fastapi`, `pydantic`, `uvicorn` and `pytest`, each pinned to the version the project is tested with.

**4. Install the Node packages.**

```bash
npm install
```

This installs Baileys (pinned to `7.0.0-rc14`), `axios`, `pino`, `dotenv` and `qrcode-terminal`. If npm warns that some packages "have install scripts not yet covered by allowScripts", that's fine. Those scripts are only version checks.

**5. Check that everything installed.**

```bash
python3 -c "import chromadb, fastapi, sentence_transformers, google.genai; print('python deps ok')"
node -e "require('@whiskeysockets/baileys'); console.log('node deps ok')"
```

---

## 3. Configuration

### 3.1 The `.env` file (your API key)

Create a file called `.env` in the project folder with your Gemini key:

```bash
cat > .env <<'EOF'
GEMINI_API_KEY=paste-your-key-here
EOF
```

| Variable | What it's for |
|---|---|
| `GEMINI_API_KEY` | Lets the agent call Gemini (model `gemini-3.5-flash`) to check each message and write drafts. |
| `REPLY_DESK_ALLOWED_HOSTS` | Optional. Your Mac's private Tailscale name (e.g. `my-mac.tail1234.ts.net`), so you can open Reply Desk from your phone. Comma-separate several. See [Using Reply Desk from your phone](#using-reply-desk-from-your-phone). |

`.env` is git-ignored, so your key never ends up on GitHub.

### 3.2 Your WhatsApp chat exports

The History Brain learns from real chats you export from your phone:

1. Open a one-to-one chat in WhatsApp.
2. Tap the contact's name (or the ⋮ menu) and choose **Export chat → Without media**.
3. Save the `.txt` file into a folder inside `Whatsapp chats/`, for example:

```
Whatsapp chats/
├── Chat with family/   WhatsApp Chat - Amma.txt
├── Chat with friends/  WhatsApp Chat - Sarath SSN.txt
├── Chat with GF/       …
└── Business chat/      …
```

The folder names are just for your own tidiness; you can use any. Both iPhone (`[date, time] Name: text`) and Android (`date, time - Name: text`) export formats work.

**Don't export group chats.** The agent never replies in groups, and group conversations don't make good "they said → I replied" examples.

### 3.3 The `config/` folder

| File | What it is | Who writes it |
|---|---|---|
| `config/contacts.md` | Your address book: names and phone numbers, as a Markdown table (format below). Git-ignored. | **You** |
| `config/relationship_map.json` | Every phone number → its category. The live router uses this to decide who's texting. Git-ignored. | Built by a script, then **you** fill in categories |
| `config/contact_relationship_map.json` | Each exported chat (by an anonymous id like `chat-e9b783a5b9`) → its category. Used to file your history. | Built by a script, then **you** fill in categories |
| `config/auto_reply.json` | The auto-reply on/off switch. Not in git: created the first time you flip the switch; while it's missing, auto-reply is off. | Reply Desk (the switch) or you |
| `config/settings.json` | The WhatsApp client's settings: `dry_run` and the random pre-send delay (`min_delay_seconds`, `max_delay_seconds`). Not in git: created the first time you change a setting in Reply Desk. Read fresh for every message, so changes apply without a restart. If it's missing or broken, the safe defaults are used: dry run on, 3–12 s delay. | Reply Desk (**Live sending** switch and **Reply timing**), or you |
| `kill_switch.flag` (repo root) | The emergency stop. While this file exists, the WhatsApp client processes and sends nothing. Git-ignored. | Reply Desk (**🛑 Kill switch** / **Clear kill switch**), or you: `touch kill_switch.flag` to stop, `rm kill_switch.flag` to resume |
| `config/constants.py` | One-word acknowledgements ("ok", "thanks", …) that never need a reply. | Already set |

**Categories.** Every contact and chat gets one of these, matching the relationship sections in `persona/persona.json`:
`family`, `friends`, `girlfriend`, `professional`, `business_freelance`. Anyone not in your map counts as `unknown`, and the agent never replies to unknown senders.

**Format of `config/contacts.md`.** One Markdown table row per contact. Phone numbers need the `+` and country code; a contact can have several numbers. Other columns are ignored.

```markdown
| Name | Phone(s) | Relationship |
|---|---|---|
| Amma | +919444000000 |  |
| Sarath SSN | +919025000000 |  |
| Netaji SSN | +917299000000, +918098000000 |  |
```

### 3.4 Your persona

`persona/persona.json` is the Persona Brain. It ships describing the repository owner's voice (Tanglish with family and friends, plain English with clients). If you're setting this up for yourself, rewrite each relationship's `tone` and `example_replies`, and keep the `hard_rules`. `persona/build_persona.py` can measure your style from an iPhone-format export to help:

```bash
python3 persona/build_persona.py --file "Whatsapp chats/Chat with friends/WhatsApp Chat - Sarath SSN.txt" --name "Vaideesh" --output persona/style_signals_test.json
```

---

## 4. One-time setup: build the History Brain

Do this once, and again whenever you add new chat exports. Throughout, replace `"Vaideesh"` with **your name exactly as it appears in your exports**: it's the sender name on your own messages.

**Step 1: Start ChromaDB** in its own terminal and leave it running. ChromaDB is the database that stores your history so it can be searched by meaning.

```bash
source .venv/bin/activate
chroma run --path ./chroma_data --port 8000
```

**Step 2: Turn your exports into "they said → I replied" pairs.** In a second terminal:

```bash
source .venv/bin/activate
python3 ingestion/parse_export.py --name "Vaideesh"
python3 ingestion/validate_pairs.py
```

The first command prints one line per chat, showing its anonymous id, for example
`chat-e9b783a5b9  Chat with friends/WhatsApp Chat - Sarath SSN.txt: 11 pairs`. Keep that output handy for step 3. The validator should end with `✅ Looks good`.

**Step 3: Tell the agent which category each chat belongs to.**

```bash
python3 ingestion/build_relationship_map.py
```

Open `config/contact_relationship_map.json` and replace every `"REPLACE_ME"` with a category, using the ids from step 2. Then check it:

```bash
python3 ingestion/validate_relationship_map.py
```

You want: `PASS: all N conversation_ids have a relationship.`

**Step 4: Build the phone-number map** used for live messages. First create `config/contacts.md` (see [3.3](#33-the-config-folder)). Then:

```bash
python3 ingestion/build_phone_map.py --name "Vaideesh"
```

New numbers are added to `config/relationship_map.json` as `"REPLACE_ME"`; the script never guesses. Fill in each one with a category, then check:

```bash
python3 ingestion/validate_phone_map.py --name "Vaideesh"
```

You want: `PASS: every number is valid E.164, routes from its JID, and maps to a known relationship.` E.164 just means "full international format", like `919444000000`. Add `--dry-run` to `build_phone_map.py` to preview changes without writing anything.

**Step 5: Load your history into ChromaDB.** ChromaDB must be running (step 1).

```bash
python3 ingestion/embed_to_chroma.py
```

The first run downloads the `paraphrase-multilingual-mpnet-base-v2` model (about 1 GB), so give it a few minutes. It ends with a count per category, for example `friends: 12  (removed 0 stale)`. Re-running is safe: the database always ends up matching your pairs file exactly.

**Step 6: Try a search.**

```bash
python3 ingestion/retrieval_demo.py --relationship friends --query "Free ah iruka? Call pannalama"
```

You'll see your three most similar past exchanges, each with a distance score (lower means closer), and a ✅ sanity line at the end.

---

## 5. Running the application

### The quick way: one command

Pair WhatsApp once first (Terminal 4 below: run it, scan the QR code, press Ctrl+C). After that, start everything with:

```bash
./start.sh
```

It starts all four parts in order: ChromaDB, the bridge, Reply Desk, then the WhatsApp client. It also:

- **restarts a part that crashes**, waiting 5 seconds at first and up to a minute if it keeps failing;
- **keeps your Mac awake** while it runs (it uses macOS's `caffeinate`), so WhatsApp stays connected;
- writes each part's output to `logs/services/<name>.log` (`tail -f logs/services/*.log` to watch);
- **refuses to start** if any of the ports is already in use, so it never fights a copy you started yourself.

Press **Ctrl+C** to stop everything it started. The Mac can still sleep if you close the lid (unless it's connected to an external display and power), and the agent is offline while it sleeps.

### The manual way: four terminals

Start each part **in this order**, from the project folder, with `source .venv/bin/activate` run in each Python terminal.

**Terminal 1: ChromaDB** (the History Brain's database)

```bash
chroma run --path ./chroma_data --port 8000
```

**Terminal 2: the bridge** (the agent's "brain" that the WhatsApp client talks to)

```bash
python3 agent/bridge.py
```

Wait for `Uvicorn running on http://127.0.0.1:5001`.

**Terminal 3: Reply Desk** (your dashboard)

```bash
python3 console/app.py
```

It stays quiet in the terminal. Open **http://127.0.0.1:5002** in your browser. The original, simpler feed page is still at http://127.0.0.1:5002/basic.

**Terminal 4: the WhatsApp client**

```bash
node whatsapp/baileys_client.js
```

The first time, it prints a QR code. On your phone, open **WhatsApp → Settings → Linked devices → Link a device** and scan it. Your login is saved in `auth_info_baileys/` (git-ignored), so later runs connect without a QR code. When you see `[ROUTE] connected to WhatsApp`, you're live.

To stop everything, press **Ctrl+C** in each terminal.

### Managing the servers

Each part listens on its own port on your computer:

| Port | What | Started with |
|---|---|---|
| 8000 | ChromaDB | `chroma run --path ./chroma_data --port 8000` |
| 5001 | The bridge | `python3 agent/bridge.py` |
| 5002 | Reply Desk | `python3 console/app.py` |

**Run each one in its own visible terminal.** Then you can always see it and stop it with Ctrl+C. A copy started in the background, or left running in a terminal you've since closed, keeps holding its port, and the next start fails with `address already in use`.

**See what's running on the three ports:**

```bash
lsof -nP -iTCP -sTCP:LISTEN | grep -E ':(5001|5002|8000) '
```

Each line shows the program and its process number (PID). To check one port, use `lsof -nP -iTCP:5001 -sTCP:LISTEN`.

**Find out what a PID is and how long it's been running:**

```bash
ps -o pid,lstart,etime,command -p <PID>
```

**Stop it.** Press Ctrl+C in the terminal running it if you can. Otherwise:

```bash
kill <PID>
```

Use `kill -9 <PID>` only if it doesn't exit after a few seconds. To stop whatever holds a port without looking up the PID:

```bash
kill $(lsof -t -iTCP:5001 -sTCP:LISTEN)   # the bridge
kill $(lsof -t -iTCP:5002 -sTCP:LISTEN)   # Reply Desk
kill $(lsof -t -iTCP:8000 -sTCP:LISTEN)   # ChromaDB
```

**Stop the agent acting without stopping any server:** press the red **🛑 Kill switch** in Reply Desk, or run `touch kill_switch.flag`. The WhatsApp client then ignores every message and sends nothing until you clear it (the **Clear kill switch** button, or `rm kill_switch.flag`).

### Using Reply Desk from your phone

Reply Desk only answers on your Mac. To use it from your phone or another of **your own** devices, wherever you are, connect them with [Tailscale](https://tailscale.com), a free private network between your devices. There is no public web address and no login page to attack: only devices signed in to your Tailscale account can reach it.

1. **Install Tailscale** on your Mac and your phone, and sign in to the same account on both.
2. **Find your Mac's Tailscale name:** `tailscale status`, or the Tailscale menu on the Mac. It looks like `my-mac.tail1234.ts.net`.
3. **Tell Reply Desk to accept that name.** Add it to `.env`:

   ```bash
   echo 'REPLY_DESK_ALLOWED_HOSTS=my-mac.tail1234.ts.net' >> .env
   ```

   Then restart Reply Desk (or `./start.sh`). Any other name is still refused.
4. **Share only Reply Desk over Tailscale, with HTTPS:**

   ```bash
   tailscale serve --bg http://127.0.0.1:5002
   ```

   The first time, Tailscale may print a link to turn on HTTPS certificates for your account; open it, approve, and run the command again. This setting survives restarts. Check it with `tailscale serve status`, and undo it with `tailscale serve reset`.
5. On your phone, open **https://my-mac.tail1234.ts.net**. Add it to your home screen for one-tap access.

**Never share ports 5001 (the bridge) or 8000 (ChromaDB)**, and don't change the servers to listen on `0.0.0.0`. Only Reply Desk is meant to be reached, and only through `tailscale serve`.

Your Mac has to be on, awake and running the agent for this to work. `./start.sh` keeps it awake while it runs.

---

## 6. Verification

**ChromaDB is up:**

```bash
curl -s http://localhost:8000/api/v2/heartbeat
```

Expected: something like `{"nanosecond heartbeat":1792696667808968000}`.

**The bridge answers.** This sample message comes from a number that isn't in your contacts, so it's ignored straight away, without calling Gemini:

```bash
curl -s -X POST http://127.0.0.1:5001/process \
  -H "Content-Type: application/json" \
  -d '{"jid":"15550000000@s.whatsapp.net","text":"Hi, is this Vaideesh?","message_type":"text","is_forwarded":false,"from_me":false}' \
  | python3 -m json.tool
```

Expected:

```json
{
    "should_reply": false,
    "reply": null,
    "relationship": "unknown",
    "reason": "sender not in allowlist",
    "status": "ignored",
    "needs_approval": false,
    "auto_reply_enabled": false,
    "approval_id": null
}
```

This request is logged like a real message, so it also appears in Reply Desk's live feed.

**Reply Desk is live.** At http://127.0.0.1:5002 the header shows a green dot and "Live · updated just now", refreshing every 2 seconds. The auto-reply switch shows **Off** and the client mode shows **Client: dry run**. Until real messages arrive, the feed says "waiting for first message...".

**The whole pipeline works** (this one does call Gemini):

```bash
python3 agent/batch_test.py
```

It runs 11 sample messages through every safety layer and prints a table. Expect a summary like `11 messages: 7 ignored, 2 need approval, 2 ready`. The last two numbers can shift a little between runs, because the drafts are written fresh by the AI each time.

**The WhatsApp client is working.** When someone texts you, Terminal 4 shows lines like:

```
[ROUTE] 919025877874@s.whatsapp.net | type=text | forwarded=false | text="Free a?"
[DECISION] needs_approval | relationship=friends | … | auto-reply switch is off
[REPLY] held for approval, not sending: Paakalam da
```

The same message appears in Reply Desk within 2 seconds.

---

## 7. Going live safely

Two independent switches stand between a draft and WhatsApp, and **both start off**. A third, the kill switch, stops everything at once.

| Switch | Where | What it does |
|---|---|---|
| **Auto-reply** | Reply Desk header (stored in `config/auto_reply.json`) | While off, every draft waits for your approval in Reply Desk. |
| **Dry run** | `"dry_run": true` in `config/settings.json` | While on, the client only logs what it *would* send, including replies you approve. |
| **Kill switch** | a file named `kill_switch.flag` in the project folder | While the file exists, the client doesn't even pass messages to the agent, and sends nothing. |

Just before every send, the WhatsApp client also re-checks the sender against `config/relationship_map.json` on its own. If the number isn't mapped to a real category, it logs `[BLOCKED] failed independent allowlist check` and doesn't send, even if the agent said yes.

Even with both on, the agent never auto-replies to:
- group chats or unknown numbers;
- money or serious topics (these are simply left for you);
- anything that asks you to commit to a plan, call, time, or invite (held for your approval);
- any draft that agrees to something ("varen", "sure", "ok da", "done"…), names a time, or has a blank to fill in (also held).

**To go live:**

1. In Reply Desk, flip **Live sending** on and confirm. This sets `"dry_run": false` in `config/settings.json`, and it takes effect on the next message, with no restart needed.
2. Flip **Auto-reply** on and confirm.

The same controls are in the sidebar of the simpler page at http://127.0.0.1:5002/basic, along with the reply-delay settings.

To stop instantly, turn **Auto-reply** off (automatic replies stop), or press the red **🛑 Kill switch** button (everything stops, approved replies included). A red banner stays at the top of the page until you press **Clear kill switch**. From a terminal, the same kill switch is:

```bash
touch kill_switch.flag
```

Delete it to resume:

```bash
rm kill_switch.flag
```

---

## 8. Common issues

**`GEMINI_API_KEY is missing from .env`**
The `.env` file is missing, empty, or not in the project folder. Recreate it as in [3.1](#31-the-env-file-your-api-key).

**`Could not connect to ChromaDB at localhost:8000` / `Could not connect to a Chroma server`**
ChromaDB isn't running. Start it in its own terminal: `chroma run --path ./chroma_data --port 8000`.

**A script sits silently for minutes at startup (bridge, `embed_to_chroma.py`, `retrieval_demo.py`)**
Each of these loads the language model when it starts, and on every start it first checks Hugging Face for updates. On some networks that check stalls. Once the model has downloaded at least once, tell it to use the copy on your disk:

```bash
export HF_HUB_OFFLINE=1
```

Run that in the terminal before starting the script. Loading then takes about 10 seconds.

**`[Errno 48] Address already in use` when starting the bridge or Reply Desk**
Another copy is already running on that port (5001 for the bridge, 5002 for Reply Desk), often one started in the background or left in a closed terminal. Find and stop it:

```bash
lsof -nP -iTCP:5001 -sTCP:LISTEN
kill <the PID from the output>
```

See [Managing the servers](#managing-the-servers) for more.

**`parse_export.py` warns "produced zero pairs"**
The `--name` you passed doesn't match how your own messages are labelled in that export. Open the `.txt` file and copy your name exactly as it appears before the colon.

**`validate_phone_map.py` or `validate_relationship_map.py` says FAIL**
Usually a `"REPLACE_ME"` is still in the map, or a category is misspelled (`friend` instead of `friends`). The validator lists each problem; fix those lines and run it again.

**`[DECISION] bridge error, not replying: connect ECONNREFUSED 127.0.0.1:5001`**
The WhatsApp client can't reach the bridge. Start Terminal 2 (`python3 agent/bridge.py`) before Terminal 4.

**No QR code appears, or the client says `logged out`**
Delete the saved login and pair again:

```bash
rm -rf auth_info_baileys
node whatsapp/baileys_client.js
```

**`This package requires Node.js 20+`**
Your Node version is too old. Install Node 20 or newer from [nodejs.org](https://nodejs.org) and run `npm install` again.

**`ModuleNotFoundError: No module named 'agent'` (or `ingestion`, `config`)**
Run commands from the project folder, not from inside a subfolder, and make sure the virtual environment is active.

**Reply Desk says "waiting for first message..." and nothing changes**
That's normal until a real message reaches the bridge. Check that Terminal 4 says `[ROUTE] connected to WhatsApp`, then ask someone in your contacts to send you a message.

**Gemini returns `404 NOT_FOUND … model is no longer available`**
Google retires model versions from time to time. Change `MODEL` in `agent/gemini.py` to a current Flash model listed in Google AI Studio.

---

## 9. Running tests

Run these from the project folder. ChromaDB must be running (Terminal 1), because some agent modules connect to it as they load, and the language model must have been downloaded once (step 5 of the setup). If pytest sits silently at the start, run `export HF_HUB_OFFLINE=1` first (see [Common issues](#8-common-issues)).

```bash
python3 -m pytest                            # all Python tests: agent/, console/ and config/
node --test whatsapp/baileys_client.test.js  # the WhatsApp client
```

Expected: `248 passed` for Python and `pass 37` for Node. Without an API key or a relationship map, a few Python tests show as skipped instead.

Almost every test swaps Gemini, WhatsApp, and your private files for stand-ins, so it runs offline and costs nothing. Two kinds are marked, because they need something real:

| Marker | Needs | On your machine | In CI |
|---|---|---|---|
| `live` | Your private `config/relationship_map.json` | Runs (skips itself if the file is missing) | Deselected with `-m "not live"` |
| `gemini` | The real Gemini API: 3 small calls using `GEMINI_API_KEY` | Runs when the key is in `.env` or your shell, skips otherwise | Runs in its own job with the `GEMINI_API_KEY` secret. Rate limits and server errors are retried twice. |

```bash
python3 -m pytest -m "not gemini"               # skip the real Gemini calls (offline, free)
python3 -m pytest -m gemini -v                  # only the Gemini smoke tests
python3 -m pytest -m "not live and not gemini"  # exactly what the CI Python job runs
```

The key is read from the `GEMINI_API_KEY` environment variable (in `agent/gemini.py`). On your machine `.env` fills it in; in CI the repository secret does. The smoke tests use the agent's own `MODEL`, so a retired model fails CI before it breaks your replies.

### Continuous integration

`.github/workflows/tests.yml` runs on every push to `main`. You can also start it by hand: **Actions → Tests → Run workflow**. It has three independent jobs, each stopped after 15 minutes:

| Job | What it does |
|---|---|
| **Python tests** | Python 3.13 with the pinned packages and CPU-only torch, the embedding model from the cache, and a throwaway ChromaDB in a temporary folder. Runs everything except `live` and `gemini` tests. |
| **Gemini smoke tests** | The same setup, plus the `GEMINI_API_KEY` secret, which only this step receives. A Gemini outage or rate limit fails this job only. |
| **Node tests** | Node 24 and `npm ci` with the npm cache, then the WhatsApp client tests. |

The two Python jobs share their setup steps, in `.github/actions/python-env/action.yml`. The badge at the top of this page shows the latest run. It turns red if any job fails, the Gemini one included.

**One-time setup:** on GitHub, open **Settings → Secrets and variables → Actions → New repository secret**. Name it `GEMINI_API_KEY` and paste your key.

None of the Node tests needs a live WhatsApp session. If you add one that does, skip it in CI the same way: `test('…', { skip: process.env.CI && 'needs a live WhatsApp session' }, () => { … })`.

---

## Where things live

| Folder | What's inside |
|---|---|
| `agent/` | The live "thinking": router, decision engine, reply generator, draft check, approval queue, and the bridge server. |
| `ingestion/` | One-time and occasional scripts that turn exports into a searchable history. |
| `persona/` | The Persona Brain (`persona.json`) and style-measuring tools. |
| `whatsapp/` | The Node.js WhatsApp client. |
| `console/`, `frontend/` | Reply Desk: its server and its web page. |
| `config/` | Maps, switches, and your contacts. |
| `Whatsapp chats/` | Your chat exports. |
| `data/`, `chroma_data/`, `logs/` | Generated files: pairs, the approval queue, the database, decision logs. All git-ignored. |

[Architecture.md](Architecture.md) explains what each part does and how a message travels through them.
