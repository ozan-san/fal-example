"""Generate the Grafana dashboard JSON, so panels stay consistent and diffs stay readable.

Run: uv run python scripts/build_dashboard.py
"""

import json
from pathlib import Path

OUT = Path(__file__).parent.parent / "deploy/grafana/dashboards/fal-overview.json"
PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
TEMPO = {"type": "tempo", "uid": "tempo"}

# Fixed categorical order, so a given series keeps its color across panels.
BLUE, ORANGE, AQUA = "#3987e5", "#d95926", "#199e70"
# Status colors are kept for state and never used to tell series apart.
GOOD, WARN, CRITICAL = "#199e70", "#c98500", "#e66767"

SEL = 'fal_mode="$mode"'
ATTEMPTS = f'fal_client_requests_total{{{SEL},outcome=~"success|failure"}}'
SUCCESSES = f'fal_client_requests_total{{{SEL},outcome="success"}}'
RANGE = "$__range"


def target(expr, legend="", ref="A", exemplar=False, instant=False):
    return {
        "datasource": PROM,
        "expr": expr,
        "legendFormat": legend or "__auto",
        "refId": ref,
        "exemplar": exemplar,
        "instant": instant,
        "range": not instant,
    }


def quantiles(metric, extra=""):
    """p50 and p95 for one histogram: enough to see both typical and tail behavior."""
    sel = f"{SEL}{extra}"
    rate = f"sum by (le) (rate({metric}_bucket{{{sel}}}[$__rate_interval]))"
    return [
        target(f"histogram_quantile(0.5, {rate})", "p50", "A", exemplar=True),
        target(f"histogram_quantile(0.95, {rate})", "p95", "B", exemplar=True),
    ]


def overrides(colors):
    return [
        {
            "matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}],
        }
        for name, color in colors.items()
    ]


