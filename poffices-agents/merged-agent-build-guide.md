# 合并 Agent 搭建手册（平台精确版 · 单老师 teacher_1）

把原来分开的两个 Agent 合并成一个。用平台的 `Agent Variable Read/Write`
（按老师存取的"储物格"）替代人手复制 JSON。**先只做一个老师**，Context Key 一律
写死 `teacher_1`，跑通后再考虑多老师。

---

## 0. 最关键的一点（先看这个）

Read/Write 只"搬运" KB JSON，本身不建库、不打分。真正干活的是**跑旧 prompt 的 LLM 卡**。
所以每条分支都是"搬运卡 + LLM 卡"配合：

- **打分**：`Read 取KB` → `批改 LLM 卡` → 出报告
- **更新库**：`Read 取旧KB` → `建库 LLM 卡(提取)` → `建库 LLM 卡(合并)` → `Write 存新KB`
  （Write 的 VALUE = 合并卡的输出，**不是**手写的 `{"scheme":...}`）

---

## 1. 怎么"复用" Read/Write 卡

不用真的去 copy 师兄的卡。这两个是**卡片类型**，你有两种方式拿到：
- 从左侧节点面板里，直接拖一张新的 `Agent Variable Read` / `Agent Variable Write` 进来；
- 或点卡片右上角的**复制图标**（垃圾桶旁边那个方块图标）复制一张，再改里面的字段。

拿到后只需填三件事：`Variable Key`、`Context Key`、（Write 还有）`Value`。

---

## 2. 变量槽分配（不同 prompt 用不同变量名）

| 变量槽 | 放什么 |
|---|---|
| `image_rules` | ⚠️ 不放 prompt——留作每个老师的 **KB 储物格**（Read/Write 的 Variable Key 用它） |
| Input Analysis 卡的 Prompt 框 | **意图分类 prompt**（第 5 节，直接粘） |
| `agent_content_generate` | 旧 Agent 1 的 **Content Generate**（建库-提取） |
| `reference_rules` | 旧 Agent 1 的 **Image Rules**（建库-合并校验） |
| `agent_knowledge` | 旧 Agent 2 的 **Content Generate**（批改） |

> 说明：变量槽只是"容器"，放哪个都行，只要**三个 prompt 互不同名**。这里选这几个槽是为了避开 `image_rules`。

---

## 3. 整体连线

```
[Input Analysis (7)]  读 query，输出 grade / update_kb
        │
        ▼
[If-Then-Else (1)]  条件: !{layer_name_intent_output}.includes("update")
        │
   then │(打分)                              else │(更新库)
        ▼                                         ▼
[Agent Variable Read (6)]                   [Agent Variable Read (2)]
  Key=image_rules  Ctx=teacher_1              Key=image_rules  Ctx=teacher_1
  Output=kb_json                              Output=existing_kb
        ▼                                         ▼
★[LLM 卡: 批改]  (新增)                      ★[LLM 卡: 建库-提取] (新增)
  prompt={agent_knowledge}                    prompt={agent_content_generate}
  grading_knowledge_base={kb_json}            输入=上传材料解析文本
  student_worksheet=上传作业                  Output=draft_kb
  → Content to user 出报告                         ▼
                                             ★[LLM 卡: 建库-合并] (新增)
                                               prompt={reference_rules}
                                               draft={draft_kb}
                                               existing_knowledge_base={existing_kb}
                                               Output=final_kb
                                                   ▼
                                             [Agent Variable Write (3)]
                                               Key=image_rules  Ctx=teacher_1
                                               VALUE={final_kb}   ← 改这里
                                               Output=writeSuccess
                                               → Content to user 回执
```

★ = 你现在还没加、需要新增的 LLM 卡。其余卡你已经放好，只需按下面改字段。

---

## 4. 逐卡设置

### Input Analysis (id: 7)
- API：gpt-4.1-mini（或更强的模型，判断更稳）
- Instance Name：`intent`
- Prompt：粘第 5 节的意图分类 prompt

### If-Then-Else (id: 1)
- Condition：`!{layer_name_intent_output}.includes("update")`
  - 这样：只有明确"更新库"走 else，其它一律走 then（打分）——即使判断不清也安全（批改器读到空库会提示先建库）。
  - ⚠️ `{layer_name_intent_output}` 是 Input Analysis 的输出 token。按你现有 LLM 层的命名规律（如 `{layer_name_gen-docx1_output}`），instance 名叫 `intent` 就是 `{layer_name_intent_output}`。若平台显示的 token 名不同，用它实际显示的。
