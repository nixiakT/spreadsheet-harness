# Spreadsheet Agent Harness

A Linux-first harness for realistic spreadsheet agents. It combines structured workbook inspection, transactional editing tools, LibreOffice rendering/recalculation, direct vision input, trajectory logging, and validation-gated skill evolution.

The implementation is clean-room. It borrows architectural ideas—not source code—from SpreadsheetAgent, Spreadsheet-RL, and Trace2Skill. This matters because several research repositories or released skill files do not carry a clear permissive license.

## What is implemented

- One isolated workspace and output workbook per run.
- Atomic `.xlsx`/`.xlsm` edits with a snapshot before every mutation.
- Spreadsheet tools: sheet inventory, range inspection, search, write, formula fill, formatting, clear, row/column deletion, sheet management, recalculation, rendering, image viewing, undo, and a bounded local Python interpreter.
- Three workbook views: structured JSON/YAML, Markdown tables/formulas, and LibreOffice-rendered PNG pages.
- A Responses API agent loop. `view_image` attaches the original PNG as an `input_image` block instead of returning a lossy textual description.
- Secret-redacted JSONL trajectories and versioned run manifests.
- SpreadsheetBench adapters that keep evaluation protocol and calculation backend explicit.
- Candidate-only skill evolution; learned procedures are never silently written over production skills.

## Architecture

```text
Dataset adapter -> isolated WorkbookSession -> tool/vision agent -> output workbook
                         |                         |
                         +-> snapshots             +-> redacted trajectory
                         +-> LibreOffice backend    +-> candidate skill evolver
                                                      |
                                           validation-gated promotion
```

## SheetLedger research protocol

The optional v28 path adds revision-aware spreadsheet evidence and delivery
records around the existing agent arms. It stages each protected mutation,
computes the workbook effect independently of the tool's self-report, requires
the effect to stay inside a previously inspected and declared target, and binds
post-edit witnesses to the affected scope and exact workbook revision. Every
`submit_result` attempt is snapshotted before gating. After the agent terminates,
the observer evaluates those snapshots without feeding outcomes back, records
postprocessing, and gives the scorer a read-only byte-identical replica.

Enable this engineering protocol explicitly:

```bash
sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --output benchmarks/results/v28-development \
  --arm bare --arm profile --arm native --arm paper --arm ours \
  --deliverable-lineage
```

The fixed contract is `contracts/spreadsheet-evidence-v1.yaml`. A fresh audit
replays the contract, target-authorization chain, completion-attempt lifecycle,
candidate-to-final transition, and scoring-copy binding. The v28 switch is a
component-integration protocol, not by itself the B5/B6/FULL causal matrix in
the paper design and not evidence of benchmark improvement. Freeze a separate
run spec before any reportable experiment, and do not compare its results with
the historical v23--v27 studies as if only one mechanism changed.

LibreOffice is a practical Linux backend, not a bit-for-bit substitute for desktop Excel. Results always record the calculation engine and protocol. In particular, scores from Excel COM and LibreOffice, or `solution_once_apply_n` and `agent_per_workbook`, must not be presented as the same leaderboard setting.

## Install

Python 3.10+ and LibreOffice are required. No Windows host or Docker is required for the Linux path.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
sheet-harness doctor
```

The harness discovers the active provider, model, and API key from `~/.codex/config.toml` and `~/.codex/auth.json`. You can instead set `SHEET_AGENT_BASE_URL`, `SHEET_AGENT_MODEL`, and `OPENAI_API_KEY`. Secrets are not copied into run directories.

## Use

Preprocess and render without calling a model:

```bash
sheet-harness preprocess workbook.xlsx --output runs/profile
sheet-harness render workbook.xlsx --output runs/render
```

Run one task:

```bash
sheet-harness run workbook.xlsx \
  --instruction 'Add a Total formula to D2:D20 and match the adjacent number format.' \
  --runs-dir runs
