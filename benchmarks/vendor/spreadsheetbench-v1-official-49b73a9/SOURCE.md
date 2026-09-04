# SpreadsheetBench v1 official source

- Repository: `https://github.com/RUCKBReasoning/SpreadsheetBench`
- Revision: `49b73a94775fb489063f60ca1865e3a650079a79`
- Archive: `data/spreadsheetbench_912_v0.1.tar.gz`
- Archive SHA-256: `9cf7228b54f1edcdd4b372eb736774adf29cb4f804c9920229bac6c154833399`
- Extracted `dataset.json` SHA-256: `e5137ecbec4273d91344a0c8feb2aff2d4a93d5881ac40e490250dfd8db227de`
- `evaluation/evaluation.py` SHA-256: `4ae77cee8df01d1f34684fceab972810d696886533d33be2e89373de6b4d3de3`

The pinned archive has 912 instructions and 2,726 present input workbooks. Seven
instructions have fewer than three input cases. This differs from the 2,729
case count stated in the repository README and must be disclosed in reports.
The historical evaluator nevertheless uses a fixed three-case denominator for
every instruction. Provider or harness failures remain `not_scored` and block a
complete-study claim; they are not converted to model failures.
