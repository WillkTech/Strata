# Community benchmark on Apple M5 Max

Measured on 2026-10-22 with `tools/strata_bench.py` (suite 1, 5 runs per test, medians with ranges). Speeds are the engine's own timings; first-token times are measured by the client over HTTP. Speed only: no correctness check was run.

## Hardware and software

- GPU and VRAM: Apple M5 Max, 122 GiB VRAM
- CPU: arm (18 cores, 18 threads); installed RAM: 128.0 GiB; storage: not measured
- OS: Darwin 27.0.0; GPU driver: not read; CUDA: not read
- Strata: project 0.1.47, commit 011c9e60; engine 0.1.46-metal
- Background workloads and power limits: not recorded

## Model and configuration

- Model and quantization: Swift-1.5-Qwen3.8-Flash-Next-Abliterated-IQ3_S
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
| short chat | 46 (43-51) | 0 | 100 (100-100) | 15 | not measured | 95.2 (91.1-111.1) | 0.27 (0.25-0.30) |
| 4K prompt, fresh | 4019 (4019-4019) | 0 (0-0) | 24 cap | 5 | 1659.2 (1446.4-1680.5) | not measured | 2.44 (2.41-2.80) |
| 16K prompt, fresh | 16058 (16058-16058) | 0 (0-0) | 24 cap | 5 | 1530.6 (1511.6-1553.9) | not measured | 10.52 (10.36-10.65) |
| 32K prompt, fresh | 32049 (32049-32049) | 0 (0-0) | 24 cap | 5 | 1509.2 (1509.2-1521.7) | not measured | 21.26 (21.09-21.27) |
| agent session, follow-up turns | 14615 (14543-14625) at the last turn | see below | up to 64 | 5 | see below | 90.5 (74.5-100.2) | 2.14 (1.11-2.63) |

Ranges are the lowest and highest of the runs; `Runs` counts requests that returned a token.

### Agent session (conversation cache)

Cache hit over the follow-up turns: 69.7 % (69.6-69.7); earlier-turn tokens read again per session: 3 (1-4) (earlier turns that the cache did not hold; the 13732 (13730-13733) new tokens of the files are read once); first turn (nothing cached) first token: 0.55 s (0.54-0.56).

| Turn (run 1) | Prompt tokens | Reused | Read | Prompt tok/s | First token (s) | Decode tok/s |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 609 | 0 | 609 | 1146.5 | 0.551 | 94.5 |
| 1 | 4093 | 622 | 3471 | 1329.8 | 2.633 | 94.6 |
| 2 | 5626 | 4157 | 1469 | 1281.3 | 1.169 | 90.2 |
| 3 | 9045 | 5676 | 3369 | 1327.7 | 2.56 | 82.5 |
| 4 | 11798 | 9090 | 2708 | 1281.2 | 2.145 | 96.3 |
| 5 | 14543 | 11826 | 2717 | 1232.5 | 2.228 | 86.2 |

Per-request data: `results.json`. Engine log lines from the start: `engine-log-excerpt.txt` (when this run started the server).

## Correctness and limitations

Not measured: this is a speed suite. The conversation uses the model's own replies, so token counts differ a little between runs. Machine details come from the server, `nvidia-smi`/`rocm-smi` and the OS; background load and power limits are not recorded. Total time of the suite: 269 s.
