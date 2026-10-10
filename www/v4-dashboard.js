"use strict";

function resolveStrategyModel(candidates, details, engine, modelId) {
    // An edited/frozen model takes precedence over the latest research candidate.
    const model = modelId ? details?.[modelId] : candidates?.[engine];
    return model?.engine && model.engine !== engine ? undefined : model;
}

const strategyEngineNames = {
    legacy_v3: "V3 旧策略", adaptive_net_yield_v1: "V4.0 策略",
    adaptive_net_yield_v2: "V4.1 策略", adaptive_net_yield_v3: "V4.2 策略",
};

function strategyEngineProfile(engine) {
    return {name: strategyEngineNames[engine] || "未知策略", adaptive: String(engine || "").startsWith("adaptive_net_yield_"),
        multi: ["adaptive_net_yield_v2", "adaptive_net_yield_v3"].includes(engine),
        repricing: engine === "adaptive_net_yield_v3"};
}

function validateRepricingInputs(gain, minutes) {
    if (gain == null || gain === "" || !Number.isFinite(Number(gain)) || Number(gain) < 0 || Number(gain) > 100)
        throw new Error("调价收益门槛必须填写0～100之间的数字");
    if (minutes == null || minutes === "" || !Number.isInteger(Number(minutes)) || Number(minutes) < 15 || Number(minutes) > 360)
        throw new Error("被动等待时限必须是15～360分钟之间的整数");
}

function displayMetric(value, scale, digits, suffix) {
    return value == null || value === "" || !Number.isFinite(Number(value)) ? "未知" : `${(Number(value) * scale).toFixed(digits)}${suffix}`;
}

function adaptiveReasonLabel(reason) {
    const labels = {
        KEEP: "保持", CANCEL: "撤单调整", SUBMIT: "提交新单", WAIT: "等待", REPRICE: "重新报价",
        QUEUE_VALUE: "保留报价的净收益更高", MINIMUM_AGE: "未达最短保留时间",
        MINIMUM_AGE_OR_AMOUNT: "未达最短保留时间或剩余金额不足", VALUE_GAIN: "调价收益优势成立",
        CYCLE_VALUE_GAIN: "下一资金周期的收益效率更高", CYCLE_EFFICIENCY_GAIN: "下一资金周期的收益效率更高",
        LEASE_EXPIRED: "等待时限已到，重新比较报价", PASSIVE_WAIT_EXPIRED: "等待时限已到，重新比较报价",
        LEASE_ROLLOVER: "等待到期，按新报价重新规划", LONG_HORIZON_GUARD: "120天收益保护未通过",
        PASSIVE_LEASE_RENEW: "出现新的独立需求证据，延长被动等待", VALUATION_INCOMPLETE: "估值未完成，本轮禁止写入",
        MODEL_OR_DATA_UNAVAILABLE: "模型或市场数据未就绪，本轮禁止写入",
        HARD_FLOOR: "报价违反收益底线，安全调整", CAP_EXCEEDED: "超过资金上限，安全调整",
        EXTERNAL_OFFER: "外部挂单尚未接管", EVALUATION_INTERVAL: "等待下一评估周期",
        NO_BETTER_VALUE: "暂未发现更高净收益的可执行报价", WAIT_FOR_VALUE: "当前可执行报价的净收益优势不足",
        NO_AVAILABLE_BALANCE: "没有可用Funding余额", BELOW_MINIMUM: "余额低于最小订单金额",
        MARKET_BELOW_FLOOR: "市场参考报价低于收益底线", FUNDING_CAP_REACHED: "已达资金上限",
        FRR_DATA_UNAVAILABLE: "FRR数据未就绪，暂不执行", MODEL_UNAVAILABLE: "模型未就绪，暂不执行",
        REPRICE_COOLDOWN: "调价冷却中", CONFIRMATION_REQUIRED: "等待第二次优势确认",
        REPRICE_BUDGET: "已达到每小时调价预算", REQUEST_BUDGET: "等待账户写入预算",
        INVALID_OFFER_PARAMETERS: "报价参数不合法，已阻止提交", KNOWN_REJECTED_QUOTE: "该报价已被明确拒绝，等待参数变化",
        INSUFFICIENT_DEMAND: "可确认的兼容借款需求不足", SAFETY_REPRICE: "订单合法性安全调整",
        CANCEL_NOT_EFFECTIVE: "撤单尚未由账户确认，保留资金锁定并继续核对",
        RESTART_REPLACEMENT_BOUND: "恢复已绑定的替代单，等待账户确认后提交",
        REPLACEMENT_RETURN_CASH: "替代报价不再合格，资金归还可用余额",
    };
    return reason ? labels[reason] || `待核对原因（${reason}）` : "未提供原因";
}

