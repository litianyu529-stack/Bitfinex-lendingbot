"use strict";

function resolveStrategyModel(candidates, details, engine, modelId) {
    // An edited/frozen model takes precedence over the latest research candidate.
    return modelId ? details?.[modelId] : candidates?.[engine];
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

if (typeof module !== "undefined" && module.exports) module.exports = {CurrencySettings, createCurrencyRequester, logCurrency, resolveStrategyModel};

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
    window.mikaV4 = {contexts, scopeIds, resolveStrategyModel, dialogOwner: null,
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
