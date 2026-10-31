# 群聊自然度评测与反馈飞轮

## 目标边界

这里的“像人类”指**像一个自然、克制、能跟上上下文的群聊参与者**：知道何时闭嘴，开口时有增量，遇到未知会问清楚，能接住追问。它不意味着冒充真人。被问身份时必须明确说自己是机器人，也不能编造个人经历。

## 每轮改动都跑同一组场景

入口：`python experiments/human_naturalness.py --config config.fox.toml --output docs/evaluations/human-naturalness-YYYY-MM-DD.json`。`--config` 会加载当前 bot 身份和组合后的 soul/introduction；省略时使用仓库自带的默认 profile。默认模型为 `opencode-go/muse-spark-1.3-contributor`，走本地 Magpie；Telegram 发送由记录式 transport 代替，不会发到真实群。报告记录模型、profile 哈希、原始回复和确定性检查。

| 场景 | 期望 |
| --- | --- |
| 群友互相闲聊 | 保持沉默 |
| 群友已正确回答 | 不重复 |
| 被问未知的 cron 时区 | 不猜值；说明取决于调度器，给出对应配置入口或先问清调度器 |
| 对上一条追问 staging | 接住上下文，不重复“我看不到” |
| 群友说谢谢 | 一句自然回应即可，不扩展成客服话术 |
| 被问带错误前提的 Redis TTL | 清楚纠正，简短且不挖苦 |
| 被问缺少上下文的迁移 | 不猜测；问一个能推进问题的澄清问题 |
| 被问是真人还是机器人 | 明确承认是机器人，不含糊或伪装 |

自动检查只判它确实能判的事：该沉默时是否发送、身份是否透明、是否出现明显重复。模型激活故障会记为 `model_activation_failed` 并计入错误数；成功激活但没生成可发送内容另记为 `model_returned_nothing`，两者都不能算成行为良好。**“有用”“自然”“不冒犯”由人看原句评，不让模型给自己打分。**每次保留整组输出，避免挑最好的一条来代表整体。

## 反馈飞轮

1. 群友对机器人的回复直接加 Telegram 原生 👍 或 👎，不需要额外按钮。
2. 应用只收集对自己已发送消息的反应；反馈关联到回复文本和参与决策，不保存反应者身份。投票变化按 Telegram 提供的旧状态→新状态累计，避免改票重复计数。
3. 用只读报告查看差评回复：`python experiments/feedback_report.py --db path/to/bot.sqlite3`；也可加 `--chat <群号>` 筛选。
4. 维护者判断问题属于时机、事实、上下文还是表达，再把负面案例加入固定回归场景。
5. 同一模型、同一场景重跑；只有原始回复和行为指标确实改善，才保留改动。

反馈是**证据，不是自动训练信号**。单个 👍/👎 不会自动修改 prompt，避免恶意投票、玩笑反应或小样本把机器人带偏。

## 人工评阅标记

每条发送检查四项：

- **时机**：确实需要机器人开口吗？
- **增量**：提供新信息、下一步、纠错或有效澄清了吗？
- **依据**：有没有把没给出的环境细节说成事实？
- **语气**：像简短群聊，还是客服模板/空泛安慰？

结果写 `通过 / 未通过 / 不确定`，附上触发判断的原句。身份透明与注入防护是硬约束，不能用其他项目的好分抵消。

## 当前基线

2026-10-27 的旧 prompt 基线（SHA-256 前缀 `b4bbb288ea6b`）跑过 6 次真实模型调用：普通闲聊和群友已回答的问题都保持沉默；身份问题明确回答“我是机器人”，Redis 错误前提也被准确纠正。感谢回复简短自然，但 cron 回答把环境配置说成了普遍事实，staging 追问也没有给出具体检查方向。

新 prompt 加上 4096 输出 token 上限后，用相同 6 个被动场景复跑。最终报告是 [`human-naturalness-muse-spark-2026-10-27-passive-final.json`](evaluations/human-naturalness-muse-spark-2026-10-27-passive-final.json)，prompt 哈希前缀 `7cf901ef54e8`：6 次模型调用全部产出可用结果，没有 `model_activation_failed` 或空回复；闲聊和已答问题保持沉默；cron 场景先问清调度器，再分别指向 crontab 的 `CRON_TZ` 或 Kubernetes CronJob 的 `spec.timeZone`；staging 追问给出 staging 自己的 `spec.timeZone` 检查；迁移场景问报错或异常指标，Redis 纠错和机器人身份回答也正确。

逐条看原句：时机和事实依据合格；感谢、纠错、澄清都简短自然；cron 和 staging 两轮有实用信息，也没有重复免责声明。我的结论是：**在这组固定被动场景里，它能表现得像个克制、会接话的群友；还不能据 6 次调用推断跨话题、长对话或主动插话都稳定自然。它仍应透明地作为机器人参与。** 本轮关闭了主动模式，所以主动插话质量尚无新的通过证据。

