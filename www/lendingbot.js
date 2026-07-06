const state = {
    config: null,
    status: null,
    control: null,
    mode: "dry",
    localOnly: window.location.protocol === "file:",
};

const formFieldNames = [
    "currencies",
    "mindailyrate",
    "maxdailyrate",
    "spreadlend",
    "gapbottom",
    "gaptop",
    "smartstrategy",
    "smartrateoffset",
    "smartfastdepth",
    "smartbalanceddepth",
    "smartopportunitydepth",
    "smartopportunitypremium",
    "repricestaleoffers",
    "repriceafterminutes",
    "xdaythreshold",
    "xdays",
    "minloansize",
    "sleeptimeactive",
    "sleeptimeinactive",
    "platformfeerate",
    "outputcurrency",
    "transferablecurrencies",
];

const fallbackConfig = {
    configPath: "default.cfg",
    credentialsConfigured: false,
    bitfinex: { currencies: "USD,UST" },
    bot: {
        sleeptimeactive: "60",
        sleeptimeinactive: "300",
        mindailyrate: "0.04",
        maxdailyrate: "2",
        spreadlend: "3",
        gapbottom: "10",
        gaptop: "200",
        smartstrategy: "true",
        smartrateoffset: "0.001",
        smartfastdepth: "5",
        smartbalanceddepth: "150",
        smartopportunitydepth: "300",
        smartopportunitypremium: "0.01",
        repricestaleoffers: "true",
        repriceafterminutes: "15",
        xdaythreshold: "0.2",
        xdays: "60",
        minloansize: "150",
        dryrunbalance: "300",
        platformfeerate: "15",
        outputcurrency: "USD",
        transferablecurrencies: "",
        jsonfile: "www/botlog.json",
        jsonlogsize: "200",
        startwebserver: "true",
    },
};

const fallbackStatus = {
    last_status: "还没有 botlog.json",
    last_update: "",
    log: [],
    outputCurrency: { currency: "USD", highestBid: "1" },
    platformFeeRate: "15",
    earnings: { available: false, summaryCurrency: "USD/UST" },
    raw_data: {},
};

function $(id) {
    return document.getElementById(id);
}

function showToast(message) {
    const toast = $("toast");
    toast.textContent = message;
    toast.classList.add("visible");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toast.classList.remove("visible"), 2600);
}

function setConnection(online, label) {
    const chip = $("connectionChip");
    chip.classList.toggle("offline", !online);
    chip.querySelector("span:last-child").textContent = label;
}

function modeLabel(mode) {
    if (mode === "live") return "实盘";
    if (mode === "dry") return "模拟";
    return "未知";
}

async function getJson(url) {
    const response = await fetch(url, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok || data.ok === false) {
        throw new Error(data.error || response.statusText);
    }
    return data;
}

async function postJson(url, body) {
    const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) {
        throw new Error(data.error || response.statusText);
    }
    return data;
}

async function loadConfig() {
    if (state.localOnly) {
        state.config = loadLocalConfig();
        renderConfig();
        return;
    }
    const config = await getJson("/api/config");
    state.config = config;
    saveLocalConfig(config);
    renderConfig();
}

async function loadStatus() {
    if (state.localOnly) {
        state.status = fallbackStatus;
        renderStatus();
        return;
    }
    const status = await getJson("/api/status");
    state.status = status;
    renderStatus();
}

async function loadControl() {
    if (state.localOnly) {
        state.control = { running: false, mode: null, pid: null, command: "" };
        renderControl();
        return;
    }
    state.control = await getJson("/api/control/status");
    renderControl();
}

function saveLocalConfig(config) {
    try {
        localStorage.setItem("lendingbot-config", JSON.stringify(config));
    } catch (error) {
        return;
    }
}

function loadLocalConfig() {
    try {
        const saved = JSON.parse(localStorage.getItem("lendingbot-config") || "null");
        return saved || fallbackConfig;
    } catch (error) {
        return fallbackConfig;
    }
}

