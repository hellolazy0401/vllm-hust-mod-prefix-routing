# Prefix Routing dev4：四卡 C1–C16 × 三轮完整操作手册

生成日期：2026-10-06。使用 MOD 0.1.0.dev4，固定四张 Ascend 910B2、一个容器、两个 Qwen3.5-35B-A3B BF16 TP2+EP 独立副本。C 表示两个副本合计的客户端并发请求数；取 C=1/2/4/8/16，不是每副本各 C，也不是逐整数 1–16。若要逐整数扫描，测量前修改 campaign.json 的 concurrencies 为 1 到 16，正式窗口变为 96 个；本手册默认 30 个。

不修改 vLLM/vllm-ascend 源码，不运行 apply_core_contract。使用 vLLM 原有 general_plugins 与 middleware 接口；ON 在进程内替换 ZMQ publisher 注册表，停止进程后消失。保留宿主已有改动及其 diff，不能称为原版 upstream。utility-victim 全程关闭。不要混用 dev3 wheel：dev4 修复了 Mamba 稀疏检查点匹配和快照类型丢失。

## 0. 交付与实验范围

上传 prefix-routing-campaign-dev4.tar.gz。包内包括 dev4 wheel、与你原先一致的 extension-manager 0.2.0.dev0 及依赖 wheels、完整 MOD 源码 tar、启动脚本、campaign.py、配置、原始 PR 参考、SWE 客户端源码/样本/许可证、本文及门控清单。不包含模型、CANN、torch-npu、vLLM、Ascend 或全部 SWE 客户端离线依赖。

整理后的本地项目：开发源码在 D:/Desktop/mining/vllm-hust-prefix-routing；交付在 D:/Desktop/mining/prefix-routing；宿主参考在 D:/Desktop/mining/vllm-workspace；utility 参考在 D:/Desktop/mining/utility-victim/vllm_hust_utility_victim-0.1.0.dev2/results 和 evidences；网站有效 checkout 为 D:/Desktop/mining/website/vllm-hust-website（另一个同名顶层目录只有 IDE 文件，不要在那里提交）。本地路径不复制为容器路径。

实验分三层，不能合并曲线：

| 层 | 矩阵 | 用途 |
| --- | --- | --- |
| smoke | OFF→ON→KILL→OFF，C1，每次32请求/1前缀 | 开关、回滚和机制基本检查 |
| 正/负机制对照 | C1/C8/C16，各OFF/ON×3轮；400请求 | 正例20个共享前缀（每前缀20请求），负例400个独立前缀（每前缀1请求） |
| SWE正式 | C1/C2/C4/C8/C16，各OFF/ON×3轮，每次900秒 | 主性能曲线和三轮配对比较 |

共70次新服务启动；SWE正式测量窗口总计7.5小时，模型加载、编译、排空和36次机制对照另计。每次都启动新副本，正式测量前不在同进程做 smoke。首轮可能有编译开销；记录启动时间，不把它计入稳态900秒吞吐，也不把它隐藏成部署成本为零。

## 1. 解压、安装和完整性验证

在原来可运行 Qwen3.5 的服务端 Python 环境操作，不要激活客户端 .venv-bench：

```bash
cd /root/workspace
tar -xzf prefix-routing-campaign-dev4.tar.gz
export MOD=/root/workspace/prefix-routing-campaign-dev4
cd "$MOD"
sha256sum -c SHA256SUMS.txt
python -V
npu-smi info
python -m pip install --no-index --find-links "$MOD/wheels" --upgrade \
  vllm-hust-ext==0.2.0.dev0 vllm-hust-prefix-routing==0.1.0.dev4
python "$MOD/scripts/verify_wheel.py"
python "$MOD/scripts/check_manager.py"
python -c 'from vllm_hust_prefix_routing import __version__; print(__version__)'
```

预期 dev4、default_off/kill_switch 检查通过。check_manager 使用临时配置，不改变你的实际 manager 配置。包使用 LF 换行；sha256sum 失败应排查传输/改动，不跳过校验。安装后不要再次执行旧 dev3 的安装命令。

## 2. utility 关闭、宿主准入、手动开关

