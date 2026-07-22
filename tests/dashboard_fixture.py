"""Read-only local fixture used for browser and responsive dashboard acceptance."""

import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
WWW = os.path.join(ROOT, "www")

CONFIG = {
    "configPath": os.path.join(ROOT, "tests", "fixture.cfg"),
    "credentialsConfigured": True,
    "bitfinex": {"currencies": "USD,UST"},
    "bot": {
        "sleeptimeactive": "60",
        "sleeptimeinactive": "300",
        "mindailyrate": "0.04",
        "maxdailyrate": "2",
        "spreadlend": "3",
        "gapbottom": "10",
        "gaptop": "200",
        "xdaythreshold": "0.2",
        "xdays": "60",
        "minloansize": "150",
        "platformfeerate": "15",
        "maxtolent": "10000",
        "maxpercenttolent": "80",
        "maxtolentrate": "0.05",
        "transferablecurrencies": "",
        "transferfromwallets": "exchange,margin",
        "outputcurrency": "USD",
        "jsonfile": "www/botlog.json",
        "jsonlogsize": "200",
        "startwebserver": "true",
        "smartstrategy": "true",
        "smartrateoffset": "0.008",
        "smartfastdepth": "5",
        "smartbalanceddepth": "150",
        "smartopportunitydepth": "300",
        "smartopportunitypremium": "0.01",
        "smartfastshare": "50",
        "smartlongshare": "40",
        "smartfloordepth": "2",
        "smartlongperiod": "true",
        "smartlongwaitminutes": "120",
        "repricestaleoffers": "true",
        "repriceafterminutes": "15",
        "repriceminratedelta": "0.002",
    },
}

V3_POLICY = {
    "short_share": "50",
    "medium_share": "30",
    "long_share": "20",
    "quick_share": "40",
    "balanced_share": "40",
    "high_share": "20",
    "short_floor_apr": "6",
    "medium_floor_apr": "8",
    "long_floor_apr": "10",
    "short_periods": [2, 3, 5, 7],
    "medium_periods": [8, 14, 21, 30],
    "long_periods": [120],
    "target_slices": 60,
    "min_order_amount": "150",
    "max_lend_amount": None,
    "max_lend_percent": "100",
    "amount_jitter": "3",
    "max_pool_shift": "10",
    "normal_fee_rate": "15",
    "hidden_fee_rate": "18",
    "enable_limit": True,
    "enable_frr": False,
    "enable_frr_delta_fixed": False,
    "enable_frr_delta_variable": False,
    "variable_max_share": "10",
    "enable_hidden": False,
    "hidden_max_share": None,
    "minimum_offer_minutes": 10,
    "reprice_cooldown_minutes": 15,
    "max_reprices_per_hour": 6,
    "minimum_rate_change": "0.002",
    "iqr_change_fraction": "0.25",
    "spike_volume_ratio": "1.5",
    "outlier_min_volume_share": "0.5",
    "ws_fallback_seconds": 300,
    "rest_stale_seconds": 60,
    "market_retention_days": 90,
}
CONFIG["strategyV3"] = dict(V3_POLICY)
CONFIG["strategyV3Draft"] = None
CONFIG["strategyV3Pending"] = None
CONFIG["strategyV3State"] = {
    "active": {
        "version_id": "fixture-active-v3",
        "status": "ACTIVE",
        "policy": dict(V3_POLICY),
    },
    "draft": None,
    "pending": None,
}

STRATEGY_BASE = {
    "version": 2,
    "profile": "balanced_yield",
    "auto_order_types": True,
    "replay_window": "7d",
    "fast_share": "50",
    "balanced_share": "10",
    "long_share": "40",
    "fast_period": 2,
    "balanced_period": 7,
    "long_period": 60,
    "fast_wait_minutes": "10",
    "balanced_wait_minutes": "30",
    "long_wait_minutes": "120",
    "rate_offset": "0.001",
    "long_premium": "0.01",
    "fast_depth": "5",
    "balanced_depth": "150",
    "long_depth": "300",
    "floor_depth": "2",
    "trend_min_delta": "0.002",
    "utilization_low": "65",
    "utilization_high": "85",
    "reprice_min_delta": "0.002",
    "fast_order_type": "AUTO",
    "balanced_order_type": "AUTO",
    "long_order_type": "AUTO",
    "fast_frr_offset": "0",
    "balanced_frr_offset": "0",
    "long_frr_offset": "0",
}


