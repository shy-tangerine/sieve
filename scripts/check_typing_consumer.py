"""Check installed-wheel type contracts with positive and negative consumers."""
from __future__ import annotations

import argparse
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

POSITIVE = '''from sieve.sdk import SieveClient, BatchRecord
async def example() -> list[BatchRecord]:
    async with SieveClient(timeout=10, concurrency=2) as client:
        await client.fetch("https://example.com")
        return await client.batch(["https://example.com"], {"baseSelector":"article","fields":[]})
'''
NEGATIVE = '''from sieve.sdk import SieveClient
client = SieveClient(timeout="wrong")
async def example() -> None:
    await client.fetch(123)
'''


def main(args=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    parser.add_argument("--python", default="3.13")
    options = parser.parse_args(args)
    with zipfile.ZipFile(options.wheel) as archive:
        if "sieve/py.typed" not in archive.namelist():
            raise ValueError("wheel lacks py.typed")
    with tarfile.open(options.sdist) as archive:
        if not any(name.endswith("/sieve/py.typed") for name in archive.getnames()):
            raise ValueError("sdist lacks py.typed")
    with tempfile.TemporaryDirectory(prefix="sieve-typing-consumer-") as directory:
        root = Path(directory)
        environment = root / "venv"
        python = environment / "bin/python"
        subprocess.run(["uv", "venv", "--python", options.python, str(environment)], check=True)
        subprocess.run(["uv", "pip", "install", "--python", str(python), str(options.wheel.resolve()), "mypy>=1.18,<3"], check=True)
        for name, source, expected in (("positive", POSITIVE, 0), ("negative", NEGATIVE, 1)):
            consumer = root / (name + ".py")
            consumer.write_text(source)
            result = subprocess.run([str(python), "-I", "-m", "mypy", "--follow-imports=silent",
                                     "--python-executable", str(python), str(consumer)],
                                    cwd=root, capture_output=True, text=True)
            if result.returncode != expected or (name == "negative" and result.stdout.count("[arg-type]") != 2):
                raise ValueError(f"{name} consumer failed its contract: {result.stdout}{result.stderr}")
            print(f"{name} installed-wheel typing contract passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
