# NMI Master Plan：Trace2Claim（21 天锁稿版）

> **方向更新（2026-08-09）：** 用户提供的 RARR、local attribution、data-to-paper、LongCite、ScientistOne、XScientist 与 EditPropBench 直接削弱了 Trace2Claim 的原始 novelty。本文保留为 provenance 基础设施计划；NMI 主 research question 已转为动态 evidence drift 下的预算化科研动作选择，见 [`rdo_voi_evidence_drift_direction.md`](rdo_voi_evidence_drift_direction.md)。在完成七篇全文及 VOI/metareasoning 交叉检索前，不再声称 provenance-gated local repair 本身有 novelty。

**目标不是保证录用，而是在 21 天内完成一份达到 NMI 审稿标准所需证据结构的投稿稿件。** 期刊结果不可保证；可控制的是 claim 的干净程度、novelty audit、外部验证、统计和 artifact。

## 1. 第一件事：先冻结 falsifiable claim，不先继续堆系统

前三天唯一最高优先级是做 **Trajectory Availability Audit**：随机抽取 10 次现有 FlowerNet runs，检查是否保存以下数据。

| 层 | 必需字段 | 没有时的处理 |
|---|---|---|
| literature | source ID、URL/DOI、retrieved passage/span、query、timestamp | 无 span 则只能做 citation-level 版本 |
| claim | atomic claim ID、类型、report span、uncertainty | 运行 deterministic claim extractor 回填 |
| experiment | `run_id`、commit、config hash、dataset/split、seed、metric/value、log/table cell、status | 无真实实验 artifact 则不能声称 executable auto research |
| discourse | section contract、claim IDs、handoff、negative-result disclosure | 从 outline/history 回填，但标记 inferred |
| harness | parent version、action、before/after、budget、accept/reject、rollback reason | 新运行必须强制记录 |

**D3 决策门：**

* 数据齐全：做 literature + executable trajectory fidelity；
* 只有 source spans：收窄为 evidence-provenance research writing；
* 两者都不齐：停止 Auto Research 方法 claim，转 measurement/data paper。

## 2. Clean & sharp method

方法名：**Trace2Claim-PGSR（Provenance-Gated Selective Repair）**。

### 2.1 表示

把一次自动科研运行表示为 typed provenance graph：

\[
G=(C,S,R,A,D,E),
\]

其中 `C` 是 atomic claims，`S` 是 source spans，`R` 是 experiment runs，`A` 是 immutable artifacts，`D` 是 discourse/section nodes，`E` 是 typed edges：`supports`, `contradicts`, `measured_by`, `reported_as`, `qualifies`, `hands_off_to`。

与一般 claim graph 的区别不是“用了图”，而是每条 quantitative edge 必须能解引用到 immutable artifact；最终报告只是 `G` 的序列化视图，不是真值来源。

### 2.2 TraceVerifier

Verifier 对每个 claim–provenance pair 输出：

\[
p(y\mid c,e),\quad y\in\{support,contradict,insufficient,mismatch\},
\]

以及 defect type、support span 和 calibrated confidence。确定性 validators 先捕获数字、split、run ID 和负结果遗漏；learned verifier 处理语义蕴含、限定词、跨句证据和隐式 overclaim。

### 2.3 Selective repair

只允许七个 typed actions：

`retrieve_support`, `retrieve_counterevidence`, `bind_run_artifact`, `correct_numeric_claim`, `mark_uncertain`, `restore_negative_result`, `repair_section_handoff`。

一个 action 一次只能改变一个 claim edge 或 disclosure node；禁止默认全文重写。这给出可识别的 intervention 和低成本优势。

### 2.4 Provenance/no-harm gate

候选 patch 只有在下式成立时保留：

\[
\Delta F_{trace}>0\;\land\;
\Delta A_{artifact}\ge0\;\land\;
\Delta Q_{coverage}\ge-\epsilon\;\land\;
\Delta Q_{longdoc}\ge-\epsilon\;\land\;
cost\le B.
\]

否则 rollback。faithfulness 是 hard constraint，不能被 readability 的均值抵消。

## 3. Novelty 到底在哪里

不声称以下内容有 novelty：多 Agent、PDCA、claim graph、citation verifier、LLM judge、workflow optimizer、TextGrad 式反馈、自动写论文。

投稿 claim 只落在三个组合条件：

1. **measurement gap**：同时评价 executable/literature trajectory 与最终长篇 research narrative，而不是只评 artifact success 或 report quality；
2. **method**：以可解引用 provenance edge 为 hard constraint 的 claim-local selective repair，而不是 generic full rewrite；
3. **causal evaluation**：matched-budget、同 trajectory、同 model，对 corruption/repair 作 paired comparison，并报告 external no-harm。

必须将其写成“在冻结检索 protocol 中未发现同时满足三项的工作”，不能写“世界首个”。如果最终 novelty audit 找到直接最近邻，则把贡献转成 benchmark/measurement，并以对方方法作 baseline。

## 4. Training：借鉴什么、训练什么

### 4.1 最近邻训练范式

* **NLI/AlignScore 类 cross-encoder**：学习 claim–evidence entailment；可直接作为 TraceVerifier backbone/基线。
* **FActScore 式 atomic decomposition**：先拆 claim 再验证，借鉴评测分解而不是其最终分数。
* **DSPy/OPRO**：在开发集优化 prompt/program，但不得访问 held-out topics；作为 static optimized-workflow baseline。
* **TextGrad**：把 evaluator textual feedback 传回复合系统；我们的变体不是自由文本“梯度”，而是 typed provenance defect。
* **ADAS/AFlow**：搜索 agent/workflow；作为 unrestricted workflow optimization 最近邻。我们的 action space 被 provenance 和 no-harm gate 约束。
* **DPO/preference ranking**：可用于 repair candidate ranker，但三周主结果不能依赖大规模 policy training。
* **Contextual bandit/offline RL**：留到收集足够真实 intervention transitions 后；当前 FlowerNet bandit 只作 engineering baseline/ablation。

