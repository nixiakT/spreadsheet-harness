#!/usr/bin/env python3
"""Generate paper-ready figures from audited paper_fig_data artifacts.

The script deliberately keeps formal results, the 24-family pilot, and
validation/search dynamics in separate figures. Missing values are omitted;
they are never coerced to zero.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "paper_figures" / "20260918"
OUT.mkdir(parents=True, exist_ok=True)

COLORS = {
    "basic": "#4C78A8",
    "financial": "#F58518",
    "fin269": "#4C78A8",
    "fin15k": "#E45756",
    "accept": "#2A9D8F",
    "reject": "#D1495B",
    "h": "#6A4C93",
    "d": "#F2A900",
}


def read_csv(name: str) -> list[dict[str, str]]:
    with (ROOT / "paper_fig_data" / name).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save(fig: plt.Figure, stem: str) -> None:
    if stem == "fig5_evolution_gate_trajectory":
        fig.subplots_adjust(left=0.09, right=0.99, bottom=0.38, top=0.86)
    elif stem == "fig6_accuracy_cost_tradeoff":
        fig.subplots_adjust(left=0.10, right=0.98, bottom=0.22, top=0.88)
    else:
        fig.tight_layout()
    for ext in ("pdf", "png", "svg"):
        fig.savefig(OUT / f"{stem}.{ext}", dpi=320, bbox_inches="tight")
    plt.close(fig)


def paper_style() -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Serif",
            "font.size": 9,
            "axes.titlesize": 11,
            "axes.labelsize": 9,
            "xtick.labelsize": 8,
            "ytick.labelsize": 8,
            "legend.fontsize": 8,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.alpha": 0.22,
            "grid.linewidth": 0.6,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
            "svg.fonttype": "none",
        }
    )


def figure_category_gain() -> None:
    rows = read_csv("static_plugin_gains.csv")
    categories = ["Overall", "Template", "Financial Modeling", "Debugging"]
    display = {
        "Overall": "Overall",
        "Template": "Template",
        "Financial Modeling": "Financial\nModeling",
        "Debugging": "Debugging",
    }
    data: dict[str, dict[str, dict[str, float]]] = {m: {} for m in ("Exact", "Modification")}
    for r in rows:
        if (
            r["backbone"] == "DeepSeek-V4-Flash"
            and r["benchmark"] == "SpreadsheetBench v2"
            and r["method"] in ("SHEETHARNESS-BASIC", "SHEETHARNESS-FINANCIAL")
            and r["metric"] in data
            and r["score"]
        ):
            data[r["metric"]].setdefault(r["category"], {})[r["method"]] = float(r["score"])

    fig, axes = plt.subplots(1, 2, figsize=(7.1, 3.0), sharey=False)
    x = np.arange(len(categories))
    width = 0.36
    for ax, metric in zip(axes, ("Exact", "Modification"), strict=True):
        basic = [data[metric][c]["SHEETHARNESS-BASIC"] for c in categories]
        financial = [data[metric][c]["SHEETHARNESS-FINANCIAL"] for c in categories]
        ax.bar(x - width / 2, basic, width, color=COLORS["basic"], label="Basic")
        ax.bar(x + width / 2, financial, width, color=COLORS["financial"], label="Financial")
        ax.set_xticks(x, [display[c] for c in categories])
        ax.set_ylabel(f"{metric} (%)")
        ax.set_title(metric)
        ax.set_ylim(0, max(max(basic), max(financial)) * 1.22)
        for vals, offset in ((basic, -width / 2), (financial, width / 2)):
            for i, v in enumerate(vals):
                ax.text(i + offset, v + 0.7, f"{v:.1f}", ha="center", va="bottom", fontsize=7)
    axes[0].legend(frameon=False, loc="upper left")
    fig.suptitle("Domain-plugin gains are concentrated in Financial Modeling", y=1.02)
    save(fig, "fig1_category_plugin_gain")


def load_pilot() -> dict:
    p = ROOT / "benchmarks" / "results" / "paper24-heldout-pilot-qwen36plus-20260917" / "report.json"
    with p.open(encoding="utf-8") as f:
        return json.load(f)


def figure_pilot_ts() -> None:
    report = load_pilot()
    arms = ["initial", "general_only", "domain_only", "alternating"]
    labels = ["Initialization", "General-only", "Domain-only", "Alternating"]
    suites = [("Fin-269", COLORS["fin269"]), ("Fin-1.5K", COLORS["fin15k"])]
    x = np.arange(len(arms))
    width = 0.35
    fig, ax = plt.subplots(figsize=(7.0, 3.4))
    for j, (suite, color) in enumerate(suites):
        means = [100 * report["arms"][a][suite]["accuracy"]["mean"] for a in arms]
        lows = [100 * (report["arms"][a][suite]["accuracy"]["mean"] - report["arms"][a][suite]["accuracy"]["family_bootstrap_95"][0]) for a in arms]
        highs = [100 * (report["arms"][a][suite]["accuracy"]["family_bootstrap_95"][1] - report["arms"][a][suite]["accuracy"]["mean"]) for a in arms]
        ax.bar(x + (j - 0.5) * width, means, width, color=color, label=suite, yerr=[lows, highs], capsize=3, error_kw={"elinewidth": 0.8})
        for i, v in enumerate(means):
            ax.text(i + (j - 0.5) * width, v + 1.2, f"{v:.1f}", ha="center", fontsize=7)
    ax.set_xticks(x, labels)
    ax.set_ylabel("Task Success (%)")
    ax.set_ylim(0, 60)
    ax.set_title("24-family held-out pilot: exact task success")
    ax.legend(frameon=False, ncol=2)
    ax.text(0.0, -0.24, "Bars: family mean; error bars: family-bootstrap 95% CI; pilot, not full-suite evaluation.", transform=ax.transAxes, fontsize=7)
    save(fig, "fig2_pilot_task_success")


def figure_pilot_interaction() -> None:
    report = load_pilot()
    arms = ["initial", "alt_h_only", "alt_d_only", "alternating"]
    labels = ["C00", "C10", "C01", "C11"]
    suites = [("Fin-269", COLORS["fin269"]), ("Fin-1.5K", COLORS["fin15k"])]
    x = np.arange(len(arms))
    width = 0.35
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(7.1, 3.1), gridspec_kw={"width_ratios": [2.0, 1.0]})
    for j, (suite, color) in enumerate(suites):
        vals = [100 * report["arms"][a][suite]["accuracy"]["mean"] for a in arms]
        ax.bar(x + (j - 0.5) * width, vals, width, color=color, label=suite)
    ax.set_xticks(x, labels)
    ax.set_ylabel("Task Success (%)")
    ax.set_ylim(0, 60)
    ax.set_title("Crossed endpoint compositions")
    ax.legend(frameon=False, fontsize=7)
    interaction = {"Fin-269": (8.3, 0.0, 25.0), "Fin-1.5K": (0.0, -25.0, 25.0)}
    sx = np.arange(2)
    vals = [interaction[s][0] for s, _ in suites]
    lows = [vals[i] - interaction[s][1] for i, (s, _) in enumerate(suites)]
    highs = [interaction[s][2] - vals[i] for i, (s, _) in enumerate(suites)]
    ax2.errorbar(sx, vals, yerr=[lows, highs], fmt="o", color="#222222", capsize=4, markersize=5)
    ax2.axhline(0, color="#666666", linewidth=0.8)
    ax2.set_xticks(sx, ["Fin-269", "Fin-1.5K"], rotation=30, ha="right")
    ax2.set_ylabel("Interaction I (pp)")
    ax2.set_title("Difference-in-differences")
    ax2.set_ylim(-35, 35)
    fig.suptitle("Pilot endpoint interaction", y=1.03)
    save(fig, "fig3_pilot_interaction")


def figure_plugin_heatmap() -> None:
    rows = read_csv("plugin_usage_heatmap.csv")
    cats = ["Template", "Financial Modeling", "Debugging", "Visualization"]
    selected = [r for r in rows if r["backbone"] == "DeepSeek-V4-Flash" and r["method"] == "SHEETHARNESS-FINANCIAL"]
    plugins = []
    for r in selected:
        if r["plugin_name"] not in plugins:
            plugins.append(r["plugin_name"])
    plugins.sort(key=lambda p: (next(r["plugin_group"] for r in selected if r["plugin_name"] == p), p))
    matrix = np.full((len(plugins), len(cats)), np.nan)
    for i, p in enumerate(plugins):
        for j, c in enumerate(cats):
            match = [r for r in selected if r["plugin_name"] == p and r["category"] == c]
            if match and match[0]["task_activation_rate"]:
                matrix[i, j] = float(match[0]["task_activation_rate"])
    fig, ax = plt.subplots(figsize=(6.8, 5.4))
    im = ax.imshow(matrix, cmap="YlOrRd", vmin=0, vmax=100, aspect="auto")
    ax.set_xticks(range(len(cats)), ["Template", "Financial\nModeling", "Debugging", "Visualization"])
    short = {
        "act-code-plus-formula-validation": "act: code + formula validation",
        "observe-profile-compact": "observe: compact profile",
        "control-ours": "control: ours policy",
        "knowledge-structure": "knowledge: structure",
        "knowledge-formula": "knowledge: formula",
        "knowledge-manipulation": "knowledge: manipulation",
        "knowledge-analysis": "knowledge: analysis",
        "knowledge-visualization": "knowledge: visualization",
        "knowledge-verification": "knowledge: verification",
        "knowledge-memory": "knowledge: memory",
        "verify-formula-runtime": "verify: formula runtime",
        "repair-date-text": "date-text repair",
        "knowledge-financial-model": "knowledge: financial model",
        # Legacy CSV snapshots remain renderable during the naming migration.
        "runtime-code-plus-formula-validation": "act: code + formula validation",
        "profile-deterministic-compact": "observe: compact profile",
        "policy-ours": "control: ours policy",
        "skill-spreadsheet-structure": "knowledge: structure",
        "skill-spreadsheet-formula": "knowledge: formula",
        "skill-spreadsheet-manipulation": "knowledge: manipulation",
        "skill-spreadsheet-analysis": "knowledge: analysis",
        "skill-spreadsheet-visualization": "knowledge: visualization",
        "skill-spreadsheet-verification": "knowledge: verification",
        "skill-spreadsheet-memory": "knowledge: memory",
        "verifier-formula-runtime": "verify: formula runtime",
        "skill-spreadsheet-financial-model": "knowledge: financial model",
    }
    ax.set_yticks(range(len(plugins)), [short.get(p, p) for p in plugins])
    for i in range(len(plugins)):
        for j in range(len(cats)):
            if not np.isnan(matrix[i, j]):
                ax.text(j, i, f"{matrix[i,j]:.0f}", ha="center", va="center", fontsize=7, color="black" if matrix[i, j] < 65 else "white")
    cbar = fig.colorbar(im, ax=ax, pad=0.02)
    cbar.set_label("Task activation rate (%)")
    ax.set_title("Task-aware runtime plugin routing\nDeepSeek-V4-Flash / Financial composition")
    ax.text(0.0, -0.15, "Activation is an auditable runtime signal; it is not a causal ablation.", transform=ax.transAxes, fontsize=7)
    save(fig, "fig4_plugin_routing_heatmap")


def figure_evolution() -> None:
    rows = read_csv("evolution_trajectory.csv")
    order = {"General-only": 0, "Domain-only": 1, "Alternating": 2}
    rows.sort(key=lambda r: (order[r["strategy"]], int(r["round_id"]), r["candidate_id"]))
    xs = np.arange(1, len(rows) + 1)
    ys = np.array([float(r["candidate_score"]) for r in rows])
    accepted = np.array([r["decision"] == "promote" for r in rows])
    fig, ax = plt.subplots(figsize=(7.2, 3.5))
    ax.axhline(float(rows[0]["incumbent_score_before"]), color="#555555", linestyle="--", linewidth=1, label="Baseline incumbent")
    ax.plot(xs, ys, color="#999999", linewidth=0.9, zorder=1)
    ax.scatter(xs[~accepted], ys[~accepted], s=48, color=COLORS["reject"], marker="x", label="Rejected", zorder=3)
    ax.scatter(xs[accepted], ys[accepted], s=50, color=COLORS["accept"], marker="o", label="Promoted", zorder=3)
    for x, y, r in zip(xs, ys, rows, strict=True):
        ax.text(x, y + 1.8, r["target_group"], ha="center", fontsize=7)
    ax.set_xticks(xs, [r["candidate_id"] for r in rows], rotation=65, ha="right", fontsize=6)
    ax.set_ylabel("Weighted validation quality")
    ax.set_title("Strict co-evolution gate filters most candidate updates")
    ax.legend(frameon=False, ncol=3, loc="upper left")
    save(fig, "fig5_evolution_gate_trajectory")


def figure_cost_accuracy() -> None:
    rows = read_csv("accuracy_cost_tradeoff.csv")
    keep = [r for r in rows if r["primary_score"] and r["avg_model_calls"]]
    fig, ax = plt.subplots(figsize=(6.3, 3.7))
    offsets = {
        "Codex + spreadsheet-core": (4, 5),
        "Claude Code + spreadsheet-core": (4, -12),
        "DeepSeekHarness + spreadsheet-core": (4, 5),
        "SHEETHARNESS-BASIC": (4, 5),
        "SHEETHARNESS-FINANCIAL": (4, 5),
    }
    short_names = {
        "Codex + spreadsheet-core": "Codex + core",
        "Claude Code + spreadsheet-core": "Claude + core",
        "DeepSeekHarness + spreadsheet-core": "DSH + core",
        "SHEETHARNESS-BASIC": "SH-BASIC",
        "SHEETHARNESS-FINANCIAL": "SH-FINANCIAL",
    }
    for r in keep:
        x = float(r["avg_model_calls"])
        y = float(r["primary_score"])
        is_ds = r["backbone"] == "DeepSeek-V4-Flash"
        color = "#264653" if is_ds else "#E76F51"
        marker = "o" if "SHEETHARNESS" in r["method"] else "s"
        ax.scatter(x, y, s=50, color=color, marker=marker, edgecolor="white", linewidth=0.5)
        label = short_names.get(r["method"], r["method"])
        ax.annotate(label, (x, y), xytext=offsets.get(r["method"], (4, 4)), textcoords="offset points", fontsize=6)
    ax.set_xlabel("Average model calls per task")
    ax.set_ylabel("V2 Overall Exact (%)")
    ax.set_title("Accuracy–interaction-cost trade-off")
    ax.grid(True, alpha=0.22)
    ax.text(0.0, -0.22, "Colors: DeepSeek (blue-green), Qwen (orange-red); marker shape: SheetHarness vs external core.", transform=ax.transAxes, fontsize=7)
    save(fig, "fig6_accuracy_cost_tradeoff")


def _v2_metadata() -> dict[str, dict]:
    metadata = {}
    for path in (ROOT / "benchmarks" / "data" / "spreadsheetbench-v2").glob("*/dataset.json"):
        category = path.parent.name
        with path.open(encoding="utf-8") as f:
            records = json.load(f)
        for record in records:
            if "answer_position" not in record:
                continue
            task_id = f"{category}/{record['id']}"
            sheets = set(__import__("re").findall(r"'([^']+)'!", record["answer_position"]))
            record["_target_sheet_count"] = len(sheets)
            metadata[task_id] = record
    return metadata


def _paired_v2_scores() -> dict[str, dict[str, float | None]]:
    rows = read_csv("task_plugin_composition.csv")
    scores: dict[str, dict[str, float | None]] = {"Basic": {}, "Financial": {}}
    method_map = {"SHEETHARNESS-BASIC": "Basic", "SHEETHARNESS-FINANCIAL": "Financial"}
    for row in rows:
        if row["backbone"] != "DeepSeek-V4-Flash" or row["method"] not in method_map:
            continue
        method = method_map[row["method"]]
        task = row["task_id"]
        if task not in scores[method]:
            scores[method][task] = None if not row["final_score"] else float(row["final_score"])
    return scores


def figure_target_sheet_gain() -> None:
    metadata = _v2_metadata()
    scores = _paired_v2_scores()
    bins = [("1 sheet", lambda n: n == 1), ("2–3 sheets", lambda n: 2 <= n <= 3), ("4+ sheets", lambda n: n >= 4)]
    basic, financial = [], []
    for _, predicate in bins:
        pairs = []
        for task, record in metadata.items():
            if task.startswith("Visualization/") or not predicate(record["_target_sheet_count"]):
                continue
            b = scores["Basic"].get(task)
            f = scores["Financial"].get(task)
            if b is not None and f is not None:
                pairs.append((b, f))
        basic.append(100 * np.mean([p[0] for p in pairs]))
        financial.append(100 * np.mean([p[1] for p in pairs]))
    x = np.arange(len(bins))
    width = 0.35
    fig, ax = plt.subplots(figsize=(6.4, 3.4))
    ax.bar(x - width / 2, basic, width, color=COLORS["basic"], label="Basic")
    ax.bar(x + width / 2, financial, width, color=COLORS["financial"], label="Financial")
    for values, offset in ((basic, -width / 2), (financial, width / 2)):
        for i, value in enumerate(values):
            ax.text(i + offset, value + 0.7, f"{value:.1f}", ha="center", fontsize=8)
    ax.set_xticks(x, [label for label, _ in bins])
    ax.set_ylabel("V2 Overall Exact (%)")
    ax.set_ylim(0, 35)
    ax.set_title("Domain harness gains concentrate on multi-sheet tasks")
    ax.legend(frameon=False, ncol=2)
    ax.text(0.0, -0.20, "DeepSeek-V4-Flash; non-visual paired tasks; target-sheet count from answer-position metadata.", transform=ax.transAxes, fontsize=7)
    save(fig, "fig7_target_sheet_harness_gain")


def _result_maps() -> dict[str, dict[str, dict]]:
    runs = {
        "Basic": "deepseek-v4-flash-harness-v26-basic-v2-p6-20260915",
        "Financial": "deepseek-v4-flash-harness-v26-all-v2-p6-20260915",
    }
    output = {}
    for method, run in runs.items():
        records = {}
        for path in (ROOT / "benchmarks" / "results" / run / "tasks").glob("*/results.json"):
            try:
                with path.open(encoding="utf-8") as f:
                    record = json.load(f)[0]
            except Exception:
                continue
            score = record.get("official_score") or {}
            usage = (record.get("agent") or {}).get("usage") or {}
            records[record["task_id"]] = {
                "exact": score.get("accuracy"),
                "tokens": usage.get("total_tokens"),
                "turns": (record.get("agent") or {}).get("turns"),
            }
        output[method] = records
    return output


def figure_token_exact_curve() -> None:
    maps = _result_maps()
    fig, ax = plt.subplots(figsize=(6.5, 3.7))
    for method, color in (("Basic", COLORS["basic"]), ("Financial", COLORS["financial"])):
        rows = [r for r in maps[method].values() if r["tokens"] is not None and r["exact"] is not None]
        rows.sort(key=lambda r: r["tokens"])
        edges = [rows[int((len(rows) - 1) * q / 100)]["tokens"] for q in (0, 25, 50, 75, 100)]
        x_values, y_values = [], []
        for i in range(4):
            low, high = edges[i], edges[i + 1]
            bucket = [r for r in rows if r["tokens"] >= low and (r["tokens"] < high if i < 3 else r["tokens"] <= high)]
            if not bucket:
                continue
            x_values.append(np.median([r["tokens"] for r in bucket]) / 1000)
            y_values.append(100 * np.mean([r["exact"] for r in bucket]))
        ax.plot(x_values, y_values, marker="o", linewidth=2, color=color, label=method)
    ax.set_xlabel("Median total tokens per task (thousands)")
    ax.set_ylabel("Exact success (%)")
    ax.set_ylim(bottom=0)
    ax.set_title("Token–accuracy relationship is non-monotonic")
    ax.legend(frameon=False)
    ax.text(0.0, -0.20, "Points are token quartile bins; association only, not a causal scaling law.", transform=ax.transAxes, fontsize=7)
    save(fig, "fig8_token_exact_curve")


def figure_cross_model_transfer() -> None:
    qwen_path = ROOT / "benchmarks" / "results" / "spreadsheetbench-v2-qwen36-full-nonvisual-postopt-v15-20260901" / "results.json"
    gpt_path = ROOT / "benchmarks" / "results" / "sheetcompass-gpt55-smoke-20260905" / "results.json"
    with qwen_path.open(encoding="utf-8") as f:
        qwen_records = json.load(f)
    with gpt_path.open(encoding="utf-8") as f:
        gpt_records = json.load(f)
    qwen = next(r for r in qwen_records if r["task_id"] == "Debugging/01_04" and r["arm"] == "spreadsheet-harness-basic")
    gpt = next(r for r in gpt_records if r["task_id"] == "Debugging/01_04")
    qscore, gscore = qwen["official_score"], gpt["official_score"]
    labels = ["Exact", "Modification", "Regression"]
    qvalues = [100 * qscore["accuracy"], 100 * qscore["modification_accuracy"], 100 * qscore["regression_accuracy"]]
    gvalues = [100 * gscore["accuracy"], 100 * gscore["modification_accuracy"], 100 * gscore["regression_accuracy"]]
    x = np.arange(3)
    width = 0.35
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(7.1, 3.2), gridspec_kw={"width_ratios": [1.6, 1.0]})
    ax.bar(x - width / 2, qvalues, width, color="#6A4C93", label="Qwen3.6-35B")
    ax.bar(x + width / 2, gvalues, width, color="#2A9D8F", label="GPT-5.5")
    ax.set_xticks(x, labels)
    ax.set_ylim(0, 110)
    ax.set_ylabel("Score (%)")
    ax.set_title("Same harness composition")
    ax.legend(frameon=False, fontsize=7)
    for values, offset in ((qvalues, -width / 2), (gvalues, width / 2)):
        for i, value in enumerate(values):
            ax.text(i + offset, value + 2, f"{value:.1f}", ha="center", fontsize=7)
    token_values = [qwen["agent"]["usage"]["total_tokens"] / 1000, gpt["agent"]["usage"]["total_tokens"] / 1000]
    ax2.bar([0, 1], token_values, color=["#6A4C93", "#2A9D8F"], width=0.6)
    ax2.set_xticks([0, 1], ["Qwen", "GPT-5.5"])
    ax2.set_ylabel("Total tokens (thousands)")
    ax2.set_title("Interaction cost")
    for i, value in enumerate(token_values):
        ax2.text(i, value + 3, f"{value:.1f}", ha="center", fontsize=8)
    fig.suptitle("Cross-model transfer smoke test (n=1 matched task)", y=1.03)
    ax2.text(-0.1, -0.27, "Same composition SHA; both Exact=0. This is transfer of behavior/cost, not a success-rate claim.", transform=ax2.transAxes, fontsize=7)
    save(fig, "fig9_cross_model_transfer_smoke")


def write_manifest() -> None:
    text = """# Paper figure manifest\n\nGenerated on 2026-09-18 from audited repository artifacts.\n\n| File | Intended use | Evidence type |\n|---|---|---|\n| `fig1_category_plugin_gain.*` | Main paper: Basic vs Financial category gain | V2 static comparison |\n| `fig2_pilot_task_success.*` | Supplement: pilot Table 2 visualization | 24-family held-out pilot |\n| `fig3_pilot_interaction.*` | Supplement: pilot Table 3 visualization | 24-family held-out pilot |\n| `fig4_plugin_routing_heatmap.*` | Main/supplement mechanism figure | Runtime activation heatmap |\n| `fig5_evolution_gate_trajectory.*` | Supplement: search dynamics | 7-family validation gate |\n| `fig6_accuracy_cost_tradeoff.*` | Main/supplement efficiency figure | V2 accuracy/cost snapshot |\n| `fig7_target_sheet_harness_gain.*` | Main/supplement difficulty analysis | Paired target-sheet stratification |\n| `fig8_token_exact_curve.*` | Supplement: token scaling analysis | Token quartile association |\n| `fig9_cross_model_transfer_smoke.*` | Supplement: transfer diagnostic | Same composition, Qwen/GPT smoke |\n\nMissing cells were omitted, not imputed as zero. Pilot and validation figures are explicitly labeled in the plots and should not be presented as full formal held-out results.\n"""
    (OUT / "README.md").write_text(text, encoding="utf-8")


def main() -> None:
    paper_style()
    figure_category_gain()
    figure_pilot_ts()
    figure_pilot_interaction()
    figure_plugin_heatmap()
    figure_evolution()
    figure_cost_accuracy()
    figure_target_sheet_gain()
    figure_token_exact_curve()
    figure_cross_model_transfer()
    write_manifest()
    print(f"wrote figures to {OUT}")


if __name__ == "__main__":
    main()
