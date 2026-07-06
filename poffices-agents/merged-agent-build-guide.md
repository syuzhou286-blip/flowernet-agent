# 合并 Agent 搭建手册（Knowledge Base Builder + Worksheet Grader → 一个 Agent）

目标：把原来分开的两个 Agent 合并成一个，用平台的 `Agent Variable Write/Read`
（按老师存取的"储物格"）替代人手复制 JSON。

---

## 0. 一个核心概念：按老师分格子的储物柜

师兄的 demo 证明了平台有这对节点：

- **Agent Variable Write** = 把一段内容**存**进储物格
- **Agent Variable Read** = 从储物格**读**回内容

每个格子由两把钥匙定位：
- **VARIABLE KEY**：储物柜的名字（我们统一用 `knowledge_base`；师兄 demo 里用的是 `image_rules`，用哪个名都行，但两条分支必须用同一个名）
- **CONTEXT KEY**：老师的身份 `{teacher_id}` —— 这就是"多个老师互不串"的关键

> 记住：**Write 到 (`knowledge_base`, `teacher_id`)** 存进去的东西，
> 只有 **Read 同一个 (`knowledge_base`, `teacher_id`)** 才能读回来。

---

## 1. 整体流程

```
输入(query + 文件 + teacher_id)
      │
      ▼
[① 意图分类 LLM]  →  输出 {intent} = "update_kb" 或 "grade"
      │
      ▼
[② If-Then-Else]  条件: {intent}.includes("update")
      │
 then │(更新知识库)                       else │(批改作业)
      ▼                                        ▼
[Agent Variable Read]  读旧KB               [Agent Variable Read]  读该老师KB
  key=knowledge_base                          key=knowledge_base
  context=teacher_id                          context=teacher_id
  输出 existing_kb                            输出 grading_knowledge_base
      ▼                                        ▼
[Metadata Extraction] 解析上传的评分材料      [Metadata Extraction] 解析上传的学生作业
      ▼                                        ▼
[LLM: 提取]  = Agent1 的 Content Generate     [LLM: 批改] = Agent2 的 Content Generate
  产出 draft_kb                                 image_rules 输入 = {grading_knowledge_base}
      ▼                                         student_worksheet 输入 = 解析出的作业
[LLM: 合并校验] = Agent1 的 Image Rules        产出 评分报告
  existing_knowledge_base = {existing_kb}          ▼
  产出 final_kb (Part2 的 JSON)              [Content to user] 输出报告
      ▼
[Agent Variable Write]  写回新KB
  key=knowledge_base
  context=teacher_id
  VALUE = {final_kb}
      ▼
[Content to user] 回执 "✅ 知识库已更新"
```

---

## 2. 逐个节点怎么搭

### 节点 A — 输入
沿用你现在的输入节点。需要拿到三样东西：
- `{query}`：老师打的字
- `{file_urls}` / 上传的文件
- `{teacher_id}`：老师身份（见第 5 节，决定用什么当它）

### 节点 ① — 意图分类（新增一个 LLM Layer）
- 类型：LLM Layer
- Prompt：见第 3 节，直接粘
- 输出变量：`intent`（内容是 `update_kb` 或 `grade`）

### 节点 ② — If-Then-Else（新增）
- 条件：`{intent}.includes("update")`
- **then** 分支 → 接"更新知识库"那串节点
- **else** 分支 → 接"批改作业"那串节点

### then 分支（更新知识库）= 你的 Agent 1
| 顺序 | 节点 | 关键设置 |
|---|---|---|
| 1 | **Agent Variable Read** | VARIABLE KEY=`knowledge_base`；CONTEXT KEY=`{teacher_id}`；OUTPUT=`existing_kb` |
| 2 | **Metadata Extraction** | 解析老师上传的评分材料（评分标准+数据库），跟你 Agent 1 现在一样 |
| 3 | **LLM Layer（提取）** | 粘 Agent 1 的 **Content Generate**（`knowledge-base-builder/content_generate.md`） |
| 4 | **LLM Layer（合并校验）** | 粘 Agent 1 的 **Image Rules**（`knowledge-base-builder/image_rules.md`）；把 `existing_knowledge_base` 输入接上一步 Read 的 `{existing_kb}` |
| 5 | **Agent Variable Write** | VARIABLE KEY=`knowledge_base`；CONTEXT KEY=`{teacher_id}`；VALUE=第4步产出的 **final JSON**；OUTPUT=`write_result` |
| 6 | **Content to user** | 回执，例如把合并步骤的 Part 1 状态摘要给老师看 |

> ⚠️ 第 5 步是**覆盖**写入（不是追加）。这是对的：因为第 4 步的合并已经把
> 旧KB（`existing_kb`）折进去了，写回的是"旧+新"的完整版，不会重复。