function renderConfig() {
    const config = state.config || fallbackConfig;
    $("configPath").textContent = config.configPath || "default.cfg";
    $("credentialStatus").textContent = config.credentialsConfigured ? "已配置" : "未配置";
    const bot = config.bot || {};
    const bitfinex = config.bitfinex || {};
    const form = $("strategyForm");
    form.elements.currencies.value = bitfinex.currencies || "USD,UST";
    for (const name of formFieldNames) {
        if (name === "currencies") continue;
        const element = form.elements[name];
        if (element) {
            if (element.type === "checkbox") {
                element.checked = String(bot[name] ?? "false").toLowerCase() === "true";
            } else {
                element.value = bot[name] ?? "";
            }
        }
    }
    $("feeLabel").textContent = `${bot.platformfeerate || 15}% 平台费模型`;
    updateCommand();
}

function renderStatus() {
    const status = state.status || fallbackStatus;
    const rawData = status.raw_data || {};
    const currencies = Object.keys(rawData);
    $("statusText").textContent = translateBotText(status.last_status || "等待机器人状态");
    $("lastUpdate").textContent = status.last_update ? `最近更新 ${status.last_update}` : "暂无更新时间";
    $("logCount").textContent = `${(status.log || []).length} 条记录`;

    let totalCoins = 0;
    let totalLent = 0;
    let weightedRate = 0;
    for (const currency of currencies) {
        const item = rawData[currency] || {};
        const total = numberValue(item.totalCoins);
        const lent = numberValue(item.lentSum);
        const rate = numberValue(item.averageLendingRate);
        totalCoins += total;
        totalLent += lent;
        weightedRate += lent * rate;
    }
    const avgRate = totalLent > 0 ? weightedRate / totalLent : 0;
    $("totalCoins").textContent = formatNumber(totalCoins, 4);
    $("totalLent").textContent = formatNumber(totalLent, 4);
    $("currencyCount").textContent = `${currencies.length} 个币种`;
    $("lentRatio").textContent = `${formatNumber(totalCoins > 0 ? (totalLent / totalCoins) * 100 : 0, 2)}%`;
    $("averageRate").textContent = `${formatNumber(avgRate, 5)}%`;
    renderEarnings(status.earnings || fallbackStatus.earnings);
    renderCoins(rawData);
    renderLogs(status.log || []);
}

function renderEarnings(earnings) {
    const available = Boolean(earnings?.available);
    const currency = earnings?.summaryCurrency || "USD/UST";
    $("earningsCurrency").textContent = `${currency} 已入账`;
    if (!available) {
        $("earningsToday").textContent = "暂无数据";
        $("earningsSevenDays").textContent = "暂无数据";
        $("earningsThirtyDays").textContent = "暂无数据";
        $("earningsApy").textContent = "暂无数据";
        $("idleRatio").textContent = "暂无数据";
        return;
    }
    $("earningsToday").textContent = `${formatNumber(earnings.today, 8)} ${currency}`;
    $("earningsSevenDays").textContent = `${formatNumber(earnings.sevenDays, 8)} ${currency}`;
    $("earningsThirtyDays").textContent = `${formatNumber(earnings.thirtyDays, 8)} ${currency}`;
    $("earningsApy").textContent = `${formatNumber(earnings.thirtyDayApy, 2)}%`;
    $("idleRatio").textContent = `${formatNumber(earnings.idleRatio, 2)}%`;
}

function renderControl() {
    const control = state.control || { running: false };
    const button = $("startBotButton");
    const label = button.querySelector("span");
    const status = $("botProcessStatus");
    button.classList.toggle("running", Boolean(control.running));
    button.disabled = state.localOnly;
    if (control.running) {
        status.textContent = `运行中 ${modeLabel(control.mode)}，PID ${control.pid}`;
        label.textContent = "停止机器人";
        button.querySelector("svg path").setAttribute("d", "M7 7h10v10H7z");
    } else {
        status.textContent = control.stopReason === "stopped_by_dashboard"
            ? "已由控制台停止"
            : control.returnCode === null || control.returnCode === undefined
            ? "已停止"
            : `已停止，退出码 ${control.returnCode}`;
        label.textContent = state.mode === "live" ? "启动实盘" : "启动模拟";
        button.querySelector("svg path").setAttribute("d", "M8 5v14l11-7z");
    }
}

