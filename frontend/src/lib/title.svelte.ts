/** The page's name (roadmap BL): spark-dash unless the reader names it, in
 *  this browser, like the theme. Drawn in the header and the browser tab. */

import { cleanTitle, DEFAULT_TITLE, TITLE_KEY } from './title';

export { cleanTitle, DEFAULT_TITLE, MAX_TITLE, TITLE_KEY } from './title';

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
