# Test Cases (English / Traditional Chinese)

Three more example sets for the merged grading agent — English and Traditional
Chinese only (no Simplified Chinese). Each set uses a different grading-standard
type, so together they cover all three grader modes.

| Set | Subject | Grading standard type | Grader mode | KB `standard_type` |
|---|---|---|---|---|
| 01 | English argumentative essay | Holistic bands A–E (open-ended) | MODE C (band judgement) | `grade_bands` |
| 02 | Mathematics (Grade 8) | Points per question, /100, method marks | MODE B (per-question) | `points_per_item` |
| 03 | 中國語文 記敘文 (Traditional Chinese) | 分項評分 優/良/合格/待改進 | MODE A (dimension) | `rubric_matrix` |

## How to run
1. **Build KB (update branch):** upload `reference_materials.md` + `grading_standard.md` for one set.
   Check the KB's `status_message` shows the grading standard was captured (`provided: true`).
2. **Grade (grade branch):** with that set's KB in place, upload `student_submission.md`.
3. The report language should follow the submission: sets 01/02 → English report; set 03 → Traditional Chinese report.

---

## Planted issues (check the grader catches these)

### 01 English essay (Student A) — expected band: **D**, at most **C**
- **No counterargument** at all (a C/D marker).
- **Reasons listed, not developed**, almost no evidence ("phones are useful… there are many apps").
- **Informal / slang tone**: "chill", "way better", "basically" — the register rule caps it at C.
- **Weak thesis** ("phones are really useful").
- **Too short** (~150 words) and spelling/grammar slips ("Thats").
- ✅ Check: grader uses **A–E bands** (not a 1–5 score), picks the band by descriptor, cites the missing counterargument + informal tone, and states pass/fail against **C**.

### 02 Maths (Student B) — expected total ≈ **74–78 (grade B/C border)**
Per-question expectation (method marking):
- Q1 A ✓ 4 · Q2 B ✓ 4 · Q3 A ✓ 4 · **Q4 D ✗ 0** (answer is C)
- Q5–Q8 all ✓ → 16
- Q9 ✓ full **8**
- **Q10 → sign error** ("−(−5)" taken as "−5", answer −25): power & multiply correct → **~6/8**
- **Q11 blank → 0/8**
- Q12 ✓ full **8**
- **Q13 → arithmetic slip** (8 ÷ 2 written as x = 3), method correct → **~6/8**
- Q14 full correct → **14/14**
- **Q15 → final answer only, no working** → per the partial-credit rule, answer mark only → **2/14**
- ✅ Check: grader marks **question by question with method marks**, gives **partial credit** on Q10/Q13, applies the **"answer only, no working"** rule on Q15, sums to /100 and maps to the **A/B/C/D/F boundaries**.

### 03 中國語文記敘文 (學生C) — 預期綜合等級：**合格** 邊緣或 **待改進**
- **偏題**：題目要寫「嘗試」，學生寫成「到公園玩的一天」，只有「划船」一小段沾邊 → 內容與立意應評低。
- **流水帳**：按時間平鋪（早上→公園→划船→午餐→回家），無詳略 → 結構與詳略應評低。
- **無感悟／點題弱**：結尾只是「很開心、很難忘」。
- **心理描寫欠缺**，文句平淡。
- **字數不足 600**（約 260 字）→ 應觸發字數扣分。
- ✅ 檢查：批改器是否用 **優/良/合格/待改進** 這套等級（而非預設 1–5 分）、是否逐項對照描述、是否數了字數並扣分、報告是否為**繁體中文**。

---

## Notes
- Student submissions are deliberately mid-to-weak so you can see whether the grader is honest and avoids over-praising.
- The three grading standards use three different scales on purpose, to verify the grader is fully dynamic (no hard-coded rubric).
- Companion Simplified-Chinese sets are in `../examples/` (语文/数学/英语).
