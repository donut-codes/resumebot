// resumebot's mark: a 16-by-16 pixel robot built for its one job. Its head is a visor; its body is a page. The
// antenna lamp blinks while it works. Drawn as one rect per pixel so it stays crisp at any whole-number size.
//
// The agents are resumebot in disguise, an outfit painted over the same robot (it fools nobody): the scout in a pith
// helmet, the analyst in a deerstalker with a magnifying glass, the writer in a beret and a stuck-on mustache. The lamp
// always pokes through the hat on a steel stem, so it shows on any ground and can still blink.

// One row per line, one character per pixel: o outline, s steel, v visor, w white (the eyes and the page), r lamp.
const SPRITE = [
  ".......rr.......",
  ".......oo.......",
  "....oooooooo....",
  "...osssssssso...",
  "...osvvvvvvso...",
  "...osvwvvwvso...",
  "...osssssssso...",
  "....oooooooo....",
  "..oowwwwwwwwoo..",
  ".osowoooooowoso.",
  ".osowwwwwwwwoso.",
  "..oowoooowwwoo..",
  "...owwwwwwwwo...",
  "...owoooooowo...",
  "...oooooooooo...",
  "...oo......oo...",
];

// Each outfit paints over the robot: "." keeps the robot's pixel. k khaki and d band (pith helmet), t tweed and
// T its check (deerstalker), g brass (the glass's rim), b beret, m mustache.
const OUTFITS = {
  scout: [
    "................",
    "....kkksskkk....",
    "...dddddddddd...",
    ".kkkkkkkkkkkkkk.",
  ],
  analyst: [
    "................",
    "....tTtsstTt....",
    "....tTtTtTtTt...",
    "..tttttttttttt..",
    "...T........T...",
    "...t........t...",
    ".............ggg",
    ".............gvg",
    ".............ggg",
    "............t...",
  ],
  writer: [
    "................",
    "......bss.......",
    "..bbbbbbbbbb....",
    ".bbbbbbbbbbbb...",
    "................",
    "....m......m....",
    "....mmmmmmmm....",
  ],
} as const;

export type Outfit = keyof typeof OUTFITS;

const FILL: Record<string, string> = {
  o: "var(--fi-bot-outline)",
  s: "var(--fi-bot-steel)",
  v: "var(--fi-signal)",
  w: "var(--fi-bot-page)",
  k: "var(--fi-bot-khaki)",
  d: "var(--fi-bot-band)",
  t: "var(--fi-bot-tweed)",
  T: "var(--fi-bot-tweed-check)",
  g: "var(--fi-bot-brass)",
  b: "var(--fi-bot-beret)",
  m: "var(--fi-bot-mustache)",
};

/** The robot, dressed: each row of the outfit paints over the robot's row where it isn't ".". */
function dressed(outfit: Outfit | undefined): string[] {
  if (!outfit) return SPRITE;
  const over = OUTFITS[outfit];
  return SPRITE.map((row, y) => {
    const paint = over[y];
    if (!paint) return row;
    return [...row].map((c, x) => (paint[x] && paint[x] !== "." ? paint[x] : c)).join("");
  });
}

interface Props {
  /** Drawn size in pixels; a multiple of 16 keeps every pixel square. */
  size?: number;
  /** It is working: the lamp blinks. */
  active?: boolean;
  /** Which agent it is dressed as; plain resumebot without one. */
  outfit?: Outfit;
  className?: string;
}

export default function BotMark({ size = 32, active = false, outfit, className }: Props) {
  const cells: React.ReactNode[] = [];
  dressed(outfit).forEach((row, y) => {
    for (let x = 0; x < row.length; x++) {
      const c = row[x];
      if (c === ".") continue;
      cells.push(
        <rect
          key={`${x}-${y}`}
          x={x}
          y={y}
          width={1}
          height={1}
          fill={c === "r" ? undefined : FILL[c]}
          className={c === "r" ? "bot-lamp" : undefined}
        />,
      );
    }
  });
  return (
    <svg
      className={["bot-mark", active ? "is-active" : "", className ?? ""].filter(Boolean).join(" ")}
      width={size}
      height={size}
      viewBox="0 0 16 16"
      shapeRendering="crispEdges"
      aria-hidden="true"
      focusable="false"
    >
      {cells}
    </svg>
  );
}
