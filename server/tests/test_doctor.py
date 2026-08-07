"""Doctor output must never leak a credential — that's the whole point of it
being safe to paste into a chat or an issue."""

from __future__ import annotations

import pytest

from jarvis.doctor import _looks_like_app_password, classify_models, mask


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


class FakeEndpoint:
    def __init__(self, label, model, base_url):
        self.label, self.model, self.base_url = label, model, base_url


GROQ = "https://api.groq.com/openai/v1"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai"

GROQ_EP = FakeEndpoint("groq:model-a", "model-a", GROQ)
GEMINI_EP = FakeEndpoint("gemini:gemini-2.0-flash", "gemini-2.0-flash", GEMINI)


def test_a_providers_catalogue_does_not_answer_for_another():
    """The bug this replaces: catalogues were merged, so Gemini's model was
    judged against Groq's list and reported as unreachable."""
    by_provider = {GROQ: {"model-a": {}}}  # Gemini's listing failed

    missing, unverified = classify_models([GROQ_EP, GEMINI_EP], by_provider)

    assert missing == [], "an unread catalogue is no evidence the model is missing"
    assert unverified == ["gemini:gemini-2.0-flash"]


def test_google_namespace_prefix_is_not_a_missing_model():
    """Google lists `models/gemini-2.0-flash` and accepts `gemini-2.0-flash`."""
    by_provider = {GROQ: {"model-a": {}}, GEMINI: {"models/gemini-2.0-flash": {}}}

    assert classify_models([GROQ_EP, GEMINI_EP], by_provider) == ([], [])


def test_genuinely_absent_model_is_still_reported():
    """The check has to keep working — a retired model name must still surface."""
    by_provider = {GROQ: {"model-a": {}}, GEMINI: {"models/gemini-3-pro": {}}}

    missing, unverified = classify_models([GROQ_EP, GEMINI_EP], by_provider)

    assert missing == ["gemini:gemini-2.0-flash (gemini-2.0-flash)"]
    assert unverified == []
