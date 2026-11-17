// The warning shown before the limit of two open applications per employer is turned off (docs/PRD.md 34.2): the owner's
// words, kept here once. It names two employers as examples, at the owner's direction, so the check of 2.8 allows this
// one text.

export const LIMIT_WARNING = {
  intro:
    "Applying to multiple roles at the same company can increase your visibility, but it can also make you look unfocused if not handled carefully.",
  heading: "Key Best Practices",
  points: [
    ["Limit your applications", "Apply to no more than 2 to 3 closely related positions within the same company to avoid looking unfocused or desperate."],
    ["Respect company limits", "Check for explicit rules before submitting. For example, one large employer limits applicants to three roles in a 30-day window, while another allows up to 10 active applications."],
    ["Tailor every submission", "Customize your resume and cover letter for each specific job description rather than sending a generic copy."],
    ["Target related fields", "Keep your applications within the same functional family (e.g., two different product management or data analysis roles) rather than jumping across unrelated departments like sales, engineering, and HR."],
    ["Avoid \"churning\"", "Do not spam-apply to every open role on the company's career page in a single day, as applicant tracking systems (ATS) and recruiters often flag this behavior."],
    ["Be transparent in interviews", "If a recruiter asks about your other applications, clearly explain your core skill set and how each role aligns with your overall career trajectory."],
  ] as const,
};
