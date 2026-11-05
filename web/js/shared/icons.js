// Inline Lucide-style icons. Vendored as paths rather than pulled from a CDN so
// the CSP stays tight and the app works offline. Same rule as markdown-it.
// 24x24 grid, 1.75 stroke, currentColor.

const PATHS = {
  plus: 'M12 5v14M5 12h14',
  search: 'M11 3a8 8 0 1 0 0 16 8 8 0 0 0 0-16zM21 21l-4.35-4.35',
  sidebar: 'M3 5.5A2.5 2.5 0 0 1 5.5 3h13A2.5 2.5 0 0 1 21 5.5v13a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 18.5zM9 3v18',
  user: 'M12 4a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM4 21a8 8 0 0 1 16 0',
  lock: 'M6 11h12v10H6zM8.5 11V7.5a3.5 3.5 0 0 1 7 0V11',
  settings: 'M4 6h10M18 6h2M4 12h2M10 12h10M4 18h6M14 18h6M16 4v4M8 10v4M12 16v4',
  send: 'M12 19V5M5 12l7-7 7 7',
  stop: 'M7 7h10v10H7z',
  chevron: 'M6 9l6 6 6-6',
  check: 'M20 6L9 17l-5-5',
  clock: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM12 7.5V12l3 2',
  close: 'M18 6L6 18M6 6l12 12',
  refresh: 'M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6',
  globe: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM3 12h18M12 3a15 15 0 0 1 0 18M12 3a15 15 0 0 0 0 18',
  layers: 'M12 3l9 5-9 5-9-5 9-5zM3 14l9 5 9-5',
  trash: 'M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 12a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-12',
  spark: 'M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z',
  copy: 'M9 9h10v10H9zM5 15V5h10',
  link: 'M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7l-1 1M14 11a5 5 0 0 0-7 0l-2 2a5 5 0 0 0 7 7l1-1',
  alert: 'M12 9v4M12 17h.01M10.3 4.3L2.8 17.3a2 2 0 0 0 1.7 3h15a2 2 0 0 0 1.7-3L13.7 4.3a2 2 0 0 0-3.4 0z',
  message: 'M21 12a8 8 0 0 1-8 8H7l-4 3v-5.5A8 8 0 0 1 11 4h2a8 8 0 0 1 8 8z',
  gauge: 'M12 21a9 9 0 1 1 9-9M12 12l5-3',
  zap: 'M13 3L5 14h6l-1 7 8-11h-6z',
  telescope: 'M4 15l7-3M11 12l7-4M6 20l3-6M15 18l-3-7M9 21h2',
  // Tools: the sidebar entry, and the redact tool (a struck-through eye), and
  // opening a file.
  wrench: 'M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.8-3.8a6 6 0 0 1-7.9 7.9l-6.9 6.9a2.1 2.1 0 0 1-3-3l6.9-6.9a6 6 0 0 1 7.9-7.9z',
  redact: 'M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4.1M6.6 6.6C3.9 8.4 2 12 2 12s3.6 7 10 7a9.7 9.7 0 0 0 5.4-1.6M9.9 9.9a3 3 0 0 0 4.2 4.2',
  undo: 'M9 14L4 9l5-5M4 9h11a5 5 0 0 1 0 10h-3',
  redo: 'M15 14l5-5-5-5M20 9H9a5 5 0 0 0 0 10h3',
  upload: 'M12 15V3M7 8l5-5 5 5M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4',
  // Attaching a file to a chat, and the file itself on its chip.
  paperclip: 'M21 11l-8.5 8.5a5.5 5.5 0 0 1-7.8-7.8l9-9a3.7 3.7 0 0 1 5.2 5.2l-9 9a1.8 1.8 0 0 1-2.6-2.6L15.5 6',
  // The model picker's tabs: favourites (filled by CSS when active or starred),
  // free, local, and everything.
  star: 'M12 3.5l2.6 5.3 5.9.9-4.3 4.1 1 5.8L12 16.9l-5.2 2.7 1-5.8-4.3-4.1 5.9-.9z',
  tag: 'M3 12V4a1 1 0 0 1 1-1h8l9 9-9 9zM7.5 7.5h.01',
  laptop: 'M5 5h14v10H5zM2 19h20',
  // A picture attached to a chat, and the drop overlay's mark for one.
  image: 'M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM21 15l-5-5L5 21M8.5 10a1.5 1.5 0 1 0 0-3 1.5 1.5 0 0 0 0 3z',
  file: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5M9 13h6M9 17h6',
  // The literature page: back to the chat, the .bib export, and "maybe".
  back: 'M19 12H5M11 18l-6-6 6-6',
  download: 'M12 3v12M7 10l5 5 5-5M4 15v4a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4',
  help: 'M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM9.6 9.2a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.2-2.4 3.6M12 17h.01',
  // Source lenses. One shape per corpus, so the composer chip says where a run
  // will look without spelling it out.
  flask: 'M9 3h6M10 3v5.5L5.2 18A2 2 0 0 0 7 21h10a2 2 0 0 0 1.8-3L14 8.5V3M7.5 14h9',
  newspaper: 'M4 6a1 1 0 0 1 1-1h12v14H5a1 1 0 0 1-1-1zM17 9h3v8a2 2 0 0 1-2 2M7 9h7M7 13h7M7 17h4',
  code: 'M9 18l-6-6 6-6M15 6l6 6-6 6',
  book: 'M4 5a2 2 0 0 1 2-2h14v18H6a2 2 0 0 1-2-2zM20 17H6',
  filter: 'M3 5h18l-7 8.2V20l-4-2.2v-4.6z',
  // Projects: the folder, and the move-into-one action.
  folder: 'M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
  'folder-in': 'M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V18a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2zM12 10v6M9.5 13.5L12 16l2.5-2.5',

  // The source-lens menu.
  library: 'M4 4h4v16H4zM10 4h4v16h-4zM17 4.5l3.5 1-4 15-3.4-1z',
  // The verification block's clean verdict.
  shield: 'M12 3l8 3v6c0 4.5-3.2 8.3-8 9-4.8-.7-8-4.5-8-9V6l8-3zM9 12l2 2 4-4',

  // Weather. One per value of the closed condition vocabulary in
  // agent/widgets.py. A condition the model improvises maps to nothing and
  // renders no icon, rather than the wrong one.
  clear: 'M12 4V2M12 22v-2M4 12H2M22 12h-2M5.6 5.6L4.2 4.2M19.8 19.8l-1.4-1.4M18.4 5.6l1.4-1.4M4.2 19.8l1.4-1.4M12 7.5a4.5 4.5 0 1 0 0 9 4.5 4.5 0 0 0 0-9z',
  // The model's thoughts: a cloud, the shape thinking is drawn in.
  thought: 'M7 18h10a4 4 0 0 0 .4-8 6 6 0 0 0-11.4 1.6A3.5 3.5 0 0 0 7 18z',
  cloudy: 'M7 18h10a4 4 0 0 0 .4-8 6 6 0 0 0-11.4 1.6A3.5 3.5 0 0 0 7 18z',
  'partly-cloudy': 'M8 6.5a3.5 3.5 0 0 1 6.6 1.2M5 3.6V2M2.5 6H1M8.5 3l-1-1M2.6 10l-1 1M9 19h8a3.5 3.5 0 0 0 .3-7 5 5 0 0 0-9.6 1.3A3 3 0 0 0 9 19z',
  fog: 'M6 12h10a4 4 0 0 0 .4-8A6 6 0 0 0 5 5.6 3.5 3.5 0 0 0 6 12zM4 16h16M6 20h12',
  drizzle: 'M7 15h10a4 4 0 0 0 .4-8A6 6 0 0 0 6 8.6 3.5 3.5 0 0 0 7 15zM9 19v1M13 19v1.5M17 19v1',
  rain: 'M7 15h10a4 4 0 0 0 .4-8A6 6 0 0 0 6 8.6 3.5 3.5 0 0 0 7 15zM8.5 18.5L7.5 21M12.5 18.5L11.5 21M16.5 18.5L15.5 21',
  showers: 'M7 14h10a4 4 0 0 0 .4-8A6 6 0 0 0 6 7.6 3.5 3.5 0 0 0 7 14zM8 17l-1.5 4M12 17l-1.5 4M16 17l-1.5 4',
  snow: 'M7 15h10a4 4 0 0 0 .4-8A6 6 0 0 0 6 8.6 3.5 3.5 0 0 0 7 15zM9 19h.01M13 19h.01M17 19h.01M11 21.5h.01M15 21.5h.01',
  sleet: 'M7 15h10a4 4 0 0 0 .4-8A6 6 0 0 0 6 8.6 3.5 3.5 0 0 0 7 15zM8.5 18.5L7.5 21M13 19h.01M16.5 18.5L15.5 21',
  thunderstorm: 'M7 14h10a4 4 0 0 0 .4-8A6 6 0 0 0 6 7.6 3.5 3.5 0 0 0 7 14zM13 16l-3 4h4l-3 4',
  wind: 'M3 8h10a3 3 0 1 0-3-3M3 13h14a3 3 0 1 1-3 3M3 18h7a2.5 2.5 0 1 1-2.5 2.5',
};

