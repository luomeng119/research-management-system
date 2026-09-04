from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
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
    offline_root = args.offline_root.resolve()
    requirements = args.requirements.resolve()
    if not requirements.is_file():
        raise ValueError(f"requirements file does not exist: {requirements}")
    locked = _read_lock(requirements)

    offline_root.mkdir(parents=True, exist_ok=True)
    if offline_root.is_symlink():
        raise ValueError("offline root may not be a symbolic link")
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
    if not args.python_source.strip() or not args.postgres_source.strip():
        raise ValueError("runtime source labels must not be empty")

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
        }
        wheel = _wheel_identity(path)
        if relative.startswith("wheelhouse/") and wheel is not None:
            entry.update(
                component="python-package",
                name=wheel[0],
                version=wheel[1],
                source="PyPI via pinned requirements.txt",
            )
        elif relative.startswith("runtime/python/"):
            entry.update(
                component="python-runtime",
                version=args.python_version,
                source=args.python_source,
            )
        elif relative.startswith("runtime/postgresql/"):
            entry.update(
                component="postgresql-server-runtime",
                version=args.postgres_version,
                source=args.postgres_source,
            )
        elif relative.startswith("test-runtime/"):
            entry.update(
                component="offline-acceptance-runtime",
                version="bundle-input",
                source="bundle-input",
            )
        elif relative.startswith("licenses/"):
            entry.update(
                component="license-notices",
                version="not-applicable",
                source="bundle-input",
            )
        else:
            entry.update(
                component="dependency-lock",
                version="locked",
                source="source-repository",
            )
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
            "python": args.python_source,
            "postgresqlServerRuntime": args.postgres_source,
            "pythonPackages": "PyPI via pinned requirements.txt",
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
    parser.add_argument("--python-version", default="3.13")
    parser.add_argument("--postgres-version", required=True)
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
