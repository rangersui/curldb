"""Check built archives and exercise the installed wheel outside the checkout."""
from __future__ import annotations

import argparse
import ast
from email import message_from_bytes
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import venv
import zipfile


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist", type=Path, nargs="?", default=Path("dist"))
    args = parser.parse_args()
    wheels = list(args.dist.resolve().glob("*.whl"))
    sources = list(args.dist.resolve().glob("*.tar.gz"))
    require(len(wheels) == len(sources) == 1,
            "Expected one wheel and one sdist; use a directory without old builds.")

    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        metadata_path = next(n for n in names if n.endswith(".dist-info/METADATA"))
        metadata = message_from_bytes(wheel.read(metadata_path))
        require(metadata["Name"] == "curldb", "Unexpected package name")
        require(metadata["License-Expression"] == "MIT", "Missing MIT license expression")
        require(metadata.get_all("Requires-Dist", []) == [], "Unexpected runtime dependencies")
        license_path = metadata_path.rsplit("/", 1)[0] + "/licenses/LICENSE"
        require(license_path in names, "Wheel is missing LICENSE")
        version = metadata["Version"]
        tree = ast.parse(wheel.read("httpdb.py").decode("utf-8"))
        code_version = next(
            ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
        )
        require(version == code_version, "Package and code versions disagree")

    with tarfile.open(sources[0]) as source:
        prefix = f"curldb-{version}/"
        required = {"SYSTEM.md", "LICENSE", "README.md", "pyproject.toml", "httpdb.py",
                    "MANIFEST.in", "tests/test_httpdb.py", "scripts/check_dist.py"}
        missing = {prefix + name for name in required} - set(source.getnames())
        require(not missing, f"Source distribution is missing: {sorted(missing)}")
        source_metadata = message_from_bytes(source.extractfile(prefix + "PKG-INFO").read())
        require(source_metadata["Version"] == version, "Wheel and sdist versions disagree")

    with tempfile.TemporaryDirectory(prefix="httpdb-package-") as temporary:
        directory = Path(temporary)
        environment = directory / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")
        executable = scripts / ("httpdb.exe" if os.name == "nt" else "httpdb")
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env["HTTPDB_PATH"] = str(directory / "session.sqlite")

        def run(*args, data=None):
            return subprocess.run(
                [str(arg) for arg in args], input=data, cwd=directory, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=60,
            ).stdout

        run(python, "-I", "-m", "pip", "--isolated", "install", "--no-index", "--no-deps", wheels[0])
        require(b"HTTP exchange datastore" in run(executable, "--help"), "Broken console entry point")
        body = "  中文\r\nsecond\rthird\n\n".encode("utf-8")
        wrapped = run(executable, "wrap", "POST /check", "-H", "X-Scope:packaging", data=body)
        require(wrapped.split(b"\n\n", 1)[1] == body, "Installed wrap changed the body")
        input_file = directory / "input.http"
        input_file.write_bytes(wrapped)
        for args, data in ((("add",), wrapped), (("add", input_file), None)):
            rid = run(executable, *args, data=data).split()[0][1:].decode()
            require(run(executable, "get", rid) == wrapped, "Installed add/get changed raw data")
        matches = run(executable, "query", "kind=request header:X-Scope=packaging body~中文")
        require(matches.count(b"POST /check") == 2, "Installed query failed")
    print(f"OK: httpdb {version}; sdist contents, metadata, isolated wheel install and CLI roundtrips")


if __name__ == "__main__":
    main()
