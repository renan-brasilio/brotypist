# Cotypist investigation

Initial inspection: 2026-10-23 using mounted REA 6.7.0. Target: installed `/Applications/Cotypist.app`. Later, separately scoped passes are dated below; initial missing-provider limitations are historical, not the final analysis status. This is an independent behavioral implementation, not recovered Cotypist source. No licensing checks were bypassed, no proprietary assets/models copied, and no personal documents were inspected.

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

At this initial pass, the exact prompts, scheduling, caching, overlay technique, privacy guards, runtime-selected model, personalization algorithm, and insertion strategy remained unverified. The later observations below narrow only specific unknowns.

## Initial analysis limits — 2026-10-23

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

In this menu-reference pass, the model picker identified Gemma 4 E2B as the recommendation (2.8 GB displayed). That established visible recommendation/configuration, **not which model actually executed a prediction**. The then-current Inklow used independently downloaded Qwen weights, not a copied Cotypist model.

On this ISO Mac keyboard, the above-Tab key is § and has native keycode 10; ANSI uses keycode 50. The geometry was checked against HIToolbox keyboard/layout APIs rather than assuming an ANSI keymap.

No Cotypist suggestion overlay or accepted live prediction was established **in that menu-reference pass**. It therefore did **not** prove typing-output parity, equivalent prompts/model quality, latency superiority, memory superiority, training behavior, or network privacy. Later synthetic live observations are separately scoped below; Inklow's native typing proof is in [VERIFICATION.md](VERIFICATION.md).

The reconstruction intentionally does not impersonate Cotypist accounts, subscriptions, iCloud synchronization, proprietary update infrastructure, or its exact model internals. At the menu-reference revision, Inklow did not implement Cotypist's spell-correction UI/current-word-only mode. Menu-bar recovery is always retained instead of offering a hide-all-controls state. Linux/Windows product work is now deferred; an identically styled panel cannot make unavailable OS accessibility capabilities real.

## Reference correction pass — 2026-10-25

This was the earlier pass, before deep-provider setup.

Fresh mounted REA observations retain the same installed executable identity: SHA-256 `78286accbb9f388d724fc6c759df7ba3af9ff0e04b49bbe0c828f90ca235f0cb`.

- **Actual selected model:** native General observation `ev_2855ed0731f4d62eeb867d56075de15720d62ac7db980ad9041cc7f13b2d9ce5` reports the picker value `Gemma 4 E2B (2.8 GB)` and recommendation `Gemma 4 E2B`. This is selected/recommended configuration, not a traced model execution. No open `.gguf` descriptor was found during the General-pane inspection.
- **Independent static corroboration:** controlled string-extraction record `ev_adb78e1bfc3af42a977b18dcc067fa1659662d5f63aba3f9ea73782a031c8a9e` contains `Google Gemma 4`, Gemma 4 E2B/E4B catalogue copy, `llama_model_gemma4`, GGUF and llama.cpp implementation identifiers. These strings do not identify an exact quantization, runtime-selected weights, prompt or inference result.
- **UI direction:** the running General pane describes short medium completions as approximately 2–4 words and explicitly warns that longer completions slow typing and drift from intent. It has separate menu-bar/accessory preferences, per-app defaults, inside-existing-text behavior and typo-suppression/fix controls. Its exact field-menu contents and live typing overlay were not reproduced in this pass; no exact UI/latency parity claim is made.
- **BroTypist correction:** its field-menu implementation copied the entire menu-bar menu, causing the reported oversized dashboard. It now constructs an independent, three-action native field menu: current-app predictions, temporary pause/resume and Settings. Dashboard/model/statistics/quit controls stay out of that surface. The menu bar retains Quit. The actual source-native compact menu was displayed and captured using synthetic context. Small artwork now has explicit logical-size Retina representations and the field glyph is unboxed.
- **Analysis limits:** provider-independent Mach-O/package inspection and native UI observation worked. Deep string/xref/decompilation via `search_strings` was unavailable because Ghidra/Java, Hopper, IDA and Kuna were not configured. The controlled static extraction is not decompilation. Authenticated raw records are retained privately at `~/Library/Caches/inklow/cotypist-reference-pass-20261025.json`; no proprietary model weights/assets/source were copied.

### Google-model decision

