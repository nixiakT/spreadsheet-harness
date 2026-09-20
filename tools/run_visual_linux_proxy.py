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
if sys.prefix != "/usr":
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
    # The reliable Linux path is an isolated Calc PDF export followed by
    # rasterization.  It avoids UNO's in-process DrawingML deadlocks and gives
    # every workbook the same full-sheet visual context.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lo-visual-pdf-") as temp:
        profile = Path(temp) / "profile"
        pdf_dir = Path(temp) / "pdf"
        pdf_dir.mkdir()
        subprocess.run(
            ["/usr/bin/libreoffice", "--headless", "--nologo", "--nodefault",
             "--nofirststartwizard", f"-env:UserInstallation={uno.systemPathToFileUrl(str(profile))}",
             "--convert-to", "pdf", "--outdir", str(pdf_dir), str(source)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45, check=True,
        )
        pdf = pdf_dir / (source.stem + ".pdf")
        if not pdf.is_file():
            return 0
        import fitz
        doc = fitz.open(pdf)
        if not doc.page_count:
            return 0
        images = []
        from PIL import Image
        for page in doc:
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            images.append(Image.frombytes("RGB", [pix.width, pix.height], pix.samples))
        canvas = Image.new("RGB", (max(i.width for i in images), sum(i.height for i in images)), "white")
        y = 0
        for image in images:
            canvas.paste(image, (0, y)); y += image.height; image.close()
        canvas.save(output, format="PNG")
        doc.close()
        return len(images)

    # Kept below as a reference implementation for future chart-only export.
    # UNO can deadlock inside a single long-lived Python process on malformed
    # DrawingML.  Isolate every workbook in a fresh interpreter so the parent
    # batch remains recoverable and can hard-kill the child on timeout.
    if os.environ.get("VISUAL_PROXY_WORKER") != "1":
        env = os.environ.copy(); env["VISUAL_PROXY_WORKER"] = "1"
        # Do not leak the project's virtualenv import path into system Python;
        # UNO's importer is incompatible with the venv XML modules.
        env.pop("PYTHONPATH", None); env.pop("VIRTUAL_ENV", None)
        env["PATH"] = "/usr/bin:/bin"
        proc = subprocess.run(
            ["/usr/bin/python3", str(Path(__file__).resolve()), "--worker-source", str(source),
             "--worker-output", str(output)], env=env, timeout=45,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()[-500:]
            raise RuntimeError(f"isolated LibreOffice export failed (rc={proc.returncode}): {detail}")
        try:
            return int(proc.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"isolated exporter returned invalid output: {proc.stdout!r}") from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    def _timeout(_signum, _frame):
        raise TimeoutError(f"LibreOffice chart export timed out: {source.name}")
    previous_alarm = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(90)
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
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_alarm)


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
        if (
            list(root.glob("tasks/Visualization__*/output.xlsx"))
            or list(root.glob("**/visual_outputs/*/1_Task *_output.xlsx"))
            or list(root.glob("outputs/Visualization_Task */initial_output.xlsx"))
        ):
            roots.append(root)
    return roots


def visual_workbooks(method: Path) -> list[tuple[str, Path]]:
    """Return unique task IDs and final outputs from all known result layouts."""
    found: dict[str, Path] = {}
    for workbook in sorted(method.glob("tasks/Visualization__*/output.xlsx")):
        found.setdefault(workbook.parent.name.removeprefix("Visualization__"), workbook)
    for workbook in sorted(method.glob("**/visual_outputs/*/1_Task *_output.xlsx")):
        task_id = workbook.name.removeprefix("1_").removesuffix("_output.xlsx")
        found.setdefault(task_id, workbook)
    for workbook in sorted(method.glob("outputs/Visualization_Task */initial_output.xlsx")):
        task_id = workbook.parent.name.removeprefix("Visualization_")
        found.setdefault(task_id, workbook)
    return sorted(found.items())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-root", type=Path, default=ROOT / "benchmarks/results")
    ap.add_argument("--output-root", type=Path, default=ROOT / "benchmarks/results/visual-linux-proxy-20260919")
    ap.add_argument("--method", action="append", help="Result directory name; repeatable")
    ap.add_argument("--api-key-file", type=Path, default=Path("/tmp/spreadsheet-harness-litellm.key"))
    ap.add_argument("--base-url", default="http://10.130.138.46:8010/v1")
    ap.add_argument("--model", default="dashscope/qwen3-vl-235b-a22b-instruct")
    ap.add_argument("--sleep-seconds", type=float, default=0.2)
    ap.add_argument("--worker-source", type=Path)
    ap.add_argument("--worker-output", type=Path)
    args = ap.parse_args()
    if args.worker_source and args.worker_output:
        print(export_charts(args.worker_source.resolve(), args.worker_output.resolve()), flush=True)
        return 0
    if not EVALUATOR.is_file(): raise SystemExit(f"missing evaluator: {EVALUATOR}")
    if args.method:
        methods = [args.results_root / name for name in args.method]
        missing = [str(path) for path in methods if not path.is_dir()]
        if missing:
            raise SystemExit("missing method directories: " + ", ".join(missing))
    else:
        methods = discover_roots(args.results_root)
    args.output_root.mkdir(parents=True, exist_ok=True)
    manifest = {"proxy": True, "renderer": "LibreOffice CLI PDF + PyMuPDF", "evaluator": str(EVALUATOR),
                "model": args.model, "base_url": args.base_url, "methods": []}
    for method in methods:
        out = args.output_root / method.name
        out.mkdir(parents=True, exist_ok=True)
        existing_report = out / "qwen3-vl_evaluation_report.json"
        if existing_report.is_file():
            try:
                existing = json.loads(existing_report.read_text(encoding="utf-8"))
                summary = existing.get("summary", {})
                complete = summary.get("completed") == summary.get("total_tasks") == 24
            except (OSError, ValueError, TypeError):
                complete = False
            if complete:
                print(f"SKIP existing complete report: {method.name}")
                manifest["methods"].append({"name": method.name, "output_dir": str(out),
                                            "report": str(existing_report), "skipped_existing": True})
                continue
        rows = []
        for task_id, xlsx in visual_workbooks(method):
            png = out / f"1_Visualization/{task_id}_output.png"
            # evaluator expects 1_<task-id>_output.png directly in output-dir
            png = out / f"1_{task_id}_output.png"
            try:
                count = export_charts(xlsx, png)
                source_kind = "libreoffice_pdf_full_sheet"
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
