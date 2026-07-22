(async () => {
    "use strict";

    if (!(await (window.mikaBuildReady || Promise.resolve(false)))) return;

    const state = {
        policy: null, runtime: null, preview: null, previewPolicy: null, dirty: false,
        draftVersionId: null, applyToken: null,
    };
    const byId = (id) => document.getElementById(id);
    const surface = byId("v3StrategySurface");
    if (!surface) return;

    const groups = [
        {
            title: "期限资金池",
            description: "USD 全部本金按期限管理；资金池比例必须合计 100%。",
            fields: [
                ["short_share", "短期比例", "number", "%", { min: 0, max: 100, step: 1 }],
                ["medium_share", "中期比例", "number", "%", { min: 0, max: 100, step: 1 }],
                ["long_share", "长期比例", "number", "%", { min: 0, max: 100, step: 1 }],
                ["short_floor_apr", "短期最低净年化", "number", "%", { min: 0, step: 0.01, placeholder: "LIVE 必填", floor: "short" }],
                ["medium_floor_apr", "中期最低净年化", "number", "%", { min: 0, step: 0.01, placeholder: "LIVE 必填", floor: "medium" }],
                ["long_floor_apr", "长期最低净年化", "number", "%", { min: 0, step: 0.01, placeholder: "LIVE 必填", floor: "long" }],
                ["short_periods", "短期期限", "text", "天", {}],
                ["medium_periods", "中期期限", "text", "天", {}],
                ["long_periods", "长期期限", "text", "天", { readonly: true }],
            ],
        },
        {
            title: "切片与成交层",
            description: "实际切片数会根据全部本金与最低金额自动缩减。",
            fields: [
                ["target_slices", "目标切片数", "number", "笔", { min: 1, max: 100, step: 1 }],
                ["min_order_amount", "最低单笔金额", "number", "USD", { min: 150, step: 0.01 }],
                ["max_lend_amount", "最大放贷金额", "number", "USD", { min: 0, step: 0.01, placeholder: "不限制" }],
                ["max_lend_percent", "最大放贷比例", "number", "%", { min: 0, max: 100, step: 1 }],
                ["amount_jitter", "金额扰动", "number", "%", { min: 0, max: 10, step: 0.1 }],
                ["quick_share", "快速成交层", "number", "%", { min: 0, max: 100, step: 1 }],
                ["balanced_share", "平衡层", "number", "%", { min: 0, max: 100, step: 1 }],
                ["high_share", "高收益层", "number", "%", { min: 0, max: 100, step: 1 }],
                ["max_pool_shift", "动态比例最大偏移", "number", "百分点", { min: 0, max: 25, step: 1 }],
            ],
        },
        {
            title: "订单类型与费用",
            description: "普通 FRR 映射为零偏移 Variable，并共享 Variable 资金上限。",
            fields: [
                ["enable_limit", "LIMIT", "checkbox", "", {}],
                ["enable_frr", "FRR", "checkbox", "", {}],
                ["enable_frr_delta_fixed", "FRR Delta Fixed", "checkbox", "", {}],
                ["enable_frr_delta_variable", "FRR Delta Variable", "checkbox", "", {}],
                ["enable_hidden", "Hidden", "checkbox", "", {}],
                ["variable_max_share", "Variable最高占比", "number", "%", { min: 0, max: 100, step: 1 }],
                ["hidden_max_share", "Hidden最高占比", "number", "%", { min: 0, max: 100, step: 1 }],
                ["normal_fee_rate", "普通手续费", "number", "%", { min: 0, max: 99, step: 0.1 }],
                ["hidden_fee_rate", "Hidden手续费", "number", "%", { min: 0, max: 99, step: 0.1 }],
            ],
        },
        {
            title: "市场、撤挂与数据",
            details: true,
            fields: [
                ["minimum_offer_minutes", "最短挂单时间", "number", "分钟", { min: 1 }],
                ["reprice_cooldown_minutes", "重定价冷却", "number", "分钟", { min: 1 }],
                ["max_reprices_per_hour", "每小时重定价上限", "number", "次", { min: 0, max: 90 }],
                ["minimum_rate_change", "显著利率变化", "number", "%/日", { min: 0, step: 0.0001 }],
                ["iqr_change_fraction", "IQR阈值系数", "number", "", { min: 0, max: 2, step: 0.01 }],
                ["spike_volume_ratio", "尖峰成交量倍数", "number", "倍", { min: 1, step: 0.1 }],
                ["outlier_min_volume_share", "异常值最小量占比", "number", "%", { min: 0, max: 100, step: 0.1 }],
                ["ws_fallback_seconds", "实时断线降级时限", "number", "秒", { min: 30 }],
                ["rest_stale_seconds", "REST数据过期", "number", "秒", { min: 5 }],
                ["market_retention_days", "市场数据保留", "number", "天", { min: 1 }],
            ],
        },
        {
            title: "候选订单评分权重",
            details: true,
            description: "全部权重必须合计 100%。",
            fields: [
                ["score_net_yield", "净收益", "number", "%", { min: 0, max: 100 }],
                ["score_fill_probability", "成交概率", "number", "%", { min: 0, max: 100 }],
                ["score_wait_time", "预计等待", "number", "%", { min: 0, max: 100 }],
                ["score_book_depth", "盘口深度", "number", "%", { min: 0, max: 100 }],
                ["score_trade_speed", "成交速度", "number", "%", { min: 0, max: 100 }],
                ["score_utilization", "资金利用率", "number", "%", { min: 0, max: 100 }],
                ["score_trend_volatility", "趋势与波动", "number", "%", { min: 0, max: 100 }],
                ["score_term_opportunity", "期限机会成本", "number", "%", { min: 0, max: 100 }],
                ["score_hidden_cost", "Hidden成本", "number", "%", { min: 0, max: 100 }],
                ["score_variable_risk", "Variable风险", "number", "%", { min: 0, max: 100 }],
            ],
        },
    ];

    const allFields = groups.flatMap((group) => group.fields.map((field) => field[0]));
    const scoreFields = allFields.filter((name) => name.startsWith("score_"));

    function createField([name, label, type, unit, options]) {
        const wrapper = document.createElement("label");
        wrapper.className = type === "checkbox" ? "v3-check" : "v3-field";
        const input = document.createElement("input");
        input.name = name;
        input.type = type;
        for (const [key, value] of Object.entries(options || {})) {
            if (["floor"].includes(key)) continue;
            if (typeof value === "boolean") input[key] = value;
            else input.setAttribute(key, value);
        }
        if (type === "checkbox") {
            const text = document.createElement("span");
            text.textContent = label;
            wrapper.append(input, text);
        } else {
            const text = document.createElement("span");
            text.textContent = label;
            const control = document.createElement("div");
            control.append(input);
            if (unit) {
                const suffix = document.createElement("em");
                suffix.textContent = unit;
                control.append(suffix);
            }
            wrapper.append(text, control);
            if (options.floor) {
                const hint = document.createElement("small");
                hint.dataset.dailyFloor = options.floor;
                hint.textContent = "下单日利率 --";
                wrapper.append(hint);
            }
        }
        return wrapper;
    }

    function renderShell() {
        surface.innerHTML = `
            <div class="v3-mode-bar">
                <div><span>运行模式</span><strong id="v3Mode">PAUSED</strong></div>
                <div><span>ACTIVE</span><strong id="v3ActiveVersion">--</strong></div>
                <div><span>DRAFT</span><strong id="v3DraftVersion">--</strong></div>
                <div><span>PENDING</span><strong id="v3PendingVersion">--</strong></div>
                <div><span>数据源</span><strong id="v3DataSource">--</strong></div>
                <div><span>市场状态</span><strong id="v3Regime">--</strong></div>
                <div class="v3-mode-actions">
                    <button id="v3PauseButton" class="button secondary" type="button">暂停</button>
                    <button id="v3ReplayButton" class="button secondary" type="button">回放</button>
                    <button id="v3LiveButton" class="button danger" type="button">实盘预检</button>
                </div>
            </div>
            <div class="v3-workspace">
                <form id="v3StrategyForm" class="v3-editor"></form>
                <aside class="v3-monitor" aria-live="polite">
                    <section><h2>实时市场</h2><dl class="v3-metrics">
                        <div><dt>FRR日利率</dt><dd id="v3Frr">--</dd></div>
                        <div><dt>最高Bid</dt><dd id="v3BestBid">--</dd></div>
                        <div><dt>最低Offer</dt><dd id="v3BestOffer">--</dd></div>
                        <div><dt>资金使用率</dt><dd id="v3Utilization">--</dd></div>
                        <div><dt>组合本金</dt><dd id="v3Principal">--</dd></div>
                        <div><dt>计划订单</dt><dd id="v3PlanCount">--</dd></div>
                    </dl></section>
                    <section><h2>计划分布</h2><div id="v3PlanList" class="v3-plan-list"><p>等待预览</p></div></section>
                    <section><h2>实际统计</h2><div id="v3Stats" class="v3-stats"></div></section>
                </aside>
            </div>`;
        const form = byId("v3StrategyForm");
        for (const group of groups) {
            const section = document.createElement(group.details ? "details" : "section");
            section.className = "v3-section";
            const heading = document.createElement(group.details ? "summary" : "div");
            heading.className = "v3-section-heading";
            const title = document.createElement(group.details ? "span" : "h2");
            title.textContent = group.title;
            heading.append(title);
            if (group.description) {
                const description = document.createElement("p");
                description.textContent = group.description;
                heading.append(description);
            }
            const grid = document.createElement("div");
            grid.className = "v3-grid";
            group.fields.forEach((field) => grid.append(createField(field)));
            section.append(heading, grid);
            form.append(section);
        }
        const actions = document.createElement("div");
        actions.className = "v3-form-actions";
        actions.innerHTML = `<p id="v3FormMessage" role="status">等待载入 v3 配置</p><div>
            <button id="v3PreviewButton" class="button secondary" type="button">重新计算</button>
            <button id="v3DiscardButton" class="button secondary" type="button">放弃草稿</button>
            <button class="button primary" type="submit">保存草稿</button>
            <button id="v3ApplyButton" class="button danger" type="button">应用策略</button>
        </div>`;
        form.append(actions);
    }

    async function requestJson(path, options = {}) {
        const response = await fetch(path, { cache: "no-store", ...options });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || data.ok === false) throw new Error(data.error || `请求失败 (${response.status})`);
        return data;
    }

    function postJson(path, payload) {
        return requestJson(path, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload || {}),
        });
    }

    function input(name) {
        return byId("v3StrategyForm").elements[name];
    }

    function fillPolicy(policy) {
        state.policy = typeof structuredClone === "function"
            ? structuredClone(policy || {})
            : JSON.parse(JSON.stringify(policy || {}));
        for (const name of allFields) {
            const element = input(name);
            let value = policy?.[name];
            if (["short_periods", "medium_periods", "long_periods"].includes(name) && Array.isArray(value)) value = value.join(",");
            if (element.type === "checkbox") element.checked = Boolean(value);
            else element.value = value ?? "";
        }
        input("hidden_max_share").disabled = !input("enable_hidden").checked;
        state.dirty = false;
        updateDerived();
    }

    function collectPolicy() {
        const policy = {};
        for (const name of allFields) {
            const element = input(name);
            if (element.type === "checkbox") policy[name] = element.checked;
            else if (["short_periods", "medium_periods", "long_periods"].includes(name)) {
                policy[name] = element.value.split(",").map((value) => value.trim()).filter(Boolean).map(Number);
            } else policy[name] = element.value === "" ? null : element.value;
        }
        return policy;
    }

    function numberValue(name) {
        const value = Number(input(name)?.value);
        return Number.isFinite(value) ? value : 0;
    }

    function validateForm() {
        const poolTotal = numberValue("short_share") + numberValue("medium_share") + numberValue("long_share");
        const layerTotal = numberValue("quick_share") + numberValue("balanced_share") + numberValue("high_share");
        const scoreTotal = scoreFields.reduce((sum, name) => sum + numberValue(name), 0);
        if (poolTotal !== 100) throw new Error(`期限资金池比例当前为 ${poolTotal}%，必须等于100%`);
        if (layerTotal !== 100) throw new Error(`成交层比例当前为 ${layerTotal}%，必须等于100%`);
        if (scoreTotal !== 100) throw new Error(`评分权重当前为 ${scoreTotal}%，必须等于100%`);
        if (numberValue("min_order_amount") < 150) throw new Error("USD最低单笔金额不能低于150");
        if (input("enable_hidden").checked && numberValue("hidden_max_share") <= 0) throw new Error("启用Hidden时必须设置最高占比");
        if (![input("enable_limit"), input("enable_frr"), input("enable_frr_delta_fixed"), input("enable_frr_delta_variable")].some((item) => item.checked)) {
            throw new Error("至少启用一种Funding订单类型");
        }
    }

    function updateDerived() {
        const fee = numberValue("normal_fee_rate") / 100;
        for (const pool of ["short", "medium", "long"]) {
            const apr = numberValue(`${pool}_floor_apr`);
            const hint = surface.querySelector(`[data-daily-floor="${pool}"]`);
            hint.textContent = apr > 0 && fee < 1 ? `下单毛日利率 ≥ ${(apr / 365 / (1 - fee)).toFixed(6)}%` : "下单日利率 --";
        }
        input("hidden_max_share").disabled = !input("enable_hidden").checked;
        const poolTotal = numberValue("short_share") + numberValue("medium_share") + numberValue("long_share");
        const layerTotal = numberValue("quick_share") + numberValue("balanced_share") + numberValue("high_share");
        const scoreTotal = scoreFields.reduce((sum, name) => sum + numberValue(name), 0);
        byId("v3FormMessage").textContent = `资金池 ${poolTotal}% · 成交层 ${layerTotal}% · 评分 ${scoreTotal}%${state.dirty ? " · 未保存" : ""}`;
    }

    function percentDaily(value) {
        const number = Number(value);
        return Number.isFinite(number) ? `${(number * 100).toFixed(5)}%` : "--";
    }

    function renderPreview(data) {
        state.preview = data;
        const signals = data.signals || {};
        const plan = data.plan || {};
        byId("v3Regime").textContent = signals.regime || "--";
        byId("v3Frr").textContent = percentDaily(signals.frr_daily_rate);
        byId("v3BestBid").textContent = percentDaily(signals.best_bid);
        byId("v3BestOffer").textContent = percentDaily(signals.best_offer);
        byId("v3Utilization").textContent = signals.utilization == null ? "--" : `${(Number(signals.utilization) * 100).toFixed(1)}%`;
        byId("v3Principal").textContent = `${Number(data.principal || plan.planned_amount || 0).toLocaleString("zh-CN")} USD`;
        byId("v3PlanCount").textContent = `${(plan.plan || []).length} / ${plan.target_slice_count || 0}`;
        const basis = data.accountSnapshot || {};
        byId("v3DataSource").textContent = basis.stale
            ? `历史快照 · ${basis.timestamp ? new Date(basis.timestamp).toLocaleString("zh-CN", { hour12: false }) : "无数据"}`
            : "实时账户";
        const list = byId("v3PlanList");
        list.replaceChildren();
        const orders = plan.plan || [];
        if (!orders.length) {
            const empty = document.createElement("p");
            empty.textContent = (data.warnings || []).join("；") || "当前没有满足收益下限的候选订单";
            list.append(empty);
            return;
        }
        const summary = new Map();
        for (const order of orders) {
            const key = `${order.pool}|${order.layer}|${order.display_type}${order.hidden ? " Hidden" : ""}|${order.period}`;
            const row = summary.get(key) || { count: 0, amount: 0, rate: 0 };
            row.count += 1;
            row.amount += Number(order.amount);
            row.rate += Number(order.effective_rate);
            summary.set(key, row);
        }
        for (const [key, row] of summary) {
            const [pool, layer, type, period] = key.split("|");
            const item = document.createElement("div");
            item.className = "v3-plan-summary";
            item.innerHTML = `<strong>${pool} · ${layer}</strong><span>${type} · ${period}天 · ${row.count}笔</span><b>${row.amount.toFixed(2)} USD · ${((row.rate / row.count) * 100).toFixed(5)}%</b>`;
            list.append(item);
        }
    }

    function renderRuntime(data, status) {
        const runtime = data.runtime || {};
        state.runtime = runtime;
        byId("v3Mode").textContent = data.displayMode || runtime.mode || "PAUSED";
        byId("v3ActiveVersion").textContent = data.activeStrategy?.version_id || "--";
        byId("v3DraftVersion").textContent = data.draftStrategy?.version_id || "--";
        byId("v3PendingVersion").textContent = data.pendingStrategy?.version_id || "--";
        const market = status?.market || {};
        const marketData = status?.marketData || data.marketSnapshot || {};
        byId("v3DataSource").textContent = marketData.source || "--";
        if (market.regime) byId("v3Regime").textContent = market.regime;
        if (market.frr_daily_rate != null) byId("v3Frr").textContent = percentDaily(market.frr_daily_rate);
        if (market.best_bid != null) byId("v3BestBid").textContent = percentDaily(market.best_bid);
        if (market.best_offer != null) byId("v3BestOffer").textContent = percentDaily(market.best_offer);
        if (market.utilization != null) byId("v3Utilization").textContent = `${(Number(market.utilization) * 100).toFixed(1)}%`;
        if (status?.account?.total != null) byId("v3Principal").textContent = `${Number(status.account.total).toLocaleString("zh-CN")} USD`;
        if (runtime.mode === "SAFE" && runtime.safe_reason) {
            byId("v3FormMessage").textContent = `SAFE：${runtime.safe_reason}`;
        }
        byId("v3ApplyButton").disabled = !data.draftStrategy;
        byId("v3DiscardButton").disabled = !data.draftStrategy;
    }

    function renderStats(statistics) {
        const container = byId("v3Stats");
        container.replaceChildren();
        for (const key of ["7d", "30d", "90d", "all"]) {
            const row = statistics?.[key];
            if (!row) continue;
            const item = document.createElement("div");
            item.className = "v3-stat-summary";
            item.innerHTML = `<strong>${key === "all" ? "全部" : key}</strong><span>利用率 ${Number(row.utilizationPercent).toFixed(1)}%</span><span>净利息 ${Number(row.netInterest).toFixed(4)} USD</span><b>净APR ${Number(row.actualNetAprPercent).toFixed(2)}%</b>`;
            container.append(item);
        }
    }

    async function loadAll() {
        let config;
        try {
            config = await requestJson("/api/config");
            if (!state.dirty) fillPolicy(config.strategyV3Draft || config.strategyV3Pending || config.strategyV3);
        } catch (error) {
            byId("v3FormMessage").textContent = `配置载入失败：${error.message}`;
            return false;
        }

        const [runtimeResult, statusResult, statsResult] = await Promise.allSettled([
                requestJson("/api/runtime/v3"),
                requestJson("/api/status"),
                requestJson("/api/stats/v3"),
        ]);
        const runtime = runtimeResult.status === "fulfilled" ? runtimeResult.value : {};
        const status = statusResult.status === "fulfilled" ? statusResult.value : {};
        const stats = statsResult.status === "fulfilled" ? statsResult.value : {};
        renderRuntime(runtime, status);
        renderStats(stats.statistics);
        const failures = [runtimeResult, statusResult, statsResult]
            .filter((result) => result.status === "rejected")
            .map((result) => result.reason?.message || "未知错误");
        if (failures.length) {
            byId("v3FormMessage").textContent = `配置已载入；部分运行数据失败：${failures.join("；")}`;
        }
        return true;
    }

    async function preview() {
        try {
            validateForm();
            byId("v3FormMessage").textContent = "正在读取公共市场并生成计划…";
            const data = await postJson("/api/strategy/v3/preview", { strategyV3: collectPolicy() });
            renderPreview(data);
            state.previewPolicy = JSON.stringify(collectPolicy());
            byId("v3FormMessage").textContent = (data.warnings || []).join("；") || "预览完成";
        } catch (error) {
            byId("v3FormMessage").textContent = error.message;
        }
    }

    async function saveDraft(event) {
        event.preventDefault();
        try {
            validateForm();
            const strategyV3 = collectPolicy();
            byId("v3FormMessage").textContent = "正在生成影响预览…";
            const impact = await postJson("/api/strategy/v3/preview", { strategyV3 });
            renderPreview(impact);
            state.previewPolicy = JSON.stringify(strategyV3);
            const result = await postJson("/api/strategy/v3/draft", {
                strategyV3,
                previewToken: impact.previewToken,
            });
            state.dirty = false;
            state.draftVersionId = result.draftVersionId || null;
            state.applyToken = result.applyToken || null;
            byId("v3FormMessage").textContent = result.status === "DRAFT" ? "草稿已保存；预览后点击应用策略" : "策略已保存并生效";
            await loadAll();
        } catch (error) {
            byId("v3FormMessage").textContent = error.message;
        }
    }

    async function applyDraft() {
        try {
            if (!state.preview || state.previewPolicy !== JSON.stringify(collectPolicy())) {
                throw new Error("配置已变化，请先重新计算影响预览");
            }
            const plan = state.preview.plan || {};
            const accepted = window.confirm(
                `确认应用当前 V3 策略？\n` +
                `预计新计划 ${(plan.plan || []).length} 笔，金额 ${Number(plan.planned_amount || 0).toFixed(2)} USD。\n` +
                `将先撤销 ${(state.preview.incompatibleOffers || []).filter((row) => row.managed).length} 笔不兼容机器人挂单。\n` +
                `${(state.preview.nonChangeableCredits || []).length} 笔已成交贷款无法改变。`
            );
            if (!accepted) return;
            if (!state.draftVersionId || !state.applyToken) {
                throw new Error("应用确认已失效，请重新计算并保存草稿");
            }
            const result = await postJson("/api/strategy/v3/apply", {
                draftVersionId: state.draftVersionId,
                applyToken: state.applyToken,
            });
            state.applyToken = null;
            state.draftVersionId = null;
            byId("v3FormMessage").textContent = result.status === "PENDING" ? "策略已进入待应用状态，机器人将按撤挂节奏完成切换" : "策略已生效";
            await loadAll();
        } catch (error) {
            byId("v3FormMessage").textContent = error.message;
        }
    }

    async function setMode(mode) {
        try {
            const result = await postJson("/api/runtime/v3/mode", { mode });
            if (result.replay) renderPreview({ signals: result.replay.signals || {}, plan: { plan: result.replay.orders || [] }, replay: result.replay });
            await loadAll();
        } catch (error) {
            byId("v3FormMessage").textContent = error.message;
        }
    }

    async function discardDraft() {
        try {
            await postJson("/api/strategy/v3/discard", {});
            state.preview = null;
            state.previewPolicy = null;
            state.applyToken = null;
            state.draftVersionId = null;
            byId("v3FormMessage").textContent = "草稿已放弃，恢复 ACTIVE 策略";
            await loadAll();
        } catch (error) {
            byId("v3FormMessage").textContent = error.message;
        }
    }

    renderShell();
    byId("v3StrategyForm").addEventListener("input", () => { state.dirty = true; updateDerived(); });
    byId("v3StrategyForm").addEventListener("change", () => { state.dirty = true; updateDerived(); });
    byId("v3StrategyForm").addEventListener("submit", saveDraft);
    byId("v3PreviewButton").addEventListener("click", preview);
    byId("v3ApplyButton").addEventListener("click", applyDraft);
    byId("v3DiscardButton").addEventListener("click", discardDraft);
    byId("v3PauseButton").addEventListener("click", () => setMode("PAUSED"));
    byId("v3ReplayButton").addEventListener("click", () => setMode("REPLAY"));
    byId("v3LiveButton").addEventListener("click", () => byId("preflightButton")?.click());
    loadAll().then((loaded) => { if (loaded) preview(); });
    window.setInterval(loadAll, 15000);
})();
