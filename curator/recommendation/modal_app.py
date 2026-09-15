"""Modal deployment declaration. Import only for an explicitly enabled deploy."""

import os
import re
import hashlib
import json
from pathlib import Path

import modal


def _enabled(name: str) -> bool:
    value = os.environ.get(name, "false")
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError(f"{name} must be an integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


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
function_timeout = _bounded_int("NEWS_CURATOR_MODAL_FUNCTION_TIMEOUT_SECONDS", 15, 7, 60)
max_containers = _bounded_int("NEWS_CURATOR_MODAL_MAX_CONTAINERS", 4, 1, 20)
max_inputs = _bounded_int("NEWS_CURATOR_MODAL_MAX_INPUTS_PER_CONTAINER", 8, 1, 32)
scaledown_window = _bounded_int("NEWS_CURATOR_MODAL_SCALEDOWN_SECONDS", 60, 1, 3600)


def _endpoint():
    from .runtime import build_application
    return build_application()


if deployment_mode == "service":
    runtime_secret = modal.Secret.from_name(os.environ["NEWS_CURATOR_RANKER_SECRET_NAME"])
    endpoint = app.function(image=image, secrets=[runtime_secret], timeout=function_timeout,
        max_containers=max_containers, scaledown_window=scaledown_window,
        restrict_modal_access=True)(modal.concurrent(max_inputs=max_inputs)(modal.asgi_app()(_endpoint)))


def _smoke_rankllm_image():
    """Credential-free image/import check using 200 captured public stories."""
    import hashlib
    import inspect
    import json
    import os
    import re
    from pathlib import Path

    import yaml
    import tiktoken_ext.openai_public
    from .engine import ReviewedRankLLMPromptBuilder
    from .runtime import configured_token_counter

    cache = Path("/opt/tiktoken-cache/fb374d419588a4632f3f557e76b4b70aebbca790")
    cache_hash = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
    source = inspect.getsource(tiktoken_ext.openai_public.o200k_base)
    url = re.search(r'load_tiktoken_bpe\(\s*"([^"]+)"', source).group(1)
    expected_hash = re.search(r'expected_hash="([0-9a-f]{64})"', source).group(1)
    if hashlib.sha1(url.encode()).hexdigest() != cache.name or expected_hash != cache_hash:
        raise RuntimeError("o200k tokenizer source metadata mismatch")
    if hashlib.sha256(cache.read_bytes()).hexdigest() != cache_hash:
        raise RuntimeError("o200k tokenizer cache mismatch")
    stories = json.loads(Path("/opt/news-curator/smoke-public-200.json").read_text())
    if not isinstance(stories, list) or len(stories) != 200:
        raise RuntimeError("smoke fixture must contain exactly 200 public stories")
    passages = [f"Title: {row['title']}\nSource: {row['source_id']}\nSummary: {row['summary']}"
                for row in stories]
    builder = ReviewedRankLLMPromptBuilder("/opt/vendor/rank_llm/rerank/prompt_templates/rank_gpt_template.yaml")
    prompt = builder.create_prompt(query="personalized news", passages=passages)
    serialized = json.dumps(prompt, ensure_ascii=False, separators=(",", ":"))
    policy = yaml.safe_load(Path("/opt/news-curator/config/ranker-policy-r1.yaml").read_text())
    counter = configured_token_counter(policy, os.environ)
    unknown_encoding_rejected = unknown_model_rejected = False
    try:
        configured_token_counter({**policy, "tokenizer_encoding": "unreviewed"}, os.environ)
    except ValueError:
        unknown_encoding_rejected = True
    try:
        configured_token_counter({**policy, "model": "unknown-model"}, os.environ)
    except ValueError:
        unknown_model_rejected = True
    if not unknown_encoding_rejected or not unknown_model_rejected:
        raise RuntimeError("tokenizer negative guard failed")
    return {"stories": 200, "messages": len(prompt),
            "o200k_tokens": counter(serialized), "cache_key_derived": True,
            "unknown_encoding_rejected": unknown_encoding_rejected,
            "unknown_model_rejected": unknown_model_rejected,
            "prompt_sha256": hashlib.sha256(serialized.encode()).hexdigest()}


if deployment_mode == "smoke":
    smoke_rankllm_image = app.function(image=image, timeout=function_timeout, max_containers=1,
        scaledown_window=1, restrict_modal_access=True, block_network=True,
        include_source=False)(_smoke_rankllm_image)
