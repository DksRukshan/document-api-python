# Formatting, Visual Hierarchy & Accessibility Best Practices

Sources: *Tableau Official Documentation (`llms-full.txt`: formatting hierarchy, accessibility, dashboard best practices, tooltips)* & *Designing Efficient Workbooks* (InterWorks, 2021)

## 1. Formatting Hierarchy & Maintainability
- **Apply Formatting Top-Down (Workbook -> Worksheet -> Specific Field)**:
  - Set baseline fonts, header colors, dividers, borders, and tooltips at the **Workbook level** (`Format -> Workbook`), then apply subtle worksheet overrides only where needed, and field-level formatting only for exceptions.
  - Avoid drifting worksheet-by-worksheet font/border overrides across 10+ sheets, which bloats `.twb` XML and creates visual inconsistency.

## 2. Typography & Readability
- Use a single clean family across the workbook: **`Tableau Book` / `Tableau Medium` / `Tableau Bold` / `Tableau Light` / `Tableau Semibold`**, or widely supported system/web-safe fonts (**Arial, Trebuchet MS, Verdana, Times New Roman, Lucida Sans**) so Tableau Server/Cloud matches Tableau Desktop cleanly.
- Limit font weights/sizes to **3-4 typographic tiers** (Dashboard Title -> Section/Sheet Header -> Body/Axis Labels -> Annotations/Captions).
- **Avoid ALL-CAPS Titles & Labels**: Reduces word-shape scannability and can make screen readers spell out acronyms. Use Title Case or Sentence Case.
- **Descriptive Titles & Captions**: Give every view a clear, active title explaining *what* it shows (plus subtitle/caption summarizing filter context or units). Provide **Alt Text / Descriptions** (Tableau Accessibility guidance) for embedded images, navigation buttons, and key visualizations.

## 3. Color, Contrast & Accessibility (WCAG 2.2 AA)
- **Contrast Ratios**: Maintain at least **4.5:1** contrast for body/header text against background shading, and **3:1** for bars, lines, icons, and interactive controls.
- **Colorblind-Safe Design**:
  - Never rely on **Red vs Green** alone (`Deuteranopia` affects ~8% of males). Prefer Blue/Orange or Viridis/Okabe-Ito palettes, and encode meaning redundantly via **shape, direct data labels, icons, or dashed vs solid line styles**.
  - **Restraint**: Keep distinct categorical colors to <= 5-7 per view. Put primary insights in a focal accent color and push secondary/benchmark series into muted grays.
- **Client-Side Rendering & Assistive Technology (<1,000 Marks)**:
  - Keep worksheet mark counts **below ~1,000 marks** wherever possible so Tableau renders SVG/Canvas client-side (enabling crisp zoom, fast interaction, and keyboard/screen-reader accessibility). Heavy mark counts trigger **Server-Side Rendering** (slower raster PNG tiles and weaker accessibility!).

## 4. Clutter Reduction & Layout Polish
- **Minimize Chart Junk**: Turn off unnecessary **Gridlines**, **Zero Lines**, **Drop Lines**, **Axis Rulers**, and heavy cell **Borders** (`Format -> Lines / Borders`). Use subtle light gray hairlines only where quantitative reading needs them.
- **Row/Column Banding**: Disable loud alternating shading bands on bar/line charts (save light banding strictly for wide scannable summary tables).
- **Use Whitespace Deliberately**: Configure **Outer Padding** (e.g. 8-12px) and **Inner Padding** cleanly around Layout Containers rather than adding blank spacer sheets. In Tableau 2026.1+, leverage rounded corners consistently rather than nested border hacks.
- **Replace KPI Arrow Calculation + Shape Sheet Hacks with Custom Number Formatting**:
  - Instead of creating string/shape calcs like `IF [YoY] > 0 THEN "Up" ELSE "Down" END` and rendering Shape marks, format a single numeric measure using Custom Number Format:
    `▲ +0.0%;▼ -0.0%;— 0.0%`
  - One pill, no extra mark rendering or string calc overhead!

## 5. Tooltips That Add Insight
- **Do not leave default `<Field>: <Value>` tooltips untouched** on user-facing dashboards.
- Format tooltips **last** after finalizing sheet structure; write concise natural-language sentences (e.g. `"In <Region>, <Category> generated <SUM(Sales)> (<% vs Prior Year> vs last year)."`), style key numbers bold, hide redundant hidden-detail fields, and uncheck **"Include command buttons"** on executive views unless ad-hoc keep-only/exclude interaction is intended.
- Use **Viz in Tooltip** sparingly — remember each hover executes a filtered query against the hidden child sheet! Keep child sheets ultra-light and fast.
