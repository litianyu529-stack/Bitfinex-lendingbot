"use strict";

window.mikaV4 = {
    currency: "USD",
    async request(path, options = {}) {
        const currency = this.currency;
        const routes = {
            "/api/config": "/api/config/v4", "/api/status": "/api/status/v4",
            "/api/runtime/v3": "/api/runtime/v4", "/api/stats/v3": "/api/stats/v4",
            "/api/control/status": "/api/control/v4/status",
            "/api/control/preflight": "/api/control/v4/preflight",
            "/api/control/start": "/api/control/v4/start", "/api/control/stop": "/api/control/v4/stop",
        };
        path = routes[path] || path.replace("/api/strategy/v3/", "/api/strategy/v4/")
            .replace("/api/runtime/v3/", "/api/runtime/v4/");
        if (options.method === "POST") {
            const payload = JSON.parse(options.body || "{}");
            options = { ...options, body: JSON.stringify({ ...payload, currency }) };
        } else if (path !== "/api/health") {
            path += `${path.includes("?") ? "&" : "?"}currency=${encodeURIComponent(currency)}`;
        }
        const response = await fetch(path, { cache: "no-store", ...options });
        if (currency !== this.currency) throw new Error("币种已切换，请等待新数据。");
        const readJson = response.json.bind(response);
        response.json = async () => {
            const data = await readJson();
            if (currency !== this.currency) throw new Error("币种已切换，请等待新数据。");
            return data;
        };
        return response;
    },
};

(() => {
    const selector = document.getElementById("v4Currency");
    const enabled = document.getElementById("v4Enabled");
    const transfer = document.getElementById("v4Transfer");
    const message = document.getElementById("v4SettingsMessage");
    const csrf = document.querySelector('meta[name="mika-dashboard-csrf"]')?.content || "";

    async function refreshOverview() {
        try {
            const currency = window.mikaV4.currency;
            const response = await fetch(`/api/config/v4?currency=${currency}`, { cache: "no-store" });
            const config = await response.json();
            if (!response.ok || config.ok === false) throw new Error(config.error || "读取币种配置失败");
            if (currency !== window.mikaV4.currency) return;
            enabled.checked = config.enabled;
            transfer.checked = config.autoTransfer;
            const statuses = await Promise.all(["USD", "USDT"].map(async (coin) => {
                const reply = await fetch(`/api/status/v4?currency=${coin}`, { cache: "no-store" });
                const data = await reply.json();
                if (!reply.ok || data.ok === false) throw new Error(data.error || "读取币种状态失败");
                return [coin, data];
            }));
            for (const [coin, data] of statuses) {
                const node = document.getElementById(`v4Overview${coin}`);
                const available = data.snapshotAvailable !== false && data.account;
                const total = available ? Number(data.account.total || 0).toFixed(2) : "—";
                const mode = data.recovery?.active ? "恢复中" : data.operationMode || "PAUSED";
                node.textContent = `${coin} · ${mode} · 总资金 ${total} ${coin}`;
            }
        } catch (error) {
            message.textContent = error.message;
        }
    }

    selector.addEventListener("change", () => {
        const event = new CustomEvent("mika:before-currency-change", { cancelable: true });
        if (!window.dispatchEvent(event)) {
            selector.value = window.mikaV4.currency;
            return;
        }
        window.mikaV4.currency = selector.value;
        document.querySelectorAll(".dialog-backdrop").forEach((node) => { node.hidden = true; });
        message.textContent = "";
        document.querySelectorAll("[data-currency-unit]").forEach((node) => {
            node.textContent = `${selector.value} · 真实入账`;
        });
        window.dispatchEvent(new Event("mika:currency-change"));
        refreshOverview();
    });
    document.getElementById("v4SaveSettings").addEventListener("click", async () => {
        try {
            const response = await window.mikaV4.request("/api/config/v4", {
                method: "POST", headers: { "Content-Type": "application/json", "X-Mika-CSRF": csrf },
                body: JSON.stringify({ enabled: enabled.checked, autoTransfer: transfer.checked }),
            });
            const data = await response.json();
            if (!response.ok || data.ok === false) throw new Error(data.error || "保存失败");
            message.textContent = "币种设置已保存。启动实盘前仍需预检。";
            window.dispatchEvent(new Event("mika:currency-settings-change"));
        } catch (error) { message.textContent = error.message; }
    });
    document.getElementById("v4PauseCurrency").addEventListener("click", async () => {
        try {
            const response = await window.mikaV4.request("/api/runtime/v4/mode", {
                method: "POST", headers: { "Content-Type": "application/json", "X-Mika-CSRF": csrf },
                body: JSON.stringify({ mode: "PAUSED" }),
            });
            const data = await response.json();
            if (!response.ok || data.ok === false) throw new Error(data.error || "暂停失败");
            message.textContent = `${window.mikaV4.currency} 已暂停；已有挂单保留。`;
            window.dispatchEvent(new Event("mika:currency-settings-change"));
            refreshOverview();
        } catch (error) { message.textContent = error.message; }
    });
    refreshOverview();
    window.setInterval(refreshOverview, 30000);
})();
