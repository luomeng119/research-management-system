from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath


REQUIRED_RUNTIME_FILES = (
    "runtime/python/python.exe",
    "runtime/postgresql/bin/initdb.exe",
    "runtime/postgresql/bin/pg_ctl.exe",
    "runtime/postgresql/bin/postgres.exe",
    "runtime/postgresql/bin/psql.exe",
    "runtime/postgresql/bin/pg_dump.exe",
    "runtime/postgresql/bin/pg_restore.exe",
    "runtime/postgresql/bin/libpq.dll",
    "test-runtime/node/node.exe",
    "test-runtime/node_modules/@playwright/test/cli.js",
    "test-runtime/node_modules/@playwright/test/index.mjs",
    "test-runtime/node_modules/@playwright/test/package.json",
    "test-runtime/node_modules/playwright/package.json",
    "test-runtime/node_modules/playwright-core/package.json",
    "licenses/THIRD_PARTY-NOTICES.txt",
)
REQUIRED_POSTGRES_RUNTIME_DIRS = (
    "runtime/postgresql/lib",
    "runtime/postgresql/share",
)
REQUIRED_ACCEPTANCE_RUNTIME_DIRS = (
    "test-runtime/playwright-browsers",
)
POSTGRES_EXECUTABLES = (
    "initdb.exe", "pg_ctl.exe", "postgres.exe", "psql.exe",
    "pg_dump.exe", "pg_restore.exe",
)
PINNED_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?"
    r"==(?P<version>[^\s;]+)(?:\s*;.*)?$"
)


