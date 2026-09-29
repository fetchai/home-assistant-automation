// The +/- rendering of two YAML configs, in one place.
//
// It was written inside useChat for the approval modal, and the Suggestions tab
// needs exactly the same thing for the config the journal recorded when the
// agent raised its proposal. Two copies would be two ideas of what counts as a
// changed line, on one screen — so it moved here rather than being written out
// again.
//
// Deliberately naive: line-set membership, not a real LCS diff. An automation
// config is short, the panel's job is to show what the approval covers rather
// than to be a merge tool, and a moved line reading as -/+ is honest about the
// text having changed.

export function diffLines(oldYaml, newYaml) {
  const oldLines = (oldYaml || "").split("\n");
  const newLines = (newYaml || "").split("\n");
  const oldSet = new Set(oldLines);
  const newSet = new Set(newLines);
  const result = [];
  for (const line of oldLines) {
    result.push(newSet.has(line)
      ? { type: "context", sign: " ", text: line }
      : { type: "removed", sign: "-", text: line });
  }
  for (const line of newLines) {
    if (!oldSet.has(line)) result.push({ type: "added", sign: "+", text: line });
  }
  return result;
}

export function diffStats(oldYaml, newYaml) {
  const oldLines = oldYaml ? oldYaml.split("\n").filter(Boolean) : [];
  const newLines = newYaml ? newYaml.split("\n").filter(Boolean) : [];
  const oldSet = new Set(oldLines);
  const newSet = new Set(newLines);
  return {
    added: newLines.filter(line => !oldSet.has(line)).length,
    removed: oldLines.filter(line => !newSet.has(line)).length,
  };
}

// A config with nothing before it: every line added. Not `diffLines("", yaml)`,
// which reads the empty old side as one empty line and prints it as removed —
// a "-" on a card proposing something new would claim a line is being taken
// away.
export function addedLines(newYaml) {
  return (newYaml || "").split("\n").filter(Boolean)
    .map(line => ({ type: "added", sign: "+", text: line }));
}
