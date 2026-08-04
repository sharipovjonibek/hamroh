"""One-time ChatGPT login helper for Shahnoza's isolated Codex identity.

Run inside the dedicated Compose service::

    docker compose run --rm codex-auth

No Telegram or integration secrets are loaded by that service. The resulting
OAuth cache is written to the shared ``codex-home`` volume and reused by the
agent container. Browser login is the default; device-code login is an explicit
fallback for environments where the localhost callback cannot be used.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from openai_codex import Codex, CodexConfig


def _client() -> Codex:
    codex_home = Path(os.environ.get("CODEX_HOME", "/var/lib/codex"))
    codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    codex_home.chmod(0o700)
    return Codex(
        CodexConfig(
            codex_bin=os.environ.get("CODEX_BIN") or None,
            cwd=os.environ.get("HAMROH_CODEX_CWD", "/app"),
            env={"CODEX_HOME": str(codex_home)},
            config_overrides=(
                "features.shell_tool=false",
                "features.unified_exec=false",
                "features.multi_agent=false",
                "features.apps=false",
                "features.remote_plugin=false",
            ),
        )
    )


def _account_label(codex: Codex, *, refresh_token: bool) -> str | None:
    """Return a validated account label, or ``None`` when logged out."""

    state = codex.account(refresh_token=refresh_token)
    if state.account is None:
        return None
    account = state.account.root
    account_type = getattr(account, "type", "unknown")
    plan = getattr(account, "plan_type", None)
    plan_value = getattr(plan, "value", plan)
    return f"logged in ({account_type}, plan={plan_value or 'unknown'})"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Authenticate Shahnoza's Codex runtime"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--status", action="store_true", help="show login status without changing it"
    )
    mode.add_argument(
        "--force",
        action="store_true",
        help="discard the stored login and authenticate again",
    )
    parser.add_argument(
        "--device-code",
        action="store_true",
        help="use device-code login instead of the default browser URL flow",
    )
    args = parser.parse_args()

    with _client() as codex:
        if args.status:
            try:
                label = _account_label(codex, refresh_token=True)
            except Exception as exc:
                print(f"login invalid: {exc}")
                raise SystemExit(1) from exc
            print(label or "not logged in")
            return

        if args.force:
            codex.logout()
        else:
            try:
                label = _account_label(codex, refresh_token=True)
            except Exception as exc:
                print(f"Stored Codex login is invalid; clearing it: {exc}")
                codex.logout()
            else:
                if label is not None:
                    print(f"Codex is already {label}.")
                    return

        if args.device_code:
            device_login = codex.login_chatgpt_device_code()
            print("Open this page in a browser:")
            print(device_login.verification_url)
            print(f"Enter code: {device_login.user_code}")
            print("Waiting for sign-in to finish…")
            completed = device_login.wait()
        else:
            browser_login = codex.login_chatgpt()
            print("Open this authorization URL in a browser:")
            print(browser_login.auth_url)
            print("Waiting for sign-in to finish…")
            completed = browser_login.wait()
        if not completed.success:
            raise SystemExit(f"Codex login failed: {completed.error or 'unknown error'}")

    # The app-server process used for login can retain its pre-login account
    # snapshot. Verify with a fresh SDK process so the result reflects the
    # credentials that were just persisted to CODEX_HOME.
    with _client() as codex:
        try:
            label = _account_label(codex, refresh_token=True)
        except Exception as exc:
            raise SystemExit(f"Codex login verification failed: {exc}") from exc
    if label is None:
        raise SystemExit("Codex login verification failed: no account was stored")
    print(f"Codex login complete: {label}")


if __name__ == "__main__":
    main()
