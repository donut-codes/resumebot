import { useState } from "react";
import { api } from "../../api/client";
import type { HarnessProcess } from "../../api/types/agents";
import { BpmnProcessView } from "process-view";
// The package's editor and step-detail styles: its barrel imports them, but a build that uses only part of the package
// drops that import, so this view brings them itself.
import "../../../../packages/process-view/src/editor.css";
import { useAction } from "../../lib/useAction";
import { useResource } from "../../lib/useResource";
import { Block } from "../focus/parts";
import ErrorBanner from "../primitives/ErrorBanner";
import Loading from "../primitives/Loading";
import VersionList from "./VersionList";

/** A process's versions, newest first (docs/PRD.md 16.2): the file as it first shipped is version 1, the owner's saves
 * are the next ones. An older one can be looked at, and "Restore this version" saves it as a new version. */
export default function ProcessVersions({
  process,
  onChanged,
}: {
  process: HarnessProcess;
  /** A version was restored: the page reads the process again. */
  onChanged: () => void;
}) {
  const { data, error, reload } = useResource(
    () => api.processVersions(process.id),
    // The version in use is in the key: a save or a restore reads the history again.
    [process.id, process.version],
    { fallback: "Couldn't load the versions." },
  );
  const [viewing, setViewing] = useState<number | null>(null);
  const [xml, setXml] = useState<string | null>(null);
  const look = useAction(
    async (number: number) => {
      if (viewing === number) {
        setViewing(null);
        return;
      }
      setXml(null);
      setViewing(number);
      setXml((await api.processVersionFile(process.id, number)).xml);
    },
    { fallback: "Couldn't load that version." },
  );
  const restore = useAction(
    async (number: number) => {
      await api.restoreProcess(process.id, number);
      setViewing(null);
      reload();
      onChanged();
    },
    { fallback: "Couldn't bring that version back." },
  );
  return (
    <Block
      id="how-versions"
      title="Versions"
      count={data?.versions.length}
      hint="Each save is a version, and so is each restore. The next run uses the one in use; a run going now finishes on the version it started with."
    >
      <div className="how-versions">
        {error && <ErrorBanner as="p">{error}</ErrorBanner>}
        {look.error && <ErrorBanner as="p">{look.error}</ErrorBanner>}
        {restore.error && <ErrorBanner as="p">{restore.error}</ErrorBanner>}
        {!data && !error && <Loading>Loading&hellip;</Loading>}
        {data?.running.note && <p className="fi-note">{data.running.note}</p>}
        {data && (
          <VersionList
            versions={data.versions}
            busy={restore.busy}
            viewing={viewing}
            onView={(n) => look.run(n)}
            onRestore={(n) => restore.run(n)}
            label={`Versions of ${process.name}`}
            viewed={
              xml === null ? (
                <Loading>Loading&hellip;</Loading>
              ) : (
                <BpmnProcessView key={viewing ?? 0} bpmn={xml} live={false} performerOnRows />
              )
            }
          />
        )}
      </div>
    </Block>
  );
}
