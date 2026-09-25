import os
import argparse
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from tqdm import tqdm


def _service_port() -> int:
    """Return the per-worker UNO port.

    The historical evaluator used 2002 unconditionally.  That is safe only
    for one process; task-level screening can run several evaluator
    subprocesses, so callers may assign an isolated port through the
    environment without changing the official command-line contract.
    """
    raw = os.environ.get("SPREADSHEETBENCH_LO_PORT", "2002")
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("SPREADSHEETBENCH_LO_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("SPREADSHEETBENCH_LO_PORT must be between 1 and 65535")
    return port


def find_libreoffice() -> str:
    """Locate the LibreOffice binary."""
    candidates = [
        "libreoffice",
        "soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",  # macOS
    ]
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return path
        if os.path.isfile(candidate):
            return candidate
    return None


def _find_libreoffice_program_dir() -> str:
    """Auto-detect the LibreOffice program directory."""
    soffice = find_libreoffice()
    if soffice is None:
        raise RuntimeError(
            "Cannot find LibreOffice. Please install it:\n"
            "  - macOS: brew install --cask libreoffice\n"
            "  - Linux: sudo apt install libreoffice (or equivalent)"
        )
    # Resolve symlinks to get the real path (e.g. /usr/lib/libreoffice/program/soffice)
    real = os.path.realpath(soffice)
    return os.path.dirname(real)


LIBREOFFICE_PROGRAM_DIR = _find_libreoffice_program_dir()

# Global variable to hold the LibreOffice process
_libreoffice_process = None
_libreoffice_profile = None


def _env_for_lo_python() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{LIBREOFFICE_PROGRAM_DIR}:" + env.get("PYTHONPATH", "")
    env["UNO_PATH"] = LIBREOFFICE_PROGRAM_DIR
    env["URE_BOOTSTRAP"] = (
        f"vnd.sun.star.pathname:{LIBREOFFICE_PROGRAM_DIR}/fundamentalrc"
    )
    return env


def _lo_python() -> str:
    """Return the Python interpreter that can import uno.

    The standalone (tarball) install ships its own `program/python`;
    the system package relies on the system python3 instead.
    """
    # Try bundled Python (Windows uses python.exe)
    for bundled_name in ["python", "python.exe"]:
        bundled = os.path.join(LIBREOFFICE_PROGRAM_DIR, bundled_name)
        if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
            return bundled
    # Fall back to system python3 (works for apt-installed LibreOffice)
    return "/usr/bin/python3"


def _soffice_bin() -> str:
    return os.path.join(LIBREOFFICE_PROGRAM_DIR, "soffice")


def _check_service_ready() -> bool:
    """Check whether the LibreOffice UNO service is ready."""
    port = _service_port()
    check_code = f'''
import uno
try:
    local_context = uno.getComponentContext()
    resolver = local_context.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_context
    )
    resolver.resolve("uno:socket,host=localhost,port={port};urp;StarOffice.ComponentContext")
    print("OK")
except:
    print("FAIL")
'''
    import tempfile
    with tempfile.NamedTemporaryFile(prefix="lo_check_", suffix=".py", delete=False, mode='w') as fp:
        temp_path = fp.name
        fp.write(check_code)

    try:
        proc = subprocess.run(
            [_lo_python(), temp_path],
            capture_output=True,
            text=True,
            env=_env_for_lo_python(),
            timeout=10
        )
        return "OK" in proc.stdout
    except:
        return False
    finally:
        try:
            os.remove(temp_path)
        except:
            pass


def start_libreoffice_service() -> bool:
    """Start the LibreOffice headless service."""
    global _libreoffice_process, _libreoffice_profile

    if _libreoffice_process is not None:
        print("LibreOffice service is already running")
        return True

    soffice = _soffice_bin()
    # LibreOffice locks its user profile.  Give every evaluator subprocess a
    # private profile as well as a private UNO endpoint; otherwise parallel
    # workers can still block before they reach the socket listener.
    profile_dir = Path(tempfile.mkdtemp(prefix="spreadsheetbench-lo-"))
    _libreoffice_profile = profile_dir
    profile_uri = profile_dir.resolve().as_uri()
    port = _service_port()
    cmd = [
        soffice,
        "--headless",
        f"--accept=socket,host=127.0.0.1,port={port};urp;",
        "--norestore",
        "--nofirststartwizard",
        f"-env:UserInstallation={profile_uri}",
    ]

    try:
        _libreoffice_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_env_for_lo_python(),
        )
        # Wait for service to be ready, max 30 seconds
        max_wait = 30
        for i in range(max_wait):
            time.sleep(1)
            if _check_service_ready():
                print(f"LibreOffice service started (ready after {i+1}s)")
                return True
            print(f"Waiting for LibreOffice service... ({i+1}/{max_wait})")

        print("LibreOffice service failed to become ready")
        stop_libreoffice_service()
        return False
    except Exception as e:
        print(f"Failed to start LibreOffice service: {e}")
        return False


def stop_libreoffice_service() -> None:
    """Stop the LibreOffice service."""
    global _libreoffice_process, _libreoffice_profile

    if _libreoffice_process is None:
        return

    try:
        _libreoffice_process.terminate()
        _libreoffice_process.wait(timeout=10)
        print("LibreOffice service stopped")
    except subprocess.TimeoutExpired:
        _libreoffice_process.kill()
        print("LibreOffice service killed")
    except Exception as e:
        print(f"Error stopping LibreOffice service: {e}")
    finally:
        _libreoffice_process = None
        if _libreoffice_profile is not None:
            shutil.rmtree(_libreoffice_profile, ignore_errors=True)
            _libreoffice_profile = None


