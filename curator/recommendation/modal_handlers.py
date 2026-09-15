"""Remote Modal callables with no deployment-host environment dependency."""


def endpoint():
    from .runtime import build_application
    return build_application()


def smoke_rankllm_image():
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