function renderCoins(rawData) {
    const list = $("coinList");
    list.innerHTML = "";
    const currencies = Object.keys(rawData || {});
    if (!currencies.length) {
        list.innerHTML = '<div class="empty-state">还没有资金钱包数据。</div>';
        return;
    }
    for (const currency of currencies.sort()) {
        const data = rawData[currency] || {};
        const total = numberValue(data.totalCoins);
        const lent = numberValue(data.lentSum);
        const maxToLend = numberValue(data.maxToLend || total);
        const avgRate = numberValue(data.averageLendingRate);
        const marketRate = numberValue(data.marketDailyRate);
        const smartRate = numberValue(data.smartDailyRate);
        const strategyMode = data.strategyMode === "smart" ? "智能策略" : "固定策略";
        const openOfferCount = numberValue(data.openOfferCount);
        const staleOfferCount = numberValue(data.staleOfferCount);
        const openOfferSum = numberValue(data.openOfferSum);
        const progress = total > 0 ? Math.min(100, Math.max(0, (lent / total) * 100)) : 0;
        const row = document.createElement("article");
        row.className = "coin-row";
        row.innerHTML = `
            <div class="coin-code">${escapeHtml(currency)}</div>
            <div>
                <div class="coin-progress"><span style="width:${progress}%"></span></div>
                <div class="coin-meta">已放贷 ${formatNumber(lent, 4)} / ${formatNumber(total, 4)}，可放贷上限 ${formatNumber(maxToLend, 4)}</div>
                <div class="coin-meta">${strategyMode} · 市场 ${formatNumber(marketRate, 5)}% · 底价 ${formatNumber(smartRate, 5)}%</div>
                <div class="coin-meta">等待挂单 ${formatNumber(openOfferSum, 4)}，新挂单 ${openOfferCount} 个，超时重定价 ${staleOfferCount} 个</div>
            </div>
            <div class="coin-stat"><span>日利率</span><strong>${formatNumber(avgRate, 5)}%</strong></div>
            <div class="coin-stat"><span>已放贷</span><strong>${formatNumber(progress, 2)}%</strong></div>
        `;
        list.appendChild(row);
    }
}

function renderLogs(logs) {
    const stream = $("logStream");
    const filter = $("logFilter").value.trim().toLowerCase();
    const filtered = logs
        .slice()
        .reverse()
        .filter((line) => !filter || String(line).toLowerCase().includes(filter))
        .slice(0, 160);
    stream.innerHTML = "";
    if (!filtered.length) {
        stream.innerHTML = '<div class="empty-state">没有匹配的日志。</div>';
        return;
    }
    for (const line of filtered) {
        const item = document.createElement("div");
        item.className = "log-line";
        item.textContent = translateBotText(line);
        stream.appendChild(item);
    }
}

function numberValue(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
}

function formatNumber(value, decimals) {
    return numberValue(value).toLocaleString(undefined, {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
    });
}

function escapeHtml(value) {
    return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
}

