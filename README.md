# Bitfinex 自动放贷机器人

这是一个 Python 3.14 兼容、仅管理 Bitfinex USD Funding 的动态放贷机器人。市场数据优先使用 WebSocket，REST 用于启动同步、周期校验和断线降级。控制台只绑定 `127.0.0.1`，首次启动状态为 `PAUSED`，不会自行进入 LIVE。

## 安全设计

- 运行模式为 `PAUSED/LIVE/REPLAY/SAFE`；已删除 PAPER，模拟分析使用隔离的 REPLAY。
- 网页启动前执行只读预检，并签发最长 5 分钟有效的一次性本地启动确认令牌；它不是 Bitfinex API 令牌。
- 令牌绑定 Dashboard build、配置文件 SHA-256、ACTIVE 版本、策略哈希、账户摘要、待撤单集合和计划哈希；过期、重复使用或任一内容变化都必须重新预检。
- 桌面图标每次都会安全替换已验证的 Dashboard，并在前端与后端 build 握手成功后才打开页面；无法验证的 8000 端口进程不会被终止。
- 必须具备 `wallets` 读取权限和 `funding` 读取/写入权限。
- 配置自动转账时，必须额外具备 `wallets` 写入权限。
- `withdraw` 和 `ui_withdraw` 写入权限必须关闭，否则阻断启动。
- 创建、撤销挂单和钱包转账必须收到 Bitfinex 明确的 `SUCCESS` 通知；HTTP 200 业务错误也会被视为失败。
- 配置与状态文件使用原子替换，避免控制台读取到半写入内容。
- 控制台使用进程锁，防止并发启动产生多个机器人实例。
- 项目没有提现实现。

## 安装依赖

启动时只检查依赖，不会自动联网安装：

```powershell
.\install-dependencies.cmd
```

也可手动运行 `python -m pip install -r requirements.txt`。实时行情依赖 `websockets>=14,<17`。

## 配置

复制示例配置：

```powershell
Copy-Item default.cfg.example default.cfg
```

编辑 `default.cfg`，或优先使用环境变量保存凭据：

```powershell
$env:BITFINEX_API_KEY="your_key"
$env:BITFINEX_API_SECRET="your_secret"
```

`default.cfg`、状态 JSON、机器人挂单归属文件和进程日志包含敏感账户信息，已由 `.gitignore` 排除。V3 草稿只写 SQLite；激活后才镜像允许的 V3 字段到配置文件，不会改写 API 凭据或原有注释。

## 启动控制台

```powershell
python lendingbot.py --dashboard
```

