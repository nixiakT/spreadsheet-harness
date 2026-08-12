# Relay diagnostic: v4 canary `493-18/paper`

Date: 2026-08-12 (Asia/Shanghai)

This record contains no API key, response text, tool arguments, workbook
content, or full request payload. It explains why protocol v4 is failed and
retired; it is not a model-quality result.

## Preceding smoke

The new non-scored v4 smoke ran task `41691` through all three arms. All three
arm-tasks completed without an infrastructure, routing, or budget error. The
bare, paper-inspired, and ours arms scored 0/1, 1/1, and 1/1 respectively.
Those scores are smoke diagnostics, not benchmark estimates.

All 26 smoke responses completed on their first HTTP attempt. The slowest
headers latency was 135.723 seconds on the first bare response. Paper vision
attached the original rendered PNG; range-to-LaTeX, parseable YAML provenance,
the four read-only workbook hashes, terminal routing, and output reopening all
passed independent artifact review.

## Formal canary failure

The formal v4 canary used a completely new directory. Its first scheduled row
was `493-18/paper`. The first nine model responses completed successfully in
about 86 seconds and reported 44,923 tokens. Their HTTP headers arrived in
0.617-1.091 seconds.

At 03:00:08.068, the paper solve stage sent turn 1. The request:

- was 9,318 serialized bytes;
- contained no image or prior raw tool output;
- exposed only `code_interpreter`;
- used `tool_choice: required`;
- was the first request of a new stage client.

Its attempt history was:

| Attempt | Result | Headers/SSE | Backoff after attempt |
| ---: | --- | --- | ---: |
| 1 | `ConnectTimeout` at 20.026 s | none | 1.003 s |
| 2 | `ReadTimeout` at 300.158 s | none | 60.060 s |
| 3 | `ReadTimeout` at 300.163 s | none | 60.059 s |
| 4 | `ReadTimeout` at 300.164 s | none | none |

The logical request consumed 1,041.634 seconds. The arm-task ended after
1,127.746 seconds as `provider_transient`, with 9 successful responses and
44,923 reported tokens. Because the canary requires all 18 arm-tasks to finish
without infrastructure errors, the run was stopped immediately after this row.
The partial next-arm trajectory is retained, but no row from this directory may
be resumed, imported, or combined with another protocol.

The next arm had already started before the runner was stopped. Its first two
requests received headers after 2.729 and 3.204 seconds and completed normally.
This rules out a continuously unavailable local network, but does not identify
whether the failed request hit a Relay queue, shared quota window, upstream
timeout, or another payload-dependent path.

## Byte-identical long-timeout replay

A single non-scored diagnostic reconstructed the failed first-turn JSON body
from its recorded prompt and frozen schema. The reconstructed size matched the
recorded 9,318 bytes exactly. No tool was executed and no response content or
arguments were retained.

The replay changed only the transport envelope to a 900-second client timeout
and zero retries. It received an empty HTTP 408 after 320.998 seconds, with
headers at the same time and no SSE event. Thus simply raising the client
timeout above 300 seconds does not recover this condition; it exposes a roughly
321-second Relay/upstream timeout instead.

## Interpretation and required follow-up

Payload size is not a sufficient explanation: the preceding stage completed a
58,144-byte vision request, and successful solve requests in the v4 smoke were
9,910-13,402 bytes. Local host snapshots after the event showed ample CPU and
memory headroom and no socket exhaustion, but no historical `sar`/`atop` data
exists for the exact interval.

The smoke plus the first formal row also produced about 161,346 successful
provider-reported tokens over roughly ten minutes. Shared quota or queue
backpressure is therefore a plausible hypothesis, but no 429, rate-limit
header, or `Retry-After` was returned. It is not established as the root cause.

The Relay administrator should correlate access, queue, upstream, quota, and
scheduled-job logs for 03:00:08-03:17:30. Future diagnostics need a safe client
request ID, selected response/rate-limit headers, and an explicit delivery-state
classification. Retrying a POST after a read timeout is delivery-ambiguous and
can duplicate hidden upstream inference, so v4's four-attempt policy must not be
used for a later scored protocol.
