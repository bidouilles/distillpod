// A research report, typeset.
//
// Deliberately a briefing note rather than a paper. The content is a model's
// synthesis of web sources, and dressing that as a journal article — abstract,
// authors, DOI — would claim a rigour it does not have. What it does have is
// worth typesetting properly: a claim, a verdict, evidence with numbered
// citations, and the questions left open.
//
// Everything comes from report.json alongside this file. Interpolated strings
// are inserted as text, never parsed as markup, so a quote containing #, [ or *
// cannot break the document — which matters when the input is arbitrary
// transcript and web-page titles.

#let d = json("report.json")

#let palette = (
  supported: rgb("#1a7f37"),
  mixed: rgb("#9a6700"),
  contested: rgb("#bc4c00"),
  unsupported: rgb("#cf222e"),
  no_evidence: rgb("#57606a"),
)
#let verdict-colour = palette.at(d.verdict, default: palette.no_evidence)

#set document(title: d.claim, author: "DistillPod")
#set page(
  paper: "a4",
  margin: (x: 2.2cm, y: 2.4cm),
  footer: context [
    #set text(8pt, fill: luma(45%))
    #d.episode_line
    #h(1fr)
    #counter(page).display("1 of 1", both: true)
  ],
)
#set text(font: ("Libertinus Serif", "New Computer Modern", "DejaVu Serif"), size: 10.5pt, lang: d.lang)
#set par(justify: true, leading: 0.62em)
#show heading: set block(above: 1.4em, below: 0.7em)
#show heading.where(level: 1): set text(size: 13pt)
#show heading.where(level: 2): set text(size: 11pt)
#show link: set text(fill: rgb("#0969da"))

// ── Masthead ────────────────────────────────────────────────────────────────
#block[
  #set text(8.5pt, fill: luma(40%))
  #smallcaps[DistillPod · research note] #h(1fr) #d.generated
]
#v(-0.4em)
#line(length: 100%, stroke: 0.5pt + luma(70%))
#v(0.8em)

#block[
  #set text(15pt, weight: "bold")
  #set par(justify: false, leading: 0.5em)
  #d.claim
]
#v(0.2em)
#block[
  #set text(9.5pt, fill: luma(35%))
  #d.episode_line
]

// ── Verdict ─────────────────────────────────────────────────────────────────
#v(0.9em)
#block(
  fill: verdict-colour.lighten(92%),
  stroke: (left: 2.5pt + verdict-colour),
  inset: (x: 12pt, y: 10pt),
  radius: 2pt,
  width: 100%,
)[
  #text(weight: "bold", size: 9pt, fill: verdict-colour)[#upper(d.verdict_label)]
  #v(0.35em)
  #set text(10pt)
  #d.verdict_note
]

// ── The moment being checked ────────────────────────────────────────────────
#if d.quote != "" [
  #v(0.9em)
  #block(inset: (left: 10pt), stroke: (left: 1pt + luma(80%)))[
    #set text(9.5pt, style: "italic", fill: luma(30%))
    #d.quote
  ]
]

// ── Findings ────────────────────────────────────────────────────────────────
#for section in d.sections [
  == #section.heading
  #section.body
]

#if d.open_questions.len() > 0 [
  == #d.labels.open_questions
  #for q in d.open_questions [
    - #q
  ]
]

#if d.echoes.len() > 0 [
  == #d.labels.echoes
  #for e in d.echoes [
    / #e.title: #text(fill: luma(40%))[#e.detail]
  ]
]

// ── Sources ─────────────────────────────────────────────────────────────────
#if d.sources.len() > 0 [
  == #d.labels.sources
  #set text(9pt)
  #for (i, s) in d.sources.enumerate() [
    #grid(columns: (1.2em, 1fr), gutter: 4pt,
      align(right)[#text(fill: luma(45%))[#(i + 1).]],
      [
        #link(s.url)[#s.title]
        #linebreak()
        #text(8.5pt, fill: luma(50%))[#s.meta]
      ],
    )
    #v(0.25em)
  ]
]

#v(1em)
#line(length: 100%, stroke: 0.5pt + luma(80%))
#v(0.4em)
#text(8pt, fill: luma(50%))[#d.footer]