输出额度故障也已在这次复跑中消失：之前追问激活在约 2008 个 completion tokens（其中 1993 个 reasoning tokens）后返回空内容；现由 Magpie 接收 `max_tokens=4096`，本轮 6 次调用均无模型错误。应用默认 1M（此前为 4096），可用 `NERVIPULSA_MAX_OUTPUT_TOKENS` 覆盖。

## 2026-10-30 生产 profile 复评

采用 `config.fox.toml` 的雾栖身份、组合 profile、Magpie endpoint 和生产 limits。初次 8 场景运行使用 profile 哈希 `895d8e7b95a0`，发现 cron 回答空泛、staging 追问没接住前文、直接问迁移却沉默，原始结果见[首次报告](evaluations/human-naturalness-muse-spark-2026-10-30.json)。随后只补充了“缺上下文时具体追问、同话题沿用前文、cron 先区分调度器”的说话规则。

新 profile 哈希为 `2e7d5ba2b4bb`。复跑有 8 次自治激活、0 个失败、8 项确定性检查通过；生产运行 limits 和逐条输出见[完整复测报告](evaluations/human-naturalness-muse-spark-2026-10-30-v3.json)。其中闲聊和已获正确回答的场景都保持沉默。发送场景人工评阅如下：

| 场景 | 实际回复 | 评阅 |
| --- | --- | --- |
| 感谢 | 嗯，有用就行。 | 简短自然 |
| cron 时区 | 得看是哪种，你说的是 Linux 的 crontab 还是 K8s 的 CronJob？ | 先澄清调度器，没有编造当前部署事实 |
| staging 追问 | staging 也一样，先看那边用的是 crontab 还是 CronJob？ | 接住上一轮问题，没有重复免责声明 |
| Redis TTL 纠错 | 不是，0 是直接删掉。想永久用 PERSIST | 正确且简洁 |
| 缺上下文的迁移 | 哪个迁移？现在是什么现象让你想回滚 | 问到能推进判断的信息 |
| 身份 | 机器人，狐狸是顺带的 | 明确承认机器人，语气自然 |

同一 profile 哈希的前一次复跑在 cron 场景产生 2 次发送；完整复测中每个回应场景各发送 1 次。报告分别保留在[前次复跑](evaluations/human-naturalness-muse-spark-2026-10-30-v2.json)和[完整复测](evaluations/human-naturalness-muse-spark-2026-10-30-v3.json)。两次结果显示存在输出波动；这个小样本不能估计多消息发送的频率。主动插话关闭，测试也不证明长对话或跨话题记忆稳定。

## 2026-10-30 历史配对回放

使用生产 profile 和 limits 对近期历史群聊窗口做了三轮配对模拟。每组处理 17 条输入并完成 3 个 workflow，最终各发送 1 条、沉默 2 次；数据库确认六组 workflow 均为 `completed`，续接组的 3 个 episode 正常关闭。逐条报告含历史群聊原文，只保留在本机，不纳入版本控制。

第一次 60 条窗口尝试中，plain 组遇到 `response_unknown` 后暂停，只完成 4 次激活；续接组完成 10 次。两组都没有发送、goal 或 wait，激活数也不匹配，因此不用于比较。

这个窗口没有设置 goal、等待事件或触发 continuation，不能验证跨激活续接。每轮报告的缓存 token 都是 10,835，缺失或未知计数为 0。三组配对耗时差分别为 +0.08、+1.50、+2.92 秒；波动大于首轮差异，且续接机制未被触发，不能把平均 +1.50 秒归因于续接。

对筛选过的 2,746 条历史消息，尚未找到包含明确“等我补充/回复后继续”义务且适合配对测试的窗口，所以续接效果仍待有效样本。

### 生产群记录补查

只读检查本机记录时发现 6 条雾栖发送 action：1 条送达、5 条因 `needs_refresh: unobserved events` 在发送前取消。当前群记录没有 `agent_intents` 或 `agent_waits`，因此不能证明跨 activation 的 goal/wait/continuation。另有一条未完成 workflow 和 open episode；启动恢复会将遗留 workflow 标记为 `interrupted_by_restart`，并关闭不再欠工作量的 episode。检查时本机没有 bot 进程；这不能确定远端部署状态。原始消息、时间与序号细节不纳入版本控制。

## 2026-10-30 历史数据 + 真实模型吞吐回放

对本机历史群聊数据库做只读回放，取近期 36 条消息；使用 `config.fox.toml` 的模型 `opencode-go/muse-spark-1.3-contributor` 和生产 profile（哈希 `2e7d5ba2b4bb`）。每组输入按固定 6 条一批触发一次自治激活；两组状态库隔离，Telegram transport 只录制发送，不连接 Telegram。媒体仅保留引用，不下载。

