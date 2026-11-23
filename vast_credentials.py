"""Isolated Vast/Hugging Face credential storage and SSH identity helpers."""
import os
from pathlib import Path

SERVICE = "AXDAR Studio Vast"
VAST_IDENTITY = Path.home() / ".ssh" / "id_axdar_vast"
SSH_CONNECT_SECONDS = 30
SSH_CHECK_SECONDS = 45


def identity(target):
    return str(target.get("id") or f"{target['host']}:{target['port']}")


def identity_options(target):
    if not target.get("id") or not VAST_IDENTITY.is_file():
        return []
    files = [VAST_IDENTITY] + [VAST_IDENTITY.with_name(name) for name in ("id_ed25519", "id_rsa", "id_ecdsa")]
    return [arg for file in files if file.is_file() for arg in ("-i", str(file))]


def api_key(value=None):
    # Explicit backend prevents falling back to a plaintext keyring.
    from keyring.backends.Windows import WinVaultKeyring
    vault = WinVaultKeyring()
    if value is not None:
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 4096:
            raise ValueError("Enter a valid Vast API key")
        vault.set_password(SERVICE, "api", value.strip())
    return vault.get_password(SERVICE, "api") or os.environ.get("VAST_API_KEY")


def hf_token(value=None):
    from keyring.backends.Windows import WinVaultKeyring
    vault = WinVaultKeyring()
    if value is not None:
        if not isinstance(value, str) or not value.strip().startswith("hf_") or len(value.strip()) > 4096:
            raise ValueError("Enter a Hugging Face token beginning with hf_")
        vault.set_password(SERVICE, "huggingface", value.strip())
    return vault.get_password(SERVICE, "huggingface")
