每日数据对账策略 v0.4.0
==============================================
更新时间：2026-10-12

用途
--------------------------------------
当前 SDK 的原生预告读取完整、经核验的聚宽导出归档，字段/文本直接来自原生数据；
必须满足窗口、证券范围及决策前实际观察/导入时间。下文旧版本投影修复和既往
比较数字属于当时验证记录，不表示后来采集的归档能解除历史阻塞。
完整导出与导入步骤见 ../docs/native_data_import.md。

同一份 data_alignment_audit_strategy.py 在本地 jq-tushare-sdk 和聚宽运行，
通过完整日志导出，按交易日、采集阶段、配置校验值对齐请求和结果。
首版采用只读采集，同时记录样本股票的虚拟选股排名。程序没有下单、Redis、
邮件或其它通知调用。股票、财报、行业和指数查询失败后会继续采集其它接口，
并明确记录 unsupported/error/partial/empty 状态。

策略仅维护在 examples/data_alignment_audit_strategy.py；采集、比较工具和说明
也位于 examples/。修改股票名单等配置后，把本地实际采集的文件完整内容
复制到聚宽，保证两端配置一致。可通过 --strategy 指定自己的策略副本。
覆盖范围、此前问题和日志缺口见同目录 DATA_ALIGNMENT_REVIEW.rst；
基础日志完整性通过不等于所有 API、数据源或原生产策略一致性已经验收。

本地运行
--------------------------------------
本目录是 SDK 自带的公开示例，不依赖 Join 项目或私有策略。
先按仓库 README 的 Installation / Prepare Data 准备 Python 3.11+、
安装 SDK 并取得真实缓存；缓存至少需要覆盖测试窗口及此前 21 个交易日。
默认回放只读缓存。需要补齐时加 --prepare：先按已有 Tushare 权限下载真实数据，
完成后冻结缓存和 SDK 源码摘要，再进行只读回放。补数失败或权限不足会明确停止。

在 jq-tushare-sdk 仓库根目录，使用安装好 SDK 的 Python 环境：

.. code-block:: bash

    python examples/run_data_alignment_audit.py \
        --start 2026-09-01 --end 2026-09-08 --prepare

如使用自己修改的独立副本：

.. code-block:: bash

    python examples/run_data_alignment_audit.py \
        --strategy "/实际路径/data_alignment_audit_strategy.py" \
        --start 2026-10-08 --end 2026-10-09

默认从 data/jq_tushare_cache.db 只读获取真实数据。
可通过 --cache 指定其它真实缓存，通过 --financial-mode 明确数据模式。
默认 cached_snapshot，历史财务 vintage 没有取得 PIT 认证。
strict_pit 模式如果预检不通过会停止，保存已有协议文件。
所有生成物自动保存在 artifacts/data_alignment_<timestamp>_<pid>/，该目录 Git ignored：
  data_alignment_audit_strategy.py  本次实际运行的冻结副本
  protocol.json                   策略、SDK 源码和缓存的校验值
  data-preparation.json           使用 --prepare 时记录的真实补数结果
  alignment.log                   机器可读的结构化对账日志
  local-validation.json           日志、快照数量和本地状态检查
  local_inventory/report.html     本地覆盖报告，等待聚宽记录
  runs/                           SDK 完整原始日志和业务账本
输出目录已经存在时拒绝覆盖，保留每次实际观测。

聚宽运行
--------------------------------------
1. 新建一个独立策略，将本地实际采集的策略文件完整内容复制进去。
2. 首次用与本地相同日期（例如 2026-09-01 至 2026-09-08）做回测。
   初始资金可以设为 100 万，基准为创业板指。程序不提交订单。
3. 导出完整日志文本或 CSV。需要包含所有 JQALIGN1 行，以及每组 begin/end。
   不要只截取页面可见几行，不要手动修改日志内部的数据或来源标签。
4. 用本地比较工具读取两个文件。确认后，可把这个独立策略用于模拟运行，
   每个交易日盘前和收盘后自动产生记录。部署及原生运行仍需在聚宽实际验收。
