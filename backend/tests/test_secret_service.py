import pytest

from app.services.secret_service import SecretServiceError, decrypt_secret, encrypt_secret


@pytest.mark.skipif(__import__("os").name != "nt", reason="DPAPI 仅在 Windows 可用")
def test_secret_round_trip_returns_original_value() -> None:
    encrypted = encrypt_secret("direct-provider-secret")

    assert encrypted
    assert encrypted != "direct-provider-secret"
    assert decrypt_secret(encrypted) == "direct-provider-secret"


def test_secret_empty_value_returns_empty_ciphertext_and_plaintext() -> None:
    assert encrypt_secret("") == ""
    assert decrypt_secret("") == ""


@pytest.mark.skipif(__import__("os").name != "nt", reason="DPAPI 仅在 Windows 可用")
def test_secret_invalid_ciphertext_raises_configuration_error() -> None:
    with pytest.raises(SecretServiceError, match="DPAPI"):
        decrypt_secret("not-a-valid-dpapi-blob")
