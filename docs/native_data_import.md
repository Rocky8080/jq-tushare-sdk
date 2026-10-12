# 聚宽原生预告与估值的每日导入

`jqts import-jq-native` 接收聚宽下载的完整日志 ZIP 或 UTF-8 日志，同时核验 `JQNF1` 财报预告和 `JQVAL1` 估值。它保留原生字段、文本、负 PE/PB、完整分页和真实观察时间，不用 Tushare 摘要替换预告文本。

## 1. 为完成的数据日重新导出

在聚宽新建独立策略，使用仓库里的 `examples/export_native_forecast_snapshot.py`。这份脚本只读取公开市场数据和输出日志，不提交订单，不访问 Redis，也不读取账户或持仓信息。

修改脚本顶部醒目的配置：

```python
DATA_DATE = '2026-10-09'  # 最近已经完成的数据交易日
LOOKBACK_DAYS = 90       # 与使用该数据的策略窗口一致
```

在聚宽运行 `DATA_DATE` 当天的单日回测；脚本会检查回测日期与配置一致。运行完成后使用“导出 → 日志”下载完整 ZIP，保留原文件和回测详情链接。界面显示的单行日志可能被截断，不能拿屏幕上的片段代替完整日志。

**每个新的数据交易日都要重新导出。** 例如 10 月 12 日开盘前可以使用 10 月 9 日已完成的数据；10 月 12 日收盘后准备 10 月 13 日候选时，必须重新导出 `DATA_DATE = '2026-10-12'`。不能改旧文件的日期、重命名旧 ZIP，或者将 10 月 9 日估值当作 10 月 12 日估值使用。预告起始日会按 `LOOKBACK_DAYS` 自动重算。

## 2. 核验并导入指定缓存

先做完整核验，不打开或修改缓存：

```bash
jqts import-jq-native '/path/to/log.zip' \
  --cache-db '/path/to/cache.sqlite3' \
  --data-date 2026-10-09 \
  --source-reference 'https://www.joinquant.com/algorithm/backtest/detail?backtestId=YOUR_BACKTEST_ID' \
  --validate-only
```

删除 `--validate-only` 再执行，即追加到指定缓存。解压后的完整 `log.txt` 也可直接作为输入。若策略窗口不是 90 日，导出时和导入命令都要使用相同的 `LOOKBACK_DAYS` / `--lookback-days`。

命令要求两份快照完整，并检查压缩包络、总哈希、每页哈希、原生字段和类型、全市场范围、末尾空页、窗口、数据日及真实观察时间。任何数据校验失败都发生在写入之前；日期不匹配会明确拒绝，不能通过参数给旧数据重新标日期。

输出为 JSON 回执，包含数据日、窗口、行数、哈希、观察时间和实际保存的导入时间，不包含凭据或原始公告正文。导入按内容哈希追加，不更新或删除已有快照；重复运行不会增加同一快照的副本。两份快照分别提交，若磁盘或数据库错误打断导入，排除问题后重跑同一命令即可补齐。

## 3. 使用边界

导入后由工作台重新执行候选准备。预告和估值快照只是数据要求的一部分，行情、交易日历和其他财务数据仍须完整。

日志里的 `observed_at` 是实际导出时刻，不是回测日期。10 月 11 日导出的 10 月 9 日数据可以供其后的真实准备使用，但不能证明这些修订在 10 月 9 日已经可见。回执始终保留 `historical_vintage_verified: false`，历史 PIT 模式不会因此获得通过资格。

## 4. 诊断具体阻塞原因

`examples/inspect_native_forecast.py` 读取已归档的完整原生预告并核查指定决策时点。就绪结果分别报告 `native_forecast_source_missing`、`native_forecast_window_missing`、`native_forecast_scope_missing` 和 `native_forecast_not_observed_before_decision`，避免把数据窗口缺口误当成接口不支持。

例如 2026-07-11 至 10-09 的完整归档，即使今天可以读取，也不能覆盖 2026-06-02 至 08-31 的查询窗口；在 10 月才采集的归档也不能用于 9 月的历史决策。不能通过改时间或缩短策略查询窗口获得历史认证。

`--inspect-current-archive` 仅导出当前看到的原始字段和文本供分析，不解锁历史执行。扩样之后仍需导出对应窗口和同版本对齐策略日志，才能完成双平台比较。
