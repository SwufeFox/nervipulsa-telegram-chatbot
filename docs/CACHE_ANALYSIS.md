# 缓存命中下降：实验记录与证据边界

更新时间：2026-10-28。模型：`opencode-go/muse-spark-1.3-contributor`，通过本机 Magpie OpenAI-compatible 端点调用。除特别标注外，群聊由脚本生成，Telegram 使用录制替身，未向真实群发送消息。

## 当前结论

2026-10-28 的一段 49 轮长对话中，末次模型调用的 `cached_tokens` 确实从上一次的 3569 降到了 0。但该次运行没有保存发送到端点的完整请求及前缀指纹，**现有数据无法确定这一次掉零的原因**。此前把它归因于服务端淘汰，并排除 TTL 或多副本，是超出证据的结论；在此更正。

App 会在需要回复时把最近群聊渲染成快照；Core 再将各次快照和模型输出保留在 transcript 中。重复文本确实存在，但已有实验证明不了所有重复都是必要的，也不能直接由字符数推算 token 或费用。

## 原始掉零与后续实验

### 原始长对话

[1M 预算运行的 JSON](evaluations/long-conversation-1m-2026-10-28.json) 和[聊天记录](evaluations/long-conversation-1m-2026-10-28.md) 记下两次相邻模型调用：

| 脚本记录的 turn | prompt tokens | cached tokens | 返回的缓存比例 |
| --- | ---: | ---: | ---: |
| 41 | 4016 | 3569 | 88.9% |
| 48 | 4448 | 0 | 0% |

这是一段单话题、串行的模拟群聊。日志没有保存这两次 HTTP 请求的完整消息、序列化参数或指纹，因此既无法比较它们的实际前缀，也无法核实 Magpie 或上游使用了什么缓存策略。这里的 `cached_tokens` 是端点返回的 usage 字段；它的计费含义和缓存实现不能只凭这一数字确定。

### 后续前缀探针是另一轮运行

[cache-probe.json](evaluations/cache-probe.json) 来自之后独立重跑的一次 49 轮脚本；记录的第 48 轮为 4873 prompt tokens、4337 cached tokens（89.0%），并非上表的掉零请求。

这次重跑中，相邻请求之前的消息内容按脚本比较保持相同，历史内容持续追加。但旧探针仅取每条消息的 `content` 比较，没有比较完整消息对象（例如 role 或其他字段），也没有验证 HTTP 请求体逐字节相同。它能描述本次重跑里被记录的内容，不能反推原始掉零请求是否改写前缀。

### 相同合成请求的间隔实验

以下是其他独立运行，使用短的合成请求，约 406 prompt tokens、`max_tokens=512`；与长对话的消息序列、长度及输出预算不同：

- [cache-ttl.json](evaluations/cache-ttl.json)：一次串行试验中，等待 30 秒后报告 0；之后再等待 60、120、300 秒的请求报告 369。每次请求都可能刷新或重建缓存，这不是从相同初始状态分别等待的独立对照，因此**不能排除 TTL**。首次、改 system 和追加消息的结果当时仅打印，没有保存在该 JSON 中。
- [cache-burst.json](evaluations/cache-burst.json)：20 次连续请求均报告 369 cached tokens。这只能描述这 20 次的结果，不能排除粘性路由、已经预热的多个副本或其他路由方式。
- [cache-gap.json](evaluations/cache-gap.json)：12 次请求中的最后一次报告 0，其余报告 369。脚本在响应结束后再等待 10 秒，实际请求起始间隔还包括生成耗时；这并非对原始长对话真实时间间隔的精确复刻。

旧脚本曾将缺失的 usage 字段与数字 0 合并处理；这些记录无法区分未返回字段和实际返回 0。即使请求体没有变化，端点内部是否发生淘汰、重建、路由切换或 usage 统计差异，也需要更直接的证据。

## 请求链路中可能影响前缀的因素

