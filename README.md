# GPU Profit Hunter

**Version:** 0.7.9 · **Run mode by default:** scanner only · **Live spending by default:** disabled

GPU Profit Hunter is a local dashboard and conservative automation framework for comparing GPU-cloud rental offers against cryptocurrency-mining profitability estimates. It integrates:

- **Vast.ai REST API**: search verified on-demand single-GPU offers, inspect instances, create an instance from a qualified offer, and destroy managed instances. Vast documents these endpoints in its [REST API guide](https://vast.ai/developers/api) and [instance-creation guide](https://docs.vast.ai/api-reference/creating-instances-with-api).
- **WhatToMine API**: load its current GPU benchmark and coin catalogs and calculate candidate revenue for provider-supported algorithms. Vast rental-offer scans run every 60 seconds by default, and configured Kryptex pool/coin telemetry polls every 60 seconds. The all-coin catalogue uses a 5-minute cache and calculated GPU revenue uses a 15-minute cache by default to avoid exhausting monthly API quotas. It requires an API token and is subject to plan/quota limits; see [WhatToMine API docs](https://whattomine.com/api-docs).
- **Kryptex Pool public API**: monitor a configured coin account's worker list, hashrate chart, miner balance, pool metadata and coin-price sample. Configure the public Kryptex mining username/payout wallet and coin in the dashboard's **Kryptex Pool connection** panel, or use `KRYPTEX_MINING_USERNAME`, `KRYPTEX_COIN` and optional `KRYPTEX_WORKER` environment variables. No private Kryptex API token, account password, seed phrase or private key is needed. The public Pool API does not document the legacy per-GPU benchmark route used by older versions, so that feed is disabled and excluded from rental economics by default. Miner balance is never mislabeled as daily income. See [Kryptex Pool API](https://pool.kryptex.com/api).
- **A miner profile registry**: the scanner can cover the hardware/coin/algorithm entries returned by WhatToMine, while live auto-mining is only allowed for combinations that have a compatible miner profile, a wallet, an on-start command, and remote monitoring configured.
- **Persistent audit log and dashboard**: stores scan results, policy rejections, managed instances, estimated revenue/cost, measured hashrate and share evidence in PostgreSQL when `DATABASE_URL` is set (Render), or SQLite for local development.

## Login, UI and INR display updates (v0.7.2)

- The dashboard uses an in-app sign-in page instead of the opaque browser Basic Auth prompt. It shows the exact configured username, has a show/hide password control, gives a useful error after rejected credentials, and explains where to check `APP_USER` / `APP_PASSWORD` in Render. Sessions use HttpOnly, SameSite=Lax cookies that are Secure on Render; they expire after 12 hours. Ten failed attempts from an address within 15 minutes trigger a temporary 15-minute wait.
- The app remains scanner-only by default. The redesign does not silently enable paid rentals.
- USD remains the accounting currency. The dashboard converts monetary estimates to INR using the timestamped USD/INR reference supplied by ExchangeRate-API; it caches the daily quote and hides INR conversions if the quote is unavailable or stale. This conversion does not feed back into profit gates, costs, or auto-rent decisions.
- Instance monitoring now labels the model's conservative gross forecast separately from the pool connector's current 24-hour revenue run-rate. They are different measures and should not be read as realized payout. The top KPI is explicitly the **best candidate's estimated net/day**, not total portfolio net.

## UI action feedback (v0.7.2)

Every main action now opens a persistent live-status panel with a progress bar, current stage, timestamped step history, completion/failure state, and a disabled/spinning action button while work is in progress. The market scan reports its actual server-side stages: provider configuration, Vast offer query, WhatToMine GPU and coin catalogue requests, offer/coin economics, freshness/risk checks, and persistence. Kryptex refresh, deployment evaluation, manual rent, destroy, settings save, and table refresh also show task-specific progress. The panel is announced to assistive technology using a live region; controls have visible keyboard focus. Percentages for external API calls are stage indicators, not a guarantee of exact elapsed work remaining. A successful request is distinguished from confirmed GPU readiness; check the Instances table and provider state after deployment/destruction.


## Scan repair and one-instance-first target (v0.7.3)

- Fixed `UnboundLocalError` in market scanning: the scan function no longer re-imports `now_iso` and `normalized_gpu` inside the function after using the module-level imports.
- The deployment target defaults to one instance on a fresh install. The dashboard separately shows the currently selected target, active instance count, and fully qualified candidate count. Changing the target updates the preview immediately; the selection must still be saved before it applies.
- A target is a ceiling on simultaneous instances, not an instruction to rent every scanned offer. Existing active instances count toward the target; `0` remains scan-only. Live eligibility, qualified offers, monitoring and daily spend guards remain in force.

## Scan hang fix (v0.7.9 hotfix)

- **Bug:** a scan could sit forever on "Searching preferred Vast offers · 12%", so no opportunities ever appeared. One stalled Vast request (DNS/TLS stall or a slow-drip response, which `requests` read-timeouts do not catch) kept the scan lock, so every later click only "attached" to the dead scan.
- **Fix:** every Vast offer search now has a hard 45 s wall-clock limit (streamed body, size cap, fresh HTTP session after a stall); the whole scan has a 240 s watchdog (`SCAN_HARD_TIMEOUT_SECONDS`, optional) that fails the scan with a clear message and always releases the lock; a failed provider search no longer overwrites the last good opportunity list with an empty one; the dashboard now retries a busy DB pool and shows the real error type instead of a generic message.

## Wallet / missing-key reporting fix (v0.7.9 hotfix 2)

- A yellow banner now lists every missing key by name (`VAST_API_KEY`, `WHATTOMINE_API_TOKEN`, Kryptex username, SSH key, payout wallets) and what each one disables. It is served by a database-independent `/api/setup/status`, so it still appears when the dashboard database snapshot fails.
- The Vast wallet panel says "Vast API key missing" instead of an endless "Checking…", starts its first balance check automatically, and accepts the `credit` field from Vast's account response when `balance` is absent.
- `data/wallets.json` problems (missing file, invalid JSON) are reported instead of silently ignored, and wallet keys match regardless of letter case.
- The supervisor's first balance/scan checks now run immediately after boot.

## Important limitations

1. **No software can guarantee no loss.** Mining reward rates, network difficulty, coin prices, marketplace prices, host health and share luck can change between polls. This app fails closed: it refuses stale or uncertain estimates and destroys a managed instance when monitoring/performance guards fail, but a short loss can still occur during startup or before the next check.
2. **Vast credit eligibility is checked before live actions.** The current Vast terms prohibit cryptocurrency mining when credits were purchased using a credit card. Do not enable live mining unless your credit source is eligible under the terms that apply to your account. See [Vast Terms](https://vast.ai/terms).
3. **“All coins and all GPUs” means all items the connected profitability provider returns and has benchmarks for.** It cannot safely mine a coin without an available compatible miner, supported algorithm, working pool, payout address and measurable performance. Add reviewed profiles for every desired coin/algorithm. There is no universal miner that works with every coin and GPU.
4. **Revenue is not the same as payout.** In this release, day-to-day forecast revenue is a conservative estimate calculated from provider benchmarks and, for managed instances, adjusted by observed hashrate. A compatible pool payout API is needed for pool-account earnings/balance to be shown as provider-reported actuals; the dashboard explicitly labels estimates and does not invent realized payout data.
5. **The market scan uses a bounded offer search.** It requests up to 500 verified, rentable, single-GPU on-demand offers; this is not a promise to enumerate every offer on Vast's full marketplace. Candidate economics use WhatToMine as the documented gross-revenue benchmark. Kryptex pool-account telemetry is separate from GPU profitability, and the undocumented legacy per-GPU reference route stays disabled by default. It cannot affect rental economics unless an operator explicitly enables it after independent verification.
6. This is a personal control panel, not a hosted multi-user service. The default bind address is `127.0.0.1`. Do not expose it to the public internet without a strong `APP_PASSWORD`, a unique `APP_SECRET_KEY` (at least 32 random characters), HTTPS, and a security review. The sign-in username is `APP_USER`, defaulting to `gph` when omitted.

---


## Deploy on Render with your existing PostgreSQL database

This project is for your existing Render web service and external Render PostgreSQL database. **Do not create a Blueprint or SQLite disk for this deployment.**

1. Upload the contents of the `gpu_profit_hunter` folder to the root of your connected GitHub repository, then deploy/redeploy your existing Render web service (not a Blueprint).
2. In Render, open your web service → **Environment** → **Add Environment Variable**. Set `DATABASE_URL` to the connection string for your existing Render PostgreSQL database. Prefer the **Internal Database URL** when the web service and database are in the same Render region; an External Database URL can also work when its network access is enabled. Do not include quotes or whitespace around the URL. The app rejects non-PostgreSQL URLs on Render; `DATABASE_PATH` is not used there.
3. Add the required provider variables: `APP_PASSWORD`, `APP_SECRET_KEY`, `VAST_API_KEY`, `WHATTOMINE_API_TOKEN`, and `KRYPTEX_MINING_USERNAME`. `APP_USER` is optional and defaults to `gph`; set it if you want a different login username. The sign-in page shows the configured username and tells you where to check both settings. Set `KRYPTEX_COIN=prl` and `KRYPTEX_WORKER` to the worker label actually used by your miner.
4. Set the build command to `pip install -r requirements.txt` and the start command to `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120` (assuming `app.py` is at the repository root). Keep one Gunicorn worker because the background supervisor is intentionally a single process.
5. Save and redeploy. The app will create its namespaced `gph_*` tables in the PostgreSQL database on first start, avoiding collisions with unrelated apps that share the same Render database. It uses `DATABASE_URL` automatically when present; `DATABASE_PATH` is only a local SQLite fallback.

The dashboard uses a dedicated sign-in page rather than the browser's opaque Basic Auth popup. Username is `APP_USER` (defaults to `gph`); password is the exact `APP_PASSWORD` value. Session cookies are HttpOnly, SameSite=Lax, Secure on Render, and expire after 12 hours.

USD remains the accounting currency for costs, forecasts and all profit gates. The UI may show INR equivalents using a timestamped USD/INR reference quote from [ExchangeRate-API Open Access](https://www.exchangerate-api.com/docs/free), refreshed and cached daily with attribution. Those conversions never change profitability calculations or rental decisions. If the quote is missing or stale, INR figures are hidden instead of silently using an old rate; conversion values are indicative, not guaranteed exchange or payout amounts.

If you use the supplied Render environment template, replace the `DATABASE_URL` placeholder with your actual PostgreSQL connection string and replace both password/secret placeholders before importing it in Render. Alternatively, add the variables individually in the existing service's Environment panel. Never import an unedited template or commit the completed file. Keep all credentials private. Never put your database URL into the frontend or a public GitHub repository.

### Scan frequency and source-data freshness

The recurring defaults are deliberately faster than five minutes:

- **Vast GPU rental offers and opportunity ranking:** every 60 seconds.
- **Kryptex worker/account telemetry and configured coin-price sample:** every 60 seconds.
- **Kryptex pool/miner/coin telemetry:** uses the documented read-only public API routes. The older per-GPU route `/api/v1/device/gpu/{slug}` is not listed in the published Pool API documentation and is disabled by default; it cannot affect rental economics unless `KRYPTEX_GPU_REFERENCE_ENABLED=true` is deliberately set after independent endpoint/field verification. WhatToMine remains the default documented gross-revenue benchmark.
- **WhatToMine per-GPU revenue calculations:** cached for 60 seconds by default so fresh calculated data is requested at most once per minute per GPU model.
- **WhatToMine GPU catalogue and all-coin identity/algorithm catalogue:** cached separately (the coin catalogue default remains 5 minutes because it is metadata used to label calculations, not the fast market-price feed).

A one-minute polling interval does not mean the upstream website changes data every minute. The dashboard must show timestamps and API errors, and the profit gate must reject stale results. Frequent calls may use more provider quota; monitor HTTP 429/quota errors and raise the provider-specific cache interval if the provider plan requires it. The app does not bypass quotas or invent replacement values.

### Before any automatic rental

The Render deployment starts in scanner-only mode. It does **not** ship with enabled mining profiles because there is no single miner, pool, or wallet format that works for every coin and GPU. For each supported coin/algorithm, review a compatible miner profile, pool URL, public wallet address, miner source, log parsing rules, and pool-reported revenue connector. Upload your SSH **private** key using Render's Secret Files feature and set `SSH_PRIVATE_KEY_PATH` to the resulting `/etc/secrets/<filename>` path; add the matching **public** key to Vast. Then test scanner-only mode and verify the pool data before changing the risk-gate environment variables.

Vast's API key can create and destroy paid instances. Keep it in Render environment variables, keep the in-app sign-in enabled, do not enable auto-mining merely to make the dashboard look active, and never assume the software can guarantee zero loss. Every live deployment still has startup, API, network, reward and pricing risk. The application must refuse to rent when the live blockers shown in the dashboard remain.

## v0.7.7: documented Vast search, asynchronous scans, and independent live monitoring

- Vast API defaults and any legacy `console.vast.ai` base URL are normalized to `https://cloud.vast.ai/api/v0`. Strict and broad marketplace searches now use the documented `GET /bundles/?q=<JSON>` interface.
- The Kryptex `/api/v1/device/gpu/{slug}` route is undocumented by the published Pool API docs, so the per-GPU cross-check is disabled by default and excluded from rental economics until the operator explicitly enables and verifies it.
- The manual scan endpoint responds immediately and runs market work in a background worker. The dashboard polls saved server-side progress instead of holding an HTTP request open through long provider calls.
- Dashboard state reads cached telemetry only; it no longer makes live Vast instance-list, Kryptex, or FX requests during page load. The independent monitor worker persists a timestamped, validated Vast instance snapshot; provider errors leave the last successful snapshot intact and record the most recent error.
- `/api/state` and other API exceptions return JSON errors with an incident reference; the browser now reports the HTTP status/content type when an upstream proxy returns HTML/non-JSON.
- Instance monitoring runs in a separate thread from market scans/deployment whenever a Vast key is configured, even after switching future rentals back to scanner-only mode. Individual managed instances are checked with bounded concurrency so a stalled SSH endpoint for one rental does not serially delay every other rental. Manual/automatic rentals re-query the selected Vast offer, enforce verified/rentable/single-GPU/reliability requirements, and rerun conservative net economics using the current all-in rental rate before instance creation.
- In live mode, three consecutive missing/invalid pool-revenue telemetry samples after the startup grace period trigger best-effort termination. Repeated failures to list Vast instances also trigger best-effort fail-safe termination requests. A destroy API response explicitly marked as failed is no longer recorded as success. Accepted DELETE requests remain `destroy_requested`; monitoring retries while the instance is still visible and only records `destroyed` after the provider list confirms it is gone. Uncertain/missing instances continue to reserve budget and block new rentals.
- These guards reduce avoidable losses, but no software can guarantee zero loss from price changes, delayed pool reporting, API outages, startup time, or the provider accepting a destroy request asynchronously. Verify Render logs and the provider's instance list before enabling paid automation.

## 1. Install on Windows

1. Download and extract the project ZIP.
2. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/). During install, enable **Add Python to PATH**.
3. Open the extracted `gpu_profit_hunter` folder.
4. Copy `.env.example` to `.env`.
5. Open Command Prompt or PowerShell in that folder and run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python app.py
```

If PowerShell blocks activation, use Command Prompt and run `\.venv\Scripts\activate.bat`, then continue with the `pip install` command.

Open **http://127.0.0.1:8088** in your browser.

### Linux/macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

## 2. Connect the data providers

### Vast.ai

1. Create an API key in the [Vast console](https://cloud.vast.ai/), under **Keys**.
2. Put it in `.env` as `VAST_API_KEY=...`.
3. Keep the key private. It can control paid cloud resources.

### WhatToMine

1. Create an API token through the [WhatToMine API page](https://whattomine.com/api-docs).
2. Put it in `.env` as `WHATTOMINE_API_TOKEN=...`.
3. API calls are quota-limited; the app refreshes GPU revenue calculations at most once per minute per GPU model by default while the coin metadata catalogue is cached for five minutes. If the provider rejects requests or the quota is exhausted, the scanner reports the error and will not substitute invented values.

Restart the app after modifying `.env`, then press **Scan market**.

## Choosing multiple GPUs

The dashboard uses a numeric target input and supports **0 (scan only) through 25 simultaneous instances**. Enter any whole number and click **Save target**. The explicit saved UI target controls automatic target-fill, rather than being silently truncated by the legacy `MAX_ACTIVE_INSTANCES` environment default. A hard software ceiling of 25 remains. The scanner ranks eligible deployment candidates by conservative expected net profit per day **after** modeled rent, disk, pool/miner fees, revenue haircut and bandwidth reserve. It avoids reusing the same Vast offer for multiple deployments.

The target is a desired total, not a promise that many GPUs will be rented. The active count already consumes target slots. `MAX_DAILY_SPEND_USD`, per-offer limits, live eligibility, compatible miner profiles, fresh profitability data, provider availability and monitoring checks can limit the deployed count. Increase the daily budget only if you have deliberately decided that the corresponding spend is acceptable. A positive estimate is not guaranteed earnings, and GPU mining liquidity/order-book execution is not comprehensively verified by this release.

## 3. Start scanner-only mode first

These defaults are intentional:

```dotenv
AUTOMATION_MODE=scan
ENABLE_LIVE_MINING=false
MINING_ELIGIBILITY_CONFIRMED=false
AUTO_SCAN_ENABLED=true
AUTO_RENT_ENABLED=false
SCAN_INTERVAL_SECONDS=60
KRYPTEX_POLL_INTERVAL_SECONDS=60
WHATTOMINE_COINS_CACHE_SECONDS=300
WHATTOMINE_CALC_CACHE_SECONDS=60
```

Scanner-only mode repeats Vast rental-offer scans every minute when the Vast and WhatToMine providers are configured. Kryptex configured-account/coin telemetry is polled every minute; the undocumented legacy per-GPU reference endpoint is disabled by default and does not feed rental economics. WhatToMine per-GPU revenue calculations use a 60-second cache; the all-coin identity/algorithm catalogue remains cached for five minutes to reduce API quota use. Scanning does not create paid rentals; the dashboard's Auto-deploy switch only works after live eligibility, miner-profile and remote-monitoring gates pass.

## 4. Miner profiles and public wallets

The app intentionally ships with **no enabled mining profiles**. That avoids accidentally launching an unreviewed third-party miner on a paid machine.

1. Copy `data/mining_profiles.example.json` to `data/mining_profiles.json`.
2. For each supported coin/algorithm, add a profile that specifies a reviewed miner image, a compatible start command, a log file path, expected hashrate, miner fee and pool fee. The profile `onstart` command runs inside the rented instance, so review every line before enabling it.
3. Add **public receiving addresses only** to `data/wallets.json`, matching the profile's `wallet_key`. A profile uses `GPH_WALLET` and `GPH_WORKER` inside the rented container.
4. Make sure the miner log writes parseable hashrate and accepted-share lines. By default the monitor understands common lines such as `Total 48.7 TH/s` and `accepted`. You can supply profile regexes (`hashrate_regex`, `accepted_regex`) when your miner uses different output.
5. For live mode, configure `pool_metrics` per profile. It is a read-only HTTPS JSON adapter that must report a **current hashrate-based 24-hour revenue projection in USD** (not lifetime balance or a raw coin amount). Use `{wallet}`, `{worker}` and `{coin_tag}` placeholders in the URL; optionally configure JSON paths for pool hashrate, accepted shares and balance. `revenue_24h_usd_path` must point to USD/day. A small trusted adapter may be needed where the pool API reports native coin units or trailing historical totals. The guard uses the lower of WhatToMine's hashrate-adjusted forecast and this pool run-rate after applying the safety haircut. Missing telemetry repeatedly causes a fail-safe destroy. The example connector is deliberately a placeholder and blocks live mode.
6. Never put wallet seed phrases, private keys, exchange passwords or API secrets into wallet/profile files.

Example profile structure (not enabled, and not a turnkey miner installation):

```json
{
  "profiles": [
    {
      "id": "example-profile",
      "enabled": false,
      "coin_tags": ["COIN_TAG"],
      "algorithms": ["ALGORITHM_NAME"],
      "gpu_models": ["*"],
      "image": "nvidia/cuda:12.2.2-base-ubuntu22.04",
      "disk_gb": 12,
      "wallet_key": "COIN_TAG",
      "worker": "gph-worker",
      "pool_fee_pct": 1.0,
      "miner_fee_pct": 2.0,
      "log_path": "/tmp/gph-miner.log",
      "expected_hashrate_hs": 1000000000,
      "onstart": "REPLACE_WITH_REVIEWED_INSTALL_AND_MINER_START_COMMAND"
    }
  ]
}
```

Set `enabled` to `true` only after you have verified the exact miner binary/source, launch flags, algorithm, pool URL, wallet format and miner log output.

## 5. Remote connection and monitoring setup

Live automation requires SSH monitoring; status alone is not enough.

1. Create an SSH key pair on the machine running the dashboard, or use an existing dedicated key.
2. Register the **public key** in your Vast account's SSH keys.
3. Set `SSH_PRIVATE_KEY_PATH` in `.env` to the matching private key file. Do not upload the private key to Vast or commit it into the project.
4. The app creates `ssh_direct` instances. It polls the Vast instance state, connects over SSH, reads the configured miner log and `nvidia-smi`, and checks hashrate, accepted-share evidence, GPU temperature and net forecast.
5. If SSH/log monitoring cannot be established within the startup grace period, or no valid hashrate/share evidence appears, the app attempts to destroy the managed instance. This is a fail-safe, not a promise of zero cost during startup.

If the SSH key is not registered or the instance API response lacks direct SSH endpoint data, live mode remains blocked or the monitored instance is destroyed after the grace period.

## 6. Enabling live mode (do not do this until you have verified the above)

**Do not set these values if your Vast credits were purchased with a credit card.** Confirm the current Vast terms and account-specific credit eligibility first.

Only after scanner-only tests and profile validation, edit `.env` with deliberate limits. Example configuration (adjust to a budget you can afford to lose):

```dotenv
AUTOMATION_MODE=live
ENABLE_LIVE_MINING=true
MINING_ELIGIBILITY_CONFIRMED=true
VAST_CREDIT_SOURCE=crypto
AUTO_SCAN_ENABLED=true
AUTO_RENT_ENABLED=true

MIN_RELIABILITY=0.99
# 0 disables a fixed hourly-rate ceiling; profit/budget checks remain active.
MAX_GPU_HOURLY_USD=0
MAX_TOTAL_HOURLY_USD=0
MAX_DAILY_SPEND_USD=1.75
# Legacy compatibility setting; saved dashboard target (0–25) controls target-fill.
MAX_ACTIVE_INSTANCES=3
MIN_REVENUE_COST_RATIO=1.50
MIN_NET_PROFIT_USD_DAY=0.50
REVENUE_HAIRCUT=0.30
STARTUP_GRACE_MINUTES=10
MAX_NO_SHARE_MINUTES=15
MIN_OBSERVED_HASHRATE_RATIO=0.70
# 0 removes the fixed runtime ceiling. Instances remain subject to live profitability and health checks.
MAX_INSTANCE_RUNTIME_HOURS=0
```

The `VAST_CREDIT_SOURCE` value must reflect reality; `crypto` is an example, not a workaround. Do not claim eligibility unless it is true and permitted by the current terms. Restart the app. Review **Safety & setup** and make sure there are no live blockers before enabling a profile.

The app never tops up Vast credits automatically. It defaults to a maximum of three active instances, subject to spend and safety gates. Setting `MAX_GPU_HOURLY_USD=0` and `MAX_TOTAL_HOURLY_USD=0` disables only fixed hourly price ceilings; profitability ratios, minimum conservative net profit, freshness, reliability, and the daily spend budget still gate new rentals. Setting `MAX_INSTANCE_RUNTIME_HOURS=0` disables only the fixed time ceiling. An instance can continue only while market data stays fresh, pool/hashrate monitoring works, accepted-share evidence and health checks pass, and conservative live net remains above the threshold. For an unlimited-runtime instance, the admission controller still reserves a full 24 hours of costs against `MAX_DAILY_SPEND_USD` before creating it. Automatic destruction prevents additional future charges but cannot refund rent already accrued. No system can guarantee zero loss: API delays, pool outages, sudden reward/price changes and provisioning time can still incur rent before a stop rule fires. The bot's policy is to avoid uncertain deals, cap exposure, and fail closed—not to promise guaranteed profit.

## Profitability model

## Vast wallet, 24-hour runway and realized net (v0.7.5)

- **Vast wallet balance**: the app uses the read-only `GET /users/current/` endpoint and displays the returned account `balance` in USD. The balance is cached in the existing app database and refreshed periodically; use **Refresh balance** for a direct check. A failed refresh is displayed as unverified/last-known rather than as a confirmed live balance.
- **24-hour funding requirement**: adds 24 hours of current active-instance rent plus the configured bandwidth reserve, then estimates any unfilled target slots using the highest expected-net economically positive candidate from the latest scan. The label identifies that estimate and still warns when live qualification gates are not satisfied. It is not a billing quote and cannot model all changing bandwidth/storage charges.
- **Add-more-funds warning**: when the latest live balance is below the modeled requirement, the dashboard shows the minimum estimated shortfall and links to Vast Billing. If the balance cannot be confirmed, the dashboard does not tell you that funds are sufficient.
- **Expected net**: conservative forecast for the best economically positive candidate, separate from any actual/pool-observed result.
- **Achieved rolling-24h net**: shown only when every active rental has fresh actual trailing-24h USD earnings from its configured pool endpoint and at least 24 hours of rental history. Configure the optional profile field `pool_metrics.realized_revenue_24h_usd_path` only when your pool response provides a realized 24-hour USD total. The existing required `revenue_24h_usd_path` is a current USD/day run-rate estimate for safety monitoring, not achieved income. Achieved net subtracts estimated 24h rent and configured bandwidth reserve; unreported fees, storage and payout adjustments may remain excluded.
- **Visible scan indicator**: the live action panel now has an animated circular stage indicator, percentage, current provider/profitability stage and step history. The ring changes to a check on completion or an error mark if the action fails.

Vast account-balance API reference: https://vast.ai/developers/api (read-only `GET /api/v0/users/current/`). Credits can be managed at https://cloud.vast.ai/billing/.

For each coin/algorithm row returned by the provider:

- **Raw gross/day**: revenue supplied by WhatToMine's calculator for the matched GPU benchmark.
- **Conservative gross/day**: raw gross after the configured revenue haircut (default 30%).
- **After fees**: conservative gross after miner/pool fee assumptions.
- **Total cost/day**: current Vast all-in hourly price × 24 + a configurable bandwidth reserve.
- **Expected net/day**: after-fee conservative gross − total cost/day.
- **Revenue/cost ratio**: after-fee conservative gross ÷ total cost/day.
- **Qualified**: additionally requires fresh provider data, host reliability, rate caps, target margin, an enabled matching miner profile, a configured wallet, valid live settings and SSH monitoring.
- **Cross-source check**: the legacy Kryptex per-GPU feed is disabled by default because its route is not listed in the public Pool API documentation. Unless the operator independently verifies and explicitly enables it, WhatToMine is the sole gross-revenue benchmark used by the scanner; pool account balance and hashrate telemetry are not used as daily revenue.

The default gate requires at least **1.5× conservative revenue/cost coverage** and **$0.50/day net forecast**, alongside a daily spending budget. A zero hourly price cap means price is judged by these economics rather than a blanket dollar ceiling. Set a positive hourly cap if you want an additional strict price limit. You can lower profit gates in `.env`, but doing so only changes the policy—not the underlying economics—and is not recommended just to force a rental.

## Dashboard guide

- **Overview**: estimated net candidate, active rental burn, number of offers and gate status; current Vast credits, modeled funding required for the next 24 hours, low-balance/top-up warning, expected net, and achieved rolling-24h net when a pool endpoint explicitly provides actual realized USD earnings. If it only provides a current USD/day run-rate, achieved net is intentionally unavailable rather than overstated.
- **Market scanner**: each candidate, model, algorithm, price, raw/conservative gross, expected net, coverage, freshness, reliability and reasons for rejection.
- **Instances**: active Vast instances plus local history for instances no longer returned by Vast, with price, observed hashrate, GPU temperature/utilization, share evidence, forecast net and guard-triggered destroy reason.
- **Automatic deployment target**: type a whole number from 0 to 25 in the dashboard and click **Save target**. The saved UI target controls the desired simultaneous count; optionally enable auto-deploy after live gates are complete, then request a target-fill deployment. The total daily spend limit still takes precedence; if too many instances would breach it, the remaining slots are skipped.
- **Kryptex Pool connection**: worker count, API-reported hashrate, miner balance and optional coin-price sample. The app's dashboard form configures the public username/wallet and coin. Account telemetry remains separate from forecast revenue; balance is not daily earnings. Public telemetry lookups run concurrently with bounded timeouts. The undocumented legacy per-GPU reference route is disabled by default and is not used to qualify rentals.
- **Safety & setup**: missing API keys, SSH requirements, active profiles and all live blockers.
- **Event log**: scan failures, rejected candidates, rental creation, monitoring failures and guard-triggered destruction.

## Tests

```bash
python -m unittest discover -s tests -v
```

No tests create cloud resources or connect to paid APIs.

## Troubleshooting

- **“VAST_API_KEY is missing”**: add the API key to `.env`, restart.
- **WhatToMine 401/429**: confirm token, plan and monthly quota; wait until the API quota resets if needed.
- **No matching GPU benchmark**: that marketplace model name is not matched to a WhatToMine GPU benchmark; this candidate is skipped instead of assigned an invented rate.
- **No miner profile**: scanner still shows provider-supported opportunities, but live rental is blocked until a profile and public wallet are configured.
- **Monitoring cannot connect**: check the matching public key is registered in Vast, the private key path is correct, and the instance supports direct SSH.
- **All opportunities rejected**: that means no candidate currently passes your conservative settings. Do not assume the software is broken and do not lower safeguards merely to force a machine to start.


## v0.7.5 deployment and static asset diagnostics

This release serves bundled CSS/JavaScript from the absolute application path with explicit cache control. After redeploy, open `/api/health`: `assets_ok` must be `true` and `asset_sizes_bytes` for `app.js`, `app.css`, and `login.css` must all be greater than zero. If an asset reports zero, redeploy the complete `gpu_profit_hunter` directory from this ZIP; do not upload only `app.py` or only the templates. The asset URLs use a new version query to avoid stale cached UI.


## v0.7.6: broad-market review, visible rejected offers, recurring scan diagnostics

- Every scan runs the strict verified/rentable/high-reliability single-GPU query and then a broader on-demand query (`num_gpus >= 1`) so strict matches do not hide rejected/uncertain offers. The strict and broad queries use the documented `GET /bundles/?q=...` search on `cloud.vast.ai`.
- Broad results are **review-only until local checks prove one-GPU count, verification, rentability, reliability, fresh benchmarks, and the configured mining/profile gates**. Multi-GPU and uncertain offers are retained in the offer-level audit table with specific exclusion reasons; they are never rented merely because the fallback surfaced them.
- The Market Scanner exposes strict POST, broad POST, and GET-fallback counts, provider diagnostic notes, missing offer IDs, unsupported GPU benchmark names, and the reasons each returned offer did not qualify. If all API calls return no records, the UI explains that there are no records to audit and points to account/API/base-URL/provider response checks; public marketplace inventory is not assumed to equal the authenticated API response.
- **Vast search quota.** Vast limits offer searches by *rows returned* (20,000/day on this account, reset at 00:00 UTC), not by request count. Version 0.7.10 requests `VAST_SEARCH_LIMIT` (default 100) rows sorted by `VAST_SEARCH_ORDER` (default cheapest first; prefix `-` for descending, empty for Vast's default), runs the broad audit query only every `VAST_AUDIT_INTERVAL_SECONDS` with `VAST_AUDIT_LIMIT` rows (0 disables it), keeps `VAST_ROW_RESERVE` rows free for pre-rental revalidation, and stretches the automatic scan interval so the remaining rows last until the reset. On HTTP 429 `search_quota_exceeded` it stops searching until Vast's `retry_after`, keeps the last good results on screen, and shows "SEARCH PAUSED" with the resume time. The row counter and cool-down are stored in the database, so a redeploy cannot reset them. **Extra search-only keys:** set `VAST_SEARCH_API_KEYS` (comma separated) to add keys from other Vast accounts. They are used only for offer searches (tried first, each with its own persisted row counter and 429 cool-down; the main key is the last fallback). Rent, payment, balance, monitoring and destroy always use `VAST_API_KEY`, so every rental is created and billed on the funded account, and each offer is revalidated with that key just before renting.
- Automatic scans remain scheduled by `AUTO_SCAN_ENABLED` and `SCAN_INTERVAL_SECONDS` (minimum 60 seconds). The scanner status panel polls background progress and labels successful, running, and failed states. Auto-scan is recurring, not a continuous per-second marketplace poll.
- Regression tests cover strict-query empty results, broad fallback, documented GET fallback, fallback metadata safety, and offer-level rejections. The app never lowers its safety gates to force a rental.

