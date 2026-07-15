# 06 — The compliance subgraph

Activated only when `--pdd` is given (`route_compliance` on the `compose`
node). Source: `graph/compliance.py`, rendering in
`render/document.py: render_compliance`. It reuses everything the main
pipeline already built — narrative, summaries, inventory — which is why it
runs *after* `compose` instead of in parallel.

## Stage 1 — requirement extraction

`extract_requirements` turns free-form PDD prose into a typed checklist:

- The PDD is sliced into 20k-char chunks (`PDD_CHUNK_CHARS`) and each chunk
  goes through one structured call returning a `RequirementList`. Chunking is
  by size, not by heading — PDDs vary too much for reliable structural
  splitting, and extraction tolerates a requirement group spanning a cut far
  better than a prompt overflow.
- The extraction prompt defines what counts as a requirement ("anything the
  built automation could comply with or violate") and explicitly excludes
  document boilerplate (revision tables, org charts), because that is what
  PDDs are mostly made of.
- Post-processing is deterministic: near-duplicate texts are dropped
  (first 120 lowercase chars as key — duplicates across chunk boundaries are
  common), IDs are renumbered `R-01, R-02, ...` in document order regardless
  of what the model produced, and the list is capped at `MAX_REQUIREMENTS`
  (60) with a warning.

Each `Requirement` carries a `kind` (functional / exception-handling /
reporting / non-functional / other) — shown to the verifier as a hint of what
kind of evidence would settle it.

## Stage 2 — parallel isolated verification

`dispatch_verify` builds one `VerifyPayload` per requirement; `route_verify`
fans them out with `Send`, exactly like the map phase (same reducer pattern
on `state["compliance"]`).

Each payload's context is assembled from three bounded pieces:

1. the process narrative (first 6k chars),
2. the **4 most relevant workflow summaries** (`RELEVANT_SUMMARIES`),
3. a 40-entry config excerpt.

Relevance is word-overlap scoring (`_relevant_summaries`): tokenize the
requirement (`[A-Za-z][A-Za-z0-9_]{3,}` — identifiers and words length >= 4),
tokenize each summary (purpose + steps + external systems), rank by
intersection size. Deliberately primitive — no embeddings (decision D2), fully
explainable ("these workflows shared these words"), and its failure mode is
benign: a bad selection produces `Not verifiable`, which triggers stage 3
instead of a wrong verdict.

The verifier (`worker` role — many small calls) must return one of four
verdicts with a justification, evidence paths and, when not fully compliant,
an explicit `gap`. The prompt pins the verdict semantics ("Compliant: the
implementation clearly covers the requirement...") so the four labels mean the
same thing across requirements and models.

## Stage 3 — evidence rescue

`evidence_rescue` selects every verdict that *needs* evidence (capped at 8):
the `Not verifiable` subset, plus any `Compliant` / `Partially compliant`
verdict whose cited evidence does not survive validation against the
inventory (`_filter_evidence`, D11) — a claim of compliance backed by nothing,
or by an invented path, is exactly the hallucination an audit must not ship.
Each selected verdict gets a second chance with real evidence gathering:

1. A fresh tool loop (`VERIFY_TOOL_SYSTEM`, `lead` role) searches the project
   for anything relevant to the requirement — search first, read selectively,
   report plainly if nothing exists. Evidence recorder attached (D8).
2. A second structured verification call re-judges the requirement given the
   found evidence and the list of workflows actually consulted; the recorder's
   paths fill `evidence` if the model returned none.

The two-step design (agentic *evidence gathering*, then separate *judgment*)
keeps the verdict call clean: the judge sees a compact evidence statement, not
a 15-message exploration transcript. Requirements the rescue still cannot
settle remain honestly `Not verifiable` — the report tells the reader to check
those manually, which is the correct behavior for an auditor.

## Stage 4 — the report

`render_compliance` is fully deterministic:

- **Scope and method** — states what was assessed and, importantly, what was
  not: "Runtime behavior was not executed as part of this assessment." A
  static-analysis compliance report must say so.
- **Verdict summary** — counts per verdict.
- **Requirements traceability matrix** — one row per requirement:
  ID, condensed text, verdict, evidence paths. Cited evidence is filtered
  through `_filter_evidence` first: paths are canonicalized (separators,
  backticks, a missing `.xaml` extension) and anything that does not exist in
  the inventory is dropped — the report never points the reader at a file
  that is not there. Requirements that never got a verdict (extraction
  produced them but verification failed) show as `Not assessed` rather than
  disappearing.
- **Gaps and deviations** — a detail block (justification + gap) for every
  non-`Compliant` item.
- **Conclusion** — computed sentence: "N of M extracted requirements are fully
  compliant (...) Items listed under Gaps and deviations should be reviewed
  with the process owner before sign-off."

The traceability-matrix shape follows standard requirements-engineering
practice (see [requirements traceability](https://en.wikipedia.org/wiki/Requirements_traceability)):
every verdict is linked back to its requirement and forward to the
implementing artifacts, so a reviewer can audit any row independently.

## Failure modes, end to end

| Failure | Behavior |
|---------|----------|
| Extraction call fails on a chunk | warning; other chunks still contribute |
| Extraction returns garbage | dedup + renumber still yield a clean (possibly short) list |
| A verification call fails | that requirement becomes `Not verifiable` + warning; run continues |
| Rescue tool loop fails | warning; original `Not verifiable` verdict stands |
| Model cites no evidence after rescue | recorder paths are used |
| Compliant verdict cites an invented path | citation dropped from the matrix; verdict re-checked by the rescue pass |
| PDD has no verifiable requirements | report says exactly that in the conclusion |
