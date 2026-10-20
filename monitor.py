#!/usr/bin/env python3
"""Live Textual dashboard for Strata's local monitoring API.

Endpoints:
    GET /metrics -> Strata JSON metrics
    GET /status  -> current request status
    GET /slots   -> per-slot state

Design goals:
    - Prioritize inference state and throughput.
    - Status bar shows the model name (left) and engine version (right);
      the URL labels the request table.
    - Single column: a state strip (only the current state is colored), live
      counters, the reading-progress and context bars, then the hardware
      charts; each chart sits above its current value.
    - Slow-changing configuration sits greyed under the URL; the footnote
      carries the timeline.
    - Preserve missing telemetry as unavailable.
    - Keep the dashboard usable in smaller terminals.
    - Keep API polling asynchronous and keyboard navigation simple.
    - Show every piece of information exactly once.
    - Single built-in graphite palette with semantic threshold-based coloring.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static

SERIES_WINDOW = 60  # matches the server's 60-sample telemetry history
SPARKLINE_WIDTH = 40
LABEL_PAD = 20  # label column shared by the bars, charts and counters

# States shown in the strip; only the current one is colored.
STRIP_STATES = ("IDLE", "READING", "GENERATING", "QUEUED", "COMPLETED",
                "CANCELLED", "ERROR")
BASE_URL = os.environ.get("STRATA_URL", "http://127.0.0.1:8000").rstrip("/")
POLL_SECONDS = max(0.25, float(os.environ.get("STRATA_MONITOR_INTERVAL", "1.0")))
HTTP_TIMEOUT = max(0.1, float(os.environ.get("STRATA_MONITOR_TIMEOUT", "2.0")))


# ---------------------------------------------------------------------------
# Configurable thresholds & built-in semantic palette
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TelemetryThresholds:
    gpu_elevated: float = float(os.environ.get("STRATA_GPU_ELEVATED", "60.0"))
    gpu_high: float = float(os.environ.get("STRATA_GPU_HIGH", "85.0"))
    gpu_critical: float = float(os.environ.get("STRATA_GPU_CRITICAL", "95.0"))

    ctx_elevated: float = float(os.environ.get("STRATA_CTX_ELEVATED", "50.0"))
    ctx_near_cap: float = float(os.environ.get("STRATA_CTX_NEAR_CAP", "75.0"))
    ctx_critical: float = float(os.environ.get("STRATA_CTX_CRITICAL", "90.0"))

    cpu_high: float = float(os.environ.get("STRATA_CPU_HIGH", "80.0"))
    mem_high: float = float(os.environ.get("STRATA_MEM_HIGH", "85.0"))
    mem_critical: float = float(os.environ.get("STRATA_MEM_CRITICAL", "95.0"))


@dataclass(frozen=True)
class SemanticPalette:
    surface_bg: str
    panel_bg: str
    border_subtle: str
    border_focus: str
    header_bg: str
    text_primary: str
    text_secondary: str
    text_muted: str
    selection_bg: str

    status_connected: str
    status_disconnected: str
    status_starting: str
    status_loading: str
    status_idle: str
    status_busy: str
    status_error: str
    status_stale: str

    phase_queued: str
    phase_reading: str
    phase_generating: str
    phase_completed: str
    phase_cancelled: str
    phase_failed: str

    perf_decode: str
    perf_decode_last: str
    perf_prefill: str
    perf_latency: str
    perf_trend: str

    hw_gpu_low: str
    hw_gpu_elevated: str
    hw_gpu_high: str
    hw_gpu_critical: str
    hw_cpu: str
    hw_mem_normal: str
    hw_mem_high: str
    hw_mem_critical: str
    hw_storage: str

    ctx_normal: str
    ctx_elevated: str
    ctx_near_cap: str
    ctx_critical: str

    telemetry_missing: str


PALETTE = SemanticPalette(
    surface_bg="#121316",
    panel_bg="#1A1C20",
    border_subtle="#2A2E35",
    border_focus="#7DD3FC",
    header_bg="#16181D",
    text_primary="#E4E4E7",
    text_secondary="#A1A1AA",
    text_muted="#71717A",
    selection_bg="#27272A",
    status_connected="#4ADE80",
    status_disconnected="#F87171",
    status_starting="#FACC15",
    status_loading="#7DD3FC",
    status_idle="#4ADE80",
    status_busy="#FACC15",
    status_error="#F87171",
    status_stale="#FB923C",
    phase_queued="#FACC15",
    phase_reading="#7DD3FC",
    phase_generating="#4ADE80",
    phase_completed="#22C55E",
    phase_cancelled="#71717A",
    phase_failed="#EF4444",
    perf_decode="#38BDF8",
    perf_decode_last="#0284C7",
    perf_prefill="#C084FC",
    perf_latency="#A1A1AA",
    perf_trend="#38BDF8",
    hw_gpu_low="#A1A1AA",
    hw_gpu_elevated="#FBBF24",
    hw_gpu_high="#FB923C",
    hw_gpu_critical="#F43F5E",
    hw_cpu="#93C5FD",
    hw_mem_normal="#A1A1AA",
    hw_mem_high="#FBBF24",
    hw_mem_critical="#F87171",
    hw_storage="#D8B4FE",
    ctx_normal="#4ADE80",
    ctx_elevated="#7DD3FC",
    ctx_near_cap="#FBBF24",
    ctx_critical="#F87171",
    telemetry_missing="#52525B",
)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
MISSING = "—"


def number(value: Any, digits: int = 1) -> str:
    if value is None:
        return MISSING
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError, OverflowError):
        return MISSING


def integer(value: Any) -> str:
    if value is None:
        return MISSING
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError, OverflowError):
        return MISSING


def mib(value: Any) -> str:
    """Format byte-valued telemetry as MiB, preserving the original scale."""
    if value is None:
        return MISSING
    try:
        return f"{float(value) / (1024 ** 2):,.1f} MiB"
    except (TypeError, ValueError, OverflowError):
        return MISSING


def pct_float(used: Any, total: Any) -> float | None:
    try:
        used_f, total_f = float(used), float(total)
        if total_f <= 0:
            return None
        return min(100.0, 100.0 * used_f / total_f)
    except (TypeError, ValueError, OverflowError):
        return None


def pct(used: Any, total: Any) -> str:
    try:
        used_f, total_f = float(used), float(total)
        if total_f <= 0:
            return MISSING
        return f"{100 * used_f / total_f:.1f}%"
    except (TypeError, ValueError, OverflowError):
        return MISSING


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError, OverflowError):
        return default


def bar(value: Any, width: int = 24) -> str:
    try:
        n = max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError, OverflowError):
        return "—" * width

    filled = round(width * n / 100)
    return "█" * filled + "░" * (width - filled)


def sparkline(values: Any, width: int = 40) -> str:
    if not isinstance(values, (list, deque)):
        return MISSING

    nums: list[float] = []
    for value in values:
        try:
            if value is not None:
                nums.append(float(value))
        except (TypeError, ValueError, OverflowError):
            continue

    if not nums:
        return MISSING

    nums = nums[-width:]
    low, high = min(nums), max(nums)
    glyphs = "▁▂▃▄▅▆▇█"

    if high == low:
        return glyphs[0] * len(nums)

    scale = len(glyphs) - 1
    return "".join(
        glyphs[round((n - low) / (high - low) * scale)]
        for n in nums
    )


def local_time(timestamp: Any) -> str:
    try:
        return datetime.fromtimestamp(float(timestamp)).strftime("%H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return MISSING


# ---------------------------------------------------------------------------
# Semantic color tokens
# ---------------------------------------------------------------------------
def gpu_load_token(value: Any, thresholds: TelemetryThresholds) -> tuple[str, str, str]:
    try:
        n = float(value)
        if n < 0 or n != n:
            raise ValueError
    except (TypeError, ValueError):
        return PALETTE.telemetry_missing, "UNAVAIL", "○"

    if n >= thresholds.gpu_critical:
        return PALETTE.hw_gpu_critical, "MAX/CRIT", "▲"
    if n >= thresholds.gpu_high:
        return PALETTE.hw_gpu_high, "HIGH", "▴"
    if n >= thresholds.gpu_elevated:
        return PALETTE.hw_gpu_elevated, "ACTIVE", "◆"
    return PALETTE.hw_gpu_low, "NOMINAL", "◇"


def context_usage_token(pct_val: Any, thresholds: TelemetryThresholds) -> tuple[str, str, str]:
    try:
        n = float(pct_val)
        if n < 0 or n != n:
            raise ValueError
    except (TypeError, ValueError):
        return PALETTE.telemetry_missing, "IDLE", "○"

    if n >= thresholds.ctx_critical:
        return PALETTE.ctx_critical, "CRIT", "!"
    if n >= thresholds.ctx_near_cap:
        return PALETTE.ctx_near_cap, "NEAR", "▲"
    if n >= thresholds.ctx_elevated:
        return PALETTE.ctx_elevated, "ELEV", "◆"
    return PALETTE.ctx_normal, "NORM", "●"


def memory_usage_token(pct_val: Any, thresholds: TelemetryThresholds) -> str:
    try:
        n = float(pct_val)
    except (TypeError, ValueError):
        return PALETTE.telemetry_missing
    if n >= thresholds.mem_critical:
        return PALETTE.hw_mem_critical
    if n >= thresholds.mem_high:
        return PALETTE.hw_mem_high
    return PALETTE.hw_mem_normal


def phase_token(state: str) -> tuple[str, str]:
    st = state.upper()
    mapping = {
        "IDLE": (PALETTE.status_idle, "●"),
        "GENERATING": (PALETTE.phase_generating, "▶"),
        "READING": (PALETTE.phase_reading, "◈"),
        "QUEUED": (PALETTE.phase_queued, "◎"),
        "LOADING": (PALETTE.status_loading, "◌"),
        "STARTING": (PALETTE.status_starting, "◌"),
        "BUSY": (PALETTE.status_busy, "◈"),
        "COMPLETED": (PALETTE.phase_completed, "✓"),
        "CANCELLED": (PALETTE.phase_cancelled, "✕"),
        "STALE": (PALETTE.status_stale, "⚠"),
        "DISCONNECTED": (PALETTE.status_disconnected, "✕"),
        "API ERROR": (PALETTE.status_error, "!"),
        "ERROR": (PALETTE.phase_failed, "!"),
    }
    return mapping.get(st, (PALETTE.text_secondary, "?"))


# Display labels for finished requests: `stop`, `end_turn` and `completed`
# are the engine's words for "the reply ended by itself", which reads as DONE
# in a history table.
FINISH_LABELS = {"STOP": "DONE", "END_TURN": "DONE", "COMPLETED": "DONE"}


def finish_color(finish: Any) -> str:
    f = str(finish or "").strip().upper()
    if f in {"COMPLETED", "STOP", "END_TURN"}:
        return PALETTE.phase_completed
    if f in {"CANCELLED", "ABORTED"}:
        return PALETTE.phase_cancelled
    if f in {"FAILED", "ERROR"}:
        return PALETTE.phase_failed
    return PALETTE.text_secondary


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def fetch_json(path: str) -> Any:
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        headers={
            "Accept": "application/json",
            "User-Agent": "strata-tui-monitor/2.0",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError(f"HTTP {response.status}")
        return json.loads(response.read().decode("utf-8"))


async def fetch_all() -> tuple[dict, dict, list, list[str]]:
    async def one(path: str):
        try:
            result = await asyncio.to_thread(fetch_json, path)
            return path, result, None
        except Exception as exc:
            return path, None, (
                f"{path}: {type(exc).__name__}: {exc}"
            )

    results = await asyncio.gather(
        *(one(path) for path in ("/metrics", "/status", "/slots"))
    )

    values: dict[str, Any] = {}
    errors: list[str] = []

    for path, result, error in results:
        if error:
            errors.append(error)
        else:
            values[path] = result

    metrics = values.get("/metrics")
    status = values.get("/status")
    slots = values.get("/slots")

    return (
        metrics if isinstance(metrics, dict) else {},
        status if isinstance(status, dict) else {},
        slots if isinstance(slots, list) else [],
        errors,
    )


def get_state(metrics: dict, status: dict) -> str:
    if not metrics:
        return "API ERROR"

    live = metrics.get("live") or {}
    state = str(live.get("state") or "unknown").strip().upper()
    queued = safe_int(live.get("queued", status.get("queued", 0)))

    busy = status.get("busy")
    phase = str(status.get("phase") or live.get("phase") or "").lower()

    if state in {"READING", "PREFILL", "PROCESSING"}:
        state = "READING"
    elif state in {"GENERATING", "DECODING"}:
        state = "GENERATING"
    elif state in {"IDLE", "UNKNOWN", "NONE"}:
        if busy is True:
            if any(word in phase for word in ("read", "prefill", "prompt")):
                state = "READING"
            elif phase:
                state = "GENERATING"
            else:
                state = "BUSY"
        elif queued:
            state = "IDLE"
        elif state in {"UNKNOWN", "NONE"}:
            state = "IDLE"
    elif state in {"ERROR", "FAILED"}:
        state = "ERROR"

    return state


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
class StrataMonitor(App):
    TITLE = "MODEL"
    SUB_TITLE = ""  # engine version, once the first poll lands

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }

    #content {
        height: 1fr;
        padding: 0 1;
    }

    .panel {
        width: 1fr;
        height: auto;
        min-height: 20;
        margin-bottom: 1;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }

    #main {
        border-title-color: $success;
    }

    #requests-heading {
        height: 3;
        padding: 0 1;
        color: $text;
    }

    #requests {
        height: 12;
        min-height: 8;
        margin-bottom: 1;
        border: round $primary;
    }

    DataTable {
        height: 1fr;
    }

    #footnote {
        height: auto;
        min-height: 2;
        padding: 0 1;
        color: $text-muted;
    }

    Footer {
        background: $panel;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("escape", "quit", "Quit"),
        ("r", "refresh_now", "Refresh"),
    ]

    def __init__(
        self,
        thresholds: TelemetryThresholds | None = None,
    ) -> None:
        super().__init__()
        self.thresholds = thresholds or TelemetryThresholds()

        # Rolling per-poll samples for the hardware charts; the API's history
        # has no disk-write series, so the monitor keeps its own window.
        self.series: dict[str, deque] = {
            key: deque(maxlen=SERIES_WINDOW)
            for key in ("tok_s", "gpu_util", "gpu_mem_used",
                        "cpu", "ram_used", "disk_read_mb", "disk_write_mb")
        }

        self.last_ok = 0.0
        self.last_metrics: dict = {}
        self.last_status: dict = {}
        self.last_slots: list = []
        self.last_errors: list[str] = []
        self.polling = False

    def compose(self) -> ComposeResult:
        yield Header()

        with VerticalScroll(id="content"):
            yield Static(
                "CONNECTING  /  Waiting for Strata telemetry…",
                id="main",
                classes="panel",
            )

            yield Static(BASE_URL, id="requests-heading")
            yield DataTable(id="requests")
            yield Static("Initializing…", id="footnote")

        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#requests", DataTable)
        table.add_columns(
            "Time",
            "Duration",
            "Finish",
            "Prompt",
            "Output",
            "Decode tk/s",
        )
        table.cursor_type = "row"
        table.zebra_stripes = True

        self.apply_palette_styles()
        self.set_interval(POLL_SECONDS, self.refresh_data)
        self.run_worker(
            self.refresh_data(),
            group="poll",
            exclusive=True,
        )

    def apply_palette_styles(self) -> None:
        p = PALETTE
        self.screen.styles.background = p.surface_bg
        self.screen.styles.color = p.text_primary

        for selector in ("#main", "#requests"):
            widget = self.query_one(selector)
            widget.styles.background = p.panel_bg
            widget.styles.color = p.text_primary
            widget.styles.border = ("round", p.border_subtle)

        self.query_one("#main", Static).styles.border_title_color = p.status_idle
        self.query_one("#requests", DataTable).styles.border_title_color = p.text_secondary

        heading = self.query_one("#requests-heading", Static)
        heading.styles.background = p.surface_bg
        heading.styles.color = p.text_primary

        footnote = self.query_one("#footnote", Static)
        footnote.styles.background = p.surface_bg
        footnote.styles.color = p.text_muted

    def config_line(self, engine: dict, static: dict) -> str:
        """Slow-changing configuration, one greyed line under the URL."""
        return (
            f"HARDWARE {static.get('gpu_name') or MISSING}  ·  "
            f"KV {engine.get('kv', MISSING)}  ·  "
            f"Spec.MTP {engine.get('mtp_max', MISSING)}  ·  "
            f"Expert cache {number(engine.get('expert_cache_mib'))} MiB"
        )

    async def action_refresh_now(self) -> None:
        await self.refresh_data()

    async def refresh_data(self) -> None:
        if self.polling:
            return

        self.polling = True
        try:
            metrics, status, slots, errors = await fetch_all()

            if metrics:
                self.last_ok = time.time()
                self.last_metrics = metrics
                self.last_status = status
                self.last_slots = slots
                hw = metrics.get("hardware") or {}
                live = metrics.get("live") or {}
                for key, samples in self.series.items():
                    value = hw.get(key)
                    samples.append(value if value is not None else live.get(key))

            self.last_errors = errors
            self.render_dashboard()
        finally:
            self.polling = False

    def render_dashboard(self) -> None:
        p = PALETTE
        m = self.last_metrics
        s = self.last_status

        if not m:
            details = "\n".join(self.last_errors) or (
                f"Waiting for /metrics at {BASE_URL}"
            )
            self.query_one("#main", Static).update(
                f"[bold {p.status_error}]API UNAVAILABLE[/]\n"
                f"[{p.text_secondary}]{details}[/]"
            )
            self.query_one("#requests-heading", Static).update(
                f"[bold]{BASE_URL}[/]\n[{p.text_muted}]{self.config_line({}, {})}[/]"
            )
            self.query_one("#footnote", Static).update(
                f"[{p.text_muted}]No successful metrics snapshot received.[/]"
            )
            return

        live = m.get("live") or {}
        engine = m.get("engine") or {}
        hw = m.get("hardware") or {}
        static = m.get("hardware_static") or {}
        requests = m.get("requests") or []

        # Status bar: model name on the left, engine version on the right.
        # Reactives deduplicate, so per-poll assignment is cheap.
        self.title = f"MODEL  {engine.get('model') or MISSING}"
        self.sub_title = str(engine.get("version", engine.get("engine", MISSING)))

        state = get_state(m, s)
        active = state in {"READING", "GENERATING", "BUSY"}

        # State strip: every state greyed out, only the current one colored.
        strip_states = list(STRIP_STATES)
        if state not in strip_states:
            strip_states.append(state)
        strip = "  ".join(
            f"[bold {c}]{sym} {name}[/]" if name == state
            else f"[{p.text_muted}]{sym} {name}[/]"
            for name in strip_states
            for c, sym in (phase_token(name),)
        )

        queued = live.get("queued")
        if queued is None:
            queued = s.get("queued")

        last_req = requests[0] if requests and isinstance(requests[0], dict) else {}

        speed = live.get("tok_s") if active else None
        if speed is None and active:
            speed = hw.get("tok_s")
        if speed is None:
            speed = last_req.get("decode_tok_s")
        if speed is None:
            speed_color = p.telemetry_missing
        elif active:
            speed_color = p.perf_decode
        else:
            speed_color = p.perf_decode_last

        elapsed = live.get("elapsed_s") if active else None
        generated = (
            live.get("generated")
            if active
            else last_req.get("output_tokens")
        )

        prefill = live.get("prefill_tok_s_mean") if active else None
        if prefill is None and not active:
            prompt_ms = last_req.get("prompt_ms")
            prompt_tokens = last_req.get("prompt_tokens")
            try:
                if prompt_ms and prompt_tokens is not None:
                    prefill = float(prompt_tokens) / (
                        float(prompt_ms) / 1000
                    )
            except (TypeError, ValueError, ZeroDivisionError):
                prefill = None
        prefill_color = p.perf_prefill if prefill is not None else p.telemetry_missing

        last_success = datetime.fromtimestamp(
            self.last_ok
        ).strftime("%H:%M:%S")

        age = max(0, time.time() - self.last_ok)
        fresh = age <= max(3 * POLL_SECONDS, 3)
        freshness_color = p.status_connected if fresh else p.status_stale

        def chart(label: str, series_key: str, color: str, value: str) -> str:
            return (
                f"{label:<{LABEL_PAD}}[{color}]{sparkline(self.series[series_key], SPARKLINE_WIDTH)}[/]\n"
                f"{' ' * LABEL_PAD}{value}"
            )

        # Prefill speed: the bar shows prompt-reading progress (absorbing the
        # old Prompt progress line); the value line carries the rate plus the
        # actual progress numbers. A finished request read its whole prompt,
        # so idle shows that completed bar instead of an empty one.
        prompt_read = live.get("prompt_read")
        prompt_total = live.get("prompt_total")
        if not active:
            if prompt_read is None:
                prompt_read = last_req.get("prompt_tokens")
            if prompt_total is None:
                prompt_total = last_req.get("prompt_tokens")
        progress_pct = pct_float(prompt_read, prompt_total)

        if progress_pct is None:
            progress_line = (
                f"Prefill speed     [{p.telemetry_missing}]{bar(None, 20)}[/]\n"
                f"{' ' * LABEL_PAD}[{prefill_color}]{number(prefill)} tk/s[/]  ·  "
                f"[{p.text_muted}]no reading progress[/]"
            )
        else:
            progress_line = (
                f"Prefill speed     [{p.phase_reading}]{bar(progress_pct, 20)}[/]\n"
                f"{' ' * LABEL_PAD}[{prefill_color}]{number(prefill)} tk/s[/]  ·  "
                f"[{p.phase_reading}]{integer(prompt_read)} / {integer(prompt_total)} "
                f"({progress_pct:.1f}%)[/]"
            )

        # Context gauge: prefers live counters and falls back to the last
        # request so it stays populated when idle.
        ctx_prompt = live.get("prompt_tokens")
        if ctx_prompt is None:
            ctx_prompt = live.get("prompt_total")
        ctx_generated = live.get("generated")
        if ctx_prompt is None:
            ctx_prompt = last_req.get("prompt_tokens")
        if ctx_generated is None:
            ctx_generated = last_req.get("output_tokens")

        ctx_limit = engine.get("max_context", engine.get("context"))
        ctx_used = None
        if ctx_prompt is not None:
            ctx_used = safe_int(ctx_prompt) + safe_int(ctx_generated)
        ctx_pct = pct_float(ctx_used, ctx_limit)

        if ctx_pct is None:
            context_line = f"Context usage     [{p.telemetry_missing}]unavailable[/]"
        else:
            ctx_color, ctx_badge, ctx_symbol = context_usage_token(
                ctx_pct, self.thresholds
            )
            context_line = (
                f"Context usage     [{ctx_color}]{bar(ctx_pct, 20)} "
                f"{ctx_pct:.1f}% {ctx_symbol} {ctx_badge}[/]\n"
                f"{' ' * LABEL_PAD}[{p.text_primary}]{integer(ctx_used)} / "
                f"{integer(ctx_limit)} tokens[/]"
            )

        gpu_used = hw.get("gpu_mem_used")
        gpu_total = hw.get("gpu_mem_total")
        ram_used = hw.get("ram_used")
        ram_total = hw.get("ram_total")

        gpu_color = gpu_load_token(hw.get("gpu_util"), self.thresholds)[0]
        mem_color = memory_usage_token(
            pct_float(ram_used, ram_total), self.thresholds
        )
        cpu_val = hw.get("cpu")
        cpu_color = (
            p.hw_gpu_high
            if isinstance(cpu_val, (int, float))
            and cpu_val == cpu_val
            and cpu_val >= self.thresholds.cpu_high
            else p.hw_cpu
        )

        # Single merged column: state strip, counters, the two bars, then the
        # hardware charts. Slow-changing config lives under the URL.
        self.query_one("#requests-heading", Static).update(
            f"[bold]{BASE_URL}[/]\n[{p.text_muted}]{self.config_line(engine, static)}[/]"
        )

        main_text = (
            f"{strip}\n\n"
            f"Q.Depth [{p.text_primary}]{integer(queued)}[/]  ·  "
            f"tks [{p.text_primary}]{integer(generated)}[/]  ·  "
            f"Elapsed [{p.text_primary}]{number(elapsed)} s[/]\n\n"
            f"{progress_line}\n\n"
            f"{context_line}\n\n"
            + chart("Decode tk/s", "tok_s", speed_color,
                    f"[{speed_color}]{number(speed)} tk/s[/]") + "\n"
            + chart("GPU util", "gpu_util", gpu_color,
                    f"[{gpu_color}]{number(hw.get('gpu_util'), 0)}%[/]") + "\n"
            + chart("GPU memory", "gpu_mem_used", p.hw_storage,
                    f"[{p.text_primary}]{mib(gpu_used)} / {mib(gpu_total)}[/]  "
                    f"[{gpu_color}]({pct(gpu_used, gpu_total)})[/]") + "\n"
            + chart("CPU", "cpu", p.hw_cpu,
                    f"[{cpu_color}]{number(cpu_val)}%[/]") + "\n"
            + chart("RAM", "ram_used", mem_color,
                    f"[{mem_color}]{mib(ram_used)} / {mib(ram_total)}[/]  "
                    f"[{mem_color}]({pct(ram_used, ram_total)})[/]") + "\n"
            + chart("Disk read", "disk_read_mb", p.hw_storage,
                    f"[{p.hw_storage}]{number(hw.get('disk_read_mb'), 2)} MB[/]") + "\n"
            + chart("Disk write", "disk_write_mb", p.hw_storage,
                    f"[{p.hw_storage}]{number(hw.get('disk_write_mb'), 2)} MB[/]")
        )
        self.query_one("#main", Static).update(main_text)

        # Request history
        table = self.query_one("#requests", DataTable)
        table.clear()

        for req in requests[:8]:
            if not isinstance(req, dict):
                continue

            finish = str(req.get("finish") or MISSING)
            finish_label = FINISH_LABELS.get(finish.strip().upper(), finish)
            table.add_row(
                local_time(req.get("time")),
                f"{number(req.get('duration_s'))} s",
                f"[{finish_color(finish)}]{finish_label}[/]",
                integer(req.get("prompt_tokens")),
                integer(req.get("output_tokens")),
                number(req.get("decode_tok_s")),
            )

        if not table.row_count:
            table.add_row(
                MISSING,
                MISSING,
                "No history",
                MISSING,
                MISSING,
                MISSING,
            )

        # Footer: the timeline lives here; the strip above only carries state.
        age_text = f"{age:.1f}s ago" if fresh else f"STALE · {age:.0f}s ago"
        conn_color = p.status_stale if self.last_errors else p.status_connected
        footer_note = (
            f"[{conn_color}]●[/] Last successful metrics: {last_success}"
            f"  ·  [{freshness_color}]{age_text}[/]"
            f"  ·  Poll interval: {POLL_SECONDS:g}s"
        )

        if self.last_errors:
            footer_note += (
                f"\n[bold {p.status_busy}]Partial endpoint errors:[/] "
                + "  ·  ".join(self.last_errors)
            )

        self.query_one("#footnote", Static).update(footer_note)


if __name__ == "__main__":
    StrataMonitor().run()
