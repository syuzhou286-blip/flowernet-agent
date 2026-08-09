# RDO-VOI for Evidence Drift：最终 Research Question 与加速方案

## 1. 对用户提出的最近邻证据的裁决

用户列出的工作足以推翻上一版 Trace2Claim 的宽泛 novelty 表述：

* [RARR](https://aclanthology.org/2023.acl-long.910/) 已做“检索证据→局部修改不受支持内容→尽量保持原输出”；
* [Locally-attributable Text Generation](https://aclanthology.org/2024.acl-long.182/) 已强调事实到最小局部证据的归因；
* [data-to-paper](https://arxiv.org/abs/2404.17605) 已覆盖 raw data、analysis code 到论文数值的可追踪生成；
* [LongCite](https://aclanthology.org/2025.findings-acl.264/) 已覆盖长文本细粒度引用；
* 用户提供的 [ScientistOne](https://arxiv.org/html/2605.26340v1)、[XScientist](https://arxiv.org/html/2607.12301v1) 与 [EditPropBench](https://arxiv.org/html/2605.02083v1) 分别声称 claim–artifact binding/句级 refinement、research DAG/repair history/re-execution、科学论文局部编辑传播。

因此不能再把 provenance binding、局部修复、claim anchor、repair history 或 edit propagation 单独写成贡献。上一版 deterministic provenance harness 可以保留为基础设施和 baseline，但不能成为 NMI 主方法。

**核验边界：** 当前 Web 工具对这些页面返回 HTTP 401，容器外网代理此前也返回 403。前三项 ACL/arXiv 工作可作为确定的强最近邻；2026 三项必须在可联网环境中逐段读取方法、实验、appendix 和代码后再冻结差异。不能仅凭题目/摘要判断“没有人做过”。

## 2. 最终 Research Question

> **When scientific evidence supporting a long-form research report drifts, which epistemic intervention should an autonomous research agent perform under a finite budget to minimize downstream decision regret?**

中文：

> 当一篇研究报告所依赖的文献或实验证据出现不确定、冲突、过期、失效或不可复现时，在有限时间/算力/检索预算下，Research Agent 应该重读文献、重跑实验、重新实现、补消融、弱化/撤回 claim，还是只做局部文档 patch，才能最小化后续科研决策的 regret？

这不是“发现错误后怎么改一句话”，而是“发现证据漂移后，下一步科研动作应该是什么”。

## 3. Research Question 是否成立

成立，但必须满足四个操作化条件：

1. **Drift 可观察**：source retraction/correction、新矛盾论文、dataset/version change、rerun variance、artifact loss、environment breakage；
2. **Decision 明确**：对 claim `retain/weaken/retract`，或决定是否继续依赖该 claim 设计后续实验；
3. **Action 有不同信息结果与成本**：reread、rerun、reimplement、ablation 不能只是不同 prompt；
4. **Oracle 可构造**：在揭示完整 evidence/action outcomes 后可计算最优 commitment，从而定义 regret。

如果“decision regret”只是一句抽象口号，没有 loss matrix、outcome、budget 和 oracle，这个问题不成立，也无法评测。

## 4. 是否能确定没有任何人做过

**不能。** 当前只能提出一个高价值、尚待正式 novelty audit 的差异假设：

> 最近邻主要解决 attribution、factual repair、artifact binding、DAG re-execution 或 edit propagation；RDO-VOI 联合研究的是动态 evidence drift、异构 epistemic action selection、预算约束和 downstream Bayes decision regret。

这个组合仍可能与以下领域发生直接重合，必须专项检索：

* value of information / Bayesian experimental design；
* rational metareasoning / bounded rationality；
* active feature acquisition / cost-sensitive active learning；
* adaptive experiment selection / optimal stopping；
* scientific claim verification under temporal knowledge drift；
* dynamic RAG / knowledge-base update；
* research-agent planning、experiment selection 和 budgeted tool use。

正式 novelty statement 必须是：“在预注册检索范围与截止日期内，未发现同时建模 evidence drift、heterogeneous research actions、document commitment 和 decision regret 的 end-to-end research-writing agent”，不能是“世界首个”。

## 5. Clean & sharp 方法：RDO-VOI

RDO-VOI 定义为 **Research Decision Optimization by Value of Information**。

### 5.1 State

每个 claim `c_i` 有：

* 当前有效概率 `p_i=P(valid|evidence)`；
* 对下游 claims/decisions 的 dependency impact `w_i`；
* evidence age、conflict、retraction、replication state；
* 当前 report commitment：retain/weaken/retract。

### 5.2 Decision regret

设真实状态 `z_i∈{valid,invalid}`，报告决策 `d_i∈{retain,weaken,retract}`，定义非对称 loss `L(d_i,z_i)`。例如保留错误 claim 的损失高于弱化正确 claim；撤回正确 claim 也有机会成本。

当前最小 Bayes risk：

\[
R(p_i)=\min_{d_i}\mathbb E_{z_i\sim p_i}L(d_i,z_i).
\]

### 5.3 Epistemic action

信息动作：`reread`, `retrieve_counterevidence`, `rerun`, `reimplement`, `add_ablation`, `replicate_with_new_seed/data`。

文档动作：`weaken`, `retract`, `local_patch`。文档动作不制造新证据；它们只是根据当前 posterior 改变 commitment。

### 5.4 Value of information

对动作 `a`：

\[
VOI(a)=R(p)-\mathbb E_{o\sim P(o|a,p)}R(p'|o,a)-\lambda cost(a).
\]

claim dependency graph 将上游 claim 的验证价值传播给依赖它的结论。策略在预算 `B` 下选择期望 decision-regret reduction 最大的 action sequence；若所有 action 的净 VOI 非正，则 abstain 并直接弱化/撤回高风险 claim。

### 5.5 与已有工作的 clean 区别

* RARR：检测/检索后修文本；RDO-VOI 先判断继续获取哪类科学信息是否值得；
* local attribution/LongCite：解决证据定位；RDO-VOI 解决证据漂移后的 action allocation；
* data-to-paper/ScientistOne：建立 artifact binding；RDO-VOI 把 binding 的不确定/冲突作为动态决策状态；
* XScientist：即使已有 DAG/re-execution，仍需核验其 action selector 是否显式优化 calibrated downstream regret/cost；
* EditPropBench：解决 edit propagation；RDO-VOI 决定应该获取信息、改变 claim commitment，还是触发 propagation。

## 6. 训练什么

不训练新的 writer。训练两个小模型，解析决策仍由显式 VOI 完成：

1. **Drift/validity calibrator**：输入 claim、旧/新 source span、run/log/config，输出 `P(valid)`、drift type 与 calibration；使用 temporal contradiction、retraction、numeric/split corruption、rerun variance 数据训练；
2. **Action outcome model**：估计 `P(o|state,a)`，即 reread/rerun/reimplement/ablation 后可能观察到什么及 posterior 如何变化。

VOI policy 不需要端到端 RL 才成立。三周内以 empirical outcome table + calibrated model + deterministic greedy/one-step VOI 为主；offline contextual bandit/sequence policy 作为后续扩展。这样训练目标与方法贡献分开，避免 reward hacking。

训练/测试必须按 paper/topic/repository/time 分组。测试 drift event 和同一论文的相邻 evidence 不能泄漏到训练。

## 7. Benchmark：Evidence Drift Research Observatory

### Drift events

1. 文献被 correction/retraction；
2. 新论文给出相反证据；
3. rerun 结果跨 seed 不稳定；
4. dataset split/version 改变；
5. dependency/environment 导致旧实验不可复现；
6. 新 ablation 破坏原 mechanism claim。

### 每个 episode 必须包含

初始 report/claim DAG、初始 evidence、drift event、可选 actions、每个 action 的真实/回放 outcome 与 cost、揭示后的 oracle commitment、受影响文档 spans。

### Baselines

`do_nothing`, `always_patch`, `retrieve_all_then_RARR`, `rerun_all`, `uncertainty_threshold`, `greedy_claim_impact`, `random_budgeted`, `XScientist-style re-execution`（按其真实方法复现）、`RDO-VOI`, `oracle`。

### Primary metrics

* cumulative decision regret；
* regret reduction per unit cost；
* unsafe-claim exposure over time；
* time/cost to safe report；
* action-selection accuracy vs oracle；
* calibration（Brier/ECE）；
* long-document coherence/no-harm；
* downstream experiment waste avoided。

## 8. 三周加速路线

### D1–D2：必须先做最近邻全文核验

下载/阅读用户列出的七篇全文和代码，重点查：drift model、action space、cost/budget、action outcome model、regret objective、dynamic evaluation。并专项检索 VOI/metareasoning/Bayesian experiment design 与 scientific agents 的交叉。

### D3：冻结 RQ 和 protocol

若找到直接同机制工作，立即 pivot；否则冻结 loss matrix、drift taxonomy、actions、oracle、primary metrics 和 20–30 个 episodes。不得实验后改 loss。

### D4–D7：Observatory + deterministic baseline

构造真实/回放 drift episodes、实现 Bayes risk/VOI/预算策略、接入 FlowerNet claim DAG 与 provenance trace。

### D8–D11：calibrator/outcome model

训练 validity calibrator，估计或拟合 action outcomes；做 grouped held-out calibration。若 outcome 数据不足，明确使用 empirical/simulator model，不伪装 learned policy。

### D12–D16：主实验

所有 baselines、matched budget、2 model families、3 seeds、dependency/VOI/outcome-model ablations。

### D17–D18：专家人评与统计

blind review、paired bootstrap/randomization、calibration/failure analysis。

### D19–D21：arXiv + NMI 稿

先冻结代码/data/results，再完成主稿、methods、limitations、model/data card 和 reproduction artifact；arXiv 与 NMI 稿使用同一结果快照，不能为抢时间删掉负结果。

## 9. Go / No-Go

**Go：** 在 unseen drift episodes 上，RDO-VOI 相比 uncertainty/impact/retrieve-all/rerun-all baselines 显著降低 cumulative decision regret，且成本更低、report quality 不退化。

**Measurement pivot：** 若 VOI policy 不胜出，但现有 agents 在 evidence drift 下造成显著 report/decision failure，则投 evidence-drift benchmark/measurement，RDO-VOI 作 baseline。

**No-Go：** 找到已明确使用同一 state/action/regret/VOI 定义的直接工作；无法定义 oracle/outcomes；或结果完全由人工 loss matrix 选择决定且对合理 loss sensitivity 不稳健。

