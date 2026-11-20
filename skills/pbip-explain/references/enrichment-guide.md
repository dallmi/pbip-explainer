# Writing enrichment.json

`extract` writes a template to `work/<name>/enrichment.json` with an entry for every object that lacks a model description. `docs` merges it into the documentation and marks each text **AI-drafted · review**.

```json
{
  "report":   { "purpose": "", "audience": "", "howToUse": "", "dataFreshness": "", "owner": "" },
  "tables":   { "Engagement": { "description": "", "grain": "" } },
  "measures": { "Open Rate": { "description": "", "businessDefinition": "" } },
  "columns":  { "Campaign[Channel]": { "description": "" } },
  "pages":    { "Overview": { "purpose": "", "keyQuestions": [] } },
  "glossary": [ { "term": "", "definition": "", "relatedObjects": [] } ],
  "notes":    []
}
```

Empty strings are ignored, so a partly filled file is fine. Keys must match object names exactly.

## Sources you may use

DAX expressions and their dependencies, object names, display folders, format strings, page names and text boxes, visual titles, slicers, filters, Power Query sources and steps, existing descriptions. Nothing else. `owner` and `dataFreshness` usually stay empty unless the report states them in a text box or the user tells you.

## How to write

- **Measures**: one or two sentences. What it counts or compares, in business words, and the conditions that matter. Example: `"Joins": "Number of employees who joined in the selected month. Counts movement records of type Join for the snapshot month chosen in the Snapshot slicer; other date filters are ignored."`
- **Tables**: what one row is (the grain) and where it comes from. Example: `"grain": "One row per employee per monthly snapshot"`.
- **Pages**: the purpose in one sentence, then 2–4 questions a user answers with the page.
- **Report**: purpose and audience in plain words; `howToUse` = the filters to set first and how pages relate.
- **Glossary**: business terms that appear in titles or measure names but are not objects themselves (abbreviations like CTOR, GPN, YM, FTE). Only define what the report makes clear.
- Reuse the report's terms. Do not translate names.
- When the DAX contradicts the name (e.g. "Rate" that is not a ratio), say so in `notes` instead of smoothing it over.

## Order of work for large models

1. `report` and `pages` (users read these first).
2. Measures shown on pages, in page order.
3. Tables with role fact/dimension.
4. Remaining measures and columns.

Then `python3 -I "$S" docs work/<name>` and check the HTML once.

## Writing descriptions back into the model (only on request)

Descriptions belong in the model (tooltips in Power BI, used by Copilot). If the user wants that, add `/// text` lines above the object in the TMDL files (or `description` in model.bim) in a copy of the project and let the user open it in Desktop. Never overwrite the user's original files.