本地与聚宽使用完全相同的 VERSION、股票名单、指数名单、采集频率和配置。
修改配置后 config_sha256 会变化，旧记录保留为其它对账协议。
v0.2.0 新增复权因子、字段类型、来源属性、函数来源、请求耗时和完整异常堆栈；
显式使用 Python 内置判断与聚合函数，防止 jqdata 通配导入改变 generator 判断。
v0.3.0 增加总股本/流通股本、2025q3/2025q4/2025 年度的收入和净利润明细。
财务诊断周期由 INCOME_DIAGNOSTIC_PERIODS 明确配置，不改变已有季度探针。
年度累计与单季度使用不同单位标签；同时导出营业总收入、营业收入、净利润、
归母净利润、发布日和报告期，便于定位单季度差值、字段口径及修订。
另外采集平安银行 2025-09-30、2025-12-31 原生累计报表及修订行的完整字段，
用来区分累计原值差异和单季转换差异；NATIVE_INCOME_PERIOD_ENDS 可调整报告期。
这些原生原值仅作来源诊断，SDK 尚未映射该原生表时明确保留 unsupported，
不会用现有单季表替代原生表，也不据公布日期自动取得历史 vintage 认证。
业绩预告独立名单增加 002274.XSHE：已从真实 Tushare 接口取得 2026-07-09 记录，
已在实际聚宽 v0.3.0 log 9 中核实同一 code/pub_date 记录，SDK 支持明确选择
这两个字段；仍须完整公告区间覆盖和上一交易日公布截止，不能据此认证整个原生表。
版本和环境日志同时保留可取得的原生 STK_FIN_FORCAST 表结构；其它数值/类型/ID
字段与整表投影仍明确报告不支持。
另外导出同一窗口的原生预告完整字段和原值，仅作结构/单位诊断；不提前猜测
字段映射，也不把此诊断分组计为数值一致。可用下一次非空观测继续补齐兼容能力。
v0.3.1 修正原生诊断整表中合法可空字段造成的 partial 误报：仍完整保留 null_fields，
主键缺值、列缺失和普通数值探针的缺值仍为 partial。诊断表另保存真实返回字段/dtype，
即使 __table__ 不可取得也能核验观察到的结构；返回结构不冒充原生列类型声明。
source_attrs 超过日志长度限制时显式记录原始字符数和 SHA256，不能把截断视为完整原值。
本地和聚宽必须使用相同版本的新日志配对，v0.1/v0.2/v0.3.0/v0.3.1 日志不能混用。
已有 log 9 的对比继续使用冻结 v0.3.0 源码；v0.3.1 本地验证不计入该双端结果。
同一天重复采集有冲突内容时，工具要求明确单次运行来源，不会自动挑较新的。

比较日志
--------------------------------------
以下日志路径替换成实际导出路径，--out 使用一个新的结果目录：

.. code-block:: bash

    python examples/compare_data_alignment_logs.py \
        --local "本地输出目录/alignment.log" \
        --joinquant "/实际路径/聚宽日志.txt" \
        --protocol "本地输出目录/protocol.json" \
        --out "artifacts/新的对账结果目录"

输出 report.html、comparison.json、differences.csv。
字段差异按“交易日 / 阶段 / 接口 / 股票或时间 / 字段”定位，保留两边原值。
输入文件 SHA256 写入结果，便于追踪是哪一份日志。
--protocol 按冻结交易日逐一检查盘前、盘后快照，识别双方同时漏掉整组的情形。
不提供此参数时，比较器只能检查输入已有快照，无法认证完整区间覆盖。
UTF-8、GB18030、带 CSV 引号及聚宽页面 HTML 转义的日志容器均可读取。
缺片、校验值错误、来源相同、版本/日期不匹配会阻止跨平台验收通过。
不提供 --joinquant 时只生成本地覆盖清单，状态为 awaiting_joinquant_log。

继续分析差异
--------------------------------------
可用下面的独立诊断工具读取同一组真实日志：

.. code-block:: bash

    python examples/analyze_data_alignment_logs.py \
        --local "本地输出目录/alignment.log" \
        --joinquant "/实际路径/聚宽日志.txt" \
        --protocol "本地输出目录/protocol.json" \
        --out "artifacts/新的差异诊断目录"

输出 analysis.json、报告和双端无损解码 JSONL。字段差异按真实请求日/报告期、
股票/日期、字段和值去重，每次盘前/盘后出现位置仍完整保存。
诊断会核对固定样本的筛选结果及排名，并将参考日志中的因子代入本地原价，
核对其是否能解释价格和成交量误差。这是因果诊断，不是独立数据源验证，
不会把聚宽参考值写入缓存或替换 SDK 行情。整组遗漏/版本/完整性问题阻止诊断。