```

Each run contains the untouched input, `artifacts/output.xlsx`, rendered pages, snapshots, `trajectory.jsonl`, and `run.json`.

Before using a local vLLM endpoint, verify the exact Responses function-call
round trip required by the harness. This synthetic check sends two model
requests but does not open a workbook or execute a local tool:

```bash
sheet-harness doctor --online --tools \
  --base-url http://HOST:PORT/v1 --api-key EMPTY \
  --model Qwen/Qwen3.5-35B-A3B --reasoning-effort none
```

Trace2Skill's released runner uses Chat Completions with text ReAct and bash, so
an endpoint that runs that code does not necessarily support this harness's
Responses `function_call` / `function_call_output` protocol.

Run tests:

```bash
pytest
ruff check .
```

For VS Code Remote-SSH, open the repository checkout. The workspace settings select
`.venv/bin/python`, enable pytest, and recommend the Python and Ruff extensions.

## SpreadsheetBench Verified

The adapter pins `KAKA22/SpreadsheetBench` revision `ab0b742b0fc95b946f212d80ac7771b5531272e4` and verifies the archive SHA-256 before extraction:

```bash
sheet-harness benchmark download --output benchmarks/data
sheet-harness benchmark run \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --output benchmarks/results/baseline \
  --model gpt-5.4 --reasoning-effort low
```

By default, two dataset rows explicitly marked `exclude` are skipped, leaving 398 tasks. The clean-room scorer matches the published value normalization but fixes malformed sheet/range metadata and uses `answer_sheet` when appropriate. It has a golden-as-candidate self-check of 398/398 across 298,012 answer cells.

The official scorer ignores formatting and only checks cached values. Harness summaries therefore record `style_checked: false`, the calculation backend, completion/error rate, completed-only accuracy, and end-to-end accuracy. A small pilot is not a leaderboard result.

Provider transience and safe replay are separate decisions. Automatic replay is limited to
pre-send connection failures and explicit 425/429/503 or structured overload rejections;
read/write failures, HTTP 408, interrupted streams, and ambiguous deliveries fail closed. The
runner stops as soon as `response.completed` arrives, journals each task durably, and never
resamples an ambiguous delivery. A manifest pins task/workbook hashes, source code, skills,
runtime versions, model, effort, pacing, and run flags so incompatible runs cannot be mixed.

Because this relay does not support `previous_response_id`, the agent uses a bounded stateless
context policy: it sends the original instruction, a lossy capped summary of older tool calls,
and exactly one most-recent raw model/tool turn. Bounded previews of large range inspections and
original PNG data therefore remain available for the next decision without being replayed through
every later turn.
The complete older-history envelope is capped at 16,000 characters, model-visible tool outputs
are capped at 64,000 characters for the whole recent turn, and attached PNGs are capped at 20 MiB
of raw image bytes per turn (base64 makes the wire request larger). Per-request input/wire sizes
and token usage are recorded so long-run gates can be checked from the result artifacts.

The current finite three-arm study uses Luna-low, a runner-wide 20-second Relay pacer, one
delivery-safe retry, an immediate circuit breaker, and a 30-task exploratory engineering set.
Its credential-isolated gate, commands, task hash, paper-comparison boundary, and reporting plan
are frozen in `benchmarks/protocols/luna-low-three-arm-limited30-v6.md`. No full-398 run is
authorized by that protocol. The v5 smoke failed on its first request with an ambiguous empty
HTTP 408 and is permanently retired; do not resume it or import any v5 row into v6.

> **Historical protocol:** the formal v4 canary failed on its first arm-task
> with a Relay transport error and is permanently retired. Do not execute or
> resume the command below; it is retained so the failed run remains auditable.

```bash
sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --task-id-file benchmarks/protocols/luna-low-three-arm-canary-v4.txt \
  --output benchmarks/results/luna-low-three-arm-canary-v4 \
  --model gpt-5.6-luna --reasoning-effort low \
  --request-timeout 300 --request-retries 3 \
  --max-model-calls 20 --max-total-tokens 100000 \
  --max-output-tokens 4096 --task-timeout 1800 \
  --circuit-breaker 3 --arm-order-seed 20260811
