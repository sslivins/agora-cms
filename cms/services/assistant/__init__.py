"""Assistant feature internals.

The Assistant feature is split across several modules so each layer can
be tested in isolation:

* :mod:`cms.services.assistant.llm_client` — Azure OpenAI wrapper.
* :mod:`cms.services.assistant.prompts`    — system-prompt builder.
* :mod:`cms.services.assistant.agent`      — orchestration: persists
  the user turn, calls the LLM, persists the assistant turn.

PR 3a (this PR) ships a non-streaming, no-tools single-shot flow.
PR 3b will add MCP tool calls + SSE streaming on top of the same
interfaces.
"""
