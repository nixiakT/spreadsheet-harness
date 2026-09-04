# SpreadsheetBench-v2 Qwen36 policy 1.28.0 stratified pilot30

Status: frozen before execution on 2026-08-26 (Asia/Shanghai).

This evaluation reuses the fixed 30-task stratified sample from the 2026-08-20 protocol: 10
Debugging, 10 Financial_Model, and 10 Template tasks. Each task receives adjacent `bare` and
`ours=plugevolve-seed` arms under one manifest. The frozen ours composition SHA-256 is
`1aa5dcb7f4547c7d9ddd03d89571f334ddd9864951ca4126990bc294217b39a4` and contains
`policy-ours` 1.28.0.

The provider is the laboratory open-source `qwen36-35b-a3b` model through Chat Completions.
Thinking is disabled. Generation uses temperature/top-p 1/1, seed 41, presence penalty 2, top-k
40, min-p 0, and repetition penalty 1. Each arm receives at most 8 calls/turns, 120,000 total
tokens, 4,096 output tokens per call, and 1,200 seconds. Request timeout is 700 seconds, LiteLLM
timeout is 600 seconds, and the arm-order seed is 20260820. Delivery-safe transient retries are
limited to three; ambiguous deliveries are never replayed.

The frozen task order is:

1. `Debugging/01_04`
2. `Financial_Model/05_04`
3. `Template/01_04`
4. `Debugging/09_01`
5. `Financial_Model/13_05`
6. `Template/08_03`
7. `Debugging/04_06`
8. `Financial_Model/04_01`
9. `Template/02_03`
10. `Debugging/09_06`
11. `Financial_Model/01_01`
12. `Template/15_02`
13. `Debugging/08_05`
14. `Financial_Model/20_02`
15. `Template/06_09`
16. `Debugging/08_08`
17. `Financial_Model/01_02`
18. `Template/02_05`
19. `Debugging/06_01`
20. `Financial_Model/09_01`
21. `Template/13_06`
22. `Debugging/05_03`
23. `Financial_Model/16_04`
24. `Template/14_07`
25. `Debugging/10_08`
26. `Financial_Model/07_01`
27. `Template/06_02`
28. `Debugging/02_08`
29. `Financial_Model/02_02`
30. `Template/02_01`

Provider or infrastructure failures are unscored, never converted to zero. All 60 arms must
receive official scores and a fresh audit before reporting the complete paired result. Retry
evidence may be merged only when dataset, evaluator, model, generation settings, resource limits,
and both composition hashes match exactly.

Output identity:
`benchmarks/results/policy-v1280-pilot30-paired-20260826`
