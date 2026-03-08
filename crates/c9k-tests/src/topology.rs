// Copyright (c) 2026 Sylvain Niles. MIT License.

//! Programmatic topology builder for Causinator 9000.
//!
//! Generates realistic Azure infrastructure graphs at any scale
//! without SQL, files, or Python. Use this for tests and benchmarks.
//!
//! # Example
//!
//! ```
//! use c9k_tests::topology::TopologyBuilder;
//!
//! let graph = TopologyBuilder::new()
//!     .regions(5)
//!     .racks_per_region(10)
//!     .vms_per_rack(10)
//!     .containers_per_vm(7)
//!     .apps_per_region(100)
//!     .pods_per_app(4)
//!     .build();
//!
//! // graph.nodes and graph.edges are ready to POST to /api/graph/load
//! println!("{} nodes, {} edges", graph.nodes.len(), graph.edges.len());
//! ```

use c9k_engine::solver::{BlueprintEdge, CausalNode, GraphPayload};

/// Builder for generating synthetic infrastructure topologies.
pub struct TopologyBuilder {
    regions: usize,
    racks_per_region: usize,
    vms_per_rack: usize,
    containers_per_vm: usize,
    identities_per_vm: usize,
    apps_per_region: usize,
    pods_per_app: usize,
    include_platform_services: bool,
    include_app_stacks: bool,
    region_names: Vec<String>,
}

impl Default for TopologyBuilder {
    fn default() -> Self {
        Self {
            regions: 10,
            racks_per_region: 10,
            vms_per_rack: 10,
            containers_per_vm: 7,
            identities_per_vm: 2,
            apps_per_region: 100,
            pods_per_app: 4,
            include_platform_services: true,
            include_app_stacks: true,
            region_names: vec![
                "eastus", "eastus2", "westus2", "westus3", "centralus",
                "northeurope", "westeurope", "southeastasia", "japaneast", "australiaeast",
            ].into_iter().map(|s| s.to_string()).collect(),
        }
    }
}

impl TopologyBuilder {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn regions(mut self, n: usize) -> Self { self.regions = n; self }
    pub fn racks_per_region(mut self, n: usize) -> Self { self.racks_per_region = n; self }
    pub fn vms_per_rack(mut self, n: usize) -> Self { self.vms_per_rack = n; self }
    pub fn containers_per_vm(mut self, n: usize) -> Self { self.containers_per_vm = n; self }
    pub fn identities_per_vm(mut self, n: usize) -> Self { self.identities_per_vm = n; self }
    pub fn apps_per_region(mut self, n: usize) -> Self { self.apps_per_region = n; self }
    pub fn pods_per_app(mut self, n: usize) -> Self { self.pods_per_app = n; self }
    pub fn include_platform_services(mut self, v: bool) -> Self { self.include_platform_services = v; self }
    pub fn include_app_stacks(mut self, v: bool) -> Self { self.include_app_stacks = v; self }

    /// Minimal topology — just physical infra, no apps.
    pub fn minimal() -> Self {
        Self::new()
            .regions(1)
            .racks_per_region(2)
            .vms_per_rack(3)
            .containers_per_vm(2)
            .identities_per_vm(1)
            .include_platform_services(false)
            .include_app_stacks(false)
    }

    /// Standard POC topology (~26k nodes).
    pub fn standard() -> Self {
        Self::new()
    }

    /// Large topology for scale testing (~46k nodes).
    pub fn large() -> Self {
        Self::new()
            .regions(10)
            .racks_per_region(10)
            .vms_per_rack(10)
            .containers_per_vm(7)
            .apps_per_region(200)
            .pods_per_app(6)
    }

    /// Realistic Azure region — models a single region at production scale.
    ///
    /// Structure:
    /// - 3 availability zones, 2 power domains each
    /// - 50 racks per AZ (150 total), 40 VMs per rack (6,000 VMs)
    /// - 4 containers + 1 managed identity per VM (30,000 containers)
    /// - 8 regional platform services (KV×3, ACR×2, DNS, CA, IdP)
    /// - 500 application stacks, each with:
    ///   - AKS cluster, gateway, LB, route, VNet, 2 subnets, 2 NSGs
    ///   - managed identity, data store (SQL/Redis/Mongo/MessageQueue)
    ///   - 6 pods per app (3,000 pods)
    ///
    /// Total: ~50k nodes, ~100k edges
    pub fn azure_region() -> Self {
        Self::new()
            .regions(1)
            .racks_per_region(150)
            .vms_per_rack(40)
            .containers_per_vm(4)
            .identities_per_vm(1)
            .apps_per_region(500)
            .pods_per_app(6)
    }

