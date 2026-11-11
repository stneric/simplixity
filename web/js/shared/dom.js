// Building DOM nodes, the one way every module here does it.
//
// Nothing in this app assigns innerHTML with anything that could have come
// from a model or a page it read. Nodes are made with createElement and filled
// with textContent, so text is always text and there is nothing to sanitise.

/**
 * A new element with a class and a text, either of which may be left out.
 *
 * @param {string} tag
 * @param {string} [className]
 * @param {string|number|null} [text] set as textContent, never parsed as markup
 */
export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
