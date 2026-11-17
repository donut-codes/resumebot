import { fractionOf, percentOf } from "./percent";

describe("fractionOf and percentOf", () => {
  it("move the decimal point, so the value kept is exactly the one the digits say", () => {
    // The ones multiplying or dividing by 100 drifts on: 0.57 x 100 is 56.99999999999999, 0.07 x 100 is 7.000000000000001.
    expect(fractionOf("57")).toBe(0.57);
    expect(percentOf(0.57)).toBe(57);
    expect(percentOf(0.07)).toBe(7);
    expect(percentOf(0.29)).toBe(29);
    expect(percentOf(0.58)).toBe(58);
    // The three the owner's spec names.
    expect(percentOf(0.5)).toBe(50);
    expect(percentOf(0.1)).toBe(10);
    expect(percentOf(0.85)).toBe(85);
    expect(fractionOf("50")).toBe(0.5);
    expect(fractionOf("10")).toBe(0.1);
    expect(fractionOf("85")).toBe(0.85);
  });

  it("read a share with a decimal part, a share over 100%, and 0", () => {
    expect(fractionOf("62.5")).toBe(0.625);
    expect(percentOf(0.625)).toBe(62.5);
    expect(fractionOf("110")).toBe(1.1);
    expect(percentOf(1.1)).toBe(110);
    expect(fractionOf("0")).toBe(0);
    expect(percentOf(0)).toBe(0);
    expect(fractionOf("5")).toBe(0.05);
    expect(fractionOf(".5")).toBe(0.005);
    expect(fractionOf("-5")).toBe(-0.05);
    expect(fractionOf("1e2")).toBe(1);
  });

  it("are not a number when the text is not one", () => {
    expect(fractionOf("")).toBeNaN();
    expect(fractionOf("  ")).toBeNaN();
    expect(fractionOf("abc")).toBeNaN();
  });

  it("round-trip every tenth of a percent from 0 to 150 with no drift, in both directions", () => {
    for (let tenths = 0; tenths <= 1500; tenths++) {
      const percent = String(tenths / 10);
      // A fraction is what the digits say: "57.3" is the number 0.573 as the parser reads it from "57.3e-2".
      expect(fractionOf(percent)).toBe(Number(`${percent}e-2`));
      expect(percentOf(fractionOf(percent))).toBe(Number(percent));
    }
  });
});
