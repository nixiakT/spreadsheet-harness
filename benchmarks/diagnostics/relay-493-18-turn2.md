# Relay diagnostic: SpreadsheetBench 493-18 bare turn 2

Date: 2026-08-12 (Asia/Shanghai)

This record contains no API key, provider response body, workbook content, or
full request payload. It documents why protocol v2 changed routing and timeout;
it is not a model-quality result.

## Trigger

The v1 canary completed `493-18/paper` and `493-18/ours`. The bare arm completed
turn 1 in 7.004 seconds, invoked `code_interpreter`, and then failed turn 2 after
two read attempts totaling 181.532 seconds. Its final row records
`provider_transient`, `phase=read`, and `attempts=2`. The old client did not
retain whether either attempt had received HTTP headers or an early SSE event.

## Instrumented reconstruction

The diagnostic reconstructed the model, instructions, initial user item,
function call, function-call output, tool schema, effort, and output limit of
the roughly 5 KiB turn-2 condition. Each row below is one request with no retry.

| Variant | Wire bytes | Timeout/result | Headers | First SSE | Terminal |
| --- | ---: | --- | ---: | ---: | ---: |
| Explicit `tool_choice: auto` | 5,040 | read timeout at 180.334 s | none | none | none |
| Omitted `tool_choice` (API default auto) | 5,019 | read timeout at 180.345 s | none | none | none |
| Named `code_interpreter` | 5,079 | HTTP 200; completed | 146.811 s | 146.887 s | 151.981 s |

The successful named request emitted 49 SSE events and ended in
`response.completed` with a `reasoning` item and a `code_interpreter`
`function_call`.

## Interpretation and limits

Named routing is the only tested condition that completed, so v2 uses a short,
audited named-tool prefix followed by `auto` and a 300-second per-request bound.
This demonstrates a payload-condition association, not the Relay's internal
root cause. The reconstruction used fresh request/call identifiers and did not
retain full payloads or server-side logs; byte sizes therefore differ slightly
from the original v1 request. No v1 result is copied, selected, or resampled in
v2.

## Follow-up overload observation

An earlier exploratory bare routing smoke did not reproduce the long read stall. Its first
named response completed after one transient retry, but the second named request
received `server_is_overloaded` on both attempts and failed after 3.513 seconds.
The arm had one successful response (3,463 tokens) and ended after 11.461
seconds. This is a distinct, explicit capacity signal rather than evidence
about the earlier read timeout. Protocol v2 keeps one retry but waits at least
15 seconds before retrying an overload/service-unavailable stream.

## Required-route follow-up and protocol v3

Two further single-attempt reconstructions used the same semantic turn-2
condition with `tool_choice: required`. Their wire bodies were 5,232 bytes.
They returned HTTP 200 headers at 0.985 and 0.801 seconds and completed at
5.923 and 5.687 seconds. The second probe retained only output item types and
function names: it observed one reasoning item and exactly one
`code_interpreter` function call. Neither probe retained function arguments,
workbook content, response text, or credentials.

Protocol v3 therefore makes every forced-prefix response expose only its
prescribed operational tool under required routing. Post-prefix responses
expose the stage's operational tools plus a shared `submit_result` control tool,
again under required routing. A submission consumes one model-response slot but
is not counted as a workbook tool invocation. This changes the tool interface
and termination semantics, so no v1/v2 row is reused in v3. Required routing is
a measured Relay compatibility mitigation, not proof of an internal root cause.

## Formal v3 canary failure and protocol v4

Fresh v3 bare and paper-inspired smoke runs completed successfully. The first
formal v3 canary row nevertheless failed at `493-18/paper` extraction turn 1.
Both permitted attempts ended in `ReadTimeout` after 300.160 and 300.223 seconds;
neither received HTTP headers or an SSE event. The row therefore consumed zero
successful model responses and zero provider-reported tokens, and was classified
as `provider_transient` after 601.707 seconds.

The formal v3 directory is failed, retired, and must not be resumed or mixed
with later results. Protocol v4 preserves required routing and all model-resource
caps but pre-registers three transient retries, a 1,800-second arm-task bound,
and no-header cooldowns of 30, 60, and 60 seconds. This gives an individual
provider response at most four real HTTP attempts. It remains a transport
mitigation; only a fresh v4 smoke and complete 18-row canary can establish that
the experimental gate is usable.
