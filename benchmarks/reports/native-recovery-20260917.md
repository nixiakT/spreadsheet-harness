# Spreadsheet-RL native proxy recovery (2026-09-17)

This is a Linux/LibreOffice clean-room Tools+NativeHarness approximation, **not
the official Microsoft Excel environment and not an RL-trained checkpoint**.
Four groups: DeepSeek-V4-Flash and dashscope/qwen3-coder-480b-a35b-instruct,
each on SpreadsheetBench v1 (912 instructions, sibling replay) and v2 (297
nonvisual tasks). The 24 Visualization tasks remain outside this protocol.

## Evidence and operation

- Recovery root: `benchmarks/results/native-recovery-20260917/`.
- `plan.json`: frozen task selection, source identities, evaluator hashes,
  generation settings and the reason for each recovery decision.
- `sources/`: original result/manifest snapshots. Original run directories and
  dataset input/golden files are not modified.
- `frozen-source/identity.json`: the implementation used by background workers.
- `tasks/GROUP/TASK/result.json`: independently evaluated result, provenance,
  original and new artifact hashes, source outcome and recovery action.
- `reports/GROUP/results.json`: collected per-task results.
- `summary.json`: live progress, scored denominators and completeness. A full
  benchmark mean is withheld while any task is pending or unresolved.
- `supervisor-venv.log`: supervisor log. Per-task logs are in task directories.

The supervisor uses four inference workers and two artifact/evaluation workers.
Each inference attempt has at most 50 model calls/turns, temperature 0,
top_p 1, seed 41, thinking requested and reasoning effort medium, a 32768
output-token setting, 10M cumulative token limit and a six-hour task limit.
The campus relay is used; request deadlines are 600 seconds. Model reasoning
support is provider-dependent; acceptance of a parameter is not proof of hidden
thinking. No substitute model or paid vision model is used.

Missing tasks and provider-failed tasks (including previously scored salvage)
are selected for new inference. A second inference attempt is permitted only
for a recorded infrastructure failure. Normally completed wrong answers are
not selected for resampling. Three empty v1 instructions are retained as
`dataset_invalid` for each model, without fabricating instructions.

## Verified fixes

1. Array-formula restoration now transfers the cache value **and type**.
   The old restorer produced numeric-typed `#NAME?` caches that openpyxl could
   not read. Old artifacts are copied and only malformed error-cache types are
   repaired; the error values themselves are not changed.
2. v1 sibling replay now includes successful mutating `bash` calls. Affected
   siblings are replayed from the same frozen solution and recalculated.
3. Cache seeding permits an intentionally added/deleted/renamed sheet and only
   matches seed worksheets by identical name, never by index.
4. LibreOffice's narrowly identified `name` → `name -1` import renaming is
   corrected through UNO before calculation. The ordered sheet identity gate
   still rejects unrelated name/order/kind/visibility changes.
5. Both deployed routes failed the image-visibility probe. The public DeepSeek
   relay rejected image messages with HTTP 400; the campus relay accepted but
   did not deliver image contents. The adapter explicitly tells these text-only
   routes that the image was not delivered, retaining textual inspection tools.
6. The metadata-only XML namespace compatibility reader now handles lxml as
   well as ElementTree errors, without changing workbook values on disk.

## Evaluators and interpretation

v1 uses unmodified official `evaluation.py` at revision
`49b73a94775fb489063f60ca1865e3a650079a79`, SHA256
`4ae77cee8df01d1f34684fceab972810d696886533d33be2e89373de6b4d3de3`.
The official CLI counts comparison exceptions as false; the recovery wrapper
does the same and retains exception diagnostics. It does **not** conflate a
missing model run with an evaluator failure on an existing output. Soft is
mean success over three cases; Hard requires all three cases to pass.

v2 uses unmodified official `evaluation.py` at revision
`83d415ce87b1d6b8e8eafcc26957f5d13d37210f`, SHA256
`04a2a75b29805ab40efe93e202384c365d1d32b9c924c1a4aed56e41249facb0`.
The existing metadata/chartsheet-compatible loading shim is retained and
disclosed; accuracy, modification accuracy and regression accuracy are emitted
by the pinned evaluator. No scoring rules or answer workbooks are edited.

The aggregate is a **recovery composite** of retained solutions and repaired or
retried infrastructure failures, not a single homogeneous fresh run. Original
and recovery implementation/manifest hashes remain available per task.
