# ICLR 2027 submission checklist

Status: working compliance ledger, verified against the live ICLR 2027 conference pages on
2026-08-15. Recheck the official call and FAQ immediately before each deadline; the official
site controls if any local note differs.

Official sources:

- <https://iclr.cc/Conferences/2027/CallForPapers>
- <https://iclr.cc/Conferences/2027/AuthorGuidelines>
- <https://iclr.cc/Conferences/2027/FAQ>
- <https://iclr.cc/Conferences/2027/AIPolicyForAuthors>

## Deadlines (Anywhere on Earth)

- abstract submission: 2026-09-18;
- full-paper submission: 2026-09-25;
- reviews released / discussion opens: 2026-11-05;
- discussion closes: 2026-11-18;
- decision notification: 2026-12-16.

No author may be added or removed after the abstract deadline. Freeze the complete author set
before 2026-09-18; author order may still change until the full-paper deadline, after which no
author changes are permitted. Verify valid OpenReview profiles, institutional domains,
conflicts, paper-count quotas, and reciprocal-reviewing obligations for every author. Treat
profile completion before the abstract deadline as the operational gate even though the FAQ
allows account-linkage fixes until the full-paper deadline.

## Manuscript

- Use the vendored, hash-pinned official ICLR 2027 style without modification.
- Review submission main text is at most 9 pages; rebuttal/camera-ready allowance is not used
  to justify an over-length initial submission.
- Keep `\iclrfinalcopy` disabled and all authors/affiliations/acknowledgments hidden.
- Include the required AI-use statement and keep it consistent with the OpenReview form. The
  statement must disclose all applicable required categories in the 2027 AI policy, including
  conceptualization, hypothesis/experiment design, code or artifact creation, literature work,
  paper drafting/editing, titles, references, and synthetic-data generation; explicitly mark
  required categories that are not applicable when needed for clarity.
- Include reproducibility and relevant ethics statements; verify how the current official
  style counts each statement before the final page audit.
- No unsupported quantitative claim, hand-entered result, or visible `\pending{}` marker.
- Every citation is checked against the source; arXiv entries are upgraded to proceedings
  metadata when available.
- Figures remain legible at 100% and in grayscale, use redundant shape/line encodings, and
  contain no identifying paths or metadata.

## Double blind

- Anonymity covers the PDF, supplement, code, data manifests, workbook metadata, filenames,
  logs, and external links.
- Do not link the public development repository from the review submission.
- Produce a new anonymous archive without Git history, remotes, usernames, hostnames, home
  paths, API endpoints, credentials, identifying trajectories, acknowledgments, or grant text.
- Strip Office core properties such as creator/lastModifiedBy and scan embedded objects,
  comments, custom XML, images, and document properties.
- Anonymous hosted links must not use visitor analytics or reviewer tracking; prefer the
  OpenReview supplement archive.

## Evidence and reproducibility gates

- Build from the exact clean, published source commit recorded in every run manifest.
- Export de-identified task-by-arm-by-seed rows, not only aggregate means.
- Regenerate all tables and figures from one freshly audited export.
- Verify every result row's task/input/output/scorer-copy hashes, terminal status, observer
  finalization-record consistency, scorer version, and zero outcome-dependent retry. Verify
  accepted-deliverable certificates and scoring replicas only for accepted-candidate rows;
  audited noncompletions must claim neither.
- Include immutable split manifests, environment/container lock, model checkpoint/revision,
  decoding and resource ceilings, hardware/backend versions, seeds, and exact commands.
- If dataset licenses prohibit workbook redistribution, publish acquisition instructions and
  expected hashes rather than copying restricted files.
- Run secret, absolute-path, identity, remote, Office-metadata, symlink, and archive-content
  scans on the final supplement itself.

## Release build gates

```bash
cd paper
sha256sum --check official_style.sha256
latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex
```

The release CI must additionally fail on modified style hashes, undefined references,
overfull boxes in figures/tables, main-text overflow, visible pending markers, inconsistent
claim-ledger anchors, or anonymous-artifact scan findings.
