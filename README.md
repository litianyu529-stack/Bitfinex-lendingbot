# Bitfinex Lending Bot

This is a Python 3.14-compatible Bitfinex margin funding bot based on the old
MikaLendingBot strategy. It no longer uses Poloniex APIs.

The bot is safe by default: it runs in dry-run mode unless you pass `--live`.
Dry-run mode never calls Bitfinex write endpoints.

## Features

- Bitfinex-only REST v2 funding bot.
- Local Chinese web dashboard at `http://127.0.0.1:8000/lendingbot.html`.
- Dry-run by default; live trading requires an explicit `--live` flag or the dashboard live confirmation.
- Smart market strategy that follows the funding book and splits offers into fast-fill, balanced, and higher-rate buckets.
- Stale-offer repricing: old active offers can be canceled and replaced at current market-aware rates.
- Realized earnings panel based on Bitfinex funding wallet ledger entries.
- No withdrawal API implementation.

## Setup

1. Copy the example config:

   ```powershell
   Copy-Item default.cfg.example default.cfg
   ```

2. Edit `default.cfg`:

   ```ini
   [BITFINEX]
   apikey = your_key
   secret = your_secret
   currencies = USD,UST
   ```

   You can also provide credentials with environment variables:

   ```powershell
   $env:BITFINEX_API_KEY="your_key"
   $env:BITFINEX_API_SECRET="your_secret"
   ```

3. Use a Bitfinex API key with the minimum required permissions:

   - Wallet read
   - Funding read/write
   - No withdrawal permission

## Run

Dry-run one cycle:

```powershell
python lendingbot.py --once --dryrun
```

Dry-run with JSON status output:

```powershell
python lendingbot.py --once --dryrun --json www/botlog.json --jsonsize 200
```

Run the local dashboard. The bot will not start until you press the web Start button:

```powershell
python lendingbot.py --dashboard
```

Then open:

```text
http://127.0.0.1:8000/lendingbot.html
```

The dashboard Start button starts a managed bot process using the selected
DRY-RUN or LIVE mode. The Stop button only stops the process that was started
from this dashboard.

On Windows, you can also run:

```powershell
.\启动自动放贷控制台.cmd
```

Live mode:

```powershell
python lendingbot.py --once --live
```

Run one live cycle first and check the Bitfinex account before running it
continuously.

## Strategy

The bot reads the public Bitfinex funding book for each configured currency,
splits available funding wallet balance across `spreadlend` offers, and places
fixed-rate `LIMIT` funding offers between `gapbottom` and `gaptop`.

Important config values:

- `mindailyrate`: minimum daily funding rate, percent.
- `maxdailyrate`: maximum daily funding rate, percent.
- `spreadlend`: number of offers to split into.
- `gapbottom` / `gaptop`: book depth range used to choose rates.
- `xdaythreshold` / `xdays`: use longer periods when rates are high.
- `minloansize`: default minimum offer amount. Bitfinex minimum is about 150 USD equivalent.
- `platformfeerate`: funding provider fee used by the web earnings estimate.
- `smartstrategy`: follow the live market book instead of holding a fixed minimum rate.
- `smartrateoffset`: minimum extra daily rate above the current market floor.
- `smartfastdepth`, `smartbalanceddepth`, `smartopportunitydepth`: book depth targets for split offers.
- `smartopportunitypremium`: extra daily rate for the highest waiting bucket.
- `repricestaleoffers`: cancel and reprice active offers that have waited too long.
- `repriceafterminutes`: how long an active offer can wait before it is repriced.

The earnings cards in the dashboard use Bitfinex ledger category `28`
(`margin / swap / interest payment`) for the funding wallet. If the API key lacks
ledger permission, the bot still runs and the dashboard shows no earnings data.

For currencies other than `USD` and `UST`, add a per-currency minimum amount in
`coinconfig`:

```ini
coinconfig = ["BTC:0.01:1:0:0:0:0.005"]
```

Format:

```text
COIN:mindailyrate:enabled:maxtolent:maxpercenttolent:maxtolentrate:minloansize
```

## Security

Never commit `default.cfg`, `www/botlog.json`, process logs, or local shortcut
files. They are ignored by `.gitignore` because they can contain credentials,
balances, account activity, or machine-specific paths.
