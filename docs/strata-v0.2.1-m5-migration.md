# Strata v0.2.1-m5 Integration & Migration Guide — Apple Silicon M5 Max 128 GB

## 1. Overview & Mission Objective

This document outlines the integration, migration, configuration, and operational guidelines for **Strata v0.2.1-m5**, an Apple Silicon-optimized build targeting Apple M5 Max hardware with 128 GB unified memory running macOS 27 Golden Gate.

The goal of this release is to combine upstream Strata v0.2.1 process infrastructure improvements with critical local optimizations (GPU-accelerated Metal RoPE/YaRN scaling, Apple Mach VM statistics, and local API-key authentication).

---

## 2. Integration Classification Matrix

| Component / Feature | Original Fork (HEAD) | Upstream v0.2.1 | Integrated v0.2.1-m5 | Status / Category | Validation Level |
|---|---|---|---|---|---|
| **Metal RoPE / YaRN Scaling** | GPU scaling (`window.cpp`) | Reverted to fixed RoPE | Retained local GPU scaling (`window.cpp`, `setup_host.cpp`) | **Preserved** | Unit test (`test_setup_rope.py`) |
| **macOS Mach VM Stats** | Mach VM sampling | Linux/Windows sysfs only | Retained Mach VM sampling (`conversation_memory.cpp`) | **Preserved** | Unit test (`test_setup_mac.py`) |
| **API Key Authentication** | Local API key check | Standard middleware | Preserved local auth in scripts/middleware; added secure `key status/set/rotate` workflow | **Preserved & Extended** | Script / Inspection / Unit tests |
| **Process Lifeline Supervision** | Basic PID tracking | `lifeline.py` & `owner_guard.py` | Integrated `serve/lifeline.py`, `ready.py`, `launch_identity.py` | **Adopted** | Import / Mechanism check |
| **Out-of-Tree Metal Locator** | Hardcoded compiled paths | `resource_path.h` (`strata_metal::resource`) | Integrated `resource_path.h` & `mtl_rt.mm` | **Adopted** | Build & Code Inspection |
| **Standalone Vision Tool** | In-process script | Standalone CMake tool (`tools/vision/`) | Integrated `tools/vision/` CMake + sources | **Adopted** | Code Inspection |
| **Unified CLI & Model Management** | `switch.sh` standalone menu | Subcommands only (error on 0-arg) | Integrated shared model manager (`model_manager.py`), interactive zero-arg TTY menu, `strata switch` and `strata key` CLI | **Integrated & Unified** | Unit test (`test_unified_cli.py`) & Runtime validation |
| **Monitor TUI Models View** | 6 views (Overview..Logs) | 6 views (+ Connect in launch) | Extended to 7 core views (+ Models & Control view with live switching) | **Extended** | Unit test & TUI Inspection |
| **Desktop Control Mode** | CLI / API Direct | `strata_control/` | Deferred (not required for CLI/API engine) | **Deferred** | N/A |

---

## 3. Key Local Modifications Retained

1. **GPU-Accelerated Metal RoPE / YaRN Scaling:**
   - Retained `build_rope_table_interleaved` dispatch in `metal/src/engine/window.cpp` (lines 490–500) and `metal/src/engine/setup_host.cpp` (lines 114–149).
   - Guarantees bit-exact frequency scaling and YaRN ramp correction directly on Metal for contexts exceeding 32k tokens.

2. **Apple Silicon Mach VM Memory Sampling:**
   - Retained Mach VM host sampling (`sysctlbyname("hw.memsize")`, `host_statistics64`) in `src/core/conversation_memory.cpp`.
   - Accurately reports host RAM availability to non-Metal memory guards.

3. **Local API-Key Authentication:**
   - Preserved API-key header validation (`Authorization: Bearer <key>` / `x-api-key`) across `switch.sh` and server monitoring utilities.

---

## 4. Upstream Improvements Integrated

1. **Process Lifeline Supervision (`serve/lifeline.py`, `serve/owner_guard.py`):**
   - Automatically cleans up child Metal C++ engine processes upon server restart, termination, or unexpected crash, eliminating orphaned GPU memory allocations on Apple Silicon.

2. **Relocatable Metal Resource Locator (`metal/src/resource_path.h`):**
   - Dynamically resolves engine MSL kernels and resource paths using `strata_metal::resource()`, supporting relocatable executable layouts.

3. **Updated Standalone Vision Tool (`tools/vision/`):**
   - Updated CMake build definitions (`tools/vision/CMakeLists.txt`), `gguf_meta.h`, `gpu_arch.h`, and `strata_vision.cpp`.

---

## 5. Recommended Configuration for M5 Max (128 GB Unified Memory)

For maximum inference throughput and long-context stability on an M5 Max (128 GB):

