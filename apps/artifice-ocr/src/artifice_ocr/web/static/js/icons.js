// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/*
 * Shared line-drawn SVG icons (Design_Philosophy.md §8.8: 24x24 viewBox,
 * stroke=currentColor, stroke-width 2, round caps/joins, aria-hidden="true").
 * Loaded before any script that references `Icons.*` so icon-duty glyphs
 * (tab markers, action icons, transport controls) never fall back to an
 * emoji rendered in the user's system font — the one glyph in the interface
 * that can never be made to match the typography, and that renders
 * differently on Windows and macOS.
 *
 * Each entry is a ready-to-insert SVG string (not a DOM node), matching how
 * palette.js and app.js already build markup via template strings.
 */
const Icons = (function () {
  function svg(inner, size) {
    size = size || 16;
    return '<svg viewBox="0 0 24 24" width="' + size + '" height="' + size +
      '" fill="none" stroke="currentColor" stroke-width="2" ' +
      'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      inner + "</svg>";
  }

  return {
    chevron: svg('<polyline points="9 6 15 12 9 18"></polyline>'),
    folder: svg('<path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"></path>'),
    folderPlus: svg('<path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"></path>' +
      '<line x1="12" y1="10" x2="12" y2="14"></line><line x1="10" y1="12" x2="14" y2="12"></line>'),
    file: svg('<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline>'),
    fileText: svg('<path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"></path>' +
      '<polyline points="14 2 14 8 20 8"></polyline><line x1="8" y1="13" x2="16" y2="13"></line><line x1="8" y1="17" x2="12" y2="17"></line>'),
    trash: svg('<polyline points="3 6 5 6 21 6"></polyline>' +
      '<path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"></path>' +
      '<line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line>'),
    skipForward: svg('<polygon points="5 4 15 12 5 20 5 4"></polygon><line x1="19" y1="5" x2="19" y2="19"></line>'),
    refreshCw: svg('<polyline points="23 4 23 10 17 10"></polyline><path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"></path>'),
    refreshCcw: svg('<polyline points="1 4 1 10 7 10"></polyline><path d="M3.51 15a9 9 0 102.13-9.36L1 10"></path>'),
    play: svg('<polygon points="5 3 19 12 5 21 5 3"></polygon>'),
    pause: svg('<line x1="8" y1="5" x2="8" y2="19"></line><line x1="16" y1="5" x2="16" y2="19"></line>'),
    stop: svg('<rect x="5" y="5" width="14" height="14" rx="1"></rect>'),
    save: svg('<path d="M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z"></path>' +
      '<polyline points="17 21 17 13 7 13 7 21"></polyline><polyline points="7 3 7 8 15 8"></polyline>'),
    link: svg('<path d="M10 14a5 5 0 007 0l3-3a5 5 0 00-7-7l-1 1"></path><path d="M14 10a5 5 0 00-7 0l-3 3a5 5 0 007 7l1-1"></path>'),
    moon: svg('<path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"></path>'),
    clipboard: svg('<path d="M9 2h6a1 1 0 011 1v1h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2V3a1 1 0 011-1z"></path>' +
      '<rect x="8" y="1" width="8" height="4" rx="1"></rect>'),
    menu: svg('<line x1="3" y1="6" x2="21" y2="6"></line><line x1="3" y1="12" x2="21" y2="12"></line><line x1="3" y1="18" x2="21" y2="18"></line>'),
    eye: svg('<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle>'),
    clock: svg('<circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline>'),
    barChart: svg('<line x1="18" y1="20" x2="18" y2="10"></line><line x1="12" y1="20" x2="12" y2="4"></line><line x1="6" y1="20" x2="6" y2="14"></line>'),
    sliders: svg('<line x1="4" y1="6" x2="20" y2="6"></line><circle cx="9" cy="6" r="1.6"></circle>' +
      '<line x1="4" y1="12" x2="20" y2="12"></line><circle cx="15" cy="12" r="1.6"></circle>' +
      '<line x1="4" y1="18" x2="20" y2="18"></line><circle cx="7" cy="18" r="1.6"></circle>'),
  };
})();

window.Icons = Icons;
