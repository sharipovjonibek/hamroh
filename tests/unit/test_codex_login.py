"""Tests for the isolated Codex browser-login helper."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hamroh import codex_login


def _account_state(*, present: bool = True) -> SimpleNamespace:
    account = None
    if present:
        account = SimpleNamespace(
            root=SimpleNamespace(
                type="chatgpt",
                plan_type=SimpleNamespace(value="plus"),
            )
        )
    return SimpleNamespace(account=account)


class _FakeLogin:
    auth_url = "https://example.test/browser"
    verification_url = "https://example.test/device"
    user_code = "TEST-CODE"

    def __init__(self) -> None:
        self.wait_calls = 0

    def wait(self) -> SimpleNamespace:
        self.wait_calls += 1
        return SimpleNamespace(success=True, error=None)


class _FakeCodex:
    def __init__(self, account_results: list[object]) -> None:
        self.account_results = list(account_results)
        self.account_calls: list[bool] = []
        self.logout_calls = 0
        self.browser_login_calls = 0
        self.device_login_calls = 0
        self.login = _FakeLogin()

    def __enter__(self) -> "_FakeCodex":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def account(self, *, refresh_token: bool = False) -> SimpleNamespace:
        self.account_calls.append(refresh_token)
        result = self.account_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]

    def logout(self) -> None:
        self.logout_calls += 1

    def login_chatgpt(self) -> _FakeLogin:
        self.browser_login_calls += 1
        return self.login

    def login_chatgpt_device_code(self) -> _FakeLogin:
        self.device_login_calls += 1
        return self.login


def test_status_forces_token_refresh(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeCodex([_account_state()])
    monkeypatch.setattr(codex_login, "_client", lambda: fake)
    monkeypatch.setattr("sys.argv", ["hamroh.codex_login", "--status"])

    codex_login.main()

    assert fake.account_calls == [True]
    assert "logged in (chatgpt, plan=plus)" in capsys.readouterr().out


def test_status_fails_for_revoked_refresh_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeCodex([RuntimeError("refresh token revoked")])
    monkeypatch.setattr(codex_login, "_client", lambda: fake)
    monkeypatch.setattr("sys.argv", ["hamroh.codex_login", "--status"])

    with pytest.raises(SystemExit) as exc_info:
        codex_login.main()

    assert exc_info.value.code == 1
    assert "login invalid: refresh token revoked" in capsys.readouterr().out


def test_default_login_replaces_invalid_stored_auth(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    login_client = _FakeCodex([RuntimeError("refresh token revoked")])
    verify_client = _FakeCodex([_account_state()])
    clients = iter([login_client, verify_client])
    monkeypatch.setattr(codex_login, "_client", lambda: next(clients))
    monkeypatch.setattr("sys.argv", ["hamroh.codex_login"])

    codex_login.main()

    assert login_client.logout_calls == 1
    assert login_client.browser_login_calls == 1
    assert login_client.device_login_calls == 0
    assert login_client.login.wait_calls == 1
    assert login_client.account_calls == [True]
    assert verify_client.account_calls == [True]
    output = capsys.readouterr().out
    assert "Stored Codex login is invalid; clearing it" in output
    assert "https://example.test/browser" in output
    assert "Codex login complete" in output


def test_device_code_login_is_explicit_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    login_client = _FakeCodex([_account_state(present=False)])
    verify_client = _FakeCodex([_account_state()])
    clients = iter([login_client, verify_client])
    monkeypatch.setattr(codex_login, "_client", lambda: next(clients))
    monkeypatch.setattr("sys.argv", ["hamroh.codex_login", "--device-code"])

    codex_login.main()

    assert login_client.browser_login_calls == 0
    assert login_client.device_login_calls == 1
    assert verify_client.account_calls == [True]
    output = capsys.readouterr().out
    assert "https://example.test/device" in output
    assert "TEST-CODE" in output


@pytest.mark.parametrize(
    ("verification_result", "error_match"),
    [
        (_account_state(present=False), "no account was stored"),
        (RuntimeError("refresh token revoked"), "refresh token revoked"),
    ],
)
def test_login_rejects_failed_fresh_verification(
    monkeypatch: pytest.MonkeyPatch,
    verification_result: object,
    error_match: str,
) -> None:
    login_client = _FakeCodex([])
    verify_client = _FakeCodex([verification_result])
    clients = iter([login_client, verify_client])
    monkeypatch.setattr(codex_login, "_client", lambda: next(clients))
    monkeypatch.setattr("sys.argv", ["hamroh.codex_login", "--force"])

    with pytest.raises(SystemExit, match=error_match):
        codex_login.main()

    assert login_client.logout_calls == 1
    assert login_client.login.wait_calls == 1
    assert verify_client.account_calls == [True]


def test_valid_stored_login_does_not_prompt_again(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = _FakeCodex([_account_state()])
    monkeypatch.setattr(codex_login, "_client", lambda: fake)
    monkeypatch.setattr("sys.argv", ["hamroh.codex_login"])

    codex_login.main()

    assert fake.logout_calls == 0
    assert fake.browser_login_calls == 0
    assert fake.device_login_calls == 0
    assert "Codex is already logged in" in capsys.readouterr().out
