# Changelog

## 0.1.0 — 2026-10-23

- Add an independent cross-platform Qt predictive-writing editor with real local inference, word/full acceptance, dismissal, undo, and IME/focus safeguards.
- Add local GGUF, Ollama, and OpenAI-compatible continuation backends; refuse remote/proxied/redirected inference routes.
- Add verified, immutable-revision starter-model acquisition without committing weights.
- Add opt-in macOS AX/Quartz, Windows UIA/native input, and Linux AT-SPI/X11 integration with protected-field and stale-snapshot checks.
- Use UTF-8 byte lengths for AT-SPI insertion while preserving character-based caret offsets.
- Explicitly refuse system-wide Wayland interception; retain the built-in editor.
- Preserve REA Cotypist evidence, annotated architectural inferences, actual Inklow screenshots, and bounded verification results.
- Add macOS/Windows/Ubuntu CI checks and installable wheel builds. Interactive Windows/Linux integration is not yet verified.
