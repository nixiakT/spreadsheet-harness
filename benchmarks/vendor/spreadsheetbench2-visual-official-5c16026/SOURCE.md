# SpreadsheetBench v2 Visualization evaluator provenance

- Repository: `https://github.com/RUCKBReasoning/SpreadsheetBench-2`
- Revision: `5c160265aa93c15b38e4034cbf1e09ab498335d9`
- Upstream path: `evaluation/run_visual_vlm_checklist_eval.py`
- SHA-256: `8d32fa3a895edf205f3749aaeaba46e6ff79f39c3eb8f7dff1888e92e390d703`
- Public default judge: `glm-4.6v`
- Public threshold: `score > 0.7`

The upstream evaluator requires Windows with either the `Excel.Application` or
`Ket.Application` COM server. Linux/LibreOffice rendering is not an official
substitute. Provider, COM, export, or judge failures are `not_scored`; they are
not zero-accuracy task outcomes.

The evaluator source is kept in the pinned upstream checkout during development
and must be copied byte-for-byte into a Windows evaluation bundle only after its
SHA-256 is verified against the value above.
