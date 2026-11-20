#!/usr/bin/env python3
"""Generate the synthetic example projects used by the tests.

campaign-analytics/  PBIP with TMDL model + PBIR report. Covers: Databricks / SQL / embedded sources,
                     parameters, shared query with a Power Query join, calculated table + column, date table,
                     hierarchy, inactive relationship + USERELATIONSHIP, bi-directional relationship,
                     calculation group, field parameter, RLS role, descriptions, display folders,
                     implicit measure, broken visual field, unresolved DAX reference, page/report filters,
                     drill-through page, hidden page, bookmark.
legacy-sales/        PBIP with model.bim (TMSL) + legacy report.json (stringified configs).

All names and values are invented. Run: python3 examples/generate_examples.py
"""
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.lstrip("\n"), encoding="utf-8")


def wj(path: Path, obj) -> None:
    w(path, json.dumps(obj, indent=2) + "\n")


T = "\t"


def tm(s: str) -> str:
    """Write TMDL with 4-space indentation in this file, convert to tabs."""
    out = []
    for line in s.strip("\n").split("\n"):
        stripped = line.lstrip(" ")
        n = (len(line) - len(stripped)) // 4
        rest = (len(line) - len(stripped)) % 4
        out.append(T * n + " " * rest + stripped)
    return "\n".join(out) + "\n"


# ============================================================================ campaign-analytics

