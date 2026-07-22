# MikaLendingBot 0.3.0

面向单台 Windows 电脑、单一 Bitfinex 账户的 USD Funding 自动放贷工具。项目只保留 V3 策略；Dashboard 默认 `PAUSED`，只有完成真实账户只读预检并人工确认后才会启动 LIVE Worker。

## 安全边界

- 仅支持 `USD`，期限限定为 Bitfinex Funding 的 2–120 天。
- API 必须允许 wallets 读取、funding 读取/写入；withdraw 与 ui_withdraw 写权限必须关闭。
- 机器人只撤改 SQLite 中能够证明归属的挂单。外部挂单计入资金上限，但绝不会由机器人撤销。
- 提交、撤单和钱包转账统一区分 `CONFIRMED / DEFINITE_REJECT / UNKNOWN`。请求发出后的超时、断线、截断响应、非法 JSON 或提交成功但缺少 Offer ID 都属于 `UNKNOWN`，不会自动重试。
- 进程重启时，未发出的 `PLANNED` 意图会关闭；遗留 `SUBMITTING` 会转为 `AMBIGUOUS` 并进入人工 SAFE。只有 Offers、Funding Trades/历史记录能唯一匹配时才自动绑定，恢复后仍回到 `PAUSED`。
- WebSocket 每代连接必须重新收到 Book、Wallet、Offers、Credits 快照；新 Book snapshot 会清空上一代盘口。快照未齐全时只能使用新鲜 REST 完整降级数据。
- Dashboard 只绑定 `127.0.0.1:8000`。所有 POST 要求同源 Host/Origin、随机 CSRF 头、JSON Content-Type，且请求体不超过 64 KiB。
- SQLite 使用显式 schema v4。升级前在线备份，迁移在事务中完成。

详细流程见 [安全恢复手册](docs/safety-recovery.md) 和 [架构说明](docs/architecture.md)。

## 安装

```powershell
python -m pip install -r requirements.txt
Copy-Item default.cfg.example default.cfg
```

建议通过环境变量提供凭据：

```powershell
$env:BITFINEX_API_KEY = "your-key"
$env:BITFINEX_API_SECRET = "your-secret"
```

在 `default.cfg` 中设置三个期限池的最低净 APR；它们没有默认值，缺失时 LIVE 会被阻止。`max_lend_amount`、`max_lend_percent`、最低净 APR、最低订单金额和权限要求始终是硬边界。

## 运行

启动本地控制台：

```powershell
python lendingbot.py --dashboard
```

浏览器打开 [http://127.0.0.1:8000/lendingbot.html](http://127.0.0.1:8000/lendingbot.html)。停止 Worker 不会撤销已有挂单。

直接启动 LIVE（预检后还需输入一次 `LIVE`）：

```powershell
python lendingbot.py --live
```

单周期验证：

```powershell
python lendingbot.py --live --once
```

## 离线命令

这些命令拒绝在 LIVE 锁被占用时运行，且不会调用任何交易写接口。

```powershell
# 仅执行一次旧托管挂单归属迁移；重复运行幂等
python lendingbot.py --migrate-legacy

# 分页回填至少 90 天 fUSD Funding Trades 与 Stats
python lendingbot.py --backfill-market-data --research-days 90

# 固定 60 天训练、15 天验证、15 天测试
python lendingbot.py --evaluate-strategy --research-days 90 --principal 10000
```

历史盘口无法回填；从部署后开始，每分钟保存 REST 盘口快照。评估器固定比较当前 V3、FRR-only、LIMIT 贴近成交价，并只在候选样本外收益更高、配对 Bootstrap 95% 下界大于零且安全不变量全部通过时标记为实盘候选。报告写入 `docs/strategy-validation-report.json`，不会自动提升候选或改变 ACTIVE 策略。

手工评分权重不再由普通控制台或配置文件编辑，它们属于版本化研究实现。

## 验证

```powershell
python -m pip install -r requirements-dev.txt
.\verify.ps1
```

验证包括 Ruff、Python 编译、103+ 单元/集成测试、核心安全覆盖率、前端语法、Dashboard HTTP 关键流程和 `git diff --check`。测试通过临时 `AppContext` 隔离真实锁、默认数据库、日志和凭据，即使真实 Worker 正在运行也不会读取或写入其状态。

## 版本与文件

- 应用/User-Agent：`0.3.0`
- Dashboard：按内容生成 build hash
- 核心入口：`lendingbot.py --dashboard`、`lendingbot.py --live`
- 状态库：`.state/lendingbot-v3.sqlite3`
- 运行架构：[docs/architecture.md](docs/architecture.md)
- 异常处置：[docs/safety-recovery.md](docs/safety-recovery.md)
- 策略证据标准：[docs/strategy-validation.md](docs/strategy-validation.md)
