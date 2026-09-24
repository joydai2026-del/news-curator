"""Modal deployment declaration. Import only for an explicitly enabled deploy."""

import os
import re
import hashlib
import json
from pathlib import Path

import modal

from .deployment import bounded_int, function_timeout_seconds
from .modal_handlers import endpoint as _endpoint
from .modal_handlers import prepare_next_run as _prepare_next_run
from .modal_handlers import scrub_expired_preparations as _scrub_expired_preparations
from .modal_handlers import smoke_rankllm_image as _smoke_rankllm_image


def _enabled(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name, str(default).lower())
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    # One implementation, shared with the container's own boot validation.
    return bounded_int(os.environ, name, default, minimum, maximum)


def _name(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value):
        raise ValueError(f"{name} must be a lowercase Modal name")
    return value


def _validate_context(context: Path, expected_digest: str) -> None:
    root = context.resolve(strict=True)
    manifest_path = root / "context-manifest.json"
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != expected_digest:
        raise ValueError("ranker image context manifest digest mismatch")
    manifest = json.loads(manifest_path.read_text())
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ValueError("ranker image context manifest files are invalid")
    if any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("ranker image context cannot contain symlinks")
    declared = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise ValueError("invalid ranker image context entry")
        relative = Path(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() in declared:
            raise ValueError("unsafe or duplicate ranker image context path")
        declared.add(relative.as_posix())
        candidate = root / relative
        if candidate.is_symlink() or not candidate.is_file() or not candidate.resolve().is_relative_to(root):
            raise ValueError("ranker image context file is missing or unsafe")
        data = candidate.read_bytes()
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError("ranker image context file mismatch")
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*")
              if path.is_file() and path != manifest_path}
    if actual != declared:
        raise ValueError("ranker image context has missing or extra files")


if not _enabled("NEWS_CURATOR_MODAL_DEPLOYMENT_ENABLED"):
    raise RuntimeError("Modal deployment is disabled by policy")

app = modal.App(_name("NEWS_CURATOR_MODAL_APP_NAME", "news-curator-m2-ranker"))
context_path = Path(os.environ["NEWS_CURATOR_RANKER_CONTEXT"])
manifest_path = context_path / "context-manifest.json"
expected_manifest_digest = os.environ["NEWS_CURATOR_RANKER_CONTEXT_SHA256"]
if not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_digest):
    raise ValueError("NEWS_CURATOR_RANKER_CONTEXT_SHA256 must be lowercase SHA-256")
_validate_context(context_path, expected_manifest_digest)
image = modal.Image.from_dockerfile(context_path / "Containerfile", context_dir=context_path)
deployment_mode = os.environ.get("NEWS_CURATOR_MODAL_MODE", "service")
if deployment_mode not in {"service", "smoke"}:
    raise ValueError("NEWS_CURATOR_MODAL_MODE must be service or smoke")
# The container's wall clock for one request. The service refuses to boot when
# its own worst case (provider deadline + settle window + the claimed section's
# Supabase budget) is not strictly below this, so the two are validated together
# rather than drifting apart across a deploy.
function_timeout = function_timeout_seconds(os.environ)
max_containers = _bounded_int("NEWS_CURATOR_MODAL_MAX_CONTAINERS", 4, 1, 20)
min_containers = _bounded_int("NEWS_CURATOR_MODAL_MIN_CONTAINERS", 0, 0, max_containers)
max_inputs = _bounded_int("NEWS_CURATOR_MODAL_MAX_INPUTS_PER_CONTAINER", 8, 1, 32)
# Modal SDK 1.4.2 only rejects non-positive values. The observed server
# enforces 2..3600; the cold-start guide also documents a two-second minimum.
scaledown_window = _bounded_int("NEWS_CURATOR_MODAL_SCALEDOWN_SECONDS", 60, 2, 3600)


if deployment_mode == "service":
    from .runtime import load_ranker_policy, next_run_preparation_policy

    _, staged_policy = load_ranker_policy(os.environ, root=context_path)
    next_run = next_run_preparation_policy(staged_policy)
    if "enabled" in next_run and type(next_run["enabled"]) is not bool:
        raise ValueError("next_run_preparation.enabled must be boolean in the staged policy")
    worker_enabled = _enabled("NEWS_CURATOR_MODAL_PREPARATION_WORKER_ENABLED")
    if next_run.get("enabled", False) != worker_enabled:
        raise ValueError(
            "next_run_preparation.enabled and "
            "NEWS_CURATOR_MODAL_PREPARATION_WORKER_ENABLED must match")
    runtime_secret = modal.Secret.from_name(os.environ["NEWS_CURATOR_RANKER_SECRET_NAME"])
    endpoint = app.function(image=image, secrets=[runtime_secret], timeout=function_timeout,
        max_containers=max_containers, min_containers=min_containers, scaledown_window=scaledown_window,
        enable_memory_snapshot=_enabled("NEWS_CURATOR_MODAL_MEMORY_SNAPSHOT_ENABLED", default=True),
        restrict_modal_access=True)(modal.concurrent(max_inputs=max_inputs)(modal.asgi_app()(_endpoint)))

    # Private payload expiry continues even when paid preparation is disabled.
    scrub_cron = os.environ.get("NEWS_CURATOR_MODAL_PREPARATION_SCRUB_CRON", "* * * * *")
    if not scrub_cron or len(scrub_cron) > 80:
        raise ValueError("NEWS_CURATOR_MODAL_PREPARATION_SCRUB_CRON must be a nonempty cron expression")
    scrub_expired_preparations = app.function(
        image=image, secrets=[runtime_secret], schedule=modal.Cron(scrub_cron),
        timeout=90, max_containers=1, min_containers=0, restrict_modal_access=True,
    )(modal.concurrent(max_inputs=1)(_scrub_expired_preparations))

    # A separate, explicit deploy switch prevents an ordinary service redeploy
    # from starting a paid schedule. The image policy is a second runtime gate.
    if worker_enabled:
        preparation_batch_size = _bounded_int(
            "NEWS_CURATOR_MODAL_PREPARATION_BATCH_SIZE", 1, 1, 5)
        preparation_timeout = _bounded_int(
            "NEWS_CURATOR_MODAL_PREPARATION_TIMEOUT_SECONDS", 300, 90, 1800)
        preparation_cron = os.environ.get("NEWS_CURATOR_MODAL_PREPARATION_CRON", "* * * * *")
        if not preparation_cron or len(preparation_cron) > 80:
            raise ValueError("NEWS_CURATOR_MODAL_PREPARATION_CRON must be a nonempty cron expression")
        preparation_image = image.env({
            "NEWS_CURATOR_MODAL_PREPARATION_BATCH_SIZE": str(preparation_batch_size),
        })
        prepare_next_run = app.function(
            image=preparation_image, secrets=[runtime_secret],
            schedule=modal.Cron(preparation_cron), timeout=preparation_timeout,
            max_containers=1, min_containers=0, restrict_modal_access=True,
        )(modal.concurrent(max_inputs=1)(_prepare_next_run))

if deployment_mode == "smoke":
    smoke_rankllm_image = app.function(image=image, timeout=function_timeout, max_containers=1,
        scaledown_window=2, restrict_modal_access=True, block_network=True,
        include_source=False)(_smoke_rankllm_image)
