/** The page's name (roadmap BL): spark-dash unless the reader names it, in
 *  this browser, like the theme. Drawn in the header and the browser tab. */

export const TITLE_KEY = 'spark-dash.page-title.v1';
export const DEFAULT_TITLE = 'spark-dash';
export const MAX_TITLE = 40;

/** What is kept: trimmed, single-line, capped. Empty means the default. */
export function cleanTitle(raw: string): string {
  return raw.replace(/\s+/g, ' ').trim().slice(0, MAX_TITLE).trim();
}

function read(): string {
  try {
    return cleanTitle(localStorage.getItem(TITLE_KEY) ?? '');
  } catch {
    return '';
  }
}

class PageTitle {
  /** The reader's name for the page, or '' for the default. */
  custom = $state(read());

  get text(): string {
    return this.custom || DEFAULT_TITLE;
  }

  set(raw: string) {
    this.custom = cleanTitle(raw);
    try {
      if (this.custom) localStorage.setItem(TITLE_KEY, this.custom);
      else localStorage.removeItem(TITLE_KEY);
    } catch {
      // Private window: kept until reload.
    }
  }
}

export const pageTitle = new PageTitle();