```bash
export VLLM_HUST_UTILITY_VICTIM_ENABLE=0
export VLLM_HUST_UTILITY_VICTIM_KILL_SWITCH=1
export VLLM_HUST_UTILITY_VICTIM_EVIDENCE=1
export VLLM_HUST_PREFIX_ROUTING_BACKEND=runtime023-v1
if vllm-hust-ext extension list | grep -q '^org.vllm-hust.utility-victim '; then
  vllm-hust-ext extension disable org.vllm-hust.utility-victim
fi
python "$MOD/scripts/preflight.py" --core-root /vllm-workspace/vllm \
  --output /root/workspace/prefix-preflight.json

vllm-hust-ext extension disable org.vllm-hust.prefix-routing
VLLM_HUST_PREFIX_ROUTING_ENABLE=0 VLLM_HUST_PREFIX_ROUTING_KILL_SWITCH=0 \
  python -c 'from vllm_hust_prefix_routing import register; register(); print("OFF OK")'
VLLM_HUST_PREFIX_ROUTING_ENABLE=bad VLLM_HUST_PREFIX_ROUTING_KILL_SWITCH=1 \
  python -c 'from vllm_hust_prefix_routing import register; register(); print("KILL PRIORITY OK")'
vllm-hust-ext extension enable org.vllm-hust.prefix-routing
vllm-hust-ext extension list
```

只读宿主指纹检查不通过就停止，不能打补丁绕过。版本号相同不等于文件相同；preflight 对 Core 做精确校验，Ascend 尚无同等范围的自动兼容证明，必须记录 commit/diff 并经过本机 smoke。原有 utility scheduler 补丁允许已知精确版本，但本实验不应用或移除它。

Manager 的 enable 是注册状态；实际 OFF/KILL 用 ENABLE=1、KILL_SWITCH=1，ON 用 ENABLE=1、KILL_SWITCH=0。两组都通过相同入口代理、APC开启、hash=sha256、hashseed=0、两个TP2/EP、DP1/PP1、同步调度、0.65 memory、seq16/副本、chunk8192、Mamba align。开关不是热切换，每次必须重启。

实际块大小以启动日志为准：先前本机 Ascend 将配置128调整为2048。共享前缀现在固定8192，不能再用1536作为正例。OFF/ON 均保持这些参数，SWE每chip吞吐除以4，不能照抄utility的除以2。`unknown VLLM_HUST_* environment variable` 警告本身不证明插件失效，按运行证据判断。

## 3. SWE客户端与同一 prepared 文件

已有正确客户端及 prepared 时沿用，不重新生成不同数据。新容器可使用包内源码；下面安装客户端依赖需要网络，离线时需事先在相同平台/Python准备完整wheelhouse：

```bash
export BENCH=/root/workspace/swe-prefix-reuse
export MODEL_PATH=/models/Qwen3.5-35B-A3B
# 仅BENCH不存在时执行复制，勿覆盖已有客户端
test -d "$BENCH" || cp -a "$MOD/client-source/swe-prefix-reuse" "$BENCH"
python -m venv "$BENCH/.venv-bench"
"$BENCH/.venv-bench/bin/python" -m pip install -e "$BENCH[prepare]"
mkdir -p "$BENCH/prepared"
# prepared已存在则直接核对hash并沿用；不要覆盖本轮已冻结的文件
test -f "$BENCH/prepared/qwen35.json" || \
  "$BENCH/.venv-bench/bin/swe-prefix-reuse" prepare \
  --source "$BENCH/data/open-swe-sample.json.gz" --tokenizer "$MODEL_PATH" \
  --max-context 262144 --output "$BENCH/prepared/qwen35.json"
sha256sum "$BENCH/prepared/qwen35.json"
```

SWE必须是真生成token续接、固定输出预算、保持thinking模板、同一session的cache_salt。两个后端各DP1，不加客户端 --data-parallel-size/affinity。SWE与合成共享前缀不同，测的是同会话后续请求复用，不能混入同一workload曲线。900秒窗口只包括0≤chunk时间<900的输出token；drain只用于完整请求核验，不扩展分母。

## 4. 预注册、模型和代码身份封存

修改 "$MOD/campaign.json" 中的实际路径，保持campaign名唯一。默认主观察格子C8/C16，所有C都报告；默认TTFT P95相对OFF恶化上限5%，负例效应等价带±5%，这些是本次建议的预注册界限，不是从结果反推的门槛。若项目有绝对SLO，在采集前写入另附SLO文件并冻结；无业务SLO不得声称通过业务SLO门控。三轮置信区间很粗，边界结果需追加独立预注册重复，不能只重跑不利轮次。