def campaign():
    base = ROOT / "campaign-analytics"
    if base.exists():
        shutil.rmtree(base)
    sm = base / "CampaignAnalytics.SemanticModel"
    rp = base / "CampaignAnalytics.Report"
    wj(base / "CampaignAnalytics.pbip", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
        "version": "1.0", "artifacts": [{"report": {"path": "CampaignAnalytics.Report"}}],
        "settings": {"enableAutoRecovery": True}})
    wj(sm / "definition.pbism", {"version": "4.0", "settings": {}})
    d = sm / "definition"
    w(d / "database.tmdl", tm("""
database
    compatibilityLevel: 1600
"""))
    w(d / "model.tmdl", tm("""
/// Engagement analytics for internal communication campaigns.
model Model
    culture: en-US
    defaultPowerBIDataSourceVersion: powerBI_V3
    discourageImplicitMeasures
    sourceQueryCulture: en-US

annotation PBI_ProTooling = ["DevMode"]

ref table Engagement
ref table Campaign
ref table Division
ref table Date
ref table _Measures
ref table 'Time Intelligence'
ref table 'Metric Selector'
"""))
    w(d / "expressions.tmdl", tm("""
expression Host = "adb-0000000000000000.0.example.net" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]
    lineageTag: 0b1f0000-0000-0000-0000-000000000001
    queryGroup: Parameters

expression HttpPath = "/sql/1.0/warehouses/example" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]
    lineageTag: 0b1f0000-0000-0000-0000-000000000002
    queryGroup: Parameters

/// Maps division keys to the reporting region.
expression 'Region Map' =
        let
            Source = Table.FromRows({{"GWM", "Global"}, {"IB", "Americas"}, {"PCB", "Switzerland"}}, {"Division Key", "Region"})
        in
            Source
    lineageTag: 0b1f0000-0000-0000-0000-000000000003
"""))
    w(d / "relationships.tmdl", tm("""
relationship a1000000-0000-0000-0000-000000000001
    fromColumn: Engagement.Date
    toColumn: Date.Date

relationship a1000000-0000-0000-0000-000000000002
    isActive: false
    fromColumn: Engagement.'Send Date'
    toColumn: Date.Date

relationship a1000000-0000-0000-0000-000000000003
    fromColumn: Engagement.'Campaign Key'
    toColumn: Campaign.'Campaign Key'

relationship a1000000-0000-0000-0000-000000000004
    crossFilteringBehavior: bothDirections
    fromColumn: Engagement.'Division Key'
    toColumn: Division.'Division Key'
"""))
    tables = d / "tables"
    w(tables / "Engagement.tmdl", tm('''
/// One row per campaign, division and day. Grain: Date x Campaign x Division x Visitor.
table Engagement
    lineageTag: e0000000-0000-0000-0000-000000000001

    column Date
        dataType: dateTime
        formatString: Short Date
        lineageTag: e0000000-0000-0000-0000-000000000002
        summarizeBy: none
        sourceColumn: event_date

    column 'Send Date'
        dataType: dateTime
        isHidden
        formatString: Short Date
        lineageTag: e0000000-0000-0000-0000-000000000003
        summarizeBy: none
        sourceColumn: send_date

    column 'Campaign Key'
        dataType: int64
        isHidden
        formatString: 0
        lineageTag: e0000000-0000-0000-0000-000000000004
        summarizeBy: none
        sourceColumn: campaign_key

    column 'Division Key'
        dataType: string
        isHidden
        lineageTag: e0000000-0000-0000-0000-000000000005
        summarizeBy: none
        sourceColumn: division_key

    column Opens
        dataType: int64
        formatString: #,0
        lineageTag: e0000000-0000-0000-0000-000000000006
        summarizeBy: sum
        sourceColumn: opens

    column Clicks
        dataType: int64
        formatString: #,0
        lineageTag: e0000000-0000-0000-0000-000000000007
        summarizeBy: sum
        sourceColumn: clicks

    column Recipients
        dataType: int64
        formatString: #,0
        lineageTag: e0000000-0000-0000-0000-000000000008
        summarizeBy: sum
        sourceColumn: recipients

    /// Pseudonymous visitor identifier from the tracking pipeline.
    column 'Visitor ID'
        dataType: string
        isHidden
        lineageTag: e0000000-0000-0000-0000-000000000009
        summarizeBy: none
        sourceColumn: visitor_id

    column 'Legacy Flag'
        dataType: string
        lineageTag: e0000000-0000-0000-0000-000000000010
        summarizeBy: none
        sourceColumn: legacy_flag

    column 'Click Rate Row' = DIVIDE ( Engagement[Clicks], Engagement[Recipients] )
        dataType: double
        formatString: 0.0%
        lineageTag: e0000000-0000-0000-0000-000000000011
        summarizeBy: none

    partition Engagement = m
        mode: import
        source =
                let
                    Source = Databricks.Catalogs(Host, HttpPath, [Catalog = null, Database = null, EnableAutomaticProxyDiscovery = null]),
                    comms_catalog = Source{[Name="comms",Kind="Database"]}[Data],
                    gold_schema = comms_catalog{[Name="gold",Kind="Schema"]}[Data],
                    fact = gold_schema{[Name="fact_engagement",Kind="Table"]}[Data],
                    #"Only valid" = Table.SelectRows(fact, each [is_valid] = true and [event_date] >= #date(2024, 1, 1))
                in
                    #"Only valid"

    annotation PBI_ResultType = Table
'''))
    w(tables / "Campaign.tmdl", tm('''
/// Communication campaigns as planned in the campaign tool.
table Campaign
    lineageTag: c0000000-0000-0000-0000-000000000001

    column 'Campaign Key'
        dataType: int64
        isHidden
        isKey
        formatString: 0
        lineageTag: c0000000-0000-0000-0000-000000000002
        summarizeBy: none
        sourceColumn: campaign_key

    column 'Campaign Name'
        dataType: string
        lineageTag: c0000000-0000-0000-0000-000000000003
        summarizeBy: none
        sourceColumn: campaign_name

    /// Distribution channel: Email, Intranet or App.
    column Channel
        dataType: string
        lineageTag: c0000000-0000-0000-0000-000000000004
        summarizeBy: none
        sourceColumn: channel
        sortByColumn: 'Channel Sort'

    column 'Channel Sort'
        dataType: int64
        isHidden
        formatString: 0
        lineageTag: c0000000-0000-0000-0000-000000000005
        summarizeBy: sum
        sourceColumn: channel_sort

    column 'Launch Year'
        dataType: int64
        formatString: 0
        lineageTag: c0000000-0000-0000-0000-000000000006
        summarizeBy: sum
        sourceColumn: launch_year

    partition Campaign = m
        mode: import
        source =
                let
                    Source = Sql.Database("sql-campaigns.example.internal", "campaigns"),
                    Query = Value.NativeQuery(Source, "SELECT campaign_key, campaign_name, channel, channel_sort, launch_year FROM dbo.campaign WHERE is_test = 0")
                in
                    Query
'''))
    w(tables / "Division.tmdl", tm('''
table Division
    lineageTag: d0000000-0000-0000-0000-000000000001

    column 'Division Key'
        dataType: string
        isHidden
        lineageTag: d0000000-0000-0000-0000-000000000002
        summarizeBy: none
        sourceColumn: Division Key

    column Division
        dataType: string
        lineageTag: d0000000-0000-0000-0000-000000000003
        summarizeBy: none
        sourceColumn: Division

    column Region
        dataType: string
        lineageTag: d0000000-0000-0000-0000-000000000004
        summarizeBy: none
        sourceColumn: Region

    partition Division = m
        mode: import
        source =
                let
                    Source = Table.FromRows(Json.Document(Binary.Decompress(Binary.FromText("i45WMlTSUTIyMDLQNzDRNzBQMDQyNjRSitUBAA==", BinaryEncoding.Base64), Compression.Deflate)), let _t = ((type nullable text) meta [Serialized.Text = true]) in type table [#"Division Key" = _t, Division = _t]),
                    Joined = Table.NestedJoin(Source, {"Division Key"}, #"Region Map", {"Division Key"}, "Map", JoinKind.LeftOuter),
                    Expanded = Table.ExpandTableColumn(Joined, "Map", {"Region"}, {"Region"})
                in
                    Expanded
'''))
    w(tables / "Date.tmdl", tm('''
/// Calendar from 2024 to 2026, marked as date table.
table Date
    dataCategory: Time
    lineageTag: da000000-0000-0000-0000-000000000001

    column Date
        dataType: dateTime
        isKey
        formatString: Short Date
        lineageTag: da000000-0000-0000-0000-000000000002
        summarizeBy: none
        isNameInferred
        sourceColumn: [Date]

    column Year = YEAR ( 'Date'[Date] )
        dataType: int64
        formatString: 0
        lineageTag: da000000-0000-0000-0000-000000000003
        summarizeBy: none

    column Month = FORMAT ( 'Date'[Date], "MMM" )
        dataType: string
        lineageTag: da000000-0000-0000-0000-000000000004
        summarizeBy: none
        sortByColumn: 'Month Number'

    column 'Month Number' = MONTH ( 'Date'[Date] )
        dataType: int64
        isHidden
        formatString: 0
        lineageTag: da000000-0000-0000-0000-000000000005
        summarizeBy: none

    hierarchy 'Calendar'
        lineageTag: da000000-0000-0000-0000-000000000006

        level Year
            lineageTag: da000000-0000-0000-0000-000000000007
            column: Year

        level Month
            lineageTag: da000000-0000-0000-0000-000000000008
            column: Month

    partition Date = calculated
        mode: import
        source = CALENDAR ( DATE ( 2024, 1, 1 ), DATE ( 2026, 12, 31 ) )
'''))
    w(tables / "_Measures.tmdl", tm('''
table _Measures
    lineageTag: f0000000-0000-0000-0000-000000000001

    /// Number of times a campaign message was opened.
    measure 'Total Opens' = SUM ( Engagement[Opens] )
        formatString: #,0
        displayFolder: Volume
        lineageTag: f0000000-0000-0000-0000-000000000002

    measure 'Total Clicks' = SUM ( Engagement[Clicks] )
        formatString: #,0
        displayFolder: Volume
        lineageTag: f0000000-0000-0000-0000-000000000003

    measure Recipients = SUM ( Engagement[Recipients] )
        formatString: #,0
        displayFolder: Volume
        lineageTag: f0000000-0000-0000-0000-000000000004

    /// Share of recipients who opened the message.
    measure 'Open Rate' = DIVIDE ( [Total Opens], [Recipients] )
        formatString: 0.0%
        displayFolder: Rates
        lineageTag: f0000000-0000-0000-0000-000000000005

    measure 'Click-to-Open Rate' =
            // clicks relative to opens, not to recipients
            VAR _opens = [Total Opens]
            RETURN
                DIVIDE ( [Total Clicks], _opens )
        formatString: 0.0%
        displayFolder: Rates
        lineageTag: f0000000-0000-0000-0000-000000000006

    measure 'Unique Visitors' = DISTINCTCOUNT ( Engagement[Visitor ID] )
        formatString: #,0
        displayFolder: Reach
        lineageTag: f0000000-0000-0000-0000-000000000007

    measure 'Clicks by Send Date' = CALCULATE ( [Total Clicks], USERELATIONSHIP ( Engagement[Send Date], 'Date'[Date] ) )
        formatString: #,0
        displayFolder: Volume
        lineageTag: f0000000-0000-0000-0000-000000000008

    measure 'Clicks YTD' = TOTALYTD ( [Total Clicks], 'Date'[Date] )
        formatString: #,0
        displayFolder: Time
        lineageTag: f0000000-0000-0000-0000-000000000009

    measure 'Old Bounce Rate' = DIVIDE ( SUM ( Engagement[Bounces] ), [Recipients] )
        formatString: 0.0%
        lineageTag: f0000000-0000-0000-0000-000000000010

    measure 'Selected Metric Value' =
            SWITCH (
                SELECTEDVALUE ( 'Metric Selector'[Metric Order] ),
                0, [Total Opens],
                1, [Total Clicks],
                [Unique Visitors]
            )
        formatString: #,0
        lineageTag: f0000000-0000-0000-0000-000000000011

    column Value
        dataType: string
        isHidden
        lineageTag: f0000000-0000-0000-0000-000000000012
        summarizeBy: none
        isNameInferred
        sourceColumn: [Value]

    partition _Measures = calculated
        mode: import
        source = ROW ( "Value", BLANK () )
'''))
    w(tables / "Time Intelligence.tmdl", tm('''
table 'Time Intelligence'
    lineageTag: 71000000-0000-0000-0000-000000000001

    calculationGroup
        precedence: 1

        calculationItem Current = SELECTEDMEASURE ()

        calculationItem YTD = CALCULATE ( SELECTEDMEASURE (), DATESYTD ( 'Date'[Date] ) )

        calculationItem 'Prior Year' = CALCULATE ( SELECTEDMEASURE (), SAMEPERIODLASTYEAR ( 'Date'[Date] ) )

    column 'Time Calculation'
        dataType: string
        lineageTag: 71000000-0000-0000-0000-000000000002
        summarizeBy: none
        sourceColumn: Name
        sortByColumn: Ordinal

    column Ordinal
        dataType: int64
        isHidden
        formatString: 0
        lineageTag: 71000000-0000-0000-0000-000000000003
        summarizeBy: sum
        sourceColumn: Ordinal

    partition 'Time Intelligence' = calculationGroup
        mode: import
'''))
    w(tables / "Metric Selector.tmdl", tm('''
table 'Metric Selector'
    lineageTag: 3e000000-0000-0000-0000-000000000001

    column Metric
        dataType: string
        lineageTag: 3e000000-0000-0000-0000-000000000002
        summarizeBy: none
        sourceColumn: [Value1]
        sortByColumn: 'Metric Order'

        extendedProperty ParameterMetadata =
                {
                  "version": 3,
                  "kind": 2
                }

    column 'Metric Fields'
        dataType: string
        isHidden
        lineageTag: 3e000000-0000-0000-0000-000000000003
        summarizeBy: none
        sourceColumn: [Value2]

    column 'Metric Order'
        dataType: int64
        isHidden
        formatString: 0
        lineageTag: 3e000000-0000-0000-0000-000000000004
        summarizeBy: sum
        sourceColumn: [Value3]

    partition 'Metric Selector' = calculated
        mode: import
        source =
                {
                    ("Opens", NAMEOF('_Measures'[Total Opens]), 0),
                    ("Clicks", NAMEOF('_Measures'[Total Clicks]), 1),
                    ("Unique Visitors", NAMEOF('_Measures'[Unique Visitors]), 2)
                }
'''))
    w(d / "roles" / "Division Lead.tmdl", tm('''
/// Division leads see only their own division.
role 'Division Lead'
    modelPermission: read

    tablePermission Division = Division[Division] = "Investment Bank"
'''))

    # ------------------------------------------------------------------ report
    wj(rp / "definition.pbir", {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/1.0.0/schema.json",
        "version": "4.0", "datasetReference": {"byPath": {"path": "../CampaignAnalytics.SemanticModel"}}})
    rd = rp / "definition"
    wj(rd / "version.json", {"version": "2.0.0"})

    def col(t, c):
        return {"Column": {"Expression": {"SourceRef": {"Entity": t}}, "Property": c}}

    def mea(t, m):
        return {"Measure": {"Expression": {"SourceRef": {"Entity": t}}, "Property": m}}

    def lit(s):
        return {"expr": {"Literal": {"Value": f"'{s}'"}}}

    def title(s):
        return {"title": [{"properties": {"show": {"expr": {"Literal": {"Value": "true"}}}, "text": lit(s)}}]}

    def in_filter(name, t, c, values):
        return {"name": name, "field": col(t, c), "type": "Categorical", "filter": {
            "Version": 2, "From": [{"Name": "x", "Entity": t, "Type": 0}],
            "Where": [{"Condition": {"In": {
                "Expressions": [{"Column": {"Expression": {"SourceRef": {"Source": "x"}}, "Property": c}}],
                "Values": [[{"Literal": {"Value": f"'{v}'"}}] for v in values]}}}]}}

    wj(rd / "report.json", {
        "themeCollection": {"baseTheme": {"name": "CY24SU10", "type": "SharedResources"}},
        "filterConfig": {"filters": [in_filter("rf1", "Campaign", "Channel", ["Email", "Intranet", "App"])]},
        "settings": {"useStylableVisualContainerHeader": True}})
    wj(rd / "pages" / "pages.json", {"pageOrder": ["overview", "campaignDetail", "qa"], "activePageName": "overview"})

    def visual(page, name, vtype, x, y, wd, ht, query=None, objs=None, vco=None, filters=None):
        v = {"name": name, "position": {"x": x, "y": y, "z": 0, "width": wd, "height": ht},
             "visual": {"visualType": vtype}}
        if query:
            v["visual"]["query"] = {"queryState": {
                role: {"projections": [{"field": f, "queryRef": "q"} for f in fields]} for role, fields in query.items()}}
        if objs:
            v["visual"]["objects"] = objs
        if vco:
            v["visual"]["visualContainerObjects"] = vco
        if filters:
            v["filterConfig"] = {"filters": filters}
        wj(rd / "pages" / page / "visuals" / name / "visual.json", v)

    wj(rd / "pages" / "overview" / "page.json", {
        "name": "overview", "displayName": "Overview", "displayOption": "FitToPage", "width": 1280, "height": 720,
        "filterConfig": {"filters": [in_filter("pf1", "Campaign", "Channel", ["Email", "Intranet"])]}})
    visual("overview", "title", "textbox", 20, 10, 600, 50, objs={"general": [{"properties": {"paragraphs": [
        {"textRuns": [{"value": "Campaign engagement overview"}]},
        {"textRuns": [{"value": "Opens and clicks for email and intranet campaigns."}]}]}}]})
    visual("overview", "kpiOpenRate", "card", 20, 80, 200, 120, query={"Values": [mea("_Measures", "Open Rate")]},
           vco=title("Open rate"))
    visual("overview", "kpiUV", "card", 240, 80, 200, 120, query={"Values": [mea("_Measures", "Unique Visitors")]},
           vco=title("Unique visitors"))
    visual("overview", "trend", "lineChart", 20, 220, 800, 300,
           query={"Category": [{"HierarchyLevel": {"Expression": {"Hierarchy": {"Expression": {"SourceRef": {"Entity": "Date"}},
                                                                                  "Hierarchy": "Calendar"}}, "Level": "Month"}}],
                  "Y": [mea("_Measures", "Total Opens"), mea("_Measures", "Clicks by Send Date")]},
           vco=title("Opens over time"))
    visual("overview", "byChannel", "clusteredBarChart", 840, 220, 420, 300,
           query={"Category": [col("Campaign", "Channel")],
                  "Y": [{"Aggregation": {"Expression": col("Engagement", "Clicks"), "Function": 0}}]},
           vco=title("Clicks by channel"))
    visual("overview", "slicerDivision", "slicer", 840, 80, 420, 120, query={"Values": [col("Division", "Division")]},
           vco=title("Division"))
    visual("overview", "metricSwitch", "lineChart", 20, 540, 800, 160,
           query={"Category": [col("Date", "Date")], "Y": [mea("_Measures", "Selected Metric Value")],
                  "Series": [col("Time Intelligence", "Time Calculation")]},
           vco=title("Selected metric"))
    visual("overview", "logo", "image", 1180, 10, 80, 50)

    wj(rd / "pages" / "campaignDetail" / "page.json", {
        "name": "campaignDetail", "displayName": "Campaign Detail", "displayOption": "FitToPage",
        "width": 1280, "height": 720,
        "pageBinding": {"name": "pb1", "type": "Drillthrough", "parameters": [
            {"name": "p1", "boundFilter": "f1", "fieldExpr": col("Campaign", "Campaign Name")}]}})
    visual("campaignDetail", "detailTable", "tableEx", 20, 20, 1240, 600,
           query={"Values": [col("Campaign", "Campaign Name"), col("Campaign", "Campaign Owner"),
                             mea("_Measures", "Total Opens"), mea("_Measures", "Click-to-Open Rate"),
                             mea("_Measures", "Clicks YTD")]},
           vco=title("Campaigns"),
           filters=[{"name": "vf1", "field": mea("_Measures", "Total Opens"), "type": "Advanced", "filter": {
               "Version": 2, "From": [{"Name": "m", "Entity": "_Measures", "Type": 0}],
               "Where": [{"Condition": {"Comparison": {"ComparisonKind": 1, "Left": {"Measure": {
                   "Expression": {"SourceRef": {"Source": "m"}}, "Property": "Total Opens"}},
                   "Right": {"Literal": {"Value": "100L"}}}}}]}}])

    wj(rd / "pages" / "qa" / "page.json", {"name": "qa", "displayName": "QA", "displayOption": "FitToPage",
                                           "width": 1280, "height": 720, "visibility": "HiddenInViewMode"})
    visual("qa", "qaTable", "tableEx", 20, 20, 600, 400,
           query={"Values": [col("Engagement", "Date"), mea("_Measures", "Recipients")]})
    wj(rd / "bookmarks" / "bm1.bookmark.json", {"name": "bm1", "displayName": "Email only",
                                               "explorationState": {"activeSection": "overview"}})


