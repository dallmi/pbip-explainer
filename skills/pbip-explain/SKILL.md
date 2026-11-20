---
name: pbip-explain
description: "Understand, document and answer questions about a Power BI report and its semantic model from a PBIP project (zip or folder with .pbip, *.SemanticModel and *.Report; TMDL or model.bim; PBIR or legacy report.json). Use when someone hands over a PBIP zip or folder, asks how a Power BI report or semantic layer works, wants documentation, a data glossary, a data model diagram, DAX/measure explanations, relationships and join keys, lineage (source to table to measure to visual), impact of changing a column or measure, unused objects or a model health check. Also use for follow-up questions about a report that was already extracted (a folder with knowledge/model.json)."
---

# pbip-explain

Turns a Power BI project into a **knowledge pack** (structured facts) and **documentation** (Markdown + one self-contained HTML file), then answers questions from those facts.

```
PBIP zip/folder ──extract──▶ knowledge/model.json, *.csv, agent-brief.md ──docs──▶ docs/*.md, *.html, model-diagram.svg
                                         │                                    ▲
                                         └──── you draft enrichment.json ─────┘   (business wording, marked "AI-drafted")
```

Scripts are stdlib-only Python 3.9+. `S=<this skill>/scripts/pbip_explain.py`.

## 1. Extract and build the docs

```bash
python3 -I "$S" run <project.zip | project-folder | X.pbip> --out work/<name>
```

- Prints model and report stats and the number of findings. The zip is unpacked safely into `work/<name>/00-input/` (path traversal and links refused, data caches `*.abf` skipped).
- If it says no *.SemanticModel / *.Report was found: the file was saved as .pbix. Ask the user to use *File › Save as › Power BI project (.pbip)* in Desktop and zip the whole folder.
- A report with a live connection (`byConnection`) has no model in the package. Say so; you can only document the report side and the fields it uses.

## 2. Read before you say anything

1. `work/<name>/knowledge/agent-brief.md` – the overview: tables and roles, join keys, every measure with its DAX in one line, pages and what each visual shows, findings.
2. Look up details with the query commands (section 4) or in `knowledge/model.json` (schema: `references/knowledge-schema.md`). Do not read model.json whole when it is large; query it.

## 3. Enrich (draft the business layer)

The extraction holds what the files say. Most models have few descriptions, so the docs read technical. Fill `work/<name>/enrichment.json` (template already written) and rebuild:

```bash
python3 -I "$S" docs work/<name>
```

Rules, details in `references/enrichment-guide.md`:
- Derive every sentence from extracted facts: DAX, names, filters, page texts, titles, sources. No invented thresholds, owners, refresh times or business rules.
- Leave a field empty when the facts do not support it. Put open points into `notes`.
- Model descriptions always win; your text is shown with an **AI-drafted · review** badge. Never edit the PBIP files to add descriptions unless asked.
- Write for a business reader: what the number means, not how DAX computes it. Mention quirks the DAX reveals (e.g. "+ 0 shows 0 instead of blank", "ignores the date slicer").

Do this step whenever the user wants documentation for other people. Skip it for a quick technical question.

## 4. Answer questions

```bash
python3 -I "$S" query work/<name> <command> [args]
```

| Question | Command |
|---|---|
| What is in the model / report? | `summary` |
| Where does X appear? (name, description, DAX, page text) | `find <text>` |
| How is measure M calculated, what feeds it, where is it shown? | `measure <M>` |
| What is in table T, where does it come from, how is it joined? | `table <T>` |
| What is column T[C], who uses it? | `column T[C]` |
| What does page P show and which data feeds it? | `page <P>` |
| How are tables A and B connected / which join keys? | `path <A> <B>` |
| What breaks if I change or remove X? | `impact <M | T[C] | T>` |
| What can be cleaned up? | `unused`, `findings` |

How to answer (more in `references/answering-questions.md`):
- **Facts first, with the object names**: `[Open Rate]`, `Engagement[Date]`, page *Overview*. Quote DAX when the question is about logic.
- **Say what kind of statement it is**: extracted fact, pattern-based reading of DAX, or your interpretation. If unsure, say so.
- **A PBIP has no data.** Never state values, totals, row counts or refresh times. Answer "how is it calculated / where does it come from", not "what is the number".
- "Unused" means unused by the report(s) in this package; a shared model can serve other reports.
- Explain filter context in plain words: which slicers and filters reach a visual, which relationships carry them (direction, inactive ones only via USERELATIONSHIP, bi-directional ones in both directions).

## 5. Deliver

Point the user to:
- `work/<name>/docs/<Name>-documentation.html` – one file, works offline, for sharing (navigation, search, interactive model diagram, glossary, DAX catalog, lineage, sources, health checks).
- `work/<name>/docs/<Name>-documentation.md` – same content for wikis/Git (Mermaid diagram), plus `model-diagram.svg`.
- `work/<name>/knowledge/` – the knowledge pack: hand `agent-brief.md` + `model.json` (or the CSVs) to another agent, e.g. as knowledge files of a Copilot Studio agent.

Report in a few lines: what the report does (if enrichment was done), size, the top findings, open questions. Do not paste the docs into chat.

## Limits

See `references/limitations.md`. In short: no data, no visual-level formatting semantics, DAX is analysed by references not executed, table roles (fact/dimension) are heuristics, M analysis covers common connectors.
