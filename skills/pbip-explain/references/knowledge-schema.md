# Knowledge pack schema (schemaVersion 1.0)

`knowledge/model.json` is the single source of facts. The CSVs and `agent-brief.md` are views of it.

```
{
  schemaVersion, generator, generatedAt,
  source: { name, pbipFiles[{file, version, artifacts[]}], skippedFiles[] },
  semanticModels: [ Model ],
  reports: [ Report ]
}
```

## Model

| Field | Meaning |
|---|---|
| `name`, `format` | `tmdl` (definition/*.tmdl) or `bim` (model.bim) |
| `compatibilityLevel`, `culture`, `settings`, `annotations`, `description` | model-level properties |
| `tables[]` | see Table |
| `relationships[]` | `fromTable/fromColumn` (usually many side) → `toTable/toColumn` (usually one side), `fromCardinality`, `toCardinality`, `cardinality` ("*:1"), `crossFilteringBehavior` (`oneDirection`/`bothDirections`), `isActive`, `activatedByUserelationship`, `fromDataType`, `toDataType`, `id` |
| `roles[]` | RLS: `name`, `modelPermission`, `tablePermissions[{table, filterExpression, dependsOn}]`, `memberCount` (member names are never extracted) |
| `expressions[]` | Power Query parameters (`isParameter`, `parameter{value,type,required,allowedValues}`) and shared queries (`mAnalysis`) |
| `perspectives[]`, `cultures[]`, `dataSources[]`, `functions[]`, `daxQueries[]` | as found |
| `reports[]` | names of packaged reports that use this model |
| `analysis` | see Analysis |

### Table

`name, description, isHidden, dataCategory ("Time" = marked date table), role, storageMode, isCalculatedTable, isAutoDateTable, fieldParameter, relationshipCount{many,one}, columns[], measures[], hierarchies[{name, levels[{name,column}]}], partitions[], calculationGroup{precedence, items[{name, expression, ordinal, dependsOn}]}, source`

`role` (heuristic): `fact`, `dimension`, `date`, `bridge`, `measureTable`, `calculationGroup`, `fieldParameter`, `whatIfParameter`, `disconnected`, `autoDate`.

`source`: `type` (Power Query (M) / DAX calculated table / Entity), `mode`, `connectors[{function, kind, arguments, via?}]`, `navigation[{Name, Kind | Schema, Item ...}]`, `nativeQueries[]`, `steps[{name, function}]`, `joins[{left, leftKeys, right, rightKeys, kind}]`, `rowFilters[{step, condition}]`, `references[]` (other queries), `parameters[{name, value}]`, `daxExpression`.

### Column

`name, dataType, columnType (data | calculated | calculatedTableColumn), expression, sourceColumn, formatString, isHidden, isKey, summarizeBy, sortByColumn, dataCategory, displayFolder, description, dependsOn (calculated only), usedBy[], usedInReport, referenced`

### Measure

`name, table, expression, formatString, formatStringExpression, displayFolder, description, isHidden, autoDescription, complexity{lines, functionCalls, nesting, level, usesIterators, usesTimeIntelligence, usesFilterModifiers, usesCalculate}, dependsOn, lineage, usedBy[], usedInReport, impact`

`impact` (also on every column and report-level measure) = everything that changes if this object changes: `measures[]`, `reportMeasures[]`, `columns[]` (calculated columns and columns sorted by this one), `visuals{direct[], indirect[{page, visual, title, type, path[]}]}` (`path` runs from the object through the dependent measures to the visual), `filters[{where, path}]`, `pages[]`, `other[]` (relationships, RLS roles, calculation items, hierarchies, parameter tables), `counts{measures, visuals, direct, indirect, pages, filters, other}`, `usage` (direct | indirect | none). Lists are capped at 150 entries; `counts` are complete.

- `dependsOn` = direct references: `measures[]`, `columns[[table, column]]`, `tables[]`, `functions[]`, `userelationship[[[t,c],[t,c]]]`, `unresolved[]` (e.g. columns of virtual tables like `[@hc]`, or broken references), `variables[]`.
- `lineage` = transitive closure: every measure, column and table the measure ultimately reads.
- `usedBy[]` = `{by, kind, label, role?, report?, page?, visual?, visualTitle?, visualType?}`; `kind` ∈ measure, calculatedColumn, visual, visualFilter, pageFilter, reportFilter, drillthrough, rls, relationship, sortBy, hierarchy, calculationItem.

### Analysis

`stats{}`, `findings[{severity: error|warning|info, code, message, objects[], advice}]`, `unresolvedReportReferences[]`, `referenceStatus{verified, reportMeasure, unresolved}`, `unresolvedDaxReferences[]`, `unused{measures[], columns[], note}`, `dependencies{objectId: [objectIds]}`.

Object ids: `table:T`, `column:T[C]`, `measure:M`, `calcItem:T[I]`, `hierarchy:T[H]`, `role:R`, `expression:Q`, `relationship:<id>`, `page:P`, `visual:P/V`, `report:R`.

Finding codes: `unresolved-report-reference`, `relationship-missing-table`, `bidirectional-relationship`, `many-to-many`, `inactive-relationship-used`, `inactive-relationship-unused`, `relationship-type-mismatch`, `auto-date-time`, `date-table-not-marked`, `unresolved-dax-reference`, `implicit-measures`, `summarize-keys`, `missing-descriptions`, `unused-measures`, `many-calculated-columns`, `local-file-source`, `row-level-security`, `hidden-pages`, `custom-visuals`.

## Report

`name, format (pbir | legacy), folder, semanticModel, reportMeasures[{table, name, expression, dataType, formatString, dependsOn, impact}], observedModel{tables[{name, columns[], measures[], hierarchies[] each {name, pages, aggregations}}], counts, note} (only for live-connection reports), semanticModelRef{type: byPath|byConnection, ...}, theme{base, custom}, filters[], pages[], bookmarks[{name, displayName, page}], customVisuals[], settings, activePage`

Page: `name, displayName, ordinal, width, height, displayOption, hidden, pageType (Standard | Drillthrough | Tooltip), drillthroughFields[], filters[], visuals[], lineage{measures, columns, tables}`

Visual: `name, type, typeLabel, title, text (text boxes), position{x,y,z,width,height}, hidden, group, decorative, fields[], filters[], tables[]`

Field: `status (verified | reportMeasure | unresolved | observedExternal), kind (column | measure | hierarchyLevel), table, name, role (projection bucket such as Values, Category, Y, Rows, Series, Tooltips; or formatting, sort, drillthrough), aggregation + implicitMeasure (column aggregated in the visual), variation (auto date/time level), hierarchy, queryRef, displayName, id, exists`

Filter: `scope (report | page | visual), type, field, condition (readable, e.g. "Campaign[Channel] in (Email; Intranet)"), isActive, hidden, locked`

## CSVs

`tables.csv`, `columns.csv`, `measures.csv`, `relationships.csv`, `visuals.csv`, `sources.csv` – UTF-8 with BOM so Excel opens them directly.
