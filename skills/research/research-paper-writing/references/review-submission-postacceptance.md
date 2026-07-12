## Phase 6: Self-Review & Revision

**Goal**: Simulate the review process before submission. Catch weaknesses early.

### Step 6.1: Simulate Reviews (Ensemble Pattern)

Generate reviews from multiple perspectives. The key insight from automated research pipelines (notably SakanaAI's AI-Scientist): **ensemble reviewing with a meta-reviewer produces far more calibrated feedback than a single review pass.**

**Step 1: Generate N independent reviews** (N=3-5)

Use different models or temperature settings. Each reviewer sees only the paper, not other reviews. **Default to negative bias** — LLMs have well-documented positivity bias in evaluation.

```
You are an expert reviewer for [VENUE]. You are critical and thorough.
If a paper has weaknesses or you are unsure about a claim, flag it clearly
and reflect that in your scores. Do not give the benefit of the doubt.

Review this paper according to the official reviewer guidelines. Evaluate:

1. Soundness (are claims well-supported? are baselines fair and strong?)
2. Clarity (is the paper well-written? could an expert reproduce it?)
3. Significance (does this matter to the community?)
4. Originality (new insights, not just incremental combination?)

Provide your review as structured JSON:
{
  "summary": "2-3 sentence summary",
  "strengths": ["strength 1", "strength 2", ...],
  "weaknesses": ["weakness 1 (most critical)", "weakness 2", ...],
  "questions": ["question for authors 1", ...],
  "missing_references": ["paper that should be cited", ...],
  "soundness": 1-4,
  "presentation": 1-4,
  "contribution": 1-4,
  "overall": 1-10,
  "confidence": 1-5
}
```

**Step 2: Meta-review (Area Chair aggregation)**

Feed all N reviews to a meta-reviewer:

```
You are an Area Chair at [VENUE]. You have received [N] independent reviews
of a paper. Your job is to:

1. Identify consensus strengths and weaknesses across reviewers
2. Resolve disagreements by examining the paper directly
3. Produce a meta-review that represents the aggregate judgment
4. Use AVERAGED numerical scores across all reviews

Be conservative: if reviewers disagree on whether a weakness is serious,
treat it as serious until the authors address it.

Reviews:
[review_1]
[review_2]
...
```

**Step 3: Reflection loop** (optional, 2-3 rounds)

Each reviewer can refine their review after seeing the meta-review. Use an early termination sentinel: if the reviewer responds "I am done" (no changes), stop iterating.

**Model selection for reviewing**: Reviewing is best done with the strongest available model, even if you wrote the paper with a cheaper one. The reviewer model should be chosen independently from the writing model.

**Few-shot calibration**: If available, include 1-2 real published reviews from the target venue as examples. This dramatically improves score calibration. See [references/reviewer-guidelines.md](references/reviewer-guidelines.md) for example reviews.

### Step 6.1b: Visual Review Pass (VLM)

Text-only review misses an entire class of problems: figure quality, layout issues, visual consistency. If you have access to a vision-capable model, run a separate **visual review** on the compiled PDF:

```
You are reviewing the visual presentation of this research paper PDF.
Check for:
1. Figure quality: Are plots readable? Labels legible? Colors distinguishable?
2. Figure-caption alignment: Does each caption accurately describe its figure?
3. Layout issues: Orphaned section headers, awkward page breaks, figures far from their references
4. Table formatting: Aligned columns, consistent decimal precision, bold for best results
5. Visual consistency: Same color scheme across all figures, consistent font sizes
6. Grayscale readability: Would the figures be understandable if printed in B&W?

For each issue, specify the page number and exact location.
```

This catches problems that text-based review cannot: a plot with illegible axis labels, a figure placed 3 pages from its first reference, inconsistent color palettes between Figure 2 and Figure 5, or a table that's clearly wider than the column width.

### Step 6.1c: Claim Verification Pass

After simulated reviews, run a separate verification pass. This catches factual errors that reviewers might miss:

```
Claim Verification Protocol:
1. Extract every factual claim from the paper (numbers, comparisons, trends)
2. For each claim, trace it to the specific experiment/result that supports it
3. Verify the number in the paper matches the actual result file
4. Flag any claim without a traceable source as [VERIFY]
```

For agent-based workflows: delegate verification to a **fresh sub-agent** that receives only the paper text and the raw result files. The fresh context prevents confirmation bias — the verifier doesn't "remember" what the results were supposed to be.

### Step 6.2: Prioritize Feedback

After collecting reviews, categorize:

| Priority | Action |
|----------|--------|
| **Critical** (technical flaw, missing baseline) | Must fix. May require new experiments → back to Phase 2 |
| **High** (clarity issue, missing ablation) | Should fix in this revision |
| **Medium** (minor writing issues, extra experiments) | Fix if time allows |
| **Low** (style preferences, tangential suggestions) | Note for future work |

### Step 6.3: Revision Cycle

For each critical/high issue:
1. Identify the specific section(s) affected
2. Draft the fix
3. Verify the fix doesn't break other claims
4. Update the paper
5. Re-check against the reviewer's concern

### Step 6.4: Rebuttal Writing

When responding to actual reviews (post-submission), rebuttals are a distinct skill from revision:

**Format**: Point-by-point. For each reviewer concern:
```
> R1-W1: "The paper lacks comparison with Method X."

We thank the reviewer for this suggestion. We have added a comparison with
Method X in Table 3 (revised). Our method outperforms X by 3.2pp on [metric]
(p<0.05). We note that X requires 2x our compute budget.
```

**Rules**:
- Address every concern — reviewers notice if you skip one
- Lead with the strongest responses
- Be concise and direct — reviewers read dozens of rebuttals
- Include new results if you ran experiments during the rebuttal period
- Never be defensive or dismissive, even of weak criticisms
- Use `latexdiff` to generate a marked-up PDF showing changes (see Professional LaTeX Tooling section)
- Thank reviewers for specific, actionable feedback (not generic praise)

**What NOT to do**: "We respectfully disagree" without evidence. "This is out of scope" without explanation. Ignoring a weakness by only responding to strengths.

### Step 6.5: Paper Evolution Tracking

Save snapshots at key milestones:
```
paper/
  paper.tex                    # Current working version
  paper_v1_first_draft.tex     # First complete draft
  paper_v2_post_review.tex     # After simulated review
  paper_v3_pre_submission.tex  # Final before submission
  paper_v4_camera_ready.tex    # Post-acceptance final
```

---

## Phase 7: Submission Preparation

**Goal**: Final checks, formatting, and submission.

### Step 7.1: Conference Checklist

Every venue has mandatory checklists. Complete them carefully — incomplete checklists can result in desk rejection.

See [references/checklists.md](references/checklists.md) for:
- NeurIPS 16-item paper checklist
- ICML broader impact + reproducibility
- ICLR LLM disclosure policy
- ACL mandatory limitations section
- Universal pre-submission checklist

### Step 7.2: Anonymization Checklist

Double-blind review means reviewers cannot know who wrote the paper. Check ALL of these:

```
Anonymization Checklist:
- [ ] No author names or affiliations anywhere in the PDF
- [ ] No acknowledgments section (add after acceptance)
- [ ] Self-citations written in third person: "Smith et al. [1] showed..." not "We previously showed [1]..."
- [ ] No GitHub/GitLab URLs pointing to your personal repos
- [ ] Use Anonymous GitHub (https://anonymous.4open.science/) for code links
- [ ] No institutional logos or identifiers in figures
- [ ] No file metadata containing author names (check PDF properties)
- [ ] No "our previous work" or "in our earlier paper" phrasing
- [ ] Dataset names don't reveal institution (rename if needed)
- [ ] Supplementary materials don't contain identifying information
```

**Common mistakes**: Git commit messages visible in supplementary code, watermarked figures from institutional tools, acknowledgments left in from a previous draft, arXiv preprint posted before anonymity period.

### Step 7.3: Formatting Verification

```
Pre-Submission Format Check:
- [ ] Page limit respected (excluding references and appendix)
- [ ] All figures are vector (PDF) or high-res raster (600 DPI PNG)
- [ ] All figures readable in grayscale
- [ ] All tables use booktabs
- [ ] References compile correctly (no "?" in citations)
- [ ] No overfull hboxes in critical areas
- [ ] Appendix clearly labeled and separated
- [ ] Required sections present (limitations, broader impact, etc.)
```

### Step 7.4: Pre-Compilation Validation

Run these automated checks **before** attempting `pdflatex`. Catching errors here is faster than debugging compiler output.

```bash
# 1. Lint with chktex (catches common LaTeX mistakes)
# Suppress noisy warnings: -n2 (sentence end), -n24 (parens), -n13 (intersentence), -n1 (command terminated)
chktex main.tex -q -n2 -n24 -n13 -n1

# 2. Verify all citations exist in .bib
# Extract \cite{...} from .tex, check each against .bib
python3 -c "
import re
tex = open('main.tex').read()
bib = open('references.bib').read()
cites = set(re.findall(r'\\\\cite[tp]?{([^}]+)}', tex))
for cite_group in cites:
    for cite in cite_group.split(','):
        cite = cite.strip()
        if cite and cite not in bib:
            print(f'WARNING: \\\\cite{{{cite}}} not found in references.bib')
"

# 3. Verify all referenced figures exist on disk
python3 -c "
import re, os
tex = open('main.tex').read()
figs = re.findall(r'\\\\includegraphics(?:\[.*?\])?{([^}]+)}', tex)
for fig in figs:
    if not os.path.exists(fig):
        print(f'WARNING: Figure file not found: {fig}')
"

# 4. Check for duplicate \label definitions
python3 -c "
import re
from collections import Counter
tex = open('main.tex').read()
labels = re.findall(r'\\\\label{([^}]+)}', tex)
dupes = {k: v for k, v in Counter(labels).items() if v > 1}
for label, count in dupes.items():
    print(f'WARNING: Duplicate label: {label} (appears {count} times)')
"
```

Fix any warnings before proceeding. For agent-based workflows: feed chktex output back to the agent with instructions to make minimal fixes.

### Step 7.5: Final Compilation

```bash
# Clean build
rm -f *.aux *.bbl *.blg *.log *.out *.pdf
latexmk -pdf main.tex

# Or manual (triple pdflatex + bibtex for cross-references)
pdflatex -interaction=nonstopmode main.tex
bibtex main
pdflatex -interaction=nonstopmode main.tex
pdflatex -interaction=nonstopmode main.tex

# Verify output exists and has content
ls -la main.pdf
```

**If compilation fails**: Parse the `.log` file for the first error. Common fixes:
- "Undefined control sequence" → missing package or typo in command name
- "Missing $ inserted" → math symbol outside math mode
- "File not found" → wrong figure path or missing .sty file
- "Citation undefined" → .bib entry missing or bibtex not run

### Step 7.6: Conference-Specific Requirements

| Venue | Special Requirements |
|-------|---------------------|
| **NeurIPS** | Paper checklist in appendix, lay summary if accepted |
| **ICML** | Broader Impact Statement (after conclusion, doesn't count toward limit) |
| **ICLR** | LLM disclosure required, reciprocal reviewing agreement |
| **ACL** | Mandatory Limitations section, Responsible NLP checklist |
| **AAAI** | Strict style file — no modifications whatsoever |
| **COLM** | Frame contribution for language model community |

### Step 7.7: Conference Resubmission & Format Conversion

When converting between venues, **never copy LaTeX preambles between templates**:

```bash
# 1. Start fresh with target template
cp -r templates/icml2026/ new_submission/

# 2. Copy ONLY content sections (not preamble)
#    - Abstract text, section content, figures, tables, bib entries

# 3. Adjust for page limits
# 4. Add venue-specific required sections
# 5. Update references
```

| From → To | Page Change | Key Adjustments |
|-----------|-------------|-----------------|
| NeurIPS → ICML | 9 → 8 | Cut 1 page, add Broader Impact |
| ICML → ICLR | 8 → 9 | Expand experiments, add LLM disclosure |
| NeurIPS → ACL | 9 → 8 | Restructure for NLP conventions, add Limitations |
| ICLR → AAAI | 9 → 7 | Significant cuts, strict style adherence |
| Any → COLM | varies → 9 | Reframe for language model focus |

When cutting pages: move proofs to appendix, condense related work, combine tables, use subfigures.
When expanding: add ablations, expand limitations, include additional baselines, add qualitative examples.

**After rejection**: Address reviewer concerns in the new version, but don't include a "changes" section or reference the previous submission (blind review).

### Step 7.8: Camera-Ready Preparation (Post-Acceptance)

After acceptance, prepare the camera-ready version:

```
Camera-Ready Checklist:
- [ ] De-anonymize: add author names, affiliations, email addresses
- [ ] Add Acknowledgments section (funding, compute grants, helpful reviewers)
- [ ] Add public code/data URL (real GitHub, not anonymous)
- [ ] Address any mandatory revisions from meta-reviewer
- [ ] Switch template to camera-ready mode (if applicable — e.g., AAAI \anon → \camera)
- [ ] Add copyright notice if required by venue
- [ ] Update any "anonymous" placeholders in text
- [ ] Verify final PDF compiles cleanly
- [ ] Check page limit for camera-ready (sometimes differs from submission)
- [ ] Upload supplementary materials (code, data, appendix) to venue portal
```

### Step 7.9: arXiv & Preprint Strategy

Posting to arXiv is standard practice in ML but has important timing and anonymity considerations.

**Timing decision tree:**

| Situation | Recommendation |
|-----------|---------------|
| Submitting to double-blind venue (NeurIPS, ICML, ACL) | Post to arXiv **after** submission deadline, not before. Posting before can technically violate anonymity policies, though enforcement varies. |
| Submitting to ICLR | ICLR explicitly allows arXiv posting before submission. But don't put author names in the submission itself. |
| Paper already on arXiv, submitting to new venue | Acceptable at most venues. Do NOT update arXiv version during review with changes that reference reviews. |
| Workshop paper | arXiv is fine at any time — workshops are typically not double-blind. |
| Want to establish priority | Post immediately if scooping is a concern — but accept the anonymity tradeoff. |

**arXiv category selection** (ML/AI papers):

| Category | Code | Best For |
|----------|------|----------|
| Machine Learning | `cs.LG` | General ML methods |
| Computation and Language | `cs.CL` | NLP, language models |
| Artificial Intelligence | `cs.AI` | Reasoning, planning, agents |
| Computer Vision | `cs.CV` | Vision models |
| Information Retrieval | `cs.IR` | Search, recommendation |

**List primary + 1-2 cross-listed categories.** More categories = more visibility, but only cross-list where genuinely relevant.

**Versioning strategy:**
- **v1**: Initial submission (matches conference submission)
- **v2**: Post-acceptance with camera-ready corrections (add "accepted at [Venue]" to abstract)
- Don't post v2 during the review period with changes that clearly respond to reviewer feedback

```bash
# Check if your paper's title is already taken on arXiv
# (before choosing a title)
pip install arxiv
python -c "
import arxiv
results = list(arxiv.Search(query='ti:\"Your Exact Title\"', max_results=5).results())
print(f'Found {len(results)} matches')
for r in results: print(f'  {r.title} ({r.published.year})')
"
```

### Step 7.10: Research Code Packaging

Releasing clean, runnable code significantly increases citations and reviewer trust. Package code alongside the camera-ready submission.

**Repository structure:**

```
your-method/
  README.md              # Setup, usage, reproduction instructions
  requirements.txt       # Or environment.yml for conda
  setup.py               # For pip-installable packages
  LICENSE                # MIT or Apache 2.0 recommended for research
  configs/               # Experiment configurations
  src/                   # Core method implementation
  scripts/               # Training, evaluation, analysis scripts
    train.py
    evaluate.py
    reproduce_table1.sh  # One script per main result
  data/                  # Small data or download scripts
    download_data.sh
  results/               # Expected outputs for verification
```

**README template for research code:**

```markdown
# [Paper Title]

Official implementation of "[Paper Title]" (Venue Year).

## Setup
[Exact commands to set up environment]

## Reproduction
To reproduce Table 1: `bash scripts/reproduce_table1.sh`
To reproduce Figure 2: `python scripts/make_figure2.py`

## Citation
[BibTeX entry]
```

**Pre-release checklist:**
```
- [ ] Code runs from a clean clone (test on fresh machine or Docker)
- [ ] All dependencies pinned to specific versions
- [ ] No hardcoded absolute paths
- [ ] No API keys, credentials, or personal data in repo
- [ ] README covers setup, reproduction, and citation
- [ ] LICENSE file present (MIT or Apache 2.0 for max reuse)
- [ ] Results are reproducible within expected variance
- [ ] .gitignore excludes data files, checkpoints, logs
```

**Anonymous code for submission** (before acceptance):
```bash
# Use Anonymous GitHub for double-blind review
# https://anonymous.4open.science/
# Upload your repo → get an anonymous URL → put in paper
```

---

## Phase 8: Post-Acceptance Deliverables

**Goal**: Maximize the impact of your accepted paper through presentation materials and community engagement.

### Step 8.1: Conference Poster

Most conferences require a poster session. Poster design principles:

| Element | Guideline |
|---------|-----------|
| **Size** | Check venue requirements (typically 24"x36" or A0 portrait/landscape) |
| **Content** | Title, authors, 1-sentence contribution, method figure, 2-3 key results, conclusion |
| **Flow** | Top-left to bottom-right (Z-pattern) or columnar |
| **Text** | Title readable at 3m, body at 1m. No full paragraphs — bullet points only. |
| **Figures** | Reuse paper figures at higher resolution. Enlarge key result. |

**Tools**: LaTeX (`beamerposter` package), PowerPoint/Keynote, Figma, Canva.

**Production**: Order 2+ weeks before the conference. Fabric posters are lighter for travel. Many conferences now support virtual/digital posters too.

### Step 8.2: Conference Talk / Spotlight

If awarded an oral or spotlight presentation:

| Talk Type | Duration | Content |
|-----------|----------|---------|
| **Spotlight** | 5 min | Problem, approach, one key result. Rehearse to exactly 5 minutes. |
| **Oral** | 15-20 min | Full story: problem, approach, key results, ablations, limitations. |
| **Workshop talk** | 10-15 min | Adapt based on workshop audience — may need more background. |

**Slide design rules:**
- One idea per slide
- Minimize text — speak the details, don't project them
- Animate key figures to build understanding step-by-step
- Include a "takeaway" slide at the end (single sentence contribution)
- Prepare backup slides for anticipated questions

### Step 8.3: Blog Post / Social Media

An accessible summary significantly increases impact:

- **Twitter/X thread**: 5-8 tweets. Lead with the result, not the method. Include Figure 1 and key result figure.
- **Blog post**: 800-1500 words. Written for ML practitioners, not reviewers. Skip formalism, emphasize intuition and practical implications.
- **Project page**: HTML page with abstract, figures, demo, code link, BibTeX. Use GitHub Pages.

**Timing**: Post within 1-2 days of paper appearing on proceedings or arXiv camera-ready.

---
