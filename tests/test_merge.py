"""
Tests for the GraphPayload merge utility.
"""

import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sources.merge import merge_graphs


class TestMergeGraphs:
    """Test graph merging logic."""

    def test_merge_empty(self):
        result = merge_graphs({"nodes": [], "edges": []})
        assert result["nodes"] == []
        assert result["edges"] == []

    def test_merge_single_graph(self):
        g = {
            "nodes": [{"id": "a", "label": "A", "class": "X", "region": None, "rack_id": None, "properties": {}}],
            "edges": [],
        }
        result = merge_graphs(g)
        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["id"] == "a"

    def test_node_dedup(self):
        g1 = {"nodes": [{"id": "a", "label": "A1", "class": "X", "properties": {}}], "edges": []}
        g2 = {"nodes": [{"id": "a", "label": "A2", "class": "Y", "properties": {}}], "edges": []}
        result = merge_graphs(g1, g2)
        assert len(result["nodes"]) == 1
        # Later source overrides
        assert result["nodes"][0]["label"] == "A2"

    def test_property_shallow_merge(self):
        g1 = {"nodes": [{"id": "a", "properties": {"source": "arg", "region": "eastus"}}], "edges": []}
        g2 = {"nodes": [{"id": "a", "properties": {"source": "tf", "extra": True}}], "edges": []}
        result = merge_graphs(g1, g2)
        props = result["nodes"][0]["properties"]
        assert props["source"] == "tf"       # overridden
        assert props["region"] == "eastus"    # preserved from g1
        assert props["extra"] is True         # added from g2

    def test_edge_dedup(self):
        e = {"id": "e1", "source_id": "a", "target_id": "b", "edge_type": "dependency", "properties": {}}
        g1 = {"nodes": [{"id": "a"}, {"id": "b"}], "edges": [e]}
        g2 = {"nodes": [{"id": "a"}, {"id": "b"}], "edges": [e]}
        result = merge_graphs(g1, g2)
        assert len(result["edges"]) == 1

    def test_dangling_edges_dropped(self):
        g = {
            "nodes": [{"id": "a"}],
            "edges": [{"id": "e1", "source_id": "a", "target_id": "missing", "edge_type": "dep", "properties": {}}],
        }
        result = merge_graphs(g)
        assert len(result["edges"]) == 0

    def test_cross_source_edges(self):
        g1 = {"nodes": [{"id": "a"}], "edges": []}
        g2 = {
            "nodes": [{"id": "b"}],
            "edges": [{"id": "e1", "source_id": "a", "target_id": "b", "edge_type": "dep", "properties": {}}],
        }
        result = merge_graphs(g1, g2)
        assert len(result["edges"]) == 1  # cross-source edge valid because both nodes exist