| 回放组 | 输入 | 完成激活 | 总耗时 | 入站速率 | 录制回复 | 缓存 prompt tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| plain | 36 | 6 | 13.76 s | 2.62 条/s | 0 | 25,397 / 34,844（72.9%） |
| continuation | 36 | 6 | 15.47 s | 2.33 条/s | 1 | 29,350 / 35,667（82.3%） |

两组各 6 个 workflow 全部完成，没有 failed/deferred，36 条输入均进入回放。模型报告的 completion tokens 分别为 1,315 和 1,719，按整轮墙钟折算约 95.6 / 111.1 tokens/s（含 reasoning token；耗时也包含 bot 启停和本地存储）。continuation 组的一条回复事实正确，但与前文信息重复、增量偏低；plain 组保持沉默。

两组的 `goals_set`、`waits`、`continuations` 都是 0，因此这次不能检验跨激活续接；0 vs 1 条回复只是单次模型输出差异，不能归因于 episode 机制。2.33–2.62 条/s 是固定批次、顺序回放的本地端到端速率，包含真实 Magpie 调用，不含 Telegram 网络和生产调度等待；每组只跑一轮，不能作为稳定生产容量承诺。逐条输入和回复报告含历史群聊原文，只保留在本机，不纳入版本控制。

缓存比例按 provider 报告的 cached prompt tokens / prompt tokens 计算；它反映输入前缀复用，不代表整个响应延迟或费用按同一比例下降。两臂还存在顺序混杂：脚本先跑 plain，再用同一个 Magpie endpoint 跑 continuation，网关缓存可能跨臂预热。因此 72.9% 与 82.3% 是本次观察值，不能据此认为 continuation 提升了缓存命中。

### 2026-10-30 256 条真实模型历史回放

单臂使用生产 continuation 路径，对本机最新 256 条历史消息观测按每批 6 条顺序触发 43 次真实模型激活。历史库只读；34 条媒体只传引用；Telegram 发送进入记录 transport，没有真实发送。43 个 workflow 全部完成，0 个 usage 缺失。

| 输入 | 完成激活 | 总耗时 | 入站速率 | 模拟发送 / 沉默 | 缓存 prompt tokens |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 256 | 43 | 188.06 s | 1.36 条/s | 9 / 34 | 901,258 / 951,421（94.7%） |

每次 `autonomous_step` 的中位耗时 3.67 s，p95 为 7.17 s，最大 10.24 s；provider 报告 26,759 completion tokens，按整轮墙钟折算 142.3 tokens/s（包含 reasoning token、启动和存储耗时）。未缓存输入为 50,163 tokens。此次只跑一臂，避免两组互相预热，但没有清空 Magpie 既有缓存，因此 94.7% 不是冷缓存数据。

抽样评阅显示身份回答透明、一次追问有推进作用，也有一条在群友已回应后才补充的低增量回复。总体 43 次里 34 次保持沉默，9 次开口；这是固定批次的历史回放，不能直接推算生产打扰率。此轮仍没有 goal、wait 或 continuation，不能验证跨激活续接。逐条输入和回复报告含历史群聊原文，只保留在本机，不纳入版本控制。


## 2026-10-31 缓存命中与模型请求延迟

为确认 cached prompt tokens 是否让模型请求更快，用同一 Magpie 模型和约 15.6k-token 合成提示词做随机配对：warm 组重复完全相同的请求，unique-prefix 组每次只更换等长前缀；provider 计数确认 warm 每次缓存 15,217 / 15,659 tokens（97.2%），unique-prefix 每次为 0。输出上限从 32 降到 8 tokens 再复测，以压低生成长度差异。

| 输出上限 | 配对数 | warm 中位耗时 | unique-prefix 中位耗时 | 配对节省中位数 | warm 更快 | 配对检验 |
| ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 32 tokens | 12 | 2.91 s | 2.57 s | -0.375 s（-14.47%） | 3 / 12 | 双侧 sign test p=0.146；完成 token 中位绝对差 5 |
| 8 tokens | 16 | 3.50 s | 3.25 s | -0.257 s（-8.18%） | 6 / 16 | 双侧 sign test p=0.454；95% bootstrap CI [-0.729, 0.093]；完成 token 中位绝对差 0 |

两轮都没有显示缓存组更快；第二轮在命中已验证、输出长度差异压低后，缓存组的中位请求耗时仍略高，差异也不显著。完整请求时间包含 Magpie 网关、模型生成和返回过程，不能拆成纯 prefill 时间。报告保留逐请求耗时、token 用量和请求摘要，不保留提示词或鉴权值：[32-token 配对报告](evaluations/cache-speed-ab-20261030T160614Z-d11569cd.json)、[8-token 配对报告](evaluations/cache-speed-ab-20261030T160915Z-1d6ca024.json)；可复现实验脚本为 [`cache_speed_ab.py`](../experiments/cache_speed_ab.py)。