### 4.2 三周内训练 TraceVerifier，不训练 generator

建议 backbone 为可在现有算力上 LoRA/全参微调的 encoder 或小型 instruction model。输入为 `(claim, source span/artifact summary, local report context)`，使用多任务损失：

\[
\mathcal L=
\mathcal L_{relation}
+\lambda_1\mathcal L_{corruption-rank}
+\lambda_2\mathcal L_{span}
+\lambda_3\mathcal L_{calibration}.
\]

* `relation`：support/contradict/insufficient/mismatch 分类；
* `corruption-rank`：clean edge 得分高于受控 corruption；
* `span`：恢复支持 claim 的 source/log span；
* `calibration`：Brier/ECE 或 temperature scaling，保证 hard gate 能解释。

### 4.3 数据

* 20–30 条真实 trajectories；
* 每条四种 corruption：numeric swap、split substitution、non-entailing citation、negative-result deletion；
* 自动 corruption 只用于训练和压力测试，held-out clean/corrupt pairs 按 trajectory 分组；
* 300–500 个专家 adjudicated claim edges 作为校准/测试核心；
* 同一 paper/topic/repo 的 edges 不得跨 train/test；
* 额外保留 unseen domain 和 unseen generator model transfer split。

若训练数据不足，deterministic validator + frozen NLI baseline 是主方法，fine-tuned verifier 作为 exploratory result。绝不能为了“有训练”而把测试 corruption 泄漏到训练集。

### 4.4 第二阶段才训练 repair ranker

收集至少数千条真实 `(defect, action, before, after, cost)` 后再做 pairwise repair ranking/offline policy learning。reward 必须来自 trace fidelity 与 external no-harm，不能只来自 FlowerNet 自己的 verifier。

## 5. Auto Research 体现在哪里

Auto Research 不是文章用了“research”这个词，而体现在可执行状态变换：

1. 检索支持证据与反证；
2. 形成 hypothesis/claim；
3. 规划并执行实验，保存 artifact；
4. 读取正负结果并更新 claim；
5. 把 claim 与真实 source/run 绑定；
6. 生成跨章节 research narrative；
7. 发现 trace–report defect 后选择 research action，而非只润色文字；
8. 对无证据 claim abstain/降级，而不是让语言模型补全。

核心 end-to-end demo 必须至少展示：一个错误数字被追溯到 run、一个 validation/test 偷换被拒绝、一个负结果被恢复、一个不蕴含引用触发 counterevidence retrieval。缺少这些，系统仍只是 long-document writer。

## 6. Benchmark 与 baselines

### Primary

1. PaperBench 或 CORE-Bench 低算力冻结子集：artifact→report consistency；
2. STORM/OpenScholar/PaperQA 风格 scientific synthesis 子集：source span→claim fidelity；
3. 20–30 trajectory corruption stress set：只作 mechanism test，不作为唯一证据。

### Diagnostic/no-harm

继续现有 length-controlled long-document benchmark，并报告 coherence、coverage、redundancy；用途仅是证明 research repair 没破坏写作。

### Baselines

`single-pass`, `static PDCA`, `DSPy/static optimized prompt`, `unrestricted full rewrite`, `selective repair without gate`, `PGSR`, `oracle defect location`。全部固定 model、trajectory/source pool、token/tool budget。

### Primary outcomes

unsupported quantitative claim rate、trace–report mismatch、negative-result disclosure recall、repair precision/regression、artifact success no-harm、expert readability no-harm、cost。统计单位是 trajectory/topic/repo，做 paired bootstrap/randomization，不能把同一报告的 claims 当独立样本。

## 7. 21 天不可滑动 critical path

### D1–D3：冻结问题

* 完成 trajectory availability audit；
* 在可联网环境完成正式 novelty search；
* 冻结两个外部子集、模型、预算、hypotheses、统计分析；
* 确定 executable 或 citation-only 分支。

### D4–D7：数据与 TraceVerifier

* provenance schema 和 migration；
* deterministic validators；
* corruption generator；
* 双人标注和 adjudication；
* frozen NLI baseline + TraceVerifier fine-tune。

### D8–D11：PGSR

* 七个 typed repair actions；
* edge-local patch executor；
* shadow validation、no-harm、rollback、lineage；
* unit/integration/adversarial tests。

### D12–D16：主实验

* 6 baselines；
* 2 model families（预算不足时 1 主模型 + 小型 transfer）；
* 3 seeds；
* matched-budget Pareto 与全部 ablations。

### D17–D18：人评与统计

* 专家 blind pairwise；
* calibration、CI、paired significance；
* failure taxonomy 和负结果。

### D19–D21：锁稿

* main paper、methods、limitations、appendix；
* code/data/model card/reproduction commands；
* 按 NMI reporting checklist 自审；
* D21 冻结，不因单个不显著结果临时改 benchmark。

## 8. Go / pivot

**方法稿 Go：** PGSR 在 unseen trajectory 上相对 static PDCA 与 full rewrite 同时降低 unsupported quantitative claims 和 trace–report mismatch，且 artifact/readability 不退化。

**Measurement pivot：** repair 不显著，但现有 report judges 对 corruption 显著不敏感，则主稿转为 trajectory-fidelity measurement；PGSR 作为验证性 baseline。

**Stop claim：** deterministic baseline 已完全解决问题、拿不到真实 artifact、或 formal novelty audit 找到同机制直接最近邻，则停止宣称方法 novelty。
