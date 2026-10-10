"use strict";
// Manual isolated fixture QA. Start `python tests/v4_dashboard_fixture.py --v42` first.
// MIKA_PLAYWRIGHT_MODULE may point at the desktop runtime's bundled Playwright.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");
const {chromium} = require(process.env.MIKA_PLAYWRIGHT_MODULE || "playwright");
const origin = "http://127.0.0.1:8124";
const output = path.join(os.tmpdir(), "mika-v42-browser-qa");

async function inspectLayout(page, name, report) {
    const layout = await page.evaluate(() => {
        const ids = [...document.querySelectorAll("[id]")].map(node => node.id);
        return {
            duplicateIds: ids.filter((id,index) => ids.indexOf(id) !== index),
            width: document.documentElement.clientWidth,
            scrollWidth: document.documentElement.scrollWidth,
            overviewCurrencyCount: document.querySelectorAll(".currency-overview").length,
            strategyCurrencyCount: document.querySelectorAll(".currency-strategy").length,
            overflow: [...document.querySelectorAll("body *")].filter(node => {
                const rect = node.getBoundingClientRect();
                return rect.width && rect.right > document.documentElement.clientWidth + 1;
            }).slice(0,10).map(node => ({tag:node.tagName,id:node.id,className:node.className})),
        };
    });
    report[name] = layout;
    assert.deepEqual(layout.duplicateIds,[], `${name}: duplicate DOM IDs`);
    assert.equal(layout.overviewCurrencyCount,2);
    assert.equal(layout.strategyCurrencyCount,2);
    assert.ok(layout.scrollWidth <= layout.width + 1, `${name}: horizontal overflow ${JSON.stringify(layout)}`);
    assert.deepEqual(layout.overflow,[], `${name}: content outside viewport`);
    await page.screenshot({path:path.join(output, `${name}.png`),fullPage:true});
}

async function main() {
    await fs.mkdir(output,{recursive:true});
    const browser = await chromium.launch({channel:"chrome",headless:true});
    const report = {fixture:origin,readOnlyNetworkOrigin:origin,scenarios:[],errors:[]};
    const context = await browser.newContext({viewport:{width:1440,height:1000}});
    // Reject every destination except the dedicated fixture, including localhost:8000.
    await context.route("**/*", route => new URL(route.request().url()).origin === origin
        ? route.continue() : route.abort("blockedbyclient"));
    const page = await context.newPage();
    page.on("pageerror", error => report.errors.push(error.message));
    const field = (currency,name) => page.locator(`#strategy-${currency} [name="${name}"]`);
    const ref = (currency,name) => page.locator(`#strategy-${currency} [data-ref="${name}"]`);
    try {
        await page.goto(origin + "/lendingbot.html#strategy");
        await field("USDT","strategy_engine").waitFor({state:"visible"});
        await page.waitForFunction(() => document.querySelector('#strategy-USDT [name="strategy_engine"]')?.value === "adaptive_net_yield_v3");
        for (const currency of ["USD","USDT"]) {
            assert.equal(await field(currency,"strategy_engine").inputValue(),"adaptive_net_yield_v3");
            assert.equal(await field(currency,"reprice_gain_apr").inputValue(),"0.25");
            assert.equal(await field(currency,"passive_wait_minutes").inputValue(),"60");
            for (const name of ["enable_limit","enable_frr","enable_frr_delta_fixed","enable_frr_delta_variable"])
                assert.ok(await field(currency,name).isVisible());
            for (const name of ["short_share","quick_share","short_reprice_stages_minutes"])
                assert.equal(await field(currency,name).isVisible(),false);
            assert.match(await ref(currency,"v4OperationalState").textContent(),/运行验收：已通过/);
            assert.match(await ref(currency,"v4ProfitabilityState").textContent(),/尚未验证收益优势/);
        }
        await inspectLayout(page,"desktop-strategy",report);
        await field("USD","passive_wait_minutes").fill("30");
        await field("USDT","passive_wait_minutes").fill("90");
        await page.locator("#refreshButton").click();
        assert.equal(await field("USD","passive_wait_minutes").inputValue(),"30");
        assert.equal(await field("USDT","passive_wait_minutes").inputValue(),"90");
        for (const currency of ["USD","USDT"]) {
            const preview = page.waitForResponse(response => response.url().includes("/api/strategy/v4/preview")
                && response.request().postDataJSON().currency === currency,{timeout:120000});
            await ref(currency,"v3PreviewButton").click();
            const response = await preview;
            assert.ok(response.ok(), `${currency}: preview HTTP failure`);
            const body = await response.json();
            assert.equal(body.plan.engine,"adaptive_net_yield_v3");
            assert.equal(body.plan.modelId,await field(currency,"model_id").inputValue());
            report.scenarios.push(`${currency}: V4.2 preview keeps its frozen model`);
        }
        await page.locator("#overviewTab").click();
        await page.locator(".coin-safety").evaluateAll(nodes => nodes.forEach(node => {node.open=true;}));
        await inspectLayout(page,"desktop-overview",report);
        assert.match(await page.locator('#overview-USD [data-ref="strategyVersionPerformance"]').textContent(),/未精确归属|未核算/);
        await page.setViewportSize({width:390,height:844});
        await inspectLayout(page,"mobile-overview",report);
        await page.locator("#strategyTab").click();
        await inspectLayout(page,"mobile-strategy",report);
        assert.equal(await field("USD","passive_wait_minutes").inputValue(),"30");
        assert.equal(await field("USDT","passive_wait_minutes").inputValue(),"90");
        report.scenarios.push("desktop/mobile: two currency forms remain independent across refresh and routing");
        assert.deepEqual(report.errors,[]);
    } finally {
        await fs.writeFile(path.join(output,"report.json"),JSON.stringify(report,null,2));
        await browser.close();
        console.log(JSON.stringify({output,...report},null,2));
    }
}
main().catch(error => {console.error(error);process.exitCode=1;});