// Icons that are more than one path, because a part of them moves. The eye's
// pupil is its own node so CSS can walk it left and right while the citation
// check runs. That step is the app *reading* rather than fetching, and a
// spinner says nothing about which of the two is happening.
//
// `redacting` is the redact tool's working state: a page whose lines are struck
// through one after another, then lifted away for the next page. Each bar is its
// own node with its own keyframes (web/styles.css, `.rx-*`), timed so they all
// reset while the page is out of sight, which is what makes the loop seamless.
const SHAPES = {
  redacting: `<g class="rx-page">
        <path d="M6 3h8.5L18 6.5V21H6z" stroke-width="1.2"/><path d="M14.5 3v3.5H18" stroke-width="1.2"/>
        <path d="M8.5 9.5h7M8.5 12.5h7M8.5 15.5h7M8.5 18.5h5" stroke-width="1" opacity="0.4"/>
        <rect class="rx-bar rx-bar-0" x="10.5" y="8.6" width="5" height="1.8" rx="0.4" fill="currentColor" stroke="none"/>
        <rect class="rx-bar rx-bar-1" x="8.5" y="11.6" width="4.5" height="1.8" rx="0.4" fill="currentColor" stroke="none"/>
        <rect class="rx-bar rx-bar-2" x="11" y="14.6" width="4.5" height="1.8" rx="0.4" fill="currentColor" stroke="none"/>
        <rect class="rx-bar rx-bar-3" x="8.5" y="17.6" width="3.5" height="1.8" rx="0.4" fill="currentColor" stroke="none"/>
      </g>`,
  eye: `<path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z"/>
        <circle class="eye-pupil" cx="12" cy="12" r="2.7" fill="currentColor" stroke="none"/>`,
};

export function icon(name, { size = 18, className = '' } = {}) {
  const body = SHAPES[name] ?? (PATHS[name] ? `<path d="${PATHS[name]}"/>` : '');
  if (!body) return '';
  return `<svg class="icon ${className}" width="${size}" height="${size}" viewBox="0 0 24 24"
    fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"
    stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

/** An icon as a node. The markup is a vendored constant, never model output,
 *  and wrapping it here keeps every other module free of innerHTML. */
export function iconNode(name, size = 16, className = 'icon-slot') {
  const holder = document.createElement('span');
  holder.className = className;
  holder.innerHTML = icon(name, { size });
  return holder;
}

/** Replaces every <i data-icon="name"> in a tree with its SVG. */
export function hydrate(root = document) {
  root.querySelectorAll('[data-icon]').forEach((el) => {
    const size = Number(el.dataset.size) || 18;
    el.outerHTML = icon(el.dataset.icon, { size, className: el.className });
  });
}
