'''Unit tests for local-protocol pure helpers.'''

import pytest

from openhands.app_server.local_protocol.helpers import (
    WorkingDirIndex,
    _is_prefix,
    build_server_info,
    external_base_from_request_base_url,
    models_to_wrapped,
    providers_page_to_wrapped,
    rewrite_conversation_url,
    verified_map_to_object,
)


class TestRewriteConversationUrl:
    def test_basic(self):
        url = rewrite_conversation_url('http://localhost:8000', 'abc123', 'def456')
        assert url == 'http://localhost:8000/runtime/abc123/api/conversations/def456'

    def test_strips_trailing_slash(self):
        url = rewrite_conversation_url('http://localhost:8000/', 'abc', 'def')
        assert url == 'http://localhost:8000/runtime/abc/api/conversations/def'

    def test_strips_portion_slashes(self):
        url = rewrite_conversation_url('http://host:3000/', '/sid/', '/cid/')
        assert url == 'http://host:3000/runtime/sid/api/conversations/cid'

    def test_external_base_helper(self):
        assert external_base_from_request_base_url('http://localhost:3000/') == 'http://localhost:3000'
        assert external_base_from_request_base_url('http://example.com/base/') == 'http://example.com/base'


class TestWorkingDirIndex:
    def test_empty_resolves_to_none(self):
        idx = WorkingDirIndex()
        assert idx.resolve('/workspace/project/file.txt') is None

    def test_longest_prefix_wins(self):
        idx = WorkingDirIndex()
        idx.register('/workspace/project', 'sb1')
        idx.register('/workspace/project/subdir', 'sb2')
        assert idx.resolve('/workspace/project/subdir/file.txt') == 'sb2'
        assert idx.resolve('/workspace/project/other.txt') == 'sb1'

    def test_exact_match(self):
        idx = WorkingDirIndex()
        idx.register('/workspace/project', 'sb1')
        assert idx.resolve('/workspace/project') == 'sb1'

    def test_boundary_aware(self):
        idx = WorkingDirIndex()
        idx.register('/workspace/project', 'sb1')
        # Should NOT match project2
        idx.register('/workspace/project2', 'sb2')
        assert idx.resolve('/workspace/project2/file.txt') == 'sb2'
        # Fallback to most recent when no prefix matches but index non-empty
        assert idx.resolve('/other/path') == 'sb2'

    def test_fallback_to_most_recent(self):
        idx = WorkingDirIndex()
        idx.register('/workspace/project', 'sb1')
        idx.register('/workspace/other', 'sb2')
        # Path not matching any prefix falls back to most recent
        assert idx.resolve('/no/match') == 'sb2'

    def test_clear(self):
        idx = WorkingDirIndex()
        idx.register('/workspace/project', 'sb1')
        idx.clear()
        assert idx.resolve('/workspace/project') is None
        assert idx.most_recent_sandbox_id is None

    def test_snapshot(self):
        idx = WorkingDirIndex()
        idx.register('/a', 's1')
        idx.register('/b', 's2')
        snap = idx.snapshot()
        assert snap == {'/a': 's1', '/b': 's2'}


class TestIsPrefix:
    def test_is_prefix_true(self):
        assert _is_prefix('/workspace/project', '/workspace/project/file.txt') is True
        assert _is_prefix('/workspace/project', '/workspace/project') is True
        assert _is_prefix('/workspace/project/', '/workspace/project') is True

    def test_is_prefix_false_boundary(self):
        assert _is_prefix('/workspace/project', '/workspace/project2/file.txt') is False
        assert _is_prefix('/workspace/project', '/workspace/project-other') is False

    def test_is_prefix_root(self):
        assert _is_prefix('/', '/workspace/project') is True


class TestBuildServerInfo:
    def test_shape(self):
        info = build_server_info('1.37.1')
        assert info['version'] == '1.37.1'
        assert info['sdk_version'] == '1.37.1'
        assert info['compatibility']['minimum_agent_server'] == '1.28.0'
        assert 'terminal' in info['usable_tools']


class TestShapeHelpers:
    def test_providers_wrapped(self):
        assert providers_page_to_wrapped(['a', 'b']) == {'providers': ['a', 'b']}

    def test_models_wrapped(self):
        assert models_to_wrapped(['m1', 'm2']) == {'models': ['m1', 'm2']}

    def test_verified_map(self):
        assert verified_map_to_object(None) == {}
        assert verified_map_to_object({'openai': ['gpt-4']}) == {'openai': ['gpt-4']}
