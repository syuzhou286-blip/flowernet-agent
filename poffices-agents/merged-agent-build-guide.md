# 合并 Agent 最终接线表（对照你已搭好的卡 · 单老师 teacher_1）

你已经把整条链搭出来了。这份文档 = 你的每张卡应该怎么设 + 本次必须修的点。
Context Key 先全部写死 `teacher_1`，跑通后再换成平台登录用户ID。

---

## 0. 本次必须修的 3 个点

1. **Input Analysis 的 Instance Name 从 `input` 改成 `intent`** —— 否则 If 里的
   `{layer_name_intent_output}` 取不到值。
2. **If 条件匹配 `update` 不是 `upload`**：`!{layer_name_intent_output}.includes("update")`
3. **合并卡输出改成纯 JSON**（已在 `reference_rules` 里改好），并且
   **Text Merge(13) 回执引用 `{layer_name_combine_kb_output}`**（不是 `{layer_name_gen-docx1_output}`）。

---

## 1. 整条链（你已搭的拓扑）

```
[Input Analysis (7)]  改名 intent，读 {query}，输出 grade / update_kb
        ▼
[Metadata Extraction (10)]  解析上传文件（沿用旧 agent）
        ▼
[Metadata Extraction (11) read-md]  产出 {layer_name_read-md_output}
        ▼
[If-Then-Else (1)]  !{layer_name_intent_output}.includes("update")
        │
   then │(打分)                                 else │(更新库)
        ▼                                            ▼
[Agent Variable Read (6)]                      [Agent Variable Read (2)]
  Key=image_rules Ctx=teacher_1                  Key=image_rules Ctx=teacher_1
  Output=kb_json                                 Output=existing_kb
        ▼                                            ▼
[Text Generation (8) 批改]                     [Text Generation (9) extract_kb]
  {agent_knowledge}                              {agent_content_generate}
  student_worksheet={layer_name_read-md_output}  main_paper_text={layer_name_read-md_output}
  grading_knowledge_base={kb_json}               Output→{layer_name_extract_kb_output}
  ✓ Content to user  → 出报告                        ▼
                                               [Text Generation (12) combine_kb]
                                                 {reference_rules}
                                                 draft={layer_name_extract_kb_output}
                                                 existing_knowledge_base={existing_kb}
                                                 Output→{layer_name_combine_kb_output}  (纯JSON)
                                                     ▼
                                               [Agent Variable Write (3)]
                                                 Key=image_rules Ctx=teacher_1
                                                 VALUE={layer_name_combine_kb_output}
                                                 Output=writeSuccess
                                                     ▼
                                               [Text Merge (13)]  回执
                                                 INPUT={layer_name_combine_kb_output}   ← 改这里
                                                 ✓ Content to user
```

---

## 2. 逐卡设置（Input / Output / 下游）

| 卡片 | 关键设置 | 输出 token | 下游 |
|---|---|---|---|
| **Input Analysis (7)** | Instance=`intent`；Prompt=意图分类（第 4 节）；读 `{query}` | `{layer_name_intent_output}` = grade/update_kb | Metadata(10) |
| **Metadata Extraction (10)** | 沿用旧 agent 原样 | 内部 | Metadata(11) |
| **Metadata Extraction (11) read-md** | 沿用旧 agent 原样 | `{layer_name_read-md_output}` = 上传文件的文本 | If(1) |
| **If-Then-Else (1)** | 条件 `!{layer_name_intent_output}.includes("update")` | — | then→Read(6)；else→Read(2) |
| **Agent Variable Read (6)** | Key=`image_rules`；Ctx=`teacher_1`；Output=`kb_json` | `{kb_json}` = 该老师已存的 KB | Text Gen(8) |
| **Text Generation (8) 批改** | Prompt=`{agent_knowledge}`；`student_worksheet`={layer_name_read-md_output}；`grading_knowledge_base`={kb_json}；✓Content to user | 报告 | 结束（给老师） |
| **Agent Variable Read (2)** | Key=`image_rules`；Ctx=`teacher_1`；Output=`existing_kb` | `{existing_kb}` = 旧 KB（首次为空） | Text Gen(9) |
| **Text Generation (9) extract_kb** | Prompt=`{agent_content_generate}`；`main_paper_text`={layer_name_read-md_output} | `{layer_name_extract_kb_output}` = 草稿KB(纯JSON) | Text Gen(12) |
| **Text Generation (12) combine_kb** | Prompt=`{reference_rules}`；`draft`={layer_name_extract_kb_output}；`existing_knowledge_base`={existing_kb} | `{layer_name_combine_kb_output}` = 最终KB(纯JSON) | Write(3) |
| **Agent Variable Write (3)** | Key=`image_rules`；Ctx=`teacher_1`；VALUE=`{layer_name_combine_kb_output}`；Output=`writeSuccess` | `{writeSuccess}` = yes/no | Text Merge(13) |
| **Text Merge (13)** | INPUT=`{layer_name_combine_kb_output}`；✓Content to user | 回执 | 结束（给老师） |

