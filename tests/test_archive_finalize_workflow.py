from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "curate.yml"


def _jobs() -> dict:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    return workflow["jobs"]


def _steps(job: dict) -> list[dict]:
    return job["steps"]


def _step(job: dict, name: str) -> dict:
    matches = [item for item in _steps(job) if item.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def test_build_emits_only_the_bounded_archive_candidate_artifact() -> None:
    build = _jobs()["build"]
    command = str(_step(build, "Build the page")["run"])
    assert '--archive-candidate "$RUNNER_TEMP/archive-candidate.json"' in command
    assert '--build-nonce "$GITHUB_RUN_ID:$GITHUB_RUN_ATTEMPT"' in command
    assert '--commit-sha "$GITHUB_SHA"' in command
    uploads = [
        item
        for item in _steps(build)
        if str(item.get("uses", "")).startswith("actions/upload-artifact@")
        and item.get("with", {}).get("name") == "archive-candidate"
    ]
    assert len(uploads) == 1
    assert uploads[0]["with"] == {
        "name": "archive-candidate",
        "path": "${{ runner.temp }}/archive-candidate.json",
        "if-no-files-found": "error",
        "retention-days": 2,
    }
    steps = _steps(build)
    copy_static = _step(build, "Copy static pages")
    stamp = _step(build, "Stamp archive candidate with final deployed page")
    verify = _step(build, "Verify the rendered page has real content")
    assert steps.index(copy_static) < steps.index(stamp) < steps.index(verify)
    assert "python -m curator.archive_candidate" in str(stamp["run"])
    assert '"$RUNNER_TEMP/archive-candidate.json"' in str(stamp["run"])
    assert "--stamp-site ./site/index.html" in str(stamp["run"])
    assert steps.index(stamp) < steps.index(uploads[0])


def test_archive_finalization_is_bound_to_successful_exact_deploy() -> None:
    jobs = _jobs()
    deploy = jobs["deploy"]
    finalizer = jobs["finalize-archive"]
    assert deploy["outputs"] == {"page_url": "${{ steps.deployment.outputs.page_url }}"}
    assert set(finalizer["needs"]) == {"build", "deploy"}
    assert finalizer["if"] == (
        "${{ !cancelled() && github.ref == 'refs/heads/main' && "
        "vars.NEWS_CURATOR_PERSONALIZATION_ENABLED == 'true' && "
        "needs.build.result == 'success' && needs.deploy.result == 'success' }}"
    )
    assert finalizer["permissions"] == {"contents": "read"}
    assert finalizer["environment"] == "personalization"
    checkout = next(
        item
        for item in _steps(finalizer)
        if str(item.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"] == {
        "ref": "${{ github.sha }}",
        "persist-credentials": False,
    }
    command = _step(finalizer, "Finalize deployed archive and prune expired history")
    assert command["env"] == {
        "DEPLOYED_URL": "${{ needs.deploy.outputs.page_url }}",
        "NEWS_CURATOR_SUPABASE_URL": "${{ vars.NEWS_CURATOR_SUPABASE_URL }}",
        "NEWS_CURATOR_SUPABASE_SECRET_KEY": (
            "${{ secrets.NEWS_CURATOR_SUPABASE_SECRET_KEY }}"
        ),
        "NEWS_CURATOR_DEPLOY_VERIFY_ATTEMPTS": (
            "${{ vars.NEWS_CURATOR_DEPLOY_VERIFY_ATTEMPTS || '6' }}"
        ),
        "NEWS_CURATOR_DEPLOY_VERIFY_DELAY_SECONDS": (
            "${{ vars.NEWS_CURATOR_DEPLOY_VERIFY_DELAY_SECONDS || '10' }}"
        ),
    }
    assert "python -m curator.archive_finalize" in str(command["run"])
    assert '"$DEPLOYED_URL"' in str(command["run"])
    assert '--expected-commit "$GITHUB_SHA"' in str(command["run"])


def test_unconfigured_personalization_skips_the_secret_archive_job() -> None:
    finalizer = _jobs()["finalize-archive"]
    condition = str(finalizer["if"])
    assert "vars.NEWS_CURATOR_PERSONALIZATION_ENABLED == 'true'" in condition
    assert finalizer.get("continue-on-error") is None


def test_configured_personalization_keeps_archive_finalization_mandatory() -> None:
    finalizer = _jobs()["finalize-archive"]
    condition = str(finalizer["if"])
    assert "vars.NEWS_CURATOR_PERSONALIZATION_ENABLED == 'true'" in condition
    assert "needs.build.result == 'success'" in condition
    assert "needs.deploy.result == 'success'" in condition
    finalization = _step(finalizer, "Finalize deployed archive and prune expired history")
    assert finalization.get("if") is None
    assert finalization.get("continue-on-error") is None


def test_archive_service_secret_is_not_exposed_to_deploy() -> None:
    jobs = _jobs()
    assert "NEWS_CURATOR_SUPABASE_SECRET_KEY" not in yaml.safe_dump(
        jobs["deploy"], sort_keys=True
    )
    assert "pages" not in jobs["finalize-archive"]["permissions"]
    assert "id-token" not in jobs["finalize-archive"]["permissions"]
