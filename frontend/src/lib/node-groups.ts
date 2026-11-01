/* Settings → Nodes, grouped the way the dashboard frames them (roadmap CT).
 *
 * DISPLAY ONLY. The draft keeps cluster.yml's order and is saved in it, since
 * a node's colour is its line in that file (`nodeSlots`). Each member carries
 * its index in the draft, which is both its colour slot and what an edit or a
 * removal addresses.
 *
 * A STANDALONE NODE IS A GROUP OF ONE, never pooled with the other standalone
 * nodes into one frame: a shared frame would read as grouped, which is the
 * rule the dashboard's compact view keeps too.
 */

export interface NodeGroup<T> {
  /** The cluster name, or `solo:<index>` for a standalone node. */
  key: string;
  /** Null for a standalone node. */
  cluster: string | null;
  members: { node: T; index: number }[];
}

/** Groups in the order each first appears in the file; members in file order. */
export function groupNodes<T extends { cluster: string | null }>(nodes: T[]): NodeGroup<T>[] {
  const groups: NodeGroup<T>[] = [];
  const byName = new Map<string, NodeGroup<T>>();
  nodes.forEach((node, index) => {
    const name = node.cluster?.trim() || null;
    if (name === null) {
      groups.push({ key: `solo:${index}`, cluster: null, members: [{ node, index }] });
      return;
    }
    let group = byName.get(name);
    if (!group) {
      group = { key: name, cluster: name, members: [] };
      byName.set(name, group);
      groups.push(group);
    }
    group.members.push({ node, index });
  });
  return groups;
}

/** Cluster names, sorted, for the cluster field's suggestions. */
export function clusterNames(nodes: { cluster: string | null }[]): string[] {
  return [...new Set(nodes.map((n) => n.cluster?.trim()).filter((c): c is string => !!c))].sort();
}
