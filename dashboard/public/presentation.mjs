// Provenance follows the data, including when a saved snapshot is opened.
export function researchProvenance(snapshot, explicitDemo = false) {
  const synthetic = explicitDemo || snapshot?.mode === "synthetic-demo";
  return {
    synthetic,
    statusPrefix: synthetic ? "Synthetic demonstration · " : "",
    banner: synthetic
      ? "DEMONSTRATION DATA · Fictional prices and trades. This is a synthetic example, not observed market performance."
      : "",
  };
}
