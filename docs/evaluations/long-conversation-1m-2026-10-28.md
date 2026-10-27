# 长对话实测记录

模型 `opencode-go/muse-spark-1.3-contributor`，history 24，max_output_tokens 1,000,000

## 吞吐汇总

| 项 | 值 |
| --- | --- |
| 轮次 | 49 |
| 决定回复 | 9 |
| 实际发出 | 9 |
| 沉默 | 40 |
| 计费模型调用 | 9 |
| 发送失败 | 0 |
| prompt 总 | 25,082 |
| 命中缓存 | 17,288（68.9%） |
| completion 总 | 9,894 |
| 其中推理 | 9,612（97.1%） |
| 延迟 均值/中位/p90/最大 | 8.23 / 7.3 / 11.59 / 14.21 s |
| 发出速率 | 7.29 条/分钟 |

## 对话

**alice**: deploy 挂了，rollout 卡在 3/5 ready
> · 沉默（`proactive_disabled`）

**bob**: 又是 probe 那个？
> · 沉默（`proactive_disabled`）

**alice**: 不像，这次 pod 起来就 OOMKilled
> · 沉默（`proactive_disabled`）

**bob**: 那内存 limit 谁改的
> · 沉默（`proactive_disabled`）

**alice**: 上周为了修 GC 停顿调到 512Mi，之前是 256Mi
> · 沉默（`proactive_disabled`）

**bob**: 先回滚吧，别在周五晚上查
> · 沉默（`proactive_disabled`）

**alice**: 回滚要多久，我看现在还留了两个 old replica
> · 沉默（`proactive_disabled`）

**alice**: @Alma rolling back a k8s deployment 的时候 maxSurge/maxUnavailable 会影响回滚速度吗
> 🤖 They do — a rollback is just a rollout to the old ReplicaSet, so the same strategy applies.  
> `6.597s` · prompt 1,244 (cached 1,137) · out 779 (reasoning 749)

**bob**: 我猜是 image pull 慢
> · 沉默（`proactive_disabled`）

**alice**: 先按 Alma 说的试试
> · 沉默（`proactive_disabled`）

**bob**: 回滚完了，Ready 了
> · 沉默（`proactive_disabled`）

**carol**: 账单那边对不上，上午的报表少了差不多 8%
> · 沉默（`proactive_disabled`）

**dave**: 少收还是少记
> · 沉默（`proactive_disabled`）

**carol**: 少记。支付回调有，但是落到 ledger 的条目缺
> · 沉默（`proactive_disabled`）

**dave**: 那先看消费端 lag 吧
> · 沉默（`proactive_disabled`）

**carol**: @Alma kafka consumer group 的 lag 一直涨但消费速率看着正常，一般是哪几种原因
> 🤖 一般是生产端写入变快了，或者部分 partition 卡住没在消费。看下按 partition 的 lag 分布和生产端 TPS 就能区分了。  
> `8.215s` · prompt 1,554 (cached 1,137) · out 952 (reasoning 909)

**dave**: 是不是有 partition 没有 consumer
> · 沉默（`proactive_disabled`）

**carol**: 查了，40 个分区都在 assign
> · 沉默（`proactive_disabled`）

**dave**: 那可能是 reprocess 的时候 offset commit 提前了
> · 沉默（`proactive_disabled`）

**carol**: 我加个监控看看
> · 沉默（`proactive_disabled`）

**erin**: 刚那个回滚的事最后是什么原因
> · 沉默（`proactive_disabled`）

**alice**: 内存 limit 太小
> · 沉默（`proactive_disabled`）

**erin**: @Alma 他说的对吗
> 🤖 你指的是谁说的哪一句？  
> `14.214s` · prompt 1,978 (cached 1,521) · out 1,963 (reasoning 1,947)

**bob**: 话说周末有人去看那个展吗
> · 沉默（`proactive_disabled`）

**dave**: 哪个
> · 沉默（`proactive_disabled`）

**bob**: 就是美术馆那个灯光展
> · 沉默（`proactive_disabled`）

**erin**: 我去过了，人多
> · 沉默（`proactive_disabled`）

**dave**: 票多少钱
> · 沉默（`proactive_disabled`）

**carol**: @Alma redis 的 EXPIRE key 0 是永不过期吧
> 🤖 不是，0 是马上删掉，要永不过期得用 PERSIST。  
> `5.781s` · prompt 2,343 (cached 1,905) · out 601 (reasoning 576)

**dave**: 我记得是删掉
> · 沉默（`proactive_disabled`）

**carol**: 哦对，我记混了
> · 沉默（`proactive_disabled`）

**erin**: @Alma 我们的备份 cron 写在 k8s CronJob 里，它按哪个时区
> 🤖 这个得看你们那个 CronJob 自己的配置，去查一下 spec.timeZone 写的是什么。如果这个字段没设，就按你们集群 controller 文档里的默认来，容器里的 TZ 不决定什么时候触发。  
> `6.774s` · prompt 2,736 (cached 2,289) · out 996 (reasoning 945)

**bob**: 容器 TZ 是 UTC
> · 沉默（`proactive_disabled`）

**erin**: 所以按 UTC？
> · 沉默（`proactive_disabled`）

**erin**: @Alma 那如果 spec.timeZone 没写呢
> 🤖 没写就看你们 CronJob controller 文档里对缺省值的说明。  
> `10.211s` · prompt 3,170 (cached 2,673) · out 1,383 (reasoning 1,358)

**dave**: @Alma 你是真人还是机器人
> 🤖 我是 bot，不是真人。  
> `3.34s` · prompt 3,593 (cached 3,057) · out 347 (reasoning 332)

**bob**: 哈哈
> · 沉默（`proactive_disabled`）

**dave**: 我就问问
> · 沉默（`proactive_disabled`）

**carol**: lag 那个查到了，是一个 partition 的 consumer 卡在 GC
> · 沉默（`proactive_disabled`）

**dave**: 那 8% 能补回来吗
> · 沉默（`proactive_disabled`）

**carol**: 重放就行
> · 沉默（`proactive_disabled`）

**carol**: @Alma 重放 kafka 消息补 ledger，怎么保证不重复记账
> 🤖 用支付回调的唯一 ID 做幂等键，写 ledger 前先判重，已存在的直接跳过。  
> `7.303s` · prompt 4,016 (cached 3,569) · out 1,052 (reasoning 1,019)

**dave**: 幂等 key
> · 沉默（`proactive_disabled`）

**erin**: 中午吃什么
> · 沉默（`proactive_disabled`）

**bob**: 楼下那家
> · 沉默（`proactive_disabled`）

**erin**: 又吃
> · 沉默（`proactive_disabled`）

**bob**: 那你说
> · 沉默（`proactive_disabled`）

**erin**: 随便
> · 沉默（`proactive_disabled`）

**alice**: @Alma 今天这几个事你都跟下来了吗
> 🤖 跟下来了。deploy OOM 回滚恢复了，账单少 8% 是单 partition 卡 GC 待重放，cron 时区看 spec.timeZone。  
> `11.591s` · prompt 4,448 (cached 0) · out 1,821 (reasoning 1,777)