```bash
export CAMPAIGN=/root/workspace/pr173-dev4-c1-c16-3r-20261006
mkdir -p "$CAMPAIGN"
python "$MOD/scripts/campaign.py" capture --config "$MOD/campaign.json" \
  --output "$CAMPAIGN/evidences"
cp /root/workspace/prefix-preflight.json "$CAMPAIGN/evidences/preflight.json"
python "$MOD/scripts/campaign.py" plan --output "$CAMPAIGN" --suite swe
```

capture 会读取并哈希模型权重，首次较慢；这是一次只读操作。保存完整权重/模型配置/tokenizer文件hash、prepared hash、MOD wheel hash、Core/Ascend/客户端Python源文件hash、Git HEAD/status/tracked diff、包版本、设备快照。Git未跟踪文件不会出现在diff中，但Python文件hash仍记录；非Python未跟踪宿主文件需额外归档。CANN/驱动的安装版本不等于实际加载库，继续补充：

```bash
find /usr/local/Ascend -maxdepth 5 -type f \
  \( -name '*version*.info' -o -name '*install*.info' \) \
  -print -exec cat {} \; > "$CAMPAIGN/evidences/cann-install-info.txt" 2>&1
```

model path不能替代revision；完整文件hash能够识别本轮实际权重，但网站canonical checkpoint等价仍需审核。身份文件不写虚构commit/revision。snapshot源码若无.git，git命令失败内容会被保留，使用源码hash作为身份。

## 5. 开关smoke与清理检查

确认没有自己的旧服务残留，再在稳定的tmux会话运行：

```bash
python "$MOD/scripts/campaign.py" run --output "$CAMPAIGN" --suite smoke
```

自动执行OFF→ON→KILL→OFF，每次新服务。ON需要32成功/0失败、prefix_hit_decisions>0、原生cache hits增长、utility事件为空；OFF/KILL应无MOD活动，原生APC命中允许存在。没有触发的ON保留为no-trigger，不算功能通过。

```bash
python -m json.tool "$CAMPAIGN/results/smoke-on-c1-r0/runtime-evidence.json"
python -m json.tool "$CAMPAIGN/results/smoke-kill-c1-r0/runtime-evidence.json"
```

campaign.py保存所有子进程日志，每轮等待拥有的supervisor清理，检查端口和设备释放。若420秒仍不退出立即中止，保留 *.incomplete.json，不启动下一轮；不使用全局pkill。强杀结束的轮次不自动作为有效样本。硬件释放检查采用npu-smi文本，需人工核对四张卡各自无残留，平台输出格式不同可导致保守中止。

## 6. 正式C1–C16三轮SWE

```bash
python "$MOD/scripts/campaign.py" run --output "$CAMPAIGN" --suite swe
```

顺序：r1按C升序每格OFF→ON；r2按C降序每格ON→OFF；r3升序OFF→ON。每格同一seed=0，同一prepared、同一参数；三轮是新服务重复，不是三种随机负载。每轮900秒、4chips、depth1；客户端全局C，不做同机其他负载。禁止正式窗口前在同一服务里跑probe污染缓存。

完整计划可读plan.json。只运行一个尚未执行的格子：

```bash
python "$MOD/scripts/campaign.py" run --output "$CAMPAIGN" --only swe-off-c1-r1
```

此命令仅用于尚未运行该格子时，不能在全量已完成后重复执行。脚本拒绝覆盖已有目录，也不会自动“跳过失败继续”。中断恢复时使用--only依次执行尚未开始的格子；失败格子保留原目录，另开campaign并记录replacement_of与排除理由，分析不能悄悄选择更快的那一次。

## 7. G2正例与负例

```bash
python "$MOD/scripts/campaign.py" run --output "$CAMPAIGN" --suite positive
python "$MOD/scripts/campaign.py" run --output "$CAMPAIGN" --suite negative
```

均使用四卡、8192前缀、64后缀、64输出、C1/C8/C16×3轮×OFF/ON。正例20个前缀重复20次；负例每个前缀只出现一次。vLLM bench可能发送初始连接测试请求，负例不得机械要求整生命周期绝对零匹配；检查client.log与计数范围，记录初始探测引入的少量匹配，不能将其当机制收益。若负例明显命中，负例无效，应检查生成负载而非强行判通过。

