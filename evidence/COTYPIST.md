# Cotypist investigation

Inspected 2026-10-23 using mounted REA 6.7.0. Target: installed `/Applications/Cotypist.app`. This is an independent behavioral implementation, not recovered Cotypist source. No licensing checks were bypassed, no proprietary assets/models copied, and no personal documents were inspected.

## Reproducible evidence

Full machine-readable records: [cotypist-rea.json](cotypist-rea.json). REA exported 13 records and 4 residual unknowns. The bundle includes package inventories, native dependency/import metadata, and controlled captures of static string extraction. Static strings are not execution traces.

| Finding | Observation | Evidence record |
| --- | --- | --- |
| Identity | Version 2026.5.1, build 83, bundle `app.cotypist.Cotypist`, macOS minimum 14.0, `LSUIElement=true` (agent/menu-bar style) | `ev_ab277671cba9dd138f010c1b1479223722eb780e5120d7c05ca21afa4bcd8ff4` |
| Native executable | Mach-O arm64; executable SHA-256 `78286accbb9f388d724fc6c759df7ba3af9ff0e04b49bbe0c828f90ca235f0cb` | `ev_c31333fb2b2be9e90373d08ecd73b6945be7396d150c6119239795251c626fd5` |
| GPU/vector dependencies | Metal, MetalKit, Accelerate | `ev_f4356d551f7b20678e038f50f7cb8300becc59abd5d64e14992c2a4c4792a4bd` |
| Native UI/storage/update dependencies | AppKit, SwiftUI, ApplicationServices, GRDB, Sparkle 2.10.0 | Same dependency record |
| Local inference code present | `llama_token_data`, `llama_batch`, many `llama_model_*` strings; `https://github.com/ggml-org/llama.cpp`; embedded GGUF model repository names | `ev_2dafd9b85c4ce54d04e33c7acf05ccca2c3dddb9c677570f8921be0e9edcb206`, `ev_a4e4ac40a3d3579d309ede2d9f2bcb163ef2c74961850e66b60933c6d5964abf` |
| Accessibility/input integration clues | `AXUIElement`, `CGEventTapOptions`, `CGEventTapLocation`, `AXFocusedUIElementChanged`, `AXSelectedTextChanged` | Same string records |
| Personalization feature strings | `personalization-strength`, gentle/balanced/strong personalization labels | `ev_a4e4ac40a3d3579d309ede2d9f2bcb163ef2c74961850e66b60933c6d5964abf` |
| Package coverage | 216 occurrences, 207 nodes, 118 files; zero inventory integrity contradictions | `ev_d58d99dd6908069251935116b60118c1db1485ba69010398a0f548faebed10b0` |

## Published behavior, not reverse-engineered runtime proof

Vendor documentation: <https://cotypist.app/help/shortcuts> (retrieved 2026-10-23).

- Tab accepts the next word, repeated presses walk through a completion.
- The key above Tab accepts the full suggestion, layout-dependent.
- Escape dismisses and can briefly pause suggestions in the current field.
- Option+Tab bypasses acceptance and passes a normal Tab to the application.
- Per-app and global toggles exist.

Vendor compatibility page: <https://cotypist.app/compatibility>. The product is Mac-only today. Website claims about privacy/model execution are vendor claims, not proven by binary strings or dependency analysis.

## Reconstructed architecture [INFERENCE]

The combined native dependencies, AX notification strings, event-tap type strings, llama implementation strings, and documented acceptance UX support this likely pipeline:

1. Observe the focused editable field through accessibility APIs.
2. Read text/caret context and determine whether completion is eligible.
3. Run a local llama.cpp model and show a suggestion near the caret.
4. Intercept acceptance/dismissal keys and insert accepted text into the original field.

The exact prompts, scheduling, caching, overlay technique, privacy guards, model selected at runtime, personalization algorithm, and insertion strategy remain unverified.

## Analysis limits

REA `trace_feature("completion")` could not execute: no configured Ghidra/Java, Hopper, IDA, or Kuna provider. The failure diagnostics identify missing prerequisites. Provider-neutral package/Mach-O/plist inspection and REA-controlled string captures succeeded. No decompilation, call-graph proof, live Cotypist typing test, or network privacy audit was performed. `capture_process_scenario` ran the analyst's static extraction command, not Cotypist. REA's target was closed after export.

The evidence bundle contains binary-internal development paths and public update verification keys, not user credentials. Public update verification keys are not secrets.