采集口径
--------------------------------------
  09:00 盘前：日线截止上一个交易日；财报与估值也查询上一个交易日。
  15:30 收盘后：日线截止当天；财报与估值保持上一个交易日的查询口径。
  配置使用真实价格选项，前复权日线与不复权日线分别采集。
  单独采集 factor，便于定位数据源复权精度差异；前复权成交量按反向因子调整。
  SDK 日线输出统一成交量为股/份、成交额为元，来源缓存仍保留原始单位。
  交易日、指数成分日期、行业查询日期和财务 statDate 逐条保留。
  财报单独查询最近三个结束季度，同时查询 date 模式及报告/发布日字段。
  statDate 属于回顾性接口探针，不能凭该探针证明当时已公布或历史 PIT。
  原生预告仅检查 code/pub_date，不猜测聚宽财务表与 Tushare 的数值映射。
  股票列表默认导出样本基础信息，并记录全市场代码数及代码集 SHA256。
  设置 EXPORT_FULL_UNIVERSE=True 可以导出完整股票代码集以定位名单差异。
  财务金额约定为元；总市值和流通市值约定为亿元，保留单位信息。
  价格窗口通过交易日历核对 21 个交易日，样本虚拟信号使用 MA20 和 5 期收益。
  排名只在固定样本中计算，没有复现原小市值策略的选股、目标仓位或成交。
  当前行情价格单独作为环境参考记录；日线参考价格不取得分钟实时认证。

分钟数据
--------------------------------------
默认 ENABLE_MINUTE_PROBES=False，每组明确记录未启用。
两个平台都改为 True 后，采集相同数据日期的 09:30—09:35、14:55—15:00。
分钟行保留完整时间戳。需要已具备分钟行情的执行环境、相应权限和历史覆盖。
SDK 纯日线回测没有分钟 feed，可能明确返回不支持；不能用日线值替代后认证。

差异判定
--------------------------------------
默认绝对/相对浮点容差均为 1e-6，可用 --abs-tol、--rel-tol 明确调整。
成交量和成交额绝对容差最低为 1 股/1 元，同时使用相对容差。
布尔、日期、股票名单、行业代码和虚拟选股排名按精确值比较。
不做单位自动缩放、缺失补零或停牌数据自行填充。
两边都缺值会计为覆盖问题；两个空结果也不会取得数据一致认证。
有匹配观测时，仅认证已启用样本的数据值，没有宣称全市场或原生引擎等价。
接口错误与数值差异同时保留，避免把收益差异直接归因于数据源。

当前本地观察
--------------------------------------
已取得聚宽 v0.1.0 在 2026-09-01 至 09-08 的 12 组原生记录，并用相同旧程序
在本地修复前后回放。已修复缺失指数线被当作停牌填充、成交量单位、原价调用、
季度补数、date 财报 nan 异常、发布/报告期字段、流通市值及股票基础字段。
财报补数完整分页；新增报表修订只能记录本次观察，不能恢复遗失的历史 PIT。
宁德时代复权精度及部分估值、平安银行 2025q4 营业收入仍有来源差异，保留原值。
原生业绩预告仍明确不支持，分钟探针默认未启用。
已取得聚宽 v0.2.0 的相同区间 12 组日志（log 8）。日历、股票列表、行业、
状态、原价/指数日线、指数成分、date 财报和 2026q1/q2 均在容差内一致。
187 个分组一致、41 个有差异、12 个原生预告阻塞；另有 24 个环境分组和
24 个未启用分钟分组，没有 coverage_gap、配对或日志校验问题。
41 个有差异分组存在重复观测，不能解释为 41 个独立问题。
7560 个价格/成交量字段可由参考因子与本地原价在现有容差内重现；12 组
固定样本的 eligible/排名都未改变，MA20 仍保留小差异。专业因子接口及银行
直接单季度接口重新取得的源值仍与当前缓存一致，不硬编码参考值。
v0.3.0 扩展探针已收到 log 9，相同窗口完成 12 组原生配对。
实际证据位于 artifacts/data_alignment_v02_pair_20261011T204950/。

高精度因子与公告证据（本地未发布功能）
--------------------------------------
默认仍使用 tushare 因子。可显式选择 sina_hfq：公开端点的完整十进制因子
与原始响应、来源 URL、Hash、捕获时间独立存储，原 Tushare 表不会被改写。
该来源有独立的复权口径；更高的小数精度不代表与聚宽完全一致，也不认证历史 PIT。
缺失来源覆盖时阻止取价，不隐式混用不同来源。

先补齐真实样本来源：
  .venv/bin/python examples/prepare_market_reference_data.py \
    --codes 000001.SZ,300750.SZ,600000.SH,600519.SH,688001.SH,688981.SH \
    --precise-factors

再在回放时显式选择：
  .venv/bin/python examples/run_data_alignment_audit.py \
    --start 2026-09-01 --end 2026-09-08 --adjustment-source sina_hfq

