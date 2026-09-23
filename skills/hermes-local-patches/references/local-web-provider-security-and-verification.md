# Local web-provider patch: security and verification

Use this reference when preserving or reapplying a locally added Hermes `web_extract` provider after an update.

## Architecture pattern

- Implement the extractor as a normal bundled web-provider plugin with an extraction-only capability declaration.
- Keep search and extraction selections separate. An extraction-only provider should write `web.extract_backend`; it must not replace `web.search_backend` or the shared fallback unintentionally.
- Declare optional Python dependencies in the project extra and lazy-dependency registry, and update the lockfile. A one-time interactive `pip install` is not durable.
- Give the setup schema a capability marker such as `web_capability: extract`, and ensure both interactive and non-interactive provider-selection paths preserve the other capability.

## Network security boundary

Do not let a third-party content-extraction library fetch user-controlled URLs directly merely because Hermes performed a preflight URL check. Library downloaders may:

- resolve DNS again after validation,
- follow redirects to private/internal addresses,
- retry TLS failures with certificate verification disabled,
- buffer unbounded responses.

Instead:

1. Fetch through Hermes's `tools.url_safety.create_ssrf_safe_client` or async equivalent.
2. Use connect-time IP validation.
3. Revalidate every redirect target with a response hook.
4. Keep normal TLS certificate verification enabled.
5. Stream with an explicit response-size cap.
6. Pass only the already-fetched HTML to the extraction library.

A one-time `is_safe_url()` check is defense in depth, not a substitute for connect-time and redirect-hop enforcement.

## Trafilatura 2.x compatibility

- `bare_extraction()` returns a `trafilatura.settings.Document`, not necessarily a dictionary.
- Read `Document` attributes (`text`, `title`, `author`, `date`, `sitename`) and retain a defensive dictionary fallback if supporting older versions.
- Use `output_format="python"` when requesting a Document result.
- Avoid delegating page download to `trafilatura.fetch_url()`; use Hermes's safe downloader and provide downloaded HTML to `bare_extraction()`.

## Verification checklist

1. Confirm configuration resolves independently:
   - `web.search_backend` remains the intended search provider.
   - `web.extract_backend` is the local extractor.
2. Test setup/provider selection from a config containing both a shared fallback and a distinct search provider; selecting the extractor must change only `extract_backend`.
3. Unit-test:
   - capability declarations and setup schema,
   - normalized result shape and metadata,
   - empty-download and empty-extraction errors,
   - actual `web_extract` registry dispatch,
   - SSRF-safe client construction,
   - redirect rejection,
   - TLS verification not disabled,
   - response-size enforcement.
4. Run the complete web-tool and web-plugin suites, configuration/server provider tests, Ruff, `git diff --check`, and `uv lock --check`.
5. Live-test both search and extraction through the actual Hermes wrappers, not only a direct provider call.
6. Request an independent fail-closed review. Any security concern or logic error blocks activation.
7. Restart the gateway from an independent supervisor/timer rather than asking the running gateway process to synchronously restart itself.

## Review pitfall

A provider can pass happy-path extraction tests and still be unsafe. Inspect the downloader's redirect, DNS, TLS, and buffering behavior explicitly. Treat the extractor as a parser only unless its network behavior has been audited to Hermes's standards.
