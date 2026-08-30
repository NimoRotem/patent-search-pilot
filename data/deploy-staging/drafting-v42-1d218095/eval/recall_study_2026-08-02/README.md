# Recall study, 2026-08-02: the measurement harness

These are the one-off probes behind every number in `RECALL_STUDY_2026-08-02.md`. They are kept
because that study changed the architecture, and a measurement whose harness was thrown away is an
assertion rather than a result.

They are **probes, not tooling**. Absolute paths are pinned to the instance-3 checkout, they read
the live corpus directly (reachable only from that host), and several of them spend real model
calls. Run one with `.venv/bin/python eval/recall_study_2026-08-02/expN.py`.

| script | what it measured |
|---|---|
| `exp1.py` | best-chunk cosine for the two named references, and how deep the dense funnel actually reaches |
| `exp2.py` | the rank of a named reference under six different query formulations |
| `exp3.py` | CPC-scoped retrieval, and RRF fusion over many short queries against one long one |
| `exp4.py` | the same reference judged from a 900-character snippet against its full text, plus a wide cheap screen |
| `exp6.py` | score against text budget (900 chars / all claims / full text), and a deep field-scoped funnel |
| `exp7.py` | how widespread the wrong-abstract data hazard is |
| `exp8.py` | the honest US-only prevalence, and the document-chunk channel under three pooling rules |
| `exp9.py` | the first end-to-end prototype of the proposed pipeline, including what it got wrong |
| `exp10.py` | grounded evidence counting against a free-form score, and citation expansion |
| `exp11.py` | the recommended change applied to the LIVE candidate list. This is the headline result |
| `exp12.py` | which features each named reference actually grounds |
| `exp13.py` | whether the product's grounding gate passes German quotes. It does; the probe was the thing that was wrong |

Two of these exist only because a probe was the defect rather than the product: `exp13.py` was
written after a naive exact-substring grounding check wrongly reported the shipped fuzzy gate as
broken, and `exp9.py` records a prototype that scored nine text-less records at 85 out of 100.
Both are here on purpose.
