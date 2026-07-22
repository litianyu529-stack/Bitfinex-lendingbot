# 安全恢复手册

## UNKNOWN 写入

出现超时、连接中断、响应截断、非法 JSON、通知不完整或提交成功但缺少 Offer ID 时：

1. Worker 停止继续提交，运行状态进入人工 `SAFE`。
2. 不要再次点击启动，也不要手工复制同参数提交。
3. 在 Bitfinex Offers、Funding Trades 和历史记录中核对金额、期限、类型、flags 与请求时间。
4. 如果系统找到唯一记录，会自动绑定，但模式只回到 `PAUSED`。
5. 无唯一记录时，在 Dashboard 的 ambiguous 处理入口填写唯一 Offer ID，或在已确认不存在后选择 `confirmAbsent`。
6. 重新执行完整只读预检，再人工启动 LIVE。

## 崩溃恢复

- `PLANNED / NOT_SENT`：确认未发出，启动时关闭。
- `SUBMITTING / SENT`：可能已到达交易所，转为 `AMBIGUOUS / UNKNOWN`。
- `CONFIRMED`：按绑定 Offer ID 对账。
- 撤单或钱包转账为 UNKNOWN：进入人工 SAFE；绝不自动重复写入。

## WebSocket 重连

每次连接建立都是新代次。新 Book snapshot 替换旧盘口；Wallet、Offers、Credits 也必须在同一认证连接代次收到完整快照。未齐全时 WebSocket 不可用于实盘，只有足够新鲜的完整 REST 快照可以暂时降级；超过时限进入 SAFE。

## 数据库迁移失败

schema v4 迁移前会在数据库同级 `backups/` 创建在线备份。DDL 与版本号更新在同一事务内；失败后保持旧版本。处理步骤：

1. 保持 Worker 停止。
2. 保存错误日志和失败数据库副本。
3. 验证最新 `schema-vN-*.sqlite3` 可打开且 `PRAGMA integrity_check` 为 `ok`。
4. 修复后在复制出的数据库上运行测试，再替换生产状态库。

## 候选策略回退

候选异常时把新增资金占比设为 0，并只撤销该 `strategy_variant` 的未成交托管挂单。已经成交的 Credits 不强行处理，等待自然归还。每个 `10% → 25% → 50% → 100%` 阶段至少观察 7 天；存在 SAFE、UNKNOWN 或归属错误时不得提升。