def batch_open_files(files: list) -> None:
    """Batch process multiple Excel files, reusing UNO connection for better performance."""
    if not files:
        return

    import tempfile
    import shutil
    import json

    # Write file list to a temporary JSON file
    with tempfile.NamedTemporaryFile(prefix="lo_files_", suffix=".json", delete=False, mode='w') as fp:
        files_json_path = fp.name
        json.dump([os.path.abspath(f) for f in files], fp)

    # Batch processing script - establish UNO connection only once
    port = _service_port()
    code = f'''
import uno
import json
import sys
from com.sun.star.beans import PropertyValue

def process_file(desktop, filename):
    file_url = f"file://{{filename}}"
    temp_output = filename + ".tmp.xlsx"
    temp_output_url = f"file://{{temp_output}}"

    try:
        doc = desktop.loadComponentFromURL(file_url, "_blank", 0, ())
        if doc:
            # Enable iterative calculation
            doc.IsIterationEnabled = True
            doc.IterationCount = 100
            doc.IterationEpsilon = 0.0001

            doc.calculateAll()
            save_props = [PropertyValue(Name="FilterName", Value="Calc MS Excel 2007 XML")]
            doc.storeToURL(temp_output_url, tuple(save_props))
            doc.close(True)
            return ("OK", temp_output)
        else:
            return ("FAIL", "Failed to open document")
    except Exception as e:
        return ("FAIL", str(e))

try:
    # Establish UNO connection (only once)
    local_context = uno.getComponentContext()
    resolver = local_context.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_context
    )
    context = resolver.resolve("uno:socket,host=localhost,port={port};urp;StarOffice.ComponentContext")
    desktop = context.ServiceManager.createInstanceWithContext(
        "com.sun.star.frame.Desktop", context
    )

    # Read file list
    with open("{files_json_path}", "r") as f:
        files = json.load(f)

    # Process each file in batch
    for filename in files:
        status, result = process_file(desktop, filename)
        # Output format: STATUS|filename|result
        print(f"{{status}}|{{filename}}|{{result}}")
        sys.stdout.flush()

except Exception as e:
    print(f"INIT_FAIL|{{e}}")
'''

    with tempfile.NamedTemporaryFile(prefix="lo_batch_", suffix=".py", delete=False, mode='w') as fp:
        script_path = fp.name
        fp.write(code)

    lo_python = _lo_python()
    env = _env_for_lo_python()

    try:
        proc = subprocess.Popen(
            [lo_python, script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            bufsize=1,
            universal_newlines=True
        )

        # Read output in real-time and update progress
        processed = 0
        with tqdm(total=len(files), desc="Processing spreadsheets") as pbar:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue

                if line.startswith("INIT_FAIL"):
                    tqdm.write(f"Initialization failed: {line}")
                    break

                parts = line.split("|", 2)
                if len(parts) >= 3:
                    status, filename, result = parts
                    if status == "OK":
                        # Move temporary file to replace original
                        temp_output = result
                        if os.path.exists(temp_output):
                            shutil.move(temp_output, filename)
                    else:
                        tqdm.write(f"Error [{filename}]: {result}")
                        # Clean up possible temporary file
                        temp_output = filename + ".tmp.xlsx"
                        if os.path.exists(temp_output):
                            try:
                                os.remove(temp_output)
                            except:
                                pass

                    processed += 1
                    pbar.update(1)

        proc.wait(timeout=30)

    except Exception as e:
        tqdm.write(f"Batch processing error: {e}")
    finally:
        # Clean up temporary files
        for temp_file in [script_path, files_json_path]:
            try:
                os.remove(temp_file)
            except:
                pass


def open_all_spreadsheet_in_dir(dir_path: str, recursive: bool = True) -> None:
    """Open all Excel files in a directory and save them.

    Args:
        dir_path: Directory path
        recursive: Whether to recursively traverse subdirectories, defaults to True
    """
    if not os.path.isdir(dir_path):
        print(f"Not a valid dir path: {dir_path}")
        return

    # Start LibreOffice service
    if not start_libreoffice_service():
        print("Cannot start LibreOffice service, aborting")
        return

    try:
        # First collect all files to be processed
        files_to_process = []
        if recursive:
            for root, dirs, files in os.walk(dir_path):
                for filename in files:
                    if filename.endswith("output.xlsx"):
                        files_to_process.append(os.path.join(root, filename))
        else:
            for filename in os.listdir(dir_path):
                if filename.endswith("output.xlsx"):
                    files_to_process.append(os.path.join(dir_path, filename))

        print(f"Found {len(files_to_process)} files to process")

        # Batch process all files (reusing UNO connection)
        batch_open_files(files_to_process)
    finally:
        # Ensure service is stopped
        stop_libreoffice_service()


if __name__ == '__main__':
    parser = argparse.ArgumentParser("command line arguments for open spreadsheets.")

    parser.add_argument('--dir_path', type=str, help='the dir path of spreadsheets')
    parser.add_argument('--no-recursive', action='store_true',
                        help='do not recursively process subdirectories')

    opt = parser.parse_args()

    if opt.dir_path:
        open_all_spreadsheet_in_dir(opt.dir_path, recursive=not opt.no_recursive)
    else:
        parser.print_help()
