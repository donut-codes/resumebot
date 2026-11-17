import { fireEvent, screen, waitFor } from "@testing-library/react";
import type { MailLogin as MailLoginState, MailProvider } from "../../api/types/agents";
import { json, render, stubFetch } from "../../test-utils";
import MailLogin, { providerFor } from "./MailLogin";

const PROVIDERS: MailProvider[] = [
  { name: "Gmail", match: ["gmail.com"], host: "imap.gmail.com", port: 993, steps: ["Make an app password in the account's settings."] },
  { name: "Another provider", match: [], host: "", port: 993, steps: ["Look for IMAP settings in your provider's help."] },
];
const EMPTY: MailLoginState = { connected: false, username: null, host: null, port: null, provider: null, providers: PROVIDERS };
const CONNECTED: MailLoginState = { ...EMPTY, connected: true, username: "me@gmail.com", host: "imap.gmail.com", port: 993, provider: "Gmail" };

function stub(state: MailLoginState, answers: Record<string, unknown> = {}) {
  const calls: { method: string; url: string; body: unknown }[] = [];
  stubFetch((input: RequestInfo | URL, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ method, url: String(input), body: init?.body ? JSON.parse(String(init.body)) : null });
    return json(answers[`${method} ${String(input)}`] ?? state);
  });
  return calls;
}

describe("MailLogin", () => {
  it("finds the provider by the address's domain, and 'another' for one nobody lists", () => {
    expect(providerFor("me@GMAIL.com", PROVIDERS)?.name).toBe("Gmail");
    expect(providerFor("me@own.example", PROVIDERS)?.name).toBe("Another provider");
    expect(providerFor("no-at-sign", PROVIDERS)).toBeNull();
  });

  it("shows the provider's steps for the address and sends the password once, then shows only whom it is connected as", async () => {
    const calls = stub(EMPTY, { "PUT /api/v1/mail-login": CONNECTED });
    render(<MailLogin />);
    const address = await screen.findByLabelText("Email address");
    fireEvent.change(address, { target: { value: "me@gmail.com" } });
    expect(screen.getByText("Make an app password in the account's settings.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Mail server (IMAP address)")).toBeNull();
    const password = screen.getByLabelText("App password") as HTMLInputElement;
    expect(password.type).toBe("password");
    fireEvent.change(password, { target: { value: "abcd efgh" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("me@gmail.com")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "PUT")?.body).toEqual({ username: "me@gmail.com", password: "abcd efgh" });
    await waitFor(() => expect(password.value).toBe(""));
    expect(document.body.textContent).not.toContain("abcd");
  });

  it("asks for the server only for another provider", async () => {
    stub(EMPTY);
    render(<MailLogin />);
    fireEvent.change(await screen.findByLabelText("Email address"), { target: { value: "me@own.example" } });
    expect(screen.getByLabelText("Mail server (IMAP address)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("tests the connection in words, and removes the login", async () => {
    const calls = stub(CONNECTED, {
      "POST /api/v1/mail-login/test": { ok: true, message: "Signed in as me@gmail.com. It reads INBOX.", folders: ["INBOX"] },
      "DELETE /api/v1/mail-login": EMPTY,
    });
    render(<MailLogin />);
    fireEvent.click(await screen.findByRole("button", { name: "Test connection" }));
    expect(await screen.findByText(/It reads INBOX/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Remove" })).toBeNull());
    expect(calls.map((c) => c.method)).toContain("DELETE");
  });
});
