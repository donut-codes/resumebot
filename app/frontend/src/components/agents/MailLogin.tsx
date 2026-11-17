import { useState } from "react";
import { api } from "../../api/client";
import type { MailLoginTest, MailProvider } from "../../api/types/agents";
import { useRefreshKey } from "../../lib/appContext";
import { useAction } from "../../lib/useAction";
import { useResource } from "../../lib/useResource";
import { Block } from "../focus/parts";
import ErrorBanner from "../primitives/ErrorBanner";

/** The provider an address belongs to, by its domain: the one that lists it, else the one that lists none ("Another"). */
export function providerFor(address: string, providers: MailProvider[]): MailProvider | null {
  const at = address.trim().toLowerCase().lastIndexOf("@");
  if (at < 0) return null;
  const domain = address.trim().toLowerCase().slice(at + 1);
  if (!domain) return null;
  return providers.find((p) => p.match.includes(domain)) ?? providers.find((p) => p.match.length === 0) ?? null;
}

/** Connect your email, so Go can read it for an application already sent. The password is written here and never
 * read back: once saved, the screen says only whom it is connected as. */
export default function MailLogin() {
  const refreshKey = useRefreshKey();
  const { data, setData } = useResource(() => api.mailLogin(), [refreshKey], { fallback: "Couldn't load the mail login." });
  const [address, setAddress] = useState("");
  const [password, setPassword] = useState("");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("");
  const [tried, setTried] = useState<MailLoginTest | null>(null);
  const provider = providerFor(address, data?.providers ?? []);
  const other = provider !== null && provider.match.length === 0;

  const save = useAction(
    async () => {
      const saved = await api.saveMailLogin({
        username: address.trim(),
        password,
        ...(other ? { host: host.trim(), ...(port.trim() ? { port: Number(port) } : {}) } : {}),
      });
      setData(saved);
      setPassword("");
      setTried(null);
    },
    { fallback: "Couldn't save the login." },
  );
  const test = useAction(async () => setTried(await api.testMailLogin()), { fallback: "Couldn't test the login." });
  const remove = useAction(
    async () => {
      setData(await api.removeMailLogin());
      setTried(null);
    },
    { fallback: "Couldn't remove the login." },
  );

  if (!data) return null;
  const busy = save.busy || test.busy || remove.busy;
  const ready = address.includes("@") && password.trim() !== "" && (!other || host.trim() !== "");

  return (
    <Block
      id="agents-mail"
      title="Your email"
      hint="So Launch in Browser can check your mail for an application you already sent. It only reads: nothing is sent, moved or marked."
    >
      <form
        className="mail-login"
        onSubmit={(e) => {
          e.preventDefault();
          if (ready && !busy) void save.run();
        }}
      >
        {data.connected && (
          <p className="mail-login-state" role="status">
            Connected as <strong>{data.username}</strong>
          </p>
        )}
        <div className="fi-field">
          <label htmlFor="mail-address">Email address</label>
          <input id="mail-address" className="fi-input" type="email" autoComplete="off" value={address} onChange={(e) => setAddress(e.target.value)} placeholder="you@example.com" />
          {provider && <span className="fi-note">{provider.match.length > 0 ? provider.name : "Another provider"}</span>}
        </div>
        {other && (
          <div className="mail-login-server">
            <div className="fi-field">
              <label htmlFor="mail-host">Mail server (IMAP address)</label>
              <input id="mail-host" className="fi-input" type="text" autoComplete="off" value={host} onChange={(e) => setHost(e.target.value)} placeholder="imap.example.com" />
            </div>
            <div className="fi-field">
              <label htmlFor="mail-port">Port</label>
              <input id="mail-port" className="fi-input" type="number" min={1} max={65535} value={port} onChange={(e) => setPort(e.target.value)} placeholder="993" />
            </div>
          </div>
        )}
        <div className="fi-field">
          <label htmlFor="mail-password">App password</label>
          <input
            id="mail-password"
            className="fi-input"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={data.connected ? "Paste a new one to replace it" : "Paste the app password"}
            aria-describedby="mail-steps"
          />
        </div>
        <div id="mail-steps" className="mail-login-steps">
          {provider ? (
            <>
              <p className="mail-login-steps-title">How to make an app password{provider.match.length > 0 ? ` for ${provider.name}` : ""}</p>
              <ol>
                {provider.steps.map((s) => (
                  <li key={s}>{s}</li>
                ))}
              </ol>
            </>
          ) : (
            <p className="fi-note">Type your address to see how to make an app password for it. It is never your account&rsquo;s own password.</p>
          )}
        </div>
        <div className="mail-login-buttons">
          <button type="submit" className="fi-btn fi-btn-go" disabled={!ready || busy}>
            {data.connected ? "Replace login" : "Save"}
          </button>
          {data.connected && (
            <>
              <button type="button" className="fi-btn" disabled={busy} onClick={() => void test.run()}>
                Test connection
              </button>
              <button type="button" className="fi-btn" disabled={busy} onClick={() => void remove.run()}>
                Remove
              </button>
            </>
          )}
        </div>
        {tried && (
          <p className="mail-login-result" role="status" data-ok={tried.ok}>
            {tried.message}
          </p>
        )}
        {(save.error || test.error || remove.error) && <ErrorBanner as="p">{save.error ?? test.error ?? remove.error}</ErrorBanner>}
      </form>
    </Block>
  );
}
