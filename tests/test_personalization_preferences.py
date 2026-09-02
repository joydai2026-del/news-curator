import json

import pytest

from curator.personalization import AuthConfig, AuthError, PreferenceClient, PreferenceInput, Session
from curator.personalization.preferences import JsonRestTransport


PUBLIC_KEY = "sb_publishable_test"
CONFIG = AuthConfig("https://example.supabase.co", PUBLIC_KEY)


def record(*, user_id="user-a", revision=0, locale="en") -> dict:
    return {
        "user_id": user_id,
        "revision": revision,
        "locale": locale,
        "interests": ["agents"],
        "saved_searches": [{"id": "daily", "query": "agent news", "enabled": True}],
        "created_at": "2026-08-29T12:00:00Z",
        "updated_at": "2026-08-29T12:00:00Z",
    }


def update(*, expected_revision=0, locale="en") -> PreferenceInput:
    return PreferenceInput.from_mapping(
        {
            "expected_revision": expected_revision,
            "locale": locale,
            "interests": ["agents"],
            "saved_searches": [{"id": "daily", "query": "agent news", "enabled": True}],
        }
    )


class FakeRestTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body=None, timeout=15.0):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        return self.responses.pop(0)


def session(user="user-a") -> Session:
    return Session("caller-access", "caller-refresh", expires_at=9999999999, user_id=user)


def exception_graph_text(error: BaseException) -> str:
    pending: list[object] = [error]
    seen: set[int] = set()
    rendered: list[str] = []
    while pending:
        value = pending.pop()
        if id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, BaseException):
            rendered.extend((str(value), repr(value)))
            pending.extend(value.args)
            if value.__cause__ is not None:
                pending.append(value.__cause__)
            if value.__context__ is not None:
                pending.append(value.__context__)
            pending.extend(vars(value).values())
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, (list, tuple, set)):
            pending.extend(value)
        elif isinstance(value, bytes):
            rendered.append(value.decode("utf-8", errors="replace"))
        elif isinstance(value, str):
            rendered.append(value)
    return "\n".join(rendered)


