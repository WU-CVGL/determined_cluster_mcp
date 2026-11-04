import pytest

_CREDENTIAL_ENVIRONMENT = (
    "DETERMINED_COMPUTE_SECRETS",
    "DET_MASTER",
    "DET_MASTER_ADDR",
    "DET_MASTER_HOST",
    "DET_API_TOKEN",
    "DET_USERNAME",
    "DET_PASSWORD",
)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """Keep the developer's Determined master and credentials out of every test."""

    for name in _CREDENTIAL_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_lookup_spacing(monkeypatch):
    """Search for an unconfirmed launch without waiting between searches."""

    from determined_compute.compute import service

    monkeypatch.setattr(service, "_LOOKUP_SPACING_SECONDS", 0)