    /// Multiple Azure regions at production scale.
    /// Each region is an `azure_region()` — 3 regions gives ~150k nodes.
    pub fn azure_multi_region(regions: usize) -> Self {
        Self::new()
            .regions(regions)
            .racks_per_region(150)
            .vms_per_rack(40)
            .containers_per_vm(4)
            .identities_per_vm(1)
            .apps_per_region(500)
            .pods_per_app(6)
    }

    pub fn build(self) -> GraphPayload {
        let mut nodes: Vec<CausalNode> = Vec::new();
        let mut edges: Vec<BlueprintEdge> = Vec::new();

        let region_count = self.regions.min(self.region_names.len());

        for r in 0..region_count {
            let region = &self.region_names[r];
            let props = serde_json::json!({});

            // Latent: PowerDomain — 2 per region (redundant power)
            let pd1_id = format!("latent-power-{region}-1");
            let pd2_id = format!("latent-power-{region}-2");
            nodes.push(node(&pd1_id, &format!("Power Domain {region} A"), "PowerDomain", region, None));
            nodes.push(node(&pd2_id, &format!("Power Domain {region} B"), "PowerDomain", region, None));

            // Latent: AvailabilityZones — 3 per region
            let az_ids: Vec<String> = (1..=3).map(|z| format!("latent-az-{region}-{z}")).collect();
            for (i, az_id) in az_ids.iter().enumerate() {
                nodes.push(node(az_id, &format!("AZ {region}-{}", i+1), "AvailabilityZone", region, None));
                // AZ 1,2 → PD1; AZ 3 → PD2 (realistic: 2 AZs share power, 1 is isolated)
                let pd = if i < 2 { &pd1_id } else { &pd2_id };
                edges.push(edge(pd, az_id, "containment"));
            }

            // Platform services — multiple instances for realism
            if self.include_platform_services {
                // 3 KeyVaults (secrets, certs, app-keys)
                for kv_idx in 1..=3 {
                    let kv_id = format!("kv-{region}-{kv_idx:02}");
                    nodes.push(node(&kv_id, &format!("KeyVault {region}/{kv_idx}"), "KeyVault", region, None));
                }
                // 2 container registries (prod, staging)
                for acr_idx in 1..=2 {
                    let acr_id = format!("acr-{region}-{acr_idx:02}");
                    nodes.push(node(&acr_id, &format!("ACR {region}/{acr_idx}"), "ContainerRegistry", region, None));
                }
                // Regional gateway (AGW)
                let gw_id = format!("gw-{region}-01");
                nodes.push(node(&gw_id, &format!("Regional Gateway {region}"), "Gateway", region, None));
                // DNS
                let dns_id = format!("dns-{region}");
                nodes.push(node(&dns_id, &format!("DNS {region}"), "DNS", region, None));
                // Identity provider (AAD/Entra)
                let idp_id = format!("idp-{region}");
                nodes.push(node(&idp_id, &format!("IdentityProvider {region}"), "IdentityProvider", region, None));
                // Certificate authority
                let ca_id = format!("ca-{region}");
                nodes.push(node(&ca_id, &format!("CertAuthority {region}"), "CertAuthority", region, None));
                // 2 shared message queue clusters (Event Hub)
                for mq_idx in 1..=2 {
                    let mq_id = format!("mq-{region}-{mq_idx:02}");
                    nodes.push(node(&mq_id, &format!("MessageQueue {region}/{mq_idx}"), "MessageQueue", region, None));
                }
                // Shared environment node
                let env_id = format!("env-{region}-prod");
                nodes.push(node(&env_id, &format!("Env {region}/prod"), "Environment", region, None));
            }

            // Physical: racks → VMs → containers/identities
            // Distribute racks across AZs
            for rack in 0..self.racks_per_region {
                let az_idx = rack % az_ids.len();
                let az_id = &az_ids[az_idx];
                let rack_id_str = format!("rack-{region}-{rack:03}");
                let tor_id = format!("latent-tor-{region}-{rack:03}");
                nodes.push(node_with_rack(&tor_id, &format!("ToR {region} rack {rack:03}"), "ToRSwitch", region, &rack_id_str));
                edges.push(edge(az_id, &tor_id, "containment"));

                for vm in 0..self.vms_per_rack {
                    let vm_id = format!("vm-{region}-{rack:02}-{vm:02}");
                    nodes.push(node_with_rack(&vm_id, &format!("VM {vm_id}"), "VirtualMachine", region, &rack_id_str));
                    edges.push(edge(&tor_id, &vm_id, "containment"));

                    for c in 0..self.containers_per_vm {
                        let ctr_id = format!("ctr-{region}-{rack:02}-{vm:02}-{c:02}");
                        nodes.push(node_with_rack(&ctr_id, &format!("Container {ctr_id}"), "Container", region, &rack_id_str));
                        edges.push(edge(&vm_id, &ctr_id, "containment"));
                    }

                    for mi in 0..self.identities_per_vm {
                        let mi_id = format!("mi-{region}-{rack:02}-{vm:02}-{mi:02}");
                        nodes.push(node_with_rack(&mi_id, &format!("MI {mi_id}"), "ManagedIdentity", region, &rack_id_str));
                        edges.push(edge(&vm_id, &mi_id, "dependency"));
                    }
                }
            }

            // Application stacks
            if self.include_app_stacks {
                // Distribute apps across platform service instances
                let dns_id = format!("dns-{region}");
                let idp_id = format!("idp-{region}");
                let ca_id = format!("ca-{region}");
                let env_id = format!("env-{region}-prod");

                for app in 0..self.apps_per_region {
                    let prefix = format!("{region}-app{app:03}");
                    // Each app uses a different KV and ACR (round-robin)
                    let kv_id = format!("kv-{region}-{:02}", (app % 3) + 1);
                    let acr_id = format!("acr-{region}-{:02}", (app % 2) + 1);
                    let mq_id = format!("mq-{region}-{:02}", (app % 2) + 1);

                    let app_id = format!("app-{prefix}");
                    nodes.push(node(&app_id, &format!("App {prefix}"), "Application", region, None));
                    // App belongs to environment
                    edges.push(edge(&env_id, &app_id, "containment"));

                    let vnet_id = format!("vnet-{prefix}");
                    nodes.push(node(&vnet_id, &format!("VNet {prefix}"), "VirtualNetwork", region, None));

                    // Subnets + NSGs
                    for sn in &["frontend", "backend"] {
                        let sn_id = format!("subnet-{prefix}-{sn}");
                        nodes.push(node(&sn_id, &format!("Subnet {sn} {prefix}"), "SubnetGateway", region, None));
                        edges.push(edge(&vnet_id, &sn_id, "containment"));

                        let nsg_id = format!("nsg-{prefix}-{sn}");
                        nodes.push(node(&nsg_id, &format!("NSG {sn} {prefix}"), "NetworkInterface", region, None));
                        edges.push(edge(&sn_id, &nsg_id, "dependency"));
                    }

                    // AKS
                    let aks_id = format!("aks-{prefix}");
                    nodes.push(node(&aks_id, &format!("AKS {prefix}"), "AKSCluster", region, None));
                    edges.push(edge(&app_id, &aks_id, "containment"));
                    let be_subnet = format!("subnet-{prefix}-backend");
                    edges.push(edge(&be_subnet, &aks_id, "containment"));

                    // Gateway
                    let appgw_id = format!("appgw-{prefix}");
                    nodes.push(node(&appgw_id, &format!("Gateway {prefix}"), "Gateway", region, None));
                    edges.push(edge(&app_id, &appgw_id, "containment"));
                    edges.push(edge(&appgw_id, &aks_id, "dependency"));
                    let fe_subnet = format!("subnet-{prefix}-frontend");
                    edges.push(edge(&fe_subnet, &appgw_id, "containment"));
                    edges.push(edge(&dns_id, &appgw_id, "dependency"));
                    edges.push(edge(&ca_id, &appgw_id, "dependency"));

                    // LB
                    let lb_id = format!("lb-{prefix}");
                    nodes.push(node(&lb_id, &format!("LB {prefix}"), "LoadBalancer", region, None));
                    edges.push(edge(&app_id, &lb_id, "containment"));
                    edges.push(edge(&lb_id, &aks_id, "dependency"));

                    // Route
                    let route_id = format!("route-{prefix}");
                    nodes.push(node(&route_id, &format!("Route {prefix}"), "HttpRoute", region, None));
                    edges.push(edge(&aks_id, &route_id, "containment"));
                    edges.push(edge(&appgw_id, &route_id, "dependency"));

                    // MI
                    let mi_id = format!("mi-{prefix}");
                    nodes.push(node(&mi_id, &format!("MI {prefix}"), "ManagedIdentity", region, None));
                    edges.push(edge(&idp_id, &mi_id, "dependency"));

                    // Data store
                    let ds_class = match app % 3 {
                        0 => "SqlDatabase",
                        1 => "RedisCache",
                        _ => "MongoDatabase",
                    };
                    let ds_label = match app % 3 { 0 => "SQL", 1 => "Redis", _ => "Mongo" };
                    let ds_id = format!("ds-{prefix}");
                    nodes.push(node(&ds_id, &format!("{ds_label} {prefix}"), ds_class, region, None));
                    edges.push(edge(&app_id, &ds_id, "containment"));

                    // Pods
                    for pod in 0..self.pods_per_app {
                        let pod_id = format!("pod-{prefix}-{pod:02}");
                        nodes.push(node(&pod_id, &format!("Pod {prefix}/{pod}"), "Container", region, None));
                        edges.push(edge(&aks_id, &pod_id, "containment"));
                        edges.push(edge(&mi_id, &pod_id, "dependency"));
                        edges.push(edge(&kv_id, &pod_id, "dependency"));
                        edges.push(edge(&acr_id, &pod_id, "dependency"));
                        edges.push(edge(&ds_id, &pod_id, "connection"));
                        // Every 3rd app also depends on a shared message queue
                        if app % 3 == 0 {
                            edges.push(edge(&mq_id, &pod_id, "connection"));
                        }
                    }
                }
            }
        }

        GraphPayload { nodes, edges }
    }
}

