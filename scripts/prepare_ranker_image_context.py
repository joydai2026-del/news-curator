#!/usr/bin/env python3
"""Stage the reviewed ranker image context without installing dependencies."""

import argparse
import hashlib
import json
import shutil
import re
from pathlib import Path

import yaml

RANKER_POLICY = Path("config/ranker-policy-r1.yaml")
# The only directory staged into the image, and the only place a policy may
# point at. Mirrored by curator/recommendation/composition.RETENTION_INPUTS_FILE,
# which the runtime reads; tests assert the two agree.
CONFIG_ROOT = "config"
RETENTION_INPUTS = Path("config/retention-inputs.yaml")


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


def _path_shaped_values(document, key="<root>"):
    """Every value that names a file, with the key that named it.

    A path is a value with a separator and no URL scheme: `endpoint:
    https://api.openai.com/v1` is not a file, `config/ranking-policy-r2.yaml`
    is. Only the POLICY document is walked. The files it names hold prose and
    numbers, and a prompt template that writes "A/B" must not read as a path.
    """
    if isinstance(document, str):
        if "/" in document and "://" not in document:
            yield key, document
    elif isinstance(document, dict):
        for name, value in document.items():
            yield from _path_shaped_values(value, str(name))
    elif isinstance(document, list):
        for value in document:
            yield from _path_shaped_values(value, key)


def _validated_reference(repo: Path, key: str, value: str) -> Path:
    """One reference, refused rather than skipped when it escapes config/.

    A staging script that quietly ignores a reference it did not expect is how
    the Phase 2 files went missing, so every path-shaped value either stages or
    fails the build naming the key that carried it.
    """
    def refuse(reason: str):
        raise SystemExit(f"ranker policy {key}: {value} {reason}")

    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        refuse("must be a repository-relative path with no parent traversal")
    if relative.parts[:1] != (CONFIG_ROOT,):
        refuse(f"must live inside {CONFIG_ROOT}/, the only directory staged into the image")
    current = repo
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            refuse("passes through a symlink")
    if not current.is_file():
        refuse("is missing")
    if not current.resolve().is_relative_to(repo.resolve()):
        refuse("resolves outside the repository")
    return relative


def referenced_config_files(repo: Path, policy: Path = RANKER_POLICY) -> list[Path]:
    """Every config file the ranker policy names, the policy itself first.

    Discovered from the same file the runtime loads, so adding a key like
    prompt_template is a config change and not a second edit here. The old
    hardcoded pair is what shipped an image whose policy named two files it did
    not contain.
    """
    document = yaml.safe_load((repo / policy).read_text(encoding="utf-8"))
    ordered = [policy]
    for key, value in _path_shaped_values(document):
        relative = _validated_reference(repo, key, value)
        if relative not in ordered:
            ordered.append(relative)
    return ordered


def stage_retention_inputs(repo: Path, context: Path) -> int:
    """Generate the boot-time retention input the image has no sources.yaml for.

    sources.yaml configures collection, not the ranker, so it does not belong in
    the ranker image. The one value the retention cross-check needs does, and the
    runtime now REFUSES to boot without it instead of skipping the check.
    """
    document = yaml.safe_load((repo / "sources.yaml").read_text(encoding="utf-8"))
    coverage = document.get("coverage") if isinstance(document, dict) else None
    days = coverage.get("observations_retention_days") if isinstance(coverage, dict) else None
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise SystemExit("sources.yaml coverage.observations_retention_days must be a positive integer")
    target = context / RETENTION_INPUTS
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Generated by scripts/prepare_ranker_image_context.py from sources.yaml.\n"
                      "# The image carries no sources.yaml. curator/recommendation/composition.py\n"
                      "# reads this file at boot and refuses to start without it.\n"
                      f"schema_version: 1\ncoverage:\n  observations_retention_days: {days}\n",
                      encoding="utf-8")
    return days


def stage_config(repo: Path, context: Path, policy: Path = RANKER_POLICY) -> list[Path]:
    """Copy exactly the config files the policy names, plus the generated input."""
    staged = referenced_config_files(repo, policy)
    for relative in staged:
        target = context / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / relative, target)
    stage_retention_inputs(repo, context)
    return staged


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
    stage_config(repo, args.output)
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
