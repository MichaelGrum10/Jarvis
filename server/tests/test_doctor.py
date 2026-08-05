"""Doctor output must never leak a credential — that's the whole point of it
being safe to paste into a chat or an issue."""

from __future__ import annotations

import pytest

from jarvis.doctor import _looks_like_app_password, mask


@pytest.mark.parametrize(
    "secret",
    [
        "gsk_abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGH",
        "wxyz-abcd-efgh-ijkl",
        "supersecretpassword123",
    ],
)
def test_mask_never_reveals_enough_to_use(secret):
    masked = mask(secret)
    assert secret not in masked
    # At most a 4-char head and 2-char tail may survive.
    assert masked.startswith(secret[:4])
    assert secret[4:-2] not in masked


def test_mask_hides_short_secrets_entirely():
    assert mask("abc123") == "******"
    assert "abc" not in mask("abc123")


def test_mask_reports_unset():
    assert mask("") == "(not set)"


@pytest.mark.parametrize("value", ["wxyz-abcd-efgh-ijkl", "abcd-efgh-ijkl-mnop"])
def test_app_password_shape_accepted(value):
    assert _looks_like_app_password(value)


@pytest.mark.parametrize(
    "value",
    ["MyApplePassword1!", "abcd-efgh-ijkl", "ABCD-EFGH-IJKL-MNOP", "", "abcd efgh ijkl mnop"],
)
def test_non_app_password_shape_rejected(value):
    """Catching this is what stops the most common setup failure: pasting the
    Apple ID password instead of an app-specific one."""
    assert not _looks_like_app_password(value)