1. App 按 `(chat_id, topic_id)` 选择近期历史，清理与渲染后提交给对应的 Runtime。新快照会包含一部分旧快照已有的文本。
2. Core 的 transcript 保存 system 提示、之前的 user 快照和 assistant 结果。ContextProjector 还会在 system 之后插入运行状态消息；该状态若变化，会影响较靠前的消息。应用发送到 Telegram 的文本可能与模型原始回复不同，因而两边历史不能简单视为完全相同。
3. Runtime 重建后，旧 Core transcript 不会自动重建为原来的模型视图。接近 `context_limit` 时，摘要或压缩也可能更改历史；这个限制以 Core 计算的**字符数**计，而非模型 token 数。
4. 本机 Core adapter 将 `model`、`messages` 和已配置的 `max_tokens` 序列化为 HTTP 请求；工具非空时还会加入 `tools` 和 `tool_choice`。任何参与服务端缓存键或 token 化的字段是否起作用，需要对真实端点测量。

这些都是从代码得到的可能影响因素，并不是对原始掉零原因的证明。

## 重复行与静默消息

[cache-duplication.json](evaluations/cache-duplication.json) 的最后一次请求记录：`unique_chars=2484`，`repeated_chars=5286`，两者合计 7770；按该脚本口径，重复占 **68.0%**。分母是排除 system 消息、去除行首尾空白并忽略空行后计数的字符，包含 assistant、标记等非群友发言。`total_chars=12741` 则使用另一口径，包含 system 与换行等内容。

因此 68.0% 不是整个 HTTP 请求的重复比例，也不是重复 token 或可节省费用的比例；相同文本被不同人重复说出时也可能计为重复。

应用当前只在决定回复时调用模型；一个直接把历史全部删掉、只保留当前被回复 turn 的方案，会漏掉静默期间未另行提交的消息。[transcript-coverage.json](evaluations/transcript-coverage.json) 是基于脚本模型的两个末尾 projector 快照，使用存储文本子串匹配得到 full `50/50`、朴素 delta `10/50`；查询也包含 bot 消息。它说明朴素删历史有风险，但并非逐次请求的消息 ID 覆盖验证，也未测试「累积自上次提交以来的未发送消息」等方案。**不能由此断言所有重复都不可消除，或某种方案是唯一解。**

## 新增：真实请求体指纹与原样重发

2026-10-28 的另一次独立运行使用 [cache_e2e_v1.py](../experiments/cache_e2e_v1.py) 和[结果 JSON](evaluations/cache-e2e-v1-20261028T044354Z-e740f4f6.json)。脚本将 49 条既有台词输入 App，调用真实 Runtime 和本地端点，Telegram 仍为替身。它没有传入 Telegram 的 reply-to 元数据，因此不等同于原始长对话。共记录 9 次 Runtime 模型调用；其中一次非首次掉零后，探针另外原样重发了两次 HTTP 请求体。

| 请求（本次运行） | prompt tokens | cached tokens | 备注 |
| --- | ---: | ---: | --- |
| 上一次 Runtime 调用 | 2372 | 1905 | 9 条完整 messages |
| 掉零的 Runtime 调用 | 2777 | 0 | 11 条 messages，其中前 9 条完整对象与上次相等 |
| 同一个请求体，立即重发 1 | 2777 | 2673 | SHA-256 与掉零请求相同 |
| 同一个请求体，立即重发 2 | 2777 | 2673 | SHA-256 与掉零请求相同 |

探针在 HTTP 发送前读取实际序列化 body：将其解析后的完整 message 对象按字段比较，并计算 body 字节的 SHA-256；结果只保存摘要与计数，不保存请求正文、回答或认证头。相同 message 比较结果是在运行时计算并保存的计数，原始内容没有落盘，不能事后从 JSON 独立重算。usage 的缺失与 0 分开保存。

这组数据证明**本次复现**中，前一请求的全部 9 条消息保持相同，而且同一个序列化请求体可以先报告零、再连续两次报告 2673 个缓存 token。它不排除新增尾部、服务端分块、TTL、路由、重建缓存或 usage 统计等机制，也不能替原始掉零追认原因；重发本身可能改变了缓存状态。

