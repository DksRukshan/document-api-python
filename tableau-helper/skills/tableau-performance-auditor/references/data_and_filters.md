# Data Sources, Extracts, Joins, Blending & Filter Performance

Sources: *Designing Efficient Workbooks* (InterWorks, 2021) & *Tableau Official Docs*

## 1. Data Connections & Hyper Extracts
- **Parallel Query Limits**: Tableau can execute up to **16 queries in parallel** on Hyper Extracts and major SQL databases, whereas **Excel, CSV/Text, and legacy file connectors execute single-threaded (1 query at a time)**! Moving Excel/CSV workbooks to **Hyper Extracts** delivers immediate multi-sheet speedup.
- **Keep Extracts Lean**:
  - **Hide All Unused Fields** before generating/refreshing an extract (dramatically trims column storage & extract refresh speed).
  - **Aggregate data for visible dimensions** when row-level granularity isn't displayed.
  - Apply **Extract Filters** (e.g. only last 2-3 years instead of 15 years) and **Data Source Filters** to reduce scan size.
  - Use **Incremental Refresh** where append-only dates/IDs exist.
  - Run **Compute Calculations Now** so row-level deterministic calculations are physically pre-materialized in `.hyper` (note: calcs referencing `NOW()`, `TODAY()`, Parameters, LODs, or Table Calcs cannot be materialized).
- **Published vs Embedded Data Sources (`perf_reduce_upload_times.md`)**:
  - Publishing `.twb` + Published Data Source separately accelerates workbook republishing, enables shared Hyper cache & scheduled refreshes, and avoids re-uploading huge `.twbx` packages.

## 2. Data Modeling: Relationships vs Joins vs Custom SQL vs Blending
- **Logical Relationships (Tableau 2020.2+ Object Model) over Legacy Physical Joins**:
  - Relationships query tables only when fields from those tables appear in a viz ("join culling" at logical table level) and preserve native table granularity without row duplication!
  - Configure **Performance Options -> Referential Integrity** (`All records match` / `Some records match`) only when guaranteed by data constraints, allowing Tableau to prune unnecessary joins.
- **Avoid Custom SQL (`<relation type='text'>`)**:
  - Tableau wraps Custom SQL inside an opaque `SELECT ... FROM (<your custom sql>)` subquery on every sheet query, disabling join culling, preventing partition pruning, and blocking metadata optimizations.
  - **Fix**: Replace Custom SQL with Database Views / Materialized Tables + Tableau Logical Relationships or Initial SQL.
- **Avoid Data Blending for High-Cardinality Links**:
  - Blending executes separate queries on primary and secondary sources and joins in-memory at the *linking field* granularity. High-cardinality linking dimensions (e.g. Customer ID, Order ID) cause heavy temp memory pressure and slow rendering.
  - **Fix**: Combine tables using Tableau Logical Relationships or warehouse/ETL joins.

## 3. Tableau Order of Operations & Filter Pipeline
1. Extract Filters
2. Data Source Filters
3. **Context Filters**
4. Sets / Conditional Filters / **Top N Filters** / **`FIXED` LOD**
5. **Dimension Filters**
6. `INCLUDE` / `EXCLUDE` LODs
7. Measure Filters
8. Table Calculation Filters (`LOOKUP(ATTR([Dim]),0)`)

### Filter Performance Rules:
- **Avoid `Only Relevant Values` on Quick Filters**: Forces cascading `SELECT DISTINCT` domain queries across the entire dashboard whenever *any* other filter changes.
  - **Fix**: Switch to `All Values in Database` or `All Values in Context`, or consolidate via Dashboard Action Filters.
- **Avoid High-Cardinality Multi-Value Lists / Dropdowns**: Rendering & querying 5,000+ checkbox items stalls browser layout and initial load.
  - **Fix**: Use **Custom Value List**, **Wildcard Match**, **Single Value Dropdown**, or hierarchical drill-down **Action Filters**.
- **Add the "Apply" Button on Multi-Select Filters**: Prevents firing 10 sequential queries as a user unchecks/checks multiple boxes one by one.
- **Use Continuous Date / Range Filters instead of `MY()` / `MDY()` Discrete Lists**: Discrete formatted date strings often defeat database/Hyper date indexes; **Relative Date** or **Continuous Range** filters compile cleanly to indexed `BETWEEN`.
- **Context Filter Discipline**: Use Context filters specifically when you must filter down >90% of rows or before **Top N / `FIXED` LOD** calculations (`Dimension Filter` alone runs *after* Top N and gives wrong results!). Avoid overusing Context filters for tiny reductions because Context creation builds temporary structures/queries.
- **Replace Excessive Quick Filters with Action Filters**:
  - Keep <= 3-5 Quick Filters visible on a dashboard.
  - Set Dashboard Filter Action clearing behavior to **"Exclude all values"** on heavy detail drill-down sheets so the detail sheet renders 0 marks until a user actually selects an item!
  - Ensure Action Filter source & target fields live in the view (`Detail` shelf if needed) to avoid slow cross-source/generated queries.