def preset(profile, fast, long, periods, waits, premium):
    policy = dict(STRATEGY_BASE)
    policy.update(
        {
            "profile": profile,
            "fast_share": str(fast),
            "balanced_share": str(100 - fast - long),
            "long_share": str(long),
            "fast_period": periods[0],
            "balanced_period": periods[1],
            "long_period": periods[2],
            "fast_wait_minutes": str(waits[0]),
            "balanced_wait_minutes": str(waits[1]),
            "long_wait_minutes": str(waits[2]),
            "long_premium": premium,
        }
    )
    return policy


CONFIG["strategy"] = {
    "version": 2,
    "autoMigrated": False,
    "global": dict(STRATEGY_BASE),
    "overrides": {},
    "profiles": {
        "utilization": preset("utilization", 65, 10, (2, 7, 30), (5, 15, 60), "0.005"),
        "balanced_yield": preset("balanced_yield", 50, 40, (2, 7, 60), (10, 30, 120), "0.01"),
        "yield": preset("yield", 30, 60, (2, 14, 90), (15, 60, 240), "0.02"),
    },
}

STATUS = {
    "schemaVersion": 2,
    "operationMode": "live",
    "last_status": "已放贷：[9,760.00 USD] · 3 笔挂单等待成交",
    "last_update": "2026-07-18 16:20:08",
    "outputCurrency": {"currency": "USD", "highestBid": "1"},
    "platformFeeRate": "15",
    "raw_data": {
        "USD": {
            "totalCoins": "12480",
            "lentSum": "9760",
            "openOfferSum": "1520",
            "walletAvailable": "1200",
            "marketDailyRate": "0.0149",
            "smartDailyRate": "0.0159",
            "averageLendingRate": "0.0145",
        },
    },
    "earnings": {
        "available": True,
        "summaryCurrency": "USD",
        "today": "4.28",
        "sevenDays": "30.12",
        "thirtyDays": "118.64",
        "thirtyDayApy": "11.56",
        "idleRatio": "9.62",
        "byCurrency": {},
        "error": "",
    },
    "openOffers": [
        {
            "id": "92100341",
            "currency": "USD",
            "created": 1784361908000,
            "updated": 1784361908000,
            "amount": "500",
            "rate": "0.0001499",
            "dailyRatePercent": "0.01499",
            "period": 2,
            "offerType": "LIMIT",
            "bucket": "fast",
            "managedByBot": True,
            "status": "ACTIVE",
        },
        {
            "id": "92100342",
            "currency": "USD",
            "created": 1784363708000,
            "updated": 1784363708000,
            "amount": "520",
            "rate": "0.00014",
            "dailyRatePercent": "0.014",
            "period": 7,
            "offerType": "FRRDELTAFIX",
            "bucket": "balanced",
            "managedByBot": True,
            "status": "ACTIVE",
        },
        {
            "id": "92100343",
            "currency": "USD",
            "created": 1784365508000,
            "updated": 1784365508000,
            "amount": "500",
            "rate": "0.000137",
            "dailyRatePercent": "0.0137",
            "period": 60,
            "offerType": "LIMIT",
            "bucket": None,
            "managedByBot": False,
            "status": "ACTIVE",
        },
    ],
    "log": [
        "2026-07-18 16:20:08 实盘同步完成：钱包、放贷和挂单状态已更新",
        "2026-07-18 16:20:07 USD 当前市场日利率 0.0149%，策略底价 0.0159%",
        "2026-07-18 16:20:06 已读取 3 笔活跃挂单",
    ],
}

CONTROL = {
    "running": False,
    "pid": None,
    "startedAt": None,
    "returnCode": None,
    "stopReason": None,
}
EXPIRE_NEXT = False