function describeAdaptiveDecision(row, currency) {
    const assessment = row.assessment || row;
    const old = assessment.current || assessment.keep || {};
    const next = assessment.replacement || assessment.candidate || {};
    const lease = assessment.leaseExpiresAtMs ?? row.leaseExpiresAtMs;
    const leaseDate = lease == null ? null : new Date(Number(lease));
    return `${row.offerId ? `挂单 ${row.offerId} · ` : `${currency} · `}${adaptiveReasonLabel(row.action)} · ${adaptiveReasonLabel(row.reason)}`
        + ` · 下一周期效率提升 ${displayMetric(assessment.cycleEfficiencyGain ?? row.cycleEfficiencyGain, 100, 2, "个百分点")}`
        + ` · 120天保守增量净利息 ${displayMetric(assessment.p10InterestGain ?? row.p10InterestGain, 1, 4, ` ${currency}`)}`
        + ` · 成交概率 ${displayMetric(assessment.expectedFillProbability ?? assessment.fillProbability ?? next.expectedFillProbability ?? old.expectedFillProbability, 100, 1, "%")}`
        + ` · 预计剩余等待 ${displayMetric(assessment.remainingWaitMinutes ?? assessment.expectedWaitMinutes ?? next.remainingWaitMinutes ?? old.remainingWaitMinutes, 1, 1, "分钟")}`
        + ` · 累计等待 ${displayMetric(assessment.cumulativeWaitMinutes ?? row.cumulativeWaitMinutes, 1, 1, "分钟")}`
        + ` · 等待到期 ${leaseDate && Number.isFinite(leaseDate.getTime()) ? leaseDate.toLocaleString("zh-CN", {timeZone:"Asia/Shanghai", hour12:false}) : "未知"}`
        + ` · 置信度 ${({LOW:"低", MEDIUM:"中", HIGH:"高"})[assessment.confidence ?? next.confidence ?? old.confidence] || "未校准"}`;
}

function describeAdaptiveCandidate(row, currency) {
    return `${row.display_type || "LIMIT"} · ${row.period}天 · 报价净年化 ${displayMetric(row.netApr, 100, 2, "%")}`
        + ` · 下一周期净收益效率 ${displayMetric(row.cycleEfficiencyNetApr ?? row.cycleNetApr, 100, 2, "%")}`
        + ` · 120天保守资金时间年化 ${displayMetric(row.conservativeNetApr, 100, 2, "%")}`
        + ` · 预期净利息 ${displayMetric(row.expectedNetInterest, 1, 2, ` ${currency}`)} / ${displayMetric(row.amount, 1, 2, ` ${currency}`)}`
        + ` · 成交概率 ${displayMetric(row.expectedFillProbability, 100, 1, "%")}`
        + ` · 预计等待 ${displayMetric(row.expectedWaitMinutes, 1, 1, "分钟")}`
        + ` · 预计持有 ${displayMetric(row.expectedHoldingHours, 1, 1, "小时")}`
        + ` · ${({LOW:"低置信度", MEDIUM:"中置信度", HIGH:"高置信度"})[row.confidence] || "置信度未校准"}${row.rateRiskNote ? ` · ${row.rateRiskNote}` : ""}`;
}

