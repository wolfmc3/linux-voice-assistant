from linux_voice_assistant.models import (
    WAKE_WORD_THRESHOLD_PRESET_CUSTOM,
    WAKE_WORD_THRESHOLD_PRESET_MODEL_DEFAULT,
    normalize_wake_word_threshold,
    normalize_wake_word_threshold_preset,
    resolve_wake_word_threshold,
)


def test_model_default_threshold_is_none() -> None:
    assert (
        resolve_wake_word_threshold(
            WAKE_WORD_THRESHOLD_PRESET_MODEL_DEFAULT,
            0.5,
        )
        is None
    )


def test_preset_threshold_resolution() -> None:
    assert resolve_wake_word_threshold("Strict", 0.5) == 0.60
    assert resolve_wake_word_threshold("Default", 0.5) == 0.50
    assert resolve_wake_word_threshold("Sensitive", 0.5) == 0.45
    assert resolve_wake_word_threshold("VerySensitive", 0.5) == 0.40


def test_custom_threshold_resolution_and_clamp() -> None:
    assert (
        resolve_wake_word_threshold(
            WAKE_WORD_THRESHOLD_PRESET_CUSTOM,
            0.44,
        )
        == 0.44
    )
    assert normalize_wake_word_threshold(-1.0) == 0.10
    assert normalize_wake_word_threshold(2.0) == 0.95


def test_invalid_preset_falls_back_to_model_default() -> None:
    assert normalize_wake_word_threshold_preset("unknown") == WAKE_WORD_THRESHOLD_PRESET_MODEL_DEFAULT
