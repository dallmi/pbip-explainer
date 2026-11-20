# What the extractor covers and what it does not

## Covered

| Area | TMDL / PBIR (current) | model.bim / report.json (legacy) |
|---|---|---|
| Tables, columns, measures, hierarchies, descriptions, display folders, format strings | ✔ | ✔ |
| Calculated columns and tables, calculation groups, field and what-if parameters | ✔ | ✔ |
| Relationships incl. cardinality, direction, inactive, USERELATIONSHIP use | ✔ | ✔ |
| RLS roles (filter expressions, member count only) | ✔ | ✔ |
| Power Query: connectors, navigation, native SQL, steps, joins, row filters, parameters, shared queries | ✔ | ✔ |
| Pages, visuals, fields per role, titles (static and dynamic), text boxes | ✔ | ✔ |
| Report / page / visual filters as readable conditions | ✔ | ✔ |
| Drill-through and tooltip pages, hidden pages, bookmarks (names) | ✔ | partly (page type flags are best effort) |
| Implicit measures, auto date/time fields | ✔ | ✔ |

## Not covered or approximate

- **No data.** PBIP stores metadata. The `.pbi/cache.abf` data cache is skipped on purpose.
- **DAX is not executed.** References are found by tokenising; dynamic constructs (strings built at runtime, `SELECTEDMEASURE` in calculation groups) are documented as text. Columns of virtual tables (`[@hc]` inside ADDCOLUMNS) show up as unresolved references; that is expected.
- **Table roles are heuristics** from relationship cardinalities, names and data categories. A table filtered through a bridge may show as dimension although it behaves like a fact.
- **Visual interactions** (edit interactions), conditional formatting rules beyond the fields they use, bookmark states, sync slicers, and visual calculations are not interpreted.
- **Power Query**: common connectors are recognised (SQL, Databricks, Snowflake, files, SharePoint, OData, web, Excel, Parquet, embedded tables …); unusual connectors appear as steps only. Credentials are never in PBIP.
- **Thin reports** (`byConnection`): the model is not in the package; only report-side fields are listed and cannot be validated.
- **Usage / unused** is relative to the packaged report(s). Several reports in one package share one usage index; pages with the same name in different reports are merged in page lineage.
- **Diagram layout** is automatic. Very large models (60+ tables) stay readable in the HTML via zoom and click-to-focus; the static SVG gets dense.
