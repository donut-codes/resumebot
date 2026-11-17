import ItemList, { nameOf } from "./ItemList";
import { Text } from "../primitives/fields";
import { BLANK_RULE } from "./meParts";

interface Props {
  items: { id: string; says: string }[];
  onChange: (items: { id: string; says: string }[]) => void;
  /** What one is called: "rule", "step". The add button reads "Add a rule". */
  noun: string;
  /** The label of each line for a screen reader, numbered: "Voice rule 1". */
  label: string;
}

/** A list of one-point rules, one line each, with Add and Remove. */
export default function RuleList({ items, onChange, noun, label }: Props) {
  return (
    <ItemList
      items={items}
      onChange={onChange}
      noun={noun}
      blank={BLANK_RULE}
      describe={(r) => nameOf(r.says, `a new ${noun}`)}
      render={(r, i, update) => <Text label={`${label} ${i + 1}`} hideLabel value={r.says} onChange={(says) => update({ says })} />}
    />
  );
}
