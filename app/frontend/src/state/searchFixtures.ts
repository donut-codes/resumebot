// Made-up search settings for the store's and the screen's tests: one target role, one hard pass sentence, one ask and
// one site. Nobody's real data.

import type { Profile } from "../api/types/profile";
import type { Site } from "../api/types/where";

export function profileOf(extra: Partial<Profile> = {}): Profile {
  return {
    criteria: { company: "A made-up kind of employer.", target_roles: [{ id: "builders", name: "Builders", titles: ["Builder"], description: "", responsibilities: "", qualifications: "", preferred: "", compensation: "" }] },
    gates: {
      sections_off: [],
      location: { remote: { on: false, locations: [] }, hybrid: { on: false, locations: [] }, in_person: { on: false, locations: [] } },
      compensation: { amount: null, against: "maximum", wide_band_ratio: 1.5 },
      other: [{ id: "travel", kind: "rule", section: "responsibilities", text: "Travel is under a quarter of the time.", roles: [] }],
      by_role: {},
      employer_limit: true,
      employer_names: [],
      notes: [],
    },
    scoring: { sections_off: [], asks: [{ id: "team", section: "description", says: "The role leads a team", points: 2, checked_by: "reader" }], groups: [], suggestions: [], by_role: {}, notes: [], match_weight: 0.5 },
    settings: {
      comp_basis: "base", comp_assumptions: "", comp_tiers: "", comp_transferable_weight: 0.5, comp_stance_clamp: 0.1, comp_stance_min_siblings: 2,
      comp_likely_low: 0.9, comp_likely_high: 1.1, comp_round_to: null, comp_range_width: null,
      rules: [], notes: [],
    },
    versions: { criteria: 3, gates: 2, scoring: 5, settings: 1 },
    says: { gates: { location: "", compensation: "", other: {} }, asks: {} },
    ...extra,
  };
}

export function site(id: string, extra: Partial<Site> = {}): Site {
  return {
    id, url: `https://${id}.test`, label: `Board ${id}`, on: true, employer_id: null, state: "working", status: "Read nightly",
    skill: null, needs: null, roles: 3, list: "search", read_by: "app", read_found: null, read_note: null, created_at: "2026-10-01T00:00:00Z", ...extra,
  };
}

