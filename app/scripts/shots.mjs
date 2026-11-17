// Screenshots of the running app for `dev.py shots` (app/scripts/dev.py; adapted from another project's script): each route, daylight or night (SHOTS_SCHEME), at
// 1400px and 375px wide. `dev.py shots` copies this file next to its Playwright install and runs it with Node on this computer.
//
// Navigate only: nothing here clicks anything.
//
// Each picture is also compared with the one of the same route and width from the last release: SHOTS_LAST names the
// folder that holds those (one folder per scheme inside it), and the line for the picture says "unchanged" or how much of
// it differs. With SHOTS_UPDATE_LAST=1 the new picture then replaces the old one there, so the next run compares with
// this one. `dev.py shots` sets both: only a run against the running local instance updates it, since that is the last release.
//
// Usage: node shots.mjs <base-url> <out-dir> <route>...
import { copyFileSync, existsSync, mkdirSync, readFileSync } from "node:fs";
import pixelmatch from "pixelmatch";
import pngjs from "pngjs";
import { chromium } from "playwright";

const { PNG } = pngjs;

const [base, out, ...routes] = process.argv.slice(2);
const slug = (route) => route.replace(/[^a-z0-9]+/gi, "-").replace(/^-+|-+$/g, "") || "home";

// SHOTS_SCHEME picks daylight or night; daylight is the app's default.
const SCHEME = process.env.SHOTS_SCHEME === "dark" ? "dark" : "light";

// The earlier pictures: SHOTS_LAST/<scheme>/<file>. Off for a first-screen run, whose pictures are shaped differently.
const LAST = process.env.SHOTS_LAST && process.env.SHOTS_FIRST_SCREEN !== "1" ? `${process.env.SHOTS_LAST}/${SCHEME}` : "";
const UPDATE_LAST = process.env.SHOTS_UPDATE_LAST === "1";

// The top-left w x h corner of a picture, so two pictures of different heights can still be compared where they overlap.
function corner(png, w, h) {
  const rows = [];
  for (let y = 0; y < h; y++) rows.push(png.data.subarray(y * png.width * 4, y * png.width * 4 + w * 4));
  return Buffer.concat(rows);
}

// What to say about a picture next to the one from the last release; null when there is nothing to compare with.
function againstLast(file) {
  if (!LAST) return null;
  const before = `${LAST}/${file}`;
  const now = `${out}/${file}`;
  let note = "no picture from the last release to compare with";
  if (existsSync(before)) {
    const a = PNG.sync.read(readFileSync(before));
    const b = PNG.sync.read(readFileSync(now));
    const w = Math.min(a.width, b.width);
    const h = Math.min(a.height, b.height);
    const differ = pixelmatch(corner(a, w, h), corner(b, w, h), null, w, h, { threshold: 0.1 });
    const sized = a.width === b.width && a.height === b.height;
    if (sized && differ === 0) note = "unchanged since the last release";
    else {
      const share = (100 * differ) / (w * h);
      note = `${share < 1 ? share.toFixed(2) : share.toFixed(1)}% of it differs from the last release`;
      if (!sized) note += `; it was ${a.width}x${a.height}px, now ${b.width}x${b.height}px`;
    }
  }
  if (UPDATE_LAST) {
    mkdirSync(LAST, { recursive: true });
    copyFileSync(now, before);
  }
  return note;
}

// Headless Chromium hides scrollbars; show them, since the app themes them.
const browser = await chromium.launch({ ignoreDefaultArgs: ["--hide-scrollbars"] });
for (const width of [1400, 375]) {
  const ctx = await browser.newContext({ viewport: { width, height: 1000 }, colorScheme: SCHEME });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });

  await page.goto(base, { waitUntil: "networkidle" });
  for (const route of routes) {
    errors.length = 0;
    // A route is only the part after the address, so the page is loaded afresh for each one.
    await page.goto(`${base}/${route}`, { waitUntil: "networkidle" });
    await page.reload({ waitUntil: "networkidle" });
    // A phone has no classic scrollbar. A 15px one would take the 375px layout down to 360px and cut off the right edge of
    // the picture, so the phone width is captured without it. The desktop width keeps its scrollbar.
    if (width < 768) await page.addStyleTag({ content: "html { scrollbar-width: none !important; } ::-webkit-scrollbar { display: none !important; }" });
    await page.waitForTimeout(800);
    const file = `${slug(route)}-${width}.png`;
    // SHOTS_FIRST_SCREEN=1 takes only the first screen, for reading a long page's type at full size.
    await page.screenshot({ path: `${out}/${file}`, fullPage: process.env.SHOTS_FIRST_SCREEN !== "1" });
    // Wider than the window, whether the page scrolls sideways or something past the edge is quietly cut off: the page's own
    // width (the root's and the body's), and any element whose right edge is past the window that is not inside a box
    // meant to scroll sideways. (A page that hides its overflow looks fine to the first check and is cut off in the picture.)
    const { sw, cw, poking } = await page.evaluate(() => {
      const cw = document.documentElement.clientWidth;
      const scrolls = (el) => {
        for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
          if (/(auto|scroll)/.test(getComputedStyle(p).overflowX)) return true;
        }
        return false;
      };
      // The element is, or is inside, a visually-hidden box (base.css and the phone table's header group in where.css: 1px by
      // 1px, absolute, clipped). That is read by screen readers and never drawn. What is in it runs on past the box by design
      // (a line of text, nowrap so a screen reader keeps the words apart; a table's header cells), and is clipped, so none of it
      // is cut off at the edge.
      const hidden = (el) => {
        for (let p = el; p && p !== document.body; p = p.parentElement) {
          const b = p.getBoundingClientRect();
          if (b.width > 1 || b.height > 1) continue;
          const s = getComputedStyle(p);
          if (s.position === "absolute" && s.clip !== "auto") return true;
        }
        return false;
      };
      const poking = [];
      for (const el of document.body.querySelectorAll("*")) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        const style = getComputedStyle(el);
        // Its box, or its content spilling out of the box (a long word or a big heading in a box that cuts it off).
        const spills = el.clientWidth > 0 && el.scrollWidth > el.clientWidth + 1 && !/(auto|scroll)/.test(style.overflowX)
          && style.textOverflow !== "ellipsis";
        if (Math.max(r.right, spills ? r.left + el.scrollWidth : 0) <= cw + 1) continue;
        if (style.position === "fixed" || scrolls(el) || hidden(el)) continue;
        poking.push(`${el.tagName.toLowerCase()}${el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/)[0] : ""}`);
      }
      return { sw: Math.max(document.documentElement.scrollWidth, document.body.scrollWidth), cw, poking };
    });
    const notes = [];
    if (sw > cw) notes.push(`SIDEWAYS SCROLL (${sw}px wide in a ${cw}px window)`);
    else if (poking.length) notes.push(`CUT OFF AT THE EDGE (${poking.length} element(s) reach past the ${cw}px window: ${[...new Set(poking)].slice(0, 3).join(", ")})`);
    if (errors.length) notes.push(`${errors.length} console error(s): ${errors[0].slice(0, 160)}`);
    const last = againstLast(file);
    console.log(`${file}  ${notes.length ? notes.join("; ") : "ok"}${last ? `; ${last}` : ""}`);
  }
  await ctx.close();
}
await browser.close();