```

The three default arms are code-only `bare`, a clean-room small-model-only
SpreadsheetAgent-inspired adaptation named `paper` (reported as `paper-inspired`), and this harness plus its
frozen skills named `ours`. Comparison runs require the strict Linux Bubblewrap
workspace boundary and fail before any model call when it is unavailable. The
retired v4 task lists and failure record remain documented in
`benchmarks/protocols/luna-low-three-arm-v4.md`.

`bare` is a code-interpreter baseline, not a no-tool chat baseline and not the
original SpreadsheetBench `solution_once_apply_n` protocol. It receives a
bounded five-row preview and can inspect/edit its one isolated workbook using
only Python. Two optional diagnostic arms are available explicitly:

- `profile`: `bare` plus a zero-model-call, task-independent deterministic
  workbook profile with bounded provenance, confidence, and truncation fields.
- `native`: the `ours` prompt and spreadsheet-native tools without any skill,
  isolating the skill increment.

For example, `--arm bare --arm profile --arm native --arm ours` collects a
paired ablation matrix; omitting `--arm` retains the historical three defaults.
Use `sheet-harness benchmark audit RESULTS --dataset DATASET` to reopen and
freshly rescore every recorded artifact, verify hashes, and reject incomplete or
duplicate task-arm matrices.

The failed v1 canary is retained as a Relay diagnostic and is never merged into v2. On task
`493-18`, an `auto` follow-up ended after two 90-second read timeouts without a terminal response.
In separately instrumented, semantically equivalent 180-second probes, explicit and implicit
`auto` returned no HTTP headers, while the named `code_interpreter` route returned headers at
146.8 seconds and completed at 152.0 seconds. V2 therefore freezes a short named-tool prefix for
every tool-using stage, uses `auto` only afterwards, and raises the per-request bound to 300 seconds
while retaining the 900-second arm-task deadline and one transient request retry. The redacted
diagnostic record and its limitations are in `benchmarks/diagnostics/relay-493-18-turn2.md`.
V3 and V4 avoid `auto` entirely. Each frozen prefix response exposes only its prescribed operational
tool and uses `tool_choice: required`. After the prefix, the stage exposes its normal operational
tools plus the shared `submit_result` control tool, again under `required`; the model calls that
control tool to end the stage. Two reconstructed required-route probes completed in 5.9 and 5.7
seconds and the second emitted exactly one `code_interpreter` call. A submission consumes one
provider response, its tokens, elapsed time, and HTTP attempt, but is not included in
`agent.tool_calls` or `tool_trace`, which count operational workbook-tool attempts. Paper
reconciliation has no tools and still returns text directly. This is a compatibility mitigation
supported by the observed condition, not a claimed Relay root-cause fix or a guarantee that every
request will complete.

All v1-v3 result directories are retired diagnostics and are never merged into v4. A v2 canary
was accidentally started during read-only review and stopped after exactly one completed paper row;
that row is explicitly excluded rather than selectively resumed.

V3 then passed fresh bare and paper-inspired smoke gates, but its formal canary failed on the first
arm-task before any model response: both permitted 300-second attempts ended in no-header
`ReadTimeout`. V4 preserves the v3 failure artifact and never resumes it; v4 reruns the fixed task
list from scratch in a new directory. It pre-registers three request retries, a 1,800-second
arm-task bound, and a 30/60/60-second cooldown after no-header read timeouts. Successful model
responses and provider tokens remain capped at 20 and 100,000.

V4 subsequently passed a fresh three-arm smoke, then failed its first formal
canary row (`493-18/paper`) after one connect timeout and three 300-second
no-header read timeouts. A byte-identical, non-scored replay with a 900-second
client timeout and no retry received an empty HTTP 408 from the Relay at
320.998 seconds. V4 is therefore retired and must not be resumed. No v4 pilot
or full-398 run is valid or authorized by this document.

Historical v4 behavior retried explicit overloads and no-header read timeouts with progressively
longer delays. That policy is retired: v5 never replays a read/write timeout, HTTP 408, interrupted
stream, or other ambiguous delivery. Only the delivery-safe allowlist described above may retry.

For the CRS relay used in this deployment, `gpt-5.6-sol` rejects the Codex label `ultra`; the
highest accepted Responses effort is `max`. Passing `--reasoning-effort ultra` therefore records
`requested_reasoning_effort=ultra` while sending and recording `reasoning_effort=max`. Responses
are sent with `store=false` by default.

The following full command is historical only. A paired 398-task comparison
may run only under a later pre-registered protocol whose canary and 30-task
pilot both succeed:

```bash
sheet-harness benchmark compare \
  --dataset benchmarks/data/spreadsheetbench_verified_400 \
  --arm bare --arm paper --arm ours \
  --output benchmarks/results/luna-low-three-arm-full398-v4 \
  --model gpt-5.6-luna --reasoning-effort low \
  --request-timeout 300 --request-retries 3 \
  --max-model-calls 20 --max-total-tokens 100000 \
  --max-output-tokens 4096 --task-timeout 1800 \
  --circuit-breaker 3 --arm-order-seed 20260811
