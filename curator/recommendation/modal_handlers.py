"""Remote Modal callables with no deployment-host environment dependency."""

# Where the image puts the tree. Everything below is discovered from the ranker
# policy relative to it; no handler names a config file of its own.
IMAGE_ROOT = "/opt/news-curator"


def image_inputs(root=IMAGE_ROOT):
    """The policy and the prompt template, discovered exactly as service mode does.

    Smoke mode used to name config/rankllm-news-curator-json.yaml directly. The
    staged config set follows the policy now, so a hardcoded second name is a
    file that may not be in the image: it would have died lazily inside
    engine.prepare() rather than at boot.
    """
    import os
    from pathlib import Path

    from .runtime import load_ranker_policy, policy_reference

    path, policy = load_ranker_policy(os.environ, root=Path(root))
    return policy, policy_reference(path, policy["prompt_template"])


def endpoint():
    from .runtime import build_application
    return build_application()


def smoke_rankllm_image():
    """Credential-free shipping-engine preparation check using public stories."""
    import hashlib
    import inspect
    import json
    import os
    import re
    from datetime import datetime
    from pathlib import Path

    import tiktoken_ext.openai_public
    from curator.contracts.ranking_request import ModelRankingInput, RankingCandidate
    from .engine import OpenAIRankLLMEngine, ReviewedRankLLMPromptBuilder
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
    root = Path(IMAGE_ROOT)
    stories = json.loads((root / "smoke-public-200.json").read_text())
    if not isinstance(stories, list) or len(stories) != 200:
        raise RuntimeError("smoke fixture must contain exactly 200 public stories")
    policy, prompt_template = image_inputs(root)
    counter = configured_token_counter(policy, os.environ)
    count = policy["candidate_limit"]
    candidates = tuple(RankingCandidate(row["story_id"], row["story_id"], row["story_id"], row["title"],
        row["summary"], row["source_id"], row["language"],
        datetime.fromisoformat(row["published_at"].replace("Z", "+00:00"))) for row in stories[:count])
    if len(candidates) != count:
        raise RuntimeError("smoke fixture does not satisfy configured candidate count")
    builder = ReviewedRankLLMPromptBuilder(str(prompt_template))
    engine = OpenAIRankLLMEngine(prompt_builder=builder, endpoint=policy["endpoint"], api_key="disabled",
        model=policy["model"], maximum_output_tokens=policy["maximum_output_tokens"],
        reasoning_effort=policy["reasoning_effort"], verbosity=policy["verbosity"],
        reasoning_token_allowance=policy["reasoning_token_allowance"],
        prompt_framing_token_allowance=policy["prompt_framing_token_allowance"],
        prompt_framing_tokens_per_message=policy["prompt_framing_tokens_per_message"], token_counter=counter,
        client_factory=lambda: (_ for _ in ()).throw(RuntimeError("smoke must not call provider")))
    prepared = engine.prepare(ModelRankingInput("current public news", candidates, (), 0, 0, 1, 1,
        policy["prompt_revision"], policy["model"]))
    serialized = json.dumps(prepared.prompt, ensure_ascii=False, separators=(",", ":"))
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
    return {"stories": len(candidates), "messages": len(prepared.prompt),
            "input_tokens_bound": prepared.input_tokens_bound,
            "o200k_prompt_tokens": counter(serialized), "cache_key_derived": True,
            "unknown_encoding_rejected": unknown_encoding_rejected,
            "unknown_model_rejected": unknown_model_rejected,
            "prompt_sha256": hashlib.sha256(serialized.encode()).hexdigest()}
