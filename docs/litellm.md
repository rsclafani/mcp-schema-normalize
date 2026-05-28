# LiteLLM integration guide

Deeper coverage than the README's quick-start. Read this if your container is hardened, your callbacks have ordering concerns, or the hook seems to silently not fire.

## Installation paths

The library has to be importable from the LiteLLM proxy's Python environment. Pick the path that matches your deployment.

### Path A: custom Docker image (recommended)

The standard pattern for adding callbacks to a LiteLLM proxy. Works on any default LiteLLM Docker deployment, k8s / Helm chart, or systemd-managed container.

```dockerfile
FROM ghcr.io/berriai/litellm:main-latest
RUN pip install --no-cache-dir 'mcp-schema-normalize[litellm]'
```

Build and push:

```bash
docker build -t my-registry/litellm-with-schema-normalize:latest .
docker push my-registry/litellm-with-schema-normalize:latest
```

Then point your `docker-compose.yml` / k8s deployment / etc. at the custom tag. Restart the proxy.

**Pin for reproducibility** by pinning to a published version once it's on PyPI:

```dockerfile
RUN pip install --no-cache-dir 'mcp-schema-normalize[litellm]==0.1.0'
```

Or, until PyPI is set up, pin to a git ref:

```dockerfile
RUN pip install --no-cache-dir \
    'mcp-schema-normalize[litellm] @ git+https://github.com/rsclafani/mcp-schema-normalize.git@v0.1.0'
```

### Path B: volume-mount the package source (hardened / read-only containers)

Use this if your container has `read_only: true`, `cap_drop: ALL`, or other security hardening that prevents `pip install` at build/runtime, **and** you don't want to maintain a custom image.

Clone the repo somewhere on the host:

```bash
git clone https://github.com/rsclafani/mcp-schema-normalize.git ~/code/mcp-schema-normalize
```

Mount the package source into the container and add it to `PYTHONPATH`. Example `docker-compose.yml` excerpt:

```yaml
services:
  litellm:
    image: ghcr.io/berriai/litellm:main-latest
    read_only: true
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ~/code/mcp-schema-normalize/src/mcp_schema_normalize:/app/lib/mcp_schema_normalize:ro
    environment:
      PYTHONPATH: /app:/app/lib
    # ... rest of your config
```

You still need `litellm` to be installed in the container's Python (which it is, since the image ships with it). Only the *integration* class has a hard dependency on `litellm`; the pure core would work without it.

**Tradeoff**: you're loading the library as a directory, not a wheel. No metadata, no `pip show mcp-schema-normalize`, no dependency resolution. For a zero-runtime-dep library like this one it's fine; you just don't get the niceties.

### Path C: in-tree shim (development / debugging)

If you're modifying the library and want hot reloads against the LiteLLM container without rebuilding the image every time, put a thin shim in your callbacks directory:

```python
# callbacks/normalize_tool_schemas.py
from mcp_schema_normalize.integrations.litellm import normalize_tool_schemas_handler

__all__ = ["normalize_tool_schemas_handler"]
```

Then mount the library source as in Path B, and register the callback either way. This indirection lets you swap between an installed version and a local source tree with one edit.

## Registering the callback

After install, add to `config.yaml`:

```yaml
litellm_settings:
  callbacks:
    - "mcp_schema_normalize.integrations.litellm.normalize_tool_schemas_handler"
```

The string is the importable path to the **module-level handler instance** the library exports. LiteLLM will import the module, look up the attribute, and add it to the proxy's pre-call hook chain.

You can also instantiate the handler yourself if you want to subclass or wrap it:

```yaml
callbacks:
  - "my_module.my_custom_handler_instance"
```

```python
# my_module.py
from mcp_schema_normalize.integrations.litellm import NormalizeToolSchemasHandler

class MyHandler(NormalizeToolSchemasHandler):
    # ... override async_pre_call_hook if needed
    pass

my_custom_handler_instance = MyHandler()
```

## Callback ordering

LiteLLM runs `async_pre_call_hook` on callbacks in registration order. This library should generally run **early** in the chain, because subsequent callbacks may inspect or filter `data["tools"]` based on schema shape.

Common ordering with other callbacks we've encountered:

