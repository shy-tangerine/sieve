"""Exercise declared dependency bounds in disposable Python environments."""
from __future__ import annotations

import argparse
import subprocess
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTRAS = ("core", "browser", "pdf", "ocr", "ocr-legacy", "rerank", "stt", "nlp", "legacy-server")
IMPORTS = {
    "core": ("import sieve.sdk, sieve.security, sieve.extraction, sieve.fetcher; "
             "import primp, inspect; assert 'stream' in inspect.signature(primp.Client.get).parameters; "
             "assert hasattr(primp.Response, 'iter_bytes') and hasattr(primp.Response, 'close')"),
    "browser": "import browserforge, playwright, patchright",
    "pdf": "import pdfplumber, pypdfium2",
    "ocr": "import rapidocr, onnxruntime, pypdfium2",
    "ocr-legacy": "import rapidocr_onnxruntime",
    "rerank": "import onnxruntime, tokenizers",
    "stt": "import faster_whisper",
    "nlp": "import nltk, tiktoken",
    "legacy-server": "import mcp, starlette, uvicorn",
}


def run(args=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", choices=("3.11", "3.12", "3.13"), default="3.13")
    parser.add_argument("--extra", choices=EXTRAS, default="core")
    parser.add_argument("--mode", choices=("minimum", "locked"), default="minimum")
    options = parser.parse_args(args)
    if options.extra == "ocr-legacy" and options.python == "3.13":
        parser.error("ocr-legacy supports Python 3.11 and 3.12; use the ocr extra on Python 3.13")
    with tempfile.TemporaryDirectory(prefix="sieve-compatibility-") as directory:
        environment = Path(directory) / "venv"
        python = environment / "bin/python"
        subprocess.run(["uv", "venv", "--python", options.python, str(environment)], check=True)
        if options.mode == "minimum":
            package = str(ROOT) + (f"[{options.extra}]" if options.extra != "core" else "")
            metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
            requirements = metadata["dependencies"] + metadata["optional-dependencies"].get(options.extra, [])
            install = ["uv", "pip", "install", "--python", str(python), "--resolution", "lowest-direct", *requirements, package]
        else:
            requirements = Path(directory) / "requirements.txt"
            export = ["uv", "export", "--locked", "--no-dev", "--no-emit-project", "--output-file", str(requirements)]
            if options.extra != "core":
                export += ["--extra", options.extra]
            subprocess.run(export, cwd=ROOT, check=True)
            install = ["uv", "pip", "install", "--python", str(python), "-r", str(requirements), str(ROOT)]
        subprocess.run(install, check=True)
        subprocess.run(["uv", "pip", "check", "--python", str(python)], check=True)
        subprocess.run([str(python), "-I", "-c", IMPORTS["core"] + ";" + IMPORTS[options.extra]], cwd=directory, check=True)
        subprocess.run([str(environment / "bin/sieve"), "--version"], cwd=directory, check=True)
        subprocess.run(["uv", "pip", "install", "--python", str(python), "pytest", "pytest-asyncio"], check=True)
        tests = ["tests/test_security.py", "tests/test_resource_budget.py", "tests/test_fetcher_fallback.py",
                 "tests/test_trafilatura_extractor.py", "tests/test_crawl4ai_ports.py", "tests/test_batch_extract.py"]
        if options.extra == "browser":
            tests.append("tests/test_browser_request_policy.py")
        subprocess.run([str(python), "-I", "-m", "pytest", "-q", *tests], cwd=ROOT, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