### else 分支（批改作业）= 你的 Agent 2
| 顺序 | 节点 | 关键设置 |
|---|---|---|
| 1 | **Agent Variable Read** | VARIABLE KEY=`knowledge_base`；CONTEXT KEY=`{teacher_id}`；OUTPUT=`grading_knowledge_base` |
| 2 | **Metadata Extraction** | 解析老师上传的学生作业 |
| 3 | **LLM Layer（批改）** | 粘 Agent 2 的 **Content Generate**（`worksheet-grader/content_generate.md`）；它的 `image_rules` / `grading_knowledge_base` 输入接第1步的 `{grading_knowledge_base}`；`student_worksheet` 接第2步解析出的作业 |
| 4 | **Content to user** | 输出评分报告 |

---

## 3. 意图分类节点的 Prompt（直接粘进节点①）

```
# 意图路由 / Intent Router

你要判断老师这次请求属于哪一类，只输出一个词，不要输出别的任何内容。

两类：
- update_kb：老师在提供"评分用的材料"来建立或更新知识库。信号：上传的是评分标准/
  评分细则/答案/参考数据库；或 query 里说"建立知识库""更新知识库""上传评分标准""build/update knowledge base"。
- grade：老师想让你"批改一份学生作业"。信号：上传的是学生填好的作业/作文/答卷；
  或 query 里说"批改""打分""grade this""评一下这份作业"。

判断规则：
1. 上传的像"标准/答案/参考资料" → update_kb
2. 上传的像"学生自己写的、待批改的作业" → grade
3. query 明确说了要做什么，就以 query 为准
4. 实在分不清 → 输出 grade（批改分支在没有知识库时会安全地提示老师先建库）

只输出下面之一，不要加引号、标点、解释：
update_kb
grade
```

若你的平台上 If 节点更适合读结构化字段，也可以改用 **Metadata Extraction**，
schema 设 `{ "intent": "string" }`，让它输出 `update_kb`/`grade`，再在 If 里用 `{intent}`。

---

## 4. If 条件怎么写

- 更新分支放 **then**，条件：`{intent}.includes("update")`
- 其余（含分不清时的 `grade`）自动走 **else**

（用 `includes` 而不是 `== "update_kb"`，是为了容忍模型偶尔多出空格/换行。）

---

## 5. teacher_id（Context Key）= 平台登录用户ID / session（已定）

**决定：所有 Agent Variable Read/Write 的 CONTEXT KEY 一律用平台的登录用户ID / session。**
老师无感知、每人天然唯一、也不怕填错，是最稳的方案。

你要做的只有一件事：**在 poffices 节点里找到"当前登录用户"对应的那个系统变量的
真实名字**（可能叫 `{user_id}`、`{session_id}`、`{uid}` 或类似），然后把本手册中所有
出现 `{teacher_id}` 的地方，统一替换成那个变量名。

- 师兄 demo 里手填的 `teacher_1`/`teacher_2` 只是模拟，真实环境换成这个登录ID变量即可。
- ⚠️ 前提：确认该变量在**每个节点里都取得到**、且**同一老师每次会话都相同**（不是每次
  请求都变的临时ID）。如果 session 变量每次请求都会变，就改用稳定的用户ID，否则第二次
  来批改时会读不到上次建的库。
- 找不到这个系统变量、或不确定它稳不稳，把节点里能选的变量列表截图发我，我帮你挑。

---

## 6. 两个 Prompt 已经帮你对接好的地方（不用额外处理）

1. **第一次建库时旧KB是空的** —— 合并 Prompt（Image Rules）里已加"First-batch
   handling：existing KB 为空/非JSON 就直接忽略"，所以 then 分支第1步 Read 到空也没事。
2. **还没建库就来批改** —— 批改 Prompt 里已加"NO-STANDARD MODE"：else 分支第1步
   Read 到空 → 批改器会**拒绝乱打分**，并提示老师"请先上传评分材料建立知识库"。
   → 所以"先建库后批改"的顺序保护是自动的，你不用再单独搭 if 去挡。

---

## 7. 最小验证步骤

1. 老师身份先固定用一个测试值（如 `teacher_test`）。
2. 走一次 **update_kb**：上传语文那套"参考资料+评分标准" → 看回执是否 ✅，
   并确认 Write 成功。
3. 紧接着走一次 **grade**：只上传"学生作业.md"，query 写"批改这份作文" →
   批改器应能 **Read 到刚才的 KB** 并出中文报告（不用你手动 copy 任何 JSON）。
4. 换一个没建过库的身份（如 `teacher_empty`）直接走 grade → 应触发
   NO-STANDARD MODE 的提示，而不是乱打分。
```