[第二次独立运行](evaluations/cache-e2e-v1-20261028T044705Z-5cbbaf35.json) 同样有 9 次 Runtime 模型调用，首次报告 0，其余 8 次均报告非零，因此没有触发原样重发。首个请求并不一定是冷缓存：运行之前的服务端状态未知。两次运行的样本量都不足以估计稳定的掉零概率。

## Magpie 源码检查

本机可见的 Magpie 进程报告版本 `0.1.1311`；公开仓库的 `v0.1.1311` 标签指向提交 [`82fb070`](https://github.com/yetone/magpie/tree/82fb0702fc43fd8a12f4d69e279676f47baf175b)。读取本机可执行文件内嵌构建信息，`vcs.revision` 与该提交一致、`vcs.modified=false`。这提供了版本对应的依据，但不是构建过程的密码学证明，也不能确定本次请求走了哪个分支。

- [请求处理和模型路由](https://github.com/yetone/magpie/blob/82fb0702fc43fd8a12f4d69e279676f47baf175b/internal/gateway/gateway.go#L1429) 可以选择实际提供商与模型，并按协议选择直通或转换；[直通代码](https://github.com/yetone/magpie/blob/82fb0702fc43fd8a12f4d69e279676f47baf175b/internal/gateway/gateway.go#L4174) 至少会将入口模型名改为上游名称。相同入口 body 不保证内部路由或发送给上游的请求一定相同。
- 在[检查过的普通 Chat 直通整理逻辑](https://github.com/yetone/magpie/blob/82fb0702fc43fd8a12f4d69e279676f47baf175b/internal/gateway/chattidy.go#L118)中，没有发现重新计算并写回缓存数值的代码；这不是对所有适配器与分支的穷尽性保证。
- 在[协议转换和 usage 映射](https://github.com/yetone/magpie/blob/82fb0702fc43fd8a12f4d69e279676f47baf175b/internal/gateway/chat.go#L912)中，网关从上游多种 usage 字段归一化内部缓存读取数，再生成客户端看到的 `prompt_tokens_details.cached_tokens`。因此，上游字段缺失也可能在转换后表现为 0；这段代码本身没有根据提示词计算缓存命中。

未经认证检查网关的 `/health`、`/healthz`、`/metrics` 和 `/openapi.json` 均返回 404，尚未获得同次上游响应的 usage 或实际路由分支。

### 网关响应的路由标签

[路由探针](../experiments/cache_e2e_v2.py)记录实际响应中的 `X-Magpie-Provider` 和 `X-Magpie-Model`，将各自不同的值转换为仅在该次运行内有效的匿名标签；不会保存原始响应头或请求正文。它也保留请求体指纹、完整消息前缀计数、请求时间及 usage 是否缺失。当前脚本在一次运行中，最多对第一次非首次调用出现零值的请求重发两次。

两次 49 条脚本运行分别记录在 [050114Z 结果](evaluations/cache-e2e-v2-20261028T050114Z-c2b5be96.json) 和 [050243Z 结果](evaluations/cache-e2e-v2-20261028T050243Z-3d3cfb47.json)。每次各有 9 次 Runtime 调用，所有调用都返回了非零缓存计数；每次运行内部的提供商和模型标签均各自保持一致，因此没有触发重发。第二次运行的首个请求开始时，第一次运行的最后一个请求尚未结束，两次并非互不干扰的独立对照。标签按运行分别生成，不能从 `p1` / `m1` 的字面相同推断两次运行实际路由相同。

这两轮没有复现掉零，也没有覆盖 v1 已观察到的掉零与原样重发；它们无法确定掉零时路由有无变化。即使同一次运行的标记相同，也不能排除同一提供商内的不同副本、上游缓存重建、协议转换或 usage 映射等因素。

## 后续测量

若能访问网关或上游的缓存日志、请求路由和 usage 定义，可以通过请求时间和关联 ID 对照上述异常。进一步测试应使用可控的新前缀、独立和随机化的等待间隔，并保留完整消息结构指纹及缺失 usage 状态。没有服务端遥测时，仍不能直接证明内部淘汰、TTL、路由或计费规则；也不能恢复原始掉零时没有记录的数据。
