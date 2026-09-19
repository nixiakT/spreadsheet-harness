#!/usr/bin/env /usr/bin/python3
"""Run a Linux/LibreOffice proxy for SpreadsheetBench V2 Visualization.

This deliberately does not alter source XLSX files or official result files.  It
exports chart objects with LibreOffice UNO, then invokes the pinned public VLM
checklist evaluator against those PNGs.  The resulting reports are explicitly
marked as a Linux proxy and are not official Excel/WPS scores.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import sys
import signal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, "/usr/lib/python3/dist-packages")

import uno
from com.sun.star.beans import PropertyValue

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "benchmarks/data/spreadsheetbench-v2/Visualization/dataset.json"
EVALUATOR = Path("/tmp/SpreadsheetBench-2-full/evaluation/run_visual_vlm_checklist_eval.py")
EVAL_PYTHON = ROOT / ".venv/bin/python"


def prop(name: str, value: object) -> PropertyValue:
    item = PropertyValue()
    item.Name, item.Value = name, value
    return item


def export_charts(source: Path, output: Path) -> int:
    """Export all Calc OLE chart shapes from one workbook to one PNG."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lo-visual-proxy-") as profile:
        port = 24000 + os.getpid() % 10000
        accept = f"socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext"
        proc = subprocess.Popen(
            ["/usr/bin/libreoffice", "--headless", "--nologo", "--nodefault",
             "--nofirststartwizard", f"-env:UserInstallation={uno.systemPathToFileUrl(profile)}",
             f"--accept={accept}"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        document = None
        try:
            local = uno.getComponentContext()
            resolver = local.ServiceManager.createInstanceWithContext(
                "com.sun.star.bridge.UnoUrlResolver", local)
            context = None
            for _ in range(100):
                try:
                    context = resolver.resolve(
                        f"uno:socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext")
                    break
                except Exception:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.1)
            if context is None:
                raise RuntimeError("LibreOffice UNO connection failed")
            smgr = context.ServiceManager
            desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", context)
            document = desktop.loadComponentFromURL(
                uno.systemPathToFileUrl(str(source)), "_blank", 0,
                (prop("Hidden", True), prop("ReadOnly", True)))
            if document is None:
                raise RuntimeError("LibreOffice could not open workbook")
            exporter = smgr.createInstanceWithContext(
                "com.sun.star.drawing.GraphicExportFilter", context)
            exported = []
            for si in range(document.Sheets.Count):
                page = document.Sheets.getByIndex(si).DrawPage
                for ji in range(page.Count):
                    shape = page.getByIndex(ji)
                    if "com.sun.star.drawing.OLE2Shape" not in tuple(shape.getSupportedServiceNames()):
                        continue
                    tmp = output.with_name(output.stem + f"-chart-{len(exported)+1}.png")
                    exporter.setSourceDocument(shape)
                    ok = exporter.filter((prop("URL", uno.systemPathToFileUrl(str(tmp))),
                                         prop("MediaType", "image/png")))
                    if ok and tmp.is_file() and tmp.stat().st_size:
                        exported.append(tmp)
            if not exported:
                return 0
            if len(exported) == 1:
                shutil.move(str(exported[0]), str(output))
            else:
                from PIL import Image
                images = [Image.open(p).convert("RGB") for p in exported]
                canvas = Image.new("RGB", (max(i.width for i in images),
                                            sum(i.height for i in images)), "white")
                y = 0
                for image in images:
                    canvas.paste(image, (0, y)); y += image.height; image.close()
                canvas.save(output, format="PNG")
                for p in exported: p.unlink(missing_ok=True)
            # Some Calc chart exports are only one pixel high/wide (a known
            # rendering quirk for certain chart types). Pad them so the VLM
            # API accepts the image; this does not change chart content.
            from PIL import Image
            image = Image.open(output).convert("RGB")
            if image.width < 16 or image.height < 16:
                padded = Image.new("RGB", (max(16, image.width), max(16, image.height)), "white")
                padded.paste(image, (0, 0)); padded.save(output, format="PNG")
            image.close()
            return len(exported)
        finally:
            if document is not None:
                document.close(True)
            proc.terminate()
            try: proc.wait(timeout=10)
            except subprocess.TimeoutExpired: proc.kill()


def export_sheet_preview(source: Path, output: Path) -> int:
    """Fallback for chart types Calc cannot expose as OLE shapes."""
    from spreadsheet_harness.render import render_workbook
    render_dir = output.parent / (output.stem + "-pages")
    def _timeout(_signum, _frame):
        raise TimeoutError(f"LibreOffice sheet preview timed out: {source.name}")
    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(90)
    try:
        result = render_workbook(source, render_dir, dpi=144)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    pages = [page.path for page in result.pages]
    if not pages:
        return 0
    from PIL import Image
    images = [Image.open(p).convert("RGB") for p in pages]
    canvas = Image.new("RGB", (max(i.width for i in images), sum(i.height for i in images)), "white")
    y = 0
    for image in images:
        canvas.paste(image, (0, y)); y += image.height; image.close()
    canvas.save(output, format="PNG")
    return len(pages)


def discover_roots(results_root: Path) -> list[Path]:
    roots = []
    for root in sorted(results_root.iterdir()):
        if not root.is_dir(): continue
        if list(root.glob("tasks/Visualization__*/output.xlsx")):
            roots.append(root)
    return roots


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-root", type=Path, default=ROOT / "benchmarks/results")
    ap.add_argument("--output-root", type=Path, default=ROOT / "benchmarks/results/visual-linux-proxy-20260919")
    ap.add_argument("--method", action="append", help="Result directory name; repeatable")
    ap.add_argument("--api-key-file", type=Path, default=Path("/tmp/spreadsheet-harness-litellm.key"))
    ap.add_argument("--base-url", default="http://10.130.138.46:8010/v1")
    ap.add_argument("--model", default="dashscope/qwen3-vl-235b-a22b-instruct")
    ap.add_argument("--sleep-seconds", type=float, default=0.2)
    args = ap.parse_args()
    if not EVALUATOR.is_file(): raise SystemExit(f"missing evaluator: {EVALUATOR}")
    methods = discover_roots(args.results_root)
    if args.method:
        wanted = set(args.method); methods = [p for p in methods if p.name in wanted]
    args.output_root.mkdir(parents=True, exist_ok=True)
    manifest = {"proxy": True, "renderer": "LibreOffice UNO", "evaluator": str(EVALUATOR),
                "model": args.model, "base_url": args.base_url, "methods": []}
    for method in methods:
        out = args.output_root / method.name
        out.mkdir(parents=True, exist_ok=True)
        rows = []
        for xlsx in sorted(method.glob("tasks/Visualization__*/output.xlsx")):
            task_id = xlsx.parent.name.removeprefix("Visualization__")
            png = out / f"1_Visualization/{task_id}_output.png"
            # evaluator expects 1_<task-id>_output.png directly in output-dir
            png = out / f"1_{task_id}_output.png"
            try:
                count = export_charts(xlsx, png)
                source_kind = "libreoffice_chart_export"
                if not count:
                    count = export_sheet_preview(xlsx, png)
                    source_kind = "libreoffice_sheet_preview" if count else "none"
            except Exception as exc:
                count = 0; source_kind = "none"; print(f"WARN {method.name}/{task_id}: {exc}")
            rows.append({"task_id": task_id, "xlsx": str(xlsx), "png": str(png),
                         "pages_or_charts": count, "image_source": source_kind})
            time.sleep(args.sleep_seconds)
        (out / "proxy_export_manifest.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        report = out / "qwen3-vl_evaluation_report.json"
        env = os.environ.copy(); env["VLM_API_KEY"] = args.api_key_file.read_text().strip()
        cmd = ["/usr/bin/python3", str(EVALUATOR), "--tasks-json", str(DATASET),
               "--output-dir", str(out), "--report-path", str(report), "--base-url", args.base_url,
               "--model", args.model, "--sleep-seconds", str(args.sleep_seconds)]
        subprocess.run([str(EVAL_PYTHON), *cmd[1:]], env=env, check=False)
        manifest["methods"].append({"name": method.name, "output_dir": str(out),
                                    "workbooks": len(rows), "pngs": sum(r["pages_or_charts"] > 0 for r in rows),
                                    "report": str(report)})
    (args.output_root / "proxy_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__": raise SystemExit(main())
