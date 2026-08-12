# Qwen3.5 Trace2Skill-style SpreadsheetBench-Verified agent protocol v1

This protocol answers one narrow question: for the same Qwen checkpoint on the
same SpreadsheetBench-Verified workbook, how much does this spreadsheet harness
improve over a code-interpreter-only agent? It is an exploratory paired agent
evaluation, not a reproduction of the original SpreadsheetBench leaderboard.

## Models and revisions

Run each checkpoint as a separate study:

- `Qwen/Qwen3.5-35B-A3B`
- `Qwen/Qwen3.5-122B-A10B`

Trace2Skill v5 names these as its two main skill-author/skill-user models. The
paper does not pin Hugging Face revisions. Before collection, record the exact
downloaded commit and file hashes in the deployment record; never describe the
current Hugging Face HEAD as the paper's original revision.

Serve the agent in non-thinking/instruct mode with the Trace2Skill settings:

```json
{
  "temperature": 1.0,
  "top_p": 1.0,
  "presence_penalty": 2.0,
  "timeout_seconds": 600,
  "top_k": 40,
  "min_p": 0.0,
  "repetition_penalty": 1.0,
  "enable_thinking": false
}
```

Use seeds `41`, `42`, and `43`. A valid provider adapter must forward and record
all sampling fields and the seed. The current harness sends the OpenAI Responses
reasoning-effort field but does not yet expose all fields above, so formal Qwen
collection is blocked until either the adapter is added or a semantically
equivalent pinned server-side generation configuration is independently verified
and recorded.

Trace2Skill's released implementation uses Chat Completions with text ReAct and
a bash tool. This harness uses Responses function calls. Before any workbook
request, run the synthetic tool compatibility canary. If the vLLM endpoint does
not implement Responses function calls and `function_call_output`, use a pinned
Chat-Completions/ReAct adapter; do not silently change model, provider, or tool
protocol after a failed canary.

## Dataset isolation

Pin `KAKA22/SpreadsheetBench` revision
`ab0b742b0fc95b946f212d80ac7771b5531272e4` and archive SHA-256
`10ef893dd29cb13ab97143ea787e68cdc9574a13873ab9a54e50b31dc03fc949`.

- Dataset rows `0:200`: evolution/development only (200 tasks).
- Dataset rows `200:400`: held-out evaluation only (198 usable tasks after the
  two dataset exclusions at original rows 337 and 338).
- The usable held-out split is entirely Cell-Level (198/198); Sheet-Level
  stratification is therefore unavailable for this exact split and must be
  reported as not applicable, not as zero accuracy.
- Evolution split usable-ID SHA-256: `71b4d91e27e1324e947f0ad29316e5bcf4406f70e314ed08b4713569f408bb2a`.
- Held-out split usable-ID SHA-256: `445ceec8e033601a054babf7997e340cf21d1c1d2d54a4aa421a8ba29b189582`.

Each hash is computed over task IDs in original dataset order, one ID plus newline.
Golden workbooks, answer ranges/sheets, instruction types, and evaluator output
must remain outside the model workspace and prompt. Frozen skills used for a
held-out run may depend only on the evolution split.

Freeze and verify the held-out membership with the harness before selecting a
smoke, pilot, or full run:

```bash
sheet-harness benchmark split \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --write benchmarks/protocols/qwen35-trace2skill-heldout-v1.json

sheet-harness benchmark split \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --verify benchmarks/protocols/qwen35-trace2skill-heldout-v1.json
```

The command slices original `dataset.json` rows `[200, 400)` before applying
the `exclude` field. It fails closed unless the only excluded raw rows are 337
and 338, the usable count is exactly 198, and the ordered-ID hash matches the
value above. The frozen JSON records the pinned dataset revision/archive hash,
the exact local `dataset.json` hash, original-index bounds, excluded task IDs,
all 198 usable task IDs, and their ordered hash. `--write` refuses to overwrite
an existing manifest; `--verify` performs no writes.

Formal comparisons must select the split through the verified manifest, not
through `--offset 200` on the already filtered 398-task loader:

