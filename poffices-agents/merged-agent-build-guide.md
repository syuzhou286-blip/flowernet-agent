# 合并 Agent 最终接线表（对照你已搭好的卡 · 单老师 teacher_1）

你已经把整条链搭出来了。这份文档 = 你的每张卡应该怎么设 + 本次必须修的点。
Context Key 先全部写死 `teacher_1`，跑通后再换成平台登录用户ID。

---

## 0. 本次必须修的点

1. **Input Analysis 的 Instance Name 从 `input` 改成 `intent`** —— 否则 If 里的
   `{layer_name_intent_output}` 取不到值。
2. **Input Analysis 的 Prompt 必须是完整分类指令**（第 4 节），不能只写 `'User_Query': {query}`。
3. **If 条件匹配 `update` 不是 `upload`**：`!{layer_name_intent_output}.includes("update")`
4. **合并卡输出改成纯 JSON**（已在 `reference_rules` 里改好），并且
   **Text Merge(13) 回执引用 `{layer_name_combine_kb_output}`**（不是 `{layer_name_gen-docx1_output}`）。
5. **三张储物格卡的 Context Key 必须完全一致** —— Read(6)、Read(2)、Write(3) 用同一把钥匙。
   两种写法二选一，别混用：
   - 做法 A（推荐单老师 demo）：三处都用**纯文本 `teacher_1`**（不加花括号，也不用定义变量）。
   - 做法 B（师兄那套）：三处都用 `{teacher_1}`，**并且**像师兄的 demo 那样加一张 Text Merge
     定义变量 `teacher_1`（`{ "teacher_1": "teacher_1" }`）。花括号 `{teacher_1}` = "变量 teacher_1 的值"，
     不定义就解析成空。
   你现在是混用（Read6=纯文本、Read2/Write3=`{teacher_1}` 且没定义变量），存和读对不上 →
   建好的库打分时读不到，会误以为建库失败。多老师时走做法 B，把变量换成平台登录ID `{user_id}`。

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
| **Text Merge (13)** | INPUT 回执三选一：`{writeSuccess}`（只显示 yes，最省事）／`{layer_name_combine_kb_output}`（含 ✅/❌ 状态但一坨JSON）／推荐加一张小状态卡只输出 `status_message`；✓Content to user | 回执 | 结束（给老师） |

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
`update_kb`/`grade`，If 就分流不了。指令全部用英文，按其它卡的 `[Instructions] ... --- ## Input` 格式：

```
[Instructions]
You are an intent router. Read the teacher's request in `User_Query` below and output exactly ONE word, nothing else:
- If the teacher wants to BUILD or UPDATE the knowledge base (they uploaded a grading rubric / marking scheme / answer key / reference materials, or the request says things like "build the knowledge base", "update the knowledge base", "upload the grading standard") → output: update_kb
- If the teacher wants to GRADE a student submission (they uploaded a student's worksheet / essay / answers, or the request says things like "grade this", "mark this", "score this worksheet") → output: grade
- If it is unclear → output: grade

Output ONLY one of the following, lowercase, with no quotes, no punctuation, and no explanation:
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

---

## 6. 排障：不管传什么都得到 grader 的 "No grading standard found"

**症状**：上传材料、query 写“update the knowledge base”，却收到 grader 的
“⚠ No grading standard found…”报告（Overall Performance Summary / Key Priorities…）。

**这不是 grader 或 rubric 检测的 bug。** 那份报告本身就是 Grade 分支（then）的输出——
说明请求被**错误路由到了 Grade 分支**，拿你上传的材料当“学生作业”去批一个空 KB。
Update 分支根本没跑，所以什么都没写进抽屉，KB 一直空。grader 对空 KB 说“没有标准”是**正确反应**。

**根因**：If 条件 `!{layer_name_intent_output}.includes("update")` **恒为 true**，即
`{layer_name_intent_output}` 里不含 "update" —— token 要么是空，要么分类器没输出 `update_kb`。

**排查顺序**：
1. 看运行记录里 `intent`（分类）节点**实际输出了什么**，应恰好是 `update_kb`。
   - 输出为空 / 不是 update_kb → 分类器 Prompt 的问题：确认是完整英文分类指令
     （`[Instructions] … update_kb / grade … --- ## Input`），不是只有 `'User_Query': {query}`。
   - 输出确实是 update_kb 却仍走 grade → If 里的 token 没解析成它（取到空 → 恒 true）。
2. ⚠️ `{layer_name_<name>_output}` 这套命名是给 **Text Generation / LLM 卡**用的
   （read-md、extract_kb、combine_kb 都是）。**Input Analysis 是另一种卡，其输出 token
   不一定叫 `{layer_name_intent_output}`** → If 取到空 → 每次都走 Grade。
   **最稳修法**：分类器改用一张 **Text Generation 卡**（跟 combine_kb 同类型），instance 名 `intent`，
   Prompt 放英文分类指令 —— 它的输出 `{layer_name_intent_output}` 就会像其它卡一样正常解析。
