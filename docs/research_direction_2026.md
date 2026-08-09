# FlowerNet 研究转型与实现备忘录（2026-08-09）

> **结论先行。** 不建议把论文包装成“又一个 Plan–Do–Check–Act 写作 Agent”，也不建议把“用旧论文预测未来热点”作为主贡献。建议主线改成 **Auditable Research Harness Optimization**：系统把研究报告表示成跨章节的 claim–evidence–discourse graph，在真实外部证据和冻结的 held-out 任务上，对研究流程本身作有边界、可回滚、可审计的优化。长文生成是它产生和检验研究假设的载体，而不是终点。

## 1. 调研方法与证据边界

本备忘录优先列出论文主页、arXiv 或机构页面，方便团队逐项复核。执行环境的 Web 搜索服务返回 401，外网代理也返回 403；因此这里是**可执行的研究地图而不是系统综述**，不能声称穷尽了截至今日的所有工作。投稿前必须由两位研究者独立做一次 PRISMA 风格检索和 backward/forward citation chasing，并冻结检索式、日期及排除理由。

核心入口：

* 自动科学流程：[The AI Scientist](https://arxiv.org/abs/2408.06292)、[The AI Scientist-v2](https://arxiv.org/abs/2504.08066)、[Agent Laboratory](https://arxiv.org/abs/2501.04227)、[MLAgentBench](https://arxiv.org/abs/2310.03302)。
* 检索到报告：[STORM](https://arxiv.org/abs/2402.14207)、[Co-STORM](https://arxiv.org/abs/2408.15232)、[DeepResearcher](https://arxiv.org/abs/2504.03160)、[OpenAI deep research system card](https://openai.com/index/introducing-deep-research/)。
* 长文本：[LongWriter](https://arxiv.org/abs/2408.07055)、[LongGenBench](https://arxiv.org/abs/2409.02076)、[HelloBench](https://arxiv.org/abs/2409.16191)。
* 事实与评价：[FActScore](https://arxiv.org/abs/2305.14251)、[AlignScore](https://arxiv.org/abs/2305.16739)、[G-Eval](https://arxiv.org/abs/2303.16634)、[UniEval](https://arxiv.org/abs/2210.07197)、[RAGAS](https://arxiv.org/abs/2309.15217)。
* Harness Engineering：Lilian Weng 的公开材料应以其[个人站点](https://lilianweng.github.io/)及原帖为准。这里采用的工程定义是：能力并不只属于 base model；工具、提示、状态、上下文管理、验证器、恢复策略、评测与可观测性共同形成 harness。我们进一步把“optimizer”限制成**提出最小改动 → shadow run → held-in/held-out no-harm gate → 保留或回滚**，而不是让 Agent 任意改自己的代码。

## 2. Auto Research Agent 现状：输入、输出与反馈

| 范式 | 典型输入 | 典型输出 | 循环/反馈 | 长文位置 | 主要缺口 |
|---|---|---|---|---|---|
| Deep-research/report agent | 用户问题、网页/论文、时间预算 | 带引文的综合报告 | 搜索→阅读→补充搜索→综合；有时由 judge 决定继续 | 报告是主要交付物 | 引文“存在”不等于支持 claim；章节间状态通常隐式 |
| Wiki/outline agent（STORM/Co-STORM） | 主题、检索语料、不同视角 | 分层长文章 | 多视角提问/对话→检索→大纲→写作 | 以结构覆盖和引用组织见长 | 不是完整的提出假设—做实验—证伪闭环 |
| AI scientist | 研究方向、代码库、算力/实验预算 | idea、代码、实验、论文、review | idea→experiment→write→review/revise | 论文是研究轨迹的序列化结果 | 实验污染、错误自洽、novelty 检索不足、成本与复现风险 |
| Benchmark-driven coding researcher | 数据集、任务、baseline repo | patch、曲线、实验日志 | edit→run→score→repair | 文档通常是次级输出 | 容易对单一 benchmark 过拟合 |
| FlowerNet 当前系统 | topic、章节数、大纲、RAG sources | Markdown/DOCX、verifier trace | outline→逐小节 generate→verify→bandit repair | 逐小节生成是主循环 | 全文 discourse state 不够显式；内部 proxy 与科研价值未校准 |

竞争的共同点已经非常清楚：检索、规划、多 Agent、反思、LLM judge、代码执行本身都不再构成强 claim。真正尚未解决的是：**研究过程何时应该改变、改变什么、如何证明改变没有利用测试集或损坏其它科研属性。**

## 3. 长文衔接应该怎样构造

### 3.1 四层 contract，而不是把历史全文塞回 prompt

1. **段落内部（claim unit）**：每段只能有一个主 claim；采用 claim → mechanism/evidence → qualification → implication。引用绑定到原子 claim，不绑定到一整段。
2. **段落之间（discourse edge）**：写作前声明边类型：elaboration、contrast、cause、evidence、limitation、resolution。下一段开头应复用一个 predecessor anchor，但主体必须增加新的 evidence/aspect。这解决“完全不重复就不连贯、重复太多又冗余”的张力。
3. **章节内部（section contract）**：每节记录 `question`, `claims_to_establish`, `evidence_budget`, `must_not_repeat`, `handoff`。完成后不是只存全文，而是压缩成已覆盖 claim ledger 与 unresolved questions。
4. **章节之间（global argument graph）**：章节是论证节点，不只是标题列表。结束时产出 handoff；下一章必须消费 handoff 或显式说明转折。讨论章回指实验 claim ID，结论不得引入未出现的新 claim。

生成顺序应从“按标题续写”变成：**global argument graph → section contract → paragraph discourse plan → evidence-bound realization → graph reconciliation**。全文完成后再做一次 contradiction/redundancy sweep；局部通过不能替代全局验收。

### 3.2 测算：拒绝一个模糊总分

建议 verifier 输出可诊断的 metric vector：

* **Entailment / faithfulness**：把文本切成 atomic claims，用 NLI/AlignScore 类方法计算 claim–source entailment；另报 source coverage、citation correctness、unsupported-claim risk，不能与流畅度平均后被掩盖。
* **Discourse continuity**：相邻段 entity/term continuation、显式 relation realization、RST relation plausibility、dangling anaphora、章节 handoff consumption。
* **Non-redundancy**：同时计算段内 self-overlap、非相邻段近重复、与历史 claim ledger 的重复。ROUGE 只能作为词面诊断，不能冒充事实重复。
* **Information gain**：新 claim、新 evidence edge、新 limitation/contradiction，而不是新词比例。只有与 section contract 相关的新信息才计分。
* **Global argument closure**：outline question 是否被 claim 回答；claim 是否有 evidence；evidence 是否被正文消费；open question 是否被解决或明确保留。
* **Calibration**：LLM judge 必须报告多次采样方差/置信区间，并在人类 pairwise preference 上校准。对不同长度、领域、语言分别报告，不以 ROUGE/BERTScore 单独支撑科研质量 claim。

本次代码实现了轻量、无需模型下载的 **Discourse Delta**：

\[
D_\Delta=.45B_{local}+.25IG+.20C_{boundary}+.10C_{bridge}
\]

其中 `B_local` 奖励“足以承接但非复制”的相邻重叠，`IG` 是扣除历史重复后的信息增益，`C_boundary` 测量与前文边界连接，`C_bridge` 测量显式桥接。关键是同时公开各分量、重复段对和 repair target；这个指标是可解释的 proxy，论文中必须用人工 discourse 标注和强 embedding/NLI baseline 验证，不能现在就宣称它是 SOTA。

## 4. 代码审计：Kingsley/团队目前究竟做了什么

### 4.1 真实架构

代码不是单一 Agent，而是 web/outliner/generator/verifier/controller 五服务流水线。优势是状态和故障边界清楚；代价是同一逻辑存在重复实现（例如 verifier 的重型旧版与 `main.py` 的部署版），配置依赖大量环境变量，论文实验很容易因为配置漂移不可复现。

Verifier 已远超 README 早期描述：它融合 lexical relevance、历史最大重复、source check、coverage/evidence/novelty heuristics、可选 UniEval、uncertainty 和 soft/epsilon pass。问题是：

* “coherence” 主要由综合启发式/UniEval 给出，没有把**段落边、章节边、重复段证据**暴露给 controller；
* 大量权重与阈值为手调 proxy，soft pass 与 epsilon pass 会让“到底为什么通过”变得难以解释；
* reference-free proxy 与 ROUGE/BERTScore 淠杂，不能作为 NMI 核心证据；
* 当前逐小节 gate 不能证明全文 argument closure。

### 4.2 Bandit 不是在“训练一个语言模型”

Controller 做的是 contextual multi-armed bandit：臂是不同修复策略；根据失败维度给每个臂打分/探索，记录 chosen arm、logging propensity 和生成后的 reward。`bandit_ope.py` 再用 IPS、SNIPS、Doubly Robust 估计一个 target policy 的离线价值。这属于**修复策略选择器**，不是训练 generator，也不是端到端 RL。

论文风险：若 propensity 不是真实随机化概率、某些臂没有 overlap/support，IPS/DR 都不可信；直接模型若在同一事件上拟合和评价会乐观；reward 若由同一个 verifier 定义，会形成 Goodhart loop。最低要求是：记录完整 context/policy distribution、固定 exploration floor、按 document/topic 分组 cross-fit、报告 effective sample size 和最大权重、用外部/人工 outcome 验证 reward，并预注册 OPE policy。小样本 bandit 应被定位为工程组件或 ablation，而不是主创新。

### 4.3 不建议推翻重写

现有服务有大量回归、降级和部署处理，整体推翻会丢失工程资产。建议先做“strangler”改造：保留接口，逐步把 verifier 拆成纯 metric plugins，把 global graph state 加到 checkpoint，把 controller reward 改成外部校准 vector，并冻结一个可复现实验 profile。

## 5. Harness Optimizer 如何落地

优化对象不是 base-model 权重，而是版本化的 harness patch：

* prompt/section contract；检索 query、top-k 与 source diversity；context compression；tool routing；verifier threshold/calibration；repair arm；timeout/retry；graph reconciliation 次序。
* 每次只改变一个最小组件，带 `proposal_id`, parent version, causal rationale, predicted metric, budget, rollback condition。
* shadow evaluator 在 frozen held-in 与 held-out topics 上运行；hard gates（citation faithfulness、实验真实性、安全、成本上限）不可被平均分抵消。
* 用 sequential testing/置信区间而不是“单次分数变高”接受 patch；记录所有 rejected proposals，防止 publication bias。
* optimizer 永不改 raw result、reference answer、test topics、source truth；生产启用需要人工签署。

这比当前 `harness_optimizer_v1` 的规则建议更进一步：后者已经有 bounded proposal 与 no-harm 概念，但尚缺可执行 patch schema、版本 lineage、shadow runner、grouped statistics、回滚和人为批准。

## 6. Sharp & Clean Idea / Claim

### Idea：**FLOWER-Gym — Falsification-led Optimization of Workflow and Evidence for Research**

系统维护三张相联图：

1. **Research graph**：hypothesis–method–experiment–result–counterevidence；
2. **Claim–evidence graph**：正文原子 claim 到检索来源/实验 artifact；
3. **Discourse graph**：段落/章节之间的 rhetorical relation 与 handoff。

Verifier 不再只问“写得好吗”，而是产生可证伪缺陷（unsupported edge、contradiction、missing handoff、duplicate claim、unresolved limitation）。Harness optimizer 根据缺陷提出最小 workflow patch，并仅在 frozen held-out 上通过 no-harm gate 后保留。研究报告是图和研究轨迹的可审计投影。

### 可检验的主 Claim

> **在相同 base models、检索语料和 token/tool budget 下，缺陷驱动且带 held-out no-harm gate 的 harness optimization，相比静态 PDCA/self-refine 与无约束自改进，能提高外部校准的科研有效性（claim faithfulness、counterevidence recovery、experiment reproducibility）及跨章节 argument closure，同时不增加 unsupported-claim rate。**

这个 claim 的“sharp”来自因果控制：固定模型/预算，改变的是 workflow optimizer；“clean”来自 hard no-harm 与可审计 lineage。**不要宣称“首个 Auto Research Agent”或“达到/超过人类科学家”。**

### 关键实验

* 任务：文献综合、可执行 ML experiment、对抗性 contradictory literature 三类；按研究领域和时间切分 held-out，时间切分用于测试泛化而不是“预测未来热点”。
* 对照：single-pass、static PDCA、STORM-like outline/report、static FlowerNet、optimizer without held-out gate、optimizer without discourse graph、without counterevidence。
* 主终点：盲评 claim–evidence correctness、独立复现实验成功率、counterevidence recall、argument closure；次终点为 discourse preference、冗余、成本、时延。
* 统计：topic-level paired bootstrap/mixed-effects model，报告 effect size 与置信区间；多重比较校正；评审者不知道系统条件。
* 污染控制：冻结 source snapshot、container、seed、prompt/model version；按 document 分组，不允许同 topic 的段落跨 train/test。

## 7. 面向 NMI 的定位与止损条件

NMI 需要的不只是系统 demo。稿件必须贡献一个可一般化的机器智能机制，并以严谨实验证明，而不是宣称“目标期刊适配”就能保证录用。建议把贡献排序为：

1. falsification-led harness optimization algorithm；
2. 三图一致性与 no-harm acceptance protocol；
3. 带人工标注、可复现 artifact 的 benchmark；
4. FlowerNet 作为实例。

止损条件：如果预实验不能在至少两个领域、两个 model family 上稳定提高**外部人工/执行指标**，就不要以 optimizer 为 NMI 主线；转成 benchmark/measurement paper，聚焦内部 verifier 与真实科研质量的失配。这仍比堆叠更多 Agent 更可信。

## 8. 三周快速投稿决策

进一步竞争审计表明，“通用 Auto Research + harness optimizer”过宽且与 AI Scientist、Agent Laboratory、ADAS、AFlow 等工作高度重合。三周版本改为 **Trace2Claim / Provenance-Gated Selective Repair**：集中测量并修复 research trajectory 到最终报告的 provenance inconsistency；不训练 generator/RL policy，不创建大型新 benchmark。完整竞争矩阵、novelty 边界、baseline、kill criteria 与 21 天 critical path 见 [`auto_research_competitive_audit_2026.md`](auto_research_competitive_audit_2026.md)。