- **Context Window:** Up to 65,536 tokens (`--max-context 65536`).
- **Arena Keep-Free Buffer:** Defaults to `max(7.0 GB, 0.15 * 128) = 19.2 GB` (`STRATA_METAL_KEEP_FREE_GB=19.2`).
- **Engine Working Set Cap:** ~108.8 GB available for engine resident allocations and KV cache.
- **Speculative Draft Layer:** Enabled (`--spec 2` / `--mtp`) for models supporting MTP.

---

## 6. Build, Setup, and Launch Instructions

### Building the Metal Engine
```bash
python3 metal/setup_mac.py --build --check
```

### Running Validation Tests
```bash
python3 tools/test_setup_mac.py
python3 tools/test_setup_rope.py
```

### Launching the Server
```bash
STRATA_KEY="your-api-key" python3 serve/server.py --engine strata --config strata-iq3_s.json --port 8080
```

---

## 7. Rollback Instructions

If a rollback to the original fork baseline is required:

```bash
git checkout main
git branch -D v0.2.1-m5
```

---

## 8. Summary of Validation Executed

1. **Level 1 (Static Inspection):** Verified code diffs across Metal RoPE, Mach statistics, process supervision modules, and unified CLI dispatchers.
2. **Level 2 (Unit Tests):**
   - Executed `tools/test_setup_mac.py` (25/25 passed).
   - Executed `tools/test_setup_rope.py` (19/19 passed).
   - Executed `tools/test_unified_cli.py` (13/13 passed: connection precedence, credential fallback, process guards, TTY zero-arg behavior, API-key masking, and subcommands).
3. **Level 3 (Process Supervision Verification):** Validated Python module loading (`serve.lifeline`) and lifeline mechanism resolution (`pdeathsig`).
4. **Level 4 (Hardware Runtime Validation):**
   - Launched Swift 1.5 Abliterated IQ3_S via `strata switch swift` on Apple Silicon Metal; verified ready state and authenticated telemetry on port 8000.
   - Tested zero-flag monitoring: `strata monitor --once` cleanly connected to the running instance without `--url` or `--credential-env`.
   - Launched Qwen 3.8 Flash Next IQ3_S via `strata switch qwen`; verified automatic graceful shutdown of Swift and clean startup of Qwen on port 8000.
   - Tested `strata switch stop` and verified graceful termination via SIGTERM, process reclamation, and port freeing.

---

## 9. Unified CLI, Model Management & Monitoring Architecture

### 9.1 Interactive Entry Point (`strata`)
- **Interactive TTY:** Typing `strata` with no arguments in a terminal launches the interactive model management menu (`model_manager.interactive_menu()`), offering model start/switch, status, graceful stop, monitor launch, and API key management.
- **Non-Interactive / Scripting:** Running `strata` without arguments in a non-TTY environment displays the canonical help/usage message and exits with status 2, ensuring standard CLI behavior.
- **Subcommands Retained:** All existing subcommands (`strata serve`, `strata monitor`, `strata status`, `strata doctor`, `strata models`, `strata bench`, etc.) remain fully functional.

### 9.2 Shared Model Manager (`strata_cli/model_manager.py`)
- Provides shared process supervision and lifecycle control for Swift 1.5 Abliterated (`strata-iq3_s-512k.json`) and Qwen 3.8 Flash Next (`strata-iq3_s.json`).
- Dynamically inspects context size (`--max-context`), vision support, and ports without hardcoded capabilities.
- Enforces graceful termination (SIGTERM followed by verified cleanup before SIGKILL escalation).
- Never terminates unrelated processes that happen to occupy port 8000; verifies process command lines against Strata signatures.

### 9.3 Deterministic Connection & Credential Fallback (`strata_cli/connections.py`)
- **URL Precedence:** Explicit `--url` flag > saved `--connection` > `STRATA_URL` environment > local instance discovery (probing ports 8000 and 8095) > local config fallback (`http://127.0.0.1:8000`).
- **Credential Precedence:** Explicit `--credential-env` flag > saved connection > `STRATA_KEY` / `STRATA_API_KEY` in environment > config file fallback.
- Detects multiple-instance ambiguity and distinguishes unauthenticated, initializing, and unrelated HTTP endpoints.

### 9.4 Monitoring TUI Integration (`strata_cli/monitor.py` & `render.py`)
- Added core view 7 (`Models` / `models`) to the terminal monitor.
- Displays live server status, loaded model identity, context tokens, unified memory utilization, and available model profiles.
- Provides interactive keyboard shortcuts (`s` for Swift, `w` for Qwen, `x` for stop, `r` for refresh) that invoke the shared `model_manager`.

### 9.5 Secure API Key Management
- `strata key status`: Inspects environment variables and run configurations. Never prints secrets; displays masked tokens with character length and verifies active server authentication.
- `strata key set <key>`: Atomically updates configuration files and alerts if a server restart is required.
- `strata key rotate`: Generates a cryptographically secure 256-bit hex token and updates configurations.