```bash
sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --split-manifest benchmarks/protocols/qwen35-trace2skill-heldout-v1.json \
  --arm bare --arm ours \
  ...
```

The runner preserves manifest task order. `--split-manifest` cannot be combined
with `--task-id` or `--task-id-file`; a smoke or preregistered subset should use
a separately frozen derivative manifest rather than an after-the-fact offset.

## Arms and estimands

The mandatory primary pair is:

- `bare`: deterministic five-row preview plus only `code_interpreter`, in an
  isolated Linux working directory; no skills or native spreadsheet tools.
- `ours`: the same model and preview plus native spreadsheet tools, vision,
  LibreOffice, and a frozen skill tree.

Optional diagnostic arms should be run on a separate preregistered pilot or
included for every selected task:

- `profile`: `bare` plus deterministic task-independent workbook profiling.
- `native`: native tools and vision but no skill, isolating the skill increment.
- `paper`: clean-room multi-format structural/vision/LaTeX adaptation.

The primary `ours - bare` contrast estimates the whole harness package. It does
not by itself attribute gains to preprocessing, tools, vision, prompting, or
skills. Use the optional adjacent contrasts for component attribution.

All selected arms receive the same per-task limits, calculation backend, scorer,
task order counterbalancing, and provider sampling configuration. Trace2Skill
allows up to 100 ReAct turns. The current comparison orchestrator has
arm-specific 20-response caps, so its present runs are 20-call engineering
evaluations, not turn-budget reproductions of Trace2Skill. A 100-turn study
requires a separately versioned configurable-cap implementation and a different
result directory; always report actual model calls.

## Gate and collection sequence

Do not allocate or preempt shared GPUs without the cluster owner's approval.
Do not download either checkpoint until storage and GPU placement are agreed.

For each model and seed:

1. Pin the checkpoint revision, vLLM/container revision, tokenizer/chat template,
   Python environment, LibreOffice version, and provider adapter source hash.
2. Run a synthetic forced-tool compatibility canary. It must return exactly one
   requested function call, accept its `function_call_output`, and terminate.
3. Run one fresh held-out workbook smoke with `bare` and `ours`; audit both
   outputs by hash, reopen, and fresh score. Never import the smoke into results.
4. Run a paired, stratified pilot selected before inference. Use a fresh output
   directory for every model/seed/budget combination.
5. Only after a clean pilot, collect all 198 usable held-out tasks. Never resume
   an ambiguously delivered request or selectively resample failures.

The runner must fail closed on missing/duplicate rows, task or artifact hash
mismatch, tool-routing mismatch, provider incompatibility, and evaluator absence.
No formal run may begin while either generation-field forwarding or the intended
turn cap is only documented rather than enforced in the payload and manifest.

## Reporting

For every model, seed, arm, and Cell-/Sheet-Level stratum report:

- completion rate;
- end-to-end accuracy (all selected tasks in the denominator);
- completed-only accuracy;
- model calls, input/output/total tokens, wall time, and tool/provider errors;
- tokens and wall time per pass, with undefined values shown explicitly when
  there are no passes;
- paired discordance table, exact McNemar p-value, and task-stratified bootstrap
  interval for the accuracy delta.

Aggregate three seeds only after showing each seed. The same-model primary pairs
are `35B bare` versus `35B ours` and `122B bare` versus `122B ours`. Optional
Trace2Skill-style cross-scale skill transfer (`35B -> 122B`, `122B -> 35B`) is a
separate experiment and must retain the author model in provenance.

LibreOffice is the common Linux calculation backend. Call these
SpreadsheetBench-Verified agent results, not Excel-COM leaderboard reproduction.
The original SpreadsheetBench `solution_once_apply_n` protocol requires one
generated program to be replayed across sibling workbooks and must be evaluated
separately on the original multi-case data.

## Current execution status

At protocol creation time no configured endpoint or local checkpoint for either
Qwen model was found, all eight shared GPUs had active workloads, and the
credential-isolated benchmark key required by the prior Relay study was absent.
Therefore no Qwen model request or scored pilot has been run under this protocol.
This is an explicit infrastructure gate, not a zero score or failed model run.
