"""Pure helpers for the local-protocol adapter.

These are deliberately side-effect free so they can be unit-tested without
Docker or a live sandbox.
"""

from __future__ import annotations


def rewrite_conversation_url(
    external_base: str,
    sandbox_id: str,
    conversation_id: str,
) -> str:
    """Rewrite a sandbox-native conversation_url to the externally-reachable

    ``/runtime/{sandbox_id}`` prefix.

    Args:
        external_base: ``str(request.base_url).rstrip('/')`` — the origin the
            browser reached (e.g. ``http://localhost:3000``).
        sandbox_id: The owning sandbox's id.
        conversation_id: Hex (or canonical) conversation id.

    Returns:
        ``{external_base}/runtime/{sandbox_id}/api/conversations/{conversation_id}``

    Example:
        >>> rewrite_conversation_url('http://localhost:8000', 'abc', 'def')
        'http://localhost:8000/runtime/abc/api/conversations/def'
    """
    base = external_base.rstrip('/')
    sid = sandbox_id.strip('/')
    cid = conversation_id.strip('/')
    return f'{base}/runtime/{sid}/api/conversations/{cid}'


def external_base_from_request_base_url(base_url: str) -> str:
    """Normalise ``str(request.base_url)`` to the external base.

    FastAPI's ``request.base_url`` always ends with ``/``; this strips it so
    callers can safely append ``/runtime/...``.
    """
    return base_url.rstrip('/')


# ---------------------------------------------------------------------------
# Working-dir → sandbox index
# ---------------------------------------------------------------------------


class WorkingDirIndex:
    """In-memory ``working_dir → sandbox_id`` index with longest-prefix routing.

    Registered at conversation create; used by workspace-scoped endpoints
    (``/api/git/changes``, ``/api/file/*``) that the frontend calls on the
    backend host with only a ``path`` query param.

    The index also tracks the most-recently-registered sandbox as a fallback
    when no prefix matches.
    """

    def __init__(self) -> None:
        self._index: dict[str, str] = {}
        self._most_recent_sandbox_id: str | None = None

    # -- mutation ----------------------------------------------------------

    def register(self, working_dir: str, sandbox_id: str) -> None:
        self._index[working_dir] = sandbox_id
        self._most_recent_sandbox_id = sandbox_id

    def clear(self) -> None:
        self._index.clear()
        self._most_recent_sandbox_id = None

    # -- query -------------------------------------------------------------

    @property
    def most_recent_sandbox_id(self) -> str | None:
        return self._most_recent_sandbox_id

    def resolve(self, path: str) -> str | None:
        """Resolve ``path`` to a sandbox id.

        Picks the registered ``working_dir`` that is the longest prefix of
        ``path`` (path-boundary aware).  Falls back to the most-recently-
        registered sandbox when no prefix matches.

        Returns None when the index is empty.
        """
        if not self._index:
            return None

        best_sid: str | None = None
        best_len = -1
        for wd, sid in self._index.items():
            if _is_prefix(wd, path):
                if len(wd) > best_len:
                    best_len = len(wd)
                    best_sid = sid

        if best_sid is not None:
            return best_sid
        return self._most_recent_sandbox_id

    def snapshot(self) -> dict[str, str]:
        return dict(self._index)


def _is_prefix(working_dir: str, path: str) -> bool:
    """True if ``working_dir`` is a path-prefix of ``path``.

    Boundary-aware: ``/workspace/project`` is a prefix of
    ``/workspace/project/file.txt`` and ``/workspace/project`` itself, but
    NOT of ``/workspace/project2``.
    """
    if not working_dir:
        return False
    # Normalise trailing slashes for comparison, but keep root '/' intact
    wd = working_dir.rstrip('/') or '/'
    p = path.rstrip('/') or '/'
    if wd == '/':
        return p.startswith('/')
    if p == wd:
        return True
    # Ensure the prefix ends at a '/' boundary
    return p.startswith(wd + '/')


# ---------------------------------------------------------------------------
# Shape helpers (pure, testable mappings)
# ---------------------------------------------------------------------------


def build_server_info(version: str) -> dict:
    """Synthesise the ``/server_info`` payload expected by agent-canvas."""
    return {
        'version': version,
        'sdk_version': version,
        'usable_tools': [
            'terminal',
            'file_editor',
            'task_tracker',
            'browser_tool_set',
        ],
        'compatibility': {'minimum_agent_server': '1.28.0'},
    }


def build_upstream_ws_url(agent_base: str, path: str, query: str) -> str:
    """Build the upstream WebSocket URL for the sandbox bridge.

    Args:
        agent_base: Sandbox agent-server base URL (e.g. ``http://localhost:18000``).
        path: Upstream path (e.g. ``/sockets/events/{id}``).
        query: Raw query string without leading ``?`` (may be empty).

    Returns:
        ``{agent_base}{path}`` with ``?{query}`` appended when non-empty.
    """
    url = f'{agent_base.rstrip("/")}{path}'
    if query:
        url += f'?{query}'
    return url


def verified_map_to_object(verified_by_provider: dict[str, list[str]] | None) -> dict:
    """Passthrough helper for the ``/api/llm/models/verified`` shape.

    The agent-canvas Typescript client reads ``Object.keys(verifiedByProvider)``.
    We ensure the response is always an object, even when upstream returns None.
    """
    return verified_by_provider or {}


def providers_page_to_wrapped(providers: list[str]) -> dict:
    """Wrap a plain provider name list into the agent-server wire shape."""
    return {'providers': providers}


def models_to_wrapped(models: list[str]) -> dict:
    """Wrap a plain model name list into the agent-server wire shape."""
    return {'models': models}