def stat(
    title,
    expr,
    grid,
    unit,
    description,
    thresholds=None,
    decimals=None,
    no_value="-",
    color_mode="value",
):
    steps = thresholds or [{"color": "text", "value": None}]
    defaults = {
        "unit": unit,
        "noValue": no_value,
        "color": {"mode": "thresholds"},
        "thresholds": {"mode": "absolute", "steps": steps},
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "stat",
        "title": title,
        "description": description,
        "datasource": PROM,
        "gridPos": grid,
        "targets": [target(expr, instant=True)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {
            "colorMode": color_mode,
            "graphMode": "none",
            "justifyMode": "auto",
            "textMode": "value",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
        },
    }


def timeseries(title, targets, grid, unit, description, colors=None, stacked=False, **custom):
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": PROM,
        "gridPos": grid,
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "min": 0,
                "custom": {
                    "lineWidth": 2,
                    "fillOpacity": 60 if stacked else 0,
                    "showPoints": "never",
                    "spanNulls": False,
                    "stacking": {"mode": "normal" if stacked else "none"},
                    "drawStyle": "bars" if stacked else "line",
                    **custom,
                },
            },
            "overrides": overrides(colors or {"p50": BLUE, "p95": ORANGE}),
        },
        "options": {
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def row(title, y):
    return {
        "type": "row",
        "title": title,
        "collapsed": False,
        "panels": [],
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
    }


def grid(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


panels = [
    row("Is it healthy?", 0),
    stat(
        "Success rate",
        f"sum(increase({SUCCESSES}[{RANGE}])) / sum(increase({ATTEMPTS}[{RANGE}]))",
        grid(0, 1, 5, 4),
        "percentunit",
        "Successful share of attempts in the selected range. SLO target: 99%. "
        "Budget-cap rejections are not counted.",
        [
            {"color": CRITICAL, "value": None},
            {"color": WARN, "value": 0.95},
            {"color": GOOD, "value": 0.99},
        ],
        decimals=1,
    ),
    stat(
        "Requests",
        f"sum(increase({ATTEMPTS}[{RANGE}]))",
        grid(5, 1, 4, 4),
        "short",
        "Attempts in the selected range.",
        decimals=0,
    ),
    stat(
        "p95 queue wait",
        "histogram_quantile(0.95, sum by (le) "
        f"(increase(fal_client_queue_duration_seconds_bucket{{{SEL}}}[{RANGE}])))",
        grid(9, 1, 5, 4),
        "s",
        "95th percentile wait from acceptance to processing start.",
        decimals=2,
    ),
    stat(
        "Estimated spend",
        f"sum(increase(fal_client_cost_usd_total{{{SEL}}}[{RANGE}]))",
        grid(14, 1, 5, 4),
        "currencyUSD",
        "Estimate from list price, rounded up to whole megapixels. In mock mode this is "
        "simulated. fal's billing page is the source of truth.",
        decimals=3,
    ),
    stat(
        "Live requests left",
        # min, not sum: after a restart the old instance's series lingers for a few minutes.
        f"min(fal_client_budget_remaining{{{SEL}}})",
        grid(19, 1, 5, 4),
        "short",
        "Remaining before the hard cap. Empty in mock mode, which has no cap. "
        "The FalRequestBudgetLow alert fires below 10.",
        decimals=0,
        color_mode="none",
    ),
    row("Where does the time go?", 5),
    timeseries(
        "Queue wait",
        quantiles("fal_client_queue_duration_seconds"),
        grid(0, 6, 8, 8),
        "s",
        "Accepted until processing started, measured by polling, so accurate to about the "
        "poll interval (0.2s). Exemplar dots link to traces.",
    ),
    timeseries(
        "Inference time",
        quantiles("fal_client_inference_duration_seconds"),
        grid(8, 6, 8, 8),
        "s",
        "Processing time as reported by fal in metrics.inference_time.",
    ),
    timeseries(
        "End-to-end latency",
        [
            *quantiles("fal_client_request_duration_seconds", ',outcome="success"'),
            target(
                "histogram_quantile(0.95, sum by (le) (rate("
                f"fal_client_local_wait_duration_seconds_bucket{{{SEL}}}[$__rate_interval])))",
                "p95 local wait",
                "C",
            ),
        ],
        grid(16, 6, 8, 8),
        "s",
        "Successful requests from this client's view. If local wait tracks the total, the "
        "app's own concurrency limit is the bottleneck, not fal.",
        {"p50": BLUE, "p95": ORANGE, "p95 local wait": AQUA},
    ),
    row("What is failing?", 14),
    timeseries(
        "Requests by outcome",
        [
            target(
                "sum by (outcome) (increase(fal_client_requests_total"
                f"{{{SEL}}}[$__rate_interval]))",
                "{{outcome}}",
            )
        ],
        grid(0, 15, 12, 8),
        "short",
        "success, failure, or rejected (stopped locally by the budget cap).",
        {"success": BLUE, "failure": ORANGE, "rejected": AQUA},
        stacked=True,
    ),
    timeseries(
        "Failures by type",
        [
            target(
                "sum by (error_type) (increase(fal_client_requests_total"
                f'{{{SEL},outcome="failure"}}[$__rate_interval]))',
                "{{error_type}}",
            )
        ],
        grid(12, 15, 12, 8),
        "short",
        "auth, rate_limited, client_error, server_error, model_error, timeout, network.",
        {},
        stacked=True,
    ),
    row("Investigate", 23),
    {
        "type": "table",
        "title": "Recent requests",
        "description": "Most recent traces from Tempo. Click a trace ID to see the queue and "
        "inference phases.",
        "datasource": TEMPO,
        "gridPos": grid(0, 24, 12, 10),
        "targets": [
            {
                "datasource": TEMPO,
                "queryType": "traceql",
                "query": '{ name = "fal.generate" && .fal.mode = "$mode" }',
                "limit": 20,
                "tableType": "traces",
                "refId": "A",
            }
        ],
    },
    {
        "type": "logs",
        "title": "Logs",
        "description": "Application logs from Loki. Expand a line and use View trace to open it.",
        "datasource": LOKI,
        "gridPos": grid(12, 24, 12, 10),
        "targets": [
            {
                "datasource": LOKI,
                "expr": '{service_name="fal-telemetry"}',
                "refId": "A",
            }
        ],
        "options": {
            "showTime": True,
            "wrapLogMessage": True,
            "enableLogDetails": True,
            "sortOrder": "Descending",
        },
    },
]

dashboard = {
    "uid": "fal-overview",
    "title": "fal API overview",
    "tags": ["fal", "slo"],
    "schemaVersion": 41,
    "editable": False,
    "refresh": "30s",
    "time": {"from": "now-1h", "to": "now"},
    "timezone": "browser",
    "templating": {
        "list": [
            {
                "name": "mode",
                "label": "Mode",
                "type": "query",
                "datasource": PROM,
                "query": {
                    "qryType": 1,
                    "query": "label_values(fal_client_requests_total, fal_mode)",
                    "refId": "mode",
                },
                "definition": "label_values(fal_client_requests_total, fal_mode)",
                "refresh": 2,
                "sort": 1,
                "includeAll": False,
                "multi": False,
            }
        ]
    },
    "panels": [{"id": i + 1, **panel} for i, panel in enumerate(panels)],
}

OUT.write_text(json.dumps(dashboard, indent=2) + "\n")
print(f"Wrote {OUT}")
