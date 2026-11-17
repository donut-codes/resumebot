import { elapsedInWords, humanize, lowercaseFirst, namesInWords, plural, wordFor } from "./text";

describe("humanize", () => {
  it("turns underscores into spaces and sentence-cases the result", () => {
    expect(humanize("work_mode")).toBe("Work mode");
  });

  it("sentence-cases a single word, and lowercases the rest", () => {
    expect(humanize("scout")).toBe("Scout");
    expect(humanize("WRITER")).toBe("Writer");
  });

  it("leaves an empty value alone", () => {
    expect(humanize("")).toBe("");
    expect(humanize("__")).toBe("__");
  });
});

describe("lowercaseFirst", () => {
  it("lowercases only the first letter", () => {
    expect(lowercaseFirst("Say pass or fail")).toBe("say pass or fail");
    expect(lowercaseFirst("")).toBe("");
    expect(lowercaseFirst("A")).toBe("a");
  });

  it("keeps an acronym, a start with a second capital, as it is", () => {
    expect(lowercaseFirst("CAPTCHA on every page.")).toBe("CAPTCHA on every page.");
    expect(lowercaseFirst("ACME's job list is behind a route.")).toBe("ACME's job list is behind a route.");
    expect(lowercaseFirst("It asks for a sign-in.")).toBe("it asks for a sign-in.");
    expect(lowercaseFirst("A-list site")).toBe("a-list site");
  });
});

describe("plural", () => {
  it("counts with the right form of the word", () => {
    expect(plural(1, "phrase")).toBe("1 phrase");
    expect(plural(0, "phrase")).toBe("0 phrases");
    expect(plural(22, "phrase")).toBe("22 phrases");
    expect(plural(2, "entry", "entries")).toBe("2 entries");
    expect(wordFor(1, "role")).toBe("role");
    expect(wordFor(3, "role")).toBe("roles");
  });
});

describe("words for a line", () => {
  it("names who has changes in a line of words", () => {
    expect(namesInWords([])).toBe("");
    expect(namesInWords(["Company"])).toBe("Company");
    expect(namesInWords(["Company", "Builders"])).toBe("Company and Builders");
    expect(namesInWords(["Company", "Builders", "Analysts"])).toBe("Company, Builders and Analysts");
    expect(namesInWords(["Company", "A", "B", "C", "D"])).toBe("Company, A, B and 2 more");
  });

  it("says how long a reading has gone on in words", () => {
    expect(elapsedInWords(0)).toBe("0 seconds");
    expect(elapsedInWords(1.9)).toBe("1 second");
    expect(elapsedInWords(59)).toBe("59 seconds");
    expect(elapsedInWords(60)).toBe("1 minute");
    expect(elapsedInWords(125)).toBe("2 minutes 5 seconds");
  });
});
