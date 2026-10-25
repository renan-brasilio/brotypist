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

## Native reference pass for the menu-first replacement

REA subsequently observed the running Cotypist process and navigated its actual Setup, General, Context, Personalization, Emoji, Shortcuts, Battery, App Settings, Labs, and Statistics panes. Navigation used process-scoped native accessibility observations and existing preference-pane deep links, not an invented mockup. Preferences, licensing, and permissions were not changed. The reference settings window opened for inspection was closed afterward.

Published evidence: [sanitized native records](cotypist-menu-reference.json), including authenticated record IDs and the same executable SHA-256. The authenticated raw bundle contains 11 records and is retained outside Git at `~/Library/Caches/inklow/cotypist-menu-rea-20261023.json`; its digest is in the sanitized summary. Raw preferences can expose personal custom instructions, so editable values and the Personalization screenshot are deliberately not published.

| Observed surface | Behavioral/UI evidence |
| --- | --- |
| Native grouped sidebar preferences | [General screenshot](cotypist-general-20261023.png); launch/accessory controls, model chooser, completion defaults/inside-text/length |
| Optional context | [Context](cotypist-context-20261023.png); clipboard and local screen-context controls. Privacy text is a vendor assertion, not a network audit. |
| Physical acceptance keys and punctuation modes | [Shortcuts](cotypist-shortcuts-20261023.png); Tab, layout-dependent above-Tab, force, toggles, trailing space/punctuation, Escape behavior |
| Emoji, battery, per-app/domain controls | [Emoji](cotypist-emoji-20261023.png), [Battery](cotypist-battery-20261023.png), [App Settings](cotypist-appSettings-20261023.png) |
| Experimental choosers and local activity UI | [Labs](cotypist-labs-20261023.png), [Statistics](cotypist-statistics-20261023.png) |

The model picker identified Gemma 4 E2B as the recommendation (2.8 GB displayed). This establishes a visible recommendation/configuration, **not which model actually executed a prediction**. Inklow uses its independently downloaded Qwen model, not a copied Cotypist model.

On this ISO Mac keyboard, the above-Tab key is § and has native keycode 10; ANSI uses keycode 50. The geometry was checked against HIToolbox keyboard/layout APIs rather than assuming an ANSI keymap.

No Cotypist suggestion overlay or accepted live prediction was established in the disposable native text target. Therefore this pass does **not** prove typing-output parity, equivalent prompts/model quality, latency superiority, memory superiority, training behavior, or network privacy. Inklow's actual native typing proof is separate in [VERIFICATION.md](VERIFICATION.md).

The reconstruction intentionally does not impersonate Cotypist accounts, subscriptions, iCloud synchronization, proprietary update infrastructure, or its exact model internals. Inklow also does not implement Cotypist's spell-correction UI/current-word-only mode. Menu-bar recovery is always retained instead of offering a hide-all-controls state. Windows/Linux capability differences are explicit in the README; an identically styled panel cannot make unavailable OS accessibility capabilities real.

## Reference correction pass — 2026-10-25

Fresh mounted REA observations retain the same installed executable identity: SHA-256 `78286accbb9f388d724fc6c759df7ba3af9ff0e04b49bbe0c828f90ca235f0cb`.

- **Actual selected model:** native General observation `ev_2855ed0731f4d62eeb867d56075de15720d62ac7db980ad9041cc7f13b2d9ce5` reports the picker value `Gemma 4 E2B (2.8 GB)` and recommendation `Gemma 4 E2B`. This is selected/recommended configuration, not a traced model execution. No open `.gguf` descriptor was found during the General-pane inspection.
- **Independent static corroboration:** controlled string-extraction record `ev_adb78e1bfc3af42a977b18dcc067fa1659662d5f63aba3f9ea73782a031c8a9e` contains `Google Gemma 4`, Gemma 4 E2B/E4B catalogue copy, `llama_model_gemma4`, GGUF and llama.cpp implementation identifiers. These strings do not identify an exact quantization, runtime-selected weights, prompt or inference result.
- **UI direction:** the running General pane describes short medium completions as approximately 2–4 words and explicitly warns that longer completions slow typing and drift from intent. It has separate menu-bar/accessory preferences, per-app defaults, inside-existing-text behavior and typo-suppression/fix controls. Its exact field-menu contents and live typing overlay were not reproduced in this pass; no exact UI/latency parity claim is made.
- **BroTypist correction:** its field-menu implementation copied the entire menu-bar menu, causing the reported oversized dashboard. It now constructs an independent, three-action native field menu: current-app predictions, temporary pause/resume and Settings. Dashboard/model/statistics/quit controls stay out of that surface. The menu bar retains Quit. The actual source-native compact menu was displayed and captured using synthetic context. Small artwork now has explicit logical-size Retina representations and the field glyph is unboxed.
- **Analysis limits:** provider-independent Mach-O/package inspection and native UI observation worked. Deep string/xref/decompilation via `search_strings` was unavailable because Ghidra/Java, Hopper, IDA and Kuna were not configured. The controlled static extraction is not decompilation. Authenticated raw records are retained privately at `~/Library/Caches/inklow/cotypist-reference-pass-20261025.json`; no proprietary model weights/assets/source were copied.

### Google-model decision

[Google's Gemma 4 E2B model card](https://huggingface.co/google/gemma-4-E2B-it) identifies 2.3B effective parameters, 5.1B including embeddings, multilingual support and a non-thinking generation mode. Those are upstream capabilities, not BroTypist latency/quality measurements. Current [llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/src/llama-model.cpp) dispatches `gemma4`, and its [build documentation](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md) provides the cross-platform GGUF path with Metal on Mac. A Mac-only MLX replacement is not required to investigate Google's model.

**Candidate:** benchmark independently sourced Gemma 4 E2B instruction weights, with thinking disabled, against the current Qwen2.5 1.5B writer on short context-conditioned continuations in multiple languages. Measure last-keystroke-to-visible output, cold/warm prompt costs, resident memory, cancellation and semantic/grammar errors; do not infer suitability from general chatbot benchmarks.

**Not a drop-in swap:** BroTypist's current guided writer explicitly accepts only `qwen2` architecture and constructs Qwen chat tokens. Gemma needs its own verified prompt/template and stop-token handling before a valid comparison or catalogue recommendation. No Gemma weights were downloaded, no untested Gemma option was added, and the user's configured model was not changed. The original's displayed 2.8 GB is larger than the current pinned Qwen file's 1.12 GB; neither number is resident-memory usage. Google model direction is confirmed; performance, quality and runtime parity remain unverified.
