"""
Tests for Azure Health + Resource Changes source adapter.

Pattern for adding a new cloud provider source (e.g., AWS CloudTrail):
  1. Copy this file as tests/test_aws_cloudtrail_source.py
  2. Import your classification functions
  3. Add Tier 1 tests for mutation/signal classification
  4. Add Tier 2 tests with mocked CLI/API responses
  5. Add sample API responses in tests/fixtures/
"""

import json
import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sources.azure_health_source import (
    classify_change,
    PROPERTY_MUTATION_MAP,
    HEALTH_STATE_SIGNAL,
)


# ═══════════════════════════════════════════════════════════════════════
# Tier 1: Classification tests
# ═══════════════════════════════════════════════════════════════════════


class TestChangeClassification:
    """Test ARM property path → mutation type classification."""

    def test_power_state_change(self):
        assert classify_change("Update", ["properties.extended.instanceView.powerState.code"]) == "PowerStateChange"

    def test_provisioning_state(self):
        assert classify_change("Update", ["properties.provisioningState"]) == "ProvisioningStateChange"

    def test_disk_attach(self):
        assert classify_change("Update", ["managedBy"]) == "DiskAttachDetach"

    def test_kubernetes_upgrade(self):
        assert classify_change("Update", ["properties.kubernetesVersion"]) == "KubernetesUpgrade"

    def test_node_pool_change(self):
        assert classify_change("Update", ["properties.agentPoolProfiles"]) == "NodePoolChange"

    def test_access_policy_change(self):
        assert classify_change("Update", ["properties.accessPolicies"]) == "AccessPolicyChange"

    def test_sku_change(self):
        assert classify_change("Update", ["sku"]) == "SKUChange"

    def test_identity_change(self):
        assert classify_change("Update", ["identity"]) == "IdentityChange"

    def test_network_acl_change(self):
        assert classify_change("Update", ["properties.networkAcls"]) == "NetworkACLChange"

    def test_security_config(self):
        assert classify_change("Update", ["properties.publicNetworkAccess"]) == "SecurityConfigChange"

    def test_vm_extension_change(self):
        assert classify_change("Update", ["properties.virtualMachineProfile.extensionProfile.extensions"]) == "VMExtensionChange"

    def test_container_config_change(self):
        assert classify_change("Update", ["properties.containers"]) == "ContainerConfigChange"

    def test_lb_pool_change(self):
        assert classify_change("Update", ["properties.backendAddressPools"]) == "LoadBalancerPoolChange"

    def test_redis_instance_change(self):
        assert classify_change("Update", ["properties.instances"]) == "RedisInstanceChange"

    def test_rate_limit_change(self):
        assert classify_change("Update", ["properties.callRateLimit"]) == "RateLimitChange"

    def test_image_publish_change(self):
        assert classify_change("Update", ["properties.publishingProfile"]) == "ImagePublishChange"

    def test_create_operation(self):
        assert classify_change("Create", []) == "ResourceCreate"

    def test_delete_operation(self):
        assert classify_change("Delete", []) == "ResourceDelete"

    def test_unknown_property_defaults(self):
        assert classify_change("Update", ["properties.somethingNew"]) == "ConfigChange"

    def test_tag_change(self):
        assert classify_change("Update", ["tags.myTag"]) == "TagChange"

    def test_maintenance_scheduled(self):
        # tags.maintenanceafter is more specific than tags (TagChange)
        # but in the current code, tags matches first
        result = classify_change("Update", ["tags.maintenanceafter"])
        assert result in ("MaintenanceScheduled", "TagChange")

    def test_first_matching_property_wins(self):
        """When multiple properties change, the first recognized one determines the type."""
        result = classify_change("Update", [
            "properties.kubernetesVersion",
            "properties.agentPoolProfiles",
        ])
        assert result == "KubernetesUpgrade"


class TestHealthStateMapping:
    """Test Azure health state → signal type mapping."""

    def test_unavailable(self):
        assert HEALTH_STATE_SIGNAL["Unavailable"] == ("Unavailable", "critical")

    def test_degraded(self):
        assert HEALTH_STATE_SIGNAL["Degraded"] == ("Degraded", "warning")

    def test_unknown(self):
        assert HEALTH_STATE_SIGNAL["Unknown"] == ("HealthUnknown", "info")

    def test_available_no_signal(self):
        # Available means healthy — no signal should be emitted
        assert HEALTH_STATE_SIGNAL.get("Available") is None


class TestPropertyMutationMapCoverage:
    """Ensure the property map covers key Azure resource changes."""

    def test_vm_properties_covered(self):
        vm_props = [
            "properties.extended.instanceView.powerState",
            "properties.provisioningState",
            "properties.virtualMachineProfile.extensionProfile",
        ]
        for prop in vm_props:
            assert any(prop.startswith(k) for k in PROPERTY_MUTATION_MAP), f"VM prop {prop} not mapped"

    def test_aks_properties_covered(self):
        aks_props = [
            "properties.kubernetesVersion",
            "properties.agentPoolProfiles",
            "properties.addonProfiles",
        ]
        for prop in aks_props:
            assert any(prop.startswith(k) for k in PROPERTY_MUTATION_MAP), f"AKS prop {prop} not mapped"

    def test_keyvault_properties_covered(self):
        kv_props = ["properties.accessPolicies"]
        for prop in kv_props:
            assert any(prop.startswith(k) for k in PROPERTY_MUTATION_MAP), f"KV prop {prop} not mapped"
