English | [한국어](https://github.com/Noah-TaeHwan/blackclip-guardrail/blob/main/README.ko.md)

# BlackClip Guardrail

If you run a trading bot on a strategy you built with AI, Guardrail watches the logs the bot already writes, without an exchange API key, and messages you on Telegram when they stop coming in. It runs locally on your Mac. It also checks a strategy before you put real money on it and reconciles live fills against the backtest.

BlackClip is a set of tools individual investors use to check a decision before they act on it.

- [Research](https://research.blackclip.app): Can I trust this investment idea?
- [Guardrail](https://guardrail.blackclip.app): Is this strategy ready to run with real money?
- [Hedge](https://hedge.blackclip.app): How much of the risk I hold today can I realistically cut?

Brand site: [blackclip.app](https://blackclip.app)

![Sample dashboard home. Two live strategies with their status and the gap against the same-period backtest, plus two stopped strategies](https://raw.githubusercontent.com/Noah-TaeHwan/blackclip-guardrail/main/assets/readme-demo.png)

The [sample dashboard](https://guardrail.blackclip.app/demo/) at 1440 px. It's a read-only page drawn from four made-up strategies, so you can look around before installing anything. The screens, commands, and Telegram messages are in Korean only for now.

## What it does

- It watches your bot without any keys. It reads the log file your running bot already writes, and if nothing new shows up for longer than the limit you set (say, 90 minutes), it sends "봇이 안 보여요" (Bot not seen) on Telegram. When entries come back, it sends "봇이 다시 보여요" (Bot seen again).
- Before a strategy goes live, it checks reproducibility, look-ahead bias, trading costs, how many variants you tried, and market regimes, then sets guardrails for that strategy: a normal range and warning lines.
- If you choose to, it reconciles against your real account. It pulls your fills with a read-only API key, compares them every day with the backtest over the same period, and tells you on Telegram when they drift apart.
- If your bot was already running, it starts with a "첫 회고" (first retro) built from the last three months of fills.

## What it doesn't do

- It doesn't place orders. It only warns you and never stops your bot. Stopping is your call.
- It doesn't change your bot's code. It only reads the logs the bot writes.
- It won't store a key that has trading or withdrawal permissions turned on.
- It isn't investment advice. Check results, guardrails, and warnings are reference numbers and don't guarantee returns.

## Requirements

- macOS (scheduled jobs run on launchd)
- [uv](https://docs.astral.sh/uv/) (uv fetches Python 3.11 or later for you)
- [Claude Code](https://claude.com/claude-code), to write the adapter that turns your bot's logs into events and the file that links your strategy. It can also run the other commands for you and explain the output.
- A Telegram account, for alerts
- A Binance **read-only** API key (spot and futures), only if you want to reconcile against your real account

## Install

The engine (the `blackclip` command):

    uv tool install blackclip-guardrail
    blackclip --version

If your shell can't find `blackclip`, run `uv tool update-shell` and open a new terminal.

The Claude Code plugin (one skill and four hooks). Open Claude Code in your strategy repo (Claude Code 2.1.275 or later) and run:

    /plugin install blackclip-guardrail --marketplace https://github.com/Noah-TaeHwan/blackclip-guardrail.git

From a terminal, the same install looks like this (Claude Code 2.1.292 or later):

    claude plugin install blackclip-guardrail --marketplace https://github.com/Noah-TaeHwan/blackclip-guardrail.git --scope project

With `--scope project` the plugin only runs in that repo. With the default (user) scope the hooks run in every folder, and they skip the first-setup prompt in folders that don't look like a quant repo.

## First 10 minutes: attach to a running bot without keys

Open Claude Code in your strategy repo and ask it to watch your bot ("봇 지켜봐 줘"). It works through these steps:

1. Find the bot and its log: `blackclip scan .` reads through the folder without running anything, then Claude Code reads the bot code to pick the file the bot appends to (a log, jsonl, or csv). If it can't tell which file belongs to the bot, it asks you.
2. Adapter: Claude Code writes `.blackclip/strategies/<strategy>/adapter.py`, which turns one log line into a standard event. The engine runs this file in a separate process with no keys and no environment variables.
3. Check the events

       blackclip events <strategy>

   It shows which event types are coming in (decisions, fills, positions, balances), lines that don't match the format, and when the last entry was written.

4. Set the limit: Claude Code looks at how often your bot usually writes and suggests a number of minutes (90 minutes for a bot that decides once an hour, for example). Once you pick one:

       blackclip follow <strategy> --quiet-minutes 90

5. Schedule and Telegram

       blackclip schedule
       blackclip connect --open --telegram
       blackclip connect --telegram

   `schedule` sets up a check every 5 minutes. Create the Telegram bot with @BotFather, then send it the number the last command prints to link the chat. Checks pause while your Mac is asleep.

## Reconcile against your real account (read-only key, 30 minutes)

Use this when you want to compare real fills against the backtest over the same period. You need a Binance read-only API key. Run these in your strategy repo. You can also ask Claude Code to set things up ("셋업해 줘"), and it follows the next steps `blackclip setup` reports, one at a time.

1. See where you are

       blackclip scan .
       blackclip setup

   `setup` tells you the one next step. It only reads the folder and doesn't run any code.

2. Add the read-only key: in Binance API Management, create a key with only read access turned on, then

       blackclip connect --open

   A page opens in your browser that only this computer can reach. The key goes into your Mac's Keychain.

3. Pull fills

       blackclip fetch --symbols BTCUSDT --since "$(date -u -v-90d +%Y-%m-%dT00:00:00Z)"

   `--since` is where the first pull starts. The first retro covers the last three months, hence 90 days back (`date -v-90d` is the macOS syntax). Fills pile up in `.blackclip/fills/`.

4. First retro

       mkdir -p .blackclip/strategies/<strategy>
       blackclip retro --fills .blackclip/fills/binance-main/spot/fills.csv \
         --backtest <backtest trades CSV> --capital <starting live balance> \
         --live-start <live start, ISO time> --days 90 \
         --end "$(date -u +%Y-%m-%dT00:00:00Z)" \
         --out .blackclip/strategies/<strategy>/retro.md

   This covers the 90 days up to midnight UTC today. Without `--end`, it runs through the day after the last fill. For futures, change `spot` to `futures` in the path and add `--funding .blackclip/fills/binance-main/futures/funding.csv`.

   The CSV formats and a sample result are in [`examples/retro/`](https://github.com/Noah-TaeHwan/blackclip-guardrail/tree/main/examples/retro) (in Korean). Execution alerts (missed orders and fills that came in at a worse price) are off by default. Turn them on after you've read your first retro:

       blackclip notify --execution-alerts on

5. Link Telegram: create a bot with @BotFather on Telegram and get its token, then

       blackclip connect --open --telegram
       blackclip connect --telegram

   Send your bot the number the second command prints to link the chat.

6. Strategy link file: ask Claude Code to link the strategy ("이 전략 연결해 줘") and it writes `.blackclip/strategies/<strategy>/link.py`. The engine imports this file in a separate process (top-level code runs too) and calls `load_prices()` to read prices and `signals()` for the signal function.

7. Check, guardrails, going live, daily alerts

       blackclip check <strategy>
       blackclip save <strategy>
       blackclip live <strategy>
       blackclip schedule

   `schedule` registers a daily reconcile (09:10 in your Mac's local time by default; change it with `--at HH:MM`) and an hourly watch as macOS scheduled jobs. If your Mac is asleep at that time, the job runs once when it wakes. Remove them with `blackclip schedule --remove`.

## Dashboard

    blackclip dashboard

It opens in your browser on this computer and shows your strategy list, guardrails, and retros. Add your key and Telegram token under "연결" (Connections). Each run creates a new access token. Stop it with Ctrl+C. The default port is 8765.

## How it works

- The numbers come from Python calculations in the engine, which doesn't call any AI model. The tests for those calculations are in [`tests/`](https://github.com/Noah-TaeHwan/blackclip-guardrail/tree/main/tests). Claude Code's part is writing the adapter and the strategy link file, running commands for you, and explaining the output.
- Everything runs on your Mac. Keys, fills, and strategy code stay on it, and the pages only open on this computer (127.0.0.1).
- Your Binance key and Telegram token go into the macOS Keychain. Only when the `security` command can't be found does it fall back to a file only you can read (`~/.config/blackclip/secrets.json`).
- Fills, retros, and daily reconcile history live in `.blackclip/` inside your repo. Only strategy records (checks, guardrails, history, link code) go into Git; `.blackclip/.gitignore` keeps fills, connection state, and run history out.
- The only outbound requests go to the Binance API (account reads and public candles), the Telegram Bot API, and any health check URL you add yourself.

## Development

You need macOS and uv. Run these from the repo folder.

| Task | Command |
|---|---|
| Install dependencies | `uv sync` |
| Run the tests | `uv run pytest` |
| Build and check the package | `uv build`, `uvx twine check --strict dist/*` |
| Build the sample demo (runs its tests first) | `demo/build.sh <site folder>` |
| Rewrite the dashboard golden pages | `uv run python scripts/update_golden.py` |

| Path | What's there |
|---|---|
| `src/blackclip/` | The engine (the `blackclip` command) |
| `plugin/` | Claude Code plugin (skill and hooks) |
| `.claude-plugin/marketplace.json` | Plugin marketplace |
| `demo/` | Static build of the sample demo |
| `examples/retro/` | Retro input CSV formats and a sample result |
| `tests/` | Tests |

What changed in each version is in [`CHANGELOG.md`](https://github.com/Noah-TaeHwan/blackclip-guardrail/blob/main/CHANGELOG.md) (in Korean).

## Investment notice

- This tool isn't investment advice. Check results, guardrails, and warnings are reference numbers and don't guarantee returns.
- You're responsible for your investment decisions and their outcomes, including losses.
- It only warns you. It doesn't place orders or stop your bot. Stopping is your call.
- It only accepts read-only keys and won't store a key with trading or withdrawal permissions turned on.
- Backtests and live trading can differ, and if the exchange responds late or with wrong data, results can be wrong too.
- BlackClip Guardrail isn't affiliated with or endorsed by Binance, Telegram, or Anthropic.

## License

Source available under FSL-1.1-ALv2. Each version converts to Apache-2.0 two years after its release.