```yaml
callbacks:
  - "otel"                                                                   # telemetry first
  - "mcp_schema_normalize.integrations.litellm.normalize_tool_schemas_handler"  # rewrite schemas
  - "callbacks.strip_invalid_tools.strip_invalid_tools_handler"              # drop non-function entries after rewrite
  # ... auth / rate-limit / etc.
```

If a downstream callback filters tools based on `function.parameters` content (e.g., dropping tools whose schemas look "too complex"), it'll see the normalized version. That's usually what you want.

## Logging

The library's logger name is `mcp_schema_normalize.integrations.litellm`. By default it inherits level/handler config from the root logger — meaning LiteLLM's `LITELLM_LOG` env var controls it.

```bash
LITELLM_LOG=INFO   # surfaces routine summary lines
LITELLM_LOG=WARNING  # default; only lossy-rewrite summaries surface
```

Each modified request emits one summary log line. All telemetry counters are attached as structured `extra={...}` LogRecord attributes, which any JSON formatter (`python-json-logger`, structlog with a stdlib adapter, etc.) will pick up automatically.

Example formatted record (with JSON formatter):

```json
{
  "level": "WARNING",
  "logger": "mcp_schema_normalize.integrations.litellm",
  "message": "mcp_schema_normalize summary: model=qwen3-coder-30b-a3b modified=13/44",
  "model": "qwen3-coder-30b-a3b",
  "tools_modified": 13,
  "tools_seen": 44,
  "refs_inlined": 0,
  "refs_unresolved": 9,
  "anyof_rewrites": 14,
  "not_drops": 81,
  "...": "..."
}
```

Without a JSON formatter, the message is greppable but the structured fields are silent. A reasonable Grafana / Datadog alert:

```
sum(rate(refs_unresolved[5m])) by (model) > 0
```

This pages whenever any tool's schema starts emitting dangling refs — usually a sign an MCP server you depend on has an upstream bug.

## Troubleshooting

### "The hook isn't logging anything"

Two likely causes:

1. **LiteLLM's root log level filters INFO.** The library only emits a summary line when at least one tool's schema was *modified*. If a request's tools were all simple enough to pass through untouched, nothing fires. Send a request with a known-broken schema (anything with `anyOf` beside `properties`) to verify the hook is reachable, or set `LITELLM_LOG=INFO`.

2. **The callback isn't registered.** Confirm via the proxy startup log:

   ```
   Initialized litellm callbacks, Async Success Callbacks: [
     ...,
     <mcp_schema_normalize.integrations.litellm.NormalizeToolSchemasHandler ...>,
     ...,
   ]
   ```

   If the class isn't in that list, your `config.yaml` callback string is wrong (typo in the import path) or the library isn't on `PYTHONPATH`.

### "I see `refs_unresolved=N` and the model is misbehaving"

This is the [load-bearing fallback](../README.md#%EF%B8%8F-when-not-to-use-this--load-bearing-assumption) firing. Some MCP server's `$ref`s don't resolve, so they're being replaced with `{}` (permissive). The model is free to emit structurally wrong values for those fields.

Path forward:
- Identify which MCP server is emitting broken schemas (the per-ref WARN line tells you the pointer string)
- File an upstream bug there
- Until the upstream fix lands, either accept the loosened validation or set `STRICT_UNRESOLVED_REFS = True` (in your own startup code) to fail-loud instead of fail-open

### "I'm getting 400s about `$ref` resolution"

If you've set `STRICT_UNRESOLVED_REFS = True`, the library leaves dangling refs in place and llama.cpp will reject the tool. This is intentional in strict mode. To turn it off, just remove that override and the permissive fallback re-engages.

### "The callback list at startup shows the class but counters are zero"

The hook only acts when `data["tools"]` is non-empty. If your requests are pure chat completions without tool calls, there's nothing to normalize. Send a tool-carrying request to verify the hook fires.

## Compatibility

| LiteLLM version | Status |
|---|---|
| 1.x | Tested in production; primary target |
| 0.x | Untested; may work if `litellm.integrations.custom_logger.CustomLogger` exists |
| Future v2.x | Will track API changes as they land |

If LiteLLM's `CustomLogger` interface changes (e.g., new required methods, signature changes to `async_pre_call_hook`), this library will need a corresponding release. Open an issue if you hit a version that doesn't work.
