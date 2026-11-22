"""Credentials shared by the bridge, relay and setup page."""

import hmac
import re
import secrets


NAME = re.compile(r"[a-z][a-z0-9_-]{0,23}")
ACCOUNT_ID = re.compile(r"[0-9a-f]{32}")


def token(key: bytes, purpose: str, length: int = 32) -> str:
    return hmac.digest(key, purpose.encode(), "sha256").hex()[:length]


def valid_token(value: str, expected: str) -> bool:
    return value.isascii() and hmac.compare_digest(value, expected)


def issue_account_token(key: bytes) -> str:
    account = secrets.token_hex(16)
    return f"ocm_{account}.{token(key, 'account/' + account, 64)}"


def verify_account_token(key: bytes, credential: str) -> str | None:
    prefix, separator, signature = credential.partition(".")
    account = prefix.removeprefix("ocm_")
    if separator and prefix.startswith("ocm_") and ACCOUNT_ID.fullmatch(account):
        if valid_token(signature, token(key, "account/" + account, 64)):
            return account
    return None


def agent_path(key: bytes, public_url: str | None, purpose: str, name: str, account: str | None = None) -> str:
    identity = f"{account}/{name}" if account else name
    prefix = f"/u/{account}" if account else ""
    # Keep existing relay credentials usable on the same domain.
    label = f"relay/{public_url}/{purpose}/{identity}" if public_url else f"{purpose}/{identity}"
    segment = "i" if purpose == "install" else "a"
    return f"{prefix}/{segment}/{name}/{token(key, label)}"


def valid_agent_request(key: bytes, public_url: str | None, purpose: str, name: str, account: str | None, credential: str) -> bool:
    if not NAME.fullmatch(name) or (account is not None and not ACCOUNT_ID.fullmatch(account)):
        return False
    return valid_token(credential, agent_path(key, public_url, purpose, name, account).rsplit("/", 1)[1])


def installer_command(url: str) -> str:
    return f"wget -f {url} /tmp/oc-mcp.lua && /tmp/oc-mcp.lua install"
