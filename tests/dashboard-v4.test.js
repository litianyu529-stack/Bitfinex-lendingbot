"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const {CurrencySettings, createCurrencyRequester, logCurrency, resolveStrategyModel, strategyEngineProfile,
    validateRepricingInputs, describeAdaptiveDecision, describeAdaptiveCandidate, describeVersionPerformance, describeControlState} = require("../www/v4-dashboard.js");
const initial = () => ({enabled:false, autoTransfer:false});
const deferred = () => {let resolve, reject; const promise = new Promise((yes,no) => {resolve=yes; reject=no;}); return {promise,resolve,reject};};
const tick = () => new Promise(resolve => setImmediate(resolve));

test("a prepared candidate cannot replace an edited frozen model or another currency model", () => {
    const engine = "adaptive_net_yield_v2";
    const usd = {candidates:{[engine]:{id:"usd-new",operationalReady:true}}, details:{"usd-active":{id:"usd-active",operationalReady:false}}};
    const usdt = {candidates:{[engine]:{id:"usdt-new",operationalReady:false}}, details:{"usdt-active":{id:"usdt-active",operationalReady:true}}};
    assert.equal(resolveStrategyModel(usd.candidates,usd.details,engine,"usd-active").id,"usd-active");
    assert.equal(resolveStrategyModel(usdt.candidates,usdt.details,engine,"usdt-active").id,"usdt-active");
    assert.equal(resolveStrategyModel(usd.candidates,usd.details,engine,"").id,"usd-new");
    assert.equal(resolveStrategyModel(usdt.candidates,usdt.details,engine,"").id,"usdt-new");
    assert.equal(resolveStrategyModel(usd.candidates,usd.details,engine,"usdt-active"),undefined);
    assert.equal(resolveStrategyModel(usd.candidates,usd.details,"adaptive_net_yield_v1",""),undefined);
});

test("V4.2 keeps four order types available while selecting the next-cycle repricing controls", () => {
    const v42 = strategyEngineProfile("adaptive_net_yield_v3");
    assert.equal(v42.name,"V4.2 策略"); assert.equal(v42.multi,true); assert.equal(v42.repricing,true);
    assert.equal(strategyEngineProfile("adaptive_net_yield_v2").repricing,false);
    assert.equal(strategyEngineProfile("adaptive_net_yield_v1").multi,false);
    assert.equal(strategyEngineProfile("legacy_v3").adaptive,false);
    assert.equal(strategyEngineProfile(undefined).name,"未知策略");
});

test("V4.2 repricing form rejects missing inputs and accepts both wait boundaries", () => {
    for (const wait of [15,60,360]) assert.doesNotThrow(() => validateRepricingInputs("0.25",String(wait)));
    for (const wait of ["",null,14,361,60.5,"not-a-number"])
        assert.throws(() => validateRepricingInputs("0.25",wait),/15～360/);
    for (const gain of ["",null,-0.01,101,"not-a-number"])
        assert.throws(() => validateRepricingInputs(gain,"60"),/调价收益门槛/);
});

test("V4.2 prepared models remain isolated by currency and engine", () => {
    const engine = "adaptive_net_yield_v3", older = "adaptive_net_yield_v2";
    const usd = {[engine]:{id:"usd-v42",operationalReady:true,eligibleForLiveCandidate:false}, [older]:{id:"usd-v41"}};
    const usdt = {[engine]:{id:"ust-v42",operationalReady:false,eligibleForLiveCandidate:false}};
    assert.equal(resolveStrategyModel(usd,{},engine,"").id,"usd-v42");
    assert.equal(resolveStrategyModel(usdt,{},engine,"").id,"ust-v42");
    assert.equal(resolveStrategyModel(usd,{},engine,"ust-v42"),undefined);
    assert.equal(resolveStrategyModel(usdt,{},older,""),undefined);
    assert.equal(resolveStrategyModel(usd,{"usd-v41":{id:"usd-v41",engine:older,operationalReady:true}},engine,"usd-v41"),undefined);
    assert.equal(resolveStrategyModel(usd,{},engine,"").eligibleForLiveCandidate,false);
});

