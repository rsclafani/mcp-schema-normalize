"""LiteLLM ``CustomLogger`` pre-call hook that normalizes tool schemas.

Install via the ``[litellm]`` extra::

    pip install mcp-schema-normalize[litellm]

Register in your LiteLLM proxy ``config.yaml``::

    litellm_settings:
      callbacks:
        - "mcp_schema_normalize.integrations.litellm.normalize_tool_schemas_handler"

Order matters: register **before** any callback that filters or rewrites
tools downstream (e.g. ``strip_invalid_tools``) so this hook normalizes
the OpenAI-format function schemas first.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy._types import UserAPIKeyAuth

from mcp_schema_normalize._core import is_lossy_telemetry, normalize_tools

logger = logging.getLogger("mcp_schema_normalize.integrations.litellm")


class NormalizeToolSchemasHandler(CustomLogger):
    """LiteLLM pre-call hook — rewrite tool schemas in-flight.

    For every chat-completion / responses / etc. call carrying a
    ``tools`` array, run each tool's ``function.parameters`` through
    :func:`mcp_schema_normalize.normalize_tools`. Non-function tool
    entries pass through untouched.

    Logging: one summary line per modified request, at INFO when the
    rewrites are routine and WARN when any lossy event fires (see
    :data:`mcp_schema_normalize.LOSSY_KEYS`). All telemetry counters
    are attached as structured ``extra=`` fields on the LogRecord for
    JSON-formatter consumption.
    """

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: Any,
        data: dict,
        call_type: Literal[
            "completion",
            "text_completion",
            "embeddings",
            "image_generation",
            "moderation",
            "audio_transcription",
            "pass_through_endpoint",
            "rerank",
            "mcp_call",
            "responses",
        ],
    ) -> dict | None:
        tools = data.get("tools")
        if tools is None:
            return data
        new_tools, telemetry = normalize_tools(tools)
        if telemetry["tools_modified"]:
            level = logging.WARNING if is_lossy_telemetry(telemetry) else logging.INFO
            logger.log(
                level,
                "mcp_schema_normalize summary: model=%s modified=%d/%d",
                data.get("model"),
                telemetry["tools_modified"],
                telemetry["tools_seen"],
                extra={
                    "model": data.get("model"),
                    "tools_modified": telemetry["tools_modified"],
                    "tools_seen": telemetry["tools_seen"],
                    **{k: telemetry[k] for k in telemetry
                       if k not in ("tools_modified", "tools_seen")},
                },
            )
            data["tools"] = new_tools
        return data


#: Module-level handler instance for direct registration in LiteLLM's
#: callback list. Equivalent to instantiating :class:`NormalizeToolSchemasHandler`
#: yourself.
normalize_tool_schemas_handler = NormalizeToolSchemasHandler()