function describeVersionPerformance(performance, currency) {
    if (!performance) return "当前策略新增成交：尚未完成归属统计。全账户利息包含升级前已有贷款，不能代表新策略成交。";
    const name = strategyEngineNames[performance.engine] || "当前策略";
    const count = performance.newFillCount ?? performance.newLoanCount;
    const principal = performance.newLoanPrincipal ?? performance.newFillPrincipal;
    const net = performance.interestAttribution === "EXACT" && performance.netInterest != null
        ? `可归属净利息 ${displayMetric(performance.netInterest, 1, 4, ` ${currency}`)}` : "新贷款净利息尚未精确归属";
    return `${name} · 新增成交 ${count == null ? "未知" : count} 笔 · 新增贷款本金 ${displayMetric(principal, 1, 2, ` ${currency}`)} · ${net}。全账户收益包含升级前已有贷款。`;
}

function describeControlState(control, status, connected = true, nowMs = Date.now()) {
    const sync = status?.last_update || "尚无同步记录";
    const parsed = Date.parse(String(status?.last_update || "").replace(" ", "T"));
    const fallbackAge = Number.isFinite(parsed) ? Math.max(0, (nowMs - parsed) / 1000) : Infinity;
    const age = control?.dataAgeSeconds == null ? fallbackAge : Number(control.dataAgeSeconds);
    const stale = control?.sourceFresh === false || !Number.isFinite(age) || age > 180;
    if (!connected || !control || typeof control.running !== "boolean")
        return {title:"运行状态未知", detail:`本地控制服务未连接；最后同步 ${sync}。页面保留的金额和挂单不是当前状态。`, stale:true, age};
    if (!control.running) {
        const labels = {UNKNOWN_STOP:"停止原因未知", USER_STOP:"用户停止", GLOBAL_STOP:"用户停止两币", PROCESS_EXIT:"进程退出", PROCESS_EXITED:"进程退出", WATCHDOG_STOP:"守护进程停止", START_FAILED:"启动失败"};
        const reason = labels[control.stopReason] || (control.stopReason ? `停止记录：${control.stopReason}` : "停止原因未知");
        const event = control.lastLifecycleEvent;
        const eventTime = event?.atMs ?? event?.createdAtMs ?? event?.timestampMs;
        const date = eventTime ? new Date(Number(eventTime)) : null;
        return {title:"进程已停止", detail:`${reason}${date && Number.isFinite(date.getTime()) ? ` · ${date.toLocaleString("zh-CN", {timeZone:"Asia/Shanghai", hour12:false})}` : ""}；最后同步 ${sync}。重新启动须先通过实盘预检。`, stale, age};
    }
    return {title: stale ? "进程运行 · 数据已过期" : "运行中", detail:`最后同步 ${sync}${Number.isFinite(age) ? ` · ${Math.floor(age)}秒前` : ""}。`, stale, age};
}

function createCurrencyRequester(fetcher, csrf) {
    const routes = {
        "/api/config": "/api/config/v4", "/api/status": "/api/status/v4",
        "/api/runtime/v3": "/api/runtime/v4", "/api/stats/v3": "/api/stats/v4",
        "/api/control/status": "/api/control/v4/status", "/api/control/preflight": "/api/control/v4/preflight",
        "/api/control/start": "/api/control/v4/start", "/api/control/stop": "/api/control/v4/stop",
    };
    return async (currency, path, options = {}) => {
        if (!["USD", "USDT"].includes(currency)) throw new Error("请求缺少明确币种");
        path = routes[path] || path.replace("/api/strategy/v3/", "/api/strategy/v4/")
            .replace("/api/runtime/v3/", "/api/runtime/v4/");
        if (options.method === "POST") {
            options = {...options, headers: {...options.headers, "Content-Type": "application/json", "X-Mika-CSRF": csrf},
                body: JSON.stringify({...JSON.parse(options.body || "{}"), currency})};
        } else {
            const url = new URL(path, "http://localhost"); url.searchParams.set("currency", currency);
            path = url.pathname + url.search;
        }
        const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 20000);
        try { return await fetcher(path, {cache: "no-store", ...options, signal: options.signal || controller.signal}); }
        finally {clearTimeout(timer);}
    };
}

