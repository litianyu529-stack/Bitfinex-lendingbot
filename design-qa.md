# Design QA — 状态优先实盘控制室与策略 v2

## Evidence

- Desktop strategy page, 1440×1024 viewport: `artifacts/strategy-desktop-1440x1024.png`
- Mobile strategy page, 390×844 viewport: `artifacts/strategy-mobile-390x844.png`
- Mobile live preflight panel, 390×844 viewport: `artifacts/strategy-mobile-preflight-390x844.png`
- Existing overview baseline: `artifacts/dashboard-desktop-final-1440x1024.png`
- Existing mobile overview baseline: `artifacts/dashboard-mobile-final-390x844.png`
- Browser fixture: `tests/dashboard_fixture.py`; fixed balances, offers, public market signals and replay values only. No real account write operation was used.

## Result

- No remaining actionable P0, P1 or P2 findings.
- Desktop: `innerWidth = 1440`, `scrollWidth = 1425`; no horizontal overflow. The editor and sticky live-plan rail measured 969 px and 390 px.
- Mobile: `innerWidth = 390`, `scrollWidth = 375`; no horizontal overflow. The live plan is ordered before the form, preset cards become one column, and the preflight dialog becomes a full-width bottom panel.
- Browser console: no warnings or errors after the full interaction run.

## Visual and content checks

- The selected “状态优先控制室” language remains intact: teal brand accents, white operational cards, pale blue-green background and a dark green live-plan/control rail.
- Strategy hierarchy is clear at 1440 px: presets → currency inheritance → basic allocation → advanced parameters → account limits; the right rail keeps market regime, plan and replay visible.
- At 390 px, the plan appears first, fields become one column, offer data becomes cards and every primary action remains within the viewport width.
- Every adjustable parameter includes a unit and a Chinese explanation of purpose or risk. Daily rates and premiums expose estimated APR context.
- The plan distinguishes submitted rate from estimated effective rate and explains that variable FRR may decline after matching.
- Replay is explicitly labeled as a historical scenario replay, not a full order-book backtest or return guarantee.
- Open offers show order type and ownership; manual/external offers are not presented as robot-managed.

## Accessibility checks

- Hash navigation exposes tab/tabpanel semantics with arrow-key navigation.
- Preset cards expose radiogroup/radio semantics, roving focus and arrow-key selection.
- Currency tabs expose tab semantics and keyboard navigation.
- Ratio sliders, numeric ratio inputs, FRR order types and FRR offsets have independent accessible names.
- Dialogs expose modal semantics, trap focus, close with Escape and restore focus to the invoking control.
- Pass, warning and blocking states include text labels and never rely on color alone.
- Strategy inputs and all strategy actions are disabled while the bot process is running.

## Interactions exercised

- Switched from “均衡偏收益” to “收益优先”; allocation changed from 50/10/40 to 30/10/60 and the live long bucket updated to 60% / 90 days.
- Enabled a UST currency override and verified that it copied the current global policy before becoming independently editable.
- Changed replay from 7 days to 30 days; the profile became “自定义” and the live replay label updated.
- Verified that unsaved changes prevent opening preflight and route the user back to Strategy with a persistent explanation.
- Saved the strategy and verified the dirty state cleared.
- Successful preflight displayed real-balance summary fields, strategy profile, allocation, order-type behavior and the final per-currency plan.
- Conditional wallet-write permission failure disabled confirmation and exposed “前往策略设置”.
- Invalid/expired preflight token kept the dialog open with a recoverable inline error and “重新运行预检”.
- Successful start changed the process to running and made all strategy controls read-only.
- Stop confirmation explicitly stated that existing Bitfinex offers remain; Escape restored focus to “停止机器人”, and confirmed stop returned to the stopped state.

## Verification

- 52 Python unit tests passed using temporary configs and fake Bitfinex clients.
- Python compilation, JavaScript syntax check and `git diff --check` passed.
- Runtime code, page and example configuration contain no simulated balance or alternate non-live execution path.

final result: passed
