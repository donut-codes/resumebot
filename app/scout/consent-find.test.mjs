// The cookie-banner finder (app/backend/app/jobsearch/browser/recipes/consent_find.js): the script the recording's Chrome
// runs in a page to list a banner's controls. It is run here, from that file by its relative path, against a small
// stand-in page: elements that answer the few things it asks (a selector, `matches`, a box) and count every read of
// `innerText`. No Chrome, no network. Run: node --test (from app/scout).
// These cases lived in app/backend/tests/jobsearch/test_recipe_interact.py (run there under Node by a subprocess) until
// 2026-11-04: moved here, not removed, so the backend suite starts no Node.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const SCRIPT = readFileSync(
  fileURLToPath(new URL("../backend/app/jobsearch/browser/recipes/consent_find.js", import.meta.url)), "utf-8");

// A page is a list of elements, each [tag, attributes, own text, children]. `found(spec)` runs the finder in a page whose
// body holds `spec` and answers the labels it returned, the controls it marked (number, text) and the reads of `innerText`.
const found = (spec) => {
  let reads = 0;
  const parse = (sel) => sel.split(",").map((part) => {
    const m = part.trim().match(/^([a-z]*)((?:\[[^\]]+\])*)$/);
    return { tag: m[1], attrs: [...m[2].matchAll(/\[([a-z-]+)(?:=([^\]]*))?\]/g)].map((a) => [a[1], a[2]]) };
  });
  const hit = (el, sel) => parse(sel).some(({ tag, attrs }) => (!tag || el.tag === tag) &&
    attrs.every(([n, v]) => n in el.attrs && (v === undefined || String(el.attrs[n]) === v)));
  const all = (el) => el.children.flatMap((c) => [c, ...all(c)]);
  const textOf = (el) => [el.own, ...el.children.map(textOf)].join(" ").trim();
  const make = ([tag, attrs, own, kids], parent) => {
    const el = {
      tag, tagName: tag.toUpperCase(), attrs: { ...attrs }, own, parentElement: parent, children: [],
      get id() { return this.attrs.id || ""; }, get className() { return this.attrs.class || ""; },
      get disabled() { return false; }, get value() { return this.attrs.value; },
      get innerText() { reads += 1; return textOf(this); },
      getAttribute(n) { return n in this.attrs ? String(this.attrs[n]) : null; },
      setAttribute(n, v) { this.attrs[n] = String(v); }, removeAttribute(n) { delete this.attrs[n]; },
      matches(sel) { return hit(this, sel); }, querySelectorAll(sel) { return all(this).filter((e) => hit(e, sel)); },
      getBoundingClientRect() { return { width: "data-hidden" in this.attrs ? 0 : 10, height: 10 }; },
    };
    el.children = kids.map((k) => make(k, el));
    return el;
  };
  const html = make(["html", {}, "", [["body", {}, "", spec]]], null);
  const document = {
    body: html.children[0], documentElement: html, querySelectorAll: (sel) => all(html).filter((e) => hit(e, sel)),
  };
  const getComputedStyle = () => ({ visibility: "visible", display: "block" });
  const labels = JSON.parse(vm.runInNewContext(SCRIPT, { document, getComputedStyle }));
  const marked = all(html).filter((e) => "data-rb-consent" in e.attrs).map((e) => [e.attrs["data-rb-consent"], e.own]);
  return { labels, marked, reads };
};

const banner = () => ["div", { id: "onetrust-consent-sdk" }, "", [
  ["p", {}, "We use cookies and similar tracking.", []], ["button", {}, "Manage preferences", []],
  ["button", { "data-hidden": "1" }, "Hidden control", []], ["button", {}, "Reject all", []],
  ["a", { href: "#" }, "Accept all", []], ["a", { href: "/privacy" }, "Privacy policy", []]]];

// A listing page of `links` job links (over 300 of them, the page's text is longer than any banner's), all under a wrapper
// that carries a banner's class: the page's own `app` element, which is no banner.
const resultsPage = (links) => {
  const rows = Array.from({ length: links }, (_, n) =>
    ["li", {}, "", [["a", { href: `/job/${n}` }, `Example Role number ${n} in the region`, []]]]);
  const page = [["header", {}, "", [["nav", {}, "", [["a", { href: "/" }, "Home", []]]]]],
    ["main", {}, "", [["ul", {}, "", rows]]], banner(), ["footer", {}, "", [["button", {}, "Reject all", []]]]];
  return [["div", { id: "app", class: "has-cookie-consent" }, "", page]];
};

describe("the banner finder", () => {
  it("marks and labels only the shown controls of a banner, in page order", () => {
    const got = found(resultsPage(300));
    assert.deepEqual(got.labels, ["Manage preferences", "Reject all", "Accept all"]);
    assert.deepEqual(got.marked, [["0", "Manage preferences"], ["1", "Reject all"], ["2", "Accept all"]]);
  });

  it("takes a dialog about cookies and leaves a dialog about anything else", () => {
    const cookies = ["div", { role: "dialog" }, "", [["p", {}, "This site uses cookies", []], ["button", {}, "Decline", []]]];
    const news = ["div", { role: "dialog" }, "", [["p", {}, "Join the newsletter", []], ["button", {}, "No thanks", []]]];
    assert.deepEqual(found([cookies, news]).labels, ["Decline"]);
    assert.deepEqual(found([news, ["button", {}, "Reject all", []]]).labels, []);
  });

  // Found in review, 2026-11-04: it read the text of every ancestor of every button and link before it looked at a name,
  // so a results page of hundreds of links forced layout thousands of times, once per page the recipe loaded.
  it("reads the text of the banners only, however many controls the page has", () => {
    const small = found(resultsPage(300));
    const large = found(resultsPage(900));
    assert.deepEqual(small.labels, ["Manage preferences", "Reject all", "Accept all"]);
    assert.deepEqual(large.labels, small.labels);
    assert.equal(large.reads, small.reads);
    assert.ok(large.reads <= 8, `${small.reads} reads on 300 links, ${large.reads} on 900`);
  });

  it("reads no text at all on a page with no banner to find none", () => {
    const got = found([["main", {}, "", Array.from({ length: 50 }, (_, n) => ["a", { href: `/job/${n}` }, `Role ${n}`, []])]]);
    assert.deepEqual(got.labels, []);
    assert.equal(got.reads, 0);
  });
});