class CurrencySettings {
    constructor(read, write, changed = () => {}) {
        this.read = read; this.write = write; this.changed = changed;
        this.confirmed = null; this.desired = null;
        this.revision = 0; this.savedRevision = 0; this.epoch = 0;
        this.saving = false; this.error = null; this.task = null;
    }
    get ready() { return Boolean(this.confirmed) && !this.saving && !this.error && this.revision === this.savedRevision; }
    async refresh() {
        const revision = this.revision, epoch = this.epoch, data = await this.read();
        if (revision === this.revision && epoch === this.epoch && !this.saving && !this.error
            && this.revision === this.savedRevision) {
            this.confirmed = data; this.desired = {enabled: data.enabled, autoTransfer: data.autoTransfer}; this.changed();
        }
        return this.confirmed;
    }
    change(patch) {
        if (!this.confirmed) throw new Error("配置尚未载入");
        this.desired = {...this.desired, ...patch}; this.revision += 1; this.error = null; this.changed();
        return this.save();
    }
    save() {
        if (this.task) return this.task;
        this.task = this.drain().finally(() => {this.task = null;}); return this.task;
    }
    async drain() {
        this.saving = true; this.error = null; this.changed();
        try {
            while (this.savedRevision < this.revision) {
                const revision = this.revision, desired = {...this.desired};
                try {
                    const data = await this.write(desired);
                    this.confirmed = data; this.savedRevision = revision; this.epoch += 1;
                } catch (error) {
                    this.error = error.message || String(error);
                    // A timeout can happen after saving; read before retrying.
                    try {
                        const data = await this.read(); this.confirmed = data; this.epoch += 1;
                        if (data.enabled === this.desired.enabled && data.autoTransfer === this.desired.autoTransfer) {
                            this.savedRevision = this.revision; this.error = null;
                        }
                    } catch (_) { /* Preserve the desired settings while their result is unconfirmed. */ }
                    if (this.error) break;
                }
            }
        } finally {this.saving = false; this.changed();}
    }
}

function logCurrency(line) {
    if (line && typeof line === "object" && ["USD", "USDT"].includes(line.currency)) return line.currency;
    const text = typeof line === "object" ? String(line.message || "") : String(line);
    if (/\b(?:USDT|fUST|UST)\b/i.test(text)) return "USDT";
    return /\b(?:USD|fUSD)\b/i.test(text) ? "USD" : "system";
}

if (typeof module !== "undefined" && module.exports) module.exports = {CurrencySettings, createCurrencyRequester, logCurrency, resolveStrategyModel,
    strategyEngineProfile, validateRepricingInputs, adaptiveReasonLabel, describeAdaptiveDecision, describeAdaptiveCandidate, describeVersionPerformance, describeControlState};

