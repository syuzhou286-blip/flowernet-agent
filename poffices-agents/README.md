# Poffices.ai Agent Prompts — Grading System

Prompts for the two poffices.ai Creator Studio agents that power the worksheet
grading demo for primary/secondary school teachers.

| Agent | Poffices workspace | Purpose |
|---|---|---|
| Knowledge Base Builder | agent=359 | Turns the teacher's uploads (grading database + grading standard) into one merged knowledge-base JSON |
| Worksheet Grader | agent=360 | Grades a student submission using that knowledge-base JSON |

## Where each file goes

### Agent 1 — Knowledge Base Builder (agent 359)

| File | Paste into variable |
|---|---|
| `knowledge-base-builder/content_generate.md` | **Content Generate** (`agent_content_generate`) — used by LLM Layer `gen-docx1` (extraction step) |
| `knowledge-base-builder/image_rules.md` | **Image Rules** (`image_rules`) — used by LLM Layer `gen-docx2` (merge/validate step; `existing_knowledge_base` comes from `{query}`) |

Also update the input instruction on the **Merge (id 39)** node to make the
grading-standard upload explicit and mandatory, e.g.:

> Please upload 2 files: (1) the reference materials / database needed for
> grading (source documents, model answer, reference guides); (2) the grading
> standard — marking rubric, scoring scheme, per-question mark allocation, or
> grade band descriptors. The grading standard is REQUIRED: without it the
> knowledge base cannot be used for grading. If your grading standard is part
> of a combined document, make sure that document includes it.

### Agent 2 — Worksheet Grader (agent 360)

| File | Paste into variable |
|---|---|
| `worksheet-grader/content_generate.md` | **Content Generate** (`agent_content_generate`) — used by LLM Layer `gen-docx1` |
| (knowledge-base JSON produced by Agent 1) | **Image Rules** (`image_rules`) — referenced in the layer prompt as `grading_knowledge_base` |

## How the two agents connect

1. Teacher runs Agent 1 with the database materials **and** the grading
   standard. Output = Part 1 status summary + Part 2 merged knowledge-base
   JSON. The first line of Part 1 says ✅/❌ whether a grading standard was
   found — if ❌, ask the teacher for the rubric and re-run before demoing
   the grader.
2. For multi-batch uploads, paste the previous JSON into Agent 1's `query`
   (`existing_knowledge_base`) and upload the next batch; the merge step
   combines them without losing data.
3. Copy the final JSON (Part 2 code block only) into Agent 2's **Image Rules**
   variable. Agent 2 reads `grading_standard` from it and grades in whatever
   system the teacher's standard defines:
   - `rubric_matrix` → per-dimension levels with verbatim descriptors
   - `points_per_item` → per-question marks with partial credit (maths etc.)
   - `grade_bands` / `holistic` → band judgement (A–E, 优/良/合格, …)
   - `checklist` → met / not-met criteria
   - `hybrid` → each component under its own rules, combined per the
     standard's aggregation rules
4. Backward compatibility: Agent 2 falls back to the legacy `scoring_anchors`
   section if an old knowledge base has no `grading_standard`; Agent 1's merge
   step migrates legacy `scoring_anchors` into `grading_standard`
   automatically.
