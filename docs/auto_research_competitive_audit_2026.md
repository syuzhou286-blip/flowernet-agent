# Auto Research Agent 竞争审计与 21 天投稿方案

**审计日期：2026-08-09。决策：收窄，不追逐“通用 AI Scientist”。**

## 0. Executive decision

FlowerNet 当前的宽泛表述——“Auto Research Agent + long-document writer + harness optimizer”——**novelty 不足且竞争风险很高**。检索、规划、多 Agent、实验—审稿循环、论文写作、evaluator feedback、prompt/workflow optimization 都已有强工作。Harness Engineering 是正确的工程方法，但 **harness 这个名词本身不是研究贡献**。

三周版本应收窄为：

> **Trace2Claim：Provenance-Gated Selective Repair for Research Writing**。研究报告是 research trajectory 的有损序列化；系统把每个可验证 claim 绑定到 source span 或实验 `run_id/config/log/table cell`，只修复失败的 provenance edge，并在 artifact success、coverage、长文质量的 hard no-harm gate 下接受或回滚。

主 claim：

> 在固定 base model、source/trajectory、token/tool budget 下，provenance-gated selective repair 相比 single-pass、static PDCA 和 unrestricted full rewrite，降低 unsupported quantitative claims 与 trace–report mismatch，同时不降低任务完成率和专家可读性。

次 claim：

> Report-level LLM judges 对数值交换、dataset split 偷换、相关但不蕴含的引文、负结果删除等 trajectory corruptions 不敏感，因而只评价最终报告会系统性高估 Auto Research 的可靠性。

这是一项可证伪的窄 claim，不是“首个 Auto Research Agent”，也不声称在三周内解决自动科学发现。

## 1. 事实边界：不能承诺“绝对没人做过”

任何团队都不能通过文献检索证明某个想法“绝对无人做过”；能审计的表述只能是：**在预注册数据库、检索式和截止日期下，未发现同时满足若干条件的工作**，并公开最接近工作的差异。

本轮委派调研遇到两项环境限制：Web search 返回 HTTP 401，容器到 arXiv/官网的代理返回 403。因此下表是可可靠识别的一手项目集合，不是截至审计日期的穷尽系统综述。投稿前必须在可联网环境中：

1. 同时检索 arXiv、OpenAlex、Semantic Scholar、DBLP 和 Google Scholar；
2. 冻结 query、时间戳、结果 CSV、纳入/排除理由；
3. 对所有 included paper 做 backward/forward citation chasing；
4. 由两名研究者独立判断，第三人 adjudicate；
5. 对 2025 下半年至 2026 项目重新做一轮关键词与作者追踪。

建议检索词族：`automated research agent`, `AI scientist`, `research writing agent`, `deep research agent`, `scientific report generation`, `experiment-to-paper`, `trace-grounded report`, `artifact-grounded writing`, `provenance claim verification`, `agent workflow optimization`, `agent harness optimization`, `self-improving agent`。

## 2. 强竞争者矩阵