日线自动补数也可设置 JQTS_ADJUSTMENT_SOURCE=sina_hfq，准备明确股票依赖；
动态选股的额外股票必须显式准备，不能凭六个样本声称全市场已覆盖。
JQTS_SYNC_ANNOUNCEMENTS=szse 启用明确深市股票依赖的公告元数据补齐。
公告接口返回交易所精确发布时间；这是深交所的可见时间，未认证全球首次披露。

prepare_market_reference_data.py 的 --capital-statement 接受人工核对原公告的
JSON（code、pdf_path、statement）。必须先归档官方公告元数据并匹配公告 ID、
PDF Hash、股份类别及单位。statement 记录 date_basis、effective_on_or_before、
economic_effective_date、原始整数股数和 source_pages。
月报只能建立期末股本和生效上界；缺失逐笔登记日及流通股本时只进入诊断，
不修改估值。发表时点与实际捕获时间分别保存，公告发表前查询不可见。
财报始终保留原始公告值，不复制聚宽日志中的收入修订值。
示例自 v0.3.2 起单独记录完整 source_evidence，包括因子回执和股本公告摘要，
不被全市场状态字典挤出日志；仍保留合法空字段、属性截断长度及原内容 Hash。
配对历史 log 9 必须指定冻结的 v0.3.0 源文件，不能与更新版本混配。

股本研究视图与参考报价复权规则（本地未发布功能）
--------------------------------------
analyze_data_alignment_logs.py 增加 root-causes.json，按根因归并重复观测与字段
传播，保留原值及每次出现的位置，并核算分组的并集是否覆盖所有差异。
根因候选不自动决定哪一个平台的数据正确，仍需原始公告核查。

prepare_market_reference_data.py --capital-history 归档东方财富
RPT_F10_EH_EQUITY 的原始整数股数、日期和响应 Hash。LISTED_A_SHARES 是流通
A 股，不包含流通 H 股；月末 END_DATE/LISTING_DATE 不等于逐笔登记日期。
--disclosed-capital-reference 接受经过核对的 JSON：code、snapshot_date、
capital_history_sha256、filings、income_inputs、ordinary_equity。
filings 含 role、announcement_id、pdf_path、pdf_sha256；必须先归档深交所公告。
income_inputs 含 end_date、source_value、source_unit_cny、filing_role、source_page，
保留年度/本期/上年同期累计数；与原始 income_statement 再交叉验证。
ordinary_equity 必须有原报告页码及其他权益工具确实为零的核对，空值不当成零。
总股本必须匹配已导入的发行人月报；流通股本独立引用上述提供商记录。
日期只有日精度的来源按当日结束后可见处理。范围限制在下次提供商股本变动前。

显式设置 --capital-source disclosed_reference 或 JQTS_CAPITAL_SOURCE 后，使用
已可见且范围合格的凭证；其余股票保留 Tushare 日线来源，并逐股标明出处。
用当日原价、披露股本和原公告 TTM/普通股权益计算市值、PE/PB、换手率。
这是最新已披露股本的研究视图，未建立逐笔中登登记或历史逐秒可见认证。
默认 tushare 不变；不能直接把研究结果作为实盘取数认证。

参考报价规则需显式准备完整回看范围，例如：
  .venv/bin/python examples/prepare_market_reference_data.py \
    --codes 000001.SZ,300750.SZ,600000.SH,600519.SH,688001.SH,688981.SH \
    --reference-price-factors --start 2026-01-01 --end 2026-09-08

再显式选择 --adjustment-source daily_ex_reference。因子变化时，使用已记录的
前一日原始收盘价 / 当日除权昨收参考价累计，保留十进制比值和原始输入。
初始绝对单位来自首日 Tushare 因子；前复权归一化会消去该单位。
该规则不读取聚宽对比日志，报价舍入仍可能导致 0.01 元差异；不推导精确
原生因子，也不改写分红实际金额。缺事件报价、来源范围或股票覆盖时明确停止。
JQTS_ADJUSTMENT_SOURCE=daily_ex_reference 的日线准备会在补数完成后归档此规则。
严格历史模式不允许使用后来捕获的股本/因子记录。

示例 v0.3.3 输出完整股本与原公告估值计算凭证。历史 log 9 的同区间比较仍
须使用冻结 v0.3.0，不能与 v0.3.3 混配。

