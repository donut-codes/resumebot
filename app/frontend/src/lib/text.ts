// Small display-text helpers. Values POSTed to the API stay raw; these only touch what's rendered (plain
// sentences, sentence case).

/** underscores -> spaces, sentence case: "work_mode" -> "Work mode", "scout" -> "Scout". */
export function humanize(value: string): string {
  if (!value) return value;
  const words = value.split("_").filter(Boolean);
  if (!words.length) return value;
  const sentence = words.join(" ").toLowerCase();
  return sentence.charAt(0).toUpperCase() + sentence.slice(1);
}

/** "Say pass or fail" -> "say pass or fail", for joining step titles into
 * one sentence ("Perform the check, then say pass or fail"), or for reading a note after other words ("Found out: it asks
 * for a sign-in."). A start with a second capital ("CAPTCHA on every page", "ACME's list") is an acronym and is kept as
 * it is, as the server's `lower_first` keeps it. */
export function lowercaseFirst(s: string): string {
  if (s.length > 1 && s[1] !== s[1].toLowerCase()) return s;
  return s.length ? s[0].toLowerCase() + s.slice(1) : s;
}

/** The right form of a word for a count: "role" for 1, "roles" otherwise (or `many`, for a word that does not add an s). */
export function wordFor(n: number, one: string, many = `${one}s`): string {
  return n === 1 ? one : many;
}

/** A count with its word: "1 phrase", "22 phrases". */
export function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${wordFor(n, one, many)}`;
}

/** Names in a line of words: "Company", "Company and Builders", "Company, Builders and Analysts", and past three the
 * rest counted ("Company, Builders, Analysts and 2 more"). */
export function namesInWords(names: string[]): string {
  if (names.length <= 1) return names[0] ?? "";
  if (names.length === 2) return `${names[0]} and ${names[1]}`;
  if (names.length === 3) return `${names[0]}, ${names[1]} and ${names[2]}`;
  return `${names.slice(0, 3).join(", ")} and ${names.length - 3} more`;
}

/** How long a reading has gone on, in words: "7 seconds", "1 minute 5 seconds". */
export function elapsedInWords(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return plural(s, "second");
  const m = Math.floor(s / 60);
  return s % 60 === 0 ? plural(m, "minute") : `${plural(m, "minute")} ${plural(s % 60, "second")}`;
}