fn node(id: &str, label: &str, class: &str, region: &str, rack_id: Option<&str>) -> CausalNode {
    CausalNode {
        id: id.to_string(),
        label: label.to_string(),
        class: class.to_string(),
        region: Some(region.to_string()),
        rack_id: rack_id.map(|s| s.to_string()),
        properties: serde_json::json!({}),
    }
}

fn node_with_rack(id: &str, label: &str, class: &str, region: &str, rack: &str) -> CausalNode {
    node(id, label, class, region, Some(rack))
}

fn edge(source: &str, target: &str, edge_type: &str) -> BlueprintEdge {
    BlueprintEdge {
        id: format!("edge-{source}-{target}"),
        source_id: source.to_string(),
        target_id: target.to_string(),
        edge_type: edge_type.to_string(),
        properties: serde_json::json!({}),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_minimal_topology() {
        let g = TopologyBuilder::minimal().build();
        // 1 region: 1 PD + 1 AZ + 2 ToR + 6 VM + 12 container + 6 MI = 28 nodes
        assert!(g.nodes.len() > 20, "minimal should have >20 nodes, got {}", g.nodes.len());
        assert!(g.edges.len() > 15, "minimal should have >15 edges, got {}", g.edges.len());
    }

    #[test]
    fn test_standard_topology() {
        let g = TopologyBuilder::standard().build();
        assert!(g.nodes.len() > 20000, "standard should have >20k nodes, got {}", g.nodes.len());
        assert!(g.edges.len() > 40000, "standard should have >40k edges, got {}", g.edges.len());
    }

    #[test]
    fn test_custom_scale() {
        let g = TopologyBuilder::new()
            .regions(2)
            .racks_per_region(1)
            .vms_per_rack(1)
            .containers_per_vm(1)
            .identities_per_vm(0)
            .apps_per_region(0)
            .include_app_stacks(false)
            .build();
        // 2 regions × (PD + AZ + platform_services + 1 ToR + 1 VM + 1 Container) 
        assert!(g.nodes.len() < 50, "small custom should be <50 nodes, got {}", g.nodes.len());
    }

    #[test]
    fn test_large_topology() {
        let g = TopologyBuilder::large().build();
        assert!(g.nodes.len() > 40000, "large should have >40k nodes, got {}", g.nodes.len());
    }

    #[test]
    fn test_node_ids_unique() {
        let g = TopologyBuilder::minimal().build();
        let mut ids: Vec<&str> = g.nodes.iter().map(|n| n.id.as_str()).collect();
        ids.sort();
        ids.dedup();
        assert_eq!(ids.len(), g.nodes.len(), "node IDs must be unique");
    }
}