def _normalise_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _read_lock(path: Path) -> list[tuple[str, str]]:
    locked: list[tuple[str, str]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = PINNED_REQUIREMENT.fullmatch(line)
        if match is None:
            raise ValueError(
                f"requirements must be exact name==version pins; line {line_number}: {line}"
            )
        locked.append((match.group("name"), match.group("version")))
    if not locked:
        raise ValueError("requirements lock is empty")
    return locked


def _wheel_identity(path: Path) -> tuple[str, str] | None:
    if path.suffix.lower() != ".whl":
        return None
    parts = path.name[:-4].split("-")
    if len(parts) < 5:
        return None
    return _normalise_distribution(parts[0]), parts[1]


def _tag_is_windows_cp313_compatible(python: str, abi: str, platform: str) -> bool:
    python_tags = set(python.split("."))
    abi_tags = set(abi.split("."))
    platform_tags = set(platform.split("."))
    if not platform_tags.intersection({"any", "win_amd64"}):
        return False
    if python_tags.intersection({"py3", "py313"}) and "none" in abi_tags:
        return True
    if "cp313" in python_tags and abi_tags.intersection({"cp313", "abi3", "none"}):
        return True
    if "abi3" in abi_tags:
        for python_tag in python_tags:
            match = re.fullmatch(r"cp3(?P<minor>\d+)", python_tag)
            if match is not None and int(match.group("minor")) <= 13:
                return True
    return False


def _inspect_wheel(path: Path) -> tuple[str, str]:
    identity = _wheel_identity(path)
    if identity is None:
        raise ValueError(f"invalid wheel filename: {path.name}")
    filename_parts = path.name[:-4].split("-")
    if not _tag_is_windows_cp313_compatible(*filename_parts[-3:]):
        raise ValueError(
            f"wheel is not compatible with Windows x64 CPython 3.13: {path.name}"
        )
    try:
        with zipfile.ZipFile(path) as archive:
            corrupt = archive.testzip()
            if corrupt is not None:
                raise ValueError(f"wheel contains a corrupt member: {path.name}: {corrupt}")
            members = archive.infolist()
            for member in members:
                member_path = PurePosixPath(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError(f"wheel contains an unsafe path: {path.name}")
                mode = (member.external_attr >> 16) & 0xFFFF
                if mode and stat.S_ISLNK(mode):
                    raise ValueError(f"wheel contains a symbolic link: {path.name}")
            metadata_names = [
                item.filename
                for item in members
                if item.filename.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise ValueError(f"wheel must contain exactly one METADATA file: {path.name}")
            metadata_name = metadata_names[0]
            dist_info = metadata_name.rsplit("/", 1)[0]
            member_names = {item.filename for item in members}
            if not {f"{dist_info}/WHEEL", f"{dist_info}/RECORD"}.issubset(member_names):
                raise ValueError(f"wheel is missing WHEEL or RECORD metadata: {path.name}")
            metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
            metadata_identity = (
                _normalise_distribution(metadata.get("Name", "")),
                metadata.get("Version", ""),
            )
            if metadata_identity != identity:
                raise ValueError(f"wheel filename and METADATA disagree: {path.name}")
            wheel_metadata = Parser().parsestr(
                archive.read(f"{dist_info}/WHEEL").decode("utf-8")
            )
            if not any(
                len(parts := tag.split("-")) == 3
                and _tag_is_windows_cp313_compatible(*parts)
                for tag in wheel_metadata.get_all("Tag", [])
            ):
                raise ValueError(f"WHEEL metadata has no compatible target tag: {path.name}")
    except (UnicodeDecodeError, zipfile.BadZipFile) as error:
        raise ValueError(f"invalid wheel archive: {path.name}: {error}") from error
    return identity


def _assert_wheel_coverage(
    wheelhouse: Path, locked: list[tuple[str, str]]
) -> None:
    wheel_files = [path for path in wheelhouse.iterdir() if path.is_file()]
    non_wheels = [path.name for path in wheel_files if path.suffix.lower() != ".whl"]
    if non_wheels:
        raise ValueError("wheelhouse contains non-wheel files: " + ", ".join(non_wheels))
    available = {_inspect_wheel(path) for path in wheel_files}
    missing = [
        f"{name}=={version}"
        for name, version in locked
        if (_normalise_distribution(name), version) not in available
    ]
    if missing:
        raise ValueError(
            "wheelhouse does not cover the pinned requirements: " + ", ".join(missing)
        )


def _assert_offline_resolution(requirements: Path, wheelhouse: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="research-management-wheel-check-") as target:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                "--disable-pip-version-check",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                "--only-binary=:all:",
                "--platform",
                "win_amd64",
                "--python-version",
                "3.13",
                "--implementation",
                "cp",
                "--abi",
                "cp313",
                "--abi",
                "abi3",
                "--abi",
                "none",
                "--dest",
                target,
                "--requirement",
                str(requirements),
            ],
            check=True,
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_pe_amd64(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            size = path.stat().st_size
            dos = handle.read(64)
            if len(dos) != 64 or dos[:2] != b"MZ":
                raise ValueError
            pe_offset = struct.unpack_from("<I", dos, 0x3C)[0]
            if pe_offset < 64 or pe_offset + 24 > size:
                raise ValueError
            handle.seek(pe_offset)
            coff = handle.read(24)
            if len(coff) != 24 or coff[:4] != b"PE\0\0":
                raise ValueError
            machine, sections = struct.unpack_from("<HH", coff, 4)
            optional_size = struct.unpack_from("<H", coff, 20)[0]
            if machine != 0x8664 or not 1 <= sections <= 96 or optional_size < 0x70:
                raise ValueError
            optional = handle.read(optional_size)
            section_table = handle.read(sections * 40)
            if len(optional) != optional_size or len(section_table) != sections * 40:
                raise ValueError
            if struct.unpack_from("<H", optional, 0)[0] != 0x20B:
                raise ValueError
            size_of_headers = struct.unpack_from("<I", optional, 60)[0]
            if size_of_headers < pe_offset + 24 + optional_size + sections * 40 or size_of_headers > size:
                raise ValueError
            has_raw_section = False
            for index in range(sections):
                offset = index * 40
                raw_size, raw_pointer = struct.unpack_from("<II", section_table, offset + 16)
                if raw_size:
                    has_raw_section = True
                    if raw_pointer < size_of_headers or raw_pointer + raw_size > size:
                        raise ValueError
            if not has_raw_section:
                raise ValueError
    except OSError as error:
        raise ValueError(f"cannot read Windows runtime file: {path}") from error
    except (ValueError, struct.error) as error:
        raise ValueError(f"Windows runtime is not a valid PE/AMD64 file: {path}") from error


def _version_from_output(output: str, component: str) -> str:
    match = re.search(r"(?<!\d)(\d+\.\d+(?:\.\d+)*)(?!\d)", output or "")
    if match is None:
        raise ValueError(f"{component} version probe returned no version")
    return match.group(1)


def _assert_declared_version(output: str, declared: str, component: str) -> str:
    actual = _version_from_output(output, component)
    if actual != declared and not actual.startswith(declared + "."):
        raise ValueError(
            f"{component} runtime version {actual} does not match declared version {declared}"
        )
    return actual


def _assert_exact_version(output: str, declared: str, component: str) -> str:
    actual = _version_from_output(output, component)
    if not declared or actual != declared:
        raise ValueError(
            f"{component} runtime version {actual} does not match exact declared version {declared}"
        )
    return actual


def _read_json_object(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {label}: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"invalid {label}: {path}")
    return value


def _inspect_playwright_runtime(test_runtime: Path) -> tuple[str, dict[str, str]]:
    versions = []
    for package in ("@playwright/test", "playwright", "playwright-core"):
        metadata = _read_json_object(
            test_runtime / "node_modules" / package / "package.json",
            "Playwright package metadata",
        )
        version = metadata.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version):
            raise ValueError(f"Playwright package has no valid version: {package}")
        versions.append(version)
    if len(set(versions)) != 1:
        raise ValueError("Playwright package versions disagree")

    browsers = _read_json_object(
        test_runtime / "node_modules" / "playwright-core" / "browsers.json",
        "Playwright browser metadata",
    ).get("browsers")
    if not isinstance(browsers, list):
        raise ValueError("Playwright browser metadata has no browsers list")
    revisions = {}
    for item in browsers:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).replace("-", "_")
        revision = str(item.get("revision", ""))
        if name in {"chromium", "chromium_headless_shell"} and revision.isdigit():
            revisions[name] = revision
    if "chromium" not in revisions:
        raise ValueError("Playwright Chromium revision is missing or ambiguous")
    return versions[0], revisions


def _validate_playwright_selection(
    browser_root: Path, revisions: dict[str, str], executable: Path
) -> tuple[str, str, str]:
    root = browser_root.resolve(strict=True)
    selected = executable.resolve(strict=True)
    try:
        relative = selected.relative_to(root)
    except ValueError as error:
        raise ValueError("Playwright selected a browser outside the staged browser root") from error
    if len(relative.parts) < 2:
        raise ValueError("Playwright selected browser path has no revision directory")
    match = re.fullmatch(r"(chromium|chromium_headless_shell)-(\d+)", relative.parts[0])
    if match is None:
        raise ValueError("Playwright selected browser directory is not a supported Chromium runtime")
    name, revision = match.groups()
    if revisions.get(name) != revision:
        raise ValueError(f"Playwright Chromium revision {revision} does not match package metadata")
    return name, revision, relative.as_posix()


def _path_has_link_or_reparse(path: Path) -> bool:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _canonical_safe_root(path: Path) -> Path:
    raw = path.expanduser()
    absolute = raw.absolute()
    for candidate in (absolute, *absolute.parents):
        if _path_has_link_or_reparse(candidate):
            raise ValueError(f"offline root contains a symbolic link or reparse point: {candidate}")
    absolute.mkdir(parents=True, exist_ok=True)
    for candidate in (absolute, *absolute.parents):
        if _path_has_link_or_reparse(candidate):
            raise ValueError(f"offline root contains a symbolic link or reparse point: {candidate}")
    return absolute.resolve(strict=True)


def _probe(executable: Path, *arguments: str) -> str:
    result = subprocess.run(
        [str(executable), *arguments], check=True, capture_output=True, text=True,
        timeout=60,
    )
    return (result.stdout + "\n" + result.stderr).strip()


def _assert_windows_loader(path: Path) -> None:
    binary_type = ctypes.c_uint32()
    if not ctypes.windll.kernel32.GetBinaryTypeW(str(path), ctypes.byref(binary_type)):
        raise ValueError(f"Windows loader rejected runtime executable: {path}")
    if binary_type.value != 6:
        raise ValueError(f"Windows runtime executable is not a 64-bit binary: {path}")


def _probe_playwright(node: Path, test_runtime: Path, browser_root: Path) -> Path:
    script = """
const { chromium } = require(process.argv[1]);
(async () => {
  const executablePath = chromium.executablePath();
  const browser = await chromium.launch({headless: true, executablePath});
  await browser.close();
  process.stdout.write(JSON.stringify({executablePath}));
})().catch(error => { console.error(error); process.exit(1); });
""".strip()
    environment = dict(os.environ)
    environment["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root)
    result = subprocess.run(
        [str(node), "-e", script, str(test_runtime / "node_modules/playwright")],
        check=True, capture_output=True, text=True, timeout=120, env=environment,
    )
    try:
        payload = json.loads(result.stdout)
        selected = Path(payload["executablePath"])
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("Playwright launch probe did not return its executable path") from error
    return selected


def _assert_non_placeholder_tree(path: Path, label: str) -> None:
    files = [item for item in path.rglob("*") if item.is_file()]
    if not files or sum(item.stat().st_size for item in files) < 4096:
        raise ValueError(f"{label} is empty or placeholder-sized: {path}")


def _assert_source(value: str, component: str) -> str:
    source = str(value or "").strip()
    if len(source) < 8 or source.casefold() in {
        "source", "python source", "postgres source", "bundle-input", "unknown",
    }:
        raise ValueError(f"{component} source must be a non-placeholder provenance label")
    return source


def _verify_runtime_payloads(offline_root: Path, args) -> dict:
    python = offline_root / "runtime/python/python.exe"
    postgres_bin = offline_root / "runtime/postgresql/bin"
    node = offline_root / "test-runtime/node/node.exe"
    for executable in [python, node, *(postgres_bin / name for name in POSTGRES_EXECUTABLES)]:
        _assert_pe_amd64(executable)
    _assert_pe_amd64(postgres_bin / "libpq.dll")
    playwright_version, browser_revisions = _inspect_playwright_runtime(
        offline_root / "test-runtime"
    )
    _assert_non_placeholder_tree(offline_root / "runtime/postgresql/lib", "PostgreSQL lib")
    _assert_non_placeholder_tree(offline_root / "runtime/postgresql/share", "PostgreSQL share")
    python_source = _assert_source(args.python_source, "Python")
    postgres_source = _assert_source(args.postgres_source, "PostgreSQL")
    node_source = _assert_source(args.node_source, "Node.js")
    playwright_source = _assert_source(args.playwright_source, "Playwright")
    if not re.fullmatch(r"\d+\.\d+", args.python_version):
        raise ValueError("Python declared version must be a major.minor value")
    if sys.platform != "win32":
        raise ValueError("Windows runtime probes must run on a Windows x64 build host")
    for executable in [python, node, *(postgres_bin / name for name in POSTGRES_EXECUTABLES)]:
        _assert_windows_loader(executable)

    python_version = _assert_declared_version(
        _probe(python, "--version"), args.python_version, "Python"
    )
    with tempfile.TemporaryDirectory(prefix="research-management-python-probe-") as target:
        venv_path = Path(target) / "venv"
        _probe(python, "-m", "venv", str(venv_path))
        venv_python = venv_path / "Scripts/python.exe"
        if not venv_python.is_file():
            raise ValueError("Python runtime cannot create a Windows virtual environment")
        _probe(venv_python, "-m", "ensurepip", "--version")

    postgres_versions = {
        name: _assert_declared_version(
            _probe(postgres_bin / name, "--version"),
            args.postgres_version,
            f"PostgreSQL {name}",
        )
        for name in POSTGRES_EXECUTABLES
    }
    if len(set(postgres_versions.values())) != 1:
        raise ValueError("PostgreSQL runtime executable versions disagree")
    if next(iter(postgres_versions.values())) != args.postgres_version:
        raise ValueError("PostgreSQL runtime version does not exactly match the declared version")
    node_version = _assert_exact_version(
        _probe(node, "--version"), args.node_version, "Node.js"
    )
    selected_browser = _probe_playwright(
        node, offline_root / "test-runtime",
        offline_root / "test-runtime/playwright-browsers",
    )
    browser_name, chromium_revision, browser_relative = _validate_playwright_selection(
        offline_root / "test-runtime/playwright-browsers",
        browser_revisions,
        selected_browser,
    )
    _assert_pe_amd64(selected_browser)
    _assert_windows_loader(selected_browser)
    return {
        "pythonVersion": python_version,
        "postgresVersion": next(iter(postgres_versions.values())),
        "nodeVersion": node_version,
        "playwrightVersion": playwright_version,
        "chromiumRevision": chromium_revision,
        "browserName": browser_name,
        "browserExecutable": browser_relative,
        "pythonSource": python_source,
        "postgresSource": postgres_source,
        "nodeSource": node_source,
        "playwrightSource": playwright_source,
    }


def _is_manifest_control_file(path: Path, offline_root: Path) -> bool:
    if path.parent != offline_root:
        return False
    return path.name == "manifest.json" or path.name == "manifest.json.tmp" or (
        path.name.startswith(".manifest.json.") and path.name.endswith(".tmp")
    )


def _download_wheels(requirements: Path, wheelhouse: Path) -> None:
    if any(wheelhouse.iterdir()):
        raise ValueError(
            "wheelhouse must be empty before download; use a fresh offline directory"
        )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--only-binary=:all:",
            "--platform",
            "win_amd64",
            "--python-version",
            "3.13",
            "--implementation",
            "cp",
            "--dest",
            str(wheelhouse),
            "--requirement",
            str(requirements),
        ],
        check=True,
    )