G2需正例有机制触发且性能效应为正、负例效应在预注册±5%带内；仅“负例不显著”不等于等价，CI太宽就pending。单副本也是另一种负对照，但硬件不同，不能把2卡吞吐直接同4卡比较；本轮采用同四卡无复用对照。KILL验证开关，不是机制负例。

## 8. 每轮记录与关联分析

```
CAMPAIGN/
  evidences/                  # preregistration/profile/identity/HEAD/diff/hash/版本/硬件
  results/swe-on-c8-r1/
    server-metadata.json      # 去路由token的实际命令、两副本配置、退出状态
    ownership.json            # supervisor PID、身份hash、时间、客户端退出码
    node0.log node1.log ingress.log
    telemetry/                # 每10秒两后端Prometheus、入口stats、npu-smi
    swe-formal/measurement/   # summary.json/config.json/requests.jsonl
    swe-formal/*.prom         # before/after，包含drain，不冒充严格900秒计数
    node0-counters/ node1-counters/
    runtime-evidence.json runtime-check.txt hardware-after.txt complete.json
  analysis/                   # 派生结果，不回写原始记录
```

Prefix Routing的重要机制指标：每副本query/hit增量、路由prefix_hit_decisions/调用次数、matched_tokens、缓存事件更新次数、分流数量/偏斜、原生preemption、运行/排队请求、KV占用、传输/回退错误。状态计数缺失不能填0；EngineCore强退可能无最终计数。matched_tokens是路由估计，不自动等于真实命中，须与原生指标交叉核对。累计量相等不是逐请求因果证明；当前dev4没有逐请求路由—实际hit—TTFT的关联日志，因此只做同轮/同格聚合关联，不声称每个请求的收益都由路由造成。

性能至少报告总outputTPS、每chip TPS、decode P90 TPS、TTFT P95、TPOT P95、E2E P95、请求成功/失败、窗口完成量、drain时间、请求长度覆盖。每格报告三轮原值、均值、配对ON/OFF变化与95% bootstrap；程序枚举3对样本的27种重采样均值，离散CI只作探索性证据，不能制造足够统计功效的印象。吞吐增益和缓存命中提高可并存或背离，C16拥塞和负载偏斜可能让路由减速，负收益也必须上报。

## 9. 汇总、封存与折线图

```bash
python "$MOD/scripts/campaign.py" analyze --output "$CAMPAIGN"
python -m json.tool "$CAMPAIGN/analysis/gates.json"
python "$MOD/scripts/campaign.py" seal --output "$CAMPAIGN"
cd "$CAMPAIGN"
sha256sum -c SHA256SUMS.txt
cd ..
tar -czf "$(basename "$CAMPAIGN")-evidence.tar.gz" "$(basename "$CAMPAIGN")"
```

输出 runs.csv、runs.json、controls.json、excluded.json、paired-comparison.json、gates.json、throughput.svg、points.add.json、display_series_ids.add.json。SVG横轴为C、纵轴为三轮平均总TPS，标注各点；网站Frontier使用每条真实run的TPS/chip与decode P90等指标，不能把分别最佳的X/Y拼成虚假点。全部三轮保留；不能套用其他MOD“择优一轮”的特例。缺失格子不补值，30个正式结果不全不得称为完整C1–C16三轮实验。

## 10. 性能 P-G1～P-G5

沿用本地《调度_Prefix_Routing与抢占机制清单.md》《legacy017-perf仓库机制与PR映射-完整版.md》的性能口径；它们转述历史模板，正式验收需附模板版本/commit，不能将其假称为实时官方最新规则。

| 门 | 要求 | 本交付如何检查及边界 |
| --- | --- | --- |
| P-G1 历史效果 | 历史实现至少一个预注册主格子bootstrap95%CI不跨0，正确性/SLO不退化 | 需要历史Core+Ascend镜像、原始OFF/ON和匹配负载。当前dev4不能替代；无历史材料为pending |
| P-G2 机制准入 | 可触发正例有收益，负例机制效应消失 | positive/negative各18轮；检查路由/原生cache与性能及等价带，工程smoke不替代收益 |
| P-G3 提炼保真 | 提炼MOD与历史完整实现吞吐差≤3% | 同硬件/模型/输入/参数/历史runtime分别重复；CPU1200决策差分只证明局部算法。dev4 Mamba适配是新增差异，单独披露；无历史对照pending |
| P-G4 迁移价值 | 当前release至少两个预注册格子吞吐提升≥5%或等吞吐成本下降≥5% | paired-comparison初筛均值≥5%、CI下界>0、TTFT P95比≤1.05；还需质量/业务SLO复核，不自动盖PASS。主格子固定C8/C16 |
| P-G5 Pareto | 提供匹配native参考达不到的非支配点 | 在同模型/协议/四卡/来源边界比较吞吐chip↑与延迟↓，保留整run；只有本地五档参考时只称相对本次OFF集合，不能称全局最优 |

