import pytest
from astria_common.envguard import EnvironmentGuardError
from fastapi.testclient import TestClient
from pydantic import SecretStr

from astria_gateway.main import build_app
from astria_gateway.settings import Settings


def test_health_and_ready_without_dependencies() -> None:
    with TestClient(build_app(Settings(env="test"))) as client:
        health = client.get("/health").json()
        assert health["service"] == "gateway"
        assert health["env"] == "test"
        assert client.get("/ready").status_code == 200
        assert "astria_service_info" in client.get("/metrics").text


def test_refuses_to_start_with_non_dev_database_in_dev() -> None:
    url = SecretStr("postgresql+psycopg://u:p@postgres:5432/astria")
    with pytest.raises(EnvironmentGuardError):
        build_app(Settings(env="dev", database_url=url))


def test_telegram_disabled_without_token_and_enabled_from_file(tmp_path) -> None:
    db = SecretStr(f"sqlite+aiosqlite:///{tmp_path}/gateway_test.db")
    app = build_app(Settings(env="test", database_url=db))
    assert app.state.poller is None and "telegram" not in app.state.adapters

    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text("123456:TEST-token-not-real\n")  # gitleaks:allow
    settings = Settings(env="test", database_url=db, gw_telegram_bot_token_file=token_file, gw_smtp_host="mailpit")
    assert settings.telegram_token() == "123456:TEST-token-not-real"  # gitleaks:allow
    assert "TEST-token" not in repr(settings)
    app = build_app(settings)
    assert app.state.poller is not None and set(app.state.adapters) == {"telegram", "email"}


def test_empty_token_file_disables_telegram(tmp_path) -> None:
    token_file = tmp_path / "telegram_bot_token"
    token_file.write_text("")
    settings = Settings(env="test", gw_telegram_bot_token_file=token_file)
    assert build_app(settings).state.adapters == {}


def test_invalid_seed_refuses_to_start(tmp_path) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text(
        '{"channels": [{"channel_id": "email-tim", "type": "email"}], "identities": '
        '[{"identity_id": "i1", "channel_id": "email-tim", "external_id": "bukan-email", "user_id": "usr_1"}]}'
    )
    db = SecretStr(f"sqlite+aiosqlite:///{tmp_path}/gateway_test.db")
    with pytest.raises(ValueError, match="external_id"):
        build_app(Settings(env="test", database_url=db, gw_seed_file=seed))


def test_whatsapp_needs_url_and_token_file(tmp_path) -> None:
    db = SecretStr(f"sqlite+aiosqlite:///{tmp_path}/gateway_test.db")
    token_file = tmp_path / "wa_bridge.token"
    token_file.write_text("uji-token\n")
    url = "http://wa-bridge:3000"
    for kw in ({"gw_wa_bridge_url": url}, {"gw_wa_bridge_token_file": token_file}):
        app = build_app(Settings(env="test", database_url=db, **kw))
        assert "whatsapp" not in app.state.adapters and app.state.wa_inbound is None
    settings = Settings(env="test", database_url=db, gw_wa_bridge_url=url, gw_wa_bridge_token_file=token_file)
    app = build_app(settings)
    assert set(app.state.adapters) == {"whatsapp"} and app.state.wa_inbound.channel_id == "wa-tim"
    assert app.state.wa_token == "uji-token"  # noqa: S105
