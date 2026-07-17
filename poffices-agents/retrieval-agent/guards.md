# Front-of-flow Guards (no-file check + OCR-fail check)

Two guards placed at the front of the agent, BEFORE the intent routing and the
update/grade branches.

```
[Input: query + file]
        │
[Guard 1 · If-Then-Else]  condition:  {file_urls}.length > 0
   ├─ else (no file)  → [Msg card: "please upload a file"]  → END (Content to user)
   └─ then (has file) →  [intent (Text Gen)] → [AOA mde: parse] → [read-md]
                              │
                        [ocr_check (Text Gen)]  → outputs ocr_ok / ocr_fail
                              │
[Guard 2 · If-Then-Else]  condition:  {layer_name_ocr_check_output}.includes("fail")
   ├─ then (OCR failed) → [Msg card: "OCR fail…"]  → END (Content to user)
   └─ else (OCR ok)     → [intent routing If (id 50)] → update / grade branches
```

---

## Guard 1 — No file uploaded

**Card:** an If-Then-Else placed as the FIRST node.
**Condition (JavaScript):**
```
{file_urls}.length > 0
```
- `else` (condition false = no file) → an output card (Text Merge / Content-to-user) with the fixed message below, then END. Do NOT wire it onward.
- `then` (has file) → continue to the intent classifier and the rest of the flow.

**Message card content (fixed text, Content to user):**
```
Please upload a file first (the assignment / textbook / grading materials), then send your request again.
请先上传文件（作业 / 教科书 / 评分标准），再重新发送你的请求。
```

No LLM is needed for Guard 1 — it is a pure condition on `{file_urls}`.

---

## Guard 2 — OCR / extraction failed (handwritten or blurry)

Placed AFTER the extractor (`read-md`). Two new cards: an `ocr_check` Text Generation card, then an If-Then-Else.

### Card A — `ocr_check` (Text Generation, gpt-4.1-mini)
Instance Name: `ocr_check`. Prompt:
```
[Instructions]
You check whether text extraction / OCR succeeded on an uploaded document.
The input is the extractor's output. IGNORE any URLs, file paths, or metadata fields (such as s3_url, storage_mode); judge ONLY the actual document text (e.g. the "md_content").

Output exactly ONE word, nothing else:
- ocr_fail — if the actual document text is empty, or contains only a few stray / garbled characters, isolated symbols, or no meaningful words or sentences (typical of a blurry photo or a handwritten page OCR could not read).
- ocr_ok — if the actual document text contains real, readable words or sentences.

Output ONLY one of the following, lowercase, no quotes, no punctuation, no explanation:
ocr_ok
ocr_fail
---
## Input
`extractor_output`:
"{layer_name_read-md_output}"
```

### Card B — If-Then-Else
**Condition (JavaScript):**
```
{layer_name_ocr_check_output}.includes("fail")
```
- `then` (contains "fail" = OCR failed) → an output card with the message below, then END.
- `else` (OCR ok) → continue to the intent routing If (id 50) and the update / grade branches.

**Message card content (fixed text, Content to user):**
```
OCR failed: the image is not clear enough or the text could not be recognised (e.g. a handwritten or blurry scan). Please upload a clearer file, or a document whose text can be selected.
OCR 识别失败：图片不够清晰或文字无法识别（例如手写或模糊的扫描件）。请上传更清晰的文件，或可选中文字的文档。
```

---

## Notes
- Guard 1 must be the FIRST node so the file parse / AOA never run without a file.
- Guard 2's `ocr_check` reads `{layer_name_read-md_output}` — if your parse card has a different instance name, use its real output token (same rule: instance name ↔ `{layer_name_X_output}`).
- Both message cards are dead-ends: mark "Content to user" and do NOT connect them onward, so the flow stops there.