test("V4.2 preparation and independent draft requests preserve engine and currency", async () => {
    const calls=[]; const request=createCurrencyRequester(async (url,options) => {calls.push({url,body:JSON.parse(options.body)}); return {};},"csrf");
    const engine="adaptive_net_yield_v3";
    await request("USD","/api/research/v4/prepare",{method:"POST",body:JSON.stringify({engine})});
    await Promise.all([request("USD","/api/strategy/v3/draft",{method:"POST",body:JSON.stringify({strategyV3:{strategy_engine:engine,model_id:"usd-model",reprice_gain_apr:"0.25",passive_wait_minutes:"60"}})}),
        request("USDT","/api/strategy/v3/draft",{method:"POST",body:JSON.stringify({strategyV3:{strategy_engine:engine,model_id:"ust-model",reprice_gain_apr:"0.5",passive_wait_minutes:"30"}})})]);
    assert.equal(calls[0].body.engine,engine); assert.equal(calls[0].body.currency,"USD");
    assert.equal(calls[1].url,"/api/strategy/v4/draft"); assert.equal(calls[1].body.strategyV3.model_id,"usd-model");
    assert.equal(calls[2].body.currency,"USDT"); assert.equal(calls[2].body.strategyV3.passive_wait_minutes,"30");
});

test("nested decisions preserve zero probability and unknown remaining wait without inventing data", () => {
    const text=describeAdaptiveDecision({action:"KEEP",reason:"QUEUE_VALUE",offerId:123,assessment:{cycleEfficiencyGain:"0.0025",aprGain:"-0.0001",p10InterestGain:0,expectedFillProbability:0,remainingWaitMinutes:null,cumulativeWaitMinutes:75,confidence:"LOW",leaseExpiresAtMs:1791604800000}},"USD");
    assert.match(text,/下一周期效率提升 0\.25个百分点/); assert.doesNotMatch(text,/120天.*年化变化/);
    assert.match(text,/120天保守增量净利息 0\.0000 USD/);
    assert.match(text,/成交概率 0\.0%/); assert.match(text,/预计剩余等待 未知/); assert.match(text,/累计等待 75\.0分钟/);
    assert.match(text,/置信度 低/); assert.match(text,/等待到期 2026/);
});

test("recovery and unconfirmed cancel decisions have readable Chinese explanations", () => {
    for (const [reason, label] of [["CANCEL_NOT_EFFECTIVE","撤单尚未由账户确认"],
        ["RESTART_REPLACEMENT_BOUND","恢复已绑定的替代单"], ["REPLACEMENT_RETURN_CASH","资金归还可用余额"]]) {
        const text=describeAdaptiveDecision({action:"KEEP",reason},"USD");
        assert.ok(text.includes(label)); assert.doesNotMatch(text,/待核对原因/);
    }
});

test("unknown candidate estimates remain unknown instead of appearing as zero income or proven confidence", () => {
    const text=describeAdaptiveCandidate({period:2,amount:150,expectedFillProbability:0,confidence:"LOW"},"USDT");
    assert.match(text,/预期净利息 未知/); assert.match(text,/下一周期净收益效率 未知/);
    assert.match(text,/成交概率 0\.0%/); assert.match(text,/150\.00 USDT/); assert.doesNotMatch(text,/NaN|已校准/);
});

test("an unavailable control service cannot turn its cached stopped state into a confirmed stop", () => {
    const status={last_update:"2026-10-10 12:05:28"};
    const disconnected=describeControlState({running:false,stopReason:"USER_STOP",sourceFresh:true,dataAgeSeconds:0},status,false);
    assert.equal(disconnected.title,"运行状态未知"); assert.match(disconnected.detail,/不是当前状态/);
    const stopped=describeControlState({running:false,stopReason:"UNKNOWN_STOP",dataAgeSeconds:1000},status,true);
    assert.equal(stopped.title,"进程已停止"); assert.match(stopped.detail,/停止原因未知/); assert.equal(stopped.stale,true);
});

test("fresh process control and fresh account data are separate claims", () => {
    const status={last_update:"2026-10-10 12:05:28"};
    assert.equal(describeControlState({running:true,dataAgeSeconds:181},status).title,"进程运行 · 数据已过期");
    assert.equal(describeControlState({running:true,dataAgeSeconds:1,sourceFresh:false},status).stale,true);
    assert.equal(describeControlState({running:true,dataAgeSeconds:1,sourceFresh:true},status).title,"运行中");
    assert.equal(describeControlState(null,status).title,"运行状态未知");
});

test("existing loan income is never presented as verified income of new strategy loans", () => {
    const text=describeVersionPerformance({engine:"adaptive_net_yield_v3",newFillCount:0,newLoanPrincipal:0,netInterest:"99.99"},"USD");
    assert.match(text,/V4\.2/); assert.match(text,/新增成交 0 笔/); assert.match(text,/尚未精确归属/); assert.doesNotMatch(text,/99\.99/);
    const known=describeVersionPerformance({engine:"adaptive_net_yield_v3",newFillCount:1,newLoanPrincipal:150,netInterest:"0.01",interestAttribution:"EXACT"},"USDT");
    assert.match(known,/150\.00 USDT/); assert.match(known,/0\.0100 USDT/);
    assert.match(describeVersionPerformance(undefined,"USD"),/尚未完成归属统计/);
});