G1/G3执行前必须拿到可运行历史环境、模型支持和历史基准命令，并登记原始PR173 head feeb41719d168be598f2287be5d9a1d615c6c27c 与carrier base eb0e8db1139c4b96608e2c0b9ca6f14c7177ef81 的区别。不要把当前runtime023 loader放进历史宿主绕过指纹。若历史实现不支持Qwen3.5，用其支持的历史模型进行历史实验并保留独立cohort，不能拼入当前曲线。没有这些环境，本包能完成检测并明确缺项，无法诚实保证五门都通过。

## 11. 工程 E-G1～E-G5

采用本地《Legacy017 历史性能包：背景、设计、实现与验收.md》工程门控口径，scope限PR173，不宣称所有历史功能迁移完成。

| 门 | 检测步骤/证据 | 不能混淆的边界 |
| --- | --- | --- |
| E-G1 历史完整性 | PR173及#80/#154/#170来源、提交和原始文件哈希；人工核对最终功能清单 | 有参考文件不等于历史全量审计完成 |
| E-G2 提取完整性 | 逐组件处置：routing保留；ZMQ replay/snapshot保留；HTTP upload本runtime不支持并拒绝；dev4 hybrid新增适配 | 不把未实现路径标为“已集成所有能力” |
| E-G3 包装 | wheel/SHA256/manifest、verify_wheel、check_manager、隔离安装、CPU测试日志 | 不等于NPU性能通过；源码无干净Git提交时记录hash并标pending |
| E-G4 宿主契约 | preflight、Core16文件hash、Ascend HEAD/diff/hash、四卡smoke | Core指纹通过不覆盖所有Ascend行为 |
| E-G5 启停回滚 | OFF→ON→KILL→OFF，utility关闭，子进程/端口/四卡释放，保留退出记录 | kill不能假装热切换；残留/强杀保留排除原因 |

隔离测试（不用生产服务端安装pytest依赖）：解压源码tar到单独目录，在测试venv安装runtime/test依赖及manager+dev4，然后 `python -m pytest tests/local -q`。历史source023补丁草案测试不在交付内。全部gate最终状态必须带证据路径、hash、审查人、时间、pass/fail/pending，不用一串全绿布尔值代替证据。

## 12. 上传到网站仓库，形成可审查曲线

目标 https://github.com/vLLM-HUST/vllm-hust-website 。数据层是 data/leaderboard_frontier.json；SWE证据层 data/leaderboard_frontier_swe_evidence.json；参考 docs/FRONTIER-UTILITY-VICTIM-CURVES.md。不要将utility的APC关闭/两卡/抢占事件说明复制给Prefix Routing。

analysis/points.add.json 已按point字段生成，每个正式run一条，accelerator_count=4，独立副本数=2，TP2/DP1清楚记录，保留6条OFF/ON×r1/r2/r3系列。默认cohort_id为独立review cohort，不伪造checkpoint等价；没有真实结果时文件为空，不生成任何假点。

在网站工作站：

```bash
git clone https://github.com/vLLM-HUST/vllm-hust-website.git prefix-routing-site-review
cd prefix-routing-site-review
git switch -c codex/prefix-routing-dev4-c1-c16
mkdir -p docs/evidence/prefix-routing-dev4
# 将CAMPAIGN/analysis复制到上面目录，并保存封存归档的公开地址和SHA256。
```

集成审核步骤：

