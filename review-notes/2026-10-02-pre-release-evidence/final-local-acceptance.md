# Release candidate local acceptance — 2 October 2026

Candidate versions: Pandrator **0.11.0**, MCP **0.6.0**, Manager **0.9.28**. This records local acceptance and the first platform-CI repair before managed promotion and publication. Those later gates remain required. The original audit describes the earlier failures; it is not the current release verdict.

## Automated checks

The final 13 Python lanes cover 291 non-UI test files: **4,233 passed, 10 skipped, zero failures**. The installer lane's 131 passes were reused only after package and assigned-test hashes proved unchanged; the other 12 lanes were rerun. The parent separately ran the three UI/source suites: **36 passed**. Counts are disjoint in this final protocol.

| Lane | Passed | Skipped |
| --- | ---: | ---: |
| Fast | 1240 | 1 |
| Installer, unchanged evidence reused | 131 | 0 |
| Manager | 318 | 9 |
| MCP | 308 | 0 |
| Media/export | 198 | 0 |
| Web 01–06 | 1370 | 0 |
| Web 07, full repaired rerun | 420 | 0 |
| Web 08 | 248 | 0 |
| Parent UI/source suites | 36 | 0 |

Commands used the locked Pixi environment, isolated per-lane data/cache/temp roots and JUnit receipts. Final `pixi run --locked --no-install quality` passed Ruff, basedpyright/import-cycle checks, Vulture and test-lane assignment. CI baseline locking was enabled. The typing baseline was reduced from 1209 to 1199 entries: ten removed, none added. Frontend `npm run quality` and `npm run build` passed with zero Svelte errors or warnings. `git diff --check` and `python scripts/check_docs.py` passed.

All fresh lanes' Python runtime snapshots at the first candidate match SHA-256 `9960a7c0a77e1721cbf1dcb991e26e97452abd75c464ed0dfcc5dc9fa855cf4e`. Two malformed media-test calls and one historical-schema fixture were repaired and the full affected lane rerun. The packaging-only `/reviews` sdist exclusion preserves the strict archive audit. Detailed commands/source snapshots are retained outside Git at `/tmp/pandrator-final-acceptance-20261002/final-summary.json`.

Platform CI then found two compatibility failures: Windows CP1252 decoding of Unicode catalogue resources during application import, and Node 24 requiring a JSON import attribute before Playwright could collect tests. The explicit UTF-8 readers and JSON import attribute were repaired. The focused catalogue/model suite passed **29 tests**, including **two new CP1252-default regressions**; frontend quality/build passed and Playwright collected **514 tests** without launching browsers. Full platform reruns qualify the repaired commit separately; the earlier runtime hash is retained as provenance, not asserted for the repaired source.

## Migration and package isolation

An actual old-HEAD 0051 fixture was copied and upgraded to 0052. Its 73 existing tables and six managed files were preserved; integrity, foreign-key and reference checks passed. The original fixture was untouched. Final migration source hashes still match this experiment.

Independent MCP 0.6.0 wheel installation in a fresh Python 3.12 environment registered **162 tools**, including all ten project-operation/manifest/bundle schemas. Pandrator, Manager and SQLAlchemy were absent; no application tool mutation was executed. Manager 0.9.28 wheel core imports passed without the app or GUI modules. Final archive/public artifact acceptance is recorded separately after rebuilding from the committed source.

## Parent browser acceptance

CUA operated the real bundled frontend and real application/worker in an isolated fixture workspace. Synthetic translations and timings are explicitly fixtures, not provider-inference evidence.

- Settings save/reopen, clean saved-review baseline, row-specific invalid-save errors, Delete/Undo and Save/Discard/Cancel passed. Speaker edits persisted in immutable revisions.
- A Japanese passage with complete pinned word evidence split into `庭は静かです。` and `朝の光は暖かいです。`, preserving punctuation and positive anchored windows. Partial or ambiguous evidence is refused by focused tests. A combined Chinese display cue was read-only; editing its individual spoken passage retained the other speaker and timing after save/reopen.
- Changing composition settings while a spoken-passage draft was open rejected the stale save, kept the edited text and retained leave protection. Parent-owned cache checks covered deep-copy isolation, row rebinding, revision/source/composition fences, normalized clean baselines and bounded eviction.
- Unsupported FFV1 source video loaded with zero intrinsic dimensions. Managed compatible MP4 and explicit audio fallback played. Controlled FFmpeg cancellation stopped the process, preserved the source hash and registered no partial derivative. Its disposable throttling shim was removed from the acceptance server before final checks.
- Searches found Swahili, Burmese, Khmer, Lao, pt-BR and nb. Effective 390 CSS-pixel views had no document-level horizontal overflow. The source-first language project group, pinned-source details and blocked-generation reason were visible.
- Real subtitle export jobs completed for Japanese, Polish and German. The downloaded ZIP was 1,786 bytes, SHA-256 `022846453ba9d0717128d154f9cea78afa13ae481767b7a1b10620bd8696b469`; its embedded manifest and three SRT members matched every recorded size/hash and managed source file. Restart/repeat recovered the same completed bundle. A version change correctly required a fresh target-bound operation preview.

Screenshots are retained with this record. No complete screen-reader audit or every codec/device/provider matrix is claimed.

## Native synthesis and external translation boundary

The actual cached Kokoro service synthesized “The garden is quiet, and the morning light is warm.” using `af_heart`: finite, nonempty mono PCM WAV, 24 kHz, 2.991 seconds, 143,614 bytes, SHA-256 `3efe0e872892f64f2696551439c251e4a9b9c11a0da2d93e2fef82d612f3f0b0`. Cached model SHA-256 `496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4` remained unchanged. The service was restored to stopped, its port closed, and active work/Manager operations remained zero. No other model service was changed.

The intended live Japanese/Polish/German translation smoke could not obtain a provider response. Bounded Go attempts reached the configured custom route but returned HTTP 400 `MissingSessionID`: that route requires `x-opencode-session`, and no such header was configured or dropped by the harness. One alternative call used existing Vertex credentials and returned HTTP 403 because project billing was disabled. No credentials, raw provider configuration or private project identifiers were retained. No billing or provider settings were changed. Polish/German calls stopped at the first failure; no successful live translation or provider cost is claimed. Translation contracts, synthetic content and actual export/ZIP behavior were checked separately.

Representative native ASR, detector and separation measurements, including Qwen point-like raw timestamps and long CPU benchmark limits, are in [native ASR and separation evidence](native-asr-and-separation.md).
