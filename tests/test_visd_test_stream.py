from linux_voice_assistant.visd.test_stream import (
    STREAM_HOST,
    STREAM_PORT,
    _detect_local_ips,
    _build_overlay_lines,
    _clamp_confidence,
    build_parser,
)


def test_clamp_confidence_limits() -> None:
    assert _clamp_confidence(-1.0) == 0.0
    assert _clamp_confidence(1.5) == 1.0
    assert _clamp_confidence(0.42) == 0.42


def test_overlay_lines_format() -> None:
    lines = _build_overlay_lines(
        {
            "state": "FACE_TOWARD",
            "confidence": 0.812,
            "min_confidence": 0.6,
            "face_count": 2,
            "fps": 11.94,
            "processing_ms": 28.4,
            "analysis_window": 2,
            "cpu_load_ratio": 0.37,
            "person_detected": True,
            "face_detected": True,
            "looking_toward_camera": True,
        }
    )
    assert lines[0] == "state=FACE_TOWARD"
    assert lines[1] == "confidence=0.81 threshold=0.60"
    assert lines[2] == "faces=2 fps=11.9"
    assert lines[3] == "proc_ms=28.4 window=2"
    assert lines[4] == "cpu_load=0.37"
    assert lines[5] == "person=True face=True toward=True"


def test_parser_defaults_and_args() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert isinstance(args.log_level, str)
    assert STREAM_HOST == "0.0.0.0"
    assert STREAM_PORT == 8088

    args2 = parser.parse_args(["--log-level", "DEBUG"])
    assert args2.log_level == "DEBUG"


def test_detect_local_ips_returns_strings() -> None:
    ips = _detect_local_ips()
    assert isinstance(ips, list)
    assert all(isinstance(ip, str) for ip in ips)
