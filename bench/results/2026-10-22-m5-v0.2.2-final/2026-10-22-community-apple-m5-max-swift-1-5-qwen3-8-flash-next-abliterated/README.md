# Community benchmark on Apple M5 Max

Measured on 2026-10-22 with `tools/strata_bench.py` (suite 1, 5 runs per test, medians with ranges). Speeds are the engine's own timings; first-token times are measured by the client over HTTP. Speed only: no correctness check was run.

## Hardware and software

- GPU and VRAM: Apple M5 Max, 122 GiB VRAM
- CPU: arm (18 cores, 18 threads); installed RAM: 128.0 GiB; storage: not measured
- OS: Darwin 27.0.0; GPU driver: not read; CUDA: not read
- Strata: project 0.1.47, commit 011c9e60; engine 0.1.47-metal
- Background workloads and power limits: not recorded

## Model and configuration

- Model and quantization: Swift-1.5-Qwen3.8-Flash-Next-Abliterated-IQ3_S-512K
- Context: 524288 tokens; parallel slots: 1
- Settings: the install's own configuration, unchanged; thinking off for the suite; temperature 0

## Method

Run by `python tools/strata_bench.py --long --runs 5` against the HTTP API of a Strata server (already running); model loading is not timed. One warm-up chat and a 1K prompt are discarded first. Every prompt starts with a unique tag, so the conversation cache cannot help the fresh-prompt tests.

- short chat: the three questions of `tools/ab_engine.py`, up to 100 new tokens, 5 runs
- 4K / 16K prompts: generated word lists sized by a calibration request, 24 new tokens, 5 fresh prompts each
- agent session: system prompt and four tools, then 5 turns that each add about 1500 words of a project file and continue from the model's own earlier answers (the way agent clients resend a conversation), 64 new tokens per turn, 5 sessions; the cache numbers come from the answer's usage (`cached_tokens`) and timings (`prompt_n`)
- concurrency (when run): N requests sent at the same moment, total tokens over the wall time of the burst, latency over all requests

## Results

| Configuration | Actual prompt tokens | Reused tokens | Generated tokens | Runs | Prompt tok/s median and range | Decode tok/s median and range | TTFT seconds median and range |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| short chat | 46 (43-51) | 0 | 100 (100-100) | 15 | not measured | 97.4 (90.6-110.7) | 0.27 (0.25-0.30) |
| 4K prompt, fresh | 4019 (4019-4019) | 0 (0-0) | 24 cap | 5 | 1635.2 (1513.2-1737.6) | not measured | 2.48 (2.33-2.68) |
| 16K prompt, fresh | 16058 (16058-16058) | 0 (0-0) | 24 cap | 5 | 1508.6 (1481.7-1530.2) | not measured | 10.67 (10.52-10.86) |
| 32K prompt, fresh | 32049 (32049-32049) | 0 (0-0) | 24 cap | 5 | 1468.3 (1460.1-1468.7) | not measured | 21.86 (21.85-21.98) |
| agent session, follow-up turns | 14580 (14576-14615) at the last turn | see below | up to 64 | 5 | see below | 90.6 (75.1-98.7) | 2.24 (1.22-2.75) |

Ranges are the lowest and highest of the runs; `Runs` counts requests that returned a token.

### Agent session (conversation cache)

Cache hit over the follow-up turns: 69.6 % (69.6-69.6); earlier-turn tokens read again per session: 4 (1-5) (earlier turns that the cache did not hold; the 13731 (13731-13732) new tokens of the files are read once); first turn (nothing cached) first token: 0.59 s (0.55-0.66).

| Turn (run 1) | Prompt tokens | Reused | Read | Prompt tok/s | First token (s) | Decode tok/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 609 | 0 | 609 | 968.5 | 0.656 | 77.3 |
| 1 | 4106 | 635 | 3471 | 1272.8 | 2.751 | 93.3 |
| 2 | 5639 | 4169 | 1470 | 1211.0 | 1.236 | 93.2 |
| 3 | 9072 | 5703 | 3369 | 1253.9 | 2.709 | 86.7 |
| 4 | 11821 | 9113 | 2708 | 1243.9 | 2.2 | 87.7 |
| 5 | 14576 | 11859 | 2717 | 1243.8 | 2.207 | 86.9 |

Per-request data: `results.json`. Engine log lines from the start: `engine-log-excerpt.txt` (when this run started the server).

## Correctness and limitations

Not measured: this is a speed suite. The conversation uses the model's own replies, so token counts differ a little between runs. Machine details come from the server, `nvidia-smi`/`rocm-smi` and the OS; background load and power limits are not recorded. Total time of the suite: 276 s.