test("a polling response requested before a change cannot uncheck it", async () => {
    const read = deferred(), write = deferred(); let reads = 0;
    const model = new CurrencySettings(() => ++reads === 1 ? initial() : read.promise, () => write.promise);
    await model.refresh(); const polling = model.refresh(); const saving = model.change({enabled:true});
    read.resolve(initial()); await polling;
    assert.equal(model.desired.enabled,true); assert.equal(model.ready,false);
    write.resolve({enabled:true,autoTransfer:false}); await saving; assert.equal(model.ready,true);
});

test("a stale GET cannot overwrite a completed save", async () => {
    const read = deferred(); let reads=0;
    const model = new CurrencySettings(() => ++reads === 1 ? initial() : read.promise, async value => value);
    await model.refresh(); const polling = model.refresh(); await model.change({enabled:true});
    read.resolve(initial()); await polling;
    assert.equal(model.confirmed.enabled,true); assert.equal(model.desired.enabled,true);
});

test("rapid clicks serialize writes and retain the latest two checkbox values", async () => {
    const first = deferred(), second = deferred(), writes=[];
    const model = new CurrencySettings(async () => initial(), value => {writes.push(value); return writes.length === 1 ? first.promise : second.promise;});
    await model.refresh(); const saving=model.change({enabled:true}); model.change({autoTransfer:true}); model.change({enabled:false});
    assert.equal(writes.length,1); first.resolve(writes[0]); await tick();
    assert.deepEqual(writes[1],{enabled:false,autoTransfer:true});
    assert.deepEqual(model.desired,{enabled:false,autoTransfer:true}); assert.equal(model.ready,false);
    second.resolve(writes[1]); await saving;
    assert.equal(model.ready,true); assert.deepEqual(model.confirmed,model.desired);
});

test("failed save preserves desired values across polling and can be retried", async () => {
    let fail=true;
    const model = new CurrencySettings(async () => initial(), async value => {if(fail) throw new Error("offline"); return value;});
    await model.refresh(); await model.change({enabled:true}); await model.refresh();
    assert.equal(model.desired.enabled,true); assert.equal(model.confirmed.enabled,false);
    assert.equal(model.ready,false); assert.equal(model.error,"offline");
    fail=false; await model.save(); assert.equal(model.ready,true); assert.equal(model.error,null);
});

test("a timeout after saving is verified with a read rather than repeating the write", async () => {
    let remote=initial(), writes=0;
    const model = new CurrencySettings(async () => remote, async value => {remote=value; writes++; throw new Error("timeout");});
    await model.refresh(); await model.change({autoTransfer:true});
    assert.equal(writes,1); assert.equal(model.ready,true); assert.equal(model.confirmed.autoTransfer,true);
});

test("unavailable reconciliation preserves choices and blocks preflight", async () => {
    let unavailable=false;
    const model = new CurrencySettings(async () => {if(unavailable) throw new Error("network"); return initial();}, async () => {throw new Error("timeout");});
    await model.refresh(); unavailable=true; await model.change({enabled:true});
    assert.equal(model.desired.enabled,true); assert.equal(model.ready,false); assert.equal(model.error,"timeout");
});

test("a newer choice while verifying a timeout is not overwritten", async () => {
    const reconciliation=deferred(); let reads=0, writes=0;
    const model = new CurrencySettings(() => ++reads === 1 ? initial() : reconciliation.promise,
        async value => {if(++writes === 1) throw new Error("timeout"); return value;});
    await model.refresh(); const saving=model.change({enabled:true}); await tick(); model.change({autoTransfer:true});
    reconciliation.resolve({enabled:true,autoTransfer:false}); await saving;
    assert.equal(model.desired.autoTransfer,true); assert.equal(model.ready,true);
    assert.equal(writes,2); assert.equal(model.confirmed.autoTransfer,true);
});

test("an unavailable USDT save does not block USD settings", async () => {
    const pending=deferred();
    const usd=new CurrencySettings(async () => initial(), async value => value);
    const usdt=new CurrencySettings(async () => initial(), () => pending.promise);
    await Promise.all([usd.refresh(),usdt.refresh()]); const saving=usdt.change({enabled:true});
    await usd.change({autoTransfer:true}); assert.equal(usd.ready,true); assert.equal(usdt.ready,false);
    pending.resolve({enabled:true,autoTransfer:false}); await saving;
    assert.equal(usd.confirmed.enabled,false); assert.equal(usdt.confirmed.autoTransfer,false);
});

