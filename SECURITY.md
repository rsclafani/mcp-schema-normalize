# Security Policy

## Supported Versions

This is a pre-1.0 library. Only the latest minor version receives security updates.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

## Reporting a Vulnerability

**Please do not file a public GitHub issue for security vulnerabilities.** Public issues are the right channel for ordinary bugs and feature requests, but a security disclosure deserves a private channel where the maintainers can respond before details become widely visible.

### Preferred: GitHub Private Vulnerability Reporting

Open a private security advisory at https://github.com/rsclafani/mcp-schema-normalize/security/advisories/new

This gives us a private workspace to discuss the issue, coordinate a fix, and (if needed) request a CVE — all without public disclosure until you and we agree it's time.

### Fallback: email

If you can't use the GitHub advisory flow, email `rsclafani@gmail.com` with `[mcp-schema-normalize security]` in the subject line.

## What to include

A useful report contains:

- A description of the vulnerability and its impact
- A minimal reproduction (schema, code snippet, request payload — whatever applies)
- Affected version(s)
- Suggested fix, if you have one

## What to expect

This is a solo-maintained project on a side-project cadence. I commit to:

- Acknowledging your report within **one week** of receipt
- A status update at least every **two weeks** until the issue is resolved or declined
- Crediting you in the advisory and CHANGELOG entry (unless you prefer not to be credited)

If a vulnerability is confirmed, I'll target a fix release within **30 days** of confirmation for high-severity issues, and within the next regular release for lower-severity ones.

## Scope

This library transforms JSON Schema documents in-process. The realistic attack surface is:

- **Schemas as untrusted input**: a malicious schema (deeply nested, intentionally pathological, or crafted to exploit a parser bug) could degrade performance or crash the host process. The pipeline's depth caps and size budget are intended to mitigate this; bugs in those caps are in scope.
- **Logging side channels**: the library logs ref paths and telemetry. A malicious schema could try to inject formatting strings or escape sequences via field values that end up in log output. Bugs that allow this are in scope.
- **Permissive-fallback escalation**: a malicious caller could deliberately craft schemas with dangling refs to widen what the model emits. This is documented and intended behavior; if `STRICT_UNRESOLVED_REFS = True` is set, dangling refs are rejected. A bypass of the `STRICT_UNRESOLVED_REFS = True` setting is in scope.

Out of scope:

- Bugs in LiteLLM, llama.cpp, or upstream MCP servers
- Schema bugs in `zod-to-json-schema` or other JSON Schema generators
- Misuse by callers (e.g., feeding the library schemas from untrusted sources without input validation)
