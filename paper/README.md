# Paper build

This directory targets the unmodified official ICLR 2027 LaTeX style. The review
submission keeps `\iclrfinalcopy` commented and contains no authors, affiliations,
acknowledgments, identifying repository links, machine paths, or non-anonymous artifact URLs.

The manuscript deliberately contains visible `\pending{...}` markers. They are claim gates,
not typesetting placeholders: the paper is not submission-ready until every marker is replaced
from a freshly audited aggregate export and `research/claims_ledger.md` has a matching evidence
anchor.

The following files are vendored byte-for-byte from the official style archive:

```text
paper/iclr2027_conference.sty
paper/iclr2027_conference.bst
paper/natbib.sty
paper/fancyhdr.sty
```

Build from `paper/` with:

```bash
latexmk -pdf -interaction=nonstopmode -halt-on-error main.tex
```

CI must also verify the vendored style hashes against the pinned archive and fail on any
visible `\pending{}` marker in a release build.

```bash
sha256sum --check official_style.sha256
```

Before release:

1. confirm the main text ends within nine pages, excluding references, the required AI-use
   statement, the reproducibility statement, a relevant ethics statement, and appendices;
2. inspect every figure at 100% and in grayscale;
3. run the anonymous-artifact and secret/path scans;
4. regenerate tables and figures from aggregate result files, never by hand;
5. verify that citations, model names, dates, split sizes, and hashes match source artifacts.