test("currency-scoped reads and writes never depend on a global selected coin", async () => {
    const calls=[]; const request=createCurrencyRequester(async (url,options) => {calls.push({url,options}); return {};},"csrf-token");
    await Promise.all([request("USD","/api/status"),request("USDT","/api/status")]);
    assert.equal(calls[0].url,"/api/status/v4?currency=USD"); assert.equal(calls[1].url,"/api/status/v4?currency=USDT");
    await request("USDT","/api/strategy/v3/apply",{method:"POST",body:JSON.stringify({draftVersionId:"ust-draft",applyToken:"ust-token"})});
    assert.deepEqual(JSON.parse(calls[2].options.body),{draftVersionId:"ust-draft",applyToken:"ust-token",currency:"USDT"});
    assert.equal(calls[2].options.headers["X-Mika-CSRF"],"csrf-token");
    await assert.rejects(request(undefined,"/api/status"));
});

test("preflight, start, pause and global stop retain the originating currency and token", async () => {
    const calls=[]; const request=createCurrencyRequester(async (url,options) => {calls.push([url,JSON.parse(options.body)]);},"csrf");
    for(const path of ["/api/control/preflight","/api/control/start","/api/runtime/v3/mode","/api/control/stop"]) {
        await request("USDT",path,{method:"POST",body:JSON.stringify({preflightId:"ust-only",mode:"PAUSED"})});
    }
    assert.equal(calls[0][0],"/api/control/v4/preflight"); assert.equal(calls[1][1].preflightId,"ust-only");
    assert.equal(calls[2][0],"/api/runtime/v4/mode"); assert.equal(calls[2][1].currency,"USDT");
    assert.equal(calls[3][0],"/api/control/v4/stop");
});

test("USD log filtering does not match USDT and general messages remain system logs", () => {
    assert.equal(logCurrency("USDT offer"),"USDT"); assert.equal(logCurrency("[USD] offer"),"USD");
    assert.equal(logCurrency("fUST market"),"USDT"); assert.equal(logCurrency("worker started"),"system");
    assert.equal(logCurrency({currency:"USDT",message:"offer"}),"USDT");
});

function confirmationHarness() {
    const cancel = {focus() {}}, confirm = {focus() {}, textContent:""};
    const dialog = {hidden:true, querySelector: () => cancel, querySelectorAll: () => [cancel,confirm]};
    const nodes = {strategyApplyDialog:dialog, strategyApplyTitle:{}, strategyApplySummary:{}, strategyApplyConfirmButton:confirm};
    const document = {querySelector: () => ({content:"csrf"}), getElementById: id => nodes[id],
        body:{style:{}}, activeElement:cancel};
    const window = {fetch:async () => ({}), addEventListener() {}};
    vm.runInNewContext(readFileSync(require.resolve("../www/v4-dashboard.js"),"utf8"),
        {window,document,AbortController,setTimeout,clearTimeout,URL});
    return {dashboard:window.mikaV4,document,nodes};
}

test("the shared strategy confirmation binds its currency and cancels the previous owner", async () => {
    const {dashboard,nodes}=confirmationHarness(); let closed=false;
    dashboard.dialogOwner={closeDialog() {closed=true;}};
    const usd=dashboard.confirmStrategy("USD","600 USD");
    assert.equal(closed,true); assert.equal(dashboard.dialogOwner.currency,"USD");
    const usdt=dashboard.confirmStrategy("USDT","700 USDT");
    assert.equal(await usd,false);
    assert.equal(nodes.strategyApplyTitle.textContent,"USDT · 应用策略确认");
    assert.equal(nodes.strategyApplySummary.textContent,"700 USDT");
    dashboard.dialogOwner.confirmStrategyApply();
    assert.equal(await usdt,true); assert.equal(nodes.strategyApplyDialog.hidden,true);
    assert.equal(dashboard.dialogOwner,null);
});

test("Escape cancels strategy confirmation without submitting and restores scrolling", async () => {
    const {dashboard,document}=confirmationHarness();
    const pending=dashboard.confirmStrategy("USDT","700 USDT"); let prevented=false;
    dashboard.dialogOwner.trapDialogKey({key:"Escape",preventDefault() {prevented=true;}});
    assert.equal(await pending,false); assert.equal(prevented,true);
    assert.equal(document.body.style.overflow,"");
});
