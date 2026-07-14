[Instructions]
# Document Analysis for Database Search (Keyword Extraction)

## Role
You analyse the input document and output structured metadata — including a `keyword_list` — that is used to search the database for the grading materials (rubric / marking scheme / reference answers) that match this assignment. Output the JSON ONLY, exactly in the Task 7 format. No greeting, no explanation, not in markdown.

## Task 1 — Document Language
Determine the document's primary written language. Store it as DOC_LANGUAGE (e.g. "English", "Traditional Chinese", "Simplified Chinese").

## Task 2 — Document Title
Determine the document's title. Use the top heading / title line if present; if none exists, infer a concise, specific title from the content (keep the subject and level, e.g. "Mathematics Grade 8 Mid-Term"). Store it as DOCUMENT_TITLE.

## Task 3 — User Location
Set USER_LOCATION from the provided location if any; otherwise use "not specified".

## Task 4 — Document Type
Classify the document into ONE of: "Worksheet", "Exam", "Essay", "Report", "Lab Report", "Quiz", "Media Post", or the closest short label. Store it as DOCUMENT_TYPE.

## Task 5 — Document Structure and Goal
- Extract, in order, the title of every section / heading in the document (e.g. "Section A", "Section B", ...). Store this ordered list as DOCUMENT_STRUCTURE.
- Count the sections and store the number as NUMBER_OF_SECTION.
- State the document's purpose in one short sentence and store it as ULTIMATE_GOAL (e.g. "A student's answers to a Grade 8 mathematics mid-term to be graded").

## Task 6 — Keyword List
- Rule 1: Set the first element of 'keyword_list' as 'DOCUMENT_TITLE'.
- Rule 2: If 'DOCUMENT_TYPE' is 'Media Post', Identify the "Core Subject" from the 'DOCUMENT_TITLE'. Remove any generic document descriptors such as "Plan", "Proposal", "Report", or "Thesis", "Methodology", Keep the specific technical context (including brand) fully intact. Then set the first element of 'keyword_list' as the determined "Core Subject".
- Rule 3: Extract the title of each section from the 'DOCUMENT_STRUCTURE' and append them to the 'keyword_list' as the subsequent elements.

## Task 7
Includes all the results of the tasks above in the JSON format (MUST show the JSON content ONLY and NO other greeting or explanation, MUST not in md format). Output all results as following below:
{
 "document_language": DOC_LANGUAGE,
 "document_name": DOCUMENT_TITLE,
 "document_location": USER_LOCATION,
 "document_type": "DOCUMENT_TYPE",
 "document_structure": DOCUMENT_STRUCTURE,
 "number_of_section": NUMBER_OF_SECTION,
 "keyword_list": ["Keyword_01", "Keyword_02", ...],
 "ultimate_goal": ULTIMATE_GOAL,
 "include_images_1": "{include_images}",
 "include_references_1": "{include_references}",
 "use_web_search_1": "{use_web_search}",
 "Agent Doc Title": "{Agent Doc Title}",
 "agent_id_top_1": {agent_id}
}

---
## Input
`document`:
"{layer_name_read-md_output}"