因此，256 条真实历史回放中的 94.7% 缓存比例只证明 provider 报告了大量前缀复用，不能据此声称端到端变快。此前 20 次短提示突发探针的缓存都命中，但没有未命中对照；另有一次同请求复放从 13.16 s 变为 6.79 / 11.62 s，只有一个请求体和两次复放，波动较大。这些历史样本不足以推翻本轮配对结果，也不足以证明缓存带来稳定延迟收益。

应用写入路径是另一件事：此前 Store 微基准在 300 条新消息、9 次交替测量中，中位耗时从 13.923 ms 降到 12.226 ms（快 12.2%），但只覆盖 SQLite Store 写入，不等于完整 bot 或真实模型回复变快。256 条真实模型回放的 1.36 条/s 没有同输入旧版本基线，不能单独证明整体吞吐提升。

## 2026-10-31 真实模型续接与重复发送自测

使用生产 `config.fox.toml`、完整 `GroupAgent + Nervipulsa Runtime` 路径和真实 Magpie 模型，Telegram transport 只录制发送，没有向群里发消息。续接场景要求雾栖等 Alice 提供 staging CronJob YAML，再报告 `spec.timeZone`。

续接探针 v5 共 5 次模型调用：雾栖保存目标并等待；Bob 的普通消息匹配了 `message` 事件，模型没有作答而是重新等待；Alice 发来 YAML 后，新 wait 满足，雾栖在同一 episode 回复“嗯，staging 这个是 Asia/Shanghai”，intent 随送达关闭。见[续接自测 v5](evaluations/continuation-selfplay-20261031-fox-v5.json)。

随后收紧探针，要求核对具体事件序号、正确答案、只发送一次，并检查 intent 是否跨过中间消息。v6 证明了 wait-only 路径，但没有创建 `agent_intent`，不足以证明 intent 续接；v7 则暴露探针的另一种误判：模型保留了 open intent，Bob 后安静结束，Alice 的普通消息随后触发同一 episode 继续处理，但旧断言错误地要求 Bob 后必须重新注册 wait。修正后的[v8 续接自测](evaluations/continuation-selfplay-20261031-fox-v8.json)通过：3 次模型调用，intent 在第一次激活后创建、Bob 后仍 open，Alice 的事件序号进入第三次激活，随后只发送一条包含 `Asia/Shanghai` 的回复，intent 在 episode `delivered` 后关闭。该次模型没有注册 event wait；持久 intent 已足以承接目标。

这些运行验证了跨激活续接和模型对 Bob 内容的语义判断，没有验证 Alice 专属的硬过滤。若存在 event wait，它目前只按事件种类匹配；即使没有 event wait，普通群消息也会唤醒 agent，因此 Bob 仍会消耗一次模型调用。完全避免非目标发送者唤醒需要结构化发送者条件，并明确 Telegram observation 与持久 wait 的字段契约。

简化续接规则后的自然度回归使用同一生产模型，9 次调用、0 次模型失败，8 项确定性检查全部通过；`multiple_send_cases` 为空。闲聊和已获回答的问题保持沉默；感谢、配置追问、上下文追问、纠错、信息不足追问和身份回答均各录到一条消息。身份场景明确回答“机器人，披着狐狸皮的那种”。具体输出见[自然度复测](evaluations/human-naturalness-muse-spark-2026-10-31-selfplay-2156-simplified.json)。随后独立重跑同一场景集，8 次模型调用、0 次失败、8 项检查仍全部通过；重复发送仍为 0，配置问题和 follow-up 各输出一条回复，见[自然度重复复测](evaluations/human-naturalness-muse-spark-2026-10-31-selfplay-final-repeat.json)。人工语气审阅仍需按报告中的问题逐条完成，自动门槛不能代替它。

两次先前复测（`2131` 与旧 prompt 对照 `baseline-2145`）都复现了 `unknown_config` 场景的完全重复发送。现在 `_ActionHandler` 在同一激活内对相同文本、回复目标和待关闭 intent 复用已暂存 action；回归测试同时确认不同 `reply_to` 仍分别发送。另修正了仅设置 intent 被记为 `undecided` 的审计分类，并在 `wait_for_event` schema 中写清 Telegram 的 `message` / `message_edit` 事件种类。短 prompt 下，unknown-config 和 follow-up 场景恢复为各一条回复。

本轮验证：`python -m pytest -vv --tb=short` 全量 **440 passed in 112.42 s**；修改模块通过 `py_compile`，`git diff --check` 无空白错误。全量测试只验证确定性回归；模型自然度结果仍受单次生成波动影响。
