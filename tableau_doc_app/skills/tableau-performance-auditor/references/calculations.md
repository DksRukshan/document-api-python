# Tableau Calculation Performance & Best Practices

Sources: *Designing Efficient Workbooks* (InterWorks, 2021) & *Tableau Desktop Official Documentation*

## 1. Logic & Conditional Ranking
- **Hierarchy (Fastest -> Slowest for conditional grouping)**:
  1. **Native / Hyper Materialized Booleans, Integers, or Database Columns** (fastest overall).
  2. **`CASE` statements** for exact equality checks against a single dimension/parameter — easier for VizQL and databases to optimize into clean SQL `CASE WHEN`.
  3. **Native Groups** (often cleaner and faster than long `IF/ELSEIF` chains, though large manual groups can still weigh down metadata).
  4. **Sets**: Note nuance — InterWorks benchmarks rank Sets fast for grouping, *however* official Tableau guidance advises **avoiding Sets inside calculated fields** and avoiding Set abuse for simple grouping; prefer Sets directly on Filters/Marks or rewrite in SQL/`CASE`.
  5. **`IF ... ELSEIF ... ELSE ... END`** — most flexible, slowest when wide or deeply branched.
- **Never write `ELSE IF` (two keywords)**: `ELSE IF` creates nested `IF ... END` blocks that hurt readability and query compilation; always flatten to **`ELSEIF`**.
- **Put the most frequent condition first**: Both `IF/ELSEIF` and `CASE` short-circuit. Ordering by descending frequency avoids unnecessary branch evaluations across millions of rows.
- **Replace chained `OR` (`>= 3` values) with `IN`**: Introduced in Tableau 2020.3, `[Region] IN ('East', 'West', 'Central')` compiles cleaner and runs faster than repeating `[Region] = 'East' OR [Region] = 'West' OR ...`.
- **Replace multi-branch single-field `IF` equality checks with `CASE`**:
  - Bad: `IF [Tier] = "Gold" THEN 1 ELSEIF [Tier] = "Silver" THEN 2 ELSE 3 END`
  - Better: `CASE [Tier] WHEN "Gold" THEN 1 WHEN "Silver" THEN 2 ELSE 3 END`

## 2. Aggregations, Nulls, Booleans & Dates
- **Prefer Booleans and Integers over Strings**: Store/return `TRUE/FALSE` or `1/0` rather than `"Yes"/"No"` or `"High"/"Low"`, and configure **Aliases** for display. Integer/Boolean comparisons are orders of magnitude faster in Hyper and SQL engines.
- **Integer Parameters + `Display As`**: Instead of string parameters (`'Sales'`, `'Profit'`, `'Quantity'`), use integer values (`1`, `2`, `3`) with friendly `Display As` text, and switch metrics with `CASE [Metric Param]`.
- **Prefer `MIN()` or `MAX()` over `ATTR()` and `AVG()`**: `ATTR(x)` executes `IF MIN(x) = MAX(x) THEN MIN(x) ELSE "*" END`, doubling aggregation cost and disabling certain database pushdowns. Never use `ATTR()` purely inside an `IF` guard unless you truly need the multi-value `"*"` test.
- **Minimize `COUNTD()` (Count Distinct)**: `COUNTD` prevents pre-aggregation and forces full-granularity scans. Consider whether `COUNT()`, `SUM([Number of Records / Flag])`, `SIZE()`, `FIRST()=0` + `WINDOW_SUM()`, or DB-side distinct flags achieve the goal.
- **Aggregate *before* testing / multiplying inside calcs**:
  - Bad: `SUM(IF [Param] = 1 THEN [Sales] ELSE [Profit] END)` evaluates row-by-row.
  - Better: `CASE [Param] WHEN 1 THEN SUM([Sales]) ELSE SUM([Profit]) END` evaluates the parameter once per mark instead of once per row!
  - Similarly for ratios: write `SUM([Profit]) / SUM([Sales])`, not row-level division wrapped later.
- **Prefer `TODAY()` over `NOW()`, or precompute refresh timestamps**: `NOW()` includes seconds, busting Tableau Server / Data Engine query caches on nearly every load! Even `TODAY()` can defeat extract materialization (`Compute Calculations Now`). If sub-day precision isn't required, use `TODAY()`, a data-source `Max Extract Date` column (`{MAX([Order Date])}` only once in extract or materialized), or a parameter.
- **Avoid redundant `IF ... THEN TRUE ELSE FALSE END`**: A boolean expression (`[Sales] > 100`) already returns a boolean without an `IF` wrapper!

## 3. String Manipulation & RegEx
- **Best option**: Materialize parsed flags, trimmed strings, and category buckets in the database, ETL, or Hyper Extract (`Compute Calculations Now`).
- **When parsed in Tableau (`llms-full.txt`)**: Prefer **`REGEXP_MATCH`**, **`REGEXP_REPLACE`**, or **`REGEXP_EXTRACT`** over chaining multiple `CONTAINS()`, `STARTSWITH()`, `ENDSWITH()`, `FIND()`, `LEFT()`, and `MID()` calls in the same formula.
- **Simplify string slicing**: e.g. `MID([Phone], 2)` beats `RIGHT([Phone], LEN([Phone]) - 1)` because it avoids an extra `LEN()` calculation per row.

## 4. Reusing Calculations, Nesting, LODs & Table Calcs
- **Do not invoke another calculated field multiple times in one formula**: Calling `[Profit Margin]` 4 times inside an `IF` forces Tableau to expand the expression tree 4x (exponential query bloat when chained!). Refactor logic or push intermediate columns to the extract/DB.
- **Reduce deep calculation dependency chains**: Long chains of calcs referencing calcs (`Compile query` spikes!) should be simplified or flattened.
- **LOD (`FIXED`, `INCLUDE`, `EXCLUDE`) vs Table Calculations vs Pre-Aggregation**:
  - `{FIXED ...}` generates independent subqueries that bypass regular dimension filters (see Filter Order of Operations) and cannot be materialized with `Compute Calculations Now`. High-cardinality `FIXED` dims are expensive; push stable cohorts (e.g. `First Purchase Date per Customer`) to SQL/ETL/Relationships when possible.
  - Table Calcs (`WINDOW_SUM`, `RUNNING_SUM`, `LOOKUP`, `INDEX`) run in-memory *after* DB queries on aggregated marks—often *faster* than LODs when the number of marks in the view is modest, but slow when operating across tens of thousands of marks.
- **Avoid `RAWSQL*` / `SCRIPT_*` (TabPy/Rserve) unless unavoidable**: External analytics extensions serialize rows over HTTP per view update and block parallel rendering cleanly.
- **Clean up & Document**: Delete/hide unused calculated fields (or use `Compute Calculations Now` on Extracts to materialize row-level calcs in `.hyper`). Comment non-obvious formulas with `//`.