[Google's Gemma 4 E2B model card](https://huggingface.co/google/gemma-4-E2B-it) identifies 2.3B effective parameters, 5.1B including embeddings, multilingual support and a non-thinking generation mode. Those are upstream capabilities, not BroTypist latency/quality measurements. Current [llama.cpp](https://github.com/ggml-org/llama.cpp/blob/master/src/llama-model.cpp) dispatches `gemma4`, and its [build documentation](https://github.com/ggml-org/llama.cpp/blob/master/docs/build.md) provides the cross-platform GGUF path with Metal on Mac. A Mac-only MLX replacement is not required to investigate Google's model.

**Candidate decision at that pass:** benchmark independently sourced Gemma 4 E2B instruction weights, with thinking disabled, against the current Qwen2.5 1.5B writer on short context-conditioned continuations in multiple languages. Measure last-keystroke-to-visible output, cold/warm prompt costs, resident memory, cancellation and semantic/grammar errors; do not infer suitability from general chatbot benchmarks.

**Migration constraint at that pass:** the then-current guided writer accepted only `qwen2` architecture and constructed Qwen chat tokens. Gemma needed independently verified prompt/template and stop-token handling before a valid comparison or catalogue recommendation. No Gemma weights had yet been downloaded in that pass; the user's configured model was not changed. The original's displayed 2.8 GB was larger than the pinned Qwen file's 1.12 GB; neither number is resident-memory usage. The subsequent investigation and source changes below supersede the download/adapter limitation, not the unmeasured parity limits.

## Deep native and live-model pass — recorded 2026-10-25, reviewed 2026-10-26

**Identity bound:** all original-app findings here concern Cotypist **2026.5.1, build 83**, arm64/macOS 14+, bundle `app.cotypist.Cotypist`, executable SHA-256 `78286accbb9f388d724fc6c759df7ba3af9ff0e04b49bbe0c828f90ca235f0cb`. The version is not evidence of a May release date or of behavior in another version.

The private REA **6.8.0** Unix-socket session used **Ghidra 12.1 / Temurin 21** and completed default auto-analysis in **738 seconds**. Thus the missing-provider failures above remain genuine historical results, but do not describe this later pass. Private native dossiers include decompilation, assembly and references; none of that recovered proprietary source, assets or original weights is published or incorporated into the MIT implementation. Ghidra/Java are analyst tools, not product runtime dependencies.

| Bounded finding | Source and what it establishes | Limit |
| --- | --- | --- |
| Native spelling/language path | `deep-spelling-trace.json`, record `ev_74e341ad3693a8e92594f511c375bd7c6e3b28d7105ae3c79b8133551bba0009`; function dossier `ev_35c79294256df3c5bce8fce6a7c3c2444e6561b1efd61a174c65b72837c31d43`, entry `0x10025eb84`: `NLLanguageRecognizer` and `NSSpellChecker` are used, with the detected language passed to spelling. Assembly, not an incorrect decompiler inline comment, resolves the language argument. | Static native behavior, not a real multilingual spelling/grammar accuracy comparison or proof of every UI correction mode. |
| Rendering/context and app-specific anchors | `deep-render-compat-anchors.json`, record `ev_ab2dec001d171ae234689a774a1d45f424721a64917613f985396612c124a1f6`, contains font lookup, Vision text-recognition and Terminal/VS Code identifiers. Terminal-reference dossier `ev_f6ad245fdc4555d647a8b05c74c19e4908f39d06868ee2b41ac7ef68420a9ff2` binds `com.apple.Terminal` references to function `0x100046fc8`. | Strings/references do not prove working OCR, terminal insertion, editor compatibility or privacy. These are not a compatibility certification. |
| Original runtime model file | During an actual prediction, the original had the model basename `gemma-4-E2B-Q4_C.gguf` open. | File-use evidence, not a verified private quantization, exact weights hash, prompt template or authority to copy the model. |
| Original live suffix | An owned synthetic native text target showed same-line grey suffix ` appreciate it.`; after Cotypist quit, the host-only target showed no ghost. Host inline OS prediction was disabled. Private captures: `original-typing-live.png` and `host-without-cotypist.png`. | The original's actual synthetic Tab acceptance was **not established**. No personal writing or screenshot is published. |
| Independent source-native typing | The real public Gemma 4 weights rendered grey same-line ` appreciate it.` in the owned `NSTextView`. Once the cold model was ready, real Tab inserted ` appreciate ` with no literal Tab. A separate actual focused-window AX context fixture produced ` Tuesday at 10 AM ` from the synthetic Cedar reference. Private capture: `candidate-typing-live.png`. | Source/native observation, not a rebuilt/installed release, all-app compatibility or a calibrated latency/quality comparison. A rapid-Tab experiment aborted safely on user focus change; it did not pass. |

### Independent Google weights, not a copy of the original

The real model samples used the following independently obtained public files; the existing Qwen file/selection was preserved. Larger catalogue entries are pinned/guarded but were **not executed**.

| Executed public model | Immutable repository revision and file | Verified file identity / terms |
| --- | --- | --- |
| [Gemma 4 E2B](https://huggingface.co/ggml-org/gemma-4-E2B-it-GGUF/tree/6938c58e698502037401a5f19f6905f2fb2f7dee) | `6938c58e698502037401a5f19f6905f2fb2f7dee`, `gemma-4-E2B-it-Q4_0.gguf` | 2,841,481,184 bytes; SHA-256 `8e30dff3ac4c8434c49a7036fa15564bdbb6044e42bf04550bf1a096ad7e6a52`; Apache-2.0 |
| [Gemma 3 1B](https://huggingface.co/ggml-org/gemma-3-1b-it-GGUF/tree/f9c28bcd85737ffc5aef028638d3341d49869c27) | `f9c28bcd85737ffc5aef028638d3341d49869c27`, `gemma-3-1b-it-Q4_K_M.gguf` | 806,058,240 bytes; SHA-256 `8ccc5cd1f1b3602548715ae25a66ed73fd5dc68a210412eea643eb20eb75a135`; accepted [Gemma terms](https://ai.google.dev/gemma/terms), 2026-04-01 |

The original private `Q4_C` basename and the independent public `Q4_0` file are not interchangeable evidence. No exact model, tokenizer, quantization, sampler, prompt, semantic/grammar or personalization parity is claimed. The private synthetic run provenance and failed utility experiments are summarized in [VERIFICATION.md](VERIFICATION.md#macos-source-candidate--2026-10-26-unreleased).

### Remaining boundaries

The Docs metadata/accessibility helper and opt-in Terminal/iTerm Automation integration are in development; live compatibility is not yet verified. Metadata is not writable text or reliable caret geometry, and permission preflight is not a grant. Unknown/denied/unavailable states must remain truthful. Ordinary completion stays silent without verified native input/typography; it does not fall back to a popup.

Exact original scheduling, caching, prompts, insertion implementation, personalization/training behavior and network privacy remain unresolved. iCloud work is deferred; licensing/paywall/account/telemetry imitation is out of scope. These bounded static and synthetic observations do not establish complete compatibility, repeated-key acceptance, latency/RAM superiority or multilingual grammar parity. Raw dossiers, screenshots and synthetic receipts remain private and are identified by basename/record ID here, not by invented repository links.