function translateBotText(value) {
    let text = String(value || "");
    const replacements = [
        [/No botlog\.json yet/g, "还没有 botlog.json"],
        [/Lended:/g, "已放贷："],
        [/No Bitfinex API credentials configured; using simulated dry-run balances\./g, "没有配置 Bitfinex API 密钥，正在使用模拟余额运行。"],
        [/dry-run mode: no Bitfinex write endpoints will be called/g, "模拟运行模式：不会调用 Bitfinex 写入接口"],
        [/dashboard mode: bot will start only after pressing the web start button/g, "控制台模式：点击网页启动按钮后才会启动机器人"],
        [/Welcome to Bitfinex Lending Bot \(DRY-RUN\)/g, "欢迎使用 Bitfinex 自动放贷机器人（模拟运行）"],
        [/Welcome to Bitfinex Lending Bot \(LIVE\)/g, "欢迎使用 Bitfinex 自动放贷机器人（实盘运行）"],
        [/Bitfinex API key\/secret are required for --live/g, "实盘模式需要配置 Bitfinex API key/secret"],
        [/Started WebServer at/g, "网页控制台已启动："],
        [/Failed to start WebServer:/g, "网页控制台启动失败："],
        [/Stopping WebServer/g, "正在停止网页控制台"],
        [/Failed to stop WebServer:/g, "停止网页控制台失败："],
        [/ERROR:/g, "错误："],
        [/\bbye\b/g, "已退出"],
        [/dry-run: skipping configured wallet transfers/g, "模拟运行：跳过已配置的钱包自动转入。"],
        [/The handshake operation timed out/g, "握手操作超时"],
    ];
    for (const [pattern, replacement] of replacements) {
        text = text.replace(pattern, replacement);
    }
    text = text.replace(
        /Placing ([0-9.]+) ([A-Z]+) at ([0-9.]+)% for ([0-9]+) days\.\.\. dry-run/g,
        "挂出 $1 $2，日利率 $3%，周期 $4 天，模拟运行"
    );
    text = text.replace(
        /Canceling all ([A-Z]+) offers\.\.\. dry-run, would cancel ([0-9.]+)/g,
        "取消所有 $1 挂单，模拟运行，将取消 $2"
    );
    text = text.replace(
        /dry-run, would reprice ([0-9]+) stale offers totaling ([0-9.]+)/g,
        "模拟运行，将重定价 $1 个超时挂单，合计 $2"
    );
    text = text.replace(
        /The lower rate found on ([A-Z]+) is ([0-9.]+)% vs conditional rate ([0-9.]+)%\. Lending ([0-9.]+) of ([0-9.]+) available\./g,
        "$1 当前低利率为 $2%，条件利率为 $3%。将在可用 $5 中放贷 $4。"
    );
    text = text.replace(
        /Error fetching public funding book for ([A-Z]+):/g,
        "读取 $1 公共资金盘口失败："
    );
    text = text.replace(
        /Error fetching active funding offers for ([A-Z]+):/g,
        "读取 $1 当前挂单失败："
    );
    text = text.replace(
        /Error fetching active funding (loans|credits) for ([A-Z]+):/g,
        "读取 $2 已放贷资金失败（$1）："
    );
    text = text.replace(
        /([A-Z]+) disabled in coinconfig; skipping\./g,
        "$1 已在 coinconfig 中禁用，跳过。"
    );
    text = text.replace(
        /([A-Z]+): available ([0-9.]+) is below minimum offer ([0-9.]+); skipping\./g,
        "$1：可放贷 $2 低于最小挂单金额 $3，跳过。"
    );
    return text;
}

function collectFormConfig() {
    const form = $("strategyForm");
    const bot = {};
    for (const name of formFieldNames) {
        if (name === "currencies") continue;
        const element = form.elements[name];
        if (element) {
            bot[name] = element.type === "checkbox" ? String(element.checked) : element.value.trim();
        }
    }
    bot.jsonfile = "www/botlog.json";
    bot.jsonlogsize = "200";
    bot.startwebserver = "true";
    return {
        bitfinex: {
            currencies: form.elements.currencies.value.trim().toUpperCase(),
        },
        bot,
    };
}

async function saveStrategy(event) {
    event.preventDefault();
    await persistStrategy(false);
}

async function persistStrategy(silent) {
    const message = $("formMessage");
    message.classList.remove("error");
    if (!silent) message.textContent = "正在保存...";
    const payload = collectFormConfig();
    try {
        if (state.localOnly) {
            state.config = { ...(state.config || fallbackConfig), ...payload };
            saveLocalConfig(state.config);
        } else {
            const saved = await postJson("/api/config", payload);
            state.config = saved.config;
        }
        renderConfig();
        if (!silent) {
            message.textContent = "已保存。";
            showToast("策略已保存");
        }
        return true;
    } catch (error) {
        message.classList.add("error");
        message.textContent = error.message;
        showToast("保存失败");
        return false;
    }
}

async function toggleBotProcess() {
    const button = $("startBotButton");
    button.disabled = true;
    try {
        if (state.control?.running) {
            const stopped = await postJson("/api/control/stop", {});
            state.control = stopped.bot;
            renderControl();
            showToast("机器人已停止");
            await loadStatus();
            return;
        }

        if (state.mode === "live" && !$("ackLive").checked) {
            showToast("请先确认 Bitfinex 权限");
            return;
        }
        const saved = await persistStrategy(true);
        if (!saved) return;
        const started = await postJson("/api/control/start", {
            mode: state.mode,
            confirmLive: $("ackLive").checked,
        });
        state.control = started.bot;
        renderControl();
        showToast(state.mode === "live" ? "实盘机器人已启动" : "模拟机器人已启动");
        await loadStatus();
    } catch (error) {
        showToast(error.message);
    } finally {
        button.disabled = state.localOnly;
        renderControl();
    }
}

