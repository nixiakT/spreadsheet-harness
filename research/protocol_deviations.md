# Protocol deviations

This ledger records departures from frozen study procedures. A deviation is
never repaired by relabeling it as compliant after outcomes have been observed.

## 2026-08-14: interim read-only audit of the v27 predecessor run

- **Affected study:** the frozen v27 SpreadsheetBench-Verified reserve run.
- **Planned rule:** while the run was active, observers could check process
  liveness only and could not inspect result rows, partial aggregates, or audit
  output.
- **Deviation:** an independent manuscript reviewer mistakenly invoked the
  read-only benchmark auditor against the active result directory. The reviewer
  saw the partial completion count and partial row-level audit output, then
  reported the occurrence to the primary agent. No workbook, result, manifest,
  process, prompt, code, or run configuration was modified.
- **Command:** `.venv/bin/sheet-harness benchmark audit
  benchmarks/results/qwen36-local-v27-reserve79-eval-v1-bare-ours-seed41
  --dataset benchmarks/data/spreadsheetbench_verified_400`.
- **Timing record:** the deviation was detected and disclosed during the
  2026-08-14 review session while the process was still live. The exact
  wall-clock timestamp of the mistaken read was not captured; this missing
  timestamp is retained as part of the deviation rather than reconstructed.
- **Containment:** all agents were instructed to stop reading benchmark data,
  result directories, audits, or summaries for the active run. The process was
  left to complete naturally. No observed value may be used to change the
  harness, select a prompt, choose a model, stop or resume the run, or define an
  analysis.
- **Disposition:** v27 is permanently downgraded to exploratory predecessor
  context. It cannot support a confirmatory claim, hypothesis test, model or
  method selection, or a claim about SheetLedger. Any eventual aggregate must
  be labeled post-hoc and accompanied by this deviation.
- **Unaffected work:** the v29 profile study and the proposed 100-task external
  study had not launched. Their tasks and outcomes were not inspected through
  this event.
- **Prevention:** reportable v29 runs require a sealed run root, a separate
  liveness-only command, an explicit prohibition on audit/result access before
  completion, and a fresh analyst who has not observed interim outcomes.