# ============================================================================ legacy-sales

def legacy():
    base = ROOT / "legacy-sales"
    if base.exists():
        shutil.rmtree(base)
    wj(base / "LegacySales.pbip", {"version": "1.0", "artifacts": [{"report": {"path": "LegacySales.Report"}}]})
    sm = base / "LegacySales.SemanticModel"
    wj(sm / "definition.pbism", {"version": "1.0"})
    wj(sm / "model.bim", {
        "compatibilityLevel": 1550, "model": {
            "culture": "en-US", "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "tables": [
                {"name": "Sales", "description": "Order lines.",
                 "columns": [
                     {"name": "OrderDate", "dataType": "dateTime", "sourceColumn": "OrderDate"},
                     {"name": "ProductID", "dataType": "int64", "sourceColumn": "ProductID", "summarizeBy": "sum"},
                     {"name": "Amount", "dataType": "decimal", "sourceColumn": "Amount", "formatString": "#,0.00"},
                     {"name": "Margin", "dataType": "decimal", "type": "calculated",
                      "expression": ["Sales[Amount] * 0.3"]}],
                 "measures": [
                     {"name": "Revenue", "expression": "SUM(Sales[Amount])", "formatString": "#,0"},
                     {"name": "Margin %", "expression": ["DIVIDE(", "    SUM(Sales[Margin]),", "    [Revenue]", ")"],
                      "description": "Margin as share of revenue."},
                     {"name": "Unused Measure", "expression": "COUNTROWS(Sales)"}],
                 "partitions": [{"name": "Sales", "mode": "import", "source": {"type": "m", "expression": [
                     "let", "    Source = Csv.Document(File.Contents(\"C:\\data\\sales.csv\"),[Delimiter=\",\"]),",
                     "    Promoted = Table.PromoteHeaders(Source)", "in", "    Promoted"]}}]},
                {"name": "Product",
                 "columns": [
                     {"name": "ProductID", "dataType": "int64", "sourceColumn": "ProductID"},
                     {"name": "Category", "dataType": "string", "sourceColumn": "Category"}],
                 "partitions": [{"name": "Product", "mode": "import", "source": {"type": "m", "expression":
                     "let\n    Source = Excel.Workbook(File.Contents(\"C:\\data\\products.xlsx\"), null, true),\n"
                     "    Sheet = Source{[Item=\"Products\",Kind=\"Sheet\"]}[Data]\nin\n    Sheet"}}]},
                {"name": "LocalDateTable_1234", "isHidden": True, "showAsVariationsOnly": True,
                 "columns": [{"name": "Date", "dataType": "dateTime", "sourceColumn": "Date"}],
                 "partitions": [{"name": "p", "source": {"type": "calculated",
                                                          "expression": "Calendar(Date(2020,1,1), Date(2020,12,31))"}}]}],
            "relationships": [
                {"name": "r1", "fromTable": "Sales", "fromColumn": "ProductID", "toTable": "Product", "toColumn": "ProductID"},
                {"name": "r2", "fromTable": "Sales", "fromColumn": "OrderDate", "toTable": "LocalDateTable_1234",
                 "toColumn": "Date", "joinOnDateBehavior": "datePartOnly"}]}})
    rp = base / "LegacySales.Report"
    wj(rp / "definition.pbir", {"version": "1.0", "datasetReference": {"byPath": {"path": "../LegacySales.SemanticModel"}}})

    def vc(name, vtype, x, y, projections, select, frm):
        cfg = {"name": name, "singleVisual": {
            "visualType": vtype, "projections": projections,
            "prototypeQuery": {"Version": 2, "From": frm, "Select": select},
            "vcObjects": {"title": [{"properties": {"text": {"expr": {"Literal": {"Value": f"'{name.title()}'"}}}}}]}}}
        return {"x": x, "y": y, "z": 0, "width": 400, "height": 300, "config": json.dumps(cfg), "filters": "[]"}

    frm = [{"Name": "s", "Entity": "Sales", "Type": 0}, {"Name": "p", "Entity": "Product", "Type": 0}]
    sel = [
        {"Column": {"Expression": {"SourceRef": {"Source": "p"}}, "Property": "Category"}, "Name": "Product.Category"},
        {"Measure": {"Expression": {"SourceRef": {"Source": "s"}}, "Property": "Revenue"}, "Name": "Sales.Revenue"},
        {"Measure": {"Expression": {"SourceRef": {"Source": "s"}}, "Property": "Margin %"}, "Name": "Sales.Margin %"},
    ]
    wj(rp / "report.json", {
        "config": json.dumps({"themeCollection": {"baseTheme": {"name": "CY23SU04"}}}),
        "filters": "[]",
        "sections": [{
            "name": "ReportSection1", "displayName": "Sales Overview", "ordinal": 0, "width": 1280, "height": 720,
            "config": "{}", "filters": "[]",
            "visualContainers": [
                vc("revenueByCategory", "clusteredColumnChart", 20, 20,
                   {"Category": [{"queryRef": "Product.Category"}], "Y": [{"queryRef": "Sales.Revenue"}]}, sel[:2], frm),
                vc("marginCard", "card", 440, 20, {"Values": [{"queryRef": "Sales.Margin %"}]}, [sel[2]], frm[:1]),
            ]}]})


if __name__ == "__main__":
    campaign()
    legacy()
    print("examples written to", ROOT)
