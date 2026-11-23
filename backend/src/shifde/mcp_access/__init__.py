"""MCP access (SP8 design 3, DECISIONS B-3923): SETH as an MCP server at `/mcp`.

Stateless streamable HTTP (`FastMCP`), mounted by `shifde.app` only when `SHIFDE_MCP_ENABLED`.
Only bearer `shf_` API keys holding `mcp:access` at organization level call it, and only while
their organization's `mcp_client_settings.enabled` is on. Every tool calls the owning module's
service with the caller's `TenantScope`, never a repository (import-linter forbids it).
"""