2026-10-11 同区间复验
--------------------------------------
artifacts/alignment_root_causes_20261011T221540/ 保存本次真实证据、冻结源文件、
12 组 v0.3.0 本地/聚宽快照和逐项比较。原 60 个数值差异分组按根因归为
3 类、23 个独立字段；显式选择上述股本研究视图和参考报价规则后，21 个分组
取得一致，剩余 39 个分组归为 2 类、6 个独立字段。
其中 36 个是保留平安银行原公告收入及单季差分，3 个是宁德时代 2026-08-04
前复权开盘价 393.57 / 393.58 在不同回看窗口内的重复观测。
参考报价的小数计算精度不代表恢复了聚宽未舍入的原始因子。

另有 12 个原生业绩预告分组被当前 SDK 阻止：没有在历史决策时点之前已观测且
导入的完整聚宽预告快照。这些单列为 blocked，不算作数值差异修复或匹配。
因此完整比较仍是 incomplete，不能把 39 当作全部尚未认证的接口数量。
聚宽原生预告要求完整原生来源，不能由 Tushare 摘要替代。

新增港交所月报索引确认 9 月 3 日 17:32 披露，早于深交所 19:14:28；
只有分钟精度，未认证全球首次披露。流通股本提供商只有 NOTICE_DATE 日精度，
研究视图保守地到当日结束后可见。115491 股新增股份仍没有逐笔登记日期，
保留未知，不用月末日期替代。新 v0.3.3 日志完成两组本地真实验证；其聚宽
同版本配对尚未执行。本次回放源码/缓存摘要不变，订单与成交均为零。

API 参考
--------------------------------------
https://www.joinquant.com/help/api/help#name:api
https://github.com/JoinQuant/jqdatasdk/blob/master/jqdatasdk/api.py
https://test.demo.joinquant.com/help/data/stock

扩样诊断与新的日期证据（v0.4.0）
--------------------------------------
默认保留六股票完整探针，AUDIT_PROFILE='full'；可设置 AUDIT_PROFILE='prices'
并明确 SAMPLE_CODES，执行更长的价格回放。prices 只采集 configuration、calendar、
daily_pre、daily_raw、price_factors 和 decision_trace，省略的财报/分钟接口不取得认证。
两边程序的配置和源码必须相同。原生预告完整行优先用 id 标识，code/pub_date
投影遇到重复身份会明确 partial，不静默删掉不同报告记录。

已准备补充来源后，可直接对真实缓存做扩样检查：
  .venv/bin/python examples/run_extended_data_audit.py \
    --cache data/jq_tushare_cache.db --start 2026-05-06 --end 2026-10-09 \
    --out artifacts/extended_data_audit_NEW

工具默认含沪深主板、创业板、科创板共 32 股票，核验三种来源、普通/优化路径、
日期/证券身份、有限值、价格关系及因子，并保留每份真实数据。来源之间的差异
只作为来源诊断，不能据此判定与聚宽一致。北交所、ETF 和其他接口需另行覆盖。

复权凭证必须单份覆盖请求区间；新采集的较窄区间不再遮住旧的完整记录。
停牌补齐须有真实停牌证据，factor 使用当日实际来源；原价模式明确为 1。
股本核验单独保留旧制度 NON_FREE_SHARES，不能只用 LIMITED_SHARES 核算老记录。
不完整行保留原值及原因，选用该行建立估值研究视图时仍严格拒绝。
同内容的多次采集可用于公告导入，原始股数必须绑定所在响应页及 Hash；
不同股数或不同公告内容仍明确拒绝，不合并、不猜测。

--capital-registration-notice 接受 code、pdf_path、notice JSON。notice 包含
announcement_id、pdf_sha256、source_pages、share_count、registration_status、
registration_date、preregistration_completed_on_or_before、listing_date、listing_date_basis。
pre_registered 不允许有最终 registration_date，registered 必须有原公告支持的日期；
listing_date_basis 为 announced_expected 或 completed_listing。所有证据只补充
溯源，不自动修改历史股本。发行人“已完成预登记”和“预计上市”不能冒充最终登记。

预告就绪检查与当前归档查看：
  .venv/bin/python examples/inspect_native_forecast.py \
    --cache data/jq_tushare_cache.db --start 2026-07-11 --end 2026-10-09 \
    --decision-at 2026-10-12T09:00:00+08:00 --inspect-current-archive \
    --out artifacts/native_forecast_inspection_NEW

readiness.json 分别指出窗口、范围和真实采集/导入时间问题。当前归档导出不改变
历史 finance.run_query 的门禁，始终 historical_decision_eligible=false。
补充来源和实际捕获时间以原始归档为准；本地证据位于
artifacts/sdk_data_expanded_20261012T212511/，其中连续价格回放和原区间聚宽配对
分开保存。财报原公告值没有被改成聚宽的修订收入。