def test_malformed_preference_json_has_no_token_in_exception_graph() -> None:
    malformed = b'[{"access_token":"PREF_ACCESS_SENTINEL","refresh_token":"PREF_REFRESH_SENTINEL"}'

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://example.supabase.co/rest/v1/user_preferences"

        def read(self, _limit):
            return malformed

    class FakeOpener:
        def open(self, *_args, **_kwargs):
            return FakeResponse()

    transport = JsonRestTransport()
    transport._opener = FakeOpener()
    with pytest.raises(AuthError, match="could not be reached") as caught:
        transport.request(
            "GET",
            "https://example.supabase.co/rest/v1/user_preferences",
            headers={"authorization": "Bearer caller-access"},
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    graph = exception_graph_text(caught.value)
    assert "PREF_ACCESS_SENTINEL" not in graph
    assert "PREF_REFRESH_SENTINEL" not in graph


def test_get_reads_only_rls_visible_caller_row_with_public_credentials(capsys) -> None:
    transport = FakeRestTransport([(200, [record()])])
    preference = PreferenceClient(CONFIG, transport=transport).get(session())
    assert preference.user_id == "user-a"
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"].startswith("https://example.supabase.co/rest/v1/user_preferences?")
    assert call["headers"]["apikey"] == PUBLIC_KEY
    assert call["headers"]["authorization"] == "Bearer caller-access"
    assert "service_role" not in json.dumps(call)
    output = capsys.readouterr()
    assert "caller-access" not in output.out + output.err
    assert "caller-refresh" not in output.out + output.err


def test_get_returns_none_for_missing_row_and_rejects_multiple_rows() -> None:
    assert PreferenceClient(CONFIG, transport=FakeRestTransport([(200, [])])).get(session()) is None
    with pytest.raises(AuthError, match="could not be read"):
        PreferenceClient(CONFIG, transport=FakeRestTransport([(200, [record(), record(user_id="user-b")])])).get(
            session()
        )


def test_get_rejects_row_for_a_different_session_user() -> None:
    with pytest.raises(AuthError, match="response was invalid"):
        PreferenceClient(CONFIG, transport=FakeRestTransport([(200, [record(user_id="user-b")])])).get(session())


def test_set_uses_cas_rpc_then_reads_updated_row() -> None:
    transport = FakeRestTransport(
        [
            (200, {"status": "updated", "revision": 3}),
            (200, [record(revision=3, locale="zh")]),
        ]
    )
    result = PreferenceClient(CONFIG, transport=transport).set(session(), update(expected_revision=2, locale="zh"))
    assert result["status"] == "updated"
    assert result["preference"]["revision"] == 3
    rpc = transport.calls[0]
    assert rpc["url"].endswith("/rest/v1/rpc/compare_and_swap_user_preferences")
    assert rpc["body"] == {
        "expected_revision": 2,
        "new_locale": "zh",
        "new_interests": ["agents"],
        "new_saved_searches": [{"id": "daily", "query": "agent news", "enabled": True}],
    }


def test_set_returns_conflict_without_direct_table_update() -> None:
    transport = FakeRestTransport([(200, {"status": "conflict", "revision": 4})])
    result = PreferenceClient(CONFIG, transport=transport).set(session(), update(expected_revision=3))
    assert result == {"status": "conflict", "revision": 4}
    assert len(transport.calls) == 1
    assert transport.calls[0]["method"] == "POST"


def test_conflict_output_drops_unexpected_server_fields() -> None:
    transport = FakeRestTransport([(200, {"status": "conflict", "revision": 4, "access_token": "do-not-echo"})])
    result = PreferenceClient(CONFIG, transport=transport).set(session(), update(expected_revision=3))
    assert result == {"status": "conflict", "revision": 4}


def test_initial_revision_zero_creates_caller_row_after_not_found() -> None:
    transport = FakeRestTransport([(200, {"status": "not_found"}), (201, [record()])])
    result = PreferenceClient(CONFIG, transport=transport).set(session(), update())
    assert result["status"] == "created"
    insert = transport.calls[1]
    assert insert["url"].endswith("/rest/v1/user_preferences")
    assert insert["body"]["user_id"] == "user-a"
    assert "revision" not in insert["body"]
    assert insert["headers"]["prefer"] == "return=representation"


def test_initial_insert_race_returns_current_conflict() -> None:
    transport = FakeRestTransport([(200, {"status": "not_found"}), (409, None), (200, [record(revision=1)])])
    result = PreferenceClient(CONFIG, transport=transport).set(session(), update())
    assert result == {"status": "conflict", "revision": 1}


@pytest.mark.parametrize(
    "mutation",
    [
        {"unknown": True},
        {"expected_revision": -1},
        {"locale": "fr"},
        {"interests": ["x" * 81]},
        {"saved_searches": [{"id": "x", "query": "q", "enabled": True, "extra": 1}]},
        {"saved_searches": [{"id": "x", "query": "q", "enabled": "yes"}]},
    ],
)
def test_invalid_set_input_fails_before_transport(mutation) -> None:
    value = {
        "expected_revision": 0,
        "locale": "en",
        "interests": [],
        "saved_searches": [],
    }
    value.update(mutation)
    with pytest.raises(ValueError):
        PreferenceInput.from_mapping(value)


def test_cli_get_and_set_route_safe_json_without_tokens(monkeypatch, tmp_path, capsys) -> None:
    import scripts.personalization_cli as cli

    saved_session = session()

    class FakeAuth:
        def __init__(self, config, storage):
            pass

        def valid_session(self):
            return saved_session

    class FakeClient:
        def __init__(self, config):
            pass

        def get(self, current):
            return PreferenceClient(
                CONFIG,
                transport=FakeRestTransport([(200, [record()])]),
            ).get(current)

        def set(self, current, preference):
            return {"status": "conflict", "revision": preference.expected_revision + 1}

    monkeypatch.setattr(cli, "AgentAuth", FakeAuth)
    monkeypatch.setattr(cli, "PreferenceClient", FakeClient)
    monkeypatch.setattr(cli, "MacOSKeychainStorage", lambda account: object())
    monkeypatch.setenv("NEWS_CURATOR_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("NEWS_CURATOR_SUPABASE_PUBLISHABLE_KEY", PUBLIC_KEY)

    assert cli.main(["get"]) == 0
    get_output = capsys.readouterr().out
    assert json.loads(get_output)["preference"]["user_id"] == "user-a"

    input_file = tmp_path / "preferences.json"
    input_file.write_text(
        json.dumps(
            {
                "expected_revision": 3,
                "locale": "en",
                "interests": [],
                "saved_searches": [],
            }
        )
    )
    assert cli.main(["set", "--input", str(input_file)]) == 0
    set_output = capsys.readouterr().out
    assert json.loads(set_output) == {"status": "conflict", "revision": 4}
    combined = get_output + set_output
    assert "caller-access" not in combined
    assert "caller-refresh" not in combined