if (typeof window !== "undefined") {
    const contexts = {}, csrf = document.querySelector('meta[name="mika-dashboard-csrf"]')?.content || "";
    let logs = [];
    function scopeIds(root, currency) {
        for (const node of root.querySelectorAll("[id]")) {
            if (node.dataset.ref) continue;
            node.dataset.ref = node.id; node.id = `${currency}-${node.id}`;
        }
        for (const node of root.querySelectorAll("[aria-labelledby], [aria-describedby], [for]")) {
            for (const attr of ["aria-labelledby", "aria-describedby", "for"]) {
                const value = node.getAttribute(attr);
                if (value && !value.startsWith(`${currency}-`)) node.setAttribute(attr, value.split(" ").map(id => `${currency}-${id}`).join(" "));
            }
        }
    }
    function renderLogs() {
        const currency = document.getElementById("logCurrency").value;
        const search = document.getElementById("logFilter").value.trim().toLowerCase(), stream = document.getElementById("logStream");
        stream.replaceChildren();
        for (const line of logs) {
            const text = typeof line === "object" ? String(line.message || "") : String(line);
            if ((currency !== "all" && logCurrency(line) !== currency) || !text.toLowerCase().includes(search)) continue;
            const node = document.createElement("div"); node.className = "log-line"; node.textContent = text; stream.append(node);
        }
        if (!stream.childElementCount) {const node = document.createElement("p"); node.className = "log-empty";
            node.textContent = "暂无匹配日志；未标记币种的系统日志可在“全部”查看。"; stream.append(node);}
    }
    window.mikaV4 = {contexts, scopeIds, resolveStrategyModel, strategyEngineProfile, validateRepricingInputs,
        adaptiveReasonLabel, describeAdaptiveDecision, describeAdaptiveCandidate, describeVersionPerformance, describeControlState, dialogOwner: null,
        request: createCurrencyRequester(window.fetch.bind(window), csrf),
        receiveLogs(_currency, lines) {logs = Array.isArray(lines) ? lines : []; renderLogs();},
        confirmStrategy(currency, message) {
            window.mikaV4.dialogOwner?.closeDialog();
            const dialog = document.getElementById("strategyApplyDialog"), focus = document.activeElement;
            document.getElementById("strategyApplyTitle").textContent = `${currency} · 应用策略确认`;
            document.getElementById("strategyApplySummary").textContent = message;
            document.getElementById("strategyApplyConfirmButton").textContent = `确认应用 ${currency} 策略`;
            dialog.hidden = false; document.body.style.overflow = "hidden";
            return new Promise(resolve => {
                const owner = {currency, closeDialog(accepted = false) {
                    dialog.hidden = true; document.body.style.overflow = "";
                    if (window.mikaV4.dialogOwner === owner) window.mikaV4.dialogOwner = null;
                    focus?.focus(); resolve(accepted === true);
                }, trapDialogKey(event) {
                    if (event.key === "Escape") {event.preventDefault(); owner.closeDialog();}
                    if (event.key === "Tab") {
                        const buttons = [...dialog.querySelectorAll("button")], index = buttons.indexOf(document.activeElement);
                        event.preventDefault(); buttons[(index + (event.shiftKey ? -1 : 1) + buttons.length) % buttons.length].focus();
                    }
                }, confirmStrategyApply() {owner.closeDialog(true);} };
                window.mikaV4.dialogOwner = owner; dialog.querySelector("button").focus();
            });
        },
    };
    async function json(currency, path, options) {
        const response = await window.mikaV4.request(currency, path, options), data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || "请求失败"); return data;
    }
    function renderRoute() {
        const route = ["overview", "strategy", "logs"].includes(location.hash.slice(1)) ? location.hash.slice(1) : "overview";
        for (const name of ["overview", "strategy", "logs"]) {
            const selected = name === route, tab = document.getElementById(`${name}Tab`);
            document.getElementById(`${name}Panel`).hidden = !selected;
            tab.setAttribute("aria-selected", String(selected)); tab.tabIndex = selected ? 0 : -1;
        }
        if (route === "overview") requestAnimationFrame(() => Object.values(contexts).forEach(ctx => ctx.overview.drawDistribution()));
    }
    async function initialize() {
        if (!(await window.mikaBuildReady)) return;
        for (const currency of ["USD", "USDT"]) {
            const root = document.createElement("article"); root.id = `overview-${currency}`;
            root.className = "currency-overview"; root.dataset.currency = currency; root.setAttribute("aria-label", `${currency} 放贷总览`);
            root.append(document.getElementById("currencyOverviewTemplate").content.cloneNode(true)); scopeIds(root, currency);
            root.querySelector('[data-role="currency-title"]').textContent = currency;
            root.querySelector('[data-ref="totalCurrency"]').textContent = currency;
            root.querySelector('[data-role="enable-label"]').textContent = `启用 ${currency} 放贷`;
            root.querySelectorAll("[data-currency-unit]").forEach(node => {node.textContent = `${currency} · 真实入账`;});
            root.querySelector(".rail-state small").textContent = `${currency} 运行状态`;
            document.getElementById("dualOverview").append(root);
            const ctx = contexts[currency] = {currency, root, runtime: {}, overview: null};
            ctx.renderSettings = () => {
                const model = ctx.settings, locked = ctx.runtime.operationMode === "LIVE" || ctx.runtime.recovery?.active;
                for (const input of root.querySelectorAll("[data-setting]")) {
                    input.disabled = !model.confirmed || !ctx.runtime.operationMode || Boolean(locked);
                    if (model.desired) input.checked = model.desired[input.dataset.setting];
                }
                const note = root.querySelector('[data-role="settings-status"]');
                note.textContent = model.saving ? "保存中…" : model.error ? `保存失败：${model.error}；选择尚未确认。`
                    : locked ? "先暂停该币种再修改设置。" : model.confirmed ? "已保存 · 启动实盘仍需预检。" : "正在读取设置…";
                note.classList.toggle("save-error", Boolean(model.error));
                root.querySelector('[data-role="settings-retry"]').hidden = !model.error || Boolean(locked);
                ctx.overview?.settingsChanged();
                ctx.strategy?.renderControls();
            };
            ctx.settings = new CurrencySettings(() => json(currency, "/api/config/v4"),
                desired => json(currency, "/api/config/v4", {method:"POST", body:JSON.stringify(desired)}), ctx.renderSettings);
            root.querySelectorAll("[data-setting]").forEach(input => input.addEventListener("change", () => {
                ctx.settings.change({[input.dataset.setting]:input.checked});
            }));
            root.querySelector('[data-role="settings-retry"]').addEventListener("click", () => ctx.settings.save());
            ctx.overview = window.createCurrencyOverview(root, currency, ctx);
            const strategy = document.createElement("article"); strategy.id = `strategy-${currency}`; strategy.className = "currency-strategy";
            strategy.dataset.currency = currency; strategy.setAttribute("aria-label", `${currency} 策略设置`);
            const title = document.createElement("h2"); title.className = "currency-heading"; title.textContent = `${currency} · 独立策略`;
            const surface = document.createElement("div"); surface.className = "v3-strategy-surface";
            strategy.append(title, surface); document.getElementById("dualStrategies").append(strategy);
            ctx.strategy = await window.createCurrencyStrategy(surface, currency, ctx);
        }
        document.getElementById("refreshButton").addEventListener("click", () => Object.values(contexts).forEach(ctx => {
            ctx.overview.refreshAll(true); ctx.strategy.loadAll();
        }));
        document.getElementById("globalStopButton").addEventListener("click", () => contexts.USD.overview.openStop());
        document.getElementById("confirmStartButton").addEventListener("click", () => window.mikaV4.dialogOwner?.confirmStart());
        document.getElementById("strategyApplyConfirmButton").addEventListener("click", () => window.mikaV4.dialogOwner?.confirmStrategyApply());
        document.getElementById("confirmStopButton").addEventListener("click", async () => {
            await window.mikaV4.dialogOwner?.confirmStop(); Object.values(contexts).forEach(ctx => ctx.overview.refreshAll());
        });
        document.getElementById("goStrategyButton").addEventListener("click", () => window.mikaV4.dialogOwner?.goStrategy());
        document.addEventListener("keydown", event => window.mikaV4.dialogOwner?.trapDialogKey(event));
        document.querySelectorAll("[data-close-dialog]").forEach(button => button.addEventListener("click", () => window.mikaV4.dialogOwner?.closeDialog()));
        document.querySelectorAll(".dialog-backdrop").forEach(backdrop => backdrop.addEventListener("mousedown", event => {
            if (event.target === backdrop) window.mikaV4.dialogOwner?.closeDialog();
        }));
        for (const id of ["logFilter", "logCurrency"]) document.getElementById(id).addEventListener("input", renderLogs);
        window.addEventListener("hashchange", renderRoute);
        document.querySelector(".tabs").addEventListener("keydown", event => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            const tabs = [...document.querySelectorAll('.tabs [role="tab"]')], index = tabs.indexOf(document.activeElement);
            if (index < 0) return; event.preventDefault();
            const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
                : (index + (event.key === "ArrowLeft" ? -1 : 1) + tabs.length) % tabs.length;
            tabs[next].focus(); tabs[next].click();
        });
        renderRoute();
    }
    window.addEventListener("DOMContentLoaded", initialize);
}