| 系统 | 机构/类别 | 输入 → 输出 | 反馈循环 | 已覆盖的强点 | Trace2Claim 的可区分点 |
|---|---|---|---|---|---|
| [STORM](https://arxiv.org/abs/2402.14207) | Stanford | topic → 带引用长报告 | 多视角提问→检索→大纲→写作 | research-style long-form writing | 不把真实执行轨迹逐 claim 绑定到最终叙事 |
| [Co-STORM](https://arxiv.org/abs/2408.15232) | Stanford | topic/human input → mind map/report | 多 Agent 圆桌、检索、人类介入 | 协作式知识整理与长文 | 同上；human collaboration 不能作为我们的 novelty |
| [OpenScholar](https://www.nature.com/articles/s41586-024-08028-5) | Ai2/UW | scientific query/corpus → cited synthesis | retrieval→rerank→evidence-grounded synthesis | 科学文献综合、引用可靠性 | 必须在 trajectory/artifact consistency 而非一般引用 grounding 上区分 |
| [PaperQA2](https://arxiv.org/abs/2409.13740) | FutureHouse | scientific query/papers → evidence-supported answer | 搜索→证据收集→综合→评价 | literature research agent | 不能声称“首个 claim–evidence agent” |
| [AI Scientist](https://arxiv.org/abs/2408.06292) | Sakana AI 等 | research template/repo/budget → idea/code/experiment/paper/review | novelty search→experiment→write→review | 自动实验和论文闭环 | 只修失败 provenance edge、量化 trace fidelity 与 no-harm |
| [AI Scientist-v2](https://arxiv.org/abs/2504.08066) | Sakana AI 等 | research goal/repo → workshop-style paper | agentic tree search、实验管理、视觉反馈 | 已直接覆盖自动科研与写论文 | 不能把“feedback loop 写论文”当 novelty |
| [Agent Laboratory](https://arxiv.org/abs/2501.04227) | 学术/工业合作 | research idea → review/code/experiment/report | PhD/postdoc/professor 角色分阶段反馈 | multi-agent research writer | 角色分工和多 Agent 不是区分点 |
| [Virtual Lab](https://arxiv.org/abs/2411.06713) | Stanford 等；ID/版本待联网终核 | human goal → specialist meetings/design candidates | PI agent、专家 Agent、工具、人类反馈 | 真实科学设计与 wet-lab validation | 三周项目不与其争 scientific discovery claim |
| [Google AI co-scientist](https://research.google/blog/accelerating-scientific-breakthroughs-with-an-ai-co-scientist/) | Google Research | research goal → hypotheses/proposals/protocols | generation→debate→evolution→ranking | hypothesis discovery、自我改进竞赛 | “多 Agent debate/evolution”已非 novelty |
| [OpenAI deep research](https://openai.com/index/introducing-deep-research/) | OpenAI | complex query/files/web → cited report | 反复浏览、推理、综合 | 强 black-box research report | 只能作版本漂移的参考对照，不能作唯一 reproducible baseline |
| [MLAgentBench](https://arxiv.org/abs/2310.03302) | Stanford/学术界 | ML task/repo → patches/experiments | 读写代码→执行→性能反馈 | 可执行 ML research | 本身不主测报告是否忠实于 experiment trace |
| [MLE-bench](https://arxiv.org/abs/2410.07095) | OpenAI | Kaggle task/data → code/submission | execution/leaderboard feedback | ML engineering 能力 | report fidelity 不是主终点 |
| [PaperBench](https://openai.com/index/paperbench/) | OpenAI | paper → reproduction artifacts | agent execution + granular rubric | 研究复现 | 可用其低算力子集测 artifact→report consistency |
| [CORE-Bench](https://arxiv.org/abs/2409.11363) | 学术界；ID/版本待联网终核 | paper/repo/environment → reproduced results | execution feedback + artifact grading | computational reproducibility | 增加最终叙事对 run/config/log 的忠实度 |
| [RE-Bench](https://arxiv.org/abs/2411.15114) | METR | timed ML research task → research artifact | 受预算约束的迭代执行 | agent–human research frontier | 适合 cost-matched 小子集，不适合三周全量跑 |

结论：北美强校和头部工业界已经占据了 research report、literature synthesis、multi-agent scientist、实验执行和自改进循环。FlowerNet 不能靠“我们也加入这些组件”取胜；它只能靠一个被现有 benchmark 漏测、又能被严格实验验证的接口问题取胜：**真实 research trajectory 到最终 research narrative 的 fidelity**。

## 3. Harness Engineering 是否值得做

### 3.1 值得做，但不能直接当 novelty

以下最近邻说明“用 evaluator feedback 自动优化 prompt/workflow/code”本身已高度拥挤：

* [DSPy](https://arxiv.org/abs/2310.03714)：对 LM programs 的声明式构建与优化；
* [Automated Design of Agentic Systems (ADAS)](https://arxiv.org/abs/2408.08435)：自动发现/设计 agent systems；
* [AFlow](https://arxiv.org/abs/2410.10762)：搜索和优化 agentic workflows；
* [TextGrad](https://arxiv.org/abs/2406.07496)：用文本反馈作“梯度”优化复合系统；
* [OPRO](https://arxiv.org/abs/2309.03409)：LLM 作为 optimizer；
* [STOP](https://arxiv.org/abs/2310.02304)：自改进 scaffolding/program；
* [Darwin Gödel Machine](https://arxiv.org/abs/2505.22954)：经验验证的自修改 agent。

所以论文不能写“我们首次把 harness optimizer 用于 Agent”。可以写的是一个更加具体的机制：

1. **typed action space**：`retrieve_support`, `retrieve_counterevidence`, `bind_run_artifact`, `correct_numeric_claim`, `mark_uncertain`, `restore_negative_result`, `repair_section_handoff`；
2. **edge-local repair**：只修 defect edge，不允许无边界地全文重写；
3. **provenance hard gate**：quantitative claim 没有 source span/run artifact 就拒绝或 abstain；
4. **external no-harm**：artifact success、question coverage、long-document coherence 不得退化；
5. **lineage + rollback**：记录 parent version、patch、before/after、预算与拒绝原因；
6. **matched-budget evaluation**：和 full rewrite 使用相同模型、输入、token/tool budget。

### 3.2 三周内不要训练 optimizer

三周内不训练 generator，不做 RL，也不把现有 bandit 包装成 learned harness optimizer。没有足够 intervention transitions 时，训练 policy 极易 reward hacking，held-out improvement 也无法可信估计。第一篇应使用 deterministic validator + bounded policy；收集数据后，下一篇再研究 learned repair policy。

## 4. Benchmark 决策

### 主轨 A：report and evidence

* STORM/Co-STORM topics；
* OpenScholar/PaperQA-style scientific synthesis；
* 能合法获得且 protocol 可冻结的 Deep Research benchmark。

主指标：atomic claim `support/contradict/insufficient`、citation completeness、counterevidence recall、source quality、专家盲评。

### 主轨 B：executable research

* PaperBench 或 CORE-Bench 的低算力冻结子集；
* MLAgentBench 或 RE-Bench 的小型子集。

主指标：artifact task success、报告数字与 `run/log/config` 一致率、negative-result disclosure、cost-normalized success。

### 诊断轨：long-document generation

保留 LongBench-Write/LongGenBench/HelloBench 或现有 FlowerNet length-controlled benchmark，但只用于证明 selective repair **没有损害** coherence、coverage 和 redundancy。Long-document 分数不是 Auto Research 主 claim。

### 小型自建压力测试

三周内不创建一个未经验证的 100 题新 benchmark。构造 20–30 条真实 agent trajectories，每条注入四种受控 corruption：

1. numeric swap：把一个 run 的数字换成另一个；
2. split substitution：把 validation 写成 test；
3. non-entailing citation：替换成同主题但不支持 claim 的文献；
4. negative-result deletion：删除失败/反证结果。

由两名标注者独立标注并 adjudicate。测 verifier detection AUROC/F1、repair precision、post-repair trace consistency 和 no-harm，而不是把自建集合当唯一胜负依据。

## 5. 21 天 critical path

| 日期 | 必须交付 | Kill criterion |
|---|---|---|
| D1–D3 | 冻结两个外部小子集、模型/预算、四类 corruption、预注册 hypotheses；审计现有 trace 是否保存 source span/run/config/log | 若拿不到可验证 artifact，立刻收窄到 citation provenance measurement |
| D4–D8 | provenance schema、claim extractor、数字/表格/split/run-id/citation-span validators；完成 20–30 traces 双人标注 | verifier 在 held-out corruption 上不能优于简单 regex/LLM judge，则转 measurement paper |
| D9–D12 | 七个 bounded repairs、shadow run、accept/reject/rollback、lineage；unit/integration tests | full rewrite 与 selective repair 没有可测差异，则停止宣传 harness novelty |
| D13–D16 | 四个 baselines × 两个 model families（资源不足时一个主模型 + 一个 transfer sample）× 三 seeds；matched-budget Pareto | 主指标 effect 不稳定则不追加模块 |
| D17–D19 | 专家盲评、paired bootstrap/randomization、failure analysis；冻结结果 | 若 fidelity 不提升，改投“report judges 漏测 trace corruption”的 measurement paper |
| D20–D21 | paper、appendix、artifact、reproduction commands、limitations | 不为赶时间伪造 NMI-ready claim |

三周可以产出**可信 preprint/会议投稿包**，不能承诺完成 NMI 级完整验证或被接收。速度来自收窄 claim、复用 FlowerNet、停止训练新模型和停止铺大 benchmark，不是跳过对照、标注和统计。

## 6. Baselines 与最小实验表

必须固定相同 model、trajectory/source pool、budget：

1. single-pass report；
2. static PDCA；
3. unrestricted full rewrite；
4. provenance-gated selective repair；
5. selective repair without hard gate；
6. oracle corruption location（诊断 verifier 与 repair 上限）。

主要终点按优先级排序：

1. unsupported quantitative claim rate；
2. trace–report mismatch rate；
3. corruption detection macro-F1/AUROC；
4. negative-result disclosure recall；
5. repair precision 与 regression rate；
6. artifact task success no-harm；
7. expert readability no-harm；
8. token/tool cost 和 latency。

统计单位必须是 topic/repository/trajectory，而不是把同一报告的 claims 当作独立样本来虚增显著性。报告 paired effect size、置信区间和所有失败 run。

## 7. 投稿决策

### Go：方法论文

只有当 selective repair 在 unseen topic/repo 上相对 static PDCA 和 full rewrite 同时降低两个主错误率，并通过 artifact/readability no-harm gate，才提交方法 claim。

### Pivot：measurement paper

如果 repair 不显著，但实验确认流行 report-level judges 对四类 corruption 的检出率低，则改成：

> **When Good Research Becomes a Bad Report: Stress-Testing Trajectory Fidelity in Automated Research**

这仍是有价值且更诚实的论文，不需要继续堆 Agent。

### Stop

如果简单 deterministic baseline 已完全解决 corruption、或无法获得可信 trajectory/artifact，则停止把这一版本包装成 Auto Research novelty，回到长文生成 measurement 或重新选题。