打开 [http://127.0.0.1:8000/lendingbot.html](http://127.0.0.1:8000/lendingbot.html)。控制台本身不会启动机器人。点击“启动实盘”后，页面会展示权限、真实余额、可转入金额、最大可能放贷额、拆单和重定价参数；只有所有阻断检查通过后才能确认。

停止机器人进程不会撤销 Bitfinex 账户中已有挂单。

## USD 动态策略 v3

v3 把全部 USD 本金（Funding 钱包可用余额、Offers 和 Credits）折算为约 50 个虚拟切片。默认期限池为短期 50%、中期 30%、长期 20%，每池再按快速 40%、平衡 40%、高收益 20% 分层。市场处于尖峰或低利率状态时，期限池可在用户基准上下最多移动 10 个百分点。

三个期限池的最低值填写“扣除手续费后的简单净 APR 百分比”，并且故意没有默认值。下单毛日利率按下式计算：

```text
毛日利率 = 净 APR / 365 / (1 - 手续费率)
```

普通和 Hidden 手续费默认分别为 15% 和 18%。任何新订单都不能低于所属期限池的净收益下限；当前与历史市场没有足够利率支持时，资金保持闲置。最低单笔默认 150 USD，金额使用 Decimal 量化到 8 位，并按策略版本、USD 和切片编号生成可复现的 +/-3% 扰动。

支持 `LIMIT`、普通 `FRR`、`FRRDELTAFIX`、`FRRDELTAVAR` 和 Hidden `flags=64`。普通 FRR 映射为零偏移 `FRRDELTAVAR`，与 Variable 共用默认 10% 的组合上限。Hidden 默认关闭，启用时必须填写最大占比，且只有扣除更高手续费后评分仍优于可见候选才会采用。

SQLite `ACTIVE` 是唯一实盘策略；配置文件只保留首次迁移与 ACTIVE 镜像。保存只创建 `DRAFT`，存在 DRAFT/PENDING 时预检会阻止启动。LIVE 切换期间不提交任何新单；禁用类型、Hidden、期限或底价不兼容的机器人挂单会立即撤销，REST 确认消失后才原子激活并重新计算计划。普通市场重定价仍遵守最短挂单时间、冷却与每小时上限。

`max_lend_amount` 与 `max_lend_percent` 分别限制最大放贷金额和账户本金比例，同时配置时取更严格值。已有贷款、机器人挂单和外部挂单都占用额度；超过上限后不再创建新单，只允许撤销机器人挂单。Variable/Hidden 上限同样按账户全部现有敞口计算。

状态保存在 `.state/lendingbot-v3.sqlite3`（SQLite WAL）。不明确的下单结果会进入人工 SAFE 且禁止重试；普通数据故障在两次一致对账、间隔至少 30 秒后恢复。WebSocket 断线期间使用 REST 完整降级，连续断线达到 5 分钟进入 SAFE。

控制台 API：

- `GET /api/runtime/v3`：模式、策略版本和进程状态。
- `GET /api/stats/v3`：7/30/90 天和全部统计。
- `POST /api/strategy/v3/preview`：使用真实账户快照和市场数据预览，不写账户；历史快照会明确标记。
- `POST /api/strategy/v3/draft`、`/api/strategy/v3/apply`、`/api/strategy/v3/discard`：保存、应用或放弃版本。
- `POST /api/runtime/v3/mode`：切换 PAUSED 或 REPLAY。
- `POST /api/runtime/v3/resolve-ambiguous`：人工绑定唯一 Offer 或确认未创建；处理后回到 PAUSED。

## 命令行实盘

先运行单次实盘循环并检查账户：

```powershell
python lendingbot.py --live --once
```

持续运行：

```powershell
python lendingbot.py --live
```

命令行启动同样会在任何写操作前完成权限、钱包和资金盘口预检。
每次直接命令行启动都会在预检后要求输入一次 `LIVE`；控制台启动则使用预检对话框签发的一次性确认令牌。

## 实时计划与历史情景回放

V3 预览、启动预检和实盘循环调用同一个计划入口，并使用相同的账户本金、可用余额、Offers、Credits、市场信号、ACTIVE 版本与资金上限。响应包含策略版本和计划哈希，便于核对最终提交参数。数据过期时页面显示“历史快照”，启动预检不会把历史数据当作实时账户。

机器人只撤改 SQLite 中能确认归属的挂单。手工或其他机器人挂单不会被撤销，但会计入总敞口；已成交贷款无法撤销，不兼容时只显示合规警告并阻止继续创建同类订单。旧 V2 配置字段保留在文件中但不再读取、展示或参与实盘。

## 状态协议

控制台使用 `schemaVersion: 3`，接受 `PAUSED/LIVE/REPLAY/SAFE/APPLYING`（兼容旧进程的小写 `live`）状态。其他历史状态会被忽略，避免旧余额被误认为当前资产。v3 状态包含 WebSocket/REST 新鲜度、市场信号、账户本金、Offers、Credits、策略版本及四个统计窗口；外部订单和 Credits 保留展示，但不会被机器人撤改。

## 测试

```powershell
python -m pytest -q
python -m py_compile lendingbot.py StrategyEngine.py StrategyV3.py RuntimeV3.py StateStore.py MarketDataStream.py bitfinex.py Logger.py FileUtils.py
node --check www/v3-dashboard.js
git diff --check
```

测试使用临时配置和假 Bitfinex 客户端，不调用真实账户写接口。
