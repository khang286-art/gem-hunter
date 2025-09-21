# Pumpbonk Worker v4 (Dexscreener + Birdeye)

## Features
- Combines Dexscreener + Birdeye token data
- 5-min test mode with relaxed filters
- Rate-limit handling (429 with exponential backoff)
- Tick logs each loop
- Telegram alerts

## Deploy on Render
Build: `pip install -r requirements.txt`
Start: `python -u main.py`

### Env Vars
- DEX_URLS=https://api.dexscreener.com/latest/dex/search?q=chain:solana%20dex:pumpfun
- BIRDEYE_API=https://public-api.birdeye.so/defi/tokenlist?chain=solana
- BIRDEYE_KEY=<optional>
- MIN_LIQ_USD=1500
- MAX_LIQ_USD=25000
- FDV_MIN=20000
- FDV_MAX=80000
- MAX_AGE_MIN=360
- PREF_AGE_MIN=40
- SPREAD_MAX=1.5
- MIN_SCORE_TO_ALERT=2.5
- TELEGRAM_BOT_TOKEN=xxx
- TELEGRAM_CHAT_ID=yyy