> 变量槽分配（三个 prompt 互不同名，`image_rules` 留作 KB 储物格）：
> `agent_content_generate`=建库提取 · `reference_rules`=建库合并 · `agent_knowledge`=批改。
> Input Analysis 的意图 prompt 直接写在卡里，不占变量槽。

---

## 3. 关于"合并 KB 能不能靠现有 prompt 完成"——能

你的建库分支是**两步**，和旧 Agent 1 完全一致，够用：
- `extract_kb (9)` 跑 `{agent_content_generate}` → 从上传材料抽出一份**草稿 KB（纯JSON）**。
- `combine_kb (12)` 跑 `{reference_rules}` → 把草稿 + 旧 KB(`existing_kb`) **合并、校验、去重**，
  输出**最终纯 JSON**（本次已把它从"Part1+Part2"改成"纯JSON"，警告与状态放进
  JSON 的 `warnings` / `status_message` 字段，多批次合并才不会坏）。
- 首次建库时 `existing_kb` 为空 → 合并 prompt 的 first-batch 处理会自动忽略空值。

所以合并任务是覆盖到的，你不缺 prompt。唯一要保证的是上面三个变量槽里的 prompt
分别是对的（提取 / 合并 / 批改），别放串。

---

## 4. 意图分类 Prompt（放进 Input Analysis(7) 的 Prompt 框）

⚠️ 必须写完整的判断指令，不能只写 `'User_Query': {query}` —— 那样模型不知道要输出
`update_kb`/`grade`，If 就分流不了。按其它卡的 `[Instructions] ... --- ## Input` 格式：

```
[Instructions]
你是一个意图路由器。读取下面 `User_Query` 里老师的请求，只输出一个词，不要输出任何其它内容：
- 老师想【建立或更新知识库】（上传了评分标准/评分细则/答案/参考资料，或说“建库”“更新知识库”“上传评分标准”“update/build knowledge base”）→ 输出：update_kb
- 老师想【批改学生作业】（上传了学生作业/作文/答卷，或说“批改”“打分”“评一下”“grade”）→ 输出：grade
- 分不清时 → 输出：grade

只允许输出下面之一（全小写，无引号、无标点、无解释）：
update_kb
grade
---
## Input
'User_Query': {query}
```

> 若老师常常只上传文件、不打字，query 太薄会判不准。想更稳，可把 Input Analysis
> 移到两张 Metadata 之后，并在 `## Input` 里多喂一句 `{layer_name_read-md_output}` 的开头
> 让它也参考文件内容再判。先做 demo 的话，让老师在 query 里说清楚要干嘛即可。

---

## 5. 打通验证（单老师 teacher_1）

1. **建库**：query 写“更新知识库”，上传语文那套“参考资料+评分标准” →
   combine_kb 出纯 JSON → Write 成功 → 回执显示 `status_message`（应为 ✅）。
2. **批改**：query 写“批改这份作文”，上传“学生作业.md” →
   Read(6) 取到刚存的 KB → 出中文报告，**全程不用手动 copy JSON**。
3. **空库兜底**：换个没建过库的行为直接批改 → grader 触发 NO-STANDARD MODE，
   提示先建库，而不是乱打分。

跑通后，把所有 `teacher_1` 换成平台登录用户ID，即支持多老师。