def build(args: argparse.Namespace) -> Path:
    offline_root = _canonical_safe_root(args.offline_root)
    requirements = args.requirements.resolve()
    if not requirements.is_file():
        raise ValueError(f"requirements file does not exist: {requirements}")
    locked = _read_lock(requirements)

    stale_manifest_temps = [
        path
        for path in offline_root.iterdir()
        if path.is_file()
        and (
            path.name == "manifest.json.tmp"
            or (path.name.startswith(".manifest.json.") and path.name.endswith(".tmp"))
        )
    ]
    if stale_manifest_temps:
        raise ValueError(
            "stale manifest temporary file exists: "
            + ", ".join(sorted(path.name for path in stale_manifest_temps))
        )
    wheelhouse = offline_root / "wheelhouse"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    manifest_path = offline_root / "manifest.json"

    if not args.no_download:
        _download_wheels(requirements, wheelhouse)

    symlinks = [
        item.relative_to(offline_root).as_posix()
        for item in offline_root.rglob("*")
        if item.is_symlink()
    ]
    if symlinks:
        raise ValueError("offline bundle contains symbolic links: " + ", ".join(symlinks))

    missing_runtime = [
        relative
        for relative in REQUIRED_RUNTIME_FILES
        if not (offline_root / PurePosixPath(relative)).is_file()
    ]
    if missing_runtime:
        raise ValueError("offline bundle is missing: " + ", ".join(missing_runtime))
    incomplete_runtime_dirs = [
        relative
        for relative in REQUIRED_POSTGRES_RUNTIME_DIRS
        if not (offline_root / PurePosixPath(relative)).is_dir()
        or not any((offline_root / PurePosixPath(relative)).iterdir())
    ]
    if incomplete_runtime_dirs:
        raise ValueError(
            "PostgreSQL server runtime directory is missing or empty: "
            + ", ".join(incomplete_runtime_dirs)
        )
    incomplete_acceptance_dirs = [
        relative
        for relative in REQUIRED_ACCEPTANCE_RUNTIME_DIRS
        if not (offline_root / PurePosixPath(relative)).is_dir()
        or not any((offline_root / PurePosixPath(relative)).iterdir())
    ]
    if incomplete_acceptance_dirs:
        raise ValueError(
            "Offline acceptance runtime directory is missing or empty: "
            + ", ".join(incomplete_acceptance_dirs)
        )
    browser_root = offline_root / "test-runtime/playwright-browsers"
    if not any(
        path.is_file() and path.name.lower() in {"chrome.exe", "headless_shell.exe"}
        for path in browser_root.rglob("*")
    ):
        raise ValueError(
            "Offline acceptance runtime has no Windows Chromium executable"
        )
    _assert_wheel_coverage(wheelhouse, locked)
    _assert_offline_resolution(requirements, wheelhouse)
    runtime = _verify_runtime_payloads(offline_root, args)

    shutil.copyfile(requirements, offline_root / "requirements.txt")

    files = []
    for path in sorted(
        (item for item in offline_root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(offline_root).as_posix(),
    ):
        relative = path.relative_to(offline_root).as_posix()
        if _is_manifest_control_file(path, offline_root):
            continue
        entry = {
            "path": relative,
            "sha256": _sha256(path),
            "size": path.stat().st_size,
            "verified": True,
            "sourceVerified": False,
            "verification": ["sha256"],
        }
        wheel = _wheel_identity(path)
        if relative.startswith("wheelhouse/") and wheel is not None:
            entry.update(
                component="python-package",
                name=wheel[0],
                version=wheel[1],
                source="PyPI via pinned requirements.txt",
            )
            entry["verification"].extend(["wheel-metadata", "offline-resolution"])
        elif relative.startswith("runtime/python/"):
            entry.update(
                component="python-runtime",
                version=runtime["pythonVersion"],
                source=runtime["pythonSource"],
            )
            if relative == "runtime/python/python.exe":
                entry["verification"].extend([
                    "pe-amd64", "windows-loader", "version-probe",
                    "venv-create", "ensurepip-probe",
                ])
        elif relative.startswith("runtime/postgresql/"):
            entry.update(
                component="postgresql-server-runtime",
                version=runtime["postgresVersion"],
                source=runtime["postgresSource"],
            )
            name = path.name.lower()
            if name in POSTGRES_EXECUTABLES:
                entry["verification"].extend(["pe-amd64", "windows-loader", "version-probe"])
            elif name == "libpq.dll":
                entry["verification"].append("pe-amd64")
            elif "/lib/" in f"/{relative}" or "/share/" in f"/{relative}":
                entry["verification"].append("runtime-tree-non-placeholder-aggregate")
        elif relative.startswith("test-runtime/"):
            if relative.startswith("test-runtime/node/"):
                entry.update(
                    component="node-runtime", version=runtime["nodeVersion"],
                    source=runtime["nodeSource"],
                )
                if relative == "test-runtime/node/node.exe":
                    entry["verification"].extend(["pe-amd64", "windows-loader", "version-probe"])
            else:
                entry.update(
                    component="playwright-runtime", version=runtime["playwrightVersion"],
                    source=runtime["playwrightSource"],
                )
                if relative.endswith("/package.json"):
                    entry["verification"].append("playwright-package-version-consistency")
                if relative.endswith("playwright-core/browsers.json"):
                    entry["verification"].append("playwright-browser-revision-metadata")
                selected = "test-runtime/playwright-browsers/" + runtime["browserExecutable"]
                if relative == selected:
                    entry["verification"].extend([
                        "pe-amd64", "windows-loader", "playwright-resolved-path",
                        "playwright-launch-close",
                    ])
        elif relative.startswith("licenses/"):
            entry.update(
                component="license-notices",
                version="not-applicable",
                source="project third-party notices inventory",
            )
        else:
            entry.update(
                component="dependency-lock",
                version="locked",
                source="source-repository",
            )
            entry["verification"].append("exact-pin-validation")
        files.append(entry)

    manifest = {
        "schemaVersion": 1,
        "target": {
            "os": "windows",
            "arch": "x64",
            "python": args.python_version,
            "postgresqlServerVersion": args.postgres_version,
        },
        "sources": {
            "python": runtime["pythonSource"],
            "postgresqlServerRuntime": runtime["postgresSource"],
            "node": runtime["nodeSource"],
            "playwright": runtime["playwrightSource"],
            "pythonPackages": "PyPI via pinned requirements.txt",
        },
        "sourceSemantics": "operator-declared provenance labels; payload origin is not cryptographically authenticated",
        "verifiedMeaning": "the listed local validation checks passed; verified does not authenticate source provenance",
        "runtimeValidation": {
            "pythonVersion": runtime["pythonVersion"],
            "postgresqlServerVersion": runtime["postgresVersion"],
            "nodeVersion": runtime["nodeVersion"],
            "playwrightVersion": runtime["playwrightVersion"],
            "chromiumRevision": runtime["chromiumRevision"],
            "browserName": runtime["browserName"],
            "browserExecutable": runtime["browserExecutable"],
            "buildHost": "windows-x64",
        },
        "files": files,
    }
    temporary_manifest: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=offline_root,
            prefix=".manifest.json.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            temporary_manifest = Path(handle.name)

        expected_paths = {entry["path"] for entry in files}
        actual_paths = {
            path.relative_to(offline_root).as_posix()
            for path in offline_root.rglob("*")
            if path.is_file() and not _is_manifest_control_file(path, offline_root)
        }
        if actual_paths != expected_paths:
            raise ValueError("offline bundle changed while manifest was being written")
        temporary_manifest.replace(manifest_path)
        temporary_manifest = None
    finally:
        if temporary_manifest is not None and temporary_manifest.exists():
            temporary_manifest.unlink()
    return manifest_path


def _parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        description="Build and hash the Windows x64 offline dependency bundle."
    )
    parser.add_argument("--offline-root", type=Path, default=root / "offline")
    parser.add_argument("--requirements", type=Path, default=root / "requirements.txt")
    parser.add_argument("--python-source", required=True)
    parser.add_argument("--postgres-source", required=True)
    parser.add_argument("--node-source", default="")
    parser.add_argument("--playwright-source", default="")
    parser.add_argument("--python-version", default="3.13")
    parser.add_argument("--postgres-version", required=True)
    parser.add_argument("--node-version", default="")
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Validate already staged wheels instead of accessing PyPI.",
    )
    return parser


def main() -> int:
    try:
        manifest = build(_parser().parse_args())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
