"""Structural checks for the locked workflow trust boundaries."""

from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parent.parent
CURATE_PATH = ROOT / ".github" / "workflows" / "curate.yml"
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def _workflow(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _jobs() -> dict[str, dict[str, object]]:
    jobs = _workflow(CURATE_PATH).get("jobs")
    assert isinstance(jobs, dict)
    return jobs


def _steps(job: dict[str, object]) -> list[dict[str, object]]:
    steps = job.get("steps")
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps  # type: ignore[return-value]


def _step_named(job: dict[str, object], name: str) -> dict[str, object]:
    matches = [step for step in _steps(job) if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def _action_steps(job: dict[str, object], owner_action: str) -> list[dict[str, object]]:
    prefix = owner_action + "@"
    return [step for step in _steps(job) if str(step.get("uses", "")).startswith(prefix)]


def _environment_name(job: dict[str, object]) -> str:
    environment = job.get("environment")
    if isinstance(environment, str):
        return environment
    assert isinstance(environment, dict)
    name = environment.get("name")
    assert isinstance(name, str)
    return name


def test_curate_workflow_emits_health_before_pages_upload():
    build = _jobs()["build"]
    steps = _steps(build)
    report = _step_named(build, "Report source freshness")
    upload = _action_steps(build, "actions/upload-pages-artifact")
    assert len(upload) == 1
    assert "--health-report ./source-health.json" in str(_step_named(build, "Build the page")["run"])
    assert "python -m curator.health" in str(report["run"])
    assert "$GITHUB_STEP_SUMMARY" in str(report["run"])
    assert steps.index(report) < steps.index(upload[0])


def test_curate_push_paths_cover_every_runtime_surface() -> None:
    workflow = _workflow(CURATE_PATH)
    trigger = workflow.get("on") or workflow.get(True)
    assert isinstance(trigger, dict)
    push = trigger.get("push")
    assert isinstance(push, dict)
    paths = push.get("paths")
    assert isinstance(paths, list)
    assert {"curator/**", "scripts/**", "static/**"}.issubset(paths)


def test_schedule_produces_one_daily_digest_in_new_york() -> None:
    workflow = _workflow(CURATE_PATH)
    trigger = workflow.get("on") or workflow.get(True)
    assert isinstance(trigger, dict)
    schedule = trigger.get("schedule")
    assert schedule == [{"cron": "17 9 * * *", "timezone": "America/New_York"}]


def test_health_reporting_runs_after_a_failed_build_without_masking_it():
    step = _step_named(_jobs()["build"], "Report source freshness")
    assert step["if"] == "${{ always() }}"
    assert step["continue-on-error"] is True
    assert "if [ -f ./source-health.json ]" in str(step["run"])


def test_rendered_newsletter_privacy_assertion_remains_intact():
    run = str(_step_named(_jobs()["build"], "Verify the rendered page has real content")["run"])
    for locked in (
        'data-newsletter',
        'if "<img" in block or "data-image" in block',
        "is_suspect(candidate)",
        'if "@" in query or "%40" in query.lower()',
    ):
        assert locked in run


def test_secret_jobs_are_read_only_and_secret_steps_are_main_only() -> None:
    jobs = _jobs()
    secret_jobs = {
        name
        for name, job in jobs.items()
        if "${{ secrets." in yaml.safe_dump(job, sort_keys=True)
    }
    assert secret_jobs == {"newsletter", "build", "translation"}
    for name in ("newsletter", "translation"):
        job = jobs[name]
        condition = str(job.get("if", ""))
        assert "github.ref == 'refs/heads/main'" in condition
        assert _environment_name(job) == name
        permissions = job.get("permissions")
        assert isinstance(permissions, dict)
        assert permissions.get("contents") == "read"
        assert "pages" not in permissions
        assert all("cache" not in step.get("with", {}) for step in _steps(job))
    build = jobs["build"]
    assert build["permissions"] == {"contents": "read"}
    assert _environment_name(build) == "personalization"
    materialize = _step_named(build, "Materialize saved-interest ranking")
    assert materialize["if"] == (
        "${{ github.ref == 'refs/heads/main' && "
        "vars.NEWS_CURATOR_PERSONALIZATION_ENABLED == 'true' }}"
    )


def test_translation_is_dark_without_exact_enable_variable() -> None:
    condition = _jobs()["translation"]["if"]
    assert condition == (
        "${{ github.ref == 'refs/heads/main' && "
        "vars.TRANSLATION_WORKFLOW_ENABLED == 'true' }}"
    )


def test_secret_job_checkouts_never_persist_credentials() -> None:
    jobs = _jobs()
    for name in ("newsletter", "build", "translation"):
        checkouts = _action_steps(jobs[name], "actions/checkout")
        assert len(checkouts) == 1
        assert checkouts[0].get("with") == {"persist-credentials": False}


def test_permissions_are_bound_to_the_exact_jobs() -> None:
    jobs = _jobs()
    assert jobs["newsletter"]["permissions"] == {"contents": "read"}
    assert jobs["translation"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert jobs["persist-state"]["permissions"] == {"contents": "write"}
    assert jobs["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    id_token_jobs = {
        name for name, job in jobs.items() if job.get("permissions", {}).get("id-token") == "write"
    }
    assert id_token_jobs == {"translation", "deploy"}
    pages_jobs = {
        name for name, job in jobs.items() if "pages" in job.get("permissions", {})
    }
    assert pages_jobs == {"deploy"}


def test_artifact_dependencies_are_bound_to_exact_jobs() -> None:
    jobs = _jobs()
    assert jobs["translation"]["needs"] == "source-snapshot"
    assert set(jobs["build"]["needs"]) == {
        "source-snapshot",
        "newsletter",
        "translation",
    }
    assert jobs["persist-state"]["needs"] == "build"
    assert jobs["deploy"]["needs"] == "build"
    assert jobs["deploy"]["if"] == (
        "${{ github.ref == 'refs/heads/main' && needs.build.result == 'success' }}"
    )

    translation_uploads = _action_steps(jobs["translation"], "actions/upload-artifact")
    assert len(translation_uploads) == 1
    assert translation_uploads[0]["with"] == {
        "name": "translation-artifact",
        "path": "${{ runner.temp }}/translation_artifact.json",
        "if-no-files-found": "error",
        "retention-days": 2,
    }
    build_downloads = _action_steps(jobs["build"], "actions/download-artifact")
    assert {step["with"]["name"] for step in build_downloads} == {
        "source-snapshot",
        "newsletter-artifact",
        "translation-artifact",
    }
    all_uploads = [
        step
        for job in jobs.values()
        for step in _action_steps(job, "actions/upload-artifact")
    ]
    assert "interest-ranking" not in yaml.safe_dump(all_uploads, sort_keys=True)
    state_uploads = [
        step for step in _action_steps(jobs["build"], "actions/upload-artifact")
        if step["with"]["name"] == "repository-state"
    ]
    assert len(state_uploads) == 1
    state_downloads = _action_steps(jobs["persist-state"], "actions/download-artifact")
    assert len(state_downloads) == 1
    assert state_downloads[0]["with"]["name"] == "repository-state"


def test_translation_upload_cannot_include_raw_or_debug_outputs() -> None:
    translation = _jobs()["translation"]
    uploads = _action_steps(translation, "actions/upload-artifact")
    assert [step["with"]["path"] for step in uploads] == [
        "${{ runner.temp }}/translation_artifact.json"
    ]
    dumped = yaml.safe_dump(uploads, sort_keys=True).lower()
    for forbidden in ("response", "trace", "cache", "debug", "runner_temp"):
        assert forbidden not in dumped


def test_build_validates_translation_and_fails_soft_to_originals() -> None:
    build = _jobs()["build"]
    validator = _step_named(build, "Validate downloaded translation artifact")
    assert validator["continue-on-error"] is True
    assert "translation artifact invalid; building originals" in str(validator["run"])
    command = str(_step_named(build, "Build the page")["run"])
    assert 'if [ -f "$RUNNER_TEMP/validated-translation.json" ]' in command
    assert '--translation-artifact "$RUNNER_TEMP/validated-translation.json"' in command


def test_translation_job_has_explicit_workstream_i_dependency() -> None:
    step = _step_named(_jobs()["translation"], "Produce translation candidate")
    command = str(step["run"])
    assert "test -f scripts/run_translation_job.py" in command
    assert "python scripts/run_translation_job.py" in command
    assert '--google-access-token-file "$RUNNER_TEMP/google-access-token"' in command
    assert '--source-snapshot "$RUNNER_TEMP/source-snapshot.json"' in command


def test_oidc_identity_inputs_cross_the_expression_boundary_through_env() -> None:
    step = _step_named(_jobs()["translation"], "Acquire short-lived Google access")
    assert step["env"] == {
        "GOOGLE_WORKLOAD_IDENTITY_PROVIDER": "${{ vars.GOOGLE_WORKLOAD_IDENTITY_PROVIDER }}",
        "GOOGLE_TRANSLATION_SERVICE_ACCOUNT": "${{ vars.GOOGLE_TRANSLATION_SERVICE_ACCOUNT }}",
    }
    command = str(step["run"])
    assert '${{ vars.' not in command
    assert '--provider "$GOOGLE_WORKLOAD_IDENTITY_PROVIDER"' in command
    assert '--service-account "$GOOGLE_TRANSLATION_SERVICE_ACCOUNT"' in command


def test_build_materializes_auth_callback_without_overwriting_it() -> None:
    build = _jobs()["build"]
    steps = _steps(build)
    materialize = _step_named(build, "Materialize auth callback")
    copy = _step_named(build, "Copy static pages")
    assert steps.index(materialize) < steps.index(copy)
    assert materialize["env"] == {
        "NEWS_CURATOR_PERSONALIZATION_ENABLED": "${{ vars.NEWS_CURATOR_PERSONALIZATION_ENABLED }}",
        "NEWS_CURATOR_SUPABASE_URL": "${{ vars.NEWS_CURATOR_SUPABASE_URL }}",
        "NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY": "${{ vars.NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY }}",
    }
    command = str(materialize["run"])
    assert "python scripts/build_auth_callback.py" in command
    assert 'if [ "$NEWS_CURATOR_PERSONALIZATION_ENABLED" = "true" ]' in command
    assert "personalization_link_args=(--site-index ./site/index.html)" in command
    assert '"${personalization_link_args[@]}"' in command
    assert '--output ./site/auth/callback/index.html' in command
    assert 'if [ -z "$NEWS_CURATOR_SUPABASE_URL" ] && [ -z "$NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY" ]' in command
    assert 'elif [ -z "$NEWS_CURATOR_SUPABASE_URL" ] || [ -z "$NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY" ]' in command
    assert 'test -n "$NEWS_CURATOR_SUPABASE_URL"' not in command
    assert 'test -n "$NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY"' not in command
    copy_command = str(copy["run"])
    assert "static/auth/client.js" in copy_command
    assert "static/auth/styles.css" in copy_command
    assert "static/privacy.html" in copy_command
    assert "cp -R static/. site/" not in copy_command


def test_custom_domain_change_triggers_the_deploy_workflow() -> None:
    workflow_text = (ROOT / ".github/workflows/curate.yml").read_text()
    workflow = yaml.safe_load(workflow_text)
    assert "CNAME" in workflow[True]["push"]["paths"]
    assert "if [ -f CNAME ]; then cp CNAME site/CNAME; fi" in workflow_text


def test_one_source_snapshot_drives_translation_and_publication() -> None:
    jobs = _jobs()
    collector = _step_named(jobs["source-snapshot"], "Collect source snapshot")
    assert "python scripts/source_snapshot.py collect" in str(collector["run"])
    source_uploads = _action_steps(jobs["source-snapshot"], "actions/upload-artifact")
    assert len(source_uploads) == 1
    assert source_uploads[0]["with"] == {
        "name": "source-snapshot",
        "path": "${{ runner.temp }}/source-snapshot.json",
        "if-no-files-found": "error",
        "retention-days": 1,
    }
    build_command = str(_step_named(jobs["build"], "Build the page")["run"])
    assert '--source-snapshot "$RUNNER_TEMP/source-snapshot.json"' in build_command
    assert '--interest-ranking-artifact "$RUNNER_TEMP/interest-ranking.json"' in build_command


def test_personalization_scores_never_leave_the_build_job() -> None:
    job = _jobs()["build"]
    step = _step_named(job, "Materialize saved-interest ranking")
    assert step["env"] == {
        "NEWS_CURATOR_OWNER_USER_ID": "${{ secrets.NEWS_CURATOR_OWNER_USER_ID }}",
        "NEWS_CURATOR_SUPABASE_SECRET_KEY": "${{ secrets.NEWS_CURATOR_SUPABASE_SECRET_KEY }}",
        "NEWS_CURATOR_SUPABASE_URL": "${{ vars.NEWS_CURATOR_SUPABASE_URL }}",
    }
    command = str(step["run"])
    assert "python scripts/build_interest_ranking.py build" in command
    assert '--source-snapshot "$RUNNER_TEMP/source-snapshot.json"' in command
    assert '--output "$RUNNER_TEMP/interest-ranking.json"' in command
    assert step["if"] == (
        "${{ github.ref == 'refs/heads/main' && "
        "vars.NEWS_CURATOR_PERSONALIZATION_ENABLED == 'true' }}"
    )
    workflow = CURATE_PATH.read_text(encoding="utf-8")
    assert "name: interest-ranking-artifact" not in workflow


def test_enabled_main_build_requires_the_interest_ranking_file() -> None:
    step = _step_named(_jobs()["build"], "Validate saved-interest ranking")
    assert step["env"] == {
        "REQUIRE_PERSONALIZATION": (
            "${{ github.ref == 'refs/heads/main' && "
            "vars.NEWS_CURATOR_PERSONALIZATION_ENABLED == 'true' }}"
        )
    }
    command = str(step["run"])
    assert "python scripts/build_interest_ranking.py validate" in command
    assert 'if [ "$REQUIRE_PERSONALIZATION" = "true" ]' in command
    assert "saved-interest ranking artifact is required on main" in command


def test_translation_validators_accept_the_domain_google_model_resource() -> None:
    workflow = CURATE_PATH.read_text(encoding="utf-8")
    expected = r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}"
    assert workflow.count(expected) == 2


def test_every_action_is_pinned_to_a_full_commit_sha() -> None:
    for path in (CI_PATH, CURATE_PATH):
        jobs = _workflow(path).get("jobs")
        assert isinstance(jobs, dict)
        for name, job in jobs.items():
            assert isinstance(job, dict)
            for step in _steps(job):
                if "uses" not in step:
                    continue
                action, separator, ref = str(step["uses"]).partition("@")
                assert separator, f"{path.name}:{name}:{action} is unpinned"
                assert re.fullmatch(r"[0-9a-f]{40}", ref), (
                    f"{path.name}:{name}:{action} is not pinned to a full SHA"
                )
