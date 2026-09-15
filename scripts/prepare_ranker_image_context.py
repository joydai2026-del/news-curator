#!/usr/bin/env python3
"""Stage the reviewed ranker image context without installing dependencies."""

import argparse
import hashlib
import json
import shutil
import re
from pathlib import Path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_python_tree(source: Path, destination: Path) -> None:
    """Copy only regular Python package files, rejecting source symlinks."""
    destination.mkdir(parents=True)
    for path in source.rglob("*"):
        if path.is_symlink():
            raise SystemExit("Python source tree cannot contain symlinks")
        if path.is_file() and path.suffix == ".py":
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def copy_reviewed_vendor(source: Path, destination: Path, manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text())
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise SystemExit("vendor manifest files are invalid")
    destination.mkdir(parents=True)
    declared = set()
    for entry in entries:
        relative = Path(entry["path"])
        if (relative.is_absolute() or ".." in relative.parts or relative.as_posix() in declared or
                relative.suffix == ".pyc" or "__pycache__" in relative.parts):
            raise SystemExit("vendor manifest contains an unsafe file")
        declared.add(relative.as_posix())
        path = source / relative
        if path.is_symlink() or not path.is_file() or sha(path) != entry["sha256"]:
            raise SystemExit("vendor source does not match reviewed manifest")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    actual = {path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file()}
    if actual != declared:
        raise SystemExit("vendor tree has unreviewed files")


def validate_containerfile_sources(context: Path) -> None:
    for line in (context / "Containerfile").read_text().splitlines():
        match = re.fullmatch(r"COPY\s+([^\s]+)\s+[^\s]+", line.strip())
        if match and not (context / match.group(1).rstrip("/")).exists():
            raise SystemExit(f"Containerfile COPY source missing: {match.group(1)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheels", type=Path, required=True)
    parser.add_argument("--public-artifact", type=Path, required=True)
    parser.add_argument("--tokenizer-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    if args.output.exists():
        raise SystemExit("output must not already exist")
    args.output.mkdir(parents=True)
    copy_reviewed_vendor(repo / "deploy/ranker/vendor", args.output / "vendor",
                         repo / "deploy/ranker/rankllm-vendor-manifest.json")
    copy_python_tree(repo / "curator", args.output / "curator")
    (args.output / "config").mkdir()
    shutil.copy2(repo / "config/ranker-policy-r1.yaml", args.output / "config/ranker-policy-r1.yaml")
    shutil.copy2(repo / "config/rankllm-news-curator-json.yaml",
                 args.output / "config/rankllm-news-curator-json.yaml")
    shutil.copy2(repo / "deploy/ranker/Containerfile", args.output / "Containerfile")
    shutil.copy2(repo / "deploy/ranker/requirements-linux-cp312-x86_64.lock", args.output / "requirements.lock")
    shutil.copy2(repo / "deploy/ranker/linux-wheel-provenance.json", args.output / "wheel-provenance.json")
    shutil.copy2(repo / "deploy/ranker/rankllm-vendor-manifest.json", args.output / "vendor-manifest.json")
    expected = {item["filename"]: item["sha256"] for item in
                json.loads((repo / "deploy/ranker/linux-wheel-provenance.json").read_text())["artifacts"]}
    (args.output / "wheels").mkdir()
    actual = {path.name: sha(path) for path in args.wheels.glob("*.whl")}
    if actual != expected:
        raise SystemExit("wheel directory does not exactly match reviewed provenance")
    for path in args.wheels.glob("*.whl"):
        shutil.copy2(path, args.output / "wheels" / path.name)
    cache_name = "fb374d419588a4632f3f557e76b4b70aebbca790"
    if args.tokenizer_cache.name != cache_name or sha(args.tokenizer_cache) != "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d":
        raise SystemExit("o200k tokenizer cache mismatch")
    (args.output / "tiktoken-cache").mkdir()
    shutil.copy2(args.tokenizer_cache, args.output / "tiktoken-cache" / cache_name)
    rows = json.loads(args.public_artifact.read_text()).get("rows")
    if not isinstance(rows, list) or len(rows) < 200:
        raise SystemExit("public artifact needs at least 200 rows")
    fields = ("story_id", "title", "source_id", "summary", "language", "published_at")
    smoke = [{key: row[key] for key in fields} for row in rows[:200]]
    (args.output / "smoke-public-200.json").write_text(json.dumps(smoke, ensure_ascii=False,
        separators=(",", ":")) + "\n")
    validate_containerfile_sources(args.output)
    files = []
    for path in sorted(args.output.rglob("*")):
        if path.is_file():
            files.append({"path": path.relative_to(args.output).as_posix(), "size": path.stat().st_size,
                          "sha256": sha(path)})
    manifest = json.dumps({"schema_version": 1, "files": files}, indent=2) + "\n"
    (args.output / "context-manifest.json").write_text(manifest)
    print(hashlib.sha256(manifest.encode()).hexdigest())


if __name__ == "__main__":
    main()
