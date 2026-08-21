/** Which node the page is scoped to, and whether idle models are hidden.
 *
 * ONE INTERACTION, NOT A WIDGET PER TABLE. The high-value filter is by node,
 * and the page already has a natural control for it: the node cards. Clicking
 * one to scope every table beats four separate filter boxes, and it is what
 * people already expect from a card that represents a thing.
 *
 * DELIBERATELY NOT PERSISTED. A filter is a thing you are doing right now, not
 * a preference — coming back tomorrow to a dashboard silently showing one node
 * of three is the same failure as a silence that outlives the memory of
 * setting it. Section order and theme persist because they are how you like
 * the page; this is where you happen to be looking.
 */

const ALL = null;

class PageFocus {
  /** Node id the page is scoped to, or null for the whole cluster. */
  node = $state<string | null>(ALL);

  /** Hide models that hold no weights. Measured at four nodes: 32 of 36 rows
   *  were `unloaded`, which is what you scroll past to find the four doing
   *  anything. A toggle rather than a general predicate, because "is it
   *  actually loaded" is the only row filter anyone has wanted. */
  hideIdleModels = $state(false);

  get scoped(): boolean {
    return this.node !== ALL;
  }

  /** Clicking the focused node again clears it — the control that sets a
   *  filter has to be able to unset it, or the only way back is a reload. */
  toggle(nodeId: string): void {
    this.node = this.node === nodeId ? ALL : nodeId;
  }

  clear(): void {
    this.node = ALL;
  }

  /** True when a row belonging to `nodeId` should be shown. Every table asks
   *  this rather than reimplementing the comparison, so "scoped to nothing"
   *  cannot mean different things in different tables. */
  includes(nodeId: string): boolean {
    return this.node === ALL || this.node === nodeId;
  }
}

export const pageFocus = new PageFocus();
