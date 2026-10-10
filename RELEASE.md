# 0.11.0 本地发布记录 — 2026-10-11

SDK 提供公开 jq_tushare_sdk.daily 契约，支持下游独立 jq-strategy-platform 0.1.0。UI、持久账户、邮件和任务管理不再进入 SDK 发布包；SDK 环境无需安装平台。

运行契约 daily_close_next_open/1，检查点格式 1。保留原财务对象模块路径与历史运行逻辑；旧可信检查点已用两个真实账户验证恢复。公开接口、真实历史日账、单日执行、共享缓存锁、独立包边界与平台验证详见同级 jq-strategy-platform/RELEASE.md。

SDK wheel SHA-256：`e6b82e1b7bc84163784075a3ad2ed31f14551608717bb1898e7e85783e8dfe51`。

迁移不会重算历史；新检查点记录实际 SDK 版本，未知历史版本不猜测。不兼容检查点需要显式迁移。旧 simulation-web 命令提示使用独立平台的 jqsp web。完整停止、迁移和回退步骤见平台 README。

当前为本地 Git 与 wheel 发布记录，未推送远程或上传包索引。
