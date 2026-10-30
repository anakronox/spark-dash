/** The page title's rules, kept out of the store so they run under node. */

export const TITLE_KEY = 'spark-dash.page-title.v1';
export const DEFAULT_TITLE = 'spark-dash';
export const MAX_TITLE = 40;

/** What is kept: single-line and capped, SPACES INCLUDED (CF). Leading and
 *  repeated spaces were trimmed and collapsed, so a title could not be
 *  indented or spaced out. Only whitespace means the default. */
export function cleanTitle(raw: string): string {
  const one = raw.replace(/[\r\n\t]/g, ' ').slice(0, MAX_TITLE);
  return one.trim() ? one : '';
}
