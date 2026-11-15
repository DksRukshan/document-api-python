# Worksheets, Dashboards, Rendering & Server Publishing Best Practices

Sources: *Designing Efficient Workbooks* (InterWorks, 2021) & *Tableau Official Docs (`llms-full.txt`)*

## 1. Worksheet Efficiency & Mark Types
- **Mark Count Budgets**: Each mark adds query sorting, layout, and rendering cost. Keep typical analytical views **under 1,000 marks** (both for speed and client-side rendering). Avoid multi-thousand-row granular tables unless downloaded or explicitly gated behind an Action Filter (`Exclude all values`).
- **Replace Heavy Crosstabs / Text Tables**: Large text tables with many discrete headers + `Measure Names`/`Measure Values` trigger expensive `Computing layouts` overhead on CPU/server. Split wide tables into summary bars/KPIs + an on-demand drill-down table.
- **Use Density Marks / Hexbins for Dense Scatter & Spatial Views**: Instead of plotting 20,000 individual circles/marks, enable **Density** marks or spatial hexbin aggregation.
- **Minimize Polygon Marks & Custom Geocoding**: High-vertex polygons (`Polygon` mark type) force heavy spatial transfer and often push views into **Server-Side Rendering**. Use built-in filled map roles or simplified GeoJSON/spatial files when possible.
- **Avoid the Pages Shelf for Complex Views**: The **Pages Shelf** loads data and geometry for *every page* across the entire domain upfront, magnifying memory and load times. Use a clean single-select Filter or Parameter instead.
- **Limit Dual-Axis / Multi-Pane Views Only to True Needs**: Every additional pane/axis adds rendering & layout passes.

## 2. Dashboard Architecture & Layout Computation
- **Always Prefer Fixed Dashboard Size (or Bounded Range) over `Automatic`**:
  - `Automatic` resizing invalidates Tableau Server/Cloud pre-rendered **Image/View Caches** across different browser viewports and recomputes geometry on every session.
- **Dashboard Scope Budget**:
  - Ideal: **2-3 primary views** (max ~4-5 views) and **<= 3-5 visible filters** per dashboard, with the primary focal KPI/chart anchored in the **top-left** (F-pattern scanning).
  - Never pack 8-15 sheets onto one monolithic executive dashboard; split into focused thematic tabs linked via **Navigation Buttons** and **Filter Actions**.
- **Lazy Load Secondary Content with Show/Hide Containers**:
  - Worksheets inside collapsed **Show/Hide Containers** (e.g. help overlays, advanced filter drawers, detail tables) do not compute layouts visibly on initial paint and reduce visual clutter.
- **Flatten Nested Layout Containers**:
  - Stacking 4-8+ nested Horizontal/Vertical containers causes exponential layout solver complexity (`Computing layouts` bottlenecks). Flatten container hierarchy and remove empty `Blank` spacers by using **Outer/Inner Padding**.
- **Remove Unused Device Layouts**:
  - Tableau auto-generates Phone/Tablet layouts or retains legacy device layouts that multiply layout XML and rendering checks unless intentionally designed.
- **Optimize `.twbx` Assets & Images**:
  - Compress dashboard logos/icons under **50 KB**. For invisible padding/click targets, use a tiny **32x32 transparent PNG** (<1 KB), never high-res uncompressed PNGs/BMPs. Avoid bundling unused raw CSV/Excel files alongside extracts inside `.twbx`.

## 3. Publishing, Server & Performance Recording Diagnostics
- **Turn Off `Show Sheets as Tabs` unless required**: Showing tabs causes Tableau Server/Cloud to maintain broader session state and load/validate multiple sheet contexts cleanly at startup; prefer Dashboard **Navigation Buttons**.
- **Publish Workbooks and Data Sources Separately**: Publishing `.twb` connected to a **Published Data Source** cuts `.twbx` upload time and centralizes scheduled extract refreshes.
- **Reading a Tableau Performance Recording Workbook**:
  - Focus on the longest bars in **Events** / **Timeline**:
    - `Executing query` / `executeQueries`: Slow SQL/live DB or unoptimized filters -> switch to Hyper extract, add Context filter, prune joins/Custom SQL, replace `Only Relevant Values`.
    - `Compiling query`: Bloated nested/repeated calculations, complex `IF`/String calcs, high-cardinality LODs, or too many quick filters -> simplify calcs, push to extract (`Compute Calculations Now`) or DB, use `CASE`/`IN`.
    - `Computing layouts`: Too many sheets, dense text tables/crosstabs, deep nested containers, or `Automatic` dashboard sizing -> reduce sheet count, split crosstabs, switch to Fixed Size, flatten containers.
    - `Blending data`: High-cardinality secondary links -> replace with Logical Relationships or warehouse joins.
    - `Geocoding`: Unindexed/huge custom geocoding or excess map marks -> filter data, reduce spatial detail.
    - `Server rendering`: Mark count exceeds client-side rendering threshold (~1,000 marks) or complex polygons/density -> aggregate marks, use Density marks, simplify polygons.
