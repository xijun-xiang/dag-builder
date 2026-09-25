# API 参数与用量契约

## 当前授权政策

2026-09-21后续：用户允许thinking enabled/high、32768、32 workers和更大活动预算，并授权长输出探针失败后采用逐响应验收。探针失败原样保留；此授权不证明代理的参数控制已恢复。新配置显式使用`content_gated_response=true`：仅completion超请求max但总量未超原预留时记告警，总量超预留仍停止新增；非stop与不合法JSON拒绝，不修补或使用reasoning兜底。严格usage/模型标识校验不变。排队尚未发送的请求也服从停止信号；在途请求不能追回。详见[HumanEval当前执行规则](humaneval-validation.md)。

短构图请求已实际通过，HumanEval五题复验有1题通过完整模型审核，随后启动全量候选构建。不能将API正常响应等同于DAG语义正确。以下为默认严格策略与历史探针记录；历史“暂停/未放量”仅指当时状态。

## 历史探针与默认严格策略

早期验收（2026-09-21）：用户允许 thinking 后，enabled/low 的短 JSON 可以正确解析。但完整上限探针请求4096输出tokens，代理报告completion_tokens=31391、total_tokens=31480、finish_reason=stop，仍未通过。当时科学构建没有放量，不能把开启 thinking 当作额度控制问题已解决。

探针现在同时支持显式 enabled/disabled：enabled 为每次至多请求4096输出tokens，disabled 保留原64；均最多两请求，配置中更小的预算优先。下文原64-token验收是历史非思考协议。

若需验证用户指定的真实配置上限，可显式增加 `--configured-max-tokens`。
该模式不缩小 `max_tokens`（最大支持32768），也不改 `reasoning_effort`；两条请求分别检查短JSON和长输出截断。长输出压力输入为0到19999的整数数组，截断响应只用于接口检查，不进入科学数据。仍最多两请求、无自动重试，预算上限取配置额度与 `2 * max_tokens + 32768` 的较小者；跨run总授权不变。`probe_policy.json` 固定实际采用的上限，原请求保留每个字段，不能将小上限探针冒充正式配置测试。

可选 `transport=curl` 用原生HTTPS客户端排查urllib的SSL中断，默认仍为urllib。凭据仅经stdin；禁用curlrc、自动重试和跳转，保留证书验证。可选 `tls_max_version=TLSv1.2` 是逐run记录的兼容性设置，不改变系统网络；本次单用TLS1.2未解决中断。所有客户端使用同一响应契约检查。

这个检查属于工程门槛，不是 DAG 语义质量或 PALS 有效性实验。

## 故障与修复

HumanEval 首批请求设置了 `thinking.type=disabled`、`max_tokens=8192`，实际收到 reasoning，且部分响应报告的 completion_tokens 超过 8192。原来的保护仅检查总量是否超出预留；异常响应仍可能被解析为阶段结果，缓存响应和续跑又没有复检。

新实现对新响应、缓存响应和恢复预算统一检查：

- 输出 token 超过请求上限、禁用思考却返回 reasoning、用量字段自相矛盾，立即停止；不进入阶段 output，不记为题目语义拒绝。
- `strict_response_contract=true` 还要求完整有效的 usage，以及返回模型 ID 与请求相同。不猜测模型别名或自动降级。
- 原始 response 保留，违规原因保存在同一 attempt 的 `contract_check-v1.json`。
- 预算占用按每次请求的 `max(预留, 服务报告总量, 服务报告输入+输出)` 计；两者字段错误时仍取较大有效值。超时/未知状态不退还预留；`reserved_tokens` 与 `accounted_tokens` 分别报告。
- `--resilient` 不重试契约失败；重新启动也不能绕过。并发下已经发出的请求无法追回，新请求在预算锁内被阻止。
- 原有配置的默认序列化不变；新配置/代码必须用新 run，不能原地覆盖已冻结产物。API 探针和旧 run 的用量也必须计入同一授权预算。

这是客户端的事后检查，不是服务端扣费硬限额。若服务忽略上限，单次请求仍可能在客户端拿到响应前超支；禁止宣传为费用绝对保证。

## 有界兼容性探针

[DeepSeek 官方接口文档](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/)说明 `reasoning_effort=none` 可关闭思考，`max_tokens` 限制 completion。私有代理是否正确转发必须实测。

`configs/humaneval-reference-contract-v2.json` 是候选配置模板，不代表服务已验收，也不是本次活动扣除历史用量后的剩余额度。它保持原构图提示、T=0 和 8192 上限，显式增加 none 与严格契约；并发 1、无自动传输重试。建立后继 run 时，必须扣除旧 run 和探针用量，禁止以新目录重置总预算。

```bash
# 需要另行取得服务调用授权；--key-file 只传私有文件路径。
dag-builder probe-contract \
  --config configs/humaneval-reference-contract-v2.json \
  --root /absolute/private/path/new-api-probe \
  --key-file /absolute/private/path/credential
```

最多两次收费调用，每次至多请求 64 输出 tokens（保留配置中更低的上限）：先请求固定 JSON 短答，再请求超长列表以检查上限截断。后者的 length 是预期探针结果，不会作为 DAG 内容。第一个失败即停止，不改参数重试。探针预算至多 32768 预留 tokens，保留配置中更小的请求/预算限制。不会发送 benchmark，也不打印凭据。

`probe_result.json`、请求/原始响应、实现快照均保留；相同代码配置下复读不产生新调用。探针通过也不能证明所有长请求都合规，因此正式每次请求仍逐一检查。

## 2026-09-21 实际验收

增加 none 后的第一条真实合成探针仍返回 97 字符 reasoning；服务报告输入 63、输出 28、总量 91 tokens。输出未超 64，但禁用思考未通过；新保护正确暂停，第二条探针未发出。本轮不能声称输出上限的压力检查已通过，更不能放开 HumanEval 全量。

下一步需由代理服务方确认非思考参数兼容性/转发规则。也可另行批准改变构图思考模式并重新冻结协议，但不能把参数被忽略当成当前非思考协议的成功实现。本次没有修改 PALS 子项目、公式、提示、DAG 筛选、GPQA 已部署快照或旧产物。