```

Use the exact same command and output directory to resume. Non-retryable failures and transient
failures that have consumed their recorded task-retry budget remain final end-to-end failures;
they are not silently resampled. The summary always uses the selected task set as its denominator
and reports missing tasks, error categories, known completed-token usage, and request retries.

## Skill evolution

Generation only writes a candidate directory; it cannot overwrite a production skill.
Every input trajectory must contain an explicit evaluator outcome; agent
completion or a committed workbook mutation is not treated as correctness:

```bash
sheet-harness evolve generate runs/*/trajectory.jsonl --output evolution
```

Promotion is a separate explicit command. It requires at least three paired validation seeds, a candidate mean strictly above baseline plus the requested margin, matching hashes, and no severe regression flag:

```bash
sheet-harness evolve promote evolution/candidates/<id> \
  --skill-root skills/spreadsheet-core \
  --validation-report validation.json \
  --min-delta 0.01
```

## Safety and fidelity boundaries

- Ordinary local runs treat interpreter code as trusted; their `cwd` and resource limits are not a complete security boundary. Three-arm comparison runs are stricter: they require Linux Bubblewrap, expose only allowlisted runtime files plus the current arm workspace, disable networking, and never fall back to unsandboxed execution.
- Openpyxl can preserve VBA payloads in `.xlsm`, but advanced Excel objects may still be lossy. Keep the original input and inspect warnings.
- LibreOffice may differ on dynamic arrays, newer Excel functions, external links, date systems, and some formatting. Use an Excel COM sidecar for canonical Excel-compatible evaluation.
- The relay URL in the deployment described here uses plain HTTP. Treat credentials as exposed to the network path and rotate them after setup or move the relay behind valid TLS.

## Research lineage

- SpreadsheetAgent (arXiv:2604.12282): structural sketches and complementary cell/text/image views.
- Spreadsheet-RL (arXiv:2605.22642): granular spreadsheet tools, isolated workspaces, and inspect-edit-verify interaction.
- Trace2Skill (arXiv:2603.25158): trajectory analysis, consolidated candidate skills, and validation before promotion.
- SpreadsheetBench: realistic task inputs and answer-range evaluation.
- Stirrup: a useful reference for multimodal tool-return handling and agent session design; this package remains Python 3.10 compatible and does not depend on Stirrup.

The Trace2Skill-aligned Qwen3.5 model choice, split hashes, sampling settings,
compatibility gate, paired estimands, and reporting requirements are frozen in
`benchmarks/protocols/qwen35-trace2skill-verified-agent-v1.md`. No Qwen result is
claimed until a pinned endpoint/checkpoint passes that gate.
