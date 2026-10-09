---
name: visual-review
description: Use workbook rendering and original images to resolve layout and formatting ambiguity.
---

# Visual spreadsheet review

Structured cell inspection and visual inspection answer different questions. Use structured ranges for exact values, formulas, and formats. Use rendered images for spatial grouping, charts, clipped text, merged headers, color encodings, and print layout.

Call `render_workbook` after the workbook state you want to inspect has been saved. Select a returned PNG with `view_image`; the harness attaches the original PNG directly as model vision input. Do not infer exact cell values from pixels when `inspect_range` can retrieve them.

For large sheets, inspect structure first so you can choose the smallest relevant rendered page. Re-render after a visual edit and compare the new page rather than relying on memory.