- Output Variable：可留空，或填 `isGrade`

### then 分支（打分）
**Agent Variable Read (id: 6)**
- Variable Key：`image_rules`
- Context Key：`teacher_1`（先写死，纯文本，不加花括号）
- Output Variable：`kb_json`

**★ 新增 LLM 卡（批改）** —— 接在 Read(6) 后面
- 用跟旧 gen-docx1 一样的 LLM 层卡片
- Instance Name：`grader`
- Prompt 框填：
  ```
  {agent_knowledge}

  ---
  ## Input
  `grading_knowledge_base`: {kb_json}

  `student_worksheet`: {query}
  ```
  （若学生作业是上传文件，就先加一张 Metadata Extraction 解析成文本，再把这里的 `{query}` 换成解析输出）
- 勾选 "Content to user"，把报告给老师

### else 分支（更新库）
**Agent Variable Read (id: 2)**
- Variable Key：`image_rules`
- Context Key：`teacher_1`
- Output Variable：`existing_kb`

**★ 新增 LLM 卡（建库-提取）** —— 接在 Read(2) 后面
- Instance Name：`kb_extract`
- Prompt 框填：
  ```
  {agent_content_generate}

  ---
  ## Input
  `main_paper_text`: {上传材料的解析文本}
  ```
  （上传的是 PDF/docx，就先加 Metadata Extraction 解析，再把它的输出接到这里）
- Output：`draft_kb`（即该卡输出 token `{layer_name_kb_extract_output}`）

**★ 新增 LLM 卡（建库-合并）** —— 接在 kb_extract 后面
- Instance Name：`kb_merge`
- Prompt 框填：
  ```
  {reference_rules}

  ---
  ## Input
  draft knowledge base: {draft_kb}
  `existing_knowledge_base`: {existing_kb}
  ```
- Output：`final_kb`（即 `{layer_name_kb_merge_output}`）

**Agent Variable Write (id: 3)**
- Variable Key：`image_rules`
- Context Key：`teacher_1`
- **Value：`{final_kb}`** ← 把原来手写的 `{"scheme":"balabala..."}` 换成这个 token
- Output Variable：`writeSuccess`
- 后面接一张 "Content to user"，把合并卡的 Part1 状态摘要回给老师

> 提示：我写 `{draft_kb}` / `{final_kb}` 只是好记的占位。实际引用时用该 LLM 卡真正的
> 输出 token（平台显示为 `{layer_name_<instance>_output}`）。Read/Write 的 Output Variable
> 则是你自己命名的，直接 `{kb_json}` / `{existing_kb}` 引用即可。

---

## 5. 意图分类 Prompt（粘进 Input Analysis 的 Prompt 框）

```
你是一个意图路由器。读取老师的请求，只输出一个词，不要输出任何其它内容。

老师的请求：
{query}

判断：
- 老师想【建立或更新知识库】（上传了评分标准/评分细则/答案/参考资料，或说“建库”“更新知识库”“上传评分标准”“build/update knowledge base”）→ 输出：update_kb
- 老师想【批改学生作业】（上传了学生作业/作文/答卷，或说“批改”“打分”“评一下”“grade this”）→ 输出：grade
- 分不清时 → 输出：grade

只允许输出下面之一（全小写，无引号、无标点、无解释）：
update_kb
grade
```

> `{query}` 换成你输入节点里代表"老师这次请求"的那个变量名（若不是 `query`）。

---

## 6. 打通验证（单老师）

1. 走一次 **update_kb**：query 写"更新知识库/上传评分标准"，上传语文那套
   "参考资料+评分标准" → 看 Write 是否成功、回执 ✅。
2. 紧接着走一次 **grade**：query 写"批改这份作文"，上传"学生作业.md" →
   批改器应能 **Read 到刚才的 KB**（Context Key 都是 teacher_1）并出中文报告，
   **全程不用你手动 copy JSON**。
3. 先不上传任何东西直接 grade（模拟空库）→ 批改器应触发"NO-STANDARD MODE"，
   提示先建库，而不是乱打分。

跑通后，把 teacher_1 换成第 5 节之前说的"平台登录用户ID"就能支持多老师。
```
