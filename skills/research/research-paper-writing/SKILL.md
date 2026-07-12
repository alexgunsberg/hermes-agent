---
name: research-paper-writing
title: Research Paper Writing Pipeline
description: "Write ML papers for NeurIPS/ICML/ICLR: design→submit."
version: 1.2.0
author: Orchestra Research
license: MIT
dependencies: [semanticscholar, arxiv, habanero, requests, scipy, numpy, matplotlib, SciencePlots]
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Research, Paper Writing, Experiments, ML, AI, NeurIPS, ICML, ICLR, ACL, AAAI, COLM, LaTeX, Citations, Statistical Analysis]
    category: research
    related_skills: [arxiv, ml-paper-writing, subagent-driven-development, plan]
    requires_toolsets: [terminal, files]
---

# Research Paper Writing Pipeline

Use this skill for end-to-end ML/AI research: contribution framing,
literature review, experiment design and execution, statistical analysis,
drafting, review, venue conversion, submission, and post-acceptance material.
The process is iterative: results can return work to experiment design, and
reviews can trigger new analysis or experiments.

## Non-negotiable principles

1. Never hallucinate citations. Fetch and verify every citation; mark anything
   unverified as `[CITATION NEEDED]`.
2. State the paper's contribution in one sentence. Experiments must map to a
   claim in that contribution.
3. Draft proactively, flag uncertainties in the draft, and block only on a
   choice that materially changes the work.
4. Commit experiments and draft milestones with descriptive messages.
5. Report negative/null results honestly and separate observed facts from
   interpretation.
6. Validate statistics, figures, citations, LaTeX, anonymization, page limits,
   ethics requirements, and reproducibility artifacts before submission.

## Required reference routing

Read this file completely, then load the references needed for the current
phase. Never load every reference by default.

- Project setup, literature review, experiment design/execution, monitoring,
  and analysis (phases 0–4): `references/phases-0-to-4.md`
- Narrative, abstract, introduction, methods, results, related work,
  limitations, and drafting workflow: `references/phase-5-drafting.md`
- Templates, tables, figures, LaTeX packages, diagrams, diffs, and plotting:
  `references/latex-and-figures.md`
- Self-review, rebuttal, submission, anonymization, camera-ready, arXiv, code
  packaging, posters, and talks: `references/review-submission-postacceptance.md`
- Workshop/short/theory/survey/benchmark/position papers and Hermes tool
  integration: `references/paper-types-and-hermes.md`
- Citation verification details: `references/citation-workflow.md`
- Experiment implementation patterns: `references/experiment-patterns.md`
- Human evaluation: `references/human-evaluation.md`
- Reviewer simulation and criteria: `references/reviewer-guidelines.md`
- Checklists: `references/checklists.md`
- Writing guidance and source evidence: `references/writing-guide.md` and
  `references/sources.md`
- Iterative generation/evaluation method: `references/autoreason-methodology.md`
- Paper-type comparison: `references/paper-types.md`

## Minimal workflow

1. Inspect the repository, existing results, venue constraints, and authorship
   state. Create a TODO list and experiment journal.
2. Frame one contribution sentence and map each claim to evidence.
3. Search broadly, then deepen; verify BibTeX/DOIs programmatically.
4. Design baselines, ablations, evaluation protocol, seeds, and compute budget.
5. Run monitored experiments, preserve failures, and aggregate with appropriate
   uncertainty/significance tests.
6. Write an experiment log, then draft around the evidence and contribution.
7. Run independent content, visual, claim, citation, and compilation reviews.
8. Apply the venue checklist and produce reproducible submission artifacts.

For any external side effect or expensive experiment, surface the concrete
plan and cost/risk before execution. For ambiguous scientific decisions, ask
one or two targeted questions while continuing all unblocked drafting work.
