# Kiro Gateway refusal patch

`kiro-refusals.patch` is a downstream modification of the pinned Kiro Gateway
source, licensed under AGPL-3.0 like that source. Copyright and license notices
remain in `vendor/kiro-gateway`. The patch includes parser and formatter changes
plus regression tests for both API formats and streaming modes.

`scripts/stack.py setup` and `serve` apply it under an OS lock. Repeated application
is harmless. Conflicting source edits cause a clear error and are preserved.
The submodule remains at its pinned commit with this patch in its working tree.
After changing the pin, review and regenerate the patch instead of discarding it.

Run its tests from `vendor/kiro-gateway`:

```powershell
../../.venv/Scripts/python.exe -m pytest tests/unit/test_parsers.py tests/unit/test_streaming_core.py tests/unit/test_streaming_anthropic.py tests/unit/test_streaming_openai.py -q
```