function configSnippet() {
    const payload = collectFormConfig();
    const bot = payload.bot;
    return `[BITFINEX]
currencies = ${payload.bitfinex.currencies}

[BOT]
sleeptimeactive = ${bot.sleeptimeactive}
sleeptimeinactive = ${bot.sleeptimeinactive}
mindailyrate = ${bot.mindailyrate}
maxdailyrate = ${bot.maxdailyrate}
spreadlend = ${bot.spreadlend}
gapbottom = ${bot.gapbottom}
gaptop = ${bot.gaptop}
smartstrategy = ${bot.smartstrategy}
smartrateoffset = ${bot.smartrateoffset}
smartfastdepth = ${bot.smartfastdepth}
smartbalanceddepth = ${bot.smartbalanceddepth}
smartopportunitydepth = ${bot.smartopportunitydepth}
smartopportunitypremium = ${bot.smartopportunitypremium}
repricestaleoffers = ${bot.repricestaleoffers}
repriceafterminutes = ${bot.repriceafterminutes}
xdaythreshold = ${bot.xdaythreshold}
xdays = ${bot.xdays}
minloansize = ${bot.minloansize}
platformfeerate = ${bot.platformfeerate}
outputCurrency = ${bot.outputcurrency}
jsonfile = ${bot.jsonfile}
jsonlogsize = ${bot.jsonlogsize}
startWebServer = ${bot.startwebserver}`;
}

function updateCommand() {
    const config = state.config || fallbackConfig;
    const modeFlag = state.mode === "live" ? "--live" : "--dryrun";
    const pieces = ["python", "lendingbot.py", modeFlag];
    if ($("commandServer").checked) pieces.push("--server");
    if ($("commandJson").checked) pieces.push("--json", config.bot?.jsonfile || "www/botlog.json", "--jsonsize", config.bot?.jsonlogsize || "200");
    if ($("commandOnce").checked) pieces.push("--once");
    $("commandOutput").textContent = pieces.join(" ");
}

async function copyText(text, label) {
    try {
        await navigator.clipboard.writeText(text);
        showToast(`${label}已复制`);
        return;
    } catch (error) {
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.setAttribute("readonly", "");
        textarea.style.position = "fixed";
        textarea.style.left = "-9999px";
        document.body.appendChild(textarea);
        textarea.select();
        const copied = document.execCommand("copy");
        document.body.removeChild(textarea);
        showToast(copied ? `${label}已复制` : `请手动复制${label}`);
    }
}

async function refreshAll() {
    try {
        await Promise.all([loadConfig(), loadStatus(), loadControl()]);
        setConnection(!state.localOnly, state.localOnly ? "文件模式" : "已连接");
    } catch (error) {
        setConnection(false, "离线");
        showToast(error.message);
        if (!state.config) {
            state.config = fallbackConfig;
            renderConfig();
        }
        if (!state.status) {
            state.status = fallbackStatus;
            renderStatus();
        }
    }
}

function bindEvents() {
    $("refreshButton").addEventListener("click", refreshAll);
    $("reloadConfigButton").addEventListener("click", loadConfig);
    $("strategyForm").addEventListener("submit", saveStrategy);
    $("copyCommandButton").addEventListener("click", () => copyText($("commandOutput").textContent, "命令"));
    $("copyConfigButton").addEventListener("click", () => copyText(configSnippet(), "配置"));
    $("logFilter").addEventListener("input", () => renderLogs((state.status || fallbackStatus).log || []));
    for (const id of ["commandServer", "commandJson", "commandOnce"]) {
        $(id).addEventListener("change", updateCommand);
    }
    for (const button of document.querySelectorAll(".mode-option")) {
        button.addEventListener("click", () => {
            state.mode = button.dataset.mode;
            document.querySelectorAll(".mode-option").forEach((item) => item.classList.toggle("active", item === button));
            updateCommand();
            renderControl();
        });
    }
    $("startBotButton").addEventListener("click", toggleBotProcess);
    $("ackLive").addEventListener("change", (event) => {
        const liveButton = document.querySelector('.mode-option[data-mode="live"]');
        liveButton.disabled = !event.target.checked;
        if (state.mode === "live" && liveButton.disabled) {
            document.querySelector('.mode-option[data-mode="dry"]').click();
        }
    });
    document.querySelector('.mode-option[data-mode="live"]').disabled = true;
}

bindEvents();
refreshAll();
window.setInterval(loadStatus, 30000);
window.setInterval(loadControl, 5000);