def preflight_payload():
    global EXPIRE_NEXT
    auto_transfer = bool(CONFIG["bot"].get("transferablecurrencies", "").strip())
    token = "expired-token" if EXPIRE_NEXT else "fixture-token"
    EXPIRE_NEXT = False
    payload = {
        "ok": True,
        "preflightId": None if auto_transfer else token,
        "expiresAt": "2026-07-18T16:21:08+08:00",
        "canStart": not auto_transfer,
        "checks": [
            {
                "id": "config",
                "label": "策略配置",
                "status": "pass",
                "detail": "配置格式与策略参数有效",
            },
            {
                "id": "credentials",
                "label": "API 凭据",
                "status": "pass",
                "detail": "凭据有效，权限接口可访问",
            },
            {
                "id": "wallets_read",
                "label": "钱包读取权限",
                "status": "pass",
                "detail": "wallets 读取权限已启用",
            },
            {
                "id": "funding_write",
                "label": "放贷写入权限",
                "status": "pass",
                "detail": "funding 读写权限已启用",
            },
            {
                "id": "withdraw_disabled",
                "label": "提现权限",
                "status": "pass",
                "detail": "withdraw 与 ui_withdraw 写入权限均已关闭",
            },
            {
                "id": "book_usd",
                "label": "USD 资金盘口",
                "status": "pass",
                "detail": "盘口可访问",
            },
        ],
        "warnings": [],
        "summary": {
            "strategyVersion": 3,
            "strategySource": "SQLITE_ACTIVE",
            "activeStrategyVersion": "fixture-active-v3",
            "policyHash": "fixture-active-v3",
            "enabledOrderTypes": ["LIMIT"],
            "onlyLimit": True,
            "policy": dict(V3_POLICY),
            "accountSnapshot": {
                "source": "REAL_ACCOUNT",
                "timestamp": 1784391608000,
                "stale": False,
            },
            "account": {
                "total": "12480",
                "wallet": "1200",
                "offers": "1520",
                "credits": "9760",
            },
            "fundingPools": {
                "short": {
                    "share": "50",
                    "netFloorAprPercent": "6",
                    "periods": [2, 3, 5, 7],
                },
                "medium": {
                    "share": "30",
                    "netFloorAprPercent": "8",
                    "periods": [8, 14, 21, 30],
                },
                "long": {"share": "20", "netFloorAprPercent": "10", "periods": [120]},
            },
            "executionLayers": {"quick": "40", "balanced": "40", "high": "20"},
            "fundingLimit": {
                "maxAmount": None,
                "maxPercent": "100",
                "effectiveCap": "12480",
                "existingExposure": "11280",
                "capRemaining": "1200",
            },
            "targetSlices": 60,
            "actualSlices": 2,
            "planHash": "fixture-plan-hash",
            "strategyPlan": [
                {
                    "display_type": "LIMIT",
                    "amount": "600",
                    "period": 2,
                    "submitted_rate": "0.00015",
                    "flags": 0,
                },
                {
                    "display_type": "LIMIT",
                    "amount": "600",
                    "period": 14,
                    "submitted_rate": "0.00018",
                    "flags": 0,
                },
            ],
            "pendingCancellations": [],
            "externalIncompatibilities": [],
            "nonChangeableCredits": [],
        },
    }
    if auto_transfer:
        payload["checks"].insert(
            4,
            {
                "id": "wallets_write",
                "label": "钱包转账权限",
                "status": "fail",
                "detail": "已启用自动转入，但 wallets 写入权限未启用",
            },
        )
    return payload


class FixtureHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WWW, **kwargs)

    def log_message(self, format, *args):
        return

    def send_json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        global EXPIRE_NEXT
        path = urlparse(self.path).path
        if path == "/api/config":
            return self.send_json(CONFIG)
        if path == "/api/status":
            return self.send_json(STATUS)
        if path == "/api/control/status":
            return self.send_json(CONTROL)
        if path == "/api/runtime/v3":
            state = CONFIG["strategyV3State"]
            return self.send_json(
                {
                    "ok": True,
                    "runtime": {"mode": "LIVE" if CONTROL["running"] else "PAUSED"},
                    "displayMode": "PENDING" if state["pending"] else ("LIVE" if CONTROL["running"] else "PAUSED"),
                    "activeStrategy": state["active"],
                    "draftStrategy": state["draft"],
                    "pendingStrategy": state["pending"],
                    "effectiveStrategy": state["active"],
                    "policy": CONFIG["strategyV3"],
                    "incompatibleOffers": [],
                }
            )
        if path == "/api/stats/v3":
            return self.send_json({"ok": True, "statistics": STATUS.get("statistics", {})})
        if path == "/api/health":
            return self.send_json(
                {
                    "ok": True,
                    "service": "mika-lending-dashboard-v3",
                    "buildId": "fixture-build-v3",
                    "pid": os.getpid(),
                    "projectRoot": ROOT,
                    "configPath": CONFIG["configPath"],
                }
            )
        if path in {"/", "/lendingbot.html"}:
            with open(os.path.join(ROOT, "www", "lendingbot.html"), "rb") as file:
                data = file.read().replace(b"__MIKA_DASHBOARD_BUILD_ID__", b"fixture-build-v3")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/api/fixture/expire":
            EXPIRE_NEXT = True
            return self.send_json({"ok": True, "nextStart": "expired"})
        return super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        payload = self.read_json()
        if path == "/api/config":
            return self.send_json(
                {"ok": False, "code": "V2_STRATEGY_DISABLED", "error": "V2 disabled"},
                410,
            )
        if path == "/api/strategy/v3/preview":
            policy = dict(CONFIG["strategyV3"])
            policy.update(payload.get("strategyV3", {}))
            plan = [
                {
                    "pool": "short",
                    "layer": "quick",
                    "amount": "600",
                    "period": 2,
                    "offer_type": "LIMIT",
                    "display_type": "LIMIT",
                    "submitted_rate": "0.00015",
                    "effective_rate": "0.00015",
                    "flags": 0,
                    "hidden": False,
                },
                {
                    "pool": "medium",
                    "layer": "balanced",
                    "amount": "600",
                    "period": 14,
                    "offer_type": "LIMIT",
                    "display_type": "LIMIT",
                    "submitted_rate": "0.00018",
                    "effective_rate": "0.00018",
                    "flags": 0,
                    "hidden": False,
                },
            ]
            return self.send_json(
                {
                    "ok": True,
                    "currency": "USD",
                    "principal": "12480",
                    "available": "1200",
                    "accountSnapshot": {
                        "source": "REAL_ACCOUNT",
                        "timestamp": 1784391608000,
                        "stale": False,
                    },
                    "activeVersion": "fixture-active-v3",
                    "proposedVersion": "fixture-proposed-v3",
                    "strategyDiff": [],
                    "policy": policy,
                    "signals": {
                        "regime": "neutral",
                        "frr_daily_rate": "0.00015",
                        "best_bid": "0.000149",
                        "best_offer": "0.000151",
                        "utilization": "0.86",
                    },
                    "plan": {
                        "plan": plan,
                        "planned_amount": "1200",
                        "idle_amount": "0",
                        "target_slice_count": 60,
                        "plan_hash": "fixture-plan-hash",
                        "funding_cap": "12480",
                        "existing_exposure": "11280",
                        "cap_remaining": "1200",
                    },
                    "fundingLimit": {
                        "amount": None,
                        "percent": "100",
                        "effectiveCap": "12480",
                        "existingExposure": "11280",
                    },
                    "incompatibleOffers": [],
                    "nonChangeableCredits": [],
                    "warnings": [],
                    "previewToken": "fixture-preview-token",
                    "accountDigest": "fixture-account-digest",
                    "buildId": "fixture-build-v3",
                    "expiresAt": "2026-07-22T12:00:00+00:00",
                    "replay": {
                        "mode": "REPLAY",
                        "sampleCount": 672,
                        "estimatedUtilizationPercent": "84.6",
                        "netInterest": "8.5",
                        "actualNetAprPercent": "9.1",
                    },
                }
            )
        if path == "/api/strategy/v3/draft":
            CONFIG["strategyV3Draft"] = dict(payload.get("strategyV3", {}))
            draft = {
                "version_id": "fixture-draft-v3",
                "status": "DRAFT",
                "policy": dict(CONFIG["strategyV3Draft"]),
            }
            CONFIG["strategyV3State"]["draft"] = draft
            return self.send_json(
                {
                    "ok": True,
                    "status": "DRAFT",
                    "versionId": "fixture-draft-v3",
                    "draftVersionId": "fixture-draft-v3",
                    "applyToken": "fixture-apply-token",
                    "strategy": draft,
                }
            )
        if path == "/api/strategy/v3/apply":
            draft = CONFIG["strategyV3State"].get("draft")
            if draft:
                draft = {**draft, "status": "ACTIVE"}
                CONFIG["strategyV3State"].update({"active": draft, "draft": None, "pending": None})
                CONFIG["strategyV3"] = dict(draft["policy"])
                CONFIG["strategyV3Draft"] = None
            return self.send_json(
                {
                    "ok": True,
                    "status": "ACTIVE",
                    "strategy": CONFIG["strategyV3State"]["active"],
                }
            )
        if path == "/api/strategy/v3/discard":
            CONFIG["strategyV3State"]["draft"] = None
            CONFIG["strategyV3Draft"] = None
            return self.send_json(
                {
                    "ok": True,
                    "status": "DISCARDED",
                    "activeStrategy": CONFIG["strategyV3State"]["active"],
                }
            )
        if path == "/api/control/preflight":
            return self.send_json(preflight_payload())
        if path == "/api/control/start":
            if payload.get("preflightId") != "fixture-token":
                return self.send_json({"ok": False, "error": "预检令牌无效或已过期"}, 400)
            CONTROL.update(
                {
                    "running": True,
                    "pid": 4321,
                    "startedAt": "2026-07-18 16:21:01",
                    "returnCode": None,
                    "stopReason": None,
                }
            )
            return self.send_json({"ok": True, "bot": CONTROL})
        if path == "/api/control/stop":
            CONTROL.update(
                {
                    "running": False,
                    "pid": None,
                    "returnCode": 0,
                    "stopReason": "stopped_by_dashboard",
                }
            )
            return self.send_json({"ok": True, "bot": CONTROL})
        return self.send_json({"ok": False, "error": "Not found"}, 404)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8123), FixtureHandler).serve_forever()