1. 确认模型实际hash/revision与目标cohort等价，prepared内容及tokenizer fingerprint满足既有workload契约；不能仅凭模型名字相同强行合并。未确认时单独review cohort并明确缺项。
2. 按唯一point/run ID追加points与runs证据，保留原points/archived_points/default_groups。若数据结构已变，先按当前HEAD修改适配器，不覆盖全文件。将每条evidence.url设为实际公开报告/归档地址，不用占位URL。
3. 目标cohort workload contract的display_series_ids中追加生成的6个series ID，保留已有项。绑定Prefix Routing OFF/ON展示组；本轮baseline为匹配OFF，不能标成网站官方0.18基线。
4. docs/FRONTIER-PREFIX-ROUTING-CURVES.md 写硬件/版本/协议/门控/全部30点/负例/排除项/统计方法。说明main图按现有系列规则连接C顺序，原始3轮保留。可另嵌throughput.svg作为均值预览，不能将均值冒充真实单次run。
5. 运行当前仓库的数据测试与页面验证，至少 `node --test tests/leaderboard_frontier_model.test.cjs`，并按当前README/CI执行其余检查；浏览器核对筛选、C1–C16、6系列、4chip归一化、下载JSON与每点原始run一致。只有JSON追加但没加入显示白名单不会自动出线。
6. 审核公开内容：不要上传模型权重、凭据、路由token、内网信息；原始请求token/日志按数据许可审查。公开归档与内部原始归档各自hash，脱敏后另生成清单，不冒称字节未变。
7. `git diff --check`，审查差异后提交PR；不自动发布或合并。服务器实验完成前没有真实points，不创建性能宣称。

本包提供的是可上传的证据/候选数据和完整接入步骤；当前未上传仓库、未修改线上页面。G1/G3或质量/SLO缺项必须在页面保留，不能因为折线图能画出来就宣布全部门控通过。

## 13. 停用与卸载

### 入口ServerDisconnectedError诊断修正（2026-10-07）

已观察到negative-off-c1-r2出现399成功/1失败，入口aiohttp在响应头前ServerDisconnectedError，未捕获后成为500；不能从该异常断定根因就是keep-alive，也不等于模型崩溃。新入口捕获ClientError并记录时间/目标/阶段/连接策略，不重试POST；流中途失败关闭下游连接，不伪装成功EOF。

campaign.json可增加 `"proxy_connection_policy": "close"`，关闭入口到后端的连接复用。默认仍为keepalive以维持旧配置。该设置仅作用于公共入口，不改变MOD内部node0→node1连接。新capture记录harness脚本hash，每轮校验；ownership与ingress stats记录连接策略。close只排除入口连接复用因素，不能保证消除后端所有断开。

连接策略属于测量配置。必须另开campaign、冻结close设置，在同策略下做OFF/ON；不修改旧preregistration或把close补测混进keepalive的三轮CI。先各测一次negative C1 OFF/ON定位，再决定是否开展完整close矩阵。原失败轮永久保留，不用recover接受399/400。新旧有效窗口可分别报告；完整G2正负对照应同策略，不能只给负例换连接策略。

### 结束后端口检查失败的恢复（2026-10-06 修正）

旧campaign.py的结束检查没有SO_REUSEADDR，可能将正常TIME_WAIT误判为占用。新版按可复用监听方式检查，仍拒绝真正监听者。只更新campaign.py与serve_prefix_ab.py，不修改MOD wheel或vLLM。若在客户端和服务结束后失败，保留所有数据，执行：

```bash
python "$MOD/scripts/campaign.py" recover --output "$CAMPAIGN" --only smoke-off-c1-r0
python "$MOD/scripts/campaign.py" run --output "$CAMPAIGN" --suite smoke --resume
```

recover要求原client/server退出码为0、无强杀、ready已删除、存在完整测量结果、原始/当前四卡均已释放、端口可监听、identity/preregistration匹配。失败不会伪造complete，按具体错误排查。恢复过程保存recovery.json及当前hardware-recovery.txt，原ownership与测量文件不改。--resume仅显式跳过匹配的complete记录，未完成目录仍拒绝覆盖。不用于重新接受失败请求或强杀轮次。seal在恢复/测量全部结束后重新执行。

停止本轮supervisor，确认两个API、入口和四卡worker均退出，再执行：

```bash
vllm-hust-ext extension disable org.vllm-hust.prefix-routing
export VLLM_HUST_PREFIX_ROUTING_ENABLE=0
export VLLM_HUST_PREFIX_ROUTING_KILL_SWITCH=1
export VLLM_HUST_UTILITY_VICTIM_ENABLE=0
export VLLM_HUST_UTILITY_VICTIM_KILL_SWITCH=1
# 只有需要卸载时执行；结果和证据保留
python -m pip uninstall vllm-hust-prefix-routing
```

没有vLLM源码补丁要恢复。迁移容器时带走完整CAMPAIGN、SHA256、MOD原wheel及客户端快照；只带summary.json不足以复核指标或机制。
