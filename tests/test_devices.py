"""The device store: what a token is, and every way one can be refused."""

from __future__ import annotations

import json

import pytest


def test_pairing_returns_a_token_that_verifies(hr_devices, store_path):
    device, token = hr_devices.create_device("Pixel 8", store_path)
    assert device.label == "Pixel 8"
    assert not device.revoked

    verified = hr_devices.verify_token(token, store_path)
    assert verified is not None
    assert verified.id == device.id


def test_token_shape_is_prefix_id_secret(hr_devices, store_path):
    device, token = hr_devices.create_device("Pixel 8", store_path)
    prefix, device_id, secret = token.split(".")
    assert prefix == hr_devices.TOKEN_PREFIX
    assert device_id == device.id
    assert len(secret) >= 40  # 32 bytes of base64url
    assert hr_devices.parse_token(token) == (device.id, secret)


def test_the_secret_is_never_written_to_disk(hr_devices, store_path):
    _device, token = hr_devices.create_device("Pixel 8", store_path)
    secret = token.split(".")[2]
    raw = store_path.read_text(encoding="utf-8")
    assert secret not in raw
    record = json.loads(raw)["devices"][token.split(".")[1]]
    assert record["algo"] == "sha256"
    assert set(record) >= {"salt", "hash", "created_at", "label"}


def test_a_tampered_secret_is_refused(hr_devices, store_path):
    _device, token = hr_devices.create_device("Pixel 8", store_path)
    prefix, device_id, secret = token.split(".")
    forged = f"{prefix}.{device_id}.{secret[:-1]}{'A' if secret[-1] != 'A' else 'B'}"
    assert hr_devices.verify_token(forged, store_path) is None


def test_an_unknown_device_id_is_refused(hr_devices, store_path):
    _device, token = hr_devices.create_device("Pixel 8", store_path)
    secret = token.split(".")[2]
    assert hr_devices.verify_token(f"hr1.aaaaaaaaaaaa.{secret}", store_path) is None


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-token",
        "hr1.short.secret",  # id is not 12 hex chars
        "hr1.ZZZZZZZZZZZZ.secret",  # id is not hex
        "hr0.aaaaaaaaaaaa.secret",  # wrong prefix
        "hr1.aaaaaaaaaaaa.",  # empty secret
        "sk-someone-elses-opaque-bearer",
    ],
)
def test_a_foreign_bearer_is_not_ours(hr_devices, store_path, token):
    """``parse_token`` returning None is what lets the auth seam try the next provider."""
    assert hr_devices.parse_token(token) is None
    assert hr_devices.verify_token(token, store_path) is None


def test_revoking_refuses_the_token_but_keeps_the_record(hr_devices, store_path):
    device, token = hr_devices.create_device("Pixel 8", store_path)
    assert hr_devices.revoke_device(device.id, store_path) is True
    assert hr_devices.verify_token(token, store_path) is None

    listed = hr_devices.list_devices(store_path)
    assert [d.id for d in listed] == [device.id]
    assert listed[0].revoked is True


def test_revoking_twice_reports_no_change(hr_devices, store_path):
    device, _token = hr_devices.create_device("Pixel 8", store_path)
    assert hr_devices.revoke_device(device.id, store_path) is True
    assert hr_devices.revoke_device(device.id, store_path) is False
    assert hr_devices.revoke_device("aaaaaaaaaaaa", store_path) is False


def test_devices_are_independent(hr_devices, store_path):
    first, first_token = hr_devices.create_device("Pixel 8", store_path)
    second, second_token = hr_devices.create_device("iPad", store_path)
    assert first.id != second.id

    hr_devices.revoke_device(first.id, store_path)
    assert hr_devices.verify_token(first_token, store_path) is None
    assert hr_devices.verify_token(second_token, store_path).id == second.id


def test_a_missing_store_is_empty_not_an_error(hr_devices, store_path):
    assert hr_devices.list_devices(store_path) == []
    assert hr_devices.verify_token("hr1.aaaaaaaaaaaa.x", store_path) is None


def test_a_malformed_store_fails_loudly(hr_devices, store_path):
    """A truncated write must page the operator, not silently look like "no devices paired"."""
    store_path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(hr_devices.DeviceStoreUnavailable):
        hr_devices.list_devices(store_path)
    with pytest.raises(hr_devices.DeviceStoreUnavailable):
        hr_devices.verify_token("hr1.aaaaaaaaaaaa.x", store_path)


def test_a_store_of_the_wrong_shape_fails_loudly(hr_devices, store_path):
    store_path.write_text('{"version": 1, "devices": []}', encoding="utf-8")
    with pytest.raises(hr_devices.DeviceStoreUnavailable):
        hr_devices.list_devices(store_path)


def test_last_seen_is_recorded_then_throttled(hr_devices, store_path, monkeypatch):
    device, token = hr_devices.create_device("Pixel 8", store_path)
    assert hr_devices.list_devices(store_path)[0].last_seen_at is None

    hr_devices.verify_token(token, store_path)
    first = hr_devices.list_devices(store_path)[0].last_seen_at
    assert first is not None

    # A second verify inside the throttle window must not rewrite the file.
    before = store_path.read_text(encoding="utf-8")
    hr_devices.verify_token(token, store_path)
    assert store_path.read_text(encoding="utf-8") == before

    # Past the window it updates again.
    hr_devices._last_seen_written[device.id] -= hr_devices.LAST_SEEN_THROTTLE_SECONDS + 1
    monkeypatch.setattr(hr_devices.time, "time", lambda: 2_000_000_000.0)
    hr_devices.verify_token(token, store_path)
    assert hr_devices.list_devices(store_path)[0].last_seen_at == 2_000_000_000


def test_bookkeeping_failure_never_fails_the_verify(hr_devices, store_path, monkeypatch):
    """``last_seen_at`` is advisory; an unwritable store must not 401 an authenticated phone."""
    _device, token = hr_devices.create_device("Pixel 8", store_path)

    def _boom(*args, **kwargs):
        raise hr_devices.DeviceStoreUnavailable("disk full")

    monkeypatch.setattr(hr_devices, "_write_store", _boom)
    assert hr_devices.verify_token(token, store_path) is not None


def test_the_store_survives_a_failed_write(hr_devices, store_path, monkeypatch):
    """``os.replace`` is what makes the swap atomic; a crash mid-write leaves the old file."""
    _device, token = hr_devices.create_device("Pixel 8", store_path)
    original = store_path.read_text(encoding="utf-8")

    monkeypatch.setattr(hr_devices.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(hr_devices.DeviceStoreUnavailable):
        hr_devices.create_device("iPad", store_path)

    assert store_path.read_text(encoding="utf-8") == original
    assert hr_devices.verify_token(token, store_path) is not None
