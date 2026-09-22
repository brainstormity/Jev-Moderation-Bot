# Jev Moderation Bot

A Discord moderation bot built with Python and TypeSafe AI (Jev System One). It filters spam and scam links in real time, escalates offenses automatically, and lets moderators profile members based on their message history.

## What it does

- **Real-time link and spam moderation**: Analyzes messages as they arrive. If a message is flagged as spam or a scam link, it gets deleted immediately with a reason logged to Discord's server audit log.
- **Progressive discipline**:
  - 1st offense: Warning DM
  - 2nd offense: Final warning DM
  - 3rd offense: 10-minute timeout (customizable)
  - 4th+ offenses: 1-hour timeout (customizable)
- **User profiling (`/profile`)**: Analyzes a user's recent messages (10 to 100) and scores them across several metrics: scam risk, spamminess, noobness, toxicity, and helpfulness. Assigns a persona (e.g. beginner, regular, spammer, troll) and suggests next steps.
- **Rolling message cache & channel fallback**:
  - Passively logs incoming chat messages into a local SQLite database with timestamps.
  - If a member is new or has no cached history, moderators can pick a channel from a dropdown to fetch recent messages on the fly. All scanned messages are cached so subsequent lookups are instant.
- **Mod logs with 1-click actions**: Posts alerts to a designated `#mod-log` channel with buttons to pardon false positives or ban the user.
- **Dynamic false-flag learning**: When an admin pardons a message, that message is saved as a safe precedent and included in future AI checks for that server, preventing repeated false alarms.

## Commands

| Command | Arguments | Permission | Description |
| :--- | :--- | :--- | :--- |
| `/profile` | `user`, `[message_count]`, `[channel]` | Moderate Members | Builds an AI profile for a member scoring scam risk, spam, noobness, and toxicity. |
| `/user-offenses` | `user` | Moderate Members | Lists a member's past moderation infractions and resolution status. |
| `/set-mod-log` | `channel` | Administrator | Sets the channel where moderation alerts and action buttons are posted. |
| `/unset-mod-log` | None | Administrator | Unsets the alert channel (actions still write to the Discord audit log). |
| `/set-timeouts` | `first_offense_mins`, `subsequent_offense_mins` | Administrator | Configures timeout durations for the 3rd and 4th+ offenses. |
| `/set-thresholds` | `tier1`, `tier2` | Administrator | Configures AI confidence sensitivity thresholds. |
| `/pardon` | `user` | Administrator | Manually lifts a timeout, pardons the latest offense, and saves it as safe memory. |
| `/export-feedback`| `file_format` (json/csv) | Administrator | Exports flagged messages and pardon history for offline review. |
| `/mod-config` | None | Administrator | Shows current server settings and active false-flag precedent count. |
| `/help` | None | Moderate Members | Displays an ephemeral reference guide of all available commands. |

You can also right-click any user in Discord -> **Apps** -> **Generate AI Profile** to profile them directly.

## Setup

### 1. Requirements
- Python 3.10 or newer
- Discord bot token ([Discord Developer Portal](https://discord.com/developers/applications))
- TypeSafe AI API key ([TypeSafe Console](https://console.typesafe.ai))

### 2. Install dependencies
```bash
git clone https://github.com/your-org/jev-moderation-bot.git
cd jev-moderation-bot

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
```

### 3. Environment variables
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Set your tokens in `.env`:
```env
DISCORD_TOKEN=your_discord_bot_token
TYPESAFE_API_KEY=your_typesafe_api_key
DATABASE_PATH=bot_data.db
COMMAND_PREFIX=!
```

### 4. Discord Bot Permissions
In the Discord Developer Portal, enable these **Privileged Gateway Intents** under the **Bot** tab:
- **Message Content Intent**
- **Server Members Intent**

When inviting the bot, grant:
- Manage Messages
- Moderate Members (Timeout)
- Ban Members
- View Channels
- Send Messages
- Embed Links
- Read Message History

### 5. Start the bot
```bash
python3 main.py
```

The bot will automatically initialize SQLite database tables on startup.

### 6. Syncing Commands (Owner Only)
To avoid hitting Discord rate limits on every bot restart, application commands are **not** synced automatically on startup. Whenever you first set up the bot, add new commands, or update existing ones, you must manually sync the application command tree.

You can run the owner prefix command either by **DMing the bot directly** or running it in a server channel:

```text
!sync
```

#### Sync Options
- `!sync` — Syncs all slash commands globally (recommended for production deployment).
- `!sync ~` — Syncs slash commands specifically to the current server (instant, skips Discord's global propagation delay).
- `!sync *` — Copies global application commands to the current server and syncs.
- `!sync ^` — Clears all commands from the current server.
- `!sync <guild_id_1> <guild_id_2>` — Syncs specific server IDs (e.g. `!sync 1234567890 9876543210`).

## How User Profiling Works

When you run `/profile @member`:
1. The bot checks SQLite for that user's recent messages (captured automatically by `on_message`).
2. If the user hasn't talked since the bot was added or has no cached history, you'll see a channel selector dropdown. Pick a channel where they were active.
3. The bot reads recent history in that channel until it reaches your requested message count (stopping early to avoid unnecessary API calls). All messages scanned during that pass are cached for future lookups.
4. It sends the message sample and prior infraction history to TypeSafe AI to compute scores:
   - **Scam / Threat**: Phishing, malicious links, wallet/crypto solicitations.
   - **Spam / Promo**: Repetitive links, unsolicited ads, copy-paste flooding.
   - **Noobness**: Basic questions, confusion about server rules or Discord basics.
   - **Toxicity**: Hostility, insults, argumentative behavior.
   - **Helpfulness**: Constructive answers, community guidance.
5. The response includes an archetype summary and action buttons to view the exact messages sampled or scan another channel.

## How False-Flag Memory Works

When an admin clicks **Pardon (False Flag)** in the `#mod-log` channel:
1. The user's timeout is lifted and the offense is marked as pardoned in SQLite.
2. The message text is stored in `moderation_feedback`.
3. On future message evaluations in that server, recent pardoned messages are fed directly into the model's prompt as verified legitimate examples.
4. The bot immediately learns server-specific slang, links, and jokes without retraining.

## Fail-Open Architecture & Outage Monitoring

To protect communities from accidental mass deletions or bot crashes during upstream API outages, the moderation engine **fails open**:
- If a TypeSafe AI evaluation encounters a connection error, invalid API key, rate limit, or unexpected exception, the message is allowed through unmoderated rather than deleted.
- **Outage Warning**: If evaluations fail for 3 consecutive messages in a server, the bot dispatches a one-time warning alert to the configured `#mod-log` channel, notifying administrators that automated moderation is temporarily offline.
- **Deduplication**: Additional message failures while in the outage state log warnings internally but do not spam `#mod-log`.
- **Recovery Notification**: Once a message evaluation succeeds after an outage, the bot resets the failure counter and posts an operational recovery notification to `#mod-log` confirming that automated moderation has resumed.

## Running Tests

Run the test suite with pytest:
```bash
python3 -m pytest -v
```

Tests cover SQLite migrations, message caching, channel scraping logic, escalation ladders, admin view permissions, and slash commands.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
