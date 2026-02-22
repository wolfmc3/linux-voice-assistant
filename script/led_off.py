#!/usr/bin/env python3
"""Best-effort LED OFF helper for service shutdown."""

from __future__ import annotations


def main() -> int:
    try:
        from linux_voice_assistant.gpio_controller import Ws2812Bar, LED_BRIGHTNESS, LED_COUNT, LED_GPIO

        bar = Ws2812Bar(LED_GPIO, LED_COUNT, LED_BRIGHTNESS)
        try:
            bar.off()
        finally:
            bar.close()
        return 0
    except Exception:
        # Never fail unit stop path because of LED cleanup issues.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
